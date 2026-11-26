"""Созревшие повторы обслуживаются среди новых URL, сохраняя квоты и паузы."""
import asyncio
from collections import Counter, deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bs4 import BeautifulSoup
import pytest
import web_crawler as wc
from config import Config

BASE = 'https://factory.test/'
HTML = '<html><body><h1>Насос</h1>' + '<p>Мощность 20 кВт. Производительность 30 м3/ч.</p>' * 25 + '</body></html>'


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = [1000.0]
    real_sleep = asyncio.sleep
    time = SimpleNamespace(**{n: getattr(wc.time, n) for n in dir(wc.time)})
    time.monotonic = lambda: clock[0]
    monkeypatch.setattr(wc, 'time', time)
    async def sleep(seconds, result=None):
        clock[0] += max(0, seconds)
        return await real_sleep(0, result=result)
    monkeypatch.setattr(wc.asyncio, 'sleep', sleep)
    cfg = Config(); cfg.base_dir = str(tmp_path); cfg.max_concurrent_pages = 1
    cfg.product_quota_card_reserve = 0
    c = wc.WebCrawler(cfg); c.current_base_url = BASE
    c._effective_max_concurrent_pages = 1
    c._check_memory_usage = AsyncMock()
    c._process_links_from_parse_result = AsyncMock()
    c.max_pages_per_site = 8
    c.max_product_pages_per_site = 2
    c.product_quota_card_reserve = 0
    c.url_categorizer.is_listing_url = lambda _: False
    c.page_retry_max_attempts = 2
    return c, clock


def install_fetch(crawler, clock, failures, *, status=503, delay=0):
    calls, times = [], []
    counts = Counter()
    async def fetch(url, category):
        calls.append(url); times.append(clock[0]); counts[url] += 1
        clock[0] += 1
        await asyncio.sleep(0)
        if counts[url] <= failures.get(url, 0):
            return wc.FetchOutcome(url, status=status, reason='unavailable' if status==503 else 'not_found',
                                   retryable=status==503, retry_after=delay)
        page = wc.ParseResult(BeautifulSoup(HTML, 'html.parser'), HTML, [], 'product_page', {}, url=url)
        return wc.FetchOutcome(url, url, status=200, reason='ok', retryable=False, content=page)
    crawler.fetch_page = fetch
    return calls, counts, times


async def admit(c, urls, category='category'):
    queue = deque()
    for url in urls:
        if await c._should_add_to_queue_parallel(url, 0, category):
            queue.append((url, 0, category, 9))
    return queue


def test_due_retry_precedes_exhaustion_of_nonempty_queue(setup):
    c, clock = setup; target = BASE + 'catalog/first'
    c.max_pages_per_site = 5
    calls, counts, times = install_fetch(c, clock, {target: 1})
    async def run():
        q = await admit(c, [target] + [BASE + f'category/{n}' for n in range(20)])
        await c._drain_crawl_queue(q, BASE, 'Завод', {}, [])
    asyncio.run(run())
    assert counts[target] == 2 and len(calls) == 5
    assert c.stats['failed_pages'] == 0 and c.stats['failed_attempts'] == 1
    assert c.stats['pending_retries'] == []
    assert times[calls.index(target, 1)] - times[0] >= 3  # запрос + backoff


@pytest.mark.parametrize('sitemap', [False, True])
def test_retry_keeps_its_quota_ahead_of_fresh_sitemap_reserve(setup, sitemap):
    c, clock = setup; target = BASE + 'catalog/first'
    c.max_product_pages_per_site = 1; c.config.sitemap_max_urls = 1
    calls, counts, _ = install_fetch(c, clock, {target: 1})
    async def run():
        if sitemap:
            rows = [(u, u, 'product', 10) for u in [target, BASE + 'catalog/next']]
            c._sitemap_published_urls.update({u: u for u, *_ in rows})
            q = deque(); await c._add_sitemap_urls_to_queue(q, rows, BASE)
        else:
            q = await admit(c, [target], 'product')
        await c._drain_crawl_queue(q, BASE, 'Завод', {}, [])
    asyncio.run(run())
    assert counts[target] == 2 and c.stats['failed_pages'] == 0
    assert len(c.product_urls) == 1 and c.stats['pending_retries'] == []
    if sitemap:
        assert counts[BASE + 'catalog/next'] == 0
        assert len(c._sitemap_slots) == 1 and len(c._sitemap_pending) == 1


@pytest.mark.parametrize('status, attempts', [(503, 2), (404, 1)])
def test_terminal_failure_releases_slot_for_reserve(setup, status, attempts):
    c, clock = setup; target = BASE + 'catalog/first'; next_url = BASE + 'catalog/next'
    c.max_product_pages_per_site = 1; c.config.sitemap_max_urls = 1; c.page_retry_max_attempts = 1
    calls, counts, _ = install_fetch(c, clock, {target: 20}, status=status)
    async def run():
        rows = [(u, u, 'product', 10) for u in [target, next_url]]
        c._sitemap_published_urls.update({u:u for u, *_ in rows})
        q = deque(); await c._add_sitemap_urls_to_queue(q, rows, BASE)
        await c._drain_crawl_queue(q, BASE, 'Завод', {}, [])
    asyncio.run(run())
    assert counts == {target: attempts, next_url: 1}
    assert c.stats['pending_retries'] == [] and target not in c.product_urls
    assert target not in c._sitemap_slots and len(c.product_urls) <= 1


def test_ready_retries_and_fresh_pages_alternate(setup):
    c, clock = setup; c.max_pages_per_site = 4
    retries = [BASE + f'category/retry{n}' for n in range(4)]
    fresh = [BASE + f'category/new{n}' for n in range(4)]
    calls, _, _ = install_fetch(c, clock, {})
    async def run():
        for u in retries:
            c._register_page_retry(u, 0, 'category')
        q = await admit(c, fresh)
        await c._drain_crawl_queue(q, BASE, 'Завод', {}, [])
    asyncio.run(run())
    assert calls == [retries[0], fresh[0], retries[1], fresh[1]]
    assert len(c.stats['pending_retries']) == 2


def test_retry_after_is_not_shortened_while_fresh_queue_is_busy(setup):
    c, clock = setup; target = BASE + 'category/slow'
    calls, counts, times = install_fetch(c, clock, {target:1}, delay=120)
    async def run():
        q = await admit(c, [target] + [BASE + f'category/{n}' for n in range(20)])
        await c._drain_crawl_queue(q, BASE, 'Завод', {}, [])
    asyncio.run(run())
    assert counts[target] == 1 and len(calls) == 8
    assert c.stats['pending_retries'][0]['retry_in'] > 100


def test_paused_retry_does_not_block_other_host_or_exceed_budget(setup):
    c, clock = setup; c.max_pages_per_site = 3; c._effective_max_concurrent_pages = 2
    # Разрешённый www-вариант; пауза хранится для точного хоста.
    target = BASE + 'category/retry'; fresh = 'https://www.factory.test/category/new'
    calls, counts, times = install_fetch(c, clock, {})
    async def run():
        c._register_page_retry(target, 0, 'category')
        c._host_retry_until['factory.test'] = clock[0] + 10
        q = await admit(c, [fresh])
        await c._drain_crawl_queue(q, BASE, 'Завод', {}, [])
    asyncio.run(run())
    assert calls == [fresh, target] and times[1] >= 1010
    assert c.stats['attempted_pages'] == 2


def test_parallel_retry_batch_respects_remaining_global_budget(setup):
    c, clock = setup; c.max_pages_per_site = 3; c._effective_max_concurrent_pages = 10
    retries = [BASE + f'category/retry{n}' for n in range(4)]
    fresh = [BASE + f'category/new{n}' for n in range(4)]
    calls, counts, _ = install_fetch(c, clock, {})
    async def run():
        for u in retries:
            c._register_page_retry(u, 0, 'category')
        q = await admit(c, fresh)
        await c._drain_crawl_queue(q, BASE, 'Завод', {}, [])
    asyncio.run(run())
    assert calls == [retries[0], fresh[0], retries[1]]
    assert c.stats['total_pages'] == 3 and max(counts.values()) == 1
    assert c.stats['remaining_pages'] == 5
    assert len(c.stats['pending_retries']) == 2


def test_recovered_catalog_children_are_saved_before_budget_ends(setup):
    c, clock = setup; c.max_pages_per_site = 5
    target = BASE + 'category/catalog'; child = BASE + 'catalog/pump-123/'
    calls, counts, _ = install_fetch(c, clock, {target:1})
    c.page_sink = asyncio.Queue()
    async def links(url, parsed, depth, queue):
        if url == target and await c._should_add_to_queue_parallel(child, depth+1, 'product'):
            queue.append((child, depth+1, 'product', 11))
    c._process_links_from_parse_result = links
    async def run():
        q = await admit(c, [target, BASE+'category/a', BASE+'category/b'])
        stored = []
        await c._drain_crawl_queue(q, BASE, 'Завод', {}, stored)
        return stored
    stored = asyncio.run(run())
    assert calls == [target, BASE+'category/a', BASE+'category/b', target, child]
    assert counts[target] == 2 and c.stats['failed_pages'] == 0
    assert child in {p['url'] for p in stored} and c.page_sink.qsize() == 4
