# web_crawler.py 1.0.0
import re
import os
import gc
import time
import json
import hashlib
import psutil
import aiohttp
import asyncio
import logging
import aiofiles
from datetime import datetime
from contextlib import nullcontext
from collections import deque
from dataclasses import dataclass
from bs4 import BeautifulSoup, Tag
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

from config import Config
from product_utils import sanitize_filename
from temp_storage_manager import TempStorageManager
from domain_equivalency import DomainEquivalencyManager
from data_island import (needs_javascript, has_product_island, is_unrendered_store_listing,
                         looks_like_product_shell)

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


PERMANENT_ERRORS = [
    400, 401, 402, 403, 404, 405, 406, 407, 409, 410, 
    411, 412, 413, 414, 415, 416, 417, 418, 421, 422, 
    423, 424, 425, 426, 428, 431, 451, 501, 505, 510
]

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

@dataclass
class ParseResult:
    soup: BeautifulSoup
    html: str
    additional_links: List[Tuple[str, str, int]]
    content_type: str
    elements_found: Dict[str, int]


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
        try:
            if self.stealth_enabled:
                # Реалистичный контекст вместо «голого» — снижает детект автоматизации
                # частью WAF/антибот-систем (UA/locale/viewport/timezone + маскировка webdriver).
                context = await self._browser.new_context(
                    user_agent=self.stealth_user_agent,
                    locale="ru-RU",
                    timezone_id="Europe/Moscow",
                    viewport={"width": 1920, "height": 1080},
                )
                try:
                    await context.add_init_script(
                        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                    )
                except Exception:
                    pass
            else:
                context = await self._browser.new_context()
            return context
        except Exception:
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

    def __init__(self, browser_pool: BrowserPool, config: Config, extra_tab_panel_selector: str = None):
        self.browser_pool = browser_pool
        self.config = config
        # Пер-сайтовый селектор панели вкладки из профиля (crawl.tab_panel_selector) —
        # добавляется к глобальному списку config.dynamic_tab_panel_selectors.
        self.extra_tab_panel_selector = extra_tab_panel_selector

    async def extract(self, url: str) -> Union[List[str], str]:
        """
        Загружает страницу, определяет динамические элементы и возвращает:
        - список HTML (каждый для одной вкладки), если найдены вкладки,
        - один HTML (после раскрытия всех аккордеонов) в противном случае.
        """
        context = await self.browser_pool.get_browser()
        page = None

        try:
            page = await context.new_page()
            # ИЗМЕНЕНО: wait_until='domcontentloaded', таймаут 20 сек
            await page.goto(url, wait_until='domcontentloaded', timeout=20000)
            # ДОБАВЛЕНО: короткий networkidle (3 сек) – необязателен
            try:
                await page.wait_for_load_state('networkidle', timeout=3000)
            except Exception:
                pass

            # Определяем тип элементов
            if await self._has_tabs(page):
                # Вкладки: кликаем по каждой и собираем HTML
                results = await self._process_tabs(page)
            else:
                # Аккордеоны: кликаем по всем и возвращаем финальный DOM
                await self._process_accordions(page)
                results = await page.content()

            return results
        except Exception as e:
            log.error(f"Ошибка при обработке динамического контента {url}: {e}")
            return []
        finally:
            if page:
                await page.close()
            await self.browser_pool.return_browser(context)

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

    async def _process_tabs(self, page) -> List[str]:
        """Кликает по каждой вкладке и возвращает HTML после каждого клика."""
        # Пытаемся закрыть мешающие диалоги
        try:
            await page.evaluate("""
                document.querySelectorAll('dialog[open]').forEach(d => {
                    if (typeof d.close === 'function') d.close();
                    else d.remove();
                });
                // Отключаем перекрывающие элементы
                document.querySelectorAll('[class*="modal"], [class*="popup"], [role="dialog"]').forEach(el => {
                    el.style.pointerEvents = 'none';
                    el.style.visibility = 'hidden';
                });
            """)
            await page.wait_for_timeout(500)
        except Exception as e:
            log.debug(f"Не удалось закрыть диалоги: {e}")

        results = []
        selectors = self.config.dynamic_tab_selectors

        # Собираем уникальные элементы вкладок
        all_tabs = []
        for sel in selectors:
            tabs = await page.query_selector_all(sel)
            for tab in tabs:
                if tab not in all_tabs:
                    all_tabs.append(tab)

        for idx, tab in enumerate(all_tabs):
            try:
                if not await tab.is_visible():
                    continue

                await tab.click()
                await page.wait_for_timeout(self.config.dynamic_click_delay_ms)

                html = await page.content()
                results.append(html)
                log.info(f"Извлечён контент вкладки {idx+1}")
            except Exception as e:
                log.warning(f"Ошибка при клике на вкладку {idx}: {e}")

        return results

    async def extract_product_tabs_merged(self, url: str) -> Optional[str]:
        """D01: товарная страница с AJAX-вкладками (опт-ин по домену).
        Кликает по каждой вкладке и СКЛЕИВАЕТ контент панелей в ОДИН HTML
        (базовый DOM + <section data-tab="..."> на вкладку). Один HTML → одна
        карточка со всеми характеристиками; отдельные #tab-страницы не создаются
        (иначе дедуп по имени товара терял характеристики — регресс РУФ-135).
        Возвращает склеенный HTML или None (страница не отдалась/вкладок нет)."""
        context = await self.browser_pool.get_browser()
        page = None
        try:
            page = await context.new_page()
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            try:
                await page.wait_for_load_state('networkidle', timeout=5000)
            except Exception:
                pass

            base_html = await page.content()

            # Кандидаты-вкладки: видимые элементы по селекторам вкладок
            tabs = []
            for sel in self.config.dynamic_tab_selectors:
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

            async def visible_panel_html() -> str:
                try:
                    return await page.evaluate(
                        """(sels) => {
                            const els = document.querySelectorAll(sels);
                            for (const el of els) {
                                const r = el.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0 && el.innerText.trim().length > 0)
                                    return el.innerHTML;
                            }
                            return '';
                        }""", panel_selectors)
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
                    await tab.click()
                    await page.wait_for_timeout(self.config.dynamic_click_delay_ms)
                    # страховка: клик мог оказаться навигацией (меню/ссылка) — возвращаемся
                    if page.url != base_page_url:
                        log.debug(f"Клик по элементу {idx} увёл со страницы ({page.url}), возврат")
                        await page.go_back(wait_until='domcontentloaded', timeout=15000)
                        await page.wait_for_timeout(300)
                        continue
                    panel = await visible_panel_html()
                    h = hash(panel)
                    if not panel or h in seen_panels:
                        continue  # клик не дал нового контента (не вкладка/дубль)
                    seen_panels.add(h)
                    sections.append(f'<section data-tab="{label}"><h2>{label}</h2>{panel}</section>')
                    log.info(f"Вкладка товара склеена ({idx+1}: {label!r}): {url}")
                except Exception as e:
                    log.warning(f"Ошибка при клике на вкладку товара {idx}: {e}")

            if not sections:
                return base_html
            merged = base_html
            insert_at = merged.rfind("</body>")
            block = "\n".join(sections)
            if insert_at != -1:
                merged = merged[:insert_at] + block + merged[insert_at:]
            else:
                merged += block
            return merged
        except Exception as e:
            log.error(f"Ошибка при склейке вкладок товара {url}: {e}")
            return None
        finally:
            if page:
                await page.close()
            await self.browser_pool.return_browser(context)

    async def _process_accordions(self, page):
        """Кликает по всем аккордеонам (раскрывашкам)."""
        selectors = self.config.dynamic_accordion_selectors
        for sel in selectors:
            elements = await page.query_selector_all(sel)
            for el in elements:
                try:
                    if await el.is_visible():
                        await el.click()
                        await page.wait_for_timeout(self.config.dynamic_click_delay_ms)
                except Exception:
                    continue

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
            r'/buy/', r'/purchase/', r'/[a-z0-9-]+-\d+[a-z]*/',
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
                
                # Проверяем, является ли поддоменом
                return url_domain.endswith('.' + normalized_base_domain)
            
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
        query_params = parse_qs(parsed.query)
        for param in query_params.keys():
            if (param.startswith('filter_')
                    or param.lower() in ['sort', 'order', 'limit',
                                         # D51: сортировка/вид листинга (Bitrix) — дубли каталожной страницы
                                         'orderby', 'sortby', 'sort_by', 'order_by', 'display', 'set_filter']):
                log.debug(f"Исключаем URL с параметром фильтра: {url}")
                return 'excluded', 0
            
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
            if _p_path.rstrip('/') in [r if r.startswith('/') else '/' + r for r in _roots if r]:
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

        # Проверка на главную страницу
        parsed_url = urlparse(url)
        path = parsed_url.path.strip('/')
        if not path or path in self.main_page_indicators:
            return 'main_page', self.priority_levels['main_page']
        
        # Проверка на контакты
        if any(keyword in path_lower or keyword in text_lower for keyword in self.contact_keywords):
            return 'contacts', self.priority_levels['contacts']
        
        # Проверка на дистрибьюторов
        if any(keyword in path_lower or keyword in text_lower for keyword in self.distributor_keywords):
            return 'distributor', self.priority_levels['distributor']
        
        # Проверка на прайс-листы
        if any(keyword in path_lower or keyword in text_lower for keyword in self.price_list_keywords):
            return 'price_list', self.priority_levels['price_list']
        
        # Проверка на товары (антипаттерн профиля запрещает классификацию «товар»).
        # P04 U2/D228: текст ссылки участвует в товарной оценке.
        if not profile_not_product and self._is_product_url(url, link_text):
            # downgrade (P04 U1, п. 3): URL прошёл мимо exclude-токена только за счёт
            # каталожного сигнала — берём его в обход, но позже настоящих товарных
            return 'product', max(self.priority_levels['product'] - downgrade, 1)

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
        self.params_to_remove = {
            'sessionid', 'sid', 'token', 'csrf', 'auth', 'key',
            'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
            'fbclid', 'gclid', 'msclkid', 'yclid', 'ref', 'source', 'from',
            'redirect', 'cache', 'timestamp', 'time', 'date', 'version',
            'sort', 'order', 'filter', 'view', 'format', 'type',
            # D51 (Полигон/Bitrix): сортировка/вид листинга — та же страница; без фильтра
            # каталожная страница сохранялась 6 раз и сжигала бюджет товарных страниц
            'orderby', 'sortby', 'sort_by', 'order_by', 'display', 'set_filter',
            # D98 (АэроБел/Bitrix): региональный фасет ?region=belgorod|kursk не влияет
            # на контент, но дублирует каждую страницу ×2 и в связке с порчей &reg;→®
            # (парсер декодирует &reg;) плодит бесконечное пространство URL
            # (®ion=...®ion=...), сжигающее весь бюджет краула до товаров. Канонизируем прочь.
            'region',
        }
        
        # Пагинационные параметры с ограничением глубины
        self.pagination_params = {
            'page': 50, 'p': 50, 'paging': 50, 'offset': 100, 'start': 100
        }

        # Кэш для ускорения нормализации - ключ: нормализованный URL
        self._normalization_cache = {}
        # Обратное отображение для быстрого поиска
        self._reverse_cache = {}

    def normalize_url(self, url: str) -> str:
        """Нормализация URL с кэшированием по нормализованному виду"""
        if not url:
            return url

        # D98 (АэроБел): где-то в пайплайне html.unescape декодирует «&region» в URL как
        # legacy-сущность &reg; → «®ion» (®=U+00AE, %C2%AE). Сайт таких URL НЕ отдаёт —
        # это мусор регионального фасета, плодящий бесконечное пространство ссылок
        # (…®ion=belgorod®ion=kursk…) и сжигающий бюджет краула до товаров. ® может осесть
        # в пути, в имени или в значении параметра — срезаем URL от первого ® (в любой
        # кодировке) до конца: все варианты схлопываются к чистому префиксу и дедупятся.
        # Остаточный «чистый» ?region= убирается ниже через params_to_remove.
        url = re.split(r'®|%[Cc]2%[Aa][Ee]', url, maxsplit=1)[0].rstrip('?&')

        try:
            original_parsed = urlparse(url)
            original_scheme = original_parsed.scheme

            if (self.url_categorizer and 
                self.url_categorizer.domain_equivalency and
                self.url_categorizer.config and
                self.url_categorizer.config.domain_equivalency_enabled):
                
                canonical_url = self.url_categorizer.domain_equivalency.get_canonical_url(url)
                parsed = urlparse(canonical_url)
                
                # ВАЖНО: Сохраняем оригинальную схему, если она была
                if original_scheme:
                    parsed = parsed._replace(scheme=original_scheme)
                # Если не было схемы, оставляем ту, что в каноническом URL
            else:
                # стандартная нормализация
                parsed = original_parsed
            
            # Базовая нормализация
            parsed = parsed._replace(fragment="")
            path = parsed.path
            # D41: русский языковой префикс — та же страница под двумя URL
            # (/ru/katalog/... и /katalog/...); без нормализации обе версии
            # обходятся и дают ДУБЛЬ товара (LLM даёт чуть разные имена → разные id).
            # Срезаем ведущий сегмент /ru/ до единого вида (только 'ru' — прочие
            # языки отсекает языковой фильтр категоризатора).
            if path:
                path = re.sub(r'^/ru(?=/|$)', '', path, flags=re.I)
            if path:
                # Проверяем, заканчивается ли путь на число
                if re.search(r'/\d+/?$', path):
                    # Если URL уже имеет слеш - сохраняем его
                    if path.endswith('/'):
                        path = path  # оставляем как есть
                    else:
                        path = path + '/'  # добавляем слеш
                else:
                    # Для остальных URL - стандартная обработка
                    path = path.rstrip('/')
            
            if not path:
                path = '/'
            
            # Обрабатываем параметры запроса
            query_params = parse_qs(parsed.query, keep_blank_values=True)
            filtered_params = {}
            
            for key, values in query_params.items():
                key_lower = key.lower()

                # Удаляем трекерные параметры
                if key_lower in self.params_to_remove:
                    continue
                    
                # Обрабатываем пагинацию
                if key_lower in self.pagination_params:
                    if values and values[0].isdigit():
                        page_num = int(values[0])
                        max_pages = self.pagination_params[key_lower]
                        if page_num <= max_pages:
                            filtered_params[key] = values
                    continue
                
                filtered_params[key] = values
            
            # Сортируем параметры
            sorted_params = sorted(filtered_params.items())
            normalized_query = urlencode(sorted_params, doseq=True)
            
            # Собираем URL обратно
            normalized_url = urlunparse((
                parsed.scheme,
                parsed.netloc.lower(),
                path,
                parsed.params,
                normalized_query,
                ""
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
    
    def extract_canonical_url(self, soup: BeautifulSoup, current_url: str) -> str:
        """Извлечение canonical URL из HTML мета-тегов"""
        try:
            # Ищем canonical link
            canonical_link = soup.find('link', rel='canonical')
            if canonical_link and canonical_link.get('href'):
                canonical_url = canonical_link['href']
                # Если URL относительный, преобразуем в абсолютный
                if not canonical_url.startswith(('http://', 'https://')):
                    canonical_url = urljoin(current_url, canonical_url)
                return self.normalize_url(canonical_url)
            
            # Ищем og:url
            og_url = soup.find('meta', property='og:url')
            if og_url and og_url.get('content'):
                og_url_content = og_url['content']
                if not og_url_content.startswith(('http://', 'https://')):
                    og_url_content = urljoin(current_url, og_url_content)
                return self.normalize_url(og_url_content)
            
            # Возвращаем нормализованный текущий URL как fallback
            return self.normalize_url(current_url)
            
        except Exception as e:
            log.warning(f"Ошибка извлечения canonical URL: {e}")
            return self.normalize_url(current_url)

class SiteMapParser:
    """Парсер карт сайта для извлечения товарных URL"""
    
    def __init__(self, config: Config, url_categorizer: URLCategorizer, url_normalizer: SmartURLNormalizer):
        self.config = config
        self.url_categorizer = url_categorizer
        self.url_normalizer = url_normalizer
        
        
        # Форматы карт сайта
        self.sitemap_paths = [
            '/sitemap.xml', '/sitemap/index.xml', '/sitemap_index.xml', '/sitemap',
            '/sitemap.txt', '/sitemap.html', '/site-map.html', '/sitemap/xml/', '/xml/sitemap.xml', '/sitemap1.xml'
            '/rss.xml', '/feed.xml', '/atom.xml'
        ]
        
        # Кэш для избежания повторной обработки
        self._processed_sitemaps = set()
        
    async def discover_sitemap_urls(self, base_url: str) -> List[str]:
        """Обнаружение карт сайта по стандартным путям и через robots.txt"""
        sitemap_urls = []
        parsed_base = urlparse(base_url)
        
        # 1. Проверка стандартных путей
        for path in self.sitemap_paths:
            sitemap_url = f"{parsed_base.scheme}://{parsed_base.netloc}{path}"
            if await self._check_sitemap_exists(sitemap_url):
                sitemap_urls.append(sitemap_url)
        
        # 2. Извлечение из robots.txt
        robots_url = f"{parsed_base.scheme}://{parsed_base.netloc}/robots.txt"
        robots_sitemaps = await self._extract_sitemaps_from_robots(robots_url)
        sitemap_urls.extend(robots_sitemaps)
        
        # 3. Поиск ссылок на карту сайта на главной странице
        html_sitemaps = await self._find_sitemap_links_in_html(base_url)
        sitemap_urls.extend(html_sitemaps)
        
        # Удаление дубликатов
        return list(set(sitemap_urls))
    
    async def _check_sitemap_exists(self, url: str) -> bool:
        """Проверка существования карты сайта"""
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.head(url, timeout=10) as response:
                    return response.status == 200
        except:
            return False
    
    async def _extract_sitemaps_from_robots(self, robots_url: str) -> List[str]:
        """Извлечение карт сайта из robots.txt"""
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(robots_url, timeout=10) as response:
                    if response.status == 200:
                        content = await response.text()
                        sitemaps = []
                        for line in content.split('\n'):
                            line = line.strip()
                            if line.lower().startswith('sitemap:'):
                                sitemap_url = line.split(':', 1)[1].strip()
                                sitemaps.append(sitemap_url)
                        return sitemaps
        except:
            pass
        return []
    
    async def _find_sitemap_links_in_html(self, base_url: str) -> List[str]:
        """Поиск ссылок на карту сайта в HTML главной страницы"""
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(base_url, timeout=10) as response:
                    if response.status == 200:
                        html = await response.text()
                        soup = BeautifulSoup(html, 'html.parser')
                        
                        sitemap_links = []
                        sitemap_keywords = ['карта сайта', 'sitemap', 'site map', 'карта-сайта']
                        
                        for link in soup.find_all('a', href=True):
                            link_text = link.get_text().lower()
                            href = link['href']
                            
                            # Проверка по тексту ссылки
                            if any(keyword in link_text for keyword in sitemap_keywords):
                                full_url = urljoin(base_url, href)
                                sitemap_links.append(full_url)
                        
                        return sitemap_links
        except:
            pass
        return []
    
    async def parse_sitemap(self, sitemap_url: str, depth: int = 0, max_depth: int = 3) -> List[Tuple[str, str, int]]:
        """Рекурсивный парсинг карты сайта с ограничением глубины"""
        if depth > max_depth or sitemap_url in self._processed_sitemaps:
            return []
            
        self._processed_sitemaps.add(sitemap_url)
        log.info(f"Парсинг карты сайта: {sitemap_url} (глубина: {depth})")
        
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(sitemap_url, timeout=10) as response:
                    if response.status != 200:
                        return []
                    
                    content = await response.text()
                    
                    # Определение типа карты сайта по content-type или расширению
                    content_type = response.headers.get('content-type', '').lower()
                    
                    if 'xml' in content_type or sitemap_url.endswith('.xml') or '/sitemap' in sitemap_url.lower():
                        return await self._parse_xml_sitemap(content, sitemap_url, depth, max_depth)
                    elif 'html' in content_type or any(ext in sitemap_url for ext in ['.html', '.htm']):
                        return await self._parse_html_sitemap(content, sitemap_url)
                    else:
                        if content.strip().startswith('<?xml') or '<urlset' in content or '<sitemapindex' in content:
                            return await self._parse_xml_sitemap(content, sitemap_url, depth, max_depth)
                        else:
                            return await self._parse_text_sitemap(content, sitemap_url)
                        
        except asyncio.TimeoutError:
            log.warning(f"Таймаут при парсинге карты сайта: {sitemap_url}")
        except Exception as e:
            log.error(f"Ошибка парсинга карты сайта {sitemap_url}: {e}")
        
        return []
    
    async def _parse_xml_sitemap(self, content: str, sitemap_url: str, depth: int, max_depth: int) -> List[Tuple[str, str, int]]:
        """Парсинг XML карты сайта (включая sitemap index)"""
        try:
            soup = BeautifulSoup(content, 'xml')
            all_urls = []
            
            # Проверка на sitemap index
            sitemap_tags = soup.find_all('sitemap')
            if sitemap_tags:
                # Это индексный файл - парсим вложенные карты сайта
                tasks = []
                for sitemap_tag in sitemap_tags[:5]:  # Ограничиваем количество вложенных
                    loc_tag = sitemap_tag.find('loc')
                    if loc_tag and loc_tag.text:
                        task = self.parse_sitemap(loc_tag.text, depth + 1, max_depth)
                        tasks.append(task)
                
                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for result in results:
                        if isinstance(result, list):
                            all_urls.extend(result)
                return all_urls
            
            # Обычная карта сайта с URL
            url_tags = soup.find_all('url')
            for url_tag in url_tags[:self.config.sitemap_max_urls]:
                loc_tag = url_tag.find('loc')
                if loc_tag and loc_tag.text:
                    url = loc_tag.text.strip()
                    normalized_url = self.url_normalizer.normalize_url(url)

                    # Пропускаем URL с не-русскими языковыми префиксами
                    if self.url_categorizer.should_exclude_by_language(url):
                        continue

                    category, priority = self.url_categorizer.categorize_url(url)
                    
                    # Повышаем приоритет для URL из карты сайта
                    enhanced_priority = min(priority + 1, 10)
                    
                    all_urls.append((normalized_url, category, enhanced_priority))
            
            log.info(f"Извлечено {len(all_urls)} URL из XML карты сайта")
            return all_urls
            
        except Exception as e:
            log.error(f"Ошибка парсинга XML карты сайта: {e}")
            return []
    
    async def _parse_html_sitemap(self, content: str, sitemap_url: str) -> List[Tuple[str, str, int]]:
        """Парсинг HTML карты сайта"""
        try:
            soup = BeautifulSoup(content, 'html.parser')
            all_urls = []
            base_domain = urlparse(sitemap_url).scheme + "://" + urlparse(sitemap_url).netloc
            
            # Ищем все ссылки в HTML
            for link in soup.find_all('a', href=True)[:self.config.sitemap_max_urls]:
                href = link['href']
                if href and not href.startswith(('#', 'javascript:', 'mailto:')):
                    full_url = urljoin(base_domain, href)

                    if self.url_categorizer.should_exclude_by_language(full_url):
                        continue

                    normalized_url = self.url_normalizer.normalize_url(full_url)
                    
                    category, priority = self.url_categorizer.categorize_url(full_url)
                    enhanced_priority = min(priority + 1, 10)
                    
                    all_urls.append((normalized_url, category, enhanced_priority))
            
            log.info(f"Извлечено {len(all_urls)} URL из HTML карты сайта")
            return all_urls
            
        except Exception as e:
            log.error(f"Ошибка парсинга HTML карты сайта: {e}")
            return []
    
    async def _parse_text_sitemap(self, content: str, sitemap_url: str) -> List[Tuple[str, str, int]]:
        """Парсинг текстовой карты сайта"""
        try:
            all_urls = []
            base_domain = urlparse(sitemap_url).scheme + "://" + urlparse(sitemap_url).netloc
            
            for line in content.split('\n')[:self.config.sitemap_max_urls]:
                line = line.strip()
                if line and not line.startswith('#') and self._is_valid_url(line):
                    full_url = urljoin(base_domain, line) if not line.startswith('http') else line

                    # Пропускаем URL с не-русскими языковыми префиксами
                    if self.url_categorizer.should_exclude_by_language(full_url):
                        continue
                    
                    normalized_url = self.url_normalizer.normalize_url(full_url)
                    
                    category, priority = self.url_categorizer.categorize_url(full_url)
                    enhanced_priority = min(priority + 1, 10)
                    
                    all_urls.append((normalized_url, category, enhanced_priority))
            
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
            for selector in self.product_grid_selectors:
                try:
                    grid_elements = soup.select(selector)
                    for element in grid_elements:
                        links = self._extract_links_from_element(element, base_url, current_url)
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
        
        # Ищем все ссылки внутри элемента сетки
        for link in element.find_all('a', href=True):
            href = link['href']
            if not href or href.startswith(('#', 'javascript:')):
                continue
                
            full_url = urljoin(current_url, href)
            
            # Проверяем, является ли ссылка товарной
            category, priority = self.url_categorizer.categorize_url(full_url)
            if category == 'product':
                links.append((full_url, category, priority))
        
        return links
    
class FileDownloadManager:
    """Менеджер для скачивания файлов с обработкой пагинации"""
    
    def __init__(self, crawler, config: Config):
        self.crawler = crawler
        self.config = config
        self.processed_tabs = set()
        self.downloaded_files_cache = set()
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
        return self.crawler.url_categorizer._is_same_domain(url, base_domain)

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
                
                async with session.head(image_url, allow_redirects=True) as response:
                    if response.status == 200:
                        # Пробуем получить размер из Content-Length
                        content_length = response.headers.get('Content-Length')
                        if content_length and content_length.isdigit():
                            return int(content_length)
                        
                        # Если размер не указан в заголовках, получаем небольшой кусок для анализа
                        try:
                            # Делаем GET запрос с ограниченным размером
                            async with session.get(image_url, headers={'Range': 'bytes=0-1024'}) as partial_response:
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
        Не проверяет расширение, не фильтрует URL, не блокирует скачивание.
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
                            async with session.head(next_url, timeout=5) as response:
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
                    self.crawler.url_categorizer.is_main_domain(file_url, base_url) and  # <-- замена
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
            full_url = urljoin(page_url, a['href'].strip()).split('#', 1)[0]
            if not full_url or full_url in seen:
                continue
            seen.add(full_url)
            path = urlparse(full_url).path.lower()
            if any(path.endswith(ext) for ext in self.supported_extensions):
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
        async with self.download_semaphore:
            await self._file_throttle(file_url)
            return await self._download_aiohttp_unlimited(file_url, save_dir, page_category)

    async def _download_aiohttp_unlimited(self, file_url: str, save_dir: str, page_category: str = None) -> Optional[str]:
        """Универсальное скачивание файлов через aiohttp (из Fallback)"""
        try:
            _ptt_t0 = time.perf_counter()
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
                
                async with session.get(file_url, allow_redirects=True) as response:
                    if response.status == 200:
                        # Получаем имя файла
                        file_name = self._extract_filename_from_response(file_url, response.headers)
                        if not file_name:
                            log.warning(f"Не удалось извлечь имя файла для {file_url}")
                            return None
                        
                        # Создаем полный путь
                        file_path = os.path.join(save_dir, file_name)
                        
                        # Проверяем, не скачан ли файл уже
                        if os.path.exists(file_path):
                            log.info(f"Файл уже существует: {file_path}")
                            return file_path
                        
                        # Скачиваем контент
                        content = await response.read()
                        
                        # Проверяем размер файла
                        file_size_mb = len(content) / 1024 / 1024
                        if file_size_mb > self.config.max_file_size_mb:
                            log.warning(f"Файл {file_name} превышает максимальный размер: {file_size_mb:.2f} MB")
                            return None
                        
                        # Сохраняем файл
                        async with aiofiles.open(file_path, 'wb') as f:
                            await f.write(content)
                        
                        log.info(f"Файл успешно скачан: {file_name} ({file_size_mb:.2f} MB)")
                        _ptt = getattr(self.crawler, 'processing_tracker', None)
                        if _ptt:
                            _ptt.add_clean('files', time.perf_counter() - _ptt_t0)
                        return file_path
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
            max_size_mb=config.temp_storage_max_size_mb)
        self.file_download_manager = FileDownloadManager(self, config)
        self.sitemap_parser = SiteMapParser(config, self.url_categorizer, self.url_normalizer)
        self.permanent_errors_cache = self._load_permanent_errors_cache()
        self._errors_cache_lock = asyncio.Lock()
        
        # Статистика
        self.stats = {
            'total_pages': 0,
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
        # Кросс-доменная/путь-склейка (C) вынесена на стадию УСПЕШНОГО извлечения в main.py
        # (_product_dedup_key), чтобы провалившаяся версия (soft-404 на осн. домене холдинга)
        # НЕ вытесняла рабочую версию со вторичного домена. См. _scheme_dedup_key.
        self._crawled_scheme_keys = set()
        # D36: детект протухшего sitemap (soft-404 оболочки). Если N товарных страниц
        # дали одинаковый видимый текст — sitemap отдаёт заглушки; вычищаем его хвост
        # из очереди, чтобы не сжечь бюджет max_product_pages_per_site впустую.
        self._product_text_hash_counts = {}
        self._stale_sitemap_guard_tripped = False
        self.stale_sitemap_dup_threshold = getattr(self.config, 'stale_sitemap_dup_threshold', 10)
        # D40: ретраи страниц, не отдавших контент (таймауты медленных сайтов):
        # {normalized_url: (url, depth, category, attempts)}
        self._page_retry_candidates = {}
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

    async def _check_stale_sitemap_guard(self, html: str, url: str, queue: deque) -> None:
        """D36: считает одинаковые сигнатуры текста товарных страниц; при достижении
        порога вычищает из очереди необойдённые товарные URL карты сайта (depth=0)
        и освобождает их слоты в product_urls (бюджет max_product_pages_per_site)."""
        if self._stale_sitemap_guard_tripped:
            return
        sig = self._soft404_text_signature(html)
        n = self._product_text_hash_counts.get(sig, 0) + 1
        self._product_text_hash_counts[sig] = n
        if n < self.stale_sitemap_dup_threshold:
            return
        self._stale_sitemap_guard_tripped = True
        purged = 0
        async with self._queue_lock:
            kept = deque()
            while queue:
                item = queue.popleft()
                q_url, q_depth, q_category = item[0], item[1], item[2]
                if q_category == 'product' and q_depth == 0:
                    norm = self.url_normalizer.normalize_url(q_url)
                    async with self._urls_lock:
                        self.product_urls.discard(norm)
                        self.visited_urls.discard(norm)
                    purged += 1
                else:
                    kept.append(item)
            queue.extend(kept)
        log.warning(f"Протухший sitemap: {n} товарных страниц с одинаковым текстом "
                    f"(последняя: {url}). Из очереди убрано {purged} товарных URL карты сайта, "
                    f"бюджет освобождён для ссылок из каталога")

    def _register_page_retry(self, url: str, depth: int, category: str) -> None:
        """D40: запоминает страницу, не отдавшую контент, для повторной попытки.
        Ретраим только целевые категории — терять их значит терять товары/дилеров."""
        if category not in ('product', 'contacts', 'distributor', 'main_page'):
            return
        norm = self.url_normalizer.normalize_url(url)
        cur = self._page_retry_candidates.get(norm)
        attempts = cur[3] + 1 if cur else 1
        if attempts > self.page_retry_max_attempts:
            return
        self._page_retry_candidates[norm] = (url, depth, category, attempts)

    async def reset_state(self, company_name: str):
        """Сброс состояния и загрузка обработанных URL из временного хранилища для компании"""
        async with self._urls_lock:
            self.visited_urls.clear()
            self.processed_urls.clear()
            self.product_urls.clear()
            self._crawled_scheme_keys.clear()
            self._product_text_hash_counts.clear()
            self._stale_sitemap_guard_tripped = False
            self._page_retry_candidates.clear()

        # Получаем все файлы компании из временного хранилища
        company_files = await self.temp_storage.get_company_files(company_name)
        async with self._urls_lock:
            for file_info in company_files:
                metadata = file_info['metadata']
                # Если страница обработана AI, добавляем ее URL в посещенные и обработанные
                if metadata.get('checkpoints', {}).get('ai_processed', False):
                    normalized_url = metadata.get('normalized_url')
                    if normalized_url:
                        self.visited_urls.add(normalized_url)
                        self.processed_urls.add(normalized_url)
                        # D59 RC1: восстанавливаем схемный ключ, чтобы на возобновлении
                        # не перекачать http/https-двойник уже обработанной страницы.
                        if self.url_categorizer.categorize_url(normalized_url)[0] == 'product':
                            skey = self._scheme_dedup_key(normalized_url)
                            if skey is not None:
                                self._crawled_scheme_keys.add(skey)

        log.info(f"Загружено {len(self.visited_urls)} обработанных URL для компании {company_name}")

    async def _reset_stats(self):
        """Сброс статистики (без очистки visited_urls, processed_urls, product_urls)"""
        async with self._stats_lock:
            self.stats = {
                'total_pages': 0,
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

            async with self._urls_lock:
                self.visited_urls.clear()
                self.processed_urls.clear()
                self.product_urls.clear()
                self._crawled_scheme_keys.clear()

            # Дополнительная очистка если память все еще высокая
            if read_memory_usage_mb() > self.config.memory_cleanup_threshold_mb:
                self.permanent_errors_cache.clear()
                gc.collect()

            log.info(f"Очистка памяти завершена. Текущее потребление: {read_memory_usage_mb():.2f}MB")

        except Exception as e:
            log.warning(f"Ошибка проверки памяти: {e}")

    def _load_permanent_errors_cache(self) -> set:
        """Загрузка кэша перманентных ошибок из файла"""
        cache_file = os.path.join(self.config.base_dir, "permanent_errors_cache.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    return set(json.load(f))
            except Exception as e:
                log.warning(f"Ошибка загрузки кэша ошибок: {e}")
        return set()

    def _save_permanent_errors_cache(self):
        """Сохранение кэша перманентных ошибок в файл"""
        cache_file = os.path.join(self.config.base_dir, "permanent_errors_cache.json")
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(list(self.permanent_errors_cache), f, ensure_ascii=False)
        except Exception as e:
            log.warning(f"Ошибка сохранения кэша ошибок: {e}")

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
        
        # Определяем рабочий URL с учетом эквивалентности доменов
        working_url = await self._get_working_url(site_url)

        # Сохраняем базовый URL для проверки поддоменов
        self.current_base_url = working_url

        # Профиль сайта: пер-сайтовые лимиты/стратегии (mvp/profiles/<домен>.yaml).
        self._apply_site_profile(working_url)

        # P04 U1 (D211, D260): базовая локаль обхода — языковой префикс рабочего URL
        # (или URL из Site_list). У зарубежного производителя национальная версия и
        # есть весь сайт, языковым фильтром её резать нельзя.
        self.url_categorizer.set_base_locale(
            self.url_categorizer.detect_locale_prefix(working_url)
            or self.url_categorizer.detect_locale_prefix(site_url))

        if self.profile is not None and self.profile.crawl.antibot == 'blocked':
            log.warning(f"Профиль {self.profile.domain}: antibot=blocked — сайт не "
                        f"обрабатывается (ждёт решения в REVIEW)")
            return {'error': 'antibot=blocked (профиль сайта)', 'stored_pages': [],
                    'original_url': site_url, 'working_url': working_url}

        # Сбрасываем статистику для нового сайта
        await self._reset_stats()
        
        stored_pages = []  # Список сохраненных страниц
        
        try:
            domain_dirs = self.create_site_directories(site_url, company_name, company_dir)
            await self._start_crawling_with_sitemap_support(working_url, company_name, domain_dirs, stored_pages)                       
            self._save_permanent_errors_cache()

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
            self.stats['failed_pages'] += 1
            return {'error': str(e), 'stored_pages': stored_pages, 'original_url': site_url, 'working_url': working_url}
    
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
        if not self.config.domain_equivalency_enabled or not self.config.treat_http_https_as_same:
            return url
            
        try:
            # Используем DomainEquivalencyManager для поиска рабочего URL
            working_url = await self.url_categorizer.domain_equivalency.find_working_url(url)
            
            if working_url != url:
                log.info(f"Используется рабочий URL: {working_url} вместо {url}")
            
            return working_url
        except Exception as e:
            log.warning(f"Ошибка поиска рабочего URL для {url}: {e}")
            return url
    
    async def _start_crawling_with_storage(self, site_url: str, company_name: str, 
                                         domain_dirs: Dict[str, str], stored_pages: List[Dict]):
        """Запуск процесса краулинга с сохранением в временное хранилище"""              

        queue = deque()
        start_urls = self._get_start_urls(site_url)
        
        for url in start_urls:
            category, priority = self.url_categorizer.categorize_url(url)
            queue.append((url, 0, category, priority))
            self.visited_urls.add(self.url_normalizer.normalize_url(url))
        
        # Запускаем обработку очереди
        while queue and self.stats['total_pages'] < self.max_pages_per_site:
            url, depth, category, priority = queue.popleft()
            
            if await self._should_skip_url(url, depth, company_name):
                continue
                
            try:
                # Обрабатываем страницу и сохраняем в хранилище
                stored_page = await self._process_page_with_storage(url, depth, category, company_name, domain_dirs, queue)
                if stored_page:
                    stored_pages.append(stored_page)
                    
            except Exception as e:
                log.error(f"Ошибка обработки страницы {url}: {e}")
                self.stats['failed_pages'] += 1
                continue
    
    async def _start_crawling_with_sitemap_support(self, site_url: str, company_name: str, 
                                         domain_dirs: Dict[str, str], stored_pages: List[Dict]):
        """Запуск процесса краулинга с поддержкой карты сайта"""
        queue = deque()        
        
        sitemap_urls = await self._discover_and_parse_sitemap(site_url)
        if sitemap_urls:
            await self._add_sitemap_urls_to_queue(queue, sitemap_urls, site_url)
            log.info(f"Добавлено {len(sitemap_urls)} URL из карты сайта")
        
        # 2. Добавляем стандартные стартовые URL
        start_urls = self._get_start_urls(site_url)
        added_standard_urls = 0
        for url in start_urls:
            normalized_url = self.url_normalizer.normalize_url(url)
            
            async with self._urls_lock:
                if normalized_url not in self.visited_urls:
                    category, priority = self.url_categorizer.categorize_url(url)
                    adjusted_priority = max(priority - 2, 1)
                    queue.append((url, 0, category, adjusted_priority))
                    self.visited_urls.add(normalized_url)
                    added_standard_urls += 1
        
        log.info(f"Добавлено {added_standard_urls} стандартных URL, общий размер очереди: {len(queue)}")
        
        # 3. Параллельная обработка очереди
        processed_count = 0
        active_tasks = set()
        
        while ((queue or active_tasks) or self._apply_exclude_failopen(queue)) \
                and await self._get_total_pages() < self.max_pages_per_site:
            # Сортируем очередь по приоритету
            async with self._queue_lock:
                queue_list = list(queue)
                queue_list.sort(key=lambda x: x[3], reverse=True)
                queue.clear()
                queue.extend(queue_list)
            
            # Создаем задачи для параллельной обработки
            async with self._queue_lock:
                batch_size = min(self._effective_max_concurrent_pages - len(active_tasks), len(queue))
                if batch_size > 0:
                    for _ in range(batch_size):
                        if queue:
                            url, depth, category, priority = queue.popleft()
                            task = asyncio.create_task(
                                self._process_page_parallel(url, depth, category, company_name, domain_dirs, queue, stored_pages)
                            )
                            active_tasks.add(task)
                            task.add_done_callback(active_tasks.discard)
            
            # Ожидаем завершения некоторых задач
            if active_tasks:
                await asyncio.sleep(0.1)  # Короткая пауза для управления нагрузкой
            
            # Проверка памяти каждые 10 страниц
            if processed_count % 10 == 0:
                await self._check_memory_usage()
        
        # Ожидаем завершения всех оставшихся задач
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)

        # D40: повторный проход по целевым страницам, не отдавшим контент (таймауты
        # медленных сайтов). Без ретрая каждый прогон терял случайное подмножество товаров.
        retry_round = 0
        while (self._page_retry_candidates and retry_round < self.page_retry_max_attempts
               and await self._get_total_pages() < self.max_pages_per_site):
            retry_round += 1
            batch = list(self._page_retry_candidates.values())
            self._page_retry_candidates.clear()
            log.info(f"Повторная попытка для {len(batch)} страниц без контента (раунд {retry_round})")
            for url, depth, category, _ in batch:
                if await self._get_total_pages() >= self.max_pages_per_site:
                    break
                try:
                    with (self.processing_tracker.retry_span('crawler') if self.processing_tracker else nullcontext()):
                        stored_page = await self._process_page_with_storage(
                            url, depth, category, company_name, domain_dirs, queue)
                    if stored_page:
                        stored_pages.append(stored_page)
                        if self.page_sink is not None:
                            await self.page_sink.put(stored_page)
                except Exception as e:
                    log.error(f"Ошибка повторной обработки страницы {url}: {e}")

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
            if category.startswith('excluded'):
                continue
            queue.append((url, 1, category, priority))
            self.visited_urls.add(normalized_url)
            restored += 1
        log.warning(f"Предохранитель фильтров: в обход возвращено {restored} отложенных URL")
        return restored > 0

    async def _process_page_parallel(self, url: str, depth: int, category: str, company_name: str,
                                   domain_dirs: Dict[str, str], queue: deque, stored_pages: List[Dict]):
        """Параллельная обработка отдельной страницы"""
        if await self._should_skip_url(url, depth, company_name):
            return
        
        try:
            with (self.processing_tracker.clean_span('crawler') if self.processing_tracker else nullcontext()):
                stored_page = await self._process_page_with_storage(url, depth, category, company_name, domain_dirs, queue)
            if stored_page:
                stored_pages.append(stored_page)
                if self.page_sink is not None:
                    await self.page_sink.put(stored_page)

        except Exception as e:
            log.error(f"Ошибка обработки страницы {url}: {e}")
            async with self._stats_lock:
                self.stats['failed_pages'] += 1

    async def _get_total_pages(self) -> int:
        """Потокобезопасное получение общего количества страниц"""
        async with self._stats_lock:
            return self.stats['total_pages']

    async def _discover_and_parse_sitemap(self, site_url: str) -> List[Tuple[str, str, int]]:
        """Обнаружение и парсинг карты сайта"""
        if not self.config.sitemap_discovery_enabled:
            return []
        
        try:
            # Обнаружение карт сайта
            sitemap_urls = await self.sitemap_parser.discover_sitemap_urls(site_url)
            log.info(f"Найдено карт сайта: {len(sitemap_urls)}")
            
            if not sitemap_urls:
                return []
            
            # Параллельный парсинг всех найденных карт сайта
            tasks = []
            for sitemap_url in sitemap_urls:
                task = self.sitemap_parser.parse_sitemap(sitemap_url, max_depth=self.config.sitemap_max_depth)
                tasks.append(task)
            
            # Ограничиваем время выполнения
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=30.0
            )
            
            # Собираем все URL
            all_urls = []
            for result in results:
                if isinstance(result, list):
                    all_urls.extend(result)
            
            # Фильтруем только товарные URL и категории
            filtered_urls = []
            for url, category, priority in all_urls:
                if category in ['product', 'category']:
                    filtered_urls.append((url, category, priority))
                elif priority >= 7:  # Высокоприоритетные страницы
                    filtered_urls.append((url, category, priority))
            
            # Ограничиваем общее количество (профиль сайта может задать свой лимит)
            _sitemap_cap = self.config.sitemap_max_urls
            if self.profile is not None and self.profile.crawl.limits.sitemap_urls:
                _sitemap_cap = self.profile.crawl.limits.sitemap_urls
            filtered_urls = filtered_urls[:_sitemap_cap]

            log.info(f"Отфильтровано {len(filtered_urls)} URL из карты сайта")
            if self.metrics_collector is not None:
                self.metrics_collector.record_sitemap(len(filtered_urls))
            return filtered_urls
            
        except asyncio.TimeoutError:
            log.warning("Таймаут при парсинге карты сайта")
        except Exception as e:
            log.error(f"Ошибка при работе с картой сайта: {e}")
        
        return []
    
    async def _add_sitemap_urls_to_queue(self, queue: deque, sitemap_urls: List[Tuple[str, str, int]], base_url: str):
        """Добавление URL из карты сайта в очередь краулинга"""
        added_count = 0
        skipped_count = 0
        skipped_subdomains = 0
        
        # Сортируем URL по приоритету (от высокого к низкому)
        sitemap_urls.sort(key=lambda x: x[2], reverse=True)

        for url, category, priority in sitemap_urls:
            normalized_url = self.url_normalizer.normalize_url(url)
            
            # Проверяем основной домен
            if self.config.ignore_subdomains:
                if self.url_categorizer.is_subdomain(url, base_url):
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

            # Проверяем дублирование
            if normalized_url in self.visited_urls:
                skipped_count += 1
                continue

            # Лимит продуктовых страниц действует и на URL из карты сайта: иначе
            # sitemap кладёт сотни товарных URL в очередь в обход
            # max_product_pages_per_site. Продукты в карте отсортированы первыми по
            # приоритету, поэтому лишние просто пропускаем (контакты/категории идут дальше).
            if category == 'product' and len(self.product_urls) >= self.max_product_pages_per_site:
                skipped_count += 1
                continue

            # D59 RC1: схемный дубль http↔https из sitemap (общий набор ключей с гейтом ссылок).
            if category == 'product':
                skey = self._scheme_dedup_key(url)
                if skey is not None:
                    if skey in self._crawled_scheme_keys:
                        log.info(f"D59 RC1: схемный дубль из sitemap (ключ {skey}): {url}")
                        skipped_count += 1
                        continue
                    self._crawled_scheme_keys.add(skey)

            url_depth = self.url_categorizer.calculate_url_depth(url)
            # Увеличиваем приоритет URL из карты сайта
            enhanced_priority = min(priority + 3, 10)

            # Добавляем в начало очереди
            queue.appendleft((url, 0, category, enhanced_priority))
            self.visited_urls.add(normalized_url)
            if category == 'product':
                self.product_urls.add(normalized_url)
            added_count += 1
            
            # Ограничиваем количество для первого прохода
            if added_count >= self.config.sitemap_initial_batch_size:
                break
        
        log.info(f"Добавлено {added_count} URL из карты сайта в начало очереди")
        if skipped_count > 0:
            log.info(f"Пропущено {skipped_count} дубликатов")
        if skipped_subdomains > 0:
            log.info(f"Пропущено {skipped_subdomains} поддоменов из карты сайта")
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

        async with self._urls_lock:
            self.processed_urls.add(normalized_url)

        async with self._stats_lock:
            self.stats['total_pages'] += 1

        # Мониторинг памяти
        self._pages_processed_since_last_check += 1
        if (self._pages_processed_since_last_check >= self.config.memory_check_interval_pages or
                time.time() - self._last_memory_check > 300):
            await self._check_memory_usage()
            self._pages_processed_since_last_check = 0
            self._last_memory_check = time.time()

        # Проверка, не была ли страница уже обработана ранее (не блокирует извлечение ссылок)
        is_processed = await self.temp_storage.is_page_processed(normalized_url, company_name, category)
        if is_processed:
            log.info(f"Страница уже обработана ранее, пропускаем сохранение, но ссылки всё равно извлечём: {url}")

        try:
            # 1. Получение контента страницы (может вернуть один ParseResult или список)
            parse_result = await self._fetch_page_content(url, category)

            # Обработка случая, когда страница вернула список вкладок
            if isinstance(parse_result, list):
                for idx, single_result in enumerate(parse_result):
                    tab_url = f"{url}#tab_{idx}"
                    tab_normalized = self.url_normalizer.normalize_url(tab_url)
                    if single_result:
                        # Извлекаем ссылки из каждой вкладки
                        await self._process_links_from_parse_result(tab_url, single_result, depth, queue)
                    # Если категория подходит для сохранения - сохраняем вкладку
                    if category in ('product', 'contacts', 'distributor', 'main_page'):
                        await self._save_single_page(
                            url, tab_normalized, single_result, depth, category,
                            company_name, domain_dirs, stored_pages, queue,
                            redirected_from=url
                        )
                return None

            if not parse_result:
                log.warning(f"Не удалось получить содержимое страницы: {url}")
                # D40: таймаутнутые целевые страницы не теряем — кладём в очередь ретраев,
                # она обходится повторно после основного прохода
                self._register_page_retry(url, depth, category)
                return None

            # 2. ИЗВЛЕЧЕНИЕ ССЫЛОК (ВСЕГДА, независимо от категории и is_processed)
            await self._process_links_from_parse_result(url, parse_result, depth, queue)

            # 2а. ОБЩИЕ ДОКУМЕНТЫ КОМПАНИИ: если страница принадлежит одному из
            # 4 блоков профиля (certificates/documents/instructions/price_list) —
            # скачиваем файлы с неё в соответствующую папку компании
            doc_section = self.url_categorizer.profile_document_section(url)
            if doc_section:
                await self._download_company_documents(url, parse_result.html, doc_section, domain_dirs)

            # 3. СПЕЦИАЛЬНАЯ ОБРАБОТКА ДЛЯ ДОКУМЕНТАЦИИ (скачивание файлов)
            if category == 'documentation':
                log.info(f"Обработка страницы документации (скачивание файлов): {url}")
                try:
                    extractor = self._make_dynamic_extractor()
                    result = await extractor.extract(url)
                    if result:
                        await self._download_files_from_documentation_page(url, result, domain_dirs, company_name)
                    else:
                        log.warning(f"Не удалось получить контент для документации {url}")
                except Exception as e:
                    log.error(f"Ошибка при скачивании файлов документации {url}: {e}")
                return None

            # 4. РЕШЕНИЕ: сохранять ли страницу?
            target_categories = {'product', 'contacts', 'distributor', 'main_page'}
            if category not in target_categories:
                log.debug(f"Страница категории '{category}' не подлежит сохранению, ссылки извлечены: {url}")
                return None

            # 5. Проверка размера оригинального HTML
            if len(parse_result.html) < 300:
                log.info(f"Пропускаем сохранение страницы {url}: размер оригинального HTML менее 300 символов")
                return None

            # 5а. D36: детект протухшего sitemap — одинаковый видимый текст у товарных страниц
            if category == 'product':
                await self._check_stale_sitemap_guard(parse_result.html, url, queue)

            # 6. Подготовка метаданных
            metadata = {
                'url': url,
                'normalized_url': self.url_normalizer.normalize_url(parse_result.url) if hasattr(parse_result, 'url') else normalized_url,
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
                url, metadata['normalized_url'], parse_result.html, company_name, metadata, category
            )

            # 8. Дополнительное сохранение для контактов как дистрибьюторов
            if category == 'contacts':
                distributor_metadata = metadata.copy()
                distributor_metadata['category'] = 'distributor'
                distributor_metadata['is_contact_duplicate'] = True
                await self.temp_storage.save_cleaned_html(
                    url, metadata['normalized_url'], parse_result.html, company_name, distributor_metadata, 'distributor'
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
                    'url': url,
                    'category': category,
                    'normalized_url': metadata['normalized_url'],
                    'timestamp': metadata['timestamp'],
                    'metadata': storage_metadata
                }
                if stored_pages is not None:
                    stored_pages.append(stored_page)

            await self._update_stats(category, url)
            return stored_page

        except Exception as e:
            log.error(f"Ошибка обработки страницы {url}: {e}", exc_info=True)
            async with self._stats_lock:
                self.stats['failed_pages'] += 1
            return None


    async def _process_links_from_parse_result(self, url: str, parse_result: ParseResult,
                                            depth: int, queue: deque) -> None:
        """
        Вспомогательный метод: извлекает ссылки из ParseResult и добавляет их в очередь.
        Вызывается всегда, даже если страница не сохраняется.
        """
        try:
            new_links = await self._extract_links(url, parse_result, depth, base_url=self.current_base_url)
            added = 0
            for link_url, link_category, priority in new_links:
                if await self._should_add_to_queue_parallel(link_url, depth + 1):
                    async with self._queue_lock:
                        queue.append((link_url, depth + 1, link_category, priority))
                    async with self._urls_lock:
                        self.visited_urls.add(self.url_normalizer.normalize_url(link_url))
                    added += 1
            if added:
                log.debug(f"Добавлено {added} ссылок из страницы {url}")
        except Exception as e:
            log.error(f"Ошибка извлечения ссылок из {url}: {e}")

    # D59: генерические слаги — не идентификатор товара; по ним склеивать нельзя
    # (иначе разные товары/категории с общим последним сегментом схлопнутся).
    _GENERIC_PRODUCT_SLUGS = frozenset({
        'product', 'products', 'produkciya', 'produktsiya', 'produkty',
        'tovar', 'tovary', 'catalog', 'katalog', 'index', 'page', 'item', 'items',
        'main', 'home', 'default', 'card', 'detail',
    })

    def _scheme_dedup_key(self, url: str):
        """D59 RC1: ключ для отсева ТОЛЬКО схемных дублей http↔https одной страницы —
        нормализованный URL без схемы (домен и путь сохраняются, включая фактический домен,
        без сведе́ния к группе). Две версии http/https одного домена+пути дают один ключ;
        кросс-домен и разные пути НЕ склеиваются здесь (их развести́т extract-стадия по слагу,
        т.к. на разных доменах холдинга контент может отличаться — вплоть до soft-404)."""
        try:
            n = self.url_normalizer.normalize_url(url)
            key = re.sub(r'^https?://', '', n or '')
            return key or None
        except Exception:
            return None

    def _product_dedup_key(self, url: str):
        """D59 (RC1+C): ключ склейки версий ОДНОГО товара, доступного под разными URL.
        Отбрасывает схему (http↔https — RC1) и различие путей/доменов холдинга (C):
        ключ = <представитель группы эквивалентных доменов>|<последний слаг пути>.
        Возвращает None, если слаг не различим (тогда действует обычный дедуп по visited_urls).
        НЕ мутирует реальный URL (скачивание идёт по оригиналу) — только ключ для set-а."""
        try:
            parsed = urlparse(url)
            de = getattr(self.url_categorizer, 'domain_equivalency', None)
            if de is not None and getattr(self.config, 'domain_equivalency_enabled', False):
                # стабильный представитель группы: все домены холдинга -> один ключ
                repr_domain = min(de._domain_group(de.get_canonical_domain(url)))
            else:
                repr_domain = parsed.netloc.lower().split(':')[0]
                if getattr(self.config, 'domain_strict_www', False) and repr_domain.startswith('www.'):
                    repr_domain = repr_domain[4:]
            slug = re.sub(r'/+$', '', parsed.path or '').rsplit('/', 1)[-1].lower()
            if len(slug) < 3 or slug in self._GENERIC_PRODUCT_SLUGS:
                return None
            return f"{repr_domain}|{slug}"
        except Exception as e:
            log.debug(f"_product_dedup_key: не удалось построить ключ для {url}: {e}")
            return None

    async def _should_add_to_queue_parallel(self, url: str, depth: int) -> bool:
        """Потокобезопасная проверка, нужно ли добавлять URL в очередь"""
        category, _ = self.url_categorizer.categorize_url(url)

        if category == 'excluded':
            log.debug(f"Не добавляем исключённый URL в очередь: {url}")
            return False

        if category == 'product' and self.url_categorizer.is_print_version(url, category):
            log.debug(f"Не добавляем печатную версию в очередь: {url}")
            return False
        
        if depth > self.max_depth:
            return False
            
        normalized_url = self.url_normalizer.normalize_url(url)
        
        async with self._urls_lock:
            if normalized_url in self.visited_urls:
                return False
            
            # Проверяем языковые префиксы
            if self.url_categorizer.should_exclude_by_language(url):
                return False
            
            category, _ = self.url_categorizer.categorize_url(url)
            url_depth = self.url_categorizer.calculate_url_depth(url)
            
            # Для не-товарных страниц ограничиваем глубину. Контакты/дистрибьюторы часто
            # лежат глубже (напр. /where-to-buy/регион/город), поэтому им даём больший лимит,
            # согласованный с _should_skip_url (там не-товарные отсекаются при depth > 4).
            non_product_depth_limit = 4 if category in ('contacts', 'distributor') else 2
            if category != 'product' and url_depth > non_product_depth_limit:
                return False
                
            # Для товарных страниц ограничиваем количеством
            if category == 'product' and len(self.product_urls) >= self.max_product_pages_per_site:
                return False

            # D59 (RC1+C): один товар под несколькими URL (http↔https / алиас пути /
            # разные домены холдинга) даёт дубль карточки, т.к. product_id из имени LLM
            # нестабилен. Склеиваем по (группа доменов, последний слаг) ДО постановки в
            # очередь — вторая версия не скачивается и не извлекается.
            if category == 'product':
                skey = self._scheme_dedup_key(url)
                if skey is not None:
                    if skey in self._crawled_scheme_keys:
                        log.info(f"D59 RC1: пропускаем схемный дубль (ключ {skey}): {url}")
                        return False
                    self._crawled_scheme_keys.add(skey)

            # Засчитываем допущенный продуктовый URL сразу при постановке в очередь,
            # а не только при обработке (_update_stats): при параллельной обработке
            # (max_concurrent_pages) иначе лимит перебирается на размер батча.
            if category == 'product':
                self.product_urls.add(normalized_url)

            return True

    async def _download_site_files(self, site_url: str, domain_dirs: Dict[str, str]) -> List[str]:
        """Скачивание файлов с сайта"""
        downloaded_files = []
        
        context = await self.browser_pool.get_browser()
        try:
            page = await context.new_page()
            await page.goto(site_url)
            
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
            if page:
                await page.close()
            if context:
                await self.browser_pool.return_browser(context)
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
            parse_result = await self._fetch_page_content(url, 'product')
            if not parse_result:
                return []
            return self._extract_file_links_from_soup(parse_result.soup, url)
        except Exception as e:
            log.error(f"Ошибка извлечения файловых ссылок со страницы {url}: {e}")
            return []

    def _extract_file_links_from_soup(self, soup, url: str) -> List[str]:
        """Извлечение файловых ссылок из готового soup (общая логика: сохранённый HTML и сетевая загрузка)."""
        try:
            file_links = []

            # Ищем все ссылки с текстом, указывающим на скачивание
            download_keywords = ['скачать', 'download', 'загрузить', 'Скачать', 'Download',
                                 'техническая документация', 'инструкция', 'руководство',
                                 'паспорт', 'сертификат', 'certificate', 'manual', 'Документация', 'сертификаты']

            for link in soup.find_all('a', href=True):
                href = link['href']
                link_text = link.get_text().lower()
                
                if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
                    continue
                
                # Проверяем, содержит ли текст ссылки ключевые слова скачивания
                is_download_link = any(keyword in link_text for keyword in download_keywords)
                
                # Нормализуем URL
                normalized_url = self._normalize_file_url(href, url)
                
                # Проверяем основной домен (фильтруем поддомены)
                if self.config.ignore_subdomains:
                    if self.url_categorizer.is_subdomain(normalized_url, url):
                        if self.config.log_skipped_subdomains:
                            log.info(f"Пропускаем файл с поддомена: {normalized_url}")
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
                    href = parent_link['href']
                    normalized_url = self._normalize_file_url(href, url)
                    
                    # Проверяем основной домен
                    if self.config.ignore_subdomains:
                        if not self.url_categorizer.is_main_domain(normalized_url, url):
                            continue
                        
                    if self._is_supported_file_link(normalized_url):
                        file_links.append(normalized_url)

            # Ищем все ссылки на файлы по расширениям
            for link in soup.find_all('a', href=True):
                href = link['href']
                if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
                    continue
                
                normalized_url = self._normalize_file_url(href, url)
                if self.config.ignore_subdomains:
                    if not self.url_categorizer.is_main_domain(normalized_url, url):
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
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        
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

        # Доп. стартовые точки из профиля сайта (карта разделов sections).
        if self.profile is not None:
            sec = self.profile.sections
            for path in (sec.start_urls + sec.catalog_roots + sec.contacts_urls +
                         sec.distributor_urls + sec.certificates_urls + sec.documents_urls +
                         sec.instructions_urls + sec.price_list_urls):
                full = path if path.startswith('http') else urljoin(base_url + '/', path.lstrip('/'))
                if full not in start_urls:
                    start_urls.append(full)

        return [url for url in start_urls if self._is_valid_url(url)]

    async def _should_skip_url(self, url: str, depth: int, company_name: str) -> bool:
        """Проверка, нужно ли пропустить URL"""
        
        normalized_url = self.url_normalizer.normalize_url(url)
        
        if self.url_categorizer.should_exclude_by_language(url):
            log.debug(f"Пропускаем URL с не-русским языковым префиксом: {url}")
            return True
        
        category, _ = self.url_categorizer.categorize_url(url)
        if category == 'product' and self.url_categorizer.is_print_version(url, category):
            log.debug(f"Пропускаем печатную версию товара: {url}")
            return True
        
        # Проверяем, является ли URL изображением или файлом
        if (self.url_categorizer._is_image_url(url) or 
            self.url_categorizer.is_downloadable_file(url)):
            log.debug(f"Пропускаем URL файла: {url}")
            return True
        
        # Проверяем основной домен (фильтруем поддомены)
        if self.config.ignore_subdomains:
            if not self.url_categorizer.is_main_domain(url, self.current_base_url):
                if self.config.log_skipped_subdomains:
                    log.info(f"Пропускаем неэквивалентный домен: {url} (основной: {self.current_base_url})")
                return True 
           
        # Проверяем посещенные URL
        async with self._urls_lock:
            if normalized_url in self.processed_urls:
                log.debug(f"URL уже обработан: {url} -> {normalized_url}")
                return True       
        # Проверка временного хранилища на наличие сохраненного HTML
        try:
            category, _ = self.url_categorizer.categorize_url(url)
            storage_category = 'product' if category == 'product' else 'other'
            
            # Проверяем существующие файлы в хранилище для этого URL
            existing_files = await self.temp_storage._find_existing_files(normalized_url, company_name, storage_category)
            
            if existing_files:
                for file_path in existing_files:
                    meta_path = file_path.with_suffix('.json')
                    if meta_path.exists():
                        async with aiofiles.open(meta_path, 'r', encoding='utf-8') as f:
                            metadata = json.loads(await f.read())
                        
                        if metadata.get('normalized_url') == normalized_url:
                            log.debug(f"URL уже сохранен в хранилище: {url}")
                            
                            async with self._urls_lock:
                                self.visited_urls.add(normalized_url)
                            
                            if metadata.get('checkpoints', {}).get('ai_processed', False):
                                async with self._urls_lock:
                                    self.processed_urls.add(normalized_url)
                            
                            return True
                            
        except Exception as e:
            log.warning(f"Ошибка проверки URL в хранилище {url}: {e}")

        # Проверяем глубину для разных типов страниц
        url_depth = self.url_categorizer.calculate_url_depth(url)
        category, _ = self.url_categorizer.categorize_url(url)
        
        # Для не-товарных страниц ограничиваем глубину 4
        if category != 'product' and url_depth > 4:
            log.debug(f"Превышена глубина для не-товарной страницы: {url} (глубина: {url_depth})")
            return True
            
        # Продуктовый лимит здесь НЕ проверяем: product_urls наполняется при ПОСТАНОВКЕ URL в
        # очередь (_should_add_to_queue_parallel / _add_sitemap_urls_to_queue), поэтому к моменту
        # обработки счётчик уже = кэпу и эта проверка отбраковывала бы ВСЕ товарные страницы;
        # кэп уже обеспечен на этапе постановки (в очередь попадает ≤ кэпа товарных URL).

        return False

    async def _accept_cookie_consent(self, page) -> bool:
        """Ищет и кликает по кнопке принятия cookies (если присутствует)."""
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
       
    async def _fetch_page_content(self, url: str, category: str = None) -> Optional[ParseResult]:
        """Получение контента страницы с улучшенной обработкой"""
        if category == 'product' and self.url_categorizer.is_print_version(url, category):
            log.debug(f"Не загружаем печатную версию: {url}")
            return None
        try:
            # Проверяем, не в кэше ли перманентных ошибок
            normalized_url = self.url_normalizer.normalize_url(url)
            async with self._errors_cache_lock:
                if normalized_url in self.permanent_errors_cache:
                    log.debug(f"URL в кэше перманентных ошибок, пропускаем: {url}")
                    return None

            # Пер-хостовый троттлинг из профиля (crawl.load.page_delay_ms); 0 = no-op.
            if self.profile is not None and self.host_throttle is not None:
                await self.host_throttle.acquire(urlparse(url).netloc.lower(),
                                                 self.profile.crawl.load.page_delay_ms)
            # Профиль: render=browser -> сразу Playwright (SPA); antibot=impersonate/warmup ->
            # HTTP-first (curl_cffi) пробуется первым и для contacts/distributor.
            force_browser = self.profile is not None and self.profile.crawl.render == 'browser'
            profile_antibot = self.profile.crawl.antibot if self.profile is not None else 'none'

            if category in ('contacts', 'distributor') and self.config.dynamic_content_enabled:
                if profile_antibot in ('impersonate', 'warmup') and not force_browser:
                    http_html = await self._http_first_get(url)
                    if http_html:
                        log.info(f"Профиль (antibot={profile_antibot}): {category}-страница "
                                 f"получена HTTP-first без браузера: {url}")
                        self._census_fetch(url, category, 'http_first')
                        return await self._parse_content(http_html, url)
                log.info(f"Обработка динамического контента для {category}: {url}")
                extractor = self._make_dynamic_extractor()
                result = await extractor.extract(url)

                if isinstance(result, list):
                    # Список HTML (вкладки) -> создаём ParseResult для каждого
                    parsed_list = []
                    for html in result:
                        parsed = await self._parse_content(html, url)
                        if parsed:
                            parsed_list.append(parsed)
                    if parsed_list:
                        self._census_fetch(url, category, 'dynamic')
                        return parsed_list
                elif isinstance(result, str):
                    # Один HTML (аккордеоны) -> один ParseResult
                    parsed = await self._parse_content(result, url)
                    if parsed:
                        self._census_fetch(url, category, 'dynamic')
                        return parsed

                # D79: браузер не отдал контент (частая причина — Page.goto Timeout на медленных
                # динамических контакт/дилерских страницах, напр. dipos.ru/contact/<город>, /dealers,
                # /gde-kupit). Не теряем страницу — откатываемся на HTTP-first/aiohttp, как у товаров.
                # Иначе дилерские страницы не сохраняются и дубль-сохранение contacts->distributor
                # (шаг 8 в _process_page) не срабатывает -> дилеры не извлекаются.
                log.warning(f"Динамический контент не получен для {category}, HTTP-fallback: {url}")
                http_html = await self._http_first_get(url)
                if not http_html:
                    http_html = await self._fetch_with_aiohttp(url)
                if http_html:
                    log.info(f"HTTP-fallback отдал {category}-страницу без браузера ({len(http_html)} симв): {url}")
                    self._census_fetch(url, category, 'http_first')
                    return await self._parse_content(http_html, url)
                if self.metrics_collector is not None:
                    self.metrics_collector.record_fetch_fail(url, category)
                return None
                
            # Для продуктовых/каталожных страниц сначала пробуем aiohttp: статические каталоги
            # (напр. aerobel.ru) отдают полный HTML со ссылками на карточки обычным GET, тогда как
            # Playwright по таким страницам иногда возвращает пустой каркас. Если товарных ссылок
            # в aiohttp-ответе нет (сетка рендерится JS), откатываемся на Playwright.
            if category == 'product':
                # D01: домены с AJAX-вкладками характеристик — рендер браузером с кликами
                # по вкладкам и склейкой в один HTML (HTTP-first бесполезен: первичный DOM
                # не содержит контента вкладок)
                ajax_domains = getattr(self.config, 'ajax_product_tabs_domains', []) or []
                page_host = urlparse(url).netloc.lower()
                profile_ajax = self.profile is not None and self.profile.crawl.ajax_tabs
                if profile_ajax or any(page_host == d or page_host.endswith('.' + d) for d in ajax_domains):
                    extractor = self._make_dynamic_extractor()
                    merged_html = await extractor.extract_product_tabs_merged(url)
                    if merged_html:
                        log.info(f"Товарная страница с AJAX-вкладками склеена ({len(merged_html)} симв): {url}")
                        self._census_fetch(url, category, 'ajax_tabs')
                        return await self._parse_content(merged_html, url)
                    # если браузер не отдал страницу — падаем на обычную лестницу ниже

                # HTTP-first: curl_cffi impersonate + cookie-warmup при antibot-челлендже.
                # Если статический HTML достаточен и JS не нужен (или есть товарный остров) —
                # берём без браузера; иначе (None) идём по старой лестнице aiohttp→Playwright.
                # Профиль render=browser: HTTP-слой пропускается — сразу Playwright.
                aiohttp_parsed = None
                aiohttp_content = None
                if not force_browser:
                    http_html = await self._http_first_get(url, expect_product=True)
                    if http_html:
                        log.info(f"HTTP-first отдал товарную страницу без браузера ({len(http_html)} симв): {url}")
                        self._census_fetch(url, category, 'http_first')
                        return await self._parse_content(http_html, url)

                    aiohttp_content = await self._fetch_with_aiohttp(url)
                if aiohttp_content:
                    aiohttp_parsed = await self._parse_content(aiohttp_content, url)
                    if any(cat == 'product' for _, cat, _ in aiohttp_parsed.additional_links):
                        self._census_fetch(url, category, 'aiohttp')
                        return aiohttp_parsed
                    # Гейт data-island: если полный контент товарной страницы уже пришёл
                    # обычным GET (достаточно SSR-текста либо в HTML есть товарный остров
                    # JSON-LD/__NEXT__/__NUXT__ — needs_javascript учитывает оба сигнала),
                    # запускать Playwright не нужно. needs_javascript строит собственный
                    # временный soup и не мутирует aiohttp_parsed.
                    # Гейт оболочки повторяется и здесь: иначе эскалация из _http_first_get
                    # обесценивается — aiohttp принесёт тот же пустой HTML, и мы вернём его,
                    # так и не дойдя до Playwright.
                    _min_text = self._product_shell_min_text()
                    _shell = bool(_min_text) and looks_like_product_shell(aiohttp_content, min_text=_min_text)
                    if (not _shell and not needs_javascript(aiohttp_content)
                            and not is_unrendered_store_listing(aiohttp_content)):
                        log.debug(f"data-island: контент товара уже в HTML, пропускаем Playwright: {url}")
                        self._census_fetch(url, category, 'aiohttp')
                        return aiohttp_parsed

                # Товарных ссылок в обычном GET нет — пробуем Playwright (динамический рендеринг)
                result = await self._fetch_with_playwright(url)

                # result всегда должен быть кортежем из 3 элементов
                if result is None:
                    log.error(f"_fetch_with_playwright вернул None для {url}")
                    return aiohttp_parsed

                content, is_permanent_error, final_url = result
                if is_permanent_error:
                    return aiohttp_parsed
                # Если Playwright вернул меньше, чем обычный GET (типичный «каркас» вместо контента) —
                # используем более полный aiohttp-ответ.
                if aiohttp_content and len(aiohttp_content) > len(content or ''):
                    log.info(
                        f"Playwright вернул меньше ({len(content or '')} симв), чем aiohttp "
                        f"({len(aiohttp_content)} симв) — берём aiohttp: {url}"
                    )
                    return aiohttp_parsed
                if content:
                    effective_url = final_url if final_url and final_url != url else url
                    # Проверяем, что конечный домен эквивалентен базовому
                    if final_url and final_url != url:
                        if not self.url_categorizer.is_main_domain(final_url, self.current_base_url):
                            log.warning(f"Редирект на неэквивалентный домен: {url} -> {final_url}, пропускаем")
                            return None
                    # Парсим с учётом конечного URL
                    parse_result = await self._parse_content(content, effective_url)
                    # Добавляем информацию о редиректе в метаданные (будет использовано при сохранении)
                    if parse_result and final_url and final_url != url:
                        # Сохраним исходный URL в дополнительном поле parse_result (можно через атрибут)
                        parse_result.redirected_from = url
                    self._census_fetch(url, category, 'playwright')
                    return parse_result
                if aiohttp_parsed is not None:
                    self._census_fetch(url, category, 'aiohttp')
                elif self.metrics_collector is not None:
                    self.metrics_collector.record_fetch_fail(url, category)
                return aiohttp_parsed
            else:
                # HTTP-impersonate первым (сильнее голого aiohttp). Не гейтим по needs_javascript:
                # для не-товарных страниц важны ссылки для BFS, они есть и в «тонком» HTML.
                # Профиль render=browser: HTTP-слой пропускается — сразу Playwright.
                if not force_browser and getattr(self.config, 'http_first_enabled', True) and _CURL_CFFI_AVAILABLE:
                    res = await self._fetch_with_http_impersonate(url)
                    if res and not res.from_cache_error and res.status == 200 and res.html:
                        ok_domain = True
                        if res.final_url and res.final_url != url:
                            ok_domain = self.url_categorizer.is_main_domain(res.final_url, self.current_base_url)
                        if ok_domain:
                            self._census_fetch(url, category, 'http_first')
                            return await self._parse_content(res.html, url)

                content = None if force_browser else await self._fetch_with_aiohttp(url)
                if content:
                    self._census_fetch(url, category, 'aiohttp')
                    return await self._parse_content(content, url)
                result = await self._fetch_with_playwright(url)
                if result is not None:
                    content, _, final_url = result
                    if content:
                        self._census_fetch(url, category, 'playwright')
                        return await self._parse_content(content, final_url or url)
                if self.metrics_collector is not None:
                    self.metrics_collector.record_fetch_fail(url, category)
                return None

        except Exception as e:
            log.error(f"Ошибка получения контента {url}: {e}")
            return None

    async def _fetch_with_playwright(self, url: str) -> tuple[Optional[str], bool, Optional[str]]:
        """
        Специализированная загрузка продуктовых страниц с полной обработкой динамического контента

        Returns:
            Tuple[Optional[str], bool, Optional[str]]: (содержимое страницы, была ли перманентная ошибка, финальный URL после редиректа)
        """
        normalized_url = self.url_normalizer.normalize_url(url)

        async with self._errors_cache_lock:
            if normalized_url in self.permanent_errors_cache:
                log.debug(f"Пропускаем URL из кэша ошибок: {url}")
                return None, True, None

        context = await self.browser_pool.get_browser()
        page = None
        try:
            page = await context.new_page()
            # ИЗМЕНЕНО: убрали set_default_timeout(6000), таймауты теперь явные

            log.info(f"Загружаем продуктовую страницу с улучшенной обработкой: {normalized_url}")

            # ИЗМЕНЕНО: увеличен таймаут до 20 сек, wait_until='domcontentloaded'
            response = await page.goto(url, wait_until='domcontentloaded', timeout=20000)
            if page.is_closed():
                log.debug(f"Страница закрыта после goto для {url}")
                return None, False, None

            # ДОБАВЛЕНО: короткое ожидание networkidle (3 сек) – необязательно
            try:
                await page.wait_for_load_state('networkidle', timeout=3000)
            except Exception:
                pass  # нормально, не блокируем

            await self._accept_cookie_consent(page)
            await self._scroll_page(page)

            if response is not None and response.status in PERMANENT_ERRORS:
                log.warning(f"Playwright: Перманентная ошибка {response.status} для {url}")
                async with self._errors_cache_lock:
                    self.permanent_errors_cache.add(normalized_url)
                return None, True, None

            final_url = page.url

            await self._wait_for_product_content(page)
            await self._wait_for_dynamic_links(page)
            content = await self._extract_dynamic_product_content(page)

            log.info(f"Успешно обработана продуктовая страница через Playwright: {url}")
            return content, False, final_url

        except PlaywrightTimeoutError:
            log.warning(f"Таймаут при обработке продуктовой страницы {url}")
            return None, False, None
        except Exception as e:
            log.debug(f"Playwright не смог обработать продуктовую страницу {url}: {e}")
            return None, False, None
        finally:
            if page:
                await page.close()
            if context:
                await self.browser_pool.return_browser(context)

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
        
            # Ожидаем появления хотя бы одного ключевого элемента # ИЗМЕНЕНО: таймаут увеличен до 10 секунд
            for selector in products_selectors:
                try:
                    await page.wait_for_selector(selector, timeout=10000)
                    log.debug(f"Найден ключевой элемент товара: {selector}")
                    break
                except PlaywrightTimeoutError:
                    continue

                except Exception as e:
                    # Если страница закрыта, выходим
                    if "closed" in str(e).lower():
                        log.debug(f"Страница закрыта при ожидании селектора {selector}: {e}")
                        return
                    raise

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
            
            for indicator in file_indicators:
                try:
                    await page.wait_for_selector(f"xpath={indicator}", timeout=3000)
                    log.debug(f"Найден файловый индикатор: {indicator}")
                except PlaywrightTimeoutError:
                    continue
                except Exception as e:
                    if "closed" in str(e).lower():
                        log.debug(f"Страница закрыта при ожидании индикатора {indicator}: {e}")
                        return
                        
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
        if not _CURL_CFFI_AVAILABLE:
            return None

        normalized_url = self.url_normalizer.normalize_url(url)
        async with self._errors_cache_lock:
            if normalized_url in self.permanent_errors_cache:
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
                    resp = await sess.get(url, headers=IMPERSONATE_HEADERS, allow_redirects=True)
                    html = resp.text
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    try:
                        cookies = {c.name: c.value for c in sess.cookies.jar}
                    except Exception:
                        cookies = {}
                    if resp.status_code in PERMANENT_ERRORS:
                        async with self._errors_cache_lock:
                            self.permanent_errors_cache.add(normalized_url)
                    return HttpFetchResult(html, resp.status_code, headers, str(resp.url), cookies)
                finally:
                    if own_session:
                        await sess.close()
            except Exception as e:
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
        return DynamicContentExtractor(self.browser_pool, self.config, extra_tab_panel_selector=extra)

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
        if not _CURL_CFFI_AVAILABLE:
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
                await sess.get(home, headers=IMPERSONATE_HEADERS, allow_redirects=True)
                await asyncio.sleep(delay)
                # 2) повтор цели в той же сессии
                hdrs = {**IMPERSONATE_HEADERS, "Referer": home, "Sec-Fetch-Site": "same-origin"}
                resp = await sess.get(url, headers=hdrs, allow_redirects=True)
                headers = {k.lower(): v for k, v in resp.headers.items()}
                try:
                    cookies = {c.name: c.value for c in sess.cookies.jar}
                except Exception:
                    cookies = {}
                return HttpFetchResult(resp.text, resp.status_code, headers, str(resp.url), cookies)
            except Exception as e:
                log.debug(f"warmup не удался для {url}: {e}")
                return None
            finally:
                await sess.close()

    def _product_shell_min_text(self) -> int:
        """Порог гейта «оболочки» товарной страницы (0 = гейт выключен)."""
        return getattr(self.config, 'product_min_text', 1000)

    async def _http_first_get(self, url: str, *, expect_product: bool = False) -> Optional[str]:
        """
        Лестница эскалации на уровне HTTP (без Playwright):
          1) HTTP-impersonate -> 2) при challenge: cookie-warmup -> повтор.
        Возвращает HTML (валидный 200 с контентом) либо None — тогда вызывающий
        эскалирует к Playwright.
        """
        if not (getattr(self.config, 'http_first_enabled', True) and _CURL_CFFI_AVAILABLE):
            return None
        res = await self._fetch_with_http_impersonate(url)
        if res is None or res.from_cache_error:
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
        # Tilda store-ЛИСТИНГ с JS-плитками: статический HTML не содержит ссылок на
        # товары (рендерятся браузером). HTTP-first бесполезен — форсим Playwright,
        # иначе целый раздел каталога теряется (D96). Проверка до needs_javascript и
        # фолбэка has_product_island (иначе объёмный текст листинга их обманывает).
        if is_unrendered_store_listing(res.html):
            return None
        # Товарная страница-«оболочка»: разметка есть, видимого текста почти нет — контент
        # дорисовывает JS. needs_javascript такое пропускает (ему нужен ещё признак SPA),
        # поэтому HTTP-first отдавал страницу без товара и LLM отвергал её как Trash_418#.
        # Отдаём None -> лестница идёт на aiohttp/Playwright; если и там пусто, вызывающий
        # код всё равно сохранит то, что получил (поведение fail-open не меняется).
        min_text = self._product_shell_min_text()
        if expect_product and min_text and looks_like_product_shell(res.html, min_text=min_text):
            log.debug(f"HTTP-first: товарная страница похожа на оболочку, эскалируем в браузер: {url}")
            return None
        min_content = getattr(self.config, 'http_min_content', 800)
        # Контент пришёл и его достаточно / data-island / не нужен JS -> отдаём HTML
        if len(res.html) >= min_content and not needs_javascript(
            res.html, text_threshold=getattr(self.config, 'js_render_text_threshold', 300)
        ):
            # Проверка редиректа на неэквивалентный домен
            if res.final_url and res.final_url != url:
                if not self.url_categorizer.is_main_domain(res.final_url, self.current_base_url):
                    return None
            return res.html
        # Контент есть, но похоже нужен JS — если есть товарный остров, всё равно отдаём
        if res.html and has_product_island(res.html):
            return res.html
        return None

    async def _fetch_with_aiohttp(self, url: str) -> Optional[str]:
        """Получение контента через aiohttp с использованием семафора"""
        normalized_url = self.url_normalizer.normalize_url(url)

        # Проверяем кэш ошибок
        async with self._urls_lock:
            if normalized_url in self.permanent_errors_cache:
                log.debug(f"Пропускаем URL из кэша ошибок: {url}")
                return None
        
        async with self.aiohttp_semaphore:
            try:
                # КОННЕКТОР С ОТКЛЮЧЕННОЙ ПРОВЕРКОЙ SSL (удалить connector=connector из aiohttp.ClientSession(connector=connector) as session)
                connector = aiohttp.TCPConnector(ssl=False)
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=self._effective_http_timeout())) as response:
                        if response.status == 200:
                            content = await response.text()
                            log.debug(f"Успешно получен контент через aiohttp: {url}")
                            return content
                        
                        elif response.status in PERMANENT_ERRORS:
                            log.warning(f"Перманентная ошибка {response.status} для {url}")
                            self.permanent_errors_cache.add(normalized_url)
                            return None
                        
                        else:
                            log.debug(f"aiohttp: статус {response.status} для {url}")
                            return None
                        
            except Exception as e:
                log.debug(f"aiohttp не смог получить {url}: {e}")
                return None

    async def _parse_content(self, html: str, url: str) -> ParseResult:
        """Парсинг контента страницы с учетом canonical URL"""
        try:
            # D82: для Bitrix-шаблона betar.ru дотягиваем блок «Исполнение и цена» (AJAX
            # product_offers.php) и вклеиваем таблицу исполнений в HTML — до конвертации в
            # markdown и до сохранения cleaned-HTML (сохраняется именно parse_result.html).
            # No-op для всех прочих доменов/страниц (гейт внутри метода).
            html = await self._enrich_bitrix_offers(html, url)

            soup = BeautifulSoup(html, 'html.parser')
            
            canonical_url = self.url_normalizer.extract_canonical_url(soup, url)            
            
            effective_url = canonical_url if canonical_url != url else url            
            
            content_type = self._detect_content_type(soup, effective_url)            
            
            additional_links = await self._extract_additional_links(soup, effective_url)            
            
            elements_found = self._count_elements(soup)
            
            return ParseResult(
                soup=soup,
                html=html,
                additional_links=additional_links,
                content_type=content_type,
                elements_found=elements_found
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
        """GET /local/ajax/product_offers.php?section_id=N&lang=ru → dict {section, offers, filter}.
        Отдельный лёгкий запрос (не через _fetch_with_aiohttp): нужен сырой JSON того же домена
        с сохранением query-параметров; ssl=False — у betar.ru просроченный самоподписанный cert."""
        p = urlparse(page_url)
        endpoint = f"{p.scheme}://{p.netloc}/local/ajax/product_offers.php?section_id={section_id}&lang=ru"
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    endpoint,
                    timeout=aiohttp.ClientTimeout(total=20),
                    headers={'X-Requested-With': 'XMLHttpRequest'},
                ) as response:
                    if response.status != 200:
                        log.debug(f"product_offers.php статус {response.status} (section_id={section_id})")
                        return None
                    data = await response.json(content_type=None)
                    return data if isinstance(data, dict) else None
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
            current_parsed = urlparse(current_url)
            current_scheme = current_parsed.scheme

            # Извлекаем все ссылки со страницы
            for link in soup.find_all('a', href=True):
                href = link['href']
                if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:')):
                    continue
                    
                full_url = urljoin(current_url, href)

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
                link_text = link.get_text(strip=True)
                
                # Категоризируем URL
                category, priority = self.url_categorizer.categorize_url(full_url, link_text)
                
                # Добавляем в список
                links.append((full_url, category, priority))
            
            # Извлекаем товарные ссылки из сеток
            product_links = self.product_grid_parser.extract_product_links(soup, current_url, current_url)
            links.extend(product_links)
            
            # Удаляем дубликаты
            unique_links = list(set(links))
            
            return unique_links
            
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
        parsed_base = urlparse(base_url)
        # Используем базовый домен для проверки
        base_domain_for_check = base_url

        for link_url, category, priority in all_links:
            if category == 'product' and self.url_categorizer.is_print_version(link_url, category):
                log.debug(f"Пропускаем ссылку на печатную версию: {link_url}")
                continue
            try:
                # Проверяем основной домен (фильтруем поддомены и неэквивалентные)
                if self.config.ignore_subdomains:
                    if not self.url_categorizer.is_main_domain(link_url, base_domain_for_check):
                        if self.config.log_skipped_subdomains:
                            log.info(f"Пропускаем неэквивалентный домен: {link_url} (основной: {base_domain_for_check})")
                        async with self._stats_lock:
                            self.stats['skipped_subdomains'] += 1
                        continue
                else:
                    # Старая логика – точное совпадение доменов
                    parsed_link = urlparse(link_url)
                    if parsed_link.netloc != parsed_base.netloc:
                        continue

                normalized_url = self.url_normalizer.normalize_url(link_url)
                if normalized_url in self.visited_urls:
                    continue

                link_depth = self.url_categorizer.calculate_url_depth(link_url)
                if category != 'product' and link_depth > 4:
                    continue

                filtered_links.append((link_url, category, priority))

            except Exception as e:
                log.debug(f"Ошибка фильтрации ссылки {link_url}: {e}")
                continue

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
        if depth > self.max_depth:
            return False
            
        normalized_url = self.url_normalizer.normalize_url(url)
        if normalized_url in self.visited_urls:
            return False
            
        category, _ = self.url_categorizer.categorize_url(url)
        url_depth = self.url_categorizer.calculate_url_depth(url)
        
        # Для не-товарных страниц ограничиваем глубину
        if category != 'product' and url_depth > 2:
            return False
            
        # Для товарных страниц ограничиваем количеством
        if category == 'product' and len(self.product_urls) >= self.max_product_pages_per_site:
            return False
            
        return True

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

        # Подготовка метаданных
        metadata = {
            'url': url,
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
            if await self._should_add_to_queue_parallel(link_url, depth + 1):
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