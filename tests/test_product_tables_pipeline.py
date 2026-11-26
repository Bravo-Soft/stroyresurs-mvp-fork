"""Реальный обработчик страницы: восстановление до сохранения, карточки и upload."""
import asyncio
from contextlib import nullcontext
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bs4 import BeautifulSoup
import pytest

import main
from graph_db_uploader import GraphDBDataFormatter

FIXTURES = Path(__file__).parent / 'fixtures/product_tables'
COMPANY = {'original_name': 'Завод, ООО', 'company_id': 'test'}


def stub(tmp_path, ai):
    return SimpleNamespace(
        config=SimpleNamespace(excluded_product_name_patterns=[]),
        processing_tracker=SimpleNamespace(clean_span=lambda name: nullcontext()),
        ai_client=SimpleNamespace(extract_product_info=AsyncMock(return_value=(
            ai, {'input_tokens': 20, 'output_tokens': 30}))),
        crawler=SimpleNamespace(
            temp_storage=SimpleNamespace(save_ai_text=AsyncMock(return_value='saved.txt'),
                                         mark_as_ai_processed=AsyncMock()),
            _product_dedup_key=Mock(return_value='slug')),
        statistics=SimpleNamespace(total_input_tokens=0, total_output_tokens=0),
        product_id_manager=SimpleNamespace(generate_product_id_from_name_bag=Mock(return_value='pr_stable')),
        docx_generator=SimpleNamespace(generate_product_card=AsyncMock(return_value='card.rtf')),
        _save_markdown_file=AsyncMock(), _download_product_files=AsyncMock(return_value=[]),
        _download_images_from_ai_text=AsyncMock(return_value=[]),
        _get_product_folder_path=Mock(return_value=(str(tmp_path), 'Products/test')),
        _processing_product_ids_lock=asyncio.Condition(),
        _processing_product_ids=set(), _global_processed_product_ids=set(),
        _processing_slug_keys=set(), _global_processed_slug_keys=set())


@pytest.mark.parametrize('case', ['ural_ov87', 'sverd_130', 'vmp_holding', 'tsila_176'])
def test_real_pipeline_saves_and_uploads_restored_specs_but_not_layout_metadata(tmp_path, case):
    data = json.loads((FIXTURES / f'{case}.json').read_text())
    ai = data['ai']
    original_name = ai['product']['product_name']
    system = stub(tmp_path, ai)
    stats = main.CompanyStatistics(COMPANY)
    product = asyncio.run(main.MonitoringSystem.process_product_page(
        system, (FIXTURES / f'{case}.html').read_text(), COMPANY, stats,
        {'url': data['url'], 'normalized_url': data['url'], 'storage_path': 'page.html'}, str(tmp_path)))
    assert product is not None
    assert product['product_id'] == 'pr_stable'
    assert product['ai_raw_text']['_table_layouts']
    system.product_id_manager.generate_product_id_from_name_bag.assert_called_once_with(original_name, COMPANY['original_name'])
    saved = system.crawler.temp_storage.save_ai_text.await_args.kwargs['ai_text']
    assert saved == product['ai_raw_text']
    assert system.ai_client.extract_product_info.await_count == 1
    payload = GraphDBDataFormatter.format_product_data('test', COMPANY['original_name'], [product])
    uploaded = payload['companies'][0]['products'][0]
    assert '_table_layouts' not in json.dumps(uploaded, ensure_ascii=False)
    assert uploaded['specifications']
    if case == 'ural_ov87':
        assert len(saved['product']['specifications']['Характеристики насоса']) == 5
        assert '8 780' in json.dumps(uploaded, ensure_ascii=False)
    assert stats.products_failed == 0


def test_table_failure_does_not_mark_page_processed(tmp_path, monkeypatch):
    data = json.loads((FIXTURES / 'sverd_130.json').read_text())
    soup = BeautifulSoup((FIXTURES / 'sverd_130.html').read_text(), 'lxml')
    soup.table.find_all('tr')[1].find_all('td')[1].string = 'Марка насоса'
    # Проверяем именно путь ошибки после ответа LLM, до сохранения и numeric gate.
    monkeypatch.setattr(main, 'html_to_markdown', Mock(return_value='Текст товара'))
    system = stub(tmp_path, data['ai'])
    stats = main.CompanyStatistics(COMPANY)
    result = asyncio.run(main.MonitoringSystem.process_product_page(
        system, str(soup), COMPANY, stats, {'url': data['url']}, str(tmp_path)))
    assert result is None and stats.products_failed == 1
    system.crawler.temp_storage.save_ai_text.assert_not_awaited()
    system.crawler.temp_storage.mark_as_ai_processed.assert_not_awaited()
    system.docx_generator.generate_product_card.assert_not_awaited()


def test_source_tables_do_not_override_model_non_product_verdict(tmp_path):
    data = json.loads((FIXTURES / 'sverd_130.json').read_text())
    system = stub(tmp_path, {'error': 'Trash_418#'})
    stats = main.CompanyStatistics(COMPANY)
    result = asyncio.run(main.MonitoringSystem.process_product_page(
        system, (FIXTURES / 'sverd_130.html').read_text(), COMPANY, stats,
        {'url': data['url'], 'normalized_url': data['url'], 'storage_path': 'page.html'}, str(tmp_path)))
    assert result is None
    saved = system.crawler.temp_storage.save_ai_text.await_args.kwargs
    assert saved['text_type'] == 'not_product'
    assert saved['ai_text'] == {'error': 'Trash_418#'}
    system.docx_generator.generate_product_card.assert_not_awaited()
