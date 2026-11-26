"""Перечни карточки разделяются точкой с запятой, десятичные запятые сохраняются."""
import asyncio
from copy import deepcopy

import pytest
from docx import Document

from docx_generator import DOCXGenerator


@pytest.mark.parametrize('value,expected', [
    ('E1, E2', 'E1; E2'),
    ('E 0.5, E1, E2', 'E 0.5; E1; E2'),
    ('P3,P5', 'P3; P5'),
    ('Гипс, древесная стружка, остаточная влага',
     'Гипс; древесная стружка; остаточная влага'),
    ('1220х3060 мм, 1220х4200 мм', '1220х3060 мм; 1220х4200 мм'),
    ('1220х3060,1220х4200', '1220х3060; 1220х4200'),
    ('4, 5,5, 6, 7 мм', '4; 5,5; 6; 7 мм'),
    ('30-40,25-35,20-30 %', '30-40; 25-35; 20-30 %'),
    ('0,55 0,7 1,0', '0,55; 0,7; 1,0'),
    ('0.5,1.0,2', '0.5; 1.0; 2'),
    ('1/2", 3/4"', '1/2"; 3/4"'),
    ('белый;серый, черный', 'белый; серый; черный'),
    ('4\n5,5\n6\n7 мм', '4; 5,5; 6; 7 мм'),
    (['4', '5,5', '6', '7'], '4; 5,5; 6; 7'),
    (['белый, серый', None, '', 'черный'], 'белый; серый; черный'),
    ({'Толщина, мм': ['4', '5,5'], 'Цвет': 'белый, серый'},
     'Толщина, мм: 4; 5,5; Цвет: белый; серый'),
    ('- белый;\n- серый.', 'белый; серый.'),
])
def test_table_value_uses_semicolons(value, expected):
    gen = DOCXGenerator()
    doc = Document()
    gen._add_two_column_table(doc, [('Значения', value)])
    assert doc.tables[0].cell(1, 1).text == expected


@pytest.mark.parametrize('value', [
    '0,35', '0,3-0,4 МПа', '5,5 мм', '8,5', '10 000', '07 11', '1.234,56',
    '20Х13', 'ГОСТ 10632-2007', '4,5,5,6,7 мм',
    'сталь (железо, углерод)', 'ООО «Завод, СПб»',
    'диапазоны (30-40,25-35)', 'обозначение «0.5,1.0»',
])
def test_single_values_and_grouped_commas_are_preserved(value):
    gen = DOCXGenerator()
    doc = Document()
    gen._add_two_column_table(doc, [('Параметр, единица', value)])
    row = doc.tables[0].rows[1]
    assert row.cells[0].text == 'Параметр, единица'
    assert row.cells[1].text == gen._clean_text(value)


@pytest.mark.parametrize('count,param_count', [(2, 2), (8, 2), (8, 6)])
def test_every_multi_column_layout_formats_values(count, param_count):
    gen = DOCXGenerator()
    doc = Document()
    attrs = {f'Параметр {i}, мм': '4, 5,5, 6' for i in range(param_count)}
    gen._add_specifications(doc, {f'Марка {i}': attrs.copy() for i in range(count)})
    cells = [c.text for t in doc.tables for r in t.rows for c in r.cells]
    assert cells.count('4; 5,5; 6') == count * param_count
    assert 'Параметр 0, мм' in cells


def test_markdown_values_and_headers_have_separate_rules():
    gen = DOCXGenerator()
    doc = Document()
    gen._add_generic_field(doc, 'Комплектация',
                           '| Параметр, единица | Значение, тип |\n'
                           '| --- | --- |\n'
                           '| Класс, обозначение | P3, P5 |\n'
                           '| Толщина, мм | 4, 5,5, 6 |')
    assert [[c.text for c in r.cells] for r in doc.tables[0].rows] == [
        ['Параметр, единица', 'Значение, тип'],
        ['Класс, обозначение', 'P3; P5'],
        ['Толщина, мм', '4; 5,5; 6'],
    ]


def test_subitem_values_and_legacy_table_keep_the_same_separator():
    gen = DOCXGenerator()
    doc = Document()
    key = 'Время, с: — для главных ножей; — для заземляющих ножей'
    gen._build_specifications_table(doc, {key: '3 1', 'Классы': ['P3', 'P5']})
    assert doc.tables[0].cell(0, 1).text == '3; 1'
    assert doc.tables[0].cell(1, 1).text == 'P3; P5'


def test_card_formats_lists_without_changing_input_or_prose(tmp_path):
    gen = DOCXGenerator()

    async def no_rtf(path):
        return None
    gen._convert_to_rtf = no_rtf
    prose = 'ДВП — листовой материал, изготавливается прессованием.'
    data = {'product_id': 'test', 'source_url': 'https://example.ru/p/?a=1,2',
            'json_data': {'product': {
                'product_name': 'Плиты (Завод, ООО)',
                'description': prose,
                'specifications': {'Класс эмиссии': 'E1, E2',
                                   'Толщина, мм': ['4', '5,5', '6', '7']},
                'applications': 'Применяется в строительстве, мебельных конструкциях.',
                'advantages': '- экологическая безопасность\n- широкий размерный ряд.',
                'complectation': '- Корпус, крышка, крепеж.\n- Шайбы 4 шт.,',
                'instructions_manuals': 'Нанести состав.\n\nВыдержать, затем выровнять.',
                'variants': [{'name': 'Белая', 'attributes': {}},
                             {'name': 'Серая', 'attributes': {}}],
            }}}
    original = deepcopy(data)
    path = asyncio.run(gen.generate_product_card(data, {}, str(tmp_path)))
    doc = Document(path)
    texts = [p.text for p in doc.paragraphs]
    assert '- экологическая безопасность;\n- широкий размерный ряд;' in texts
    assert '- Корпус, крышка, крепеж;\n- Шайбы: 4 шт.;' in texts
    assert '- Белая;' in texts and '- Серая;' in texts
    assert prose in texts
    assert 'Применяется в строительстве, мебельных конструкциях.' in texts
    assert 'Нанести состав.\n     \nВыдержать, затем выровнять.' in texts
    assert 'URL страницы: https://example.ru/p/?a=1,2' in texts
    rows = [[c.text for c in r.cells] for r in doc.tables[0].rows]
    assert ['Класс эмиссии', 'E1; E2'] in rows
    assert ['Толщина, мм', '4; 5,5; 6; 7'] in rows
    assert data == original
