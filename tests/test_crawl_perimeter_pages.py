"""Периметр транспорта: переходы проверяются до запроса и до чтения тела."""
import asyncio
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from web_crawler import WebCrawler


def run(coro):
    return asyncio.run(coro)


def crawler():
    obj = object.__new__(WebCrawler)
    obj._is_allowed_url = lambda url, **kw: urlparse(url).hostname == 'company.test'
    obj._effective_http_timeout = lambda: 30
    obj.permanent_errors_cache = set()
    obj._negative_cache_status = {}
    obj._host_retry_until = {}
    obj.url_normalizer = SimpleNamespace(normalize_url=lambda url: url)
    return obj


class Response:
    def __init__(self, url, status=200, location=None):
        self.url = url
        self.status = self.status_code = status
        self.headers = {'Location': location} if location is not None else {}
        self.reads = 0

    @property
    def text(self):
        self.reads += 1
        return 'контент'


@pytest.mark.parametrize('locations, expected', [
    (['/next', 'https://foreign.test/back'], ['https://company.test/start', 'https://company.test/next']),
    (['https://foreign.test/back'], ['https://company.test/start']),
    (['/next'], ['https://company.test/start', 'https://company.test/next']),
])
def test_curl_checks_each_hop_before_request(locations, expected):
    obj = crawler()
    calls, responses = [], []

    async def get(url, **kwargs):
        assert kwargs['allow_redirects'] is False
        calls.append(url)
        i = len(calls) - 1
        response = Response(url, 302, locations[i]) if i < len(locations) else Response(url)
        responses.append(response)
        return response

    result = run(obj._curl_get_in_perimeter(SimpleNamespace(get=get),
                 'https://company.test/start', {}, base_url='https://company.test/start'))
    assert calls == expected
    assert all(r.reads == 0 for r in responses)
    assert (result is not None) == (not any('foreign.test' in loc for loc in locations))


@pytest.mark.parametrize('json_response', [False, True])
def test_aiohttp_blocks_intermediate_foreign_hop_without_read(json_response):
    obj = crawler()
    calls, reads = [], []

    class Result:
        status = 302
        headers = {'Location': 'https://foreign.test/back'}
        url = 'https://company.test/start'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def text(self):
            reads.append('text')
            return 'html'

        async def json(self, **kwargs):
            reads.append('json')
            return {}

    def get(url, **kwargs):
        assert kwargs['allow_redirects'] is False
        calls.append(url)
        return Result()

    assert run(obj._aiohttp_get_in_perimeter(SimpleNamespace(get=get),
               'https://company.test/start', base_url='https://company.test/start',
               json_response=json_response)) is None
    assert calls == ['https://company.test/start']
    assert reads == []


def test_forbidden_original_never_calls_transport():
    obj = crawler()

    async def get(*args, **kwargs):
        raise AssertionError('запрос чужого адреса')

    assert run(obj._curl_get_in_perimeter(SimpleNamespace(get=get),
               'https://foreign.test/start', {}, base_url='https://company.test/')) is None
    assert run(obj._aiohttp_get_in_perimeter(SimpleNamespace(get=get),
               'https://foreign.test/start', base_url='https://company.test/')) is None


def test_curl_rejects_unexpected_final_before_body():
    obj = crawler()
    response = Response('https://foreign.test/final')

    async def get(*args, **kwargs):
        return response

    assert run(obj._curl_get_in_perimeter(SimpleNamespace(get=get),
               'https://company.test/start', {}, base_url='https://company.test/')) is None
    assert response.reads == 0


def test_browser_cdp_redirect_events_and_route_cleanup():
    obj = crawler()
    calls = []

    class Session:
        def on(self, event, callback):
            self.callback = callback

        async def send(self, command, arguments):
            calls.append((command, arguments))

        async def detach(self):
            calls.append(('detach', None))

    session = Session()

    class Context:
        async def new_cdp_session(self, page):
            return session

        async def route(self, pattern, callback):
            self.callback = callback

        async def unroute(self, pattern, callback):
            assert callback is self.callback
            calls.append(('unroute', None))

    context = Context()
    # Реальные Page хешируемы, как и ключи таблицы CDP-сессий.
    class Page:
        pass
    page = Page()
    page.context = context

    async def scenario():
        cleanup = await obj._guard_browser_page(page, 'https://company.test/')
        await session.callback({'requestId': 'first', 'request': {'url': 'https://company.test/start'}})
        await session.callback({'requestId': 'redirect', 'request': {'url': 'https://foreign.test/back'}})
        decisions = []

        class Route:
            request = SimpleNamespace(resource_type='script', url='https://cdn.test/app.js')
            async def continue_(self):
                decisions.append('continue')
            async def abort(self):
                decisions.append('abort')

        await context.callback(Route())
        assert decisions == ['continue']
        await cleanup()

    run(scenario())
    assert ('Fetch.continueRequest', {'requestId': 'first'}) in calls
    assert ('Fetch.failRequest', {'requestId': 'redirect', 'errorReason': 'BlockedByClient'}) in calls
    assert calls[-2:] == [('unroute', None), ('detach', None)]


def test_warmup_checks_redirects_and_uses_same_session(monkeypatch):
    import web_crawler
    obj = crawler()
    obj.config = SimpleNamespace(warmup_delay_seconds=0)
    obj.aiohttp_semaphore = asyncio.Semaphore(1)
    calls, closed = [], []

    async def get(url, **kwargs):
        assert kwargs['allow_redirects'] is False
        calls.append(url)
        if url.endswith('/target'):
            return Response(url, 302, 'https://foreign.test/back')
        return Response(url)

    async def close():
        closed.append(True)

    session = SimpleNamespace(get=get, close=close, cookies=SimpleNamespace(jar=[]))
    monkeypatch.setattr(web_crawler, '_CURL_CFFI_AVAILABLE', True)
    monkeypatch.setattr(web_crawler, '_CurlAsyncSession', lambda **kwargs: session)
    assert run(obj._cookie_warmup_and_retry(
        'https://company.test/target', 'https://company.test/')) is None
    assert calls == ['https://company.test/', 'https://company.test/target']
    assert closed == [True]


@pytest.mark.parametrize('prefix', ['m', 'mobile', 'touch'])
def test_mobile_document_requires_explicit_profile(prefix):
    from config import Config
    from domain_equivalency import DomainEquivalencyManager, url_in_scope
    source = 'https://company.test/'
    mobile = f'https://{prefix}.company.test/'
    assert not url_in_scope(mobile, source)
    manager = DomainEquivalencyManager(Config())
    assert not manager._url_allowed(mobile, source)
    manager.set_profile_domains('company.test', [f'{prefix}.company.test'])
    assert manager._url_allowed(mobile, source)
