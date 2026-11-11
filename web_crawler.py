# web_crawler.py 1.0.0
import re
import os
import gc
import gzip
import xml.etree.ElementTree as ET
import time
import json
import hashlib
from file_download_cache import FileDownloadCache, atomic_write, file_sha256
import psutil
import aiohttp
import asyncio
from async_timeout import timeout as async_deadline
import logging
import aiofiles
from datetime import datetime
from uuid import uuid4
from contextlib import nullcontext
from collections import deque
from dataclasses import dataclass
from bs4 import BeautifulSoup, Tag, Comment
from asyncio import Semaphore, Lock
from typing import Dict, Any, List, Tuple, Optional, Union
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse, unquote
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# curl_cffi даёт HTTP-клиент с браузерным TLS/HTTP2-отпечатком (impersonate).
# Импорт мягкий: если библиотеки нет, HTTP-first слой авто-отключается и краулер
# работает по-старому (aiohttp/Playwright). Установка: pip install curl_cffi.
try:
    from curl_cffi.requests import AsyncSession as _CurlAsyncSession
    _CURL_CFFI_AVAILABLE = True
except Exception:
    _CurlAsyncSession = None
    _CURL_CFFI_AVAILABLE = False

from fetch_outcome import (FetchOutcome, fetch_observer, observe_fetch,
                           retry_after_seconds, is_soft_not_found)
from inline_products import inline_product_links
from product_interactions import (product_state_key, tilda_product_links, isolate_product_state,
                                  expand_product_panels)
from contact_interactions import collect_contact_panels, needs_contact_interaction
from config import Config
from product_utils import sanitize_filename
from temp_storage_manager import TempStorageManager
from domain_equivalency import (DomainEquivalencyManager, hosts_equivalent,
                                url_in_scope)
from data_island import (needs_javascript, has_product_island, is_unrendered_store_listing,
                         looks_like_product_shell, has_unloaded_product_tabs,
                         PRODUCT_TAB_SELECTOR, PRODUCT_TAB_LABEL)

# Профили сайтов (mvp/profiles/*.yaml): пер-сайтовые стратегии краулинга.
# Импорт защищён (fail-open): без пакета/зависимостей краулер работает как раньше.
try:
    from site_profiles import get_resolver as _get_profile_resolver
    from site_profiles.host_throttle import HostThrottle
except Exception:
    _get_profile_resolver = None
    HostThrottle = None

log = logging.getLogger("crawler")

# Память, которую считает лимит контейнера (mem_limit). Внутри контейнера
# /sys/fs/cgroup примонтирован на его собственный scope, поэтому эти файлы
# описывают именно контейнер, а не хост.
_CGROUP_MEMORY_CURRENT = "/sys/fs/cgroup/memory.current"
_CGROUP_MEMORY_STAT = "/sys/fs/cgroup/memory.stat"

# Очистка не возвращает память Chromium, поэтому сразу после неё порог обычно
# остаётся пробитым: без паузы сторож молотил бы на каждой проверке.
MEMORY_CLEANUP_COOLDOWN_SECONDS = 300


def read_memory_usage_mb() -> float:
    """Потребление памяти всем контейнером, в МБ.

    psutil.Process().memory_info().rss видит только сам питон, а под mem_limit
    попадают и процессы Chromium/Playwright. Из-за этого сторож не сработал ни
    разу за весь лог, пока контейнер рос до 23.6 ГиБ и его не убил OOM-киллер
    (прогон 26.08-01.09, компания 685 из 1452).

    Страничный кэш вычитаем: он вытесняется без OOM, иначе порог пробивала бы
    обычная запись документов. shmem (tmpfs/shm) учтён внутри file, но не
    вытесняется, поэтому его возвращаем обратно.

    Fail-open: вне контейнера или на cgroup v1 файлов нет — откатываемся на RSS
    процесса, то есть на прежнее поведение.
    """
    try:
        with open(_CGROUP_MEMORY_CURRENT) as f:
            current = int(f.read().strip())
        page_cache = shmem = 0
        with open(_CGROUP_MEMORY_STAT) as f:
            for line in f:
                key, _, value = line.partition(" ")
                if key == "file":
                    page_cache = int(value)
                elif key == "shmem":
                    shmem = int(value)
        return max(current - (page_cache - shmem), 0) / 1024 / 1024
    except Exception:
        return psutil.Process().memory_info().rss / 1024 / 1024


# Только подтверждённое отсутствие ресурса; WAF/auth и временные отказы
# не доказывают, что другой транспорт не сможет загрузить страницу.
PERMANENT_ERRORS = {404, 410}

# Реалистичные браузерные заголовки для HTTP-impersonate. User-Agent выставляет сам
# curl_cffi через impersonate — здесь его НЕ задаём, иначе разъедется с TLS/HTTP2-отпечатком.
IMPERSONATE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# D233: в ПУТЬ ссылки вклеен ещё один абсолютный адрес («…/catalog/https:/example.ru/x»);
# встречается и с одним слешем после схемы. В query такое легально (редиректоры,
# share-ссылки), поэтому эвристика применяется только к пути.
_GLUED_URL_IN_PATH_RE = re.compile(r'(https?:)/{1,3}', re.I)


def sanitize_href(href: str) -> str:
    """D257/D233: приводит значение атрибута href к пригодному для склейки виду —
    срезает пробельные края и чинит адрес с вклеенным в ПУТЬ абсолютным URL.
    Возвращает исходное значение, если чинить нечего."""
    cleaned = (href or '').strip()
    if not cleaned:
        return cleaned
    cut = len(cleaned)
    for separator in ('?', '#'):
        position = cleaned.find(separator)
        if position != -1:
            cut = min(cut, position)
    path_part, tail = cleaned[:cut], cleaned[cut:]
    matches = list(_GLUED_URL_IN_PATH_RE.finditer(path_part))
    if not matches or matches[-1].start() == 0:
        return cleaned
    last = matches[-1]
    return f"{last.group(1).lower()}//{path_part[last.end():].lstrip('/')}{tail}"


def document_base_url(soup: BeautifulSoup, page_url: str) -> str:
    """База относительных ссылок: первый HTML base[href] или адрес документа."""
    base = soup.find('base', href=True)
    if base is not None:
        try:
            resolved = urljoin(page_url, base['href'].strip())
            parsed = urlparse(resolved)
            if parsed.scheme in ('http', 'https') and parsed.netloc:
                return resolved
        except ValueError:
            pass
    return page_url


# D157 (P04 U4 п.4): адрес перехода, записанный не в href, а в обработчике onclick
# ("window.location.href='/catalog/x/'", "document.location=...") — так устроена
# навигация плиток каталога в части Bitrix-шаблонов (Москабельмет).
_ONCLICK_LOCATION_RE = re.compile(
    r"""location(?:\.href)?\s*=\s*['"]([^'"]+)['"]""", re.I)
# Обработчики «в корзину» / «сравнить» — это ДЕЙСТВИЕ, а не переход на страницу.
# В общем сборщике ссылок такие адреса до сих пор не появлялись вовсе, и пускать их
# в обход нельзя: '?cartAction=add&cartItem=7' балльная эвристика считает товаром
# (совпадение 'item=') и он занял бы слот товарной квоты.
_ACTION_URL_RE = re.compile(
    r'cartaction|addtocart|add2cart|add_to_cart|add2basket|action=add|compare=', re.I)


def extract_element_url(element) -> str:
    """D157: адрес перехода элемента, у которого нет href: onclick с присваиванием
    location, либо data-href / data-url. Возвращает '' , если адреса нет.
    Разбор статический — атрибут уже есть в HTML, отдельного прохода по DOM
    (в т.ч. в Playwright-ветке) не требуется."""
    try:
        value = ''
        for attr in ('data-href', 'data-url'):
            value = (element.get(attr) or '').strip()
            if value:
                break
        if not value:
            match = _ONCLICK_LOCATION_RE.search(element.get('onclick') or '')
            value = match.group(1).strip() if match else ''
        return '' if _ACTION_URL_RE.search(value) else value
    except Exception:
        return ''


class PageContent(str):
    """HTML с адресом конкретного ответа; str-совместимость для прежних потребителей."""
    requested_url: str
    final_url: str
    status: int
    headers: Dict[str, str]
    method: str

    def __new__(cls, html: str, requested_url: str, final_url: str,
                status: int = 200, headers: Optional[Dict[str, str]] = None,
                method: str = ''):
        value = super().__new__(cls, html)
        value.requested_url = requested_url
        value.final_url = final_url or requested_url
        value.status = status
        value.headers = dict(headers or {})
        value.method = method
        return value


def page_error_reason(content) -> Optional[str]:
    """Общий барьер перед обработкой HTML; номера моделей и текст статей не ошибки."""
    status = getattr(content, 'status', 200)
    if status != 200:
        return f'HTTP {status}'
    if not content or not content.strip():
        return 'empty'
    soup = BeautifulSoup(content, 'html.parser')
    if soup.find(id=re.compile(r'^(?:challenge-form|cf-challenge|cf-error-details)$', re.I)):
        return 'challenge'
    # Проверяем заголовки документа, а не произвольные упоминания в ссылках/описаниях.
    error = re.compile(
        r'^(?:error|ошибка)\s*[: -]?\s*(?:40[0348]|410|429|50[0-4])\s*[.!]?$|'
        r'^(?:(?:error|ошибка)\s*[: -]?\s*)?(?:40[0348]|410|429|50[0-4])'
        r'\s*[-:–—]?\s*(?:not found|gone|forbidden|bad request|too many requests|'
        r'internal server error|bad gateway|service unavailable|gateway time.?out|'
        r'страница не найдена|доступ запрещ[её]н)(?:\b|$)|'
        r'^(?:page not found|not found|access denied|forbidden|service unavailable|'
        r'bad gateway|internal server error|gateway time.?out|too many requests|'
        r'страница не найдена|страница удалена|доступ запрещ[её]н|сайт временно недоступен|'
        r'сайт на техническом обслуживании|технические работы)(?:\s*[.!:|–—-].*)?$', re.I)
    challenge = re.compile(
        r'^(?:just a moment|attention required|checking your browser|verify you are human|'
        r'проверяем браузер|проверка браузера|подтвердите, что вы человек)(?:[.!…]|\s|$)', re.I)
    headings = [' '.join(tag.stripped_strings) for tag in soup.find_all(['title', 'h1'])]
    # Короткий ответ сервера может быть обычным текстом без title/h1.
    if not headings:
        visible = ' '.join(soup.stripped_strings)
        if len(visible) <= 160:
            headings = [visible]
    for heading in headings:
        if challenge.search(heading):
            return 'challenge'
        if error.search(heading):
            return 'error_page'
    return None


def _track_document_response(page):
    """Статус основного документа, включая навигацию при клике по вкладке."""
    state = {'response': None}
    def received(response):
        if response.request.is_navigation_request() and response.frame == page.main_frame:
            state['response'] = response
            state.pop('failed_url', None)
    def failed(request):
        if request.is_navigation_request() and request.frame == page.main_frame:
            state['failed_url'] = request.url
    page.on('response', received)
    page.on('requestfailed', failed)
    return state


def _observe_browser_failure(url, method, error, state, allowed_url):
    """Блокировка редиректа до ответа — отказ периметра, а не сетевой сбой."""
    failed_url = state.get('blocked_url') or state.get('failed_url')
    if failed_url and allowed_url and not allowed_url(failed_url, base_url=url):
        observe_fetch(url, method, final_url=failed_url, reason='out_of_scope')
    else:
        observe_fetch(url, method, error=error)


def _browser_content(html, url, page, state, method):
    response = state['response']
    # Нет подтверждённого документа — не выдаём синтетический HTTP 200.
    status = response.status if response is not None else 0
    headers = getattr(response, 'headers', {}) if response is not None else {}
    content = PageContent(html or '', url, page.url, status, headers=headers, method=method)
    observe_fetch(url, method, status=status, final_url=page.url, headers=headers, html=content)
    return content


async def _release_browser_resources(pool, context, page, cleanup_guard):
    """После отмены не зависаем в finally; слот пула возвращается отдельно."""
    try:
        try:
            async with async_deadline(2):
                if cleanup_guard:
                    await cleanup_guard()
                if page:
                    await pool.close_page(page)
        except asyncio.TimeoutError:
            log.warning("Истёк срок очистки страницы/маршрута браузера")
    finally:
        if context:
            try:
                async with async_deadline(3):
                    await pool.return_browser(context)
            except asyncio.TimeoutError:
                log.warning("Истёк срок возврата контекста браузера")


@dataclass
class ParseResult:
    soup: BeautifulSoup
    html: str
    additional_links: List[Tuple[str, str, int]]
    content_type: str
    elements_found: Dict[str, int]
    url: str = ''                    # фактический адрес документа
    requested_url: str = ''          # адрес исходного запроса для аудита
    canonical_url: Optional[str] = None  # подсказка, не база относительных ссылок
    fetch_method: str = ''            # ступень, давшая принятый документ


@dataclass
class HttpFetchResult:
    """Результат HTTP-impersonate запроса (curl_cffi)."""
    html: Optional[str]
    status: int
    headers: Dict[str, str]          # ключи в нижнем регистре
    final_url: str                   # URL после редиректов
    cookies: Dict[str, str]          # собранные cookies (для warmup/переиспользования)
    from_cache_error: bool = False   # URL уже в кэше перманентных ошибок

class BrowserPool:
    """Пул браузеров для эффективного управления ресурсами Playwright"""
    
    # D101: потолок на закрытие контекста/браузера. Если процесс chromium умер,
    # Playwright-вызовы не возвращаются никогда — без этого потолка семафор пула
    # оставался захваченным навсегда и весь краул вставал в вечное ожидание.
    CLOSE_TIMEOUT = 30

    def __init__(self, headless: bool = True, max_concurrent_contexts: int = 20,
                 stealth_enabled: bool = False, stealth_user_agent: Optional[str] = None):
        self.headless = headless
        self.max_concurrent_contexts = max_concurrent_contexts
        self.stealth_enabled = stealth_enabled
        self.stealth_user_agent = stealth_user_agent
        self._playwright = None
        self._browser = None
        self._context_semaphore = asyncio.Semaphore(max_concurrent_contexts)
        
    async def initialize(self):
        """Инициализация: запуск Playwright и создание одного браузера."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-accelerated-2d-canvas',
                '--no-first-run',
                '--no-zygote',
                '--disable-web-security',
                '--disable-features=VizDisplayCompositor',
                '--ignore-certificate-errors'
            ]
        )
        
    async def get_browser(self):
        """
        Возвращает новый контекст (сессию) в общем браузере.
        Название метода сохранено для обратной совместимости.
        """
        await self._context_semaphore.acquire()
        context = None
        try:
            if self.stealth_enabled:
                # Реалистичный контекст вместо «голого» — снижает детект автоматизации
                # частью WAF/антибот-систем (UA/locale/viewport/timezone + маскировка webdriver).
                context = await self._browser.new_context(
                    user_agent=self.stealth_user_agent,
                    locale="ru-RU",
                    timezone_id="Europe/Moscow",
                    viewport={"width": 1920, "height": 1080},
                    service_workers="block",
                )
                try:
                    await context.add_init_script(
                        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                    )
                except Exception:
                    pass
            else:
                context = await self._browser.new_context(service_workers="block")
            return context
        except (Exception, asyncio.CancelledError):
            try:
                if context is not None:
                    async with async_deadline(3):
                        await context.close()
            except Exception as exc:
                log.warning(f"Контекст после отмены инициализации не закрылся: {exc}")
            finally:
                self._context_semaphore.release()
            raise
        
    async def return_browser(self, context):
        """
        Закрывает контекст и освобождает семафор.
        Название метода сохранено, хотя теперь принимает context.
        D101: закрытие под таймаутом — семафор освобождается в любом случае.
        """
        try:
            await asyncio.wait_for(context.close(), timeout=self.CLOSE_TIMEOUT)
        except Exception as e:
            log.warning(f"Контекст браузера не закрылся ({type(e).__name__}); семафор освобождаем принудительно")
        finally:
            self._context_semaphore.release()

    async def close_page(self, page) -> None:
        """Закрытие страницы под таймаутом — тот же класс, что context/browser (D101).
        page.close() у зависшего рендерера не возвращается никогда: задача страницы
        жила вечно, а главный цикл обхода ждал её в gather до сторожа (termal.biz,
        стенд 28.09.2026 — три задачи стояли в finally динамического экстрактора)."""
        try:
            await asyncio.wait_for(page.close(), timeout=self.CLOSE_TIMEOUT)
        except Exception as e:
            log.warning(f"Страница браузера не закрылась ({type(e).__name__}); "
                        f"продолжаем — контекст закроется под своим таймаутом")

    async def close(self):
        """Закрытие браузера и остановка Playwright (под таймаутом — см. D101)."""
        if self._browser:
            try:
                await asyncio.wait_for(self._browser.close(), timeout=self.CLOSE_TIMEOUT)
            except Exception as e:
                log.warning(f"Браузер не закрылся штатно: {type(e).__name__}")
        if self._playwright:
            try:
                await asyncio.wait_for(self._playwright.stop(), timeout=self.CLOSE_TIMEOUT)
            except Exception as e:
                log.warning(f"Playwright не остановился штатно: {type(e).__name__}")
        self._browser = None
        self._playwright = None

    async def recycle(self):
        """
        D101: пересоздание пула после аварийного таймаута компании.
        Старый браузер закрывается под таймаутом, семафор создаётся заново (разрешения,
        захваченные брошенными задачами, прощаются), затем поднимается новый браузер.
        Объект пула тот же — все ссылки на него (DynamicContentExtractor и пр.) остаются валидными.
        """
        await self.close()
        self._context_semaphore = asyncio.Semaphore(self.max_concurrent_contexts)
        await self.initialize()
        log.info("Пул браузеров пересоздан")

class DynamicContentExtractor:
    """
    Универсальный обработчик динамического контента (вкладки, аккордеоны).
    Автоматически определяет тип элементов и извлекает HTML после кликов.
    """

    def __init__(self, browser_pool: BrowserPool, config: Config, extra_tab_panel_selector: str = None,
                 guard_browser_page=None, allowed_url=None, accept_cookie_consent=None, tab_selector=None, ready_selector=None):
        self.browser_pool = browser_pool
        self.config = config
        # Пер-сайтовый селектор панели вкладки из профиля (crawl.tab_panel_selector) —
        # добавляется к глобальному списку config.dynamic_tab_panel_selectors.
        self.extra_tab_panel_selector = extra_tab_panel_selector
        self.guard_browser_page = guard_browser_page
        self.allowed_url = allowed_url
        self.accept_cookie_consent = accept_cookie_consent
        self.tab_selector = tab_selector
        self.ready_selector = ready_selector
        self.product_tabs_only = False  # автоэскалация не нажимает произвольные button

    async def extract(self, url: str) -> Union[List[PageContent], PageContent]:
        """
        Загружает страницу, определяет динамические элементы и возвращает:
        Полный снимок связанных панелей; для вкладок сохраняется совместимый
        список из одного HTML. Состояния не перезаписывают один URL по очереди.
        """
        if self.allowed_url and not self.allowed_url(url, base_url=url):
            return None
        context = await self.browser_pool.get_browser()
        page = None
        cleanup_guard = None
        state = {}
        self.incomplete_render = False

        try:
            page = await context.new_page()
            state = _track_document_response(page)
            if self.guard_browser_page:
                cleanup_guard = await self.guard_browser_page(page, url, navigation_state=state)
            # ИЗМЕНЕНО: wait_until='domcontentloaded', таймаут 20 сек
            response = await page.goto(url, wait_until='domcontentloaded', timeout=20000)
            state['response'] = response
            initial = _browser_content(await page.content(), url, page, state, 'dynamic')
            if page_error_reason(initial):
                return None
            # ДОБАВЛЕНО: короткий networkidle (3 сек) – необязателен
            try:
                await page.wait_for_load_state('networkidle', timeout=3000)
            except Exception:
                pass

            if self.allowed_url and not self.allowed_url(page.url, base_url=url):
                return None
            # Согласие может перезагрузить документ: готовность проверяем ПОСЛЕ него.
            if (self.accept_cookie_consent and needs_contact_interaction(
                    await page.content(), self.config.dynamic_tab_selectors + self.config.dynamic_accordion_selectors)):
                await self.accept_cookie_consent(page)
            if self.ready_selector:
                try:
                    await page.wait_for_selector(self.ready_selector, state='attached', timeout=10000)
                except PlaywrightTimeoutError:
                    self.incomplete_render = True
                    observe_fetch(url, 'dynamic', reason='incomplete_render')
                    return None
            # Дожидаемся окончания перестановки меню/списков после AJAX.
            try:
                await page.wait_for_function("""ready => {
                    if (ready && !document.querySelector(ready)) {
                        delete window.__crawlerContactReady;
                        return false;
                    }
                    const content = document.body.cloneNode(true);
                    content.querySelectorAll('script,style,jdiv,iframe').forEach(el => el.remove());
                    const signature = content.textContent + JSON.stringify(
                        Array.from(content.querySelectorAll('a[href]'), a => a.getAttribute('href')));
                    const now = performance.now();
                    const previous = window.__crawlerContactReady;
                    if (!previous || previous.signature !== signature) {
                        window.__crawlerContactReady = {signature, since: now};
                        return false;
                    }
                    return now - previous.since >= 1000;
                }""", arg=self.ready_selector, timeout=10000 if self.ready_selector else 5000, polling=250)
            except PlaywrightTimeoutError:
                self.incomplete_render = True
                observe_fetch(url, 'dynamic', reason='incomplete_render')
                return None
            # Определяем тип элементов
            if await self._has_tabs(page):
                # Вкладки: кликаем по каждой и собираем HTML
                results = await self._process_tabs(page, requested_url=url, response_state=state)
            else:
                # Аккордеоны: кликаем по всем и возвращаем финальный DOM
                try:
                    html = await self._process_accordions(page)
                except Exception:
                    self.incomplete_render = True
                    observe_fetch(url, 'dynamic', reason='incomplete_render')
                    return None
                if self.allowed_url and not self.allowed_url(page.url, base_url=url):
                    return None
                results = _browser_content(html, url, page, state, 'dynamic')
                if page_error_reason(results):
                    return None

            if self.ready_selector:
                snapshots = results if isinstance(results, list) else [results]
                if any(not BeautifulSoup(str(item), 'lxml').select_one(self.ready_selector) for item in snapshots):
                    self.incomplete_render = True
                    observe_fetch(url, 'dynamic', reason='incomplete_render')
                    return None
            return results
        except Exception as e:
            _observe_browser_failure(url, 'dynamic', e, state, self.allowed_url)
            log.error(f"Ошибка при обработке динамического контента {url}: {e}")
            return []
        finally:
            await _release_browser_resources(self.browser_pool, context, page, cleanup_guard)

    async def _has_tabs(self, page) -> bool:
        """Проверяет, есть ли на странице элементы, похожие на вкладки."""
        selectors = self.config.dynamic_tab_selectors
        for sel in selectors:
            elements = await page.query_selector_all(sel)
            if elements:
                # Дополнительная проверка: есть ли контейнер, который меняется при клике
                # Для простоты считаем, что если найден хотя бы один элемент, это вкладки
                log.debug(f"Найдены потенциальные вкладки по селектору {sel}")
                return True
        return False

    async def _process_tabs(self, page, requested_url: str = None, *, response_state) -> List[PageContent]:
        """Объединяет связанные AJAX-панели в один полный снимок страницы."""
        requested_url = requested_url or page.url
        try:
            html = await collect_contact_panels(page, self.config.dynamic_tab_selectors
                                               + self.config.dynamic_accordion_selectors)
        except Exception:
            self.incomplete_render = True
            failed_url = response_state.get('blocked_url') or response_state.get('failed_url')
            if failed_url and self.allowed_url and not self.allowed_url(failed_url, base_url=requested_url):
                observe_fetch(requested_url, 'dynamic', final_url=failed_url, reason='out_of_scope')
            else:
                observe_fetch(requested_url, 'dynamic', reason='incomplete_render')
            return []
        content = _browser_content(html, requested_url, page, response_state, 'dynamic')
        return [] if page_error_reason(content) else [content]

    async def extract_product_tabs_merged(self, url: str) -> Optional[PageContent]:
        """D01: товарная страница с AJAX-вкладками (опт-ин по домену).
        Кликает по каждой вкладке и СКЛЕИВАЕТ контент панелей в ОДИН HTML
        (базовый DOM + <section data-tab="..."> на вкладку). Один HTML → одна
        карточка со всеми характеристиками; отдельные #tab-страницы не создаются
        (иначе дедуп по имени товара терял характеристики — регресс РУФ-135).
        Возвращает склеенный HTML или None (страница не отдалась/вкладок нет)."""
        if self.allowed_url and not self.allowed_url(url, base_url=url):
            return None
        context = await self.browser_pool.get_browser()
        page = None
        cleanup_guard = None
        state = {}
        try:
            page = await context.new_page()
            state = _track_document_response(page)
            if self.guard_browser_page:
                cleanup_guard = await self.guard_browser_page(page, url, navigation_state=state)
            response = await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            state['response'] = response
            initial = _browser_content(await page.content(), url, page, state, 'ajax_tabs')
            if page_error_reason(initial):
                return None
            try:
                await page.wait_for_load_state('networkidle', timeout=5000)
            except Exception:
                pass

            if self.allowed_url and not self.allowed_url(page.url, base_url=url):
                return None
            base_html = _browser_content(await page.content(), url, page, state, 'ajax_tabs')
            if page_error_reason(base_html):
                return None

            if self.accept_cookie_consent:
                await self.accept_cookie_consent(page)

            try:
                panel_sections = await expand_product_panels(page)
            except Exception:
                failed_url = state.get('blocked_url') or state.get('failed_url')
                if failed_url and self.allowed_url and not self.allowed_url(failed_url, base_url=url):
                    observe_fetch(url, 'ajax_tabs', final_url=failed_url, reason='out_of_scope')
                else:
                    observe_fetch(url, 'ajax_tabs', reason='incomplete_render')
                return None
            if panel_sections:
                current = _browser_content(await page.content(), url, page, state, 'ajax_tabs')
                if page_error_reason(current):
                    return None
                block = '\n'.join(panel_sections)
                base_html = PageContent(str(base_html).replace('</body>', block + '</body>'),
                                        url, base_html.final_url, base_html.status, method='ajax_tabs')

            # Кандидаты-вкладки: видимые элементы по селекторам вкладок
            tabs = []
            selectors = ([PRODUCT_TAB_SELECTOR] if self.product_tabs_only else
                         [self.tab_selector] if self.tab_selector else self.config.dynamic_tab_selectors)
            for sel in selectors:
                for el in await page.query_selector_all(sel):
                    if el not in tabs:
                        tabs.append(el)
            if not tabs:
                return base_html

            _panel_list = list(getattr(self.config, 'dynamic_tab_panel_selectors',
                                       ["[role='tabpanel']", ".tab-content", ".tab-pane"]))
            if self.extra_tab_panel_selector and self.extra_tab_panel_selector not in _panel_list:
                _panel_list.append(self.extra_tab_panel_selector)
            panel_selectors = ", ".join(_panel_list)

            async def visible_panel_html(target_id=None) -> str:
                try:
                    return await page.evaluate(
                        """({sels, target}) => {
                            const els = target ? [document.getElementById(target)] : document.querySelectorAll(sels);
                            for (const el of els) {
                                if (!el) continue;
                                const r = el.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0 && el.innerText.trim().length > 0)
                                    return el.innerHTML;
                            }
                            return '';
                        }""", {'sels': panel_selectors, 'target': target_id})
                except Exception:
                    return ""

            sections = []
            seen_panels = {hash(await visible_panel_html())}
            base_page_url = page.url
            for idx, tab in enumerate(tabs):
                try:
                    if not await tab.is_visible():
                        continue
                    label = ((await tab.inner_text()) or "").strip()[:80]
                    if self.product_tabs_only and not PRODUCT_TAB_LABEL.search(label):
                        continue
                    target_id = None
                    if self.product_tabs_only:
                        target_id = await tab.get_attribute('aria-controls')
                        if not target_id:
                            ref = (await tab.get_attribute('data-bs-target')
                                   or await tab.get_attribute('data-target')
                                   or await tab.get_attribute('href') or '')
                            target_id = ref[1:] if ref.startswith('#') else None
                        if not target_id:
                            continue
                    await tab.click(timeout=5000)
                    await page.wait_for_timeout(self.config.dynamic_click_delay_ms)
                    # страховка: клик мог оказаться навигацией (меню/ссылка) — возвращаемся
                    if page.url != base_page_url:
                        log.debug(f"Клик по элементу {idx} увёл со страницы ({page.url}), возврат")
                        await page.go_back(wait_until='domcontentloaded', timeout=15000)
                        await page.wait_for_timeout(300)
                        continue
                    current = _browser_content(await page.content(), url, page, state, 'ajax_tabs')
                    if page_error_reason(current):
                        return None
                    panel = await visible_panel_html(target_id)
                    if page_error_reason(panel):
                        continue
                    h = (target_id, hash(panel)) if self.product_tabs_only else hash(panel)
                    if not panel or h in seen_panels:
                        continue  # клик не дал нового контента (не вкладка/дубль)
                    seen_panels.add(h)
                    sections.append(f'<section data-tab="{label}"><h2>{label}</h2>{panel}</section>')
                    log.info(f"Вкладка товара склеена ({idx+1}: {label!r}): {url}")
                except Exception as e:
                    log.warning(f"Ошибка при клике на вкладку товара {idx}: {e}")
                    observe_fetch(url, 'ajax_tabs', reason='incomplete_render')
                    return None

            if not sections:
                return base_html
            merged = base_html
            insert_at = merged.rfind("</body>")
            block = "\n".join(sections)
            if insert_at != -1:
                merged = merged[:insert_at] + block + merged[insert_at:]
            else:
                merged += block
            return PageContent(merged, url, base_html.final_url, base_html.status, method='ajax_tabs')
        except Exception as e:
            _observe_browser_failure(url, 'ajax_tabs', e, state, self.allowed_url)
            log.error(f"Ошибка при склейке вкладок товара {url}: {e}")
            return None
        finally:
            await _release_browser_resources(self.browser_pool, context, page, cleanup_guard)

    async def _process_accordions(self, page):
        """Сохраняет связанные панели; произвольные кнопки не нажимаются."""
        return await collect_contact_panels(page, self.config.dynamic_accordion_selectors)

class URLCategorizer:
    """Классификатор URL"""
    
    def __init__(self, config: Config = None):
        self.config = config
        self.domain_equivalency = DomainEquivalencyManager(config) if config else None
        self.product_url_patterns = [
            r'/product/', r'/item/', r'/tovar/', r'/goods/', r'/produkt/',
            r'/p/\d+', r'/sku/', r'/art/', r'/article/', r'/model/',
            r'/\d+\.html$', r'/\d+$', r'/[a-z0-9-]+-\d+', 
            r'_[a-z0-9]{6,}', r'/[a-z]{2,}\d{3,}', r'/\?product=', 
            # D271: после '/buy/' обязателен товарный слаг — голый '/buy/?id=8940'
            # (раздел «Где купить» kvil.ru) съедал квоту как товарная страница
            r'/buy/[a-z0-9-]+', r'/purchase/', r'/[a-z0-9-]+-\d+[a-z]*/',
            r'/productinfo/', r'/productdetail/', r'/product-detail/',
            r'/goods/', r'/ware/', r'/produkt/', r'/mah-\d+a-\d+(?:[.-]\d+)?om-m/?$/',
            # E-COMMERCE ПАТТЕРНЫ
            r'/shop/', r'/catalog/', r'/katalog/', r'/magazin/', r'/production/',
            r'/\w+/\d+-\w+',  # паттерн: /категория/123-название
            r'/\w+/\w+-\d+',  # паттерн: /категория/название-123
            r'/[a-z0-9-]+/[a-z0-9-]+$',  # паттерн: /категория/товар
            r'^/products/[a-z0-9\-/]+/[a-z0-9\-/]+/$', # Паттерн для товаров
            r'/products/(?:[a-z0-9-]+/){2,}[a-z0-9-]+/?$', # Паттерн для товаров
            r'/produktsiya/[^/]+/[^/]+/?$',
        ]
        
        # Расширения изображений для исключения
        self.image_extensions = [
            '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', 
            '.svg', '.ico', '.tiff', '.tif', '.eps', '.raw'
        ]

        # Поддерживаемые расширения файлов
        self.file_extensions = [
            '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.rtf'
        ]
        
        # Ключевые слова в URL для категоризации
        self.product_keywords = [
            'product', 'item', 'tovar', 'goods', 'produkt', 'ware', 'kabel'
            'sku', 'model', 'modell', 'variant', 'version', 'edition',
            'kauf', 'acquista', 'mah', 'shop', 'sale', 'materials',
            'productinfo', 'productdetail', 'product-detail', 'goodsinfo', 
            'catalog', 'katalog', 'magazin', 'магазин', 'каталог', 'production'
        ]
        
        self.category_keywords = [
            'category', 'catalog', 'catalogue', 'collection', 'kategoriya', 
            'katalog', 'series', 'серия', 'products', 'production', 'productions',
            'категория', 'каталог', 'sale', 'produkcija', 'produktsiya', 'plates',
            'systems', 'dopolnitelnoe-oborudovanie',
        ]
        
        self.contact_keywords = [
            'contact', 'contacts', 'контакты', 'company', 'requisites',
            'rekv', 'kontaktyi', 'ru/kontaktyi', 'kontakty', 
        ]
        
        self.distributor_keywords = [
            'distributor', 'dealer', 'where-to-buy', 'где-купить',
            'дистрибьютор', 'дилер', 'наши партнеры', 'partners', 'представительство',
            'партнеры', 'dealer-locator', 'where_to_buy', 'dealers', 'наши представительства',
            # D37: транслит-слаги и стемы — реальные разделы дилеров живут на
            # /about/predstavitelstva, /gde-kupit, /partnery и т.п.
            'predstavitel', 'gde-kupit', 'gde_kupit', 'kupit-v', 'tochki-prodazh',
            'points-of-sale', 'salespoints', 'partnery', 'dilery', 'diler',
            'представительств', 'где купить', 'точки продаж',
        ]
        
        self.price_list_keywords = [
            'price', 'pricelist', 'price-list', 'price_list', 'prices',
            'прайс', 'прайс-лист', 'прайс_лист', 'прайслист', 'цены', 'ценник',
            'стоимость', 'расценки', 'tariff', 'тариф', 'tseny', 'ceny', 'тарифы'
            'price', 'pricelist', 'price-list', 'price_list', 'prices', 'pricing',
            'скачать прайс', 'download price', 'price download', 'прайс скачать',
            'коммерческое предложение', 'commercial offer',
        ]

        self.main_page_indicators = ['', '/', 'index', 'main', 'home', 'главная']
        
        # Исключаемые паттерны
        self.exclude_url_patterns = [
            'service', 'services', 'news', 'article', 'articles', 'blog', 'about', 'policy', 'delivery',
            'blogs', 'post', 'posts', 'novosti', 'stati', 'blogi', 'press', 'forum', 'akcioneram', 'objects',
            'media', 'info', 'information', 'support', 'help', 'faq', 'howto', 'guide', 'tutorial', 'application',
            'discussion', 'comment', 'review', 'rating', 'tag', 'otzyv', 'files', 'place-start.ru', 'applying',
            'search', 'result', 'user', 'profile', 'account', 'login', 'register', 'portfolio', 'viran.ru',
            'cart', 'checkout', 'wishlist', 'compare', 'admin', 'dashboard', 'соглашение', 'webinar', 'reference',
            'logout', 'password', 'reset', 'subscription', 'newsletter', 'soglashenie', 'uslovija', 'reference_materials',
            'author', 'writer', 'publication', 'publish', 'zakaz', 'test', 'project', 'projects', 'ne-razobrannoe', 'usloviya',
            'pressa'
        ]

        # D58: подстрочное сопоставление отсекало товарные слаги, содержащие паттерн
        # внутри слова ('press' -> pressure/kompressor: 4 товара РАСКО у Саранского
        # приборостроительного не краулились). P04 U1 идёт дальше: сопоставляем не с
        # полным URL, а ПОСЕГМЕНТНО — сегмент целиком либо его часть, отделённая
        # дефисом ('/press/', 'press-tsentr' — да; 'pressure', 'service_product' — нет).
        # Хост из области сопоставления убран совсем (D139: зона '.info' вырезала сайт).
        # 'pressa' — отдельный токен, границей больше не ловится.
        self.exclude_url_patterns_set = set(self.exclude_url_patterns)

        self.exclude_language_prefixes = {
            'en', 'tr', 'de', 'fr', 'es', 'it', 'zh', 'ja', 'ko', 
            'en-us', 'en_us', 'en_uk', 'en-uk', 'en_us', 'en-us', 
            'en_gb', 'en-gb', 'eng', 'trk', 'deu', 'fra', 'esp', 
            'ita', 'zho', 'jpn', 'kor', 'uk', 'ua', 'be', 'kk', 'az', 
            'ro', 'pl', 'cs', 'sk', 'bg', 'hu', 'fi', 'sv', 'no', 'da', 
            'nl', 'pt', 'ar', 'he', 'hi', 'th', 'vi', 'id', 'ms', 'fil'
        }

        # Подмножество для проверки ПОДДОМЕНА (en.example.com) и СРЕДИННЫХ
        # сегментов пути (/catalog/en/p). Исключаем 'id'/'no' — это частые
        # нетоварные токены на рус. сайтах (идентификатор/номер), иначе отсекли
        # бы товарные URL вида /catalog/id/123 или /page/no/2. Первый сегмент
        # пути и query-параметры по-прежнему проверяются полным набором выше.
        self.exclude_language_segments = self.exclude_language_prefixes - {'id', 'no'}

        # Приоритеты категорий
        self.priority_levels = {
            'product': 9,
            'category': 8,
            'price_list': 6,
            'main_page': 7,
            'contacts': 7,
            'distributor': 7,
            'other': 1
        }
        
        # Признаки товарного контента
        self.content_indicators = {
            'price_indicators': ['₽', '$', '€', 'руб', 'цена', 'price', 'cost', 'стоимость', 'сумма'],
            'buy_buttons': ['купить', 'в корзину', 'buy', 'add to cart', 'cart', 'корзина'],
            'product_attrs': ['артикул', 'sku', 'art', 'модель', 'model', 'характеристики', 'specifications', 'код товара']
        }

        # === P04 U2: распознавание товарных и каталожных URL ===
        # Стемы разделов продукции: сопоставляются с НАЧАЛОМ сегмента пути (после
        # percent-декодирования), поэтому 'продукц' ловит '/продукция/', 'izdeliya' —
        # '/izdeliya/gajki/', 'produkcziya' — '/produkcziya/rigeli/' (D117, D223, D153).
        self.product_segment_stems = [
            'продукц', 'издели', 'товар', 'ассортимент',
            'produkciya', 'produkcia', 'produkcziya', 'produkcija', 'produktsiya',
            'izdeliya', 'izdelija', 'tovar', 'sizes', 'razmer', 'tiporazmer',
        ]
        # Каталожные стемы и точные сегменты. 'cat' — ТОЛЬКО целым сегментом (D274),
        # иначе он матчится внутри location/certificate/application.
        self.category_segment_stems = ['катал', 'категор']
        self.category_segment_tokens = {'cat'}
        # Товарные паттерны, сопоставляемые с нормализованным ПУТЁМ (не с полным URL):
        # ведущий числовой идентификатор ЧПУ PrestaShop '/28-fundamentnye-bloki' (D284).
        self.product_path_patterns = [
            r'^/\d+-[a-z0-9-]+$',
            r'/\d+-[a-z0-9-]+',
        ]
        # Идентификатор товара в query (D274 '/cat/?p_id=89', D127 '?product_ID=310')
        self._product_query_re = re.compile(
            r'(?:^|[?&])(?:p|product|tovar|item|goods|prod)_?id=\d+')
        # Листинговые параметры query — это КАТАЛОГ, а не товар (D127 '?categoryID=94')
        self._listing_query_re = re.compile(
            r'(?:^|[?&])(?:pt_id|c_id|cpath|categoryid|category_id|cat_id|'
            r'section_id|sectionid|group_id|grp)=')
        # Служебные сегменты, которые НИКОГДА не товар. Сверяются и с сегментами пути,
        # и с именем последнего сегмента без расширения, поэтому '/sitemap.html'
        # больше не товар (D145), как и '/shoppingcart' (D237).
        self.service_segments = {
            'cart', 'basket', 'korzina', 'order', 'checkout', 'oformlenie',
            'personal', 'auth', 'login', 'register', 'compare', 'sravnenie',
            'eshop_app', 'search', 'poisk',
            'sitemap', 'sitemaps', 'shoppingcart', 'shopping-cart', 'wishlist',
        }
        # Якоря ссылки, прямо указывающие на карточку товара (D228). Список намеренно
        # узкий: одиночное «подробнее» стоит и у новостей, и у статей.
        self.product_anchor_markers = [
            'подробнее о товаре', 'подробнее о продукте', 'подробнее о продукции',
            'подробнее о материале', 'описание товара', 'карточка товара',
            'технические характеристики', 'характеристики',
            'смотреть товар', 'перейти к товару',
        ]
        # Сегменты, не считающиеся товарными на «плоском» сайте (см. observe_urls)
        self.flat_site_stop_segments = self.service_segments | {
            'index', 'main', 'home', 'about', 'contacts', 'contact', 'news',
            'map', 'gb', 'price', 'faq', 'forum', 'blog', 'info', 'help',
            'glavnaya', 'главная', 'novosti', 'kontakty', 'o-nas', 'karta-sayta',
        }
        # P04 U3 (D228): шаблонные служебные пути uCoz — гостевая книга, регистрация и
        # страницы-заглушки '/index/0-N'. Товарами они не бывают никогда.
        self._ucoz_service_re = re.compile(r'^/(?:index/\d+-\d+|gb|register)(?:/|$)')
        # === P04 U4: листинг против карточки (D100) ===
        # Пагинация листинга в query (?PAGEN_1=2, ?page=3) и в пути (/page/2, /stranica-3)
        self._pagination_query_re = re.compile(
            r'(?:^|&)(?:page|pagen(?:_\d+)?|paging|pg|start|offset)=\d+')
        self._pagination_path_re = re.compile(r'/(?:page|pagen|stranica)[-_/]?\d+$')
        # Числовой идентификатор в query у скриптовых карточек ('detail.php?ID=123').
        # Листинговые параметры (categoryID, SECTION_ID) отсекаются раньше — признаком
        # листинга, поэтому здесь достаточно общей формы.
        self._generic_id_query_re = re.compile(r'(?:^|&)[a-z_]*id=\d+')
        # D271: раздел «Где купить» одним сегментом пути. Подстрокой такое слово брать
        # нельзя ('/buyer/product.php' — карточка), поэтому сверяется сегмент целиком.
        self.distributor_segments = {'buy'}
        # Признак «плоского» сайта: заполняется observe_urls() по набору URL хоста
        self.flat_site = False
        # Ключевые слова товаров/категорий сопоставляем ПО ЛЕВОЙ ГРАНИЦЕ ТОКЕНА, а не
        # произвольной подстрокой: 'item' больше не матчится внутри 'sitemap' (D145).
        # Правую границу НЕ требуем намеренно: иначе отвалились бы живые товарные
        # разделы '/products', '/catalogitems/', '/produkty' (-1400 товарных URL на
        # корпусе прогона). Служебные 'shoppingcart'/'sitemap.html' отсекаются раньше
        # стоп-листом service_segments (D237).
        self._product_keywords_re = self._compile_keyword_prefixes(self.product_keywords)
        self._category_keywords_re = self._compile_keyword_prefixes(self.category_keywords)

        # === P04 U1: область применения фильтров, вырезающих сайт целиком ===
        # Отраслевые омонимы: вырезают URL, ТОЛЬКО совпав с сегментом целиком
        # ('/profile/', '/media/'). Внутри составного сегмента это обычные отраслевые
        # слова: 'profile-catalog' — каталог ПВХ-профиля (D207), а не личный кабинет.
        self.exclude_exact_segment_only = {
            'profile', 'application', 'objects', 'files', 'media',
            'test', 'reference', 'project', 'projects',
        }
        # Маршрутные ключи CMS: их ЗНАЧЕНИЯ в exclude-сопоставлении не участвуют
        # (Joomla 'view=article' красила в excluded2 100 % страниц сайта — D197)
        self.cms_route_query_keys = {'option', 'view', 'task', 'layout', 'tmpl',
                                     'route', 'r', 'do', 'p', 'id'}
        # Явные каталожные маркеры пути: при них exclude-токен не вырезает URL, а лишь
        # понижает приоритет (D158 '/info/catalog/', D207, D251). Список намеренно
        # узкий — отраслевые омонимы товарных ключевых слов ('model', 'sale', 'ware')
        # сюда класть НЕЛЬЗЯ, иначе exclude перестанет работать на новостях.
        self.catalog_signal_stems = [
            'catalog', 'katalog', 'product', 'produkt', 'produkc', 'produkts',
            'tovar', 'izdeli', 'assortiment',
            'каталог', 'продукц', 'товар', 'издели', 'ассортимент',
        ]
        self._catalog_signal_re = self._compile_keyword_prefixes(self.catalog_signal_stems)
        # Базовая локаль обхода (set_base_locale): у зарубежного производителя
        # национальная версия сайта и есть весь сайт (D211, D260)
        self.base_locale = None
        # Предохранитель «фильтр съел сайт» (release_starved_filters)
        self.exclude_filter_disabled = False
        self.language_filter_disabled = False
        self._urls_categorized = 0
        self._lang_excluded = 0
        self._pattern_excluded = 0
        self._deferred_urls = []
        self._deferred_seen = set()

    @staticmethod
    def _compile_keyword_prefixes(keywords) -> 're.Pattern':
        """Регэксп «ключевое слово с левой границы токена»: слева от слова не должно
        быть буквы или цифры (начало сегмента пути, '-', '_', '?', '&')."""
        return re.compile("|".join(r"(?<![a-z0-9а-яё])" + re.escape(k) for k in keywords))

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Путь URL, подготовленный к сопоставлению: percent-декодирование, casefold
        и снятие завершающего слеша (D117, D145). Корень остаётся '/'."""
        try:
            decoded = unquote(path or '')
        except Exception:
            decoded = path or ''
        decoded = decoded.casefold()
        if not decoded.startswith('/'):
            decoded = '/' + decoded
        return decoded.rstrip('/') or '/'

    def observe_urls(self, urls) -> bool:
        """Сигнал «плоский сайт» (D145, D228, D237): если ни один URL хоста не лежит
        под товарным или каталожным сегментом, сайт считается плоским — односегментные
        слаги-директории и листы .htm/.html/.shtml в корне начинают учитываться как
        товарные кандидаты. Вызывается один раз на компанию по уже известному набору
        URL (карта сайта / ссылки главной). Возвращает значение флага."""
        observed = 0
        stems = self.product_segment_stems + self.category_segment_stems
        for url in urls:
            try:
                path_norm = self._normalize_path(urlparse(url).path)
            except Exception:
                continue
            segments = [seg for seg in path_norm.split('/') if seg]
            if not segments:
                continue
            observed += 1
            if (self._product_keywords_re.search(path_norm)
                    or self._category_keywords_re.search(path_norm)
                    or any(seg.startswith(stem) for seg in segments for stem in stems)
                    or any(seg in self.category_segment_tokens for seg in segments)):
                self.flat_site = False
                return False
        min_urls = getattr(self.config, 'flat_site_min_urls', 5) if self.config else 5
        self.flat_site = observed >= min_urls
        if self.flat_site:
            log.info(f"Признак «плоский сайт»: товарных/каталожных сегментов не найдено "
                     f"на {observed} URL — односегментные слаги считаем товарными кандидатами")
        return self.flat_site

    # ==================== P04 U1: область применения фильтров ====================

    def set_base_locale(self, locale: Optional[str]) -> None:
        """Базовая локаль обхода (D211, D260): языковой префикс рабочего URL компании.
        URL с этим префиксом языковой фильтр не режет — у зарубежного производителя
        национальная версия и есть весь сайт."""
        self.base_locale = (locale or '').lower() or None
        if self.base_locale:
            log.info(f"Базовая локаль обхода: /{self.base_locale}/ — языковой фильтр её не режет")

    def detect_locale_prefix(self, url: str) -> Optional[str]:
        """Первый сегмент пути, если он выглядит языковым префиксом ('/pl/', '/en-US/'),
        иначе None. Нужен, чтобы вычислить базовую локаль по URL компании."""
        try:
            segments = [seg for seg in urlparse(url).path.split('/') if seg]
        except Exception:
            return None
        if not segments:
            return None
        first = segments[0].lower()
        base = first.replace('_', '-').split('-')[0]
        if first in self.exclude_language_prefixes or base in self.exclude_language_prefixes:
            return first
        return None

    def _is_base_locale(self, segment: str) -> bool:
        """Сегмент совпадает с базовой локалью сайта, включая формы pl-PL / pl_PL."""
        if not self.base_locale:
            return False
        base = self.base_locale.replace('_', '-').split('-')[0]
        return segment.replace('_', '-').split('-')[0] == base

    def _segment_hits_exclude(self, token: str) -> bool:
        """Сегмент пути (или значение query) совпал с exclude-токеном: целиком либо
        частью, отделённой дефисом. '_' границей НЕ считается (D158 'service_product',
        D283 'product_info.php'), отраслевые омонимы — только целым сегментом (D207)."""
        if not token:
            return False
        if token in self.exclude_url_patterns_set:
            return True
        if '-' in token:
            return any(part in self.exclude_url_patterns_set
                       and part not in self.exclude_exact_segment_only
                       for part in token.split('-'))
        return False

    def _is_excluded_by_patterns(self, segments: List[str], query: str) -> bool:
        """Глобальный exclude-список, применённый ТОЛЬКО к сегментам пути и к значениям
        query (P04 U1, пп. 1-2). Хост в сопоставлении не участвует (D139: зона '.info'
        вырезала весь сайт), последний сегмент берётся без расширения, значения
        маршрутных ключей CMS пропускаются (D197: 'view=article')."""
        if self.exclude_filter_disabled:
            return False
        last = len(segments) - 1
        for i, segment in enumerate(segments):
            token = segment.rsplit('.', 1)[0] if (i == last and '.' in segment) else segment
            if self._segment_hits_exclude(token):
                return True
        for part in query.split('&'):
            if not part:
                continue
            key, _, value = part.partition('=')
            if key.strip().lower() in self.cms_route_query_keys or not value:
                continue
            try:
                value = unquote(value)
            except Exception:
                pass
            if self._segment_hits_exclude(value.casefold()):
                return True
        return False

    def _has_catalog_signal(self, path_norm: str, text_lower: str = '') -> bool:
        """Явный каталожный/товарный маркер в пути или в тексте ссылки ('/catalog/',
        '/produkt/', 'profile-catalog', '/produkcziya/', якорь «Продукция»). При нём
        exclude-токен не вырезает URL, а только понижает приоритет (P04 U1, п. 3)."""
        if self._catalog_signal_re.search(path_norm):
            return True
        return bool(text_lower) and bool(self._catalog_signal_re.search(text_lower))

    def _defer_excluded(self, url: str, link_text: str, kind: str) -> None:
        """Копим отброшенные URL, чтобы вернуть их в обход, если окажется, что фильтр
        вырезал весь сайт (P04 U1, п. 5)."""
        if kind == 'lang':
            self._lang_excluded += 1
        else:
            self._pattern_excluded += 1
        limit = getattr(self.config, 'exclude_failopen_max_deferred', 500) if self.config else 500
        if len(self._deferred_urls) >= limit or url in self._deferred_seen:
            return
        self._deferred_seen.add(url)
        self._deferred_urls.append((url, link_text))

    def release_starved_filters(self) -> Tuple[bool, List[Tuple[str, str]]]:
        """Предохранитель «фильтр вырезал весь сайт» (P04 U1, п. 5). Если доля URL,
        отсеянных языковым фильтром или глобальным exclude-списком, достигла порога
        config.exclude_failopen_ratio, фильтр отключается до конца компании. Возвращает
        (сработал ли, отложенные URL для переклассификации). Зовётся, когда очередь
        обхода опустела — то есть фильтр действительно оставил краулер без работы."""
        if self.exclude_filter_disabled and self.language_filter_disabled:
            return False, []
        total = self._urls_categorized
        min_urls = getattr(self.config, 'exclude_failopen_min_urls', 20) if self.config else 20
        ratio = getattr(self.config, 'exclude_failopen_ratio', 0.8) if self.config else 0.8
        if total < min_urls:
            return False, []
        released = False
        if not self.language_filter_disabled and self._lang_excluded / total >= ratio:
            self.language_filter_disabled = True
            released = True
            log.warning(f"Языковой фильтр отсеял {self._lang_excluded} из {total} URL и оставил "
                        f"обход без работы — отключаем его для этой компании (fail-open)")
        if not self.exclude_filter_disabled and self._pattern_excluded / total >= ratio:
            self.exclude_filter_disabled = True
            released = True
            log.warning(f"Глобальный exclude-список отсеял {self._pattern_excluded} из {total} URL "
                        f"и оставил обход без работы — отключаем его для этой компании (fail-open)")
        if not released:
            return False, []
        deferred = self._deferred_urls
        self._deferred_urls, self._deferred_seen = [], set()
        return True, deferred

    def filter_stats(self) -> Dict[str, Any]:
        """Доли URL, отсеянных фильтрами по домену — для profile_metrics (P04 U1, п. 5)."""
        total = self._urls_categorized
        return {
            'urls_categorized': total,
            'excluded2_rate': round(self._pattern_excluded / total, 3) if total else None,
            'lang_excluded_rate': round(self._lang_excluded / total, 3) if total else None,
            'exclude_filter_disabled': self.exclude_filter_disabled,
            'language_filter_disabled': self.language_filter_disabled,
        }

    # ==================== P04 U3: порядок проверок и тупиковые роли ==============

    def _has_strong_product_path(self, segments: List[str]) -> bool:
        """Сильный товарный сигнал пути: сегмент product/tovar/item/produkt/goods, за
        которым идёт идентификатор товара. Такой путь сильнее префикса '/price/'
        ('/price/product/235255' — карточка, а не прайс-лист; D181)."""
        strong = {'product', 'products', 'tovar', 'tovary', 'item', 'items',
                  'produkt', 'produkty', 'goods'}
        return any(seg in strong and segments[i + 1]
                   for i, seg in enumerate(segments[:-1]))

    def _is_flat_product_candidate(self, segments: List[str]) -> bool:
        """Односегментный слаг-директория или лист .htm/.html/.shtml в корне — типовая
        форма товарной страницы «плоского» сайта, за вычетом служебного стоп-листа."""
        if len(segments) != 1:
            return False
        name = segments[0]
        if '.' in name:
            base, ext = name.rsplit('.', 1)
            if not base or ext not in ('htm', 'html', 'shtml'):
                return False
            name = base
        return bool(name) and name not in self.flat_site_stop_segments

    # ==================== P04 U4: листинг против карточки =======================

    def is_listing_url(self, url: str) -> bool:
        """Форма URL говорит «листинг каталога», а не «карточка товара» (D100):
        пагинация в пути или в query, листинговый параметр (categoryID и т. п.) либо
        путь-раздел — последний сегмент без товарного слага (ни дефиса, ни цифры, ни
        расширения). Признак РОЛЬ НЕ МЕНЯЕТ: страница по-прежнему обходится, сохраняется
        и отдаёт ссылки; он влияет только на приоритет очереди и на резерв товарной
        квоты. Сознательно узкий: '/catalog/truboprovodnaya-armatura/' с дефисом в
        последнем сегменте листингом не считается — недобор безопаснее перебора."""
        if product_state_key(url):
            return False
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        query = parsed.query.lower()
        path_norm = self._normalize_path(parsed.path)
        # Идентификатор товара в query сильнее формы пути: '/cat/?p_id=89' — карточка
        if self._product_query_re.search(query):
            return False
        if (self._pagination_query_re.search(query)
                or self._pagination_path_re.search(path_norm)
                or self._listing_query_re.search(query)):
            return True
        segments = [seg for seg in path_norm.split('/') if seg]
        if not segments:
            return False
        last = segments[-1]
        if '.' in last:
            return False
        return not ('-' in last or any(ch.isdigit() for ch in last))

    def is_product_card_url(self, url: str) -> bool:
        """Похоже на КАРТОЧКУ товара: идентификатор товара в query (в т.ч. голый
        '?ID=123' скриптовых карточек Bitrix — листинговые параметры отсеиваются
        раньше признаком листинга), либо путь глубины >= 2 с товарным слагом в
        последнем сегменте (дефис или цифра), либо односегментный лист
        .htm/.html/.shtml (форма карточки «плоского» сайта).
        Используется только для резерва доли товарной квоты (P04 U4, п. 2)."""
        try:
            parsed = urlparse(url)
        except Exception:
            return False
        query = parsed.query.lower()
        if self._product_query_re.search(query):
            return True
        if self.is_listing_url(url):
            return False
        if self._generic_id_query_re.search(query):
            return True
        segments = [seg for seg in self._normalize_path(parsed.path).split('/') if seg]
        if not segments:
            return False
        last = segments[-1]
        base, _, ext = last.rpartition('.') if '.' in last else (last, '', '')
        if len(segments) == 1:
            return ext in ('htm', 'html', 'shtml') or self.flat_site
        return '-' in base or any(ch.isdigit() for ch in base)

    def calculate_url_depth(self, url: str) -> int:
        """Вычисление глубины URL на основе количества слэшей в пути"""
        try:
            parsed = urlparse(url)
            path = parsed.path
            clean_path = path.strip('/')            
            
            if not clean_path:
                return 0            
            
            segments = [seg for seg in clean_path.split('/') if seg]            
            return len(segments)
            
        except Exception as e:
            log.warning(f"Ошибка вычисления глубины URL {url}: {e}")
            return 0
        
    def extract_main_domain(self, domain: str) -> str:
        return self.domain_equivalency.normalize_domain(domain) if self.domain_equivalency else domain
    
    def is_main_domain(self, url: str, base_url: str) -> bool:
        """Проверяет, принадлежит ли URL основному домену"""
        try:
            if (self.domain_equivalency and 
                self.config.domain_equivalency_enabled):
                return self.domain_equivalency.are_domains_equivalent(url, base_url)            
            
        except Exception:
            return False
    
    def is_subdomain(self, url: str, base_url: str) -> bool:
        """Проверяет, является ли URL поддоменом основного домена"""
        if not self.config or not self.config.ignore_subdomains:
            return False
            
        try:
            if (self.domain_equivalency and 
                self.config.domain_equivalency_enabled):
                
                url_domain = urlparse(url).netloc.lower()
                base_domain = urlparse(base_url).netloc.lower()
                
                # Если домены одинаковые - это не поддомен
                if url_domain == base_domain:
                    return False
                
                # Проверяем, заканчивается ли домен url на base_domain
                # Пример: sub.example.com заканчивается на .example.com
                normalized_url_domain = self.domain_equivalency.normalize_domain(url_domain)
                normalized_base_domain = self.domain_equivalency.normalize_domain(base_domain)
                
                # Если домены эквивалентны после нормализации - это не поддомен
                if normalized_url_domain == normalized_base_domain:
                    return False
                
                # Проверяем, является ли поддоменом (D180: сравниваем A-label формы,
                # иначе поддомен IDN-сайта не распознаётся)
                return normalized_url_domain.endswith('.' + normalized_base_domain)
            
        except Exception:
            return False
        
    def is_downloadable_file(self, url: str) -> bool:
        """Проверяет, является ли URL ссылкой на файл для скачивания"""
        try:
            parsed = urlparse(url)
            path = parsed.path.lower()
            
            # Проверяем расширения файлов в пути
            for ext in self.file_extensions:
                if path.endswith(ext):
                    ext_index = path.rfind(ext)
                    if ext_index > 0 and path[ext_index-1] == '.':
                        return True
            
            # Проверяем паттерны файлов в URL
            file_patterns = [
                '/download/', '/file/', '/attachment/', '/documents/', 'pdf'
            ]
            
            if any(pattern in url.lower() for pattern in file_patterns):
                return True
                
            return False
        except Exception as e:
            log.warning(f"Ошибка проверки файла URL {url}: {e}")
            return False

    def _is_same_domain(self, url: str, base_domain: str, strict: bool = None) -> bool:
        """Проверяет, принадлежит ли URL тому же домену"""
        try:
            if strict is None:
                strict = not (self.config and self.config.ignore_subdomains)

            url_domain = urlparse(url).netloc
            base_domain_parsed = urlparse(base_domain).netloc

            if strict:
                return url_domain == base_domain_parsed
            else:
                return self.is_main_domain(url, base_domain)
        except Exception:
            return False

    def is_print_version(self, url: str, category: str = None) -> bool:
        """
        Проверяет, является ли URL печатной версией страницы (только для продуктов).
        Если category == 'product' (или не указана и URL продукта), выполняет проверку.
        """
        if self.config and not getattr(self.config, 'filter_print_versions', True):
            return False

        if category is not None and category != 'product':
            return False

        try:
            parsed = urlparse(url)
            path_lower = parsed.path.lower()
            query_params = parse_qs(parsed.query)

            # Проверка по путям
            if self.config:
                print_paths = self.config.print_version_paths
            else:
                print_paths = ['/print/', '/print-version/', '/print_page/', '/printview/', '/printable/']
            for p in print_paths:
                if p in path_lower:
                    log.debug(f"Печатная версия по пути '{p}': {url}")
                    return True

            # Проверка по параметрам
            if self.config:
                params = self.config.print_version_params
                values = self.config.print_version_values
            else:
                params = ['print', 'view', 'mode', 'format', 'action']
                values = ['y', '1', 'on', 'yes', 'true', 'print']

            for param in params:
                if param in query_params:
                    val = query_params[param][0].lower()
                    if val in values:
                        log.debug(f"Печатная версия по параметру {param}={val}: {url}")
                        return True

            return False

        except Exception as e:
            log.warning(f"Ошибка проверки печатной версии для {url}: {e}")
            return False
        
    def should_exclude_by_language(self, url: str) -> bool:
        """Проверяет, относится ли URL к иноязычной версии сайта.

        Исключает:
          1. языковой поддомен — en.example.com, de.example.com, en-us.example.com;
          2. языковой префикс в первом сегменте пути — /en/, /en-us/catalog;
          3. языковой сегмент в любом месте пути — /catalog/en/product;
          4. язык в query-параметрах — ?lang=en, ?locale=de_DE.
        Поддомен и срединные сегменты (п.1, п.3) матчатся курированным набором
        exclude_language_segments (без 'id'/'no'); первый сегмент и query —
        полным exclude_language_prefixes.
        Базовая локаль сайта (set_base_locale) из проверки исключается — D211/D260.
        """
        if self.language_filter_disabled:
            return False
        try:
            parsed = urlparse(url)

            # 1. Языковой поддомен: en.example.com (>=3 меток — значит это не apex-домен)
            host = parsed.netloc.lower().split(':')[0]
            labels = host.split('.')
            if len(labels) >= 3 and labels[0] in self.exclude_language_segments:
                return True

            path = parsed.path.strip('/')
            if path:
                segments = path.split('/')

                # 2. Первый сегмент пути (полный набор + составные en-US, en_US);
                #    базовую локаль сайта пропускаем (P04 U1: '/pl/' у bolix.pl — не
                #    иноязычная версия, а весь сайт)
                first_segment = segments[0].lower()
                if not self._is_base_locale(first_segment):
                    if first_segment in self.exclude_language_prefixes:
                        return True
                    if '-' in first_segment and first_segment.split('-')[0] in self.exclude_language_prefixes:
                        return True
                    if '_' in first_segment and first_segment.split('_')[0] in self.exclude_language_prefixes:
                        return True

                # 3. Языковой сегмент в любом месте пути: /catalog/en/product
                for seg in segments[1:]:
                    if seg.lower() in self.exclude_language_segments and not self._is_base_locale(seg.lower()):
                        return True

            # 4. Язык в query-параметрах
            query_params = parse_qs(parsed.query)
            for param in ('lang', 'language', 'locale'):
                if param in query_params:
                    value = query_params[param][0].lower()
                    # Язык может быть указан как 'en', 'en-US', 'en_US'
                    base_lang = value.split('-')[0].split('_')[0]
                    if base_lang in self.exclude_language_prefixes:
                        log.debug(f"Исключаем URL по параметру {param}={value}: {url}")
                        return True

            return False

        except Exception as e:
            log.warning(f"Ошибка проверки языка для {url}: {e}")
            return False
                    
    def set_profile(self, profile) -> None:
        """Профиль сайта (site_profiles.SiteProfile) текущей компании: пер-сайтовая
        карта разделов (sections) и exclude_paths. None = без профиля (как раньше)."""
        self.profile = profile
        self._profile_product_res = []
        self._profile_antipattern_res = []
        # Пер-компанийное состояние категоризатора (признак плоского сайта, базовая
        # локаль, счётчики и предохранитель фильтров) — сбрасываем вместе с профилем:
        # set_profile зовётся из _apply_site_profile на каждую компанию.
        self.flat_site = False
        self.base_locale = None
        self.exclude_filter_disabled = False
        self.language_filter_disabled = False
        self._urls_categorized = 0
        self._lang_excluded = 0
        self._pattern_excluded = 0
        self._deferred_urls = []
        self._deferred_seen = set()
        if profile is None:
            return
        for pattern in profile.sections.product_url_patterns:
            try:
                self._profile_product_res.append(re.compile(pattern, re.I))
            except re.error as e:
                log.warning(f"Профиль {profile.domain}: битый regex product_url_patterns "
                            f"{pattern!r}: {e}")
        for pattern in profile.sections.product_url_antipatterns:
            try:
                self._profile_antipattern_res.append(re.compile(pattern, re.I))
            except re.error as e:
                log.warning(f"Профиль {profile.domain}: битый regex product_url_antipatterns "
                            f"{pattern!r}: {e}")

    @staticmethod
    def _longest_prefix(path: str, prefixes) -> int:
        """Длина самого длинного префикса из списка, матчащего путь ('/contacts/'
        матчит /contacts/ и подстраницы), или -1 если ни один не матчит."""
        best = -1
        for prefix in prefixes:
            p = prefix.lower()
            if not p.startswith('/'):
                p = '/' + p
            if path == p or path == p.rstrip('/') or path.startswith(p if p.endswith('/') else p + '/'):
                best = max(best, len(p.rstrip('/')))
        return best

    def profile_document_section(self, url: str) -> Optional[str]:
        """Блок общих документов компании из профиля для URL:
        'certificates' | 'documents' | 'instructions' | 'price_list' (по самому
        длинному префиксу из 4 списков sections) или None (страница не документная
        либо профиля нет)."""
        prof = getattr(self, 'profile', None)
        if prof is None:
            return None
        path = urlparse(url).path.lower()
        best_len, best_section = -1, None
        for prefixes, section in ((prof.sections.certificates_urls, 'certificates'),
                                  (prof.sections.documents_urls, 'documents'),
                                  (prof.sections.instructions_urls, 'instructions'),
                                  (prof.sections.price_list_urls, 'price_list')):
            match_len = self._longest_prefix(path, prefixes)
            if match_len > best_len:
                best_len, best_section = match_len, section
        return best_section if best_len >= 0 else None

    def categorize_url(self, url: str, link_text: str = "") -> Tuple[str, int]:
        """Категоризация URL с возвратом категории и приоритета"""
        self._urls_categorized += 1
        # Проверяем на языковые префиксы
        if self.should_exclude_by_language(url):
            log.debug(f"Исключаем URL по языковому префиксу: {url}")
            self._defer_excluded(url, link_text, 'lang')
            return 'excluded', 0
        
        parsed = urlparse(url)
        # Сначала проверяем на файлы для скачивания
        if self.is_downloadable_file(url):
            return 'excluded', 0
        
        # Затем проверяем на изображения
        if self._is_image_url(url):
            return 'excluded1', 0
        
        url_lower = url.lower()
        text_lower = link_text.lower()
        # D123: ключевые слова ищем в ПУТИ, а не в полном URL. Поддомены краулер и так не
        # обходит, поэтому хост одинаков для всех страниц сайта — слово внутри доменного
        # имени красит сайт целиком в одну категорию (floordealer.ru -> 'dealer': все 420
        # страниц Beaulieu of America ушли в distributor, product_pages=0).
        path_lower = parsed.path.lower() + (('?' + parsed.query.lower()) if parsed.query else '')
        # P04 U2: нормализованный путь и его сегменты для сопоставления по границам
        # сегмента (percent-декодирование + casefold + без завершающего слеша)
        path_norm = self._normalize_path(parsed.path)
        path_segments = [seg for seg in path_norm.split('/') if seg]

        # D69: пер-доменное исключение разделов (скоуплено по netloc, НЕ глобально).
        # Пустой словарь для прочих доменов => цикл ничего не делает, регресса нет.
        _by_domain = getattr(self.config, 'exclude_url_patterns_by_domain', None) if self.config else None
        if _by_domain:
            _netloc = parsed.netloc.lower()
            if _netloc.startswith('www.'):
                _netloc = _netloc[4:]
            _path = parsed.path.lower()
            for _dom, _pats in _by_domain.items():
                if (_netloc == _dom or _netloc.endswith('.' + _dom)) and any(p in _path for p in _pats):
                    log.debug(f"D69: исключаем пер-доменный раздел {url}")
                    return 'excluded', 0

        # Профиль сайта: пер-доменные exclude_paths и карта разделов (sections).
        # Профиля нет — блок не выполняется вообще (нулевой регресс). Прямые матчи
        # секций сильнее глобальных exclude-паттернов: их назначение — дотянуться до
        # страниц реквизитов/документов, которые общие эвристики исключают или не видят.
        profile_not_product = False
        _prof = getattr(self, 'profile', None)
        if _prof is not None:
            _p_path = parsed.path.lower()
            if any(p in _p_path for p in (s.lower() for s in _prof.crawl.exclude_paths)):
                log.debug(f"Профиль {_prof.domain}: исключаем раздел {url}")
                return 'excluded', 0
            profile_not_product = any(r.search(url_lower) for r in self._profile_antipattern_res)
            if not profile_not_product and any(r.search(url_lower) for r in self._profile_product_res):
                return 'product', self.priority_levels['product']
            _sec = _prof.sections
            # Прямые матчи секций; при пересечении префиксов побеждает САМЫЙ ДЛИННЫЙ
            # (пример belcolor: contacts_urls=['/contacts/'], distributor_urls=
            # ['/contacts/branches/'] -> /contacts/branches/x = distributor).
            # Документы: отдельной категории нет — краулим как 'other' с приоритетом контактов.
            _best_len, _best_result = -1, None
            for _prefixes, _sec_cat, _sec_prio in (
                    (_sec.contacts_urls, 'contacts', self.priority_levels['contacts']),
                    (_sec.distributor_urls, 'distributor', self.priority_levels['distributor']),
                    (_sec.price_list_urls, 'price_list', self.priority_levels['price_list']),
                    (_sec.certificates_urls, 'other', self.priority_levels['contacts']),
                    (_sec.documents_urls, 'other', self.priority_levels['contacts']),
                    (_sec.instructions_urls, 'other', self.priority_levels['contacts'])):
                _match_len = self._longest_prefix(_p_path, _prefixes)
                if _match_len > _best_len:
                    _best_len, _best_result = _match_len, (_sec_cat, _sec_prio)
            if _best_len >= 0:
                return _best_result
            # catalog_roots: только сам корень каталога (точный путь) — подстраницы
            # идут обычной классификацией (иначе товарные URL стали бы категориями)
            _roots = [r.rstrip('/') for r in (s.lower() for s in _sec.catalog_roots)]
            if (not product_state_key(url) and
                    _p_path.rstrip('/') in [r if r.startswith('/') else '/' + r for r in _roots if r]):
                return 'category', self.priority_levels['category']

        # Проверка на исключаемые паттерны (P04 U1: посегментно, без хоста и без
        # значений маршрутных ключей CMS)
        downgrade = 0
        if self._is_excluded_by_patterns(path_segments, parsed.query.lower()):
            # D37: разделы дилеров часто живут под исключаемыми сегментами
            # (/about/predstavitelstva, /info/gde-kupit) — их не исключаем
            if any(k in path_lower or k in text_lower for k in self.distributor_keywords):
                return 'distributor', self.priority_levels['distributor']
            # P04 U1 (п. 3): явный каталожный сигнал сильнее exclude-токена — такой URL
            # не вырезаем, а лишь понижаем ему приоритет (D158, D207, D251)
            if self._has_catalog_signal(path_norm, text_lower):
                downgrade = 2
            else:
                self._defer_excluded(url, link_text, 'pattern')
                return 'excluded2', 0

        if product_state_key(url) and not profile_not_product:
            return 'product', self.priority_levels['product']

        # Проверка на главную страницу
        parsed_url = urlparse(url)
        path = parsed_url.path.strip('/')
        if not path or path in self.main_page_indicators:
            return 'main_page', self.priority_levels['main_page']
        
        # P04 U3 (D138): роль сначала определяется ПУТЁМ. Текст ссылки — слабый
        # сигнал, он смотрится ниже, только если путь роли не дал: иначе кнопка
        # «Где купить» внутри плитки каталога уводила карточки в distributor, а якорь
        # «Цены и контакты» — прайс-лист в contacts.

        # Проверка на контакты
        if any(keyword in path_lower for keyword in self.contact_keywords):
            return 'contacts', self.priority_levels['contacts']

        # Проверка на дистрибьюторов (P04 U4/D271: '/buy/' отдельным сегментом — это
        # «Где купить», а не товар; подстрокой такое слово брать нельзя — '/buyer/')
        if (any(keyword in path_lower for keyword in self.distributor_keywords)
                or any(seg in self.distributor_segments for seg in path_segments)):
            return 'distributor', self.priority_levels['distributor']

        # Проверка на прайс-листы. P04 U3 (D181): сильный товарный сигнал пути
        # ('/product/235255') сильнее префикса '/price/' — такой URL идёт в товары.
        # P01 U4 п.4: слово из СЕРЕДИНЫ пути — слабое свидетельство роли. Сразу
        # прайс-листом считаем URL, у которого слово стоит в последнем значащем
        # сегменте ('/price-plita.htm'); '/price/nasos-nm-100/' сначала проходит
        # товарную и каталожную проверки и получает прайсовую роль только в конце,
        # с пониженным приоритетом.
        price_hit = (any(keyword in path_lower for keyword in self.price_list_keywords)
                     and not self._has_strong_product_path(path_segments))
        if price_hit and any(keyword in (path_segments[-1] if path_segments else '')
                             for keyword in self.price_list_keywords):
            return 'price_list', self.priority_levels['price_list']

        # Проверка на товары (антипаттерн профиля запрещает классификацию «товар»).
        # P04 U2/D228: текст ссылки участвует в товарной оценке.
        if not profile_not_product and self._is_product_url(url, link_text):
            # downgrade (P04 U1, п. 3): URL прошёл мимо exclude-токена только за счёт
            # каталожного сигнала — берём его в обход, но позже настоящих товарных
            return 'product', max(self.priority_levels['product'] - downgrade, 1)

        # P04 U3 (D138): путь роли не дал — теперь можно слушать текст ссылки
        if text_lower:
            if any(keyword in text_lower for keyword in self.contact_keywords):
                return 'contacts', self.priority_levels['contacts']
            if any(keyword in text_lower for keyword in self.distributor_keywords):
                return 'distributor', self.priority_levels['distributor']
            if any(keyword in text_lower for keyword in self.price_list_keywords):
                return 'price_list', self.priority_levels['price_list']

        # Проверка на категории (P04 U2: ключевые слова — по границам токена, а не
        # подстрокой; плюс каталожные стемы, сегмент 'cat' и листинговые параметры
        # query, которым положена роль «категория», а не «товар» — D127, D274)
        if (self._category_keywords_re.search(path_lower) or
            self._category_keywords_re.search(text_lower) or
            any(seg.startswith(stem) for seg in path_segments for stem in self.category_segment_stems) or
            any(seg in self.category_segment_tokens for seg in path_segments) or
            self._listing_query_re.search(parsed.query.lower()) or
            any(re.search(pattern, url_lower) for pattern in [
                r'/catalog/', r'/category/', r'/collection/', r'/series/',
                r'/каталог/', r'/серия/', r'/katalog/', r'/products/'
            ])):
            return 'category', max(self.priority_levels['category'] - downgrade, 1)

        # P01 U4 п.4: отложенная прайсовая роль (слово нашлось в середине пути) —
        # ни товаром, ни категорией URL не оказался, значит прайс-лист, но с
        # пониженным доверием
        if price_hit:
            return 'price_list', max(self.priority_levels['price_list'] - 1, 1)

        return 'other', self.priority_levels['other']
        
    def _is_image_url(self, url: str) -> bool:
        """Проверка, является ли URL ссылкой на изображение"""
        url_lower = url.lower()
        
        # Проверяем расширения файлов
        for ext in self.image_extensions:
            if url_lower.endswith(ext):
                return True
            # Также проверяем расширения в середине URL (могут быть параметры)
            if f"{ext}?" in url_lower or f"{ext}&" in url_lower:
                return True
        
        # Проверяем паттерны изображений в URL
        image_patterns = [
            '/img_', '/image', '/images/', '/picture', '/pictures/',
            '/photo', '/photos/', '/gallery', '/thumb', '/thumbnail'
        ]
        
        if any(pattern in url_lower for pattern in image_patterns):
            return True
            
        return False
    
    def _is_product_url(self, url: str, link_text: str = "") -> bool:
        """Определение товарного URL с улучшенной эвристикой"""
        url_lower = url.lower()
        parsed = urlparse(url)
        # P04 U2 (D117/D145): путь нормализуем ДО матчинга — percent-декодирование,
        # casefold и снятие завершающего слеша (иначе якорные паттерны '...$' не
        # срабатывают на '/kategoriya/tovar/'). url_norm — URL с таким путём.
        path_norm = self._normalize_path(parsed.path)
        segments = [seg for seg in path_norm.split('/') if seg]
        url_norm = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path_norm}"
        if parsed.query:
            url_norm += '?' + parsed.query.lower()

        # Явное исключение для контактов (D123: по пути, иначе слово из домена
        # запрещает товарную классификацию всему сайту)
        if any(keyword in path_norm for keyword in self.contact_keywords):
            return False

        # Явные служебные/корзинные/листинговые страницы интернет-магазинов и 1С-Bitrix — это НЕ товары
        # (корзина, оформление заказа, личный кабинет, авторизация, сравнение, AJAX-листинги секций).
        # Сегментная проверка: эти маркеры стоят отдельным сегментом пути или именем скрипта, поэтому
        # НЕ отсеивают товарные страницы (их slug таких сегментов не содержит, напр.
        # /catalog/stenovye-bloki/stenovoy-blok-d400.../ или /products/ognestoykie-paneli/giplast/).
        # P04 U2 (D145/D237): стоп-лист вынесен в self.service_segments и расширен
        # ('sitemap', 'shoppingcart'); сверяем и сегменты пути, и имя последнего
        # сегмента без расширения, иначе '/sitemap.html' остаётся товаром.
        _path = parsed.path.lower()
        _last_base = segments[-1].rsplit('.', 1)[0] if segments else ''
        if (any(seg in self.service_segments for seg in segments)
                or _last_base in self.service_segments):
            return False
        # P04 U3 (D228): служебные шаблонные пути uCoz
        if self._ucoz_service_re.match(path_norm):
            return False
        # Листинги-скрипты и не-HTML ресурсы (Bitrix list.php?SECTION_ID, *.js/*.css и т.п.)
        if _path.endswith(('list.php', '.js', '.css', '.json', '.xml')) or _path.split('/')[-1] in ('list.php',):
            return False

        # Исключаем не товарные URL (P04 U1: посегментно, без хоста; явный каталожный
        # сигнал в пути или в тексте ссылки отменяет исключение — D158, D207, D251)
        if (self._is_excluded_by_patterns(segments, parsed.query.lower())
                and not self._has_catalog_signal(path_norm, (link_text or '').casefold())):
            return False


        # Проверка по паттернам URL (P04 U2/D145: дополнительно по нормализованному
        # URL — завершающий слеш больше не ломает якорные паттерны '...$').
        # Нормализованный кандидат берём ТОЛЬКО для слага (в последнем сегменте есть
        # дефис): иначе снятие слеша красит в товар любую двухсегментную служебную
        # страницу ('/where-buy/almaty/', '/dillers/dillers/', '/o-kompanii/nagrady/')
        # — на корпусе прогона это давало +260 ложных товарных URL.
        _slug_candidate = url_norm if (segments and '-' in segments[-1]) else None
        url_pattern_match = any(re.search(pattern, url_lower)
                                or (_slug_candidate is not None and re.search(pattern, _slug_candidate))
                                for pattern in self.product_url_patterns)
        # P04 U2/D284: ведущий числовой идентификатор ЧПУ — только по пути, не по URL
        if not url_pattern_match:
            url_pattern_match = any(re.search(pattern, path_norm)
                                    for pattern in self.product_path_patterns)

        # Проверка по ключевым словам в пути (P04 U2: по границам токена + стемы
        # разделов продукции на кириллице и в транслите — D117, D223, D153)
        keyword_match = (bool(self._product_keywords_re.search(path_norm))
                         or any(seg.startswith(stem) for seg in segments
                                for stem in self.product_segment_stems))

        # Проверка на наличие цифр (артикулов)
        has_digits = bool(re.search(r'\d{2,}', url_lower))

        # Дополнительная проверка для русскоязычных URL с цифрами в конце
        has_product_pattern = bool(re.search(r'/[a-z0-9-]+-\d+[a-z]*/?$', url_lower))

        # Проверка на параметры товаров (P04 U2/D274/D127: идентификатор товара в query)
        has_product_param = ('product=' in url_lower or 'item=' in url_lower
                             or 'goods=' in url_lower
                             or bool(self._product_query_re.search(parsed.query.lower())))

        # P04 U2/D228: явный товарный якорь ссылки — слабый самостоятельный сигнал
        text_norm = (link_text or '').casefold()
        anchor_match = bool(text_norm) and any(m in text_norm for m in self.product_anchor_markers)

        # P04 U2: «плоский» сайт (observe_urls) — односегментный слаг или лист
        # .htm/.html/.shtml в корне считаем товарным кандидатом
        flat_match = self.flat_site and self._is_flat_product_candidate(segments)

        # Комбинированная оценка
        score = sum([
            url_pattern_match * 2,
            keyword_match * 1.5,
            has_digits * 1,
            has_product_pattern * 1.5,
            has_product_param * 2,
            anchor_match * 1.5,
            flat_match * 1.5
        ])

        return score >= 1.5

    def is_product_page_by_content(self, soup: BeautifulSoup, url: str) -> bool:
        """Определение товарной страницы по содержимому"""
        score = 0
        
        # Проверка наличия цены
        price_elements = soup.find_all(string=re.compile(
            '|'.join(self.content_indicators['price_indicators']), 
            re.IGNORECASE
        ))
        if price_elements:
            score += 2
        
        # Проверка кнопок покупки
        buy_elements = soup.find_all(string=re.compile(
            '|'.join(self.content_indicators['buy_buttons']), 
            re.IGNORECASE
        ))
        if buy_elements:
            score += 2
            
        # Проверка товарных атрибутов
        attr_elements = soup.find_all(string=re.compile(
            '|'.join(self.content_indicators['product_attrs']), 
            re.IGNORECASE
        ))
        if attr_elements:
            score += 1.5
            
        # Проверка товарных селекторов
        product_selectors = [
            '.product', '[class*="product"]', '[class*="item"]', '.goods',
            '.catalog-item', '.shop-item', '.store-item', '.product-card',
            '.item-card', '[data-product]', '[itemtype*="Product"]',
            '.product-details', '.item-details', '.product-view',
            '.product-info', '.goods-info', '.item-info'
        ]
        
        product_elements = []
        for selector in product_selectors:
            product_elements.extend(soup.select(selector))
        if product_elements:
            score += 1
            
        # Проверка микроразметки
        schema_elements = soup.select('[itemtype*="Product"]')
        if schema_elements:
            score += 2
            
        # Проверка структурированных данных
        script_tags = soup.find_all('script', type='application/ld+json')
        for script in script_tags:
            if '"Product"' in script.text or '"product"' in script.text:
                score += 2
                break
                
        return score >= 3

class SmartURLNormalizer:
    """Улучшенная нормализация URL с извлечением canonical URL"""
    
    def __init__(self, url_categorizer=None):  
        self.url_categorizer = url_categorizer
        # Только однозначные метки аналитики. Остальные параметры могут выбирать товар.
        self.params_to_remove = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_term',
                                 'utm_content', 'utm_id', 'fbclid', 'gclid', 'dclid',
                                 'msclkid', 'yclid', '_openstat'}

        # Пагинационные параметры с ограничением глубины.
        # D245 (Гомельобои): 'p' из списка убран — в WordPress это идентификатор записи
        # (?p=5845 — товарный пермалинк), а не номер страницы. Считая его пагинацией,
        # нормализатор схлопывал 637 товарных URL карты сайта в корень сайта.
        self.pagination_params = {
            'page': 50, 'paging': 50, 'offset': 100, 'start': 100
        }

        # Кэш для ускорения нормализации - ключ: нормализованный URL
        self._normalization_cache = {}
        # Обратное отображение для быстрого поиска
        self._reverse_cache = {}

    def normalize_url(self, url: str) -> str:
        """Нормализация URL с кэшированием по нормализованному виду"""
        if not url:
            return url

        # D257 (Пластруб): хвостовой пробел в href доезжал до запроса и давал 403 —
        # сервер видел адрес с '%20' на конце. Ключ дедупликации тоже расходился.
        url = url.strip()

        policy = _get_profile_resolver().resolve(url).crawl if _get_profile_resolver else None
        # Известная порча &region -> ®ion исправляется только в профиле АэроБел.
        # Произвольный знак ® и последующие смысловые параметры не отбрасываем.
        if policy and policy.repair_region_entity:
            match = re.search(r'(?:®|%c2%ae)ion=', url, re.I)
            if match:
                prefix, tail = url[:match.start()], url[match.start():]
                separator = '&' if '?' in prefix else '?'
                tail = re.sub(r'(?:®|%c2%ae)ion=', '&region=', tail, flags=re.I)
                url = prefix + separator + tail[1:]

        try:
            parsed = urlparse(url)
            # Эквивалентность www сохраняется; порт и фактический хост не теряются.
            categorizer = self.url_categorizer
            if (categorizer and categorizer.domain_equivalency and categorizer.config
                    and categorizer.config.domain_equivalency_enabled
                    and parsed.hostname and ':' not in parsed.hostname and not parsed.username):
                host = categorizer.domain_equivalency.normalize_domain(parsed.hostname)
                port = ':' + str(parsed.port) if parsed.port is not None else ''
                parsed = parsed._replace(netloc=host + port)
            parsed = parsed._replace(fragment=product_state_key(url) or '')
            path = parsed.path
            # Подтверждённые языковые алиасы — только в указанных путях профиля.
            if policy and path.startswith('/ru/'):
                without_ru = path[3:]
                if any(without_ru.startswith(prefix) for prefix in policy.strip_ru_prefix_paths):
                    path = without_ru
            if path:
                # D198 (Костромской силикатный): раньше путям, кончающимся цифрами,
                # принудительно дописывался слеш (/product/102 -> /product/102/).
                # В роли КЛЮЧА дедупликации обе формы и так схлопываются общим rstrip('/'),
                # а как база склейки относительных ссылок дописанный слеш давал лишний
                # сегмент (/product/product/NNN) и 404 на всех карточках товаров.
                path = path.rstrip('/')
            
            if not path:
                path = '/'
            
            # Обрабатываем параметры запроса
            query_params = parse_qs(parsed.query, keep_blank_values=True, errors='surrogateescape')
            filtered_params = {}
            ignored = set(self.params_to_remove)
            if policy and (not policy.query_noise_paths or
                           path.rstrip('/') in {p.rstrip('/') for p in policy.query_noise_paths}):
                ignored.update(key.lower() for key in policy.ignored_query_params)

            for key, values in query_params.items():
                key_lower = key.lower()

                # Удаляем трекерные параметры
                if key_lower in ignored:
                    continue
                    
                # D245: пагинационный параметр НЕ вырезаем. Молчаливое удаление
                # схлопывало разные страницы в один ключ, и гейт дублей гасил их все.
                # Слишком глубокая пагинация отбраковывается вызывающим кодом целым
                # URL (is_over_pagination_limit), а не подменой адреса.
                filtered_params[key] = values
            
            # Сортируем параметры
            sorted_params = sorted(filtered_params.items())
            normalized_query = urlencode(sorted_params, doseq=True, errors='surrogateescape')
            
            # Собираем URL обратно
            normalized_url = urlunparse((
                parsed.scheme,
                parsed.netloc.lower(),
                path,
                parsed.params,
                normalized_query,
                parsed.fragment
            ))
            
            # Ищем в кэше по нормализованному виду
            if normalized_url in self._reverse_cache:
                return self._reverse_cache[normalized_url]
            
            # Сохраняем в кэши
            self._normalization_cache[url] = normalized_url
            self._reverse_cache[normalized_url] = normalized_url
            
            return normalized_url
            
        except Exception as e:
            log.warning(f"Ошибка нормализации URL {url}: {e}")
            return url

    def is_over_pagination_limit(self, url: str) -> bool:
        """D245: признак слишком глубокой пагинации (?page=999) — кандидат на отбраковку
        URL ЦЕЛИКОМ. Прежде нормализатор молча вырезал такой параметр, и адрес схлопывался
        с первой страницей листинга; теперь параметр сохраняется, а решение об отказе
        принимает вызывающий код (постановка в очередь). Fail-open: при любой ошибке False."""
        try:
            for key, values in parse_qs(urlparse(url).query, keep_blank_values=True).items():
                limit = self.pagination_params.get(key.lower())
                if limit is not None and values and values[0].isdigit() and int(values[0]) > limit:
                    return True
        except Exception as e:
            log.debug(f"is_over_pagination_limit: не удалось разобрать {url}: {e}")
        return False

    def extract_canonical_url(self, soup: BeautifulSoup, current_url: str) -> str:
        """Извлечение canonical URL из HTML мета-тегов"""
        try:
            if product_state_key(current_url):
                return self.normalize_url(current_url)
            # Ищем canonical link
            canonical_link = soup.find('link', rel='canonical')
            if canonical_link and canonical_link.get('href'):
                canonical_url = canonical_link['href']
                # Если URL относительный, преобразуем в абсолютный
                if not canonical_url.startswith(('http://', 'https://')):
                    canonical_url = urljoin(current_url, canonical_url)
                if self._canonical_is_same_site(canonical_url, current_url):
                    return self.normalize_url(canonical_url)

            # Ищем og:url
            og_url = soup.find('meta', property='og:url')
            if og_url and og_url.get('content'):
                og_url_content = og_url['content']
                if not og_url_content.startswith(('http://', 'https://')):
                    og_url_content = urljoin(current_url, og_url_content)
                if self._canonical_is_same_site(og_url_content, current_url):
                    return self.normalize_url(og_url_content)

            # Возвращаем нормализованный текущий URL как fallback
            return self.normalize_url(current_url)
            
        except Exception as e:
            log.warning(f"Ошибка извлечения canonical URL: {e}")
            return self.normalize_url(current_url)

    def _canonical_is_same_site(self, candidate_url: str, current_url: str) -> bool:
        """Годится ли canonical/og:url как база склейки ссылок. Пустой хост или хост,
        неэквивалентный текущему (зеркало, домен группы), базу подменять не должен: иначе
        относительные ссылки страницы резолвятся в чужой хост и гибнут на гейте периметра
        (D182, D272)."""
        netloc = urlparse(candidate_url).netloc
        if not netloc:
            return False
        if hosts_equivalent(netloc, urlparse(current_url).netloc):
            return True
        log.debug(f"canonical/og:url {candidate_url} с неэквивалентным хостом отброшен "
                  f"для {current_url}")
        return False

class SiteMapParser:
    """Парсер карт сайта для извлечения товарных URL"""
    
    def __init__(self, config: Config, url_categorizer: URLCategorizer, url_normalizer: SmartURLNormalizer, allowed_url=None):
        self.config = config
        self.url_categorizer = url_categorizer
        self.url_normalizer = url_normalizer
        
        
        # Форматы карт сайта. D214: после '/sitemap1.xml' была пропущена запятая —
        # Python склеивал литералы в '/sitemap1.xml/rss.xml', и в списке было 12 путей
        # вместо 13; отдельно добавлены штатные пути WordPress >= 5.5 и индексов.
        self.sitemap_paths = [
            '/sitemap.xml', '/sitemap/index.xml', '/sitemap_index.xml', '/sitemap',
            '/sitemap.txt', '/sitemap.html', '/site-map.html', '/sitemap/xml/', '/xml/sitemap.xml', '/sitemap1.xml',
            '/wp-sitemap.xml', '/sitemap-index.xml', '/wp-sitemap-index.xml',
            '/rss.xml', '/feed.xml', '/atom.xml'
        ]
        
        self._allowed_url = allowed_url
        # Кэш для избежания повторной обработки
        self._processed_sitemaps = set()
        self._results = {}
        self._states = {}
        self._loading = {}
        self._dependencies = {}
        self._completed = {}
        self._xml_trees = {}
        self._html_trees = {}
        self._documents = {}
        self._document_tasks = {}
        self._root_tasks = []
        self.phase_timed_out = False
        self._scope_base = None
        self._request_count = 0
        self._http_slots = asyncio.Semaphore(getattr(config, 'sitemap_http_concurrency', 4))
        self._map_slots = asyncio.Semaphore(max(1, getattr(config, 'sitemap_http_concurrency', 4) - 1))
        from contextvars import ContextVar
        self._current_map = ContextVar('sitemap_current_map', default=None)

    # D144/D208: разделы, которые товарных карточек не дают, но в карте сайта
    # конкурируют за кэп sitemap_max_urls с настоящими карточками.
    _INFO_PATH_TOKENS = frozenset({
        'news', 'novosti', 'blog', 'article', 'articles', 'stati', 'press',
        'about', 'o-kompanii', 'company', 'faq', 'vacancy', 'vakansii',
        'gallery', 'photo', 'video', 'reviews', 'otzyvy', 'events',
    })

    @staticmethod
    def _has_article_slug(path: str) -> bool:
        """Последний сегмент пути похож на артикул/детальную карточку: содержит цифру."""
        slug = path.rstrip('/').rsplit('/', 1)[-1].lower()
        return bool(slug) and bool(re.search(r'\d', slug))

    def _sitemap_rank_key(self, row):
        """D144/D208: ключ ранжирования кандидатов карты сайта ДО применения кэпа.
        По возрастанию: приоритет (выше — раньше), не-info раздел, артикульный слаг,
        глубина пути, сам адрес (детерминизм между запусками)."""
        url, normalized_url, category, priority = row
        try:
            path = urlparse(url).path or '/'
        except Exception:
            path = '/'
        segments = [seg.lower() for seg in path.split('/') if seg]
        is_info = any(seg in self._INFO_PATH_TOKENS for seg in segments)
        return (-priority, 1 if is_info else 0,
                0 if self._has_article_slug(path) else 1, len(segments), url)


    # D164/D240: ранжирование источников карт. Товарная карта должна разбираться
    # первой, карта вложений/новостей — последней.
    _SITEMAP_GOOD_TOKENS = ('product', 'catalog', 'katalog', 'shop', 'iblock', 'tovar')
    _SITEMAP_BAD_TOKENS = ('attachment', 'media', 'news', 'blog', 'author', 'tag', 'feed')

    def _allows_url(self, url, base_url=None):
        base = self._scope_base or base_url
        if self._allowed_url is not None:
            return self._allowed_url(url, resource='sitemap', base_url=base)
        manager = self.url_categorizer.domain_equivalency
        return url_in_scope(url, base, profile_domains=getattr(manager, 'profile_domains', ()),
                            profile_suffixes=getattr(manager, 'profile_suffixes', ()))

    def _is_own_sitemap_host(self, sitemap_url: str, base_url: str) -> bool:
        return self._allows_url(sitemap_url, base_url)

    def _sitemap_source_rank(self, sitemap_url: str, base_url: str):
        """D164/D240: детерминированный порядок карт вместо list(set(...)).
        По возрастанию: свой хост, товарное имя, не карта вложений/новостей, адрес."""
        try:
            name = urlparse(sitemap_url).path.lower()
            same_host = self.url_categorizer.is_main_domain(sitemap_url, base_url)
        except Exception:
            name, same_host = sitemap_url.lower(), False
        return (0 if same_host else 1,
                0 if any(token in name for token in self._SITEMAP_GOOD_TOKENS) else 1,
                1 if any(token in name for token in self._SITEMAP_BAD_TOKENS) else 0,
                sitemap_url)

    async def reset_run(self):
        """Завершает задачи прошлого прохода и очищает только оперативный кэш."""
        tasks = list(self._loading.values()) + list(self._document_tasks.values()) + [task for _, task in self._root_tasks]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._loading.clear()
        self._xml_trees.clear()
        self._html_trees.clear()
        self._documents.clear()
        self._document_tasks.clear()
        self._root_tasks.clear()
        self._results.clear()
        self._states.clear()
        self._dependencies.clear()
        self._completed.clear()
        self._processed_sitemaps.clear()
        self.phase_timed_out = False
        self._scope_base = None
        self._request_count = 0
        self._http_slots = asyncio.Semaphore(getattr(self.config, 'sitemap_http_concurrency', 4))
        self._map_slots = asyncio.Semaphore(max(1, getattr(self.config, 'sitemap_http_concurrency', 4) - 1))

    async def _request(self, url, method='GET'):
        """Общий бюджет HTTP; тело ограничивается во время чтения и распаковки."""
        import zlib
        base = self._scope_base or url
        if not self._allows_url(url, base):
            raise ValueError(f'Карта вне периметра: {url}')
        async with self._http_slots:
            if self._request_count >= getattr(self.config, 'sitemap_max_requests', 128):
                raise ValueError('Исчерпан лимит HTTP-запросов карт сайта')
            self._request_count += 1
            timeout = aiohttp.ClientTimeout(total=self.config.sitemap_timeout_seconds)
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector, auto_decompress=False) as session:
                for redirect in range(6):
                    if not self._allows_url(url, base):
                        raise ValueError(f'Редирект карты вне периметра: {url}')
                    if redirect:
                        if self._request_count >= getattr(self.config, 'sitemap_max_requests', 128):
                            raise ValueError('Исчерпан лимит HTTP-запросов карт сайта')
                        self._request_count += 1
                    async with session.request(method, url, timeout=timeout, allow_redirects=False,
                                               headers={'Accept-Encoding': 'identity'}) as response:
                        if not self._allows_url(str(response.url), base):
                            raise ValueError(f'Ответ карты вне периметра: {response.url}')
                        if response.status in (301, 302, 303, 307, 308) and response.headers.get('Location'):
                            url = urljoin(str(response.url), response.headers['Location'])
                            if redirect == 5:
                                raise ValueError('Слишком много редиректов карты сайта')
                            continue
                        if method == 'HEAD' or response.status != 200:
                            return response.status, str(response.url), response.headers, b''
                        compressed_limit = getattr(self.config, 'sitemap_max_compressed_bytes', 8 * 1024 * 1024)
                        decoded_limit = getattr(self.config, 'sitemap_max_decompressed_bytes', 32 * 1024 * 1024)
                        raw_count = 0
                        output = bytearray()
                        decoder = None
                        prefix = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            raw_count += len(chunk)
                            if raw_count > compressed_limit:
                                raise ValueError('Превышен лимит сжатого тела карты сайта')
                            if decoder is None and len(prefix) < 2:
                                prefix.extend(chunk)
                                if len(prefix) < 2:
                                    continue
                                chunk = bytes(prefix)
                                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if chunk[:2] == b'\x1f\x8b' else False
                            if decoder:
                                chunk = decoder.decompress(chunk, decoded_limit - len(output) + 1)
                                if decoder.unconsumed_tail or decoder.unused_data:
                                    raise ValueError('Превышен лимит или лишние данные gzip карты сайта')
                            output.extend(chunk)
                            if len(output) > decoded_limit:
                                raise ValueError('Превышен лимит распакованного тела карты сайта')
                        if decoder is None:
                            output.extend(prefix)
                        elif decoder and not decoder.eof:
                            raise ValueError('Усечённый gzip карты сайта')
                        return response.status, str(response.url), response.headers, bytes(output)

    async def collect_sitemap_urls(self, base_url, max_depth=3):
        """Одна фаза обнаружения и разбора; при таймауте сохраняются готовые карты."""
        async def collect():
            urls = await self.discover_sitemap_urls(base_url)
            known = {url for url, _ in self._root_tasks}
            self._root_tasks.extend((url, asyncio.create_task(self.parse_sitemap(url, max_depth=max_depth)))
                                    for url in urls if url not in known)
            if self._root_tasks:
                await asyncio.gather(*(task for _, task in self._root_tasks))
        self._scope_base = base_url
        self._collecting = True
        self._collect_depth = max_depth
        try:
            await asyncio.wait_for(collect(), self.config.sitemap_phase_timeout_seconds)
        except asyncio.TimeoutError:
            self.phase_timed_out = True
            log.warning('Исчерпан общий бюджет обнаружения и разбора карт сайта')
        finally:
            self._collecting = False
            tasks = list(self._loading.values()) + list(self._document_tasks.values()) + [task for _, task in self._root_tasks]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            # Отменённая до первого шага coroutine не выполняет свой finally.
            self._loading.clear()
            self._document_tasks.clear()
            self._root_tasks.clear()
            for key, state in self._states.items():
                if state == 'loading':
                    self._states[key] = 'error'
        return [self._completed[url] for url in sorted(
            self._completed, key=lambda url: self._sitemap_source_rank(url, base_url))]

    async def discover_sitemap_urls(self, base_url: str) -> List[str]:
        self._scope_base = base_url
        parsed = urlparse(base_url)
        origin = f'{parsed.scheme}://{parsed.netloc}'
        found = []
        # robots проверяется первым, найденные карты сразу разбираются: медленные
        # стандартные пути не должны лишать нас уже полученных результатов.
        robots = await self._extract_sitemaps_from_robots(origin + '/robots.txt')
        for url in sorted(dict.fromkeys(robots), key=lambda u: self._sitemap_source_rank(u, base_url))[:getattr(self.config, 'sitemap_max_requests', 128)]:
            if self._is_own_sitemap_host(url, base_url):
                found.append(url)
                if getattr(self, '_collecting', False):
                    self._root_tasks.append((url, asyncio.create_task(
                        self.parse_sitemap(url, max_depth=self._collect_depth))))
        # Разбор идёт одновременно с резервными пробами; _map_slots оставляет
        # один HTTP-слот обнаружению, чтобы медленная карта не скрыла быстрый fallback.
        for path in self.sitemap_paths:
            effective = await self._check_sitemap_exists(origin + path)
            if effective and self._is_own_sitemap_host(effective, base_url):
                found.append(effective)
                if getattr(self, '_collecting', False):
                    self._root_tasks.append((effective, asyncio.create_task(
                        self.parse_sitemap(effective, max_depth=self._collect_depth))))
        found.extend(await self._find_sitemap_links_in_html(base_url))
        return sorted({u for u in found if self._is_own_sitemap_host(u, base_url)},
                      key=lambda u: self._sitemap_source_rank(u, base_url))

    async def _check_sitemap_exists(self, url):
        try:
            status, effective, headers, _ = await self._request(url, 'HEAD')
            if 200 <= status < 300:
                return effective
            if 300 <= status < 400:
                return urljoin(url, headers.get('Location', '')) or None
            if status in (400, 403, 405, 501):
                status, effective, _, _ = await self._request(url)
                if status == 200:
                    return effective
        except Exception as e:
            log.debug(f'Карта сайта {url} недоступна: {e}')
        return None

    async def _extract_sitemaps_from_robots(self, robots_url):
        try:
            status, effective, _, body = await self._request(robots_url)
            if status == 200:
                urls = []
                for item_number, line in enumerate(body.decode('utf-8-sig', errors='replace').splitlines()):
                    if item_number % 128 == 0:
                        await asyncio.sleep(0)
                    if line.strip().lower().startswith('sitemap:'):
                        urls.append(urljoin(effective, line.split(':', 1)[1].strip()))
                return urls
        except Exception as e:
            log.debug(f'Не удалось прочитать robots.txt: {e}')
        return []

    async def _find_sitemap_links_in_html(self, base_url):
        try:
            status, effective, _, body = await self._request(base_url)
            if status == 200:
                soup = await self._html_tree(body.decode('utf-8-sig', errors='replace'), effective)
                return [urljoin(effective, sanitize_href(link['href']))
                        for link in soup.find_all('a', href=True)
                        if any(word in link.get_text().lower()
                               for word in ('карта сайта', 'sitemap', 'site map', 'карта-сайта'))]
        except Exception as e:
            log.debug(f'Не удалось найти HTML-карту: {e}')
        return []

    async def parse_sitemap(self, sitemap_url, depth=0, max_depth=3):
        if self._scope_base is None:
            self._scope_base = sitemap_url
        if not self._allows_url(sitemap_url):
            log.info(f'Карта вне периметра пропущена: {sitemap_url}')
            return []
        if depth > max_depth:
            return []
        key = (sitemap_url, max_depth - depth)
        parent = self._current_map.get()
        if key in self._results:
            return self._results[key]
        if parent is not None:
            # Не ждём задачу, которая уже прямо или косвенно ждёт нас.
            def reaches(node, target, seen):
                if node == target:
                    return True
                if node in seen:
                    return False
                seen.add(node)
                return any(reaches(child, target, seen) for child in self._dependencies.get(node, ()))
            if reaches(key, parent, set()):
                return []
            self._dependencies.setdefault(parent, set()).add(key)
        if key not in self._loading:
            self._states[key] = 'loading'
            self._loading[key] = asyncio.create_task(self._load_sitemap(sitemap_url, depth, max_depth, key))
        try:
            rows = await asyncio.shield(self._loading[key])
            if parent is not None and self._states.get(key) == 'error':
                self._states[parent] = 'error'
            return rows
        finally:
            if parent is not None:
                self._dependencies.get(parent, set()).discard(key)

    async def _xml_tree(self, content, sitemap_url):
        if sitemap_url in self._xml_trees:
            return self._xml_trees[sitemap_url]
        parser = ET.XMLPullParser(events=('start',))
        root = None
        content = content.lstrip('\ufeff \t\r\n')
        for offset in range(0, len(content), 65536):
            parser.feed(content[offset:offset + 65536])
            for _, node in parser.read_events():
                if root is None:
                    root = node
            await asyncio.sleep(0)
        parser.close()
        self._xml_trees[sitemap_url] = root
        return root

    async def _html_tree(self, content, sitemap_url):
        if sitemap_url in self._html_trees:
            return self._html_trees[sitemap_url]
        # Один HTMLParser сохраняет состояние между порциями (включая теги,
        # разрезанные границей блока); builder.feed создавал бы новый парсер.
        from bs4.builder._htmlparser import BeautifulSoupHTMLParser
        soup = BeautifulSoup('', 'html.parser')
        parser = BeautifulSoupHTMLParser(soup)
        for offset in range(0, len(content), 65536):
            parser.feed(content[offset:offset + 65536])
            await asyncio.sleep(0)
        parser.close()
        self._html_trees[sitemap_url] = soup
        return soup

    async def _body_kind(self, content, content_type, sitemap_url):
        stripped = content.lstrip('\ufeff \t\r\n')
        if stripped.startswith('<'):
            parser = ET.XMLPullParser(events=('start',))
            try:
                for offset in range(0, len(stripped), 65536):
                    parser.feed(stripped[offset:offset + 65536])
                    for _, node in parser.read_events():
                        name = node.tag.rsplit('}', 1)[-1]
                        if name in ('urlset', 'sitemapindex'):
                            return 'xml'
                        return self._sitemap_format('<' + name + '>', content_type, sitemap_url)
                    await asyncio.sleep(0)
            except ET.ParseError:
                return self._sitemap_format(stripped[:4096], content_type, sitemap_url)
            return None
        if not stripped.startswith(('{', '[')) and '<' in stripped:
            first_tag = stripped.find('<')
            return self._sitemap_format(stripped[first_tag:first_tag + 4096], content_type, sitemap_url)
        found = False
        for item_number, line in enumerate(content.splitlines()):
            if item_number % 128 == 0:
                await asyncio.sleep(0)
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parsed = urlparse(line)
            if parsed.scheme not in ('http', 'https', '') or not parsed.netloc or re.search(r'\s', line):
                return None
            found = True
        return 'text' if found else None

    async def _document(self, sitemap_url):
        """Один HTTP/разбор тела на URL; глубина влияет только на обход индекса."""
        try:
            async with self._map_slots:
                status, effective, headers, raw = await self._request(sitemap_url)
            if not self._allows_url(effective, sitemap_url):
                raise ValueError(f'Ответ карты вне периметра: {effective}')
            if status != 200:
                raise ValueError(f'HTTP {status}')
            # gzip уже распакован потоковым читателем; повторно не распаковываем.
            content = self._decode_sitemap_body(raw, effective, decompress=False)
            kind = await self._body_kind(content, headers.get('content-type', '').lower(), effective) if content is not None else None
            if kind == 'xml':
                await self._xml_tree(content, effective)
            if kind == 'html' and self._is_sitemap_error_html(await self._html_tree(content, effective)):
                self._html_trees.pop(effective, None)
                raise ValueError('HTML-заглушка вместо карты')
            result = (effective, content, kind)
            if kind:
                self._documents[sitemap_url] = result
            return result
        finally:
            self._document_tasks.pop(sitemap_url, None)

    async def _load_sitemap(self, sitemap_url, depth, max_depth, key):
        token = self._current_map.set(key)
        try:
            if sitemap_url in self._documents:
                effective, content, kind = self._documents[sitemap_url]
            else:
                if sitemap_url not in self._document_tasks:
                    self._document_tasks[sitemap_url] = asyncio.create_task(self._document(sitemap_url))
                try:
                    effective, content, kind = await asyncio.shield(self._document_tasks[sitemap_url])
                except Exception as e:
                    import zlib
                    if isinstance(e, zlib.error) or str(e) == 'Усечённый gzip карты сайта':
                        effective, content, kind = sitemap_url, None, None
                    else:
                        raise
            if kind == 'xml':
                # Невалидный XML не становится успешным пустым кэшем.
                await self._xml_tree(content, effective)
                rows = await self._parse_xml_sitemap(content, effective, depth, max_depth)
            elif kind == 'html':
                if self._is_sitemap_error_html(await self._html_tree(content, effective)):
                    raise ValueError('HTML-заглушка вместо карты')
                rows = await self._parse_html_sitemap(content, effective)
            elif kind == 'text':
                rows = await self._parse_text_sitemap(content, effective)
            else:
                parsed = urlparse(sitemap_url)
                if parsed.path.lower().endswith('.gz'):
                    rows = await self.parse_sitemap(urlunparse(parsed._replace(path=parsed.path[:-3])), depth, max_depth)
                else:
                    raise ValueError('Неизвестный формат карты')
            # Индекс с отказавшей подкартою можно вызвать повторно: готовые
            # документы переиспользуются, ошибочные HTTP-запросы повторяются.
            failed_child = self._states.get(key) == 'error' or any(self._states.get(child) == 'error'
                               for child in self._dependencies.get(key, ()))
            if not failed_child:
                self._results[key] = rows
            if kind and (kind != 'xml' or self._xml_trees[effective].tag.rsplit('}', 1)[-1] != 'sitemapindex'):
                self._completed[sitemap_url] = rows
            self._states[key] = 'error' if failed_child else 'success'
            if not failed_child:
                self._processed_sitemaps.add(sitemap_url)
            return rows
        except asyncio.CancelledError:
            self._states[key] = 'error'
            raise
        except Exception as e:
            self._states[key] = 'error'
            log.warning(f'Ошибка карты сайта {sitemap_url}: {e}')
            return []
        finally:
            self._current_map.reset(token)
            self._loading.pop(key, None)

    @staticmethod
    def _decode_sitemap_body(raw: bytes, sitemap_url: str, decompress=True) -> Optional[str]:
        """Распаковывает только фактический gzip: HTTP-клиент мог уже снять сжатие."""
        try:
            if decompress and raw[:2] == b'\x1f\x8b':
                raw = gzip.decompress(raw)
            if raw.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')):
                return raw.decode('utf-32')
            if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
                return raw.decode('utf-16')
            return raw.decode('utf-8-sig', errors='replace')
        except Exception as e:
            log.warning(f"Не удалось прочитать тело карты сайта {sitemap_url}: {e}")
            return None

    @staticmethod
    def _sitemap_format(content: str, content_type: str, sitemap_url: str) -> Optional[str]:
        """Тело определяет формат; MIME/расширение лишь помогают распознать HTML-фрагмент."""
        content = content.lstrip('\ufeff \t\r\n')
        if not content:
            return None
        if content.startswith('<'):
            # XML-парсер сохраняет настоящее имя корня и namespace. Одного наличия
            # <url> внутри RSS/Error недостаточно, чтобы считать документ картой.
            root = BeautifulSoup(content, 'xml').find(True)
            if root is not None and root.name in ('urlset', 'sitemapindex'):
                return 'xml'
            root_name = root.name.lower() if root is not None else ''
        else:
            root_name = ''
        html_roots = {'html', 'head', 'body', 'title', 'meta', 'link', 'base', 'script', 'style',
                      'a', 'div', 'nav', 'main', 'section', 'article', 'header', 'footer',
                      'ul', 'ol', 'li', 'dl', 'dt', 'dd', 'table', 'p', 'span',
                      'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'br'}
        if root_name in html_roots:
            return 'html'
        # Не переинтерпретируем XML с чужим корнем даже при text/html или .html.
        if root_name:
            return None
        if (not content.startswith(('{', '[')) and
                ('<' in content or 'html' in content_type or
                 urlparse(sitemap_url).path.lower().endswith(('.html', '.htm')))):
            first = BeautifulSoup(content, 'html.parser').find(True)
            if first is not None and first.name in html_roots:
                return 'html'
        lines = [line.strip() for line in content.splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        if lines and all(urlparse(line).scheme in ('http', 'https', '') and urlparse(line).netloc
                         and not re.search(r'\s', line) for line in lines):
            return 'text'
        return None

    def _is_sitemap_error_html(self, soup: BeautifulSoup) -> bool:
        """Известные заглушки; слова 404/captcha в ссылках карты не являются отказом."""
        headings = [' '.join(tag.stripped_strings).lower() for tag in soup.find_all(['title', 'h1'])]
        error_heading = re.compile(
            r'^(?:40[034]\b|50[0234]\b|(?:error|ошибка)\s*[: -]?\s*[45]\d\d\b|'
            r'not found\b|access denied\b|forbidden\b|just a moment\b|attention required\b|'
            r'страница не найдена|доступ запрещ[её]н|проверка браузера)')
        if any(error_heading.search(text) for text in headings):
            return True
        if soup.find(id=re.compile(r'^(?:challenge-form|cf-challenge|cf-error-details)$', re.I)):
            return True
        # Маркеры хостера проверяем вне подписей ссылок: карта может ссылаться на
        # справочную статью с таким названием. Технические маркеры — в form/script/link.
        visible = ' '.join(str(node) for node in soup.find_all(string=True)
                           if not isinstance(node, Comment) and not node.find_parent(['a', 'script', 'style'])).lower()
        technical = ''.join(str(tag) for tag in soup.find_all(['title', 'script', 'form', 'link'])).lower()
        return any(marker.lower() in visible or marker.lower() in technical
                   for marker in (getattr(self.config, 'hoster_stub_markers', None) or []) if marker)

    @staticmethod
    def _repair_loc(loc: str, sitemap_url: str) -> Optional[str]:
        """D151: восстанавливает адрес <loc> без хоста по адресу самой карты сайта.
        Bitrix с пустым SERVER_NAME пишет 'https:///catalog/x/', а часть генераторов —
        просто '/catalog/x/'; и то и другое даёт пустой netloc, проходит гейт поддоменов
        и занимает товарную квоту, а гасится молча уже после постановки в очередь.
        None — адрес неисправим."""
        try:
            parsed = urlparse(loc)
            if parsed.netloc:
                return loc
            if not parsed.path:
                return None
            return urljoin(sitemap_url, urlunparse(('', '', parsed.path, parsed.params,
                                                    parsed.query, '')))
        except Exception:
            return None

    def _submap_rank(self, loc: str):
        """D154: при ограничении числа подкарт индекса первыми разбираются товарные
        (Bitrix нумерует карты по id инфоблока, товарный часто заводится последним)."""
        name = loc.lower()
        return (0 if any(token in name for token in self._SITEMAP_GOOD_TOKENS) else 1,
                1 if any(token in name for token in self._SITEMAP_BAD_TOKENS) else 0,
                loc)

    async def _parse_xml_sitemap(self, content: str, sitemap_url: str, depth: int, max_depth: int) -> List[Tuple[str, str, str, int]]:
        """Парсинг XML карты сайта (включая sitemap index)"""
        if self._scope_base is None:
            self._scope_base = sitemap_url
        try:
            # BeautifulSoup восстанавливает битый XML; здесь не принимаем усечённую
            # или постороннюю структуру как корректную карту с частичным результатом.
            root = await self._xml_tree(content, sitemap_url)
            root_name = root.tag.rsplit('}', 1)[-1]
            if root_name not in ('urlset', 'sitemapindex'):
                log.warning(f"Неподдерживаемый корень XML карты сайта: {sitemap_url}")
                return []
            all_urls = []
            
            # Проверка на sitemap index
            if root_name == 'sitemapindex':
                sitemap_tags = [node for node in root if node.tag.rsplit('}', 1)[-1] == 'sitemap']
                # Это индексный файл - парсим вложенные карты сайта.
                # D154: жёсткий срез [:5] заменён конфиг-параметром (по умолчанию — все);
                # при действующем лимите первыми берутся товарные подкарты.
                locs = []
                for item_number, tag in enumerate(sitemap_tags):
                    if item_number % 128 == 0:
                        await asyncio.sleep(0)
                    loc = next((node for node in tag if node.tag.rsplit('}', 1)[-1] == 'loc'), None)
                    if loc is not None and loc.text:
                        locs.append(loc.text.strip())
                limit = self.config.sitemap_max_submaps
                if limit and len(locs) > limit:
                    chosen = sorted(locs, key=self._submap_rank)[:limit]
                    log.info(f"Индекс {sitemap_url}: {len(locs)} карт, разобрано {len(chosen)}, "
                             f"пропущено {len(locs) - len(chosen)}: "
                             f"{', '.join(loc for loc in locs if loc not in chosen)[:500]}")
                else:
                    chosen = locs
                    log.info(f"Индекс {sitemap_url}: {len(locs)} карт, разобраны все")
                # Не создаём неограниченное число задач для большого индекса.
                chosen = sorted(dict.fromkeys(chosen), key=self._submap_rank)[:getattr(self.config, 'sitemap_max_requests', 128)]
                tasks = [self.parse_sitemap(urljoin(sitemap_url, loc), depth + 1, max_depth) for loc in chosen]

                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    unique_rows = {}
                    for result in results:
                        if isinstance(result, list):
                            for item_number, row in enumerate(result):
                                if item_number % 128 == 0:
                                    await asyncio.sleep(0)
                                previous = unique_rows.get(row[1])
                                if previous is None or row[3] > previous[3]:
                                    unique_rows[row[1]] = row
                    all_urls = list(unique_rows.values())
                return all_urls
            
            # Технический потолок чтения отделён от квоты обхода: сохраняем
            # ранжированные кандидаты, включая резерв для пополнения очереди.
            url_tags = [node for node in root if node.tag.rsplit('}', 1)[-1] == 'url']
            scanned_tags = url_tags[:self.config.sitemap_max_scan_urls]
            broken_locs = 0
            for item_number, url_tag in enumerate(scanned_tags):
                if item_number % 128 == 0:
                    await asyncio.sleep(0)
                loc_tag = next((node for node in url_tag if node.tag.rsplit('}', 1)[-1] == 'loc'), None)
                if loc_tag is not None and loc_tag.text:
                    url = loc_tag.text.strip()
                    # D151: <loc> без хоста чиним по адресу самой карты ДО категоризации
                    # и до постановки в очередь; неисправимые отбрасываем
                    url = self._repair_loc(url, sitemap_url)
                    if not url:
                        broken_locs += 1
                        continue
                    normalized_url = self.url_normalizer.normalize_url(url)

                    # Пропускаем URL с не-русскими языковыми префиксами
                    if self.url_categorizer.should_exclude_by_language(url):
                        continue

                    category, priority = self.url_categorizer.categorize_url(url)
                    
                    # Повышаем приоритет для URL из карты сайта. D208: потолок 10 снят —
                    # он схлопывал товар, категорию и контакты в один приоритет, и
                    # пересортировка очереди переставала что-либо различать.
                    enhanced_priority = priority + 1

                    # D120: в очередь уйдёт ОПУБЛИКОВАННЫЙ адрес, нормализованный — ключ
                    all_urls.append((url, normalized_url, category, enhanced_priority))

            if broken_locs:
                log.warning(f"Карта сайта {sitemap_url}: отброшено {broken_locs} "
                            f"неисправимых <loc> (пустой путь)")
            if len(url_tags) > len(scanned_tags):
                log.warning(f"Карта сайта {sitemap_url}: {len(url_tags)} адресов, "
                            f"просканировано {len(scanned_tags)} (потолок sitemap_max_scan_urls)")
            all_urls.sort(key=self._sitemap_rank_key)
            log.info(f"Извлечено {len(all_urls)} URL из XML карты сайта")
            return all_urls
            
        except Exception as e:
            log.error(f"Ошибка парсинга XML карты сайта: {e}")
            return []
    
    async def _parse_html_sitemap(self, content: str, sitemap_url: str) -> List[Tuple[str, str, str, int]]:
        """Парсинг HTML карты сайта"""
        try:
            soup = await self._html_tree(content, sitemap_url)
            if self._is_sitemap_error_html(soup):
                log.warning(f"Вместо HTML карты сайта получена заглушка: {sitemap_url}")
                return []
            all_urls = []
            base_domain = urlparse(sitemap_url).scheme + "://" + urlparse(sitemap_url).netloc
            
            # Ищем все ссылки в HTML
            for item_number, link in enumerate(soup.find_all('a', href=True)[:self.config.sitemap_max_scan_urls]):
                if item_number % 128 == 0:
                    await asyncio.sleep(0)
                href = sanitize_href(link['href'])  # D257/D233
                if href and not href.startswith(('#', 'javascript:', 'mailto:')):
                    full_url = urljoin(base_domain, href)

                    if self.url_categorizer.should_exclude_by_language(full_url):
                        continue

                    normalized_url = self.url_normalizer.normalize_url(full_url)
                    
                    category, priority = self.url_categorizer.categorize_url(full_url)
                    # D208: потолок 10 снят (см. _parse_xml_sitemap)
                    enhanced_priority = priority + 1

                    all_urls.append((full_url, normalized_url, category, enhanced_priority))

            log.info(f"Извлечено {len(all_urls)} URL из HTML карты сайта")
            return all_urls
            
        except Exception as e:
            log.error(f"Ошибка парсинга HTML карты сайта: {e}")
            return []
    
    async def _parse_text_sitemap(self, content: str, sitemap_url: str) -> List[Tuple[str, str, str, int]]:
        """Парсинг текстовой карты сайта"""
        try:
            all_urls = []
            base_domain = urlparse(sitemap_url).scheme + "://" + urlparse(sitemap_url).netloc
            
            for item_number, line in enumerate(content.split('\n')[:self.config.sitemap_max_scan_urls]):
                if item_number % 128 == 0:
                    await asyncio.sleep(0)
                line = line.strip()
                if line and not line.startswith('#') and self._is_valid_url(line):
                    full_url = urljoin(base_domain, line) if not line.startswith('http') else line

                    # Пропускаем URL с не-русскими языковыми префиксами
                    if self.url_categorizer.should_exclude_by_language(full_url):
                        continue
                    
                    normalized_url = self.url_normalizer.normalize_url(full_url)
                    
                    category, priority = self.url_categorizer.categorize_url(full_url)
                    # D208: потолок 10 снят (см. _parse_xml_sitemap)
                    enhanced_priority = priority + 1

                    all_urls.append((full_url, normalized_url, category, enhanced_priority))

            log.info(f"Извлечено {len(all_urls)} URL из текстовой карты сайта")
            return all_urls
            
        except Exception as e:
            log.error(f"Ошибка парсинга текстовой карты сайта: {e}")
            return []
    
    def _is_valid_url(self, url: str) -> bool:
        """Проверка валидности URL"""
        try:
            parsed = urlparse(url)
            return bool(parsed.netloc)
        except:
            return False
        
class ProductGridParser:
    """Парсер товарных сеток для извлечения ссылок на отдельные товары"""
    
    def __init__(self, url_categorizer: URLCategorizer):
        self.url_categorizer = url_categorizer
        # P04 U4 п.2 (D10): ссылка, найденная в товарной сетке, — это карточка,
        # подтверждённая разметкой листинга; она должна обходиться раньше самих
        # листингов и адресов карты сайта (диапазон приоритетов 10..13).
        self.grid_link_priority = getattr(
            getattr(url_categorizer, 'config', None), 'product_grid_link_priority', 14)
        self.product_grid_selectors = [
            '.cat-item', '.item', '.ms2_product', '.msoptionsprice-product',
            '.c-catalog-list__item', '.c-catalog-list__item-container',
            '.product-grid', '.products-grid', '.catalog-items', '.items-grid',
            '.product-list', '.products-list', '.product-card',
            '[class*="product"]', '[class*="item"]', '.goods-item',
            '.shop-item', '.store-item', '.catalog-item'
        ]
    
    def extract_product_links(self, soup: BeautifulSoup, base_url: str, current_url: str) -> List[Tuple[str, str, int]]:
        """Извлечение товарных ссылок из сеток категорий"""
        product_links = []
        
        try:
            link_base = document_base_url(soup, current_url)
            for selector in self.product_grid_selectors:
                try:
                    grid_elements = soup.select(selector)
                    for element in grid_elements:
                        links = self._extract_links_from_element(element, base_url, link_base)
                        product_links.extend(links)
                except Exception as e:
                    log.debug(f"Ошибка в селекторе {selector}: {e}")
                    continue
            
            # Удаление дубликатов
            unique_links = list(set(product_links))
            log.info(f"Найдено {len(unique_links)} товарных ссылок в сетках")
            
            return unique_links
            
        except Exception as e:
            log.error(f"Ошибка извлечения товарных ссылок: {e}")
            return []
    
    def _extract_links_from_element(self, element, base_url: str, current_url: str) -> List[Tuple[str, str, int]]:
        """Извлечение ссылок из элемента сетки"""
        links = []

        # Адреса плитки: обычный <a href> плюс переход через onclick/data-href/data-url
        # (D157) — у самой плитки и у элементов внутри неё
        raw_hrefs = [link['href'] for link in element.find_all('a', href=True)]
        try:
            for holder in [element] + element.select('[onclick], [data-href], [data-url]'):
                js_href = extract_element_url(holder)
                if js_href:
                    raw_hrefs.append(js_href)
        except Exception as e:
            log.debug(f"Ошибка разбора onclick в сетке: {e}")

        for raw_href in raw_hrefs:
            href = sanitize_href(raw_href)  # D257/D233
            if not href or href.startswith(('#', 'javascript:')):
                continue

            full_url = urljoin(current_url, href)

            # Проверяем, является ли ссылка товарной
            category, priority = self.url_categorizer.categorize_url(full_url)
            if category == 'product':
                # P04 U4 п.2 (D10): ссылкам из сетки — приоритет выше листингов
                links.append((full_url, category, max(priority, self.grid_link_priority)))

        return links
    
class FileDownloadManager:
    """Менеджер для скачивания файлов с обработкой пагинации"""
    
    def __init__(self, crawler, config: Config):
        self.crawler = crawler
        self.config = config
        self.processed_tabs = set()
        self.downloaded_files_cache = set()
        self.download_cache = FileDownloadCache(config.base_dir)
        self._download_locks = {}
        self.supported_extensions = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.rtf']
        # Общий лимит одновременных скачиваний файлов/картинок: при потоковой обработке
        # скачивания идут параллельно с краулом того же домена, суммарную нагрузку на сайт
        # держим ограниченной (краул <= max_concurrent_pages + файлы <= этого семафора)
        self.download_semaphore = asyncio.Semaphore(
            getattr(config, 'max_concurrent_file_downloads', 5))
    
    def is_downloadable_file(self, url: str) -> bool:
        """Проверяет, является ли URL ссылкой на файл для скачивания"""
        return self.crawler.url_categorizer.is_downloadable_file(url)

    def _is_same_domain(self, url: str, base_domain: str) -> bool:
        """Проверяет, принадлежит ли URL тому же домену"""
        return self.crawler._is_allowed_url(url, resource='file', base_url=base_domain)

    def _file_response(self, session, method: str, url: str, *, resource='file', base_url=None, conditional_url=None, **kwargs):
        """Проверяет каждый HTTP-переход до запроса и отклоняет HTML."""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def checked_response():
            current = url
            for _ in range(11):
                if not self.crawler._is_allowed_url(current, resource=resource, base_url=base_url):
                    raise ValueError(f'Файл вне периметра компании: {current}')
                request_kwargs = dict(kwargs)
                if conditional_url and current != conditional_url:
                    request_kwargs['headers'] = {key: value for key, value in kwargs.get('headers', {}).items()
                                                 if key not in {'If-None-Match', 'If-Modified-Since'}}
                async with session.request(method, current, allow_redirects=False, **request_kwargs) as response:
                    final_url = str(response.url)
                    if not self.crawler._is_allowed_url(final_url, resource=resource, base_url=base_url):
                        raise ValueError(f'Ответ файла вне периметра компании: {final_url}')
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get('Location')
                        if not location:
                            raise ValueError('Редирект файла без Location')
                        current = urljoin(final_url, location)
                        continue
                    content_type = response.headers.get('Content-Type', '').lower()
                    if response.status != 304 and resource == 'file' and ('text/html' in content_type or 'application/xhtml+xml' in content_type):
                        raise ValueError(f'HTML вместо файла: {final_url}')
                    yield response
                    return
            raise ValueError('Слишком много редиректов файла')

        return checked_response()

    def should_download_file(self, url: str, response_headers: dict = None, page_category: str = None) -> bool:
        """Определяет, нужно ли скачивать файл на основе категории страницы"""
        # Для AI-текстов - разрешаем все файлы
        if page_category in ['ai_image']:
            return True
        
        # Для вкладок - только разрешенные форматы
        if not self.is_downloadable_file(url):
            return False
        
        # Определяем разрешенные категории для скачивания
        allowed_categories = ['price_list', 'certificates', 'instructions', 'documents']
        
        # Проверяем, принадлежит ли страница к разрешенным категориям
        if page_category not in allowed_categories:
            log.debug(f"Пропускаем файл {url} - страница категории {page_category} не разрешена для скачивания")
            return False
        
        # Проверяем Content-Type если доступен
        if response_headers:
            content_type = response_headers.get('content-type', '').lower()
            if 'text/html' in content_type:
                return False  # Это HTML, а не файл

        # Все проверки пройдены — файл разрешён к скачиванию
        return True

    async def process_certificates_tab(self, page, base_url: str, domain_dirs: Dict[str, str]) -> List[str]:
        """Обработка вкладки сертификатов с пагинацией"""
        return await self._process_documents_tab_generic(
            page, base_url, domain_dirs['certificates_dir'],
            tab_type="certificates",
            keywords=['Сертификаты', 'Certificates', 'Сертификат', 'Certificate']
        )
    
    async def process_instructions_tab(self, page, base_url: str, domain_dirs: Dict[str, str]) -> List[str]:
        """Обработка вкладки инструкций с пагинации"""
        return await self._process_documents_tab_generic(
            page, base_url, domain_dirs['instructions_dir'],
            tab_type="instructions", 
            keywords=['Инструкции', 'Instructions', 'Руководство', 'Manual', 'Инструкция', 'Instruction']
        )
            
    async def process_documents_tab(self, page, base_url: str, domain_dirs: Dict[str, str]) -> List[str]:
        """Обработка вкладки документов с пагинацией"""
        return await self._process_documents_tab_generic(
            page, base_url, domain_dirs['documents_dir'],
            tab_type="documents",
            keywords=['Документы', 'Documents', 'Документация', 'Documentation', 'Файлы', 'Files', 'Информация']
        )
    
    async def process_price_lists_tab(self, page, base_url: str, domain_dirs: Dict[str, str]) -> List[str]:
        """Обработка вкладки прайс-листов с пагинацией"""
        return await self._process_documents_tab_generic(
            page, base_url, domain_dirs['price_lists_dir'],
            tab_type="price_lists",
            keywords=['Прайс', 'Price', 'Прайс-лист', 'Price-list', 'Скачать прайс', 'Download price', 'Цены', 'Рекламные материалы', 'Каталоги']
        )    
    
    async def _process_documents_tab_generic(self, page, base_url: str, save_dir: str, 
                                           tab_type: str, keywords: List[str]) -> List[str]:
        """Общий метод обработки вкладок с документами"""        
        parsed_url = urlparse(base_url)
        domain_key = f"{parsed_url.netloc}_{tab_type}"
        
        if domain_key in self.processed_tabs:
            log.info(f"Вкладка {tab_type} уже обработана для домена {parsed_url.netloc}")
            return []
            
        self.processed_tabs.add(domain_key)
        
        # Кликаем по вкладке
        clicked = await self._click_tab_by_keywords(page, keywords)
        if not clicked:
            log.warning(f"Не удалось найти вкладку {tab_type} на {base_url}")
            return []
        
        # Скачиваем файлы с учетом пагинации
        downloaded_files = await self._download_files_with_pagination(page, base_url, save_dir, tab_type)
        log.info(f"Скачано {len(downloaded_files)} файлов с вкладки {tab_type} в папку {save_dir}")
        
        return downloaded_files
    
    async def _click_tab_by_keywords(self, page, keywords: List[str]) -> bool:
        """Клик по вкладке по ключевым словам"""
        # Глушим перекрывающие элементы типа scroll-to-top
        try:
            await page.evaluate("""
                const blockers = document.querySelectorAll('.scroll-top-wrapper');
                blockers.forEach(el => {
                    el.dataset._crawlerOriginalPointerEvents = el.style.pointerEvents || '';
                    el.style.pointerEvents = 'none';
                });
            """)
        except Exception as e:
            log.debug(f"Не удалось отключить scroll-top-wrapper: {e}")
        keywords_lower = [keyword.lower() for keyword in keywords]
        for keyword in keywords:
            try:
                selectors = [
                    f"//*[contains(text(), '{keyword}')]",
                    f"//a[contains(text(), '{keyword}')]",
                    f"//button[contains(text(), '{keyword}')]",
                    f"//span[contains(text(), '{keyword}')]",
                    f"//*[@title[contains(., '{keyword}')]]",
                    f"//*[@aria-label[contains(., '{keyword}')]]"
                ]
                
                for selector in selectors:
                    try:
                        elements = await page.query_selector_all(f"xpath={selector}")
                        
                        for element in elements:
                            try:
                                if await element.is_visible():
                                    text_content = await element.evaluate("el => el.textContent || ''")
                                    text_content_lower = text_content.lower()
                                
                                    # Проверяем, содержит ли текст любой из ключевых слов (без учета регистра)
                                    if any(kw in text_content_lower for kw in keywords_lower):
                                        await element.click()
                                        await page.wait_for_timeout(2000)
                                        log.info(f"Кликнут по вкладке: {keyword}")
                                        return True
                            except Exception as e:
                                log.debug(f"Не удалось кликнуть по элементу {keyword}: {e}")
                                continue
                    except Exception as e:
                        log.debug(f"Ошибка поиска по селектору {selector}: {e}")
                        continue
            except Exception as e:
                log.debug(f"Ошибка поиска вкладки {keyword}: {e}")
                continue
        
        return False
    
    async def _file_throttle(self, url: str) -> None:
        """Пер-хостовый интервал между скачиваниями файлов из профиля сайта
        (crawl.load.file_delay_ms); без профиля/при 0 — no-op."""
        prof = getattr(self.crawler, 'profile', None)
        throttle = getattr(self.crawler, 'host_throttle', None)
        if prof is not None and throttle is not None:
            await throttle.acquire(urlparse(url).netloc.lower(),
                                   prof.crawl.load.file_delay_ms, 'files')

    def _file_timeout(self, default_s: int) -> int:
        """Таймаут скачивания файла: профиль сайта (crawl.load.file_timeout_s) -> прежний дефолт."""
        prof = getattr(self.crawler, 'profile', None)
        if prof is not None and prof.crawl.load.file_timeout_s:
            return prof.crawl.load.file_timeout_s
        return default_s

    async def get_image_size(self, image_url: str) -> Optional[int]:
        """Получение размера изображения в байтах по URL (под общим лимитом скачиваний)"""
        async with self.download_semaphore:
            await self._file_throttle(image_url)
            return await self._get_image_size_unlimited(image_url)

    async def _get_image_size_unlimited(self, image_url: str) -> Optional[int]:
        """Получение размера изображения в байтах по URL"""
        try:
            timeout = aiohttp.ClientTimeout(total=self._file_timeout(10))
            connector = aiohttp.TCPConnector(ssl=False)
            
            async with aiohttp.ClientSession(
                timeout=timeout, 
                connector=connector,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Referer": image_url,
                }
            ) as session:
                
                async with self._file_response(session, 'HEAD', image_url) as response:
                    if response.status == 200:
                        # Пробуем получить размер из Content-Length
                        content_length = response.headers.get('Content-Length')
                        if content_length and content_length.isdigit():
                            return int(content_length)
                        
                        # Если размер не указан в заголовках, получаем небольшой кусок для анализа
                        try:
                            # Делаем GET запрос с ограниченным размером
                            async with self._file_response(session, 'GET', image_url, headers={'Range': 'bytes=0-1024'}) as partial_response:
                                if partial_response.status in (200, 206):
                                    # Если поддерживается частичная загрузка, получаем общий размер
                                    content_range = partial_response.headers.get('Content-Range')
                                    if content_range:
                                        # Формат: bytes 0-1024/123456
                                        match = re.search(r'/(\d+)', content_range)
                                        if match:
                                            return int(match.group(1))
                                    else:
                                        # Если нет Content-Range, получаем весь контент маленького файла
                                        if int(partial_response.headers.get('Content-Length', 0)) <= 1024:
                                            content = await partial_response.read()
                                            return len(content)
                        except Exception as e:
                            log.debug(f"Ошибка получения размера через частичную загрузку {image_url}: {e}")
                        
        except asyncio.TimeoutError:
            log.warning(f"Таймаут при получении размера изображения {image_url}")
        except aiohttp.ClientError as e:
            log.warning(f"Ошибка клиента при получении размера изображения {image_url}: {e}")
        except Exception as e:
            log.warning(f"Неожиданная ошибка при получении размера изображения {image_url}: {e}")
        
        return None
    
    async def download_image_with_size_check(self, image_url: str, save_dir: str, page_category: str = 'ai_image') -> Optional[str]:
        """
        Скачивание изображения с единственной проверкой — минимальный размер файла.
        Периметр URL и тип ответа проверяются при каждом HTTP-запросе.
        """
        try:
            # 1. Пытаемся получить размер изображения
            image_size_bytes = await self.get_image_size(image_url)

            # 2. Если размер не удалось получить — всё равно скачиваем
            if image_size_bytes is None:
                log.info(f"Не удалось получить размер изображения {image_url}, но пробуем скачать")
                return await self._download_aiohttp(image_url, save_dir, page_category)

            # 3. Проверяем минимальный размер (например, 1 KB)
            min_size_bytes = self.config.min_image_size_kb * 1024  # например, 10 KB
            if image_size_bytes < min_size_bytes:
                log.info(
                    f"Изображение слишком маленькое: {image_url} "
                    f"({image_size_bytes} байт < {min_size_bytes} байт), пропускаем"
                )
                return None

            # 4. Размер нормальный — скачиваем
            log.debug(
                f"Размер изображения {image_url}: {image_size_bytes} байт "
                f"(≥ {min_size_bytes} байт), скачиваем"
            )
            return await self._download_aiohttp(image_url, save_dir, page_category)

        except Exception as e:
            log.error(f"Ошибка при скачивании изображения {image_url}: {e}")
            return None

        
    async def _download_files_with_pagination(self, page, base_url: str, save_dir: str, tab_type: str) -> List[str]:
        """Улучшенное скачивание файлов с учетом различных типов пагинации"""
        downloaded_files = []
        current_page = 1
        max_pages = self.config.max_pagination_depth
        
        # Получаем текущий URL для анализа параметров пагинации
        current_url = page.url
        
        while current_page <= max_pages:
            log.info(f"Обработка страницы {current_page} пагинации для вкладки {tab_type}")
            
            # Ждем загрузки контента и появления элементов пагинации
            await page.wait_for_timeout(2000)
            
            # Ожидаем появления возможных элементов пагинации
            await self._wait_for_pagination_elements(page)
            
            # Скачиваем файлы с текущей страницы
            page_files = await self._download_files_from_current_page(page, base_url, save_dir, tab_type)
            downloaded_files.extend(page_files)
            
            log.info(f"На странице {current_page} найдено {len(page_files)} файлов")
            
            # Пробуем различные методы поиска следующей страницы
            next_page_found = False
            
            # 1. Поиск через кнопки пагинации (основной метод)
            next_button = await self._find_next_page_button(page)
            if next_button:
                try:
                    await next_button.scroll_into_view_if_needed()
                    await page.wait_for_timeout(1000)
                    
                    await next_button.click()
                    await page.wait_for_timeout(1000)  # Увеличиваем время ожидания после клика
                    
                    current_page += 1
                    next_page_found = True
                    log.info(f"Переход на страницу {current_page} через кнопку пагинации")
                    
                except Exception as e:
                    log.warning(f"Ошибка перехода через кнопку пагинации: {e}")
            
            # 2. Поиск через параметры URL (резервный метод)
            if not next_page_found:
                next_page_url = await self._find_next_page_by_url_parameters(page, current_url, current_page)
                if next_page_url:
                    try:
                        # ИЗМЕНЕНО: переход с domcontentloaded + короткий networkidle
                        if not self.crawler._is_allowed_url(next_page_url, base_url=base_url):
                            break
                        await page.goto(next_page_url, wait_until='domcontentloaded', timeout=20000)
                        try:
                            await page.wait_for_load_state('networkidle', timeout=3000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(1000)
                        
                        current_page += 1
                        next_page_found = True
                        log.info(f"Переход на страницу {current_page} через параметры URL: {next_page_url}")
                        
                    except Exception as e:
                        log.warning(f"Ошибка перехода через параметры URL: {e}")
            
            # 3. Поиск через JavaScript пагинацию
            if not next_page_found:
                js_next_found = await self._try_javascript_pagination(page, current_page)
                if js_next_found:
                    current_page += 1
                    next_page_found = True
                    log.info(f"Переход на страницу {current_page} через JavaScript")
            
            # Если следующая страница не найдена, завершаем пагинацию
            if not next_page_found:
                log.info(f"Кнопка следующей страницы не найдена, завершаем пагинацию для {tab_type}")
                break
        
        log.info(f"Завершена пагинация для {tab_type}, обработано {current_page} страниц")
        return downloaded_files

    async def _wait_for_pagination_elements(self, page, timeout: int = 5000):
        """Ожидание появления элементов пагинации"""
        try:
            # Селекторы для элементов пагинации
            pagination_selectors = [
                ".pagination",
                "[class*='pagination']",
                ".pager",
                "[class*='pager']",
                ".page-links",
                "[class*='page']"
            ]
            
            for selector in pagination_selectors:
                try:
                    await page.wait_for_selector(selector, timeout=1000)
                except:
                    continue
                    
        except Exception as e:
            log.debug(f"Ошибка при ожидании элементов пагинации: {e}")

    async def _find_next_page_button(self, page):
        """Поиск кнопки следующей страницы"""
        next_selectors = [
            # Стандартные селекторы
            "a.pagination__next", ".pagination .next", ".pagination [aria-label='Next']",
            "[class*='next']", "a:has-text('Далее')", "a:has-text('Next')", 
            "a:has-text('Следующая')", "button:has-text('Next')", ".js-pagination-next",
            ".pagination-next", "li.next a", ".page-item.next a", "a[rel='next']",
            "a:has-text('Вперёд')", "button:has-text('Вперёд')",
            "a:has-text('>')", "button:has-text('>')",
            "[class*='forward']", "[class*='вперёд']",
            
            # Селекторы для номеров страниц
            f"a:has-text('{page.evaluate('() => window.paginationCurrentPage + 1')}')",
            ".pagination a.active + a",
            
            # Селекторы для стрелок
            ".arrow-right", "[class*='arrow-right']",
            ".fa-chevron-right", ".fa-angle-right",
            ".icon-arrow-right", "[class*='icon-arrow-right']"
        ]
        
        for selector in next_selectors:
            try:
                if '//' in selector:
                    element = await page.query_selector(f"xpath={selector}")
                else:
                    element = await page.query_selector(selector)
                    
                if element:
                    is_visible = await element.is_visible()
                    is_disabled = await element.is_disabled()
                    
                    if is_visible and not is_disabled:
                        log.info(f"Найдена кнопка следующей страницы: {selector}")
                        return element
            except Exception as e:
                log.debug(f"Ошибка поиска по селектору {selector}: {e}")
                continue
        
        return None

    async def _find_next_page_by_url_parameters(self, page, base_url: str, current_page: int) -> Optional[str]:
        """Поиск следующей страницы через параметры URL"""
        try:
            parsed_url = urlparse(base_url)
            query_params = parse_qs(parsed_url.query)
            
            # Паттерны параметов пагинации
            pagination_params = ['page', 'p', 'PAGEN', 'PAGEN_1', 'pg', 'pagen', 'pagina']
            
            for param in pagination_params:
                if param in query_params:
                    # Увеличиваем номер страницы
                    query_params[param] = [str(current_page + 1)]
                    
                    # Формируем новый URL
                    new_query = urlencode(query_params, doseq=True)
                    next_url = urlunparse((
                        parsed_url.scheme,
                        parsed_url.netloc,
                        parsed_url.path,
                        parsed_url.params,
                        new_query,
                        parsed_url.fragment
                    ))
                    
                    # Проверяем, существует ли страница
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with self._file_response(
                                    session, 'HEAD', next_url, resource='page',
                                    base_url=base_url, timeout=5) as response:
                                if response.status == 200:
                                    return next_url
                    except:
                        continue
            
            return None
            
        except Exception as e:
            log.debug(f"Ошибка поиска следующей страницы через параметры URL: {e}")
            return None

    async def _try_javascript_pagination(self, page, current_page: int) -> bool:
        """Попытка перехода на следующую страницу через JavaScript"""
        try:
            # Пробуем выполнить JavaScript для перехода на следующую страницу
            next_page_found = await page.evaluate("""(currentPage) => {
                // Ищем элементы пагинации
                const paginationElements = document.querySelectorAll([
                    '.pagination a', '.pager a', '[class*="page"] a', 
                    '.pagination button', '.pager button'
                ].join(','));
                
                for (let element of paginationElements) {
                    const text = element.textContent?.trim();
                    const href = element.getAttribute('href');
                    
                    // Ищем ссылку на следующую страницу
                    if (text === (currentPage + 1).toString() || 
                        text === '>' || text === 'Next' || text === 'Далее' ||
                        (href && href.includes(`page=${currentPage + 1}`)) ||
                        (href && href.includes(`PAGEN_1=${currentPage + 1}`))) {
                        
                        element.click();
                        return true;
                    }
                }
                return false;
            }""", current_page)
            
            if next_page_found:
                await page.wait_for_timeout(1000)
                return True
                
        except Exception as e:
            log.debug(f"Ошибка JavaScript пагинации: {e}")
        
        return False    
    
    async def _download_files_from_current_page(self, page, base_url: str, save_dir: str, tab_type: str) -> List[str]:
        """Скачивание файлов с текущей страницы"""
        downloaded_files = []
        
        try:
            all_links = await page.evaluate("""() => {
                const links = [];
                const anchorElements = document.querySelectorAll('a[href]');
                
                anchorElements.forEach(link => {
                    const href = link.href;
                    const text = link.textContent.trim();
                    const innerHTML = link.innerHTML;
                    
                    links.push({
                        url: href,
                        text: text,
                        innerHTML: innerHTML
                    });
                });
                
                return links;
            }""")
            
            # Фильтруем ссылки: оставляем только те, которые являются файлами для скачивания и того же домена
            file_links = []
            for link_info in all_links:
                file_url = link_info['url']
                if (self.is_downloadable_file(file_url) and
                    self._is_same_domain(file_url, base_url) and
                    self._has_supported_extension(file_url)):
                    file_links.append(link_info)
            
            for link_info in file_links:
                file_url = link_info['url']
                
                if file_url in self.downloaded_files_cache:
                    continue
                    
                try:
                    # Скачиваем файл через aiohttp с передачей категории вкладки
                    file_path = await self._download_aiohttp(file_url, save_dir, tab_type)
                    if file_path:
                        downloaded_files.append(file_path)
                        self.downloaded_files_cache.add(file_url)
                        
                except Exception as e:
                    log.error(f"Ошибка скачивания файла {file_url}: {e}")
                    continue
                    
        except Exception as e:
            log.error(f"Ошибка извлечения файлов со страницы: {e}")
        
        return downloaded_files
    
    def _has_supported_extension(self, url: str) -> bool:
        """Проверяет, имеет ли URL поддерживаемое расширение документа"""
        try:
            parsed = urlparse(url)
            path = parsed.path.lower()
            
            for ext in self.supported_extensions:
                if path.endswith(ext):
                    return True
            return False
        except Exception as e:
            log.debug(f"Ошибка проверки расширения {url}: {e}")
            return False
        
    def extract_document_links(self, html: str, page_url: str) -> List[str]:
        """Ссылки на файлы документов со страницы: только строгое расширение
        (.pdf/.doc/.docx/.xls/.xlsx/.rtf) в пути URL, относительные ссылки
        абсолютизируются, дубли отбрасываются."""
        try:
            soup = BeautifulSoup(html, 'lxml')
        except Exception:
            soup = BeautifulSoup(html, 'html.parser')
        links, seen = [], set()
        for a in soup.find_all('a', href=True):
            full_url = urljoin(document_base_url(soup, page_url), a['href'].strip()).split('#', 1)[0]
            if not full_url or full_url in seen:
                continue
            seen.add(full_url)
            path = urlparse(full_url).path.lower()
            if (any(path.endswith(ext) for ext in self.supported_extensions)
                    and self._is_same_domain(full_url, page_url)):
                links.append(full_url)
        return links

    async def download_document_files(self, html: str, page_url: str, save_dir: str) -> List[str]:
        """Скачивает файлы документов со страницы в папку компании
        (Certificates/Documents/Instructions/Price_lists). Возвращает пути скачанных."""
        saved = []
        links = self.extract_document_links(html, page_url)
        if not links:
            return saved
        os.makedirs(save_dir, exist_ok=True)
        for file_url in links:
            if file_url in self.downloaded_files_cache:
                continue
            file_path = await self._download_aiohttp(file_url, save_dir)
            if file_path:
                self.downloaded_files_cache.add(file_url)
                saved.append(file_path)
        return saved

    async def _download_aiohttp(self, file_url: str, save_dir: str, page_category: str = None) -> Optional[str]:
        """Универсальное скачивание файлов через aiohttp (под общим лимитом скачиваний)"""
        # Один файл/каталог не записывается одновременно из нескольких карточек.
        key = os.path.abspath(save_dir)
        async with self._download_locks.setdefault(key, asyncio.Lock()):
            async with self.download_semaphore:
                await self._file_throttle(file_url)
                return await self._download_aiohttp_unlimited(file_url, save_dir, page_category)

    async def _download_aiohttp_unlimited(self, file_url: str, save_dir: str, page_category: str = None) -> Optional[str]:
        """Универсальное скачивание файлов через aiohttp (из Fallback)"""
        try:
            _ptt_t0 = time.perf_counter()
            entry = self.download_cache.load(save_dir, file_url)
            local = self.download_cache.usable_path(save_dir, entry)
            conditional_headers = {}
            if local:
                if entry.get('etag'):
                    conditional_headers['If-None-Match'] = entry['etag']
                elif entry.get('last_modified'):
                    conditional_headers['If-Modified-Since'] = entry['last_modified']
            timeout = aiohttp.ClientTimeout(total=self._file_timeout(30))
            connector = aiohttp.TCPConnector(limit=5, ssl=False)
            
            async with aiohttp.ClientSession(
                timeout=timeout, 
                connector=connector,
                headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": file_url,
            "Connection": "keep-alive"
            }
            ) as session:
                
                async with self._file_response(session, 'GET', file_url, headers=conditional_headers,
                                               conditional_url=entry.get('response_url', file_url)) as response:
                    if response.status == 304:
                        if (local and conditional_headers
                                and str(response.url) == entry.get('response_url', file_url)):
                            log.info("Файл не изменился (304): %s", file_url)
                            return str(local)
                        log.warning("304 без проверенной локальной копии: %s", file_url)
                        return None
                    if response.status == 200:
                        # Получаем имя файла
                        file_name = self._extract_filename_from_response(file_url, response.headers)
                        if not file_name:
                            log.warning(f"Не удалось извлечь имя файла для {file_url}")
                            return None
                        
                        file_path = self.download_cache.destination(save_dir, file_url, file_name, entry)
                        if file_path is None:
                            log.warning("Небезопасное имя файла: %s", file_name)
                            return None

                        # Скачиваем контент
                        content = await response.read()
                        if not content:
                            return None
                        prefix = content[:2048].decode('utf-8-sig', errors='replace').lower()
                        prefix = re.sub(r'^\s*(?:(?:<!--.*?-->|<\?xml.*?\?>)\s*)*', '', prefix, flags=re.S)
                        if (re.match(r'<(?:!doctype\s+html|html|head|body)(?:\s|>)', prefix)
                                or not self.crawler._is_allowed_url(str(response.url), resource='file')):
                            return None

                        # Проверяем размер файла
                        file_size_mb = len(content) / 1024 / 1024
                        if file_size_mb > self.config.max_file_size_mb:
                            log.warning(f"Файл {file_name} превышает максимальный размер: {file_size_mb:.2f} MB")
                            return None
                        
                        digest = hashlib.sha256(content).hexdigest()
                        # Сервер может игнорировать условные заголовки. Сверяем полные байты,
                        # включая старые файлы без метаданных, не меняя время равной копии.
                        if local and entry.get('sha256') == digest:
                            result_path = local
                        elif file_path.is_file() and file_sha256(file_path) == digest:
                            result_path = file_path
                        else:
                            self.download_cache.archive(file_path)
                            atomic_write(file_path, content)
                            result_path = file_path
                        previous_digest = entry.get('sha256')
                        if previous_digest != digest:
                            entry = {}
                        entry.update(url=file_url, response_url=str(response.url), filename=file_path.name, sha256=digest,
                                     etag=response.headers.get('ETag'),
                                     last_modified=response.headers.get('Last-Modified'),
                                     checked_at=datetime.now().isoformat())
                        try:
                            self.download_cache.save(save_dir, file_url, entry)
                        except OSError as error:
                            log.warning("Файл сохранён, но кэш загрузки недоступен: %s", error)

                        log.info(f"Файл успешно скачан: {file_name} ({file_size_mb:.2f} MB)")
                        _ptt = getattr(self.crawler, 'processing_tracker', None)
                        if _ptt:
                            _ptt.add_clean('files', time.perf_counter() - _ptt_t0)
                        return str(result_path)
                    else:
                        log.warning(f"HTTP ошибка {response.status} для {file_url}")
                        return None
                        
        except asyncio.TimeoutError:
            log.warning(f"Таймаут при скачивании {file_url}")
        except aiohttp.ClientError as e:
            log.warning(f"Ошибка клиента при скачивании {file_url}: {e}")
        except Exception as e:
            log.warning(f"Неожиданная ошибка при скачивании {file_url}: {e}")
        
        return None
    
    def _extract_filename_from_response(self, file_url: str, response_headers) -> str:
        """Извлечение имени файла из URL и заголовков ответа"""
        filename = None
        
        # Пробуем извлечь из Content-Disposition
        content_disposition = response_headers.get('Content-Disposition', '')
        if isinstance(content_disposition, str) and 'filename=' in content_disposition:
            match = re.findall('filename="?([^"]+)"?', content_disposition)
            if match:
                filename = match[0]
        
        # Если не нашли в заголовках, извлекаем из URL
        if not filename:            
            path = urlparse(file_url).path
            filename = os.path.basename(path) if path else None
        
        # Декодируем URL-encoded символы
        if filename:
            try:
                filename = unquote(filename)
            except:
                pass
        
        return filename

class WebCrawler:
    """Веб-краулер с поддержкой всех типов страниц"""
    
    def __init__(self, config: Config, processing_tracker=None):
        self.config = config
        self.processing_tracker = processing_tracker
        self.url_categorizer = URLCategorizer(config)
        self.url_normalizer = SmartURLNormalizer(self.url_categorizer)
        self.product_grid_parser = ProductGridParser(self.url_categorizer)
        self.temp_storage = TempStorageManager( base_dir=os.path.join(config.base_dir, "temp_html_storage"),
            max_size_mb=config.temp_storage_max_size_mb,
            url_normalizer=self.url_normalizer.normalize_url)
        self.file_download_manager = FileDownloadManager(self, config)
        self.sitemap_parser = SiteMapParser(config, self.url_categorizer, self.url_normalizer,
                                            allowed_url=self._is_allowed_url)
        self.permanent_errors_cache = self._load_permanent_errors_cache()
        self._negative_cache_status = {}
        self._errors_cache_lock = asyncio.Lock()
        
        # Статистика
        self.stats = {
            'total_pages': 0,
            'attempted_pages': 0,
            'fetched_pages': 0,
            'resumed_pages': 0,
            'successful_urls': 0,
            'failed_attempts': 0,
            'processing_errors': 0,
            'product_pages': 0,
            'category_pages': 0,
            'price_list_pages': 0,  
            'main_pages': 0,
            'contact_pages': 0,
            'distributor_pages': 0,
            'other_pages': 0,
            'failed_pages': 0,
            'skipped_subdomains': 0,
            'total_size_mb': 0.0
        }
        
        # Ограничения
        self.max_depth = self.config.max_depth
        self.max_pages_per_site = self.config.max_pages_per_site
        self.max_product_pages_per_site = self.config.max_product_pages_per_site
        # Порог «пустого» Playwright-результата: если продуктовая страница вернула меньше символов,
        # пробуем aiohttp-фолбэк (Bitrix-категории/страницы часто отдают каркас Playwright, но полный HTML — GET).
        self.playwright_min_content = getattr(self.config, 'playwright_min_content', 1500)
        self.aiohttp_semaphore = Semaphore(config.max_concurrent_pages)
        # Профиль сайта текущей компании (site_profiles); None = generic-поведение.
        # Применяется в crawl_site -> _apply_site_profile.
        self.profile = None
        self.host_throttle = HostThrottle() if HostThrottle is not None else None
        self._effective_max_concurrent_pages = config.max_concurrent_pages
        # Коллектор метрик профилирования (site_profiles.profile_metrics);
        # устанавливается main.py на компанию. None = телеметрия выключена.
        self.metrics_collector = None
        # Потоковая обработка: опциональная asyncio.Queue; если задана, каждая сохранённая
        # страница дополнительно кладётся в неё сразу после записи в хранилище
        self.page_sink = None
        self.crawl_run_id = None
        self._resume_pages = {}

        self.browser_pool = BrowserPool(
            headless=True,
            max_concurrent_contexts=config.max_concurrent_pages,  # или задайте своё число
            stealth_enabled=config.stealth_context_enabled,
            stealth_user_agent=config.stealth_user_agent,
        )
           
        # Кэши
        self.visited_urls = set()
        self.processed_urls = set()
        self.product_urls = set()
        # D59 RC1: ключи схемного дедупа (домен+путь без схемы) — гасят ТОЛЬКО http↔https
        # одной и той же страницы при постановке в очередь (контент идентичен, безопасно).
        # Разные хосты и пути сохраняются до извлечения; общий слаг не доказывает дубль.
        # Успешное извлечение дополнительно проверяет полный адрес и стабильный product_id.
        self._crawled_scheme_keys = set()
        # D36: детект протухшего sitemap (soft-404 оболочки). Если N товарных страниц
        # дали подтверждённую одинаковую soft-404 — вычищаем товарный хвост
        # из очереди, чтобы не сжечь бюджет max_product_pages_per_site впустую.
        self._product_text_hash_counts = {}
        self._sitemap_soft404_seen = {}
        self._sitemap_hard_fail_seen = set()
        self._stale_sitemap_guard_tripped = False
        self.stale_sitemap_dup_threshold = getattr(self.config, 'stale_sitemap_dup_threshold', 10)
        # P02 U1: учёт товарной квоты. Слот занимается при ПОСТАНОВКЕ URL в очередь и
        # до сих пор не возвращался ни на одном пути неуспеха — сайт с мёртвой картой
        # сжигал все 50 слотов, не сохранив ни одной страницы.
        self._quota_released = set()          # ключи, по которым слот уже возвращён
        self._product_quota_reused = 0        # сколько раз слот переиспользован
        self.product_quota_reuse_limit = getattr(self.config, 'product_quota_reuse_limit', 50)
        self._quota_rejected_urls = []        # товарные адреса, отбитые гейтом квоты
        # P04 U4 п.2: доля товарной квоты, зарезервированная под карточки. Листинги
        # глубины 1 выбирали все 50 слотов раньше, чем обход доходил до карточек (D100).
        self.product_quota_card_reserve = getattr(
            self.config, 'product_quota_card_reserve', 0.3)
        # P04 U4 (хвост полосы B): признак «плоский сайт» считается один раз на компанию —
        # по карте сайта, а если её нет, по ссылкам первой разобранной страницы.
        self._flat_site_observed = False
        self._queue_gate_rejections = {}      # немые отказы гейтов очереди: причина -> счётчик
        self._sitemap_hard_fail_streak = 0    # подряд идущие жёсткие отказы товарных URL карты
        self.stale_sitemap_hard_fail_threshold = getattr(
            self.config, 'stale_sitemap_hard_fail_threshold', 15)
        # D120 п.7: опубликованные в карте сайта адреса, отличающиеся от своего
        # нормализованного ключа: {нормализованный ключ: адрес из <loc>}. Нужны, чтобы
        # при no_content/404 на другом написании того же ключа (напр. бесслешевом
        # стартовом пути) повторить запрос ровно опубликованным адресом.
        self._sitemap_published_urls = {}
        self._sitemap_pending = deque()
        self._sitemap_slots = set()
        # P01 U3: подтверждённая find_working_url рабочая форма хоста и схемы компании.
        # К ней приводятся хост и схема каждой собранной ссылки ДО постановки в очередь
        # (D87 — www-форма сохраняется, D134 — http поднимается до https, D270 — www-дубль
        # не создаётся). Пусто = приведения нет (fail-open).
        self._working_host = ''
        self._working_scheme = ''
        # P05 U3: пер-компанийный allowlist хостов сверх домена компании — поддомены-носители
        # каталога (D131) и подтверждённое зеркало на внешнем хосте (D124). Наследуют бюджет
        # страниц компании, без транзитивности (разрешённый хост сам никого не разрешает).
        self._allowed_hosts = set()
        self._skipped_link_hosts = {}      # отброшено гейтом периметра: хост -> счётчик
        self._mirror_autodetect_done = False
        # D40: ретраи страниц, не отдавших контент (таймауты медленных сайтов):
        # {normalized_url: (url, depth, category, attempts)}
        self._retry_due = {}
        self._retry_reasons = {}
        self._host_retry_until = {}
        self._successful_fetch_urls = set()
        self._usable_page_urls = set()
        self._terminal_crawl_reason = None
        self._depth_limited_urls = set()
        self._pagination_limited_urls = set()
        self._remaining_queue = []
        self._fetch_failures = {}
        self._page_retry_candidates = {}
        self._page_retry_attempts = {}
        self._queued_retries = set()
        self._quota_rejected_depths = {}
        self.page_retry_max_attempts = getattr(self.config, 'page_retry_max_attempts', 1)
        self.permanent_errors_cache = self._load_permanent_errors_cache()
        # Блокировки для потокобезопасности
        self._stats_lock = Lock()
        self._urls_lock = Lock()
        self._queue_lock = Lock()
        
        # Счетчики для мониторинга памяти
        self._pages_processed_since_last_check = 0
        self._last_memory_check = time.time()        
        self._last_memory_cleanup = 0.0

    @staticmethod
    def _soft404_text_signature(html: str) -> str:
        """D36: сигнатура видимого текста страницы (без <title>, скриптов и стилей).
        У soft-404 оболочек протухшего sitemap текст одинаковый при разных URL/title."""
        txt = re.sub(r'<title.*?</title>', ' ', html, flags=re.S | re.I)
        txt = re.sub(r'<(script|style).*?</\1>', ' ', txt, flags=re.S | re.I)
        txt = re.sub(r'<[^>]+>', ' ', txt)
        txt = re.sub(r'\s+', ' ', txt).strip()
        return hashlib.md5(txt.encode('utf-8', errors='ignore')).hexdigest()

    async def _check_stale_sitemap_guard(self, html: str, url: str, queue: deque,
                                         *, outcome=None) -> None:
        """Считает разные адреса карты с подтверждённой одинаковой soft-404."""
        norm = self.url_normalizer.normalize_url(url)
        if (self._stale_sitemap_guard_tripped
                or norm not in self._sitemap_published_urls
                or (outcome is not None and outcome.reason != 'soft_not_found')
                or not is_soft_not_found(html)):
            return
        soup = BeautifulSoup(html, 'html.parser')
        if (has_product_island(html)
                or self.url_categorizer.is_product_page_by_content(soup, url)):
            return
        sig = self._soft404_text_signature(html)
        seen = self._sitemap_soft404_seen.setdefault(sig, set())
        seen.add(norm)
        n = len(seen)
        self._product_text_hash_counts[sig] = n
        if n < self.stale_sitemap_dup_threshold:
            return
        self._stale_sitemap_guard_tripped = True
        await self._purge_sitemap_tail(
            queue, f"Протухший sitemap: {n} разных товарных адресов с одинаковой soft-404 "
                   f"(последняя: {url}).")

    async def _purge_sitemap_tail(self, queue: deque, reason: str) -> None:
        """D36/D225: снимает из очереди необойдённые товарные URL карты сайта
        и освобождает их слоты товарной квоты. Вместе с product_urls/visited_urls
        снимается и схемный ключ D59 RC1 — иначе те же адреса, предложенные ссылками из
        каталога, отбиваются как «схемный дубль», и освобождённый бюджет некому занять."""
        pending = self._sitemap_pending
        self._sitemap_pending = deque(row for row in pending if row[2] != 'product')
        self.stats['sitemap_guard_reason'] = reason
        self.stats['sitemap_guard_pending_purged'] = len(pending) - len(self._sitemap_pending)
        purged = 0
        purged_keys = 0
        async with self._queue_lock:
            kept = deque()
            while queue:
                item = queue.popleft()
                q_url, q_depth, q_category = item[0], item[1], item[2]
                norm = self.url_normalizer.normalize_url(q_url)
                if q_category == 'product' and norm in self._sitemap_published_urls:
                    skey = self._scheme_dedup_key(q_url)
                    async with self._urls_lock:
                        self.product_urls.discard(norm)
                        self._sitemap_slots.discard(norm)
                        self.visited_urls.discard(norm)
                        if skey is not None and skey in self._crawled_scheme_keys:
                            self._crawled_scheme_keys.discard(skey)
                            purged_keys += 1
                    purged += 1
                else:
                    kept.append(item)
            queue.extend(kept)
        self.stats['sitemap_guard_purged'] = purged
        log.warning(f"{reason} Из очереди убрано {purged} товарных URL карты сайта "
                    f"(снято схемных ключей: {purged_keys}), бюджет освобождён для "
                    f"ссылок из каталога")

    async def _check_hard_fail_sitemap_guard(self, url: str, depth: int, category: str,
                                             queue: deque, *, outcome=None) -> None:
        """Серия подтверждённых 404/410 разных товарных адресов карты сайта."""
        norm = self.url_normalizer.normalize_url(url)
        if (self._stale_sitemap_guard_tripped or category != 'product'
                or norm not in self._sitemap_published_urls):
            return
        if (outcome is None or outcome.reason not in ('not_found', 'gone')
                or outcome.retryable):
            self._sitemap_hard_fail_streak = 0
            return
        if norm in self._sitemap_hard_fail_seen:
            return
        self._sitemap_hard_fail_seen.add(norm)
        self._sitemap_hard_fail_streak += 1
        if self._sitemap_hard_fail_streak < self.stale_sitemap_hard_fail_threshold:
            return
        self._stale_sitemap_guard_tripped = True
        await self._purge_sitemap_tail(
            queue, f"Карта сайта содержит отсутствующие страницы: {self._sitemap_hard_fail_streak} "
                   f"разных товарных адресов подряд с 404/410 (последний: {url}).")

    async def _release_product_quota_slot(self, normalized_url: str, reason: str) -> bool:
        """P02 U1 п.1: возврат слота товарной квоты на пути неуспеха товарной страницы.
        Потолок product_quota_reuse_limit обязателен: без него сайт со сплошными 404
        крутил бы квоту, пока не упрётся в max_pages_per_site."""
        async with self._urls_lock:
            if normalized_url not in self.product_urls or normalized_url in self._quota_released:
                return False
            if self._product_quota_reused >= self.product_quota_reuse_limit:
                return False
            self.product_urls.discard(normalized_url)
            self._sitemap_slots.discard(normalized_url)
            self._quota_released.add(normalized_url)
            self._product_quota_reused += 1
            reused = self._product_quota_reused
        log.debug(f"Возвращён слот товарной квоты ({reason}): {normalized_url} "
                  f"[{reused}/{self.product_quota_reuse_limit}]")
        return True

    def _product_quota_reject_reason(self, url: str) -> Optional[str]:
        """P04 U4 п.2: причина отказа гейта товарной квоты или None. Кроме исчерпания
        квоты появился резерв: последняя доля слотов (product_quota_card_reserve)
        отдаётся только адресам, похожим на карточку, — иначе листинги и разделы
        глубины 1 выбирают все 50 слотов раньше, чем обход дойдёт до карточек (D100).
        Вызывается там же, где раньше стояла проверка len(product_urls) >= квоты."""
        used = len(self.product_urls)
        limit = self.max_product_pages_per_site
        if used >= limit:
            return 'товарная квота'
        reserve = int(limit * self.product_quota_card_reserve)
        if reserve and used >= limit - reserve and not self.url_categorizer.is_product_card_url(url):
            return 'резерв квоты под карточки'
        return None

    async def release_product_slot(self, url: str) -> bool:
        """P04 U4 п.3: обратная связь «вердикт извлечения -> бюджет обхода». Потоковый
        воркер main.py зовёт этот метод, когда модель отвергла товарную страницу
        (Trash_418#): слот квоты возвращается тем же механизмом и под тем же потолком
        product_quota_reuse_limit, что и на путях неуспеха краула. В батч-режиме вызова
        нет — там извлечение идёт после обхода и возвращать слот уже некому."""
        try:
            normalized_url = self.url_normalizer.normalize_url(url)
        except Exception:
            return False
        return await self._release_product_quota_slot(normalized_url, 'вердикт Trash_418#')

    async def _release_listing_quota_slot(self, url: str, normalized_url: str,
                                          soup: BeautifulSoup) -> bool:
        """P04 U4 п.1: страница получила слот товарной квоты как карточка, но по форме
        URL это листинг, и DOM карточку не подтверждает (is_product_page_by_content) —
        слот возвращаем. Сама страница при этом сохраняется в своей категории, ссылки
        с неё уже собраны: возвращается только бюджет."""
        if not self.url_categorizer.is_listing_url(url):
            return False
        try:
            if self.url_categorizer.is_product_page_by_content(soup, url):
                return False
        except Exception as e:
            log.debug(f"Не удалось оценить страницу по содержимому {url}: {e}")
            return False
        return await self._release_product_quota_slot(
            normalized_url, 'страница-листинг, а не карточка')

    def _register_page_retry(self, url: str, depth: int, category: str, outcome=None) -> None:
        """Повтор только временного отказа; лимит попыток и серверная пауза сохраняются."""
        norm = self.url_normalizer.normalize_url(url)
        if outcome is not None and not outcome.retryable:
            self._page_retry_candidates.pop(norm, None)
            self._retry_due.pop(norm, None)
            self._retry_reasons.pop(norm, None)
            return
        attempts = self._page_retry_attempts.get(norm, 0)
        if attempts < self.page_retry_max_attempts:
            delay = max(min(2 ** (attempts + 1), 30), outcome.retry_after) if outcome else 0
            self._page_retry_candidates[norm] = (url, depth, category, attempts + 1)
            self._retry_due[norm] = time.monotonic() + delay
            self._retry_reasons[norm] = 'delay'

    async def _enqueue_page_retries(self, queue):
        for norm, (url, depth, category, attempt) in list(self._page_retry_candidates.items()):
            if self._retry_due.get(norm, 0) > time.monotonic():
                continue
            if self._queue_url_rejection(url, depth, category):
                self._retry_reasons[norm] = 'scope'
                self._sitemap_slots.discard(norm)
                if category == 'product':
                    await self._release_product_quota_slot(norm, 'повтор вне допуска')
                continue
            if await self._should_add_to_queue_parallel(url, depth, category, retry=True):
                self._page_retry_candidates.pop(norm, None)
                self._retry_due.pop(norm, None)
                self._retry_reasons.pop(norm, None)
                self._page_retry_attempts[norm] = attempt
                self._queued_retries.add(norm)
                queue.append((url, depth, category, 1))
                log.info(f"Повтор {attempt}/{self.page_retry_max_attempts}: {url}")
            else:
                self._retry_reasons[norm] = 'quota'

    def _retry_summary(self, queue=()):
        now = time.monotonic()
        budget = self.stats['total_pages'] >= self.max_pages_per_site
        self.stats['pending_retries'] = [
            {'url': row[0], 'attempt': row[3],
             'reason': 'page_budget' if budget else self._retry_reasons.get(norm, 'delay'),
             'retry_in': max(0, self._retry_due.get(norm, now) - now)}
            for norm, row in self._page_retry_candidates.items()]
        for url, depth, category, priority in queue:
            norm = self.url_normalizer.normalize_url(url)
            if norm in self._queued_retries:
                due = self._host_retry_until.get(urlparse(url).netloc.lower(), now)
                self.stats['pending_retries'].append({'url': url,
                    'attempt': self._page_retry_attempts.get(norm, 0),
                    'reason': 'page_budget' if budget else 'host_cooldown',
                    'retry_in': max(0, due - now)})
        self.stats['fetch_failures'] = list(self._fetch_failures.values())

    def _record_page_success(self, url, *, resumed=False):
        norm = self.url_normalizer.normalize_url(url)
        self._successful_fetch_urls.add(norm)
        self._page_retry_candidates.pop(norm, None)
        self._retry_due.pop(norm, None)
        self._retry_reasons.pop(norm, None)
        self.stats['successful_urls'] = len(self._successful_fetch_urls)
        key = 'resumed_pages' if resumed else 'fetched_pages'
        self.stats[key] += 1
        for failed in list(self._fetch_failures):
            if self.url_normalizer.normalize_url(failed) == norm:
                self._fetch_failures.pop(failed, None)
        self.stats['failed_pages'] = len({self.url_normalizer.normalize_url(u) for u in self._fetch_failures})

    def _record_page_failure(self, url, reason, *, processing=False):
        self.stats['processing_errors' if processing else 'failed_attempts'] += 1
        self._fetch_failures.setdefault(url, {'requested_url': url, 'reason': reason})
        self.stats['failed_pages'] = len({self.url_normalizer.normalize_url(u) for u in self._fetch_failures})

    def _finalize_crawl_status(self, queue=None, *, reason=None):
        """Полнота именно обхода: исчерпание очереди не равно исчерпанию лимита."""
        if queue is not None:
            self._remaining_queue = list(queue)
        queue = self._remaining_queue
        self._retry_summary(queue)
        successful = self._successful_fetch_urls
        pending_products = {self.url_normalizer.normalize_url(u) for u in
                            list(self._quota_rejected_urls) + list(self._quota_rejected_depths)} - self.processed_urls
        pending_map = {row[1] for row in self._sitemap_pending} - self.processed_urls
        depth = self._depth_limited_urls - self.processed_urls
        pagination = self._pagination_limited_urls - self.processed_urls
        self.stats['successful_urls'] = len(successful)
        self.stats['failed_pages'] = len({self.url_normalizer.normalize_url(u) for u in self._fetch_failures})
        self.stats['remaining_pages'] = len(queue)
        self.stats['remaining_sitemap_urls'] = len(pending_map)
        self.stats['remaining_product_urls'] = len(pending_products)
        reasons = []
        if reason:
            self._terminal_crawl_reason = reason
        if self._terminal_crawl_reason:
            reasons.append(self._terminal_crawl_reason)
        if self.stats['total_pages'] >= self.max_pages_per_site and (queue or pending_map or pending_products or self._page_retry_candidates):
            reasons.append('page_limit')
        if self.stats.get('host_pause_pending') or any(self._host_paused(item[0]) for item in queue):
            reasons.append('host_cooldown')
        if self.stats.get('pending_retries'):
            reasons.append('pending_retries')
        if pending_products:
            reasons.append('product_limit')
        if pending_map:
            reasons.append('sitemap_limit')
        if depth:
            reasons.append('depth_limit')
        if pagination:
            reasons.append('pagination_limit')
        if self._stale_sitemap_guard_tripped:
            reasons.append('sitemap_guard')
        if self._fetch_failures:
            reasons.append('fetch_failures')
        if getattr(self.sitemap_parser, 'phase_timed_out', False) or any(
                state == 'error' for state in self.sitemap_parser._states.values()):
            reasons.append('sitemap_incomplete')
        self.stats['usable_pages'] = len(self._usable_page_urls)
        if not self._usable_page_urls:
            reasons.append('no_content')
        if queue and not reasons:
            reasons.append('remaining_queue')
        self.stats['crawl_status'] = 'failed' if not self._usable_page_urls else 'partial' if reasons else 'complete'
        self.stats['stop_reasons'] = list(dict.fromkeys(reasons))
        self.stats['stop_reason'] = reasons[0] if reasons else 'exhausted'
        return self.stats

    async def reset_state(self, company_name: str, *, crawl_run_id: str = None, resume: bool = False):
        """Новый мониторинг проверяет сайт; resume использует HTML только того же прохода."""
        if resume and not crawl_run_id:
            raise ValueError('Для возобновления необходим crawl_run_id')
        self.crawl_run_id = crawl_run_id or uuid4().hex
        self.temp_storage.crawl_run_id = self.crawl_run_id
        if _get_profile_resolver:
            _get_profile_resolver().reload_if_changed()
        self.temp_storage.reset_url_index()
        # Кэш в памяти относится только к одному обходу; следующий update обязан
        # перепроверить сервер по постоянным валидаторам, а не пропустить URL.
        self.file_download_manager.downloaded_files_cache.clear()
        self.file_download_manager.processed_tabs.clear()
        self.file_download_manager._download_locks.clear()
        self._resume_pages = {}
        await self.sitemap_parser.reset_run()
        self.current_base_url = None
        self._apply_site_profile('')
        self._sitemap_pending.clear()
        self._sitemap_slots.clear()
        # Отрицательные результаты прошлых проходов не доказывают текущий отказ.
        self.permanent_errors_cache.clear()
        self._negative_cache_status.clear()
        async with self._urls_lock:
            self.visited_urls.clear()
            self.processed_urls.clear()
            self.product_urls.clear()
            self._crawled_scheme_keys.clear()
            self._product_text_hash_counts.clear()
            self._sitemap_soft404_seen.clear()
            self._sitemap_hard_fail_seen.clear()
            self._retry_due.clear()
            self._retry_reasons.clear()
            self._host_retry_until.clear()
            self._fetch_failures.clear()
            self._successful_fetch_urls.clear()
            self._usable_page_urls.clear()
            self._terminal_crawl_reason = None
            self._depth_limited_urls.clear()
            self._pagination_limited_urls.clear()
            self._remaining_queue.clear()
            self._stale_sitemap_guard_tripped = False
            self._sitemap_published_urls.clear()
            self._quota_released.clear()
            self._product_quota_reused = 0
            self._quota_rejected_urls.clear()
            self._queue_gate_rejections.clear()
            self._sitemap_hard_fail_streak = 0
            self._page_retry_candidates.clear()
            self._page_retry_attempts.clear()
            self._queued_retries.clear()
            self._quota_rejected_depths.clear()
            # P05 U3: периметр обхода пер-компанийный
            self._allowed_hosts.clear()
            self._skipped_link_hosts.clear()
            self._mirror_autodetect_done = False
            self._flat_site_observed = False

        if resume:
            for item in await self.temp_storage.get_company_files(company_name):
                meta = item['metadata']
                if meta.get('crawl_run_id') != self.crawl_run_id:
                    continue
                for address in (meta.get('url'), meta.get('requested_url'), meta.get('normalized_url')):
                    if address:
                        self._resume_pages[self.url_normalizer.normalize_url(address)] = item
        await self._reset_stats()
        log.info(f"Режим обхода: {'возобновление' if resume else 'новая проверка'}, "
                 f"проход {self.crawl_run_id}, сохранённых адресов: {len(self._resume_pages)}")

    async def _reset_stats(self):
        """Сброс статистики (без очистки visited_urls, processed_urls, product_urls)"""
        async with self._stats_lock:
            self._successful_fetch_urls.clear()
            self._usable_page_urls.clear()
            self._fetch_failures.clear()
            self._remaining_queue.clear()
            self._depth_limited_urls.clear()
            self._pagination_limited_urls.clear()
            self._terminal_crawl_reason = None
            self.stats = {
                'total_pages': 0,
                'attempted_pages': 0,
                'fetched_pages': 0,
                'resumed_pages': 0,
                'successful_urls': 0,
                'failed_attempts': 0,
                'processing_errors': 0,
                'product_pages': 0,
                'category_pages': 0,
                'price_list_pages': 0,
                'main_pages': 0,
                'contact_pages': 0,
                'distributor_pages': 0,
                'other_pages': 0,
                'failed_pages': 0,
                'skipped_subdomains': 0,
                'total_size_mb': 0.0
            }        

    async def load_processed_urls_from_storage(self, company_name: str):
        """Загрузить обработанные URL из временного хранилища (альтернативное имя для reset_state)"""
        await self.reset_state(company_name)

    async def reset_crawler_state(self, company_name: str):
        """Сбросить состояние краулера для новой компании (альтернативное имя)"""
        await self.reset_state(company_name)

    async def initialize(self):
        """Инициализация краулера (вызывается из main.py)"""
        await self.browser_pool.initialize()
        
    async def close(self):
        """Закрытие ресурсов краулера (вызывается из main.py)"""
        await self.browser_pool.close()
        self._save_permanent_errors_cache()

    async def _check_memory_usage(self):
        """Проверка использования памяти и принудительная очистка при необходимости"""
        try:
            memory_mb = read_memory_usage_mb()
            if memory_mb <= self.config.memory_cleanup_threshold_mb:
                return

            now = time.time()
            if now - self._last_memory_cleanup < MEMORY_CLEANUP_COOLDOWN_SECONDS:
                return
            self._last_memory_cleanup = now

            # Питоновский RSS печатаем рядом: он показывает, чья это память —
            # питона или Chromium (по нему решать, что чинить дальше).
            rss_mb = psutil.Process().memory_info().rss / 1024 / 1024
            log.warning(f"Потребление памяти контейнером {memory_mb:.2f}MB (питон {rss_mb:.2f}MB) "
                        f"превышает порог {self.config.memory_cleanup_threshold_mb}MB. Выполняем очистку.")

            # Принудительный сбор мусора
            gc.collect()

            # Посещённые адреса, квоты, повторы и отрицательные результаты —
            # состояние обхода. Их очистка повторно открыла бы уже закрытые URL.

            log.info(f"Очистка памяти завершена. Текущее потребление: {read_memory_usage_mb():.2f}MB")

        except Exception as e:
            log.warning(f"Ошибка проверки памяти: {e}")

    def _load_permanent_errors_cache(self) -> set:
        """Кэш ограничен обходом; старый файл без статусов/сроков не используем."""
        return set()

    def _save_permanent_errors_cache(self):
        """Отказы текущего прохода не переносятся на следующий. Архив не трогаем."""
        pass

    @staticmethod
    def _negative_cache_key(url, transport):
        # Не нормализуем путь/схему: /catalog и /catalog/ могут отвечать по-разному.
        return transport, url.split('#', 1)[0]

    def _record_fetch_status(self, url, transport, status):
        key = self._negative_cache_key(url, transport)
        if status in PERMANENT_ERRORS:
            self.permanent_errors_cache.add(key)
            self._negative_cache_status[key] = status
        elif status == 200:
            # Успех другого транспорта опровергает предыдущий отрицательный ответ.
            self.permanent_errors_cache.difference_update(
                self._negative_cache_key(url, name) for name in ('aiohttp', 'curl', 'playwright'))
            for name in ('aiohttp', 'curl', 'playwright'):
                self._negative_cache_status.pop(self._negative_cache_key(url, name), None)

    def _sanitize_filename(self, filename: str) -> str:
        return sanitize_filename(filename)
    
    def create_site_directories(self, site_url: str, company_name: str = None, company_dir: str = None) -> Dict[str, str]:
        
        base_dir = company_dir            
        
        directories = {
            'base_domain_dir': base_dir,
            'instructions_dir': os.path.join(base_dir, 'Instructions'),
            'price_lists_dir': os.path.join(base_dir, 'Price_lists'),
            'documents_dir': os.path.join(base_dir, 'Documents'),
            'certificates_dir': os.path.join(base_dir, 'Certificates')
        }
        
        log.debug(f"Получены пути директорий для сайта: {list(directories.values())}")
            
        return directories

    async def crawl_site(self, site_url: str, company_name: str, company_dir: str) -> Dict[str, Any]:
        """Основной метод краулинга сайта с сохранением в временное хранилище"""
        log.info(f"Начинаем краулинг сайта: {site_url} для компании: {company_name}")
        
        # Профиль сайта: пер-сайтовые лимиты/стратегии (mvp/profiles/<домен>.yaml).
        # P05 U3 п.1: применяется ДО выбора рабочего URL и резолвится по исходному адресу
        # компании — его crawl.equivalent_domains / subdomain_collapse / strict_www должны
        # влиять на выбор базы обхода, а не только на то, что делать после.
        await self._reset_stats()
        self.current_base_url = site_url
        self._apply_site_profile(site_url)

        # Определяем рабочий URL с учетом эквивалентности доменов
        working_url = await self._get_working_url(site_url)

        # Сохраняем базовый URL для проверки поддоменов
        self.current_base_url = working_url

        # Профиль закреплён за исходной компанией. Разрешённое зеркало не может
        # подменить его своим профилем и транзитивно открыть новые домены/CDN.

        # P01 U3: рабочая форма хоста и схемы — после профиля, чтобы учесть его strict_www.
        self._set_working_base(working_url)

        # P04 U1 (D211, D260): базовая локаль обхода — языковой префикс рабочего URL
        # (или URL из Site_list). У зарубежного производителя национальная версия и
        # есть весь сайт, языковым фильтром её резать нельзя.
        self.url_categorizer.set_base_locale(
            self.url_categorizer.detect_locale_prefix(working_url)
            or self.url_categorizer.detect_locale_prefix(site_url))

        if self.profile is not None and self.profile.crawl.antibot == 'blocked':
            log.warning(f"Профиль {self.profile.domain}: antibot=blocked — сайт не "
                        f"обрабатывается (ждёт решения в REVIEW)")
            self._finalize_crawl_status(reason='blocked')
            return {**self.stats, 'error': 'antibot=blocked (профиль сайта)', 'stored_pages': [],
                    'original_url': site_url, 'working_url': working_url}

        # Машинный статус выбора базы обхода (parked_domain / redirect_shim / hoster_stub /
        # http_only / no_host) — пишется после сброса, чтобы не быть затёртым (P05 U2).
        if self.site_status:
            self.stats['site_status'] = self.site_status

        stored_pages = []  # Список сохраненных страниц
        
        try:
            domain_dirs = self.create_site_directories(site_url, company_name, company_dir)
            await self._start_crawling_with_sitemap_support(working_url, company_name, domain_dirs, stored_pages)                       
            self._save_permanent_errors_cache()

            self._finalize_crawl_status()
            # Сохраняем статистику
            await self._save_crawling_stats(site_url, domain_dirs)            
            log.info(f"Краулинг завершен для {site_url}. Сохранено страниц: {len(stored_pages)}")
            
            # Возвращаем результат с информацией о сохраненных страницах
            result = self.stats.copy()
            result['stored_pages'] = stored_pages
            result['original_url'] = site_url
            result['working_url'] = working_url
            return result
            
        except Exception as e:
            log.error(f"Ошибка краулинга сайта {working_url}: {e}")
            self._finalize_crawl_status(reason='internal_error')
            return {**self.stats, 'error': str(e), 'stored_pages': stored_pages, 'original_url': site_url, 'working_url': working_url}
    
    def _apply_site_profile(self, working_url: str) -> None:
        """Резолвит профиль сайта и применяет пер-сайтовые лимиты/настройки.
        Профиля нет (или он «пустой», или site_profiles недоступен) — все значения
        возвращаются к глобальным из config (профиль прошлой компании не протекает)."""
        self.profile = None
        self.max_depth = self.config.max_depth
        self.max_pages_per_site = self.config.max_pages_per_site
        self.max_product_pages_per_site = self.config.max_product_pages_per_site
        self.page_retry_max_attempts = getattr(self.config, 'page_retry_max_attempts', 1)
        self._effective_max_concurrent_pages = self.config.max_concurrent_pages
        self.aiohttp_semaphore = Semaphore(self.config.max_concurrent_pages)
        self.file_download_manager.download_semaphore = asyncio.Semaphore(
            getattr(self.config, 'max_concurrent_file_downloads', 5))
        self.url_categorizer.set_profile(None)
        if self.url_categorizer.domain_equivalency is not None:
            self.url_categorizer.domain_equivalency.set_profile_strict_www(None)
            self.url_categorizer.domain_equivalency.set_profile_domains(None)
        if _get_profile_resolver is None:
            return
        try:
            profile = _get_profile_resolver().resolve(working_url)
        except Exception as e:
            log.warning(f"Профиль сайта не разрезолвлен для {working_url}: {e}")
            return
        if profile.is_default():
            return
        self.profile = profile
        self.url_categorizer.set_profile(profile)
        # P01 U3 п.6: профильное поле crawl.strict_www (объявлено в схеме, но краулером
        # до сих пор не читалось) — домен отдаёт контент только на www.
        if profile.crawl.strict_www and self.url_categorizer.domain_equivalency is not None:
            self.url_categorizer.domain_equivalency.set_profile_strict_www(profile.domain)
            log.info(f"Профиль {profile.domain}: strict_www — хост канонизируется к www")
        # P05 U3 п.1: периметр обхода из профиля — зеркала и бренд-домены группы
        # (crawl.equivalent_domains) и поддомены (crawl.subdomain_collapse). До сих пор эти
        # поля влияли только на выбор профиля, а гейт периметра их не видел.
        if self.url_categorizer.domain_equivalency is not None and (
                profile.crawl.equivalent_domains or profile.crawl.subdomain_collapse):
            self.url_categorizer.domain_equivalency.set_profile_domains(
                profile.domain, profile.crawl.equivalent_domains,
                profile.crawl.subdomain_collapse)
            log.info(f"Профиль {profile.domain}: периметр обхода расширен на "
                     f"{profile.crawl.equivalent_domains or []} "
                     f"{profile.crawl.subdomain_collapse or []}")
        limits, load = profile.crawl.limits, profile.crawl.load
        if limits.pages:
            self.max_pages_per_site = limits.pages
        if limits.product_pages:
            self.max_product_pages_per_site = limits.product_pages
        if limits.depth:
            self.max_depth = limits.depth
        if load.page_retry_attempts is not None:
            self.page_retry_max_attempts = load.page_retry_attempts
        if load.max_concurrent_pages:
            self._effective_max_concurrent_pages = load.max_concurrent_pages
            self.aiohttp_semaphore = Semaphore(load.max_concurrent_pages)
        if load.max_concurrent_files:
            self.file_download_manager.download_semaphore = asyncio.Semaphore(
                load.max_concurrent_files)
        log.info(f"Применён профиль сайта {profile.domain} v{profile.profile_version} "
                 f"(source={profile.source}, tier={profile.extract.tier}, "
                 f"render={profile.crawl.render or 'auto'}, antibot={profile.crawl.antibot})")

    async def _get_working_url(self, url: str) -> str:
        """Получение рабочего URL с проверкой доступности и учетом эквивалентности доменов"""
        self.site_status = None
        if not self.config.domain_equivalency_enabled or not self.config.treat_http_https_as_same:
            return url

        try:
            # Используем DomainEquivalencyManager для поиска рабочего URL
            working_url = await self.url_categorizer.domain_equivalency.find_working_url(url)
            self.site_status = getattr(
                self.url_categorizer.domain_equivalency, 'last_site_status', None)
            if self.site_status:
                log.warning(f"Статус выбора базы обхода для {url}: {self.site_status}")

            if working_url != url:
                log.info(f"Используется рабочий URL: {working_url} вместо {url}")
            
            return working_url
        except Exception as e:
            log.warning(f"Ошибка поиска рабочего URL для {url}: {e}")
            return url
    
    def _set_working_base(self, working_url: str) -> None:
        """P01 U3 п.2: запоминает рабочую форму хоста и схемы, подтверждённую
        find_working_url. Хост берётся ДОСЛОВНО (нормализатор срезал бы www и вернул
        ровно тот дефект D87, из-за которого ссылки уходили на non-www); исключение —
        домены strict_www, для которых рабочей формой объявлена www."""
        self._working_host = ''
        self._working_scheme = ''
        parsed = urlparse(working_url or '')
        host = (parsed.netloc or '').lower()
        if not host or parsed.scheme not in ('http', 'https'):
            return
        de = getattr(self.url_categorizer, 'domain_equivalency', None)
        if de is not None:
            forced = de.normalize_domain(host)
            if forced.startswith('www.') and not host.startswith('www.'):
                host = forced
        self._working_host = host
        self._working_scheme = parsed.scheme
        log.info(f"Рабочая форма базы обхода: {self._working_scheme}://{self._working_host}")

    def _canonicalize_to_working_base(self, url: str) -> str:
        """P01 U3 п.2: приводит хост и схему ссылки к рабочей форме компании. Путь,
        параметры и фрагмент не трогаются.

        Приводится ТОЛЬКО ссылка на тот же хост в другой форме (апекс/www): www-дубль не создаётся (D270), www-форма не теряется (D87), http
        поднимается до подтверждённой https (D134). Явные зеркала профиля
        и прочие хосты остаются как есть. Fail-open: при любой ошибке возвращается исходный URL."""
        if not self._working_host or not url:
            return url
        try:
            parsed = urlparse(url)
            netloc = (parsed.netloc or '').lower()
            if parsed.scheme not in ('http', 'https') or not netloc or ':' in netloc:
                # нестандартная схема или явный порт — вмешиваться не во что
                return url
            if netloc == self._working_host and parsed.scheme == self._working_scheme:
                return url
            if not url_in_scope(url, f'{self._working_scheme}://{self._working_host}'):
                return url
            return urlunparse((self._working_scheme, self._working_host, parsed.path,
                               parsed.params, parsed.query, parsed.fragment))
        except Exception as e:
            log.debug(f"Не удалось привести {url} к рабочей базе: {e}")
            return url

    @staticmethod
    def _url_host(url: str) -> str:
        """Хост адреса в нижнем регистре, без порта."""
        try:
            return (urlparse(url).netloc or '').lower().split(':')[0]
        except Exception:
            return ''

    def _is_allowed_url(self, url: str, *, resource='page', base_url=None) -> bool:
        """Одна политика для очереди, всех транспортов, sitemap и загрузки файлов."""
        base = getattr(self, 'current_base_url', None) or base_url
        profile = self.profile
        domains, suffixes, files = [], [], []
        if profile is not None:
            domains = [profile.domain] + list(profile.crawl.equivalent_domains)
            suffixes = profile.crawl.subdomain_collapse
            files = getattr(profile.crawl, 'file_hosts', ())
        return url_in_scope(url, base, profile_domains=domains,
                            profile_suffixes=suffixes, file_hosts=files, resource=resource)

    def _in_crawl_perimeter(self, url: str, base_url: str) -> bool:
        return self._is_allowed_url(url, base_url=base_url)

    def _maybe_allow_subdomain(self, url: str, category: str, depth: int, base_url: str) -> bool:
        """Ссылки сайта больше не дают разрешение: дополнительные хосты задаёт профиль."""
        return False

    async def _maybe_autodetect_mirror_host(self, parse_result: ParseResult, depth: int,
                                            skipped_hosts: dict, skipped_examples: dict,
                                            product_links: int) -> bool:
        """Совпадение title/canonical не заменяет явное разрешение профиля."""
        return False

    def _is_allowed_final_url(self, requested_url: str, final_url: Optional[str]) -> bool:
        return (self._is_allowed_url(requested_url, base_url=requested_url)
                and self._is_allowed_url(final_url or requested_url, base_url=requested_url))

    def _log_effective_url(self, requested: str, final_url: Optional[str]) -> None:
        """P01 U3 п.5: в логе виден реально запрошенный адрес и конечный после редиректов
        (переписывание пути на www-зеркале не было видно вовсе — D270)."""
        if final_url and final_url != requested:
            log.info(f"Запрошен {requested} -> конечный URL после редиректов: {final_url}")

    async def _start_crawling_with_storage(self, site_url: str, company_name: str, 
                                         domain_dirs: Dict[str, str], stored_pages: List[Dict]):
        """Запуск процесса краулинга с сохранением в временное хранилище"""              

        queue = deque()
        start_urls = self._get_start_urls(site_url)
        
        for url in start_urls:
            category, priority = self.url_categorizer.categorize_url(url)
            if await self._should_add_to_queue_parallel(url, 0, category):
                queue.append((url, 0, category, priority))
        
        await self._drain_crawl_queue(queue, site_url, company_name, domain_dirs, stored_pages)

    async def _start_crawling_with_sitemap_support(self, site_url: str, company_name: str, 
                                         domain_dirs: Dict[str, str], stored_pages: List[Dict]):
        """Запуск процесса краулинга с поддержкой карты сайта"""
        queue = deque()        
        
        sitemap_urls = await self._discover_and_parse_sitemap(site_url)
        if sitemap_urls:
            # P04 U2 п.5 (хвост полосы B, F6 ревью слияния): набор URL хоста известен —
            # считаем признак «плоского» сайта до постановки адресов в очередь.
            # Элемент карты — 4-кортеж полосы A (адрес, ключ, роль, приоритет).
            self._flat_site_observed = True
            observed_rows = sitemap_urls + list(self._sitemap_pending)
            if self.url_categorizer.observe_urls(u for u, _n, _c, _p in observed_rows):
                # Признак включился уже ПОСЛЕ категоризации в парсере карты — роли
                # адресов пересчитываем, иначе на плоском сайте они остаются 'other'
                # и гейт сохранения выбрасывает страницы (D145, D228, D237).
                sitemap_urls = [(u, n) + self._recategorize_sitemap_row(u)
                                for u, n, _c, _p in sitemap_urls]
                self._sitemap_pending = deque(
                    (u, n) + self._recategorize_sitemap_row(u)
                    for u, n, _c, _p in self._sitemap_pending)
            await self._add_sitemap_urls_to_queue(queue, sitemap_urls, site_url)
            log.info(f"Добавлено {len(sitemap_urls)} URL из карты сайта")
        
        # 2. Добавляем стандартные стартовые URL
        start_urls = self._get_start_urls(site_url)
        added_standard_urls = 0
        for url in start_urls:
            category, priority = self.url_categorizer.categorize_url(url)
            if await self._should_add_to_queue_parallel(url, 0, category):
                queue.append((url, 0, category, max(priority - 2, 1)))
                added_standard_urls += 1

        log.info(f"Добавлено {added_standard_urls} стандартных URL, общий размер очереди: {len(queue)}")
        
        await self._drain_crawl_queue(queue, site_url, company_name, domain_dirs, stored_pages)

        # P02 U1 пп.4-5: сводка немых отказов гейтов очереди и страховка на случай,
        # когда квота потрачена, а товарных страниц не сохранено ни одной.
        if self._queue_gate_rejections:
            summary = ', '.join(f"{reason}: {count}"
                                for reason, count in sorted(self._queue_gate_rejections.items()))
            log.info(f"Отказы гейтов очереди за компанию — {summary}")
        if self._product_quota_reused:
            log.info(f"Слотов товарной квоты возвращено: {self._product_quota_reused} "
                     f"(потолок {self.product_quota_reuse_limit})")
        # P05 U3 п.6: без разреза по хостам класс «каталог на другом домене» не виден
        if self._skipped_link_hosts:
            top = sorted(self._skipped_link_hosts.items(), key=lambda item: item[1], reverse=True)
            log.info(f"Отброшено гейтом периметра ссылок: {sum(self._skipped_link_hosts.values())}; "
                     f"чаще всего — {', '.join(f'{host}: {count}' for host, count in top[:5])}")
        if self._allowed_hosts:
            log.info(f"Хосты сверх домена компании в периметре: "
                     f"{', '.join(sorted(self._allowed_hosts))}")

    async def _drain_crawl_queue(self, queue, site_url, company_name, domain_dirs, stored_pages):
        """Обычные URL, резерв и повторы обслуживаются до общего исчерпания работы."""
        active = set()
        quota_reconsidered = False
        waited = 0.0
        retry_turn = True
        try:
            while active or await self._get_total_pages() < self.max_pages_per_site:
                if await self._get_total_pages() < self.max_pages_per_site:
                    # Созревший повтор получает доступ до пополнения резерва карты.
                    await self._enqueue_page_retries(queue)
                    await self._refill_sitemap_queue(queue, site_url)
                if not active and await self._get_total_pages() >= self.max_pages_per_site:
                    break
                if not queue and not active:
                    self._apply_exclude_failopen(queue)
                    if not queue and not quota_reconsidered:
                        quota_reconsidered = True
                        await self._second_pass_for_quota_rejected(
                            company_name, domain_dirs, stored_pages, queue)
                    if not queue:
                        future = [due for norm, due in self._retry_due.items()
                                  if due > time.monotonic() and self._retry_reasons.get(norm) != 'scope']
                        if future and waited < 60:
                            delay = min(min(future) - time.monotonic(), 60 - waited)
                            await asyncio.sleep(max(0, delay))
                            waited += max(0, delay)
                            continue
                        for norm in self._page_retry_candidates:
                            if self._retry_due.get(norm, 0) > time.monotonic():
                                self._retry_reasons[norm] = 'delay_budget'
                        break
                # Активные задачи также резервируют общий бюджет: даже первый
                # параллельный батч не должен превышать остаток лимита страниц.
                remaining = self.max_pages_per_site - await self._get_total_pages() - len(active)
                slots = min(self._effective_max_concurrent_pages - len(active), remaining)
                async with self._queue_lock:
                    ordered = sorted(queue, key=lambda item: item[3], reverse=True)
                    queue.clear()
                    retries = deque()
                    fresh = deque()
                    for item in ordered:
                        target = retries if self.url_normalizer.normalize_url(item[0]) in self._queued_retries else fresh
                        target.append(item)
                    # Чередуем фактически запускаемые задачи; пауза хоста не
                    # расходует очередь другой группы и не меняет её приоритеты.
                    while slots > 0 and (retries or fresh):
                        pending = retries if retries and (retry_turn or not fresh) else fresh
                        item = pending.popleft()
                        url, depth, category, _ = item
                        if self._host_retry_until.get(urlparse(url).netloc.lower(), 0) > time.monotonic():
                            queue.append(item)
                            continue
                        slots -= 1
                        retry_turn = pending is fresh
                        active.add(asyncio.create_task(self._process_page_parallel(
                            url, depth, category, company_name, domain_dirs, queue, stored_pages)))
                    queue.extend(retries)
                    queue.extend(fresh)
                if active:
                    done, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                    await asyncio.gather(*done, return_exceptions=True)
                elif queue:
                    due = min(self._host_retry_until.get(urlparse(item[0]).netloc.lower(), 0) for item in queue)
                    delay = max(0, due - time.monotonic())
                    if waited >= 60:
                        self.stats['host_pause_pending'] = [item[0] for item in queue]
                        break
                    pause = min(delay, 60 - waited)
                    await asyncio.sleep(pause)
                    waited += pause
        except asyncio.CancelledError:
            self._terminal_crawl_reason = 'cancelled'
            raise
        finally:
            # Отмена обхода не оставляет фоновые задачи от предыдущей компании.
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            self._finalize_crawl_status(queue, reason='cancelled' if active else None)

    async def _second_pass_for_quota_rejected(self, company_name: str, domain_dirs: Dict[str, str],
                                              stored_pages: List[Dict], queue: deque) -> None:
        """Возвращает отложенные адреса в общий цикл только при свободной квоте."""
        if self.stats['product_pages'] or not self._quota_rejected_urls:
            return
        candidates = list(dict.fromkeys(self._quota_rejected_urls))
        self._quota_rejected_urls.clear()
        for url in candidates:
            depth = self._quota_rejected_depths.get(url, 1)
            if await self._should_add_to_queue_parallel(url, depth, 'product'):
                queue.append((url, depth, 'product', 1))

    def _apply_exclude_failopen(self, queue: deque) -> bool:
        """P04 U1 (п. 5): очередь обхода опустела — проверяем, не вырезал ли сайт целиком
        языковой фильтр или глобальный exclude-список. Если да, фильтр отключается для
        компании, а отложенные URL переклассифицируются и возвращаются в очередь
        (fail-open). Заодно отдаём доли отсева в метрики прогона. Вызывается только при
        пустой очереди и без активных задач, поэтому блокировки не требуются."""
        categorizer = self.url_categorizer
        if self.metrics_collector is not None:
            self.metrics_collector.record_url_filters(categorizer.filter_stats())
        released, deferred = categorizer.release_starved_filters()
        if not released:
            return False
        restored = 0
        for url, link_text in deferred:
            normalized_url = self.url_normalizer.normalize_url(url)
            if normalized_url in self.visited_urls:
                continue
            category, priority = categorizer.categorize_url(url, link_text)
            if self._queue_url_rejection(url, 1, category):
                continue
            # F8 ревью слияния: возвращённый товарный URL тоже занимает слот квоты —
            # иначе учёт полосы A по этим адресам не ведётся вовсе. Без await: хук
            # зовётся только при пустой очереди и нуле активных задач, гонки нет.
            if category == 'product':
                reason = self._product_quota_reject_reason(url)
                if reason:
                    self._count_queue_gate_rejection(reason)
                    self._quota_rejected_urls.append(url)
                    continue
                skey = self._scheme_dedup_key(url)
                if skey in self._crawled_scheme_keys:
                    continue
                if skey is not None:
                    self._crawled_scheme_keys.add(skey)
                self.product_urls.add(normalized_url)
            queue.append((url, 1, category, priority))
            self.visited_urls.add(normalized_url)
            restored += 1
        log.warning(f"Предохранитель фильтров: в обход возвращено {restored} отложенных URL")
        return restored > 0

    async def _process_page_parallel(self, url: str, depth: int, category: str, company_name: str,
                                   domain_dirs: Dict[str, str], queue: deque, stored_pages: List[Dict]):
        """Параллельная обработка отдельной страницы"""
        if await self._should_skip_url(url, depth, company_name, category):
            self._sitemap_slots.discard(self.url_normalizer.normalize_url(url))
            self._queued_retries.discard(self.url_normalizer.normalize_url(url))
            return
        
        norm = self.url_normalizer.normalize_url(url)
        retry = norm in self._queued_retries
        self._queued_retries.discard(norm)
        try:
            span = (self.processing_tracker.retry_span if retry else self.processing_tracker.clean_span) if self.processing_tracker else None
            with (span('crawler') if span else nullcontext()):
                stored_page = await self._process_page_with_storage(url, depth, category, company_name, domain_dirs, queue)
            if stored_page:
                stored_pages.append(stored_page)
                if self.page_sink is not None:
                    await self.page_sink.put(stored_page)

        except Exception as e:
            log.error(f"Ошибка обработки страницы {url}: {e}")
            self._record_page_failure(url, 'processing_error', processing=True)

    async def _get_total_pages(self) -> int:
        """Потокобезопасное получение общего количества страниц"""
        async with self._stats_lock:
            return self.stats['total_pages']

    @staticmethod
    def _sitemap_locale_prefix(site_url: str) -> Optional[str]:
        """D240: локаль-префикс из адреса компании в Site_list ('/ru-ru/', '/ru/').
        None — адрес без локали, фильтр не применяется."""
        try:
            first = (urlparse(site_url).path or '/').strip('/').split('/')[0].lower()
        except Exception:
            return None
        return first if re.fullmatch(r'[a-z]{2}(-[a-z]{2})?', first or '') else None

    @staticmethod
    def _url_has_locale(url: str, locale_prefix: str) -> bool:
        """URL принадлежит той же локали (или лежит в корне сайта)."""
        try:
            segments = (urlparse(url).path or '/').strip('/').split('/')
        except Exception:
            return True
        first = segments[0].lower() if segments and segments[0] else ''
        if not first:
            return True
        if first == locale_prefix:
            return True
        # чужая локаль отсекается, обычный раздел — нет
        return not re.fullmatch(r'[a-z]{2}(-[a-z]{2})?', first)

    @staticmethod
    def _merge_sitemap_maps(per_map: List[List[Tuple[str, str, str, int]]], cap: int) -> List[Tuple[str, str, str, int]]:
        """D144/D240: сводит списки отдельных карт в один, забирая из каждой по кругу,
        пока не набран кэп. Так одна большая карта не вытесняет остальные целиком."""
        merged = []
        seen = set()
        index = 0
        while len(merged) < cap and any(index < len(rows) for rows in per_map):
            for rows in per_map:
                if index < len(rows):
                    row = rows[index]
                    if row[1] in seen:
                        continue
                    seen.add(row[1])
                    merged.append(row)
                    if len(merged) >= cap:
                        break
            index += 1
        return merged

    def _sitemap_budget(self):
        """Профиль переопределяет квоту обхода; размер порции её не уменьшает."""
        if self.profile is not None and self.profile.crawl.limits.sitemap_urls:
            return self.profile.crawl.limits.sitemap_urls
        return self.config.sitemap_max_urls

    def _eligible_sitemap_row(self, row, base_url):
        """Статические фильтры применяются до квоты и повторяются перед очередью."""
        url, normalized_url, category, priority = row
        if urlparse(url).scheme not in ('http', 'https') or category.startswith('excluded'):
            return None
        url = self._canonicalize_to_working_base(url)
        if self._queue_url_rejection(url, 0, category, base_url):
            return None
        locale = self._sitemap_locale_prefix(base_url)
        if locale and not self._url_has_locale(url, locale):
            return None
        if (self.url_categorizer.should_exclude_by_language(url)
                or self.url_normalizer.is_over_pagination_limit(url)):
            return None
        return url, self.url_normalizer.normalize_url(url), category, priority

    async def _refill_sitemap_queue(self, queue, base_url):
        """Пополняет очередь из резерва, пока квота и сторож это допускают."""
        if (not self._sitemap_pending
                or len(self._sitemap_slots) >= self._sitemap_budget()):
            return 0
        return await self._add_sitemap_urls_to_queue(
            queue, [], base_url, batch_size=self.config.sitemap_processing_batch_size)

    def _recategorize_sitemap_row(self, url: str) -> Tuple[str, int]:
        """P04 U4: роль и приоритет адреса карты сайта после того, как включился признак
        «плоский сайт». Повторяет надбавку приоритета парсера карты (+1)."""
        category, priority = self.url_categorizer.categorize_url(url)
        return category, priority + 1

    async def _discover_and_parse_sitemap(self, site_url: str) -> List[Tuple[str, str, str, int]]:
        """Обнаружение и парсинг карты сайта"""
        if not self.config.sitemap_discovery_enabled:
            return []
        
        try:
            results = await self.sitemap_parser.collect_sitemap_urls(
                site_url, max_depth=self.config.sitemap_max_depth)
            # Фильтруем только товарные URL и категории, ПОКАРТОЧНО
            # (элемент: опубликованный адрес, нормализованный ключ, категория, приоритет)
            locale_prefix = self._sitemap_locale_prefix(site_url)
            per_map = []
            other_urls = []
            for result in results:
                if not isinstance(result, list):
                    continue
                kept = []
                for row in result:
                    row = self._eligible_sitemap_row(row, site_url)
                    if row is None:
                        continue
                    url, normalized_url, category, priority = row
                    # D240: локаль из Site_list — берём только адреса своей локали,
                    # иначе мультилокальная карта отдаёт кэп чужим языкам
                    if locale_prefix and not self._url_has_locale(url, locale_prefix):
                        continue
                    if category in ['product', 'category'] or priority >= 7:
                        kept.append((url, normalized_url, category, priority))
                    else:
                        other_urls.append((url, normalized_url, category, priority))
                if kept:
                    kept.sort(key=self.sitemap_parser._sitemap_rank_key)
                    per_map.append(kept)

            # Ограничиваем общее количество (профиль сайта может задать свой лимит).
            # D144/D240: кэп квотируется ПО КАРТАМ, а не по объединённому списку —
            # иначе одна карта (например, карта вложений) забирала весь лимит, а
            # товарная не получала ни слота.
            _sitemap_cap = self._sitemap_budget()
            filtered_urls = self._merge_sitemap_maps(per_map, sum(map(len, per_map)))

            # P04 U2 п.7: если товарных адресов в карте не нашлось вовсе, служебные
            # (роль other) не выбрасываем — ставим их в конец с низким приоритетом,
            # иначе у сайта не остаётся ни одной точки входа из карты.
            # Ревью слияния F4: вырезанное exclude-списком (excluded2) в резерв не берём —
            # гейт очереди его не остановит, а бюджет страниц уйдёт на исключённые разделы.
            other_urls = [row for row in other_urls if not row[2].startswith('excluded')]
            if other_urls and not any(row[2] == 'product' for row in filtered_urls):
                other_urls.sort(key=self.sitemap_parser._sitemap_rank_key)
                addition = other_urls
                if addition:
                    log.info(f"В карте сайта нет товарных адресов — добавляем {len(addition)} "
                             f"служебных с низким приоритетом")
                    filtered_urls.extend(addition)

            # Полный допустимый резерв сохраняется до конца обхода. Дубли схемы
            # товара не расходуют квоту, как и нормализованные дубли между картами.
            unique = []
            seen = set()
            for row in filtered_urls:
                key = self._scheme_dedup_key(row[0]) if row[2] == 'product' else row[1]
                if key not in seen:
                    unique.append(row)
                    seen.add(key)
            filtered_urls = unique[:_sitemap_cap]
            self._sitemap_pending = deque(unique[_sitemap_cap:])
            log.info(f"Отфильтровано {len(filtered_urls)} URL из карты сайта")
            if self.metrics_collector is not None:
                self.metrics_collector.record_sitemap(len(filtered_urls))
            return filtered_urls
            
        except asyncio.TimeoutError:
            log.warning("Таймаут при парсинге карты сайта")
        except Exception as e:
            log.error(f"Ошибка при работе с картой сайта: {e}")
        
        return []
    
    async def _add_sitemap_urls_to_queue(self, queue: deque, sitemap_urls: List[Tuple[str, str, str, int]], base_url: str, *, batch_size=None):
        """Добавление URL из карты сайта в очередь краулинга.

        D120/D219/D198: элемент — (опубликованный адрес, нормализованный ключ, категория,
        приоритет). В очередь ставится ОПУБЛИКОВАННЫЙ адрес, нормализованный вид служит
        только ключом дедупликации (visited_urls / product_urls / схемный ключ)."""
        added_count = 0
        skipped_count = 0
        skipped_dup = 0     # F9: только дубли нормализованного/схемного ключа (метрика D245)
        skipped_subdomains = 0
        skipped_pagination = 0
        skipped_quota = 0
        diverged_count = 0
        diverged_examples = []

        # Сортируем URL по рангу (приоритет, не-info раздел, артикульный слаг, глубина)
        sitemap_urls = sorted(sitemap_urls, key=self.sitemap_parser._sitemap_rank_key)
        candidates = deque(sitemap_urls)
        candidates.extend(self._sitemap_pending)
        self._sitemap_pending.clear()
        deferred = deque()
        batch_limit = max(1, batch_size if batch_size is not None
                          else self.config.sitemap_initial_batch_size)
        # D208: блок собирается целиком и кладётся в голову одной операцией. Прежний
        # appendleft в цикле разворачивал порядок: первым из очереди доставался элемент
        # с САМЫМ НИЗКИМ рангом, а товарные адреса уезжали в хвост за все служебные.
        block = []

        while candidates:
            row = candidates.popleft()
            eligible = self._eligible_sitemap_row(row, base_url)
            if eligible is None:
                skipped_count += 1
                continue
            url, normalized_url, category, priority = eligible
            # P01 U3: хост и схема адреса карты приводятся к рабочей форме компании; путь
            # остаётся опубликованным (D120). Ключ дедупа пересчитывается по новому адресу,
            # иначе схема ключа разойдётся с тем, что реально запрашивается.
            canonical_url = self._canonicalize_to_working_base(url)
            if canonical_url != url:
                url = canonical_url
                normalized_url = self.url_normalizer.normalize_url(url)

            # Проверяем основной домен. P05 U3: фильтр карты сайта (is_subdomain) и гейт
            # очереди (is_main_domain) должны решать согласованно, иначе разрешённый
            # поддомен-носитель каталога добавлялся бы и тут же отбрасывался.
            if self.config.ignore_subdomains:
                if (self.url_categorizer.is_subdomain(url, base_url)
                        and not self._in_crawl_perimeter(url, base_url)):
                    if self.config.log_skipped_subdomains:
                        log.info(f"Пропускаем поддомен из карты сайта: {url}")
                    skipped_subdomains += 1
                    skipped_count += 1
                    continue

            # Языковой фильтр для карты сайта: иноязычные версии (en.x.ru,
            # /catalog/en/p) пропускаем здесь, иначе они занимают бюджет
            # товарных страниц и помечаются visited/product_urls ниже.
            if self.url_categorizer.should_exclude_by_language(url):
                log.debug(f"Пропускаем иноязычный URL из карты сайта: {url}")
                skipped_count += 1
                continue

            # D245: слишком глубокая пагинация отбраковывается URL ЦЕЛИКОМ (раньше
            # нормализатор молча вырезал параметр, и адрес схлопывался с листингом).
            if self.url_normalizer.is_over_pagination_limit(url):
                log.debug(f"Пропускаем URL глубокой пагинации из карты сайта: {url}")
                skipped_pagination += 1
                continue

            # Проверяем дублирование
            if normalized_url in self.visited_urls:
                skipped_count += 1
                skipped_dup += 1
                continue

            # Лимит продуктовых страниц действует и на URL из карты сайта: иначе
            # sitemap кладёт сотни товарных URL в очередь в обход
            # max_product_pages_per_site. Продукты в карте отсортированы первыми по
            # приоритету, поэтому лишние просто пропускаем (контакты/категории идут дальше).
            if category == 'product' and self._product_quota_reject_reason(url):
                # D222/D144: отказ по квоте раньше попадал в счётчик «дубликатов» и был
                # неотличим от дедупа; теперь считается отдельно и служит входом
                # страховки «второй проход», если товарных страниц не сохранилось вовсе.
                skipped_quota += 1
                if url not in self._quota_rejected_urls:
                    self._quota_rejected_urls.append(url)
                self._quota_rejected_depths.setdefault(url, 0)
                self._sitemap_published_urls[normalized_url] = url
                deferred.append(eligible)
                continue

            if len(self._sitemap_slots) >= self._sitemap_budget():
                deferred.append(eligible)
                deferred.extend(candidates)
                candidates.clear()
                break

            # D59 RC1: схемный дубль http↔https из sitemap (общий набор ключей с гейтом ссылок).
            if category == 'product':
                skey = self._scheme_dedup_key(url)
                if skey is not None:
                    if skey in self._crawled_scheme_keys:
                        log.info(f"D59 RC1: схемный дубль из sitemap (ключ {skey}): {url}")
                        skipped_count += 1
                        skipped_dup += 1
                        continue
                    self._crawled_scheme_keys.add(skey)

            # Увеличиваем приоритет URL из карты сайта. D208: потолок 10 снят — иначе
            # товар, категория и контакты получали один и тот же приоритет, и
            # пересортировка очереди в главном цикле переставала их различать.
            enhanced_priority = priority + 3

            # Блок пойдёт в голову очереди, ОПУБЛИКОВАННЫМ адресом (D120/D219/D198)
            block.append((url, 0, category, enhanced_priority))
            self.visited_urls.add(normalized_url)
            if category == 'product':
                self.product_urls.add(normalized_url)
            # D120 п.7: запоминаем опубликованный адрес, чтобы при no_content на ином
            # написании того же ключа повторить запрос ровно тем адресом, что в карте
            # (и чтобы считать долю адресов карты, не отдавших содержимое).
            self._sitemap_published_urls[normalized_url] = url
            self._sitemap_slots.add(normalized_url)
            if url != normalized_url:
                diverged_count += 1
                if len(diverged_examples) < 3:
                    diverged_examples.append(f"{url} != {normalized_url}")
            added_count += 1

            # Размер порции не обрезает резерв и не заменяет общую квоту.
            if added_count >= batch_limit:
                deferred.extend(candidates)
                candidates.clear()
                break

        self._sitemap_pending = deferred

        # D208: extendleft разворачивает вставляемую последовательность, поэтому
        # подаём её перевёрнутой — порядок ранжирования доживает до popleft.
        queue.extendleft(reversed(block))

        log.info(f"Добавлено {added_count} URL из карты сайта в начало очереди")
        if block:
            # D208 п.5: фактический порядок обхода раньше в логе не печатался вовсе
            log.info(f"Первые адреса очереди из карты сайта: "
                     f"{', '.join(item[0] for item in block[:5])}")
        if skipped_count > 0:
            log.info(f"Пропущено {skipped_count} дубликатов")
        if skipped_quota > 0:
            log.info(f"Пропущено {skipped_quota} товарных URL по квоте "
                     f"(занято {len(self.product_urls)} из {self.max_product_pages_per_site})")
        if skipped_pagination > 0:
            log.info(f"Пропущено {skipped_pagination} URL глубокой пагинации из карты сайта")
        if skipped_subdomains > 0:
            log.info(f"Пропущено {skipped_subdomains} поддоменов из карты сайта")
        if diverged_count > 0:
            # D120 п.7б: расхождение «опубликованный адрес / нормализованный ключ» раньше
            # было невидимым — 404 логировался как обычная сетевая ошибка.
            log.info(f"Карта сайта: у {diverged_count} адресов опубликованный вид отличается "
                     f"от нормализованного ключа (в очередь поставлен опубликованный); "
                     f"примеры: {'; '.join(diverged_examples)}")
        if self.metrics_collector is not None:
            # F9: в метрику D245 идут только дубли ключа, не поддомены/локали
            self.metrics_collector.record_sitemap_offered(len(sitemap_urls), skipped_dup)
            # P05 U2: «из карты сайта добавлено 0 при N отфильтрованных» — алерт коллектора
            self.metrics_collector.record_sitemap_queue(
                added_count, len(sitemap_urls) - added_count)
        return added_count
    
    async def _process_page_with_storage(self, url: str, depth: int, category: str,
                                     company_name: str, domain_dirs: Dict[str, str],
                                     queue: deque, stored_pages: List[Dict] = None) -> Optional[Dict]:
        """
        Обработка отдельной страницы с сохранением во временное хранилище.
        Извлекает ссылки ВСЕГДА, даже если страница не подлежит сохранению.
        ИЗМЕНЕНИЕ: сохраняется оригинальный HTML (без очистки).
        """
        log.info(f"Обработка страницы: {url} (глубина: {depth}, категория: {category})")

        normalized_url = self.url_normalizer.normalize_url(url)

        if self._queue_url_rejection(url, depth, category):
            return None
        async with self._urls_lock:
            from_sitemap = normalized_url in self._sitemap_published_urls
            if from_sitemap and normalized_url not in self._sitemap_slots:
                if len(self._sitemap_slots) >= self._sitemap_budget():
                    return None
            if category == 'product' and normalized_url not in self.product_urls:
                if self._product_quota_reject_reason(url):
                    return None
                self.product_urls.add(normalized_url)
                self._quota_released.discard(normalized_url)
            if from_sitemap:
                self._sitemap_slots.add(normalized_url)

        async with self._stats_lock:
            if self.stats['total_pages'] >= self.max_pages_per_site:
                return None
            self.stats['total_pages'] += 1
            self.stats['attempted_pages'] += 1
        async with self._urls_lock:
            self.processed_urls.add(normalized_url)

        # Мониторинг памяти
        self._pages_processed_since_last_check += 1
        if (self._pages_processed_since_last_check >= self.config.memory_check_interval_pages or
                time.time() - self._last_memory_check > 300):
            await self._check_memory_usage()
            self._pages_processed_since_last_check = 0
            self._last_memory_check = time.time()

        sitemap_accepted = False
        fetched = False
        try:
            # 1. Получение контента страницы (может вернуть один ParseResult или список)
            cached = self._resume_pages.get(normalized_url)
            cached_content = (await self.temp_storage.load_cleaned_html(cached['html_path'])
                              if cached else None)
            outcome = None
            if cached_content:
                saved_url = cached_content['metadata'].get('url') or url
                parse_result = await self._parse_content(cached_content['html_content'], saved_url)
                if parse_result:
                    parse_result.requested_url = url
                    parse_result.fetch_method = cached_content['metadata'].get('fetch_method', '')
            else:
                outcome = await self.fetch_page(url, category)
                parse_result = outcome.content

            if parse_result:
                self._record_page_success(url, resumed=bool(cached_content))
                fetched = True
            # Обработка случая, когда страница вернула список вкладок
            if isinstance(parse_result, list):
                self._fetch_failures.pop(url, None)
                if category == 'product' and normalized_url in self._sitemap_published_urls:
                    self._sitemap_hard_fail_streak = 0
                sitemap_accepted = any(parse_result)
                for idx, single_result in enumerate(parse_result):
                    tab_url = f"{url}#tab_{idx}"
                    tab_normalized = self.url_normalizer.normalize_url(tab_url)
                    if single_result:
                        # Извлекаем ссылки из каждой вкладки
                        await self._process_links_from_parse_result(tab_url, single_result, depth, queue)
                    # Если категория подходит для сохранения - сохраняем вкладку
                    # (F10: тот же набор ролей, что target_categories, включая category/price_list)
                    if category in ('product', 'contacts', 'distributor', 'main_page',
                                    'category', 'price_list'):
                        await self._save_single_page(
                            url, tab_normalized, single_result, depth, category,
                            company_name, domain_dirs, stored_pages, queue,
                            redirected_from=url
                        )
                return None

            if not parse_result:
                # D120 п.7а: адрес, отличающийся от опубликованного в карте сайта, мог
                # получить 404 именно из-за написания (снятый слеш, снятый /ru). Перед
                # отбраковкой повторяем запрос ровно опубликованным адресом. Кэш
                # ошибок различает эти написания и не мешает отдельной попытке.
                published_url = self._sitemap_published_urls.get(normalized_url)
                if published_url and published_url != url:
                    log.warning(f"Нет содержимого по адресу {url}; повтор опубликованным "
                                f"адресом карты сайта: {published_url}")
                    outcome = await self.fetch_page(published_url, category)
                    retried = outcome.content
                    if isinstance(retried, list):
                        retried = retried[0] if retried else None
                    parse_result = retried
                    if parse_result:
                        self._record_page_success(url)
                        fetched = True

            if not parse_result:
                outcome = outcome or FetchOutcome(url, reason='empty')
                self._fetch_failures[url] = {k: getattr(outcome, k) for k in
                    ('requested_url', 'final_url', 'transport', 'status', 'reason', 'retryable', 'retry_after')}
                self._record_page_failure(url, outcome.reason)
                log.warning(f"Не удалось получить содержимое страницы: {url}: {outcome.reason}")
                if normalized_url in self._sitemap_published_urls and self.metrics_collector is not None:
                    self.metrics_collector.record_sitemap_no_content(url)
                if category == 'product':
                    await self._check_hard_fail_sitemap_guard(url, depth, category, queue, outcome=outcome)
                    if outcome.reason == 'soft_not_found':
                        await self._check_stale_sitemap_guard(outcome.evidence, url, queue, outcome=outcome)
                # D40: таймаутнутые целевые страницы не теряем — кладём в очередь ретраев,
                # её обслуживает тот же цикл, включая найденных при повторе потомков
                self._register_page_retry(url, depth, category, outcome)
                # Временный отказ сохраняет уже выделенную квоту до повтора.
                # Иначе свежие URL займут её за время backoff и вытеснят повтор.
                if category == 'product' and normalized_url not in self._page_retry_candidates:
                    await self._release_product_quota_slot(normalized_url, 'нет содержимого')
                return None

            # P02 U1: содержимое пришло — серия жёстких отказов карты сайта прервана
            self._fetch_failures.pop(url, None)
            if category == 'product' and normalized_url in self._sitemap_published_urls:
                self._sitemap_hard_fail_streak = 0

            # 2. ИЗВЛЕЧЕНИЕ ССЫЛОК (включая неизменённые и восстановленные страницы)
            await self._process_links_from_parse_result(url, parse_result, depth, queue)

            # 2а. ОБЩИЕ ДОКУМЕНТЫ КОМПАНИИ: если страница принадлежит одному из
            # 4 блоков профиля (certificates/documents/instructions/price_list) —
            # скачиваем файлы с неё в соответствующую папку компании
            doc_section = self.url_categorizer.profile_document_section(url)
            if doc_section:
                await self._download_company_documents(parse_result.url or url, parse_result.html, doc_section, domain_dirs)

            # 3. СПЕЦИАЛЬНАЯ ОБРАБОТКА ДЛЯ ДОКУМЕНТАЦИИ (скачивание файлов)
            if category == 'documentation':
                log.info(f"Обработка страницы документации (скачивание файлов): {url}")
                try:
                    extractor = self._make_dynamic_extractor()
                    async with async_deadline(self.config.page_fetch_timeout):
                        result = await extractor.extract(url)
                    if result:
                        await self._download_files_from_documentation_page(url, result, domain_dirs, company_name)
                    else:
                        log.warning(f"Не удалось получить контент для документации {url}")
                except Exception as e:
                    self._record_page_failure(url, 'documentation_error', processing=True)
                    log.error(f"Ошибка при скачивании файлов документации {url}: {e}")
                return None

            # 4. РЕШЕНИЕ: сохранять ли страницу?
            # P04 U3 (D168/D181): category и price_list — не тупиковые роли: страницы
            # сохраняются в Other_pages и доступны второму проходу извлечения в main.py
            target_categories = {'product', 'contacts', 'distributor', 'main_page', 'category', 'price_list'}
            if category not in target_categories:
                sitemap_accepted = True
                self._usable_page_urls.add(normalized_url)
                log.debug(f"Страница категории '{category}' не подлежит сохранению, ссылки извлечены: {url}")
                return None

            # 5. Проверка размера оригинального HTML
            if len(parse_result.html) < 300:
                self._record_page_failure(url, 'content_too_small', processing=True)
                log.info(f"Пропускаем сохранение страницы {url}: размер оригинального HTML менее 300 символов")
                # P02 U1: страница не сохранена — слот товарной квоты возвращается
                if category == 'product':
                    await self._release_product_quota_slot(normalized_url, 'HTML менее 300 символов')
                return None

            # 5а. D36: детект протухшего sitemap — одинаковый видимый текст у товарных страниц
            if category == 'product':
                await self._check_stale_sitemap_guard(parse_result.html, url, queue)
                # P04 U4 п.1 (D100): слот квоты занят как под карточку, но по форме URL
                # это листинг, и DOM карточку не подтверждает — слот возвращаем.
                # Страница сохраняется дальше как обычно, ссылки с неё уже собраны.
                await self._release_listing_quota_slot(url, normalized_url, parse_result.soup)

            # 6. Подготовка метаданных
            metadata = {
                'url': parse_result.url or url,
                'requested_url': parse_result.requested_url or url,
                'canonical_url': parse_result.canonical_url,
                'fetch_method': parse_result.fetch_method,
                'normalized_url': self.url_normalizer.normalize_url(parse_result.url or url),
                'category': category,
                'content_type': parse_result.content_type,
                'depth': depth,
                'timestamp': datetime.now().isoformat(),
                'elements_found': parse_result.elements_found,
                'file_size_bytes': len(parse_result.html),
                'priority': {'product': 9, 'contacts': 7, 'distributor': 7, 'main_page': 7}.get(category, 1)
            }
            if hasattr(parse_result, 'redirected_from') and parse_result.redirected_from:
                metadata['redirected_from'] = parse_result.redirected_from

            # 7. Сохранение оригинального HTML во временное хранилище
            file_path, is_new, storage_metadata = await self.temp_storage.save_cleaned_html(
                metadata['url'], metadata['normalized_url'], parse_result.html, company_name, metadata, category
            )

            # 8. Дополнительное сохранение для контактов как дистрибьюторов
            if category == 'contacts':
                distributor_metadata = metadata.copy()
                distributor_metadata['category'] = 'distributor'
                distributor_metadata['is_contact_duplicate'] = True
                await self.temp_storage.save_cleaned_html(
                    metadata['url'], metadata['normalized_url'], parse_result.html, company_name, distributor_metadata, 'distributor'
                )

            # 9. Формирование результата. Словарь строим независимо от наличия списка
            # stored_pages: параллельный путь (_process_page_parallel) передаёт его как None
            # и работает с ВОЗВРАЩАЕМЫМ значением — прежний гейт «stored_pages is not None»
            # делал возврат вечным None, список краулера оставался пустым, и пайплайн жил
            # только за счёт подбора страниц из хранилища после краула
            stored_page = None
            if file_path and is_new:
                stored_page = {
                    'file_path': file_path,
                    'url': metadata['url'],
                    'requested_url': metadata['requested_url'],
                    'category': category,
                    'normalized_url': metadata['normalized_url'],
                    'timestamp': metadata['timestamp'],
                    'metadata': storage_metadata
                }
                if stored_pages is not None:
                    stored_pages.append(stored_page)

            sitemap_accepted = bool(file_path)
            if not file_path:
                self._record_page_failure(url, 'storage_error', processing=True)
            else:
                self._usable_page_urls.add(normalized_url)
            await self._update_stats(category, url)
            return stored_page

        except Exception as e:
            log.error(f"Ошибка обработки страницы {url}: {e}", exc_info=True)
            self._record_page_failure(url, 'processing_error', processing=fetched)
            return None
        finally:
            if not sitemap_accepted and normalized_url not in self._page_retry_candidates:
                self._sitemap_slots.discard(normalized_url)
                if category == 'product':
                    await self._release_product_quota_slot(normalized_url, 'страница не сохранена')


    async def _process_links_from_parse_result(self, url: str, parse_result: ParseResult,
                                            depth: int, queue: deque) -> None:
        """
        Вспомогательный метод: извлекает ссылки из ParseResult и добавляет их в очередь.
        Вызывается всегда, даже если страница не сохраняется.
        """
        try:
            new_links = await self._extract_links(parse_result.url or url, parse_result, depth,
                                                  base_url=self.current_base_url)
            added = 0
            for link_url, link_category, priority in new_links:
                if await self._should_add_to_queue_parallel(link_url, depth + 1, link_category):
                    async with self._queue_lock:
                        queue.append((link_url, depth + 1, link_category, priority))
                    async with self._urls_lock:
                        self.visited_urls.add(self.url_normalizer.normalize_url(link_url))
                    added += 1
            if added:
                log.debug(f"Добавлено {added} ссылок из страницы {url}")
        except Exception as e:
            log.error(f"Ошибка извлечения ссылок из {url}: {e}")

    def _scheme_dedup_key(self, url: str):
        """D59 RC1: ключ для отсева ТОЛЬКО схемных дублей http↔https одной страницы —
        нормализованный URL без схемы (домен и путь сохраняются, включая фактический домен,
        без сведе́ния к группе). Две версии http/https одного домена+пути дают один ключ;
        кросс-домен и разные пути НЕ склеиваются ни здесь, ни по последнему сегменту,
        т.к. на разных доменах холдинга контент может отличаться — вплоть до soft-404."""
        try:
            n = self.url_normalizer.normalize_url(url)
            key = re.sub(r'^https?://', '', n or '')
            return key or None
        except Exception:
            return None

    def _product_dedup_key(self, url: str):
        """Полный адрес товара без схемы: host/port/path/query, без склейки по слагу.

        Адрес уже прошёл редиректы перед извлечением. Группа доступных доменов
        определяет периметр обхода, но не доказывает тождество товаров на них.
        """
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ('http', 'https') or not parsed.netloc:
                return None
            return self._scheme_dedup_key(url)
        except Exception as e:
            log.debug(f"_product_dedup_key: не удалось построить ключ для {url}: {e}")
            return None

    def _queue_url_rejection(self, url, depth, category, base_url=None):
        """Одинаковый допуск для ссылок, стартов, sitemap, резерва и повторов."""
        if not self._is_allowed_url(url, base_url=base_url or url):
            return 'домен вне профиля'
        if depth > self.max_depth:
            self._depth_limited_urls.add(self.url_normalizer.normalize_url(url))
            return 'глубина переходов'
        if category.startswith('excluded'):
            return 'исключённая категория'
        if self.url_categorizer.should_exclude_by_language(url):
            return 'язык'
        if self.url_normalizer.is_over_pagination_limit(url):
            self._pagination_limited_urls.add(self.url_normalizer.normalize_url(url))
            return 'предел пагинации'
        if category == 'product' and self.url_categorizer.is_print_version(url, category):
            return 'печатная версия'
        if self.url_categorizer._is_image_url(url) or self.url_categorizer.is_downloadable_file(url):
            return 'файл вместо страницы'
        return None

    async def _should_add_to_queue_parallel(self, url: str, depth: int,
                                            category: str = None, *, retry=False) -> bool:
        """Потокобезопасная проверка, нужно ли добавлять URL в очередь.

        F3 ревью слияния: роль передаётся УЖЕ ПОСЧИТАННОЙ сборщиком ссылок — она
        учитывает текст ссылки (якоря P04 U2/U3), а пересчёт здесь шёл без текста.
        Из-за расхождения товарный URL с якорем ложился в очередь как 'product', но
        слот квоты не резервировал и схемный дедуп D59 не проходил."""
        if category is None:
            category, _ = self.url_categorizer.categorize_url(url)
        reason = self._queue_url_rejection(url, depth, category)
        if reason:
            self._count_queue_gate_rejection(reason)
            return False
        normalized_url = self.url_normalizer.normalize_url(url)
        async with self._urls_lock:
            if not retry and (normalized_url in self.visited_urls or normalized_url in self.processed_urls):
                return False
            if normalized_url in self._sitemap_published_urls:
                if normalized_url not in self._sitemap_slots and len(self._sitemap_slots) >= self._sitemap_budget():
                    return False

            # Для товарных страниц ограничиваем количеством.
            # D193/D143/D212: отказ был немым; теперь он считается и попадает в сводку
            # за компанию, а сам адрес — во вход страховки «второй проход».
            if category == 'product' and normalized_url not in self.product_urls:
                reason = self._product_quota_reject_reason(url)
                if reason:
                    self._count_queue_gate_rejection(reason)
                    self._quota_rejected_urls.append(url)
                    self._quota_rejected_depths.setdefault(url, depth)
                    return False

            # Схемный дубль не создаёт новую работу; явный повтор того же URL
            # допускается с сохранением индивидуального бюджета попыток.
            if category == 'product':
                skey = self._scheme_dedup_key(url)
                if skey is not None:
                    if not retry and skey in self._crawled_scheme_keys:
                        log.info(f"D59 RC1: пропускаем схемный дубль (ключ {skey}): {url}")
                        return False
                    self._crawled_scheme_keys.add(skey)

            # Засчитываем допущенный продуктовый URL сразу при постановке в очередь,
            # а не только при обработке (_update_stats): при параллельной обработке
            # (max_concurrent_pages) иначе лимит перебирается на размер батча.
            if category == 'product':
                self.product_urls.add(normalized_url)
                self._quota_released.discard(normalized_url)

            if normalized_url in self._sitemap_published_urls:
                self._sitemap_slots.add(normalized_url)
            self.visited_urls.add(normalized_url)
            return True

    async def _download_site_files(self, site_url: str, domain_dirs: Dict[str, str]) -> List[str]:
        """Скачивание файлов с сайта"""
        downloaded_files = []
        
        if not self._is_allowed_url(site_url, base_url=site_url):
            return downloaded_files
        page = None
        cleanup_guard = None
        context = await self.browser_pool.get_browser()
        try:
            page = await context.new_page()
            cleanup_guard = await self._guard_browser_page(page, site_url)
            await page.goto(site_url)
            if not self._is_allowed_url(page.url, base_url=site_url):
                return downloaded_files
            
            # Скачиваем файлы с различных вкладок
            downloaded_files.extend(
                await self.file_download_manager.process_certificates_tab(page, site_url, domain_dirs)
            )
            downloaded_files.extend(
                await self.file_download_manager.process_instructions_tab(page, site_url, domain_dirs)
            )
            downloaded_files.extend(
                await self.file_download_manager.process_documents_tab(page, site_url, domain_dirs)
            )
            downloaded_files.extend(
                await self.file_download_manager.process_price_lists_tab(page, site_url, domain_dirs)
            )
            
        except Exception as e:
            log.error(f"Ошибка скачивания файлов с {site_url}: {e}")
        finally:
            await _release_browser_resources(self.browser_pool, context, page, cleanup_guard)
        return downloaded_files
    
    def _normalize_file_url(self, url: str, base_url: str) -> str:
        """Нормализация URL файла"""
        try:
            # Если URL уже абсолютный, возвращаем как есть
            if url.startswith(('http://', 'https://')):
                return url
            
            # Если URL начинается с //, добавляем схему из base_url
            if url.startswith('//'):
                parsed_base = urlparse(base_url)
                return f"{parsed_base.scheme}:{url}"
            
            # Для сайта actey.com: если ссылка начинается с assets/, files/, download/ - это абсолютный путь от корня
            if url.startswith(('assets/', 'files/', 'download/', 'documents/')):
                parsed_base = urlparse(base_url)
                return f"{parsed_base.scheme}://{parsed_base.netloc}/{url}"
            
            # Если URL начинается с /, это абсолютный путь от корня домена
            if url.startswith('/'):
                parsed_base = urlparse(base_url)
                return f"{parsed_base.scheme}://{parsed_base.netloc}{url}"
            
            # Для остальных относительных URL используем urljoin
            return urljoin(base_url, url)
            
        except Exception as e:
            log.warning(f"Ошибка нормализации URL {url}: {e}")
            return url
        
    async def extract_file_links_from_page(self, url: str, storage_path: str = '') -> List[str]:
        """Извлечение ссылок на файлы с разрешенными расширениями с указанной страницы.
        D72: сначала пробуем уже СОХРАНЁННЫЙ при обходе HTML — повторная загрузка страницы на
        медленных/антибот-сайтах (напр. pspcom.ru) таймаутит в Playwright и обнуляет список файлов.
        Если сохранённого HTML нет или в нём не нашлось файловых ссылок — ФОЛБЭК на сетевую загрузку
        (прежнее поведение), поэтому на других сайтах результат не уменьшается."""
        # 1) Уже сохранённый при обходе HTML
        if storage_path:
            try:
                async with aiofiles.open(storage_path, 'r', encoding='utf-8') as f:
                    saved_html = await f.read()
                if saved_html:
                    links = self._extract_file_links_from_soup(BeautifulSoup(saved_html, 'html.parser'), url)
                    if links:
                        return links
            except Exception as e:
                log.debug(f"D72: не удалось прочитать сохранённый HTML {storage_path}: {e}")
        # 2) Фолбэк: сетевая загрузка (прежнее поведение)
        try:
            outcome = await self.fetch_page(url, 'product')
            parse_result = outcome.content
            if not parse_result:
                return []
            return self._extract_file_links_from_soup(parse_result.soup, parse_result.url or url)
        except Exception as e:
            log.error(f"Ошибка извлечения файловых ссылок со страницы {url}: {e}")
            return []

    def _extract_file_links_from_soup(self, soup, url: str) -> List[str]:
        """Извлечение файловых ссылок из готового soup (общая логика: сохранённый HTML и сетевая загрузка)."""
        try:
            file_links = []
            link_base = document_base_url(soup, url)

            # Ищем все ссылки с текстом, указывающим на скачивание
            download_keywords = ['скачать', 'download', 'загрузить', 'Скачать', 'Download',
                                 'техническая документация', 'инструкция', 'руководство',
                                 'паспорт', 'сертификат', 'certificate', 'manual', 'Документация', 'сертификаты']

            for link in soup.find_all('a', href=True):
                href = sanitize_href(link['href'])  # D257/D233
                link_text = link.get_text().lower()
                
                if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
                    continue
                
                # Проверяем, содержит ли текст ссылки ключевые слова скачивания
                is_download_link = any(keyword in link_text for keyword in download_keywords)
                
                # Нормализуем URL
                normalized_url = self._normalize_file_url(href, link_base)
                
                if not self._is_allowed_url(normalized_url, resource='file', base_url=url):
                    continue
                    
                # Если это явно ссылка для скачивания ИЛИ это поддерживаемый файл
                if is_download_link or self._is_supported_file_link(normalized_url):
                    file_links.append(normalized_url)

            # Также ищем кнопки/элементы с текстом скачивания
            download_elements = soup.find_all(['button', 'div', 'span'], 
                                            string=re.compile(r'скачать|download|загрузить', re.IGNORECASE))
            
            for element in download_elements:
                parent_link = element.find_parent('a', href=True)
                if parent_link and parent_link['href']:
                    href = sanitize_href(parent_link['href'])  # D257/D233
                    normalized_url = self._normalize_file_url(href, link_base)
                    
                    if not self._is_allowed_url(normalized_url, resource='file', base_url=url):
                        continue
                        
                    if self._is_supported_file_link(normalized_url):
                        file_links.append(normalized_url)

            # Ищем все ссылки на файлы по расширениям
            for link in soup.find_all('a', href=True):
                href = sanitize_href(link['href'])  # D257/D233
                if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
                    continue
                
                normalized_url = self._normalize_file_url(href, link_base)
                if not self._is_allowed_url(normalized_url, resource='file', base_url=url):
                    continue
                if self._is_supported_file_link(normalized_url) and normalized_url not in file_links:
                    file_links.append(normalized_url)

            # Удаляем дубликаты
            unique_links = list(set(file_links))
            log.info(f"Найдено {len(unique_links)} файловых ссылок на странице {url}")
            
            return unique_links
            
        except Exception as e:
            log.error(f"Ошибка извлечения файловых ссылок со страницы {url}: {e}")
            return []

    def _is_supported_file_link(self, url: str) -> bool:
        """Проверяет, является ли URL ссылкой на файл с поддерживаемым расширением"""
        try:
            parsed = urlparse(url)
            path = parsed.path.lower()
            
            # Список поддерживаемых расширений из FileDownloadManager
            supported_extensions = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.rtf', 'jpg']
            
            # Проверяем расширения файлов в пути
            for ext in supported_extensions:
                if path.endswith(ext):
                    # Убеждаемся, что это действительно расширение файла
                    ext_index = path.rfind(ext)
                    if ext_index > 0 and path[ext_index-1] == '.':
                        return True
            
            # Дополнительные проверки для специфичных паттернов файлов
            file_patterns = [
                '/download/', '/file/', '/attachment/', '/documents/', 
                'download.php', 'file.php', 'document.php', '/files/',
                '?download=', '?file=', '?document=', 'pdf'
            ]
            
            if any(pattern in url.lower() for pattern in file_patterns):
                # Проверяем параметры запроса на наличие расширений файлов
                query_params = parse_qs(parsed.query)
                for param, values in query_params.items():
                    for value in values:
                        value_lower = value.lower()
                        for ext in supported_extensions:
                            if value_lower.endswith(ext):
                                return True
                return True
            
            return False
            
        except Exception as e:
            log.debug(f"Ошибка проверки файловой ссылки {url}: {e}")
            return False

    def _get_start_urls(self, site_url: str) -> List[str]:
        """Генерация стартовых URL"""
        parsed = urlparse(site_url)
        root_url = f"{parsed.scheme}://{parsed.netloc}"
        # Языковой префикс из Site_list сохраняется (dkc.ru/ru/): угаданные разделы строятся
        # от него, иначе база обхода снова схлопывается к корню с языковым редиректом (D140).
        # Ревью слияния F2: рабочий URL может принести и случайный путь после 301
        # (/index.php, /home/) — базой разделов служит только локаль, иначе корень сайта.
        first_segment = next((seg for seg in parsed.path.split('/') if seg), '')
        is_locale = bool(self.url_categorizer.detect_locale_prefix(site_url)) or \
            first_segment.lower().replace('_', '-').split('-')[0] in ('ru', 'rus')
        locale_prefix = first_segment if is_locale else None
        base_url = f"{root_url}/{locale_prefix}" if locale_prefix else root_url
        site_path = parsed.path.rstrip('/')
        
        start_urls = [
            base_url,  # Главная страница
            f"{base_url}/",  # Главная с слэшем
            f"{base_url}/materials",    # Каталог 
            f"{base_url}/products",    # Товары
            f"{base_url}/catalog",     # Каталог
            f"{base_url}/category",    # Категории
            f"{base_url}/contacts",    # Контакты
            f"{base_url}/contact",     # Контакт            
            f"{base_url}/dealers",     # Дилеры
            f"{base_url}/distributors", # Дистрибьюторы
            f"{base_url}/gde-kupit",   # Где купить (D37)
            f"{base_url}/where-to-buy", # Где купить, англ. слаг (D37)
            f"{base_url}/partners",    # Партнёры/дилерская сеть (D37)
            f"{base_url}/about/predstavitelstva",  # Представительства (D37)
            f"{base_url}/price",       # Прайс-листы
            f"{base_url}/tseny"       # Прайс-листы
        ]
        if base_url != root_url:
            start_urls.insert(2, f"{root_url}/")  # корень сайта не теряем при базе с локалью
        if site_path and not locale_prefix:
            # адрес с путём (из Site_list или после редиректа) — точка входа, не база разделов
            start_urls.insert(0, f"{root_url}{site_path}/")

        # Доп. стартовые точки из профиля сайта (карта разделов sections) — пути корне-относительные.
        if self.profile is not None:
            sec = self.profile.sections
            for path in (sec.start_urls + sec.catalog_roots + sec.contacts_urls +
                         sec.distributor_urls + sec.certificates_urls + sec.documents_urls +
                         sec.instructions_urls + sec.price_list_urls):
                full = path if path.startswith('http') else urljoin(root_url + '/', path.lstrip('/'))
                if full not in start_urls:
                    start_urls.append(full)

        # D192/D120: жёсткие стартовые пути печатались без завершающего слеша, а часть
        # сайтов (Bitrix со строгой маршрутизацией, Egger) отдаёт раздел только со слешем.
        # Даём каждому пути слеш-вариант и ставим его ПЕРЕД бесслешевым: очередь дедупится
        # по нормализованному ключу, где обе формы совпадают, поэтому обойдён будет первый —
        # и это должна быть каноническая форма со слешем.
        expanded_urls = []
        for url in start_urls:
            if url != base_url and '?' not in url and '#' not in url and not url.endswith('/'):
                expanded_urls.append(url + '/')
            expanded_urls.append(url)

        return [url for url in expanded_urls if self._is_valid_url(url)]

    async def _should_skip_url(self, url: str, depth: int, company_name: str, category: str = None) -> bool:
        """Проверка, нужно ли пропустить URL"""
        
        normalized_url = self.url_normalizer.normalize_url(url)
        if category is None:
            category, _ = self.url_categorizer.categorize_url(url)
        reason = self._queue_url_rejection(url, depth, category)
        if reason:
            await self._release_product_quota_slot(normalized_url, reason)
            return True
        async with self._urls_lock:
            return (normalized_url in self.processed_urls
                    and normalized_url not in self._queued_retries)

    async def _accept_cookie_consent(self, page) -> bool:
        """Ищет и кликает по кнопке принятия cookies (если присутствует)."""
        # Тизол: кнопка баннера включается только после двух согласий.
        # Не трогаем флажки форм заказа и других сайтов.
        if (urlparse(page.url).hostname or '').lower() in ('tizol.com', 'www.tizol.com'):
            try:
                banner = await page.query_selector('.personal-warning--show')
                if banner and await banner.is_visible():
                    for checkbox in await banner.query_selector_all('input[type="checkbox"]'):
                        await checkbox.check(timeout=2000)
                    button = await banner.query_selector('.personal-warning__btn')
                    if button:
                        await button.click(timeout=2000)
                        await page.wait_for_timeout(300)
                        return True
            except Exception as exc:
                log.debug(f"Не удалось закрыть баннер Тизола: {exc}")
                return False
        # Распространённые селекторы для кнопок согласия
        consent_selectors = [
            # Кнопки с текстом
            "[data-cookie-accept]",
            "#cookie-accept",
            ".cookie-accept",
            ".cookie-consent__accept",
            ".cookie__accept",
            ".cookie-notice__accept",
            ".cookie-policy__accept",
            "button:has-text('Даю согласие')",
            "button:has-text('Согласен')",
            "button:has-text('Accept')",
            "button:has-text('OK')",
            "button:has-text('Принять')",
            "button:has-text('Согласие')",
            "button:has-text('Закрыть')",
            "button.cookie-button",
            "a.cookie-accept",
            # Классические баннеры
            ".cookies__button",
            ".cookie-popup__button",
            ".cookie-consent__button",
            ".agree-button",
            "#cookieConsent button",
        ]
        
        for selector in consent_selectors:
            try:
                button = await page.query_selector(selector)
                if button and await button.is_visible():
                    await button.click()
                    log.info(f"Принято cookie-согласие через селектор: {selector}")
                    await page.wait_for_timeout(1000)  # Даём время на обработку
                    return True
            except Exception as e:
                log.debug(f"Не удалось кликнуть по {selector}: {e}")
                continue
        
        # Дополнительно: поиск по XPath
        xpath_expressions = [
            "//button[contains(text(), 'Даю согласие')]",
            "//button[contains(text(), 'Согласен')]",
            "//button[contains(text(), 'Accept')]",
            "//button[contains(text(), 'Принять')]",
            "//a[contains(text(), 'Даю согласие')]",
            "//div[contains(@class, 'cookie')]//button",
        ]
        for xpath in xpath_expressions:
            try:
                elements = await page.query_selector_all(f"xpath={xpath}")
                for el in elements:
                    if await el.is_visible():
                        await el.click()
                        log.info(f"Принято cookie-согласие через XPath: {xpath}")
                        await page.wait_for_timeout(1000)
                        return True
            except:
                continue
        
        return False  # Баннер не найден или не требует действий   
       
    def _usable_product_content(self, parsed: ParseResult, *, rendered: bool) -> bool:
        """Каталог отдаёт ссылки; короткая HTTP-карточка требует проверки браузером."""
        html = parsed.html
        if (page_error_reason(html) or is_unrendered_store_listing(html)
                or needs_javascript(html)):
            return False
        if product_state_key(parsed.url) and parsed.soup.select_one('main[data-tilda-product], main[data-inline-product]'):
            return True
        if has_product_island(html):
            return True
        if (any(cat == 'product' for _, cat, _ in parsed.additional_links)
                and (rendered or self.url_categorizer.is_listing_url(parsed.url))):
            return True
        if rendered and self.url_categorizer.is_product_page_by_content(parsed.soup, parsed.url):
            return True
        minimum = self._product_shell_min_text()
        return not minimum or not looks_like_product_shell(html, min_text=minimum)

    def _host_paused(self, url):
        return self._host_retry_until.get(urlparse(url).netloc.lower(), 0) > time.monotonic()

    async def fetch_page(self, url: str, category=None) -> FetchOutcome:
        """Одна попытка со всеми транспортами; свидетельства изолированы по asyncio-task."""
        if not self._is_allowed_url(url, base_url=url):
            return FetchOutcome(url, reason='out_of_scope', retryable=False)
        if self._host_paused(url):
            return FetchOutcome(url, reason='host_cooldown', retry_after=
                self._host_retry_until[urlparse(url).netloc.lower()] - time.monotonic())
        trace = []
        def record(outcome):
            trace.append(outcome)
            if outcome.status in (429, 503):
                host = urlparse(outcome.final_url or outcome.requested_url).netloc.lower()
                pause = max(2, outcome.retry_after)
                self._host_retry_until[host] = max(self._host_retry_until.get(host, 0), time.monotonic() + pause)
                # Исходный URL тоже не должен немедленно повторять цепочку редиректов.
                origin = urlparse(url).netloc.lower()
                self._host_retry_until[origin] = max(self._host_retry_until.get(origin, 0), time.monotonic() + pause)
        token = fetch_observer.set(record)
        deadline = async_deadline(max(0.001, float(self.config.page_fetch_timeout)))
        try:
            async with deadline:
                content = await self._fetch_page_content(url, category)
            if content:
                page = content[0] if isinstance(content, list) else content
                success = next((item for item in reversed(trace) if item.reason == 'ok'), None)
                return FetchOutcome(url, getattr(page, 'url', url),
                                    getattr(page, 'fetch_method', '') or (success.transport if success else ''),
                                    200, 'ok', False, content=content)
            failures = [item for item in trace if item.reason != 'ok']
            # Любой временный отказ делает вывод об исчезновении страницы недоказанным.
            transient = [item for item in failures if item.retryable]
            other = [item for item in failures if item.reason not in ('not_found', 'gone', 'soft_not_found')]
            outcome = (transient or other or failures or [FetchOutcome(url)])[-1]
            # Явный последний отказ периметра завершает попытку, даже если исходный
            # HTTP потребовал браузер из-за пустой панели характеристик.
            if failures and failures[-1].reason == 'out_of_scope':
                outcome = failures[-1]
            outcome.requested_url = url
            return outcome
        except (asyncio.TimeoutError, PlaywrightTimeoutError):
            return FetchOutcome(url, transport='deadline' if deadline.expired else '', reason='timeout')
        except Exception as exc:
            log.exception(f"Внутренняя ошибка получения страницы {url}")
            return FetchOutcome(url, reason='internal_error', retryable=False)
        finally:
            fetch_observer.reset(token)

    async def _fetch_page_content(self, url: str, category: str = None) -> Optional[Union[ParseResult, List[ParseResult]]]:
        """Лестница загрузки: одна проверка пригодности после каждой ступени."""
        if not self._is_allowed_url(url, base_url=url):
            return None
        if category == 'product' and self.url_categorizer.is_print_version(url, category):
            return None
        if self.profile is not None and self.host_throttle is not None:
            await self.host_throttle.acquire(urlparse(url).netloc.lower(),
                                             self.profile.crawl.load.page_delay_ms)

        force_browser = self.profile is not None and self.profile.crawl.render == 'browser'
        profile_antibot = self.profile.crawl.antibot if self.profile is not None else 'none'
        host = urlparse(url).netloc.lower()
        ajax_required = category == 'product' and (
            (self.profile is not None and self.profile.crawl.ajax_tabs)
            or any(host == domain or host.endswith('.' + domain)
                   for domain in (getattr(self.config, 'ajax_product_tabs_domains', []) or [])))
        dynamic_contacts = category in ('contacts', 'distributor') and self.config.dynamic_content_enabled
        if ajax_required:
            # Без интерактивного результата профиль не разрешает принять исходный DOM.
            stages = deque(['ajax_tabs'])
        elif force_browser or (dynamic_contacts and self.profile is not None
                               and self.profile.crawl.contact_ready_selector):
            stages = deque(['dynamic' if dynamic_contacts else 'playwright'])
        elif dynamic_contacts:
            stages = deque(['http_first', 'dynamic', 'aiohttp'] if profile_antibot in ('impersonate', 'warmup')
                           else ['dynamic', 'http_first', 'aiohttp'])
        else:
            stages = deque(['http_first', 'aiohttp', 'playwright'])

        while stages:
            if self._host_paused(url):
                break  # 429/503 закрывает все следующие ступени этой попытки.
            method = stages.popleft()
            try:
                if method == 'http_first':
                    content = await self._http_first_get(url, expect_product=category == 'product',
                                                         check_content=False)
                elif method == 'aiohttp':
                    content = await self._fetch_with_aiohttp(url)
                elif method == 'ajax_tabs':
                    extractor = self._make_dynamic_extractor()
                    extractor.product_tabs_only = not ajax_required
                    content = await extractor.extract_product_tabs_merged(url)
                elif method == 'dynamic':
                    extractor = self._make_dynamic_extractor()
                    content = await extractor.extract(url)
                    if getattr(extractor, 'incomplete_render', False):
                        return None  # Статический GET не заменяет незагруженную панель контактов.
                else:
                    result = await self._fetch_with_playwright(url)
                    content = None
                    if result is not None:
                        html, permanent, final_url = result
                        if html and not permanent:
                            content = html if isinstance(html, PageContent) else PageContent(
                                html, url, final_url or url, method=method)
                if self._host_paused(url):
                    break
                if not content:
                    continue

                parsed_pages = []
                for html in content if isinstance(content, list) else [content]:
                    parsed = await self._parse_content(html, url)
                    if parsed is None:
                        continue
                    # Длинный текст и JSON-LD не доказывают загрузку отдельной панели.
                    pending_tabs = (category in ('product', 'category')
                                    and has_unloaded_product_tabs(parsed.html))
                    if pending_tabs:
                        observe_fetch(url, method, reason='incomplete_render')
                        log.info(f"Лестница: {method} -> незагруженные товарные вкладки: {url}")
                        if method != 'ajax_tabs':
                            # Повторный обычный GET не раскроет уже обнаруженную вкладку.
                            stages = deque(['ajax_tabs'])
                        continue
                    if category == 'product':
                        usable = self._usable_product_content(
                            parsed, rendered=method not in ('http_first', 'aiohttp'))
                    else:
                        usable = not needs_javascript(parsed.html) and not is_unrendered_store_listing(parsed.html)
                    if not usable:
                        observe_fetch(url, method, reason='incomplete_render')
                        log.info(f"Лестница: {method} -> неполное содержимое: {url}")
                        continue
                    parsed.fetch_method = method
                    if parsed.url != url:
                        parsed.redirected_from = url
                        self._log_effective_url(url, parsed.url)
                    parsed_pages.append(parsed)
                if parsed_pages:
                    self._census_fetch(url, category, method)
                    log.debug(f"Лестница: принят результат {method}: {url}")
                    return parsed_pages if isinstance(content, list) else parsed_pages[0]
            except Exception as exc:
                # Отмена общего дедлайна и внешняя отмена (CancelledError) проходят наружу.
                observe_fetch(url, method, error=exc)
                log.warning(f"Лестница: отказ {method}, следующая ступень для {url}: {exc}")
        if self.metrics_collector is not None:
            self.metrics_collector.record_fetch_fail(url, category)
        return None

    async def _fetch_with_playwright(self, url: str) -> tuple[Optional[PageContent], bool, Optional[str]]:
        """
        Специализированная загрузка продуктовых страниц с полной обработкой динамического контента

        Returns:
            Tuple[Optional[str], bool, Optional[str]]: (содержимое страницы, была ли перманентная ошибка, финальный URL после редиректа)
        """
        if not self._is_allowed_url(url, base_url=url):
            return None, False, None
        if self._host_paused(url):
            return None, False, None
        normalized_url = self.url_normalizer.normalize_url(url)

        async with self._errors_cache_lock:
            if self._negative_cache_key(url, 'playwright') in self.permanent_errors_cache:
                observe_fetch(url, 'playwright', status=self._negative_cache_status.get(self._negative_cache_key(url, 'playwright')))
                log.debug(f"Пропускаем URL из кэша ошибок: {url}")
                return None, True, None

        context = await self.browser_pool.get_browser()
        page = None
        cleanup_guard = None
        state = {}
        try:
            page = await context.new_page()
            state = _track_document_response(page)
            cleanup_guard = await self._guard_browser_page(page, url, navigation_state=state)
            # ИЗМЕНЕНО: убрали set_default_timeout(6000), таймауты теперь явные

            log.info(f"Загружаем продуктовую страницу с улучшенной обработкой: {normalized_url}")

            # ИЗМЕНЕНО: увеличен таймаут до 20 сек, wait_until='domcontentloaded'
            response = await page.goto(url, wait_until='domcontentloaded', timeout=20000)
            state['response'] = response
            if response is not None and response.status != 200:
                observe_fetch(url, 'playwright', status=response.status, final_url=page.url, headers=getattr(response, 'headers', {}))
            if not self._is_allowed_url(page.url, base_url=url):
                observe_fetch(url, 'playwright', final_url=page.url, reason='out_of_scope')
                return None, False, None
            if page.is_closed():
                log.debug(f"Страница закрыта после goto для {url}")
                return None, False, None

            # ДОБАВЛЕНО: короткое ожидание networkidle (3 сек) – необязательно
            try:
                await page.wait_for_load_state('networkidle', timeout=3000)
            except Exception:
                pass  # нормально, не блокируем

            if response is not None and response.status in PERMANENT_ERRORS:
                log.warning(f"Playwright: Перманентная ошибка {response.status} для {url}")
                async with self._errors_cache_lock:
                    self._record_fetch_status(url, 'playwright', response.status)
                return None, True, None

            if response is not None and response.status != 200:
                return None, False, None
            initial = _browser_content(await page.content(), url, page, state, 'playwright')
            if page_error_reason(initial):
                return None, False, None
            await self._accept_cookie_consent(page)
            await self._scroll_page(page)
            await self._wait_for_product_content(page)
            await self._wait_for_dynamic_links(page)
            if not self._is_allowed_url(page.url, base_url=url):
                observe_fetch(url, 'playwright', final_url=page.url, reason='out_of_scope')
                return None, False, None
            content = await self._extract_dynamic_product_content(page)

            final_url = page.url
            if not self._is_allowed_url(final_url, base_url=url):
                observe_fetch(url, 'playwright', final_url=final_url, reason='out_of_scope')
                return None, False, None
            content = _browser_content(content, url, page, state, 'playwright')
            reason = page_error_reason(content)
            if reason:
                self._record_fetch_status(url, 'playwright', content.status if content.status != 200 else 0)
                return None, content.status in PERMANENT_ERRORS, final_url
            self._record_fetch_status(url, 'playwright', 200)
            log.info(f"Успешно обработана продуктовая страница через Playwright: {url}")
            return content, False, final_url

        except PlaywrightTimeoutError as e:
            _observe_browser_failure(url, 'playwright', e, state, self._is_allowed_url)
            log.warning(f"Таймаут при обработке продуктовой страницы {url}")
            return None, False, None
        except Exception as e:
            _observe_browser_failure(url, 'playwright', e, state, self._is_allowed_url)
            log.debug(f"Playwright не смог обработать продуктовой страницы {url}: {e}")
            return None, False, None
        finally:
            await _release_browser_resources(self.browser_pool, context, page, cleanup_guard)

    async def _scroll_page(self, page, scroll_step=500, scroll_delay=0.1):
        try:
            # Проверяем, не закрыта ли страница
            if page.is_closed():
                log.debug("Страница закрыта, пропускаем прокрутку")
                return

            last_height = await page.evaluate("document.body.scrollHeight")
            log.debug(f"Начальная высота страницы: {last_height}px")

            scroll_attempts = 0
            max_attempts = 20

            while scroll_attempts < max_attempts:
                if page.is_closed():
                    log.debug("Страница закрыта во время прокрутки")
                    break

                await page.evaluate(f"window.scrollBy(0, {scroll_step})")
                await asyncio.sleep(scroll_delay)

                if page.is_closed():
                    break

                new_height = await page.evaluate("document.body.scrollHeight")
                if new_height == last_height:
                    break

                last_height = new_height
                scroll_attempts += 1

            log.debug(f"Прокрутка завершена. Финальная высота: {last_height}px")

        except Exception as e:
            if "closed" in str(e).lower():
                log.debug(f"Страница закрыта при прокрутке: {e}")
            else:
                log.debug(f"Ошибка при прокрутке: {e}")

    async def _wait_for_product_content(self, page):
        """Ожидание загрузки ключевых элементов товара"""
        try:
            if page.is_closed():
                log.debug("Страница уже закрыта, пропускаем ожидание контента товара")
                return
            # Селекторы для ключевых элементов товара
            products_selectors = [
                # Цены
                '[class*="price"]', '[class*="cost"]', '[class*="стоимость"]', '[class*="цены"]',
                '.price', '.cost', '.product-price', '.item-price',
                # Характеристики
                '[class*="characteristic"]', '[class*="spec"]', '[class*="param"]',
                '.specifications', '.characteristics', '.properties', '.params',
                # Основные элементы товара
                '[class*="product"]', '[class*="item"]', '.product-details', 
                '.item-details', '.product', '.item', '.goods',
                # Параметры товара
                '.jshop_prod_attributes', '.attributes', '.product-attributes',
                '[class*="attribute"]', '[class*="attr"]', '.attributes_value',
                '.select-mask'
            ]
        
            # Любой подходящий элемент в одном общем окне ожидания.
            try:
                await page.wait_for_selector(', '.join(products_selectors), timeout=10000)
            except PlaywrightTimeoutError:
                pass

            # Дополнительное ожидание для AJAX-контента
            await page.wait_for_timeout(2000)

            try:
                radio_selectors = [
                    'input[type="radio"]',
                    '[class*="input_type_radio"]',
                    '.attributes_value input',
                    '.jshop_prod_attributes input'
                ]
                
                for selector in radio_selectors:
                    elements = await page.query_selector_all(selector)
                    if elements:
                        log.debug(f"Найдено {len(elements)} элементов с параметрами")
                        break
            except Exception as e:
                if "closed" in str(e).lower():
                    log.debug(f"Страница закрыта при поиске радио-кнопок: {e}")
                    return
                log.debug(f"Ошибка при поиске радио-кнопок: {e}")

        except Exception as e:
            if "closed" in str(e).lower():
                log.debug(f"Страница закрыта в _wait_for_product_content: {e}")
            else:
                log.warning(f"Ошибка при ожидании контента товара: {e}")


    async def _wait_for_dynamic_links(self, page):
        """Ожидание загрузки динамических ссылок и файлов"""
        try:
            if page.is_closed():
                log.debug("Страница уже закрыта, пропускаем ожидание динамических ссылок")
                return
            # Ждем появления элементов, которые могут содержать файловые ссылки
            file_indicators = [
                '//a[contains(@href, ".pdf")]',
                '//a[contains(@href, ".doc")]',
                '//a[contains(@href, ".xls")]',
                '//a[contains(text(), "скачать")]',
                '//a[contains(text(), "Скачать")]',
                '//*[contains(text(), "kB)")]',
                '//*[contains(text(), "MB)")]',
            ]
            
            try:
                await page.wait_for_selector('xpath=' + ' | '.join(file_indicators), timeout=3000)
            except PlaywrightTimeoutError:
                pass

            # Дополнительное ожидание для AJAX-контента
            await page.wait_for_timeout(2000)
            if page.is_closed():
                log.debug("Страница закрыта после ожидания индикаторов")
                return
            # Проверяем, появились ли ссылки в DOM
            file_links_count = await page.evaluate("""
                () => {
                    const links = document.querySelectorAll('a[href*=".pdf"], a[href*=".doc"], a[href*=".xls"]');
                    return links.length;
                }
            """)
            
            log.debug(f"Найдено {file_links_count} файловых ссылок после ожидания")
            
        except Exception as e:
            if "closed" in str(e).lower():
                log.debug(f"Страница закрыта в _wait_for_dynamic_links: {e}")
            else:
                log.warning(f"Ошибка при ожидании динамических ссылок: {e}")
            
    async def _extract_dynamic_product_content(self, page) -> str:
        """Извлечение динамического контента товара с сохранением параметров"""
        try:
            # Кликнуть на элементы параметров для активации
            try:
                # Найти и кликнуть на первый доступный элемент параметров
                param_elements = await page.query_selector_all(','.join([
                    '.jshop_prod_attributes input[type="radio"]',
                    '.attributes_value label',
                    '.select-mask'
                ]))
                
                if param_elements and len(param_elements) > 0:
                    # Кликнуть на несколько элементов для активации скриптов
                    for i in range(min(3, len(param_elements))):
                        try:
                            await param_elements[i].click()
                            await page.wait_for_timeout(300)
                        except:
                            continue
                            
            except Exception as e:
                log.debug(f"Ошибка при активации параметров: {e}")
            
            # Получаем полный HTML после активации
            final_html = await page.content()
            
            return final_html
        except Exception as e:
            log.warning(f"Ошибка извлечения динамического контента: {e}")
            return await page.content()

    # ------------------------------------------------------------------ #
    #  HTTP-first слой: curl_cffi с браузерным TLS/HTTP2-отпечатком        #
    # ------------------------------------------------------------------ #
    async def _curl_get_in_perimeter(self, session, url, headers, *, base_url):
        """Проверяет каждый переход до запроса и адрес ответа до чтения тела."""
        current = url
        for _ in range(11):
            if not self._is_allowed_url(current, base_url=base_url):
                observe_fetch(url, 'curl', final_url=current, reason='out_of_scope')
                return None
            if self._host_paused(current):
                observe_fetch(url, 'curl', final_url=current, reason='host_cooldown')
                return None
            response = await session.get(current, headers=headers, allow_redirects=False)
            final = str(response.url)
            if not self._is_allowed_url(final, base_url=base_url):
                observe_fetch(url, 'curl', final_url=final, reason='out_of_scope')
                return None
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get('Location') or response.headers.get('location')
                if not location:
                    observe_fetch(url, 'curl', status=response.status_code, final_url=final)
                    return None
                current = urljoin(final, location)
                continue
            return response
        observe_fetch(url, 'curl', final_url=current, reason='http_error')
        return None

    async def _guard_browser_page(self, page, base_url, *, navigation_state=None):
        """CDP проверяет каждый HTTP-переход; маршрут охраняет новые вкладки."""
        context = page.context
        sessions = {}
        session_lock = asyncio.Lock()

        async def attach(target):
            async with session_lock:
                if target in sessions:
                    return
                session = await context.new_cdp_session(target)
                main_frame_id = None
                if target is page and navigation_state is not None:
                    tree = await session.send('Page.getFrameTree', {})
                    main_frame_id = tree['frameTree']['frame']['id']

                async def paused(event):
                    request_id = event['requestId']
                    main_document = main_frame_id is not None and event.get('frameId') == main_frame_id
                    if self._is_allowed_url(event['request']['url'], base_url=base_url):
                        if main_document:
                            navigation_state.pop('blocked_url', None)
                        await session.send('Fetch.continueRequest', {'requestId': request_id})
                    else:
                        if main_document:
                            # requestfailed может сохранить исходный URL редиректа.
                            # Пишем адрес до блокировки, в состояние этой страницы.
                            navigation_state['blocked_url'] = event['request']['url']
                        await session.send('Fetch.failRequest', {
                            'requestId': request_id, 'errorReason': 'BlockedByClient'})

                session.on('Fetch.requestPaused', paused)
                await session.send('Fetch.enable', {'patterns': [
                    {'resourceType': 'Document', 'requestStage': 'Request'}]})
                sessions[target] = session

        await attach(page)

        async def guard(route):
            request = route.request
            if request.resource_type == 'document':
                if not self._is_allowed_url(request.url, base_url=base_url):
                    if navigation_state is not None and request.frame == page.main_frame:
                        navigation_state['blocked_url'] = request.url
                    await route.abort()
                    return
                # Маршрут срабатывает до первого запроса popup; CDP затем видит
                # и редиректы, для которых Playwright повторно route не вызывает.
                try:
                    await attach(request.frame.page)
                except Exception:
                    await route.abort()
                    return
            await route.continue_()

        await context.route('**/*', guard)

        async def cleanup():
            try:
                await asyncio.wait_for(context.unroute('**/*', guard),
                                       timeout=BrowserPool.CLOSE_TIMEOUT)
            except Exception as e:
                log.debug(f"Маршрут закрытого контекста не снят: {e}")
            for session in list(sessions.values()):
                try:
                    await asyncio.wait_for(session.detach(), timeout=BrowserPool.CLOSE_TIMEOUT)
                except Exception:
                    pass  # Закрытая вкладка уже освободила свою CDP-сессию.

        return cleanup

    async def _fetch_with_http_impersonate(
        self, url: str, *, session=None, proxy: Optional[str] = None
    ) -> Optional[HttpFetchResult]:
        """
        HTTP-GET с браузерным TLS/HTTP2-отпечатком (curl_cffi, impersonate).
        Заменяет «голый» aiohttp (ssl=False, без заголовков), который WAF режут.
        ВКЛЮЧЕНА нормальная проверка TLS (verify=True).

        session — если передан, переиспользуем сессию (для cookie-warmup).
        Возвращает HttpFetchResult или None при сетевой ошибке.
        """
        if self._host_paused(url):
            return None
        if not self._is_allowed_url(url, base_url=url) or not _CURL_CFFI_AVAILABLE:
            return None

        async with self._errors_cache_lock:
            if self._negative_cache_key(url, 'curl') in self.permanent_errors_cache:
                observe_fetch(url, 'curl', status=self._negative_cache_status.get(self._negative_cache_key(url, 'curl')))
                return HttpFetchResult(None, 0, {}, url, {}, from_cache_error=True)

        impersonate = getattr(self.config, 'impersonate_profile', 'chrome')
        verify = getattr(self.config, 'http_verify_tls', True)
        timeout = self._effective_http_timeout()
        own_session = session is None

        async with self.aiohttp_semaphore:
            try:
                sess = session
                if own_session:
                    kwargs = {"impersonate": impersonate, "verify": verify, "timeout": timeout}
                    if proxy:
                        kwargs["proxies"] = {"http": proxy, "https": proxy}
                    sess = _CurlAsyncSession(**kwargs)
                try:
                    resp = await self._curl_get_in_perimeter(sess, url, IMPERSONATE_HEADERS, base_url=url)
                    if resp is None:
                        return None
                    html = resp.text
                    observe_fetch(url, 'curl', status=resp.status_code, final_url=str(resp.url), headers=resp.headers, html=html)
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    try:
                        cookies = {c.name: c.value for c in sess.cookies.jar}
                    except Exception:
                        cookies = {}
                    if resp.status_code != 200 or not page_error_reason(html):
                        self._record_fetch_status(url, 'curl', resp.status_code)
                    return HttpFetchResult(html, resp.status_code, headers, str(resp.url), cookies)
                finally:
                    if own_session:
                        async with async_deadline(5):
                            await sess.close()
            except Exception as e:
                observe_fetch(url, 'curl', error=e)
                log.debug(f"curl_cffi не смог получить {url}: {e}")
                return None

    # Блок общих документов профиля -> ключ папки компании в domain_dirs
    _DOC_SECTION_DIRS = {'certificates': 'certificates_dir', 'documents': 'documents_dir',
                         'instructions': 'instructions_dir', 'price_list': 'price_lists_dir'}

    async def _download_company_documents(self, url: str, html: str, doc_section: str,
                                          domain_dirs: Dict[str, str]) -> None:
        """Скачивание общих документов компании со страницы блока профиля в папку
        Certificates/Documents/Instructions/Price_lists. Fail-open: ошибка скачивания
        не прерывает обработку страницы."""
        try:
            target_dir = (domain_dirs or {}).get(self._DOC_SECTION_DIRS[doc_section])
            if not target_dir:
                return
            saved = await self.file_download_manager.download_document_files(html, url, target_dir)
            if saved:
                log.info(f"Общие документы компании ({doc_section}): скачано {len(saved)} "
                         f"файлов со страницы {url} -> {target_dir}")
                if self.metrics_collector is not None:
                    self.metrics_collector.record_document_files(doc_section, len(saved))
        except Exception as e:
            log.error(f"Ошибка скачивания общих документов ({doc_section}) со страницы {url}: {e}")

    def _census_fetch(self, url: str, category, method: str) -> None:
        """Телеметрия профилирования: каким методом лестницы получена страница."""
        if self.metrics_collector is not None:
            self.metrics_collector.record_fetch(url, category, method)

    def _make_dynamic_extractor(self):
        """DynamicContentExtractor с пер-сайтовым селектором панели вкладки из профиля."""
        extra = self.profile.crawl.tab_panel_selector if self.profile is not None else None
        return DynamicContentExtractor(self.browser_pool, self.config, extra_tab_panel_selector=extra,
                                       guard_browser_page=self._guard_browser_page,
                                       allowed_url=self._is_allowed_url,
                                       accept_cookie_consent=self._accept_cookie_consent,
                                       tab_selector=self.profile.crawl.tab_selector if self.profile else None,
                                       ready_selector=self.profile.crawl.contact_ready_selector if self.profile else None)

    def _effective_http_timeout(self) -> int:
        """Таймаут HTTP-фетча: профиль сайта (crawl.load.fetch_timeout_s) -> config."""
        if self.profile is not None and self.profile.crawl.load.fetch_timeout_s:
            return self.profile.crawl.load.fetch_timeout_s
        return getattr(self.config, 'http_timeout', 30)

    @staticmethod
    def _is_challenge(res: Optional[HttpFetchResult]) -> bool:
        """
        Лёгкий детект антибот-челленджа по HTTP-ответу (без отдельного модуля):
        блок-статусы, cf-ray при не-200, маркеры страниц проверки в теле.
        """
        if res is None or res.html is None:
            return False
        if res.status in (403, 429, 503):
            return True
        if 'cf-ray' in res.headers and res.status != 200:
            return True
        text = res.html[:4000].lower()
        markers = (
            'checking your browser', 'cf-browser-verification', '__cf_chl',
            'just a moment', 'attention required', 'проверяем браузер',
            'ddos-guard', 'access denied',
        )
        return any(m in text for m in markers)

    async def _cookie_warmup_and_retry(self, url: str, base_url: str) -> Optional[HttpFetchResult]:
        """
        Cookie warmup: тянем главную в той же curl_cffi-сессии (собираем антибот-cookies
        cf_clearance/_abck/bm_sz), затем повторяем целевой URL той же сессией с Referer.
        Снимает часть Cloudflare/DDoS-Guard без запуска браузера. Прокси не требуется.
        """
        if not self._is_allowed_url(url, base_url=url) or not _CURL_CFFI_AVAILABLE:
            return None
        impersonate = getattr(self.config, 'impersonate_profile', 'chrome')
        verify = getattr(self.config, 'http_verify_tls', True)
        timeout = self._effective_http_timeout()
        delay = getattr(self.config, 'warmup_delay_seconds', 1.0)
        home = base_url or url
        async with self.aiohttp_semaphore:
            sess = _CurlAsyncSession(impersonate=impersonate, verify=verify, timeout=timeout)
            try:
                # 1) прогрев главной — собираем cookies в сессию
                warmup = await self._curl_get_in_perimeter(sess, home, IMPERSONATE_HEADERS, base_url=url)
                if warmup is None:
                    return None
                if warmup.status_code in (429, 503):
                    observe_fetch(url, 'curl', status=warmup.status_code, final_url=str(warmup.url), headers=warmup.headers)
                    return None
                await asyncio.sleep(delay)
                # 2) повтор цели в той же сессии
                hdrs = {**IMPERSONATE_HEADERS, "Referer": home, "Sec-Fetch-Site": "same-origin"}
                resp = await self._curl_get_in_perimeter(sess, url, hdrs, base_url=url)
                if resp is None:
                    return None
                observe_fetch(url, 'curl', status=resp.status_code, final_url=str(resp.url), headers=resp.headers, html=resp.text)
                if resp.status_code != 200 or not page_error_reason(resp.text):
                    self._record_fetch_status(url, 'curl', resp.status_code)
                headers = {k.lower(): v for k, v in resp.headers.items()}
                try:
                    cookies = {c.name: c.value for c in sess.cookies.jar}
                except Exception:
                    cookies = {}
                return HttpFetchResult(resp.text, resp.status_code, headers, str(resp.url), cookies)
            except Exception as e:
                observe_fetch(url, 'curl', error=e)
                log.debug(f"warmup не удался для {url}: {e}")
                return None
            finally:
                async with async_deadline(5):
                    await sess.close()

    def _product_shell_min_text(self) -> int:
        """Порог гейта «оболочки» товарной страницы (0 = гейт выключен)."""
        return getattr(self.config, 'product_min_text', 1000)

    async def _http_first_get(self, url: str, *, expect_product: bool = False, check_content: bool = True) -> Optional[PageContent]:
        """
        Лестница эскалации на уровне HTTP (без Playwright):
          1) HTTP-impersonate -> 2) при challenge: cookie-warmup -> повтор.
        Возвращает PageContent (валидный 200 с адресом ответа) либо None — тогда вызывающий
        эскалирует к Playwright.
        """
        if not self._is_allowed_url(url, base_url=url):
            return None
        if not (getattr(self.config, 'http_first_enabled', True) and _CURL_CFFI_AVAILABLE):
            return None
        res = await self._fetch_with_http_impersonate(url)
        if res is None or res.from_cache_error:
            return None
        observe_fetch(url, 'curl', status=res.status, final_url=res.final_url, headers=res.headers, html=res.html)
        if res.status in (429, 503):
            return None
        # Антибот-челлендж -> прогрев и повтор. Профиль antibot=warmup: прогреваем
        # агрессивнее — при любом не-200 (не только по маркерам челленджа).
        _profile_warmup = (self.profile is not None
                           and self.profile.crawl.antibot == 'warmup' and res.status != 200)
        _challenged = (getattr(self.config, 'antibot_in_fetch_enabled', True)
                       and self._is_challenge(res))
        if _challenged and self.metrics_collector is not None:
            self.metrics_collector.record_challenge(url)
        if _profile_warmup or _challenged:
            warm = await self._cookie_warmup_and_retry(url, self.current_base_url)
            if self.metrics_collector is not None:
                self.metrics_collector.record_warmup(warm is not None and warm.status == 200)
            if warm is not None and warm.status == 200:
                res = warm
        if res.status != 200 or not res.html:
            return None
        if not self._is_allowed_final_url(url, res.final_url):
            return None
        content = PageContent(res.html, url, res.final_url, res.status, res.headers, 'http_first')
        if page_error_reason(content):
            return None
        # Общая лестница проверяет содержимое после разбора, включая AJAX-вкладки.
        if not check_content:
            return content
        # Tilda store-ЛИСТИНГ с JS-плитками: статический HTML не содержит ссылок на
        # товары (рендерятся браузером). HTTP-first бесполезен — форсим Playwright,
        # иначе целый раздел каталога теряется (D96). Проверка до needs_javascript и
        # фолбэка has_product_island (иначе объёмный текст листинга их обманывает).
        if is_unrendered_store_listing(res.html):
            return None
        # Товарная страница-«оболочка»: разметка есть, видимого текста почти нет — контент
        # дорисовывает JS. needs_javascript такое пропускает (ему нужен ещё признак SPA),
        # поэтому HTTP-first отдавал страницу без товара и LLM отвергал её как Trash_418#.
        # Отдаём None -> лестница идёт на aiohttp/Playwright. Неотрендеренная
        # оболочка не сохраняется как успешная товарная страница.
        min_text = self._product_shell_min_text()
        if expect_product and min_text and looks_like_product_shell(res.html, min_text=min_text):
            log.debug(f"HTTP-first: товарная страница похожа на оболочку, эскалируем в браузер: {url}")
            return None
        min_content = getattr(self.config, 'http_min_content', 800)
        # Контент пришёл и его достаточно / data-island / не нужен JS -> отдаём HTML
        if len(res.html) >= min_content and not needs_javascript(
            res.html, text_threshold=getattr(self.config, 'js_render_text_threshold', 300)
        ):
            return content
        # Контент есть, но похоже нужен JS — если есть товарный остров, всё равно отдаём
        if res.html and has_product_island(res.html):
            return content
        return None

    async def _fetch_with_aiohttp(self, url: str) -> Optional[PageContent]:
        """Получение контента с проверкой каждого HTTP-перехода."""
        if self._host_paused(url):
            return None
        if not self._is_allowed_url(url, base_url=url):
            return None
        async with self._urls_lock:
            if self._negative_cache_key(url, 'aiohttp') in self.permanent_errors_cache:
                observe_fetch(url, 'aiohttp', status=self._negative_cache_status.get(self._negative_cache_key(url, 'aiohttp')))
                return None
        async with self.aiohttp_semaphore:
            try:
                connector = aiohttp.TCPConnector(ssl=False)
                async with aiohttp.ClientSession(connector=connector) as session:
                    return await self._aiohttp_get_in_perimeter(session, url, base_url=url)
            except Exception as e:
                observe_fetch(url, 'aiohttp', error=e)
                log.debug(f"aiohttp не смог получить {url}: {e}")
                return None

    async def _aiohttp_get_in_perimeter(self, session, url, *, base_url, json_response=False):
        """Автопереходы отключены: тело читается только после проверки адреса."""
        current = url
        for _ in range(11):
            if not self._is_allowed_url(current, base_url=base_url):
                if not json_response:
                    observe_fetch(url, 'aiohttp', final_url=current, reason='out_of_scope')
                return None
            if not json_response and self._host_paused(current):
                observe_fetch(url, 'aiohttp', final_url=current, reason='host_cooldown')
                return None
            async with session.get(
                current, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=20 if json_response else self._effective_http_timeout()),
                headers={'X-Requested-With': 'XMLHttpRequest'} if json_response else None,
            ) as response:
                final = str(response.url)
                if not self._is_allowed_url(final, base_url=base_url):
                    if not json_response:
                        observe_fetch(url, 'aiohttp', final_url=final, reason='out_of_scope')
                    return None
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get('Location') or response.headers.get('location')
                    if not location:
                        if not json_response:
                            observe_fetch(url, 'aiohttp', status=response.status, final_url=final)
                        return None
                    current = urljoin(final, location)
                    continue
                if not json_response and response.status != 200:
                    observe_fetch(url, 'aiohttp', status=response.status, final_url=final, headers=response.headers)
                if response.status != 200:
                    if response.status in PERMANENT_ERRORS and not json_response:
                        self._record_fetch_status(url, 'aiohttp', response.status)
                    return None
                if json_response:
                    data = await response.json(content_type=None)
                    return data if isinstance(data, dict) else None
                content = PageContent(await response.text(), url, final, response.status,
                                      dict(response.headers), 'aiohttp')
                observe_fetch(url, 'aiohttp', status=response.status, final_url=final, headers=response.headers, html=content)
                if page_error_reason(content):
                    return None
                self._record_fetch_status(url, 'aiohttp', 200)
                return content
        if not json_response:
            observe_fetch(url, 'aiohttp', final_url=current, reason='http_error')
        return None

    async def _parse_content(self, html: str, url: str) -> Optional[ParseResult]:
        """Парсинг контента страницы с учетом canonical URL"""
        reason = page_error_reason(html)
        if reason:
            observe_fetch(url, getattr(html, 'method', 'page'), status=getattr(html, 'status', 200),
                          final_url=getattr(html, 'final_url', url), html=html)
            log.warning(f"Страница отклонена до разбора: {url}: {reason}")
            return None
        try:
            requested_url = getattr(html, 'requested_url', url)
            url = getattr(html, 'final_url', url)
            if not self._is_allowed_final_url(requested_url, url):
                raise ValueError(f"Редирект вне периметра: {requested_url} -> {url}")
            if getattr(html, 'status', 200) == 200:
                self._record_fetch_status(requested_url, getattr(html, 'method', 'page'), 200)
                self._record_fetch_status(url, getattr(html, 'method', 'page'), 200)
            # D82: для Bitrix-шаблона betar.ru дотягиваем блок «Исполнение и цена» (AJAX
            # product_offers.php) и вклеиваем таблицу исполнений в HTML — до конвертации в
            # markdown и до сохранения cleaned-HTML (сохраняется именно parse_result.html).
            # No-op для всех прочих доменов/страниц (гейт внутри метода).
            html = await self._enrich_bitrix_offers(html, url)

            if product_state_key(requested_url):
                requested = urlparse(requested_url)
                final = urlparse(url)
                if not final.fragment and final.path.rstrip('/') == requested.path.rstrip('/'):
                    url = urlunparse(final._replace(fragment=requested.fragment))
                selected = isolate_product_state(html, url)
                if product_state_key(url) != product_state_key(requested_url) or selected is None:
                    observe_fetch(requested_url, getattr(html, 'method', 'page'), reason='incomplete_render')
                    return None
                html = selected
            soup = BeautifulSoup(html, 'html.parser')
            
            canonical_url = self.url_normalizer.extract_canonical_url(soup, url)            
            
            # P01 U1 / ревью слияния F5: canonical — только ключ дедупа. Базой склейки
            # относительных ссылок и определения типа страницы остаётся фактический адрес:
            # нормализованный canonical терял '/ru' и завершающий слеш (D41 против D198/D120),
            # а canonical на корень красил любую страницу в main_page (D278).
            effective_url = url
            
            content_type = self._detect_content_type(soup, effective_url)            
            
            additional_links = await self._extract_additional_links(soup, effective_url)            
            
            elements_found = self._count_elements(soup)
            
            return ParseResult(
                soup=soup,
                html=html,
                additional_links=additional_links,
                content_type=content_type,
                elements_found=elements_found,
                url=url,
                requested_url=requested_url,
                canonical_url=canonical_url
            )
            
        except Exception as e:
            log.error(f"Ошибка парсинга контента {url}: {e}")
            raise

    async def _enrich_bitrix_offers(self, html: str, url: str) -> str:
        """D82: вклеивает блок «Исполнение и цена» betar.ru в HTML товарной страницы.

        На Bitrix-шаблоне сайта таблица исполнений (типоразмеры, фасеты конфигуратора, цена)
        рендерится AngularJS отдельным запросом product_offers.php; в статическом HTML на её
        месте только пустой тег <product-container product-container="N"> и слово «Исполнение:».
        Дотягиваем JSON исполнений и вставляем в HTML таблицу характеристик (без цены) плюс
        отдельный блок цены. Цена намеренно вынесена в свой блок, чтобы на извлечении она ушла
        строго в поле price и не смешалась с характеристиками. No-op, если домена нет в
        config.bitrix_offers_domains или на странице нет тега product-container."""
        try:
            domains = getattr(self.config, 'bitrix_offers_domains', []) or []
            profile_bitrix = self.profile is not None and self.profile.crawl.bitrix_offers
            if (not domains and not profile_bitrix) or 'product-container' not in html:
                return html
            host = urlparse(url).netloc.lower()
            if not profile_bitrix and not any(host == d or host.endswith('.' + d) for d in domains):
                return html
            m = re.search(r'<product-container[^>]*\bproduct-container="(\d+)"', html)
            if not m:
                return html
            section_id = m.group(1)
            data = await self._fetch_offers_json(url, section_id)
            if not data:
                return html
            soup = BeautifulSoup(html, 'html.parser')
            block, n_offers = self._build_offers_block(soup, data)
            if block is None:
                return html
            anchor = soup.find('product-filter-block') or soup.find('product-container')
            if anchor is None:
                return html
            anchor.insert_after(block)
            log.info(f"Bitrix offers (D82): вклеено {n_offers} исполнений (section_id={section_id}): {url}")
            return str(soup)
        except Exception as e:
            log.debug(f"Bitrix offers enrich не удался для {url}: {e}")
            return html

    async def _fetch_offers_json(self, page_url: str, section_id: str) -> Optional[dict]:
        """Дотягивает JSON исполнений, соблюдая периметр страницы на каждом переходе."""
        p = urlparse(page_url)
        endpoint = f"{p.scheme}://{p.netloc}/local/ajax/product_offers.php?section_id={section_id}&lang=ru"
        if not self._is_allowed_url(endpoint, base_url=page_url):
            return None
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                return await self._aiohttp_get_in_perimeter(
                    session, endpoint, base_url=page_url, json_response=True)
        except Exception as e:
            log.debug(f"product_offers.php не получен (section_id={section_id}): {e}")
            return None

    def _build_offers_block(self, soup: BeautifulSoup, data: dict) -> Tuple[Optional[Tag], int]:
        """Строит <div> с таблицей характеристик исполнений и (отдельно) блоком цены.

        Колонки таблицы = фасеты конфигуратора с человекочитаемыми подписями: значение исполнения
        по коду фасета джойнится в filter[].value[enum_id] → подпись. Булев фасет (подпись значения
        совпадает с названием фасета, напр. «Обратный клапан») выводится как «да». Пустые во всех
        строках колонки отбрасываются. Цена (PRICE[0].PRICE) в таблицу НЕ идёт — только в свой блок."""
        offers = data.get('offers') or []
        raw_filters = data.get('filter') or []
        if not offers:
            return None, 0

        cols = []  # (title, code, value_map)
        for f in raw_filters:
            if not isinstance(f, dict):
                continue
            title = (f.get('title') or f.get('code') or '').strip()
            code = f.get('code')
            vmap = f.get('value') if isinstance(f.get('value'), dict) else {}
            if code and title:
                cols.append((title, code, vmap))

        body_rows = []  # (name, [cell, ...])
        prices = []
        for o in offers:
            name = (o.get('NAME') or '').strip()
            if not name:
                continue
            cells = []
            for title, code, vmap in cols:
                raw = o.get(code)
                if raw in (None, 0, '0', '', 'N'):
                    cells.append('')
                    continue
                label = vmap.get(str(raw))
                if not label:
                    label = 'да' if raw == 'Y' else str(raw)
                elif label.strip().lower() == title.lower():
                    label = 'да'
                cells.append(str(label).replace('|', ' / '))
            body_rows.append((name, cells))
            price = o.get('PRICE')
            if isinstance(price, list) and price and isinstance(price[0], dict) and price[0].get('PRICE'):
                try:
                    prices.append(int(float(price[0]['PRICE'])))
                except (ValueError, TypeError):
                    pass

        if not body_rows:
            return None, 0

        # Отбрасываем колонки, пустые во всех строках
        keep = [i for i in range(len(cols)) if any(r[1][i] for r in body_rows)]

        wrapper = soup.new_tag('div')
        wrapper['class'] = 'betar-offers'
        heading = soup.new_tag('h3')
        heading.string = 'Исполнение'
        wrapper.append(heading)

        table = soup.new_tag('table')
        wrapper.append(table)
        header = soup.new_tag('tr')
        table.append(header)
        th = soup.new_tag('th')
        th.string = 'Исполнение'
        header.append(th)
        for i in keep:
            th = soup.new_tag('th')
            th.string = cols[i][0]
            header.append(th)
        for name, cells in body_rows:
            tr = soup.new_tag('tr')
            table.append(tr)
            td = soup.new_tag('td')
            td.string = name
            tr.append(td)
            for i in keep:
                td = soup.new_tag('td')
                td.string = cells[i]
                tr.append(td)

        # Блок цены — строго отдельно от характеристик (только если цены есть)
        if prices:
            lo, hi = min(prices), max(prices)
            value = f"{lo} руб." if lo == hi else f"от {lo} до {hi} руб."
            price_heading = soup.new_tag('h3')
            price_heading.string = 'Цена'
            wrapper.append(price_heading)
            price_p = soup.new_tag('p')
            price_p.string = f"Цена: {value}"
            wrapper.append(price_p)

        return wrapper, len(body_rows)

    def _detect_content_type(self, soup: BeautifulSoup, url: str) -> str:
        """Определение типа контента"""
        category, _ = self.url_categorizer.categorize_url(url)
        
        # Дополнительная проверка по содержимому для товарных страниц
        if category == 'product':
            if self.url_categorizer.is_product_page_by_content(soup, url):
                return 'product'
            else:
                return 'other'
                
        return category

    async def _extract_additional_links(self, soup: BeautifulSoup, current_url: str) -> List[Tuple[str, str, int]]:
        """Извлечение дополнительных ссылок со страницы"""
        links = []
        
        try:
            link_base = document_base_url(soup, current_url)
            current_parsed = urlparse(current_url)
            current_scheme = current_parsed.scheme

            # Источники адресов: обычные <a href> и элементы, у которых переход
            # записан в onclick/data-href/data-url (D157). Второй вид в общем
            # сборщике не разбирался вовсе, и карточки за плитками Bitrix-шаблона
            # оставались недостижимыми.
            link_sources = [(link, link['href'], True) for link in soup.find_all('a', href=True)]
            try:
                for holder in soup.select('[onclick], [data-href], [data-url]'):
                    js_href = extract_element_url(holder)
                    if js_href:
                        link_sources.append((holder, js_href, False))
            except Exception as e:
                log.debug(f"Ошибка разбора onclick на {current_url}: {e}")

            # P04 U4 (хвост полосы B): если карты сайта не было, набор URL хоста
            # впервые становится известен здесь — на ссылках первой страницы.
            # Признак «плоский сайт» считаем один раз на компанию.
            if not self._flat_site_observed:
                self._flat_site_observed = True
                self.url_categorizer.observe_urls(
                    urljoin(link_base, sanitize_href(raw)) for _el, raw, _is_a in link_sources)

            # Извлекаем все ссылки со страницы
            for link, raw_href, is_anchor in link_sources:
                # D257/D233: чиним href ДО склейки — пробельные края (403 на «испорченном
                # адресе», а не «доступ запрещён») и вклеенный в путь абсолютный URL
                # (иначе глубина считается по мангленному пути и шлюз очереди выбрасывает
                # всю навигацию сайта).
                href = sanitize_href(raw_href)
                if self.metrics_collector is not None and is_anchor and href != raw_href:
                    kind = 'whitespace' if href == raw_href.strip() else 'glued'
                    log.debug(f"Исправлен href ({kind}): {raw_href!r} -> {href!r} на {current_url}")
                    self.metrics_collector.record_mangled_href(kind)
                if self.metrics_collector is not None and is_anchor:
                    self.metrics_collector.record_href_seen()
                if (not href or href.startswith(('javascript:', 'mailto:', 'tel:'))
                        or (href.startswith('#') and not product_state_key(urljoin(link_base, href)))):
                    continue

                full_url = urljoin(link_base, href)

                # Нормализуем URL с сохранением протокола текущей страницы
                normalized_url = self.url_normalizer.normalize_url(full_url)
                
                # Если URL не имеет схемы после нормализации, добавляем схему текущей страницы
                parsed_full = urlparse(normalized_url)
                if not parsed_full.scheme and current_scheme:
                    normalized_url = urlunparse((
                        current_scheme,
                        parsed_full.netloc,
                        parsed_full.path,
                        parsed_full.params,
                        parsed_full.query,
                        parsed_full.fragment
                    ))
                    
                # Пропускаем ссылки на изображения и файлы
                if (self.url_categorizer._is_image_url(full_url) or 
                    self.url_categorizer.is_downloadable_file(full_url)):
                    continue
                # У плитки с onclick текстом бывает вся её начинка — обрезаем, иначе
                # случайное слово из описания перекрасит роль ссылки
                link_text = link.get_text(strip=True)[:200] if not is_anchor else link.get_text(strip=True)

                # Категоризируем URL
                category, priority = self.url_categorizer.categorize_url(full_url, link_text)

                # P04 U4 п.1 (D100): листинг каталога обходим ПОСЛЕ карточек — форма
                # URL (пагинация, раздел без товарного слага) понижает приоритет на 1.
                # Роль не меняется: страница по-прежнему обходится и сохраняется.
                if category == 'product' and self.url_categorizer.is_listing_url(full_url):
                    priority = max(priority - 1, 1)

                # Добавляем в список
                links.append((full_url, category, priority))
            
            links.extend((address, 'product', self.url_categorizer.priority_levels['product'])
                         for address in tilda_product_links(soup, current_url) + inline_product_links(soup, current_url))

            # Извлекаем товарные ссылки из сеток
            product_links = self.product_grid_parser.extract_product_links(soup, current_url, current_url)
            links.extend(product_links)

            # Удаляем дубликаты. P04 U4: один и тот же адрес приходит и общим сборщиком,
            # и из товарной сетки — оставляем вариант с НАИБОЛЬШИМ приоритетом, иначе
            # set() выбирал между ними случайно и приоритет сетки терялся.
            best = {}
            for link_url, link_category, link_priority in links:
                current = best.get(link_url)
                if current is None or link_priority > current[2]:
                    best[link_url] = (link_url, link_category, link_priority)

            return list(best.values())
            
        except Exception as e:
            log.error(f"Ошибка извлечения ссылок: {e}")
            return []

    async def _extract_links(self, url: str, parse_result: ParseResult, depth: int, base_url: str = None) -> List[Tuple[str, str, int]]:
        """ Извлечение ссылок с фильтрацией.
        base_url – основной домен для проверки эквивалентности.
        """
        if base_url is None:
            base_url = url

        all_links = parse_result.additional_links
        filtered_links = []
        # Используем базовый домен для проверки
        base_domain_for_check = base_url
        # P05 U3 п.6: отброшенные гейтом периметра хосты — разрезом, а не общим счётчиком
        skipped_hosts = {}

        for link_url, category, priority in all_links:
            if category == 'product' and self.url_categorizer.is_print_version(link_url, category):
                log.debug(f"Пропускаем ссылку на печатную версию: {link_url}")
                continue
            # P01 U3: хост и схема ссылки приводятся к подтверждённой рабочей форме ДО
            # всех гейтов и до нормализации (D87 www, D134 http->https, D270 www-дубль).
            canonical_link = self._canonicalize_to_working_base(link_url)
            if canonical_link != link_url:
                log.debug(f"Ссылка приведена к рабочей базе: {link_url} -> {canonical_link}")
                link_url = canonical_link
            try:
                # Одна политика независимо от старого переключателя ignore_subdomains.
                if not self._in_crawl_perimeter(link_url, base_domain_for_check):
                    if self.config.log_skipped_subdomains:
                        log.info(f"Пропускаем домен вне профиля: {link_url} (основной: {base_domain_for_check})")
                    host = self._url_host(link_url)
                    skipped_hosts[host] = skipped_hosts.get(host, 0) + 1
                    self._skipped_link_hosts[host] = self._skipped_link_hosts.get(host, 0) + 1
                    async with self._stats_lock:
                        self.stats['skipped_subdomains'] += 1
                    continue

                normalized_url = self.url_normalizer.normalize_url(link_url)
                if normalized_url in self.visited_urls:
                    continue

                if self._queue_url_rejection(link_url, depth + 1, category, base_url):
                    continue

                filtered_links.append((link_url, category, priority))

            except Exception as e:
                log.debug(f"Ошибка фильтрации ссылки {link_url}: {e}")
                continue

        if self.metrics_collector is not None:
            # P05 U2: счётчики периметра (реализованы полосой C, вызов — здесь)
            self.metrics_collector.record_link_gate(len(all_links), sum(skipped_hosts.values()))

        # Сортировка и удаление дубликатов
        unique_links = []
        seen_urls = set()
        for link_url, category, priority in filtered_links:
            normalized = self.url_normalizer.normalize_url(link_url)
            if normalized not in seen_urls:
                seen_urls.add(normalized)
                unique_links.append((link_url, category, priority))

        unique_links.sort(key=lambda x: x[2], reverse=True)
        return unique_links

    def _should_add_to_queue(self, url: str, depth: int) -> bool:
        """Проверка, нужно ли добавлять URL в очередь"""
        category, _ = self.url_categorizer.categorize_url(url)
        reason = self._queue_url_rejection(url, depth, category)
        if reason:
            self._count_queue_gate_rejection(reason)
            return False
        normalized_url = self.url_normalizer.normalize_url(url)
        if normalized_url in self.visited_urls or normalized_url in self.processed_urls:
            return False

        # Для товарных страниц ограничиваем количеством (P04 U4 п.2: плюс резерв под карточки)
        if category == 'product':
            reason = self._product_quota_reject_reason(url)
            if reason:
                self._count_queue_gate_rejection(reason)
                self._quota_rejected_urls.append(url)
                self._quota_rejected_depths.setdefault(url, depth)
                return False

        return True

    def _count_queue_gate_rejection(self, reason: str) -> None:
        """D193/D143/D212: гейты очереди отказывали немым return False. Копим причины
        отказа, чтобы после обхода компании увидеть их сводкой (лог одной строкой на
        каждый отказ утопил бы всё остальное)."""
        self._queue_gate_rejections[reason] = self._queue_gate_rejections.get(reason, 0) + 1

    def _count_elements(self, soup: BeautifulSoup) -> Dict[str, int]:
        """Подсчет элементов на странице"""
        return {
            'links': len(soup.find_all('a')),
            'images': len(soup.find_all('img')),
            'tables': len(soup.find_all('table')),
            'forms': len(soup.find_all('form')),
            'scripts': len(soup.find_all('script')),
            'styles': len(soup.find_all('style'))
        }

    async def _update_stats(self, category: str, url: str = ''):
        """Обновление статистики"""
        async with self._stats_lock:
            if category == 'product':
                self.stats['product_pages'] += 1
                # product_urls здесь НЕ наполняем: счётчик ведётся при ПОСТАНОВКЕ URL в очередь
                # (_should_add_to_queue_parallel / _add_sitemap_urls_to_queue), где держится кэп.
                # Добавление при обработке перебирало бы кэп за счёт стартовых URL, попадающих
                # в очередь в обход admission-проверки (поймано смоук-тестом 2026-06-22).
            elif category == 'category':
                self.stats['category_pages'] += 1
            elif category == 'price_list':
                self.stats['price_list_pages'] += 1  
            elif category == 'main_page':
                self.stats['main_pages'] += 1
            elif category == 'contacts':
                self.stats['contact_pages'] += 1
            elif category == 'distributor':
                self.stats['distributor_pages'] += 1
            else:
                self.stats['other_pages'] += 1

    async def _save_crawling_stats(self, site_url: str, domain_dirs: Dict[str, str]):
        """Сохранение статистики краулинга"""
        try:
            stats_file = os.path.join(domain_dirs['base_domain_dir'], 'crawling_stats.json')
            
            async with self._stats_lock:
                stats_data = {
                    'site_url': site_url,
                    'crawling_date': datetime.now().isoformat(),
                    'statistics': self.stats,
                    'config': {
                        'max_depth': self.max_depth,
                        'max_pages_per_site': self.max_pages_per_site,
                        'max_product_pages_per_site': self.max_product_pages_per_site
                    }
                }
            
            with open(stats_file, 'w', encoding='utf-8') as f:
                json.dump(stats_data, f, ensure_ascii=False, indent=2)
                
            log.info(f"Статистика сохранена: {stats_file}")
            
        except Exception as e:
            log.error(f"Ошибка сохранения статистики: {e}")

    def _is_valid_url(self, url: str) -> bool:
        """Проверка валидности URL"""
        try:
            parsed = urlparse(url)
            return bool(parsed.netloc) and bool(parsed.scheme)
        except Exception:
            return False
        
    async def _save_single_page(self, url: str, normalized_url: str, parse_result: ParseResult,
                            depth: int, category: str, company_name: str,
                            domain_dirs: Dict[str, str], stored_pages: List[Dict],
                            queue: deque, redirected_from: Optional[str] = None) -> Optional[Dict]:
        """
        Сохраняет одну страницу и извлекает ссылки.
        ИЗМЕНЕНИЕ: сохраняется оригинальный HTML (без очистки).
        """
        # Берём оригинальный HTML из ParseResult
        html = parse_result.html

        # Проверка минимального размера
        if len(html) < 800:
            log.info(f"Пропускаем сохранение страницы {url}: размер контента менее 800 символов")
            return None

        requested_url = parse_result.requested_url or url
        url = parse_result.url or url
        normalized_url = self.url_normalizer.normalize_url(url)

        # Подготовка метаданных
        metadata = {
            'url': url,
            'requested_url': requested_url,
            'canonical_url': parse_result.canonical_url,
            'fetch_method': parse_result.fetch_method,
            'normalized_url': normalized_url,
            'category': category,
            'content_type': parse_result.content_type,
            'depth': depth,
            'timestamp': datetime.now().isoformat(),
            'elements_found': parse_result.elements_found,
            'file_size_bytes': len(html),
            'priority': {'product': 9, 'contacts': 7, 'distributor': 7, 'main_page': 7}.get(category, 1)
        }
        
        if redirected_from:
            metadata['redirected_from'] = redirected_from

        # Сохраняем оригинальный HTML во временное хранилище
        file_path, is_new, storage_metadata = await self.temp_storage.save_cleaned_html(
            url, normalized_url, html, company_name, metadata, category
        )

        if file_path:
            self._usable_page_urls.add(self.url_normalizer.normalize_url(requested_url))
        else:
            self._record_page_failure(requested_url, 'storage_error', processing=True)

        if category == 'contacts':
            # Дополнительно сохраняем как дистрибьютора, если нужно
            await self._save_as_distributor_if_needed(url, normalized_url, html, company_name, metadata)

        stored_page = None
        if file_path and is_new:
            stored_page = {
                'file_path': file_path,
                'url': url,
                'category': category,
                'normalized_url': normalized_url,
                'timestamp': metadata['timestamp'],
                'metadata': storage_metadata
            }
            if stored_pages is not None:
                stored_pages.append(stored_page)

        # Извлекаем ссылки и добавляем в очередь
        new_links = await self._extract_links(url, parse_result, depth)
        for link_url, link_category, priority in new_links:
            if await self._should_add_to_queue_parallel(link_url, depth + 1, link_category):
                queue.append((link_url, depth + 1, link_category, priority))
                async with self._urls_lock:
                    self.visited_urls.add(self.url_normalizer.normalize_url(link_url))

        await self._update_stats(category, url)
        return stored_page

    async def _save_as_distributor_if_needed(self, url: str, normalized_url: str, html: str,
                                        company_name: str, metadata: Dict):
        distributor_metadata = metadata.copy()
        distributor_metadata['category'] = 'distributor'
        distributor_metadata['is_contact_duplicate'] = True
        await self.temp_storage.save_cleaned_html(
            url, normalized_url, html, company_name, distributor_metadata, 'distributor'
        )
