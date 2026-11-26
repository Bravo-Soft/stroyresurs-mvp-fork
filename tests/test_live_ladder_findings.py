"""Регрессии операторского прогона 02.11: баннер и товарные вкладки."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from config import Config
from site_profiles import profile_from_dict
from web_crawler import WebCrawler
from test_final_url_pipeline import browser


@pytest.fixture
def crawler(tmp_path):
    cfg = Config()
    cfg.base_dir = str(tmp_path)
    cfg.profiles_dir = str(Path(__file__).resolve().parents[1] / 'profiles')
    c = WebCrawler(cfg)
    c.current_base_url = 'https://tizol.com/'
    c._apply_site_profile(c.current_base_url)
    return c


def test_profile_tab_selector_validation():
    profile, errors = profile_from_dict({'domain': 'tizol.com', 'crawl': {'tab_selector': '.product-page__section-btn'}})
    assert not errors
    assert profile.crawl.tab_selector == '.product-page__section-btn'
    _, errors = profile_from_dict({'domain': 'tizol.com', 'crawl': {'tab_selector': []}})
    assert errors


def test_tizol_consent_checks_boxes_before_accept(crawler):
    events = []
    boxes = [SimpleNamespace(check=AsyncMock(side_effect=lambda **kw: events.append('check'))) for _ in range(2)]
    button = SimpleNamespace(click=AsyncMock(side_effect=lambda **kw: events.append('accept')))
    banner = SimpleNamespace(is_visible=AsyncMock(return_value=True),
                             query_selector_all=AsyncMock(return_value=boxes),
                             query_selector=AsyncMock(return_value=button))
    page = SimpleNamespace(url='https://tizol.com/catalog/p', query_selector=AsyncMock(return_value=banner),
                           wait_for_timeout=AsyncMock(), query_selector_all=AsyncMock(return_value=[]))
    assert asyncio.run(crawler._accept_cookie_consent(page))
    assert events == ['check', 'check', 'accept']


@pytest.mark.parametrize('broken', [False, True])
def test_ajax_uses_consent_and_scoped_tabs(crawler, broken):
    page = browser(crawler)
    page.url = 'https://tizol.com/catalog/p'
    crawler._guard_browser_page = AsyncMock(return_value=None)
    crawler._accept_cookie_consent = AsyncMock(return_value=True)
    tab = SimpleNamespace(is_visible=AsyncMock(return_value=True), inner_text=AsyncMock(return_value='Характеристики'),
                          click=AsyncMock(side_effect=TimeoutError('overlay') if broken else None))
    page.query_selector_all = AsyncMock(return_value=[tab])
    page.wait_for_timeout = AsyncMock()
    page.evaluate = AsyncMock(side_effect=['', '<p>Давление 16 бар</p>'])
    result = asyncio.run(crawler._make_dynamic_extractor().extract_product_tabs_merged(page.url))
    crawler._accept_cookie_consent.assert_awaited_once_with(page)
    page.query_selector_all.assert_awaited_once_with('.product-page__section-btn')
    if broken:
        assert result is None
    else:
        assert 'Давление 16 бар' in result


@pytest.mark.parametrize('body', [
    '<div class="product-page__inner"><p>Описание изоляции</p></div>',
    '<template v-slot:description><p>Описание изоляции</p></template>',
    '',
])
def test_merged_tabs_reach_markdown(body):
    from Profile_Markdown.tizol_com import extract
    html = '<html><body><h1 class="product-page__title">Изоспан А</h1>' + body + '''
    <section data-tab="ХАРАКТЕРИСТИКИ"><h2>ХАРАКТЕРИСТИКИ</h2>
    <table><tr><th>Водоупорность, мм</th><td>300</td></tr></table>
    <p>Температура от -60 до +80 °С</p></section>
    <section data-tab="СЕРТИФИКАТЫ"><h2>СЕРТИФИКАТЫ</h2>
    <a href="https://tizol.com/cert.pdf">Сертификат</a></section></body></html>'''
    md = extract(html, 'https://tizol.com/catalog/test')
    assert 'Водоупорность' in md and '300' in md
    assert 'от -60 до +80' in md
    assert 'https://tizol.com/cert.pdf' in md
    assert md.count('Водоупорность') == 1
