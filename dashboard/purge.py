# dashboard/purge.py 1.0.0
# Очистка данных по компании (спецификация «Диспетчерская» §7).
#
# Три независимо выбираемых источника:
#   temp      — рабочие файлы компании во временном хранилище
#               <base_dir>/temp_html_storage/<safe_name>          риск низкий
#   documents — каталог компании <documents_dir>/<id>_<safe_name> риск средний
#   graph     — данные компании в Neo4j через backend                риск высокий
#
# ЧЕГО ОЧИСТКА НЕ ТРОГАЕТ НИКОГДА (callout §7): кумулятивные архивы GraphDB_Arch/
# и GraphDB_JSON/, логи прогонов, файлы метрик и журнал — это аудиторский след.
# Защита не «на честном слове»: _assert_safe_target ниже физически запрещает
# удалять что-либо вне двух разрешённых корней.
#
# Протокол операции (§7): dry-run -> подтверждение вводом id -> выполнение ->
# отчёт -> запись в журнал. Очистка компании, находящейся в работе, отклоняется (§5).
#
# Состояние графа: эндпоинта удаления компании у backend пока нет (открытый вопрос
# 11.1 — физическое удаление всех версий или закрытие валидности). Клиентская
# сторона реализована полностью; при отсутствии эндпоинта источник graph честно
# возвращает supported=false, а dry-run всё равно считает объекты через
# существующий /api/companies/get. Включение — одна константа GRAPH_DELETE_PATH.

import json
import logging
import os
import shutil
import urllib.error
import urllib.request

import companies as companies_mod
from common import cfg_value

log = logging.getLogger("dashboard.purge")

SOURCES = ('temp', 'documents', 'graph')

# Путь эндпоинта удаления на backend относительно graph_db_api_url.
# None — эндпоинта нет (текущее состояние). Появится — поставить '/delete'.
GRAPH_DELETE_PATH = None
GRAPH_TIMEOUT_SECONDS = 60


class PurgeError(RuntimeError):
    """Очистка невозможна: компания в работе, id подтверждения не совпал (HTTP 409)."""


class PurgeValidationError(PurgeError):
    """Некорректный запрос: неизвестный источник, пустой список (HTTP 422)."""


# ------------------------------ безопасность целей ----------------------------

def _roots():
    """Разрешённые корни удаления. Всё остальное — вне зоны действия кнопки."""
    base_dir = cfg_value('base_dir', 'BASE_DIR')
    documents_dir = cfg_value('documents_dir', 'DOCUMENTS_DIR')
    return {
        'temp': os.path.join(base_dir, 'temp_html_storage') if base_dir else None,
        'documents': documents_dir or None,
    }


def _assert_safe_target(path, root):
    """Цель обязана лежать СТРОГО внутри своего корня и не быть самим корнем.

    Отсекает: обход путей (..), пустое safe_name (иначе целью стал бы весь корень),
    симлинки наружу и любые попытки задеть архивы/логи.
    """
    if not root:
        raise PurgeError('Корневой каталог не настроен (нет base_dir/documents_dir)')
    real_root = os.path.realpath(root)
    real_path = os.path.realpath(path)
    if real_path == real_root:
        raise PurgeError(f'Цель совпадает с корневым каталогом — удаление запрещено: {root}')
    if os.path.commonpath([real_root, real_path]) != real_root:
        raise PurgeError(f'Цель вне разрешённого каталога: {path}')
    return real_path


def _dir_stats(path):
    """(файлов, байт) в каталоге. Несуществующий каталог — нули."""
    if not path or not os.path.isdir(path):
        return 0, 0
    files = 0
    size = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            files += 1
            try:
                size += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return files, size


# ------------------------------ цели по компании ------------------------------

def targets(company_id):
    """Каталоги компании в каждом источнике. Не трогает файловую систему."""
    site = companies_mod.find_in_site_list(company_id)
    result = companies_mod.last_result(company_id)
    name = (site or {}).get('name') or (result or {}).get('name')
    if not name:
        raise PurgeError(f'Компания {company_id} не найдена ни в ситлисте, ни в прогонах')
    safe = companies_mod.safe_name(name)
    if not safe:
        raise PurgeError(f'Пустое безопасное имя для компании {company_id}')
    roots = _roots()
    return {
        'name': name,
        'safe_name': safe,
        'temp': (os.path.join(roots['temp'], safe) if roots['temp'] else None),
        'documents': (os.path.join(roots['documents'], f'{company_id}_{safe}')
                      if roots['documents'] else None),
        'roots': roots,
    }


# ------------------------------ граф ------------------------------------------

def _graph_counts(company_id):
    """Сколько объектов компании в графе — через существующий /api/companies/get."""
    summary = companies_mod.graph_summary(company_id)
    if not summary.get('available'):
        return {'available': False, 'detail': summary.get('detail')}
    return {
        'available': True,
        'found': summary.get('found', False),
        'objects': (summary.get('objects', 0)
                    + summary.get('products', 0)
                    + summary.get('distributors', 0)
                    + summary.get('files', 0)),
        'breakdown': {'company': summary.get('objects', 0),
                      'products': summary.get('products', 0),
                      'distributors': summary.get('distributors', 0),
                      'files': summary.get('files', 0)},
        'last_upload': summary.get('last_upload'),
    }


def _graph_delete(company_id):
    """Удаление компании из графа через backend. Пока эндпоинта нет — supported=false."""
    if GRAPH_DELETE_PATH is None:
        return {'supported': False, 'deleted': 0,
                'detail': 'Эндпоинт удаления компании на backend не реализован '
                          '(открытый вопрос 11.1 спецификации: физическое удаление '
                          'всех версий или закрытие валидности). Файловые источники '
                          'очищены, граф не изменён.'}
    api_url = cfg_value('graph_db_api_url', 'GRAPH_DB_API_URL')
    url = api_url.rstrip('/') + GRAPH_DELETE_PATH
    body = json.dumps({'id': company_id}, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(
        url, data=body, method='POST',
        headers={'Content-Type': 'application/json', 'User-Agent': 'dispatcher'})
    try:
        with urllib.request.urlopen(request, timeout=GRAPH_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode('utf-8') or '{}')
        return {'supported': True, 'deleted': payload.get('deleted'), 'response': payload}
    except urllib.error.HTTPError as e:
        raise PurgeError(f'Backend отклонил удаление: HTTP {e.code}')
    except Exception as e:
        raise PurgeError(f'Не удалось обратиться к backend: {type(e).__name__}: {e}')


# ------------------------------ dry-run ---------------------------------------

def dry_run(company_id, sources=SOURCES):
    """Посчитать без удаления: файлов в Temp, файлов в Documents, объектов в графе (§7)."""
    _validate_sources(sources)
    t = targets(company_id)
    report = {'company_id': company_id, 'name': t['name'], 'safe_name': t['safe_name'],
              'sources': {}, 'busy': companies_mod.is_being_processed(company_id)}

    if 'temp' in sources:
        files, size = _dir_stats(t['temp'])
        report['sources']['temp'] = {'path': t['temp'], 'exists': bool(
            t['temp'] and os.path.isdir(t['temp'])), 'files': files, 'bytes': size}
    if 'documents' in sources:
        files, size = _dir_stats(t['documents'])
        report['sources']['documents'] = {'path': t['documents'], 'exists': bool(
            t['documents'] and os.path.isdir(t['documents'])), 'files': files, 'bytes': size}
    if 'graph' in sources:
        report['sources']['graph'] = dict(_graph_counts(company_id),
                                          supported=GRAPH_DELETE_PATH is not None)
    report['protected'] = _protected_note()
    return report


def _protected_note():
    """Что кнопка не трогает никогда — показывается оператору перед подтверждением."""
    return {
        'graph_db_archive_dir': cfg_value('graph_db_archive_dir', 'GRAPH_DB_ARCHIVE_DIR'),
        'graph_db_json_output_dir': cfg_value('graph_db_json_output_dir',
                                              'GRAPH_DB_JSON_OUTPUT_DIR'),
        'logs_dir': cfg_value('logs_dir', 'LOGS_DIR'),
        'detail': 'Кумулятивные архивы, логи прогонов, файлы метрик и журнал операций '
                  'очисткой не затрагиваются — это аудиторский след (§7).',
    }


def _validate_sources(sources):
    unknown = [s for s in (sources or []) if s not in SOURCES]
    if unknown:
        raise PurgeValidationError(f'Неизвестные источники: {", ".join(unknown)}')
    if not sources:
        raise PurgeValidationError('Не выбран ни один источник очистки')


# ------------------------------ выполнение ------------------------------------

def execute(company_id, sources, confirm_company_id, issued_by='ui'):
    """Удаление по выбранным источникам. Возвращает фактические счётчики (§7)."""
    _validate_sources(sources)
    if confirm_company_id != company_id:
        raise PurgeError('Подтверждение не совпадает с id компании — операция отменена')
    if companies_mod.is_being_processed(company_id):
        raise PurgeError('Компания сейчас обрабатывается пайплайном — очистка запрещена (§5)')

    t = targets(company_id)
    before = dry_run(company_id, sources)
    result = {'company_id': company_id, 'name': t['name'], 'sources': {}}

    for source in ('temp', 'documents'):
        if source not in sources:
            continue
        path = t[source]
        planned = before['sources'][source]
        if not path or not os.path.isdir(path):
            result['sources'][source] = {'path': path, 'deleted_files': 0,
                                         'deleted_bytes': 0, 'existed': False}
            continue
        real_path = _assert_safe_target(path, t['roots'][source])
        shutil.rmtree(real_path)
        result['sources'][source] = {
            'path': path, 'existed': True,
            'deleted_files': planned['files'], 'deleted_bytes': planned['bytes'],
            'verified_gone': not os.path.exists(real_path),
        }
        log.info(f"Очистка {source}: удалён каталог {path} "
                 f"({planned['files']} файлов, {planned['bytes']} байт)")

    if 'graph' in sources:
        result['sources']['graph'] = dict(_graph_delete(company_id),
                                          planned=before['sources'].get('graph'))

    result['protected'] = _protected_note()
    return result
