"""Выделение опубликованных товаров лендинга по проверенному профилю разметки."""
import re
from collections import Counter
from copy import deepcopy
from urllib.parse import quote, unquote, urlparse, urlunparse

from bs4 import BeautifulSoup
from site_profiles import get_resolver


def inline_profile(url):
    profile = get_resolver().resolve(url).crawl.inline_products
    path = urlparse(url).path or '/'
    return profile if profile and path in profile.paths else None


def inline_product_key(url):
    parsed = urlparse(url)
    if ((parsed.hostname or '').lower().removeprefix('www.') in
            {'bsdbel.ru', 'ventexpress.ru', 'nzmk22.ru', 'npo-stekloplastic.ru'}
            and re.fullmatch(r'source-family=.+', parsed.fragment)):
        return 'source-family=' + quote(unquote(parsed.fragment.partition('=')[2]), safe='')
    profile = inline_profile(url)
    if profile is None:
        return None
    fragment = urlparse(url).fragment
    if profile.id_pattern:
        return fragment if re.fullmatch(profile.id_pattern, fragment) else None
    match = re.fullmatch(r':~:text=([^&]+)', fragment)
    if not match:
        return None
    # Сохраняем синтаксис стандартного текстового фрагмента prefix-,text.
    parts = match[1].split('-,', 1) if profile.name_prefix_selector else [match[1]]
    if profile.name_prefix_selector and len(parts) != 2:
        return None
    return ':~:text=' + '-,'.join(quote(unquote(part), safe='') for part in parts)


def _items(soup, profile):
    rows = []
    for item in soup.select(profile.item_selector):
        title = item.select_one(profile.name_selector)
        name = title.get_text(' ', strip=True) if title else ''
        if not name:
            continue
        key = item.get('id', '') if profile.id_pattern else ':~:text=' + quote(name, safe='')
        if profile.name_prefix_selector and not profile.id_pattern:
            prefix = item.select_one(profile.name_prefix_selector)
            prefix = prefix.get_text(' ', strip=True) if prefix else ''
            if not prefix:
                continue
            key = ':~:text=' + quote(prefix, safe='') + '-,' + quote(name, safe='')
        if profile.id_pattern and not re.fullmatch(profile.id_pattern, key):
            continue
        rows.append((key, name, item))
    # Два одинаковых ключа не позволяют доказать, какой товар выбран.
    counts = Counter(key for key, _, _ in rows)
    return [(key, name, item) for key, name, item in rows if counts[key] == 1]


def inline_product_links(soup, url):
    from source_product_tables import source_catalog_families
    families = source_catalog_families(str(soup), url)
    if families:
        return [family['url'] for family in families]
    profile = inline_profile(url)
    if profile is None or soup.select_one('main[data-inline-product]'):
        return []
    return [urlunparse(urlparse(url)._replace(fragment=key)) for key, _, _ in _items(soup, profile)]


def isolate_inline_product(html, url):
    from source_product_tables import source_catalog_families
    if urlparse(url).fragment.startswith('source-family='):
        marker = BeautifulSoup(html, 'lxml').select_one('main[data-source-family]')
        if marker is not None:
            return html if marker.get('data-source-family') == inline_product_key(url) else None
    for family in source_catalog_families(html, url):
        if urlparse(family['url']).fragment == urlparse(url).fragment:
            return family['html']
    key = inline_product_key(url)
    if key and key.startswith('source-family='):
        return None
    if key is None:
        return html
    soup = BeautifulSoup(html, 'html.parser')
    marker = soup.select_one('main[data-inline-product]')
    if marker is not None:
        return html if marker.get('data-inline-product') == key else None
    profile = inline_profile(url)
    selected = [(name, item) for candidate, name, item in _items(soup, profile) if candidate == key]
    if len(selected) != 1:
        return None
    name, item = selected[0]
    blocks = []
    for selector in profile.content_selectors:
        block = item.select_one(selector)
        if block is None or not block.get_text(' ', strip=True):
            return None
        blocks.append(block)
    result = BeautifulSoup('<html><head></head><body><main></main></body></html>', 'html.parser')
    if soup.find('base', href=True):
        result.head.append(deepcopy(soup.find('base', href=True)))
    title = result.new_tag('title')
    title.string = name
    result.head.append(title)
    result.main['data-inline-product'] = key
    heading = result.new_tag('h1')
    heading.string = name
    result.main.append(heading)
    for block in blocks:
        block = deepcopy(block)
        block.name = 'section'
        block.attrs = {}  # Состояние hidden/display:none относится к выбранному окну.
        for noise in block.select('script, style, form, button, a.close'):
            noise.decompose()
        for heading in block.select('.title'):
            heading.name = 'h2'
        result.main.append(block)
    return str(result)
