"""Сбой необязательного кэша/карточки не отменяет успешный товар для графа."""
import asyncio
from unittest.mock import AsyncMock

import pytest
import main
from test_crawl_refresh_resume import COMPANY, URL, build_system, crawler, save


@pytest.mark.parametrize('failure', ['card', 'cache', 'both'])
def test_cached_product_survives_refresh_io_failure(crawler, tmp_path, failure):
    async def run():
        system = build_system(crawler, tmp_path)
        company = {'company_id': '1', 'original_name': COMPANY}
        await system._prepare_company_crawl(company)
        path, _, metadata = await save(crawler)
        page = {'normalized_url': metadata['normalized_url'], 'file_path': path, 'category': 'product'}
        first = await system._process_single_page_with_checkpoint(
            page, company, main.CompanyStatistics(company), str(tmp_path))
        assert first
        # Тот же HTML, но ссылка в прежнем результате ещё со старой схемой.
        await crawler.temp_storage.save_product_result(path, dict(first, source_url=URL.replace('https:', 'http:')))
        await system._prepare_company_crawl(company)
        await save(crawler)
        system.docx_generator.generate_product_card = AsyncMock(
            side_effect=OSError('mock card IO') if failure in ('card', 'both') else None)
        if failure in ('cache', 'both'):
            crawler.temp_storage.save_product_result = AsyncMock(side_effect=OSError('mock cache IO'))
        recovered = await system._process_single_page_with_checkpoint(
            page, company, main.CompanyStatistics(company), str(tmp_path))
        assert recovered is not None
        assert recovered['product_id'] == first['product_id']
        assert recovered['source_url'] == URL
        assert system.ai_client.extract_product_info.await_count == 1
        assert not system._processing_urls
    asyncio.run(run())


def test_fresh_product_survives_result_cache_write_failure(crawler, tmp_path):
    async def run():
        system = build_system(crawler, tmp_path)
        company = {'company_id': '1', 'original_name': COMPANY}
        await system._prepare_company_crawl(company)
        path, _, metadata = await save(crawler)
        crawler.temp_storage.save_product_result = AsyncMock(side_effect=OSError('mock cache IO'))
        product = await system._process_single_page_with_checkpoint(
            {'normalized_url': metadata['normalized_url'], 'file_path': path, 'category': 'product'},
            company, main.CompanyStatistics(company), str(tmp_path))
        assert product is not None
        assert product['source_url'] == URL
        assert system.ai_client.extract_product_info.await_count == 1
        assert not system._processing_urls
    asyncio.run(run())
