"""Единый периметр компании: только профильные исключения и один источник каталога."""
import asyncio
from collections import deque
from unittest.mock import AsyncMock

import pytest
from bs4 import BeautifulSoup

from config import Config
from domain_equivalency import DomainEquivalencyManager, HttpAnswer, url_in_scope
from site_profiles import SiteProfile
from web_crawler import ParseResult, WebCrawler


@pytest.fixture
def crawler(tmp_path):
    cfg = Config()
    cfg.base_dir = str(tmp_path)
    c = WebCrawler(cfg)
    c.current_base_url = 'https://company.test/'
    return c


def profile(c, domain='company.test', mirrors=(), files=(), suffixes=()):
    c.profile = SiteProfile(domain=domain)
    c.profile.crawl.equivalent_domains = list(mirrors)
    c.profile.crawl.file_hosts = list(files)
    c.profile.crawl.subdomain_collapse = list(suffixes)


@pytest.mark.parametrize('url,allowed', [
    ('https://company.test/a', True), ('http://www.company.test/a', True),
    ('https://COMPANY.TEST./a', True), ('https://company.test:8443/a', True),
    ('https://shop.company.test/a', False), ('https://m.company.test/a', False),
    ('https://mobile.company.test/a', False), ('https://touch.company.test/a', False),
    ('https://company.test.evil.test/a', False), ('https://evilcompany.test/a', False),
    ('https://company.test@evil.test/a', False), ('https://u:p@company.test/a', False),
    ('ftp://company.test/a', False), ('file:///company.test/a', False),
    ('javascript:alert(1)', False), ('https://company.test:bad/a', False),
    ('https://[broken/a', False), ('', False), ('//company.test/a', False),
])
@pytest.mark.parametrize('resource', ['page', 'sitemap', 'file'])
def test_same_policy_for_all_resource_types(crawler, url, allowed, resource):
    assert crawler._is_allowed_url(url, resource=resource) is allowed


@pytest.mark.parametrize('enabled', [True, False])
def test_profile_mirror_is_allowed_without_global_equivalency(crawler, enabled):
    crawler.config.domain_equivalency_enabled = enabled
    profile(crawler, mirrors=['mirror.test'])
    for resource in ('page', 'sitemap', 'file'):
        assert crawler._is_allowed_url('https://mirror.test/path', resource=resource)
    assert crawler._is_allowed_final_url('https://company.test/a', 'https://mirror.test/a')


def test_file_permission_is_not_page_or_sitemap_permission(crawler):
    profile(crawler, files=['cdn.test'])
    assert crawler._is_allowed_url('https://cdn.test/catalog.pdf', resource='file')
    assert not crawler._is_allowed_url('https://cdn.test/catalog.pdf', resource='page')
    assert not crawler._is_allowed_url('https://cdn.test/map.xml', resource='sitemap')
    assert not crawler._is_allowed_url('https://child.cdn.test/a.pdf', resource='file')
    assert not crawler._is_allowed_url('https://www.cdn.test/a.pdf', resource='file')


def test_profile_for_another_company_cannot_authorize(crawler):
    profile(crawler, domain='other.test', mirrors=['mirror.test'], files=['cdn.test'])
    assert not crawler._is_allowed_url('https://mirror.test/a')
    assert not crawler._is_allowed_url('https://cdn.test/a.pdf', resource='file')


def test_profile_explicit_wildcard_stays_bounded(crawler):
    profile(crawler, suffixes=['*.company.test'])
    assert crawler._is_allowed_url('https://catalog.company.test/a')
    assert not crawler._is_allowed_url('https://company.test.evil.test/a')


def test_current_company_wins_over_caller_base(crawler):
    profile(crawler, mirrors=['mirror.test'])
    assert not crawler._is_allowed_url('https://evil.test/a', base_url='https://evil.test/')
    assert not crawler._is_allowed_final_url('https://evil.test/a', 'https://evil.test/b')
    assert not crawler._in_crawl_perimeter('https://evil.test/a', 'https://evil.test/')
    crawler.config.domain_equivalency_enabled = False
    assert crawler._eligible_sitemap_row(('https://evil.test/a', 'x', 'product', 10), 'https://evil.test/') is None


def test_global_groups_and_dynamic_allowlist_do_not_authorize(crawler):
    crawler.config.equivalent_domains = {'company.test': ['other.test']}
    crawler._allowed_hosts.add('shop.company.test')
    assert not crawler._is_allowed_url('https://other.test/a')
    assert not crawler._is_allowed_url('https://shop.company.test/a')


@pytest.mark.parametrize('base,own,foreign', [
    ('https://крышев.рф/', 'https://xn--b1afoy4br.xn--p1ai/a', 'https://крышев.рф.evil.test/a'),
    ('http://[::1]:8000/', 'http://[::1]:8000/a', 'http://[::2]:8000/a'),
])
def test_idn_and_ipv6(crawler, base, own, foreign):
    crawler.current_base_url = base
    assert crawler._is_allowed_url(own)
    assert not crawler._is_allowed_url(foreign)


def test_reset_forgets_company_profile_and_sitemap_authority(crawler):
    profile(crawler, mirrors=['mirror.test'], files=['cdn.test'])
    crawler.sitemap_parser._scope_base = crawler.current_base_url
    asyncio.run(crawler.reset_state('Следующая компания'))
    crawler.current_base_url = 'https://next.test/'
    assert not crawler._is_allowed_url('https://mirror.test/a')
    assert not crawler._is_allowed_url('https://cdn.test/a.pdf', resource='file')
    assert not crawler.sitemap_parser._allows_url('https://mirror.test/map.xml')


RWL = 'https://rwl.ru/vse-produkty/alyuminiyevaya-kleykaya-lenta/'
SHOP = ['https://shop.rwl.ru/aljuminievaja-klejkaja-lenta-rockwool-50/',
        'https://shop.rwl.ru/aljuminievaja-klejkaja-lenta-rockwool-100/']


@pytest.mark.parametrize('start,expected', [('https://rwl.ru/', [RWL]), ('https://shop.rwl.ru/', SHOP)])
def test_rwl_manufacturer_and_shop_do_not_mix(crawler, start, expected):
    async def run():
        crawler.current_base_url = start
        crawler._set_working_base(start)
        links = [(u, 'product', 9) for u in [RWL] + SHOP]
        parsed = ParseResult(BeautifulSoup('<html/>', 'html.parser'), '<html/>', links, 'main_page', {})
        extracted = await crawler._extract_links(start, parsed, 0, base_url=start)
        assert {u for u, _, _ in extracted} == set(expected)
        queue = deque()
        rows = [(u, crawler.url_normalizer.normalize_url(u), 'product', 10) for u in [RWL] + SHOP]
        await crawler._add_sitemap_urls_to_queue(queue, rows, start)
        assert {u for u, *_ in queue} == set(expected)
        assert not crawler._allowed_hosts
    asyncio.run(run())


def test_mobile_cannot_be_rewritten_into_main_host(crawler):
    crawler._set_working_base(crawler.current_base_url)
    mobile = 'https://m.company.test/catalog/a'
    assert crawler._canonicalize_to_working_base(mobile) == mobile
    assert crawler._eligible_sitemap_row((mobile, mobile, 'product', 10), crawler.current_base_url) is None


def test_sitemap_children_are_checked_before_transport(crawler):
    async def run():
        p = crawler.sitemap_parser
        requested = []
        async def request(url, method='GET'):
            requested.append(url)
            if url.endswith('index.xml'):
                body = '<sitemapindex><sitemap><loc>../products.xml</loc></sitemap><sitemap><loc>https://evil.test/map.xml</loc></sitemap></sitemapindex>'
                return 200, 'https://company.test/moved/index.xml', {}, body.encode()
            return 200, url, {}, b'<urlset><url><loc>https://company.test/catalog/a</loc></url></urlset>'
        p._request = request
        rows = await p.parse_sitemap('https://company.test/maps/index.xml')
        assert [r[0] for r in rows] == ['https://company.test/catalog/a']
        assert requested == ['https://company.test/maps/index.xml', 'https://company.test/products.xml']
    asyncio.run(run())


def test_sitemap_rejects_foreign_final_even_with_mocked_transport(crawler):
    async def run():
        p = crawler.sitemap_parser
        p._request = AsyncMock(return_value=(200, 'https://evil.test/map.xml', {}, b'<urlset/>'))
        assert await p.parse_sitemap('https://company.test/map.xml') == []
        assert not p._results
        assert not p._completed
    asyncio.run(run())


def test_working_mirror_is_accepted_with_absolute_internal_links(crawler):
    async def run():
        manager = crawler.url_categorizer.domain_equivalency
        manager.set_profile_domains('company.test', ['mirror.test'])
        body = '<html><a href="https://mirror.test/catalog/">Каталог</a>' + 'Товары ' * 100 + '</html>'
        manager._probe_http = AsyncMock(return_value=HttpAnswer(200, body, 'https://mirror.test/', None, False))
        probe = await manager._probe_candidate('https://company.test/', True)
        assert probe.ok and probe.url == 'https://mirror.test/'
    asyncio.run(run())


def test_working_mirror_keeps_original_company_profile(crawler, monkeypatch):
    async def run():
        first = SiteProfile(domain='company.test')
        first.crawl.equivalent_domains = ['mirror.test']
        first.crawl.file_hosts = ['cdn.test']
        second = SiteProfile(domain='mirror.test')
        second.crawl.equivalent_domains = ['third.test']
        class Resolver:
            def resolve(self, url):
                return first if 'company.test' in url else second
        monkeypatch.setattr('web_crawler._get_profile_resolver', lambda: Resolver())
        crawler.site_status = None
        crawler._get_working_url = AsyncMock(return_value='https://mirror.test/')
        crawler._start_crawling_with_sitemap_support = AsyncMock()
        crawler._save_crawling_stats = AsyncMock()
        crawler._save_permanent_errors_cache = lambda: None
        await crawler.crawl_site('https://company.test/', 'Компания', crawler.config.base_dir)
        assert crawler.profile is first
        assert crawler._is_allowed_url('https://cdn.test/a.pdf', resource='file')
        assert not crawler._is_allowed_url('https://third.test/a')
    asyncio.run(run())


@pytest.mark.parametrize('ignore', [False, True])
def test_profile_scope_is_independent_of_old_subdomain_flag(crawler, ignore):
    async def run():
        crawler.config.ignore_subdomains = ignore
        profile(crawler, mirrors=['mirror.test'])
        links = [('https://mirror.test/catalog/a', 'product', 9),
                 ('https://shop.company.test/catalog/a', 'product', 9)]
        parsed = ParseResult(BeautifulSoup('<html/>', 'html.parser'), '<html/>', links, 'main_page', {})
        assert await crawler._extract_links(crawler.current_base_url, parsed, 0) == [links[0]]
        assert await crawler._should_add_to_queue_parallel(links[0][0], 1)
        assert not await crawler._should_add_to_queue_parallel(links[1][0], 1)
        assert await crawler._should_skip_url(links[1][0], 1, 'Компания')
    asyncio.run(run())
