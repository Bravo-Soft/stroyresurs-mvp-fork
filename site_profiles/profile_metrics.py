"""Метрики прогона per-company для системы профилирования.

RunMetricsCollector — дешёвые счётчики, работающие в ЛЮБОМ режиме (и census, и прод):
методы фетча, антибот-челленджи, категории страниц, пути извлечения markdown, выход
(карточки/гейт >=3 ТХ). Каждый record_* обёрнут fail-open: сбой телеметрии никогда
не валит пайплайн. Результат — JSON Base/profile_metrics/<домен>/<run_ts>.json;
тот же словарь служит baseline черновика переписи (census_collector).
"""
import json
import logging
import os
from collections import Counter
from datetime import datetime

logger = logging.getLogger(__name__)


def _safe(method):
    """Декоратор fail-open: телеметрия не имеет права ронять пайплайн."""
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as e:
            logger.debug(f'profile_metrics.{method.__name__}: {e}')
            return None
    return wrapper


class RunMetricsCollector:
    """Счётчики одного прогона одной компании."""

    def __init__(self, domain: str, company_name: str = '', baseline: dict = None):
        self.domain = domain
        self.company_name = company_name
        self.baseline = baseline or {}          # baseline профиля для алертов
        self.started_at = datetime.now().isoformat(timespec='seconds')
        self.fetch_methods = Counter()          # http_first/aiohttp/playwright/dynamic/ajax_tabs
        self.fetch_fails = 0
        self.fetch_total = 0
        self.challenges = 0
        self.warmup_success = 0
        self.warmup_fail = 0
        self.sitemap_url_count = None
        self.page_urls = []                     # [(url, category)] сохранённых страниц
        self.extraction_paths = Counter()       # adapter/adapter_special/contacts_universal/universal/generic
        self.markdown_lens = []
        self.min_len = 200                      # порог «пустого» markdown (профиль может сменить)
        self.jsonld_pages = 0
        self.universal_detail_pages = 0
        self.cms_hits = Counter()               # как выбрался контейнер universal-пути
        self.structure_hashes = Counter()       # skeleton-hash товарных страниц
        self.document_files = Counter()         # общие документы по блокам профиля
        self.output = {}                        # срез CompanyStatistics

    # ---------- хуки краулера ----------

    @_safe
    def record_fetch(self, url, category, method):
        self.fetch_total += 1
        self.fetch_methods[method] += 1

    @_safe
    def record_fetch_fail(self, url, category=None):
        self.fetch_total += 1
        self.fetch_fails += 1

    @_safe
    def record_challenge(self, url):
        self.challenges += 1

    @_safe
    def record_warmup(self, success):
        if success:
            self.warmup_success += 1
        else:
            self.warmup_fail += 1

    @_safe
    def record_sitemap(self, url_count):
        self.sitemap_url_count = int(url_count)

    @_safe
    def record_document_files(self, section, count):
        """Скачанные общие документы компании по блокам профиля
        (certificates/documents/instructions/price_list)."""
        self.document_files[section] += int(count)

    @_safe
    def record_pages(self, stored_pages):
        """Сохранённые страницы краула: [{'url':..., 'category':...}, ...]."""
        for page in stored_pages or []:
            url = page.get('url') or page.get('normalized_url') or ''
            category = page.get('category') or 'other'
            if url:
                self.page_urls.append((url, category))

    # ---------- sink извлечения (text_extractor/universal_extractor) ----------

    @_safe
    def sink(self, event, **data):
        if event == 'extraction':
            self.extraction_paths[data.get('path') or 'generic'] += 1
            if data.get('md_len') is not None:
                self.markdown_lens.append(int(data['md_len']))
            if data.get('structure_hash'):
                self.structure_hashes[data['structure_hash']] += 1
        elif event == 'universal_detail':
            self.universal_detail_pages += 1
            if data.get('jsonld'):
                self.jsonld_pages += 1
            if data.get('cms_how'):
                self.cms_hits[str(data['cms_how'])] += 1

    # ---------- выход пайплайна ----------

    @_safe
    def record_output(self, stats: dict):
        """Срез итоговых счётчиков компании (main.CompanyStatistics.__dict__ или dict)."""
        keys = ('products_found', 'products_processed', 'products_failed',
                'products_below_min_specs', 'cards_generated',
                'product_pages_processed', 'company_pages_processed',
                'distributor_pages_processed', 'files_downloaded', 'errors')
        for key in keys:
            if key in stats and not callable(stats[key]):
                value = stats[key]
                self.output[key] = len(value) if isinstance(value, (list, set)) else value

    # ---------- свод ----------

    def _rate(self, part, total):
        return round(part / total, 4) if total else None

    def to_dict(self) -> dict:
        pages_by_category = Counter(cat for _, cat in self.page_urls)
        extraction_total = sum(self.extraction_paths.values())
        empty_md = sum(1 for n in self.markdown_lens if n < self.min_len)
        products_processed = self.output.get('products_processed') or 0
        below_min = self.output.get('products_below_min_specs') or 0
        gate3_total = products_processed + below_min
        dominant_structure = (self.structure_hashes.most_common(1)[0][0]
                              if self.structure_hashes else None)
        metrics = {
            'domain': self.domain,
            'company_name': self.company_name,
            'started_at': self.started_at,
            'finished_at': datetime.now().isoformat(timespec='seconds'),
            # Coverage
            'pages_crawled': len(self.page_urls),
            'pages_by_category': dict(pages_by_category),
            'product_pages': pages_by_category.get('product', 0),
            'product_cards': self.output.get('cards_generated'),
            'sitemap_url_count': self.sitemap_url_count,
            # Extraction
            'extraction_path_share': {k: self._rate(v, extraction_total)
                                      for k, v in self.extraction_paths.items()},
            'empty_markdown_rate': self._rate(empty_md, len(self.markdown_lens)),
            'gate3_pass_rate': self._rate(products_processed, gate3_total),
            'jsonld_share': self._rate(self.jsonld_pages, self.universal_detail_pages),
            'cms_detected': (self.cms_hits.most_common(1)[0][0] if self.cms_hits else None),
            'selector_hit_rate': self._rate(
                sum(v for k, v in self.cms_hits.items()
                    if k not in ('readability', 'body', 'error')),
                self.universal_detail_pages),
            # Antibot / load
            'fetch_methods': dict(self.fetch_methods),
            'fetch_fail_rate': self._rate(self.fetch_fails, self.fetch_total),
            'challenge_rate': self._rate(self.challenges, self.fetch_total),
            'warmup': {'success': self.warmup_success, 'fail': self.warmup_fail},
            # Drift
            'structure_hash': dominant_structure,
            'company_document_files': dict(self.document_files),
            'output': dict(self.output),
        }
        metrics['alerts'] = self._alerts(metrics)
        return metrics

    def _alerts(self, metrics: dict) -> list:
        """Пороговые сигналы (фаза 0: только пишутся в отчёт; реакция — Drift Watchdog, фаза 3)."""
        alerts = []
        baseline = self.baseline or {}

        def worse(name, current, base, drop=0.4):
            if isinstance(base, (int, float)) and base and isinstance(current, (int, float)):
                if current < base * (1 - drop):
                    alerts.append(f'{name}: {current} < baseline {base} (-{int(drop*100)}%)')

        worse('product_cards', metrics.get('product_cards'), baseline.get('product_cards'))
        worse('pages_crawled', metrics.get('pages_crawled'), baseline.get('pages_crawled'))
        if (baseline.get('sitemap_url_count') and metrics.get('sitemap_url_count') is not None):
            base, cur = baseline['sitemap_url_count'], metrics['sitemap_url_count']
            if base and abs(cur - base) / base > 0.3:
                alerts.append(f'sitemap diff >30%: {cur} vs baseline {base}')
        if (metrics.get('challenge_rate') or 0) > 0.2 and baseline.get('challenge_rate', 0) in (0, None):
            alerts.append(f'challenge_rate {metrics["challenge_rate"]} > 0.2 при baseline none')
        if (metrics.get('fetch_fail_rate') or 0) > 0.15:
            alerts.append(f'fetch_fail_rate {metrics["fetch_fail_rate"]} > 0.15')
        if (metrics.get('empty_markdown_rate') or 0) > 0.3:
            alerts.append(f'empty_markdown_rate {metrics["empty_markdown_rate"]} > 0.3')
        if metrics.get('gate3_pass_rate') is not None and metrics['gate3_pass_rate'] < 0.5:
            alerts.append(f'gate3_pass_rate {metrics["gate3_pass_rate"]} < 0.5')
        if (baseline.get('structure_hash') and metrics.get('structure_hash')
                and baseline['structure_hash'] != metrics['structure_hash']):
            alerts.append('structure_hash изменился (кандидат RE-PROFILE)')
        for category in ('contacts', 'distributor'):
            if not metrics.get('pages_by_category', {}).get(category):
                alerts.append(f'секция {category}: 0 страниц')
        return alerts

    @_safe
    def write(self, metrics_dir: str) -> str:
        """Пишет JSON метрик: <metrics_dir>/<домен>/<run_ts>.json. Возвращает путь."""
        out_dir = os.path.join(metrics_dir, self.domain or 'unknown')
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, datetime.now().strftime('%Y%m%d_%H%M%S') + '.json')
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=1)
        logger.info(f'Метрики профилирования записаны: {path}')
        return path
