"""Проверка журнала процесса бота: настоящие logging, файлы и запуск лаунчера."""

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MVP = Path(__file__).resolve().parents[1]


class LoggingTests(unittest.TestCase):
    def setUp(self):
        data = Path.cwd() / 'test_data'
        data.mkdir(exist_ok=True)
        self.logs = Path(tempfile.mkdtemp(prefix='logging_', dir=data))

    def execute(self, code):
        return subprocess.run([sys.executable, '-c', code],
                              env=dict(os.environ, LOGS_DIR=str(self.logs), PYTHONPATH=str(MVP)),
                              capture_output=True, text=True, timeout=10, cwd=self.logs)

    def text(self):
        return (self.logs / 'pachka_bot.log').read_text(encoding='utf-8')

    def test_all_module_loggers_and_uvicorn_warnings_and_tracebacks(self):
        result = self.execute('''
import logging, warnings
from bot_logging import configure_bot_logging
for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
    log = logging.getLogger(name)
    log.addHandler(logging.NullHandler())
    log.propagate = False
configure_bot_logging()
for name in ('bot_config', 'main_bot', 'bot_handlers_pachca', 'user_manager',
             'invite_manager', 'state_manager', 'pachca_client', 'api_client',
             'monitoring_integrator', 'report_service', 'excel_validator',
             'graph_db_client', 'company_ranker', 'data_comparator',
             'report_generators', 'excel_writer', 'report_archive_builder',
             'kafka_manager', 'uvicorn.error', 'uvicorn.access'):
    logging.getLogger(name).info('Событие модуля %s', name)
warnings.warn('Предупреждение библиотеки', RuntimeWarning)
try:
    raise ValueError('Проверка трассировки')
except ValueError:
    logging.getLogger('report_service').exception('Ошибка отчёта')
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.text()
        for name in ('bot_config', 'report_service', 'report_archive_builder', 'kafka_manager',
                     'uvicorn.error', 'uvicorn.access', 'py.warnings'):
            self.assertIn(' - ' + name + ' - ', text)
        self.assertIn('Traceback (most recent call last):', text)
        self.assertIn('ValueError: Проверка трассировки', text)
        self.assertEqual(text.count('Событие модуля'), 20)

    def test_reconfiguration_keeps_early_events_without_duplicate_entries(self):
        result = self.execute('''
import logging
from bot_logging import configure_bot_logging
configure_bot_logging()
logging.getLogger('bot_config').info('Ранняя настройка')
configure_bot_logging()
logging.getLogger('uvicorn.access').info('Входящий вебхук')
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.text().count('Ранняя настройка'), 1)
        self.assertEqual(self.text().count('Входящий вебхук'), 1)

    def test_launcher_captures_import_failure_before_bot_is_created(self):
        result = self.execute(f'''
import importlib.abc, logging, runpy, sys, types
dotenv = types.ModuleType('dotenv')
dotenv.load_dotenv = lambda *a, **kw: None
sys.modules['dotenv'] = dotenv
class BrokenBot(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == 'bot_report':
            logging.getLogger('bot_config').info('Импорт конфигурации')
            raise RuntimeError('Ошибка импорта зависимости')
sys.meta_path.insert(0, BrokenBot())
runpy.run_path({str(MVP / 'run_bot.py')!r}, run_name='__main__')
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Импорт конфигурации', self.text())
        self.assertIn('Ошибка запуска или работы бота', self.text())
        self.assertIn('RuntimeError: Ошибка импорта зависимости', self.text())

    def test_uvicorn_keeps_application_logging_configuration(self):
        # Выполняем настоящий main с заменой сервера: сокеты и Kafka не нужны.
        tree = ast.parse((MVP / 'bot_report/main_bot.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'main')
        code = ast.unparse(node)
        result = self.execute(f'''
import asyncio, logging, sys, types
from bot_logging import configure_bot_logging
configure_bot_logging()
class Config:
    def __init__(self, app, **kwargs):
        assert kwargs['log_config'] is None
class Server:
    def __init__(self, config): pass
    async def serve(self):
        logging.getLogger('uvicorn.error').info('Сервер запущен')
        logging.getLogger('uvicorn.access').info('POST /webhook 200')
sys.modules['uvicorn'] = types.SimpleNamespace(Config=Config, Server=Server)
app = object()
{code}
asyncio.run(main())
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Сервер запущен', self.text())
        self.assertIn('POST /webhook 200', self.text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
