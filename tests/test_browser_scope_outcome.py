"""Блокировка главного документа сохраняет причину, ошибки iframe её не подменяют."""
import asyncio
import contextvars
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as BrowserError

from config import Config
from site_profiles.models import SiteProfile
import web_crawler as wc

URL = 'https://factory.test/product/pump'
FOREIGN = 'https://outside.invalid/product'


@pytest.mark.parametrize('method', ['playwright', 'ajax_tabs', 'dynamic'])
@pytest.mark.parametrize('failure', ['foreign', 'same_host', 'iframe'])
def test_navigation_failure_keeps_reason(tmp_path, method, failure):
    cfg = Config()
    cfg.base_dir = str(tmp_path)
    crawler = wc.WebCrawler(cfg)
    crawler.current_base_url = 'https://factory.test/'
    crawler._http_first_get = AsyncMock(return_value=None)
    crawler._fetch_with_aiohttp = AsyncMock(return_value=None)
    crawler._guard_browser_page = AsyncMock(return_value=None)
    if method == 'ajax_tabs':
        crawler.profile = SiteProfile(domain='factory.test')
        crawler.profile.crawl.ajax_tabs = True
    listeners = {}
    frame = object()
    request = SimpleNamespace(is_navigation_request=lambda: True,
                              frame=object() if failure == 'iframe' else frame,
                              url=URL if failure == 'same_host' else FOREIGN)
    async def goto(*args, **kwargs):
        for callback in listeners.get('requestfailed', []):
            callback(request)
        raise BrowserError('net::ERR_BLOCKED_BY_CLIENT' if failure == 'foreign' else 'net::ERR_CONNECTION_RESET')
    page = SimpleNamespace(main_frame=frame, url='about:blank', goto=goto,
                           on=lambda event, callback: listeners.setdefault(event, []).append(callback))
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    crawler.browser_pool = SimpleNamespace(get_browser=AsyncMock(return_value=context),
                                           close_page=AsyncMock(), return_browser=AsyncMock())
    outcome = asyncio.run(crawler.fetch_page(URL, 'contacts' if method == 'dynamic' else 'product'))
    assert outcome.content is None and outcome.transport == method
    assert outcome.reason == ('out_of_scope' if failure == 'foreign' else 'network_error')
    assert outcome.retryable is (failure != 'foreign')
    assert outcome.requested_url == URL
    if failure == 'foreign':
        assert outcome.final_url == FOREIGN
    crawler.browser_pool.close_page.assert_awaited_once_with(page)
    crawler.browser_pool.return_browser.assert_awaited_once_with(context)


def test_document_success_clears_previous_failed_navigation():
    listeners = {}
    frame = object()
    page = SimpleNamespace(main_frame=frame, on=lambda event, callback: listeners.setdefault(event, []).append(callback))
    state = wc._track_document_response(page)
    request = SimpleNamespace(is_navigation_request=lambda: True, frame=frame, url=FOREIGN)
    for callback in listeners.get('requestfailed', []):
        callback(request)
    assert state.get('failed_url') == FOREIGN
    response = SimpleNamespace(request=request, frame=frame)
    for callback in listeners['response']:
        callback(response)
    assert not state.get('failed_url') and state['response'] is response


@pytest.mark.parametrize('method', ['playwright', 'ajax_tabs', 'dynamic'])
@pytest.mark.parametrize('event_kind', ['cdp_main', 'route_main', 'cdp_iframe', 'route_iframe', 'cdp_popup', 'cdp_recovered'])
def test_real_guard_records_denial_when_browser_reports_original_url(tmp_path, method, event_kind):
    """Настоящий guard, как в Chromium: исключение/событие содержат исходный URL."""
    cfg = Config()
    cfg.base_dir = str(tmp_path)
    crawler = wc.WebCrawler(cfg)
    crawler.current_base_url = URL
    crawler._http_first_get = AsyncMock(return_value=None)
    crawler._fetch_with_aiohttp = AsyncMock(return_value=None)
    if method == 'ajax_tabs':
        crawler.profile = SiteProfile(domain='factory.test')
        crawler.profile.crawl.ajax_tabs = True
    commands = []
    sessions = {}
    routes = []

    class Session:
        def __init__(self, target):
            self.target = target
            self.listeners = {}

        def on(self, name, callback):
            self.listeners[name] = callback

        async def send(self, command, arguments):
            commands.append((command, arguments))
            if command == 'Page.getFrameTree':
                return {'frameTree': {'frame': {'id': self.target.frame_id}}}

        async def detach(self):
            commands.append(('detach', {}))

    class Context:
        async def new_cdp_session(self, target):
            sessions[target] = Session(target)
            return sessions[target]

        async def route(self, pattern, callback):
            routes.append(callback)

        async def unroute(self, pattern, callback):
            assert callback in routes
            commands.append(('unroute', {}))

        async def new_page(self):
            return page

    context = Context()

    class Page:
        def __init__(self, frame_id):
            self.context = context
            self.frame_id = frame_id
            self.main_frame = SimpleNamespace(page=self)
            self.listeners = {}
            self.url = 'about:blank'

        def on(self, name, callback):
            self.listeners.setdefault(name, []).append(callback)

        async def goto(self, *args, **kwargs):
            target = self
            if event_kind == 'cdp_popup':
                target = Page('popup')
                request = SimpleNamespace(resource_type='document', url=URL, frame=target.main_frame)
                await routes[0](SimpleNamespace(request=request, continue_=AsyncMock(), abort=AsyncMock()))
            if event_kind.startswith('cdp_'):
                event = {'requestId': 'denied', 'resourceType': 'Document',
                         'frameId': 'iframe' if event_kind == 'cdp_iframe' else target.frame_id,
                         'request': {'url': FOREIGN}}
                # Обработчики транспорта не обязаны наследовать ContextVar fetch_page.
                task = contextvars.Context().run(asyncio.create_task,
                    sessions[target].listeners['Fetch.requestPaused'](event))
                await task
                if event_kind == 'cdp_recovered':
                    await sessions[target].listeners['Fetch.requestPaused']({
                        'requestId': 'allowed', 'resourceType': 'Document', 'frameId': target.frame_id,
                        'request': {'url': URL}})
            else:
                request = SimpleNamespace(resource_type='document', url=FOREIGN,
                    frame=object() if event_kind == 'route_iframe' else self.main_frame)
                route = SimpleNamespace(request=request, continue_=AsyncMock(), abort=AsyncMock())
                await routes[0](route)
                route.abort.assert_awaited_once()
                route.continue_.assert_not_awaited()
            request = SimpleNamespace(is_navigation_request=lambda: True, frame=self.main_frame, url=URL)
            # Позднее событие исходного 302 не должно стирать решение CDP.
            for callback in self.listeners.get('response', []):
                callback(SimpleNamespace(request=request, frame=self.main_frame, status=302))
            for callback in self.listeners.get('requestfailed', []):
                callback(request)
            raise BrowserError('Page.goto: net::ERR_BLOCKED_BY_CLIENT at ' + URL)

    page = Page('main')
    crawler.browser_pool = SimpleNamespace(get_browser=AsyncMock(return_value=context),
        close_page=AsyncMock(), return_browser=AsyncMock())
    outcome = asyncio.run(crawler.fetch_page(URL, 'contacts' if method == 'dynamic' else 'product'))
    main_denied = event_kind in ('cdp_main', 'route_main')
    assert outcome.content is None and outcome.transport == method
    assert outcome.reason == ('out_of_scope' if main_denied else 'network_error')
    assert outcome.retryable is (not main_denied)
    if main_denied:
        assert outcome.final_url == FOREIGN
    if event_kind.startswith('cdp_'):
        assert ('Fetch.failRequest', {'requestId': 'denied', 'errorReason': 'BlockedByClient'}) in commands
    if event_kind == 'cdp_recovered':
        assert ('Fetch.continueRequest', {'requestId': 'allowed'}) in commands
    assert sum(command == 'detach' for command, _ in commands) == len(sessions)
    crawler.browser_pool.close_page.assert_awaited_once_with(page)
