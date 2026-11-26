"""Обновление файлов: условные запросы, целостность и устойчивые даты."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import Config
from file_id_manager import FileIDManager
from web_crawler import FileDownloadManager

URL = 'https://factory.test/catalog.pdf'
OLD = b'%PDF-1.7 old catalog'
NEW = b'%PDF-1.7 new catalog'


class Response:
    def __init__(self, body=OLD, status=200, headers=None, url=URL):
        self.body, self.status, self.url = body, status, url
        self.headers = {'Content-Type': 'application/pdf', **(headers or {})}
        self.reads = 0
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def read(self):
        self.reads += 1
        return self.body


@pytest.fixture
def setup(tmp_path, monkeypatch):
    cfg = Config()
    cfg.base_dir = str(tmp_path/'Base')
    crawler = SimpleNamespace(profile=None, processing_tracker=None,
        _is_allowed_url=lambda url, **kw: url.startswith('https://factory.test/'))
    replies, calls = [], []
    class Session:
        def __init__(self, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def request(self, method, url, **kwargs):
            calls.append((method,url,kwargs))
            assert replies, 'Лишний HTTP-запрос'
            return replies.pop(0)
    monkeypatch.setattr('web_crawler.aiohttp.ClientSession', Session)
    dest = tmp_path/'Documents'; dest.mkdir()
    def manager():
        return FileDownloadManager(crawler,cfg)
    def download(response, url=URL, m=None):
        replies.append(response)
        return asyncio.run((m or manager())._download_aiohttp(url,str(dest)))
    return SimpleNamespace(dest=dest,cfg=cfg,manager=manager,download=download,calls=calls)


def test_conditional_get_survives_restart(setup):
    s=setup
    first=s.download(Response(headers={'ETag':'"a"'}))
    before=Path(first).stat().st_mtime_ns
    response=Response(status=304)
    assert s.download(response)==first
    assert s.calls[-1][2]['headers']['If-None-Match']=='"a"'
    assert response.reads==0 and Path(first).stat().st_mtime_ns==before


def test_last_modified_validator(setup):
    s=setup; date='Mon, 02 Nov 2026 10:00:00 GMT'
    first=s.download(Response(headers={'Last-Modified':date}))
    assert s.download(Response(status=304))==first
    assert s.calls[-1][2]['headers']['If-Modified-Since']==date


def test_same_url_changed_bytes_replace_active_copy(setup):
    s=setup
    first=s.download(Response(headers={'ETag':'"a"'}))
    assert s.download(Response(NEW,headers={'ETag':'"b"'}))==first
    assert Path(first).read_bytes()==NEW
    assert any(p.is_file() and p.read_bytes()==OLD for p in (Path(s.cfg.base_dir)/'file_download_cache').rglob('*'))


def test_without_validators_hashes_full_body_and_keeps_mtime(setup):
    s=setup; first=s.download(Response())
    os.utime(first,(1000000000,1000000000))
    again=Response()
    assert s.download(again)==first
    assert again.reads==1 and Path(first).stat().st_mtime==1000000000
    assert s.download(Response(NEW))==first and Path(first).read_bytes()==NEW


def test_equal_size_is_not_evidence_of_equal_content(setup):
    s=setup
    first=s.download(Response(headers={'Content-Length':str(len(OLD))}))
    s.download(Response(NEW,headers={'Content-Length':str(len(NEW))}))
    assert len(OLD)==len(NEW) and Path(first).read_bytes()==NEW


@pytest.mark.parametrize('local', ['missing','corrupt'])
def test_invalid_local_copy_does_not_send_validators(setup,local):
    s=setup
    first=Path(s.download(Response(headers={'ETag':'"a"'})))
    if local=='missing':
        first.rename(first.with_suffix('.saved'))
    else:
        first.write_bytes(b'broken')
    assert Path(s.download(Response(headers={'ETag':'"a"'}))).read_bytes()==OLD
    assert not s.calls[-1][2].get('headers')


def test_legacy_file_is_verified_once_without_rewriting_equal_bytes(setup):
    s=setup
    local=s.dest/'catalog.pdf'; local.write_bytes(OLD); os.utime(local,(1000000000,1000000000))
    response=Response(headers={'ETag':'"a"'})
    assert s.download(response)==str(local) and response.reads==1
    assert local.stat().st_mtime==1000000000
    s.download(Response(status=304))
    assert s.calls[-1][2]['headers']['If-None-Match']=='"a"'


def test_different_urls_same_basename_never_share_content(setup):
    s=setup
    first=s.download(Response())
    url='https://factory.test/other/catalog.pdf'
    second=s.download(Response(NEW,url=url),url=url)
    assert first!=second
    assert Path(first).read_bytes()==OLD and Path(second).read_bytes()==NEW


@pytest.mark.parametrize('response',[Response(status=500),Response(b'<html>error</html>'),Response(b'')])
def test_bad_response_preserves_previous_file(setup,response):
    s=setup; first=s.download(Response(headers={'ETag':'"a"'}))
    assert s.download(response) is None
    assert Path(first).read_bytes()==OLD


def test_unsolicited_304_is_not_success(setup):
    assert setup.download(Response(status=304)) is None


def test_download_date_comes_from_file_not_inspection_clock(tmp_path):
    file=tmp_path/'old.pdf'; file.write_bytes(OLD); os.utime(file,(1000000000,1000000000))
    info=asyncio.run(FileIDManager().get_file_info(str(file)))
    assert info['download_date']==info['last_modified']


def converter(setup, monkeypatch, produce=True):
    import sys
    from file_converter import FileConverter
    calls=[]
    def run(command, **kwargs):
        calls.append(command)
        if produce:
            source=Path(command[-1])
            out=Path(command[command.index('--outdir')+1])/source.with_suffix('.pdf').name
            out.write_bytes(b'%PDF converted '+source.read_bytes())
        return SimpleNamespace(stderr='')
    monkeypatch.setattr('file_converter.subprocess.run',run)
    result=FileConverter(str(setup.dest),libreoffice_path=sys.executable,
                         log_file=str(setup.dest.parent/'conversion.log'),
                         delete_originals=False,delete_empty_folders=False,
                         download_cache_dir=setup.cfg.base_dir)
    return result,calls


def test_converted_copy_reused_after_original_removed(setup,monkeypatch):
    s=setup; url='https://factory.test/catalog.doc'
    source=Path(s.download(Response(b'doc1',headers={'ETag':'"a"'},url=url),url=url))
    conv,calls=converter(s,monkeypatch)
    assert conv.convert_single_file(source).success
    pdf=source.with_suffix('.pdf'); old_time=pdf.stat().st_mtime_ns
    source.rename(source.with_suffix('.saved'))
    response=Response(status=304,url=url)
    assert s.download(response,url=url)==str(pdf)
    assert response.reads==0 and pdf.stat().st_mtime_ns==old_time
    assert s.calls[-1][2]['headers']['If-None-Match']=='"a"'
    assert len(calls)==1


def test_changed_office_source_regenerates_existing_pdf(setup,monkeypatch):
    s=setup; url='https://factory.test/catalog.doc'
    source=Path(s.download(Response(b'doc1',headers={'ETag':'"a"'},url=url),url=url))
    conv,calls=converter(s,monkeypatch)
    assert conv.convert_single_file(source).success
    pdf=source.with_suffix('.pdf'); old_pdf=pdf.read_bytes()
    source.rename(source.with_suffix('.saved'))
    assert s.download(Response(b'doc2',headers={'ETag':'"b"'},url=url),url=url)==str(source)
    assert conv.convert_single_file(source).success
    assert pdf.read_bytes()!=old_pdf and b'doc2' in pdf.read_bytes()
    assert len(calls)==2
    assert conv.convert_single_file(source).error=='PDF уже существует'
    assert len(calls)==2


def test_failed_reconversion_does_not_accept_or_destroy_old_pdf(setup,monkeypatch):
    s=setup; url='https://factory.test/catalog.doc'
    source=Path(s.download(Response(b'doc1',url=url),url=url))
    conv,_=converter(s,monkeypatch)
    assert conv.convert_single_file(source).success
    pdf=source.with_suffix('.pdf'); old=pdf.read_bytes()
    s.download(Response(b'doc2',url=url),url=url)
    conv,_=converter(s,monkeypatch,produce=False)
    assert not conv.convert_single_file(source).success
    assert pdf.read_bytes()==old and source.read_bytes()==b'doc2'


def test_corrupt_converted_pdf_requires_source_download(setup,monkeypatch):
    s=setup; url='https://factory.test/catalog.doc'
    source=Path(s.download(Response(b'doc1',headers={'ETag':'"a"'},url=url),url=url))
    conv,_=converter(s,monkeypatch)
    assert conv.convert_single_file(source).success
    source.rename(source.with_suffix('.saved'))
    source.with_suffix('.pdf').write_bytes(b'broken')
    assert s.download(Response(b'doc1',url=url),url=url)==str(source)
    assert not s.calls[-1][2].get('headers')


@pytest.mark.parametrize('name',['../escape.pdf','/tmp/escape.pdf','..\\escape.pdf'])
def test_unsafe_disposition_never_writes_outside_destination(setup,name):
    assert setup.download(Response(headers={'Content-Disposition':f'attachment; filename="{name}"'})) is None
    assert list(setup.dest.iterdir())==[]


def test_caches_do_not_cross_company_directories(setup):
    s=setup
    first=s.download(Response(headers={'ETag':'"a"'}))
    second=s.dest/'Other'; second.mkdir()
    # HTTP-объект остаётся общим; другой каталог не должен получить валидатор первого.
    m=s.manager()
    from unittest.mock import patch
    original=m._download_aiohttp
    async def other(url,directory):
        return await original(url,str(second))
    m._download_aiohttp=other
    path=s.download(Response(NEW),m=m)
    assert Path(path).parent==second and Path(first).read_bytes()==OLD
    assert not s.calls[-1][2].get('headers')


def test_office_files_with_same_stem_get_distinct_pdf_names(setup):
    s=setup; first_url='https://factory.test/catalog.doc'; second_url='https://factory.test/catalog.docx'
    first=s.download(Response(b'doc',url=first_url),url=first_url)
    second=s.download(Response(b'docx',url=second_url),url=second_url)
    assert Path(first).stem!=Path(second).stem


def test_redirect_does_not_send_old_resource_validator_to_new_target(setup,monkeypatch):
    s=setup
    s.download(Response(headers={'ETag':'"a"'}))
    redirect=Response(status=302,headers={'Location':'/new.pdf'})
    final=Response(NEW,url='https://factory.test/new.pdf')
    calls=[]
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        def request(self,method,url,**kwargs):
            calls.append((url,kwargs))
            return redirect if url==URL else final
    monkeypatch.setattr('web_crawler.aiohttp.ClientSession',lambda **kw:Session())
    result=asyncio.run(s.manager()._download_aiohttp(URL,str(s.dest)))
    assert Path(result).read_bytes()==NEW
    assert calls[0][1]['headers']['If-None-Match']=='"a"'
    assert not calls[1][1]['headers']


def test_new_crawl_resets_only_in_memory_file_dedup(tmp_path):
    from web_crawler import WebCrawler
    cfg=Config(); cfg.base_dir=str(tmp_path)
    crawler=WebCrawler(cfg)
    manager=crawler.file_download_manager
    manager.downloaded_files_cache.add(URL)
    manager.processed_tabs.add('documents')
    manager.download_cache.save(tmp_path,URL,{'url':URL,'etag':'"a"'})
    asyncio.run(crawler.reset_state('Завод'))
    assert not manager.downloaded_files_cache and not manager.processed_tabs
    assert manager.download_cache.load(tmp_path,URL)['etag']=='"a"'


def test_corrupt_source_is_repaired_even_if_converted_pdf_is_valid(setup,monkeypatch):
    s=setup; url='https://factory.test/catalog.doc'
    source=Path(s.download(Response(b'doc1',headers={'ETag':'"a"'},url=url),url=url))
    conv,_=converter(s,monkeypatch)
    assert conv.convert_single_file(source).success
    source.write_bytes(b'broken')
    assert s.download(Response(b'doc1',url=url),url=url)==str(source)
    assert not s.calls[-1][2].get('headers') and source.read_bytes()==b'doc1'
