"""Конечный адрес документа сохраняется без сети и общего mutable state."""
import asyncio
from unittest.mock import AsyncMock

from config import Config
from web_crawler import WebCrawler, HttpFetchResult

HTML = '<html><body><a href="pump.html">Насос</a>' + '<p>Технические характеристики насоса.</p>' * 100 + '</body></html>'


def test_http_first_retains_final_url(tmp_path):
    config = Config()
    config.base_dir = str(tmp_path)
    crawler = WebCrawler(config)
    crawler.current_base_url = 'https://example.ru/'
    crawler._fetch_with_http_impersonate = AsyncMock(return_value=HttpFetchResult(
        HTML, 200, {}, 'https://example.ru/catalog/', {}))
    result = asyncio.run(crawler._http_first_get('https://example.ru/catalog'))
    assert getattr(result, 'final_url', None) == 'https://example.ru/catalog/'

from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import web_crawler
from web_crawler import PageContent, DynamicContentExtractor

REQUEST = 'http://example.ru/catalog'
FINAL = 'https://www.example.ru/new/catalog/'


@pytest.fixture
def crawler(tmp_path):
    config = Config()
    config.base_dir = str(tmp_path)
    c = WebCrawler(config)
    c.current_base_url = 'https://example.ru/'
    return c


@pytest.mark.parametrize('category', ['product', 'category', 'contacts', 'distributor'])
@pytest.mark.parametrize('transport', ['http', 'aiohttp', 'browser'])
def test_fetch_ladder_retains_document_address(crawler, category, transport, monkeypatch):
    crawler.config.dynamic_content_enabled = False
    crawler.config.http_first_enabled = transport == 'http'
    crawler.config.product_min_text = 0
    monkeypatch.setattr(web_crawler, '_CURL_CFFI_AVAILABLE', True)
    payload = PageContent(HTML, REQUEST, FINAL, method=transport)
    crawler._http_first_get = AsyncMock(return_value=payload if transport == 'http' else None)
    crawler._fetch_with_http_impersonate = AsyncMock(return_value=(
        HttpFetchResult(HTML, 200, {}, FINAL, {}) if transport == 'http' else None))
    crawler._fetch_with_aiohttp = AsyncMock(return_value=payload if transport == 'aiohttp' else None)
    # Legacy tuple/string browser stubs remain supported too.
    crawler._fetch_with_playwright = AsyncMock(return_value=(HTML, False, FINAL))
    result = asyncio.run(crawler._fetch_page_content(REQUEST, category))
    assert result.url == FINAL
    assert result.requested_url == REQUEST
    assert any(u == FINAL + 'pump.html' for u, _, _ in result.additional_links)


def test_base_href_and_canonical_are_distinct(crawler):
    html = '<base href="../assets/"><link rel="canonical" href="/unrelated/">' + HTML
    html += '<a href="manual.pdf">Скачать паспорт</a>'
    result = asyncio.run(crawler._parse_content(PageContent(html, REQUEST, FINAL), REQUEST))
    assert result.url == FINAL and result.requested_url == REQUEST
    assert result.canonical_url.endswith('/unrelated')
    assert any(u == 'https://www.example.ru/new/assets/pump.html' for u, _, _ in result.additional_links)
    assert crawler._extract_file_links_from_soup(result.soup, result.url) == [
        'https://www.example.ru/new/assets/manual.pdf']
    assert crawler.file_download_manager.extract_document_links(html, result.url) == [
        'https://www.example.ru/new/assets/manual.pdf']


class AsyncContext:
    def __init__(self, value):
        self.value = value
    async def __aenter__(self):
        return self.value
    async def __aexit__(self, *args):
        return False


def test_actual_aiohttp_result(crawler, monkeypatch):
    response = SimpleNamespace(status=200, url=FINAL, headers={'ETag': 'v2'}, text=AsyncMock(return_value=HTML))
    session = SimpleNamespace(get=Mock(return_value=AsyncContext(response)))
    monkeypatch.setattr(web_crawler.aiohttp, 'TCPConnector', Mock())
    monkeypatch.setattr(web_crawler.aiohttp, 'ClientSession', Mock(return_value=AsyncContext(session)))
    result = asyncio.run(crawler._fetch_with_aiohttp(REQUEST))
    assert isinstance(result, PageContent)
    assert (result.requested_url, result.final_url, result.status, result.headers) == (
        REQUEST, FINAL, 200, {'ETag': 'v2'})


def browser(crawler):
    # Здесь проверяется перенос URL; периметр браузера покрыт отдельно.
    crawler._guard_browser_page = AsyncMock(return_value=AsyncMock())
    page = SimpleNamespace(url=FINAL, goto=AsyncMock(return_value=SimpleNamespace(status=200)),
                           wait_for_load_state=AsyncMock(), wait_for_function=AsyncMock(), is_closed=Mock(return_value=False),
                           content=AsyncMock(return_value=HTML), query_selector_all=AsyncMock(return_value=[]),
                           on=Mock())
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    crawler.browser_pool = SimpleNamespace(get_browser=AsyncMock(return_value=context),
                                         close_page=AsyncMock(), return_browser=AsyncMock())
    return page


def test_actual_browser_reads_address_after_dynamic_render(crawler):
    page = browser(crawler)
    for name in ('_accept_cookie_consent', '_scroll_page', '_wait_for_product_content', '_wait_for_dynamic_links'):
        setattr(crawler, name, AsyncMock())
    async def rendered(p):
        p.url = FINAL + 'rendered/'
        return HTML
    crawler._extract_dynamic_product_content = rendered
    content, permanent_error, final_url = asyncio.run(crawler._fetch_with_playwright(REQUEST))
    assert not permanent_error
    assert final_url == FINAL + 'rendered/'
    assert content.final_url == final_url and content.requested_url == REQUEST


@pytest.mark.parametrize('tabs', [False, True])
def test_dynamic_contacts_transport(crawler, tabs):
    page = browser(crawler)
    extractor = DynamicContentExtractor(crawler.browser_pool, crawler.config)
    extractor._has_tabs = AsyncMock(return_value=tabs)
    extractor._process_accordions = AsyncMock(return_value=HTML)
    if tabs:
        tab = SimpleNamespace(is_visible=AsyncMock(return_value=True), click=AsyncMock())
        page.query_selector_all = AsyncMock(return_value=[tab])
        page.evaluate = AsyncMock()
        page.wait_for_timeout = AsyncMock()
    result = asyncio.run(extractor.extract(REQUEST))
    content = result[0] if tabs else result
    assert content.requested_url == REQUEST and content.final_url == FINAL
    crawler._make_dynamic_extractor = Mock(return_value=SimpleNamespace(extract=AsyncMock(return_value=result)))
    parsed = asyncio.run(crawler._fetch_page_content(REQUEST, 'contacts'))
    parsed = parsed[0] if tabs else parsed
    assert parsed.url == FINAL and parsed.requested_url == REQUEST


def test_ajax_product_transport_without_tabs(crawler):
    browser(crawler)
    extractor = DynamicContentExtractor(crawler.browser_pool, crawler.config)
    result = asyncio.run(extractor.extract_product_tabs_merged(REQUEST))
    assert result.final_url == FINAL and result.requested_url == REQUEST
    crawler.config.ajax_product_tabs_domains = ['example.ru']
    crawler._make_dynamic_extractor = Mock(return_value=SimpleNamespace(
        extract_product_tabs_merged=AsyncMock(return_value=result)))
    parsed = asyncio.run(crawler._fetch_page_content(REQUEST, 'product'))
    assert parsed.url == FINAL and parsed.requested_url == REQUEST


def test_foreign_aiohttp_redirect_not_parsed(crawler):
    crawler.config.http_first_enabled = False
    crawler._fetch_with_aiohttp = AsyncMock(return_value=PageContent(HTML, REQUEST, 'https://foreign.ru/'))
    crawler._fetch_with_playwright = AsyncMock(return_value=(None, False, None))
    assert asyncio.run(crawler._fetch_page_content(REQUEST, 'category')) is None


@pytest.mark.parametrize('single_tab', [False, True])
def test_saved_metadata_and_streamed_page_use_final_url(crawler, single_tab):
    parsed = asyncio.run(crawler._parse_content(PageContent(HTML, REQUEST, FINAL), REQUEST))
    crawler._fetch_page_content = AsyncMock(return_value=[parsed] if single_tab else parsed)
    crawler.temp_storage.is_page_processed = AsyncMock(return_value=False)
    crawler.temp_storage.save_cleaned_html = AsyncMock(return_value=('page.html', True, {}))
    crawler._process_links_from_parse_result = AsyncMock()
    crawler._extract_links = AsyncMock(return_value=[])
    crawler._update_stats = AsyncMock()
    stored = []
    asyncio.run(crawler._process_page_with_storage(REQUEST, 1, 'category', 'Test', {}, deque(), stored))
    call = crawler.temp_storage.save_cleaned_html.call_args
    assert call.args[0] == FINAL
    assert call.args[4]['url'] == FINAL
    assert call.args[4]['requested_url'] == REQUEST
    assert stored[0]['url'] == FINAL


def test_parallel_parse_has_no_shared_final_url(crawler):
    async def run():
        return await asyncio.gather(*(
            crawler._parse_content(PageContent(HTML, REQUEST + str(i), FINAL + str(i) + '/'), REQUEST)
            for i in range(10)))
    results = asyncio.run(run())
    for i, result in enumerate(results):
        assert result.url == FINAL + str(i) + '/'
        assert result.requested_url == REQUEST + str(i)


def test_actual_impersonate_through_http_first(crawler, monkeypatch):
    response = SimpleNamespace(text=HTML, status_code=200, url=FINAL, headers={'Content-Type': 'text/html'})
    session = SimpleNamespace(get=AsyncMock(return_value=response), close=AsyncMock(),
                              cookies=SimpleNamespace(jar=[]))
    monkeypatch.setattr(web_crawler, '_CURL_CFFI_AVAILABLE', True)
    monkeypatch.setattr(web_crawler, '_CurlAsyncSession', Mock(return_value=session))
    result = asyncio.run(crawler._http_first_get(REQUEST))
    assert result.final_url == FINAL and result.requested_url == REQUEST
    assert result.headers == {'content-type': 'text/html'}
    session.close.assert_awaited_once()


def test_warmup_result_preserves_new_redirect(crawler):
    crawler._fetch_with_http_impersonate = AsyncMock(return_value=HttpFetchResult('challenge', 403, {}, REQUEST, {}))
    crawler._is_challenge = Mock(return_value=True)
    crawler._cookie_warmup_and_retry = AsyncMock(return_value=HttpFetchResult(HTML, 200, {}, FINAL, {}))
    result = asyncio.run(crawler._http_first_get(REQUEST))
    assert result.final_url == FINAL and result.requested_url == REQUEST


def test_foreign_product_island_rejected_before_acceptance(crawler):
    island = '<script type="application/ld+json">{"@type":"Product","name":"Pump"}</script>'
    crawler._fetch_with_http_impersonate = AsyncMock(return_value=HttpFetchResult(
        island, 200, {}, 'https://foreign.ru/catalog/', {}))
    assert asyncio.run(crawler._http_first_get(REQUEST)) is None


@pytest.mark.parametrize('transport', ['http', 'aiohttp'])
def test_dynamic_http_fallback_retains_address(crawler, transport):
    crawler._make_dynamic_extractor = Mock(return_value=SimpleNamespace(extract=AsyncMock(return_value=[])))
    payload = PageContent(HTML, REQUEST, FINAL)
    crawler._http_first_get = AsyncMock(return_value=payload if transport == 'http' else None)
    crawler._fetch_with_aiohttp = AsyncMock(return_value=payload)
    result = asyncio.run(crawler._fetch_page_content(REQUEST, 'contacts'))
    assert result.url == FINAL and result.requested_url == REQUEST


def test_fuller_aiohttp_keeps_its_own_final_url(crawler):
    crawler._http_first_get = AsyncMock(return_value=None)
    # Force the browser branch without losing the already parsed HTTP document.
    crawler._product_shell_min_text = Mock(return_value=1000000)
    crawler._fetch_with_aiohttp = AsyncMock(return_value=PageContent(HTML, REQUEST, FINAL))
    crawler._fetch_with_playwright = AsyncMock(return_value=('<html/>', False, FINAL + 'different/'))
    result = asyncio.run(crawler._fetch_page_content(REQUEST, 'product'))
    assert result.url == FINAL and result.requested_url == REQUEST


def test_ajax_product_merged_panels_keep_document_address(crawler):
    page = browser(crawler)
    tab = SimpleNamespace(is_visible=AsyncMock(return_value=True), click=AsyncMock(),
                          inner_text=AsyncMock(return_value='Характеристики'))
    page.query_selector_all = AsyncMock(return_value=[tab])
    page.wait_for_timeout = AsyncMock()
    page.evaluate = AsyncMock(side_effect=['Исходная панель', '<p>Новая таблица</p>'])
    extractor = DynamicContentExtractor(crawler.browser_pool, crawler.config)
    result = asyncio.run(extractor.extract_product_tabs_merged(REQUEST))
    assert result.final_url == FINAL and result.requested_url == REQUEST
    assert '<section data-tab="Характеристики">' in result


@pytest.mark.parametrize('scope', ['allowed_subdomain', 'same_host_equivalency_disabled', 'www_equivalency_disabled'])
@pytest.mark.parametrize('transport', ['http_product', 'http_category', 'aiohttp', 'browser', 'dynamic'])
def test_final_url_gate_uses_company_scope(crawler, monkeypatch, scope, transport):
    requested = 'https://example.ru/catalog'
    final = requested + '/'
    if scope == 'allowed_subdomain':
        requested = 'https://products.example.ru/catalog'
        final = requested + '/'
        from site_profiles.models import SiteProfile
        crawler.profile = SiteProfile(domain="example.ru")
        crawler.profile.crawl.equivalent_domains = ["products.example.ru"]
    else:
        crawler.config.domain_equivalency_enabled = False
        if scope == 'www_equivalency_disabled':
            final = 'https://www.example.ru/catalog/'
    monkeypatch.setattr(web_crawler, '_CURL_CFFI_AVAILABLE', True)
    crawler.config.http_first_enabled = transport.startswith('http')
    crawler.config.dynamic_content_enabled = transport == 'dynamic'
    crawler.config.product_min_text = 0
    crawler._fetch_with_http_impersonate = AsyncMock(return_value=HttpFetchResult(HTML, 200, {}, final, {}))
    crawler._fetch_with_aiohttp = AsyncMock(return_value=(
        PageContent(HTML, requested, final) if transport == 'aiohttp' else None))
    crawler._fetch_with_playwright = AsyncMock(return_value=(HTML, False, final))
    crawler._make_dynamic_extractor = Mock(return_value=SimpleNamespace(
        extract=AsyncMock(return_value=PageContent(HTML, requested, final))))
    category = 'product' if transport == 'http_product' else 'contacts' if transport == 'dynamic' else 'category'
    result = asyncio.run(crawler._fetch_page_content(requested, category))
    assert result is not None
    assert result.url == final and result.requested_url == requested


@pytest.mark.parametrize('final', ['https://foreign.ru/', 'file:///etc/passwd', 'ftp://example.ru/catalog/'])
def test_final_url_gate_rejects_outside_scope(crawler, final):
    assert not crawler._is_allowed_final_url(REQUEST, final)


def test_links_filter_receives_final_document_url(crawler):
    parsed = asyncio.run(crawler._parse_content(PageContent(HTML, REQUEST, FINAL), REQUEST))
    crawler._extract_links = AsyncMock(return_value=[])
    asyncio.run(crawler._process_links_from_parse_result(REQUEST, parsed, 1, deque()))
    assert crawler._extract_links.call_args.args[0] == FINAL
