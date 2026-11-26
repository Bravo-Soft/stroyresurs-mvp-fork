"""Единый цикл обхода, сохранение состояния и отрицательный кэш текущего прохода."""
import asyncio
from collections import Counter, deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bs4 import BeautifulSoup

import web_crawler as wc
from config import Config
from web_crawler import ParseResult, WebCrawler

BASE = 'https://factory.test/'
CAT = BASE + 'sections/deep/range/family/'
PRODUCT = BASE + 'catalog/pump-123/'


@pytest.fixture
def crawler(tmp_path, monkeypatch):
    clock = [wc.time.monotonic()]
    real_sleep = asyncio.sleep
    # Таймер wait_for использует настоящее время event loop; ожидания краулера
    # двигают отдельные часы и сохраняют переключение между async-задачами.
    crawler_time = SimpleNamespace(**{name: getattr(wc.time, name) for name in dir(wc.time)})
    crawler_time.monotonic = lambda: clock[0]
    monkeypatch.setattr(wc, 'time', crawler_time)
    async def sleep(seconds, result=None):
        clock[0] += max(0, seconds)
        return await real_sleep(0, result=result)
    monkeypatch.setattr(wc.asyncio, 'sleep', sleep)
    cfg = Config()
    cfg.base_dir = str(tmp_path)
    cfg.max_concurrent_pages = 2
    c = WebCrawler(cfg)
    c.current_base_url = BASE
    c.max_pages_per_site = 20
    c.page_retry_max_attempts = 2
    c._check_memory_usage = AsyncMock()
    c._discover_and_parse_sitemap = AsyncMock(return_value=[])
    c._get_start_urls = lambda _: [CAT]
    c.url_categorizer.categorize_url = lambda u, *a: ('category', 7) if u == CAT else ('product', 9)
    return c


def page(url, links=()):
    html = '<html><body>' + 'Характеристики насосного оборудования. ' * 40 + '</body></html>'
    return ParseResult(BeautifulSoup(html, 'html.parser'), html,
                       [(u, 'product', 9) for u in links], 'product_page', {}, url=url)


def test_retry_catalog_children_are_drained_and_streamed(crawler):
    async def run():
        calls = Counter()
        async def fetch(url, category):
            calls[url] += 1
            return None if url == CAT and calls[url] == 1 else page(url, [PRODUCT] if url == CAT else [])
        crawler._fetch_page_content = fetch
        crawler.page_sink = asyncio.Queue()
        stored = []
        await crawler._start_crawling_with_sitemap_support(BASE, 'Тест', {}, stored)
        assert calls == {CAT: 2, PRODUCT: 1}
        assert {row['url'] for row in stored} == {CAT, PRODUCT}
        assert crawler.page_sink.qsize() == 2
    asyncio.run(asyncio.wait_for(run(), 5))


def test_each_new_child_gets_own_retry_budget(crawler):
    async def run():
        calls = Counter()
        async def fetch(url, category):
            calls[url] += 1
            if calls[url] <= 2:
                return None
            return page(url, [PRODUCT] if url == CAT else [])
        crawler._fetch_page_content = fetch
        await crawler._start_crawling_with_sitemap_support(BASE, 'Тест', {}, [])
        assert calls == {CAT: 3, PRODUCT: 3}
    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize('category', ['category', 'contacts', 'other', 'product'])
def test_path_segments_do_not_replace_link_depth(crawler, category):
    async def run():
        url = BASE + 'a/b/c/d/e/f/g/h/item/'
        crawler.url_categorizer.categorize_url = lambda *_: (category, 7)
        assert crawler._should_add_to_queue(url, 1)
        assert await crawler._should_add_to_queue_parallel(url, 1, category)
        assert not await crawler._should_skip_url(url, 1, 'Тест')
        assert not await crawler._should_add_to_queue_parallel(url + 'next', crawler.max_depth + 1, category)
        assert await crawler._should_skip_url(url, crawler.max_depth + 1, 'Тест')
    asyncio.run(run())


def test_all_queue_gates_reject_over_pagination_limit(crawler):
    async def run():
        url = PRODUCT + '?page=999'
        assert not crawler._should_add_to_queue(url, 1)
        assert not await crawler._should_add_to_queue_parallel(url, 1, 'product')
        assert await crawler._should_skip_url(url, 1, 'Тест')
        assert not crawler.product_urls and not crawler.visited_urls
    asyncio.run(run())


def test_memory_cleanup_preserves_crawl_identity(crawler, monkeypatch):
    crawler._check_memory_usage = WebCrawler._check_memory_usage.__get__(crawler)
    sets = ['visited_urls', 'processed_urls', 'product_urls', '_crawled_scheme_keys',
            '_sitemap_slots', '_quota_released', 'permanent_errors_cache']
    for name in sets:
        getattr(crawler, name).add(PRODUCT)
    crawler._page_retry_candidates[CAT] = (CAT, 1, 'category', 1)
    crawler._sitemap_pending.append((PRODUCT, PRODUCT, 'product', 9))
    monkeypatch.setattr(wc, 'read_memory_usage_mb', lambda: 100000)
    asyncio.run(crawler._check_memory_usage())
    for name in sets:
        assert getattr(crawler, name) == {PRODUCT}, name
    assert crawler._page_retry_candidates and crawler._sitemap_pending


def test_old_disk_cache_is_neither_loaded_nor_rewritten(tmp_path):
    cache = tmp_path / 'permanent_errors_cache.json'
    original = ('["' + PRODUCT + '"]').encode()
    cache.write_bytes(original)
    cfg = Config(); cfg.base_dir = str(tmp_path)
    crawler = WebCrawler(cfg)
    assert not crawler.permanent_errors_cache
    crawler._save_permanent_errors_cache()
    assert cache.read_bytes() == original


def test_page_limit_counts_concurrent_requests_and_retries(crawler):
    async def run():
        crawler.max_pages_per_site = 1
        crawler._effective_max_concurrent_pages = 8
        crawler._get_start_urls = lambda _: [BASE + f'catalog/pump-{i}/' for i in range(10)]
        calls = []
        async def fetch(url, category):
            calls.append(url)
            await asyncio.sleep(0.02)
            return None
        crawler._fetch_page_content = fetch
        await crawler._start_crawling_with_sitemap_support(BASE, 'Тест', {}, [])
        assert len(calls) == 1
        assert crawler.stats['total_pages'] == 1
    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize('entry', ['storage', 'sitemap'])
def test_shared_cycle_handles_both_entry_points(crawler, entry):
    async def run():
        calls = Counter()
        crawler._effective_max_concurrent_pages = 1
        if entry == 'sitemap':
            crawler._get_start_urls = lambda _: []
            crawler._discover_and_parse_sitemap = AsyncMock(return_value=[(CAT, CAT, 'category', 9)])
        async def fetch(url, category):
            calls[url] += 1
            if url == CAT and calls[url] == 1:
                return None
            return page(url, [PRODUCT] if url == CAT else [])
        crawler._fetch_page_content = fetch
        method = crawler._start_crawling_with_storage if entry == 'storage' else crawler._start_crawling_with_sitemap_support
        await method(BASE, 'Тест', {}, [])
        assert calls == {CAT: 2, PRODUCT: 1}
    asyncio.run(asyncio.wait_for(run(), 5))


def test_failure_attempts_are_bounded_without_resetting_processed(crawler):
    async def run():
        crawler._fetch_page_content = AsyncMock(return_value=None)
        await crawler._start_crawling_with_sitemap_support(BASE, 'Тест', {}, [])
        assert crawler._fetch_page_content.await_count == 3
        assert crawler.url_normalizer.normalize_url(CAT) in crawler.processed_urls
        assert not crawler._page_retry_candidates
    asyncio.run(asyncio.wait_for(run(), 5))


def test_last_active_pages_finish_saving_at_total_limit(crawler):
    async def run():
        crawler.max_pages_per_site = 2
        urls = [PRODUCT, PRODUCT + 'other/']
        crawler._get_start_urls = lambda _: urls
        async def fetch(url, category):
            await asyncio.sleep(0.01 if url == PRODUCT else 0.04)
            return page(url)
        crawler._fetch_page_content = fetch
        stored = []
        await crawler._start_crawling_with_sitemap_support(BASE, 'Тест', {}, stored)
        assert {r['url'] for r in stored} == set(urls)
        assert crawler.stats['total_pages'] == 2
    asyncio.run(asyncio.wait_for(run(), 5))


def test_cancel_waits_for_running_pages(crawler):
    async def run():
        entered, stopped = asyncio.Event(), asyncio.Event()
        async def fetch(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        crawler._fetch_page_content = fetch
        task = asyncio.create_task(crawler._start_crawling_with_sitemap_support(BASE, 'Тест', {}, []))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert stopped.is_set()
        assert crawler.url_normalizer.normalize_url(CAT) in crawler.processed_urls
    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.parametrize('url', [PRODUCT + '?page=999', 'https://foreign.test/catalog/pump/', BASE + 'image.jpg'])
@pytest.mark.parametrize('source', ['start', 'sitemap', 'retry', 'link', 'failopen'])
def test_sources_share_url_filters(crawler, url, source):
    async def run():
        queue = deque()
        crawler._fetch_page_content = AsyncMock(return_value=None)
        if source == 'start':
            crawler._get_start_urls = lambda _: [url]
            await crawler._start_crawling_with_sitemap_support(BASE, 'Тест', {}, [])
        elif source == 'sitemap':
            await crawler._add_sitemap_urls_to_queue(queue, [(url, url, 'product', 9)], BASE)
        elif source == 'retry':
            crawler._register_page_retry(url, 1, 'product')
            await crawler._enqueue_page_retries(queue)
        elif source == 'link':
            await crawler._process_links_from_parse_result(CAT, page(CAT, [url]), 0, queue)
        else:
            crawler.url_categorizer.release_starved_filters = lambda: (True, [(url, '')])
            crawler._apply_exclude_failopen(queue)
        assert not queue and not crawler.product_urls
        assert crawler._fetch_page_content.await_count == 0
    asyncio.run(run())


def test_atomic_admission_occupies_only_one_slot(crawler):
    async def run():
        accepted = await asyncio.gather(*(crawler._should_add_to_queue_parallel(PRODUCT, 1, 'product') for _ in range(20)))
        assert accepted.count(True) == 1
        assert len(crawler.product_urls) == len(crawler.visited_urls) == 1
    asyncio.run(run())


def test_second_pass_enqueues_without_erasing_other_slots(crawler):
    async def run():
        existing = crawler.url_normalizer.normalize_url(BASE + 'catalog/existing/')
        crawler.product_urls.add(existing)
        crawler._quota_rejected_urls.append(PRODUCT)
        crawler._quota_rejected_depths[PRODUCT] = 3
        queue = deque()
        await crawler._second_pass_for_quota_rejected('Тест', {}, [], queue)
        assert list(queue) == [(PRODUCT, 3, 'product', 1)]
        assert existing in crawler.product_urls
        assert crawler.url_normalizer.normalize_url(PRODUCT) in crawler.product_urls
    asyncio.run(run())


def test_reset_clears_retry_bookkeeping_for_new_company(crawler):
    crawler._page_retry_attempts[CAT] = 2
    crawler._queued_retries.add(CAT)
    crawler._page_retry_candidates[CAT] = (CAT, 1, 'category', 2)
    crawler._quota_rejected_depths[PRODUCT] = 4
    asyncio.run(crawler.reset_state('Новая компания'))
    assert not crawler._page_retry_attempts and not crawler._queued_retries
    assert not crawler._page_retry_candidates and not crawler._quota_rejected_depths


def test_retry_disabled_means_one_fetch(crawler):
    async def run():
        crawler.page_retry_max_attempts = 0
        crawler._fetch_page_content = AsyncMock(return_value=None)
        await crawler._start_crawling_with_sitemap_support(BASE, 'Тест', {}, [])
        assert crawler._fetch_page_content.await_count == 1
    asyncio.run(run())


def test_retry_cannot_steal_slot_used_by_another_product(crawler):
    async def run():
        crawler.max_product_pages_per_site = 1
        crawler.product_urls.add(crawler.url_normalizer.normalize_url(BASE + 'catalog/other/'))
        crawler._register_page_retry(PRODUCT, 1, 'product')
        queue = deque()
        await crawler._enqueue_page_retries(queue)
        assert not queue
        assert crawler.product_urls == {crawler.url_normalizer.normalize_url(BASE + 'catalog/other/')}
    asyncio.run(run())
