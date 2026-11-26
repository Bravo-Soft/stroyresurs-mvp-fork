"""Ограничения настоящего streaming-reader без сокетов (подмена только транспорта)."""
import asyncio
import gzip
import random
from unittest.mock import patch

import pytest
from multidict import CIMultiDict
from config import Config
from web_crawler import WebCrawler

TARGET = 'https://a.ru/catalog/pump-123/'
XML = f'<urlset><url><loc>{TARGET}</loc></url></urlset>'.encode()


class Response:
    def __init__(self, url, body=b'', status=200, headers=None, delay=0):
        self.url, self.body, self.status = url, body, status
        self.headers = CIMultiDict(headers or {'content-type': 'application/xml'})
        self.delay = delay
        self.read_bytes = 0
        self.closed = False
        self.entered = False
        self.content = self

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def iter_chunked(self, size):
        for offset in range(0, len(self.body), min(size, 64)):
            await asyncio.sleep(self.delay)
            chunk = self.body[offset:offset + min(size, 64)]
            self.read_bytes += len(chunk)
            yield chunk


def transport(routes):
    calls = []
    class Session:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def request(self, method, url, **kwargs):
            calls.append((method, url))
            value = routes[url]
            return value() if callable(value) else value
    return Session, calls


@pytest.fixture
def parser(tmp_path):
    config = Config()
    config.base_dir = str(tmp_path)
    config.sitemap_max_compressed_bytes = 256
    config.sitemap_max_decompressed_bytes = 512
    return WebCrawler(config).sitemap_parser


@pytest.mark.parametrize('encoding', [False, True])
def test_streaming_gzip_expansion_is_bounded(parser, encoding):
    async def run():
        url = 'https://a.ru/map.xml'
        response = Response(url, gzip.compress(b'x' * 100000), headers={'Content-Encoding': 'gzip'} if encoding else {})
        session, calls = transport({url: response})
        with patch('web_crawler.aiohttp.ClientSession', session):
            with pytest.raises(ValueError):
                await parser._request(url)
        assert response.closed
        assert calls == [('GET', url)]
    asyncio.run(run())


def test_wire_limit_stops_stream_before_complete_body(parser):
    async def run():
        url = 'https://a.ru/map.xml'
        response = Response(url, b'x' * 10000)
        session, _ = transport({url: response})
        with patch('web_crawler.aiohttp.ClientSession', session):
            with pytest.raises(ValueError):
                await parser._request(url)
        assert response.read_bytes <= 320
        assert response.closed
    asyncio.run(run())


def test_compressed_wire_limit_is_separate_from_decoded_limit(parser):
    async def run():
        url = 'https://a.ru/map.xml'
        parser.config.sitemap_max_decompressed_bytes = 20000
        raw = gzip.compress(random.Random(42).randbytes(10000))
        response = Response(url, raw)
        session, _ = transport({url: response})
        with patch('web_crawler.aiohttp.ClientSession', session):
            with pytest.raises(ValueError):
                await parser._request(url)
        assert response.read_bytes <= 320
        assert response.closed
    asyncio.run(run())


@pytest.mark.parametrize('raw', [b'\x1f\x8bbroken', gzip.compress(XML)[:-4]])
def test_corrupt_gzip_uses_plain_fallback(parser, raw):
    async def run():
        zipped, plain = 'https://a.ru/map.xml.gz?token=123', 'https://a.ru/map.xml?token=123'
        session, calls = transport({zipped: Response(zipped, raw), plain: Response(plain, XML)})
        with patch('web_crawler.aiohttp.ClientSession', session):
            assert [row[0] for row in await parser.parse_sitemap(zipped)] == [TARGET]
        assert calls == [('GET', zipped), ('GET', plain)]
    asyncio.run(run())


@pytest.mark.parametrize('method', ['_extract_sitemaps_from_robots', '_find_sitemap_links_in_html'])
def test_discovery_bodies_use_same_streaming_limits(parser, method):
    async def run():
        url = 'https://a.ru/robots.txt' if 'robots' in method else 'https://a.ru/'
        response = Response(url, b'x' * 10000)
        session, _ = transport({url: response})
        with patch('web_crawler.aiohttp.ClientSession', session):
            assert await getattr(parser, method)(url) == []
        assert response.read_bytes <= 320
        assert response.closed
    asyncio.run(run())


def test_redirect_hops_consume_budget_without_socket(parser):
    async def run():
        parser.config.sitemap_max_requests = 3
        routes = {f'https://a.ru/{i}': Response(f'https://a.ru/{i}', status=302, headers={'Location': f'/{i + 1}'}) for i in range(8)}
        session, calls = transport(routes)
        with patch('web_crawler.aiohttp.ClientSession', session):
            assert await parser.parse_sitemap('https://a.ru/0') == []
        assert calls == [('GET', f'https://a.ru/{i}') for i in range(3)]
    asyncio.run(run())


def test_reset_cancels_and_awaits_inflight_reader(parser):
    async def run():
        url = 'https://a.ru/map.xml'
        response = Response(url, XML, delay=10)
        session, _ = transport({url: response})
        with patch('web_crawler.aiohttp.ClientSession', session):
            waiter = asyncio.create_task(parser.parse_sitemap(url))
            for _ in range(20):
                if response.entered:
                    break
                await asyncio.sleep(0)
            assert response.entered
            await parser.reset_run()
            result = await asyncio.gather(waiter, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert not parser._loading
        assert response.closed
    asyncio.run(run())


def test_robots_slow_first_map_preserves_fast_second_and_cleans_tasks(parser):
    async def run():
        parser.config.sitemap_max_compressed_bytes = 4096
        parser.config.sitemap_max_decompressed_bytes = 4096
        parser.config.sitemap_phase_timeout_seconds = 0.06
        base = 'https://a.ru'
        slow = Response(base + '/slow.xml', XML, delay=1)
        routes = {
            base + '/robots.txt': Response(base + '/robots.txt', f'Sitemap: {base}/slow.xml\nSitemap: {base}/fast.xml\n'.encode()),
            base + '/slow.xml': slow,
            base + '/fast.xml': Response(base + '/fast.xml', XML),
        }
        session, calls = transport(routes)
        with patch('web_crawler.aiohttp.ClientSession', session):
            batches = await parser.collect_sitemap_urls(base)
        assert calls[0] == ('GET', base + '/robots.txt')
        assert TARGET in [row[0] for batch in batches for row in batch]
        assert not parser._loading
        assert slow.closed
    asyncio.run(run())


def test_external_phase_cancellation_awaits_readers(parser):
    async def run():
        base = 'https://a.ru'
        parser.config.sitemap_max_compressed_bytes = 4096
        slow = Response(base + '/slow.xml', XML, delay=1)
        routes = {
            base + '/robots.txt': Response(base + '/robots.txt', f'Sitemap: {base}/slow.xml\n'.encode()),
            base + '/slow.xml': slow,
        }
        session, _ = transport(routes)
        with patch('web_crawler.aiohttp.ClientSession', session):
            task = asyncio.create_task(parser.collect_sitemap_urls(base))
            for _ in range(10):
                await asyncio.sleep(0)
            task.cancel()
            result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert not parser._loading
        assert slow.closed
    asyncio.run(run())
