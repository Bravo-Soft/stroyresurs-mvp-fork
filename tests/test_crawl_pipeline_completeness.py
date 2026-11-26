"""Неполный обход остаётся неполным после извлечения и отправки полезных данных."""
import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import main
from config import Config
from web_crawler import WebCrawler


COMPANY = {'company_id': '1', 'original_name': 'Завод', 'safe_name': 'factory',
           'website': 'https://factory.test/'}
PAGE = {'url': COMPANY['website'] + 'product/', 'normalized_url': COMPANY['website'] + 'product/',
        'category': 'product', 'file_path': 'saved.html'}


def crawl_data(status, pages=None):
    return {'crawl_status': status, 'stop_reason': 'page_budget' if status == 'partial' else
            'no_saved_pages' if status == 'failed' else 'queue_exhausted',
            'stop_reasons': ['page_budget'] if status == 'partial' else [],
            'stored_pages': [PAGE] if pages is None else pages, 'total_pages': 3,
            'attempted_pages': 3, 'fetched_pages': 2, 'successful_urls': 2,
            'usable_pages': 2,
            'failed_attempts': 1, 'failed_pages': 1, 'pending_queue': 2,
            'remaining_pages': 2, 'remaining_sitemap_urls': 4,
            'working_url': COMPANY['website']}


def system(tmp_path, data):
    obj = main.MonitoringSystem.__new__(main.MonitoringSystem)
    obj.statistics = main.Statistics()
    obj.config = SimpleNamespace(documents_dir=str(tmp_path), graph_db_enable=True,
                                graph_db_delay_between_companies=0, enable_file_conversion=False)
    obj.crawler = SimpleNamespace(crawl_site=AsyncMock(return_value=data), page_sink=None,
                                 temp_storage=SimpleNamespace(get_company_files=AsyncMock(return_value=[])))
    obj.directory_manager = SimpleNamespace(create_company_directory_structure=Mock(return_value={}),
                                            validate_directory_structure=Mock(return_value=True))
    obj.processing_tracker = SimpleNamespace(set_current_company=Mock())
    obj.checkpoint_manager = SimpleNamespace(save_checkpoint=AsyncMock(), clear_checkpoint=AsyncMock())
    for name in ('_global_processed_urls', '_global_processed_product_ids'):
        setattr(obj, name, set())
    obj._prepare_company_crawl = AsyncMock()
    obj._setup_profiling = Mock()
    obj._finalize_profiling = Mock()
    obj._save_actual_name_cache = Mock()
    obj._resolve_streaming_mode = Mock(return_value=(False, None))
    obj._get_domain_dirs = Mock(return_value={})
    obj.generate_company_card = AsyncMock(return_value={'company': {'Наименование компании': 'Завод'}})
    obj.process_distributor_pages = AsyncMock(return_value=[])
    obj._download_site_files_parallel = AsyncMock(return_value=[])
    obj.process_stored_pages_with_checkpoints = AsyncMock(return_value=[{'product_id': 'saved-product'}])
    obj.add_products_to_index = AsyncMock()
    obj._collect_company_files_for_graphdb = AsyncMock(return_value=[])
    obj._trim_global_url_cache = AsyncMock()
    obj.save_unavailable_site = AsyncMock()
    obj.graph_db_uploader = SimpleNamespace(upload_company_data=AsyncMock(return_value=True))
    return obj


@pytest.mark.parametrize('status, expected', [('complete', 'success'), ('partial', 'partial'),
                                              ('failed', 'crawling_error')])
def test_wrapper_preserves_status_reasons_and_counters(tmp_path, status, expected):
    data = crawl_data(status, [] if status == 'failed' else None)
    obj = system(tmp_path, data)
    result = asyncio.run(obj.full_site_crawling(dict(COMPANY), str(tmp_path)))
    assert result['status'] == expected
    assert result['crawl_status'] == status
    assert result['stop_reason'] == data['stop_reason']
    assert result['crawl_summary']['failed_pages'] == 1
    assert result['crawl_summary']['attempted_pages'] == 3
    assert result['crawl_summary']['usable_pages'] == 2
    assert result['crawl_summary']['remaining_pages'] == 2
    assert result['crawl_summary']['remaining_sitemap_urls'] == 4
    assert result['crawl_data'] == data


def test_missing_completeness_is_not_success(tmp_path):
    data = {'stored_pages': [PAGE], 'total_pages': 1}
    result = asyncio.run(system(tmp_path, data).full_site_crawling(dict(COMPANY), str(tmp_path)))
    assert result['status'] == 'partial'
    assert result['crawl_status'] == 'unknown'
    assert result['stop_reason'] == 'completeness_unknown'


def test_partial_exception_result_preserves_useful_pages(tmp_path):
    data = dict(crawl_data('partial'), error='site deadline reached')
    obj = system(tmp_path, data)
    result = asyncio.run(obj.process_company(dict(COMPANY)))
    assert result['status'] == 'partial'
    obj.graph_db_uploader.upload_company_data.assert_awaited_once()
    assert result['crawling_result']['stored_pages'] == [PAGE]


def test_unhandled_crawler_exception_is_failed(tmp_path):
    obj = system(tmp_path, {})
    obj.crawler.crawl_site.side_effect = RuntimeError('broken fetch')
    result = asyncio.run(obj.process_company(dict(COMPANY)))
    assert result['status'] == 'crawling_failed'
    assert result['crawl_status'] == 'failed'
    assert result['stop_reason'] == 'internal_error'
    obj.graph_db_uploader.upload_company_data.assert_not_awaited()


def test_partial_does_not_hide_graph_fatal_status(tmp_path):
    obj = system(tmp_path, crawl_data('partial'))
    obj.graph_db_uploader.upload_company_data.side_effect = main.GraphDBFatalError('backend unavailable')
    result = asyncio.run(obj.process_company(dict(COMPANY)))
    assert result['status'] == 'graph_db_fatal_error'
    assert result['crawl_status'] == 'partial'


@pytest.mark.parametrize('status', ['complete', 'partial'])
def test_partial_pipeline_still_extracts_uploads_and_marks_checkpoint(tmp_path, status):
    obj = system(tmp_path, crawl_data(status))
    result = asyncio.run(obj.process_company(dict(COMPANY)))
    assert result['status'] == ('partial' if status == 'partial' else 'success'), result['errors']
    obj.process_stored_pages_with_checkpoints.assert_awaited_once()
    obj.graph_db_uploader.upload_company_data.assert_awaited_once()
    assert result['structured_products'] == [{'product_id': 'saved-product'}]
    assert result['company_statistics']['crawl_status'] == status
    assert result['company_statistics']['crawl_summary']['attempted_pages'] == 3
    checkpoints = obj.checkpoint_manager.save_checkpoint.await_args_list
    final = next(call.kwargs for call in checkpoints if call.kwargs['stage'] == 'company_completed')
    assert final['progress']['crawl_status'] == status
    assert final['progress']['status'] == ('company_processing_partial' if status == 'partial'
                                           else 'company_processing_complete')
    assert obj.statistics.companies_success == (status == 'complete')
    assert obj.statistics.companies_partial == (status == 'partial')
    assert obj.statistics.companies_failed == 0
    assert obj.statistics.companies_processed == 1
    if status == 'partial':
        assert result['error']
        obj.checkpoint_manager.clear_checkpoint.assert_not_awaited()


def test_failed_without_saved_pages_does_not_extract_or_upload(tmp_path):
    obj = system(tmp_path, crawl_data('failed', []))
    result = asyncio.run(obj.process_company(dict(COMPANY)))
    assert result['status'] == 'crawling_failed'
    assert result['company_statistics']['crawl_status'] == 'failed'
    assert result['company_statistics']['crawl_summary']['failed_pages'] == 1
    obj.generate_company_card.assert_not_awaited()
    obj.process_stored_pages_with_checkpoints.assert_not_awaited()
    obj.graph_db_uploader.upload_company_data.assert_not_awaited()
    assert obj.statistics.companies_failed == 1
    assert obj.statistics.companies_partial == 0


def test_failed_checkpoint_error_does_not_double_count_company(tmp_path):
    obj = system(tmp_path, crawl_data('failed', []))
    async def checkpoint(**kwargs):
        if kwargs['stage'] == 'crawling_failed':
            raise OSError('checkpoint unavailable')
    obj.checkpoint_manager.save_checkpoint.side_effect = checkpoint
    result = asyncio.run(obj.process_company(dict(COMPANY)))
    assert result['status'] == 'error'
    assert obj.statistics.companies_processed == 1
    assert obj.statistics.companies_failed == 1


def test_crawl_site_second_finalize_preserves_page_limit_and_remaining_queue(tmp_path):
    async def run():
        config = Config()
        config.base_dir = str(tmp_path)
        crawler = WebCrawler(config)
        crawler._apply_site_profile = Mock()
        crawler._get_working_url = AsyncMock(return_value=COMPANY['website'])
        crawler._set_working_base = Mock()
        crawler.site_status = None
        crawler._save_crawling_stats = AsyncMock()
        async def drain(url, name, dirs, stored):
            crawler.max_pages_per_site = 1
            crawler.stats['total_pages'] = 1
            crawler._successful_fetch_urls.add(PAGE['url'])
            crawler._usable_page_urls.add(PAGE['url'])
            crawler._finalize_crawl_status(deque([(COMPANY['website'] + 'tail/', 1, 'product', 9)]))
        crawler._start_crawling_with_sitemap_support = drain
        result = await crawler.crawl_site(COMPANY['website'], COMPANY['original_name'], str(tmp_path))
        assert result['crawl_status'] == 'partial'
        assert result['stop_reason'] == 'page_limit'
        assert result['remaining_pages'] == 1
    asyncio.run(run())


@pytest.mark.parametrize('reason', ['cancelled', 'internal_error'])
def test_explicit_terminal_reason_survives_second_finalize(tmp_path, reason):
    config = Config()
    config.base_dir = str(tmp_path)
    crawler = WebCrawler(config)
    crawler._successful_fetch_urls.add(PAGE['url'])
    crawler._usable_page_urls.add(PAGE['url'])
    crawler._finalize_crawl_status(deque(), reason=reason)
    crawler._finalize_crawl_status()
    assert crawler.stats['crawl_status'] == 'partial'
    assert reason in crawler.stats['stop_reasons']
