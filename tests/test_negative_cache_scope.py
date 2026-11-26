"""Отрицательный ответ одного транспорта не закрывает остальные способы загрузки."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import web_crawler as wc
from config import Config
from web_crawler import WebCrawler

URL = 'https://factory.test/catalog/pump-1/'
HTML = '<html><body>' + 'Характеристики оборудования. ' * 100 + '</body></html>'


@pytest.fixture
def crawler(tmp_path):
    cfg = Config(); cfg.base_dir = str(tmp_path)
    c = WebCrawler(cfg); c.current_base_url = 'https://factory.test/'
    return c


class Context:
    def __init__(self, value): self.value = value
    async def __aenter__(self): return self.value
    async def __aexit__(self, *args): pass


def aiohttp(c, monkeypatch, statuses):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        response = SimpleNamespace(status=statuses.pop(0), url=url, headers={},
                                   text=AsyncMock(return_value=HTML))
        return Context(response)
    monkeypatch.setattr(wc.aiohttp, 'TCPConnector', Mock())
    monkeypatch.setattr(wc.aiohttp, 'ClientSession', lambda **kw: Context(SimpleNamespace(get=get)))
    return calls


def curl_session(statuses):
    calls = []
    async def get(url, **kw):
        calls.append(url)
        return SimpleNamespace(status_code=statuses.pop(0), url=url, headers={}, text=HTML)
    return SimpleNamespace(get=get, cookies=SimpleNamespace(jar=[]), close=AsyncMock()), calls


@pytest.mark.parametrize('status', [400, 401, 403, 405, 408, 418, 429, 500, 501, 503])
@pytest.mark.parametrize('transport', ['aiohttp', 'curl'])
def test_transient_or_client_error_does_not_poison_retry(crawler, monkeypatch, status, transport):
    async def run():
        if transport == 'aiohttp':
            calls = aiohttp(crawler, monkeypatch, [status, 200])
            method = crawler._fetch_with_aiohttp
        else:
            monkeypatch.setattr(wc, '_CURL_CFFI_AVAILABLE', True)
            session, calls = curl_session([status, 200])
            async def method(url): return await crawler._fetch_with_http_impersonate(url, session=session)
        await method(URL)
        assert not crawler.permanent_errors_cache
        result = await method(URL)
        assert result is not None
        assert calls == [URL, URL]
        assert not crawler.permanent_errors_cache
    asyncio.run(run())


@pytest.mark.parametrize('status', [404, 410])
def test_missing_resource_is_cached_only_for_same_transport(crawler, monkeypatch, status):
    async def run():
        calls = aiohttp(crawler, monkeypatch, [status, 200])
        assert await crawler._fetch_with_aiohttp(URL) is None
        assert await crawler._fetch_with_aiohttp(URL) is None
        assert calls == [URL]
        session, curl_calls = curl_session([200])
        monkeypatch.setattr(wc, '_CURL_CFFI_AVAILABLE', True)
        result = await crawler._fetch_with_http_impersonate(URL, session=session)
        assert result.status == 200 and curl_calls == [URL]
        assert not crawler.permanent_errors_cache
        assert await crawler._fetch_with_aiohttp(URL) is not None
        assert calls == [URL, URL]
    asyncio.run(run())


@pytest.mark.parametrize('other', [URL.rstrip('/'), URL.replace('https:', 'http:'), URL + '?variant=2'])
def test_negative_cache_does_not_merge_request_spellings(crawler, monkeypatch, other):
    async def run():
        calls = aiohttp(crawler, monkeypatch, [404, 200])
        assert await crawler._fetch_with_aiohttp(URL) is None
        assert await crawler._fetch_with_aiohttp(other) is not None
        assert calls == [URL, other]
    asyncio.run(run())


def test_new_run_discards_all_transport_failures(crawler):
    for transport in ('curl', 'aiohttp', 'playwright'):
        crawler._record_fetch_status(URL, transport, 404)
    assert len(crawler.permanent_errors_cache) == 3
    asyncio.run(crawler.reset_state('Следующий проход'))
    assert not crawler.permanent_errors_cache


def test_browser_fallback_after_http_404_clears_cache(crawler):
    async def run():
        crawler._record_fetch_status(URL, 'aiohttp', 404)
        crawler._record_fetch_status(URL, 'curl', 404)
        page = SimpleNamespace(url=URL, goto=AsyncMock(return_value=SimpleNamespace(status=200)),
                               wait_for_load_state=AsyncMock(), is_closed=lambda: False,
                               on=Mock(), content=AsyncMock(return_value=HTML))
        crawler.browser_pool = SimpleNamespace(get_browser=AsyncMock(return_value=SimpleNamespace(
            new_page=AsyncMock(return_value=page))), close_page=AsyncMock(), return_browser=AsyncMock())
        crawler._guard_browser_page = AsyncMock(return_value=AsyncMock())
        for name in ('_accept_cookie_consent', '_scroll_page', '_wait_for_product_content', '_wait_for_dynamic_links'):
            setattr(crawler, name, AsyncMock())
        crawler._extract_dynamic_product_content = AsyncMock(return_value=HTML)
        content, failed, final = await crawler._fetch_with_playwright(URL)
        assert content and not failed and final == URL
        assert not crawler.permanent_errors_cache
    asyncio.run(run())
