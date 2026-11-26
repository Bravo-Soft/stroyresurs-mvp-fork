"""Номера ГОСТ, цвета из сохранённых ответов и изоляция diagnostic additional_info."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

from docx import Document
import pytest

from docx_generator import DOCXGenerator


FIXTURES = Path(__file__).parent / 'fixtures/gost_colors'


def load(name):
    return json.loads((FIXTURES / (name + '.json')).read_text())


def render(tmp_path, product):
    generator = DOCXGenerator()

    async def keep_docx(path):
        return None

    generator._convert_to_rtf = keep_docx
    path = asyncio.run(generator.generate_product_card(product, {}, str(tmp_path)))
    assert path
    return Document(path)


def cells(doc):
    return [c.text for t in doc.tables for r in t.rows for c in r.cells]


@pytest.mark.parametrize('standard', [
    'ГОСТ 12,2.020-76',
    'ГОСТ Р 12,2.020-76',
    'ГОСТ Р ИСО/МЭК 12,2.020-2012',
    'ГОСТ 12, 2.020 - 76',
])
def test_gost_identifier_keeps_its_original_punctuation(standard):
    generator = DOCXGenerator()
    assert generator._format_table_value(standard) == generator._clean_text(standard)


def test_gost_identifiers_and_ordinary_lists_use_different_separators():
    value = 'ГОСТ 12,2.020-76, ГОСТ 12.1.007-76; P3, P5; 0.5,1.0,2'
    assert DOCXGenerator()._format_table_value(value) == (
        'ГОСТ 12,2.020-76; ГОСТ 12.1.007-76; P3; P5; 0.5; 1.0; 2')


@pytest.mark.parametrize('count,attributes', [(2, 1), (8, 1), (8, 7)])
def test_gost_identifier_survives_all_multi_column_layouts(count, attributes):
    doc = Document()
    specs = {f'Исполнение {n}': {'Норматив': 'ГОСТ 12,2.020-76',
             **{f'Параметр {i}': str(i) for i in range(attributes)}} for n in range(count)}
    DOCXGenerator()._add_specifications(doc, specs)
    assert cells(doc).count('ГОСТ 12,2.020-76') == count


def test_saved_fobos_card_preserves_standard_and_original_json(tmp_path):
    product = load('fobos')
    original = deepcopy(product)
    doc = render(tmp_path, product)
    assert 'ГОСТ 12,2.020-76' in '\n'.join(cells(doc))
    assert 'ГОСТ 12; 2.020-76' not in '\n'.join(cells(doc))
    assert product == original


def test_saved_kvadroblok_card_has_all_five_colors_in_a_separate_block(tmp_path):
    product = load('kvadroblok')
    original = deepcopy(product)
    doc = render(tmp_path, product)
    paragraphs = [p.text for p in doc.paragraphs]
    index = paragraphs.index('Цвета')
    content = next(p for p in doc.paragraphs[index + 1:] if p.text.strip())
    text = content.text
    for color in ['Лондонбрик', 'Амстердам', 'Ситибрик', 'Шварцбраун', 'Черный']:
        assert text.count(color) == 1
    assert '[' not in text and ']' not in text
    assert content.paragraph_format.first_line_indent == 0
    assert product == original


@pytest.mark.parametrize('colors,expected', [
    ([], None),
    (['', '  '], None),
    (['Чёрный'], 'Черный'),
    (['RAL 9005', 'RAL 9016'], '- RAL 9005;\n- RAL 9016;'),
])
def test_colors_block_handles_empty_single_and_multiple_colors(tmp_path, colors, expected):
    product = load('kvadroblok')
    product['ai_raw_text']['product']['colors'] = colors
    doc = render(tmp_path, product)
    paragraphs = [p.text for p in doc.paragraphs]
    if expected is None:
        assert 'Цвета' not in paragraphs
    else:
        assert paragraphs.count('Цвета') == 1
        assert next(p for p in paragraphs[paragraphs.index('Цвета') + 1:] if p.strip()) == expected


def test_additional_info_remains_diagnostic_and_is_not_rendered(tmp_path):
    product = load('kvadroblok')
    product['ai_raw_text']['product']['additional_info'] = {
        'text': 'ДИАГНОСТИКА: неклассифицированный материал; давление 777 МПа'}
    original = deepcopy(product)
    doc = render(tmp_path, product)
    text = '\n'.join([p.text for p in doc.paragraphs] + cells(doc))
    assert 'Дополнительная информация' not in text
    assert 'ДИАГНОСТИКА' not in text and '777 МПа' not in text
    assert product == original
