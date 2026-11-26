"""Новая urgent задача не наследует HTML незавершённого прохода другой задачи."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipeline_orchestrator import PipelineOrchestrator
from test_crawl_refresh_resume import COMPANY, build_system, crawler


@pytest.mark.parametrize('previous_task,current_task,resume', [
    ('urgent-1', 'urgent-1', True),
    ('urgent-1', 'urgent-2', False),
    ('urgent-1', None, False),
    (None, 'urgent-1', False),
    (None, None, True),
])
def test_resume_matches_monitoring_task_identity(crawler, tmp_path, monkeypatch,
                                                 previous_task, current_task, resume):
    import main
    async def run():
        system = build_system(crawler, tmp_path)
        checkpoint_company = {'company_id': '1', 'original_name': COMPANY, '_crawl_run_id': 'prior-run'}
        company = {'company_id': '1', 'original_name': COMPANY}
        if previous_task is not None:
            checkpoint_company['_monitoring_task_id'] = previous_task
        if current_task is not None:
            company['_monitoring_task_id'] = current_task
        system.checkpoint_manager.load_checkpoint = AsyncMock(return_value=SimpleNamespace(
            current_company=checkpoint_company, current_stage='critical_error'))
        monkeypatch.setattr(main.pipeline_control, 'ignore_checkpoint', lambda: False)
        await system._prepare_company_crawl(company)
        assert (company['_crawl_run_id'] == 'prior-run') == resume
    asyncio.run(run())


@pytest.mark.parametrize('checkpoint_task,resume', [('urgent-new', True), ('urgent-old', False), (None, False)])
def test_actual_urgent_dispatch_carries_task_id_into_crawl(crawler, tmp_path, monkeypatch,
                                                          checkpoint_task, resume):
    import main
    async def run():
        system = build_system(crawler, tmp_path)
        previous = {'company_id': '1', 'original_name': COMPANY, '_crawl_run_id': 'prior-run'}
        if checkpoint_task:
            previous['_monitoring_task_id'] = checkpoint_task
        system.checkpoint_manager.load_checkpoint = AsyncMock(return_value=SimpleNamespace(
            current_company=previous, current_stage='critical_error'))
        monkeypatch.setattr(main.pipeline_control, 'ignore_checkpoint', lambda: False)
        seen = []
        async def process_company(company_data):
            await system._prepare_company_crawl(company_data)
            seen.append(company_data)
            return {'status': 'success'}
        system.process_company = process_company
        orchestrator = PipelineOrchestrator.__new__(PipelineOrchestrator)
        orchestrator.config = SimpleNamespace(company_idle_timeout_seconds=0)
        orchestrator._running = True
        orchestrator._stop_if_requested = AsyncMock(return_value=False)
        orchestrator.kafka_manager = SimpleNamespace(
            get_urgent_task=AsyncMock(return_value=None), send_task_status=AsyncMock())
        original = {'company_id': '1', 'original_name': COMPANY}
        task = SimpleNamespace(task_id='urgent-new', company_data=original)
        await orchestrator._process_all_urgent_tasks(system, task)
        assert len(seen) == 1
        assert seen[0]['_monitoring_task_id'] == task.task_id
        assert (seen[0]['_crawl_run_id'] == 'prior-run') == resume
        assert original == {'company_id': '1', 'original_name': COMPANY}
    asyncio.run(run())
