"""Живой локальный HTTP: повторная загрузка без передачи неизменного тела."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest
from config import Config
from web_crawler import FileDownloadManager

pytestmark = pytest.mark.live


@pytest.fixture
def server(tmp_path):
    state=SimpleNamespace(body=b'%PDF first',etag='"one"',modified=None,calls=[],sent=0)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state.calls.append(dict(self.headers))
            unchanged=(state.etag and self.headers.get('If-None-Match')==state.etag
                       or not state.etag and state.modified and self.headers.get('If-Modified-Since')==state.modified)
            self.send_response(304 if unchanged else 200)
            self.send_header('Content-Type','application/pdf')
            if state.etag: self.send_header('ETag',state.etag)
            if state.modified: self.send_header('Last-Modified',state.modified)
            if not unchanged: self.send_header('Content-Length',str(len(state.body)))
            self.end_headers()
            if not unchanged:
                self.wfile.write(state.body)
                state.sent+=len(state.body)
        def log_message(self,*args): pass
    httpd=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=httpd.serve_forever,daemon=True); thread.start()
    cfg=Config(); cfg.base_dir=str(tmp_path/'Base')
    base=f'http://127.0.0.1:{httpd.server_port}'
    crawler=SimpleNamespace(profile=None,processing_tracker=None,_is_allowed_url=lambda u,**kw:u.startswith(base+'/'))
    state.url=base+'/catalog.pdf'; state.dest=tmp_path/'Documents'
    def download():
        return asyncio.run(FileDownloadManager(crawler,cfg)._download_aiohttp(state.url,str(state.dest)))
    state.download=download
    yield state
    httpd.shutdown(); httpd.server_close(); thread.join()


def test_real_304_keeps_bytes_mtime_and_then_detects_update(server):
    s=server
    path=Path(s.download()); before=path.stat().st_mtime_ns
    sent=s.sent
    assert s.download()==str(path)
    assert s.sent==sent and path.stat().st_mtime_ns==before
    assert s.calls[-1]['If-None-Match']=='"one"'
    s.body=b'%PDF other'; s.etag='"two"'
    assert s.download()==str(path) and path.read_bytes()==s.body
    assert s.sent==sent+len(s.body)


def test_real_last_modified(server):
    s=server; s.etag=None; s.modified='Mon, 02 Nov 2026 10:00:00 GMT'
    path=s.download(); sent=s.sent
    assert s.download()==path and s.sent==sent
    assert s.calls[-1]['If-Modified-Since']==s.modified


def test_real_server_without_validators_checks_bytes(server):
    s=server; s.etag=None
    path=Path(s.download()); before=path.stat().st_mtime_ns
    sent=s.sent
    assert s.download()==str(path) and s.sent==sent*2
    assert path.stat().st_mtime_ns==before
    s.body=b'%PDF other'
    assert s.download()==str(path) and path.read_bytes()==s.body
