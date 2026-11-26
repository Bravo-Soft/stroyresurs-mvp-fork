"""Регрессии точных обозначений и символов аудита 06.11.2026."""
import pytest
from docx import Document
from docx_generator import DOCXGenerator

@pytest.mark.parametrize('index,amount', [('А 1','22,9'), ('А 2','0,6'), ('Б 2','1,0')])
def test_component_index_is_not_quantity_range(index, amount):
    doc = Document()
    DOCXGenerator()._add_generic_field(doc, 'Комплектация',
        f'- Компонент {index} – {amount} кг\n- Стандартная упаковка – 24,5 кг')
    assert f'Компонент {index}: {amount} кг' in '\n'.join(p.text for p in doc.paragraphs)

@pytest.mark.parametrize('degree', ['ᵒ', '°', '◦', '⁰'])
def test_degree_variants_are_target_ascii(degree):
    assert DOCXGenerator()._clean_text(f'температура до 150 {degree}С') == 'температура до 150 C'

def test_quantity_range_remains_range():
    assert DOCXGenerator()._normalize_complectation_text('- Шайбы 4–6 шт.') == '- Шайбы: 4–6 шт.'

@pytest.mark.parametrize("value,expected", [("σв, МПа", "сигмав, МПа"), ("ε, %", "эпсилон, %")])
def test_greek_parameter_survives_cp1251(value, expected):
    rendered = DOCXGenerator()._clean_text(value)
    assert rendered == expected
    assert rendered.encode("cp1251").decode("cp1251") == expected
