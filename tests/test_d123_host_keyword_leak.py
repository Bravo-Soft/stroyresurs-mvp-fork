"""D123: ключевое слово внутри доменного имени красило весь сайт в одну категорию.

Beaulieu of America (floordealer.ru): слово 'dealer' из списка distributor_keywords
входит подстрокой в КАЖДЫЙ URL сайта, а дистрибьюторы проверяются раньше товаров —
все 420 сохранённых страниц ушли в Distributor_pages, product_pages = 0.
Проверяем, что теперь ключевые слова ищутся в пути, а разделы в пути по-прежнему находятся.
"""
import pytest

from config import Config
from web_crawler import URLCategorizer


@pytest.fixture
def categorizer():
    return URLCategorizer(Config())


# ==================== слово в домене больше не решает ====================

def test_dealer_in_domain_does_not_hijack_product(categorizer):
    """floordealer.ru: товарная страница остаётся товарной"""
    cat, _ = categorizer.categorize_url(
        'https://floordealer.ru/catalog/kovrovaya-plitka/products/balsan_nexus_940_269123')
    assert cat == 'product'


@pytest.mark.parametrize('url, not_cat', [
    ('https://floordealer.ru/about/',                 'distributor'),   # 'dealer' в домене
    ('https://stroycompany.ru/catalog/blok-d400/',    'contacts'),      # 'company' в домене
    ('https://ceny-stroy.ru/catalog/plita/',          'price_list'),    # 'ceny' в домене
    ('https://katalog-plus.ru/o-nas/',                'category'),      # 'katalog' в домене
])
def test_keyword_in_domain_ignored(categorizer, url, not_cat):
    cat, _ = categorizer.categorize_url(url)
    assert cat != not_cat, f'{url} -> {cat}'


def test_company_in_domain_does_not_block_product(categorizer):
    """'company' в домене не должен запрещать товарную классификацию (_is_product_url)"""
    assert categorizer._is_product_url('https://stroycompany.ru/catalog/blok/blok-d400-600x300/')


# ==================== разделы в ПУТИ по-прежнему находятся ====================

@pytest.mark.parametrize('url, cat', [
    ('https://example.ru/dealers/',             'distributor'),
    ('https://example.ru/gde-kupit/',           'distributor'),
    ('https://example.ru/partnery/',            'distributor'),
    ('https://example.ru/contacts/',            'contacts'),
    ('https://example.ru/price-list/',          'price_list'),
])
def test_section_in_path_still_detected(categorizer, url, cat):
    got, _ = categorizer.categorize_url(url)
    assert got == cat, f'{url} -> {got}'


def test_link_text_signal_preserved(categorizer):
    """текст ссылки «дилеры» по-прежнему даёт категорию дистрибьюторов"""
    cat, _ = categorizer.categorize_url('https://example.ru/info/page7/', link_text='Дилеры')
    assert cat == 'distributor'
