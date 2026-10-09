# dashboard/common.py 1.0.0
# Общая база модулей Диспетчерской: доступ к Config, каталоги прогонов, мелкие утилиты.
#
# Правило зависимостей: Диспетчерская обязана подниматься на интерпретаторе БЕЗ
# установленных зависимостей пайплайна (playwright/llama-index/aiohttp/aiokafka).
# Поэтому здесь и во всех модулях dashboard/* — только стандартная библиотека
# (+ fastapi/pydantic в app.py; psycopg2 для ситлиста — лениво внутри site_list_db.py,
# без него /companies отдаёт 503). Config подключается лениво
# и «мягко»: если config.py не импортируется, работаем на переменных окружения.

import json
import logging
import os
import re
import sys

log = logging.getLogger("dashboard")

# config.py лежит в корне mvp (родитель каталога dashboard/); модули пакета
# импортируют друг друга по короткому имени — оба каталога нужны в sys.path
# (дубль bootstrap'а из __init__.py: модуль могут запустить и напрямую).
DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
MVP_DIR = os.path.dirname(DASHBOARD_DIR)
for _p in (MVP_DIR, DASHBOARD_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

STATIC_DIR = os.path.join(DASHBOARD_DIR, 'static')

RUN_ID_RE = re.compile(r'^run_\d{8}_\d{6}$')
# id компании из ситлиста (companies.manufacturer_id)
COMPANY_ID_RE = re.compile(r'^[A-Za-z0-9_.\-]{1,64}$')
# домен профиля сайта = имя файла profiles/<домен>.yaml; punycode (xn--) — те же символы
DOMAIN_RE = re.compile(r'^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?'
                       r'(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$')

_CONFIG_UNSET = object()
_CONFIG = _CONFIG_UNSET


def cfg():
    """Ленивая загрузка Config через load_from_env() — как в main.py.

    ВАЖНО: именно load_from_env(), а не Config(): main.py стартует пайплайн так же,
    и панель обязана видеть ту же конфигурацию, что и подпроцесс.
    """
    global _CONFIG
    if _CONFIG is _CONFIG_UNSET:
        try:
            from config import Config
            _CONFIG = Config.load_from_env()
        except Exception as e:
            log.warning(f"config.py недоступен ({e}) — используются только env-переменные")
            _CONFIG = None
    return _CONFIG


def invalidate_cfg():
    """Сбросить кэш Config: вызывается после сохранения настроек (PUT /settings)."""
    global _CONFIG
    _CONFIG = _CONFIG_UNSET


def cfg_value(attr, env_name, default=None):
    """Значение настройки: env приоритетнее config.py (удобно для локальных тестов)."""
    value = os.environ.get(env_name)
    if value:
        return value
    c = cfg()
    return getattr(c, attr, default) if c is not None else default


def runs_dir():
    """Каталог прогонов: env RUNS_DIR, иначе <logs_dir>/runs."""
    explicit = os.environ.get('RUNS_DIR')
    if explicit:
        return explicit
    logs = cfg_value('logs_dir', 'LOGS_DIR')
    if not logs:
        raise RuntimeError('Не задан каталог прогонов: нужен env RUNS_DIR/LOGS_DIR или рабочий config.py')
    return os.path.join(logs, 'runs')


def logs_dir():
    return os.environ.get('LOGS_DIR') or os.path.dirname(runs_dir())


def read_json(path):
    """JSON или None. Отсутствие/повреждение файла — не ошибка (fail-open по §2)."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def atomic_write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def read_status():
    """Heartbeat последнего/текущего прогона (logs/runs/status.json) или None."""
    return read_json(os.path.join(runs_dir(), 'status.json'))


def list_run_ids():
    """Идентификаторы прогонов, новые сверху."""
    d = runs_dir()
    if not os.path.isdir(d):
        return []
    return sorted((n for n in os.listdir(d) if RUN_ID_RE.match(n)), reverse=True)


def latest_run_id():
    ids = list_run_ids()
    return ids[0] if ids else None


def read_companies_jsonl(run_id):
    """Пер-компанийные записи прогона. Оборванная последняя строка (падение) — пропускается."""
    records = []
    path = os.path.join(runs_dir(), run_id, 'companies.jsonl')
    if os.path.isfile(path):
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records
