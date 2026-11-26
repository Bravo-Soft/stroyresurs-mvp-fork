"""Проверки телеметрии и записи/чтения логов без запуска рабочего пайплайна и бота."""

import ast
import asyncio
import copy
import importlib.util
import json
import logging
import os
import sys
import tempfile
import time
import types
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import AsyncMock, Mock, patch

MVP = Path(__file__).resolve().parents[1]
WORK = Path.cwd()
sys.path[:0] = [str(MVP / 'dashboard'), str(MVP)]
import metrics
from run_recorder import RunRecorder


def definitions(path, names, namespace):
    nodes = []
    for node in ast.parse(path.read_text()).body:
        if getattr(node, 'name', None) in names:
            node = copy.deepcopy(node)
            if not isinstance(node, ast.ClassDef):
                node.decorator_list = []
            nodes.append(node)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


class ModeTests(unittest.TestCase):
    def setUp(self):
        folder = WORK / 'test_data'
        folder.mkdir(exist_ok=True)
        self.logs = Path(tempfile.mkdtemp(prefix='mode_', dir=folder))
        env = patch.dict(os.environ, LOGS_DIR=str(self.logs), RUNS_DIR=str(self.logs / 'runs'))
        env.start(); self.addCleanup(env.stop)

    def event(self, stamp, name, update):
        mode = 'ОБНОВЛЕНИЯ' if update else 'ПЕРВИЧНОЙ ЗАГРУЗКИ'
        return f'{stamp.replace("T", " ")},123 - main - INFO - Режим {mode} для компании: {name}\n'

    def rows(self, records, start='2026-10-23T06:28:01'):
        run = 'run_20261023_062731'
        directory = self.logs / 'runs' / run
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'run.json').write_text(json.dumps({'started_at': start}))
        (directory / 'companies.jsonl').write_text('\n'.join(json.dumps(r) for r in records) + '\n')
        return metrics.companies(run)['companies']

    def test_statistics_and_recorder_keep_both_modes_even_without_products(self):
        stats_class = definitions(MVP / 'main.py', {'CompanyStatistics'},
                                  {'time': time, 'Dict': Dict})['CompanyStatistics']
        for update, expected in ((False, 'initial'), (True, 'update')):
            with self.subTest(mode=expected):
                company = {'company_id': str(update), 'original_name': 'Компания'}
                stats = stats_class(company)
                if update:
                    stats.update_mode = True
                self.assertIs(stats.to_dict().get('update_mode'), update)
                recorder = RunRecorder(str(self.logs), run_id='run_20261023_062731')
                recorder.start('full', 1)
                recorder.company_started(company)
                record = recorder.company_finished(company, {
                    'status': 'no_products_processed', 'company_statistics': stats.to_dict()})
                self.assertIs(record['statistics'].get('update_mode'), update)
                row = metrics.companies(recorder.run_id)['companies'][-1]
                self.assertEqual(row.get('processing_mode'), expected)

    def test_existing_lab_records_restore_initial_then_update_from_log(self):
        (self.logs / 'monitoring.log').write_bytes((WORK / 'fixtures/monitoring_modes.log').read_bytes())
        for run, expected in [('run_20261023_055102', 'initial'), ('run_20261023_062731', 'update')]:
            source = WORK / 'fixtures/runs' / run
            target = self.logs / 'runs' / run
            target.mkdir(parents=True)
            for name in ('run.json', 'companies.jsonl'):
                (target / name).write_bytes((source / name).read_bytes())
            row = next(r for r in metrics.companies(run)['companies'] if r['company_id'] == '896711983')
            self.assertEqual(row.get('processing_mode'), expected)

    def test_old_mode_from_previous_run_and_future_event_are_not_used(self):
        text = self.event('2026-10-22T10:00:00', 'Компания', False)
        text += self.event('2026-10-23T07:00:00', 'Компания', True)
        (self.logs / 'monitoring.log').write_text(text)
        row = self.rows([{'name': 'Компания', 'ts': '2026-10-23T06:37:10'}])[0]
        self.assertIsNone(row.get('processing_mode'))

    def test_explicit_mode_wins_over_legacy_log(self):
        (self.logs / 'monitoring.log').write_text(self.event('2026-10-23T06:29:00', 'Компания', True))
        row = self.rows([{'name': 'Компания', 'ts': '2026-10-23T06:37:10',
                          'statistics': {'update_mode': False}}])[0]
        self.assertEqual(row.get('processing_mode'), 'initial')

    def test_cache_reads_appended_and_split_lines_and_resets_after_rotation(self):
        path = self.logs / 'monitoring.log'
        records = [{'name': 'Компания', 'ts': '2026-10-23T06:37:10'}]
        line = self.event('2026-10-23T06:30:00', 'Компания', False)
        path.write_text(line[:-3])
        self.assertIsNone(self.rows(records)[0].get('processing_mode'))
        with path.open('a') as output:
            output.write(line[-3:])
        self.assertEqual(self.rows(records)[0].get('processing_mode'), 'initial')
        # Новый inode вместо удаления старого файла.
        replacement = self.logs / 'replacement.log'
        replacement.write_text(self.event('2026-10-23T06:31:00', 'Компания', True))
        old = self.logs / 'previous.log'
        path.rename(old); replacement.rename(path)
        self.assertEqual(self.rows(records)[0].get('processing_mode'), 'update')

    def test_missing_log_or_run_timestamp_does_not_guess_mode(self):
        for start in ('', '2026-10-23T06:28:01'):
            row = self.rows([{'name': 'Компания', 'ts': '2026-10-23T06:37:10'}], start=start)[0]
            self.assertIsNone(row.get('processing_mode'))

    def test_mode_line_crossing_read_chunk_is_preserved(self):
        line = self.event('2026-10-23T06:30:00', 'Компания', True).encode()
        prefix = b'x' * (1048576 - 40) + b'\n'
        (self.logs / 'monitoring.log').write_bytes(prefix + line)
        row = self.rows([{'name': 'Компания', 'ts': '2026-10-23T06:37:10'}])[0]
        self.assertEqual(row.get('processing_mode'), 'update')

    def test_log_api_prohibits_caching_and_reports_file_time(self):
        response = types.SimpleNamespace(headers={})
        namespace = {'Response': object, 'metrics': metrics}
        definitions(MVP / 'dashboard/app.py', {'logs_tail'}, namespace)
        (self.logs / 'pachka_bot.log').write_text('Первая запись\n')
        first = asyncio.run(namespace['logs_tail'](response, source='bot'))
        self.assertEqual(response.headers.get('Cache-Control'), 'no-store')
        self.assertTrue(first.get('modified_at'))
        with (self.logs / 'pachka_bot.log').open('a') as output:
            output.write('Вторая запись\n')
        second = asyncio.run(namespace['logs_tail'](response, source='bot'))
        self.assertEqual(second['lines'], ['Первая запись', 'Вторая запись'])

    def test_bot_and_panel_use_the_same_log_directory_from_environment(self):
        namespace = {'__name__': __name__, 'os': os, 'Path': Path, 'dataclass': dataclass,
                     'field': field, 'List': List, 'log': logging.getLogger('test_config'),
                     'load_dotenv': Mock()}
        definitions(MVP / 'bot_report/bot_config.py', {'BotConfig'}, namespace)
        config = namespace['BotConfig'](base_dir=self.logs, logs_dir=str(self.logs / 'wrong'))
        self.assertEqual(config.logs_dir, str(self.logs))


class BotLogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        data = WORK / 'test_data'; data.mkdir(exist_ok=True)
        self.logs = Path(tempfile.mkdtemp(prefix='bot_events_', dir=data))
        env = patch.dict(os.environ, LOGS_DIR=str(self.logs), RUNS_DIR=str(self.logs / 'runs'))
        env.start(); self.addCleanup(env.stop)
        self.handler = logging.FileHandler(self.logs / 'pachka_bot.log', encoding='utf-8')
        self.handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        for name in ('main_bot', 'bot_handlers_pachca', 'invite_manager'):
            logger = logging.getLogger(name)
            original = (list(logger.handlers), logger.level, logger.propagate)
            logger.handlers = [self.handler]; logger.setLevel(logging.INFO); logger.propagate = False
            self.addCleanup(self.restore_logger, logger, original)
        self.addCleanup(self.handler.close)
        spec = importlib.util.spec_from_file_location('test_invite_storage', MVP / 'bot_report/invite_manager.py')
        storage = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = storage
        spec.loader.exec_module(storage)
        config = types.SimpleNamespace(bot_dir=self.logs / 'Bot')
        self.invites = storage.InviteManager(config)
        self.invites.invites['TESTINVITE'] = storage.InviteCode(
            code='TESTINVITE', created_by=42, created_at=datetime.now().isoformat(),
            expires_at=(datetime.now() + timedelta(days=1)).isoformat())
        self.invites._save_invites()
        ns = {'Dict': Dict, 'List': List, 'Optional': Optional, 'BotConfig': object,
              'datetime': datetime, 'timedelta': timedelta, 'log': logging.getLogger('bot_handlers_pachca')}
        definitions(MVP / 'bot_report/bot_handlers_pachca.py', {'UserStates', 'CallbackData', 'PachcaBot'}, ns)
        self.bot = ns['PachcaBot'].__new__(ns['PachcaBot'])
        self.bot.invite_manager = self.invites
        self.bot.pachca = types.SimpleNamespace(send_message=AsyncMock(), verify_webhook=Mock(return_value=True))
        self.bot.state_manager = types.SimpleNamespace(clear=Mock())
        self.bot._notify_admins = AsyncMock()
        class HTTPError(Exception):
            def __init__(self, status_code, detail):
                self.status_code, self.detail = status_code, detail
        ns = {'asyncio': asyncio, 'json': json, 'log': logging.getLogger('main_bot'), 'bot': self.bot,
              'Request': object, 'Header': lambda *a, **kw: None, 'HTTPException': HTTPError,
              'JSONResponse': lambda content, status_code=200: types.SimpleNamespace(content=content, status_code=status_code)}
        self.web = definitions(MVP / 'bot_report/main_bot.py', {'webhook', '_run_bot_action'}, ns)

    @staticmethod
    def restore_logger(logger, original):
        logger.handlers, logger.level, logger.propagate = original

    async def request(self, payload):
        request = types.SimpleNamespace(body=AsyncMock(return_value=json.dumps(payload).encode()),
                                        json=AsyncMock(return_value=payload))
        tasks = []
        create_task = asyncio.create_task
        def capture(action):
            task = create_task(action); tasks.append(task); return task
        with patch.object(asyncio, 'create_task', side_effect=capture):
            response = await self.web['webhook'](request, 'test-signature')
        if tasks:
            await asyncio.gather(*tasks)
        return response

    async def test_button_delete_is_written_and_immediately_read_by_panel(self):
        response = await self.request({'event': 'click', 'type': 'button', 'user_id': 42,
                                       'data': 'admin_confirm_delete_TESTINVITE', 'message_id': '123'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('TESTINVITE', self.invites.invites)
        self.assertNotIn('TESTINVITE', json.loads(self.invites.invites_file.read_text()))
        result = metrics.log_tail(source='bot')
        text = '\n'.join(result['lines'])
        self.assertIn('Нажатие кнопки: пользователь=42', text)
        self.assertIn('Удаление инвайта: администратор=42', text)
        self.assertIn('Обработка события завершена', text)
        self.assertTrue(result.get('modified_at'))

    async def test_text_invite_delete_and_not_found_are_audited(self):
        await self.bot._process_admin_delete_invite_code(42, ' testinvite ', '123')
        await self.bot._process_admin_delete_invite_code(42, ' testinvite ', '124')
        text = '\n'.join(metrics.log_tail(source='bot')['lines'])
        self.assertIn('результат=удалён', text)
        self.assertIn('результат=не найден/ошибка', text)

    async def test_background_handler_error_is_in_bot_log(self):
        async def fail():
            raise RuntimeError('Тестовая ошибка обработчика')
        await self.web['_run_bot_action'](fail(), 42, '123')
        text = '\n'.join(metrics.log_tail(source='bot')['lines'])
        self.assertIn('Ошибка обработки события', text)
        self.assertIn('Тестовая ошибка обработчика', text)

    async def test_legacy_callback_keeps_working(self):
        self.bot.handle_callback = AsyncMock()
        await self.request({'event': 'callback_query_created',
                            'callback_query': {'user_id': 42, 'data': 'help', 'message': {'id': '123'}}})
        self.bot.handle_callback.assert_awaited_once_with(42, 'help', '123')

    async def test_invalid_signature_does_not_dispatch_action(self):
        self.bot.pachca.verify_webhook.return_value = False
        self.bot.handle_callback = AsyncMock()
        with self.assertRaises(self.web['HTTPException']) as error:
            await self.request({'event': 'click', 'type': 'button', 'user_id': 42, 'data': 'help'})
        self.assertEqual(error.exception.status_code, 403)
        self.bot.handle_callback.assert_not_awaited()


if __name__ == '__main__':
    unittest.main(verbosity=2)
