import copy
import pytest
from product_quality import normalize_product_fields
from product_preparation import count_numeric_characteristics


def test_live_packaging_fields_are_visible_and_do_not_inflate_gate():
    data = {'product': {'specifications': {'Длина': '155 см', 'Ширина': '70 см', 'Высота': '61 см'},
        'complectation': '- Материал упаковки: картон',
        'additional_info': {'text': 'Длина упаковки, м: 1,57; Ширина упаковки, м: 0,72; Глубина упаковки, м: 0,45; Объем в упаковке, м^3: 0,509; Вес в упаковке, кг: 21'}}}
    normalize_product_fields(data)
    product = data['product']
    assert product['additional_info']['text'] == ''
    assert 'Вес в упаковке, кг: 21' in product['complectation']
    assert '0,509' in product['complectation']
    assert 'картон' in product['complectation']
    assert count_numeric_characteristics(product) == 3
    before = copy.deepcopy(data)
    normalize_product_fields(data)
    assert data == before


def test_preserves_unclassified_remainder_and_routes_only_explicit_label():
    data = {'product': {'specifications': {'Вес, кг': '22,3', 'Вес в упаковке, кг': '24,2'},
        'additional_info': {'text': 'Примечание: A; B; Длина упаковки, м: 1,07; неопределённый остаток'}}}
    normalize_product_fields(data)
    p = data['product']
    assert p['specifications'] == {'Вес, кг': '22,3'}
    assert 'Вес в упаковке, кг: 24,2' in p['complectation']
    assert p['additional_info']['text'] == 'Примечание: A; B; Длина упаковки, м: 1,07; неопределённый остаток'


@pytest.mark.parametrize('text', ['Длина упаковки, м:', 'В тексте упомянута масса упаковки: неизвестно',
    'Ширина изделия, см: 70', 'Расстояние между упаковками: 10 м'])
def test_ambiguous_or_product_fields_remain_unclassified(text):
    data = {'product': {'additional_info': {'text': text}}}
    normalize_product_fields(data)
    assert data['product']['additional_info']['text'] == text
    assert not data['product'].get('complectation')


def test_invalid_target_and_rejected_page_are_not_overridden():
    data = {'product': {'complectation': ['unexpected'], 'additional_info': {'text': 'Длина упаковки, м: 1,07'}}}
    normalize_product_fields(data)
    assert data['product']['additional_info']['text'] == 'Длина упаковки, м: 1,07'
    data['error'] = 'Trash_418#'
    before = copy.deepcopy(data)
    normalize_product_fields(data)
    assert data == before
