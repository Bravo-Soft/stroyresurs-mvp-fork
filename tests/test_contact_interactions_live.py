"""Настоящий Chromium: AJAX-панели, полнота снимка и повторное хранение."""
import asyncio

import pytest
from playwright.async_api import async_playwright

from config import Config
from contact_interactions import collect_contact_panels
from html_comparison import html_comparison_hash
from temp_storage_manager import TempStorageManager

pytestmark = pytest.mark.live
SELECTORS = Config().dynamic_tab_selectors + Config().dynamic_accordion_selectors


def run_browser(check):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=['--no-sandbox'])
            try:
                context = await browser.new_context()
                await check(await context.new_page())
            finally:
                await browser.close()
    asyncio.run(run())


def test_filled_hidden_panels_and_arbitrary_buttons_are_not_clicked():
    async def check(page):
        await page.set_content('''<button onclick="throw Error('не нажимать')">Заказать</button>
        <button role="tab" aria-controls="a" onclick="document.body.innerHTML=''">Москва</button>
        <div id="a" hidden>Дилер Москва, телефон 123</div>''')
        result = await collect_contact_panels(page, SELECTORS)
        assert 'Дилер Москва, телефон 123' in result
        assert await page.locator('#a').count() == 1
    run_browser(check)


def test_ajax_panels_merge_and_repeated_save_is_unchanged(tmp_path):
    async def check(page):
        html = '''<button class="tab" role="tab" aria-controls="a"
          onclick="setTimeout(()=>{document.getElementById('a').innerHTML='<p>Москва 123</p>'},200)">Москва</button>
        <button role="tab" aria-controls="b"
          onclick="document.getElementById('a').innerHTML='';setTimeout(()=>{document.getElementById('b').innerHTML='<p>Тула 456</p>'},200)">Тула</button>
        <div id="a"></div><div id="b"></div>'''
        storage = TempStorageManager(str(tmp_path / 'html'), max_size_mb=10)
        paths = []
        for index in range(2):
            await page.set_content(html)
            result = await collect_contact_panels(page, SELECTORS)
            assert 'Москва 123' in result and 'Тула 456' in result
            assert result.count('data-contact-panel="a"') == 1
            path, changed, _ = await storage.save_cleaned_html(
                'https://example.test/contacts', 'https://example.test/contacts', result,
                'Компания', category='contacts')
            assert changed == (index == 0)
            paths.append(path)
        assert paths[0] == paths[1]
    run_browser(check)


def test_shared_panel_waits_for_new_city_and_keeps_both():
    async def check(page):
        await page.set_content('''<button role="tab" aria-controls="a" aria-selected="true">Москва</button>
        <button role="tab" aria-controls="a"
          onclick="setTimeout(()=>{document.getElementById('a').innerHTML='Тула 456'},300)">Тула</button>
        <div id="a">Москва 123</div>''')
        result = await collect_contact_panels(page, SELECTORS)
        assert 'Москва 123' in result and 'Тула 456' in result
        assert result.count('data-contact-panel="a"') == 2
    run_browser(check)


def test_empty_ajax_panel_is_not_saved_as_success():
    async def check(page):
        await page.set_content('<button role="tab" aria-controls="a">Москва</button><div id="a">Загрузка</div>')
        with pytest.raises(Exception):
            await collect_contact_panels(page, SELECTORS)
    run_browser(check)


def test_dealer_data_change_still_changes_hash():
    async def check(page):
        await page.set_content('<button role="tab" aria-controls="a">Москва</button><div id="a" hidden>Москва 123</div>')
        before = await collect_contact_panels(page, SELECTORS)
        await page.locator('#a').evaluate("el => el.textContent='Москва 124'")
        after = await collect_contact_panels(page, SELECTORS)
        assert html_comparison_hash(before) != html_comparison_hash(after)
    run_browser(check)


@pytest.mark.parametrize('clear_delay', [None, 0, 200])
def test_readiness_is_checked_after_cookie_action(clear_delay):
    """Ридан: согласие перезагружает данные после первоначального появления JSON-LD."""
    from types import SimpleNamespace
    from web_crawler import DynamicContentExtractor

    async def check(page):
        context = page.context
        async def route(request):
            await request.fulfill(status=200, content_type='text/html; charset=utf-8', body=('''<html><body>
            <h1>Контакты</h1><div id="contact">INITIAL</div>
            <button role="tab" aria-controls="contact">Дилеры</button></body></html>'''.replace('INITIAL', 'Дилер Сигма 123' if clear_delay is None else 'Загрузка')))
        await context.route('https://factory.test/**', route)
        async def get_browser():
            return context
        async def close_page(p):
            await p.close()
        async def return_browser(ctx):
            pass
        async def consent(p):
            assert clear_delay is not None, 'Заполненная панель не требует клика cookie-баннера'
            await p.evaluate("""delay => {
                setTimeout(() => document.getElementById('contact').remove(), delay);
                setTimeout(() => {let el=document.createElement('div');el.id='contact';
                    el.textContent='Дилер Сигма 123';document.body.append(el)},1800);
            }""", clear_delay)
        pool = SimpleNamespace(get_browser=get_browser, close_page=close_page, return_browser=return_browser)
        extractor = DynamicContentExtractor(pool, Config(), accept_cookie_consent=consent,
                                            ready_selector='#contact')
        result = await extractor.extract('https://factory.test/contacts')
        assert result and len(result) == 1
        assert 'Дилер Сигма 123' in result[0] and 'Старый снимок' not in result[0]
    run_browser(check)
