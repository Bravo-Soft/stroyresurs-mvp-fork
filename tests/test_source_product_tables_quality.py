"""Регрессии аудита: проверяем исходный HTML, а не повторение реализации."""
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from inline_products import inline_product_key, inline_product_links, isolate_inline_product
from product_id_manager import ProductIDManager, restore_source_variant_name
from source_product_tables import (extract_source_tables, compact_source_tables,
                                   restore_deterministic_tables, source_catalog_families)

ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / 'AGENTS.md').exists())
SAVED = ROOT / 'codex_work/audit_runs/today_20261106/snapshot/Base/temp_html_storage'
LIVE_SAVED = ROOT / 'mvp_directories/Base/temp_html_storage'


def saved(company, pattern, base=SAVED):
    paths = list((base / company).rglob(pattern))
    assert paths, (company, pattern)
    return paths[0].read_text()


def test_vent_t_joint_has_all_positions_and_rows():
    html = saved('Вентэкспресс_ООО', '*fae774*.html')
    url = 'https://ventexpress.ru/produkciya/ventilyaciya/pryamougolnyy-troynik'
    table, = extract_source_tables(html, url)
    assert len(table['rows']) == 135
    assert table['headers'][3] == 'D, мм'
    assert table['headers'][6] == 'Толщина оцинковки, мм'
    assert len(set(table['headers'])) == 8
    assert table['rows'][0] == ['100'] * 6 + ['0,5', 'ф20']
    ai = {'product': {'specifications': {}}}
    restore_deterministic_tables(ai, html, url)
    records = next(iter(ai['product']['specifications'].values()))
    assert len(records) == 135
    assert records['100']['D, мм'] == '100'
    assert records['100']['Толщина оцинковки, мм'] == '0,5'
    compact = BeautifulSoup(compact_source_tables(html, url), 'lxml')
    assert len(compact.find('table').find_all('tr')) == 2
    assert '135 строк' in compact.get_text()


def test_bsd_multiline_marks_keep_corresponding_metal_mass():
    html = saved('Белгородстройдеталь_АО', '*mnogopustotnye*.html')
    tables = extract_source_tables(html, 'https://bsdbel.ru/catalog/mnogopustotnye-plity')
    assert len(tables) == 8
    rows = [row for table in tables for row in table['rows']]
    by_name = {row[0]: row for row in rows}
    assert by_name['ПБ 18-12-6'][3:9] == ['1780', '1195', '220', 'В30\n(М400)', '0,265', '663']
    assert by_name['ПБ 69-12-6'][-1] == '32,38'
    assert by_name['ПБ 69-12-8'][-1] == '37,68'
    assert all('\n' not in row[0] for row in rows)


def test_nzmk_equal_family_name_different_series_get_distinct_ids():
    manager = ProductIDManager()
    names = []
    for pattern in ['*16-paving*.html', '*17-paving*.html', '*18-paving*.html']:
        ai = {'product': {'product_name': 'Блок ограждения (НЗМК)'}}
        html = saved('Новоалтайский_завод_мостовых_конструкций_ОАО', pattern)
        restore_source_variant_name(ai, html, 'https://nzmk22.ru/products/bridge')
        names.append(ai['product']['product_name'])
        before = ai['product']['product_name']
        restore_source_variant_name(ai, html, 'https://nzmk22.ru/products/bridge')
        assert before == ai['product']['product_name']
    assert 'БД-2ВИМ, БД-3ВИМ' in names[1]
    assert len({manager.generate_product_id_from_name_bag(n, 'НЗМК') for n in names}) == 3


def test_steklo_false_trash_tables_have_all_five_materials():
    html = saved('Стеклопластик_НПО_АО', '*c8b761*.html')
    tables = extract_source_tables(html, 'https://npo-stekloplastic.ru/products/materialy')
    assert len(tables) == 4
    assert {t['title'] for t in tables} == {'Технические характеристики ХСВН-7',
        'Технические характеристики БмД-К', 'Технические характеристики МПС',
        'Технические характеристики БмД-ФВ-40'}
    assert tables[0]['rows'][0] == ['Ширина полотна, мм', '550 +/- 50']
    assert tables[2]['headers'][1:] == ['МПС-35', 'МПС-А-35']


def test_land_footnote_not_sixth_variant():
    html = saved('Ландшафт_ООО', '*96e1f4f*.html', LIVE_SAVED)
    table, = extract_source_tables(html, 'https://landshaft.ru/product/trotuarnaya-plita-lendika/')
    assert len(table['rows']) == 5
    assert table['notes'] == ['*коллекция «Без фаски»']
    assert [row[1] for row in table['rows']] == ['40', '60', '80', '80', '100']
    assert all('коллекция' not in str(row) for row in table['rows'])


def test_hotpipe_connect_source_blocks_preserved():
    html = saved('ХОТПАЙП_ООО', '*164a034*.html', LIVE_SAVED)
    tables = extract_source_tables(html, 'https://xotpipe.ru/product/tcilindry-xotpipe-sp-outside/')
    modifications = next(t for t in tables if t['title'] == 'Модификации')
    connect = next(row for row in modifications['rows'] if row[0] == 'XOTPIPE SP Outside Connect')
    assert '970 мм, 1170 мм' in connect[1]
    assert '40 и 50 мм — под заказ' in connect[1]
    assert 'самофокусирующимся' in connect[1]


def test_nzmk_catalog_sections_are_isolated_and_repeatable():
    html = saved('Новоалтайский_завод_мостовых_конструкций_ОАО', '*industrial*.html')
    url = 'https://nzmk22.ru/products/industrial'
    families = source_catalog_families(html, url)
    assert len(families) == 12
    links = inline_product_links(BeautifulSoup(html, 'lxml'), url)
    assert links == [family['url'] for family in families]
    assert len(set(links)) == 12
    for family in families:
        assert inline_product_key(family['url']) == urlparse(family['url']).fragment
        selected = isolate_inline_product(html, family['url'])
        assert selected == family['html']
        assert isolate_inline_product(selected, family['url']) == selected
        assert len(BeautifulSoup(selected, 'lxml').find_all('table')) == 1
    assert isolate_inline_product(families[0]['html'], families[1]['url']) is None


def test_unknown_domain_and_existing_aggregate_are_unchanged():
    html = '<h1>Насос</h1><table><tr><th>Модель</th><th>Масса</th></tr><tr><td>1</td><td>2</td></tr></table>'
    assert extract_source_tables(html, 'https://example.org/product') == []
    assert compact_source_tables(html, 'https://example.org/product') == html
    assert source_catalog_families(html, 'https://uralgidromash.ru/product') == []


def test_parameter_tables_keep_semantic_keys_and_full_prompt():
    html = saved('Стеклопластик_НПО_АО', '*c8b761*.html')
    url = 'https://npo-stekloplastic.ru/products/materialy'
    ai = {'product': {'specifications': {}}}
    restore_deterministic_tables(ai, html, url)
    specs = ai['product']['specifications']
    hsv = next(value for key, value in specs.items() if 'ХСВН-7' in key)
    assert hsv['Ширина полотна, мм'] == '550 +/- 50'
    assert all(not isinstance(value, dict) for value in hsv.values())
    # Полные физические свойства нельзя убрать из материала классификации.
    compact = compact_source_tables(html, url)
    assert '550 +/- 50' in compact
    assert '900 +/- 100' in compact


def test_source_restore_is_idempotent_and_removes_explicit_footnote_variant():
    from copy import deepcopy
    html = saved('Ландшафт_ООО', '*96e1f4f*.html', LIVE_SAVED)
    url = 'https://landshaft.ru/product/trotuarnaya-plita-lendika/'
    ai = {'product': {'specifications': {}, 'variants': [
        {'Высота': '*коллекция «Без фаски»'}, {'Исполнение': 'Серая'}]}}
    restore_deterministic_tables(ai, html, url)
    assert ai['product']['variants'] == [{'Исполнение': 'Серая'}]
    once = deepcopy(ai)
    restore_deterministic_tables(ai, html, url)
    assert ai == once


def test_all_six_hotpipe_pages_keep_four_distinct_connect_modifications():
    pages = []
    names = set()
    for path in (LIVE_SAVED / 'ХОТПАЙП_ООО/Product_pages').glob('*.html'):
        html = path.read_text()
        if 'Connect' not in html:
            continue
        pages.append(path)
        ai = {'product': {'specifications': {}, 'variants': []}}
        restore_deterministic_tables(ai, html, 'https://xotpipe.ru/product/cylinders/')
        variants = ai['product']['variants']
        connect = [v for v in variants if 'Connect' in v.get('Исполнение', '')]
        assert connect, path
        names.update(v['Исполнение'] for v in connect)
        assert all(v['Описание'] for v in connect)
    assert len(pages) == 6
    assert len(names) == 4


def test_nzmk_concrete_types_keep_their_own_conditions():
    html = saved('Новоалтайский_завод_мостовых_конструкций_ОАО', '*concrete*.html')
    families = source_catalog_families(html, 'https://nzmk22.ru/products/concrete')
    assert len(families) == 3
    assert 'F75-300' in families[1]['html'] and 'W2-10' in families[1]['html']
    assert 'F200' in families[2]['html'] and 'W6' in families[2]['html']
    assert 'F75-300' not in families[2]['html']
    assert 'Для бетонных покрытий дорог и аэродромов' in families[2]['html']


def test_nzmk_transition_slab_series_is_not_deduplicated_with_other_series():
    html = saved('Новоалтайский_завод_мостовых_конструкций_ОАО', '*12-slabs*.html')
    ai = {'product': {'product_name': 'Плиты переходные (НЗМК)'}}
    restore_source_variant_name(ai, html, 'https://nzmk22.ru/products/bridge/12-slabs-transition')
    name = ai['product']['product_name']
    assert all(mark in name for mark in ['П1-4', 'П1-6', 'П1-8'])
    assert name != 'Плиты переходные (НЗМК)'


def test_azhur_source_keeps_units_and_physical_values_before_classification():
    html = saved('Стеклопластик_НПО_АО', '*8c159a92*.html')
    url = 'https://npo-stekloplastic.ru/products/e-glass-fiber-materials/setka-steklyannaya/'
    table, = extract_source_tables(html, url)
    assert table['headers'] == ['Наименование показателей', 'Ед. изм.', 'Ажур-15П-76']
    assert table['rows'][0] == ['Ширина (по ГОСТ 6943.17-94)', 'см', '100±2']
    assert table['rows'][1] == ['Масса 1м² (по ГОСТ 6943.16-94)', 'г', '15±1,5']
    assert '100±2' in compact_source_tables(html, url)
    assert '15±1,5' in compact_source_tables(html, url)


def test_azhur_specs_are_model_to_property_with_units_in_property_label():
    from docx import Document
    from docx_generator import DOCXGenerator
    html = saved('Стеклопластик_НПО_АО', '*8c159a92*.html')
    url = 'https://npo-stekloplastic.ru/products/e-glass-fiber-materials/setka-steklyannaya/'
    ai = {'product': {'specifications': {}}}
    layouts = restore_deterministic_tables(ai, html, url)
    models = next(iter(ai['product']['specifications'].values()))
    assert list(models) == ['Ажур-15П-76']
    properties = models['Ажур-15П-76']
    assert properties['Ширина (по ГОСТ 6943.17-94), см'] == '100±2'
    assert properties['Масса 1м² (по ГОСТ 6943.16-94), г'] == '15±1,5'
    assert not any('Ед. изм.' in key for key in properties)
    assert all(key != 'Ажур-15П-76' for key in properties)
    doc = Document()
    DOCXGenerator()._add_specifications(doc, ai['product']['specifications'], layouts)
    rendered = '\n'.join(cell.text for table in doc.tables for row in table.rows for cell in row.cells)
    assert 'Ажур-15П-76' in rendered
    assert '100±2' in rendered and '15±1,5' in rendered
    assert 'Ширина' in rendered and 'см' in rendered
    assert 'Масса' in rendered and '6943.16-94' in rendered


def test_mps_matrix_keeps_models_out_of_property_namespace():
    html = saved('Стеклопластик_НПО_АО', '*c8b761*.html')
    ai = {'product': {'specifications': {}}}
    restore_deterministic_tables(ai, html, 'https://npo-stekloplastic.ru/products/materialy')
    models = next(value for key, value in ai['product']['specifications'].items()
                  if 'характеристики МПС' in key)
    assert set(models) == {'МПС-35', 'МПС-А-35'}
    assert len(models['МПС-35']) == len(models['МПС-А-35']) == 3
    assert not any('МПС-35' in key for key in models['МПС-35'])


def test_malformed_elementor_colspan_does_not_invent_empty_variants():
    html = saved('Стеклопластик_НПО_АО', '*5723d011*.html')
    table, = extract_source_tables(html, 'https://npo-stekloplastic.ru/products/coating')
    assert table['headers'] == ['Показатель', 'Значение']
    assert all(len(row) == 2 for row in table['rows'])
    assert len(table['rows']) == 7


def test_only_reachable_ui_tables_are_sources_and_document_links_are_not_read():
    table = '<table><tr><th>Модель</th><th>Масса, кг</th></tr><tr><td>1</td><td>2</td></tr></table>'
    url = 'https://bsdbel.ru/catalog/test'
    assert extract_source_tables('<section hidden>' + table + '</section>', url) == []
    assert extract_source_tables('<template>' + table + '</template>', url) == []
    assert extract_source_tables('<section style="display: none">' + table + '</section>', url) == []
    html = '<button aria-controls="tab">Характеристики</button><section id="tab" hidden>' + table + '</section>'
    assert len(extract_source_tables(html, url)) == 1
    hidden_control = '<section hidden><button aria-controls="tab">Открыть</button></section><section id="tab" hidden>' + table + '</section>'
    assert extract_source_tables(hidden_control, url) == []
    assert extract_source_tables('<a href="/source.docx">Полные характеристики</a>', url) == []


def test_units_column_is_not_a_property_value_even_when_unit_contains_digits():
    html = '''<table class="eael-data-table"><thead><tr><th>Наименование показателей</th>
    <th>Ед. изм.</th><th>Модель-123</th></tr></thead><tbody><tr><td>Модуль упругости</td>
    <td>10^6 Па</td><td>50</td></tr></tbody></table>'''
    ai = {'product': {'specifications': {}}}
    restore_deterministic_tables(ai, html, 'https://npo-stekloplastic.ru/products/test')
    models = next(iter(ai['product']['specifications'].values()))
    assert models == {'Модель-123': {'Модуль упругости, 10^6 Па': '50'}}


def test_elementor_binds_type_subheaders_before_making_model_property_records():
    html = saved('Стеклопластик_НПО_АО', '*611bb1a2*.html')
    url = 'https://npo-stekloplastic.ru/products/gibkie-elementy/'
    raw = next(table for table in extract_source_tables(html, url) if table['headers'][0] == 'Наименование')
    assert raw['headers'][1:] == ['Показатель для НГЭК типа: А', 'Показатель для НГЭК типа: Б',
                                  'Показатель для НГЭК типа: В', 'Показатель для НГЭК типа: В1']
    assert raw['rows'][0][0] == 'Максимальная температура рабочей среды, °С'
    ai = {'product': {'specifications': {}}}
    restore_deterministic_tables(ai, html, url)
    models = next(iter(ai['product']['specifications'].values()))
    assert models['Показатель для НГЭК типа: А']['Максимальная температура рабочей среды, °С'] == '200'
    assert models['Показатель для НГЭК типа: В1']['Максимальная температура рабочей среды, °С'] == '650'
    assert all(not key.startswith('Строка') for properties in models.values() for key in properties)


def test_comparison_matrix_and_material_rows_use_their_explicit_axes():
    url = 'https://npo-stekloplastic.ru/products/test/'
    comparison = saved('Стеклопластик_НПО_АО', '*d40a801c*.html')
    ai = {'product': {'specifications': {}}}
    restore_deterministic_tables(ai, comparison, url)
    models = next(iter(ai['product']['specifications'].values()))
    assert models['Муфты из ремкомплектов ГАРС']['Предел прочности при растяжении, σв, МПа'] == '950,0'
    materials = saved('Стеклопластик_НПО_АО', '*57ad7a8c*.html')
    ai = {'product': {'specifications': {}}}
    restore_deterministic_tables(ai, materials, url)
    models = next(iter(ai['product']['specifications'].values()))
    assert models['ПЭПА']['Время технологической пригодности, мин'] == '10-12'
    assert models['Полиэфирамин Д-230']['σр, МПа'] == '60'


def test_published_nzmk_slider_makes_all_eight_beam_tables_reachable_without_phantoms():
    html = saved('Новоалтайский_завод_мостовых_конструкций_ОАО', '*e828ed30*.html')
    url = 'https://nzmk22.ru/products/bridge/beams'
    tables = extract_source_tables(html, url)
    assert len(tables) == 8
    assert all(len(table['rows']) == 1 for table in tables)
    assert {table['rows'][0][0] for table in tables} == {
        'Б900.110.95', 'Б 1200.110.95', 'Б1200.130.93', 'Б 1500.110.95',
        'Б1500.130.93', 'Б 1800.110.110', 'Б1800.130.108', 'ВТК21С'}
    # Без опубликованного обработчика недоступные скрытые заготовки
    # не принимаются за содержимое UI.
    soup = BeautifulSoup(html, 'lxml')
    for script in soup.find_all('script'):
        if not script.get('src'):
            script.clear()
    assert len(extract_source_tables(str(soup), url)) == 1


def test_compaction_never_discards_parameter_rows_from_a_multi_model_matrix():
    rows = ''.join(f'<tr><td>Параметр {i}</td><td>{i}</td><td>{i + 1}</td></tr>'
                   for i in range(25))
    html = ('<table class="eael-data-table"><thead><tr><th>Наименование показателей</th>'
            '<th>Материал-А</th><th>Материал-Б</th></tr></thead><tbody>' + rows + '</tbody></table>')
    compact = compact_source_tables(html, 'https://npo-stekloplastic.ru/products/test/')
    assert len(BeautifulSoup(compact, 'lxml').find('table').find_all('tr')) == 26
    assert 'Параметр 24' in compact


def test_real_replay_azhur_drops_same_property_aliases_with_units_in_values():
    from copy import deepcopy
    import json
    directory = ROOT / 'codex_work/cards_quality_2026-11-06/cards_replay_v39/84bb3fa4b87611e6'
    data = json.loads((directory / 'result.json').read_text())
    ai = deepcopy(data['raw_result'])
    restore_deterministic_tables(ai, (directory / 'source.html').read_text(), data['url'])
    specs = ai['product']['specifications']
    assert len(specs) == 1
    models = next(iter(specs.values()))
    assert set(models) == {'Ажур-15П-76'}
    assert len(models['Ажур-15П-76']) == 4
    assert models['Ажур-15П-76']['Ширина (по ГОСТ 6943.17-94), см'] == '100±2'


def test_alias_reconciliation_preserves_other_parts_conditions_and_incompatible_units():
    html = saved('Стеклопластик_НПО_АО', '*8c159a92*.html')
    url = 'https://npo-stekloplastic.ru/products/e-glass-fiber-materials/setka-steklyannaya/'
    exact = 'Ширина (по ГОСТ 6943.17-94)'
    ai = {'product': {'specifications': {
        exact: '100±2 см',
        'Облицовка': {exact: '123 см'},
        'Ширина при +20 °С (по ГОСТ 6943.17-94)': '110 см',
        exact + ', мм': '1000±20',
        'Технические характеристики': {'Масса 1м^2 (по ГОСТ 6943.16-94)': '15±1,5 г'},
    }}}
    restore_deterministic_tables(ai, html, url)
    specs = ai['product']['specifications']
    assert exact not in specs
    assert specs['Облицовка'] == {exact: '123 см'}
    assert specs['Ширина при +20 °С (по ГОСТ 6943.17-94)'] == '110 см'
    assert specs[exact + ', мм'] == '1000±20'
    assert 'Технические характеристики' not in specs


def test_real_ograda_header_is_properties_and_rows_are_product_marks():
    html = saved('Новоалтайский_завод_мостовых_конструкций_ОАО', '*industrial*.html')
    url = 'https://nzmk22.ru/products/industrial'
    family = next(f for f in source_catalog_families(html, url) if f['title'] == 'Ограда')
    table, = extract_source_tables(family['html'], family['url'])
    assert table['headers'] == ['Марка изделия', 'Объём бетона', 'Класс бетона', 'Масса, т']
    assert [row[0] for row in table['rows']] == ['ПО-2', 'ФО-2']
    ai = {'product': {'product_name': 'Ограда (НЗМК)', 'specifications': {}}}
    restore_deterministic_tables(ai, family['html'], family['url'])
    restore_source_variant_name(ai, family['html'], family['url'])
    models = next(iter(ai['product']['specifications'].values()))
    assert models['ПО-2']['Объём бетона'] == '0,604'
    assert models['ФО-2']['Масса, т'] == '0,56'
    assert set(models) == {'ПО-2', 'ФО-2'}
    assert ai['product']['product_name'] == 'Ограда (ПО-2, ФО-2) (НЗМК)'
    assert all('ПО-2' not in properties for properties in models.values())


def test_real_price_tables_keep_prices_and_order_conditions_outside_specifications():
    from copy import deepcopy
    import json
    from main import count_numeric_characteristics
    expected = {'74bee9212c0365a5': 6, 'dfb7344306677351': 10,
                '5c709cae8c2ae637': 10, 'c0a784d3b1080c9a': 3}
    for identifier, count in expected.items():
        directory = ROOT / 'codex_work/cards_quality_2026-11-06/cards_replay_v39' / identifier
        data = json.loads((directory / 'result.json').read_text())
        ai = deepcopy(data['raw_result'])
        html = (directory / 'source.html').read_text()
        restore_deterministic_tables(ai, html, data['url'])
        product = ai['product']
        assert count_numeric_characteristics(product) == count, (identifier, product['specifications'])
        assert 'руб.' in product['price']
        assert 'шт' in product['price'] or 'Розничная цена' in product['price']
        def leaves(obj):
            for key, value in obj.items():
                if isinstance(value, dict):
                    yield from leaves(value)
                else:
                    yield key
        assert all('руб' not in key.lower() and 'цена' not in key.lower()
                   for key in leaves(product['specifications']))
        once = deepcopy(ai)
        restore_deterministic_tables(ai, html, data['url'])
        assert ai == once
    anti = json.loads((ROOT / 'codex_work/cards_quality_2026-11-06/cards_replay_v39/74bee9212c0365a5/result.json').read_text())
    ai = deepcopy(anti['raw_result'])
    restore_deterministic_tables(ai, (ROOT / 'codex_work/cards_quality_2026-11-06/cards_replay_v39/74bee9212c0365a5/source.html').read_text(), anti['url'])
    assert '870*560*820(h)' in ai['product']['price']
    assert '1000*560*820(h)' in ai['product']['price']
    assert 'До 10 шт, руб.' in ai['product']['price']
    assert 'От 10 шт до 25 шт, руб.' in ai['product']['price']


def test_other_real_nzmk_horizontal_mark_tables_are_not_transposed_into_codes():
    from copy import deepcopy
    import json
    from main import count_numeric_characteristics
    examples = [('7f9990dc03c311c0', 'ПК', 11, 44),
                ('56397b95be07d3c6', 'ПК', 12, 48),
                ('9b35bbac10fa1345', 'С', 11, 44)]
    for identifier, prefix, rows, numeric in examples:
        directory = ROOT / 'codex_work/cards_quality_2026-11-06/cards_replay_v39' / identifier
        result = json.loads((directory / 'result.json').read_text())
        ai = deepcopy(result['raw_result'])
        html = (directory / 'source.html').read_text()
        restore_deterministic_tables(ai, html, result['url'])
        restore_source_variant_name(ai, html, result['url'])
        models = next(value for value in ai['product']['specifications'].values() if isinstance(value, dict))
        assert len(models) == rows
        assert all(name.startswith(prefix) for name in models)
        assert all('L' in properties and 'Объём бетона' in properties for properties in models.values())
        assert count_numeric_characteristics(ai['product']) == numeric
        assert '(L, Объём бетона' not in ai['product']['product_name']


def test_real_model_code_alias_is_not_a_numeric_property_but_concrete_class_is():
    from copy import deepcopy
    import json
    from main import count_numeric_characteristics
    directory = ROOT / 'codex_work/cards_quality_2026-11-06/cards_replay_v39/2031a1ee5d463a48'
    data = json.loads((directory / 'result.json').read_text())
    ai = deepcopy(data['raw_result'])
    restore_deterministic_tables(ai, (directory / 'source.html').read_text(), data['url'])
    assert 'Марка изделия' not in ai['product']['specifications']
    models = next(value for value in ai['product']['specifications'].values() if isinstance(value, dict))
    assert models['БД-4ИМ']['Класс бетона'] == 'В40 (М550)'
    assert count_numeric_characteristics(ai['product']) == 5


def test_comparative_fiber_strength_keeps_its_physical_parent_and_temperature_condition():
    directory = ROOT / 'codex_work/cards_quality_2026-11-06/cards_replay_v39/0132b81e263bb706'
    ai = {'product': {'specifications': {}}}
    restore_deterministic_tables(ai, (directory / 'source.html').read_text(),
                                 'https://npo-stekloplastic.ru/products/high-modulus-high-strength-fiber-glass-materials/')
    models = next(iter(ai['product']['specifications'].values()))
    vmp = models['ВМП-стекло']
    at23 = next(key for key in vmp if 'Прочность при растяжении: +23' in key)
    at196 = next(key for key in vmp if 'Прочность при растяжении: -196' in key)
    assert vmp[at23] == '4,2' and vmp[at196] == '7,0'
    assert 'ГПа' in at23 and 'ГПа' in at196
    assert not any(key.startswith(('+23', '-196')) for key in vmp)
    # Этот тест схемы не разрешает превращать сравнительную таблицу каталога
    # в автоматически принятую самостоятельную товарную карточку.


def test_real_microsphere_groups_are_model_subheaders_and_ntd_notes_are_not_physical_properties():
    from copy import deepcopy
    import json
    from main import count_numeric_characteristics
    directory = ROOT / 'codex_work/cards_quality_2026-11-06/cards_replay_v39/821349751972f7a3'
    result = json.loads((directory / 'result.json').read_text())
    ai = deepcopy(result['raw_result'])
    restore_deterministic_tables(ai, (directory / 'source.html').read_text(), result['url'])
    specs = ai['product']['specifications']
    source_path = next(key for key in specs if 'исходная таблица' in key)
    models = specs[source_path]
    assert len(models) == 11
    first = models['МС-В и МС-В(г): Группа 1Л']
    assert first['Истинная плотность (ГОСТ Р 57962-2017), г/см³'] == '0,18-0,22'
    assert all('Группа' not in attrs and 'НТД' not in attrs and 'Примечание' not in attrs
               for attrs in models.values())
    assert 'ТУ 6-48-91-92' in ai['product']['tu']
    assert any('содержание аппрета' in note for layout in ai['_table_layouts'] for note in layout['notes'])
    assert not any(' / 1Л' in key for key in specs)
    assert count_numeric_characteristics(ai['product']) == 73


def test_real_monolit_paragraph_slots_keep_blank_conditions_and_component_headers():
    from copy import deepcopy
    import json
    from main import count_numeric_characteristics
    directory = ROOT / 'codex_work/cards_quality_2026-11-06/cards_replay_v39/b088e7af7e7c3238'
    result = json.loads((directory / 'result.json').read_text())
    ai = deepcopy(result['raw_result'])
    restore_deterministic_tables(ai, (directory / 'source.html').read_text(), result['url'])
    specs = ai['product']['specifications']
    assert len(specs) == 1  # Старые противоречащие копии заменены source DOM.
    models = next(iter(specs.values()))
    assert set(models) == {'Монолит+: паста (состав А)', 'Монолит+: клей (состав Б)', 'Аква-Монолит'}
    paste, glue, aqua = [models[name] for name in models]
    assert paste['Адгезионная прочность: — к стальной поверхности, МПа'] == '20'
    assert paste['Адгезионная прочность: — к стеклопластику, МПа'] == ''
    assert glue['Адгезионная прочность: — к стальной поверхности, МПа'] == ''
    assert glue['Адгезионная прочность: — к стеклопластику, МПа'] == '10'
    assert paste['Время гелеобразования: — при 4ºС, мин'] == ''
    assert paste['Время гелеобразования: — при 20ºС, мин'] == '45±15'
    assert glue['Время гелеобразования: — при 20ºС, мин'] == '60±15'
    assert aqua['Предел прочности: — при растяжении, МПа'] == ''
    assert aqua['Предел прочности: — при сжатии, МПа'] == 'не менее 100'
    assert aqua['Модуль упругости: — при изгибе, МПа'] == 'не менее 1900'
    assert aqua['Модуль упругости: — при сжатии, МПа'] == ''
    assert aqua['Время гелеобразования: — при 4ºС, мин'] == '90±15'
    assert aqua['Время гелеобразования: — при 20ºС, мин'] == '50±15'
    assert count_numeric_characteristics(ai['product']) == 22


def test_property_parents_preserve_storage_and_chemical_conditions_in_two_column_tables():
    url = 'https://npo-stekloplastic.ru/products/test'
    ai = {'product': {'specifications': {}}}
    restore_deterministic_tables(ai, saved('Стеклопластик_НПО_АО', '*0a59ca0e*.html'), url)
    props = next(iter(ai['product']['specifications'].values()))
    assert props['Время хранения: — при 20ºС, мес.'] == '2 — 3'
    assert props['Предел прочности: — при статическом изгибе, МПа'] == '190 — 230'
    assert not any(key.startswith('— при') for key in props)
    ai = {'product': {'specifications': {}}}
    restore_deterministic_tables(ai, saved('Стеклопластик_НПО_АО', '*4f8dad00*.html'), url)
    source = ai['product']['specifications']
    fibers = next(value for key, value in source.items() if key.startswith('Кремнеземные волокна'))
    assert fibers['КВ-11(7,5)']['Длина отрезков, мм'] == '60\n120'
    assert fibers['КВ-11(7,5)']['Химический состав, массовая доля (по ГОСТ Р. 56250-2014): SiO2, %'] == '≥95,0'
    assert all('SiO2' != key for props in fibers.values() for key in props)


def test_tu_is_separate_from_gost_or_project_references_and_neither_enters_numeric_gate():
    from main import count_numeric_characteristics
    html = '''<h1>Изделия</h1><table><tr><th>Марка изделия</th><th>Проектная док-ция</th><th>Масса, кг</th></tr>
    <tr><td>А-1</td><td>ТУ 1234-567-2026</td><td>10</td></tr>
    <tr><td>Б-2</td><td>ГОСТ 12345-2025, серия 3.503.1-23</td><td>20</td></tr></table>'''
    ai = {'product': {'tu': '', 'specifications': {}}}
    restore_deterministic_tables(ai, html, 'https://bsdbel.ru/catalog/test/')
    assert 'ТУ 1234-567-2026' in ai['product']['tu']
    assert 'ГОСТ' not in ai['product']['tu'] and 'серия' not in ai['product']['tu']
    normative = next(value for key, value in ai['product']['specifications'].items() if key.startswith('Нормативные документы'))
    assert normative == 'Б-2: ГОСТ 12345-2025, серия 3.503.1-23'
    assert count_numeric_characteristics(ai['product']) == 2


def test_real_silica_yarn_long_code_row_is_header_and_230_modifications_are_namespaces():
    from copy import deepcopy
    import json
    directory = ROOT / 'codex_work/cards_quality_2026-11-06/cards_final_fix3_resume/ace5f1c71f276a73'
    result = json.loads((directory / 'result.json').read_text())
    ai = deepcopy(result['raw_result'])
    html = (directory / 'source.html').read_text()
    restore_deterministic_tables(ai, html, result['url'])
    models = next(value for key, value in ai['product']['specifications'].items()
                  if 'исходная таблица' in key)
    assert len(models) == 8
    modified = models['Нетермообработанные: К11С6-270 (230)']
    assert modified['Номинальная линейная плотность (по ГОСТ 6943.1-2015), текс'] == '270'
    assert modified['Номинальная величина крутки (по ГОСТ 6943.4-2015), кр/м'] == '230'
    assert 'Термообработанные: К11С6-250-БА (230)' in models
    assert all(not key.startswith('Строка') for attrs in models.values() for key in attrs)
    # Коды первой строки остаются только в именах, не создают числовые properties.
    assert all('К11С' not in str(value) for attrs in models.values() for value in attrs.values())


def test_blank_parameter_stub_with_pure_numeric_values_is_not_a_model_header():
    html = '''<table class="eael-data-table"><thead><tr><th>Наименование показателей</th>
    <th colspan="2">Исполнения</th></tr></thead><tbody>
    <tr><td></td><td>100</td><td>200</td></tr>
    <tr><td>Масса, кг</td><td>10</td><td>20</td></tr></tbody></table>'''
    raw, = extract_source_tables(html, 'https://npo-stekloplastic.ru/products/test/')
    assert raw['rows'][0] == ['', '100', '200']
    assert not any('100' in label or '200' in label for label in raw['headers'])


def test_final_nzmk_beam_climate_is_qualified_to_its_own_table_and_load_projects_preserved():
    from copy import deepcopy
    import json
    base = ROOT / 'codex_work/cards_quality_2026-11-06/cards_final_fix3_resume'
    for identifier in ['da218807a715b452', 'e13448d6fea1e168']:
        directory = base / identifier
        result = json.loads((directory / 'result.json').read_text())
        ai = deepcopy(result['raw_result'])
        restore_deterministic_tables(ai, (directory / 'source.html').read_text(), result['url'])
        if identifier == 'da218807a715b452':
            assert ai['product']['description'].startswith('Б1500.130.93: ')
            assert ai['product']['exploitation'].startswith('Б1500.130.93: ')
        layouts = ai['_table_layouts']
        first_notes = '\n'.join(layouts[0]['notes'])
        second_notes = '\n'.join(layouts[1]['notes'])
        assert 'А14 и НК-102,8' in first_notes
        assert 'А11 и НК-80' in second_notes
        assert 'А11' not in first_notes
        refs = '\n'.join(value for key,value in ai['product']['specifications'].items()
                         if key.startswith('Нормативные документы'))
        assert '3.503.1-73' in refs
        assert any('541' in value for value in refs.splitlines())
        if identifier == 'da218807a715b452':
            assert 'сейсмичностью до 9' not in first_notes
            assert 'Б1500.130.93:' in second_notes


def test_four_published_npo_material_tables_become_separate_families_with_five_source_marks():
    html = saved('Стеклопластик_НПО_АО', '*c8b761*.html')
    url = 'https://npo-stekloplastic.ru/products/materialy-iz-supertonkogo-i-ultratonkogo-volokna/'
    families = source_catalog_families(html, url)
    assert [family['title'] for family in families] == [
        'Технические характеристики ХСВН-7', 'Технические характеристики БмД-К',
        'Технические характеристики МПС', 'Технические характеристики БмД-ФВ-40']
    assert inline_product_links(BeautifulSoup(html, 'lxml'), url) == [family['url'] for family in families]
    for family in families:
        assert inline_product_key(family['url']) is not None
        selected = isolate_inline_product(html, family['url'])
        assert selected == family['html']
        assert isolate_inline_product(selected, family['url']) == selected
        soup = BeautifulSoup(selected, 'lxml')
        assert len(soup.select('table.eael-data-table')) == 1
        assert not soup.find('a', href=lambda value:value and value.lower().endswith(('.pdf', '.docx')))
        assert not any(other['title'] in soup.get_text() for other in families if other is not family)
    mps = families[2]
    ai = {'product': {'specifications': {}}}
    restore_deterministic_tables(ai, mps['html'], mps['url'])
    models = next(value for value in ai['product']['specifications'].values() if isinstance(value, dict))
    assert set(models) == {'МПС-35', 'МПС-А-35'}
    assert len(models['МПС-35']) == len(models['МПС-А-35']) == 3


def test_real_ap1_temperature_glyph_aliases_do_not_duplicate_source_properties():
    from copy import deepcopy
    import json
    from main import count_numeric_characteristics
    directory = ROOT / 'codex_work/cards_quality_2026-11-06/cards_final_fix3_resume/3fec0915665b7bcd'
    result = json.loads((directory / 'result.json').read_text())
    ai = deepcopy(result['raw_result'])
    restore_deterministic_tables(ai, (directory / 'source.html').read_text(), result['url'])
    assert all(isinstance(value, dict) for value in ai['product']['specifications'].values())
    assert count_numeric_characteristics(ai['product']) == 6


def test_real_pressmaterial_unit_operators_and_celsius_are_typography_not_new_properties():
    from copy import deepcopy
    import json
    directory = ROOT / 'codex_work/cards_quality_2026-11-06/cards_final_fix3_resume/187a27457fac7dc6'
    result = json.loads((directory / 'result.json').read_text())
    ai = deepcopy(result['raw_result'])
    restore_deterministic_tables(ai, (directory / 'source.html').read_text(), result['url'])
    scalars = {key:value for key,value in ai['product']['specifications'].items() if not isinstance(value,dict)}
    assert not any(key.startswith(('Температура прессования', 'Теплостойкость по Мартенсу',
                                  'Удельное объемное сопротивление', 'Удельная ударная вязкость')) for key in scalars)
    assert scalars['Водостойкость'] == 'высокая'
    assert scalars['Устойчивость к воздействию щелочей любой концентрации при температуре до 200 C'] == 'есть'
    assert scalars['Устойчивость к большинству кислот (кроме окислителей)'] == 'есть'


def test_nzmk_family_copies_only_published_common_purpose_from_its_slider_group():
    html = saved('Новоалтайский_завод_мостовых_конструкций_ОАО', '*e828ed30*.html')
    families = source_catalog_families(html, 'https://nzmk22.ru/products/bridge/beams')
    assert len(families) == 5
    for family in families:
        soup = BeautifulSoup(family['html'], 'lxml')
        purposes = soup.select('p[data-source-common-purpose]')
        assert len(purposes) == 1
        assert purposes[0].get_text(' ', strip=True) == 'Предназначена для строительства мостов и путепроводов'
        assert 'Более 350 наименований' not in soup.get_text()
        ai = {'product': {'specifications': {}, 'applications': ''}}
        restore_deterministic_tables(ai, family['html'], family['url'])
        assert ai['product']['applications'] == purposes[0].get_text(' ', strip=True)
    nine = next(family for family in families if '9 метров' in family['title'])
    assert 'сейсмичностью до 9' not in nine['html']
    other = saved('Новоалтайский_завод_мостовых_конструкций_ОАО', '*industrial*.html')
    assert not any('data-source-common-purpose' in family['html']
                   for family in source_catalog_families(other, 'https://nzmk22.ru/products/industrial'))


def test_npo_frame_keeps_explicit_own_type_description_application_and_tu_without_neighbours():
    html = saved('Стеклопластик_НПО_АО', '*c8b761*.html')
    url = 'https://npo-stekloplastic.ru/products/materialy-iz-supertonkogo-i-ultratonkogo-volokna/'
    families = source_catalog_families(html, url)
    texts = {family['title']:BeautifulSoup(family['html'], 'lxml').get_text(' ', strip=True) for family in families}
    hsv = texts['Технические характеристики ХСВН-7']
    assert 'Холст стекловолокнистый нетканый марки ХСВН-7' in hsv
    assert 'экранно-вакуумной теплоизоляции для криогенной техники' in hsv
    assert 'ТУ 6-48-05786904-147-94' in hsv
    assert 'Стеклобумага БмД-К' not in hsv and 'Мат поверхностный' not in hsv
    bmd = texts['Технические характеристики БмД-К']
    assert 'Стеклобумага БмД-К' in bmd and 'слоисто-вакуумной многослойной теплоизоляции' in bmd
    assert 'ХСВН-7' not in bmd and 'МПС-35' not in bmd
    mps = texts['Технические характеристики МПС']
    assert 'химически стойких стеклопластиковых изделий' in mps
    assert 'ТУ 6-11-403-84' in mps and '60+/-5' in mps and '700+/-10' in mps
    assert 'согласованию с потребителем' in mps
    filters = texts['Технические характеристики БмД-ФВ-40']
    assert 'Стеклобумага БмД-ФВ' in filters and 'фильтрах для очистки' in filters
    assert 'вентиляции и кондиционирования' in filters
    assert 'ХСВН-7' not in filters and 'МПС-35' not in filters
    assert all('Холст' in BeautifulSoup(family['html'], 'lxml').h1.get_text()
               for family in families if 'ХСВН-7' in family['title'])


def test_runtime_url_and_product_slug_keys_keep_all_four_npo_families_separate():
    from web_crawler import SmartURLNormalizer, WebCrawler
    html = saved('Стеклопластик_НПО_АО', '*c8b761*.html')
    families = source_catalog_families(html, 'https://npo-stekloplastic.ru/products/materialy-iz-supertonkogo-i-ultratonkogo-volokna/')
    crawler = WebCrawler.__new__(WebCrawler)  # Только чистые функции ключа; без I/O-конструктора.
    crawler.url_normalizer = SmartURLNormalizer()
    normalized = [crawler.url_normalizer.normalize_url(family['url']) for family in families]
    slug_keys = [crawler._product_dedup_key(family['url']) for family in families]
    assert len(set(normalized)) == len(set(slug_keys)) == 4
    assert all('#source-family=' in key for key in normalized + slug_keys)


def test_npo_caption_context_does_not_borrow_an_unrelated_heading_or_cross_previous_table():
    table = lambda name: '<section class="elementor-top-section"><table class="eael-data-table"><thead><tr><th colspan="2">Технические характеристики ' + name + '</th></tr></thead><tbody><tr><td>Масса, г</td><td>1</td></tr></tbody></table></section>'
    html = '<div class="elementor"><section><h2>Чужой материал ZZ</h2><p>Чужое применение в кабелях</p></section>' + table('A123') + table('B456') + '</div>'
    families = source_catalog_families(html, 'https://npo-stekloplastic.ru/products/materials/')
    assert len(families) == 2
    assert all('Чужое применение' not in family['html'] for family in families)
    assert 'A123' not in families[1]['html']


def test_source_contract_rejects_parent_mixed_catalog_but_not_isolated_family_or_homogeneous_tables():
    from source_product_tables import source_family_catalog_reason
    cases = [
        ('Новоалтайский_завод_мостовых_конструкций_ОАО', '*e828ed30*.html', 'https://nzmk22.ru/products/bridge/beams'),
        ('Новоалтайский_завод_мостовых_конструкций_ОАО', '*industrial*.html', 'https://nzmk22.ru/products/industrial'),
        ('Стеклопластик_НПО_АО', '*c8b761*.html', 'https://npo-stekloplastic.ru/products/materialy-iz-supertonkogo-i-ultratonkogo-volokna/'),
    ]
    for company, pattern, url in cases:
        html = saved(company, pattern)
        reason = source_family_catalog_reason(html, url)
        assert reason and reason.startswith('Самостоятельные товарные блоки: ')
        for family in source_catalog_families(html, url):
            assert source_family_catalog_reason(family['html'], family['url']) is None
    homogeneous = saved('Белгородстройдеталь_АО', '*mnogopustotnye*.html')
    assert source_family_catalog_reason(homogeneous, 'https://bsdbel.ru/catalog/mnogopustotnye-plity-perekrytij') is None
    assert source_family_catalog_reason('<h1>Товар</h1>', 'https://example.org/product') is None


def test_model_table_restore_preserves_real_npo_body_properties_not_owned_by_the_table():
    html = saved('Стеклопластик_НПО_АО', '*c8b761*.html')
    families = source_catalog_families(html, 'https://npo-stekloplastic.ru/products/materialy-iz-supertonkogo-i-ultratonkogo-volokna/')
    mps = next(family for family in families if family['title'] == 'Технические характеристики МПС')
    assert '700+/-10' in mps['html'] and 'армирован стеклянными нитями' in mps['html']
    ai = {'product': {'specifications': {
        'МПС-35': {'Поверхностная плотность, г/м2 не более': '35 +/- 5',
                   'Ширина матов, мм': '60+/-5; 70+/-5; 150+/-10; 350+/-10; 700+/-10'},
        'МПС-А-35': {'Поверхностная плотность, г/м2 не более': '35 +/- 5',
                     'Армирование': 'стеклянными нитями из непрерывного волокна'},
    }}}
    restore_deterministic_tables(ai, mps['html'], mps['url'])
    specs = ai['product']['specifications']
    assert specs['МПС-35'] == {'Ширина матов, мм': '60+/-5; 70+/-5; 150+/-10; 350+/-10; 700+/-10'}
    assert specs['МПС-А-35'] == {'Армирование': 'стеклянными нитями из непрерывного волокна'}
    table = next(value for key,value in specs.items() if 'исходная таблица' in key)
    assert len(table['МПС-35']) == len(table['МПС-А-35']) == 3


def test_model_remainder_keeps_foreign_part_and_different_test_condition():
    html = saved('Стеклопластик_НПО_АО', '*8c159a92*.html')
    ai = {'product': {'specifications': {'Ажур-15П-76': {
        'Ширина (по ГОСТ 6943.17-94), см': '999',
        'Облицовка': {'Ширина (по ГОСТ 6943.17-94), см': '110'},
        'Ширина при +20 °С (по ГОСТ 6943.17-94), см': '105',
    }}}}
    restore_deterministic_tables(ai, html, 'https://npo-stekloplastic.ru/products/setka/')
    extra = ai['product']['specifications']['Ажур-15П-76']
    assert 'Ширина (по ГОСТ 6943.17-94), см' not in extra
    assert extra['Облицовка']['Ширина (по ГОСТ 6943.17-94), см'] == '110'
    assert extra['Ширина при +20 °С (по ГОСТ 6943.17-94), см'] == '105'


def test_actual_hollow_slab_horizontal_strands_are_preserved_in_model_qualified_notes():
    from copy import deepcopy
    import json
    directory = ROOT / 'codex_work/cards_quality_2026-11-06/cards_final_fix3_resume/494f9cebe73883ba'
    result = json.loads((directory / 'result.json').read_text())
    ai = deepcopy(result['raw_result'])
    restore_deterministic_tables(ai, (directory / 'source.html').read_text(), result['url'])
    notes = '\n'.join(note for layout in ai['_table_layouts'] for note in layout['notes'])
    assert 'П18-4К7, П15-4К7:' in notes
    assert 'горизонтальных семипроволочных прядей К7-15 по ГОСТ 13840' in notes


def test_real_vmp_parent_comparison_is_catalog_not_a_finished_child_product():
    from source_product_tables import source_family_catalog_reason
    import json
    base = ROOT / 'codex_work/cards_quality_2026-11-06/cards_final_fix3_resume'
    parent = base / '0132b81e263bb706'
    result = json.loads((parent / 'result.json').read_text())
    html = (parent / 'source.html').read_text()
    reason = source_family_catalog_reason(html, result['url'])
    assert reason and 'Каталог дочерних изделий' in reason
    assert all(title in reason for title in ['ВМП нити', 'ВМП ровинг', 'ВМП ткани', 'ВМП ленты'])
    # Сравнение само по себе и ссылки из чужого раздела не запрещают товар.
    soup = BeautifulSoup(html, 'lxml')
    for link in soup.select('h2 a'):
        link.unwrap()
    assert source_family_catalog_reason(str(soup), result['url']) is None
    assert source_family_catalog_reason(html, result['url'] + 'vmp-roving/') is None
    for directory in base.glob('9ea*'):
        detail = json.loads((directory / 'result.json').read_text())
        assert source_family_catalog_reason((directory / 'source.html').read_text(), detail['url']) is None


def test_real_fv_operator_moved_to_value_is_exact_alias_but_other_condition_is_not():
    import json
    from copy import deepcopy
    directory = ROOT / 'codex_work/cards_quality_2026-11-06/cards_source_fix4/593f4b1980e33cd3'
    row = json.loads((directory / 'result.json').read_text())
    from main import count_numeric_characteristics
    plain = deepcopy(row['raw_result'])
    restore_deterministic_tables(plain, (directory / 'source.html').read_text(), row['url'])
    assert count_numeric_characteristics(plain['product']) == 6
    ai = deepcopy(row['raw_result'])
    specs = ai['product']['specifications']
    specs['Толщина полотна при 50°C, мм'] = 'не более 1,0'
    specs['Толщина полотна, см'] = 'не более 1,0'
    specs['Толщина полотна, мм, не менее'] = '1,0'
    restore_deterministic_tables(ai, (directory / 'source.html').read_text(), row['url'])
    specs = ai['product']['specifications']
    assert 'Толщина полотна, мм' not in specs
    assert 'Поверхностная плотность, г/м^2' not in specs
    assert 'Массовая доля веществ, удаляемых при прокаливании, %' not in specs
    assert 'Разрывная нагрузка полоски шириной 50мм H(кгс)' not in specs
    assert specs['Толщина полотна при 50°C, мм'] == 'не более 1,0'
    assert specs['Толщина полотна, см'] == 'не более 1,0'
    assert specs['Толщина полотна, мм, не менее'] == '1,0'


def test_real_vmp_ai_numbered_pseudomodel_matches_complete_source_column_only():
    import json
    from copy import deepcopy
    directory = ROOT / 'codex_work/cards_quality_2026-11-06/cards_source_fix4/9ea881abf4cb00e9'
    row = json.loads((directory / 'result.json').read_text())
    from main import count_numeric_characteristics
    plain = deepcopy(row['raw_result'])
    restore_deterministic_tables(plain, (directory / 'source.html').read_text(), row['url'])
    assert count_numeric_characteristics(plain['product']) == 21
    ai = deepcopy(row['raw_result'])
    specs = ai['product']['specifications']
    specs['Другая часть_1'] = deepcopy(specs['РВМПН_1'])
    specs['РВМПН_2']['Собственное применение'] = 'Сохранить это свойство'
    specs['РВМПН_3']['Номинальный диаметр элементарной нити'] = '11'
    restore_deterministic_tables(ai, (directory / 'source.html').read_text(), row['url'])
    specs = ai['product']['specifications']
    assert 'РВМПН_1' not in specs and 'РВМПН_4' not in specs and 'РВМПН_5' not in specs
    assert specs['РВМПН_2']['Собственное применение'] == 'Сохранить это свойство'
    assert specs['РВМПН_3']['Номинальный диаметр элементарной нити'] == '11'
    assert 'Другая часть_1' in specs
    source = next(value for key, value in specs.items() if 'исходная таблица' in key)
    assert len(source) == 5
