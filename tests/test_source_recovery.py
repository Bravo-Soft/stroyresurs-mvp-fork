from pathlib import Path
import json
import re

import pytest
from bs4 import BeautifulSoup
from text_extractor import html_to_markdown
from product_interactions import preserve_product_panels, public_slider_panels

ROOT = Path('/ai/projects/stroy-resurs')


def test_panel_cannot_be_moved_into_navigation_content():
    html = '''<body><nav><div class="content">Меню</div></nav><section><h1>Товар</h1>
    <a aria-controls="spec" aria-expanded="false">Характеристики</a>
    <div id="spec"><table><tr><td>Плотность</td><td>1.4 кг/л</td></tr></table></div></section></body>'''
    soup = BeautifulSoup(html, 'lxml')
    assert preserve_product_panels(soup)
    assert soup.select_one('[data-product-panel]').find_parent('nav') is None
    from text_extractor import clean_noise_product
    clean_noise_product(soup)
    assert '1.4 кг/л' in soup.get_text(' ', strip=True)
    assert not preserve_product_panels(soup)


def test_all_actual_streamline_technical_tables_survive():
    corpus = json.loads((ROOT / 'codex_work/audit_runs/rest_20261106/review_b/corpus_inventory.json').read_text())['0762']
    checked = 0
    for item in corpus['pages']:
        html = (ROOT / item['source']).read_text()
        soup = BeautifulSoup(html, 'lxml')
        panel = soup.select_one('#specification')
        if panel is None or panel.find('table') is None:
            continue
        md = html_to_markdown(html, item['url'])
        for row in panel.select('table tr'):
            for cell in row.find_all(['td', 'th'], recursive=False):
                label = cell.get_text(' ', strip=True)
                if label and not cell.select_one('sup, sub') and len(label) > 5:
                    from text_extractor import _postprocess_markdown
                    label = _postprocess_markdown(label, BeautifulSoup('', 'lxml'), '')
                    assert ''.join(re.findall(r'\w+', label)) in ''.join(re.findall(r'\w+', md)), (item['url'], label)
        checked += 1
    assert checked >= 33


def test_saved_quality_eleven_individual_products_keep_own_data():
    corpus = ROOT / 'codex_work/zero_cards_fix_2026-11-06/quality_recovery/20261106_191924_597566/corpus_admission.json'
    sources = json.loads(corpus.read_text())
    assert len(sources) == 11
    for source in sources:
        md = html_to_markdown(Path(source['html']).read_text(), source['url'])
        assert source['title'] in md
        for term in source['body_container_md_terms_verified']:
            assert term in md, (source['url'], term)


@pytest.mark.parametrize('attributes', ['data-toggle="tab" href="#spec"', 'role="tab" aria-controls="spec"', 'data-toggle="collapse" data-target="#spec"'])
def test_only_user_linked_hidden_panels_are_preserved(attributes):
    html = f'''<main><h1>Товар</h1><a {attributes}>Характеристики</a>
    <div id="spec" style="display:none"><table><tr><td>Плотность</td><td>1.4 кг/л</td></tr></table></div>
    <div style="display:none"><table><tr><td>Секретная вязкость</td><td>999 мПа</td></tr></table></div></main>'''
    md = html_to_markdown(html, 'https://example.org/product')
    assert '1.4 кг/л' in md
    assert 'Секретная' not in md and '999' not in md


@pytest.mark.parametrize('hidden', ['hidden', 'style="display:none"'])
def test_unavailable_control_does_not_publish_hidden_technical_data(hidden):
    html = f'''<main><h1>Товар</h1><a {hidden} data-toggle="tab" href="#spec">Характеристики</a>
    <div id="spec" style="display:none"><table><tr><td>Секретная вязкость</td><td>999 мПа</td></tr></table></div></main>'''
    assert '999' not in html_to_markdown(html, 'https://example.org/product')


def test_unlinked_html_hidden_attribute_is_not_a_source():
    html = '<main><h1>Товар</h1><div hidden><table><tr><td>Секретная вязкость</td><td>999 мПа</td></tr></table></div></main>'
    assert '999' not in html_to_markdown(html, 'https://example.org/product')


def test_aria_hidden_alone_does_not_make_visual_control_unavailable():
    html = '<main><h1>Товар</h1><a aria-hidden="true" data-toggle="tab" href="#spec">Характеристики</a><div id="spec" hidden><p>Плотность 1.4 кг/л</p></div></main>'
    assert '1.4 кг/л' in html_to_markdown(html, 'https://example.org/product')


def test_linked_tabs_inside_product_keep_original_container():
    html = '<main><div class="product"><h1>Товар</h1><a role="tab" aria-controls="spec">Характеристики</a><div id="spec" hidden><table><tr><td>Плотность</td><td>1.4 кг/л</td></tr></table></div></div></main>'
    soup = BeautifulSoup(html, 'lxml')
    assert preserve_product_panels(soup)
    assert soup.select_one('.product [data-product-panel="spec"]') is not None
    assert '1.4 кг/л' in html_to_markdown(html, 'https://example.org/product')


def test_real_nzmk_user_slider_preserves_only_published_positions():
    path = ROOT / 'mvp_directories/Base/temp_html_storage/Новоалтайский_завод_мостовых_конструкций_ОАО/Product_pages/https-nzmk22_ru-products-bridge-beams_e828ed304e0db36a_3f22931cf1849894.html'
    html = path.read_text()
    url = 'https://nzmk22.ru/products/bridge/beams'
    md = html_to_markdown(html, url)
    assert 'Б 1200.110.95' in md and 'Б 1500.110.95' in md
    soup = BeautifulSoup(html, 'lxml')
    for script in soup.find_all('script'):
        script.decompose()
    # Тот же скрытый HTML без опубликованного обработчика не доказывает доступ.
    md_unlinked = html_to_markdown(str(soup), url)
    assert 'Б 1200.110.95' not in md_unlinked
    assert 'Б 1500.110.95' not in md_unlinked


def test_jsonld_only_facts_do_not_supplement_visible_product():
    data = {'@context': 'https://schema.org', '@type': 'Product', 'name': 'Скрытое имя 9000',
            'description': 'Скрытая температура 777 C', 'sku': 'SECRET123',
            'additionalProperty': [{'@type': 'PropertyValue', 'name': 'Скрытая плотность', 'value': '999 кг/м3'}]}
    html = '<html><head><script type="application/ld+json">' + json.dumps(data) + '</script></head><body><main><h1>Видимый товар</h1><p>Видимая мощность 15 кВт. ' + ('Видимое описание товара. ' * 20) + '</p></main></body></html>'
    from universal_extractor import extract_product_markdown
    for md in [extract_product_markdown(html, 'https://example.org/product'), html_to_markdown(html, 'https://example.org/product')]:
        assert 'Видимый товар' in md and '15 кВт' in md
        assert all(term not in md for term in ['9000', '777', 'SECRET123', '999', 'структурированные'])
    empty = '<html><head><script type="application/ld+json">' + json.dumps(data) + '</script></head><body></body></html>'
    assert extract_product_markdown(empty, 'https://example.org/product') is None


from universal_extractor import extract_product_markdown


def _nested_hidden_source(control=''):
    return '<main><h1>Товар</h1><a data-toggle="tab" href="#outer">Характеристики</a><div id="outer" style="display:none"><p>' + ('Видимое описание товара. ' * 30) + '</p>' + control + '<div id="inner" style="display:none"><table><tr><td>Скрытая вязкость</td><td>999 мПа</td></tr></table></div></div></main>'


def test_outer_user_tab_does_not_publish_unlinked_nested_hidden_table():
    md = extract_product_markdown(_nested_hidden_source(), 'https://example.org/product')
    assert 'Видимое описание' in md
    assert '999' not in md


def test_nested_user_control_keeps_its_own_hidden_panel():
    md = extract_product_markdown(_nested_hidden_source('<button data-toggle="collapse" data-target="#inner">Технические характеристики</button>'), 'https://example.org/product')
    assert '999 мПа' in md


@pytest.mark.parametrize('where', ['self', 'ancestor'])
@pytest.mark.parametrize('attribute', ['hidden', 'display', 'visibility'])
def test_hidden_slider_control_is_not_user_accessible(where, attribute):
    path = Path('/ai/projects/stroy-resurs/mvp_directories/Base/temp_html_storage/Новоалтайский_завод_мостовых_конструкций_ОАО/Product_pages/https-nzmk22_ru-products-bridge-beams_e828ed304e0db36a_3f22931cf1849894.html')
    soup = BeautifulSoup(path.read_text(), 'lxml')
    assert len(public_slider_panels(soup, 'https://nzmk22.ru/products/bridge/beams')) == 5
    control = soup.select_one('.breams-slider .slider21')
    node = control if where == 'self' else control.parent
    if attribute == 'hidden':node['hidden'] = ''
    else:node['style'] = 'display:none' if attribute == 'display' else 'visibility:hidden'
    assert public_slider_panels(soup, 'https://nzmk22.ru/products/bridge/beams') == []


@pytest.mark.parametrize('where', ['self', 'ancestor'])
def test_visibility_hidden_generic_control_does_not_open_panel(where):
    html = '<main><h1>Товар</h1><div><button data-toggle="collapse" data-target="#spec">Характеристики</button></div><div id="spec" style="display:none"><table><tr><td>Скрытая вязкость</td><td>999 мПа</td></tr></table></div></main>'
    soup = BeautifulSoup(html, 'lxml');control = soup.button
    (control if where == 'self' else control.parent)['style'] = 'visibility:hidden'
    assert '999' not in html_to_markdown(str(soup), 'https://example.org/product')
