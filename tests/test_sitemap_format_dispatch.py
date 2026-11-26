"""Публичный parse_sitemap: байты HTTP -> правильный парсер -> реальные URL."""
import asyncio
import gzip
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import web_crawler
from config import Config
from web_crawler import WebCrawler

URL='https://factory.ru/catalog/pump-123/'
XML=f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{URL}</loc></url></urlset>'
HTML=f'<html><head><title>Карта сайта</title></head><body><a href="{URL}">Насос</a></body></html>'
TEXT='# карта сайта\n\n'+URL+'\n'

class Context:
    def __init__(self, value):self.value=value
    async def __aenter__(self):return self.value
    async def __aexit__(self,*args):return False

@pytest.fixture
def parser(tmp_path):
    cfg=Config();cfg.base_dir=str(tmp_path)
    return WebCrawler(cfg).sitemap_parser

@pytest.fixture
def transport(monkeypatch):
    responses={};calls=[]
    def request(method,url,**kwargs):
        calls.append(url)
        assert url in responses, 'Неожиданный запрос '+url
        body,mime,status=responses[url]
        async def chunks(size):
            for offset in range(0,len(body),size):
                yield body[offset:offset+size]
        return Context(SimpleNamespace(status=status,url=url,headers={'content-type':mime},
                                       content=SimpleNamespace(iter_chunked=chunks)))
    monkeypatch.setattr(web_crawler.aiohttp,'TCPConnector',Mock())
    monkeypatch.setattr(web_crawler.aiohttp,'ClientSession',lambda **kwargs:Context(SimpleNamespace(request=request)))
    return responses,calls


def run(parser,transport,address,body,mime='',status=200):
    responses,calls=transport
    responses[address]=(body.encode('utf-8') if isinstance(body,str) else body,mime,status)
    return asyncio.run(parser.parse_sitemap(address))

@pytest.mark.parametrize('body',[XML,HTML,TEXT])
@pytest.mark.parametrize('path,mime',[
 ('/sitemap.xml','application/xml'),('/sitemap.html','text/html'),
 ('/sitemap.txt','text/plain'),('/sitemap','application/octet-stream'),
 ('/download?format=xml',''),
])
def test_body_wins_over_mime_extension_and_name(parser,transport,body,path,mime):
    rows=run(parser,transport,'https://factory.ru'+path,body,mime)
    assert [r[0] for r in rows]==[URL]
    assert all(len(row)==4 for row in rows)

@pytest.mark.parametrize('body',[XML,HTML,TEXT])
@pytest.mark.parametrize('compressed',[True,False])
@pytest.mark.parametrize('path',['/sitemap.xml.gz','/sitemap.gz?token=12','/download'])
def test_gzip_or_already_decoded_response(parser,transport,body,compressed,path):
    raw=body.encode();raw=gzip.compress(raw) if compressed else raw
    address='https://factory.ru'+path
    rows=run(parser,transport,address,raw,'application/gzip')
    assert [r[0] for r in rows]==[URL]
    assert transport[1]==[address]

@pytest.mark.parametrize('namespace',['',' xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"',' xmlns:sm="http://www.sitemaps.org/schemas/sitemap/0.9"'])
def test_xml_root_namespace_and_index_child_dispatch(parser,transport,namespace):
    prefix='sm:' if 'xmlns:sm' in namespace else ''
    index=f'<?xml version="1.0"?><!-- index --><{prefix}sitemapindex{namespace}><{prefix}sitemap><{prefix}loc>https://factory.ru/child.txt</{prefix}loc></{prefix}sitemap></{prefix}sitemapindex>'
    transport[0]['https://factory.ru/child.txt']=(TEXT.encode(),'application/xml',200)
    rows=run(parser,transport,'https://factory.ru/sitemap.html',index,'text/html')
    assert [r[0] for r in rows]==[URL]
    assert transport[1]==['https://factory.ru/sitemap.html','https://factory.ru/child.txt']

@pytest.mark.parametrize('encoding',['utf-8-sig','utf-16','utf-32'])
def test_bom_and_leading_whitespace(parser,transport,encoding):
    body=(' \n<?xml version="1.0"?>\n<!--comment-->\n'+XML).encode(encoding)
    assert [r[0] for r in run(parser,transport,'https://factory.ru/map',body)]==[URL]

@pytest.mark.parametrize('body',[
 f'<sm:urlset xmlns:sm="http://www.sitemaps.org/schemas/sitemap/0.9"><sm:url><sm:loc>{URL}</sm:loc></sm:url></sm:urlset>',
 f'<a href="{URL}">Насос</a>',
 f'<nav><ul><li><a href="{URL}">Насос</a></li></ul></nav>',
 '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body><a href="'+URL+'">Насос</a></body></html>',
 '<!DOCTYPE html>'+HTML,
 '<!-- header -->'+HTML,
])
def test_namespaced_xml_xhtml_and_html_fragments(parser,transport,body):
    assert [r[0] for r in run(parser,transport,'https://factory.ru/sitemap',body,'application/xml')]==[URL]

@pytest.mark.parametrize('body',[
 '<Error><Code>AccessDenied</Code></Error>',
 '<error><url><loc>'+URL+'</loc></url></error>',
 '<rss><url><loc>'+URL+'</loc></url></rss>',
 '<feed xmlns="http://www.w3.org/2005/Atom"><url><loc>'+URL+'</loc></url></feed>',
 '<root><urlset><url><loc>'+URL+'</loc></url></urlset></root>',
 XML[:-9],
 XML+'<extra/>',
 '<urlset><url><loc>'+URL+'?a=1&b=2</loc></url></urlset>',
 '', ' \n\t', 'not a sitemap', '{"url":"'+URL+'"}',
 'error\n'+URL,
])
def test_invalid_xml_unrecognized_body_or_error_is_not_map(parser,transport,body):
    assert run(parser,transport,'https://factory.ru/sitemap.html',body,'text/html')==[]

@pytest.mark.parametrize('body',[
 '<error><url><loc>'+URL+'</loc></url></error>',XML[:-9],XML+'<extra/>',
])
def test_direct_xml_parser_also_rejects_wrong_root_and_broken_xml(parser,body):
    assert asyncio.run(parser._parse_xml_sitemap(body,'https://factory.ru/sitemap.xml',0,3))==[]

@pytest.mark.parametrize('marker',[
 '<title>404 Not Found</title>', '<h1>Access Denied</h1>',
 '<title>Just a moment...</title>', '<title>Ошибка 403</title>',
 '<h1>Страница не найдена</h1>', '<div id="challenge-form">Challenge</div>',
 '<p>Домен не привязан к хостингу</p>', '<script src="/manimg/login.js"></script>',
])
def test_known_html_stub_does_not_leak_navigation_as_sitemap(parser,transport,marker):
    body='<html><body>'+marker+'<a href="'+URL+'">Каталог</a></body></html>'
    address='https://factory.ru/sitemap.xml.gz'
    assert run(parser,transport,address,body,'application/xml')==[]
    assert transport[1]==[address] # распознанная заглушка не считается битым gzip

@pytest.mark.parametrize('caption',['Ошибка 404','captcha','Access Denied','Домен не привязан к хостингу'])
def test_help_link_captions_are_not_error_documents(parser,transport,caption):
    body='<html><title>Карта сайта</title><body><a href="'+URL+'">'+caption+'</a></body></html>'
    assert [r[0] for r in run(parser,transport,'https://factory.ru/sitemap.html',body,'text/html')]==[URL]

@pytest.mark.parametrize('raw',[b'not a gzip',b'\x1f\x8bbroken',gzip.compress(XML.encode())[:-5]])
@pytest.mark.parametrize('suffix',['.gz','.GZ?token=abc&part=2'])
def test_corrupt_gzip_fallback_preserves_query(parser,transport,raw,suffix):
    gz='https://factory.ru/sitemap.xml'+suffix
    plain='https://factory.ru/sitemap.xml'+('?' + suffix.split('?',1)[1] if '?' in suffix else '')
    transport[0][plain]=(XML.encode(),'text/plain',200)
    assert [r[0] for r in run(parser,transport,gz,raw,'application/gzip')]==[URL]
    assert transport[1]==[gz,plain]

@pytest.mark.parametrize('status',[403,404,500,503])
def test_http_error_is_not_parsed_even_with_map_shaped_body(parser,transport,status):
    assert run(parser,transport,'https://factory.ru/sitemap.xml',XML,'application/xml',status)==[]

@pytest.mark.parametrize('filename',['oilon_pro_iblock81.xml','byrpex_ru.xml','gomeloboi_p.xml','silikat_numeric.xml'])
def test_saved_real_xml_fixtures_through_dispatch(parser,transport,filename):
    path=Path(__file__).parent/'fixtures/sitemaps'/filename
    assert path.is_file()
    content=path.read_text()
    expected=asyncio.run(parser._parse_xml_sitemap(content,'https://factory.ru/sitemap.xml',0,3))
    assert expected
    assert run(parser,transport,'https://factory.ru/sitemap.xml',content,'application/octet-stream')==expected

@pytest.mark.parametrize('prefix',[
 '<meta charset="utf-8">','<script>var x=1;</script>',
 '<!DOCTYPE html><title>Карта сайта</title>','<dl><dt>',
 'Карта сайта\n','<article>',
])
@pytest.mark.parametrize('mime',['text/html','application/xml',''])
def test_html_optional_root_tags_and_leading_text(parser,transport,prefix,mime):
    body=prefix+f'<a href="{URL}">Насос</a>'
    assert [r[0] for r in run(parser,transport,'https://factory.ru/download',body,mime)]==[URL]

@pytest.mark.parametrize('comment',['/ispmgr','Домен не привязан к хостингу','welcome to nginx!'])
def test_html_comments_are_not_visible_stub_markers(parser,transport,comment):
    assert [r[0] for r in run(parser,transport,'https://factory.ru/map.html','<!-- '+comment+' -->'+HTML)]==[URL]


def test_scheme_relative_text_url_retains_existing_support(parser,transport):
    rows=run(parser,transport,'https://factory.ru/map',URL.replace('https:',''))
    assert [r[0] for r in rows]==[URL]


def test_json_containing_html_is_not_html_map(parser,transport):
    assert run(parser,transport,'https://factory.ru/sitemap.html',
               '{"error":"<a href=\"'+URL+'\">help</a>"}', 'text/html')==[]

@pytest.mark.parametrize('body',[
 '<sitemapindex><url><loc>'+URL+'</loc></url></sitemapindex>',
 '<urlset><sitemap><loc>https://factory.ru/child.xml</loc></sitemap></urlset>',
])
def test_xml_root_controls_entry_type(parser,transport,body):
    assert run(parser,transport,'https://factory.ru/sitemap.xml',body,'application/xml')==[]
    assert transport[1]==['https://factory.ru/sitemap.xml']
