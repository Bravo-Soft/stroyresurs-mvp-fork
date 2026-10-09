# dashboard/settings_store.py 1.0.0
# Настройки Диспетчерской (спецификация §6): env-белый список.
#
# Контракт спецификации: UI НЕ редактирует config.py. Значения сохраняются в файл
# переопределений (.env-формат), который супервизор передаёт подпроцессу пайплайна
# при старте — поэтому изменения никогда не применяются к бегущему прогону (§5).
#
# Состав белого списка — РОВНО то, что реально читается в этой версии кода:
#   * Config.load_from_env() — параметры пайплайна;
#   * _path()-дефолты config.py — каталоги (нужны для запуска вне контейнера);
#   * собственные настройки панели (группа «Диспетчерская»).
# Параметры из §6 спецификации, которых в текущей версии пайплайна нет, перечисляются
# в NOT_IN_THIS_BUILD и показываются на экране отдельным блоком «нет в этой версии»:
# редактировать их бессмысленно — ни один модуль их не читает. Сейчас список пуст.
# У каждого параметра обязательно есть описание на русском (note) — экран не показывает
# «голых» имён. Векторная БД (VECTOR_DB_*) и AI Tunnel (AI_TUNNEL_*) из списка убраны:
# первая упразднена (остался только граф), второй больше не используется.
#
# Секреты (type='secret') не выводятся никогда: ни в GET, ни в журнале — только
# признак «задано / не задано» (§9).

import logging
import os
import re

from common import cfg, invalidate_cfg

log = logging.getLogger("dashboard.settings")

OVERRIDES_PATH = os.environ.get(
    'DASHBOARD_SETTINGS_PATH',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'settings_overrides.env'))


def _W(name, group, type_, attr=None, note='', locked=False):
    return {'name': name, 'group': group, 'type': type_, 'attr': attr,
            'note': note, 'locked': locked}


WHITELIST = [
    # --- LLM -----------------------------------------------------------------
    _W('OLLAMA_URL', 'LLM', 'str', 'ollama_url',
       'Базовый URL OpenAI-совместимого шлюза SGR-извлечения (Ollama Cloud или свой сервер инференса)'),
    _W('OLLAMA_MODEL', 'LLM', 'str', 'ollama_model',
       'id модели, как его отдаёт GET /v1/models шлюза'),
    _W('OLLAMA_API_KEY', 'LLM', 'secret', 'ollama_api_key',
       'Ключ доступа к LLM-шлюзу (Bearer в каждом запросе)'),
    _W('LLM_REQUEST_DELAY_SECONDS', 'LLM', 'int', 'llm_request_delay_seconds',
       'Таймаут между запросами к LLM: минимальная пауза между последовательными запросами, сек '
       '(общая на все параллельные воркеры). 0 — без паузы'),
    _W('LITELLM_URL', 'LLM', 'str', None,
       'Если задан — SGR идёт через LiteLLM-шлюз вместо Ollama Cloud (перекрывает OLLAMA_*)'),
    _W('LITELLM_MODEL', 'LLM', 'str', None, 'alias модели в LiteLLM'),
    _W('LITELLM_CONCURRENCY', 'LLM', 'int', None,
       'Одновременных запросов к LiteLLM (слотов инференса)'),
    _W('GEMMA4_26B_KEY', 'LLM', 'secret', None, 'Virtual key LiteLLM'),

    # --- Краулер --------------------------------------------------------------
    _W('MAX_CONCURRENT_PAGES', 'Краулер', 'int', 'max_concurrent_pages',
       'Одновременно открытых страниц при краулинге — главный рычаг нагрузки на сайт и на CPU/память'),
    _W('MAX_PAGES_PER_SITE', 'Краулер', 'int', 'max_pages_per_site',
       'Общий лимит страниц на сайт за прогон: по достижении краул останавливается. '
       'YAML-профиль сайта (limits.pages) перекрывает'),
    _W('MAX_PRODUCT_PAGES_PER_SITE', 'Краулер', 'int', 'max_product_pages_per_site',
       'Верхняя граница товарных страниц на сайт: сверх неё товарные URL не ставятся в очередь. '
       'YAML-профиль сайта (limits.product_pages) перекрывает'),
    _W('MAX_CONCURRENT_FILE_DOWNLOADS', 'Краулер', 'int', 'max_concurrent_file_downloads',
       'Одновременных скачиваний файлов (документы, изображения) с сайта'),
    _W('PRODUCT_MIN_TEXT', 'Краулер', 'int', 'product_min_text',
       'Гейт «оболочки» товарной страницы: минимум видимого текста, ниже — Playwright; 0 выключает'),
    _W('COMPANY_IDLE_TIMEOUT_SECONDS', 'Краулер', 'int', 'company_idle_timeout_seconds',
       'Сторож D101 в пайплайне: простой без записей в лог дольше порога — компания бросается '
       '(0 выключает). Панель считает процесс зависшим по этому же порогу + WATCHDOG_GRACE_SECONDS'),

    # --- Стриминг -------------------------------------------------------------
    _W('PIPELINE_STREAMING_ENABLED', 'Стриминг', 'bool', 'pipeline_streaming_enabled',
       'Потоковая обработка страниц (краул и извлечение параллельно)'),
    _W('PIPELINE_PAGE_WORKERS', 'Стриминг', 'int', 'pipeline_page_workers',
       'Воркеров обработки страниц в потоковом режиме'),

    # --- Graph DB -------------------------------------------------------------
    _W('GRAPH_DB_API_URL', 'Graph DB', 'str', 'graph_db_api_url',
       'Адрес API backend графовой БД (…/api/companies): сюда выгружаются компании, '
       'отсюда читается сводка графа на экране «Компания»'),
    _W('GRAPH_DB_ENABLE', 'Graph DB', 'bool', 'graph_db_enable',
       'Выгружать результаты в графовую БД. Выключено — данные остаются только в файлах '
       '(GRAPH_DB_JSON_OUTPUT_DIR, DOCUMENTS_DIR)'),
    _W('GRAPH_DB_PENDING_DIR', 'Graph DB', 'str', 'graph_db_pending_dir',
       'Очередь ретраев выгрузки: неподтверждённые пакеты, которые досылаются при следующем старте'),
    _W('GRAPH_DB_MAX_RETRIES_ON_RESUME', 'Graph DB', 'int',
       'graph_db_max_retries_on_resume',
       'Сколько раз пытаться дослать пакет из очереди ретраев; сверх лимита пакет остаётся '
       'недоставленным и пишется ошибка'),
    _W('GRAPH_DB_RESUME_DELAY_SECONDS', 'Graph DB', 'int',
       'graph_db_resume_delay_seconds',
       'Пауза между попытками досылки из очереди ретраев, сек (в этой версии пайплайна читается, '
       'но не применяется)'),

    # --- Kafka ----------------------------------------------------------------
    _W('KAFKA_BOOTSTRAP_SERVERS', 'Kafka', 'str', 'kafka_bootstrap_servers',
       'Адрес брокера Kafka host:port — канал срочных задач от бота и панели'),
    _W('KAFKA_TOPIC_REGULAR_TASKS', 'Kafka', 'str', 'kafka_topic_regular_tasks',
       'Топик плановых задач. В этой версии плановый список берётся из ситлиста в Postgres, '
       'топик используется только продюсером'),
    _W('KAFKA_TOPIC_URGENT_TASKS', 'Kafka', 'str', 'kafka_topic_urgent_tasks',
       'Канал «бот/панель -> срочная обработка компании»'),
    _W('KAFKA_CONSUMER_GROUP', 'Kafka', 'str', 'kafka_consumer_group',
       'Группа потребителей пайплайна в Kafka; экземпляры с одной группой делят очередь срочных задач'),

    # --- Каталоги -------------------------------------------------------------
    _W('BASE_DIR', 'Каталоги', 'str', 'base_dir',
       'Рабочий каталог пайплайна: temp-хранилище HTML, чекпоинт, кэш имён производителей'),
    _W('LOGS_DIR', 'Каталоги', 'str', 'logs_dir', 'Логи и каталог прогонов runs/'),
    _W('REPORTS_DIR', 'Каталоги', 'str', 'reports_dir',
       'Сводные отчёты прогона (XLSX), список недоступных сайтов, лог ошибок LibreOffice'),
    _W('DOCUMENTS_DIR', 'Каталоги', 'str', 'documents_dir',
       'Карточки и скачанные файлы компаний'),
    _W('GRAPH_DB_ARCHIVE_DIR', 'Каталоги', 'str', 'graph_db_archive_dir',
       'Архив пакетов, отправленных в граф. Аудиторский след — очисткой не трогается'),
    _W('GRAPH_DB_JSON_OUTPUT_DIR', 'Каталоги', 'str', 'graph_db_json_output_dir',
       'JSON-снимки компаний в формате графа перед выгрузкой. Аудиторский след — очисткой не трогается'),
    _W('PROFILES_DIR', 'Каталоги', 'str', 'profiles_dir',
       'YAML-профили сайтов; редактируются на карточке компании'),
    _W('PROFILES_DRAFTS_DIR', 'Каталоги', 'str', 'profiles_drafts_dir',
       'Черновики профилей, которые пишет перепись (CENSUS_ENABLED); принимаются в '
       'профили кнопкой на карточке компании'),
    _W('PROFILE_METRICS_DIR', 'Каталоги', 'str', 'profile_metrics_dir',
       'Метрики профилирования (алерты на экране «Обзор»)'),

    # --- Прочее ---------------------------------------------------------------
    _W('ENABLE_FILE_CONVERSION', 'Прочее', 'bool', 'enable_file_conversion',
       'Конвертация офисных файлов в PDF'),
    _W('LIBREOFFICE_PATH', 'Прочее', 'str', 'libreoffice_path',
       'Путь к исполняемому файлу LibreOffice (soffice) для конвертации в PDF'),
    _W('TREAT_HTTP_HTTPS_AS_SAME', 'Прочее', 'bool', 'treat_http_https_as_same',
       'Считать http:// и https:// версии адреса одним сайтом (дедупликация URL)'),
    _W('DOMAIN_EQUIVALENCY_ENABLED', 'Прочее', 'bool', 'domain_equivalency_enabled',
       'Распознавать эквивалентные домены (www / без www и настроенные пары) как один сайт'),
    _W('PREFER_HTTPS', 'Прочее', 'bool', 'prefer_https',
       'При выборе схемы для сайта предпочитать https'),
    _W('CENSUS_ENABLED', 'Прочее', 'bool', 'census_enabled',
       'Режим переписи: черновики профилей сайтов в profiles_drafts'),

    # --- Собственные настройки панели ----------------------------------------
    _W('WATCHDOG_GRACE_SECONDS', 'Диспетчерская', 'int', None,
       'Запас панели сверх COMPANY_IDLE_TIMEOUT_SECONDS до статуса «завис» (§5): '
       'сторож пайплайна должен успеть отработать первым'),
    _W('WATCHDOG_AUTORESTART', 'Диспетчерская', 'bool', None,
       'Мягкий перезапуск с чекпоинта при зависании'),
    _W('WATCHDOG_ALERT_PACHCA', 'Диспетчерская', 'bool', None,
       'Слать алерт о зависании в Пачку (нужны PACHKA_ACCESS_TOKEN и PACHKA_ADMIN_ID)'),
    _W('BOT_URL', 'Диспетчерская', 'str', None,
       'Адрес бота Пачки для светофора здоровья'),
    _W('PIPELINE_CMD', 'Диспетчерская', 'str', None,
       'Что запускает кнопка «Старт» (по умолчанию main.py). '
       'Для репетиции сценариев — dashboard/fake_pipeline.py'),
]

# Параметры из §6 спецификации, отсутствующие в этой версии пайплайна.
# Показываются на экране справочно: ни один модуль их не читает.
# В текущей сборке все параметры §6 читаются Config.load_from_env — список пуст.
NOT_IN_THIS_BUILD = []

_BY_NAME = {item['name']: item for item in WHITELIST}
_TRUE = ('true', '1', 'yes', 'on')
_FALSE = ('false', '0', 'no', 'off')
_ENV_LINE_RE = re.compile(r'^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$')

# Значения окружения на момент старта панели — чтобы отличать «пришло из файла
# переопределений» от «было в окружении изначально».
_BASE_ENV = {}


# ------------------------------ файл переопределений -------------------------

def read_overrides():
    """Содержимое файла переопределений как dict (пустой, если файла нет)."""
    values = {}
    if not os.path.isfile(OVERRIDES_PATH):
        return values
    try:
        with open(OVERRIDES_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip() or line.lstrip().startswith('#'):
                    continue
                m = _ENV_LINE_RE.match(line)
                if m and m.group(1) in _BY_NAME:
                    values[m.group(1)] = m.group(2)
    except OSError as e:
        log.warning(f"Не удалось прочитать {OVERRIDES_PATH}: {e}")
    return values


def apply_to_process_env():
    """Применить переопределения к окружению самой панели.

    Вызывается при старте и после каждого PUT /settings: панель должна показывать
    те же адреса и каталоги, которые получит подпроцесс пайплайна.
    """
    global _BASE_ENV
    if not _BASE_ENV:
        # Сначала подтянуть config.py: его импорт вызывает load_dotenv(), и только после
        # этого в os.environ появляются ключи из .env. Снимок, сделанный раньше, показывал
        # заданные секреты как «не задано».
        cfg()
        _BASE_ENV = {item['name']: os.environ.get(item['name']) for item in WHITELIST}
    for name, value in read_overrides().items():
        os.environ[name] = value
    invalidate_cfg()


def env_for_subprocess(base_env=None):
    """Окружение для запуска пайплайна: текущее + файл переопределений."""
    env = dict(base_env if base_env is not None else os.environ)
    env.update(read_overrides())
    return env


# ------------------------------ чтение ----------------------------------------

def _config_default(attr):
    """Значение атрибута из датакласса Config."""
    if not attr:
        return None
    try:
        import dataclasses
        from config import Config
        for f in dataclasses.fields(Config):
            if f.name == attr:
                if f.default is not dataclasses.MISSING:
                    return f.default
                if f.default_factory is not dataclasses.MISSING:
                    return f.default_factory()
        return getattr(Config, attr, None)
    except Exception:
        return None


def _stringify(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def _effective(item, overrides):
    """Действующее значение параметра и его источник."""
    name = item['name']
    if name in overrides:
        return overrides[name], 'overrides'
    env_value = _BASE_ENV.get(name) if _BASE_ENV else os.environ.get(name)
    if env_value:
        return env_value, 'env'
    return _stringify(_config_default(item['attr'])), 'config'


def read_all():
    """Белый список для экрана «Настройки» (§6). Секреты — только «задано/не задано»."""
    overrides = read_overrides()
    items = []
    for item in WHITELIST:
        value, source = _effective(item, overrides)
        row = {
            'name': item['name'],
            'group': item['group'],
            'type': item['type'],
            'note': item['note'],
            'locked': item['locked'],
            'source': source,
            'restart_required': item['group'] != 'Диспетчерская',
            'config_default': (None if item['type'] == 'secret'
                               else _stringify(_config_default(item['attr']))),
        }
        if item['type'] == 'secret':
            row['is_set'] = bool(value)
            row['value'] = None
        else:
            row['value'] = value
        items.append(row)
    return {
        'items': items,
        'overrides_path': OVERRIDES_PATH,
        'overrides_count': len(overrides),
        'not_in_this_build': [{'name': n, 'group': g} for n, g in NOT_IN_THIS_BUILD],
    }


def _journal_view():
    """Снимок значений для диффа в журнал: секреты — «задано/не задано»."""
    view = {}
    for row in read_all()['items']:
        if row['type'] == 'secret':
            view[row['name']] = 'задано' if row.get('is_set') else 'не задано'
        else:
            view[row['name']] = row.get('value')
    return view


# ------------------------------ запись ----------------------------------------

class SettingsError(ValueError):
    """Ошибка валидации значения (наружу — HTTP 422)."""


def _validate(item, raw):
    """Привести значение к каноническому виду или бросить SettingsError."""
    value = '' if raw is None else str(raw).strip()
    if item['type'] == 'int':
        try:
            return str(int(value))
        except ValueError:
            raise SettingsError(f"{item['name']}: ожидается целое число, получено «{value}»")
    if item['type'] == 'bool':
        low = value.lower()
        if low in _TRUE:
            return 'true'
        if low in _FALSE:
            return 'false'
        raise SettingsError(f"{item['name']}: ожидается true/false, получено «{value}»")
    if '\n' in value or '\r' in value:
        raise SettingsError(f"{item['name']}: перенос строки в значении недопустим")
    return value


def save(values, issued_by='ui'):
    """Сохранить переопределения. Возвращает дифф «было -> стало» для журнала.

    Пустая строка для секрета означает «не менять»: очистка секретов из UI
    намеренно не предусмотрена (ротация ключей — операция уровня .env).
    """
    overrides = read_overrides()
    before = _journal_view()

    for name, raw in (values or {}).items():
        item = _BY_NAME.get(name)
        if item is None:
            raise SettingsError(f"Параметр вне белого списка: {name}")
        if item['locked']:
            raise SettingsError(f"{name}: параметр заблокирован ({item['note']})")
        if item['type'] == 'secret' and not str(raw or '').strip():
            continue
        overrides[name] = _validate(item, raw)

    _write_overrides(overrides)
    apply_to_process_env()

    after = _journal_view()
    diff = {name: {'before': before.get(name), 'after': after.get(name)}
            for name in (values or {}) if before.get(name) != after.get(name)}
    log.info(f"Настройки сохранены ({issued_by}): изменено параметров — {len(diff)}")
    return diff


def _write_overrides(overrides):
    lines = [
        '# dashboard/settings_overrides.env',
        '# Файл переопределений Диспетчерской (§6 спецификации). Пишется экраном «Настройки».',
        '# Применяется к пайплайну ПРИ СЛЕДУЮЩЕМ СТАРТЕ, к бегущему прогону — никогда.',
        '# Правка вручную допустима; параметры вне белого списка игнорируются.',
        '',
    ]
    for item in WHITELIST:
        if item['name'] in overrides:
            lines.append(f"{item['name']}={overrides[item['name']]}")
    tmp = OVERRIDES_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines) + '\n')
    os.replace(tmp, OVERRIDES_PATH)
