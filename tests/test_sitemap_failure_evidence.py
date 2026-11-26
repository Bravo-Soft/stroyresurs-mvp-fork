"""Сторож карты учитывает доказанные отсутствующие страницы и их происхождение."""
import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config import Config
from fetch_outcome import observe_fetch
from web_crawler import PageContent, WebCrawler

BASE = 'https://factory.test/'
MISSING = '<html><title>404 Not Found</title><h1>Страница не найдена</h1></html>'


@pytest.fixture
def crawler(tmp_path):
    config = Config()
    config.base_dir = str(tmp_path)
    crawler = WebCrawler(config)
    crawler.current_base_url = BASE
    crawler.stale_sitemap_dup_threshold = 2
    crawler.stale_sitemap_hard_fail_threshold = 2
    crawler._sitemap_soft404_seen = {}
    crawler._sitemap_hard_fail_seen = set()
    return crawler


def published(crawler, name):
    url = BASE + name
    crawler._sitemap_published_urls[crawler.url_normalizer.normalize_url(url)] = url
    return url


def outcome(reason, retryable=False, evidence=''):
    return SimpleNamespace(reason=reason, retryable=retryable, evidence=evidence)


def test_hard_guard_requires_provenance_and_confirmed_status(crawler):
    async def run():
        for reason in ('timeout', 'challenge', 'http_error', 'no_content'):
            url = published(crawler, reason)
            await crawler._check_hard_fail_sitemap_guard(
                url, 0, 'product', deque(), outcome=outcome(reason, True))
        for index in range(3):
            await crawler._check_hard_fail_sitemap_guard(
                BASE + str(index), 0, 'product', deque(), outcome=outcome('not_found'))
        assert not crawler._stale_sitemap_guard_tripped
        assert crawler._sitemap_hard_fail_streak == 0
    asyncio.run(run())


def test_hard_guard_counts_unique_sitemap_urls_at_any_depth(crawler):
    async def run():
        first = published(crawler, 'first')
        second = published(crawler, 'second')
        for _ in range(3):
            await crawler._check_hard_fail_sitemap_guard(
                first, 2, 'product', deque(), outcome=outcome('not_found'))
        assert not crawler._stale_sitemap_guard_tripped
        await crawler._check_hard_fail_sitemap_guard(
            second, 1, 'product', deque(), outcome=outcome('gone'))
        assert crawler._stale_sitemap_guard_tripped
        assert crawler.stats['sitemap_guard_reason']
    asyncio.run(run())


@pytest.mark.parametrize('failure', [None, outcome('not_found', True), outcome('http_error')])
def test_hard_guard_does_not_infer_permanent_failure(crawler, failure):
    async def run():
        for index in range(3):
            await crawler._check_hard_fail_sitemap_guard(
                published(crawler, str(index)), 0, 'product', deque(), outcome=failure)
        assert not crawler._stale_sitemap_guard_tripped
    asyncio.run(run())


def test_transient_failure_breaks_missing_page_streak(crawler):
    async def run():
        for name, reason, retryable in [('one', 'not_found', False),
                                         ('slow', 'timeout', True),
                                         ('two', 'gone', False)]:
            await crawler._check_hard_fail_sitemap_guard(
                published(crawler, name), 0, 'product', deque(),
                outcome=outcome(reason, retryable))
        assert not crawler._stale_sitemap_guard_tripped
        assert crawler._sitemap_hard_fail_streak == 1
    asyncio.run(run())


@pytest.mark.parametrize('html', [
    '<html><title>Насос</title><h1>Насос</h1></html>',
    '<html><title>404 Насос</title><h1>404 Насос</h1></html>',
    '<html><title>503 Service Unavailable</title><h1>Service Unavailable</h1></html>',
    MISSING.replace('</html>', '<script type="application/ld+json">'
                    '{"@context":"https://schema.org","@type":"Product",'
                    '"name":"Насос"}</script></html>'),
    MISSING.replace('</html>', '<div class="product-card">Цена Купить Характеристики</div></html>'),
])
def test_soft_guard_does_not_use_similar_valid_or_transient_pages(crawler, html):
    async def run():
        for index in range(3):
            await crawler._check_stale_sitemap_guard(
                html, published(crawler, str(index)), deque())
        assert not crawler._stale_sitemap_guard_tripped
    asyncio.run(run())


def test_soft_guard_ignores_non_sitemap_and_retries(crawler):
    async def run():
        for index in range(3):
            await crawler._check_stale_sitemap_guard(MISSING, BASE + str(index), deque())
        url = published(crawler, 'first')
        for _ in range(3):
            await crawler._check_stale_sitemap_guard(
                MISSING, url, deque(), outcome=outcome('soft_not_found', evidence=MISSING))
        assert not crawler._stale_sitemap_guard_tripped
        await crawler._check_stale_sitemap_guard(
            MISSING, published(crawler, 'second'), deque(),
            outcome=outcome('soft_not_found', evidence=MISSING))
        assert crawler._stale_sitemap_guard_tripped
    asyncio.run(run())


def test_purge_keeps_non_sitemap_products_and_nonproduct_reserve(crawler):
    sitemap = published(crawler, 'sitemap-product')
    ordinary = BASE + 'ordinary-product'
    pending_product = published(crawler, 'pending-product')
    pending_category = published(crawler, 'pending-category')
    pending_rows = [(pending_product, pending_product, 'product', 9),
                    (pending_category, pending_category, 'category', 7)]
    crawler._sitemap_pending = deque(pending_rows)
    queue = deque([(sitemap, 2, 'product', 9), (ordinary, 0, 'product', 9)])
    for url in (sitemap, ordinary):
        norm = crawler.url_normalizer.normalize_url(url)
        crawler.product_urls.add(norm)
        crawler.visited_urls.add(norm)
        crawler._sitemap_slots.add(norm)
    asyncio.run(crawler._purge_sitemap_tail(queue, 'доказанные отсутствующие страницы'))
    assert list(queue) == [(ordinary, 0, 'product', 9)]
    assert list(crawler._sitemap_pending) == [pending_rows[1]]
    assert crawler.url_normalizer.normalize_url(ordinary) in crawler.product_urls
    assert crawler.url_normalizer.normalize_url(sitemap) not in crawler.product_urls
    assert crawler.stats['sitemap_guard_purged'] == 1
    assert crawler.stats['sitemap_guard_pending_purged'] == 1


def test_soft404_processing_never_saves_and_purges_only_after_distinct_evidence(crawler):
    async def run():
        first = published(crawler, 'first')
        second = published(crawler, 'second')
        tail = published(crawler, 'tail')
        queue = deque([(tail, 0, 'product', 9)])
        crawler.temp_storage.save_cleaned_html = AsyncMock()
        crawler._process_links_from_parse_result = AsyncMock()
        async def fetch(url, category):
            return await crawler._parse_content(PageContent(MISSING, url, url), url)
        crawler._fetch_page_content = fetch
        for url in (first, first):
            result = await crawler._process_page_with_storage(
                url, 0, 'product', 'Завод', {}, queue)
            assert result is None
            assert not crawler._stale_sitemap_guard_tripped
            assert queue
        result = await crawler._process_page_with_storage(
            second, 0, 'product', 'Завод', {}, queue)
        assert result is None
        assert crawler._stale_sitemap_guard_tripped
        assert not queue
        assert not crawler._page_retry_candidates
        assert crawler._fetch_failures[first]['reason'] == 'soft_not_found'
        crawler.temp_storage.save_cleaned_html.assert_not_awaited()
        crawler._process_links_from_parse_result.assert_not_awaited()
    asyncio.run(run())


def test_processing_mixed_404_and_timeout_does_not_purge_sitemap(crawler):
    async def run():
        tail = published(crawler, 'tail')
        queue = deque([(tail, 0, 'product', 9)])
        crawler.temp_storage.save_cleaned_html = AsyncMock()
        async def fetch(url, category):
            observe_fetch(url, 'aiohttp', status=404)
            observe_fetch(url, 'playwright', reason='timeout')
            return None
        crawler._fetch_page_content = fetch
        for index in range(3):
            url = published(crawler, f'item-{index}')
            result = await crawler._process_page_with_storage(
                url, 0, 'product', 'Завод', {}, queue)
            assert result is None
            assert crawler._fetch_failures[url]['reason'] == 'timeout'
            assert crawler._fetch_failures[url]['retryable']
        assert not crawler._stale_sitemap_guard_tripped
        assert list(queue) == [(tail, 0, 'product', 9)]
        assert len(crawler._page_retry_candidates) == 3
        crawler.temp_storage.save_cleaned_html.assert_not_awaited()
    asyncio.run(run())
