"""Общая очередь отчётов: конкуренция, снимки, сбои и настоящий XLSX/ZIP."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import zipfile

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / 'graph_reports'))
from bot_report.report_service import ReportService
from graph_reports.company_ranker import CompanyRanker
from graph_reports.data_comparator import DataComparator, ChangeType
from report_queue import ReportQueue, company_lock
from bot_report.report_service import ReportService
from bot_report.bot_handlers_pachca import PachcaBot
import bot_report.report_service as service_module


def company(cid='1', upload='2026-09-01', update='', report='', version='v1'):
    return {'id': cid, 'Наименование компании': 'Завод ' + cid,
            'Представительство в РФ': 'есть', 'upload_date': upload,
            'update_date': update, 'report_date': report, 'data_version': version,
            'Телефон': ['123'], 'products': [], 'suppliers': [], 'files': []}


def ids(companies, limit=0):
    return [c['id'] for c in CompanyRanker.rank_companies(companies, limit)]


def test_new_first_then_oldest_unreported_update():
    values = [company('done', report='2026-10-20', update='2026-10-01'),
              company('updated_new', report='2026-09-10', update='2026-10-20'),
              company('new_late', upload='2026-09-15', update='2026-10-25'),
              company('updated_old', report='2026-09-10', update='2026-10-01'),
              company('new_early', upload='2026-09-01', update='2026-10-30')]
    assert ids(values) == ['new_early', 'new_late', 'updated_old', 'updated_new']
    assert ids(values, 3) == ['new_early', 'new_late', 'updated_old']


def test_version_detects_update_during_delivery_and_equal_timestamps():
    value = company(update='2026-10-01', report='2026-10-02', version='v2')
    value['report_version'] = 'v1'
    assert ids([value]) == ['1']
    value['report_version'] = 'v2'
    assert ids([value]) == []
    value['data_update_in_progress'] = True
    value['report_date'] = ''
    assert ids([value]) == []


def test_real_parallel_reservations_are_disjoint(tmp_path):
    companies = [company(str(n)) for n in range(12)]
    queue = ReportQueue(tmp_path)
    def reserve(n):
        return ReportQueue(tmp_path).reserve(companies, 3, n, str(n), CompanyRanker.rank_companies)
    with ThreadPoolExecutor(max_workers=8) as pool:
        batches = list(pool.map(reserve, range(8)))
    selected = [c['id'] for b in batches if b for c in b['companies']]
    assert len(selected) == len(set(selected)) == 12
    assert len(queue.inspect()) == 4


def test_restart_uncertain_delivery_and_explicit_recovery(tmp_path):
    q = ReportQueue(tmp_path)
    b = q.reserve([company()], 1, 10, 'event', CompanyRanker.rank_companies)
    q.mark_sending(b['id'], 'https://example.invalid/archive')
    q = ReportQueue(tmp_path)
    assert not q.release(b['id'], 'тайм-аут')
    assert q.reserve([company()], 1, 11, 'next', CompanyRanker.rank_companies) is None
    q.delivered(b['id'], 'message-1', '2026-10-01 12:00:00')
    assert q.reserve([company()], 1, 11, 'next', CompanyRanker.rank_companies) is None
    newer = company(version='v2', update='2026-10-01 11:00:00')
    new = q.reserve([newer], 1, 11, 'next', CompanyRanker.rank_companies)
    assert new['previous']['1']['data_version'] == 'v1'


def test_pre_send_failure_releases_and_duplicate_event_never_reissues(tmp_path):
    q = ReportQueue(tmp_path)
    b = q.reserve([company()], 1, 10, 'event', CompanyRanker.rank_companies)
    assert q.release(b['id'], 'Ошибка ZIP')
    with pytest.raises(ValueError, match='уже принят'):
        q.reserve([company()], 1, 10, 'event', CompanyRanker.rank_companies)
    assert q.reserve([company()], 1, 11, 'new', CompanyRanker.rank_companies)


@pytest.mark.parametrize('change', ['phone', 'product', 'specification', 'file', 'supplier'])
def test_content_changes_propagate_to_company(change):
    old = company(report='2026-09-10')
    old['products'] = [{'id': 'p1', 'product_name': 'Плита', 'specifications': {'Масса': '5'}, 'files': []}]
    new = deepcopy(old)
    new.update(update_date='2026-10-20', data_version='v2')
    if change == 'phone':
        new['Телефон'] = ['456']
    elif change == 'product':
        new['products'].append({'id': 'p2', 'product_name': 'Новый товар'})
    elif change == 'specification':
        new['products'][0]['specifications']['Масса'] = '6'
    elif change == 'file':
        new['products'][0]['files'] = [{'id': 'f1', 'path': 'new.pdf', 'hash': 'new'}]
    else:
        new['suppliers'] = [{'id': 's1', 'Наименование': 'Дилер'}]
    analysis = DataComparator().analyze_company_changes([old], [new])
    assert analysis['1']['status'] == ChangeType.EDITING


def test_update_without_changes_is_not_new_data():
    old = company(report='2026-09-10')
    new = dict(old, update_date='2026-10-20', data_version='v2')
    assert DataComparator().analyze_company_changes([old], [new])['1']['status'] == ChangeType.NO_CHANGES


@pytest.fixture
def case(tmp_path, monkeypatch):
    config = SimpleNamespace(base_dir=tmp_path, bot_dir=tmp_path/'Bot',
        reports_dir=tmp_path/'Documents/Reports', documents_dir=tmp_path/'Documents',
        archive_temp_dir=tmp_path/'ArchiveTemp', graph_db_api_url='https://example.invalid',
        graph_db_request_timeout=1, general_report_name='Общий.xlsx',
        detailed_report_name='Детальный.xlsx', archive_name='Отчёт.zip', max_archive_size_mb=5000)
    config.documents_dir.mkdir()
    data = SimpleNamespace(current=[company()], history={}, history_calls=[])
    class Graph:
        def __init__(self, config): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get_all_companies_data(self): return deepcopy(data.current)
        async def get_companies_data_on_date(self, date):
            data.history_calls.append(date)
            return deepcopy(data.history[date])
    monkeypatch.setattr(service_module, 'GraphDBClient', Graph)
    monkeypatch.setattr(service_module, 'MonitoringIntegrator', Mock())
    api = SimpleNamespace(upload_archive=AsyncMock(return_value=(True, '', 'https://example.invalid/report')),
                          confirm_report_version=AsyncMock(return_value=True))
    service = ReportService(config, api)
    folder = config.documents_dir/'1_Завод'
    folder.mkdir()
    (folder/'first.pdf').write_bytes(b'first-upload')
    (folder/'version.pdf').write_bytes(b'latest-edition')
    (folder/'new.pdf').write_bytes(b'new-upload')
    (folder/'card.rtf').write_bytes(b'card')
    (folder/'internal.json').write_text('{}')
    # Старые редакции хранятся вне Documents, в истории загрузчика.
    history = tmp_path/'Base/file_download_cache/history/old'
    history.mkdir(parents=True)
    (history/'version.pdf').write_bytes(b'old-edition')
    return SimpleNamespace(config=config, service=service, data=data, api=api, folder=folder)


def test_actual_xlsx_zip_complete_files_and_confirmed_snapshot(case):
    b = asyncio.run(case.service.prepare_report(3, 1, 'request'))
    assert len(b['companies']) == 1
    with zipfile.ZipFile(b['archive_path']) as z:
        contents = [z.read(n) for n in z.namelist()]
        assert b'first-upload' in contents and b'new-upload' in contents
        assert b'latest-edition' in contents and b'old-edition' not in contents
        assert b'card' in contents and b'{}' not in contents
    case.api.upload_archive.assert_awaited_once()
    case.service.queue.mark_sending(b['id'], b['download_url'])
    asyncio.run(case.service.confirm_delivery(b, 'message-1'))
    case.api.confirm_report_version.assert_awaited_once()
    assert case.api.confirm_report_version.await_args.args[2] == 'v1'
    with pytest.raises(ValueError, match='Нет доступных'):
        asyncio.run(case.service.prepare_report(3, 2, 'second-user'))


def test_per_company_legacy_baseline_and_unchanged_report(case):
    old = company(report='2026-09-10 12:00:00')
    case.data.current = [dict(old, update_date='2026-10-20', data_version='v2')]
    case.data.history[old['report_date']] = [old]
    b = asyncio.run(case.service.prepare_report(1, 1, 'request'))
    assert case.data.history_calls == ['2026-09-10 12:00:00']
    book = openpyxl.load_workbook(case.config.reports_dir/b['id']/'Общий.xlsx')
    values = [c.value for row in book.active for c in row]
    assert 'Изменений нет' in values
    book.close()
    case.service.queue.mark_sending(b['id'], b['download_url'])
    asyncio.run(case.service.confirm_delivery(b, 'message-1'))
    assert case.api.confirm_report_version.await_count == 1


def test_snapshot_baseline_survives_new_update_before_delivery(case):
    b = asyncio.run(case.service.prepare_report(1, 1, 'first'))
    case.data.current = [company(update='2026-10-20', version='v2')]
    case.data.current[0]['Телефон'] = ['456']
    case.service.queue.mark_sending(b['id'], b['download_url'])
    asyncio.run(case.service.confirm_delivery(b, 'message-1'))
    second = asyncio.run(case.service.prepare_report(1, 2, 'second'))
    assert second['previous']['1']['Телефон'] == ['123']
    assert not case.data.history_calls
    assert second['archive_path'] != b['archive_path']


def test_failed_graph_confirmation_does_not_duplicate_delivery(case):
    case.api.confirm_report_version.return_value = False
    b = asyncio.run(case.service.prepare_report(1, 1, 'first'))
    case.service.queue.mark_sending(b['id'], b['download_url'])
    asyncio.run(case.service.confirm_delivery(b, 'message-1'))
    assert case.service.queue.unsynced()
    with pytest.raises(ValueError, match='Нет доступных'):
        asyncio.run(case.service.prepare_report(1, 2, 'second'))
    case.api.confirm_report_version.return_value = True
    asyncio.run(case.service.sync_report_dates())
    assert not case.service.queue.unsynced()


def test_upload_failure_releases_reservation(case):
    case.api.upload_archive.return_value = (False, 'Ошибка загрузки', None)
    with pytest.raises(ValueError, match='Ошибка загрузки'):
        asyncio.run(case.service.prepare_report(1, 1, 'failed'))
    assert case.service.queue.inspect()[0]['state'] == 'failed'
    case.api.upload_archive.return_value = (True, '', 'https://example.invalid/report')
    assert asyncio.run(case.service.prepare_report(1, 2, 'retry'))


def test_active_update_and_pending_upload_are_not_selected(case):
    with company_lock(case.config.bot_dir, '1'):
        with pytest.raises(ValueError, match='Нет доступных'):
            asyncio.run(case.service.prepare_report(1, 1, 'busy'))
    pending = case.config.base_dir/'GraphDB_Pending'
    pending.mkdir()
    (pending/'1.json').write_text('{}')
    with pytest.raises(ValueError, match='Нет доступных'):
        asyncio.run(case.service.prepare_report(1, 1, 'partial'))


@pytest.mark.parametrize('delivered', [True, False])
def test_bot_waits_for_message_confirmation(case, delivered):
    bot = PachcaBot.__new__(PachcaBot)
    bot.report_service = case.service
    bot.pachca = SimpleNamespace(send_message_and_get_id=AsyncMock(
        return_value=(delivered, 'message-1' if delivered else None)), edit_message=AsyncMock())
    bot.user_manager = Mock()
    bot.state_manager = Mock()
    asyncio.run(bot._deliver_queued_report(1, 'progress', 3, 'request'))
    record = case.service.queue.inspect()[0]
    assert record['state'] == ('delivered' if delivered else 'sending')
    assert case.api.confirm_report_version.await_count == int(delivered)
    assert 'Запрошено 3; сейчас доступно 1' in bot.pachca.send_message_and_get_id.await_args.args[1]


def test_stale_pdf_is_replaced_by_current_source_under_existing_format_rules(case):
    from file_download_cache import FileDownloadCache, file_sha256
    source = case.folder/'manual.docx'
    pdf = case.folder/'manual.pdf'
    source.write_bytes(b'updated-document')
    pdf.write_bytes(b'old-conversion')
    cache = FileDownloadCache(case.config.base_dir/'Base')
    cache.save(case.folder, 'https://example.invalid/manual', {
        'url': 'https://example.invalid/manual', 'filename': source.name,
        'sha256': file_sha256(source), 'converted_filename': pdf.name,
        'converted_sha256': file_sha256(pdf), 'converted_source_sha256': 'old-source'})
    batch = asyncio.run(case.service.prepare_report(1, 1, 'request'))
    with zipfile.ZipFile(batch['archive_path']) as archive:
        contents = [archive.read(n) for n in archive.namelist()]
        assert b'updated-document' in contents
        assert b'old-conversion' not in contents


def test_copy_error_prevents_delivery_and_releases_reservation(case, monkeypatch):
    import graph_reports.archive_builder as module
    real_copy = module.shutil.copy2
    def fail(source, target, *args, **kwargs):
        if Path(source).name == 'first.pdf':
            raise OSError('Тестовый отказ чтения файла')
        return real_copy(source, target, *args, **kwargs)
    monkeypatch.setattr(module.shutil, 'copy2', fail)
    with pytest.raises(ValueError, match='полный комплект'):
        asyncio.run(case.service.prepare_report(1, 1, 'request'))
    assert case.service.queue.inspect()[0]['state'] == 'failed'
    case.api.upload_archive.assert_not_awaited()


def test_missing_history_is_not_silently_replaced_with_yesterday(case):
    case.data.current = [company(report='2026-09-10', update='2026-10-20')]
    case.data.history['2026-09-10'] = []
    with pytest.raises(ValueError, match='Не найдено состояние'):
        asyncio.run(case.service.prepare_report(1, 1, 'request'))
    case.api.upload_archive.assert_not_awaited()


def test_interrupted_update_stays_out_of_queue(case):
    from report_queue import company_state_path
    with company_lock(case.config.bot_dir, '1'):
        company_state_path(case.config.bot_dir, '1').write_text('updating')
    with pytest.raises(ValueError, match='Нет доступных'):
        asyncio.run(case.service.prepare_report(1, 1, 'request'))


@pytest.mark.parametrize('uploaded,expected', [(True, 'ready'), (False, 'awaiting_graph')])
def test_pipeline_locks_files_until_graph_and_crawl_complete(tmp_path, uploaded, expected):
    # Настоящий wrapper main, без импорта тяжёлого браузерного окружения.
    import ast
    import os
    from report_queue import company_update_lock, company_state_path, company_ready
    tree = ast.parse((ROOT/'main.py').read_text())
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == 'process_company')
    module = ast.Module(body=[method], type_ignores=[])
    scope = dict(Dict=dict, Any=object, os=os, company_update_lock=company_update_lock,
                 company_state_path=company_state_path)
    exec(compile(module, 'main.process_company', 'exec'), scope)
    obj = SimpleNamespace(config=SimpleNamespace(graph_db_pending_dir=str(tmp_path/'GraphDB_Pending'),
        documents_dir=str(tmp_path/'Documents'), graph_db_enable=True))
    async def process(*args):
        with pytest.raises(BlockingIOError):
            with company_lock(tmp_path/'Bot', '1'): pass
        assert not company_ready(tmp_path/'Bot', '1')
        return {'company_statistics': {'graph_db_uploaded': uploaded, 'crawl_status': 'complete'}}
    obj._process_company_locked = process
    asyncio.run(scope['process_company'](obj, {'company_id': '1'}))
    assert company_state_path(tmp_path/'Bot', '1').read_text() == expected


def test_each_company_uses_its_own_previous_report_date(case):
    first = company('1', report='2026-09-10')
    second = company('2', report='2026-09-15')
    case.data.history = {'2026-09-10': [first], '2026-09-15': [second]}
    case.data.current = [dict(first, update_date='2026-10-20', data_version='v2'),
                         dict(second, update_date='2026-10-21', data_version='v2')]
    batch = asyncio.run(case.service.prepare_report(2, 1, 'request'))
    assert case.data.history_calls == ['2026-09-10', '2026-09-15']
    assert len(batch['companies']) == 2


def test_same_file_hash_with_new_check_date_is_unchanged():
    old = company(report='2026-09-10')
    old['products'] = [{'id': 'p1', 'product_name': 'Товар', 'files': [
        {'id': 'f1', 'hash': 'same-content', 'path': 'file.pdf', 'download_date': '2026-09-01'}]}]
    new = deepcopy(old)
    new.update(update_date='2026-10-20', data_version='v2')
    new['products'][0]['files'][0]['download_date'] = '2026-10-20'
    assert DataComparator().analyze_company_changes([old], [new])['1']['status'] == ChangeType.NO_CHANGES


def test_product_documents_from_initial_and_update_are_included(case):
    products = case.folder/'Products'
    product = products/'Плита'/'Documents_product'
    product.mkdir(parents=True)
    (product/'old.pdf').write_bytes(b'initial-product-file')
    (product/'new.pdf').write_bytes(b'new-product-file')
    (products/'catalog.pdf').write_bytes(b'product-catalog')
    batch = asyncio.run(case.service.prepare_report(1, 1, 'request'))
    with zipfile.ZipFile(batch['archive_path']) as archive:
        contents = [archive.read(n) for n in archive.namelist()]
        assert all(b in contents for b in (b'initial-product-file', b'new-product-file', b'product-catalog'))


def test_ambiguous_company_folders_do_not_produce_incomplete_archive(case):
    (case.config.documents_dir/'1_Другое_имя').mkdir()
    with pytest.raises(ValueError, match='Несколько папок'):
        asyncio.run(case.service.prepare_report(1, 1, 'request'))
    case.api.upload_archive.assert_not_awaited()
