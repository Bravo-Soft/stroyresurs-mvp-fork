"""Сохранённые карточки Aksa и расшифровка АОГВ только для konord.com."""
import asyncio
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from docx import Document
import pytest

from docx_generator import DOCXGenerator


FIXTURES = Path(__file__).parent / 'fixtures/aksa_konord'
GROUPS = ['Двигатель', 'Альтернатор', 'Топливная система',
          'Масляная система', 'Система охлаждения']


def load(name):
    return json.loads((FIXTURES / (name + '.json')).read_text())


def leaves(data):
    values = Counter()
    for key, value in data.items():
        if isinstance(value, dict):
            values.update(leaves(value))
        else:
            values[(key, json.dumps(value, ensure_ascii=False, sort_keys=True))] += 1
    return values


def render(tmp_path, product):
    generator = DOCXGenerator()

    async def keep_docx(path):
        return None

    generator._convert_to_rtf = keep_docx
    path = asyncio.run(generator.generate_product_card(product, {}, str(tmp_path)))
    assert path
    return Path(path), Document(path)


def cells(doc):
    return [cell.text for table in doc.tables for row in table.rows for cell in row.cells]


def test_ac1410_has_all_groups_and_keeps_engine_model_and_source_data(tmp_path):
    product = load('ac1410')
    original = deepcopy(product)
    _, doc = render(tmp_path, product)
    text = cells(doc)
    for group in GROUPS:
        # Объединённая строка представлена двумя объектами ячейки в python-docx.
        assert text.count(group) == 2
    assert 'KTA50G3' in text
    assert not any('KTA50;' in value for value in text)
    assert product == original


@pytest.mark.parametrize('model', ['KTA50G3', 'KTA38G5', '6BTA5.9G5',
                                  '6BTAA5,9G6', 'QSX15G8', 'K19G4'])
def test_model_field_keeps_literal_code_in_two_column_table(model):
    doc = Document()
    DOCXGenerator()._add_two_column_table(doc, [('Модель двигателя', model)])
    assert doc.tables[0].rows[1].cells[1].text == model


@pytest.mark.parametrize('count,attributes', [(2, 1), (8, 1), (8, 7)])
def test_model_codes_are_kept_in_all_multi_column_layouts(count, attributes):
    doc = Document()
    specs = {f'Вариант {n}': {'Модель двигателя': 'KTA50G3',
             **{f'Параметр {i}': str(i) for i in range(attributes)}} for n in range(count)}
    DOCXGenerator()._build_multi_column_table(doc, '', specs)
    assert cells(doc).count('KTA50G3') == count


def test_legacy_class_lists_dimensions_units_and_powers_are_unchanged():
    generator = DOCXGenerator()
    assert generator._split_spec_value('Р2Р3Р5') == ['Р2; Р3; Р5']
    assert generator._split_spec_value('10см') == ['10 см']
    assert generator._split_spec_value('10х20х30') == ['10х20х30']
    assert generator._split_spec_value('м^3') == ['м^3']


def test_aksa_flat_groups_preserve_every_key_value_and_do_not_mutate_input():
    product = load('ac1410')
    specifications = product['ai_raw_text']['product']['specifications']
    original = deepcopy(specifications)
    grouped = DOCXGenerator._group_aksa_specifications(specifications, product['source_url'])
    assert all(isinstance(grouped[group], dict) and grouped[group] for group in GROUPS)
    assert leaves(grouped) == leaves(original)
    assert specifications == original
    assert DOCXGenerator._group_aksa_specifications(grouped, product['source_url']) == grouped


def test_existing_ad1015_groups_are_unchanged():
    product = load('ad1015_canopy')
    specifications = product['ai_raw_text']['product']['specifications']
    assert DOCXGenerator._group_aksa_specifications(specifications, product['source_url']) == specifications


def test_hybrid_groups_unknown_values_and_conflicts_are_not_lost():
    specifications = {'Двигатель': {'Модель двигателя': 'KTA50G3'},
                      'Модель двигателя': 'KTA50G8', 'Топливный бак': '2000 л',
                      'Неизвестный параметр': {'Особенность': '123'}}
    grouped = DOCXGenerator._group_aksa_specifications(specifications, 'https://aksa.com.ru/product/x')
    assert leaves(grouped) == leaves(specifications)
    assert grouped['Модель двигателя'] == 'KTA50G8'
    assert grouped['Двигатель']['Модель двигателя'] == 'KTA50G3'
    assert grouped['Неизвестный параметр'] == specifications['Неизвестный параметр']


@pytest.mark.parametrize('url', ['', 'https://other.ru/', 'https://aksa.com.ru.other.ru/product/x'])
def test_other_sites_keep_specifications(url):
    specifications = load('ac1410')['ai_raw_text']['product']['specifications']
    assert DOCXGenerator._group_aksa_specifications(specifications, url) == specifications


@pytest.mark.parametrize('name', ['АОГВ-11,6-3', 'Котел АОГВ-11,6-1', 'Котёл АОГВ-17,4-1'])
def test_konord_name_expands_aogv_and_keeps_model(name):
    model = name.split()[-1]
    expected = 'Аппарат отопительный газовый водогрейный ' + model
    actual = DOCXGenerator._expand_product_name(name, 'https://www.konord.com/catalog/aogv/x')
    assert actual == expected
    assert DOCXGenerator._expand_product_name(actual, 'https://konord.com/') == actual


@pytest.mark.parametrize('name,url', [('АОГВК-11,6-1', 'https://konord.com/'),
                                     ('АКГВ-11,6-3', 'https://konord.com/'),
                                     ('АОГВ-11,6-1', 'https://other.ru/'),
                                     ('АОГВ-11,6-1', 'https://konord.com.other.ru/')])
def test_other_abbreviations_and_domains_are_unchanged(name, url):
    assert DOCXGenerator._expand_product_name(name, url) == name


def test_konord_title_and_filename_expand_without_changing_id_or_input(tmp_path):
    product = load('konord_aogv')
    original = deepcopy(product)
    path, doc = render(tmp_path, product)
    expected = 'Аппарат отопительный газовый водогрейный АОГВ-11,6-3'
    assert expected in [p.text for p in doc.paragraphs]
    assert path.name.startswith(expected)
    path.name.encode('cp1251')
    assert product == original
