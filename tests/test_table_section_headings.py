"""Один заголовок и одно одинаковое описание перед таблицами раздела."""
from copy import deepcopy
import json
from pathlib import Path

from docx import Document
import pytest

from docx_generator import DOCXGenerator
from product_tables import restore_source_tables


FIXTURES = Path(__file__).parent / 'fixtures/product_tables'


@pytest.mark.parametrize('case', ['vmp_holding', 'vmp_anticor'])
def test_drying_title_and_axis_appear_once_before_all_three_tables(case):
    data = json.loads((FIXTURES / f'{case}.json').read_text())
    ai = data['ai']
    restore_source_tables(ai, (FIXTURES / f'{case}.html').read_text(), data['url'])
    before = deepcopy(ai)
    layout = ai['_table_layouts'][0]
    generator = DOCXGenerator()
    doc = Document()
    generator._add_specifications(doc, ai['product']['specifications'], ai['_table_layouts'])
    assert ai == before
    title, axis = generator._clean_text(layout['title']), generator._clean_text(layout['column_axis'])
    assert [p.text for p in doc.paragraphs].count(title) == 1
    assert [p.text for p in doc.paragraphs].count(axis) == 1
    matrices = [t for t in doc.tables if t.cell(0, 0).text == 'Степень высыхания']
    assert [len(t.columns) for t in matrices] == [5, 5, 3]
    assert sum((len(t.rows) - 1) * (len(t.columns) - 1) for t in matrices) == 40
    elements = list(doc.element.body)
    first_table = elements.index(matrices[0]._tbl)
    assert elements.index(next(p._p for p in doc.paragraphs if p.text == title)) < first_table
    assert elements.index(next(p._p for p in doc.paragraphs if p.text == axis)) < first_table


def test_horizontal_pumps_have_one_heading_per_section_and_keep_all_tables():
    data = json.loads((FIXTURES / 'sverd_horizontal.json').read_text())
    ai = data['ai']
    restore_source_tables(ai, (FIXTURES / 'sverd_horizontal.html').read_text(), data['url'])
    doc = Document()
    DOCXGenerator()._add_specifications(doc, ai['product']['specifications'], ai['_table_layouts'])
    headings = [p.text for p in doc.paragraphs]
    assert headings.count('Характеристики насоса') == 1
    assert headings.count('Характеристики электродвигателя') == 1
    assert len(doc.tables) == 5


def test_same_sections_stay_together_and_distinct_conditions_are_not_hidden():
    specs = {}
    layouts = []
    for index, (title, axis) in enumerate([
        ('Насос', 'При 20 C'), ('Двигатель', 'При 20 C'),
        ('Насос', 'При 20 C'), ('Насос', 'При 30 C')]):
        path = str(index)
        specs[path] = {'Модель': {'Q': str(index)}}
        layouts.append({'path': path, 'title': title, 'column_axis': axis,
                        'columns': ['Q'], 'labels': ['Q'], 'row_header': 'Модель'})
    generator = DOCXGenerator()
    for _ in range(2):
        doc = Document()
        generator._add_specifications(doc, specs, layouts)
        paragraphs = [p.text for p in doc.paragraphs]
        assert paragraphs.count('Насос') == paragraphs.count('Двигатель') == 1
        assert paragraphs.count('При 20 C') == 2  # Свой контекст для насоса и двигателя.
        assert paragraphs.count('При 30 C') == 1
        assert [t.cell(1, 1).text for t in doc.tables] == ['0', '2', '3', '1']
        assert paragraphs.index('При 30 C') < paragraphs.index('Двигатель')


def test_same_single_record_section_has_one_heading_for_both_tables():
    specs = {'a': {'М-1': {'Q': '10'}}, 'b': {'М-2': {'Q': '20'}}}
    layouts = [{'path': key, 'title': 'Насос', 'columns': ['Q'], 'labels': ['Подача'],
                'row_header': 'Модель'} for key in specs]
    doc = Document()
    DOCXGenerator()._add_specifications(doc, specs, layouts)
    assert [p.text for p in doc.paragraphs].count('Насос') == 1
    assert len(doc.tables) == 2 and all(len(t.columns) == 2 for t in doc.tables)
    assert [t.cell(1, 1).text for t in doc.tables] == ['М-1', 'М-2']
