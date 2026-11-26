"""Регрессии потерь Tegola: реальные нижние блоки, SKU/цвет и маршрутизация."""
import copy
import json
import re

import pytest
from bs4 import BeautifulSoup

from Profile_Markdown import tegola_ru


def page(product):
    state = {'data': [{'product': {'title': 'Текущий товар', 'slug': 'current', **product},
                        'relatedProducts': [{'title': 'NEIGHBOR_978', 'size': 900}]}]}
    return '<script>window.__NUXT__=(function(){return ' + json.dumps(state, ensure_ascii=False).replace('</', '<\\/') + ';}());</script>'


URL = 'https://tegola.ru/product/current'


def test_lower_content_keeps_numeric_context_table_and_nested_description():
    html = page({'tab_description': {'title': 'Описание', 'description': '<p>Сталь 0,5 мм.</p>'},
                 'info_section': {'content': '<p>Сплав EN AW 5005.</p>'},
                 'seo_text_block': '<p>Защита Zn275 г/м2, от −60 до +100 °C.</p>'
                                   '<table><tr><th>Показатель</th><th>Значение</th></tr>'
                                   '<tr><td>Гибкость</td><td>−30 °C</td></tr></table>'
                                   '<script>BAD_SCRIPT_978()</script>',
                 'accordion': [{'description': 'OTHER_MATERIAL_978 300 г/м2'}]})
    text = tegola_ru.extract(html, URL)
    for expected in ('Сталь 0,5 мм', 'EN AW 5005', 'Zn275 г/м2', 'от −60 до +100 °C', '| Гибкость | −30 °C |'):
        assert expected in text
    assert 'BAD_SCRIPT_978' not in text
    assert 'NEIGHBOR_978' not in text
    assert 'OTHER_MATERIAL_978' not in text


def test_private_variants_do_not_create_public_choices_skus_or_prices():
    variants = [{'title': 'Панель PURAL RAL 8017', 'sku': '000123', 'color': {'title': 'Коричневый'}, 'price': 7928, 'measure': '₽/шт.'},
                {'title': 'Панель MATT RR32', 'sku': '000456', 'color': {'title': 'коричневый'}, 'price': 0}]
    html = page({'variants': variants})
    text = tegola_ru.extract(html, URL)
    assert 'Исполнения текущего товара' not in text
    assert all(value not in text for value in ('000123', '000456', '7928', 'Панель PURAL', 'Панель MATT'))
    data = {'product': {'product_name': 'Панель', 'specifications': {}, 'colors': ['КОричневый']}}
    tegola_ru.restore_source_product(data, html, URL)
    assert data['product']['colors'] == ['Коричневый']
    assert not data['product'].get('variants')
    assert '7928' not in str(data)
    assert data['product']['product_name'] == 'Панель'


def test_exact_fields_restore_pallet_guarantee_composition_and_compatibility_idempotently():
    html = page({'tab_features': [
        {'title': 'Количество коробок на поддоне', 'value': '90'},
        {'title': 'Количество рулонов на поддоне', 'value': '540'},
        {'title': 'Гарантия', 'value': '30 лет'},
        {'title': 'Состав', 'value': 'битум, органические растворители, функциональные добавки'},
        {'title': 'Площадь рулона, м.кв.', 'value': '15'},
        {'title': 'Дополнительные материалы для полной гидроизоляции', 'value': 'TH2 Stop Tape, Int/Est Corners'},
        {'title': 'Шпатель', 'value': 'Зубцы 6 x 6 мм'}]})
    data = {'product': {'specifications': {'Состав': 'битум', 'Гарантия': '10 лет',
                                        'Площадь рулона, м^2': '10'},
                        'complectation': '- Количество коробок на поддоне: 80',
                        'instructions_manuals': 'Приклеивать на основание'}}
    tegola_ru.restore_source_product(data, html, URL)
    product = data['product']
    assert product['complectation'] == '- Количество коробок на поддоне: 90\n- Количество рулонов на поддоне: 540'
    assert product['exploitation'] == '- Гарантия: 30 лет'
    assert product['specifications'] == {'Состав': 'битум, органические растворители, функциональные добавки', 'Площадь рулона, м.кв.': '15'}
    assert product['compatibility'].endswith('TH2 Stop Tape, Int/Est Corners')
    assert product['instructions_manuals'] == 'Приклеивать на основание\n- Шпатель: Зубцы 6 x 6 мм'
    before = copy.deepcopy(data)
    tegola_ru.restore_source_product(data, html, URL)
    assert data == before


@pytest.mark.parametrize('data,url', [
    ({'product': None}, URL), ({'product': {}, 'is_product': False}, URL),
    ({'product': {}}, 'https://tegola.ru/product/neighbor'),
    ({'product': {}}, 'https://other.ru/product/current'),
    ({'product': {}}, 'https://tegola.ru/catalog/current')])
def test_restore_cannot_promote_rejected_or_touch_other_product(data, url):
    before = copy.deepcopy(data)
    assert tegola_ru.restore_source_product(data, page({'tab_features': [{'title': 'Длина', 'value': '100'}]}), url) == []
    assert data == before


def test_enabled_advantages_only_and_dummy_color_label_excluded():
    source = {'advantages_show': True, 'advantages': [{'title': 'Надежно', 'description': 'Надежно'}],
              'variants': [{'title': 'Товар', 'color': {'title': 'Цвет'}}]}
    data = {'product': {}}
    tegola_ru.restore_source_product(data, page(source), URL)
    assert data['product']['advantages'] == '- Надежно'
    assert not data['product'].get('colors')
    assert tegola_ru.extract(page(source), URL).count('- Надежно') == 1
    source['advantages_show'] = False
    assert 'Надежно' not in tegola_ru.extract(page(source), URL)


def test_explicit_lower_table_restores_flexibility_guarantee_not_numeric_prose():
    source = {'seo_text_block': '<p>Цена 500 руб., гарантия на иной материал 100 лет.</p>'
              '<table><tr><td>Характеристика</td><td>Значение</td></tr>'
              '<tr><td>Гибкость при низких температурах</td><td>до −30 °C</td></tr>'
              '<tr><td>Гарантия</td><td>30 лет</td></tr></table>'}
    data = {'product': {}}
    tegola_ru.restore_source_product(data, page(source), URL)
    assert data['product']['specifications'] == {'Гибкость при низких температурах': 'до −30 °C'}
    assert data['product']['exploitation'] == '- Гарантия: 30 лет'
    assert '100 лет' not in str(data) and '500' not in str(data)


def test_duplicate_source_labels_keep_both_distinct_values():
    html = page({'tab_features': [{'title': 'Покрытие', 'value': 'Медь / Сталь'},
                                  {'title': 'Покрытие', 'value': 'Zn 275 / Pu matt'}]})
    data = {'product': {}}
    tegola_ru.restore_source_product(data, html, URL)
    assert data['product']['specifications']['Покрытие'] == 'Медь / Сталь\nZn 275 / Pu matt'


def test_all_299_saved_products_preserve_features_without_injecting_private_variants(corpus_dir):
    directory = corpus_dir / 'Tegola_Canadese_Италия/Product_pages'
    if not directory.is_dir():
        pytest.skip('Сохраненный корпус Tegola недоступен')
    counts = {'products': 0, 'features': 0, 'variants': 0, 'sku': 0, 'seo': 0, 'seo_tables': 0}
    for metadata in sorted(directory.glob('*.json')):
        url = json.loads(metadata.read_text())['url']
        if not re.fullmatch(r'https://tegola.ru/product/[^/]+/?', url):
            continue
        html = metadata.with_suffix('.html').read_text()
        soup = BeautifulSoup(html, 'lxml')
        script = next(t.get_text() for t in soup.find_all('script') if t.get_text().lstrip().startswith('window.__NUXT__='))
        source = tegola_ru._state(script)['data'][0]['product']
        markdown = tegola_ru.extract(html, url)
        assert markdown and markdown.startswith('# ' + source['title']), url
        data = {'product': {'specifications': {}}}
        tegola_ru.restore_source_product(data, html, url)
        product = data['product']; counts['products'] += 1
        for row in source.get('tab_features') or []:
            label, value = tegola_ru._text(row.get('title')), tegola_ru._text(row.get('value'))
            if not label or not value:
                continue
            counts['features'] += 1
            assert f'- {label}: {value}' in markdown, (url, label)
            if label in product['specifications']:
                assert value in product['specifications'][label].splitlines(), (url, label)
            else:
                assert any(f'- {label}: {value}' in product.get(field, '') for field in ('complectation', 'exploitation', 'compatibility', 'instructions_manuals')), (url, label)
        assert not product.get('variants'), url
        assert '## Исполнения текущего товара' not in markdown, url
        for row in source['variants']:
            counts['variants'] += 1
            if row.get('sku'):
                counts['sku'] += 1
                public_text = ' '.join(tegola_ru._content(source.get(field)) for field in
                                      ('description', 'tab_description', 'info_section', 'seo_text_block'))
                if row['sku'] not in public_text:
                    plain = re.sub(r'!?\[([^]\n]*)\]\([^\n)]*\)', r'\1', markdown)
                    assert not re.search(r'(?<!\d)' + re.escape(row['sku']) + r'(?!\d)', plain), url
        if source.get('seo_text_block'):
            counts['seo'] += 1
            # Все числовые фрагменты нижнего блока должны доходить до LLM.
            plain = BeautifulSoup(source['seo_text_block'], 'html.parser').get_text(' ', strip=True)
            assert set(re.findall(r'\d+(?:[.,]\d+)?', plain)) <= set(re.findall(r'\d+(?:[.,]\d+)?', markdown)), url
            counts['seo_tables'] += '<table' in source['seo_text_block']
    assert counts == {'products': 299, 'features': 1268, 'variants': 990, 'sku': 935, 'seo': 287, 'seo_tables': 2}
