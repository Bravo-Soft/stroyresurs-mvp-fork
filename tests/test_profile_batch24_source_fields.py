from source_detail_fields import restore_source_detail_fields
import pytest

ASCOLD = '<div class="product-info__row"><div class="product-info__label">Обозначение по чертежу</div><div class="product-info__value">ИТШЛ.364.844.002 (ВН557-66)</div></div>'
BELCOLOR = '<div class="bx_item_description"><b>ХАРАКТЕРИСТИКА</b>. Без морщин и посторонних включений.<br><b>ХРАНЕНИЕ</b>. От минус 40 °С до плюс 40 °С.</div>'

def test_drawing_designation_survives_outside_additional_info():
    data = {'product': {'specifications': {}, 'additional_info': {'text': 'ВН557-66'}}}
    assert restore_source_detail_fields(data, ASCOLD, 'http://ascold.ru/catalog/a/b/c/')
    assert data['product']['specifications']['Обозначение по чертежу'] == 'ИТШЛ.364.844.002 (ВН557-66)'
    assert not restore_source_detail_fields(data, ASCOLD, 'http://ascold.ru/catalog/a/b/c/')

def test_paint_sections_keep_full_conditions():
    data = {'product': {'description': 'Гладкое покрытие', 'storage': ''}}
    assert restore_source_detail_fields(data, BELCOLOR, 'http://belcolor.ru/catalog/a/b.html')
    assert data['product']['description'] == 'Без морщин и посторонних включений.'
    assert data['product']['storage'] == 'От минус 40 °С до плюс 40 °С.'

@pytest.mark.parametrize('html,url', [(ASCOLD, 'https://other.ru/catalog/a/'), (BELCOLOR, 'https://belcolor.ru/info/a/'), ('<div hidden>'+ASCOLD+'</div>', 'https://ascold.ru/catalog/a/'), ('<div hidden>'+BELCOLOR+'</div>', 'https://belcolor.ru/catalog/a/')])
def test_unrelated_or_hidden_source_is_not_restored(html, url):
    data = {'product': {'description': '', 'specifications': {}}}
    assert not restore_source_detail_fields(data, html, url)
    assert data == {'product': {'description': '', 'specifications': {}}}

def test_rejection_not_overridden():
    data = {'error': 'Trash_418#'}
    assert not restore_source_detail_fields(data, ASCOLD, 'http://ascold.ru/catalog/a/')

def test_gbi_series_and_plate_compatibility_survive():
    html = '<div class="text_after_items"><table><caption>Перемычки брусковые 1.038.1-1</caption></table><p>Лоток имеет П-образную форму, в комплексе с бетонными плитами защищает коммуникации.</p></div>'
    data = {'product': {'description': '', 'compatibility': ''}}
    assert restore_source_detail_fields(data, html, 'https://gbi-grand.ru/product/a/')
    assert '1.038.1-1' in data['product']['description']
    assert 'бетонными плитами' in data['product']['compatibility']
    assert not restore_source_detail_fields(data, html, 'https://gbi-grand.ru/product/a/')


def test_gbi_full_difficult_ground_and_repair_conditions():
    text = 'В условиях сложного грунта и в сейсмически опасных зонах лотки защищают от влаги и агрессивных соединений и обеспечивают быстрый доступ при ремонте.'
    html = '<div class="text_after_items"><p>'+text+'</p><p hidden>сложного грунта сейсмически опасных выдуманное</p></div>'
    data = {'product': {'advantages': 'Надежная защита'}}
    assert restore_source_detail_fields(data, html, 'https://gbi-grand.ru/product/lotki-l/')
    assert text in data['product']['advantages']
    assert 'выдуманное' not in data['product']['advantages']
