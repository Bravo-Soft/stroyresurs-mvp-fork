"""P04 U2: распознавание товарных и каталожных URL.

Табличный тест на контрольных URL досье пакета P04 — по каждому коду дефекта
позитивный случай (страница должна получить роль 'product') и контрольный
(не должна). Сети не требует: categorize_url — чистая функция от строки URL.

Коды: D145 (завершающий слеш, 'item' внутри 'sitemap'), D117 (кириллица и
percent-encoding), D223 (транслит-варианты «продукция»), D153 (sizes/razmer),
D237 ('shop' внутри 'shoppingcart'), D228 (лист .htm + якорь ссылки),
D284 (ведущий числовой id ЧПУ), D274 (сегмент 'cat' + идентификатор в query),
D127 (адресация товара в query).
"""
import pytest

from config import Config
from web_crawler import URLCategorizer


@pytest.fixture
def categorizer():
    cat = URLCategorizer(Config())
    cat.set_profile(None)
    return cat


# (код, URL, текст ссылки, ожидаемая роль)
PRODUCT_CASES = [
    # --- D145: завершающий слеш ломал якорные паттерны '...$' ---
    ('D145', 'https://karaon.ru/prod/terrasa/terrasnaya-doska-deking/', '', 'product'),
    ('D145', 'https://karaon.ru/prod/terrasa/terrasnaya-doska-deking', '', 'product'),
    ('D145', 'https://karaon.ru/prod/zabor/shtaketnik-metallicheskiy/', '', 'product'),
    # --- D117: кириллица и percent-encoding в пути ---
    ('D117', 'http://etnann.ru/izdeliya/gajki/', '', 'product'),
    ('D117', 'http://etnann.ru/izdelija/bolty/', '', 'product'),
    ('D117', 'https://zingametall.ru/%D0%B0%D1%81%D1%81%D0%BE%D1%80%D1%82%D0%B8'
             '%D0%BC%D0%B5%D0%BD%D1%82-%D0%BF%D1%80%D0%BE%D0%B4%D1%83%D0%BA'
             '%D1%86%D0%B8%D0%B8/', 'АССОРТИМЕНТ ПРОДУКЦИИ', 'product'),
    ('D117', 'https://a.ru/%D0%BF%D1%80%D0%BE%D0%B4%D1%83%D0%BA%D1%86%D0%B8%D1%8F/'
             '%D0%B3%D0%B0%D0%B9%D0%BA%D0%B8/', '', 'product'),
    ('D117', 'https://a.ru/изделия/bolty/', '', 'product'),
    ('D117', 'https://smit-ufa.ru/category/catalog/truby/', '', 'product'),
    # --- D223: транслит-варианты раздела «продукция» ---
    ('D223', 'https://teply-dom.ru/produkciya/proizvodstvo-nesemnoj-opalubki/', '', 'product'),
    ('D223', 'https://sib-centr.ru/produkcziya/rigeli/', '', 'product'),
    ('D223', 'https://senardgy.ru/produkcia/kotly/', '', 'product'),
    ('D223', 'https://steelpaint-rf.ru/produkcija/', '', 'product'),
    ('D223', 'https://a.ru/produktsiya/bloki/', '', 'product'),
    # --- D153: sizes / razmer / tiporazmer ---
    ('D153', 'https://yardos.ru/sizes/kran-sharovoy-flantsevyy/', '', 'product'),
    ('D153', 'https://a.ru/razmer/truba-102/', '', 'product'),
    ('D153', 'https://a.ru/tiporazmer/zadvizhka/', '', 'product'),
    ('D153', 'https://yardos.ru/products/kran-sharovoy-flantsevyy/', '', 'product'),
    # --- D284: ведущий числовой идентификатор ЧПУ (PrestaShop id-rewrite) ---
    ('D284', 'http://www.zgbi7.ru/28-fundamentnye-bloki', 'Фундаментные блоки', 'product'),
    ('D284', 'http://www.zgbi7.ru/28-fundamentnye-bloki/', '', 'product'),
    ('D284', 'http://www.zgbi7.ru/catalog/28-fundamentnye-bloki', '', 'product'),
    # --- D274: сегмент 'cat' + идентификатор товара в query ---
    ('D274', 'https://www.proterm.ru/cat/?p_id=89', 'NO 420', 'product'),
    ('D274', 'https://belsteel.com/buyer/product.php?id=17', '', 'product'),
    # --- D127: адресация товара в query ---
    ('D127', 'http://te-s.msk.ru/index.php?product_ID=310', '', 'product'),
    ('D127', 'http://a.ru/index.php?tovar_id=5', '', 'product'),
    ('D127', 'http://a.ru/index.php?itemid=7', '', 'product'),
    ('D127', 'http://a.ru/index.php?goodsid=9', '', 'product'),
    ('D127', 'http://a.ru/index.php?prod_id=11', '', 'product'),
    # --- D228: лист .htm в корне + явный товарный якорь ---
    ('D228', 'http://borglaz.narod.ru/steklo.htm', 'подробнее о товаре', 'product'),
    ('D228', 'http://borglaz.narod.ru/zerkalo.htm', 'Технические характеристики', 'product'),
    ('D228', 'http://a.ru/plita.html', 'Карточка товара', 'product'),
]

NEGATIVE_CASES = [
    # --- D145: 'item' внутри 'sitemap' больше не делает карту сайта товаром ---
    ('D145', 'https://www.tehnokrat-omsk.ru/sitemap.html', '', 'product'),
    ('D145', 'https://aeronik.ru/sitemap', '', 'product'),
    ('D145', 'https://chillers-carrier.ru/page-sitemap.html', '', 'product'),
    # --- D237: 'shop' внутри 'shoppingcart' больше не делает корзину товаром ---
    ('D237', 'http://www.enpiter.ru/shoppingcart', '', 'product'),
    ('D237', 'https://a.ru/shopping-cart/', '', 'product'),
    ('D237', 'https://a.ru/basket/', '', 'product'),
    ('D237', 'https://a.ru/checkout/', '', 'product'),
    ('D237', 'https://a.ru/catalog/order/', '', 'product'),
    # --- D127: листинговые параметры query — это каталог, а не товар ---
    ('D127', 'http://te-s.msk.ru/index.php?categoryID=94', '', 'product'),
    ('D127', 'http://a.ru/index.php?cPath=12', '', 'product'),
    ('D127', 'http://a.ru/index.php?pt_id=3', '', 'product'),
    ('D127', 'http://a.ru/index.php?section_id=8', '', 'product'),
    # --- Страховка от шума: снятие завершающего слеша не должно красить
    #     служебные двухсегментные страницы в товар (замер на корпусе прогона) ---
    ('шум', 'https://docke.ru/where-buy/almaty/', '', 'product'),
    ('шум', 'https://gree-air.ru/dillers/dillers/', '', 'product'),
    ('шум', 'https://gbisib.ru/o-kompanii/partnyory/', '', 'product'),
    ('шум', 'https://oikos-butik.ru/categories/10/', '', 'product'),
    ('шум', 'https://pufas.com/mldv/kontakt/', '', 'product'),
    # --- 'cat' — только целым сегментом (иначе матчится внутри location и т. п.) ---
    ('D274', 'https://a.ru/location/moskva/', '', 'category'),
    ('D274', 'https://a.ru/certificates-list/', '', 'category'),
]

LISTING_CASES = [
    # Листинговые параметры query дают роль «категория», а не «другое»
    ('D127', 'http://te-s.msk.ru/index.php?categoryID=94', 'category'),
    ('D127', 'http://a.ru/index.php?cPath=12', 'category'),
    ('D127', 'http://a.ru/index.php?pt_id=3', 'category'),
]


@pytest.mark.parametrize('code,url,text,expected', PRODUCT_CASES)
def test_product_urls_recognized(categorizer, code, url, text, expected):
    role, priority = categorizer.categorize_url(url, text)
    assert role == expected, f"{code}: {url} -> {role}"
    assert priority == categorizer.priority_levels['product']


@pytest.mark.parametrize('code,url,text,forbidden', NEGATIVE_CASES)
def test_non_product_urls_not_promoted(categorizer, code, url, text, forbidden):
    role, _ = categorizer.categorize_url(url, text)
    assert role != forbidden, f"{code}: {url} -> {role}"


@pytest.mark.parametrize('code,url,expected', LISTING_CASES)
def test_listing_query_params_are_category(categorizer, code, url, expected):
    role, _ = categorizer.categorize_url(url)
    assert role == expected, f"{code}: {url} -> {role}"


def test_normalize_path_unquote_casefold_and_trailing_slash(categorizer):
    assert categorizer._normalize_path('/Catalog/Truby/') == '/catalog/truby'
    assert categorizer._normalize_path('/%D0%B3%D0%B0%D0%B9%D0%BA%D0%B8/') == '/гайки'
    assert categorizer._normalize_path('/') == '/'
    assert categorizer._normalize_path('') == '/'


def test_service_segments_never_product(categorizer):
    """Служебный стоп-лист сверяется и с именем последнего сегмента без расширения."""
    assert categorizer._is_product_url('https://a.ru/sitemap.html') is False
    assert categorizer._is_product_url('https://a.ru/shoppingcart') is False
    assert categorizer._is_product_url('https://a.ru/catalog/cart/') is False


def test_link_text_anchor_is_narrow(categorizer):
    """Одиночное «подробнее» товарным якорем НЕ считается (стоит и у новостей)."""
    assert categorizer._is_product_url('http://a.ru/steklo.htm', 'подробнее') is False
    assert categorizer._is_product_url('http://a.ru/steklo.htm', 'подробнее о товаре') is True


# ==================== Сигнал «плоский сайт» (observe_urls) ====================

FLAT_URLS = ['http://a.ru/filtr/', 'http://a.ru/nasos/', 'http://a.ru/separ/',
             'http://a.ru/cikl/', 'http://a.ru/steklo.htm', 'http://a.ru/kontakty/']


def test_observe_urls_detects_flat_site(categorizer):
    assert categorizer.observe_urls(FLAT_URLS) is True
    assert categorizer.flat_site is True
    # односегментный слаг-директория и лист .htm в корне становятся товарами
    assert categorizer.categorize_url('http://a.ru/filtr/')[0] == 'product'
    assert categorizer.categorize_url('http://a.ru/steklo.htm')[0] == 'product'
    # служебные слаги плоского сайта товарами не становятся
    assert categorizer._is_product_url('http://a.ru/index.html') is False
    assert categorizer._is_product_url('http://a.ru/karta-sayta/') is False


def test_observe_urls_not_flat_when_catalog_segment_present(categorizer):
    urls = FLAT_URLS + ['http://a.ru/catalog/nasosy/']
    assert categorizer.observe_urls(urls) is False
    assert categorizer.categorize_url('http://a.ru/filtr/')[0] == 'other'


def test_observe_urls_needs_minimum_sample(categorizer):
    """На двух URL признак не включается: порог config.flat_site_min_urls."""
    assert categorizer.observe_urls(['http://a.ru/filtr/', 'http://a.ru/nasos/']) is False
    assert categorizer.flat_site is False


def test_set_profile_resets_flat_site(categorizer):
    """Флаг плоского сайта не протекает на следующую компанию."""
    categorizer.observe_urls(FLAT_URLS)
    assert categorizer.flat_site is True
    categorizer.set_profile(None)
    assert categorizer.flat_site is False
