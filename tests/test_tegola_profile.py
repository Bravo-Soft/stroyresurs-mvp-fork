"""Проверки восстановления вкладок Tegola из сохранённых исходных страниц."""
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from Profile_Markdown import tegola_ru

@pytest.fixture(scope='session')
def tegola_corpus(corpus_dir):
    corpus = corpus_dir / 'Tegola_Canadese_Италия/Product_pages'
    if not corpus.is_dir():
        pytest.skip(f'корпус Tegola недоступен: {corpus}')
    return corpus


def _pages(corpus):
    for metadata in sorted(corpus.glob('*.json')):
        yield metadata.with_suffix('.html'), json.loads(metadata.read_text())['url']


def _product(html):
    soup = BeautifulSoup(html, 'lxml')
    script = next(tag.get_text() for tag in soup.find_all('script')
                  if tag.get_text().startswith('window.__NUXT__='))
    return tegola_ru._state(script)['data'][0]['product']


@pytest.mark.parametrize('slug,expected', [
    ('unica-versaille', ['2.90', '9.65', '100x34', '3.1', '27.99', '56', 'Quarzo Marrone 120']),
    ('unica-rectangular', ['2.90', '10.5', '100x34', '30.45', '52']),
    ('sejfiti-fleks-sbs-ekp-45mm-kr', ['СБС-битум', 'Длина рулона, м: 8', 'Толщина, мм: 4,5', '23']),
    ('modulnaya-metallocherepica-bergen', ['1185х425', '1185х775', 'Zn140', 'Zn180', 'Zn275', 'до 50 лет']),
    ('maksistud-f', ['15 x 2', 'Диаметр рулона, см: 50', 'Вес рулона, кг: 40/80', 'Площадь рулона, м.кв.: 40/80']),
])
def test_saved_examples_restore_missing_tabs(slug, expected, tegola_corpus):
    path, url = next((path, url) for path, url in _pages(tegola_corpus) if url == 'https://tegola.ru/product/' + slug)
    html = path.read_text()
    assert not BeautifulSoup(html, 'lxml').select_one('.product-card-tab-blocks').get_text(strip=True)
    markdown = tegola_ru.extract(html, url)
    assert markdown
    for value in expected:
        assert value in markdown
    assert '## Характеристики' in markdown
    assert '## Инструкции и технические листы' in markdown
    assert 'window.__NUXT__' not in markdown
    assert 'Заказать расчет кровли' not in markdown
    assert '8 (800) 511-98-02' not in markdown


def test_entire_saved_corpus_keeps_current_features_and_documents_only(tegola_corpus):
    products = 0
    other = 0
    feature_count = 0
    for path, url in _pages(tegola_corpus):
        html = path.read_text()
        markdown = tegola_ru.extract(html, url)
        if '/product/' not in url:
            assert markdown is None, url
            other += 1
            continue
        products += 1
        assert markdown, url
        product = _product(html)
        assert markdown.startswith('# ' + tegola_ru._text(product['title']) + '\n'), url
        for feature in product.get('tab_features') or []:
            if tegola_ru._text(feature['title']) and tegola_ru._text(feature['value']):
                assert f"- {tegola_ru._text(feature['title'])}: {tegola_ru._text(feature['value'])}" in markdown, url
            feature_count += 1
        for field in ('tab_documents', 'tab_nodes', 'tab_node_exts'):
            for document in product.get(field) or []:
                link = tegola_ru._url(document.get('link') or document.get('file'), url)
                title = tegola_ru._text(document.get('title'))
                if title and link:
                    assert f'[{title}]({link})' in markdown, url
        # Шумовые секции DOM не должны проходить через извлечение состояния.
        contaminated = html.replace('</body>', '<section>UNRELATED_PRODUCT_94821 MENU_94821</section></body>')
        assert tegola_ru.extract(contaminated, url) == markdown
    assert (products, other, feature_count) == (299, 248, 1272)


def _fixture(body='p.title="Current";p.slug="current";p.tab_features=[{title:"Size",value:n}];',
             returned='{data:[{product:p,needProducts:[{title:"NEIGHBOR_SENTINEL",tab_features:[{title:"BAD",value:999}]}]}],menu:"MENU_SENTINEL"}',
             arguments='{},"30"'):
    return '<script>window.__NUXT__=(function(p,n){' + body + 'return ' + returned + ';}(' + arguments + '));</script>'


def test_alias_values_and_neighbor_isolation():
    markdown = tegola_ru.extract(_fixture(), 'https://tegola.ru/product/current')
    assert '- Size: 30' in markdown
    assert 'NEIGHBOR_SENTINEL' not in markdown
    assert 'MENU_SENTINEL' not in markdown
    assert '999' not in markdown


@pytest.mark.parametrize('html', [
    '<script>window.__NUXT__={data:[]};</script>',
    _fixture(body='p.title=alert("EXECUTED");'),
    _fixture(body='p.title=globalThis.process.exit();'),
    _fixture(body='p.title="Current";p.slug="different";'),
    _fixture(body='p.title="Current";p.slug="current";p.tab_features=unknown;'),
    _fixture().replace('));</script>', '));alert("EXECUTED");</script>'),
    _fixture(returned='{data:[]}'),
    '<h1>Карточка без состояния</h1>',
])
def test_unknown_or_executable_syntax_falls_back(html):
    assert tegola_ru.extract(html, 'https://tegola.ru/product/current') is None


@pytest.mark.parametrize('url', ['https://tegola.ru/catalog/current', 'https://tegola.ru/solution/current',
                                 'https://other.ru/product/current', 'https://tegola.ru/product/current/child'])
def test_non_product_and_foreign_urls_do_not_extract(url):
    assert tegola_ru.extract(_fixture(), url) is None


def test_unsafe_document_and_image_schemes_are_omitted():
    body = ('p.title="Current";p.slug="current";p.image="javascript:alert(1)";'
            'p.tab_documents=[{title:"BAD_LINK",link:"data:text/html,bad"}];')
    markdown = tegola_ru.extract(_fixture(body=body), 'https://tegola.ru/product/current')
    assert markdown == '# Current'


@pytest.mark.parametrize('slug,numeric_values', [
    ('unica-versaille', ['2.90', '9.65', '100х34', '3.1', '27.99', '56']),
    ('unica-rectangular', ['2.90', '10.5', '100х34', '30.45', '52']),
    ('sejfiti-fleks-sbs-ekp-45mm-kr', ['Длина рулона, м: 8', 'Ширина рулона, м: 1', 'Толщина, мм: 4,5', '23']),
    ('modulnaya-metallocherepica-bergen', ['1185х425', '1185х775', 'Zn140', 'Zn180', 'Zn275']),
    ('maksistud-f', ['15 x 2', 'Вес рулона, кг: 40/80', 'Диаметр рулона, см: 50', 'Площадь рулона, м.кв.: 40/80']),
])
def test_profile_to_markdown_restores_at_least_four_numeric_characteristics(tegola_corpus, slug, numeric_values):
    from text_extractor import html_to_markdown
    from site_profiles.resolver import ProfileResolver

    url = 'https://tegola.ru/product/' + slug
    profile = ProfileResolver().resolve(url)
    assert profile.extract.custom_module == 'tegola_ru'
    assert profile.extract.needs_raw_html
    path = next(path for path, page_url in _pages(tegola_corpus) if page_url == url)
    markdown = html_to_markdown(path.read_text(), url=url)
    assert len(numeric_values) >= 4
    for expected in numeric_values:
        assert expected in markdown
    assert '## Характеристики' in markdown
    assert '## Сертификаты' in markdown
    assert '## Изображения' in markdown
    assert 'window.__NUXT__' not in markdown
    assert 'Заказать расчет кровли' not in markdown


def test_profile_classifies_all_saved_urls(tegola_corpus):
    from site_profiles.resolver import ProfileResolver
    from web_crawler import URLCategorizer

    categorizer = URLCategorizer()
    categorizer.set_profile(ProfileResolver().resolve('https://tegola.ru'))
    products = other = 0
    for _, url in _pages(tegola_corpus):
        category, _ = categorizer.categorize_url(url)
        expected = '/product/' in url
        assert (category == 'product') == expected, (url, category)
        products += expected
        other += not expected
    assert (products, other) == (299, 248)
