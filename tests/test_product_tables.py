"""Реальные HTML/JSON дефектов и проверки сохранения контекста и всех режимов."""
from copy import deepcopy
import json
from pathlib import Path

from bs4 import BeautifulSoup
from docx import Document
import pytest

from ai_integration import _robust_json_extract, _normalize_spec_keys_ru
from docx_generator import DOCXGenerator
from product_tables import (restore_source_tables, structured_table_markdown,
                            describe_table, TableStructureError)
from text_extractor import MarkdownConverter, html_to_markdown

FIXTURES = Path(__file__).parent / 'fixtures/product_tables'


def load(case):
    data = json.loads((FIXTURES / f'{case}.json').read_text())
    return data['ai'], (FIXTURES / f'{case}.html').read_text(), data['url']


def restored(case):
    ai, html, url = load(case)
    layouts = restore_source_tables(ai, html, url)
    return ai, layouts


def render(ai):
    doc = Document()
    DOCXGenerator()._add_specifications(doc, ai['product']['specifications'], ai.get('_table_layouts'))
    return doc


@pytest.mark.parametrize('case,count', [('tsila_176', 3), ('ural_50vo', 3),
                                       ('ural_ov87', 5), ('sverd_v', 16), ('sverd_130', 3)])
def test_pump_and_motor_records_preserve_source_rows_and_connections(case, count):
    ai, layouts = restored(case)
    specs = ai['product']['specifications']
    pump = specs['Характеристики насоса']
    motor = specs['Характеристики электродвигателя']
    assert len(pump) == count and list(pump) == list(motor)
    assert len(set(pump)) == count
    assert all(k.startswith('Насос: ') for row in pump.values() for k in row)
    assert all(k.startswith('Электродвигатель: ') for row in motor.values() for k in row)
    doc = render(ai)
    text = '\n'.join(c.text for t in doc.tables for r in t.rows for c in r.cells)
    assert all(DOCXGenerator()._clean_text(key) in text for key in pump)
    assert any('Характеристики электродвигателя' == p.text for p in doc.paragraphs)
    assert max(len(t.columns) for t in doc.tables) <= 5


def test_both_ov16_modes_are_preserved_with_their_own_motor():
    ai, _ = restored('ural_ov87')
    pump = ai['product']['specifications']['Характеристики насоса']
    motor = ai['product']['specifications']['Характеристики электродвигателя']
    modes = {row['Насос: Подача, м^3/ч']: (row['Насос: Напор, м'],
             row['Насос: Част. вращ.,об/мин'], motor[key]['Электродвигатель: Тип'])
             for key, row in pump.items()}
    assert modes['8 780'] == ('4,8', '485', 'ВАН 118/28-12 У3')
    assert modes['10 580'] == ('6,8', '585', 'ВАН 118/23-10 У3')


@pytest.mark.parametrize('case', ['vmp_holding', 'vmp_anticor'])
def test_drying_matrix_has_all_40_values_axes_condition_and_original_orientation(case):
    old, _, _ = load(case)
    ai, layouts = restored(case)
    layout = layouts[0]
    rows = ai['product']['specifications'][layout['path']]
    assert '100 мкм' in layout['title'] and 'температур' in layout['column_axis']
    assert len(rows) == 4 and len(layout['columns']) == 10
    for name, values in rows.items():
        assert list(values.values()) == list(old['product']['specifications'][name].values())
    doc = render(ai)
    matrices = [t for t in doc.tables if t.cell(0, 0).text == 'Степень высыхания']
    assert [len(t.columns) for t in matrices] == [5, 5, 3]
    assert all(len(t.rows) == 5 for t in matrices)
    assert sum((len(t.columns) - 1) * (len(t.rows) - 1) for t in matrices) == 40
    assert sum('100 мкм' in p.text for p in doc.paragraphs) == 1


def test_three_level_header_and_paragraph_boundaries():
    _, html, url = load('sverd_v')
    md = html_to_markdown(html, url)
    assert 'Насос: Обозначение: с 1990' in md
    assert 'Насос: Обозначение: с 1982 по 1990' in md
    assert '2 000<br>1 600<br>1 600' in md
    assert '2 0001 6001 600' not in md
    lines = [line for line in md.splitlines() if line.startswith('|')]
    assert len(lines) == 18
    assert len(set(line.count('|') for line in lines)) == 1


def test_aggregate_mass_is_not_motor_mass_and_extra_scalars_survive():
    old, _, _ = load('tsila_176')
    ai, _ = restored('tsila_176')
    for key, value in old['product'].items():
        if key != 'specifications':
            assert ai['product'][key] == value
    specs = ai['product']['specifications']
    assert len(specs['Параметры агрегата']) == 3
    assert all('Агрегат: Масса агрегата, кг' in row for row in specs['Параметры агрегата'].values())
    assert specs['Температура перекачиваемой жидкости'] == 'от -40 C до +120 C'


def test_control_two_separate_tables_and_unknown_sites_are_unchanged():
    ai, html, url = load('tsila_65')
    original = deepcopy(ai)
    assert restore_source_tables(ai, html, url) == []
    assert ai == original
    ai, html, _ = load('ural_ov87')
    original = deepcopy(ai)
    assert restore_source_tables(ai, html, 'https://example.ru/item/') == []
    assert ai == original


@pytest.mark.parametrize('case', ['vmp_holding', 'tsila_176', 'ural_50vo', 'ural_ov87', 'sverd_v', 'sverd_130',
                                'sverd_aliases', 'sverd_horizontal', 'sverd_mass_columns', 'tsila_bad_colspan'])
def test_reconciliation_is_idempotent_and_has_no_object_arrays(case):
    ai, html, url = load(case)
    restore_source_tables(ai, html, url)
    once = deepcopy(ai)
    restore_source_tables(ai, html, url)
    assert ai == once
    def walk(obj):
        if isinstance(obj, dict):
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            assert not any(isinstance(v, dict) for v in obj)
    walk(ai['product']['specifications'])


def test_legacy_object_lists_are_tables_without_source_html():
    ai, _, _ = load('sverd_130')
    before = deepcopy(ai)
    doc = render(ai)
    assert ai == before
    assert len(doc.tables) == 2 and all(len(t.rows) == 4 for t in doc.tables)
    texts = [c.text for t in doc.tables for row in t.rows for c in row.cells]
    assert all(str(power) in texts for power in [2500, 1600, 1000])
    assert not any('Модификация:' in text and 'Мощность' in text for text in texts)


def test_scalar_lists_are_not_mistaken_for_records():
    doc = Document()
    DOCXGenerator()._add_specifications(doc, {'Цвет': ['белый', 'серый'], 'Масса, кг': '10'})
    assert doc.tables[0].cell(1, 1).text == 'белый; серый'


def test_conflicting_json_keys_are_not_silently_overwritten():
    assert _robust_json_extract('{"product":{"specifications":{"ОВ16":{"Q":"8780"},"ОВ16":{"Q":"10580"}}}}') is None
    assert _robust_json_extract('{"product":{"a":"1","a":"1"}}') == {'product': {'a': '1'}}


def test_russian_key_normalization_preserves_collisions():
    ai = {'product': {'specifications': {'nominal_voltage': '6000', 'rated_voltage': '380'}}}
    _normalize_spec_keys_ru(ai)
    assert ai['product']['specifications'] == {'nominal_voltage': '6000', 'rated_voltage': '380'}


def test_structured_grid_keeps_cell_after_continuing_rowspan():
    from product_tables import _grid
    conv = MarkdownConverter()
    soup = BeautifulSoup('<table><tr><td rowspan="2">A</td><td>B</td></tr>'
                         '<tr><td>C</td><td>D</td></tr></table>', 'lxml')
    _, grid = _grid(soup.table, conv)
    assert [[c.get_text() if c else '' for c in row] for row in grid] == [['A', 'B', ''], ['A', 'C', 'D']]


def test_blocks_br_sup_and_pipe_stay_inside_their_cells():
    conv = MarkdownConverter()
    conv.structured_tables = True
    md = conv.convert('<table><tr><td>Параметр</td><td>Значение</td></tr>'
                      '<tr><td>Расход, м<sup>3</sup>/ч</td><td><p>315</p><p>400|500<br>600</p></td></tr></table>')
    assert 'м^3/ч' in md and '315<br>400\\|500<br>600' in md
    assert len([line for line in md.splitlines() if line.startswith('|')]) == 3


def test_ambiguous_recognized_table_fails_before_mutating_product():
    ai, html, url = load('sverd_130')
    soup = BeautifulSoup(html, 'lxml')
    soup.table.find_all('tr')[1].find_all('td')[1].string = 'Марка насоса'
    before = deepcopy(ai)
    with pytest.raises(TableStructureError):
        restore_source_tables(ai, str(soup), url)
    assert ai == before


@pytest.mark.parametrize('case', ['sverd_aliases', 'sverd_horizontal', 'sverd_mass_columns', 'tsila_bad_colspan'])
def test_real_shared_headers_and_broken_colspan_do_not_drop_columns(case):
    ai, html, url = load(case)
    layouts = restore_source_tables(ai, html, url)
    assert len(layouts) >= 2
    specs = ai['product']['specifications']
    for layout in layouts:
        records = specs[layout['path']]
        assert all(set(row) == set(layout['columns']) for row in records.values())
    if case == 'sverd_mass_columns':
        pump = specs['Характеристики насоса']
        assert len(pump) == 1
        values = list(next(iter(pump.values())).values())
        assert '4925' in values and '6170' in values
    if case == 'tsila_bad_colspan':
        assert len(specs['Параметры агрегата']) == 4
        assert len(layouts[-1]['columns']) == 1
    doc = render(ai)
    assert doc.tables


@pytest.mark.parametrize('merged,expected', [(False, 2), (True, 1)])
def test_equal_values_in_distinct_cells_are_not_merged(merged, expected):
    value_cells = '<td colspan="2">315</td>' if merged else '<td>315</td><td>315</td>'
    html = ('<table><tr><td>Насос</td><td colspan="2">Электродвигатель</td></tr>'
            '<tr><td>Марка насоса</td><td colspan="2">Мощность, кВт</td></tr>'
            '<tr><td>М-100</td>' + value_cells + '</tr></table>')
    data = describe_table(BeautifulSoup(html, 'lxml').table, MarkdownConverter())
    assert len(data['headers']) - 1 == expected
    assert data['rows'][0][1:] == ['315'] * expected


def test_dimensions_outside_recognized_table_are_preserved():
    ai, html, url = load('tsila_176')
    specs = ai['product']['specifications']
    key = next(k for k, value in specs.items() if isinstance(value, dict))
    specs[key].update({'L': '2865', 'L1': '2675'})
    restore_source_tables(ai, html, url)
    assert ai['product']['specifications'][key] == {'L': '2865', 'L1': '2675'}


def test_motor_only_table_does_not_erase_separate_pump_specs():
    html = ('<table><tr><td rowspan="2">Обозначение</td><td colspan="3">Электродвигатель</td></tr>'
            '<tr><td>Марка</td><td>Мощность, кВт</td><td>Напряжение, В</td></tr>'
            '<tr><td>М100</td><td>Э100</td><td>315</td><td>6000</td></tr></table>')
    pump = {'Подача, м^3/ч': '800', 'Мощность, кВт': '250', 'КПД насоса, %': '86'}
    ai = {'product': {'specifications': {'М100': pump.copy(), 'М100 (электродвигатель)': {
        'Марка': 'Э100', 'Мощность, кВт': '315', 'Напряжение, В': '6000'}}}}
    restore_source_tables(ai, html, 'https://t-sila.ru/catalog/view/test/')
    assert ai['product']['specifications']['М100'] == pump
    assert 'М100 (электродвигатель)' not in ai['product']['specifications']


def test_mode_discriminator_does_not_change_parameter_ownership_on_second_pass():
    ai, html, url = load('sverd_130')
    html = html.replace('ВАН215/41-16У3', 'ВАН215/59-16У3')
    restore_source_tables(ai, html, url)
    first = deepcopy(ai)
    restore_source_tables(ai, html, url)
    assert ai == first
    assert len(ai['product']['specifications']['Характеристики насоса']) == 3


def test_transposed_llm_matrix_is_replaced_without_a_duplicate_copy():
    ai, html, url = load('vmp_holding')
    specs = ai['product']['specifications']
    stages = {k: specs.pop(k) for k in list(specs) if isinstance(specs[k], dict) and '-10' in specs[k]}
    for temp in next(iter(stages.values())):
        specs[temp] = {name: values[temp] for name, values in stages.items()}
    restore_source_tables(ai, html, url)
    assert all(temp not in ai['product']['specifications'] for temp in ['-10', '-5', '+35'])
    assert len(ai['product']['specifications'][ai['_table_layouts'][0]['path']]) == 4


@pytest.mark.parametrize('extra_dimension', [False, True])
def test_live_slash_joined_model_names_do_not_leave_a_duplicate_mixed_table(extra_dimension):
    data = json.loads((FIXTURES / 'ural_ov87_live.json').read_text())
    ai = data['ai']
    first_key = next(iter(ai['product']['specifications']))
    if extra_dimension:
        ai['product']['specifications'][first_key]['L'] = '2240'
    html = (FIXTURES / 'ural_ov87.html').read_text()
    layouts = restore_source_tables(ai, html, data['url'])
    specs = ai['product']['specifications']
    remaining = {key: value for key, value in specs.items() if key not in {l['path'] for l in layouts}}
    assert remaining == ({first_key: {'L': '2240'}} if extra_dimension else {})
    assert len(specs['Характеристики насоса']) == len(specs['Характеристики электродвигателя']) == 5
    before = deepcopy(ai)
    restore_source_tables(ai, html, data['url'])
    assert ai == before
