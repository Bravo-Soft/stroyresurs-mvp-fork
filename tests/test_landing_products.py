"""Реальные лендинги: окна, hover и раскрываемые спецификации не смешивают товары."""
import asyncio
from collections import deque
from pathlib import Path
from urllib.parse import quote, urljoin, unquote
from unittest.mock import AsyncMock

from bs4 import BeautifulSoup
import pytest

from config import Config
from inline_products import inline_product_key, inline_product_links, isolate_inline_product
from site_profiles import profile_from_dict
from text_extractor import html_to_markdown
from web_crawler import WebCrawler, PageContent

FIXTURES = Path(__file__).parent / 'fixtures/landing_products'
SITES = [
    ('fzlomov', 'https://fzlomov.ru/', '.plywood > .item', '.title', 7),
    ('ezim', 'https://ezim-prom.ru/', '.product', '.product__name', 16),
    ('beton', 'https://beton-zavod.ru/', '#katalog .product', '.product__title', 34),
]


def source(slug):
    return (FIXTURES / (slug + '.html')).read_text()


def expected_url(base, card, name_selector):
    if name_selector == '.product__title':
        return base + '#:~:text=' + quote(card.select_one('.product__code').get_text(' ', strip=True), safe='') + '-,' + quote(card.select_one(name_selector).get_text(' ', strip=True), safe='')
    return base + ('#' + card['id'] if card.get('id') else '#:~:text=' + quote(card.select_one(name_selector).get_text(' ', strip=True), safe=''))


@pytest.fixture
def crawler(tmp_path):
    cfg = Config()
    cfg.base_dir = str(tmp_path)
    cfg.graph_db_enable = cfg.vector_db_enable = cfg.census_enabled = False
    c = WebCrawler(cfg)
    c._fetch_with_aiohttp = AsyncMock(return_value=None)
    c._fetch_with_playwright = AsyncMock(return_value=(None, False, None))
    return c


@pytest.mark.parametrize('slug,base,selector,name_selector,count', SITES)
def test_landing_discovers_all_independent_product_states(crawler, slug, base, selector, name_selector, count):
    crawler.current_base_url = base
    crawler._apply_site_profile(base)
    soup = BeautifulSoup(source(slug), 'html.parser')
    expected = {expected_url(base, card, name_selector) for card in soup.select(selector)}
    assert len(expected) == count
    links = asyncio.run(crawler._extract_additional_links(soup, base))
    found = {url for url, cat, _ in links if cat == 'product' and '#' in url}
    assert found == expected
    keys = {crawler.url_normalizer.normalize_url(url) for url in expected | {base}}
    assert len(keys) == count + 1


@pytest.mark.parametrize('slug,base,selector,name_selector,count', SITES)
def test_every_state_keeps_its_own_content_and_documents(crawler, slug, base, selector, name_selector, count):
    raw = source(slug)
    soup = BeautifulSoup(raw, 'html.parser')
    crawler.current_base_url = base
    crawler._apply_site_profile(base)
    async def run():
        for card in soup.select(selector):
            url = expected_url(base, card, name_selector)
            crawler._http_first_get = AsyncMock(return_value=PageContent(raw, url, base, method='http_first'))
            outcome = await crawler.fetch_page(url, 'product')
            if slug == 'beton' and not card.select_one('.product__spec').get_text(strip=True):
                assert outcome.content is None and outcome.reason == 'incomplete_render'
                crawler._fetch_with_playwright.reset_mock()
                continue
            assert outcome.reason == 'ok', (url, outcome.reason)
            parsed = outcome.content
            assert parsed.url == url and parsed.canonical_url == crawler.url_normalizer.normalize_url(url)
            name = card.select_one(name_selector).get_text(' ', strip=True)
            assert parsed.soup.h1.get_text(' ', strip=True) == name
            md = html_to_markdown(parsed.html, url)
            assert name.replace('*', 'х') in md or name in md
            assert len(parsed.soup.find_all('h1')) == 1
            if slug == 'fzlomov':
                document_base = soup.find('base', href=True)
                link_base = urljoin(base, document_base['href']) if document_base else base
                expected = {urljoin(link_base, a['href']) for a in card.select('.sertif-block a.pdf_link')}
                actual = {unquote(link) for link in crawler._extract_file_links_from_soup(parsed.soup, url)}
                assert actual == expected, (name, actual ^ expected)
                assert card.select_one('.more-block .information').get_text(' ', strip=True) in parsed.soup.get_text(' ', strip=True)
            elif slug == 'ezim':
                assert card.select_one('.product__text').get_text(' ', strip=True) in parsed.soup.get_text(' ', strip=True)
                assert 'Выберите город' not in md and 'Заказать' not in md
            else:
                # Все ячейки исходной таблицы остаются в том же порядке и с теми же значениями.
                assert [x.get_text(' ', strip=True) for x in card.select('.spec__table th, .spec__table td')] == [x.get_text(' ', strip=True) for x in parsed.soup.select('table th, table td')]
                assert 'Марка бетона' in md and 'Спецификация' not in parsed.soup.select_one('h1').get_text()
        crawler._fetch_with_playwright.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize('slug,base,selector,name_selector,count', SITES)
def test_unknown_product_does_not_become_whole_landing(crawler, slug, base, selector, name_selector, count):
    crawler.current_base_url = base
    url = base + ('#product-999' if slug == 'fzlomov' else '#:~:text=missing-,product' if slug == 'beton' else '#:~:text=missing-product')
    crawler._http_first_get = AsyncMock(return_value=source(slug))
    outcome = asyncio.run(crawler.fetch_page(url, 'product'))
    assert outcome.content is None and outcome.reason == 'incomplete_render'


def test_unknown_hosts_and_navigation_anchors_keep_old_identity(crawler):
    for url in ['https://other.test/#product-1', 'https://other.test/#:~:text=Example',
                'https://fzlomov.ru/#kinds', 'https://beton-zavod.ru/#katalog']:
        assert crawler.url_normalizer.normalize_url(url).endswith('/')


@pytest.mark.parametrize('slug,base,selector,name_selector,count', SITES)
def test_root_queue_and_saved_products_stay_distinct(crawler, slug, base, selector, name_selector, count):
    async def run():
        company = 'Лендинг тест'
        await crawler.reset_state(company, crawl_run_id='landing-test')
        crawler.current_base_url = base
        crawler._apply_site_profile(base)
        crawler._http_first_get = AsyncMock(return_value=source(slug))
        crawler._check_memory_usage = AsyncMock()
        queue, saved = deque(), []
        await crawler._process_page_with_storage(base, 0, 'main_page', company, {}, queue, saved)
        product_tasks = [row for row in queue if row[2] == 'product' and inline_product_key(row[0])]
        assert len(product_tasks) == count
        for url, depth, category, _ in product_tasks:
            await crawler._process_page_with_storage(url, depth, category, company, {}, deque(), saved)
        products = [row for row in saved if row['category'] == 'product']
        populated = count - (slug == 'beton')
        assert len(products) == populated
        assert len({row['normalized_url'] for row in products}) == populated
        assert len({row['file_path'] for row in products}) == populated
        await crawler.temp_storage.mark_as_ai_processed(products[0]['file_path'])
        assert not await crawler.temp_storage.is_page_processed(products[1]['url'], company, 'product')
    asyncio.run(run())


@pytest.mark.parametrize('slug,base,selector,name_selector,count', SITES)
def test_reordering_cards_does_not_change_identity(slug, base, selector, name_selector, count):
    soup = BeautifulSoup(source(slug), 'html.parser')
    expected = set(inline_product_links(soup, base))
    cards = soup.select(selector)
    parent = cards[0].parent
    for card in reversed(cards):
        parent.append(card.extract())
    assert set(inline_product_links(soup, base)) == expected
    url = expected_url(base, cards[0], name_selector)
    selected = isolate_inline_product(str(soup), url)
    assert isolate_inline_product(selected, url) == selected
    other = expected_url(base, cards[1], name_selector)
    assert isolate_inline_product(selected, other) is None


@pytest.mark.parametrize('slug,base,selector,name_selector,count', SITES)
def test_ambiguous_keys_and_missing_panels_are_not_other_products(slug, base, selector, name_selector, count):
    soup = BeautifulSoup(source(slug), 'html.parser')
    card = soup.select_one(selector)
    url = expected_url(base, card, name_selector)
    card.insert_after(BeautifulSoup(str(card), 'html.parser'))
    assert url not in inline_product_links(soup, base)
    assert isolate_inline_product(str(soup), url) is None
    soup = BeautifulSoup(source(slug), 'html.parser')
    card = soup.select_one(selector)
    card.select_one({'fzlomov': '.more-block', 'ezim': '.product__text', 'beton': '.product__spec'}[slug]).clear()
    assert isolate_inline_product(str(soup), url) is None


@pytest.mark.parametrize('field,value', [('item_selector', ''), ('content_selectors', []),
    ('paths', ['/','']), ('id_pattern', '['), ('name_prefix_selector', 7), ('typo', True)])
def test_invalid_inline_profile_rejected(field, value):
    settings = dict(item_selector='.item', name_selector='h3', content_selectors=['.text'])
    settings[field] = value
    _, errors = profile_from_dict({'domain': 'example.test', 'crawl': {'inline_products': settings}})
    assert any('crawl.inline_products' in error and field in error for error in errors)


def test_unprofiled_paths_do_not_gain_product_states():
    for url in ['https://fzlomov.ru/news/#product-1', 'https://ezim-prom.ru/news/#:~:text=Ткань']:
        assert inline_product_key(url) is None
        assert isolate_inline_product('<h1>Новости</h1>', url) == '<h1>Новости</h1>'


@pytest.mark.parametrize('slug,base,selector,name_selector,count', SITES[1:])
def test_text_fragment_encoding_does_not_duplicate_product(crawler, slug, base, selector, name_selector, count):
    card = BeautifulSoup(source(slug), 'html.parser').select_one(selector)
    encoded = expected_url(base, card, name_selector)
    assert crawler.url_normalizer.normalize_url(unquote(encoded)) == crawler.url_normalizer.normalize_url(encoded)
    crawler.temp_storage.url_normalizer = None
    assert crawler.temp_storage._metadata_identity({'url': unquote(encoded)}) == encoded
