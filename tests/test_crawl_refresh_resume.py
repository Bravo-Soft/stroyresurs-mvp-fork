"""Новый мониторинг, версии HTML и восстановление того же прохода без сетевых вызовов."""
import asyncio
import json
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from config import Config
from web_crawler import WebCrawler
from temp_storage_manager import TempStorageManager

URL = 'https://example.ru/catalog/pump-100/'
COMPANY = 'Тест'
HTML_A = '<html><body><h1>Насос 100</h1>' + ('Напор 20 Расход 30 Масса 40 ' * 30) + '</body></html>'
HTML_B = HTML_A.replace('Напор 20', 'Напор 25')

@pytest.fixture
def crawler(tmp_path):
    cfg = Config()
    cfg.base_dir = str(tmp_path)
    c = WebCrawler(cfg)
    c.current_base_url = 'https://example.ru/'
    c._check_memory_usage = AsyncMock()
    return c

async def save(c, html=HTML_A, *, processed=False, url=URL, category='product'):
    norm = c.url_normalizer.normalize_url(url)
    path, is_new, meta = await c.temp_storage.save_cleaned_html(url, norm, html, COMPANY, category=category)
    assert path
    if processed:
        await c.temp_storage.mark_as_ai_processed(path)
    return path, is_new, meta


def test_new_monitoring_refetches_processed_url(crawler):
    async def run():
        await save(crawler, processed=True)
        await crawler.reset_state(COMPANY)
        crawler.current_base_url = 'https://example.ru/'  # crawl_site фиксирует новую базу после reset.
        assert not await crawler._should_skip_url(URL, 0, COMPANY)
        assert crawler.processed_urls == set()
    asyncio.run(run())


def test_new_monitoring_resets_sitemap_and_negative_cache(crawler):
    async def run():
        crawler.sitemap_parser._processed_sitemaps.add('https://example.ru/sitemap.xml')
        crawler.permanent_errors_cache.add(URL)
        await crawler.reset_state(COMPANY)
        assert not crawler.sitemap_parser._processed_sitemaps
        assert not crawler.permanent_errors_cache
    asyncio.run(run())


def test_same_content_keeps_ai_checkpoint_but_updates_run(crawler):
    async def run():
        await crawler.reset_state(COMPANY, crawl_run_id='one')
        path, _, _ = await save(crawler, processed=True)
        await crawler.reset_state(COMPANY, crawl_run_id='two')
        again, is_new, meta = await save(crawler)
        assert path == again and not is_new
        assert meta['checkpoints']['ai_processed']
        assert meta['crawl_run_id'] == 'two'
        assert len(await crawler.temp_storage.get_company_files(COMPANY)) == 1
        assert not await crawler.temp_storage.get_unprocessed_pages(COMPANY)
    asyncio.run(run())


def test_changed_version_preserves_archive_and_is_not_masked_by_old_ai(crawler, monkeypatch):
    def no_delete(*args, **kwargs):
        raise AssertionError('Архив удалять нельзя')
    monkeypatch.setattr(Path, 'unlink', no_delete)
    async def run():
        await crawler.reset_state(COMPANY, crawl_run_id='one')
        path, _, _ = await save(crawler, processed=True)
        old_html, old_meta = Path(path).read_bytes(), Path(path).with_suffix('.json').read_bytes()
        await crawler.reset_state(COMPANY, crawl_run_id='two')
        newer, is_new, meta = await save(crawler, HTML_B)
        assert is_new and newer != path
        assert Path(path).read_bytes() == old_html
        assert Path(path).with_suffix('.json').read_bytes() == old_meta
        assert not await crawler.temp_storage.is_page_processed(meta['normalized_url'], COMPANY, 'product')
        files = await crawler.temp_storage.get_company_files(COMPANY)
        assert [p['html_path'] for p in files] == [newer]
        assert len(list(Path(path).parent.glob('*.html'))) == 2
    asyncio.run(run())


def test_return_to_older_content_creates_new_version(crawler):
    async def run():
        paths=[]
        for i, html in enumerate([HTML_A, HTML_B, HTML_A]):
            await crawler.reset_state(COMPANY, crawl_run_id=str(i))
            p, is_new, meta = await save(crawler, html, processed=True)
            paths.append(p)
            assert is_new
        assert len(set(paths)) == 3
        assert all(Path(p).exists() for p in paths)
        assert [p['html_path'] for p in await crawler.temp_storage.get_company_files(COMPANY)] == [paths[-1]]
    asyncio.run(run())


def test_unchecked_old_pending_pages_are_not_current_input(crawler):
    async def run():
        await crawler.reset_state(COMPANY, crawl_run_id='one')
        await save(crawler)
        await crawler.reset_state(COMPANY, crawl_run_id='two')
        assert not await crawler.temp_storage.get_unprocessed_pages(COMPANY)
        assert len(list(crawler.temp_storage.base_dir.rglob('*.html'))) == 1
    asyncio.run(run())


def test_resume_replays_links_without_refetch(crawler):
    async def run():
        await crawler.reset_state(COMPANY, crawl_run_id='one')
        body=HTML_A.replace('</body>', '<a href="../pump-200/">Насос 200</a></body>')
        await save(crawler, body, processed=True)
        await crawler.reset_state(COMPANY, crawl_run_id='one', resume=True)
        crawler._fetch_page_content=AsyncMock(side_effect=AssertionError('Сохранённый HTML не скачивается'))
        queue=deque()
        crawler.current_base_url = 'https://example.ru/'  # crawl_site фиксирует новую базу после reset.
        assert not await crawler._should_skip_url(URL, 0, COMPANY)
        await crawler._process_page_with_storage(URL, 0, 'product', COMPANY, {}, queue)
        crawler._fetch_page_content.assert_not_awaited()
        assert any(row[0] == 'https://example.ru/catalog/pump-200/' for row in queue)
    asyncio.run(run())


def test_resume_does_not_use_another_runs_html(crawler):
    async def run():
        await crawler.reset_state(COMPANY, crawl_run_id='old')
        await save(crawler, processed=True)
        await crawler.reset_state(COMPANY, crawl_run_id='new', resume=True)
        crawler._fetch_page_content=AsyncMock(return_value=None)
        await crawler._process_page_with_storage(URL, 0, 'product', COMPANY, {}, deque())
        crawler._fetch_page_content.assert_awaited_once_with(URL, 'product')
    asyncio.run(run())


def test_new_monitoring_discovers_changed_html_at_same_url(crawler):
    async def run():
        await crawler.reset_state(COMPANY, crawl_run_id='old')
        await save(crawler, processed=True)
        await crawler.reset_state(COMPANY, crawl_run_id='new')
        body=HTML_B.replace('</body>', '<a href="../pump-200/">Насос 200</a></body>')
        parsed=await crawler._parse_content(body, URL)
        crawler._fetch_page_content=AsyncMock(return_value=parsed)
        queue=deque()
        page=await crawler._process_page_with_storage(URL, 0, 'product', COMPANY, {}, queue)
        assert page and not page['metadata']['checkpoints']['ai_processed']
        assert page['metadata']['crawl_run_id'] == 'new'
        assert any(row[0].endswith('/pump-200/') for row in queue)
    asyncio.run(run())

@pytest.mark.parametrize('stage,same_company,has_id,force,ignore,expected', [
    ('crawling_completed', True, True, False, False, True),
    ('critical_error', True, True, False, False, True),
    ('company_completed', True, True, False, False, False),
    ('paused_for_urgent', True, True, False, False, False),
    ('stopped_by_admin', True, True, False, False, False),
    ('company_started', False, True, False, False, False),
    ('company_started', True, False, False, False, False),
    ('company_started', True, True, True, False, False),
    ('company_started', True, True, False, True, False),
])
def test_checkpoint_selects_resume_only_for_same_incomplete_run(crawler, monkeypatch, stage, same_company, has_id, force, ignore, expected):
    import main
    async def run():
        system=main.MonitoringSystem.__new__(main.MonitoringSystem)
        system.crawler=crawler
        for attr in ['_global_processed_urls','_global_processed_product_ids','_global_processed_slug_keys',
                     '_global_processed_file_ids','_global_processed_distributor_ids','_processing_urls']:
            setattr(system,attr,{'old'})
        company={'company_id':'1','original_name':COMPANY}
        previous={'company_id':'1' if same_company else '2'}
        if has_id:previous['_crawl_run_id']='resume-id'
        system.checkpoint_manager=SimpleNamespace(load_checkpoint=AsyncMock(return_value=SimpleNamespace(current_company=previous,current_stage=stage)))
        monkeypatch.setattr(main.pipeline_control,'ignore_checkpoint',lambda:ignore)
        await system._prepare_company_crawl(company,force)
        assert (company['_crawl_run_id']=='resume-id') == expected
        assert crawler.crawl_run_id == company['_crawl_run_id']
        assert not system._global_processed_urls
        assert not system._global_processed_product_ids
    asyncio.run(run())


def test_legacy_checkpoint_without_result_retries_extraction_and_releases_flag(crawler):
    import main
    async def run():
        await crawler.reset_state(COMPANY)
        path,_,meta=await save(crawler,processed=True)
        system=main.MonitoringSystem.__new__(main.MonitoringSystem)
        system.crawler=crawler
        system._processing_urls_lock=asyncio.Lock()
        system._processing_urls=set()
        system._global_processed_urls=set()
        system.process_product_page=AsyncMock(return_value=None)
        page={'normalized_url':meta['normalized_url'],'file_path':path,'category':'product'}
        await system._process_single_page_with_checkpoint(page,{'original_name':COMPANY},SimpleNamespace(),'.')
        system.process_product_page.assert_awaited_once()
        assert not system._processing_urls
    asyncio.run(run())


def build_system(crawler, tmp_path):
    import main
    from contextlib import nullcontext
    from docx_generator import DOCXGenerator
    from product_id_manager import ProductIDManager
    from unittest.mock import Mock
    system=main.MonitoringSystem.__new__(main.MonitoringSystem)
    system.crawler=crawler
    system.config=crawler.config
    system.processing_tracker=SimpleNamespace(clean_span=lambda name:nullcontext())
    system.statistics=main.Statistics()
    system.product_id_manager=ProductIDManager()
    system._processing_urls_lock=asyncio.Lock()
    system._processing_product_ids_lock=asyncio.Condition()
    for attr in ['_global_processed_urls','_processing_urls','_global_processed_product_ids',
                 '_processing_product_ids','_global_processed_slug_keys','_processing_slug_keys',
                 '_global_processed_file_ids','_global_processed_distributor_ids']:
        setattr(system,attr,set())
    system.checkpoint_manager=SimpleNamespace(load_checkpoint=AsyncMock(return_value=None))
    system._save_markdown_file=AsyncMock()
    system._download_product_files=AsyncMock(return_value=[])
    system._download_images_from_ai_text=AsyncMock(return_value=[])
    system._get_product_folder_path=Mock(return_value=(str(tmp_path),'Products/test'))
    system.docx_generator=DOCXGenerator()
    system.docx_generator._convert_to_rtf=AsyncMock(return_value=None)
    ai={'product':{'product_name':'Насос 100','specifications':{'Напор':'20 м','Расход':'30 м3/ч','Масса':'40 кг'}}}
    system.ai_client=SimpleNamespace(extract_product_info=AsyncMock(return_value=(ai,{'input_tokens':10,'output_tokens':20})))
    return system


def test_crawl_to_llm_card_graph_and_unchanged_refresh(crawler,tmp_path,monkeypatch):
    """Реальный HTML -> storage -> обработчик -> DOCX -> payload; модель/RTF/сеть подменены."""
    import main
    from web_crawler import PageContent
    from graph_db_uploader import GraphDBDataFormatter
    from docx import Document
    async def run():
        system=build_system(crawler,tmp_path)
        company={'company_id':'1','original_name':COMPANY}
        requested='https://example.ru/old-pump'
        final=URL
        await system._prepare_company_crawl(company)
        parsed=await crawler._parse_content(PageContent(HTML_A,requested,final),requested)
        crawler._fetch_page_content=AsyncMock(return_value=parsed)
        page=await crawler._process_page_with_storage(requested,0,'product',COMPANY,{},deque())
        assert page['url']==final and page['metadata']['requested_url']==requested
        stats=main.CompanyStatistics(company)
        product=await system._process_single_page_with_checkpoint(page,company,stats,str(tmp_path))
        assert product and product['source_url']==final
        assert system.ai_client.extract_product_info.await_args.kwargs['page_url']==final
        assert system._download_product_files.await_args.args[0]==final
        card=next(tmp_path.glob('*.docx'))
        assert final in '\n'.join(p.text for p in Document(card).paragraphs)
        payload=GraphDBDataFormatter.format_product_data('1',COMPANY,[product])
        assert payload['companies'][0]['products'][0]['source_url']==final
        assert (await crawler.temp_storage.load_cleaned_html(page['file_path']))['metadata']['product_result']['source_url']==final
        # Новый проход скачивает HTML, но не повторяет модель и генерацию прежней карточки.
        await system._prepare_company_crawl(company)
        fetches=crawler._fetch_page_content.await_count
        await crawler._process_page_with_storage(requested,0,'product',COMPANY,{},deque())
        assert crawler._fetch_page_content.await_count==fetches+1
        current=(await crawler.temp_storage.get_company_files(COMPANY))[0]
        page2=dict(page,file_path=current['html_path'],metadata=current['metadata'])
        recovered=await system._process_single_page_with_checkpoint(page2,company,main.CompanyStatistics(company),str(tmp_path))
        assert recovered['product_id']==product['product_id']
        assert system.ai_client.extract_product_info.await_count==1
        assert system.docx_generator._convert_to_rtf.await_count==1
        # Изменение содержания по прежнему URL запускает извлечение вновь.
        await system._prepare_company_crawl(company)
        crawler._fetch_page_content=AsyncMock(return_value=await crawler._parse_content(PageContent(HTML_B,requested,final),requested))
        changed=await crawler._process_page_with_storage(requested,0,'product',COMPANY,{},deque())
        assert changed
        updated=await system._process_single_page_with_checkpoint(changed,company,main.CompanyStatistics(company),str(tmp_path))
        assert updated
        assert system.ai_client.extract_product_info.await_count==2
    asyncio.run(run())


def test_legacy_saved_ai_response_reused_without_llm(crawler,tmp_path):
    import main
    async def run():
        system=build_system(crawler,tmp_path)
        company={'company_id':'1','original_name':COMPANY}
        await system._prepare_company_crawl(company)
        path,_,meta=await save(crawler)
        ai=system.ai_client.extract_product_info.return_value[0]
        ai_path=await crawler.temp_storage.save_ai_text(COMPANY,URL,meta['normalized_url'],ai)
        await crawler.temp_storage.mark_as_ai_processed(path,ai_path)
        page={'file_path':path,'url':URL,'normalized_url':meta['normalized_url'],'category':'product'}
        product=await system._process_single_page_with_checkpoint(page,company,main.CompanyStatistics(company),str(tmp_path))
        assert product and product['source_url']==URL
        system.ai_client.extract_product_info.assert_not_awaited()
        assert (await crawler.temp_storage.load_cleaned_html(path))['metadata']['product_result']
    asyncio.run(run())


def test_resume_reuses_requested_alias_and_restores_product(crawler,tmp_path):
    import main
    from web_crawler import PageContent
    async def run():
        system=build_system(crawler,tmp_path)
        company={'company_id':'1','original_name':COMPANY}
        await system._prepare_company_crawl(company)
        requested='https://example.ru/old-pump'
        crawler._fetch_page_content=AsyncMock(return_value=await crawler._parse_content(PageContent(HTML_A,requested,URL),requested))
        page=await crawler._process_page_with_storage(requested,0,'product',COMPANY,{},deque())
        product=await system._process_single_page_with_checkpoint(page,company,main.CompanyStatistics(company),str(tmp_path))
        system.checkpoint_manager.load_checkpoint=AsyncMock(return_value=SimpleNamespace(current_company=dict(company),current_stage='crawling_completed'))
        await system._prepare_company_crawl(company)
        crawler._fetch_page_content=AsyncMock(side_effect=AssertionError('Сеть при resume'))
        await crawler._process_page_with_storage(requested,0,'product',COMPANY,{},deque())
        restored=await system._process_single_page_with_checkpoint(page,company,main.CompanyStatistics(company),str(tmp_path))
        assert restored['product_id']==product['product_id']
        crawler._fetch_page_content.assert_not_awaited()
        assert system.ai_client.extract_product_info.await_count==1
    asyncio.run(run())


def test_storage_quota_does_not_delete_resume_inputs(crawler, monkeypatch):
    async def run():
        await crawler.reset_state(COMPANY, crawl_run_id='interrupted')
        old, _, _ = await save(crawler, processed=True)
        crawler.temp_storage.max_size_bytes = 1
        def forbidden_unlink(*args, **kwargs):
            raise AssertionError('Архив и вход возобновления нельзя удалять')
        monkeypatch.setattr(Path, 'unlink', forbidden_unlink)
        new, _, _ = await save(crawler, HTML_B)
        assert new and Path(old).is_file() and Path(new).is_file()
        assert len(await crawler.temp_storage.get_company_files(COMPANY)) == 1
        await crawler.reset_state(COMPANY, crawl_run_id='interrupted', resume=True)
        assert crawler._resume_pages[crawler.url_normalizer.normalize_url(URL)]['html_path'] == new
    asyncio.run(run())


def test_checkpoint_roundtrip_restores_run_then_completed_starts_new(crawler, tmp_path, monkeypatch):
    import main
    from checkpoint_manager import CheckpointManager
    async def run():
        system = build_system(crawler, tmp_path)
        crawler.config.checkpoint_keep_history = False
        system.checkpoint_manager = CheckpointManager(crawler.config)
        monkeypatch.setattr(main.pipeline_control, 'ignore_checkpoint', lambda: False)
        company = {'company_id': '1', 'original_name': COMPANY}
        await system._prepare_company_crawl(company)
        run_id = company['_crawl_run_id']
        path, _, _ = await save(crawler)
        assert await system.checkpoint_manager.save_checkpoint(company, set(), 'company_started')
        # Новые экземпляры читают идентификатор из настоящего JSON чекпоинта.
        resumed = build_system(crawler, tmp_path)
        resumed.checkpoint_manager = CheckpointManager(crawler.config)
        await resumed._prepare_company_crawl(dict(company))
        assert crawler.crawl_run_id == run_id
        assert crawler._resume_pages[crawler.url_normalizer.normalize_url(URL)]['html_path'] == path
        assert await resumed.checkpoint_manager.save_checkpoint(company, set(), 'company_completed')
        await resumed._prepare_company_crawl(dict(company))
        assert crawler.crawl_run_id != run_id and not crawler._resume_pages
    asyncio.run(run())


@pytest.mark.parametrize('corrupt_cache', [False, True])
def test_legacy_missing_or_corrupt_result_recovers_product(crawler, tmp_path, corrupt_cache):
    import main
    async def run():
        system = build_system(crawler, tmp_path)
        company = {'company_id': '1', 'original_name': COMPANY}
        await system._prepare_company_crawl(company)
        path, _, meta = await save(crawler)
        ai_path = tmp_path / 'legacy_ai.txt'
        if corrupt_cache:
            ai_path.write_text('truncated response', encoding='utf-8')
        await crawler.temp_storage.mark_as_ai_processed(path, str(ai_path))
        page = {'file_path': path, 'url': URL, 'normalized_url': meta['normalized_url'], 'category': 'product'}
        product = await system._process_single_page_with_checkpoint(page, company, main.CompanyStatistics(company), str(tmp_path))
        assert product and product['source_url'] == URL
        system.ai_client.extract_product_info.assert_awaited_once()
        assert (await crawler.temp_storage.load_cleaned_html(path))['metadata']['product_result']
    asyncio.run(run())


def test_legacy_saved_negative_verdict_does_not_call_llm(crawler, tmp_path):
    import main
    async def run():
        system = build_system(crawler, tmp_path)
        company = {'company_id': '1', 'original_name': COMPANY}
        await system._prepare_company_crawl(company)
        path, _, meta = await save(crawler)
        ai_path = await crawler.temp_storage.save_ai_text(COMPANY, URL, meta['normalized_url'], {'product': None})
        await crawler.temp_storage.mark_as_ai_processed(path, ai_path)
        page = {'file_path': path, 'url': URL, 'normalized_url': meta['normalized_url'], 'category': 'product'}
        product = await system._process_single_page_with_checkpoint(page, company, main.CompanyStatistics(company), str(tmp_path))
        assert product is None
        system.ai_client.extract_product_info.assert_not_awaited()
        assert not system._processing_urls
    asyncio.run(run())
