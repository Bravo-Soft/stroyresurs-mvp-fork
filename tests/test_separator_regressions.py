"""Регрессии, обнаруженные корпусом и настоящей RTF Плитспичпрома."""
import asyncio

from docx import Document
import pytest

from docx_generator import DOCXGenerator


@pytest.mark.parametrize('specifications,expected', [
    ({'Температура нанесения': 'Склеивание допустимо при низкой температуре, при этом время изменится'},
     'Склеивание допустимо при низкой температуре, при этом время изменится'),
    ({'Реакция на огонь': 'Отрицательная, если состав наносится на невоспламеняющуюся поверхность.'},
     'Отрицательная, если состав наносится на невоспламеняющуюся поверхность.'),
    ({'Толщина, мм': {'16': {'Плотность': '750'}}}, 'Толщина, мм'),
    ({'Блок': {'Марка': {'Толщина': '4, 5,5, 6'}}}, '4; 5,5; 6'),
    ({'Блок': {'Марка': {'Толщина': [4, 6, 7]}}}, '4; 6; 7'),
    ({'Блок': {'Марка': {'Покрытие': 'Рельеф «Гранит, кварц»'}}}, 'Рельеф Гранит, кварц'),
    ({'Марка': 'P3, P5'}, 'P3; P5'),
])
def test_table_values_keep_their_semantic_boundaries(specifications, expected):
    doc = Document()
    DOCXGenerator()._add_specifications(doc, specifications)
    cells = [c.text for t in doc.tables for r in t.rows for c in r.cells]
    assert expected in cells


def test_wrapped_list_item_has_separator_after_its_last_line(tmp_path):
    gen = DOCXGenerator()

    async def docx_only(path):
        return None
    gen._convert_to_rtf = docx_only
    data = {'product_id': 'wrapped', 'json_data': {'product': {
        'product_name': 'Плиты (Завод)', 'specifications': {'Прочность': '10 МПа'},
        'advantages': '- Быстрый монтаж,\n  даже при низкой температуре\n- Высокая прочность'}}}
    path = asyncio.run(gen.generate_product_card(data, {}, str(tmp_path)))
    assert ('- Быстрый монтаж,\nдаже при низкой температуре;\n- Высокая прочность;'
            in [p.text for p in Document(path).paragraphs])


def test_dict_keys_inside_one_cell_are_cleaned_like_table_headers():
    doc = Document()
    DOCXGenerator()._add_specifications(doc, {
        'Базовые варианты исполнения': [
            {'Диапазон температур, [°C]': 'от -50 до 120',
             'Длина монтажной части L, [мм]': '70, 98, 133, 223'},
        ],
    })
    text = doc.tables[0].cell(1, 1).text
    assert 'Диапазон температур, C:' in text
    assert 'Длина монтажной части L, мм: 70; 98; 133; 223' in text
    assert '[' not in text and ']' not in text and '°' not in text


def test_white_circle_used_as_degree_is_representable_in_rtf():
    doc = Document()
    gen = DOCXGenerator()
    gen._add_specifications(doc, {'Температура сушки': 'не выше 65◦С'})
    text = doc.tables[0].cell(1, 1).text
    assert text == 'не выше 65 C'
    assert text.encode('cp1251').decode('cp1251') == text
