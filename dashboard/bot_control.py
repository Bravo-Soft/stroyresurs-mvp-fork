# Управление только сервисом bot; сборка и запуск выполняются в фоновом потоке.
# Docker CLI и Compose находятся в образе панели, сокет подключён через compose.

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import journal
from common import runs_dir

_LOCK = threading.Lock()
_STATE = {'pending': False, 'phase': 'idle', 'requested_at': None,
          'finished_at': None, 'error': None}
_DOCKER = ['docker', '--host', 'unix:///var/run/docker.sock']


class Unavailable(RuntimeError):
    """Панель не настроена для управления контейнером бота."""


class AlreadyRunning(RuntimeError):
    """Пересборка бота уже выполняется."""


def unavailable_reason():
    root = os.environ.get('DASHBOARD_PROJECT_DIR', '')
    if not root or not Path(root).is_absolute():
        return 'Не задан абсолютный путь проекта DASHBOARD_PROJECT_DIR'
    if not (Path(root) / 'docker-compose.yml').is_file():
        return 'Каталог проекта недоступен панели'
    if not shutil.which('docker'):
        return 'В образе панели отсутствует Docker CLI'
    if not os.path.exists('/var/run/docker.sock'):
        return 'Панели не подключён сокет Docker'
    return None


def status():
    reason = unavailable_reason()
    with _LOCK:
        return {'available': reason is None, 'detail': reason, **_STATE}


def _update(**values):
    with _LOCK:
        _STATE.update(values)


def _compose(root, env):
    # Имя проекта берём у действующей панели, включая развёртывания с compose -p.
    result = subprocess.run(
        _DOCKER + ['inspect', '--format', '{{json .Config.Labels}}', 'stroy-resurs_mvp'],
        check=True, capture_output=True, text=True, timeout=15, env=env)
    labels = json.loads(result.stdout)
    project = labels.get('com.docker.compose.project')
    directory = labels.get('com.docker.compose.project.working_dir')
    if not project or not directory or Path(directory).resolve() != root:
        raise Unavailable('Путь проекта не совпадает с каталогом действующей панели')
    return _DOCKER + ['compose', '--project-directory', str(root),
                      '--project-name', project, '--file', str(root / 'docker-compose.yml')]


def _wait_ready():
    # OpenAPI доступен после завершения FastAPI lifespan (подключения бота к Kafka).
    base = os.environ.get('BOT_URL', 'http://bot:5500/').rstrip('/')
    for attempt in range(30):
        try:
            with urllib.request.urlopen(base + '/openapi.json', timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        if attempt < 29:
            time.sleep(2)
    raise RuntimeError('Контейнер запущен, но бот не ответил на проверку готовности')


def _worker(root):
    path = None
    try:
        path = Path(runs_dir()) / 'bot_rebuild.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.update(DASHBOARD_PROJECT_DIR=str(root), COMPOSE_ANSI='never', BUILDKIT_PROGRESS='plain')
        # Используется один явно указанный compose-файл без окружения стороннего запуска.
        for key in ('COMPOSE_FILE', 'COMPOSE_PROFILES', 'COMPOSE_PROJECT_NAME'):
            env.pop(key, None)
        with path.open('a', encoding='utf-8') as output:
            output.write(f'\n===== Пересборка бота {datetime.now().isoformat()} =====\n')
            output.flush()
            compose = _compose(root, env)
            for phase, args, timeout in (
                ('building', ['build', 'bot'], 3600),
                ('starting', ['up', '-d', '--no-deps', '--no-build', '--force-recreate',
                              '--wait', '--wait-timeout', '60', 'bot'], 180),
            ):
                _update(phase=phase)
                output.write('\n$ ' + ' '.join(compose + args) + '\n')
                output.flush()
                subprocess.run(compose + args, cwd=root, env=env, stdout=output,
                               stderr=subprocess.STDOUT, check=True, timeout=timeout)
            _update(phase='checking')
            _wait_ready()
            output.write('\nБот пересобран, запущен и отвечает на HTTP-запросы.\n')
        _update(pending=False, phase='succeeded', finished_at=datetime.now().isoformat())
        journal.append('bot.restart', details={'phase': 'succeeded'})
    except Exception as exc:
        error = (f'Команда Docker завершилась с кодом {exc.returncode}. '
                 'Подробности — в логе пересборки.'
                 if isinstance(exc, subprocess.CalledProcessError) else str(exc))
        if path is not None:
            try:
                with path.open('a', encoding='utf-8') as output:
                    output.write('\nОшибка: ' + error + '\n')
                    if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
                        output.write(str(exc.stderr)[:8192] + '\n')
            except OSError:
                pass
        _update(pending=False, phase='failed', error=error,
                finished_at=datetime.now().isoformat())
        journal.append('bot.restart', result='error', details={'error': error})


def restart_async():
    reason = unavailable_reason()
    if reason:
        raise Unavailable(reason)
    root = Path(os.environ['DASHBOARD_PROJECT_DIR']).resolve()
    with _LOCK:
        if _STATE['pending']:
            raise AlreadyRunning('Пересборка бота уже выполняется')
        _STATE.update(pending=True, phase='building', requested_at=datetime.now().isoformat(),
                      finished_at=None, error=None)
        journal.append('bot.restart.requested')
        try:
            threading.Thread(target=_worker, args=(root,), name='bot-rebuild', daemon=True).start()
        except Exception as exc:
            _STATE.update(pending=False, phase='failed', error=str(exc),
                          finished_at=datetime.now().isoformat())
            raise
        return {'ok': True, **_STATE}
