"""Локальные проверки логов и управления ботом без Docker и сетевых соединений."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dashboard'))
import bot_control
import journal
import metrics


class BotTests(unittest.TestCase):
    def setUp(self):
        data = Path.cwd() / 'test_data'
        data.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix='bot_', dir=data))
        self.logs = self.root / 'logs'
        self.runs = self.logs / 'runs'
        self.runs.mkdir(parents=True)
        (self.root / 'docker-compose.yml').write_text('services: {}\n')
        env = patch.dict(os.environ, LOGS_DIR=str(self.logs), RUNS_DIR=str(self.runs),
                         DASHBOARD_PROJECT_DIR=str(self.root))
        env.start()
        self.addCleanup(env.stop)
        bot_control._update(pending=False, phase='idle', requested_at=None,
                            finished_at=None, error=None)

    def write(self, relative, text):
        path = self.logs / relative
        path.write_text(text, encoding='utf-8')
        return path

    def docker(self, argv, **kwargs):
        if 'inspect' in argv:
            return SimpleNamespace(stdout=json.dumps({
                'com.docker.compose.project': 'custom-project',
                'com.docker.compose.project.working_dir': str(self.root),
            }))
        self.assertEqual(argv[-1], 'bot')
        self.assertNotIn('mvp', argv)
        self.assertNotIn('shell', kwargs)
        if 'build' in argv:
            self.assertEqual(bot_control._STATE['phase'], 'building')
        else:
            self.assertEqual(bot_control._STATE['phase'], 'starting')
        kwargs['stdout'].write('Вывод Docker\n')
        return SimpleNamespace(returncode=0)

    def test_bot_log_and_rebuild_log_are_separate_from_pipeline(self):
        for name, text in [('monitoring.log', 'Пайплайн\n'),
                           ('pachka_bot.log', 'Бот: сообщение\n'),
                           ('runs/bot_rebuild.log', 'Сборка бота\n')]:
            self.write(name, text)
        self.assertEqual(metrics.log_tail(source='bot')['lines'], ['Бот: сообщение'])
        self.assertEqual(metrics.log_tail(source='bot-rebuild')['lines'], ['Сборка бота'])

    def test_missing_bot_logs_do_not_show_pipeline(self):
        self.write('monitoring.log', 'Пайплайн\n')
        for source, filename in [('bot', 'pachka_bot.log'),
                                 ('bot-rebuild', 'runs/bot_rebuild.log')]:
            with self.subTest(source=source):
                result = metrics.log_tail(source=source)
                self.assertEqual(result['path'], str(self.logs / filename))
                self.assertEqual(result['lines'], [])
                self.assertTrue(result['detail'])

    def test_appended_multiline_utf8_bot_logs_are_read_on_each_request(self):
        path = self.write('pachka_bot.log', 'Получен запрос\nТрассировка:\n  Ошибка\n')
        self.assertEqual(metrics.log_tail(lines=2, source='bot')['lines'],
                         ['Трассировка:', '  Ошибка'])
        with path.open('a', encoding='utf-8') as output:
            output.write('Отчёт готов\n')
        self.assertEqual(metrics.log_tail(lines=1, source='bot')['lines'], ['Отчёт готов'])

    def test_tail_limits_and_invalid_utf8(self):
        path = self.write('pachka_bot.log', ''.join(f'{i}\n' for i in range(1200)))
        result = metrics.log_tail(lines=5000, source='bot')['lines']
        self.assertEqual(len(result), 1000)
        self.assertEqual(result[0], '200')
        self.assertEqual(metrics.log_tail(lines=0, source='bot')['lines'], ['1199'])
        path.write_bytes(b'bad \xff\n')
        self.assertEqual(metrics.log_tail(source='bot')['lines'], ['bad \ufffd'])

    def test_existing_log_sources_and_unknown_source_stay_unchanged(self):
        self.write('monitoring.log', 'Пайплайн\n')
        self.write('runs/pipeline_stdout.log', 'stdout\n')
        self.assertEqual(metrics.log_tail()['lines'], ['Пайплайн'])
        self.assertEqual(metrics.log_tail(source='stdout')['lines'], ['stdout'])
        self.assertEqual(metrics.log_tail(source='../../private')['lines'], ['Пайплайн'])

    def test_restart_builds_only_bot_then_starts_and_checks_it(self):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        bot_control._update(pending=True)
        with patch.object(bot_control.subprocess, 'run', side_effect=self.docker) as run, \
                patch.object(bot_control.urllib.request, 'urlopen', return_value=response) as get:
            bot_control._worker(self.root)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[1][-2:], ['build', 'bot'])
        self.assertEqual(commands[2][-1], 'bot')
        for flag in ('--no-deps', '--no-build', '--force-recreate', '--wait'):
            self.assertIn(flag, commands[2])
        self.assertIn('custom-project', commands[1])
        self.assertTrue(get.call_args.args[0].endswith('/openapi.json'))
        self.assertEqual(bot_control._STATE['phase'], 'succeeded')
        self.assertFalse(bot_control._STATE['pending'])
        self.assertIsNone(bot_control._STATE['error'])
        self.assertIn('отвечает на HTTP', (self.runs / 'bot_rebuild.log').read_text())
        self.assertEqual(journal.read()[0]['action'], 'bot.restart')

    def test_build_failure_does_not_restart_the_existing_bot(self):
        def fail_build(argv, **kwargs):
            if 'build' in argv:
                raise subprocess.CalledProcessError(1, argv)
            return self.docker(argv, **kwargs)
        bot_control._update(pending=True)
        with patch.object(bot_control.subprocess, 'run', side_effect=fail_build) as run:
            bot_control._worker(self.root)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(bot_control._STATE['phase'], 'failed')
        self.assertFalse(bot_control._STATE['pending'])
        self.assertIn('кодом 1', bot_control._STATE['error'])
        self.assertEqual(journal.read()[0]['result'], 'error')

    def test_start_failure_and_timeout_are_reported(self):
        for error in (subprocess.CalledProcessError(2, ['up']),
                      subprocess.TimeoutExpired(['up'], 180)):
            with self.subTest(error=type(error).__name__):
                def fail_start(argv, **kwargs):
                    if 'up' in argv:
                        raise error
                    return self.docker(argv, **kwargs)
                bot_control._update(pending=True)
                with patch.object(bot_control.subprocess, 'run', side_effect=fail_start), \
                        patch.object(bot_control, '_wait_ready') as ready:
                    bot_control._worker(self.root)
                ready.assert_not_called()
                self.assertEqual(bot_control._STATE['phase'], 'failed')
                self.assertFalse(bot_control._STATE['pending'])

    def test_running_container_without_ready_bot_is_not_success(self):
        bot_control._update(pending=True)
        with patch.object(bot_control.subprocess, 'run', side_effect=self.docker), \
                patch.object(bot_control.urllib.request, 'urlopen', side_effect=OSError('нет ответа')), \
                patch.object(bot_control.time, 'sleep'):
            bot_control._worker(self.root)
        self.assertEqual(bot_control._STATE['phase'], 'failed')
        self.assertIn('не ответил', bot_control._STATE['error'])

    def test_project_path_mismatch_prevents_build_and_restart(self):
        labels = {'com.docker.compose.project': 'other',
                  'com.docker.compose.project.working_dir': '/other-project'}
        with patch.object(bot_control.subprocess, 'run',
                          return_value=SimpleNamespace(stdout=json.dumps(labels))) as run:
            bot_control._worker(self.root)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(bot_control._STATE['phase'], 'failed')
        self.assertIn('не совпадает', bot_control._STATE['error'])

    def test_restart_is_async_and_duplicate_is_rejected(self):
        with patch.object(bot_control, 'unavailable_reason', return_value=None), \
                patch.object(bot_control.threading, 'Thread') as thread:
            result = bot_control.restart_async()
            self.assertTrue(result['pending'])
            thread.return_value.start.assert_called_once()
            with self.assertRaises(bot_control.AlreadyRunning):
                bot_control.restart_async()
            self.assertEqual(thread.call_count, 1)

    def test_restart_without_docker_is_unavailable(self):
        with patch.object(bot_control, 'unavailable_reason', return_value='Нет Docker'), \
                patch.object(bot_control.threading, 'Thread') as thread:
            self.assertFalse(bot_control.status()['available'])
            with self.assertRaises(bot_control.Unavailable):
                bot_control.restart_async()
            thread.assert_not_called()

    def test_log_directory_failure_clears_pending(self):
        bot_control._update(pending=True)
        with patch.object(bot_control, 'runs_dir', side_effect=OSError('Каталог недоступен')):
            bot_control._worker(self.root)
        self.assertFalse(bot_control._STATE['pending'])
        self.assertEqual(bot_control._STATE['error'], 'Каталог недоступен')

    def test_compose_environment_is_not_inherited_from_another_project(self):
        with patch.dict(os.environ, COMPOSE_FILE='/other.yml', COMPOSE_PROFILES='other',
                        COMPOSE_PROJECT_NAME='other'), \
                patch.object(bot_control.subprocess, 'run', side_effect=self.docker) as run, \
                patch.object(bot_control, '_wait_ready'):
            bot_control._worker(self.root)
        for call in run.call_args_list:
            env = call.kwargs['env']
            self.assertEqual(env['DASHBOARD_PROJECT_DIR'], str(self.root))
            for key in ('COMPOSE_FILE', 'COMPOSE_PROFILES', 'COMPOSE_PROJECT_NAME'):
                self.assertNotIn(key, env)


if __name__ == '__main__':
    unittest.main(verbosity=2)
