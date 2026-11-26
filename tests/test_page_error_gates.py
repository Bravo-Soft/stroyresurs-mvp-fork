"""Ошибки транспорта и HTML ошибок не становятся товарными страницами."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import web_crawler as wc
from config import Config


URL = 'https://factory.test/catalog/pump/'
VALID = ('<html><head><title>Насос 404</title></head><body><main>'
         '<h1>Насос 404</h1><p>Артикул 404, код модели 500. '
         'Руководство: при ошибке 404 проверьте соединение.</p>'
         '<table><tr><td>Мощность</td><td>20 кВт</td></tr></table>'
         + '<p>Производительность 30 м³/ч. Корпус из стали.</p>' * 24
         + '</main></body></html>')
SHELL = ('<html><body><div id="app"></div>' + '<!-- padding -->' * 1500
         + '<script src="/app.js"></script></body></html>')
ERRORS = [
    '<html><head><title>Ошибка 404</title></head><body>Запрошенный адрес отсутствует.</body></html>',
    'Service Unavailable',
    '<html><head><title>404 Not Found</title></head><body><h1>Страница не найдена</h1></body></html>',
    '<html><head><title>Ошибка 404</title></head><body><h1>Страница не найдена</h1></body></html>',
    '<html><head><title>Access denied</title></head><body><h1>Access denied</h1><p>You do not have permission to access this resource.</p></body></html>',
    '<html><head><title>Just a moment...</title></head><body><div id="cf-challenge-running">Checking your browser before accessing the website</div></body></html>',
    '<html><head><title>502 Bad Gateway</title></head><body><h1>502 Bad Gateway</h1><hr>nginx</body></html>',
    '<html><head><title>Service Unavailable</title></head><body><h1>503 Service Unavailable</h1><p>Please try again later.</p></body></html>',
]


@pytest.fixture
def crawler(tmp_path):
    cfg = Config()
    cfg.base_dir = str(tmp_path)
    c = wc.WebCrawler(cfg)
    c.current_base_url = 'https://factory.test/'
    c._extract_additional_links = AsyncMock(return_value=[])
    c._enrich_bitrix_offers = AsyncMock(side_effect=lambda html, url: html)
    return c


class FakePage:
    """Ответы навигации и DOM без настоящего браузера."""
    def __init__(self, status=200, html=VALID, tabs=False, navigation_status=None):
        self.url = URL
        self.status = status
        self.html = html if not tabs else html.replace('</body>', '<button role="tab" aria-controls="panel">Описание</button><div id="panel"></div></body>')
        self.tabs = tabs
        self.navigation_status = navigation_status
        self.listeners = {}
        self.goto = AsyncMock(side_effect=self._goto)
        self.wait_for_load_state = AsyncMock()
        self.wait_for_function = AsyncMock()
        self.wait_for_timeout = AsyncMock()
        self.evaluate = AsyncMock(return_value='')
        self.content = AsyncMock(side_effect=lambda: self.html)
        self.query_selector_all = AsyncMock(side_effect=self._query)

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        self.listeners.get(event, []).remove(callback)

    def _response(self, status):
        request = SimpleNamespace(is_navigation_request=lambda: True,
                                  resource_type='document', frame=self)
        return SimpleNamespace(status=status, url=self.url, request=request, frame=self,
                               all_headers=AsyncMock(return_value={}))

    async def _goto(self, *args, **kwargs):
        return self._response(self.status) if self.status is not None else None

    def is_closed(self):
        return False

    async def _query(self, selector):
        return [self.tab] if self.tabs else []

    @property
    def main_frame(self):
        return self

    @property
    def tab(self):
        async def click(**kwargs):
            if self.navigation_status is not None:
                self.url = URL + 'missing/'
                # Нейтральный DOM: отказ должен определяться статусом, а не текстом ошибки.
                self.html = VALID
                for callback in self.listeners.get('response', []):
                    callback(self._response(self.navigation_status))
        return SimpleNamespace(is_visible=AsyncMock(return_value=True),
                               inner_text=AsyncMock(return_value='Описание'),
                               get_attribute=AsyncMock(side_effect=lambda key: 'panel' if key == 'aria-controls' else None),
                               click=AsyncMock(side_effect=click))


def extractor(crawler, page):
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    pool = SimpleNamespace(get_browser=AsyncMock(return_value=context),
                           close_page=AsyncMock(), return_browser=AsyncMock())
    return wc.DynamicContentExtractor(pool, crawler.config,
                                     allowed_url=lambda *a, **kw: True)


@pytest.mark.parametrize('status', [403, 404, 410, 429, 500, 503])
@pytest.mark.parametrize('method', ['extract', 'extract_product_tabs_merged'])
def test_dynamic_rejects_non_200_before_clicks(crawler, status, method):
    page = FakePage(status=status)
    result = asyncio.run(getattr(extractor(crawler, page), method)(URL))
    assert not result
    page.query_selector_all.assert_not_awaited()


@pytest.mark.parametrize('method', ['extract', 'extract_product_tabs_merged'])
@pytest.mark.parametrize('html', ERRORS)
def test_dynamic_rejects_http200_error_before_clicks(crawler, method, html):
    page = FakePage(html=html)
    assert not asyncio.run(getattr(extractor(crawler, page), method)(URL))
    page.query_selector_all.assert_not_awaited()


@pytest.mark.parametrize('method', ['extract', 'extract_product_tabs_merged'])
def test_dynamic_keeps_real_product_with_404_in_model(crawler, method):
    page = FakePage()
    result = asyncio.run(getattr(extractor(crawler, page), method)(URL))
    assert result and str(result) == VALID
    assert result.status == 200


@pytest.mark.parametrize('status', [404, 410, 503])
def test_tab_navigation_cannot_turn_error_into_status200(crawler, status):
    page = FakePage(tabs=True, navigation_status=status)
    result = asyncio.run(extractor(crawler, page).extract(URL))
    accepted = result if isinstance(result, list) else [result]
    assert not any(item and getattr(item, 'status', 200) == 200 for item in accepted)


def test_contact_panel_navigation_is_rejected_even_with_status200(crawler):
    page = FakePage(tabs=True, navigation_status=200)
    result = asyncio.run(extractor(crawler, page).extract(URL))
    assert not result


@pytest.mark.parametrize('html', ERRORS)
def test_error_detector_recognizes_error_document(html):
    assert wc.page_error_reason(html)


@pytest.mark.parametrize('extra', [
    '<p>Модель 404, артикул 503, код 500.</p>',
    '<p>В документации описана ошибка 404 Not Found.</p>',
    '<script>var message="Access denied; 404 Not Found";</script>',
    '<nav><a href="/404/">404</a></nav>',
])
def test_error_detector_preserves_legitimate_product(extra):
    assert wc.page_error_reason(VALID.replace('</main>', extra + '</main>')) is None


def test_error_detector_preserves_article_discussing_server_errors():
    article = ('<html><body><h1>Диагностика соединения</h1><article>'
               '<p>Service Unavailable и ошибка 404 описываются в инструкции. '
               'Для устранения ошибки проверьте адрес сервера.</p></article></body></html>')
    assert wc.page_error_reason(article) is None


@pytest.mark.parametrize('status', [403, 404, 410, 429, 500, 503])
def test_parse_rejects_non200_even_with_product_shaped_body(crawler, status):
    result = asyncio.run(crawler._parse_content(wc.PageContent(VALID, URL, URL, status), URL))
    assert result is None
    crawler._extract_additional_links.assert_not_awaited()


@pytest.mark.parametrize('html', ERRORS)
def test_parse_rejects_http200_error_before_link_extraction(crawler, html):
    assert asyncio.run(crawler._parse_content(wc.PageContent(html, URL, URL), URL)) is None
    crawler._extract_additional_links.assert_not_awaited()


def test_parse_accepts_product_with_error_codes(crawler):
    result = asyncio.run(crawler._parse_content(wc.PageContent(VALID, URL, URL), URL))
    assert result is not None and result.html == VALID


@pytest.mark.parametrize('category', ['product', 'category', 'contacts', 'distributor'])
def test_error200_never_accepted_from_any_fetch_route(crawler, monkeypatch, category):
    monkeypatch.setattr(wc, '_CURL_CFFI_AVAILABLE', True)
    bad = wc.PageContent(ERRORS[0], URL, URL)
    crawler._http_first_get = AsyncMock(return_value=bad)
    crawler._fetch_with_http_impersonate = AsyncMock(return_value=wc.HttpFetchResult(str(bad), 200, {}, URL, {}))
    crawler._fetch_with_aiohttp = AsyncMock(return_value=bad)
    crawler._fetch_with_playwright = AsyncMock(return_value=(bad, False, URL))
    crawler._make_dynamic_extractor = lambda: SimpleNamespace(
        extract=AsyncMock(return_value=bad), extract_product_tabs_merged=AsyncMock(return_value=bad))
    assert asyncio.run(crawler._fetch_page_content(URL, category)) is None


def test_warmup_http200_challenge_is_not_success(crawler, monkeypatch):
    monkeypatch.setattr(wc, '_CURL_CFFI_AVAILABLE', True)
    crawler.config.product_min_text = 0
    challenge = next(html for html in ERRORS if 'cf-challenge-running' in html)
    challenge = challenge.replace('</body>', '<p>Please wait while we verify your browser and connection.</p>' * 30 + '</body>')
    crawler._fetch_with_http_impersonate = AsyncMock(return_value=wc.HttpFetchResult(
        '<html><body><h1>Access denied</h1></body></html>', 403, {}, URL, {}))
    crawler._cookie_warmup_and_retry = AsyncMock(return_value=wc.HttpFetchResult(
        challenge, 200, {}, URL, {}))
    assert asyncio.run(crawler._http_first_get(URL, expect_product=True)) is None
    crawler._cookie_warmup_and_retry.assert_awaited_once()


@pytest.mark.parametrize('browser_failed', [False, True])
def test_rejected_long_shell_does_not_override_browser_result(crawler, browser_failed):
    assert len(SHELL) > len(VALID)
    crawler._http_first_get = AsyncMock(return_value=None)
    crawler._fetch_with_aiohttp = AsyncMock(return_value=wc.PageContent(SHELL, URL, URL))
    browser = None if browser_failed else wc.PageContent(VALID, URL, URL)
    crawler._fetch_with_playwright = AsyncMock(return_value=(browser, False, URL))
    result = asyncio.run(crawler._fetch_page_content(URL, 'product'))
    crawler._fetch_with_playwright.assert_awaited_once()
    if browser_failed:
        assert result is None
    else:
        assert result is not None and result.html == VALID


def browser_crawler(crawler, page):
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    crawler.browser_pool = SimpleNamespace(
        get_browser=AsyncMock(return_value=context), close_page=AsyncMock(), return_browser=AsyncMock())
    crawler._guard_browser_page = AsyncMock(return_value=None)
    crawler._accept_cookie_consent = AsyncMock()
    crawler._scroll_page = AsyncMock()
    crawler._wait_for_product_content = AsyncMock()
    crawler._wait_for_dynamic_links = AsyncMock()
    crawler._extract_dynamic_product_content = AsyncMock(side_effect=lambda p: p.html)
    return crawler


@pytest.mark.parametrize('status', [403, 404, 410, 429, 500, 503, None])
def test_regular_browser_rejects_initial_status_before_interactions(crawler, status):
    page = FakePage(status=status)
    c = browser_crawler(crawler, page)
    content, permanent, _ = asyncio.run(c._fetch_with_playwright(URL))
    assert content is None
    assert permanent == (status in (404, 410))
    c._accept_cookie_consent.assert_not_awaited()
    c._scroll_page.assert_not_awaited()


@pytest.mark.parametrize('status', [200, 403, 410])
def test_regular_browser_tracks_final_navigation_status(crawler, status):
    page = FakePage()
    c = browser_crawler(crawler, page)
    async def navigate(_):
        page.url = URL + 'redirected/'
        for callback in page.listeners.get('response', []):
            callback(page._response(status))
    c._wait_for_dynamic_links = AsyncMock(side_effect=navigate)
    content, permanent, final_url = asyncio.run(c._fetch_with_playwright(URL))
    if status == 200:
        assert content == VALID and content.status == 200
        assert final_url == page.url
    else:
        assert content is None
        assert permanent == (status == 410)


@pytest.mark.parametrize('method', ['extract', 'extract_product_tabs_merged'])
def test_dynamic_without_document_response_rejected(crawler, method):
    page = FakePage(status=None)
    assert not asyncio.run(getattr(extractor(crawler, page), method)(URL))
    page.query_selector_all.assert_not_awaited()
