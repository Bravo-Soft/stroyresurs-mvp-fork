"""Полная orchestration компании возвращает неизменённый товар в payload графа."""
import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import main
from test_crawl_refresh_resume import COMPANY, HTML_A, URL, build_system, crawler, save


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('mode', ['refresh', 'resume'])
def test_company_pipeline_uploads_cached_product(crawler, tmp_path, streaming, mode, monkeypatch):
    async def run():
        system = build_system(crawler, tmp_path)
        company = {'company_id': '1', 'original_name': COMPANY, 'safe_name': 'test',
                   'website': 'https://example.ru/'}
        await system._prepare_company_crawl(company)
        prior_run = company['_crawl_run_id']
        path, _, metadata = await save(crawler)
        seed = await system._process_single_page_with_checkpoint(
            {'file_path': path, 'url': URL, 'normalized_url': metadata['normalized_url'], 'category': 'product'},
            company, main.CompanyStatistics(company), str(tmp_path))
        assert seed
        system.config.documents_dir = str(tmp_path / 'Documents')
        company_dir = tmp_path / 'Documents' / '1_test'
        company_dir.mkdir(parents=True)
        system.config.graph_db_enable = True
        system.config.graph_db_delay_between_companies = 0
        system.config.enable_file_conversion = False
        system.config.pipeline_page_workers = 2
        system.directory_manager = SimpleNamespace(
            create_company_directory_structure=Mock(return_value={}), validate_directory_structure=Mock(return_value=True))
        system.processing_tracker.set_current_company = Mock()
        system._stream_ckpt_lock = asyncio.Lock()
        system._setup_profiling = Mock()
        system._finalize_profiling = Mock()
        system._save_actual_name_cache = Mock()
        system._resolve_streaming_mode = Mock(return_value=(streaming, COMPANY if streaming else None))
        system._get_domain_dirs = Mock(return_value={})
        system.generate_company_card = AsyncMock(return_value={'company': {'Наименование компании': COMPANY}})
        system.process_distributor_pages = AsyncMock(return_value=[])
        system._download_site_files_parallel = AsyncMock(return_value=[])
        system.add_products_to_index = AsyncMock()
        system._collect_company_files_for_graphdb = AsyncMock(return_value=[])
        system._trim_global_url_cache = AsyncMock()
        system.save_unavailable_site = AsyncMock()
        system.graph_db_uploader = SimpleNamespace(upload_company_data=AsyncMock(return_value=True))
        system.checkpoint_manager.save_checkpoint = AsyncMock(return_value=True)
        system.checkpoint_manager.clear_checkpoint = AsyncMock()
        checkpoint = SimpleNamespace(current_company=dict(company), current_stage='crawling_completed')
        system.checkpoint_manager.load_checkpoint = AsyncMock(return_value=checkpoint if mode == 'resume' else None)
        monkeypatch.setattr(main.pipeline_control, 'ignore_checkpoint', lambda: False)
        crawler._fetch_page_content = AsyncMock(return_value=await crawler._parse_content(HTML_A, URL))
        async def crawl(company_data, destination):
            # Настоящие resume/refresh, storage, hash/checkpoints и parsing.
            stored = []
            page = await crawler._process_page_with_storage(URL, 0, 'product', COMPANY, {}, deque(), stored)
            if page and crawler.page_sink:
                await crawler.page_sink.put(page)
            assert not stored  # тот же HTML не должен повторно идти в streaming/LLM
            return {'status': 'success', 'crawl_status': 'complete', 'stored_pages': stored, 'working_url': company_data['website'],
                    'crawl_data': {}, 'downloaded_files': []}
        system.full_site_crawling = crawl
        result = await system.process_company(company)
        assert result['status'] == 'success', result['errors']
        assert len(result['structured_products']) == 1
        upload = system.graph_db_uploader.upload_company_data
        upload.assert_awaited_once()
        sent = upload.await_args.kwargs['products']
        assert [item['product_id'] for item in sent] == [seed['product_id']]
        assert sent[0]['source_url'] == URL
        assert result['company_statistics']['graph_db_products_sent'] == 1
        assert system.ai_client.extract_product_info.await_count == 1
        assert (company['_crawl_run_id'] == prior_run) == (mode == 'resume')
        assert crawler._fetch_page_content.await_count == (mode == 'refresh')
        assert crawler.page_sink is None
    asyncio.run(run())


def test_same_final_alias_refresh_preserves_worker_result(crawler, monkeypatch):
    """Детерминированная гонка: alias refresh прочитал metadata раньше streaming worker."""
    import temp_storage_manager
    async def run():
        await crawler.reset_state(COMPANY, crawl_run_id='current')
        path, _, metadata = await save(crawler)
        original_open = temp_storage_manager.aiofiles.open
        refresh_ready = asyncio.Event()
        worker_done = asyncio.Event()
        class DelayedRefreshWrite:
            def __init__(self, file, mode='r', *args, **kwargs):
                self.file = file
                self.mode = mode
                self.args = args
                self.kwargs = kwargs
            async def __aenter__(self):
                # Задерживаем ДО open('w'), чтобы не усечь файл во время работы worker.
                if (asyncio.current_task().get_name() == 'alias-refresh'
                        and self.mode == 'w' and (str(self.file).endswith('.json') or '.json.' in str(self.file))):
                    refresh_ready.set()
                    await worker_done.wait()
                self.context = original_open(self.file, self.mode, *self.args, **self.kwargs)
                return await self.context.__aenter__()
            async def __aexit__(self, *args):
                return await self.context.__aexit__(*args)
        monkeypatch.setattr(temp_storage_manager.aiofiles, 'open', DelayedRefreshWrite)
        refresh = asyncio.create_task(crawler.temp_storage.save_cleaned_html(
            URL, metadata['normalized_url'], HTML_A, COMPANY,
            metadata={'url': URL, 'requested_url': 'https://example.ru/second-alias'}, category='product'),
            name='alias-refresh')
        await refresh_ready.wait()
        # Worker должен дождаться записи alias, затем обновить уже свежий snapshot.
        async def worker():
            await crawler.temp_storage.update_checkpoint(path, 'ai_processed', True)
            await crawler.temp_storage.save_product_result(path, {'product_id': 'worker-product'})
        worker_task = asyncio.create_task(worker())
        await asyncio.sleep(0)
        worker_done.set()
        await asyncio.gather(refresh, worker_task)
        final = await crawler.temp_storage.load_cleaned_html(path)
        assert (final['metadata']['checkpoints']['ai_processed'],
                final['metadata'].get('product_result', {}).get('product_id')) == (True, 'worker-product')
    asyncio.run(asyncio.wait_for(run(), timeout=10))


def test_concurrent_final_aliases_create_one_html_version(crawler):
    from pathlib import Path
    async def run():
        await crawler.reset_state(COMPANY, crawl_run_id='current')
        norm = crawler.url_normalizer.normalize_url(URL)
        results = await asyncio.gather(*(crawler.temp_storage.save_cleaned_html(
            URL, norm, HTML_A, COMPANY, {'requested_url': f'https://example.ru/alias-{i}'}, 'product')
            for i in range(8)))
        paths = {item[0] for item in results}
        assert len(paths) == 1 and '' not in paths
        assert sum(item[1] for item in results) == 1
        assert len(list(Path(next(iter(paths))).parent.glob('*.html'))) == 1
        assert len(await crawler.temp_storage.get_company_files(COMPANY)) == 1
    asyncio.run(run())


def test_readers_keep_complete_json_during_partial_sidecar_write(crawler, monkeypatch):
    import temp_storage_manager
    async def run():
        await crawler.reset_state(COMPANY, crawl_run_id='current')
        path, _, _ = await save(crawler)
        original_open = temp_storage_manager.aiofiles.open
        partial_written = asyncio.Event()
        finish_write = asyncio.Event()
        class PartialWrite:
            def __init__(self, wrapped):
                self.wrapped = wrapped
            async def write(self, content):
                split = len(content) // 2
                await self.wrapped.write(content[:split])
                await self.wrapped.flush()
                partial_written.set()
                await finish_write.wait()
                return await self.wrapped.write(content[split:])
        class SlowSidecar:
            def __init__(self, file, mode='r', *args, **kwargs):
                self.context = original_open(file, mode, *args, **kwargs)
                self.partial = mode == 'w' and str(file).endswith('.tmp')
            async def __aenter__(self):
                wrapped = await self.context.__aenter__()
                return PartialWrite(wrapped) if self.partial else wrapped
            async def __aexit__(self, *args):
                return await self.context.__aexit__(*args)
        monkeypatch.setattr(temp_storage_manager.aiofiles, 'open', SlowSidecar)
        writer = asyncio.create_task(crawler.temp_storage.update_checkpoint(path, 'ai_processed', True))
        await partial_written.wait()
        for _ in range(3):
            previous = await crawler.temp_storage.load_cleaned_html(path)
            assert previous is not None
            assert previous['metadata']['checkpoints']['ai_processed'] is False
        finish_write.set()
        await writer
        current = await crawler.temp_storage.load_cleaned_html(path)
        assert current['metadata']['checkpoints']['ai_processed'] is True
    asyncio.run(asyncio.wait_for(run(), timeout=10))


def test_metadata_replace_failure_keeps_original_and_sidecar(crawler, monkeypatch):
    from pathlib import Path
    async def run():
        await crawler.reset_state(COMPANY, crawl_run_id='current')
        path, _, _ = await save(crawler)
        meta_path = Path(path).with_suffix('.json')
        original = meta_path.read_bytes()
        def failed_replace(*args):
            raise OSError('mock rename error')
        monkeypatch.setattr(Path, 'replace', failed_replace)
        with pytest.raises(OSError, match='mock rename error'):
            await crawler.temp_storage.save_product_result(path, {'product_id': 'result'})
        assert meta_path.read_bytes() == original
        assert list(meta_path.parent.glob(f'.{meta_path.name}.*.tmp'))
    asyncio.run(run())


def test_concurrent_metadata_updates_preserve_all_worker_fields(crawler):
    async def run():
        await crawler.reset_state(COMPANY, crawl_run_id='current')
        path, _, metadata = await save(crawler)
        await asyncio.gather(
            crawler.temp_storage.update_checkpoint(path, 'indexed', True),
            crawler.temp_storage.mark_as_ai_processed(path, 'saved-ai.txt'),
            crawler.temp_storage.save_product_result(path, {'product_id': 'worker-result'}),
            crawler.temp_storage.save_cleaned_html(URL, metadata['normalized_url'], HTML_A, COMPANY, category='product'))
        final = await crawler.temp_storage.load_cleaned_html(path)
        assert final['metadata']['checkpoints']['ai_processed']
        assert final['metadata']['checkpoints']['indexed']
        assert final['metadata']['ai_text_path'] == 'saved-ai.txt'
        assert final['metadata']['product_result']['product_id'] == 'worker-result'
    asyncio.run(asyncio.wait_for(run(), timeout=10))
