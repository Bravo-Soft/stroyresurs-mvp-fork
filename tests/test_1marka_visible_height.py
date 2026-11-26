from copy import deepcopy
import pytest
from source_detail_fields import restore_source_detail_fields
from product_preparation import count_numeric_characteristics

HTML = '''<a class="tab-link" data-target="#prod-detail-chars">Характеристики</a>
<div id="prod-detail-chars" style="display:none"><div class="prod-detail-chars__row">
<div class="prod-detail-chars__label">Дополнительная информация</div>
<div class="prod-detail-chars__value">регулируемая высота 60-62 см</div>
</div></div>'''


def test_public_adjustment_range_survives_generic_source_label():
    data = {'product': {'specifications': {'Высота, см': '61'},
                       'additional_info': {'text': 'регулируемая высота 60-62 см'}}}
    assert restore_source_detail_fields(data, HTML, 'https://1marka.ru/catalog/vanny/model/')
    assert '60-62 см' in data['product']['exploitation']
    assert count_numeric_characteristics(data['product']) == 1
    before = deepcopy(data)
    assert not restore_source_detail_fields(data, HTML, 'https://1marka.ru/catalog/vanny/model/')
    assert data == before


@pytest.mark.parametrize('html,url', [
    (HTML.replace('class="tab-link"', 'hidden class="tab-link"'), 'https://1marka.ru/catalog/a/b'),
    (HTML.replace('class="prod-detail-chars__row"', 'hidden class="prod-detail-chars__row"'), 'https://1marka.ru/catalog/a/b'),
    (HTML.replace('регулируемая высота 60-62 см', 'габариты соседней модели 60-62 см'), 'https://1marka.ru/catalog/a/b'),
    (HTML, 'https://other.ru/catalog/a/b'),
])
def test_invisible_unrelated_or_foreign_facts_are_not_restored(html, url):
    data = {'product': {'specifications': {}}}
    before = deepcopy(data)
    assert not restore_source_detail_fields(data, html, url)
    assert data == before
