from pathlib import Path

from text_extractor import html_to_markdown
from product_extraction_revision import product_extraction_revision
from source_product_tables import extract_source_tables, restore_deterministic_tables
from product_id_manager import ProductIDManager, restore_source_variant_name

FIXTURES = Path(__file__).parent / 'fixtures' / 'hms'
BASE = 'https://www.hms-livgidromash.ru'


def test_model_keeps_own_specs_before_prompt_limit():
    md = html_to_markdown((FIXTURES / 'model.html').read_text(),
                         BASE + '/catalog/nasosy/2vv-2vg/2vg40-16_model_21710.html')
    assert len(md) < 10000
    assert '| Подача, м^3/ч, не менее | 35 |' in md
    assert '| Частота вращения, об/мин | 1450 |' in md
    assert '| КПД, % | 50 |' in md
    assert 'Все марки серии' not in md
    assert '2ВГ63/16' not in md


def test_series_keeps_variants_and_description():
    md = html_to_markdown((FIXTURES / 'series.html').read_text(), BASE + '/catalog/12-km.html')
    assert len(md) < 15000
    assert '| 12КМ-15 | 450 | 30 |' in md
    assert '| 12КМ-20б | 250 | 27 |' in md
    assert 'Назначение' in md
    assert 'Конструкция' in md
    assert 'Программа для подбора насосов HMS PUMP SELECTOR' not in md


def test_non_product_page_uses_existing_fallback():
    html = '<html><body><main><h1>Контакты</h1><p>Телефон: 123456</p></main></body></html>'
    assert 'Телефон: 123456' in html_to_markdown(html, BASE + '/contacts/', page_type='company')


def test_only_hms_catalog_invalidates_old_extraction():
    assert product_extraction_revision(BASE + '/catalog/12-km.html') == 'hms-maincontent-v1-20261110'
    assert product_extraction_revision(BASE + '/contacts/') is None
    assert product_extraction_revision('https://example.org/catalog/12-km.html') is None


def test_source_tables_keep_first_property_and_all_series_cells():
    html = (FIXTURES / 'series.html').read_text()
    url = BASE + '/catalog/12-km.html'
    tables = extract_source_tables(html, url)
    assert tables[0]['headers'] == ['Характеристика', 'Значение']
    assert tables[0]['rows'][0] == ['Тип конструкции', 'консольные']
    assert len(tables[1]['rows']) == 7
    data = {'product': {'specifications': {'12КМ-15': {'Подача (номин.), м^3/ч': '999'}}}}
    restore_deterministic_tables(data, html, url)
    specs = data['product']['specifications']
    assert '999' not in str(specs)
    layout = data['_table_layouts'][0]
    records = specs[layout['path']]
    assert records['12КМ-15']['Подача (номин.), м³/ч'] == '450'
    assert all(row['Частота тока, Гц'] == '50' for row in records.values())


def test_model_source_tables_exclude_other_pumps():
    html = (FIXTURES / 'model.html').read_text()
    tables = extract_source_tables(html, BASE + '/catalog/nasosy/2vv-2vg/2vg40-16_model_21710.html')
    assert len(tables) == 1
    assert ['Подача, м³/ч, не менее', '35'] in tables[0]['rows']
    assert '2ВГ63/16' not in str(tables)


def test_same_model_different_working_point_has_distinct_identity():
    html = (FIXTURES / 'model.html').read_text()
    url = BASE + '/catalog/nasosy/2vv-2vg/2vg40-16_model_21710.html'
    original = {'product': {'product_name': 'Насос 2ВГ40/16 (ГМС Ливгидромаш, АО)'}}
    changed = {'product': dict(original['product'])}
    restore_source_variant_name(original, html, url)
    restore_source_variant_name(changed, html.replace('1450', '2900'), url)
    one, two = original['product']['product_name'], changed['product']['product_name']
    assert 'Q>=35 м^3 в час' in one and 'P<=16 кгс на см^2' in one and 'n=1450 об в мин' in one
    assert 'n=2900 об в мин' in two
    manager = ProductIDManager()
    assert manager.generate_product_id_from_name_bag(one, 'ГМС') != manager.generate_product_id_from_name_bag(two, 'ГМС')


def test_working_point_keeps_source_pressure_unit():
    html = (FIXTURES / 'model.html').read_text().replace('кгс/см&sup2;', 'МПа')
    data = {'product': {'product_name': 'Насос (ГМС Ливгидромаш, АО)'}}
    restore_source_variant_name(data, html, BASE + '/catalog/nasosy/2vv-2vg/2vg40-16_model_21710.html')
    assert 'P<=16 МПа' in data['product']['product_name']
    assert 'кгс/см²' not in data['product']['product_name']


def test_source_tilde_is_representable_in_rtf_without_changing_sign():
    html = (FIXTURES / 'model.html').read_text().replace('об/мин', 'c&tilde;&sup1;')
    url = BASE + '/catalog/nasosy/2vv-2vg/2vg40-16_model_21710.html'
    assert 'c~^1' in html_to_markdown(html, url)
    assert ['Частота вращения, c~¹', '1450'] in extract_source_tables(html, url)[0]['rows']
    data = {'product': {'specifications': {'Частота вращения, c˜^1': '24.2'}}}
    restore_source_variant_name(data, html, BASE + '/catalog/nasosy-nds.html')
    assert data['product']['specifications'] == {'Частота вращения, c~^1': '24.2'}


def test_marine_series_is_not_merged_with_industrial_model():
    html = (FIXTURES / 'model.html').read_text()
    url = BASE + '/catalog/nasosy/2vv-2vg/2vg40-16_model_21710.html'
    names = []
    for text in (html, html.replace('2ВВ, 2ВГ', '2ВВ (судовые)')):
        data = {'product': {'product_name': 'Насос (ГМС Ливгидромаш, АО)'}}
        restore_source_variant_name(data, text, url)
        names.append(data['product']['product_name'])
    manager = ProductIDManager()
    assert 'серия 2ВВ (судовые)' in names[1]
    assert manager.generate_product_id_from_name_bag(names[0], 'ГМС') != manager.generate_product_id_from_name_bag(names[1], 'ГМС')


def test_spaces_around_model_hyphen_do_not_remove_control_rating():
    html = (FIXTURES / 'model.html').read_text().replace(
        'Насос 2ВГ40/16 - двухвинтовые нефтяные, для вязких сред',
        'HMS Control L3 - 80 ( - П) - станция управления')
    data = {'product': {'product_name': 'Станция (ГМС Ливгидромаш, АО)'}}
    restore_source_variant_name(data, html, BASE + '/catalog/nasosy/hms-control-l3/hms_control_l3_80_model_1354.html')
    assert data['product']['product_name'].startswith('HMS Control L3 - 80 ( - П)')
