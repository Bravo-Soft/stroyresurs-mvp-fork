# dashboard/app.py 3.0.0
# «Диспетчерская» — операторская консоль системы мониторинга Стройресурс.
# Реализация спецификации «Диспетчерская: панель управления и дашборд» в. 1.1.
#
# Контракт (§2, §4, §9):
#   * одно FastAPI-приложение, порт :5510;
#   * все эндпоинты под /api/v1, авторизация — статический токен:
#     Authorization: Bearer <env DASHBOARD_TOKEN>;
#   * API-first: UI (static/index.html) только вызывает эти же эндпоинты,
#     поэтому всё автоматизируемо и тестируемо без браузера;
#   * fail-open чтения: недоступный backend или отсутствующий файл метрик
#     не роняет страницу — блок показывает «нет данных»;
#   * журнал операций (§9) — append-only, пишется на каждое действие.
#
# Запуск (из каталога mvp):
#   python -m uvicorn dashboard.app:app --host 0.0.0.0 --port 5510
# Локальный запуск без Docker и подробности — dashboard/README.md.

import asyncio
import logging
import os
import re
import secrets
import urllib.error
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import companies as companies_mod
import journal
import metrics
import profiles_store
import purge as purge_mod
import settings_store
import supervisor
from common import COMPANY_ID_RE, RUN_ID_RE, STATIC_DIR, cfg_value, read_json

log = logging.getLogger("dashboard")

app = FastAPI(title='Стройресурс — Диспетчерская', version='1.1')

API_PREFIX = '/api/v1'


# ============================== авторизация ==================================

def require_token(authorization: str = Header(default='', alias='Authorization')):
    """Статический токен в заголовке (§9). Токен — в env, не в коде."""
    expected = os.environ.get('DASHBOARD_TOKEN', '')
    if not expected:
        raise HTTPException(
            status_code=503,
            detail='DASHBOARD_TOKEN не задан в окружении — Диспетчерская отключена')
    scheme, _, value = authorization.partition(' ')
    if scheme.lower() != 'bearer' or not secrets.compare_digest(value.strip(), expected):
        raise HTTPException(status_code=401, detail='Неверный или отсутствующий токен')


api = APIRouter(prefix=API_PREFIX, dependencies=[Depends(require_token)])


@app.get('/')
async def index():
    """Страница консоли. Токен вводится в форме входа и хранится в браузере."""
    return FileResponse(os.path.join(STATIC_DIR, 'index.html'))


# ============================== модели запросов ==============================

class StartRequest(BaseModel):
    mode: str = 'full'                      # full | urgent-only
    resume: bool = True
    company_ids: Optional[List[str]] = None


class StopRequest(BaseModel):
    force: bool = False


class RestartRequest(BaseModel):
    mode: str = 'full'
    company_ids: Optional[List[str]] = None


class SettingsRequest(BaseModel):
    values: Dict[str, str]


class PurgeRequest(BaseModel):
    sources: List[str] = ['temp', 'documents']
    confirm_company_id: Optional[str] = None


class ProfileSaveRequest(BaseModel):
    text: str
    expected_version: Optional[str] = None   # версия, которую видел редактор; None = новый файл


class ProfileTextRequest(BaseModel):
    text: str


class ProfileDeleteRequest(BaseModel):
    confirm_domain: str


class ProfileAcceptRequest(BaseModel):
    expected_version: Optional[str] = None


def _valid_run_id(run_id):
    if not RUN_ID_RE.match(run_id):
        raise HTTPException(status_code=400, detail='Некорректный run_id')
    return run_id


def _valid_company_id(company_id):
    if not COMPANY_ID_RE.match(company_id):
        raise HTTPException(status_code=400, detail='Некорректный id компании')
    return company_id


def _valid_domain(domain):
    """400 на кривой домен или служебное имя _*.yaml — до любых обращений к диску."""
    try:
        return profiles_store.valid_domain(domain)
    except profiles_store.ProfileError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _profile_http(e):
    """Доменные исключения профилей -> коды §9 (тот же принцип, что у очистки)."""
    if isinstance(e, profiles_store.ProfileValidationError):
        return HTTPException(status_code=422, detail={'detail': str(e), 'errors': e.errors})
    if isinstance(e, profiles_store.ProfilesUnavailable):
        return HTTPException(status_code=503, detail=str(e))
    return HTTPException(status_code=409, detail=str(e))


# ============================== наблюдение ===================================

@api.get('/status')
async def status():
    """Состояние пайплайна: running/stopped/stalled, PID, текущая компания, прогресс, heartbeat."""
    return supervisor.pipeline_state()


@api.get('/runs')
async def runs(limit: int = 100):
    return {'runs': metrics.runs(limit=max(1, min(limit, 500)))}


@api.get('/runs/{run_id}')
async def run_detail(run_id: str):
    detail = metrics.run_detail(_valid_run_id(run_id))
    if detail is None:
        raise HTTPException(status_code=404, detail='Прогон не найден')
    detail['reports'] = metrics.reports_for_run(run_id)
    return detail


@api.get('/logs/tail')
async def logs_tail(lines: int = 200, source: str = 'monitoring'):
    return metrics.log_tail(lines=lines, source=source)


@api.get('/metrics/summary')
async def metrics_summary(run_id: Optional[str] = None):
    if run_id:
        _valid_run_id(run_id)
    return metrics.summary(run_id)


@api.get('/metrics/companies')
async def metrics_companies(run_id: Optional[str] = None):
    if run_id:
        _valid_run_id(run_id)
    return metrics.companies(run_id)


@api.get('/errors')
async def errors(run_id: Optional[str] = None):
    if run_id:
        _valid_run_id(run_id)
    return metrics.errors_by_module(run_id)


@api.get('/checkpoint')
async def checkpoint():
    return supervisor.checkpoint_info()


@api.get('/journal')
async def journal_read(limit: int = 200, action: Optional[str] = None,
                       company_id: Optional[str] = None):
    return {'entries': journal.read(limit=limit, action=action, company_id=company_id)}


@api.get('/companies')
async def companies_list():
    """Ситлист — для выбора подмножества при запуске и для экрана «Компания»."""
    try:
        return await asyncio.to_thread(companies_mod.read_site_list)
    except companies_mod.SiteListUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


@api.get('/companies/{company_id}')
async def company_card(company_id: str, with_graph: bool = False):
    """Карточка компании: последний результат + наличие профиля (§3).

    Сводка графа по умолчанию не включается — она идёт отдельным запросом
    /companies/{id}/graph, чтобы медленный backend (~15 с) не задерживал экран.
    """
    _valid_company_id(company_id)
    card = await asyncio.to_thread(companies_mod.card, company_id, with_graph)
    if card is None:
        raise HTTPException(status_code=404,
                            detail=f'Компания {company_id} не найдена ни в ситлисте, ни в прогонах')
    card['busy'] = companies_mod.is_being_processed(company_id)
    return card


@api.get('/companies/{company_id}/graph')
async def company_graph(company_id: str):
    """Сводка компании в графе: товаров, дистрибьюторов, дата последней загрузки (§3).

    Отдельный эндпоинт, потому что backend отвечает медленно; недоступность backend —
    не ошибка, а available=false в теле ответа (fail-open §2).
    """
    _valid_company_id(company_id)
    return await asyncio.to_thread(companies_mod.graph_summary, company_id)


# ------------------------------ здоровье --------------------------------------

def _http_check(url, timeout=3):
    """Жив ли HTTP-сервис: любой HTTP-ответ (в т.ч. 404) = процесс отвечает."""
    request = urllib.request.Request(url, headers={'User-Agent': 'dispatcher-health'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, f'HTTP {response.status}'
    except urllib.error.HTTPError as e:
        return True, f'HTTP {e.code}'
    except Exception as e:
        return False, str(e)


async def _tcp_check(hostport, timeout=3):
    try:
        host, _, port = hostport.partition(':')
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port or 80)), timeout)
        writer.close()
        return True, 'TCP-соединение установлено'
    except Exception as e:
        return False, str(e)


def _base_url(url):
    m = re.match(r'^(https?://[^/]+)', url or '')
    return m.group(1) if m else url


def _llm_url():
    """URL активного LLM-шлюза: LiteLLM из окружения, иначе Ollama (env или дефолт config.py).
    AI Tunnel больше не используется — на него не откатываемся."""
    value = os.environ.get('LITELLM_URL')
    if value:
        return value, 'LITELLM_URL'
    return cfg_value('ollama_url', 'OLLAMA_URL'), 'OLLAMA_URL'


@api.get('/health')
async def health():
    """Светофор внешних зависимостей + системные индикаторы (§8)."""
    graph_db_url = _base_url(cfg_value('graph_db_api_url', 'GRAPH_DB_API_URL'))
    bot_url = os.environ.get('BOT_URL', 'http://127.0.0.1:5500/')
    llm_url, llm_source = _llm_url()
    kafka_hostport = cfg_value('kafka_bootstrap_servers', 'KAFKA_BOOTSTRAP_SERVERS')

    graph_task = asyncio.to_thread(_http_check, graph_db_url) if graph_db_url else None
    bot_task = asyncio.to_thread(_http_check, bot_url)
    llm_task = asyncio.to_thread(_http_check, _base_url(llm_url)) if llm_url else None
    kafka_task = _tcp_check(kafka_hostport) if kafka_hostport else None

    async def resolve(task, target):
        if task is None:
            return {'ok': None, 'detail': 'адрес не настроен', 'target': target}
        ok, detail = await task
        return {'ok': ok, 'detail': detail, 'target': target}

    services = {
        'graph_db': await resolve(graph_task, graph_db_url),
        'bot': await resolve(bot_task, bot_url),
        'kafka': await resolve(kafka_task, kafka_hostport),
        'llm': await resolve(llm_task, f'{llm_url} ({llm_source})' if llm_url else None),
    }
    return {'services': services, 'indicators': metrics.indicators(),
            'checked_at': datetime.now().isoformat()}


# ============================== управление ===================================

@api.post('/pipeline/start')
async def pipeline_start(body: StartRequest):
    """Старт прогона. 409, если экземпляр уже работает (§4)."""
    try:
        return await asyncio.to_thread(
            supervisor.start, body.mode, body.resume, body.company_ids, 'ui')
    except supervisor.AlreadyRunning as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@api.post('/pipeline/stop')
async def pipeline_stop(body: StopRequest):
    """Мягкая остановка (по умолчанию) или жёсткая при force=true (§5)."""
    try:
        return await asyncio.to_thread(supervisor.stop, body.force, 'ui')
    except supervisor.AlreadyStopped as e:
        raise HTTPException(status_code=409, detail=str(e))


@api.post('/pipeline/restart')
async def pipeline_restart(body: RestartRequest):
    """stop + start с resume=true — продолжение с чекпоинта, не с 1-й строки листа (§4).

    Мягкая остановка дозавершает текущую компанию (это могут быть десятки минут),
    поэтому перезапуск ставится в фон, а его ход виден в GET /status.
    """
    try:
        return supervisor.restart_async(mode=body.mode, company_ids=body.company_ids,
                                        issued_by='ui')
    except supervisor.AlreadyRunning as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@api.post('/companies/{company_id}/crawl')
async def company_crawl(company_id: str):
    """Публикация задачи в Kafka urgent_tasks — тот же путь, что у бота (§4)."""
    _valid_company_id(company_id)
    company = companies_mod.company_payload(company_id)
    if company is None:
        raise HTTPException(status_code=404, detail=f'Компания {company_id} не найдена в ситлисте')
    if not supervisor.process_status()['running']:
        raise HTTPException(
            status_code=409,
            detail='Пайплайн не запущен: срочную задачу некому взять из очереди. '
                   'Запустите его в режиме urgent-only (Старт -> «только срочные»).')
    try:
        task_id = await companies_mod.send_urgent_task(company, issued_by='ui')
    except companies_mod.KafkaUnavailable as e:
        journal.append('company.crawl', company_id=company_id, result='error',
                       details={'error': str(e)})
        raise HTTPException(status_code=503, detail=str(e))
    journal.append('company.crawl', company_id=company_id,
                   details={'task_id': task_id, 'name': company.get('original_name')})
    return {'ok': True, 'task_id': task_id, 'company_id': company_id}


# ============================== настройки ====================================

@api.get('/settings')
async def settings_get():
    """Белый список параметров: текущее, дефолт, тип, «требует перезапуска» (§6)."""
    return settings_store.read_all()


@api.put('/settings')
async def settings_put(body: SettingsRequest):
    """Сохранить значения в файл переопределений. Применятся при следующем старте (§6)."""
    try:
        diff = settings_store.save(body.values, issued_by='ui')
    except settings_store.SettingsError as e:
        raise HTTPException(status_code=422, detail=str(e))
    journal.append('settings.update', details={'diff': diff})
    return {'ok': True, 'diff': diff,
            'detail': ('Изменения применятся при следующем старте пайплайна; '
                       'к бегущему прогону они не применяются.'),
            'settings': settings_store.read_all()}


# ============================== профили сайтов ===============================

@api.get('/profiles/{domain}')
async def profile_get(domain: str):
    """Профиль сайта для редактора: текст, версия, ошибки валидации, схема, черновик (§3)."""
    _valid_domain(domain)
    return await asyncio.to_thread(profiles_store.read, domain)


@api.post('/profiles/{domain}/validate')
async def profile_validate(domain: str, body: ProfileTextRequest):
    """Проверить текст без записи: 200 валиден, 422 со списком ошибок."""
    _valid_domain(domain)
    try:
        errors = await asyncio.to_thread(profiles_store.validate, domain, body.text)
    except profiles_store.ProfilesUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    if errors:
        raise HTTPException(status_code=422, detail={'detail': '; '.join(errors), 'errors': errors})
    return {'valid': True, 'errors': []}


@api.put('/profiles/{domain}')
async def profile_put(domain: str, body: ProfileSaveRequest):
    """Сохранить профиль. 409 — файл на диске изменился с момента чтения."""
    _valid_domain(domain)
    try:
        r = await asyncio.to_thread(profiles_store.save, domain, body.text,
                                    body.expected_version, 'ui')
    except (profiles_store.ProfileError, profiles_store.ProfilesUnavailable) as e:
        journal.append('profile.update', result='rejected',
                       details={'domain': domain, 'error': str(e)})
        raise _profile_http(e)
    journal.append('profile.create' if r['created'] else 'profile.update',
                   details={'domain': domain, 'file': r['file'], 'bytes': r['bytes'],
                            'version': r['version'], 'changed_keys': r['changed_keys']})
    r['detail'] = ('Сохранено. Пайплайн перечитывает профили перед каждой компанией: '
                   'правка применится со следующей компании бегущего прогона.')
    return r


@api.delete('/profiles/{domain}')
async def profile_delete(domain: str, body: ProfileDeleteRequest):
    """Удалить профиль с подтверждением вводом домена (как очистка §7)."""
    _valid_domain(domain)
    if not (await asyncio.to_thread(profiles_store.read, domain)).get('exists'):
        raise HTTPException(status_code=404, detail=f'Профиля {domain}.yaml нет')
    try:
        r = await asyncio.to_thread(profiles_store.delete, domain, body.confirm_domain, 'ui')
    except (profiles_store.ProfileError, profiles_store.ProfilesUnavailable) as e:
        journal.append('profile.delete', result='rejected',
                       details={'domain': domain, 'error': str(e)})
        raise _profile_http(e)
    journal.append('profile.delete', details={'domain': domain, 'file': r['file'],
                                              'bytes': r['bytes'], 'version': r['version']})
    return r


@api.post('/profiles/{domain}/accept-draft')
async def profile_accept_draft(domain: str, body: ProfileAcceptRequest):
    """Принять черновик переписи в профиль (profiles_drafts -> profiles)."""
    _valid_domain(domain)
    if not (await asyncio.to_thread(profiles_store.read, domain)).get('draft', {}).get('exists'):
        raise HTTPException(status_code=404, detail=f'Черновика переписи для {domain} нет')
    try:
        r = await asyncio.to_thread(profiles_store.accept_draft, domain,
                                    body.expected_version, 'ui')
    except (profiles_store.ProfileError, profiles_store.ProfilesUnavailable) as e:
        journal.append('profile.accept_draft', result='rejected',
                       details={'domain': domain, 'error': str(e)})
        raise _profile_http(e)
    journal.append('profile.accept_draft',
                   details={'domain': domain, 'file': r['file'], 'confidence': r['confidence'],
                            'profile_version': r['profile_version'], 'version': r['version']})
    r['detail'] = 'Черновик принят в профиль; применится со следующей компании бегущего прогона.'
    return r


# ============================== очистка ======================================

@api.post('/companies/{company_id}/purge/dry-run')
async def purge_dry_run(company_id: str, body: PurgeRequest):
    """Посчитать без удаления: файлов в Temp, файлов в Documents, объектов в графе (§7)."""
    _valid_company_id(company_id)
    if companies_mod.find_in_site_list(company_id) is None \
            and companies_mod.last_result(company_id) is None:
        raise HTTPException(status_code=404, detail=f'Компания {company_id} не найдена')
    try:
        return await asyncio.to_thread(purge_mod.dry_run, company_id, body.sources)
    except purge_mod.PurgeValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except purge_mod.PurgeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@api.post('/companies/{company_id}/purge')
async def purge_execute(company_id: str, body: PurgeRequest):
    """Удаление по выбранным источникам с подтверждением вводом id компании (§7)."""
    _valid_company_id(company_id)
    if companies_mod.find_in_site_list(company_id) is None \
            and companies_mod.last_result(company_id) is None:
        raise HTTPException(status_code=404, detail=f'Компания {company_id} не найдена')
    try:
        report = await asyncio.to_thread(purge_mod.execute, company_id, body.sources,
                                         body.confirm_company_id, 'ui')
    except purge_mod.PurgeValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except purge_mod.PurgeError as e:
        journal.append('company.purge', company_id=company_id, result='rejected',
                       details={'sources': body.sources, 'error': str(e)})
        raise HTTPException(status_code=409, detail=str(e))
    journal.append('company.purge', company_id=company_id,
                   details={'sources': body.sources,
                            'deleted': {k: {kk: vv for kk, vv in v.items()
                                            if kk != 'response'}
                                        for k, v in report['sources'].items()}})
    return report


app.include_router(api)


# ============================== сторож =======================================

@app.on_event('startup')
async def _startup():
    settings_store.apply_to_process_env()
    if not os.environ.get('DASHBOARD_TOKEN'):
        log.warning('DASHBOARD_TOKEN не задан — все эндпоинты будут отвечать 503')
    app.state.watchdog = asyncio.create_task(_watchdog_loop())
    log.info('Диспетчерская запущена')


@app.on_event('shutdown')
async def _shutdown():
    task = getattr(app.state, 'watchdog', None)
    if task:
        task.cancel()


async def _watchdog_loop():
    """Сторож по простою (§5): heartbeat не двигается -> «завис» + алерт (+ авторестарт)."""
    while True:
        try:
            await asyncio.sleep(supervisor.WATCHDOG_INTERVAL_SECONDS)
            await asyncio.to_thread(supervisor.watchdog_tick)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f'Сторож: сбой прохода ({e})')
