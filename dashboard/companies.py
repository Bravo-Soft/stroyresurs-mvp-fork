# dashboard/companies.py 1.0.0
# Экран «Компания» (спецификация §3, §4 GET /companies/{id}) и постановка
# срочной задачи (§4 POST /companies/{id}/crawl).
#
# Карточка компании собирается из трёх независимых источников, каждый — fail-open:
#   1. ситлист — таблица public.companies в Postgres (site_list_db.py): кто это, какой сайт, какой id;
#   2. последний результат прогона из companies.jsonl (страницы, товары, тайминги);
#   3. сводка графа через backend /api/companies/get (товаров в графе, дата загрузки).
# Плюс факт наличия YAML-профиля сайта и черновика переписи (сам редактор — profiles_store.py).
#
# Запросы к backend — на urllib из стандартной библиотеки: панель обязана
# подниматься на интерпретаторе без aiohttp (см. common.py).

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlparse

from common import cfg_value, list_run_ids, read_companies_jsonl
import site_list_db  # только stdlib; psycopg2 импортируется лениво внутри

log = logging.getLogger("dashboard.companies")

GRAPH_TIMEOUT_SECONDS = 20


# ------------------------------ ситлист ---------------------------------------

class SiteListUnavailable(RuntimeError):
    """Ситлист (Postgres или XLSX fallback) недоступен (наружу — HTTP 503)."""


def _dsn():
    """PostgreSQL DSN или совместимый fallback на текущий Site_list.xlsx."""
    dsn = cfg_value('site_list_dsn', 'SITE_LIST_DSN')
    if dsn:
        return dsn
    excel_path = cfg_value('excel_path', 'EXCEL_PATH')
    return f'xlsx://{excel_path}' if excel_path else None


def read_site_list():
    """Компании из PostgreSQL либо текущего Site_list.xlsx."""
    dsn = _dsn()
    try:
        return {'companies': site_list_db.fetch_all(dsn), 'source': site_list_db.describe_dsn(dsn)}
    except site_list_db.SiteListUnavailable as e:
        raise SiteListUnavailable(str(e))


def find_in_site_list(company_id):
    try:
        return site_list_db.fetch_one(_dsn(), company_id)
    except Exception as e:
        log.warning(f"Ситлист недоступен: {e}")
    return None


def safe_name(original_name):
    """Безопасное имя компании — то же, что использует пайплайн для имён каталогов."""
    try:
        from product_utils import sanitize_company_name
        return sanitize_company_name(original_name)
    except Exception:
        # Копия правил product_utils.sanitize_company_name на случай, когда
        # зависимости пайплайна недоступны (панель обязана работать и так).
        name = (original_name or 'Неизвестная компания')
        name = name.replace('ё', 'е').replace('Ё', 'Е')
        for ch in '°º˚∘̊®™«»<>:"/\\|?*^':
            name = name.replace(ch, '')
        name = name.replace(' ', '_').replace('.', '_').replace(',', '_')
        while '__' in name:
            name = name.replace('__', '_')
        return name.strip('_')


# ------------------------------ последний результат ---------------------------

def last_result(company_id, max_runs=50):
    """Последняя запись о компании из companies.jsonl (по всем прогонам, новые сверху)."""
    for run_id in list_run_ids()[:max_runs]:
        for record in reversed(read_companies_jsonl(run_id)):
            if record.get('company_id') == company_id:
                return dict(record, run_id=run_id)
    return None


def history(company_id, limit=20, max_runs=50):
    """История обработок компании — для карточки."""
    items = []
    for run_id in list_run_ids()[:max_runs]:
        for record in read_companies_jsonl(run_id):
            if record.get('company_id') == company_id:
                stats = record.get('statistics') or {}
                items.append({
                    'run_id': run_id, 'ts': record.get('ts'),
                    'kind': record.get('kind'), 'status': record.get('status'),
                    'duration_seconds': record.get('duration_seconds'),
                    'products': stats.get('products_processed', 0) or 0,
                    'errors': len(record.get('errors') or []),
                })
                if len(items) >= limit:
                    return items
    return items


# ------------------------------ сводка графа ----------------------------------

def _graph_get(payload):
    """POST на backend /api/companies/get. Возвращает (данные, ошибка)."""
    api_url = cfg_value('graph_db_api_url', 'GRAPH_DB_API_URL')
    if not api_url:
        return None, 'GRAPH_DB_API_URL не настроен'
    url = api_url.rstrip('/') + '/get'
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(
        url, data=body, method='POST',
        headers={'Content-Type': 'application/json', 'User-Agent': 'dispatcher'})
    try:
        with urllib.request.urlopen(request, timeout=GRAPH_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode('utf-8')), None
    except urllib.error.HTTPError as e:
        return None, f'HTTP {e.code} от {url}'
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'


def _count_nested(node, key):
    """Число элементов в списке node[key] — форма ответа backend не фиксирована."""
    value = node.get(key) if isinstance(node, dict) else None
    return len(value) if isinstance(value, list) else 0


def graph_summary(company_id):
    """Сводка компании в графе: товаров, дистрибьюторов, дата последней загрузки (§3)."""
    data, error = _graph_get({'vector': {'id': company_id}})
    if error:
        return {'available': False, 'detail': error}
    items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    if not items:
        return {'available': True, 'found': False, 'products': 0, 'distributors': 0,
                'files': 0, 'last_upload': None}
    products = distributors = files = 0
    dates = []
    for item in items:
        if not isinstance(item, dict):
            continue
        products += _count_nested(item, 'products')
        distributors += _count_nested(item, 'suppliers') + _count_nested(item, 'distributors')
        files += _count_nested(item, 'files')
        for key in ('verification_date', 'report_date', 'valid_on_date', 'date'):
            if item.get(key):
                dates.append(str(item[key]))
    return {'available': True, 'found': True, 'objects': len(items),
            'products': products, 'distributors': distributors, 'files': files,
            'last_upload': max(dates) if dates else None}


# ------------------------------ документы на диске ----------------------------

# Четыре группы файлов компании — те же папки, что пайплайн создаёт в
# create_company_directory_structure и собирает в _collect_company_files_for_graphdb.
DOCUMENT_GROUPS = (
    ('documents', 'Documents', 'Документы'),
    ('certificates', 'Certificates', 'Сертификаты'),
    ('instructions', 'Instructions', 'Инструкции'),
    ('price_lists', 'Price_lists', 'Прайс-листы'),
)


def documents_summary(company_id, name):
    """Число файлов компании на диске по группам (<documents_dir>/<id>_<safe_name>/<папка>)."""
    documents_dir = cfg_value('documents_dir', 'DOCUMENTS_DIR')
    safe = safe_name(name) if name else None
    if not documents_dir or not safe:
        return {'available': False, 'path': None, 'groups': [], 'total': 0}
    company_dir = os.path.join(documents_dir, f'{company_id}_{safe}')
    groups = []
    total = 0
    for key, folder, label in DOCUMENT_GROUPS:
        count = 0
        path = os.path.join(company_dir, folder)
        if os.path.isdir(path):
            for _dirpath, _dirnames, filenames in os.walk(path):
                count += len(filenames)
        groups.append({'key': key, 'label': label, 'folder': folder, 'files': count})
        total += count
    return {'available': os.path.isdir(company_dir), 'path': company_dir,
            'groups': groups, 'total': total}


# ------------------------------ профиль сайта ---------------------------------

def profile_status(website):
    """Есть ли YAML-профиль сайта и черновик переписи. Сам профиль — GET /profiles/{domain}."""
    import profiles_store
    domain = profiles_store.domain_for_website(website)
    try:
        profiles_dir = profiles_store.profiles_dir()
    except profiles_store.ProfilesUnavailable:
        profiles_dir = None
    drafts_dir = profiles_store.drafts_dir()
    draft_exists = bool(drafts_dir and domain
                        and os.path.isfile(os.path.join(drafts_dir, f'{domain}.yaml')))
    if not profiles_dir or not os.path.isdir(profiles_dir):
        return {'available': False, 'exists': False, 'dir': profiles_dir, 'domain': domain,
                'draft_exists': draft_exists, 'detail': 'Каталог профилей отсутствует'}
    # Только <домен>.yaml: резолвер пайплайна .yml не читает
    path = os.path.join(profiles_dir, f'{domain}.yaml')
    if domain and os.path.isfile(path):
        return {'available': True, 'exists': True, 'file': f'{domain}.yaml',
                'dir': profiles_dir, 'domain': domain, 'draft_exists': draft_exists,
                'modified_at': datetime.fromtimestamp(os.path.getmtime(path)).isoformat()}
    return {'available': True, 'exists': False, 'dir': profiles_dir, 'domain': domain,
            'draft_exists': draft_exists}


# ------------------------------ карточка --------------------------------------

def card(company_id, with_graph=False):
    """Карточка компании (§4 GET /companies/{id}).

    Сводка графа по умолчанию НЕ запрашивается: backend отвечает на companies/get
    порядка 15 с, и карточка на это время повисала бы целиком. Файловые источники
    отдаются мгновенно, а блок графа UI дотягивает отдельным запросом
    GET /companies/{id}/graph.
    """
    site = find_in_site_list(company_id)
    result = last_result(company_id)
    website = (site or {}).get('website') or (result or {}).get('website')
    name = (site or {}).get('name') or (result or {}).get('name')
    if site is None and result is None:
        return None

    stats = (result or {}).get('statistics') or {}
    return {
        'company_id': company_id,
        'name': name,
        'website': website,
        'safe_name': safe_name(name) if name else None,
        'in_site_list': site is not None,
        'last_result': None if result is None else {
            'run_id': result.get('run_id'),
            'ts': result.get('ts'),
            'kind': result.get('kind'),
            'status': result.get('status'),
            'duration_seconds': result.get('duration_seconds'),
            'pages': {
                'product': stats.get('product_pages_processed', 0) or 0,
                'company': stats.get('company_pages_processed', 0) or 0,
                'distributor': stats.get('distributor_pages_processed', 0) or 0,
            },
            'products': stats.get('products_processed', 0) or 0,
            'cards_generated': stats.get('cards_generated', 0) or 0,
            'files_downloaded': stats.get('files_downloaded', 0) or 0,
            'distributors': stats.get('distributors_processed', 0) or 0,
            'graph_db_uploaded': stats.get('graph_db_uploaded'),
            'modules': result.get('modules') or {},
            'errors': result.get('errors') or [],
        },
        'graph': (graph_summary(company_id) if with_graph else
                  {'available': None, 'pending': True,
                   'detail': 'Сводка графа запрашивается отдельно: '
                             f'GET /companies/{company_id}/graph'}),
        'documents': documents_summary(company_id, name),
        'profile': profile_status(website) if website else {'available': False,
                                                            'exists': False},
        'history': history(company_id),
    }


# ------------------------------ срочная задача --------------------------------

class KafkaUnavailable(RuntimeError):
    """Kafka недоступна или клиент не установлен (наружу — HTTP 503)."""


async def send_urgent_task(company, issued_by='ui'):
    """Публикация задачи в Kafka urgent_tasks — тот же путь, что у бота (§4).

    Клиент Kafka (aiokafka) — зависимость пайплайна, а не панели: если его нет
    в окружении Диспетчерской, честно отдаём 503 вместо тихой потери задачи.
    """
    try:
        from kafka_manager import KafkaTaskManager
    except ImportError as e:
        raise KafkaUnavailable(
            f'Клиент Kafka недоступен в окружении Диспетчерской ({e}). '
            f'Поставьте aiokafka или запускайте компанию через выбор подмножества '
            f'на экране «Прогон».')
    from common import cfg
    config = cfg()
    if config is None:
        raise KafkaUnavailable('config.py недоступен — адрес брокера неизвестен')
    manager = KafkaTaskManager(config)
    try:
        await manager.initialize(need_urgent_consumer=False, need_status_consumer=False)
        task_id = await manager.send_urgent_task(company, user_id=None)
    except Exception as e:
        raise KafkaUnavailable(f'Не удалось отправить задачу в Kafka: {e}')
    finally:
        try:
            await manager.close()
        except Exception:
            pass
    log.info(f"Срочная задача поставлена ({issued_by}): {company.get('company_id')} -> {task_id}")
    return task_id


def company_payload(company_id):
    """Данные компании в форме, которую ждёт пайплайн (как read_company_list)."""
    site = find_in_site_list(company_id)
    if site is None:
        return None
    return {
        'company_id': site['company_id'],
        'original_name': site['name'],
        'safe_name': safe_name(site['name']),
        'folder_name': safe_name(site['name']),
        'website': site['website'],
    }


def is_being_processed(company_id):
    """Обрабатывается ли компания прямо сейчас (инвариант §5 для очистки)."""
    from common import read_status
    status = read_status() or {}
    if status.get('state') not in ('processing_regular', 'processing_urgent'):
        return False
    current = status.get('current_company') or {}
    return current.get('company_id') == company_id
