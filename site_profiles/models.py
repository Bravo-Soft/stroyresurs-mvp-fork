"""Модели декларативного профиля сайта (см. profiles/_schema.yaml и
claude_work/Анализ системы/АРХИТЕКТУРА_ПРОФИЛИРОВАНИЯ.md).

Принцип: значение None / пустой список / False = «текущее глобальное поведение»;
профиль, состоящий из одних дефолтов, байт-в-байт эквивалентен отсутствию профиля.
"""
from dataclasses import dataclass, field, fields as dc_fields
from typing import Optional


# Допустимые значения enum-полей (единственный источник истины для валидатора).
RENDER_VALUES = (None, 'html', 'browser')
ANTIBOT_VALUES = ('none', 'impersonate', 'warmup', 'blocked')
TIER_VALUES = ('custom', 'jsonld', 'cms', 'llm')
SOURCE_VALUES = ('census', 'manual', 'mixed')


@dataclass
class LoadProfile:
    """Пер-хостовая нагрузка и таймауты (анти-блокировка при массовом скачивании)."""
    page_delay_ms: int = 0            # мин. интервал между запросами страниц к хосту; 0 = как сейчас
    file_delay_ms: int = 0            # интервал между скачиваниями файлов/картинок
    max_concurrent_pages: Optional[int] = None   # None -> config.max_concurrent_pages
    max_concurrent_files: Optional[int] = None   # None -> config.max_concurrent_file_downloads
    fetch_timeout_s: Optional[int] = None        # None -> config.http_timeout
    file_timeout_s: Optional[int] = None         # None -> текущие таймауты FileDownloadManager
    page_retry_attempts: Optional[int] = None    # None -> page_retry_max_attempts (D40)


@dataclass
class CrawlLimits:
    pages: Optional[int] = None           # None -> config.max_pages_per_site
    product_pages: Optional[int] = None   # None -> config.max_product_pages_per_site
    depth: Optional[int] = None           # None -> config.max_depth
    sitemap_urls: Optional[int] = None    # None -> config.sitemap_max_urls


@dataclass
class CrawlProfile:
    render: Optional[str] = None          # None|html|browser (browser = сразу Playwright, SPA)
    antibot: str = 'none'                 # none|impersonate|warmup|blocked
    ajax_tabs: bool = False               # <- config.ajax_product_tabs_domains (D01)
    bitrix_offers: bool = False           # <- config.bitrix_offers_domains (D82)
    tab_panel_selector: Optional[str] = None  # пер-сайтовый селектор панели вкладки
    equivalent_domains: list = field(default_factory=list)  # <- config.equivalent_domains
    strict_www: bool = False              # <- config.strict_www_domains
    subdomain_collapse: list = field(default_factory=list)  # ['*.hms.ru'] -> hms.ru
    exclude_paths: list = field(default_factory=list)       # <- config.exclude_url_patterns_by_domain
    limits: CrawlLimits = field(default_factory=CrawlLimits)
    load: LoadProfile = field(default_factory=LoadProfile)


@dataclass
class SectionsProfile:
    """Карта разделов сайта: где товары, дистрибьюторы, реквизиты, общие документы."""
    start_urls: list = field(default_factory=list)       # доп. стартовые точки поверх _get_start_urls
    catalog_roots: list = field(default_factory=list)    # корни каталога (приоритет обхода)
    product_url_patterns: list = field(default_factory=list)      # regex: URL = карточка товара
    product_url_antipatterns: list = field(default_factory=list)  # regex: точно НЕ товар (сильнее patterns)
    contacts_urls: list = field(default_factory=list)    # контакты/реквизиты
    distributor_urls: list = field(default_factory=list) # дилеры/где купить/представительства
    # Общие документы компании — 4 блока, зеркалят папки компании
    # Certificates/Documents/Instructions/Price_lists:
    certificates_urls: list = field(default_factory=list)  # сертификаты/декларации/свидетельства
    documents_urls: list = field(default_factory=list)     # прочая общая документация
    instructions_urls: list = field(default_factory=list)  # инструкции/руководства
    price_list_urls: list = field(default_factory=list)    # прайс-листы


@dataclass
class MarkdownProfile:
    """Точный профиль извлечения markdown (полнота + отсечение мусора для LLM)."""
    product_container: Optional[str] = None   # CSS контейнера карточки (перекрывает цепочку generic)
    remove_selectors: list = field(default_factory=list)  # пер-сайтовый шум ДОПОЛНИТЕЛЬНО к clean_noise
    min_len: Optional[int] = None             # None -> 200 (порог «пустого» markdown / отката universal)
    max_len: Optional[int] = None             # None -> без обрезки (страховка text[:100000] в LLM остаётся)
    company_container: Optional[str] = None
    distributor_container: Optional[str] = None


@dataclass
class ExtractProfile:
    tier: str = 'llm'                     # custom|jsonld|cms|llm (llm = текущий generic-путь)
    cluster: Optional[str] = None         # ссылка в profiles/_clusters.yaml (фаза 2)
    custom_module: Optional[str] = None   # имя модуля Profile_Markdown/ (tier=custom)
    keep_hidden_tabs: bool = False        # <- атрибут адаптера KEEP_HIDDEN_TABS
    needs_raw_html: bool = False          # <- NEEDS_RAW_HTML
    clean_in_universal: bool = False      # <- CLEAN_IN_UNIVERSAL
    llm_program: Optional[str] = None     # фаза 2 (LLM-роутер)
    streaming: Optional[bool] = None      # None = глобально; False = батч (chelaz.ru, D85)
    markdown: MarkdownProfile = field(default_factory=MarkdownProfile)


@dataclass
class SiteProfile:
    domain: str = ''
    profile_version: int = 1
    profiled_at: Optional[str] = None
    confidence: Optional[float] = None    # < порога -> REVIEW queue
    source: str = 'manual'                # census|manual|mixed
    notes: list = field(default_factory=list)  # D-номера, решения заказчика
    crawl: CrawlProfile = field(default_factory=CrawlProfile)
    sections: SectionsProfile = field(default_factory=SectionsProfile)
    extract: ExtractProfile = field(default_factory=ExtractProfile)
    # Метрики последней переписи/прогона (structure_hash, product_cards, ...).
    # Свободный словарь: набор полей задаёт census, схема — в _schema.yaml.
    baseline: dict = field(default_factory=dict)

    @classmethod
    def default(cls, domain: str = '') -> 'SiteProfile':
        """Профиль-по-умолчанию: полностью текущее generic-поведение пайплайна."""
        return cls(domain=domain)

    def is_default(self) -> bool:
        return self == SiteProfile.default(self.domain)


# ==================== ЗАГРУЗКА ИЗ dict + ВАЛИДАЦИЯ ====================

def _err(errors, path, msg):
    errors.append(f'{path}: {msg}')


def _check_str_list(errors, path, value):
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        _err(errors, path, 'ожидается список строк')
        return []
    return value


def _check_opt_int(errors, path, value, minimum=0):
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        _err(errors, path, f'ожидается целое >= {minimum} или null')
        return None
    return value


def _check_bool(errors, path, value):
    if not isinstance(value, bool):
        _err(errors, path, 'ожидается true/false')
        return False
    return value


def _pop_known(errors, path, data: dict, known: tuple, ignored: tuple = ()):
    """Проверяет, что в data нет неизвестных ключей (защита от опечаток в YAML)."""
    for key in data:
        if key not in known and key not in ignored:
            _err(errors, path, f'неизвестное поле "{key}"')


def _build_load(data, errors, path='crawl.load') -> LoadProfile:
    lp = LoadProfile()
    if not isinstance(data, dict):
        _err(errors, path, 'ожидается объект')
        return lp
    _pop_known(errors, path, data, tuple(f.name for f in dc_fields(LoadProfile)))
    for name in ('page_delay_ms', 'file_delay_ms'):
        if name in data:
            setattr(lp, name, _check_opt_int(errors, f'{path}.{name}', data[name]) or 0)
    for name in ('max_concurrent_pages', 'max_concurrent_files',
                 'fetch_timeout_s', 'file_timeout_s', 'page_retry_attempts'):
        if name in data:
            setattr(lp, name, _check_opt_int(errors, f'{path}.{name}', data[name]))
    return lp


def _build_limits(data, errors, path='crawl.limits') -> CrawlLimits:
    lim = CrawlLimits()
    if not isinstance(data, dict):
        _err(errors, path, 'ожидается объект')
        return lim
    _pop_known(errors, path, data, tuple(f.name for f in dc_fields(CrawlLimits)))
    for name in ('pages', 'product_pages', 'depth', 'sitemap_urls'):
        if name in data:
            setattr(lim, name, _check_opt_int(errors, f'{path}.{name}', data[name]))
    return lim


def _build_crawl(data, errors) -> CrawlProfile:
    cp = CrawlProfile()
    if not isinstance(data, dict):
        _err(errors, 'crawl', 'ожидается объект')
        return cp
    _pop_known(errors, 'crawl', data, tuple(f.name for f in dc_fields(CrawlProfile)))
    if 'render' in data:
        if data['render'] not in RENDER_VALUES:
            _err(errors, 'crawl.render', f'допустимо: {RENDER_VALUES}')
        else:
            cp.render = data['render']
    if 'antibot' in data:
        if data['antibot'] not in ANTIBOT_VALUES:
            _err(errors, 'crawl.antibot', f'допустимо: {ANTIBOT_VALUES}')
        else:
            cp.antibot = data['antibot']
    for name in ('ajax_tabs', 'bitrix_offers', 'strict_www'):
        if name in data:
            setattr(cp, name, _check_bool(errors, f'crawl.{name}', data[name]))
    if 'tab_panel_selector' in data and data['tab_panel_selector'] is not None:
        if not isinstance(data['tab_panel_selector'], str):
            _err(errors, 'crawl.tab_panel_selector', 'ожидается строка или null')
        else:
            cp.tab_panel_selector = data['tab_panel_selector']
    for name in ('equivalent_domains', 'subdomain_collapse', 'exclude_paths'):
        if name in data:
            setattr(cp, name, _check_str_list(errors, f'crawl.{name}', data[name]))
    if 'limits' in data:
        cp.limits = _build_limits(data['limits'], errors)
    if 'load' in data:
        cp.load = _build_load(data['load'], errors)
    return cp


def _build_sections(data, errors) -> SectionsProfile:
    sp = SectionsProfile()
    if not isinstance(data, dict):
        _err(errors, 'sections', 'ожидается объект')
        return sp
    names = tuple(f.name for f in dc_fields(SectionsProfile))
    _pop_known(errors, 'sections', data, names)
    for name in names:
        if name in data:
            setattr(sp, name, _check_str_list(errors, f'sections.{name}', data[name]))
    return sp


def _build_markdown(data, errors, path='extract.markdown') -> MarkdownProfile:
    mp = MarkdownProfile()
    if not isinstance(data, dict):
        _err(errors, path, 'ожидается объект')
        return mp
    _pop_known(errors, path, data, tuple(f.name for f in dc_fields(MarkdownProfile)))
    for name in ('product_container', 'company_container', 'distributor_container'):
        if name in data and data[name] is not None:
            if not isinstance(data[name], str):
                _err(errors, f'{path}.{name}', 'ожидается строка или null')
            else:
                setattr(mp, name, data[name])
    if 'remove_selectors' in data:
        mp.remove_selectors = _check_str_list(errors, f'{path}.remove_selectors', data['remove_selectors'])
    for name in ('min_len', 'max_len'):
        if name in data:
            setattr(mp, name, _check_opt_int(errors, f'{path}.{name}', data[name]))
    return mp


def _build_extract(data, errors) -> ExtractProfile:
    ep = ExtractProfile()
    if not isinstance(data, dict):
        _err(errors, 'extract', 'ожидается объект')
        return ep
    _pop_known(errors, 'extract', data, tuple(f.name for f in dc_fields(ExtractProfile)))
    if 'tier' in data:
        if data['tier'] not in TIER_VALUES:
            _err(errors, 'extract.tier', f'допустимо: {TIER_VALUES}')
        else:
            ep.tier = data['tier']
    for name in ('cluster', 'custom_module', 'llm_program'):
        if name in data and data[name] is not None:
            if not isinstance(data[name], str):
                _err(errors, f'extract.{name}', 'ожидается строка или null')
            else:
                setattr(ep, name, data[name])
    for name in ('keep_hidden_tabs', 'needs_raw_html', 'clean_in_universal'):
        if name in data:
            setattr(ep, name, _check_bool(errors, f'extract.{name}', data[name]))
    if 'streaming' in data and data['streaming'] is not None:
        if not isinstance(data['streaming'], bool):
            _err(errors, 'extract.streaming', 'ожидается true/false или null')
        else:
            ep.streaming = data['streaming']
    if 'markdown' in data:
        ep.markdown = _build_markdown(data['markdown'], errors)
    if ep.tier == 'custom' and not ep.custom_module:
        _err(errors, 'extract', 'tier=custom требует custom_module')
    return ep


TOP_LEVEL_KEYS = ('domain', 'profile_version', 'profiled_at', 'confidence',
                  'source', 'notes', 'crawl', 'sections', 'extract', 'baseline')
# Служебные блоки черновиков переписи — допускаются и игнорируются при загрузке.
IGNORED_TOP_LEVEL_KEYS = ('census_evidence',)


def profile_from_dict(data: dict):
    """Собирает SiteProfile из словаря (разобранного YAML).

    Возвращает (profile, errors). errors — список строк «путь: проблема»;
    при непустом списке профиль считается невалидным (загрузчик его не применяет).
    """
    errors = []
    if not isinstance(data, dict):
        return SiteProfile(), ['профиль: ожидается объект YAML']
    _pop_known(errors, 'профиль', data, TOP_LEVEL_KEYS, IGNORED_TOP_LEVEL_KEYS)

    profile = SiteProfile()
    domain = data.get('domain')
    if not isinstance(domain, str) or not domain:
        _err(errors, 'domain', 'обязательное непустое строковое поле')
    else:
        profile.domain = domain.lower()

    if 'profile_version' in data:
        profile.profile_version = _check_opt_int(errors, 'profile_version',
                                                 data['profile_version'], minimum=1) or 1
    if 'profiled_at' in data and data['profiled_at'] is not None:
        profile.profiled_at = str(data['profiled_at'])
    if 'confidence' in data and data['confidence'] is not None:
        conf = data['confidence']
        if not isinstance(conf, (int, float)) or isinstance(conf, bool) or not (0 <= conf <= 1):
            _err(errors, 'confidence', 'ожидается число 0..1 или null')
        else:
            profile.confidence = float(conf)
    if 'source' in data:
        if data['source'] not in SOURCE_VALUES:
            _err(errors, 'source', f'допустимо: {SOURCE_VALUES}')
        else:
            profile.source = data['source']
    if 'notes' in data:
        profile.notes = _check_str_list(errors, 'notes', data['notes'])
    if 'crawl' in data:
        profile.crawl = _build_crawl(data['crawl'], errors)
    if 'sections' in data:
        profile.sections = _build_sections(data['sections'], errors)
    if 'extract' in data:
        profile.extract = _build_extract(data['extract'], errors)
    if 'baseline' in data:
        if not isinstance(data['baseline'], dict):
            _err(errors, 'baseline', 'ожидается объект')
        else:
            profile.baseline = data['baseline']
    return profile, errors
