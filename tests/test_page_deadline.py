"""Один дедлайн страницы охватывает всю лестницу и сохраняет отмену/cleanup."""
import asyncio
import sys
from pathlib import Path
from types import ModuleType
import async_timeout
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from bs4 import BeautifulSoup

import web_crawler as wc
from config import Config

URL = 'https://factory.test/catalog/pump/'
HTML = '<html><body><h1>Насос</h1>' + '<p>Мощность 20 кВт.</p>' * 100 + '</body></html>'


@pytest.fixture(params=['installed', 'py310_branch'], autouse=True)
def timeout_implementation(request, monkeypatch):
    """Проверяем также настоящую ветку backport без asyncio.timeout/expired()."""
    if request.param == 'py310_branch':
        module = ModuleType('_async_timeout_py310')
        source = Path(async_timeout.__file__).read_text()
        with monkeypatch.context() as patch:
            patch.setattr(sys, 'version_info', (3, 10, 12))
            exec(compile(source, async_timeout.__file__, 'exec'), module.__dict__)
        monkeypatch.setattr(wc, 'async_deadline', module.timeout)


@pytest.fixture
def crawler(tmp_path):
    cfg = Config()
    cfg.base_dir = str(tmp_path)
    cfg.page_fetch_timeout = 0.04
    crawler = wc.WebCrawler(cfg)
    crawler.current_base_url = 'https://factory.test/'
    return crawler


def test_config_has_finite_default_page_budget():
    assert Config().page_fetch_timeout == 60


def test_one_deadline_spans_several_transport_attempts(crawler):
    finished = []
    async def curl(*args, **kwargs):
        await asyncio.sleep(0.025)
        finished.append('curl')
        return None
    async def aiohttp(*args, **kwargs):
        try:
            await asyncio.sleep(1)
        finally:
            finished.append('aiohttp_cleanup')
    crawler._http_first_get = curl
    crawler._fetch_with_aiohttp = aiohttp
    crawler._fetch_with_playwright = AsyncMock(return_value=(None, False, None))
    async def run():
        start = asyncio.get_running_loop().time()
        result = await crawler.fetch_page(URL, 'product')
        assert asyncio.get_running_loop().time() - start < 0.3
        assert result.reason == 'timeout' and result.transport == 'deadline' and result.retryable
    asyncio.run(run())
    assert finished == ['curl', 'aiohttp_cleanup']
    crawler._fetch_with_playwright.assert_not_awaited()


def test_deadline_cancels_dynamic_wait_and_returns_browser_resources(crawler):
    async def network_idle(*args, **kwargs):
        await asyncio.sleep(1)
    response = SimpleNamespace(status=200, headers={})
    page = SimpleNamespace(url=URL, on=lambda *args: None,
                           goto=AsyncMock(return_value=response),
                           content=AsyncMock(return_value=HTML),
                           wait_for_load_state=network_idle)
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    pool = SimpleNamespace(get_browser=AsyncMock(return_value=context),
                           close_page=AsyncMock(), return_browser=AsyncMock())
    crawler.browser_pool = pool
    crawler._guard_browser_page = AsyncMock(return_value=None)
    crawler._http_first_get = AsyncMock(return_value=None)
    crawler._fetch_with_aiohttp = AsyncMock(return_value=None)
    result = asyncio.run(crawler.fetch_page(URL, 'contacts'))
    assert result.reason == 'timeout' and result.transport == 'deadline'
    pool.close_page.assert_awaited_once_with(page)
    pool.return_browser.assert_awaited_once_with(context)


def test_external_cancellation_propagates_after_cleanup(crawler):
    async def run():
        entered = asyncio.Event()
        cleaned = asyncio.Event()
        async def fetch(*args):
            entered.set()
            try:
                await asyncio.sleep(10)
            finally:
                cleaned.set()
        crawler._fetch_page_content = fetch
        task = asyncio.create_task(crawler.fetch_page(URL, 'product'))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()
    asyncio.run(run())


def test_fast_success_is_not_lost_to_deadline(crawler):
    parsed = wc.ParseResult(BeautifulSoup(HTML, 'html.parser'), HTML, [], 'product_page', {}, url=URL)
    crawler._fetch_page_content = AsyncMock(return_value=parsed)
    result = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert result.reason == 'ok' and result.content is parsed


def test_product_selectors_share_single_wait_budget(crawler):
    page = SimpleNamespace(is_closed=lambda: False,
                           wait_for_selector=AsyncMock(side_effect=wc.PlaywrightTimeoutError('absent')),
                           wait_for_timeout=AsyncMock(), query_selector_all=AsyncMock(return_value=[]))
    asyncio.run(crawler._wait_for_product_content(page))
    page.wait_for_selector.assert_awaited_once()
    args, kwargs = page.wait_for_selector.call_args
    assert kwargs['timeout'] == 10000
    assert '.product' in args[0] and '[class*="price"]' in args[0] and ',' in args[0]


def test_file_link_fallback_uses_typed_bounded_fetch(crawler):
    crawler.fetch_page = AsyncMock(return_value=wc.FetchOutcome(URL, reason='timeout', transport='deadline'))
    crawler._fetch_page_content = AsyncMock(side_effect=AssertionError('unbounded fetch'))
    assert asyncio.run(crawler.extract_file_links_from_page(URL)) == []
    crawler.fetch_page.assert_awaited_once_with(URL, 'product')
    crawler._fetch_page_content.assert_not_awaited()


def test_typed_file_link_success_uses_final_document_url(crawler):
    final_url = 'https://factory.test/region/product/'
    html = '<html><body><a href="files/manual.pdf">Руководство</a></body></html>'
    parsed = wc.ParseResult(BeautifulSoup(html, 'html.parser'), html, [], 'product_page', {}, url=final_url)
    crawler.fetch_page = AsyncMock(return_value=wc.FetchOutcome(URL, final_url, status=200,
                                                                reason='ok', retryable=False, content=parsed))
    crawler._fetch_page_content = AsyncMock(side_effect=AssertionError('unbounded fetch'))
    crawler._extract_file_links_from_soup = Mock(return_value=[final_url + 'files/manual.pdf'])
    links = asyncio.run(crawler.extract_file_links_from_page(URL))
    assert links == [final_url + 'files/manual.pdf']
    crawler.fetch_page.assert_awaited_once_with(URL, 'product')
    crawler._extract_file_links_from_soup.assert_called_once_with(parsed.soup, final_url)


def test_cancelled_browser_creation_releases_pool_permit():
    async def run():
        pool = wc.BrowserPool(max_concurrent_contexts=1)
        entered = asyncio.Event()
        async def create(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        pool._browser = SimpleNamespace(new_context=AsyncMock(side_effect=create))
        task = asyncio.create_task(pool.get_browser())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Повторное получение разрешения должно завершиться, а не ждать отменённый контекст.
        await asyncio.wait_for(pool._context_semaphore.acquire(), timeout=0.1)
        pool._context_semaphore.release()
    asyncio.run(run())


def test_cancelled_stealth_initialization_closes_created_context():
    async def run():
        pool = wc.BrowserPool(max_concurrent_contexts=1, stealth_enabled=True)
        entered = asyncio.Event()
        async def initialize(script):
            entered.set()
            await asyncio.Event().wait()
        context = SimpleNamespace(add_init_script=initialize, close=AsyncMock())
        pool._browser = SimpleNamespace(new_context=AsyncMock(return_value=context))
        task = asyncio.create_task(pool.get_browser())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        context.close.assert_awaited_once()
        await asyncio.wait_for(pool._context_semaphore.acquire(), timeout=0.1)
        pool._context_semaphore.release()
    asyncio.run(run())


def test_cleanup_timeout_still_returns_context(monkeypatch):
    timeout = wc.async_deadline
    monkeypatch.setattr(wc, 'async_deadline', lambda _: timeout(0.01))
    async def never(*args):
        await asyncio.Event().wait()
    pool = SimpleNamespace(close_page=AsyncMock(side_effect=never), return_browser=AsyncMock())
    asyncio.run(wc._release_browser_resources(pool, object(), object(), None))
    pool.return_browser.assert_awaited_once()


def test_context_return_timeout_does_not_escape_cleanup(monkeypatch):
    timeout = wc.async_deadline
    monkeypatch.setattr(wc, 'async_deadline', lambda _: timeout(0.01))
    async def never(*args):
        await asyncio.Event().wait()
    pool = SimpleNamespace(close_page=AsyncMock(), return_browser=AsyncMock(side_effect=never))
    asyncio.run(wc._release_browser_resources(pool, object(), object(), None))
    pool.close_page.assert_awaited_once()
