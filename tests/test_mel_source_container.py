from pathlib import Path
from bs4 import BeautifulSoup
from text_extractor import html_to_markdown

W = Path(__file__).resolve().parents[2]
HTML = (W / 'cards_final_fix3_resume/98fd8c8f35dbdd69/source.html').read_text()
URL = 'https://www.zavodmel.ru/products/vysokovoltnoe-oborudovanie/yashchik-sobstvennykh-nuzhd-yasn-v/'


def test_real_visible_mel_product_is_not_replaced_with_related_pump():
    md = html_to_markdown(HTML, URL)
    for term in ['100', '~ 50', '380/220/12', '320х177х550', '16']:
        assert term in md
    assert 'Ящик предназначен для подключения' in md
    assert 'HidroLift' not in md


def test_unlinked_hidden_product_data_is_not_opened_by_container_fix():
    soup = BeautifulSoup(HTML, 'lxml')
    for panel in soup.select('.product-data__hidden'):
        panel['style'] = 'display:none'
    md = html_to_markdown(str(soup), URL)
    assert '380/220/12' not in md and '320х177х550' not in md
