"""Изолированный запуск обнаружения: сохранённый корпус либо явный --live."""
import argparse
import asyncio
import csv
import hashlib
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone

from .structure import DiscoveryLimits, StructureDiscovery
from .resolver import ProfileResolver
from . import resolver as resolver_module
from config import Config
from web_crawler import WebCrawler
from fetch_outcome import FetchOutcome
from product_interactions import product_state_key, isolate_product_state


def workspace():
    for parent in Path(__file__).resolve().parents:
        if parent.name == 'codex_work': return parent
    return Path(__file__).resolve().parents[2] / 'codex_work'


def new_output(path):
    root = workspace().resolve()
    target = Path(path).absolute()
    resolved = target.resolve()
    if not resolved.is_relative_to(root) or resolved == root or target.is_symlink():
        raise ValueError('Результаты разрешены только в новой папке внутри codex_work')
    if target.exists(): raise ValueError('Каталог прогона уже существует; нужен новый путь')
    target.mkdir(parents=True, exist_ok=False)
    return resolved


def make_crawler(output, profiles, base_url, limits):
    # Не вызываем Config.load_from_env и main: никакой очереди/загрузчика/карточек.
    config = Config()
    for name in ('base_dir','reports_dir','vector_db_path','documents_dir','logs_dir',
                 'graph_db_json_output_dir','graph_db_archive_dir','graph_db_pending_dir',
                 'product_cards_dir','profiles_drafts_dir','profile_metrics_dir'):
        setattr(config, name, str(output / name))
    config.graph_db_enable = False
    config.profiles_dir = str(Path(profiles).resolve())
    config.max_concurrent_pages = 1
    config.max_concurrent_file_downloads = 1
    config.sitemap_http_concurrency = 2
    config.sitemap_max_scan_urls = limits.urls
    config.sitemap_max_urls = limits.urls
    config.sitemap_phase_timeout_seconds = min(30, limits.seconds)
    config.page_fetch_timeout = min(45, limits.seconds)
    Path(config.base_dir).mkdir()
    resolver = ProfileResolver(profiles)
    profile = resolver.resolve(base_url)  # v2 fail-closed до создания краулера.
    resolver_module._resolver = resolver
    crawler = WebCrawler(config)
    crawler._apply_site_profile(base_url)
    crawler.current_base_url = base_url
    assert crawler.profile == (None if profile.is_default() else profile)
    perimeter_allowed = crawler._is_allowed_url
    crawler._structure_perimeter_allowed = perimeter_allowed
    def allowed(url, *, resource='page', base_url=None):
        if not perimeter_allowed(url, resource=resource, base_url=base_url): return False
        if resource == 'page':
            role, _ = crawler.url_categorizer.categorize_url(url)
            if role in ('excluded', 'excluded2'): return False
            if crawler.url_categorizer.should_exclude_by_language(url): return False
        return True
    # Тот же запрет до каждого HTTP/browser-перехода, включая редиректы.
    crawler._is_allowed_url = allowed
    return crawler


class SavedCorpus:
    """Только явно перечисленные файлы. Промах не вызывает сеть или резервный обход."""
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.data = json.loads(self.path.read_text())
        self.pages = self.data.get('pages', {})
        self.resources = self.data.get('resources', {})
        self.reads = []

    def body(self, row):
        path = (self.path.parent / row['path']).resolve()
        raw = path.read_bytes()
        self.reads.append(dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest()))
        return raw

    async def request(self, url, method='GET'):
        row = self.resources.get(url)
        if not row: return 404, url, {}, b''
        return row.get('status',200), row.get('final_url',url), {'content-type':row.get('content_type','')}, self.body(row) if method!='HEAD' else b''

    async def fetch(self, url, role):
        row = self.pages.get(url)
        if not row: return FetchOutcome(url, reason='not_in_saved_corpus', retryable=False)
        if row.get('error'): return FetchOutcome(url, reason=row['error'], retryable=False)
        html = self.body(row).decode('utf-8', errors='replace')
        if product_state_key(url):
            html = isolate_product_state(html, url)
            if html is None: return FetchOutcome(url, reason='saved_state_missing', retryable=False)
        return FetchOutcome(url, row.get('final_url',url), 'saved', row.get('status',200),
                            'ok', False, content=html) if row.get('status',200)==200 else FetchOutcome(
                                url, reason='http_'+str(row['status']), retryable=False)


async def execute(args):
    limits = DiscoveryLimits(args.pages, args.urls, args.depth, args.seconds, args.sample)
    output = new_output(args.output)
    saved = SavedCorpus(args.manifest) if args.manifest else None
    temp = output / 'tmp'; temp.mkdir()
    previous = {name:os.environ.get(name) for name in ('TMPDIR','XDG_CACHE_HOME')}
    old_temp = tempfile.tempdir
    old_resolver = resolver_module._resolver
    crawler = None
    try:
        os.environ['TMPDIR'] = str(temp)
        os.environ['XDG_CACHE_HOME'] = str(output/'cache')
        tempfile.tempdir = str(temp)
        crawler = make_crawler(output, args.profiles, args.url, limits)
        discovery = StructureDiscovery(crawler, args.url, limits,
                                       saved.data.get('card_outcomes') if saved else None)
        if saved:
            crawler.sitemap_parser._request = saved.request
            fetch = saved.fetch
        else:
            await crawler.initialize()
            fetch = crawler.fetch_page
        snapshots = output/'html'; snapshots.mkdir()
        async def capture(url, role):
            result = await fetch(url, role)
            contents = result.content if isinstance(result.content,list) else [result.content]
            for index, content in enumerate(contents):
                if content is None: continue
                html = content.html if hasattr(content,'html') else str(content)
                name = hashlib.sha256(url.encode()).hexdigest()[:24] + f'_{index}.html'
                (snapshots/name).write_text(html)
            return result
        report = await discovery.run(capture, args.control, args.holdout, sitemap=not args.no_sitemap)
        report['mode'] = 'saved' if saved else 'live'
        report['created_at'] = datetime.now(timezone.utc).isoformat()
        report['coverage']['live'] = 'not_checked' if saved else 'fetch_attempts_recorded'
        if saved:
            report['inputs'] = sorted({r['path']:r for r in saved.reads}.values(), key=lambda r:r['path'])
            report['manifest_sha256'] = hashlib.sha256(saved.path.read_bytes()).hexdigest()
        report['code_sha256'] = {str(path.relative_to(Path(__file__).resolve().parents[1])):hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [Path(__file__),Path(__file__).with_name('structure.py'),
                         Path(__file__).resolve().parents[1]/'web_crawler.py',
                         Path(__file__).resolve().parents[1]/'product_interactions.py',
                         Path(__file__).resolve().parents[1]/'inline_products.py',
                         Path(__file__).with_name('format_v2.py')]}
        report['isolation'] = dict(output=str(output), graph_enabled=False, pipeline_started=False)
        (output/'structure.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        with (output/'sample.tsv').open('w', newline='') as handle:
            writer = csv.writer(handle, delimiter='\t')
            writer.writerow(['URL','Роль','Состояние','Группа','Выборка','Причина','Принудительный'])
            selected = set(report['sample'])
            for row in report['urls']:
                if row['key'] in selected:
                    writer.writerow([row['url'],row['role'],row['state'],row['group'],
                                     row['sample_split'],row['reason'],row['forced']])
        summary = ['Карта структуры сайта: '+args.url, 'Режим: '+report['mode'],
                   f"URL: {len(report['urls'])}; групп: {len(report['groups'])}; выборка: {len(report['sample'])}",
                   'Ограничения: '+', '.join(report['coverage']['gaps']),
                   'Полнота сайта и качество карточек этим отчётом не подтверждаются.']
        (output/'SUMMARY.txt').write_text('\n'.join(summary)+'\n')
        return output
    except Exception as exc:
        (output/'error.json').write_text(json.dumps({'error':str(exc)},ensure_ascii=False,indent=2))
        raise
    finally:
        try:
            if crawler is not None: await crawler.close()
        finally:
            resolver_module._resolver = old_resolver
            tempfile.tempdir = old_temp
            for name,value in previous.items():
                if value is None: os.environ.pop(name,None)
                else: os.environ[name]=value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('--profiles', required=True)
    parser.add_argument('--output', required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--manifest')
    mode.add_argument('--live', action='store_true')
    parser.add_argument('--no-sitemap', action='store_true')
    parser.add_argument('--control', action='append', default=[])
    parser.add_argument('--holdout', action='append', default=[])
    for name, default in [('pages',60),('urls',10000),('depth',5),('seconds',300),('sample',30)]:
        parser.add_argument('--'+name,type=int,default=default)
    args = parser.parse_args()
    try:
        output = asyncio.run(execute(args))
    except Exception as exc:
        print('Ошибка обнаружения: '+str(exc)); return 2
    print(output/'structure.json')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
