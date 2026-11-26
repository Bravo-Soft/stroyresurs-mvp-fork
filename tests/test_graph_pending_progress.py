"""Регрессия прогресса Graph DB: частичная отправка, retry и новый загрузчик."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import graph_db_uploader as graph


class LocalAsyncFile:
    """Настоящие локальные JSON без executor-потоков, ограниченных песочницей."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    async def __aenter__(self):
        self.file = open(*self.args, **self.kwargs)
        return self

    async def __aexit__(self, *args):
        self.file.close()

    async def read(self):
        return self.file.read()

    async def write(self, value):
        return self.file.write(value)


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(graph, 'aiofiles', SimpleNamespace(open=LocalAsyncFile))
    config = SimpleNamespace(
        base_dir=str(tmp_path),
        graph_db_api_url='https://graph-db.invalid/api/companies',
        graph_db_max_retries=1,
        graph_db_request_timeout=1,
        graph_db_json_output_dir=str(tmp_path / 'failed'),
        graph_db_archive_dir=str(tmp_path / 'archive'),
        graph_db_pending_dir=str(tmp_path / 'pending'),
        graph_db_enable_chunking=True,
        graph_db_chunk_size_products=1,
        graph_db_chunk_size_distributors=1,
        graph_db_upload_company=True,
        graph_db_upload_products=True,
        graph_db_upload_distributors=True,
        graph_db_max_retries_on_resume=3,
        graph_db_delay_between_chunks=0,
        graph_db_delay_between_companies=0,
    )
    inputs = dict(
        company_data={'company_id': 'test-company', 'actual_name': 'Тестовый завод',
                      'website': 'https://manufacturer.invalid/'},
        company_info={'company': {'Наименование компании': 'Тестовый завод'}},
        products=[{'product_id': f'p{i}', 'ai_raw_text': {'product': {
            'product_name': f'Товар {i}', 'specifications': {'Масса, кг': str(i)}}}}
                  for i in range(1, 4)],
        distributors=[{'distributor_id': f'd{i}', 'name': f'Дилер {i}'}
                      for i in range(1, 4)],
        company_files=[],
    )
    path = tmp_path / 'pending/test-company.json'
    return SimpleNamespace(config=config, inputs=inputs, path=path)


def pending(case):
    return json.loads(case.path.read_text(encoding='utf-8'))['pending']


@pytest.mark.parametrize('data_type', ['products', 'distributors'])
@pytest.mark.parametrize('fatal', [False, True], ids=['ordinary', 'fatal'])
@pytest.mark.parametrize('is_update', [False, True], ids=['initial', 'update'])
def test_partial_upload_preserves_progress_and_resume_sends_only_failed(
        case, data_type, fatal, is_update):
    calls = []

    async def send(data, kind, company_id, company_name, chunk_num=1, total_chunks=1):
        calls.append((kind, chunk_num))
        assert data['is_update'] is is_update
        if kind == data_type and chunk_num == 2:
            if fatal:
                raise graph.GraphDBFatalError('Тестовый сетевой отказ')
            return False
        return True

    uploader = graph.GraphDBUploader(case.config)
    uploader.send_data = send
    if fatal:
        with pytest.raises(graph.GraphDBFatalError):
            asyncio.run(uploader.upload_company_data(**case.inputs, is_update=is_update))
    else:
        assert asyncio.run(uploader.upload_company_data(**case.inputs, is_update=is_update)) is False

    state = pending(case)
    assert state['company']['status'] == 'success'
    for kind in ('products', 'distributors'):
        assert [c['status'] for c in state[kind]] == (
            ['success', 'failed', 'success'] if kind == data_type else ['success'] * 3)
        assert [c['retry_count'] for c in state[kind]] == (
            [0, 1, 0] if kind == data_type else [0] * 3)
    assert len(calls) == 7

    # Новый экземпляр восстанавливает только состояние с диска, не память первого.
    resumed = graph.GraphDBUploader(case.config)
    resumed.send_data = AsyncMock(return_value=True)
    assert asyncio.run(resumed.resume_all_pending_uploads()) is True
    assert resumed.send_data.await_count == 2
    assert resumed.send_data.await_args_list[-1].args[1] == 'company_complete'
    args = resumed.send_data.await_args_list[0].args
    assert args[1] == data_type and args[4:6] == (2, 3)
    assert args[0]['is_update'] is is_update
    assert not case.path.exists()


@pytest.mark.parametrize('data_type', ['products', 'distributors'])
@pytest.mark.parametrize('fatal', [False, True], ids=['ordinary', 'fatal'])
def test_retry_count_survives_restarts_and_stops_at_limit(case, data_type, fatal):
    calls = []

    async def send(data, kind, company_id, company_name, chunk_num=1, total_chunks=1):
        calls.append((kind, chunk_num))
        if kind == data_type and chunk_num == 2:
            if fatal:
                raise graph.GraphDBFatalError('Тестовый повторяемый сетевой отказ')
            return False
        return True

    for attempt in range(1, 4):
        uploader = graph.GraphDBUploader(case.config)
        uploader.send_data = send
        if fatal:
            with pytest.raises(graph.GraphDBFatalError):
                asyncio.run(uploader.upload_company_data(**case.inputs))
        else:
            assert asyncio.run(uploader.upload_company_data(**case.inputs)) is False
        chunks = pending(case)[data_type]
        assert [c['status'] for c in chunks] == ['success', 'failed', 'success']
        assert [c['retry_count'] for c in chunks] == [0, attempt, 0]
    assert calls.count((data_type, 2)) == 3
    assert len(calls) == 9

    exhausted = graph.GraphDBUploader(case.config)
    exhausted.send_data = AsyncMock(return_value=True)
    assert asyncio.run(exhausted.upload_company_data(**case.inputs)) is False
    exhausted.send_data.assert_not_awaited()
    assert pending(case)[data_type][1]['retry_count'] == 3


@pytest.mark.parametrize('fatal', [False, True], ids=['ordinary', 'fatal'])
def test_failed_company_does_not_reset_successful_product_and_distributor_chunks(case, fatal):
    async def send(data, kind, company_id, company_name, chunk_num=1, total_chunks=1):
        if kind == 'company':
            if fatal:
                raise graph.GraphDBFatalError('Тестовый отказ отправки компании')
            return False
        return True

    uploader = graph.GraphDBUploader(case.config)
    uploader.send_data = send
    if fatal:
        with pytest.raises(graph.GraphDBFatalError):
            asyncio.run(uploader.upload_company_data(**case.inputs))
    else:
        assert asyncio.run(uploader.upload_company_data(**case.inputs)) is False
    state = pending(case)
    assert state['company']['status'] == 'failed'
    for kind in ('products', 'distributors'):
        assert [c['status'] for c in state[kind]] == ['success'] * 3

    resumed = graph.GraphDBUploader(case.config)
    resumed.send_data = AsyncMock(return_value=True)
    assert asyncio.run(resumed.resume_all_pending_uploads()) is True
    assert [call.args[1] for call in resumed.send_data.await_args_list] == ['company', 'company_complete']


@pytest.mark.parametrize('is_update', [False, True], ids=['initial', 'update'])
def test_complete_success_still_finishes_pending_upload(case, is_update):
    uploader = graph.GraphDBUploader(case.config)
    uploader.send_data = AsyncMock(return_value=True)
    assert asyncio.run(uploader.upload_company_data(**case.inputs, is_update=is_update)) is True
    assert uploader.send_data.await_count == 8
    assert uploader.stats['companies_sent'] == 1
    assert uploader.stats['products_sent'] == 3
    assert uploader.stats['distributors_sent'] == 3
    assert not case.path.exists()


@pytest.mark.parametrize('is_update', [False, True])
def test_uploader_preserves_report_date_and_publishes_completed_version_last(case, is_update):
    uploader = graph.GraphDBUploader(case.config)
    uploader.send_data = AsyncMock(return_value=True)
    assert asyncio.run(uploader.upload_company_data(**case.inputs, is_update=is_update))
    calls = uploader.send_data.await_args_list
    for call in calls:
        for company in call.args[0]['companies']:
            assert 'report_date' not in company
            assert 'report_version' not in company
    first = calls[0].args[0]['companies'][0]
    completed = calls[-1].args[0]['companies'][0]
    assert first['data_update_in_progress'] is True
    assert completed['data_update_in_progress'] is False
    assert completed['data_version']
    assert calls[-1].args[1] == 'company_complete'
    assert 'upload_date' not in completed
    if is_update:
        assert 'upload_date' not in first and 'update_date' not in first
        assert completed['update_date']
    else:
        assert first['upload_date']


def test_failed_completion_retries_only_final_marker(case):
    async def send(data, kind, *args):
        return kind != 'company_complete'
    uploader = graph.GraphDBUploader(case.config)
    uploader.send_data = AsyncMock(side_effect=send)
    assert not asyncio.run(uploader.upload_company_data(**case.inputs, is_update=True))
    assert case.path.exists()
    resumed = graph.GraphDBUploader(case.config)
    resumed.send_data = AsyncMock(return_value=True)
    assert asyncio.run(resumed.resume_all_pending_uploads())
    resumed.send_data.assert_awaited_once()
    assert resumed.send_data.await_args.args[1] == 'company_complete'


@pytest.mark.parametrize('data_type', ['products', 'distributors'])
def test_legacy_state_without_retry_count_keeps_saved_progress(case, data_type):
    uploader = graph.GraphDBUploader(case.config)
    state = asyncio.run(uploader.pending_manager.create_or_load_state(
        'test-company', 'Тестовый завод', False, case.inputs['company_info'],
        case.inputs['products'], case.inputs['distributors'], []))
    state['pending']['company']['status'] = 'success'
    for kind in ('products', 'distributors'):
        for c in state['pending'][kind]:
            c['status'] = 'success'
            c.pop('retry_count')
    state['pending'][data_type][1]['status'] = 'failed'
    case.path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')

    resumed = graph.GraphDBUploader(case.config)
    resumed.send_data = AsyncMock(return_value=False)
    assert asyncio.run(resumed.upload_company_data(**case.inputs)) is False
    resumed.send_data.assert_awaited_once()
    chunks = pending(case)[data_type]
    assert [c['status'] for c in chunks] == ['success', 'failed', 'success']
    assert chunks[1]['retry_count'] == 1
