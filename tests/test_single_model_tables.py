"""Одна модель: один заголовок и одна таблица параметров на каждую группу."""
from copy import deepcopy
import json
from pathlib import Path

from docx import Document

from docx_generator import DOCXGenerator
from product_tables import restore_source_tables


FIXTURES = Path(__file__).parent / 'fixtures/product_tables'


def load_og5():
    data = json.loads((FIXTURES / 'sverd_mass_columns.json').read_text())
    return data['ai'], (FIXTURES / 'sverd_mass_columns.html').read_text(), data['url']


def test_og5_has_exactly_one_two_column_table_per_group_and_no_legacy_copy():
    ai, html, url = load_og5()
    name = ai['product']['product_name']
    restore_source_tables(ai, html, url)
    specs = ai['product']['specifications']
    assert set(specs) == {'Характеристики насоса', 'Характеристики электродвигателя'}
    assert ai['product']['product_name'] == name
    once = deepcopy(ai)
    restore_source_tables(ai, html, url)
    assert ai == once

    doc = Document()
    DOCXGenerator()._add_specifications(doc, specs, ai['_table_layouts'])
    headings = [p.text for p in doc.paragraphs]
    assert headings.count('Характеристики насоса') == 1
    assert headings.count('Характеристики электродвигателя') == 1
    assert len(doc.tables) == 2
    assert all(len(table.columns) == 2 for table in doc.tables)
    assert [cell.text for cell in doc.tables[0].rows[0].cells] == ['Параметр', 'Значение']
    pump = [(row.cells[0].text, row.cells[1].text) for row in doc.tables[0].rows[1:]]
    motor = [(row.cells[0].text, row.cells[1].text) for row in doc.tables[1].rows[1:]]
    assert ('Подача, м^3/ч', '9000') in pump
    assert ('КПД насоса, %', '84') in pump
    assert [value for parameter, value in pump if parameter.startswith('Масса насоса')] == ['4925', '6170']
    assert ('Марка', 'А4-450У-12У3') in motor
    assert ('Мощность, кВт', '315') in motor
    assert ('Масса электродвигателя, кг', '2790') in motor
    assert len(pump) == 8 and len(motor) == 6


def test_descriptive_pump_prefix_keeps_parameters_not_replaced_by_source():
    ai, html, url = load_og5()
    extra = {'Насосы горизонтальные осевые: Габарит L, мм': '1234', 'Материал корпуса': 'сталь'}
    ai['product']['specifications']['ОГ5-87'].update(extra)
    restore_source_tables(ai, html, url)
    assert ai['product']['specifications']['ОГ5-87'] == extra


def test_single_record_keeps_identity_and_note_once():
    doc = Document()
    records = {'М-100 / Э-100': {'Насос: Подача': '100', 'Насос: Напор': '20'}}
    layout = {'title': 'Характеристики насоса', 'row_header': 'Модель / комплектация',
              'columns': ['Насос: Подача', 'Насос: Напор'], 'labels': ['Подача', 'Напор'],
              'notes': ['Условие эксплуатации']}
    DOCXGenerator()._render_source_table(doc, records, layout)
    assert len(doc.tables) == 1 and len(doc.tables[0].columns) == 2
    rows = [[cell.text for cell in row.cells] for row in doc.tables[0].rows]
    assert rows == [['Параметр', 'Значение'], ['Модель / комплектация', 'М-100 / Э-100'],
                    ['Подача', '100'], ['Напор', '20']]
    assert sum('Условие эксплуатации' in p.text for p in doc.paragraphs) == 1
