from copy import deepcopy
import pytest
from source_detail_fields import restore_source_detail_fields
from product_preparation import count_numeric_characteristics


FACT = 'Визуальная часть установки выполнена в бело-черном дизайне 2VV, часть корпуса, предназначенная для скрытия в подвесном потолке, оставлена в оцинкованном исполнении без покраски.'
HTML = '<h1>ALFA</h1><div class="tm-product-body"><p>' + FACT + '</p></div>'


def test_visible_material_is_preserved_without_adding_numeric_specs():
    data = {'product': {'specifications': {'Расход': '3200 м³/ч', 'Высота': '598 мм', 'Шум': '59 дБ'},
                       'additional_info': {'text': 'произвольный остаток'}}}
    assert restore_source_detail_fields(data, HTML, 'https://www.2vv.cz/ru/продукты/alfa/')
    assert FACT in data['product']['specifications']['Конструкция']
    assert count_numeric_characteristics(data['product']) == 3
    assert data['product']['additional_info']['text'] == 'произвольный остаток'
    before = deepcopy(data)
    assert not restore_source_detail_fields(data, HTML, 'https://2vv.cz/ru/продукты/alfa/')
    assert data == before


@pytest.mark.parametrize('html,url', [
    (HTML, 'https://other.test/product/'),
    (HTML.replace('<p>', '<p hidden>'), 'https://2vv.cz/produkty/a'),
    (HTML.replace('<p>', '<p style="display:none">'), 'https://2vv.cz/produkty/a'),
    (HTML.replace('tm-product-body', 'unrelated'), 'https://2vv.cz/produkty/a'),
    ('<h1>ALFA</h1><div class="tm-product-body"></div><script>' + FACT + '</script>', 'https://2vv.cz/produkty/a'),
    (HTML + HTML, 'https://2vv.cz/produkty/a'),
])
def test_wrong_context_and_invisible_facts_are_not_used(html, url):
    data = {'product': {'specifications': {}}}
    before = deepcopy(data)
    assert not restore_source_detail_fields(data, html, url)
    assert data == before


def test_negative_model_result_never_becomes_a_card():
    data = {'error': 'Trash_418#'}
    assert not restore_source_detail_fields(data, HTML, 'https://2vv.cz/produkty/a')
    assert data == {'error': 'Trash_418#'}


def test_czech_casing_is_not_lost_or_treated_as_a_color_variant():
    fact = 'Pohledová část jednotky je ve 2VV bílo-černém designu, část pláště určena do podhledu je ponechána v pozinkovém provedení bez laku.'
    html = HTML.replace(FACT, fact)
    data = {'product': {'specifications': {}, 'colors': ['bílo-černý design', 'pozinkované provedení (část pláště)']}}
    assert restore_source_detail_fields(data, html, 'https://2vv.cz/produkty/alfa/')
    assert fact in data['product']['specifications']['Конструкция']
    assert data['product']['colors'] == ['bílo-černý design']
    assert count_numeric_characteristics(data['product']) == 0
