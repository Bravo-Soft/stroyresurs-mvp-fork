"""Причины отказа, изоляция результатов загрузки и управляемые повторы."""
import asyncio
from collections import deque
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import web_crawler as wc
from config import Config


BASE = 'https://factory.test/'
URL = BASE + 'catalog/pump/?variant=red'
HTML = '<html><body><h1>Насос</h1>' + '<p>Мощность 20 кВт.</p>' * 100 + '</body></html>'


@pytest.fixture
def crawler(tmp_path):
    cfg = Config()
    cfg.base_dir = str(tmp_path)
    c = wc.WebCrawler(cfg)
    c.current_base_url = BASE
    c.page_retry_max_attempts = 2
    c._extract_additional_links = AsyncMock(return_value=[])
    c._enrich_bitrix_offers = AsyncMock(side_effect=lambda html, url: html)
    return c


class Response:
    def __init__(self, status, url=URL, headers=None, body=HTML, error=None):
        self.status = status
        self.url = url
        self.headers = headers or {}
        self.body = body
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        pass

    async def text(self):
        await asyncio.sleep(0)
        return self.body


async def http_attempt(crawler, response):
    session = SimpleNamespace(get=lambda *args, **kwargs: response)
    return await crawler._aiohttp_get_in_perimeter(session, str(response.url), base_url=BASE)


class Session:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, *args, **kwargs):
        return self.response


@pytest.mark.parametrize('status,reason,retryable', [
    (404, 'not_found', False), (410, 'gone', False),
    (429, 'rate_limited', True), (503, 'unavailable', True),
    (500, 'unavailable', True), (403, 'blocked', True),
    (408, 'timeout', True),
])
def test_http_status_becomes_typed_outcome(crawler, status, reason, retryable):
    async def fetch(url, category):
        return await http_attempt(crawler, Response(status))
    crawler._fetch_page_content = fetch
    outcome = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert isinstance(outcome, wc.FetchOutcome)
    assert (outcome.requested_url, outcome.final_url, outcome.transport) == (URL, URL, 'aiohttp')
    assert (outcome.status, outcome.reason, outcome.retryable) == (status, reason, retryable)
    assert outcome.content is None


@pytest.mark.parametrize('value,expected', [('120', 120), ('0', 0), ('-4', 0), ('nonsense', 0), ('', 0), (None, 0)])
def test_retry_after_seconds_handles_server_values(value, expected):
    assert wc.retry_after_seconds(value, now=1700000000) == expected


def test_retry_after_http_date_uses_supplied_clock():
    now = 1700000000
    later = format_datetime(datetime.fromtimestamp(now + 90, timezone.utc), usegmt=True)
    earlier = format_datetime(datetime.fromtimestamp(now - 90, timezone.utc), usegmt=True)
    assert wc.retry_after_seconds(later, now=now) == 90
    assert wc.retry_after_seconds(earlier, now=now) == 0


def test_429_preserves_retry_after_and_exact_requested_url(crawler):
    async def fetch(url, category):
        return await http_attempt(crawler, Response(429, url, {'Retry-After': '17'}))
    crawler._fetch_page_content = fetch
    outcome = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert outcome.retry_after == 17
    assert outcome.requested_url == URL and outcome.status == 429


def test_timeout_not_overwritten_by_other_transport_404(crawler, monkeypatch):
    monkeypatch.setattr(wc.aiohttp, 'TCPConnector', lambda **kwargs: None)
    monkeypatch.setattr(wc.aiohttp, 'ClientSession', lambda **kwargs: Session(
        Response(200, error=asyncio.TimeoutError())))
    async def fetch(url, category):
        crawler._record_fetch_status(url, 'curl', 404)
        return await crawler._fetch_with_aiohttp(url)
    crawler._fetch_page_content = fetch
    outcome = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert outcome.retryable
    assert outcome.reason in ('timeout', 'network_error')


@pytest.mark.parametrize('status', [429, 503])
@pytest.mark.parametrize('category', ['product', 'category', 'contacts'])
def test_rate_limit_and_unavailable_do_not_trigger_more_transports(crawler, monkeypatch, status, category):
    monkeypatch.setattr(wc, '_CURL_CFFI_AVAILABLE', True)
    crawler.config.http_first_enabled = True
    crawler.config.dynamic_content_enabled = False
    async def curl(url, **kwargs):
        crawler._record_fetch_status(url, 'curl', status)
        return wc.HttpFetchResult(HTML, status, {'retry-after': '20'}, url, {})
    crawler._fetch_with_http_impersonate = AsyncMock(side_effect=curl)
    crawler._cookie_warmup_and_retry = AsyncMock(return_value=None)
    session = AsyncMock()
    monkeypatch.setattr(wc.aiohttp, 'ClientSession', session)
    crawler.browser_pool.get_browser = AsyncMock()
    result = asyncio.run(crawler.fetch_page(URL, category))
    assert result.reason == ('rate_limited' if status == 429 else 'unavailable')
    crawler._fetch_with_http_impersonate.assert_awaited_once()
    crawler._cookie_warmup_and_retry.assert_not_awaited()
    session.assert_not_called()
    crawler.browser_pool.get_browser.assert_not_awaited()


def test_concurrent_fetch_outcomes_keep_their_own_status(crawler):
    async def fetch(url, category):
        await asyncio.sleep(0)
        return await http_attempt(crawler, Response(404 if '?missing' in url else 429, url))
    crawler._fetch_page_content = fetch
    async def run():
        urls = [BASE + 'catalog/pump/?missing=1', BASE + 'catalog/pump/?busy=1']
        missing, busy = await asyncio.gather(*(crawler.fetch_page(url, 'product') for url in urls))
        assert (missing.requested_url, missing.reason, missing.status) == (urls[0], 'not_found', 404)
        assert (busy.requested_url, busy.reason, busy.status) == (urls[1], 'rate_limited', 429)
    asyncio.run(run())


def test_success_outcome_carries_parsed_content(crawler):
    async def fetch(url, category):
        body = await http_attempt(crawler, Response(200, url))
        return await crawler._parse_content(body, url)
    crawler._fetch_page_content = fetch
    result = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert result.reason == 'ok' and not result.retryable
    assert result.status == 200 and result.content.html == HTML


def test_successful_fallback_overrides_earlier_transport404(crawler):
    async def fetch(url, category):
        crawler._record_fetch_status(url, 'curl', 404)
        body = await http_attempt(crawler, Response(200, url))
        return await crawler._parse_content(body, url)
    crawler._fetch_page_content = fetch
    result = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert result.reason == 'ok' and result.status == 200
    assert result.content is not None and not result.retryable


def test_all_transport_not_found_is_permanent(crawler):
    async def fetch(url, category):
        crawler._record_fetch_status(url, 'curl', 404)
        return await http_attempt(crawler, Response(404, url))
    crawler._fetch_page_content = fetch
    result = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert result.reason == 'not_found' and not result.retryable


@pytest.mark.parametrize('error,reason,retryable', [
    (asyncio.TimeoutError(), 'timeout', True),
    (RuntimeError('programming defect'), 'internal_error', False),
])
def test_uncaught_fetch_failure_has_explicit_reason(crawler, error, reason, retryable):
    crawler._fetch_page_content = AsyncMock(side_effect=error)
    result = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert result.reason == reason and result.retryable == retryable
    assert result.content is None


def test_out_of_scope_url_never_enters_fetch_ladder(crawler):
    crawler._fetch_page_content = AsyncMock(return_value=None)
    result = asyncio.run(crawler.fetch_page('https://unrelated.test/product/', 'product'))
    assert result.reason == 'out_of_scope' and not result.retryable
    crawler._fetch_page_content.assert_not_awaited()


def outcome(reason='timeout', retryable=True, delay=0):
    return wc.FetchOutcome(requested_url=URL, final_url=URL, transport='aiohttp',
                           status=None, reason=reason, retryable=retryable,
                           retry_after=delay, content=None)


@pytest.mark.parametrize('reason', ['not_found', 'gone', 'soft_not_found', 'out_of_scope', 'internal_error'])
def test_permanent_outcome_not_registered_for_retry(crawler, reason):
    crawler._register_page_retry(URL, 1, 'product', outcome(reason, False))
    assert not crawler._page_retry_candidates


def test_retry_after_defers_without_losing_candidate(crawler, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(wc.time, 'monotonic', lambda: clock[0])
    crawler._register_page_retry(URL, 1, 'product', outcome(delay=30))
    async def run():
        queue = deque()
        await crawler._enqueue_page_retries(queue)
        assert not queue and crawler._page_retry_candidates
        assert not crawler._page_retry_attempts
        clock[0] += 31
        await crawler._enqueue_page_retries(queue)
        assert len(queue) == 1 and queue[0][0] == URL
        assert crawler._page_retry_attempts[crawler.url_normalizer.normalize_url(URL)] == 1
    asyncio.run(run())


def test_retry_quota_rejection_keeps_candidate_until_slot_available(crawler):
    crawler.max_product_pages_per_site = 1
    other = crawler.url_normalizer.normalize_url(BASE + 'catalog/other/')
    crawler.product_urls.add(other)
    crawler._register_page_retry(URL, 1, 'product', outcome())
    crawler._retry_due[crawler.url_normalizer.normalize_url(URL)] = 0
    async def run():
        queue = deque()
        await crawler._enqueue_page_retries(queue)
        assert not queue and crawler._page_retry_candidates
        assert not crawler._page_retry_attempts
        crawler._retry_summary()
        assert any(row['url'] == URL and row['reason'] == 'quota'
                   for row in crawler.stats['pending_retries'])
        crawler.product_urls.discard(other)
        await crawler._enqueue_page_retries(queue)
        assert len(queue) == 1 and queue[0][0] == URL
    asyncio.run(run())


def test_exhausted_page_budget_exposes_pending_retry(crawler):
    crawler.max_pages_per_site = 0
    crawler._register_page_retry(URL, 1, 'product', outcome())
    asyncio.run(crawler._drain_crawl_queue(deque(), BASE, 'Завод', {}, []))
    assert crawler._page_retry_candidates
    rows = crawler.stats['pending_retries']
    assert any(row['url'] == URL and row['reason'] == 'page_budget' for row in rows)
    assert all({'url', 'reason', 'attempt', 'retry_in'} <= row.keys() for row in rows)


def test_retry_delay_beyond_wait_budget_is_retained_and_reported(crawler, monkeypatch):
    clock = [1000.0]
    sleeps = []
    monkeypatch.setattr(wc.time, 'monotonic', lambda: clock[0])
    async def advance(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(wc.asyncio, 'sleep', advance)
    crawler._register_page_retry(URL, 1, 'product', outcome(delay=120))
    asyncio.run(crawler._drain_crawl_queue(deque(), BASE, 'Завод', {}, []))
    assert crawler._page_retry_candidates and not crawler._page_retry_attempts
    assert 0 < sum(sleeps) <= 60
    row = next(row for row in crawler.stats['pending_retries'] if row['url'] == URL)
    assert row['reason'] == 'delay_budget' and row['retry_in'] >= 60


@pytest.mark.parametrize('reason,status', [('rate_limited', 429), ('unavailable', 503)])
def test_retry_after_sets_pause_for_responding_host(crawler, monkeypatch, reason, status):
    monkeypatch.setattr(wc.time, 'monotonic', lambda: 1000)
    async def fetch(url, category):
        return await http_attempt(crawler, Response(status, url, {'Retry-After': '30'}))
    crawler._fetch_page_content = fetch
    failed = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert failed.reason == reason
    assert crawler._host_retry_until['factory.test'] >= 1030
    crawler._fetch_page_content = AsyncMock(return_value=None)
    postponed = asyncio.run(crawler.fetch_page(BASE + 'catalog/other/', 'product'))
    assert postponed.retryable and postponed.retry_after >= 30
    crawler._fetch_page_content.assert_not_awaited()


def test_retry_limit_is_per_url_and_not_reset_by_new_failure(crawler):
    norm = crawler.url_normalizer.normalize_url(URL)
    crawler._page_retry_attempts[norm] = crawler.page_retry_max_attempts
    crawler._register_page_retry(URL, 1, 'product', outcome())
    assert not crawler._page_retry_candidates


def test_reset_clears_retry_delays_and_host_pause(crawler):
    crawler._register_page_retry(URL, 1, 'product', outcome(delay=30))
    crawler._host_retry_until['factory.test'] = 12345
    asyncio.run(crawler.reset_state('Другая компания'))
    assert not crawler._retry_due and not crawler._host_retry_until
    assert not crawler._page_retry_candidates and not crawler._page_retry_attempts


@pytest.mark.parametrize('status', [404, 410])
@pytest.mark.parametrize('transport', ['aiohttp', 'curl', 'playwright'])
def test_cached_failure_keeps_precise_status_without_network(crawler, monkeypatch, status, transport):
    monkeypatch.setattr(wc, '_CURL_CFFI_AVAILABLE', True)
    crawler._record_fetch_status(URL, transport, status)
    session_factory = AsyncMock()
    monkeypatch.setattr(wc.aiohttp, 'ClientSession', session_factory)
    monkeypatch.setattr(wc, '_CurlAsyncSession', session_factory)
    crawler.browser_pool.get_browser = AsyncMock()
    async def fetch(url, category):
        if transport == 'aiohttp':
            await crawler._fetch_with_aiohttp(url)
        elif transport == 'curl':
            await crawler._fetch_with_http_impersonate(url)
        else:
            await crawler._fetch_with_playwright(url)
        return None
    crawler._fetch_page_content = fetch
    result = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert result.status == status
    assert result.reason == ('not_found' if status == 404 else 'gone')
    assert not result.retryable and result.requested_url == URL
    session_factory.assert_not_called()
    crawler.browser_pool.get_browser.assert_not_awaited()


@pytest.mark.parametrize('transport', ['aiohttp', 'curl'])
def test_redirect_outside_scope_has_typed_reason_and_no_second_request(crawler, transport):
    foreign = 'https://unrelated.test/catalog/pump/'
    response = Response(302, URL, {'Location': foreign})
    response.status_code = 302
    session = SimpleNamespace(get=AsyncMock(return_value=response)) if transport == 'curl' else SimpleNamespace(get=lambda *args, **kwargs: response)
    requested = []
    if transport == 'aiohttp':
        def get(url, **kwargs):
            requested.append(url)
            return response
        session.get = get
    async def fetch(url, category):
        if transport == 'curl':
            await crawler._curl_get_in_perimeter(session, url, {}, base_url=BASE)
        else:
            await crawler._aiohttp_get_in_perimeter(session, url, base_url=BASE)
        return None
    crawler._fetch_page_content = fetch
    result = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert result.reason == 'out_of_scope' and not result.retryable
    assert result.requested_url == URL and result.final_url == foreign
    if transport == 'curl':
        session.get.assert_awaited_once()
    else:
        assert requested == [URL]


@pytest.mark.parametrize('status', [429, 503])
def test_warmup_busy_home_does_not_request_target(crawler, monkeypatch, status):
    monkeypatch.setattr(wc, '_CURL_CFFI_AVAILABLE', True)
    response = SimpleNamespace(status_code=status, url=BASE,
                               headers={'Retry-After': '12'}, text=HTML)
    session = SimpleNamespace(get=AsyncMock(return_value=response), close=AsyncMock())
    monkeypatch.setattr(wc, '_CurlAsyncSession', lambda **kwargs: session)
    async def fetch(url, category):
        await crawler._cookie_warmup_and_retry(url, BASE)
        return None
    crawler._fetch_page_content = fetch
    result = asyncio.run(crawler.fetch_page(URL, 'product'))
    assert result.status == status and result.retry_after == 12
    assert result.requested_url == URL and result.final_url == BASE
    session.get.assert_awaited_once()
    assert session.get.call_args.args[0] == BASE
    session.close.assert_awaited_once()


def test_admitted_retry_remaining_in_queue_is_reported(crawler):
    crawler._register_page_retry(URL, 1, 'product', outcome())
    norm = crawler.url_normalizer.normalize_url(URL)
    crawler._retry_due[norm] = 0
    queue = deque()
    asyncio.run(crawler._enqueue_page_retries(queue))
    assert len(queue) == 1 and not crawler._page_retry_candidates
    crawler._host_retry_until['factory.test'] = wc.time.monotonic() + 100
    crawler._retry_summary(queue)
    row = next(row for row in crawler.stats['pending_retries'] if row['url'] == URL)
    assert row['reason'] == 'host_cooldown' and row['attempt'] == 1
    assert row['retry_in'] > 0
