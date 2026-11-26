"""Смешанный каталог сохраняется как не-товар и при старом положительном AI."""
import asyncio
from unittest.mock import Mock

import pytest
import main
from test_product_tables_pipeline import stub, COMPANY
from test_source_product_tables_quality import saved


@pytest.mark.parametrize('cached', [False, True])
def test_mixed_source_catalog_never_generates_parent_card(tmp_path, monkeypatch, cached):
    html = saved('Новоалтайский_завод_мостовых_конструкций_ОАО', '*e828ed30*.html')
    url = 'https://nzmk22.ru/products/bridge/beams'
    ai = {'product': {'product_name': 'Смешанные балки', 'specifications': {'Размеры': '900x100x90'}}}
    system = stub(tmp_path, ai)
    monkeypatch.setattr(main, 'html_to_markdown', Mock(return_value='Семейства балок'))
    stats = main.CompanyStatistics(COMPANY)
    result = asyncio.run(main.MonitoringSystem.process_product_page(
        system, html, COMPANY, stats, {'url': url, 'normalized_url': url, 'storage_path': 'page.html'},
        str(tmp_path), cached_ai_data=ai if cached else None))
    assert result is None
    system.ai_client.extract_product_info.assert_not_awaited()
    system.docx_generator.generate_product_card.assert_not_awaited()
    saved_ai = system.crawler.temp_storage.save_ai_text.await_args.kwargs
    assert saved_ai['text_type'] == 'not_product'
    assert saved_ai['ai_text']['source_rejection'].startswith('Самостоятельные товарные блоки: ')
    assert stats.products_failed == 0
