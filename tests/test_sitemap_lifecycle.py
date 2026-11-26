"""Состояния карт: отдельный проход, общие подкарты, повтор отказа и циклы."""
import asyncio
from collections import Counter

import pytest

from config import Config
from web_crawler import WebCrawler

BASE = 'https://a.ru/'


def leaf(name):
    return '<urlset><url><loc>' + BASE + 'catalog/' + name + '</loc></url></urlset>'


def index(*names):
    return '<sitemapindex>' + ''.join('<sitemap><loc>' + BASE + n + '</loc></sitemap>' for n in names) + '</sitemapindex>'


@pytest.fixture
def parser(tmp_path):
    cfg = Config()
    cfg.base_dir = str(tmp_path)
    return WebCrawler(cfg).sitemap_parser


def fake_transport(parser, documents):
    calls = Counter()
    async def request(url, method='GET'):
        calls[url] += 1
        await asyncio.sleep(0)
        value = documents[url]
        if isinstance(value, Exception):
            raise value
        return 200, url, {'content-type': 'application/xml'}, value.encode()
    parser._request = request
    return calls


def test_shared_submap_reuses_success_at_different_depths(parser):
    async def run():
        calls = fake_transport(parser, {BASE + 'root': index('a', 'shared'), BASE + 'a': index('shared'), BASE + 'shared': leaf('pump-1')})
        rows = await parser.parse_sitemap(BASE + 'root', max_depth=5)
        assert {row[0] for row in rows} == {BASE + 'catalog/pump-1'}
        assert calls[BASE + 'shared'] == 1
        assert await parser.parse_sitemap(BASE + 'shared')
        assert calls[BASE + 'shared'] == 1
        await parser.reset_run()
        assert await parser.parse_sitemap(BASE + 'shared')
        assert calls[BASE + 'shared'] == 2
    asyncio.run(run())


def test_failed_child_retried_through_cached_index(parser):
    async def run():
        docs = {BASE + 'root': index('good', 'bad'), BASE + 'good': leaf('pump-1'), BASE + 'bad': ValueError('temporary')}
        calls = fake_transport(parser, docs)
        assert len(await parser.parse_sitemap(BASE + 'root')) == 1
        docs[BASE + 'bad'] = leaf('pump-2')
        assert len(await parser.parse_sitemap(BASE + 'root')) == 2
        assert calls[BASE + 'root'] == calls[BASE + 'good'] == 1
        assert calls[BASE + 'bad'] == 2
    asyncio.run(run())


def test_concurrent_roots_cycles_and_waiters_finish(parser):
    async def run():
        calls = fake_transport(parser, {BASE + 'a': index('b', 'shared'), BASE + 'b': index('a', 'shared'), BASE + 'shared': leaf('pump-1')})
        results = await asyncio.wait_for(asyncio.gather(parser.parse_sitemap(BASE + 'a'), parser.parse_sitemap(BASE + 'b'), parser.parse_sitemap(BASE + 'a')), 1)
        assert all({row[0] for row in rows} == {BASE + 'catalog/pump-1'} for rows in results)
        assert calls == {BASE + 'a': 1, BASE + 'b': 1, BASE + 'shared': 1}
        assert not parser._loading and not parser._document_tasks
    asyncio.run(run())


def test_successful_empty_map_cached_and_invalid_map_retried(parser):
    async def run():
        docs = {BASE + 'empty': '<urlset/>', BASE + 'invalid': '<urlset>'}
        calls = fake_transport(parser, docs)
        assert await parser.parse_sitemap(BASE + 'empty') == []
        assert await parser.parse_sitemap(BASE + 'empty') == []
        assert calls[BASE + 'empty'] == 1
        assert await parser.parse_sitemap(BASE + 'invalid') == []
        docs[BASE + 'invalid'] = leaf('pump-2')
        assert await parser.parse_sitemap(BASE + 'invalid')
        assert calls[BASE + 'invalid'] == 2
    asyncio.run(run())


def test_depth_limit_does_not_poison_later_deeper_traversal(parser):
    async def run():
        calls = fake_transport(parser, {BASE + 'root': index('child'), BASE + 'child': leaf('pump-1')})
        assert await parser.parse_sitemap(BASE + 'root', max_depth=0) == []
        assert await parser.parse_sitemap(BASE + 'root', max_depth=1)
        assert calls == {BASE + 'root': 1, BASE + 'child': 1}
    asyncio.run(run())


def test_long_xml_comment_and_html_text_prefix_preserve_detection(parser):
    async def run():
        docs = {BASE + 'xml': '<?xml version="1.0"?>' + '<!--' + 'x' * 10000 + '-->' + leaf('pump-1'),
                BASE + 'html': 'Карта сайта\n' * 1000 + '<a href="https://a.ru/catalog/pump-2">Насос</a>'}
        fake_transport(parser, docs)
        assert [row[0] for row in await parser.parse_sitemap(BASE + 'xml')] == [BASE + 'catalog/pump-1']
        assert [row[0] for row in await parser.parse_sitemap(BASE + 'html')] == [BASE + 'catalog/pump-2']
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['xml', 'html', 'home'])
def test_phase_deadline_interrupts_large_document_parsing(parser, kind):
    async def run():
        import time
        parser.config.sitemap_phase_timeout_seconds = 0.001
        parser.sitemap_paths = []
        if kind == 'xml':
            body = '<urlset><!--' + 'x' * (4 * 1024 * 1024) + '--></urlset>'
        else:
            body = '<html>' + '<p>Материалы</p>' * 100000 + '</html>'
        async def request(url, method='GET'):
            if url.endswith('robots.txt'):
                robots = '' if kind == 'home' else 'Sitemap: ' + BASE + 'map'
                return 200, url, {}, robots.encode()
            return 200, url, {'content-type': 'text/html' if kind != 'xml' else 'application/xml'}, body.encode()
        parser._request = request
        started = time.monotonic()
        assert await parser.collect_sitemap_urls(BASE) == []
        assert time.monotonic() - started < 0.5
        assert not parser._loading and not parser._document_tasks
    asyncio.run(run())


def test_rejected_html_tree_does_not_poison_retry(parser):
    async def run():
        url = BASE + 'html'
        docs = {url: '<html><title>404 Not Found</title></html>'}
        calls = fake_transport(parser, docs)
        assert await parser.parse_sitemap(url) == []
        docs[url] = '<html><a href="https://a.ru/catalog/pump-1">Насос</a></html>'
        assert [row[0] for row in await parser.parse_sitemap(url)] == [BASE + 'catalog/pump-1']
        assert calls[url] == 2
    asyncio.run(run())


def test_cycle_cut_does_not_cache_context_dependent_empty_index(parser):
    async def run():
        calls = fake_transport(parser, {BASE + 'a': index('b', 'c'), BASE + 'b': index('a'), BASE + 'c': leaf('pump-1')})
        assert await parser.parse_sitemap(BASE + 'a', max_depth=3)
        assert await parser.parse_sitemap(BASE + 'b', max_depth=2)
        assert calls == {BASE + 'a': 1, BASE + 'b': 1, BASE + 'c': 1}
    asyncio.run(run())
