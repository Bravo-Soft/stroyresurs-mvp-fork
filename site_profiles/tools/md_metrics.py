#!/usr/bin/env python3
"""Офлайн-метрики качества markdown (M1–M8) по сохранённому корпусу страниц.

Пересобирает markdown товарных страниц боевым `text_extractor.html_to_markdown`
(тот же вызов, что в main.py: page_type='') и считает метрики приемлемости из
«Плана доработок прогона 2082», §3.4(a). Сеть не используется: вход — сохранённые
HTML прошлого прогона, поэтому изменение метрик = изменение кода/профиля, а не сайта.

Метрики (порог ОК / красный):
  M1 container_text_share — медиана len(text(контейнер))/len(text(body))   >=0.35 / <0.15
  M2 spec_lines           — медиана строк «ключ: значение с числом» + строк
                            md-таблиц с >=2 клетками и числом                >=8    / <=2
  M3 table_retention      — доля страниц, где <table> в HTML и таблица в md  >=0.95 / <0.7
  M4 short_md_rate        — доля страниц с len(md) < min_len                 <=0.05 / >0.3
  M5 tab_coverage         — доля панелей вкладок, чей текст есть в markdown  >=0.9  / <0.5
  M6 glued_cell_rate      — доля склеенных ячеек md-таблиц                   <=0.02 / >0.1
  M7 footnote_retention   — доля таблиц с маркером, чья сноска рядом         >=0.9  / <0.5
  M8 boilerplate_share    — доля строк, встречающихся на >=80 % страниц      <=0.15 / >0.4

Выход в --out: per-company/<Компания>.json, summary.json, summary.md.
Индекс корпуса «домен -> папки» кэшируется в --index (нужен верификатору профилей, W6).

Запуск:
  python site_profiles/tools/md_metrics.py --companies Тизол_ОАО --out /tmp/md_metrics
  python site_profiles/tools/md_metrics.py --sample-file sample400.json --workers 6 --out ...
"""
import argparse
import json
import re
import statistics
import sys
import time
from collections import Counter
from multiprocessing import Pool
from pathlib import Path

MVP = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(MVP))

from bs4 import BeautifulSoup  # noqa: E402

import text_extractor  # noqa: E402
from site_profiles.resolver import normalize_domain  # noqa: E402

DEFAULT_CORPUS = '/ai/projects/stroy-resurs/mvp_directories/Base/temp_html_storage'
DEFAULT_INDEX = '/ai/projects/stroy-resurs/mvp_directories/Base/profile_metrics/_corpus_index.json'
DEFAULT_MIN_LEN = 200          # тот же дефолт, что в html_to_markdown (профиль может переопределить)
MAX_WORKERS = 6                # машина общая — больше процессов не берём

# Пороги §3.4(a): метрика -> (человекочитаемое имя, тест «ОК», тест «красный»).
THRESHOLDS = {
    'M1': ('container_text_share', lambda v: v >= 0.35, lambda v: v < 0.15),
    'M2': ('spec_lines', lambda v: v >= 8, lambda v: v <= 2),
    'M3': ('table_retention', lambda v: v >= 0.95, lambda v: v < 0.7),
    'M4': ('short_md_rate', lambda v: v <= 0.05, lambda v: v > 0.3),
    'M5': ('tab_coverage', lambda v: v >= 0.9, lambda v: v < 0.5),
    'M6': ('glued_cell_rate', lambda v: v <= 0.02, lambda v: v > 0.1),
    'M7': ('footnote_retention', lambda v: v >= 0.9, lambda v: v < 0.5),
    'M8': ('boilerplate_share', lambda v: v <= 0.15, lambda v: v > 0.4),
}

# Цепочка контейнера из text_extractor._build_markdown (:693-698). Функция вложенная,
# импортировать её нельзя — список селекторов продублирован МИНИМАЛЬНО (см. отчёт W2,
# «Ограничения»); всё остальное (clean_noise, clean_noise_product, резолв профиля и
# адаптера) вызывается прямо из text_extractor.
_CONTAINER_SELECTORS = ['[itemtype*="Product"]', 'article', 'main', '.content']

# Панели вкладок/аккордеонов: ищем в ИСХОДНОМ HTML (clean_noise вырезает display:none —
# именно потерю такого текста метрика и ловит).
_TAB_PANEL_SELECTORS = [
    '[role="tabpanel"]', '.tab-pane', '.tab-content', '.tab_content', '.tab-panel',
    '.tabs__content', '.tabs-content', '.tabs__panel', '.accordion-content',
    '.accordion__content', '.accordion-body',
]

# M2: «ключ: значение с числом» — двоеточие вне md-таблицы, число после него.
_SPEC_KV_RE = re.compile(r'^\s*[-*]?\s*[^|:]{2,80}:\s*[^|]*\d')
_SEP_CELL_RE = re.compile(r'^:?-{3,}:?$')
# M6: склейка «словоСлово» и «3мм» (число + кириллица без пробела).
_GLUE_RES = (re.compile(r'[а-яё][А-ЯЁ]'), re.compile(r'\d[А-Яа-я]{3,}'))
# M7: маркер сноски в ячейке — «прилипший» к концу текста *, † или ‡ (bold-разметка **
# исключена) либо литеральные ¹/². Каретную форму (^1, ^2), в которую _postprocess_markdown
# превращает надстрочные знаки, НЕ берём: она неотличима от единиц «м^2» (ограничение M7).
_FOOTNOTE_CELL_RES = (
    (re.compile(r'(?<![\s*])\*(?!\*)\s*$'), '*'),
    (re.compile(r'†\s*$'), '†'),
    (re.compile(r'‡\s*$'), '‡'),
    (re.compile(r'¹'), '¹'),
    (re.compile(r'²'), '²'),
)


# ==================== РАЗБОР MARKDOWN ====================

def _norm(text: str) -> str:
    """Схлопывает пробелы (сравнение текста панелей с markdown, ключи строк M8)."""
    return re.sub(r'\s+', ' ', text or '').strip()


def _cells(line: str):
    """Ячейки строки md-таблицы '| a | b |' -> ['a', 'b']."""
    return [c.strip() for c in line.strip().strip('|').split('|')]


def _is_table_row(line: str) -> bool:
    s = line.strip()
    return s.startswith('|') and s.endswith('|') and len(s) > 2


def _is_separator_row(line: str) -> bool:
    return _is_table_row(line) and all(_SEP_CELL_RE.match(c) for c in _cells(line) if c)


def _table_blocks(lines):
    """Блоки подряд идущих строк md-таблицы: [(индекс_первой_строки, [строки]), ...]."""
    blocks, current, start = [], [], 0
    for i, line in enumerate(lines):
        if _is_table_row(line):
            if not current:
                start = i
            current.append(line)
        elif current:
            blocks.append((start, current))
            current = []
    if current:
        blocks.append((start, current))
    return blocks


def _is_kv_spec(line: str) -> bool:
    """Строка «ключ: значение с числом»; голая ссылка в значении не в счёт."""
    if not _SPEC_KV_RE.match(line):
        return False
    return not line.split(':', 1)[1].strip().startswith(('http', '//'))


def _spec_lines(lines) -> int:
    """M2: строки «ключ: значение с числом» + строки таблиц с >=2 клетками и числом."""
    count = 0
    for line in lines:
        if _is_table_row(line):
            if _is_separator_row(line):
                continue
            cells = [c for c in _cells(line) if c]
            if len(cells) >= 2 and any(ch.isdigit() for ch in line):
                count += 1
        elif _is_kv_spec(line):
            count += 1
    return count


def _cell_stats(lines):
    """M6: (всего ячеек md-таблиц, из них со склейкой)."""
    total = glued = 0
    for line in lines:
        if not _is_table_row(line) or _is_separator_row(line):
            continue
        for cell in _cells(line):
            if not cell:
                continue
            total += 1
            if any(rx.search(cell) for rx in _GLUE_RES):
                glued += 1
    return total, glued


def _footnote_stats(lines):
    """M7: (таблиц с маркером сноски, из них со сноской в 3 строках после таблицы).

    Строка сноски начинается с того же маркера; ведущий обратный слэш снимаем —
    markdownify экранирует звёздочку в начале строки («\\* при 20 C»)."""
    marked = kept = 0
    for start, block in _table_blocks(lines):
        markers = set()
        for line in block:
            if _is_separator_row(line):
                continue
            for cell in _cells(line):
                for regex, marker in _FOOTNOTE_CELL_RES:
                    if regex.search(cell):
                        markers.add(marker)
        if not markers:
            continue
        marked += 1
        tail = [line.strip().lstrip('\\') for line in lines[start + len(block):start + len(block) + 3]]
        if any(line.startswith(marker) and len(line) > len(marker) + 3
               for line in tail for marker in markers):
            kept += 1
    return marked, kept


# ==================== ВЫБОР КОНТЕЙНЕРА (M1) ====================

def _adapter_for(url: str, profile):
    """Адаптер, который выберет html_to_markdown: профиль (tier=custom) -> домен."""
    if profile is not None:
        if profile.extract.tier == 'custom':
            return text_extractor._get_adapter_by_module(profile.extract.custom_module)
        return None
    domain = text_extractor._normalize_domain(url)
    return text_extractor._get_adapters().get(domain) if domain else None


def _pick_container(soup, profile_container):
    """Узел, который выберет цепочка _build_markdown, и сработавший селектор."""
    selectors = ([profile_container] if profile_container else []) + _CONTAINER_SELECTORS
    for selector in selectors:
        try:
            node = soup.select_one(selector)
        except Exception:
            node = None
        if node:                      # пустой тег (len(contents)==0) ложен — как в оригинале
            return node, selector
    body = soup.body
    return (body if body else soup), 'body'


def _text_len(node) -> int:
    return len(_norm(node.get_text(' '))) if node is not None else 0


def container_share(html: str, url: str):
    """M1 для страницы: (доля текста контейнера от body, селектор, длина текста body).

    Повторяет подготовку DOM боевого пути товарной страницы: clean_noise (с учётом
    KEEP_HIDDEN_TABS адаптера/профиля) + remove_selectors профиля; доля считается
    ПОСЛЕ этой очистки и ДО clean_noise_product (так же считал замер P09)."""
    try:
        soup = BeautifulSoup(html, 'lxml')
    except Exception:
        soup = BeautifulSoup(html, 'html.parser')
    profile = text_extractor._resolve_profile(url)
    adapter = _adapter_for(url, profile)
    keep_hidden = bool(getattr(adapter, 'KEEP_HIDDEN_TABS', False))
    profile_container = None
    if profile is not None:
        keep_hidden = keep_hidden or profile.extract.keep_hidden_tabs
        profile_container = profile.extract.markdown.product_container
    soup = text_extractor.clean_noise(soup, url, strip_hidden=not keep_hidden)
    if profile is not None:
        for selector in profile.extract.markdown.remove_selectors:
            for tag in soup.select(selector):
                try:
                    tag.decompose()
                except Exception:
                    pass
    body_len = _text_len(soup.body if soup.body else soup)
    container, selector = _pick_container(soup, profile_container)
    share = round(_text_len(container) / body_len, 4) if body_len else None
    return share, selector, body_len


def _min_len_for(url: str) -> int:
    """Порог «короткого» markdown: extract.markdown.min_len профиля либо дефолт 200."""
    profile = text_extractor._resolve_profile(url)
    if profile is not None and profile.extract.markdown.min_len is not None:
        return profile.extract.markdown.min_len
    return DEFAULT_MIN_LEN


# ==================== МЕТРИКИ СТРАНИЦЫ ====================

def page_metrics(html: str, url: str):
    """Счётчики одной товарной страницы + нормализованные строки markdown (для M8)."""
    path_box = {}

    def sink(event, **data):
        """Штатный хук телеметрии извлечения: даёт фактический путь (generic/universal/adapter)."""
        if event == 'extraction':
            path_box['path'] = data.get('path', '')

    text_extractor.set_census_sink(sink)
    try:
        markdown = text_extractor.html_to_markdown(html, url, '')
    finally:
        text_extractor.set_census_sink(None)

    lines = markdown.split('\n')
    share, selector, body_len = container_share(html, url)
    cells_total, cells_glued = _cell_stats(lines)
    tables_marked, tables_footnoted = _footnote_stats(lines)
    panels_total, panels_covered = _tab_stats(html, markdown)
    record = {
        'url': url,
        'md_len': len(markdown),
        'extraction_path': path_box.get('path', ''),
        'container_share': share,
        'container_selector': selector,
        'body_text_len': body_len,
        'spec_lines': _spec_lines(lines),
        'has_table_html': bool(re.search(r'<table[\s>]', html, re.I)),
        'has_table_md': any(_is_separator_row(line) for line in lines),
        'short_md': len(markdown) < _min_len_for(url),
        'tab_panels': panels_total,
        'tab_panels_covered': panels_covered,
        'cells_total': cells_total,
        'cells_glued': cells_glued,
        'tables_marked': tables_marked,
        'tables_footnoted': tables_footnoted,
    }
    md_lines = [_norm(line) for line in lines]
    return record, [line for line in md_lines if len(line) >= 3]


def _tab_stats(html: str, markdown: str):
    """M5: (опознанных панелей вкладок, из них покрытых markdown на >=60 % строк)."""
    try:
        soup = BeautifulSoup(html, 'lxml')
    except Exception:
        return 0, 0
    md_norm = _norm(markdown)
    total = covered = 0
    seen = set()
    for selector in _TAB_PANEL_SELECTORS:
        try:
            panels = soup.select(selector)
        except Exception:
            continue
        for panel in panels:
            key = id(panel)
            if key in seen:
                continue
            seen.add(key)
            lines = [_norm(line) for line in panel.get_text('\n').split('\n')]
            lines = [line for line in lines if len(line) >= 3]
            if not lines:
                continue
            total += 1
            hits = sum(1 for line in lines if line in md_norm)
            if hits >= 0.6 * len(lines):
                covered += 1
    return total, covered


# ==================== МЕТРИКИ КОМПАНИИ ====================

def _ratio(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def _verdict(metric: str, value):
    if value is None:
        return 'n/a'
    _, is_ok, is_red = THRESHOLDS[metric]
    if is_red(value):
        return 'red'
    return 'ok' if is_ok(value) else 'warn'


def aggregate(details, page_lines):
    """M1–M8 компании из счётчиков страниц (page_lines — строки markdown для M8)."""
    shares = [d['container_share'] for d in details if d['container_share'] is not None]
    with_table = [d for d in details if d['has_table_html']]
    pages = len(details)
    metrics = {
        'M1': round(statistics.median(shares), 4) if shares else None,
        'M2': statistics.median([d['spec_lines'] for d in details]) if details else None,
        'M3': _ratio(sum(1 for d in with_table if d['has_table_md']), len(with_table)),
        'M4': _ratio(sum(1 for d in details if d['short_md']), pages),
        'M5': _ratio(sum(d['tab_panels_covered'] for d in details),
                     sum(d['tab_panels'] for d in details)),
        'M6': _ratio(sum(d['cells_glued'] for d in details),
                     sum(d['cells_total'] for d in details)),
        'M7': _ratio(sum(d['tables_footnoted'] for d in details),
                     sum(d['tables_marked'] for d in details)),
        'M8': _boilerplate_share(page_lines),
    }
    return metrics, {m: _verdict(m, v) for m, v in metrics.items()}


def _boilerplate_share(page_lines):
    """M8: доля строк, встречающихся на >=80 % товарных страниц (нужно >=3 страниц)."""
    pages = [lines for lines in page_lines if lines]
    if len(pages) < 3:
        return None
    document_freq = Counter()
    for lines in pages:
        document_freq.update(set(lines))
    threshold = 0.8 * len(pages)
    boilerplate = {line for line, freq in document_freq.items() if freq >= threshold}
    total = sum(len(lines) for lines in pages)
    hits = sum(1 for lines in pages for line in lines if line in boilerplate)
    return _ratio(hits, total)


def product_pages(company_dir: Path, limit: int):
    """Товарные страницы компании: (html-файл, url) детерминированно по имени файла."""
    pages = []
    for html_path in sorted((company_dir / 'Product_pages').glob('*.html'))[:limit]:
        url = ''
        meta_path = html_path.with_suffix('.json')
        if meta_path.exists():
            try:
                url = json.loads(meta_path.read_text(encoding='utf-8')).get('url', '')
            except Exception:
                pass
        pages.append((html_path, url))
    return pages


def company_metrics(company_dir: Path, per_company: int) -> dict:
    """Полный отчёт по одной компании корпуса."""
    started = time.time()
    product_dir = company_dir / 'Product_pages'
    pages_total = sum(1 for _ in product_dir.glob('*.html')) if product_dir.is_dir() else 0
    details, page_lines, errors = [], [], []
    for html_path, url in product_pages(company_dir, per_company):
        try:
            html = html_path.read_text(encoding='utf-8', errors='replace')
            record, lines = page_metrics(html, url)
        except Exception as e:
            errors.append(f'{html_path.name}: {e}')
            continue
        record['file'] = html_path.name
        details.append(record)
        page_lines.append(lines)
    metrics, verdict = aggregate(details, page_lines)
    domain = ''
    for d in details:
        if d['url']:
            domain = normalize_domain(d['url'])
            break
    paths = Counter(d['extraction_path'] for d in details)
    return {
        'domain': domain,
        'company_dir': company_dir.name,
        'pages_total': pages_total,
        'pages_used': len(details),
        'dominant_path': paths.most_common(1)[0][0] if paths else '',
        'metrics': metrics,
        'verdict': verdict,
        'errors': errors,
        'seconds': round(time.time() - started, 1),
        'details': details,
    }


# ==================== ИНДЕКС КОРПУСА ====================

def _company_domain(company_dir: Path) -> str:
    """Домен компании по url первой сохранённой страницы (любая категория)."""
    for category in ('Product_pages', 'Company_pages', 'Distributor_pages', 'Other_pages'):
        for meta_path in sorted((company_dir / category).glob('*.json'))[:1]:
            try:
                domain = normalize_domain(
                    json.loads(meta_path.read_text(encoding='utf-8')).get('url', ''))
            except Exception:
                domain = ''
            if domain:
                return domain
    return ''


def build_index(corpus: Path) -> dict:
    """Индекс «домен -> папки корпуса» (домен из url первой страницы компании)."""
    domains = {}
    for company_dir in sorted(corpus.iterdir()):
        if not company_dir.is_dir():
            continue
        domains.setdefault(_company_domain(company_dir), []).append(company_dir.name)
    return {'corpus': str(corpus),
            'built_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            'companies': sum(len(v) for v in domains.values()),
            'domains': domains}


def load_index(corpus: Path, index_path: Path, rebuild: bool = False) -> dict:
    """Индекс из кэша; нет кэша (или --rebuild-index) — построить и сохранить."""
    if not rebuild and index_path.exists():
        try:
            return json.loads(index_path.read_text(encoding='utf-8'))
        except Exception:
            pass
    index = build_index(corpus)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding='utf-8')
    return index


# ==================== СВОДКА ====================

def summarize(results, seconds: float) -> dict:
    """Медианы M1–M8 по компаниям, доли вердиктов, сверка с замером P09."""
    summary = {'companies': len(results),
               'pages': sum(r['pages_used'] for r in results),
               'seconds': round(seconds, 1),
               'metrics': {}}
    for metric in THRESHOLDS:
        values = [r['metrics'][metric] for r in results if r['metrics'][metric] is not None]
        verdicts = Counter(r['verdict'][metric] for r in results)
        summary['metrics'][metric] = {
            'name': THRESHOLDS[metric][0],
            'companies_with_value': len(values),
            'median': round(statistics.median(values), 4) if values else None,
            'p10': round(statistics.quantiles(values, n=10)[0], 4) if len(values) > 9 else None,
            'p90': round(statistics.quantiles(values, n=10)[8], 4) if len(values) > 9 else None,
            'ok': verdicts['ok'], 'warn': verdicts['warn'],
            'red': verdicts['red'], 'n/a': verdicts['n/a'],
            'red_share': _ratio(verdicts['red'], len(results)),
        }
    summary['extraction_paths'] = dict(Counter(
        d['extraction_path'] for r in results for d in r['details']).most_common())
    # M1 считается по generic-цепочке контейнера всегда, а markdown мог дать adapter или
    # universal_extractor со своим выбором узла — разрез по пути нужен для интерпретации.
    summary['m1_median_by_path'] = {}
    for path in summary['extraction_paths']:
        values = [r['metrics']['M1'] for r in results
                  if r['dominant_path'] == path and r['metrics']['M1'] is not None]
        if values:
            summary['m1_median_by_path'][path] = {
                'companies': len(values), 'median': round(statistics.median(values), 4)}
    summary['p09_compare'] = _p09_compare(results)
    return summary


def _p09_compare(results) -> dict:
    """Критерии замера P09: доля страниц с долей контейнера <0.3 и <0.1.

    P09 считал по случайной выборке (400 компаний, по 2 товарные страницы, seed 7,
    только страницы с body_txt >= 1000) — здесь тот же фильтр даёт сопоставимый срез."""
    out = {}
    for label, pages in (
            ('all_pages', [d for r in results for d in r['details']
                           if d['container_share'] is not None]),
            ('body_text_ge_1000', [d for r in results for d in r['details']
                                   if d['container_share'] is not None
                                   and d['body_text_len'] >= 1000])):
        out[label] = {
            'pages': len(pages),
            'share_lt_0.3': _ratio(sum(1 for d in pages if d['container_share'] < 0.3), len(pages)),
            'share_lt_0.1': _ratio(sum(1 for d in pages if d['container_share'] < 0.1), len(pages)),
            'selectors_lt_0.1': dict(Counter(
                d['container_selector'] for d in pages if d['container_share'] < 0.1).most_common()),
        }
    companies = [r for r in results if r['details']]
    out['companies_with_page_lt_0.1'] = _ratio(
        sum(1 for r in companies
            if any(d['container_share'] is not None and d['container_share'] < 0.1
                   and d['body_text_len'] >= 1000 for d in r['details'])), len(companies))
    return out


def summary_markdown(summary: dict, args) -> str:
    """summary.md: таблица медиан/вердиктов + сверка с P09 + параметры прогона."""
    lines = [f'# Метрики markdown (M1–M8) — {time.strftime("%Y-%m-%d %H:%M")}', '',
             f'Корпус: `{args.corpus}`; компаний: {summary["companies"]}, '
             f'страниц: {summary["pages"]}; по {args.per_company} товарных страниц на компанию; '
             f'воркеров: {args.workers}; время: {summary["seconds"]} с.', '',
             '| # | Метрика | Медиана по компаниям | p10 | p90 | ОК | warn | красных | доля красных |',
             '|---|---|---|---|---|---|---|---|---|']
    for metric, data in summary['metrics'].items():
        lines.append(f'| {metric} | {data["name"]} | {data["median"]} | {data["p10"]} | '
                     f'{data["p90"]} | {data["ok"]} | {data["warn"]} | {data["red"]} | '
                     f'{data["red_share"]} |')
    compare = summary['p09_compare']
    lines += ['', '## Сверка с замером P09 (контейнер)', '',
              '| срез | страниц | доля < 30 % | доля < 10 % |', '|---|---|---|---|']
    for label in ('all_pages', 'body_text_ge_1000'):
        data = compare[label]
        lines.append(f'| {label} | {data["pages"]} | {data["share_lt_0.3"]} | {data["share_lt_0.1"]} |')
    lines += ['', f'Компаний, где хотя бы одна страница (body_txt >= 1000) даёт < 10 %: '
                  f'{compare["companies_with_page_lt_0.1"]}.',
              '', f'Звенья цепочки при < 10 % (срез body_txt >= 1000): '
                  f'{compare["body_text_ge_1000"]["selectors_lt_0.1"]}.',
              '', f'Пути извлечения (страниц): {summary["extraction_paths"]}.',
              '', f'Медиана M1 по преобладающему пути извлечения компании: '
                  f'{summary["m1_median_by_path"]}.', '']
    return '\n'.join(lines)


# ==================== CLI ====================

def _worker(task):
    company_dir, per_company = task
    try:
        return company_metrics(Path(company_dir), per_company)
    except Exception as e:
        return {'domain': '', 'company_dir': Path(company_dir).name, 'pages_total': 0,
                'pages_used': 0, 'dominant_path': '', 'metrics': {m: None for m in THRESHOLDS},
                'verdict': {m: 'n/a' for m in THRESHOLDS}, 'errors': [str(e)],
                'seconds': 0, 'details': []}


def select_companies(corpus: Path, args):
    """Папки компаний из --companies / --sample-file / --all."""
    if args.companies:
        return [corpus / name for name in args.companies]
    if args.sample_file:
        data = json.loads(Path(args.sample_file).read_text(encoding='utf-8'))
        names = data['companies'] if isinstance(data, dict) else data
        return [corpus / name for name in names]
    return [d for d in sorted(corpus.iterdir()) if d.is_dir()]


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--corpus', default=DEFAULT_CORPUS)
    parser.add_argument('--companies', nargs='*', default=None, help='папки корпуса (имена)')
    parser.add_argument('--sample-file', default=None, help='JSON со списком папок корпуса')
    parser.add_argument('--all', action='store_true', help='весь корпус')
    parser.add_argument('--per-company', type=int, default=30,
                        help='товарных страниц на компанию (детерминированно: сортировка имён)')
    parser.add_argument('--out', default=None, help='папка отчёта (per-company/, summary.json|md)')
    parser.add_argument('--workers', type=int, default=4, help=f'процессов (максимум {MAX_WORKERS})')
    parser.add_argument('--index', default=DEFAULT_INDEX, help='кэш индекса «домен -> папки»')
    parser.add_argument('--rebuild-index', action='store_true')
    args = parser.parse_args()

    if not (args.companies or args.sample_file or args.all):
        parser.error('укажите --companies, --sample-file или --all')
    args.workers = max(1, min(args.workers, MAX_WORKERS))
    corpus = Path(args.corpus)

    index = load_index(corpus, Path(args.index), args.rebuild_index)
    print(f'индекс корпуса: {len(index["domains"])} доменов, {index["companies"]} компаний '
          f'-> {args.index}')

    companies = [d for d in select_companies(corpus, args) if (d / 'Product_pages').is_dir()]
    print(f'компаний к обработке: {len(companies)}')
    started = time.time()
    tasks = [(str(d), args.per_company) for d in companies]
    if args.workers > 1:
        with Pool(args.workers) as pool:
            results = []
            for i, result in enumerate(pool.imap_unordered(_worker, tasks), 1):
                results.append(result)
                if i % 20 == 0 or i == len(tasks):
                    print(f'  {i}/{len(tasks)} компаний, {round(time.time() - started)} с',
                          flush=True)
    else:
        results = [_worker(task) for task in tasks]
    results.sort(key=lambda r: r['company_dir'])
    summary = summarize(results, time.time() - started)

    if args.out:
        out = Path(args.out)
        (out / 'per-company').mkdir(parents=True, exist_ok=True)
        for result in results:
            (out / 'per-company' / f'{result["company_dir"]}.json').write_text(
                json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
        (out / 'summary.json').write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
        (out / 'summary.md').write_text(summary_markdown(summary, args), encoding='utf-8')
        print(f'отчёт: {out}')
    for metric, data in summary['metrics'].items():
        print(f'{metric} {data["name"]}: медиана {data["median"]}, '
              f'красных {data["red"]}/{summary["companies"]}')


if __name__ == '__main__':
    main()
