"""Независимые регрессии порядка источников и общего дедлайна."""
import asyncio

from config import Config
from web_crawler import WebCrawler


def test_completed_sources_keep_source_rank_order(tmp_path):
    async def run():
        config = Config()
        config.base_dir = str(tmp_path)
        parser = WebCrawler(config).sitemap_parser
        base = 'https://a.ru'
        sources = [base + '/catalog.xml', base + '/news.xml']
        async def discover(_base):
            return sources
        async def request(url, method='GET'):
            if url == sources[0]:
                await asyncio.sleep(0.02)
            slug = 'catalog-product' if url == sources[0] else 'news-product'
            body = f'<urlset><url><loc>{base}/catalog/{slug}/</loc></url></urlset>'
            return 200, url, {}, body.encode()
        parser.discover_sitemap_urls = discover
        parser._request = request
        results = await parser.collect_sitemap_urls(base)
        assert [rows[0][0] for rows in results] == [
            base + '/catalog/catalog-product/', base + '/catalog/news-product/']
    asyncio.run(run())


def test_slow_robots_map_does_not_block_fast_standard_map(tmp_path):
    async def run():
        config = Config()
        config.base_dir = str(tmp_path)
        config.sitemap_phase_timeout_seconds = 0.08
        parser = WebCrawler(config).sitemap_parser
        parser.sitemap_paths = ['/sitemap.xml']
        base = 'https://a.ru'
        target = base + '/catalog/pump-123/'
        async def request(url, method='GET'):
            if url == base + '/robots.txt':
                return 200, url, {}, f'Sitemap: {base}/slow.xml'.encode()
            if url == base + '/slow.xml':
                await asyncio.sleep(5)
            if url == base + '/sitemap.xml':
                return 200, url, {}, f'<urlset><url><loc>{target}</loc></url></urlset>'.encode()
            return 404, url, {}, b''
        parser._request = request
        results = await parser.collect_sitemap_urls(base)
        assert target in [row[0] for rows in results for row in rows]
        assert not parser._loading and not parser._document_tasks
    asyncio.run(run())


def test_shared_cyclic_index_does_not_cache_ancestor_pruned_result(tmp_path):
    async def run():
        config = Config()
        config.base_dir = str(tmp_path)
        parser = WebCrawler(config).sitemap_parser
        base = 'https://a.ru/'
        def index(*names):
            return '<sitemapindex>' + ''.join(
                f'<sitemap><loc>{base}{name}</loc></sitemap>' for name in names) + '</sitemapindex>'
        target = base + 'catalog/pump-123/'
        docs = {'a': index('b', 'leaf'), 'c': index('b'), 'b': index('a'),
                'leaf': f'<urlset><url><loc>{target}</loc></url></urlset>'}
        async def request(url, method='GET'):
            await asyncio.sleep(0)
            return 200, url, {}, docs[url.removeprefix(base)].encode()
        parser._request = request
        results = await asyncio.wait_for(asyncio.gather(
            parser.parse_sitemap(base + 'a', max_depth=5),
            parser.parse_sitemap(base + 'c', max_depth=5)), 1)
        assert all(target in [row[0] for row in rows] for rows in results)
    asyncio.run(run())
