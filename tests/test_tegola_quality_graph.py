"""Варианты должны переживать входной контракт графа, не меняя исходные данные."""
from copy import deepcopy
from graph_db_uploader import GraphDBDataFormatter


def format_variants(variants, product_id='product_1'):
    data = GraphDBDataFormatter.format_product_data('925700928', 'Tegola', [
        {'product_id': product_id, 'ai_raw_text': {'product': {'variants': variants}}}])
    return data['companies'][0]['products'][0]['variants']


def test_variants_have_stable_string_ids_independent_of_order():
    source = [{'Цвет': 'Коричневый', 'Другие параметры': 'Артикул: 123'},
              {'Цвет': 'Серый', 'Другие параметры': 'Артикул: 456'}]
    original = deepcopy(source)
    result = format_variants(source)
    assert all(isinstance(v['id'], str) and v['id'] for v in result)
    assert len({v['id'] for v in result}) == 2
    assert format_variants(source[::-1]) == result[::-1]
    assert source == original
    assert format_variants(source, 'product_2')[0]['id'] != result[0]['id']
    assert [{k: v for k, v in x.items() if k != 'id'} for x in result] == source


def test_existing_variant_ids_are_preserved():
    source = [{'id': 'source_variant_9', 'Цвет': 'Синий'}]
    assert format_variants(source) == source
    assert format_variants([]) == []
