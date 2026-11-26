"""Регрессии пользовательских примеров: Nuxt не является публичным каталогом SKU."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

from docx import Document
import pytest

from Profile_Markdown import tegola_ru
from product_quality import normalize_product_fields
from docx_generator import DOCXGenerator
from test_tegola_quality_source import page, URL


def restore(data, html, url=URL):
    tegola_ru.restore_source_product(data, html, url)
    normalize_product_fields(data, tegola_ru.extract(html, url))


def test_hidden_variant_rows_are_removed_from_both_specs_and_variants():
    title = 'Брус узкий фактурный Черный изумруд матовый RR33'
    html = page({'variants': [{'title': title, 'sku': '179057500704', 'color': {'title': 'черный'}}]})
    data = {'product': {'product_name': 'Текущий товар (TEGOLA)', 'article': '179057500704',
        'specifications': {'Марка': 'Исполнения', title: {'Артикул': '179057500704', 'Цвет': 'черный'},
                           title + ' -> Артикул': '179057500704', 'Длина, мм': '100'},
        'variants': [{'Цвет': 'черный', 'Модель': title, 'Другие параметры': 'Артикул: 179057500704'}]}}
    restore(data, html)
    assert data['product']['specifications'] == {'Длина, мм': '100'}
    assert data['product']['article'] == ''
    assert data['product']['variants'] == []
    assert data['product']['colors'] == ['черный']
    once = deepcopy(data)
    restore(data, html)
    assert data == once


def test_public_options_and_public_article_are_preserved():
    html = page({'description': '<p>Доступны исполнения: узкий, широкий. Артикул: 123456.</p>',
                 'variants': [{'title': 'Служебное имя', 'sku': '123456'}]})
    data = {'product': {'article': '123456', 'specifications': {'Артикул': '123456'},
                        'variants': [{'Размер': 'узкий'}, {'Размер': 'широкий'}]}}
    restore(data, html)
    assert data['product']['article'] == '123456'
    assert data['product']['variants'] == [{'Размер': 'узкий'}, {'Размер': 'широкий'}]


@pytest.mark.parametrize('name', ['Грунтовка битумная Праймер Сейфити Бейз 21,5л', 'Водосток временный'])
def test_single_redundant_model_is_not_a_choice(name):
    data = {'product': {'product_name': name + ' (TEGOLA)', 'colors': ['черный'],
                        'variants': [{'Модель': name, 'Цвет': 'черный', 'Размер': '', 'Другие параметры': ''}]}}
    normalize_product_fields(data)
    assert data['product']['variants'] == []


def test_single_variant_with_new_information_is_not_lost():
    data = {'product': {'product_name': 'Панель (TEGOLA)', 'variants': [{'Модель': 'Панель', 'Размер': '100x200'}]}}
    normalize_product_fields(data)
    assert data['product']['variants'] == [{'Модель': 'Панель', 'Размер': '100x200'}]


@pytest.mark.parametrize('slug', ['prajmer-sejfiti-bejz-215l', 'vremennyj-vodostok', 'skandinavskij-brus-modern-uzkij'])
def test_user_examples_do_not_render_private_variant_data(corpus_dir, tmp_path, slug):
    directory = corpus_dir / 'Tegola_Canadese_Италия/Product_pages'
    if not directory.is_dir():
        pytest.skip('Корпус недоступен')
    match = next((p for p in directory.glob('*.json') if json.loads(p.read_text()).get('url', '').endswith('/' + slug)), None)
    assert match is not None
    url = json.loads(match.read_text())['url']
    html = match.with_suffix('.html').read_text()
    source = tegola_ru._product(html, url)
    archived = json.loads((Path(__file__).parent / 'fixtures/tegola_public_variants.json').read_text())
    data = {'product': next(row for row in archived if row['source_url'] == url)}
    restore(data, html, url)
    assert not data['product']['variants']
    generator = DOCXGenerator()
    async def keep_docx(path):
        return None
    generator._convert_to_rtf = keep_docx
    path = asyncio.run(generator.generate_product_card({'name': source['title'], 'ai_raw_text': data, 'source_url': url}, {}, str(tmp_path)))
    doc = Document(path)
    paragraphs = [p.text for p in doc.paragraphs]
    text = '\n'.join(paragraphs + [c.text for t in doc.tables for r in t.rows for c in r.cells])
    assert 'Варианты' not in paragraphs
    assert 'Исполнения' not in text
    assert 'Артикул' not in text
    for variant in source['variants']:
        if variant.get('sku'):
            assert variant['sku'] not in text


def test_number_in_dimensions_or_file_url_does_not_confirm_article():
    html = page({'description': '<p>Ширина: 123456 мм.</p><a href="/123456.pdf">Сертификат</a>',
                 'variants': [{'title': 'Служебная запись', 'sku': '123456'}]})
    data = {'product': {'article': '123456', 'specifications': {'Артикул': '123456', 'Ширина': '123456 мм'}}}
    restore(data, html)
    assert data['product']['article'] == ''
    assert data['product']['specifications'] == {'Ширина': '123456 мм'}
