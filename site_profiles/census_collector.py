"""Census Collector — перепись сайта: черновик профиля из телеметрии прогона.

Пассивный: пользуется только сигналами, которые пайплайн уже вычисляет (собраны в
RunMetricsCollector), ничего не добавляет к нагрузке на сайт. По завершении компании
строит черновик profiles_drafts/<домен>.yaml с confidence; черновики НЕ применяются
пайплайном — принятие в mvp/profiles/ выполняет пост-обработка (см. profiles_drafts/README.md).
"""
import logging
import os
import re
import hashlib
from collections import Counter
from datetime import date
from urllib.parse import urlparse

from .profile_metrics import RunMetricsCollector

logger = logging.getLogger(__name__)


def dom_skeleton_hash(soup, max_depth: int = 3) -> str:
    """sha1 «скелета» DOM: только имена тегов + классы, глубина <= max_depth.
    Устойчив к правкам текста/атрибутов данных — меняется при смене вёрстки (риск №2
    design-doc: без нормализации любой пустяк давал бы ложный дрейф)."""
    parts = []

    def walk(node, depth):
        if depth > max_depth:
            return
        name = getattr(node, 'name', None)
        if not name:
            return
        classes = node.get('class') or []
        if isinstance(classes, str):
            classes = classes.split()
        parts.append(f'{depth}:{name}.{".".join(sorted(classes))}')
        for child in getattr(node, 'children', []):
            walk(child, depth + 1)

    root = getattr(soup, 'body', None) or soup
    try:
        walk(root, 0)
    except Exception:
        return ''
    return hashlib.sha1('|'.join(parts).encode('utf-8', errors='ignore')).hexdigest()


def _common_prefixes(paths, min_count=3, max_prefixes=5):
    """Частотные корневые префиксы путей: '/catalog/x/y' -> '/catalog/'.
    Порядок детерминирован (частота, затем алфавит) — черновики разных прогонов
    сравнимы диффом."""
    counter = Counter()
    for path in paths:
        segments = [s for s in path.split('/') if s]
        if segments:
            counter['/' + segments[0] + '/'] += 1
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [prefix for prefix, n in ranked[:max_prefixes] if n >= min_count]


def _product_templates(paths, min_count=3, max_templates=3):
    """Частотные шаблоны товарных URL: '/catalog/<slug>/<slug>.html' -> regex."""
    counter = Counter()
    for path in paths:
        segments = [s for s in path.split('/') if s]
        if not segments:
            continue
        tail_ext = ''
        if '.' in segments[-1]:
            tail_ext = re.escape('.' + segments[-1].rsplit('.', 1)[1])
            segments[-1] = segments[-1].rsplit('.', 1)[0]
        template = '/' + '/'.join([re.escape(segments[0])] + ['[^/]+'] * (len(segments) - 1))
        counter[template + tail_ext + '/?$'] += 1
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [t for t, n in ranked[:max_templates] if n >= min_count]


class CensusCollector:
    """Строит черновик профиля по метрикам прогона."""

    def __init__(self, metrics: RunMetricsCollector, profile=None):
        self.metrics = metrics
        self.profile = profile      # применявшийся профиль (или None) — для tier/notes

    # ---------- вывод полей из метрик ----------

    def _infer_render(self, metrics: dict) -> str:
        methods = metrics.get('fetch_methods') or {}
        http = methods.get('http_first', 0) + methods.get('aiohttp', 0)
        browser = methods.get('playwright', 0) + methods.get('dynamic', 0) + methods.get('ajax_tabs', 0)
        if http + browser == 0:
            return None
        return 'browser' if browser > http else 'html'

    def _infer_antibot(self, metrics: dict) -> str:
        challenge_rate = metrics.get('challenge_rate') or 0
        warmup = metrics.get('warmup') or {}
        if challenge_rate > 0.5 and not metrics.get('pages_crawled'):
            return 'blocked'
        if warmup.get('success'):
            return 'warmup'
        if challenge_rate > 0.05:
            return 'impersonate'
        return 'none'

    def _infer_sections(self) -> dict:
        by_category = {}
        for url, category in self.metrics.page_urls:
            by_category.setdefault(category, []).append(urlparse(url).path.lower())
        sections = {}
        catalog = _common_prefixes(by_category.get('category', []))
        if catalog:
            sections['catalog_roots'] = catalog
        for category, key in (('contacts', 'contacts_urls'),
                              ('distributor', 'distributor_urls'),
                              ('price_list', 'price_list_urls')):
            prefixes = _common_prefixes(by_category.get(category, []), min_count=1, max_prefixes=3)
            if prefixes:
                sections[key] = prefixes
        product_patterns = _product_templates(by_category.get('product', []))
        if product_patterns:
            sections['product_url_patterns'] = product_patterns
        return sections

    def confidence(self, metrics: dict) -> float:
        """0.3*F + 0.4*E + 0.3*V (стартовая формула design-doc; калибруется переписью).
        F — стабильность фетча, E — стабильность пути извлечения, V — валидность выхода."""
        methods = metrics.get('fetch_methods') or {}
        total_fetch = sum(methods.values()) or 1
        dominant_fetch = max(methods.values()) if methods else 0
        f_score = (1 - (metrics.get('challenge_rate') or 0)) * (dominant_fetch / total_fetch)
        shares = metrics.get('extraction_path_share') or {}
        e_score = max(shares.values()) if shares else 0
        cards = metrics.get('product_cards') or 0
        v_score = min(1.0, cards / 10) * (metrics.get('gate3_pass_rate') or 0)
        return round(0.3 * f_score + 0.4 * e_score + 0.3 * v_score, 3)

    # ---------- черновик ----------

    def build_draft(self) -> dict:
        metrics = self.metrics.to_dict()
        extract = {'tier': 'llm'}
        if self.profile is not None and self.profile.extract.tier == 'custom':
            extract = {'tier': 'custom', 'custom_module': self.profile.extract.custom_module}
        draft = {
            'domain': self.metrics.domain,
            'profile_version': (self.profile.profile_version + 1) if self.profile is not None else 1,
            'profiled_at': date.today().isoformat(),
            'confidence': self.confidence(metrics),
            'source': 'census',
            'notes': [],
            'crawl': {},
            'sections': self._infer_sections(),
            'extract': extract,
            'baseline': {
                key: metrics.get(key) for key in (
                    'structure_hash', 'selector_hit_rate', 'pages_crawled',
                    'product_pages', 'product_cards', 'card_yield', 'avg_specs_per_card',
                    'jsonld_share', 'cms_detected',
                    'fetch_fail_rate', 'challenge_rate', 'empty_markdown_rate',
                    'gate3_pass_rate', 'sitemap_url_count', 'extraction_path_share',
                    'limits_used')
            },
            'census_evidence': {
                'fetch_methods': metrics.get('fetch_methods'),
                'pages_by_category': metrics.get('pages_by_category'),
                'alerts': metrics.get('alerts'),
                'top_urls': {
                    category: [u for u, c in self.metrics.page_urls if c == category][:10]
                    for category in ('product', 'category', 'contacts', 'distributor', 'price_list')
                },
            },
        }
        draft['baseline']['section_counts'] = metrics.get('pages_by_category')
        render = self._infer_render(metrics)
        if render:
            draft['crawl']['render'] = render
        antibot = self._infer_antibot(metrics)
        if antibot != 'none':
            draft['crawl']['antibot'] = antibot
        if not draft['crawl']:
            del draft['crawl']
        return draft

    def write_draft(self, drafts_dir: str) -> str:
        """Пишет черновик YAML (валидируя схемой) и возвращает путь; при низкой
        confidence добавляет строку в _review_queue.md. Fail-open: любая ошибка
        логируется, пайплайн продолжается."""
        try:
            import yaml
            from .models import profile_from_dict
            draft = self.build_draft()
            _, errors = profile_from_dict(draft)
            if errors:
                logger.warning(f'Черновик профиля {self.metrics.domain} невалиден: {errors}')
                return ''
            os.makedirs(drafts_dir, exist_ok=True)
            path = os.path.join(drafts_dir, f'{self.metrics.domain}.yaml')
            with open(path, 'w', encoding='utf-8') as fh:
                yaml.safe_dump(draft, fh, allow_unicode=True, sort_keys=False, width=100)
            logger.info(f'Census: черновик профиля записан: {path} '
                        f'(confidence={draft["confidence"]})')
            if draft['confidence'] < 0.8:
                self._append_review(drafts_dir, draft)
            return path
        except Exception as e:
            logger.warning(f'Census: черновик профиля {self.metrics.domain} не записан: {e}')
            return ''

    def _append_review(self, drafts_dir: str, draft: dict) -> None:
        queue_path = os.path.join(drafts_dir, '_review_queue.md')
        alerts = (draft.get('census_evidence') or {}).get('alerts') or []
        line = (f'- {date.today().isoformat()} `{draft["domain"]}` '
                f'confidence={draft["confidence"]}'
                + (f', алерты: {"; ".join(alerts)}' if alerts else '') + '\n')
        with open(queue_path, 'a', encoding='utf-8') as fh:
            fh.write(line)
