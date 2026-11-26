"""Терминальные checkpoint и три исхода в настоящих JSON/XLSX-файлах."""
import asyncio
import json
from pathlib import Path

import pytest
from openpyxl import load_workbook
from config import Config
from checkpoint_manager import CheckpointManager
from main import Statistics
from xlsx_generator import XLSXGenerator
from test_crawl_pipeline_completeness import COMPANY, crawl_data, system


@pytest.mark.parametrize('history', [False, True])
def test_failed_pipeline_writes_terminal_checkpoint_despite_frequency(tmp_path, history):
    cfg = Config(); cfg.base_dir = str(tmp_path); cfg.checkpoint_keep_history = history
    cfg.checkpoint_frequency_minutes = 30
    obj = system(tmp_path, crawl_data('failed', []))
    obj.checkpoint_manager = CheckpointManager(cfg)
    result = asyncio.run(obj.process_company(dict(COMPANY)))
    saved = json.loads(Path(obj.checkpoint_manager.checkpoint_file).read_text())
    assert result['status'] == 'crawling_failed'
    assert saved['current_stage'] == 'crawling_failed'
    assert saved['progress']['crawl_status'] == result['crawl_status'] == 'failed'
    assert saved['progress']['stop_reasons'] == result['stop_reasons']
    assert saved['company_stats'] == result['company_statistics']
    obj.process_stored_pages_with_checkpoints.assert_not_awaited()
    obj.graph_db_uploader.upload_company_data.assert_not_awaited()
    if history:
        records = [json.loads(p.read_text()) for p in Path(obj.checkpoint_manager.checkpoint_history_dir).glob('*.json')]
        assert {r['current_stage'] for r in records} >= {'company_started', 'crawling_failed'}


def test_intermediate_checkpoint_remains_throttled(tmp_path):
    cfg = Config(); cfg.base_dir = str(tmp_path)
    manager = CheckpointManager(cfg)
    async def run():
        assert await manager.save_checkpoint(COMPANY, set(), 'company_started')
        assert not await manager.save_checkpoint(COMPANY, set(), 'crawling_completed')
        assert await manager.save_checkpoint(COMPANY, set(), 'company_completed', {'crawl_status':'partial'})
    asyncio.run(run())
    saved = json.loads(Path(manager.checkpoint_file).read_text())
    assert saved['progress']['crawl_status'] == 'partial'


@pytest.mark.parametrize('statuses', [[], ['success'], ['partial'], ['crawling_failed'],
    ['success', 'partial', 'crawling_failed', 'error', 'graph_db_fatal_error', None]])
def test_xlsx_separates_partial_and_failures_without_losing_totals(tmp_path, statuses):
    cfg = Config(); cfg.reports_dir = str(tmp_path)
    results = [{'company':dict(COMPANY,company_id=str(i)), 'status':status,
                'company_statistics': {'products_processed':i+1,'input_tokens':10},
                'errors':['Неполный краулинг: page_limit'] if status=='partial' else []}
               for i,status in enumerate(statuses)]
    path = XLSXGenerator(cfg).generate_monitoring_report(results, Statistics(), str(tmp_path), 'test')
    wb = load_workbook(path, read_only=True, data_only=True)
    rows = dict(list(wb['Общая статистика'].values)[1:])
    assert rows['Успешно обработано сайтов'] == statuses.count('success')
    assert rows['Частично обработано сайтов'] == statuses.count('partial')
    assert rows['Ошибки обработки сайтов'] == sum(s not in ('success','partial') for s in statuses)
    assert rows['Количество сайтов для проверки'] == sum(rows[k] for k in (
        'Успешно обработано сайтов','Частично обработано сайтов','Ошибки обработки сайтов'))
    assert rows['Товаров обработано (полная обработка)'] == sum(range(1,len(statuses)+1))
    assert wb.sheetnames == ['Общая статистика','Детализация по компаниям',
                            'Статистика по токенам','Статистика по времени обработки']
    if statuses:
        detail=list(wb['Детализация по компаниям'].values)
        col=detail[0].index('Ошибка при обработке сайта')
        for i,status in enumerate(statuses):
            if status=='partial':assert detail[i+1][col]=='Неполный краулинг: page_limit'
    wb.close()
