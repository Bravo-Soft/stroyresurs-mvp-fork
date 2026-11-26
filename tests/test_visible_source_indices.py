from pathlib import Path
from bs4 import BeautifulSoup
from visible_product_sections import _text
FIXTURES=Path(__file__).parent/'fixtures/npo_material_indices'


def test_real_resistivity_preserves_superscript_range():
    node=BeautifulSoup((FIXTURES/'resistivity.html').read_text(),'lxml').p
    text=_text(node)
    assert '10^{17}' in text and '10^{18}' in text
    assert '10 17' not in text and '10 18' not in text


def test_real_acid_formula_preserves_attached_subscripts():
    node=BeautifulSoup((FIXTURES/'chemical.html').read_text(),'lxml').p
    text=_text(node)
    assert 'H3PO3' in text
    assert 'H 3' not in text and 'PO 3' not in text
