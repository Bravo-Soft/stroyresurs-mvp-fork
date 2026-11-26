"""Карта обнаруженных URL и разнообразная выборка; без записи, LLM и графа."""
import asyncio
import hashlib
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from urllib.parse import parse_qsl, urljoin, urlsplit

from bs4 import BeautifulSoup
from .format_v2 import digest, select_page, ProfileRuleError
from product_interactions import product_state_key, PRODUCT_PANEL_SELECTOR
from data_island import needs_javascript, is_unrendered_store_listing, has_unloaded_product_tabs
from web_crawler import document_base_url, extract_element_url, sanitize_href, page_error_reason


@dataclass(frozen=True)
class DiscoveryLimits:
    pages: int = 60
    urls: int = 10000
    depth: int = 5
    seconds: int = 300
    sample: int = 30

    def __post_init__(self):
        for name, value in asdict(self).items():
            if type(value) is not int or value < (0 if name == 'depth' else 1):
                raise ValueError(f'{name}: неверный лимит')


def url_shape(url):
    """Группа, а не ключ дедупликации: значения query не теряются в самой карте."""
    parsed = urlsplit(url)
    parts = [p for p in parsed.path.split('/') if p]
    shaped = []
    for index, part in enumerate(parts):
        if part.isdigit():
            shaped.append('{number}')
        elif index == len(parts) - 1:
            ext = re.search(r'\.[a-zA-Z]{2,5}$', part)
            shaped.append('{leaf}' + (ext[0] if ext else ''))
        else:
            shaped.append(part)
    query = sorted({key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)})
    state = product_state_key(url)
    return parsed.netloc.lower() + '/' + '/'.join(shaped) + (
        '?' + '&'.join(query) if query else '') + ('#product-state' if state else '')


def html_shape(soup):
    """Множество структурных путей: текст и число однотипных строк не дробят группу."""
    root = soup.select_one('main, [role="main"], article') or soup.body or soup
    paths = set()
    def walk(node, path, depth):
        if depth > 8 or not getattr(node, 'name', None) or node.name in (
                'script', 'style', 'noscript', 'svg', 'nav', 'header', 'footer'):
            return
        classes = [c for c in node.get('class', []) if not re.search(r'\d|active|selected|open', c)]
        token = node.name + ''.join('.'+c for c in sorted(classes)[:4])
        current = path + '/' + token
        paths.add(current)
        for child in node.children:
            walk(child, current, depth + 1)
    walk(root, '', 0)
    return digest(sorted(paths))[:20]


def html_features(soup, html):
    """Признаки разнообразия и неопределённости; не доказательства качества карточек."""
    flags = []
    for name, selector in [('table', 'table'), ('definition_list', 'dl'),
                           ('tabs', PRODUCT_PANEL_SELECTOR),
                           ('product_grid', '[data-product-lid], .product-item, .product-card'),
                           ('pagination', '[rel="next"], .pagination, .pager')]:
        if soup.select_one(selector): flags.append(name)
    if needs_javascript(html) or is_unrendered_store_listing(html) or has_unloaded_product_tabs(html):
        flags.append('unresolved_dynamic')
    for button in soup.select('button, [role="button"], a[href="#"]'):
        label = button.get_text(' ', strip=True)
        if re.search(r'показать\s+ещ[её]|загрузить\s+ещ[её]|load\s*more|show\s*more', label, re.I):
            flags.append('load_more'); break
    return sorted(set(flags))


class StructureDiscovery:
    """Обнаружение поверх штатного краулера. Один экземпляр — один сайт/прогон."""
    def __init__(self, crawler, base_url, limits=None, card_outcomes=None):
        self.crawler = crawler
        parsed = urlsplit(base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError('Нужен абсолютный HTTP(S) URL сайта')
        self.base_url = base_url
        self.limits = limits or DiscoveryLimits()
        self.records = {}
        self.gaps = set()
        self.sitemap_events = []
        self.attempted = []
        self.controls = set()
        self.holdouts = set()
        self.card_outcomes = card_outcomes or {}
        for result in self.card_outcomes.values():
            if (not isinstance(result, dict) or type(result.get('count')) is not int
                    or result['count'] < 0 or not isinstance(result.get('source'), str) or not result['source']):
                raise ValueError('card_outcomes: нужны count >= 0 и источник прежнего результата')
        self.started = time.monotonic()
        self.crawler.current_base_url = base_url

    def _reason(self, url, role, depth):
        c = self.crawler
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname: return 'invalid_url'
            perimeter = getattr(c, '_structure_perimeter_allowed', c._is_allowed_url)
            if not perimeter(url, base_url=self.base_url): return 'out_of_scope'
            if c.url_categorizer.is_downloadable_file(url): return 'document_not_fetched'
            if c.url_categorizer._is_image_url(url): return 'asset_not_fetched'
            if role in ('excluded', 'excluded2'): return 'url_excluded'
            if c.url_categorizer.should_exclude_by_language(url): return 'language_excluded'
            if c.url_normalizer.is_over_pagination_limit(url): return 'pagination_limit'
            if depth > self.limits.depth: return 'depth_limit'
        except (ValueError, TypeError): return 'invalid_url'
        return None

    def add(self, url, source, parent='', depth=0, role=None, priority=None):
        """Опубликованный адрес сохраняется отдельно от штатного ключа дедупа."""
        try:
            key = self.crawler.url_normalizer.normalize_url(url)
            category, rank = self.crawler.url_categorizer.categorize_url(url)
            # Явные исключения сильнее товарной сетки/подсказки источника.
            if category not in ('excluded', 'excluded2'):
                category, rank = role or category, priority if priority is not None else rank
            reason = self._reason(url, category, depth)
        except (ValueError, TypeError):
            key, category, rank, reason = str(url), 'other', 0, 'invalid_url'
        if key not in self.records:
            if len(self.records) >= self.limits.urls:
                self.gaps.add('url_limit'); return None
            self.records[key] = dict(url=url, key=key, aliases=[], sources=[], depth=depth,
                                     role=category, priority=rank, state='excluded' if reason else 'pending',
                                     reason=reason, url_group=url_shape(url) if reason != 'invalid_url' else 'invalid',
                                     html_group=None, features=[], roles=[category])
        row = self.records[key]
        if row['state'] == 'pending' and rank > row['priority']:
            row.update(role=category, priority=rank, roles=[category])
        if url != row['url'] and url not in row['aliases']: row['aliases'].append(url)
        provenance = dict(kind=source, parent=parent)
        if provenance not in row['sources']: row['sources'].append(provenance)
        # Встретив более короткий путь, можно снять только ограничение глубины.
        if depth < row['depth']:
            row['depth'] = depth
            if row['reason'] == 'depth_limit' and reason is None:
                row.update(state='pending', reason=None)
        if source == 'control': self.controls.add(key)
        if source == 'holdout': self.holdouts.add(key)
        return row

    def sitemap_sink(self, event, **data):
        if event == 'url': self.add(data['url'], 'sitemap', data['source'])
        else:
            self.sitemap_events.append(dict(event=event, **data))
            if event == 'limit': self.gaps.add('sitemap_limit')

    async def discover_sitemaps(self):
        parser = self.crawler.sitemap_parser
        parser.discovery_sink = self.sitemap_sink
        try:
            maps = await parser.collect_sitemap_urls(self.base_url)
            urls = sorted({r[0] for rows in maps for r in rows})
            self.crawler.url_categorizer.observe_urls(urls)
            for rows in maps:
                for url, _, role, priority in rows:
                    self.add(url, 'sitemap', role=role, priority=priority)
            if not urls: self.gaps.add('sitemap_not_found_or_unreadable')
            if parser.phase_timed_out: self.gaps.add('sitemap_timeout')
            self.sitemap_events.extend(dict(event='map', url=key[0], remaining_depth=key[1], status=status)
                                       for key, status in sorted(parser._states.items()))
            if any(s != 'success' for s in parser._states.values()):
                self.gaps.add('sitemap_incomplete')
        finally:
            parser.discovery_sink = None

    async def ingest(self, row, html, final_url=None, transport='saved'):
        final_url = final_url or row['url']
        if not self.crawler._is_allowed_final_url(row['url'], final_url):
            row.update(state='failed', reason='redirect_out_of_scope'); return
        final_role, _ = self.crawler.url_categorizer.categorize_url(final_url)
        reason = self._reason(final_url, final_role, row['depth'])
        if reason:
            row.update(state='failed', reason='redirect_'+reason); return
        error = page_error_reason(html)
        if error:
            row.update(state='failed', reason=error); return
        soup = BeautifulSoup(html, 'html.parser')
        features = html_features(soup, html)
        row.update(state='loaded', reason=None, final_url=final_url, transport=transport,
                   html_sha256=hashlib.sha256(html.encode()).hexdigest(), html_group=html_shape(soup),
                   features=features, content_role=self.crawler._detect_content_type(soup, final_url))
        # URL-роль не заменяется догадкой по HTML; противоречие остаётся в отчёте.
        if row['role'] == 'product' and row['content_role'] != 'product':
            row['features'].append('product_without_content_signal')
        if row['url'] in self.card_outcomes:
            row['previous_card_outcome'] = self.card_outcomes[row['url']]
            if row['previous_card_outcome']['count'] == 0:
                row['features'].append('zero_cards_previous_run')
        profile = self.crawler.profile
        if profile:
            path = urlsplit(final_url).path.lower()
            for name, prefixes in [('contacts', profile.sections.contacts_urls),
                                   ('distributor', profile.sections.distributor_urls)]:
                if self.crawler.url_categorizer._longest_prefix(path, prefixes) >= 0:
                    row['roles'] = sorted(set(row['roles'] + [name]))
            if profile.schema_version == 2:
                page_type = {'contacts':'company','main_page':'company','distributor':'distributor'}.get(row['role'],'product')
                try:
                    chosen = select_page(profile, final_url, page_type, soup)
                    row['page_rule'] = chosen.diagnostics['selected_rule']
                except ProfileRuleError as exc:
                    row['rule_error'] = str(exc); self.gaps.add('page_rule_error')
        if set(features) & {'unresolved_dynamic', 'load_more'}:
            self.gaps.add('dynamic_not_exhausted')
        # Контрольная отложенная страница не раскрывает обучающему обходу свои ссылки.
        if row['key'] in self.holdouts: return
        link_base = document_base_url(soup, final_url)
        for node in soup.select('a[href], link[rel="next"], [onclick], [data-href], [data-url]'):
            href = sanitize_href(node.get('href') or extract_element_url(node) or '')
            if not href or href.startswith(('javascript:', 'mailto:', 'tel:')): continue
            address = urljoin(link_base, href)
            if href.startswith('#') and not product_state_key(address): continue
            role, priority = self.crawler.url_categorizer.categorize_url(address, node.get_text(' ', strip=True)[:200])
            source = 'pagination' if 'next' in (node.get('rel') or []) else 'link'
            self.add(address, source, final_url, row['depth']+1, role, priority)
        # Товарные сетки, onclick и опубликованные состояния — штатный сборщик.
        for address, role, priority in await self.crawler._extract_additional_links(soup, final_url):
            self.add(address, 'crawler_link', final_url, row['depth']+1, role, priority)

    def _next(self):
        pending = [r for r in self.records.values() if r['state'] == 'pending']
        if not pending: return None
        counts = Counter(self.records[k]['url_group'] for k in self.attempted)
        def order(row):
            control = row['key'] in self.controls
            # Главная/каталоги и контакты не вытесняются тысячами товаров из sitemap.
            navigation = row['role'] in ('main_page','category','contacts','distributor')
            return (row['key'] in self.holdouts, not control, counts[row['url_group']],
                    not navigation, row['depth'], -row['priority'], row['url'])
        return min(pending, key=order)

    async def run(self, fetch, controls=(), holdouts=(), sitemap=True):
        """fetch(url, role) возвращает FetchOutcome; отчёт не создаёт карточек."""
        self.add(self.base_url, 'start')
        for source, urls in [('control', controls), ('holdout', holdouts)]:
            for url in urls:
                if self.add(url, source) is None:
                    raise ValueError('Лимит URL меньше обязательных контрольных примеров')
        if self.controls & self.holdouts: raise ValueError('controls и holdouts пересекаются')
        if sitemap: await self.discover_sitemaps()
        else: self.gaps.add('sitemap_not_checked')
        while len(self.attempted) < self.limits.pages:
            row = self._next()
            if row is None: break
            remaining = self.limits.seconds - (time.monotonic()-self.started)
            if remaining <= 0: self.gaps.add('time_limit'); break
            self.attempted.append(row['key'])
            try:
                outcome = await asyncio.wait_for(fetch(row['url'], row['role']), remaining)
                if not outcome.content:
                    row.update(state='failed', reason=outcome.reason, retryable=outcome.retryable)
                    continue
                contents = outcome.content if isinstance(outcome.content, list) else [outcome.content]
                for index, content in enumerate(contents):
                    html = content.html if hasattr(content, 'html') else str(content)
                    final = getattr(content, 'url', None) or outcome.final_url or row['url']
                    if index == 0:
                        await self.ingest(row, html, final, outcome.transport)
                    else:
                        # Ссылки всех состояний участвуют в обнаружении; первый DOM не теряем.
                        state = dict(row)
                        await self.ingest(state, html, final, outcome.transport)
                        row.setdefault('extra_states', []).append({key:state.get(key) for key in
                            ('final_url','html_sha256','html_group','features','state','reason')})
                        row['features'] = sorted(set(row['features']) | set(state['features']))
                        self.gaps.add('multiple_states_need_review')
            except asyncio.TimeoutError:
                row.update(state='failed', reason='timeout'); self.gaps.add('time_limit')
            except Exception as exc:
                row.update(state='failed', reason='fetch_or_parse_error', error=str(exc))
        for row in self.records.values():
            if row['state'] == 'pending':
                row.update(state='not_fetched', reason='time_limit' if 'time_limit' in self.gaps else 'page_limit')
                self.gaps.add(row['reason'])
        return self.report()

    def report(self):
        rows = sorted(self.records.values(), key=lambda r:r['key'])
        groups = defaultdict(list)
        for row in rows:
            row['forced'] = row['key'] in self.controls or row['key'] in self.holdouts
            row['independently_discovered'] = any(s['kind'] not in ('control','holdout') for s in row['sources'])
            row['sample_split'] = 'holdout' if row['key'] in self.holdouts else 'discovery'
            row['group'] = digest([row['role'],row['url_group'],row['html_group'],row.get('page_rule'),
                                  [s['html_group'] for s in row.get('extra_states',[])]])[:20]
            groups[row['group']].append(row)
        # Обязательные пользовательские URL всегда отражены, даже при превышении лимита выборки.
        selected = {r['key'] for r in rows if r['forced']}
        remaining = [r for r in rows if r['key'] not in selected]
        covered_groups = {r['group'] for r in rows if r['key'] in selected and r['sample_split'] != 'holdout'}
        features = set()
        for row in rows:
            if row['key'] in selected and row['sample_split'] != 'holdout': features.update(row['features'])
        while remaining and len(selected) < self.limits.sample:
            def score(r):
                negative = r['role'] != 'product' or r['state'] != 'loaded' or 'product_without_content_signal' in r['features']
                additions = set(r['features']) | ({'negative'} if negative else set())
                return (-(r['group'] not in covered_groups), -len(additions-features),
                        r['state'] != 'loaded', r['url'])
            row = min(remaining, key=score); remaining.remove(row); selected.add(row['key'])
            covered_groups.add(row['group']); features.update(row['features'])
            if row['role'] != 'product' or row['state'] != 'loaded': features.add('negative')
        group_rows=[]
        for key, members in sorted(groups.items()):
            group_rows.append(dict(id=key, role=members[0]['role'], url_shape=members[0]['url_group'],
                                   html_shape=members[0]['html_group'], urls=[r['key'] for r in members],
                                   sampled=any(r['key'] in selected and r['sample_split'] != 'holdout' for r in members)))
        if any(not g['sampled'] for g in group_rows): self.gaps.add('unsampled_groups')
        if len(selected) > self.limits.sample: self.gaps.add('mandatory_sample_overflow')
        if not self.holdouts: self.gaps.add('holdout_not_provided')
        if any(r['state']=='failed' for r in rows): self.gaps.add('fetch_failures')
        if any(r['rule_error'] for r in rows if 'rule_error' in r): self.gaps.add('page_rule_error')
        if any(r['reason'] in ('depth_limit','pagination_limit') for r in rows): self.gaps.add('traversal_limit')
        return dict(report_version=1, base_url=self.base_url, limits=asdict(self.limits),
                    profile_sha256=digest(asdict(self.crawler.profile)) if self.crawler.profile else None,
                    profile=dict(domain=self.crawler.profile.domain,
                                 schema_version=self.crawler.profile.schema_version,
                                 profile_version=self.crawler.profile.profile_version,
                                 dependencies=self.crawler.profile.diagnostics.get('dependencies',{}))
                            if self.crawler.profile else None,
                    urls=rows, groups=group_rows, sample=[r['key'] for r in rows if r['key'] in selected],
                    counts=dict(Counter(r['state'] for r in rows)),
                    attempted=[self.records[k]['url'] for k in self.attempted],
                    sitemap=self.sitemap_events, coverage=dict(scope='observed_urls_only',
                    status='partial' if self.gaps else 'observed_scope_checked', gaps=sorted(self.gaps),
                    cards='not_run', live='not_asserted'))
