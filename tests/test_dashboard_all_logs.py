"""Общий журнал читает оба процесса, сохраняя порядок событий и трассировки."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import metrics


class AllLogsTests(unittest.TestCase):
    def setUp(self):
        data = Path.cwd() / 'test_data'
        data.mkdir(exist_ok=True)
        self.logs = Path(tempfile.mkdtemp(prefix='all_logs_', dir=data))
        env = patch.dict(os.environ, LOGS_DIR=str(self.logs), RUNS_DIR=str(self.logs / 'runs'))
        env.start()
        self.addCleanup(env.stop)

    def write(self, name, text):
        path = self.logs / name
        path.write_text(text, encoding='utf-8')
        return path

    def test_modules_from_both_processes_are_sorted_with_tracebacks(self):
        self.write('monitoring.log',
                   '2026-10-26 10:00:01,001 - web_crawler - INFO - Обход\n'
                   '2026-10-26 10:00:03,003 - ai_integration - ERROR - Ошибка модели\n'
                   'Traceback (most recent call last):\n  строка вызова\nValueError: тест\n'
                   '2026-10-26 10:00:05,005 - graph_db_uploader - INFO - Загрузка\n')
        self.write('pachka_bot.log',
                   '2026-10-26 10:00:02,002 - report_service - INFO - Отчёт\n'
                   '2026-10-26 10:00:04,004 - uvicorn.access - INFO - POST /webhook\n')
        result = metrics.log_tail(source='all')
        text = '\n'.join(result['lines'])
        self.assertLess(text.index('Обход'), text.index('Отчёт'))
        self.assertLess(text.index('Отчёт'), text.index('Ошибка модели'))
        self.assertLess(text.index('ValueError: тест'), text.index('POST /webhook'))
        self.assertLess(text.index('POST /webhook'), text.index('Загрузка'))
        self.assertIn('[Пайплайн]', text)
        self.assertIn('[Бот]', text)
        self.assertIn('Traceback (most recent call last):\n  строка вызова\nValueError: тест', text)
        self.assertEqual(len(result['sources']), 2)
        self.assertTrue(result['modified_at'])

    def test_one_missing_or_unreadable_source_keeps_other_module_events(self):
        self.write('pachka_bot.log', '2026-10-26 10:00:01,001 - invite_manager - INFO - Инвайт\n')
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable):
                original = metrics.log_tail
                def read(*args, **kwargs):
                    if unreadable and kwargs.get('source') == 'monitoring':
                        raise PermissionError('нет доступа')
                    return original(*args, **kwargs)
                with patch.object(metrics, 'log_tail', side_effect=read):
                    result = original(source='all')
                self.assertIn('Инвайт', '\n'.join(result['lines']))
                self.assertTrue(result['detail'])

    def test_empty_logs_and_repeated_reads_and_global_limit(self):
        result = metrics.log_tail(source='all')
        self.assertEqual(result['lines'], [])
        self.assertTrue(result['detail'])
        path = self.write('pachka_bot.log', '2026-10-26 10:00:01,001 - bot - INFO - Первая\n')
        self.write('monitoring.log', '2026-10-26 10:00:02,002 - text_extractor - INFO - Вторая\n')
        self.assertIn('Вторая', metrics.log_tail(lines=1, source='all')['lines'][0])
        with path.open('a', encoding='utf-8') as output:
            output.write('2026-10-26 10:00:03,003 - api_client - INFO - Третья\n')
        self.assertIn('Третья', metrics.log_tail(lines=1, source='all')['lines'][0])

    def test_large_files_are_bounded_and_keep_unicode(self):
        self.write('monitoring.log', ''.join(
            f'2026-10-26 10:00:00,{i:03d} - docx_generator - INFO - Карточка {i} ' + 'я' * 400 + '\n'
            for i in range(1000)))
        self.write('pachka_bot.log', '2026-10-26 11:00:00,000 - bot - INFO - Последнее событие\n')
        result = metrics.log_tail(lines=2, source='all')
        self.assertEqual(len(result['lines']), 2)
        self.assertIn('Карточка 999', result['lines'][0])
        self.assertIn('Последнее событие', result['lines'][1])
        self.assertNotIn('\ufffd', '\n'.join(result['lines']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
