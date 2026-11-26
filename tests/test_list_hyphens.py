"""Дефис отделяет пункты перечня, одиночный текст и инструкция идут без него."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import re

from docx import Document
import pytest

from docx_generator import DOCXGenerator


def field_text(name, value):
    doc = Document()
    DOCXGenerator()._add_generic_field(doc, name, value)
    return doc.paragraphs[2].text


@pytest.mark.parametrize('marker', ['-', '–', '—', '•', '*', '·'])
def test_single_description_has_no_list_marker(marker):
    value = 'Основа и отвердитель, упакованные в металлические ведра и\nметаллические банки в зависимости от веса комплекта;'
    assert field_text('Комплектация', marker + ' ' + value) == value


@pytest.mark.parametrize('name', ['Комплектация', 'Преимущества', 'Область применения',
                                'Условия эксплуатации', 'Меры безопасности'])
def test_single_item_is_plain_text(name):
    assert field_text(name, '- Крышка;') == 'Крышка;'


def test_multiple_components_and_quantities_remain_a_list():
    assert field_text('Комплектация', '- крышка;\n- прокладка;\n- контейнер.') == (
        '- крышка;\n- прокладка;\n- контейнер;')
    assert field_text('Комплектация', '- Блоков в поддоне 100 шт.;\n- Блоков в м^3 53,33 шт.;') == (
        '- Блоков в поддоне: 100 шт.;\n- Блоков в м^3: 53,33 шт.;')


def test_instructions_keep_text_headings_wraps_and_punctuation():
    value = ('Подготовка поверхности:\n- обезжирить по ГОСТ 9.402;\n'
             '- рекомендуемая: очистка до степени 2 (Sa 2 1/2);\n- обеспылить.\n\n'
             'Перед применением перемешать.\n- Наносить в 1-2 слоя\n'
             '  при температуре от -10 до +35 C.\n1. Высушить покрытие.')
    expected = ('Подготовка поверхности:\nобезжирить по ГОСТ 9.402;\n'
                'рекомендуемая: очистка до степени 2 (Sa 2 1/2);\nобеспылить.\n\n'
                'Перед применением перемешать.\nНаносить в 1-2 слоя\n'
                'при температуре от -10 до +35 C.\n1. Высушить покрытие;')
    assert field_text('Инструкции по применению', value) == DOCXGenerator()._clean_text(expected)


def test_numbering_negative_values_internal_hyphens_and_plain_text_are_preserved():
    for value in ('Основа и отвердитель.', 'ВИНИКОР-061 — грунтовка.',
                  '-10 C', '1. Крышка;\n2. Прокладка;'):
        assert field_text('Комплектация', value) == DOCXGenerator()._clean_text(value)


def test_instructions_markdown_table_is_unchanged():
    doc = Document()
    DOCXGenerator()._add_generic_field(doc, 'Инструкции по применению',
        '- Перемешать основу.\n| Слой | Время, ч |\n| --- | --- |\n| 1-2 | 24 |\n- Нанести покрытие.')
    assert [p.text for p in doc.paragraphs if p.text][-2:] == ['Перемешать основу.', 'Нанести покрытие.']
    assert [[c.text for c in row.cells] for row in doc.tables[0].rows] == [
        ['Слой', 'Время, ч'], ['1-2', '24']]


@pytest.mark.parametrize('fixture', ['vmp_anticor', 'vmp_holding'])
def test_vinikor_card_keeps_source_and_all_content(tmp_path, fixture):
    ai = json.loads((Path(__file__).parent / 'fixtures/product_tables' / (fixture + '.json')).read_text())['ai']
    original = deepcopy(ai)
    gen = DOCXGenerator()

    async def keep_docx(path):
        return None

    gen._convert_to_rtf = keep_docx
    output = asyncio.run(gen.generate_product_card(
        {'product_id': fixture, 'ai_raw_text': ai}, {}, str(tmp_path)))
    assert output
    paragraphs = [p.text for p in Document(output).paragraphs]
    for title, key in [('Комплектация', 'complectation'), ('Инструкции по применению', 'instructions_manuals')]:
        rendered = paragraphs[paragraphs.index(title) + 2]
        assert not re.search(r'^\s*[-–—•*·]\s+', rendered, flags=re.MULTILINE)
        # Удалены только маркеры: слова, числа, знаки и порядок текста прежние.
        without_markers = re.sub(r'(?m)^[ \t]*[-–—•*·][ \t]+', '', ai['product'][key])
        norm = lambda s: re.sub(r'\s+', ' ', s).strip()
        assert norm(rendered) == norm(gen._clean_text(without_markers))
    assert ai == original
