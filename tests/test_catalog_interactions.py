"""Товарные окна: идентичность, обнаружение, полнота HTML → Markdown и хранения."""
import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from bs4 import BeautifulSoup

import web_crawler as wc
from config import Config
from data_island import has_unloaded_product_tabs, is_unrendered_store_listing
from text_extractor import html_to_markdown
from temp_storage_manager import TempStorageManager
from fetch_outcome import observe_fetch

FIXTURES = Path(__file__).parent / 'fixtures' / 'vitgroup'
EXAMPLES = [
    ('vit-color', '54891649', '1498486301712', 'Эмаль для крыш и металлоконструкций'),
    ('viteco', '55040791', '1498486301712', 'Грунтовка ГФ-021 «ВИТэко»'),
    ('vit-deco-aehrozolnye-ehmali', '54771594', '1498486363994', 'Эмаль аэрозольная «ВИТ ДЕКО» термостойкая'),
]


@pytest.fixture
def crawler(tmp_path):
    cfg = Config()
    cfg.base_dir = str(tmp_path)
    c = wc.WebCrawler(cfg)
    c.current_base_url = 'https://vitgroup.ru/'
    c._http_first_get = AsyncMock(return_value=None)
    c._fetch_with_aiohttp = AsyncMock(return_value=None)
    c._fetch_with_playwright = AsyncMock(return_value=(None, False, None))
    return c


def test_hash_product_identity_and_canonical(crawler):
    root = 'https://vitgroup.ru/viteco'
    urls = [root, root + '#!/tproduct/55040791-1498486301712',
            root + '#!/tproduct/55040791-1497456130776']
    assert len({crawler.url_normalizer.normalize_url(u) for u in urls}) == 3
    assert crawler.url_normalizer.normalize_url(root + '#specs') == root
    soup = BeautifulSoup(f'<link rel="canonical" href="{root}">', 'html.parser')
    assert crawler.url_normalizer.extract_canonical_url(soup, urls[1]) == urls[1]
    assert crawler.url_categorizer.categorize_url(urls[1])[0] == 'product'


@pytest.mark.parametrize('slug,rec,lid,name', EXAMPLES)
def test_real_product_fetch_and_markdown(crawler, slug, rec, lid, name):
    base = f'https://vitgroup.ru/{slug}'
    url = f'{base}#!/tproduct/{rec}-{lid}'
    raw = (FIXTURES / f'{slug}.html').read_text()
    crawler._http_first_get.return_value = wc.PageContent(raw, url, base, method='http_first')
    result = asyncio.run(crawler.fetch_page(url, 'product'))
    assert result.reason == 'ok', result.reason
    parsed = result.content
    assert parsed.url == url and parsed.canonical_url == url
    md = html_to_markdown(parsed.html, url)
    assert name in md
    assert 'ФАСОВКА' in md and 'СОСТАВ' in md and 'НАЗНАЧЕНИЕ' in md
    source = BeautifulSoup(raw, 'html.parser').find(id='rec' + rec)
    source = source.select_one(f'.t-popup [data-product-lid="{lid}"]')
    # Все слова описания (включая числа) должны пережить выделение окна.
    assert source.select_one('[class*="__descr"]').get_text(' ', strip=True) in parsed.soup.get_text(' ', strip=True)
    source_numbers = set(re.findall(r'\d+(?:[.,]\d+)?', source.get_text(' ', strip=True)))
    assert source_numbers <= set(re.findall(r'\d+(?:[.,]\d+)?', md))
    assert len(parsed.soup.select('.js-product-name')) == 1
    assert 'Заказать звонок' not in md
    crawler._fetch_with_playwright.assert_not_awaited()


@pytest.mark.parametrize('slug', ['vit-color', 'viteco', 'vit-deco-aehrozolnye-ehmali', 'caricynskie-kraski'])
def test_catalog_discovers_every_public_popup(crawler, slug):
    raw = (FIXTURES / f'{slug}.html').read_text()
    soup = BeautifulSoup(raw, 'html.parser')
    base = f'https://vitgroup.ru/{slug}'
    links = asyncio.run(crawler._extract_additional_links(soup, base))
    found = {url for url, category, _ in links if category == 'product' and '#!/tproduct/' in url}
    expected = set()
    for product in soup.select('.t-popup [data-product-lid]'):
        rec = product.find_parent(id=lambda value: value and value.startswith('rec'))
        cards = rec.select(f'[data-product-lid="{product["data-product-lid"]}"]')
        if any(card.select_one('a[href="#prodpopup"]') for card in cards):
            expected.add(f'{base}#!/tproduct/{rec["id"][3:]}-{product["data-product-lid"]}')
    assert expected and found == expected
    assert not is_unrendered_store_listing(raw)


def test_static_popups_do_not_mask_another_unloaded_store_grid():
    raw = (FIXTURES / 'viteco.html').read_text()
    mixed = raw.replace('</body>', '<div class="js-store-grid-cont"></div></body>')
    assert is_unrendered_store_listing(mixed)


def test_unknown_product_does_not_return_catalog(crawler):
    raw = (FIXTURES / 'viteco.html').read_text()
    crawler._http_first_get.return_value = raw
    crawler._fetch_with_aiohttp.return_value = raw
    result = asyncio.run(crawler.fetch_page('https://vitgroup.ru/viteco#!/tproduct/55040791-999', 'product'))
    assert result.reason != 'ok' and result.reason == 'incomplete_render'


def test_fragment_links_discovered_but_ordinary_anchors_ignored(crawler):
    soup = BeautifulSoup('<a href="#!/tproduct/123-456">Грунт</a><a href="#specs">Характеристики</a>', 'html.parser')
    links = asyncio.run(crawler._extract_additional_links(soup, 'https://vitgroup.ru/viteco'))
    assert links == [('https://vitgroup.ru/viteco#!/tproduct/123-456', 'product', crawler.url_categorizer.priority_levels['product'])]


@pytest.mark.parametrize('attributes', [
    'data-toggle="collapse" data-target="#specs"',
    'data-bs-toggle="collapse" data-bs-target="#specs"',
    'aria-expanded="false" aria-controls="specs"',
    'data-toggle="modal" data-target="#specs"',
    'aria-haspopup="dialog" aria-controls="specs"',
])
def test_panels_require_loading_and_survive_noise_cleaning(attributes):
    html = ('<html><body><main><h1>Насос</h1><p>Насос для подачи воды.</p>'
            f'<button {attributes}>Характеристики</button></main>'
            '<div class="modal" id="specs" style="display:none">{content}</div>'
            '<div class="modal" id="advert">РЕКЛАМА 999</div></body></html>')
    assert has_unloaded_product_tabs(html.format(content='Загрузка...'))
    filled = html.format(content='<p>Напор 125 м. Масса 73 кг.</p>')
    assert not has_unloaded_product_tabs(filled)
    md = html_to_markdown(filled, 'https://factory.test/product/pump')
    assert 'Напор 125 м' in md and 'Масса 73 кг' in md
    assert 'РЕКЛАМА' not in md


def test_details_keeps_content_without_clicking():
    html = '<main><h1>Насос</h1><details><summary>Характеристики</summary><p>Напор 125 м</p></details></main>'
    assert not has_unloaded_product_tabs(html)
    assert 'Напор 125 м' in html_to_markdown(html, 'https://factory.test/product/pump')


def test_non_product_controls_do_not_trigger_browser():
    assert not has_unloaded_product_tabs('<button aria-expanded="false" aria-controls="menu">Меню</button>')
    assert not has_unloaded_product_tabs('<button data-toggle="modal" data-target="#order">Заказать</button>')


def test_heading_and_close_button_do_not_confirm_loaded_panel():
    html = ('<button data-toggle="modal" data-target="#specs">Характеристики</button>'
            '<div id="specs"><h2>Характеристики</h2><button>Закрыть</button></div>')
    assert has_unloaded_product_tabs(html)


def test_nested_modal_wrappers_keep_data_without_menu_or_form():
    html = ('<main><h1>Насос</h1><button data-toggle="modal" data-target="#specs">Характеристики</button></main>'
            '<div class="modal" id="specs" style="display:none"><div class="modal-dialog">'
            '<div class="modal-content"><div class="modal-body">Напор 125 м</div></div></div></div>'
            '<nav><button aria-expanded="false" aria-controls="menu">Документы</button></nav>'
            '<form><button data-toggle="modal" data-target="#order">Описание заказа</button></form>')
    assert not has_unloaded_product_tabs(html)
    assert 'Напор 125 м' in html_to_markdown(html, 'https://factory.test/product/pump')


def test_two_same_label_panels_require_both_snapshots():
    html = ('<button aria-expanded="false" aria-controls="a">Характеристики</button><div id="a"></div>'
            '<button aria-expanded="false" aria-controls="b">Характеристики</button><div id="b"></div>'
            '<section data-product-panel="a" data-tab="Характеристики">Масса 73 кг</section>')
    assert has_unloaded_product_tabs(html)
    assert not has_unloaded_product_tabs(html + '<section data-product-panel="b">Напор 125 м</section>')


def test_storage_identity_without_custom_normalizer(tmp_path):
    storage = TempStorageManager(str(tmp_path), config=Config())
    first = 'https://vitgroup.ru/viteco#!/tproduct/123-456'
    second = 'https://vitgroup.ru/viteco#!/tproduct/123-789'
    assert storage._metadata_identity({'url': first}) != storage._metadata_identity({'url': second})


@pytest.mark.parametrize('final', ['https://vitgroup.ru/viteco#!/tproduct/55040791-1497456130776',
                                  'https://vitgroup.ru/elsewhere'])
def test_wrong_redirect_target_cannot_replace_requested_product(crawler, final):
    url = 'https://vitgroup.ru/viteco#!/tproduct/55040791-1498486301712'
    raw = (FIXTURES / 'viteco.html').read_text()
    crawler._http_first_get.return_value = wc.PageContent(raw, url, final, method='http_first')
    outcome = asyncio.run(crawler.fetch_page(url, 'product'))
    assert outcome.content is None and outcome.reason == 'incomplete_render'


def test_product_windows_have_independent_saved_versions_and_checkpoints(crawler):
    async def run():
        company = 'Витгрупп тест'
        await crawler.reset_state(company, crawl_run_id='popup-test')
        crawler.current_base_url = 'https://vitgroup.ru/'
        raw = (FIXTURES / 'viteco.html').read_text()
        urls = ['https://vitgroup.ru/viteco#!/tproduct/55040791-' + lid
                for lid in ('1498486301712', '1497456130776', '1498486363994')]
        paths = []
        for url in urls:
            crawler._http_first_get.return_value = raw
            outcome = await crawler.fetch_page(url, 'product')
            assert outcome.content is not None
            normalized = crawler.url_normalizer.normalize_url(url)
            path, _, meta = await crawler.temp_storage.save_cleaned_html(
                url, normalized, outcome.content.html, company, category='product')
            assert meta['normalized_url'] == url
            paths.append(path)
        assert len(set(paths)) == 3
        await crawler.temp_storage.mark_as_ai_processed(paths[0])
        assert await crawler.temp_storage.is_page_processed(urls[0], company, 'product')
        assert not await crawler.temp_storage.is_page_processed(urls[1], company, 'product')
        assert len(await crawler.temp_storage.get_company_files(company)) == 3
    asyncio.run(run())


@pytest.mark.parametrize('terminal', [True, False])
def test_scope_failure_after_incomplete_http_has_correct_priority(crawler, terminal):
    url = 'https://vitgroup.ru/product/pump'
    async def fetch(*args):
        if terminal:
            observe_fetch(url, 'http_first', reason='incomplete_render')
        observe_fetch(url, 'ajax_tabs', final_url='https://outside.invalid/product', reason='out_of_scope')
        if not terminal:
            observe_fetch(url, 'ajax_tabs', reason='incomplete_render')
        return None
    crawler._fetch_page_content = fetch
    result = asyncio.run(crawler.fetch_page(url, 'product'))
    assert result.reason == ('out_of_scope' if terminal else 'incomplete_render')
    assert result.retryable is (not terminal)
