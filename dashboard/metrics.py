# dashboard/metrics.py 1.0.0
# Метрики Диспетчерской (спецификация §8, эндпоинты §4 /metrics/*).
#
# Принцип §8: НИКАКОЙ новой телеметрии и новой БД — только чтение того, что
# пайплайн уже производит, и агрегация:
#   run.json / companies.jsonl (run_recorder)  -> прогресс, нули, товары, страницы,
#                                                 медианы времени, тайминги модулей;
#   logs/sgr_error_*.json                      -> сбои разбора ответа LLM;
#   GraphDB_Pending/*.json                     -> очередь ретраев выгрузки в граф;
#   Base/profile_metrics/<domain>/*.json       -> алерты профилей (блок опциональный:
#                                                 в этой версии пайплайна каталога нет,
#                                                 блок честно отдаёт available=false).
#
# Fail-open: отсутствие любого источника — не ошибка, блок отдаёт «нет данных» (§2).
#
# Только стандартная библиотека.

import glob
import os
import re
import statistics
import threading
from datetime import datetime, timedelta

from common import (cfg_value, latest_run_id, list_run_ids, logs_dir,
                    read_companies_jsonl, read_json, runs_dir)

# Порядок модулей — как в processing_time_tracker.MODULE_ORDER / run_recorder._MODULES
MODULE_LABELS = {
    'crawler': 'Краулер', 'text': 'Извлечение текста', 'ai': 'ИИ-извлечение',
    'cards': 'Карточки', 'files': 'Файлы', 'pdf': 'Конвертация PDF',
    'graph': 'Загрузка в граф',
}


def _run_json(run_id):
    return read_json(os.path.join(runs_dir(), run_id, 'run.json'))


def _products(stats):
    """Товаров у компании: сколько реально обработано."""
    return (stats or {}).get('products_processed', 0) or 0


def _is_zero_company(record):
    """«Нулевая» компания: завершилась без единого товара."""
    return _products(record.get('statistics')) == 0


def summary(run_id=None):
    """Сводка последнего (или указанного) прогона — блок «Обзор» (§4 /metrics/summary)."""
    run_id = run_id or latest_run_id()
    if not run_id:
        return {'available': False, 'run_id': None,
                'detail': 'Прогонов ещё не было: каталог logs/runs пуст'}

    run = _run_json(run_id) or {}
    records = read_companies_jsonl(run_id)
    done = len(records)
    zero = sum(1 for r in records if _is_zero_company(r))
    failed = sum(1 for r in records if r.get('status') != 'success')
    durations = [r['duration_seconds'] for r in records
                 if isinstance(r.get('duration_seconds'), (int, float))]

    perf = run.get('perf') or {}
    modules = perf.get('modules') or {}

    return {
        'available': True,
        'run_id': run_id,
        'state': run.get('state'),
        'started_at': run.get('started_at'),
        'finished_at': run.get('finished_at'),
        'companies': run.get('companies') or {'total': None, 'done': done,
                                              'success': done - failed, 'failed': failed},
        'zero_companies': {
            'count': zero,
            'percent': round(zero * 100.0 / done, 1) if done else None,
            'names': [r.get('name') for r in records if _is_zero_company(r)][:50],
        },
        'pages': run.get('pages') or {},
        'products_total': sum(_products(r.get('statistics')) for r in records),
        'tokens': run.get('tokens') or {},
        'cost': run.get('cost') or {},
        'median_company_seconds': (perf.get('median_company_seconds')
                                   if perf.get('median_company_seconds') is not None
                                   else (round(statistics.median(durations), 1)
                                         if durations else None)),
        'page_pipeline_seconds_estimate': perf.get('page_pipeline_seconds_estimate') or {},
        'ai_module': perf.get('ai_module'),
        'modules': [{'module': m, 'label': MODULE_LABELS.get(m, m), **(modules.get(m) or {})}
                    for m in MODULE_LABELS if m in modules],
        'errors_last_24h': errors_last_24h(),
        'indicators': indicators(),
        'profile_alerts': profile_alerts(),
    }


_MODE_LINE = re.compile(
    (r'(?m)^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ - main - INFO - '
     r'Режим (ОБНОВЛЕНИЯ|ПЕРВИЧНОЙ ЗАГРУЗКИ) для компании: ([^\r\n]+)\r?\n').encode('utf-8'))
_MODE_CACHE = {'identity': None, 'offset': 0, 'events': {}}
_MODE_LOCK = threading.Lock()


def _processing_modes(records, run):
    """Старые JSONL не содержат режима: восстанавливаем его по журналу этого прогона."""
    modes = []
    for record in records:
        mode = record.get('processing_mode')
        update = (record.get('statistics') or {}).get('update_mode')
        modes.append(mode if mode in ('initial', 'update') else
                     'update' if update is True else 'initial' if update is False else None)
    start = (run.get('started_at') or '')[:19]
    if not start or all(mode is not None for mode in modes):
        return modes
    path = os.path.join(logs_dir(), 'monitoring.log')
    with _MODE_LOCK:
        try:
            with open(path, 'rb') as source:
                info = os.fstat(source.fileno())
                identity = (path, info.st_dev, info.st_ino)
                if identity != _MODE_CACHE['identity'] or info.st_size < _MODE_CACHE['offset']:
                    _MODE_CACHE.update(identity=identity, offset=0, events={})
                source.seek(_MODE_CACHE['offset'])
                remainder = b''
                # Один проход по старому журналу; дальше читается только дописанный хвост.
                while source.tell() < info.st_size:
                    data = source.read(min(1048576, info.st_size - source.tell()))
                    if not data:
                        break
                    chunk = remainder + data
                    end = chunk.rfind(b'\n') + 1
                    for match in _MODE_LINE.finditer(chunk, 0, end):
                        stamp, mode, name = [item.decode('utf-8') for item in match.groups()]
                        event = (stamp.replace(' ', 'T'),
                                 'update' if mode == 'ОБНОВЛЕНИЯ' else 'initial')
                        _MODE_CACHE['events'].setdefault(name, []).append(event)
                    remainder = chunk[end:]
                _MODE_CACHE['offset'] = source.tell() - len(remainder)
        except OSError:
            return modes
        for index, record in enumerate(records):
            finish = (record.get('ts') or '')[:19]
            if modes[index] is None and finish:
                events = [event for event in _MODE_CACHE['events'].get(record.get('name'), [])
                          if start <= event[0] <= finish]
                if events:
                    modes[index] = max(events, key=lambda event: event[0])[1]
    return modes


def companies(run_id=None):
    """Метрики по компаниям прогона (§4 /metrics/companies)."""
    run_id = run_id or latest_run_id()
    if not run_id:
        return {'run_id': None, 'companies': []}
    rows = []
    records = read_companies_jsonl(run_id)
    modes = _processing_modes(records, _run_json(run_id) or {})
    for r, processing_mode in zip(records, modes):
        stats = r.get('statistics') or {}
        rows.append({
            'company_id': r.get('company_id'),
            'name': r.get('name'),
            'website': r.get('website'),
            'kind': r.get('kind'),
            'processing_mode': processing_mode,
            'status': r.get('status'),
            'zero': _is_zero_company(r),
            'duration_seconds': r.get('duration_seconds'),
            'products': _products(stats),
            'products_found': stats.get('products_found', 0) or 0,
            'cards_generated': stats.get('cards_generated', 0) or 0,
            'distributors': stats.get('distributors_processed', 0) or 0,
            'files_downloaded': stats.get('files_downloaded', 0) or 0,
            'pages': {
                'product': stats.get('product_pages_processed', 0) or 0,
                'company': stats.get('company_pages_processed', 0) or 0,
                'distributor': stats.get('distributor_pages_processed', 0) or 0,
            },
            'graph_db_uploaded': stats.get('graph_db_uploaded'),
            'modules': r.get('modules') or {},
            'errors': r.get('errors') or [],
            'ts': r.get('ts'),
        })
    return {'run_id': run_id, 'companies': rows}


def errors_last_24h():
    """Ошибки за сутки по всем прогонам (§3 «Обзор»)."""
    cutoff = datetime.now() - timedelta(hours=24)
    total = 0
    companies_failed = 0
    for run_id in list_run_ids():
        for r in read_companies_jsonl(run_id):
            ts = r.get('ts')
            try:
                if not ts or datetime.fromisoformat(ts) < cutoff:
                    continue
            except (TypeError, ValueError):
                continue
            errors = r.get('errors') or []
            total += len(errors)
            if r.get('status') != 'success':
                companies_failed += 1
    return {'errors': total, 'companies_failed': companies_failed}


def indicators():
    """Системные индикаторы §8: очередь ретраев в граф, дампы ошибок LLM, недоступные сайты."""
    pending_dir = cfg_value('graph_db_pending_dir', 'GRAPH_DB_PENDING_DIR')
    reports_dir = cfg_value('reports_dir', 'REPORTS_DIR')
    unavailable = (read_json(os.path.join(reports_dir, 'unavailable_sites.json'))
                   if reports_dir else None)
    return {
        'graph_db_pending': (len(glob.glob(os.path.join(pending_dir, '*.json')))
                             if pending_dir and os.path.isdir(pending_dir) else None),
        'sgr_error_dumps': len(glob.glob(os.path.join(logs_dir(), 'sgr_error_*.json'))),
        'unavailable_sites': len(unavailable) if isinstance(unavailable, list) else None,
    }


def profile_alerts(limit=20):
    """Алерты профилей против baseline: <profile_metrics_dir>/<domain>/*.json (§8).

    Каталог — config.profile_metrics_dir (по умолчанию Base/profile_metrics); его пишет
    RunMetricsCollector (main._finalize_profiling). Пока каталога нет — available=false.
    """
    root = cfg_value('profile_metrics_dir', 'PROFILE_METRICS_DIR')
    if not root:
        base_dir = cfg_value('base_dir', 'BASE_DIR')
        root = os.path.join(base_dir, 'profile_metrics') if base_dir else None
    if not root or not os.path.isdir(root):
        return {'available': False, 'path': root,
                'detail': 'Каталог метрик профилей отсутствует — блок появится, '
                          'когда профилирование начнёт их писать'}
    alerts = []
    for path in sorted(glob.glob(os.path.join(root, '*', '*.json')), reverse=True):
        data = read_json(path)
        if not isinstance(data, dict):
            continue
        found = data.get('alerts') or data.get('warnings')
        if found:
            alerts.append({'domain': os.path.basename(os.path.dirname(path)),
                           'file': os.path.basename(path), 'alerts': found})
        if len(alerts) >= limit:
            break
    return {'available': True, 'path': root, 'alerts': alerts}


# ------------------------------ ошибки по блокам ------------------------------

# Классификация текста ошибки по модулям пайплайна: первый совпавший — выигрывает.
# Порядок — от специфичного к общему.
_ERROR_PATTERNS = [
    ('pdf', ('pdf', 'конверт', 'libreoffice', 'soffice')),
    ('graph', ('граф', 'graph')),
    ('files', ('файл', 'скачив', 'download')),
    ('cards', ('карточ', 'docx')),
    ('ai', ('sgr', 'llm', 'ии', 'токен', 'aitunnel', 'ollama', 'litellm', 'модел')),
    ('text', ('markdown', 'текст')),
    ('crawler', ('краул', 'crawl', 'страниц', 'браузер', 'playwright', 'сайт',
                 'таймаут', 'timeout', 'завис')),   # 'завис' — компания брошена сторожем D101
]

_MODULE_LABELS_FULL = dict(MODULE_LABELS, other='Прочее')


def classify_error(message):
    lowered = str(message).lower()
    for module, keywords in _ERROR_PATTERNS:
        if any(k in lowered for k in keywords):
            return module
    return 'other'


def errors_by_module(run_id=None):
    """Сводка ошибок прогона по блокам пайплайна + время в ретраях по модулю."""
    run_id = run_id or latest_run_id()
    if not run_id:
        return {'run_id': None, 'modules': {}, 'companies_failed': 0, 'total_errors': 0}
    modules = {m: {'label': label, 'errors': 0, 'samples': [], 'retry_seconds': 0.0}
               for m, label in _MODULE_LABELS_FULL.items()}
    companies_failed = 0
    total_errors = 0
    for record in read_companies_jsonl(run_id):
        if record.get('status') != 'success':
            companies_failed += 1
        for message in record.get('errors') or []:
            entry = modules[classify_error(message)]
            entry['errors'] += 1
            total_errors += 1
            if len(entry['samples']) < 3:
                entry['samples'].append(f"{record.get('name')}: {str(message)[:200]}")
        for module, timings in (record.get('modules') or {}).items():
            if module in modules:
                modules[module]['retry_seconds'] = round(
                    modules[module]['retry_seconds'] + (timings.get('retry') or 0.0), 1)
    return {'run_id': run_id, 'modules': modules,
            'companies_failed': companies_failed, 'total_errors': total_errors}


def _all_logs_tail(lines):
    """События двух процессов; строки трассировки остаются рядом со своей записью."""
    records, sources, details = [], [], []
    for source, label in (('bot', 'Бот'), ('monitoring', 'Пайплайн')):
        try:
            result = log_tail(lines=1000, source=source)
        except OSError as exc:
            result = {'lines': [], 'modified_at': None, 'detail': str(exc)}
        sources.append({'source': source, **{k: v for k, v in result.items() if k != 'lines'}})
        if result.get('detail'):
            details.append(label + ': ' + result['detail'])
        current = None
        for line in result['lines']:
            match = re.match(r'^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d+)?', line)
            if match or current is None:
                stamp = match.group().replace('T', ' ').replace(',', '.') if match else ''
                current = (stamp, [f'[{label}] {line}'])
                records.append(current)
            else:
                current[1].append(line)
    records.sort(key=lambda item: item[0])
    modified = [s['modified_at'] for s in sources if s.get('modified_at')]
    return {'path': None, 'sources': sources, 'detail': '; '.join(details),
            'modified_at': max(modified) if modified else None,
            'lines': [line for _, event in records for line in event][-lines:]}


def log_tail(lines=200, source='monitoring'):
    """Хвост отдельного лога или общий журнал модулей бота и пайплайна."""
    lines = max(1, min(int(lines), 1000))
    if source == 'all':
        return _all_logs_tail(lines)
    if source == 'stdout':
        path = os.path.join(runs_dir(), 'pipeline_stdout.log')
    elif source == 'bot':
        path = os.path.join(logs_dir(), 'pachka_bot.log')
    elif source == 'bot-rebuild':
        path = os.path.join(runs_dir(), 'bot_rebuild.log')
    else:
        path = os.path.join(logs_dir(), 'monitoring.log')
    if not os.path.isfile(path):
        return {'path': path, 'lines': [], 'detail': 'файл ещё не создан', 'modified_at': None}
    with open(path, 'rb') as f:
        modified_at = datetime.fromtimestamp(os.fstat(f.fileno()).st_mtime).isoformat()
        f.seek(0, os.SEEK_END)
        size = f.tell()
        offset = max(0, size - 262144)  # ограничиваем чтение последних 256 КБ
        f.seek(offset)
        if offset:
            f.readline()  # отбрасываем начало неполной строки, в том числе UTF-8
        tail = f.read().decode('utf-8', errors='replace')
    return {'path': path, 'lines': tail.splitlines()[-lines:], 'modified_at': modified_at}


def runs(limit=100):
    """Сводки прогонов, новые сверху (§4 /runs)."""
    items = []
    for run_id in list_run_ids()[:limit]:
        s = _run_json(run_id)
        if s:
            items.append(s)
    return items


def run_detail(run_id):
    """Сводка прогона + пер-компанийные записи (§4 /runs/{run_id})."""
    s = _run_json(run_id)
    if s is None:
        return None
    return {'run': s, 'companies': read_companies_jsonl(run_id)}


def reports_for_run(run_id):
    """Файлы отчётов, доступные для прогона (экран «Журнал» — ссылки на XLSX)."""
    reports_dir = cfg_value('reports_dir', 'REPORTS_DIR')
    if not reports_dir or not os.path.isdir(reports_dir):
        return []
    run = _run_json(run_id) or {}
    started, finished = run.get('started_at'), run.get('finished_at')
    try:
        lo = datetime.fromisoformat(started) if started else None
    except (TypeError, ValueError):
        lo = None
    try:
        hi = datetime.fromisoformat(finished) if finished else datetime.now()
    except (TypeError, ValueError):
        hi = datetime.now()
    files = []
    for path in glob.glob(os.path.join(reports_dir, '*')):
        if not os.path.isfile(path):
            continue
        mtime = datetime.fromtimestamp(os.path.getmtime(path))
        if lo and not (lo <= mtime <= hi + timedelta(minutes=10)):
            continue
        files.append({'name': os.path.basename(path),
                      'size_bytes': os.path.getsize(path),
                      'modified_at': mtime.isoformat()})
    return sorted(files, key=lambda f: f['modified_at'], reverse=True)[:50]
