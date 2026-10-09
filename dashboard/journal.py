# dashboard/journal.py 1.0.0
# Журнал операций оператора (спецификация «Диспетчерская» §9).
#
# Append-only JSONL: logs/runs/journal.jsonl. Строка на действие — старт/стоп/
# перезапуск, краул по id, изменение настроек (дифф «было → стало»), очистка со
# счётчиками. Журнал НЕ подлежит очистке из UI: эндпоинта удаления нет намеренно.
#
# Секреты в журнал не попадают: settings_store передаёт для секретных параметров
# только факт «задано/не задано» (см. settings_store.diff_for_journal).
#
# Только стандартная библиотека.

import json
import logging
import os
from datetime import datetime

from common import runs_dir

log = logging.getLogger("dashboard.journal")

JOURNAL_NAME = 'journal.jsonl'


def _path():
    return os.path.join(runs_dir(), JOURNAL_NAME)


def append(action, source='ui', company_id=None, details=None, result='ok'):
    """Записать действие. Никогда не бросает: журнал не имеет права ломать операцию."""
    entry = {
        'ts': datetime.now().isoformat(),
        'action': action,
        'source': source,
        'company_id': company_id,
        'result': result,
        'details': details or {},
    }
    try:
        path = _path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except Exception as e:
        log.warning(f"Не удалось записать в журнал операций: {e}")
    return entry


def read(limit=200, action=None, company_id=None):
    """Последние записи журнала, новые сверху."""
    path = _path()
    if not os.path.isfile(path):
        return []
    entries = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        log.warning(f"Не удалось прочитать журнал операций: {e}")
        return []
    if action:
        entries = [e for e in entries if e.get('action') == action]
    if company_id:
        entries = [e for e in entries if e.get('company_id') == company_id]
    entries.reverse()
    return entries[:max(1, min(limit, 2000))]
