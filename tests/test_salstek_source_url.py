"""Ссылка карточки должна открывать исходный товар после миграции salstek.ru."""
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from docx import Document

import main
from docx_generator import DOCXGenerator
from graph_db_uploader import GraphDBDataFormatter
from product_source_url import resolve_product_source_url


OLD_URL = 'https://salstek.ru/products/arh/element.php?id=110'
NEW_URL = 'https://salstek.ru/products/arkhitekturnoe-steklo/multicomfort-next/'
HTML = '<ul class="breadcrumb"><li>Продукты</li><li>MultiComfort° Next</li></ul>'


@pytest.mark.parametrize('url', [OLD_URL, OLD_URL.replace('https:', 'http:'),
                                OLD_URL.replace('salstek.ru', 'www.salstek.ru')])
def test_legacy_url_uses_exact_product_from_html(url):
    assert resolve_product_source_url(url, HTML) == NEW_URL


@pytest.mark.parametrize('title,slug', [
    ('MultiComfort° Neutral', 'multicomfort-neutral'),
    ('MultiComfort° Neutral PRO', 'multicomfort-neutral-pro'),
    ('Comfort° Neutral / Elit+', 'comfort-neutral-elit'),
    ('Comfort° Silver / Comfort° Grey', 'comfort-silver-comfort-grey'),
])
def test_similar_product_names_keep_their_variant(title, slug):
    html = f'<ul class="breadcrumb"><li>Продукты</li><li>{title}</li></ul>'
    assert resolve_product_source_url(OLD_URL, html) == (
        'https://salstek.ru/products/arkhitekturnoe-steklo/' + slug + '/')


def test_fire_resistant_glass_has_verified_product_url():
    url = 'https://salstek.ru/products/saveglass/element.php?id=1545'
    html = '<ul class="breadcrumb"><li>Огнестойкое закаленное стекло</li></ul>'
    assert resolve_product_source_url(url, html) == (
        'https://salstek.ru/products/bezopasnoe-steklo-i-steklopakety/'
        'ognestoykoe-zakalennoe-steklo/')


@pytest.mark.parametrize('html', [
    '<ul class="breadcrumb"><li>Продукты</li><li>700 мл</li></ul>',
    '<ul class="breadcrumb"><li>MultiComfort Next</li><li>Неизвестный товар</li></ul>',
    '<h2>MultiComfort Next</h2>',
    '',
])
def test_missing_or_unknown_product_is_not_guessed(html):
    # Старый id=110 уже может отдавать другой товар; id не подтверждает его название.
    assert resolve_product_source_url(OLD_URL, html) == OLD_URL


@pytest.mark.parametrize('url', [
    'https://example.ru/products/arh/element.php?id=110',
    'https://salstek.ru.example.ru/products/arh/element.php?id=110',
    'https://other.salstek.ru/products/arh/element.php?id=110',
    NEW_URL,
    'https://salstek.ru/products/arh/',
    'https://salstek.ru/products/arh/section.php?id=7',
    'https://salstek.ru/products/arh/element.php',
    'https://salstek.ru/contacts/',
    '',
])
def test_other_sites_and_current_urls_are_unchanged_without_html_parsing(url, monkeypatch):
    import bs4
    monkeypatch.setattr(bs4, 'BeautifulSoup', Mock(side_effect=AssertionError('Лишний парсинг')))
    assert resolve_product_source_url(url, HTML) == url


@pytest.mark.parametrize('url,expected_url', [
    (OLD_URL, NEW_URL),
    ('https://example.ru/catalog/next/?id=110&variant=2',
     'https://example.ru/catalog/next/?id=110&variant=2'),
])
def test_pipeline_card_and_graph_use_correct_url_without_changing_extraction(
        tmp_path, monkeypatch, url, expected_url):
    ai = {'product': {'product_name': 'Стекло MultiComfort Next (Салаватстекло, АО)',
                      'specifications': {'Светопропускание': '66%',
                                         'Солнечный фактор': '43%',
                                         'Наружное отражение': '26%'}}}
    company = {'original_name': 'Салаватстекло, АО', 'company_id': 'test'}
    metadata = {'url': url, 'normalized_url': url, 'storage_path': 'saved.html'}
    original_metadata = metadata.copy()
    storage = SimpleNamespace(save_ai_text=AsyncMock(return_value='saved_ai.txt'),
                              mark_as_ai_processed=AsyncMock())
    generator = DOCXGenerator()
    generator._convert_to_rtf = AsyncMock(return_value=None)
    product_ids = Mock()
    product_ids.generate_product_id_from_name_bag.return_value = 'pr_stable'
    stub = SimpleNamespace(
        config=SimpleNamespace(excluded_product_name_patterns=[]),
        processing_tracker=SimpleNamespace(clean_span=lambda name: nullcontext()),
        ai_client=SimpleNamespace(extract_product_info=AsyncMock(return_value=(
            ai, {'input_tokens': 20, 'output_tokens': 30}))),
        crawler=SimpleNamespace(temp_storage=storage, _product_dedup_key=Mock(return_value='slug')),
        statistics=SimpleNamespace(total_input_tokens=0, total_output_tokens=0),
        product_id_manager=product_ids,
        docx_generator=generator,
        _save_markdown_file=AsyncMock(),
        _download_product_files=AsyncMock(return_value=[]),
        _get_product_folder_path=Mock(return_value=(str(tmp_path), 'Products/test')),
        _processing_product_ids_lock=asyncio.Condition(),
        _processing_product_ids=set(), _global_processed_product_ids=set(),
        _processing_slug_keys=set(), _global_processed_slug_keys=set(),
    )
    monkeypatch.setattr(main, 'html_to_markdown', Mock(return_value='Сохранённый текст товара'))
    stats = main.CompanyStatistics(company)
    product = asyncio.run(main.MonitoringSystem.process_product_page(
        stub, HTML, company, stats, metadata, str(tmp_path)))

    assert product is not None
    assert product['source_url'] == expected_url
    assert product['ai_raw_text'] == ai
    assert metadata == original_metadata
    assert product['metadata'] == original_metadata
    assert product['product_id'] == 'pr_stable'
    assert stub._global_processed_product_ids == {'pr_stable'}
    assert stub._global_processed_slug_keys == {'slug'}
    stub.crawler._product_dedup_key.assert_called_once_with(expected_url)
    assert stub._processing_product_ids == stub._processing_slug_keys == set()
    assert stats.products_processed == stats.cards_generated == 1
    assert stats.products_failed == 0
    assert stub.ai_client.extract_product_info.await_args.kwargs['page_url'] == url
    assert f'URL страницы: {url}' in stub.ai_client.extract_product_info.await_args.args[0]
    assert stub._download_product_files.await_args.args[0] == url
    assert storage.save_ai_text.await_args.kwargs['url'] == url
    product_ids.generate_product_id_from_name_bag.assert_called_once_with(
        ai['product']['product_name'], company['original_name'])

    card = Document(next(tmp_path.glob('*.docx')))
    assert f'URL страницы: {expected_url}' in [p.text for p in card.paragraphs]
    payload = GraphDBDataFormatter.format_product_data('test', company['original_name'], [product])
    assert payload['companies'][0]['products'][0]['source_url'] == expected_url
