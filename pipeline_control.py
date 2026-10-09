"""Файловый канал команд между Диспетчерской и пайплайном.

Команда хранится в ``<logs_dir>/runs/control.json``.  Это намеренно простой
и атомарный контракт: панель и дочерний процесс могут быть разными контейнерами
или перезапускаться независимо, при этом для мягкой остановки не нужен сокет.
"""

import json
import os
from datetime import datetime


COMMAND_FILE = 'control.json'
_KNOWN_COMMANDS = {'stop'}


def _runs_dir(logs_dir):
    if not logs_dir:
        raise ValueError('Не задан logs_dir для команды управления')
    return os.path.join(logs_dir, 'runs')


def command_path(logs_dir):
    return os.path.join(_runs_dir(logs_dir), COMMAND_FILE)


def write_command(logs_dir, command, issued_by='unknown'):
    """Атомарно записать команду. Возвращает её тело для журнала/тестов."""
    if command not in _KNOWN_COMMANDS:
        raise ValueError(f'Неизвестная команда управления: {command}')
    path = command_path(logs_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        'command': command,
        'issued_by': str(issued_by or 'unknown'),
        'issued_at': datetime.now().isoformat(timespec='seconds'),
    }
    temporary = path + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(temporary, path)
    return payload


def read_command(logs_dir):
    """Прочитать валидную команду; повреждённый/чужой файл безопасно игнорируется."""
    try:
        with open(command_path(logs_dir), 'r', encoding='utf-8') as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get('command') not in _KNOWN_COMMANDS:
        return None
    return payload


def clear_command(logs_dir):
    """Убрать команду прошлого прогона. Отсутствие файла — штатная ситуация."""
    try:
        os.remove(command_path(logs_dir))
        return True
    except FileNotFoundError:
        return False


def stop_requested(logs_dir):
    return bool((read_command(logs_dir) or {}).get('command') == 'stop')


def company_ids_from_env(environ=None):
    """Список id из COMPANY_IDS или ``None``, если подмножество не задано."""
    environ = os.environ if environ is None else environ
    raw = environ.get('COMPANY_IDS')
    if raw is None:
        return None
    return [item.strip() for item in str(raw).split(',') if item.strip()]


def ignore_checkpoint(environ=None):
    environ = os.environ if environ is None else environ
    return str(environ.get('PIPELINE_IGNORE_CHECKPOINT', '')).lower() in {
        '1', 'true', 'yes', 'on'
    }
