"""Квота уникальных допустимых URL, порции и пополнение после отказов."""
import asyncio
from collections import deque
from site_profiles import SiteProfile

import pytest
from bs4 import BeautifulSoup

from config import Config
from web_crawler import WebCrawler, ParseResult


@pytest.fixture
def crawler(tmp_path):
    config = Config()
    config.base_dir = str(tmp_path)
    config.sitemap_max_urls = 2
    config.sitemap_initial_batch_size = 500
    crawler = WebCrawler(config)
    crawler.current_base_url = 'https://a.ru/'
    crawler.max_product_pages_per_site = 2000
    crawler.product_quota_card_reserve = 0
    return crawler


def row(n, category='product'):
    url = f'https://a.ru/catalog/p{n}'
    return url, url, category, 10


async def discover(crawler, maps):
    async def collect(*args, **kwargs):
        return maps
    crawler.sitemap_parser.collect_sitemap_urls = collect
    return await crawler._discover_and_parse_sitemap(crawler.current_base_url)


@pytest.mark.parametrize('kind', ['xml', 'html', 'text'])
def test_500_duplicates_keep_501st(crawler, kind):
    urls = [row(1)[0]] * 500 + [row(2)[0]]
    async def scenario():
        p = crawler.sitemap_parser
        if kind == 'xml':
            body = '<urlset>' + ''.join(f'<url><loc>{u}</loc></url>' for u in urls) + '</urlset>'
            rows = await p._parse_xml_sitemap(body, 'https://a.ru/sitemap.xml', 0, 3)
        elif kind == 'html':
            rows = await p._parse_html_sitemap(''.join(f'<a href="{u}">товар</a>' for u in urls), 'https://a.ru/sitemap.html')
        else:
            rows = await p._parse_text_sitemap('\n'.join(urls), 'https://a.ru/sitemap.txt')
        selected = await discover(crawler, [rows])
        q = deque()
        await crawler._add_sitemap_urls_to_queue(q, selected, crawler.current_base_url)
        assert {item[0] for item in q} == {row(1)[0], row(2)[0]}
    asyncio.run(scenario())


def test_single_map_profile_1000_keeps_600(crawler):
    crawler.profile = SiteProfile(domain='a.ru')
    crawler.profile.crawl.limits.sitemap_urls = 1000
    async def scenario():
        urls = [row(i)[0] for i in range(600)]
        body = '<urlset>' + ''.join(f'<url><loc>{u}</loc></url>' for u in urls) + '</urlset>'
        rows = await crawler.sitemap_parser._parse_xml_sitemap(body, 'https://a.ru/sitemap.xml', 0, 3)
        selected = await discover(crawler, [rows])
        assert len(selected) == 600
        q = deque()
        assert await crawler._add_sitemap_urls_to_queue(q, selected, crawler.current_base_url) == 500
        assert len(crawler._sitemap_pending) == 100
        assert await crawler._refill_sitemap_queue(q, crawler.current_base_url) == 100
        assert len(q) == len(crawler._sitemap_slots) == 600
    asyncio.run(scenario())


@pytest.mark.parametrize('bad', [
    ('https://foreign.ru/catalog/p0', 'https://foreign.ru/catalog/p0', 'product', 10),
    ('https://a.ru/en/catalog/p0', 'https://a.ru/en/catalog/p0', 'product', 10),
    ('https://a.ru/catalog/?page=999', 'https://a.ru/catalog?page=999', 'product', 10),
    ('javascript:void(0)', 'javascript:void(0)', 'product', 10),
    ('https://a.ru/private', 'https://a.ru/private', 'excluded2', 10),
    ('https://shop.a.ru/catalog/p0', 'https://shop.a.ru/catalog/p0', 'product', 10),
])
def test_filters_before_budget(crawler, bad):
    async def scenario():
        selected = await discover(crawler, [[bad, row(1), row(2)]])
        assert selected == [row(1), row(2)]
    asyncio.run(scenario())


def test_duplicate_between_maps_does_not_spend_slot(crawler):
    assert crawler._merge_sitemap_maps([[row(1)] * 500, [row(1), row(2)]], 2) == [row(1), row(2)]


def test_scheme_duplicate_does_not_spend_slot(crawler):
    http = ('http://a.ru/catalog/p1', 'http://a.ru/catalog/p1', 'product', 10)
    async def scenario():
        selected = await discover(crawler, [[http, row(1), row(2)]])
        assert len(selected) == 2
        assert row(2) in selected
    asyncio.run(scenario())


def test_already_visited_replaced_from_reserve(crawler):
    async def scenario():
        selected = await discover(crawler, [[row(1), row(2), row(3)]])
        crawler.visited_urls.add(row(1)[1])
        q = deque()
        await crawler._add_sitemap_urls_to_queue(q, selected, crawler.current_base_url)
        assert [item[0] for item in q] == [row(2)[0], row(3)[0]]
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['empty', 'short', 'exception'])
def test_failure_replenishes_without_repeating_failed(crawler, failure):
    crawler.page_retry_max_attempts = 0  # Окончательный отказ освобождает квоту.
    async def scenario():
        selected = await discover(crawler, [[row(1), row(2), row(3), row(4)]])
        q = deque()
        await crawler._add_sitemap_urls_to_queue(q, selected, crawler.current_base_url)
        url, depth, category, _ = q.popleft()
        async def fetch(*args):
            if failure == 'exception':
                raise RuntimeError('тестовый отказ')
            if failure == 'short':
                html = '<html>пусто</html>'
                return ParseResult(BeautifulSoup(html, 'html.parser'), html, [], 'product_page', {})
            return None
        async def no_links(*args):
            pass
        crawler._fetch_page_content = fetch
        crawler._process_links_from_parse_result = no_links
        await crawler._process_page_with_storage(url, depth, category, 'Тест', {}, q)
        assert await crawler._refill_sitemap_queue(q, crawler.current_base_url) == 1
        assert [item[0] for item in q] == [row(3)[0], row(2)[0]]
        assert url in crawler.visited_urls
        assert len(crawler._sitemap_slots) == 2
        assert list(crawler._sitemap_pending) == [row(4)]
    asyncio.run(scenario())


def test_product_quota_rejections_remain_available(crawler):
    crawler.max_product_pages_per_site = 1
    async def scenario():
        selected = await discover(crawler, [[row(1), row(2), row(3)]])
        q = deque()
        await crawler._add_sitemap_urls_to_queue(q, selected, crawler.current_base_url)
        assert len(q) == 1
        assert len(crawler._sitemap_pending) == 2
        await crawler._release_product_quota_slot(row(1)[1], 'тест')
        assert await crawler._refill_sitemap_queue(q, crawler.current_base_url) == 1
        assert len(q) == 2
    asyncio.run(scenario())


def test_stale_guard_blocks_reserve(crawler):
    async def scenario():
        selected = await discover(crawler, [[row(1), row(2), row(3)]])
        q = deque()
        await crawler._add_sitemap_urls_to_queue(q, selected, crawler.current_base_url)
        crawler._stale_sitemap_guard_tripped = True
        await crawler._purge_sitemap_tail(q, 'тест')
        assert await crawler._refill_sitemap_queue(q, crawler.current_base_url) == 0
        assert not q and not crawler._sitemap_pending
    asyncio.run(scenario())


def test_reset_discards_reserve_and_budget(crawler):
    async def scenario():
        selected = await discover(crawler, [[row(1), row(2), row(3)]])
        await crawler._add_sitemap_urls_to_queue(deque(), selected, crawler.current_base_url)
        await crawler.reset_state('Тест')
        assert not crawler._sitemap_pending and not crawler._sitemap_slots
    asyncio.run(scenario())


def test_real_queue_loop_refills_after_failure(crawler):
    crawler.config.sitemap_initial_batch_size = 1
    crawler.config.sitemap_processing_batch_size = 1
    crawler.max_pages_per_site = 10
    async def scenario():
        async def collect(*args, **kwargs):
            return [[row(1), row(2), row(3), row(4)]]
        crawler.sitemap_parser.collect_sitemap_urls = collect
        crawler._get_start_urls = lambda *_: []
        crawler._should_skip_url = _false
        crawler._check_memory_usage = _noop
        crawler._process_links_from_parse_result = _noop
        calls = []
        async def fetch(url, category):
            calls.append(url)
            if url == row(1)[0]:
                return None
            html = '<html><body>' + 'Насос модель P100. ' * 40 + '</body></html>'
            return ParseResult(BeautifulSoup(html, 'html.parser'), html, [], 'product_page', {})
        crawler._fetch_page_content = fetch
        await crawler._start_crawling_with_sitemap_support(
            crawler.current_base_url, 'Тест', {}, [])
        assert set(calls) == {row(1)[0], row(2)[0], row(3)[0]}
        assert calls.count(row(1)[0]) == 1 + crawler.page_retry_max_attempts  # Резерв допускается после последней попытки.
        assert row(4)[0] not in calls
        assert len(crawler._sitemap_slots) == 2
    asyncio.run(asyncio.wait_for(scenario(), 3))


async def _noop(*args, **kwargs):
    pass


async def _false(*args, **kwargs):
    return False


def test_flat_site_recategorizes_reserve(crawler):
    crawler.config.sitemap_max_urls = 1
    crawler.max_pages_per_site = 0
    async def scenario():
        async def collect(*args, **kwargs):
            return [[row(1, 'other'), row(2, 'other')]]
        crawler.sitemap_parser.collect_sitemap_urls = collect
        crawler._get_start_urls = lambda *_: []
        observed = []
        def observe(urls):
            observed.extend(urls)
            return True
        crawler.url_categorizer.observe_urls = observe
        crawler._recategorize_sitemap_row = lambda _: ('product', 10)
        await crawler._start_crawling_with_sitemap_support(crawler.current_base_url, 'Тест', {}, [])
        assert observed == [row(1)[0], row(2)[0]]
        assert list(crawler._sitemap_pending) == [row(2)]
    asyncio.run(scenario())


def test_streaming_rejection_frees_sitemap_slot(crawler):
    async def scenario():
        selected = await discover(crawler, [[row(1), row(2), row(3)]])
        q = deque()
        await crawler._add_sitemap_urls_to_queue(q, selected, crawler.current_base_url)
        assert await crawler.release_product_slot(row(1)[0])
        assert row(1)[1] not in crawler._sitemap_slots
        assert await crawler._refill_sitemap_queue(q, crawler.current_base_url) == 1
    asyncio.run(scenario())


def test_retry_reoccupies_free_slot(crawler):
    async def scenario():
        q = deque()
        await crawler._add_sitemap_urls_to_queue(q, [row(1), row(2)], crawler.current_base_url)
        async def empty(*args):
            return None
        crawler._fetch_page_content = empty
        crawler._process_links_from_parse_result = _noop
        url, depth, category, _ = q.popleft()
        await crawler._process_page_with_storage(url, depth, category, 'Тест', {}, q)
        assert len(crawler._sitemap_slots) == 2  # Слот сохранён до повтора.
        assert url in crawler._page_retry_candidates
        async def good(*args):
            html = '<html><body>' + 'Насос модель P100. ' * 40 + '</body></html>'
            return ParseResult(BeautifulSoup(html, 'html.parser'), html, [], 'product_page', {})
        crawler._fetch_page_content = good
        assert await crawler._process_page_with_storage(url, depth, category, 'Тест', {}, q)
        assert len(crawler._sitemap_slots) == 2
        assert row(1)[1] in crawler.product_urls
    asyncio.run(scenario())
