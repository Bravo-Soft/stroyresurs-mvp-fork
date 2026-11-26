"""Настоящие HTTP/Chromium: загрузка и закрытие взаимно исключающих панелей."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from config import Config
from text_extractor import html_to_markdown
from web_crawler import WebCrawler

pytestmark = pytest.mark.live

HTML = '''<html><body><main><h1>Насос 100</h1><p>Промышленный насос.</p>
<button id="buy" onclick="document.body.textContent='ЗАКАЗ ОФОРМЛЕН'">Купить</button>
<button aria-expanded="false" aria-controls="first" onclick="load('first','Напор 125 м')">Характеристики</button>
<div id="first"></div>
<button aria-expanded="false" aria-controls="second" onclick="load('second','Масса 73 кг')">Характеристики</button>
<div id="second"></div>
</main><script>function load(id,text){
  document.getElementById('first').innerHTML='';
  document.getElementById('second').innerHTML='';
  setTimeout(()=>{document.getElementById(id).innerHTML='<p>'+text+'</p>'}, 150);
}</script></body></html>'''


@pytest.fixture
def site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            html = HTML
            if self.path == '/modal':
                html = html.replace('aria-expanded="false" aria-controls="first"', 'data-toggle="modal" data-target="#first"')
                html = html.replace('<div id="first"></div>', '<dialog id="first" class="modal"></dialog>')
                html = html.replace("load('first','Напор 125 м')", "document.getElementById('first').showModal();load('first','Напор 125 м')")
                html = html.replace("'<p>'+text+'</p>'", "'<div class=\"modal-body\"><p>'+text+'</p></div>'")
            elif self.path == '/blocked':
                html = html.replace('onclick="load(', 'disabled onclick="load(')
            elif self.path == '/loading':
                html = html.replace("'<p>'+text+'</p>'", "'Загрузка...'")
            elif self.path == '/navigation':
                html = html.replace("load('first','Напор 125 м')", "location.href='/elsewhere'")
            elif self.path == '/foreign':
                html = html.replace("load('first','Напор 125 м')", "location.href='https://outside.invalid/product'")
            data = html.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize('path,success', [('/accordion', True), ('/modal', True),
                                       ('/blocked', False), ('/loading', False), ('/navigation', False),
                                       ('/foreign', False)])
def test_real_product_panels(site, tmp_path, path, success):
    async def run():
        cfg = Config()
        cfg.base_dir = str(tmp_path)
        cfg.graph_db_enable = cfg.vector_db_enable = cfg.census_enabled = False
        cfg.page_fetch_timeout = 25
        crawler = WebCrawler(cfg)
        crawler.current_base_url = site
        await crawler.initialize()
        try:
            outcome = await crawler.fetch_page(site + path, 'product')
            if success:
                assert outcome.reason == 'ok', outcome.reason
                assert outcome.transport == 'ajax_tabs'
                md = html_to_markdown(outcome.content.html, site + path)
                assert 'Напор 125 м' in md and 'Масса 73 кг' in md
                assert 'ЗАКАЗ ОФОРМЛЕН' not in md
            else:
                reason = 'out_of_scope' if path == '/foreign' else 'incomplete_render'
                assert outcome.content is None and outcome.reason == reason
        finally:
            await crawler.close()
    asyncio.run(run())
