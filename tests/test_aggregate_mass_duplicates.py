"""Старые пронумерованные поля не дублируют восстановленную массу агрегата."""
from copy import deepcopy
import json
from pathlib import Path

from docx import Document
import pytest

from docx_generator import DOCXGenerator
from product_tables import restore_source_tables


FIXTURES = Path(__file__).parent / 'fixtures/product_tables'


def load():
    data = json.loads((FIXTURES / 'tsila_bad_colspan.json').read_text())
    return data['ai'], (FIXTURES / 'tsila_bad_colspan.html').read_text(), data['url']


def test_50vo_mass_appears_only_in_the_aggregate_table():
    ai, html, url = load()
    layouts = restore_source_tables(ai, html, url)
    specs = ai['product']['specifications']
    assert set(specs) == {
        'Подача, м^3/ч', 'Напор, м', 'Частота вращения, об/мин', 'Мощность, кВт',
        'Условный проход напорного патрубка, см', 'Категория размещения',
        'Характеристики насоса', 'Характеристики электродвигателя', 'Параметры агрегата',
    }
    assert specs['Параметры агрегата'] == {
        '50ВО-0,6/10': {'Агрегат: Масса агрегата, кг': '1850'},
        '50ВО-0,6/8,8 / АН102-6-ОМ5': {'Агрегат: Масса агрегата, кг': '1590'},
        '50ВО-0,6/8,8 / АН111-6-ОМ5': {'Агрегат: Масса агрегата, кг': '1820'},
        '50ВО-0,46/5,2': {'Агрегат: Масса агрегата, кг': '1460'},
    }
    doc = Document()
    DOCXGenerator()._add_specifications(doc, specs, layouts)
    mass_tables = [t for t in doc.tables if any('Масса агрегата' in c.text
                   for row in t.rows for c in row.cells)]
    assert len(mass_tables) == 1
    assert [[c.text for c in row.cells] for row in mass_tables[0].rows] == [
        ['Обозначение', 'Масса агрегата, кг'],
        ['50ВО-0,6/10', '1850'],
        ['50ВО-0,6/8,8 / АН102-6-ОМ5', '1590'],
        ['50ВО-0,6/8,8 / АН111-6-ОМ5', '1820'],
        ['50ВО-0,46/5,2', '1460'],
    ]
    assert [p.text for p in doc.paragraphs].count('Параметры агрегата') == 1
    once = deepcopy(ai)
    restore_source_tables(ai, html, url)
    assert ai == once


@pytest.mark.parametrize('label,value', [
    ('Масса насоса, кг (1)', '1850'),
    ('Масса электродвигателя, кг (1)', '1850'),
    ('Масса агрегата, кг (3)', '9999'),
    ('Масса, кг (1)', '1850'),
    ('L (1)', '1850'),
])
def test_numbered_fields_without_matching_source_label_and_value_survive(label, value):
    ai, html, url = load()
    ai['product']['specifications']['50ВО-0,6/10'][label] = value
    restore_source_tables(ai, html, url)
    assert ai['product']['specifications']['50ВО-0,6/10'] == {label: value}


def test_numbered_fields_with_explicit_other_owner_survive():
    ai, html, url = load()
    ai['product']['specifications']['50ВО-0,6/10']['Насос: Масса агрегата, кг (1)'] = '1850'
    restore_source_tables(ai, html, url)
    assert ai['product']['specifications']['50ВО-0,6/10'] == {
        'Насос: Масса агрегата, кг (1)': '1850',
    }
