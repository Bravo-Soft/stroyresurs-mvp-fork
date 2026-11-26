"""P04 U3: тупиковые роли, порядок проверок и второй проход извлечения.

Текст ссылки стал слабым сигналом (смотрится только когда путь роли не дал),
сильный товарный сигнал пути бьёт префикс '/price/', служебные шаблоны uCoz
никогда не товары, роли category/price_list сохраняются и доступны второму проходу
товарного извлечения.

Коды: D168 (корень раздела продукции в тупиковой роли category), D181 ('/price/'
раньше товаров), D138 (якорь ссылки равноправен пути).
"""
import ast
import asyncio
import inspect
import re

import pytest

from config import Config
from main import MonitoringSystem
from web_crawler import URLCategorizer, WebCrawler


@pytest.fixture
def categorizer():
    cat = URLCategorizer(Config())
    cat.set_profile(None)
    return cat


# ==================== п.1: текст ссылки — слабый сигнал (D138) ================

def test_d138_anchor_does_not_override_path_role(categorizer):
    """Якорь «Цены и контакты» больше не уводит прайс-лист в contacts."""
    url = 'https://www.motorino-granit.narod.ru/price.html'
    assert categorizer.categorize_url(url, 'Цены и контакты')[0] == 'price_list'
    assert categorizer.categorize_url(url)[0] == 'price_list'


def test_d138_buy_button_in_product_tile(categorizer):
    """Кнопка «Где купить» внутри плитки каталога не уводит карточку в distributor."""
    assert categorizer.categorize_url(
        'https://isolon.ru/catalog/isolon-ppe/isolon-500/', 'Изолон 500 Где купить')[0] == 'product'


def test_link_text_still_used_when_path_is_silent(categorizer):
    """Слабый сигнал остаётся сигналом: путь роли не дал — слушаем якорь."""
    assert categorizer.categorize_url('https://a.ru/page5.html', 'Контакты')[0] == 'contacts'
    assert categorizer.categorize_url('https://a.ru/page7.html', 'Где купить')[0] == 'distributor'
    assert categorizer.categorize_url('https://a.ru/page9.html', 'Прайс-лист')[0] == 'price_list'


# ==================== п.2: сильный товарный путь бьёт /price/ (D181) =========

def test_d181_product_path_beats_price_prefix(categorizer):
    assert categorizer.categorize_url('https://itron.nt-rt.ru/price/product/235255')[0] == 'product'
    assert categorizer.categorize_url('https://itron.nt-rt.ru/catalog/product/235255')[0] == 'product'
    assert categorizer.categorize_url('https://a.ru/price/tovar/betonnyy-blok/')[0] == 'product'


def test_price_list_role_preserved_without_product_segment(categorizer):
    """Настоящий прайс-лист остаётся прайс-листом."""
    assert categorizer.categorize_url('http://oao-skai.ru/price-plita.htm')[0] == 'price_list'
    assert categorizer.categorize_url('https://a.ru/price/')[0] == 'price_list'
    assert categorizer.categorize_url('https://a.ru/pricelist.pdf')[0] == 'excluded'


def test_has_strong_product_path(categorizer):
    assert categorizer._has_strong_product_path(['price', 'product', '235255']) is True
    assert categorizer._has_strong_product_path(['price', 'product']) is False
    assert categorizer._has_strong_product_path(['price', 'plita']) is False


# ==================== п.4: служебные пути uCoz (D228) ========================

@pytest.mark.parametrize('path', ['/index/0-2', '/index/0-3/', '/gb', '/gb/', '/register'])
def test_ucoz_service_paths_never_product(categorizer, path):
    assert categorizer._is_product_url(f'https://borglaz.narod.ru{path}') is False


def test_ucoz_stoplist_does_not_touch_real_slugs(categorizer):
    """Стоп-лист привязан к началу пути и не задевает товарные слаги."""
    assert categorizer.categorize_url('https://a.ru/catalog/index-plita-200/')[0] == 'product'


# ==================== п.3: category/price_list больше не тупиковые ===========

def test_storage_gate_accepts_category_and_price_list():
    """Гейт сохранения страниц (_process_page_with_storage) пропускает новые роли."""
    source = inspect.getsource(WebCrawler._process_page_with_storage)
    match = re.search(r'target_categories = (\{[^}]*\})', source)
    assert match, 'литерал target_categories не найден'
    assert ast.literal_eval(match.group(1)) == {
        'product', 'contacts', 'distributor', 'main_page', 'category', 'price_list'}


class _SecondPassStub:
    """Минимальный self для MonitoringSystem._second_pass_product_extraction."""

    _second_pass_product_extraction = MonitoringSystem._second_pass_product_extraction

    def __init__(self, config, products):
        self.config = config
        self._global_processed_urls = set()
        self._products = products
        self.seen_pages = None

    async def process_stored_pages_with_checkpoints(self, pages, *args):
        self.seen_pages = pages
        return self._products


PAGES = [
    {'category': 'product', 'normalized_url': 'https://a.ru/p1', 'file_path': 'p1'},
    {'category': 'category', 'normalized_url': 'https://a.ru/produkcija/', 'file_path': 'c1'},
    {'category': 'price_list', 'normalized_url': 'https://a.ru/price.html', 'file_path': 'c2'},
    {'category': 'contacts', 'normalized_url': 'https://a.ru/contacts/', 'file_path': 'c3'},
]


def test_second_pass_retries_dead_end_roles():
    stub = _SecondPassStub(Config(), [{'product_name': 'Эмаль'}])
    stub._global_processed_urls = {'https://a.ru/produkcija/', 'https://a.ru/price.html'}
    result = asyncio.run(stub._second_pass_product_extraction(PAGES, {}, None, ''))
    assert result == [{'product_name': 'Эмаль'}]
    # во второй проход ушли только тупиковые роли, с подменённой ролью 'product'
    assert [p['normalized_url'] for p in stub.seen_pages] == [
        'https://a.ru/produkcija/', 'https://a.ru/price.html']
    assert all(p['category'] == 'product' for p in stub.seen_pages)
    # отметка «уже обработан» снята, иначе дедуп пропустит страницу
    assert stub._global_processed_urls == set()


def test_second_pass_respects_page_ceiling():
    config = Config()
    config.second_pass_max_pages = 1
    stub = _SecondPassStub(config, [])
    asyncio.run(stub._second_pass_product_extraction(PAGES, {}, None, ''))
    assert len(stub.seen_pages) == 1


def test_second_pass_disabled_by_zero_ceiling():
    config = Config()
    config.second_pass_max_pages = 0
    stub = _SecondPassStub(config, [{'product_name': 'Эмаль'}])
    assert asyncio.run(stub._second_pass_product_extraction(PAGES, {}, None, '')) == []
    assert stub.seen_pages is None


def test_second_pass_noop_without_candidates():
    stub = _SecondPassStub(Config(), [])
    only_products = [PAGES[0]]
    assert asyncio.run(stub._second_pass_product_extraction(only_products, {}, None, '')) == []
    assert stub.seen_pages is None
