"""Счётчики попыток, уникальных успехов и честный статус завершения обхода."""
import asyncio
import time
from collections import deque
from unittest.mock import AsyncMock

import pytest
from bs4 import BeautifulSoup

import web_crawler as wc
from config import Config

BASE = 'https://factory.test/'
URL = BASE + 'catalog/pump/'
HTML = '<html><body><h1>Насос</h1>' + '<p>Мощность 20 кВт.</p>' * 100 + '</body></html>'


@pytest.fixture
def crawler(tmp_path):
    cfg = Config()
    cfg.base_dir = str(tmp_path)
    c = wc.WebCrawler(cfg)
    c.current_base_url = BASE
    c._check_memory_usage = AsyncMock()
    c._process_links_from_parse_result = AsyncMock()
    c.temp_storage.save_cleaned_html = AsyncMock(return_value=(str(tmp_path / 'same.html'), False, {}))
    return c


def success():
    page = wc.ParseResult(BeautifulSoup(HTML, 'html.parser'), HTML, [], 'main_page', {}, url=URL)
    return wc.FetchOutcome(URL, URL, 'aiohttp', 200, 'ok', False, content=page)


async def process(crawler, outcome):
    crawler.fetch_page = AsyncMock(return_value=outcome)
    return await crawler._process_page_with_storage(URL, 0, 'main_page', 'Завод', {}, deque(), [])


def test_unchanged_success_counts_as_fetched_and_complete(crawler):
    assert asyncio.run(process(crawler, success())) is None
    crawler._finalize_crawl_status()
    assert crawler.stats['attempted_pages'] == crawler.stats['fetched_pages'] == 1
    assert crawler.stats['successful_urls'] == 1
    assert crawler.stats['failed_attempts'] == crawler.stats['failed_pages'] == 0
    assert crawler.stats['crawl_status'] == 'complete'
    assert crawler.stats['stop_reasons'] == []


def test_successful_retry_clears_final_failure_but_keeps_failed_attempt(crawler):
    async def run():
        await process(crawler, wc.FetchOutcome(URL, reason='timeout'))
        await process(crawler, success())
    asyncio.run(run())
    crawler._finalize_crawl_status()
    assert crawler.stats['attempted_pages'] == 2 and crawler.stats['fetched_pages'] == 1
    assert crawler.stats['successful_urls'] == 1 and crawler.stats['failed_attempts'] == 1
    assert crawler.stats['failed_pages'] == 0
    assert crawler.stats['crawl_status'] == 'complete'


def test_multiple_successes_for_same_url_are_not_unique_urls(crawler):
    async def run():
        await process(crawler, success())
        await process(crawler, success())
    asyncio.run(run())
    crawler._finalize_crawl_status()
    assert crawler.stats['attempted_pages'] == crawler.stats['fetched_pages'] == 2
    assert crawler.stats['successful_urls'] == 1


def test_resume_content_is_counted_separately_and_can_complete(crawler):
    norm = crawler.url_normalizer.normalize_url(URL)
    crawler._resume_pages[norm] = {'html_path': 'saved.html'}
    crawler.temp_storage.load_cleaned_html = AsyncMock(return_value={
        'metadata': {'url': URL}, 'html_content': HTML})
    crawler._parse_content = AsyncMock(return_value=success().content)
    crawler.fetch_page = AsyncMock(side_effect=AssertionError('resume should not fetch'))
    asyncio.run(crawler._process_page_with_storage(URL, 0, 'main_page', 'Завод', {}, deque(), []))
    crawler._finalize_crawl_status()
    crawler.fetch_page.assert_not_awaited()
    assert crawler.stats['resumed_pages'] == 1 and crawler.stats['fetched_pages'] == 0
    assert crawler.stats['crawl_status'] == 'complete'


def test_all404_without_exception_is_failed_not_complete(crawler):
    asyncio.run(process(crawler, wc.FetchOutcome(URL, status=404, reason='not_found', retryable=False)))
    crawler._finalize_crawl_status()
    assert crawler.stats['crawl_status'] == 'failed'
    assert crawler.stats['failed_pages'] == crawler.stats['failed_attempts'] == 1
    assert 'no_content' in crawler.stats['stop_reasons']
    assert 'fetch_failures' in crawler.stats['stop_reasons']


def test_repeated_failures_are_one_unresolved_url(crawler):
    async def run():
        await process(crawler, wc.FetchOutcome(URL, reason='timeout'))
        await process(crawler, wc.FetchOutcome(URL, reason='timeout'))
    asyncio.run(run())
    crawler._finalize_crawl_status()
    assert crawler.stats['failed_attempts'] == 2 and crawler.stats['failed_pages'] == 1


def mark_success(crawler):
    asyncio.run(process(crawler, success()))


def test_page_limit_with_known_work_left_is_partial(crawler):
    mark_success(crawler)
    crawler.max_pages_per_site = crawler.stats['total_pages']
    crawler._finalize_crawl_status(deque([(BASE + 'other/', 1, 'category', 1)]))
    assert crawler.stats['crawl_status'] == 'partial'
    assert 'page_limit' in crawler.stats['stop_reasons']


def test_exact_page_limit_without_work_left_does_not_invent_truncation(crawler):
    mark_success(crawler)
    crawler.max_pages_per_site = crawler.stats['total_pages']
    crawler._finalize_crawl_status()
    assert crawler.stats['crawl_status'] == 'complete'
    assert 'page_limit' not in crawler.stats['stop_reasons']


def test_sitemap_reserve_left_is_partial(crawler):
    mark_success(crawler)
    crawler._sitemap_pending.append((BASE + 'catalog/other/', BASE + 'catalog/other/', 'product', 9))
    crawler._finalize_crawl_status()
    assert crawler.stats['crawl_status'] == 'partial'
    assert 'sitemap_limit' in crawler.stats['stop_reasons']


@pytest.mark.parametrize('timed_out', [False, True])
def test_sitemap_phase_timeout_affects_completion_even_when_queue_empty(crawler, timed_out):
    mark_success(crawler)
    crawler.sitemap_parser.phase_timed_out = timed_out
    crawler._finalize_crawl_status()
    assert crawler.stats['crawl_status'] == ('partial' if timed_out else 'complete')
    assert ('sitemap_incomplete' in crawler.stats['stop_reasons']) == timed_out


def test_pending_retry_is_partial_after_success(crawler):
    mark_success(crawler)
    other = BASE + 'other/'
    crawler._page_retry_candidates[other] = (other, 1, 'category', 1)
    crawler._finalize_crawl_status()
    assert crawler.stats['crawl_status'] == 'partial'
    assert 'pending_retries' in crawler.stats['stop_reasons']


def test_depth_limit_rejection_is_visible_in_completion(crawler):
    mark_success(crawler)
    crawler.max_depth = 0
    assert not asyncio.run(crawler._should_add_to_queue_parallel(BASE + 'deep/', 1, 'category'))
    crawler._finalize_crawl_status()
    assert crawler.stats['crawl_status'] == 'partial'
    assert 'depth_limit' in crawler.stats['stop_reasons']


def test_product_limit_rejection_is_visible_in_completion(crawler):
    mark_success(crawler)
    crawler.max_product_pages_per_site = 1
    crawler.product_urls.add(crawler.url_normalizer.normalize_url(URL))
    assert not asyncio.run(crawler._should_add_to_queue_parallel(BASE + 'catalog/other/', 1, 'product'))
    crawler._finalize_crawl_status()
    assert crawler.stats['crawl_status'] == 'partial'
    assert 'product_limit' in crawler.stats['stop_reasons']


def test_paused_host_with_queued_work_is_partial(crawler):
    mark_success(crawler)
    crawler._host_retry_until['factory.test'] = time.monotonic() + 100
    crawler._finalize_crawl_status(deque([(BASE + 'other/', 1, 'category', 1)]))
    assert crawler.stats['crawl_status'] == 'partial'
    assert 'host_cooldown' in crawler.stats['stop_reasons']


def test_sitemap_guard_is_reported_even_after_success(crawler):
    mark_success(crawler)
    crawler._stale_sitemap_guard_tripped = True
    crawler._finalize_crawl_status()
    assert crawler.stats['crawl_status'] == 'partial'
    assert 'sitemap_guard' in crawler.stats['stop_reasons']


def test_empty_run_has_failed_status_and_primary_reason(crawler):
    crawler._finalize_crawl_status()
    assert crawler.stats['crawl_status'] == 'failed'
    assert 'no_content' in crawler.stats['stop_reasons']
    assert crawler.stats['stop_reason'] in crawler.stats['stop_reasons']


def test_short_unusable_response_does_not_make_crawl_complete(crawler):
    html = '<html><body>ok</body></html>'
    parsed = wc.ParseResult(BeautifulSoup(html, 'html.parser'), html, [], 'main_page', {}, url=URL)
    asyncio.run(process(crawler, wc.FetchOutcome(URL, status=200, reason='ok', retryable=False, content=parsed)))
    crawler._finalize_crawl_status()
    assert crawler.stats['fetched_pages'] == 1
    assert crawler.stats['crawl_status'] == 'failed'
    assert 'no_content' in crawler.stats['stop_reasons']
    crawler.temp_storage.save_cleaned_html.assert_not_awaited()


def test_terminal_failure_reason_survives_repeat_finalization(crawler):
    mark_success(crawler)
    crawler._finalize_crawl_status(reason='internal_error')
    crawler._finalize_crawl_status()
    assert crawler.stats['crawl_status'] == 'partial'
    assert 'internal_error' in crawler.stats['stop_reasons']


def test_crawl_site_exception_also_has_completion_status(crawler, tmp_path):
    crawler.site_status = None
    crawler._get_working_url = AsyncMock(return_value=BASE)
    crawler._start_crawling_with_sitemap_support = AsyncMock(side_effect=RuntimeError('fixture failure'))
    crawler.create_site_directories = lambda *args: {}
    result = asyncio.run(crawler.crawl_site(BASE, 'Завод', str(tmp_path)))
    assert result['crawl_status'] == 'failed'
    assert result['stop_reason'] in result['stop_reasons']
    assert result.get('error')


def test_new_company_resets_completion_counts_and_unique_sets(crawler):
    mark_success(crawler)
    crawler._finalize_crawl_status()
    asyncio.run(crawler.reset_state('Другая компания'))
    for key in ('attempted_pages', 'fetched_pages', 'successful_urls', 'failed_attempts', 'failed_pages'):
        assert crawler.stats[key] == 0


def test_attempted_quota_reject_is_failure_not_unvisited_product(crawler):
    mark_success(crawler)
    other = BASE + 'catalog/other/'
    crawler._quota_rejected_depths[other] = 1
    crawler.fetch_page = AsyncMock(return_value=wc.FetchOutcome(other, status=404, reason='not_found', retryable=False))
    asyncio.run(crawler._process_page_with_storage(other, 1, 'product', 'Завод', {}, deque(), []))
    crawler._finalize_crawl_status()
    assert crawler.stats['failed_pages'] == 1
    assert crawler.stats['remaining_product_urls'] == 0
    assert 'product_limit' not in crawler.stats['stop_reasons']
    assert 'fetch_failures' in crawler.stats['stop_reasons']


def test_cancelled_queue_wait_keeps_terminal_reason(crawler, monkeypatch):
    mark_success(crawler)
    crawler._register_page_retry(BASE + 'retry/', 1, 'category', wc.FetchOutcome(BASE + 'retry/', reason='timeout'))
    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(wc.asyncio, 'sleep', cancel)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(crawler._drain_crawl_queue(deque(), BASE, 'Завод', {}, []))
    crawler._finalize_crawl_status()
    assert crawler.stats['crawl_status'] == 'partial'
    assert 'cancelled' in crawler.stats['stop_reasons']
