"""Файлы: периметр проверяется до первого запроса и каждого редиректа."""
import asyncio

import pytest

from file_download_cache import FileDownloadCache
from config import Config
from site_profiles import profile_from_dict
from web_crawler import FileDownloadManager, WebCrawler


class Response:
    def __init__(self, url, status=200, headers=None, body=b'%PDF-1.7'):
        self.url, self.status = url, status
        self.headers = headers or {'Content-Type': 'application/pdf'}
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def read(self):
        return self.body


class Session:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def request(self, method, url, **kwargs):
        assert kwargs['allow_redirects'] is False
        self.calls.append((method, url))
        return self.responses[url]


def manager():
    profile, errors = profile_from_dict({
        'domain': 'company.test', 'crawl': {
            'equivalent_domains': ['mirror.test'], 'file_hosts': ['cdn.test']}})
    assert errors == []
    crawler = WebCrawler.__new__(WebCrawler)
    crawler.profile = profile
    crawler.current_base_url = 'https://company.test/'
    crawler.host_throttle = None
    crawler.processing_tracker = None
    return FileDownloadManager(crawler, Config())


def test_document_links_allow_mirror_and_file_cdn_only():
    m = manager()
    html = ''.join(f'<a href="https://{host}/a.pdf">a</a>' for host in
                   ('company.test', 'mirror.test', 'cdn.test', 'evil.test', 'cdn.test.evil.test'))
    assert m.extract_document_links(html, 'https://company.test/docs') == [
        f'https://{host}/a.pdf' for host in ('company.test', 'mirror.test', 'cdn.test')]
    assert not m.crawler._is_allowed_url('https://cdn.test/page')


@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_file_redirect_checked_before_request(method):
    m = manager()
    start, cdn, evil = ('https://company.test/a.pdf', 'https://cdn.test/a.pdf',
                        'https://evil.test/a.pdf')
    session = Session({start: Response(start, 302, {'Location': cdn}),
                       cdn: Response(cdn, 302, {'Location': evil})})
    async def run():
        with pytest.raises(ValueError, match='периметра'):
            async with m._file_response(session, method, start):
                pytest.fail('Внешний ответ не должен быть выдан')
    asyncio.run(run())
    assert session.calls == [(method, start), (method, cdn)]


@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_file_external_initial_url_never_requested(method):
    m, session = manager(), Session({})
    async def run():
        with pytest.raises(ValueError):
            async with m._file_response(session, method, 'https://evil.test/a.pdf'):
                pass
    asyncio.run(run())
    assert session.calls == []


def test_file_allowed_relative_redirect():
    m = manager()
    first, final = 'https://cdn.test/old.pdf', 'https://cdn.test/new.pdf'
    session = Session({first: Response(first, 302, {'Location': '/new.pdf'}),
                       final: Response(final)})
    async def run():
        async with m._file_response(session, 'GET', first) as response:
            assert await response.read() == b'%PDF-1.7'
    asyncio.run(run())
    assert len(session.calls) == 2


@pytest.mark.parametrize('mime', ['text/html', 'application/xhtml+xml'])
def test_cdn_html_mime_rejected(mime):
    m, url = manager(), 'https://cdn.test/a.pdf'
    session = Session({url: Response(url, headers={'Content-Type': mime})})
    async def run():
        with pytest.raises(ValueError, match='HTML'):
            async with m._file_response(session, 'GET', url):
                pass
    asyncio.run(run())


@pytest.mark.parametrize('body', [
    b' <!DOCTYPE html><html>CDN error</html>',
    b'\xef\xbb\xbf <!-- CDN error --> <html>error</html>',
])
def test_cdn_html_body_not_saved(monkeypatch, tmp_path, body):
    import web_crawler
    url, m = 'https://cdn.test/a.pdf', manager()
    m.download_cache = FileDownloadCache(tmp_path.parent / (tmp_path.name + '_cache'))
    session = Session({url: Response(url, body=body)})
    class ClientSession:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return session
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(web_crawler.aiohttp, 'ClientSession', ClientSession)
    assert asyncio.run(m._download_aiohttp_unlimited(url, str(tmp_path))) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('extension,mime,body', [
    ('pdf', 'application/pdf', b'%PDF-1.7 document'),
    ('png', 'image/png', b'\x89PNG\r\n\x1a\nimage'),
    ('rtf', 'application/rtf', b'{\\rtf1 document}'),
])
def test_allowed_cdn_file_saved(monkeypatch, tmp_path, extension, mime, body):
    import web_crawler
    url, m = f'https://cdn.test/a.{extension}', manager()
    m.download_cache = FileDownloadCache(tmp_path.parent / (tmp_path.name + '_cache'))
    session = Session({url: Response(url, headers={'Content-Type': mime}, body=body)})
    class ClientSession:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return session
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(web_crawler.aiohttp, 'ClientSession', ClientSession)
    path = asyncio.run(m._download_aiohttp_unlimited(url, str(tmp_path)))
    assert path == str(tmp_path / f'a.{extension}')
    assert (tmp_path / f'a.{extension}').read_bytes() == body


@pytest.mark.parametrize('redirect_host,expected_size', [('cdn.test', 2048), ('evil.test', None)])
def test_image_size_get_after_head_rechecks_redirect(monkeypatch, redirect_host, expected_size):
    import web_crawler
    url, m = 'https://company.test/image.png', manager()
    target = f'https://{redirect_host}/image.png'
    class ImageSession(Session):
        def request(self, method, requested, **kwargs):
            assert kwargs['allow_redirects'] is False
            self.calls.append((method, requested))
            if method == 'HEAD':
                return Response(url, headers={'Content-Type': 'image/png'})
            if requested == url:
                return Response(url, 302, {'Location': target})
            return Response(target, 206, {'Content-Type': 'image/png',
                                         'Content-Range': 'bytes 0-1024/2048'})
    session = ImageSession({})
    class ClientSession:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return session
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(web_crawler.aiohttp, 'ClientSession', ClientSession)
    assert asyncio.run(m.get_image_size(url)) == expected_size
    expected_calls = [('HEAD', url), ('GET', url)]
    if expected_size:
        expected_calls.append(('GET', target))
    assert session.calls == expected_calls


@pytest.mark.parametrize('ignore_subdomains', [True, False])
def test_product_soup_file_extraction_uses_explicit_file_hosts(ignore_subdomains):
    from bs4 import BeautifulSoup
    crawler = manager().crawler
    crawler.config = Config()
    crawler.config.ignore_subdomains = ignore_subdomains
    crawler.profile, errors = profile_from_dict({
        'domain': 'company.test', 'crawl': {
            'equivalent_domains': ['mirror.test'],
            'file_hosts': ['cdn.test', 'cdn.company.test']}})
    assert errors == []
    html = ''.join(f'<a href="https://{host}/a.pdf"><span>скачать</span></a>' for host in (
        'company.test', 'mirror.test', 'cdn.test', 'cdn.company.test',
        'evil.test', 'other.company.test', 'cdn.test.evil.test'))
    links = crawler._extract_file_links_from_soup(
        BeautifulSoup(html, 'html.parser'), 'https://company.test/product/')
    assert set(links) == {f'https://{host}/a.pdf' for host in (
        'company.test', 'mirror.test', 'cdn.test', 'cdn.company.test')}


@pytest.mark.parametrize('target_host,expected', [('mirror.test', True), ('cdn.test', False),
                                                 ('evil.test', False)])
def test_pagination_head_uses_page_policy(monkeypatch, target_host, expected):
    import web_crawler
    m = manager()
    start = 'https://company.test/docs?page=1'
    requested = 'https://company.test/docs?page=2'
    target = f'https://{target_host}/docs?page=2'
    session = Session({requested: Response(requested, 302, {'Location': target}),
                       target: Response(target, headers={'Content-Type': 'text/html'})})
    class ClientSession:
        async def __aenter__(self):
            return session
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(web_crawler.aiohttp, 'ClientSession', ClientSession)
    result = asyncio.run(m._find_next_page_by_url_parameters(None, start, 1))
    assert result == (requested if expected else None)
    assert session.calls == ([('HEAD', requested), ('HEAD', target)] if expected
                             else [('HEAD', requested)])
