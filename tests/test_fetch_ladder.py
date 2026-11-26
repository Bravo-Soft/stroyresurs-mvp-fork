"""Пригодность содержимого и переходы лестницы; транспорт заменён, разбор настоящий."""
import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import web_crawler as wc
import data_island
from config import Config
from fetch_outcome import observe_fetch
from site_profiles.models import SiteProfile

URL = 'https://factory.test/catalog/pump-100/'
FULL = '<html><body><h1>Насос 100</h1>' + '<p>Напор 20 м, расход 30 м3/ч, масса 40 кг.</p>' * 40 + '</body></html>'
SHELL = '<html><body><div id="app"></div><script src="/main.bundle.js"></script></body></html>'
TAB = '<button role="tab" aria-controls="specs">Характеристики</button><div id="specs" role="tabpanel"></div>'
PENDING = FULL.replace('</body>', TAB + '</body>')
MERGED = PENDING.replace('</body>', '<section data-tab="Характеристики"><h2>Характеристики</h2><p>Давление 16 бар</p></section></body>')


@pytest.fixture
def crawler(tmp_path):
    cfg = Config()
    cfg.base_dir = str(tmp_path)
    c = wc.WebCrawler(cfg)
    c.current_base_url = 'https://factory.test/'
    c._http_first_get = AsyncMock(return_value=None)
    c._fetch_with_http_impersonate = AsyncMock(return_value=None)
    c._fetch_with_aiohttp = AsyncMock(return_value=None)
    c._fetch_with_playwright = AsyncMock(return_value=(FULL, False, URL))
    c.dynamic = SimpleNamespace(extract=AsyncMock(return_value=[]),
                                extract_product_tabs_merged=AsyncMock(return_value=MERGED))
    c._make_dynamic_extractor = lambda: c.dynamic
    return c


@pytest.mark.parametrize('category', ['product', 'category', 'main_page', 'contacts', 'distributor'])
def test_static_http_stops_before_browser(crawler, category):
    crawler.config.dynamic_content_enabled = False
    crawler._http_first_get.return_value = FULL
    result = asyncio.run(crawler.fetch_page(URL, category))
    assert result.content.html == FULL and result.transport == 'http_first'
    crawler._fetch_with_aiohttp.assert_not_awaited()


@pytest.mark.parametrize('category', ['contacts', 'distributor'])
@pytest.mark.parametrize('profile_ready', [False, True])
def test_incomplete_contacts_cannot_fall_back_to_static_html(crawler, category, profile_ready):
    if profile_ready:
        crawler.profile = SiteProfile()
        crawler.profile.crawl.contact_ready_selector = '#loaded-dealers'
        crawler.profile.crawl.antibot = 'warmup'
    crawler._http_first_get.return_value = FULL
    async def incomplete(url):
        crawler.dynamic.incomplete_render = True
        observe_fetch(url, 'dynamic', reason='incomplete_render')
        return []
    crawler.dynamic.extract = AsyncMock(side_effect=incomplete)
    result = asyncio.run(crawler.fetch_page(URL, category))
    assert not result.content and result.reason == 'incomplete_render'
    crawler._http_first_get.assert_not_awaited()
    crawler._fetch_with_aiohttp.assert_not_awaited()
    crawler._fetch_with_playwright.assert_not_awaited()


@pytest.mark.parametrize('category', ['product', 'category', 'main_page', 'contacts'])
def test_js_shell_reaches_browser_even_with_product_link(crawler, category):
    crawler.config.dynamic_content_enabled = False
    shell = SHELL.replace('</body>', '<a href="/catalog/pump-200/">Товар</a></body>')
    crawler._http_first_get.return_value = shell
    crawler._fetch_with_aiohttp.return_value = shell
    result = asyncio.run(crawler.fetch_page(URL, category))
    assert result.content.html == FULL and result.transport == 'playwright'
    crawler._fetch_with_playwright.assert_awaited_once()


@pytest.mark.parametrize('stage', ['http', 'aiohttp'])
@pytest.mark.parametrize('error', [TimeoutError(), OSError('connection lost'), ValueError('bad response')])
def test_stage_exception_does_not_abort_remaining_ladder(crawler, stage, error):
    target = crawler._http_first_get if stage == 'http' else crawler._fetch_with_aiohttp
    target.side_effect = error
    result = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert result.content.html == FULL and result.transport == 'playwright'


def test_parse_failure_falls_through_to_next_transport(crawler):
    crawler._http_first_get.return_value = FULL
    crawler._fetch_with_aiohttp.return_value = FULL
    original = crawler._parse_content
    calls = []
    async def parse(html, url):
        calls.append(url)
        if len(calls) == 1:
            raise ValueError('Непригодный ответ первого транспорта')
        return await original(html, url)
    crawler._parse_content = parse
    result = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert result.transport == 'aiohttp'
    crawler._fetch_with_playwright.assert_not_awaited()


@pytest.mark.parametrize('category', ['product', 'category'])
@pytest.mark.parametrize('stage', ['http', 'aiohttp', 'playwright'])
def test_empty_product_tab_enters_interactive_stage(crawler, category, stage):
    if stage == 'http':
        crawler._http_first_get.return_value = PENDING
    elif stage == 'aiohttp':
        crawler._fetch_with_aiohttp.return_value = PENDING
    else:
        crawler._fetch_with_playwright.return_value = (PENDING, False, URL)
    result = asyncio.run(crawler.fetch_page(URL, category))
    assert result.content.html == MERGED and result.transport == 'ajax_tabs'
    crawler.dynamic.extract_product_tabs_merged.assert_awaited_once_with(URL)
    assert crawler.dynamic.product_tabs_only
    if stage == 'http':
        crawler._fetch_with_aiohttp.assert_not_awaited()
        crawler._fetch_with_playwright.assert_not_awaited()


@pytest.mark.parametrize('response', [None, PENDING, MERGED.replace('Давление 16 бар', 'Загрузка...')],
                         ids=['failed', 'empty-panel', 'loading-merged-panel'])
def test_unresolved_tabs_are_retryable_not_success(crawler, response):
    crawler._http_first_get.return_value = PENDING
    crawler.dynamic.extract_product_tabs_merged.return_value = response
    result = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert result.content is None and result.retryable
    assert result.reason == 'incomplete_render'
    crawler._fetch_with_playwright.assert_not_awaited()


@pytest.mark.parametrize('category', ['product', 'category', 'contacts', 'distributor'])
def test_browser_profile_never_falls_back_to_http(crawler, category):
    crawler.profile = SiteProfile(domain='factory.test')
    crawler.profile.crawl.render = 'browser'
    crawler._http_first_get.return_value = FULL
    crawler._fetch_with_aiohttp.return_value = FULL
    crawler._fetch_with_playwright.return_value = (None, False, None)
    result = asyncio.run(crawler.fetch_page(URL, category))
    assert result.content is None
    crawler._http_first_get.assert_not_awaited()
    crawler._fetch_with_aiohttp.assert_not_awaited()


def test_ajax_profile_failure_cannot_be_replaced_by_static_html(crawler):
    crawler.profile = SiteProfile(domain='factory.test')
    crawler.profile.crawl.ajax_tabs = True
    crawler.dynamic.extract_product_tabs_merged.return_value = None
    crawler._http_first_get.return_value = FULL
    result = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert result.content is None and result.retryable
    crawler._http_first_get.assert_not_awaited()
    crawler._fetch_with_playwright.assert_not_awaited()


@pytest.mark.parametrize('status,reason', [(404, 'not_found'), (410, 'gone')])
def test_required_ajax_preserves_confirmed_http_failure(crawler, status, reason):
    crawler.profile = SiteProfile(domain='factory.test')
    crawler.profile.crawl.ajax_tabs = True
    async def missing(url):
        observe_fetch(url, 'ajax_tabs', status=status)
        return None
    crawler.dynamic.extract_product_tabs_merged.side_effect = missing
    outcome = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert outcome.status == status and outcome.reason == reason
    assert not outcome.retryable


@pytest.mark.parametrize('status', [429, 503])
@pytest.mark.parametrize('stage', ['http', 'aiohttp', 'ajax_tabs', 'dynamic'])
def test_host_pause_stops_every_following_stage(crawler, stage, status):
    async def pause(url, **kwargs):
        observe_fetch(url, stage, status=status, headers={'Retry-After': '120'})
        return None
    category = 'product'
    if stage == 'http':
        crawler._http_first_get.side_effect = pause
    elif stage == 'aiohttp':
        crawler._fetch_with_aiohttp.side_effect = pause
    elif stage == 'ajax_tabs':
        crawler.profile = SiteProfile(domain='factory.test')
        crawler.profile.crawl.ajax_tabs = True
        crawler.dynamic.extract_product_tabs_merged.side_effect = pause
    else:
        category = 'contacts'
        crawler.dynamic.extract.side_effect = pause
    result = asyncio.run(crawler.fetch_page(URL, category))
    assert result.status == status and result.retry_after == 120
    crawler._fetch_with_playwright.assert_not_awaited()
    if stage in ('ajax_tabs', 'dynamic'):
        crawler._http_first_get.assert_not_awaited()


def test_external_cancellation_not_swallowed_by_stage_fallback(crawler):
    crawler._http_first_get.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(crawler.fetch_page(URL, 'product'))
    crawler._fetch_with_aiohttp.assert_not_awaited()


def test_accepted_transport_wins_over_auxiliary_fetch_trace(crawler):
    async def enrich(html, url):
        observe_fetch(url, 'aiohttp', status=200, html=FULL)
        return html
    crawler._http_first_get.return_value = FULL
    crawler._enrich_bitrix_offers = enrich
    result = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert result.transport == 'http_first'


def test_selected_transport_saved_in_page_metadata(crawler):
    crawler._http_first_get.return_value = FULL
    crawler._process_links_from_parse_result = AsyncMock()
    crawler._check_memory_usage = AsyncMock()
    async def run():
        await crawler.reset_state('Завод', crawl_run_id='test-ladder')
        crawler.current_base_url = 'https://factory.test/'
        page = await crawler._process_page_with_storage(URL, 0, 'product', 'Завод', {}, deque(), [])
        saved = await crawler.temp_storage.load_cleaned_html(page['file_path'])
        assert saved['metadata']['fetch_method'] == 'http_first'
    asyncio.run(run())


def test_hidden_filled_tab_does_not_require_interaction(crawler):
    crawler._http_first_get.return_value = PENDING.replace(
        'role="tabpanel"></div>', 'role="tabpanel" style="display:none">Масса 40 кг</div>')
    result = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert result.transport == 'http_first'
    crawler.dynamic.extract_product_tabs_merged.assert_not_awaited()
    crawler._fetch_with_playwright.assert_not_awaited()


def test_jsonld_does_not_hide_unloaded_specifications(crawler):
    island = '<script type="application/ld+json">{"@type":"Product","name":"Насос","offers":{"price":100}}</script>'
    crawler._http_first_get.return_value = PENDING.replace('</body>', island + '</body>')
    result = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert result.transport == 'ajax_tabs'


@pytest.mark.parametrize('extra', ['', '<a href="/catalog/pump-200/">Другой насос</a>'])
def test_price_and_related_product_link_do_not_make_http_shell_complete(crawler, extra):
    shell = ('<html><body><div class="product"><h1>Насос 100</h1>'
             '<p>Цена 100 рублей</p><button>Купить</button>' + extra
             + '<p>Промышленное оборудование завода.</p>' * 8 + '</div></body></html>')
    crawler._http_first_get.return_value = shell
    crawler._fetch_with_aiohttp.return_value = shell
    result = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert result.content.html == FULL and result.transport == 'playwright'


def test_static_catalog_links_are_sufficient_without_browser(crawler):
    listing = '<html><body><h1>Каталог насосов</h1>' + ''.join(
        f'<a href="/catalog/pump-{i}/">Насос промышленный {i}</a>' for i in range(5)) + '</body></html>'
    crawler._http_first_get.return_value = listing
    result = asyncio.run(crawler.fetch_page('https://factory.test/catalog/', 'product'))
    assert result.content.html == listing and result.transport == 'http_first'
    crawler._fetch_with_playwright.assert_not_awaited()


def test_automatic_interaction_uses_only_product_tabs(crawler):
    from test_final_url_pipeline import browser
    page = browser(crawler)
    buy = SimpleNamespace(is_visible=AsyncMock(return_value=True), click=AsyncMock(),
                          inner_text=AsyncMock(return_value='Купить'))
    specs = SimpleNamespace(is_visible=AsyncMock(return_value=True), click=AsyncMock(),
                            inner_text=AsyncMock(return_value='Характеристики'),
                            get_attribute=AsyncMock(return_value='specs'))
    page.query_selector_all = AsyncMock(return_value=[buy, specs])
    page.wait_for_timeout = AsyncMock()
    page.evaluate = AsyncMock(side_effect=['', '<p>Давление 16 бар</p>'])
    extractor = wc.DynamicContentExtractor(crawler.browser_pool, crawler.config)
    extractor.product_tabs_only = True
    result = asyncio.run(extractor.extract_product_tabs_merged(URL))
    assert '<section data-tab="Характеристики">' in result
    specs.click.assert_awaited_once()
    buy.click.assert_not_awaited()
    page.query_selector_all.assert_awaited_once_with(data_island.PRODUCT_TAB_SELECTOR)
    assert page.evaluate.await_args.args[1]['target'] == 'specs'


def test_resume_preserves_original_selected_transport(crawler):
    crawler._http_first_get.return_value = FULL
    crawler._process_links_from_parse_result = AsyncMock()
    crawler._check_memory_usage = AsyncMock()
    async def run():
        await crawler.reset_state('Завод', crawl_run_id='resume-ladder')
        crawler.current_base_url = 'https://factory.test/'
        page = await crawler._process_page_with_storage(URL, 0, 'product', 'Завод', {}, deque(), [])
        await crawler.reset_state('Завод', crawl_run_id='resume-ladder', resume=True)
        crawler.current_base_url = 'https://factory.test/'
        crawler.fetch_page = AsyncMock(side_effect=AssertionError('Resume не загружает страницу'))
        await crawler._process_page_with_storage(URL, 0, 'product', 'Завод', {}, deque(), [])
        saved = await crawler.temp_storage.load_cleaned_html(page['file_path'])
        assert saved['metadata']['fetch_method'] == 'http_first'
        crawler.fetch_page.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize('html,expected', [
    (PENDING, True),
    (PENDING.replace('<div id="specs" role="tabpanel"></div>', ''), True),
    (PENDING.replace('aria-controls="specs"', 'data-target="#specs"'), True),
    (PENDING.replace('aria-controls="specs"', 'href="#specs"'), True),
    (PENDING.replace('role="tabpanel"></div>', 'role="tabpanel">Загрузка...</div>'), True),
    (PENDING.replace('role="tabpanel"></div>', 'role="tabpanel" style="display:none">Масса 40 кг</div>'), False),
    (PENDING.replace('role="tabpanel"></div>', 'role="tabpanel"><a href="/manual.pdf">Паспорт</a></div>'), False),
    (PENDING.replace('Характеристики', 'Отзывы'), False),
    (FULL + '<a href="#specs">Характеристики</a>', False),
    (MERGED, False),
    (FULL, False),
], ids=['empty', 'missing', 'data-target', 'href', 'loading', 'hidden-filled',
        'document-link', 'reviews', 'ordinary-anchor', 'merged', 'static'])
def test_unloaded_tab_detection_has_positive_and_negative_controls(html, expected):
    assert data_island.has_unloaded_product_tabs(html) is expected
