"""P02 U1: учёт товарной квоты — возврат слота, сторож жёстких отказов, счётчики.

Коды: D226 (слот занимается при постановке в очередь и не возвращается никогда),
D193/D143/D212 (немые гейты очереди), D222 (отказ по квоте в счётчике «дубликатов»),
D225 (сторож снимает product_urls/visited_urls, но не схемный ключ D59 RC1).

Тесты проверяют инварианты возврата слота без сети: сетевой слой подменяется заглушкой.
"""
import asyncio
from collections import deque

import pytest
from bs4 import BeautifulSoup

from config import Config
from web_crawler import ParseResult, WebCrawler


@pytest.fixture
def crawler(tmp_path):
    config = Config()
    config.base_dir = str(tmp_path)
    crawler = WebCrawler(config)
    crawler.current_base_url = 'https://a.ru/'
    # ссылки со страницы для этих тестов не важны
    async def _no_links(url, parse_result, depth, queue):
        return None
    crawler._process_links_from_parse_result = _no_links
    return crawler


def _page(html):
    return ParseResult(BeautifulSoup(html, 'html.parser'), html, [], 'product_page', {})


def _stub_fetch(crawler, result):
    async def fake(url, category=None):
        return result
    crawler._fetch_page_content = fake


def _occupy(crawler, url):
    """Слот квоты занимается так же, как при постановке URL в очередь."""
    key = crawler.url_normalizer.normalize_url(url)
    crawler.product_urls.add(key)
    return key


def _process(crawler, url, category='product', depth=0, queue=None):
    return asyncio.run(crawler._process_page_with_storage(
        url, depth, category, 'ООО Тест', {'base_domain_dir': crawler.config.base_dir},
        queue if queue is not None else deque()))


# ==================== Возврат слота на путях неуспеха ====================

def test_slot_released_on_no_content(crawler):
    """D226: страница не получена — слот квоты освобождается."""
    key = _occupy(crawler, 'https://a.ru/catalog/x1')
    _stub_fetch(crawler, None)
    _process(crawler, 'https://a.ru/catalog/x1')
    assert key not in crawler.product_urls


def test_slot_released_on_short_html(crawler):
    """D226, второй путь потери страницы: HTML меньше 300 символов."""
    key = _occupy(crawler, 'https://a.ru/catalog/x2')
    _stub_fetch(crawler, _page('<html><body>пусто</body></html>'))
    _process(crawler, 'https://a.ru/catalog/x2')
    assert key not in crawler.product_urls


def test_slot_kept_on_success(crawler):
    """Регресс: успешно сохранённая товарная страница слот удерживает."""
    key = _occupy(crawler, 'https://a.ru/catalog/x3')
    _stub_fetch(crawler, _page('<html><body>' + 'Блок газобетонный D500. ' * 40 + '</body></html>'))
    _process(crawler, 'https://a.ru/catalog/x3')
    assert key in crawler.product_urls


def test_slot_not_released_for_non_product(crawler):
    """Квота товарная: страница другой категории её не трогает."""
    key = _occupy(crawler, 'https://a.ru/contacts/')
    _stub_fetch(crawler, None)
    _process(crawler, 'https://a.ru/contacts/', category='contacts')
    assert key in crawler.product_urls


def test_release_is_idempotent(crawler):
    """Повторная обработка того же адреса (раунд ретраев D40) не крутит счётчик."""
    _occupy(crawler, 'https://a.ru/catalog/x4')
    _stub_fetch(crawler, None)
    _process(crawler, 'https://a.ru/catalog/x4')
    _process(crawler, 'https://a.ru/catalog/x4')
    assert crawler._product_quota_reused == 1


def test_reuse_ceiling_stops_release(crawler):
    """ОБЯЗАТЕЛЬНЫЙ предохранитель: сайт со сплошными отказами не крутит квоту
    до исчерпания max_pages_per_site."""
    crawler.product_quota_reuse_limit = 2
    _stub_fetch(crawler, None)
    keys = []
    for i in range(4):
        url = f'https://a.ru/catalog/dead-{i}'
        keys.append(_occupy(crawler, url))
        _process(crawler, url)
    assert crawler._product_quota_reused == 2
    assert len([k for k in keys if k in crawler.product_urls]) == 2


# ==================== Сторож жёстких отказов карты сайта ====================

def _sitemap_queue(crawler, count):
    """Очередь из товарных адресов карты сайта (глубина 0) со снятыми слотами."""
    rows = [(f'https://a.ru/catalog/p{i}/', f'https://a.ru/catalog/p{i}', 'product', 10)
            for i in range(count)]
    queue = deque()
    asyncio.run(crawler._add_sitemap_urls_to_queue(queue, rows, 'https://a.ru/'))
    return queue


def test_hard_fail_guard_purges_sitemap_tail(crawler):
    """P02 U1 п.2: N подряд товарных адресов карты без содержимого — хвост карты
    вычищается из очереди, слоты возвращаются."""
    crawler.stale_sitemap_hard_fail_threshold = 3
    queue = _sitemap_queue(crawler, 10)
    assert len(queue) == 10 and len(crawler.product_urls) == 10
    _stub_fetch(crawler, None)
    for _ in range(3):
        url, depth, category, _priority = queue.popleft()
        _process(crawler, url, category=category, depth=depth, queue=queue)
    assert crawler._stale_sitemap_guard_tripped is True
    assert len(queue) == 0
    assert crawler.product_urls == set()


def test_hard_fail_streak_resets_on_content(crawler):
    """Одиночные отказы вперемешку с живыми страницами сторож не взводят."""
    crawler.stale_sitemap_hard_fail_threshold = 3
    queue = _sitemap_queue(crawler, 10)
    good = _page('<html><body>' + 'Блок газобетонный D500. ' * 40 + '</body></html>')
    for index in range(6):
        url, depth, category, _priority = queue.popleft()
        _stub_fetch(crawler, None if index % 2 == 0 else good)
        _process(crawler, url, category=category, depth=depth, queue=queue)
    assert crawler._stale_sitemap_guard_tripped is False


def test_purge_discards_scheme_keys(crawler):
    """D225: вместе с product_urls/visited_urls снимается схемный ключ D59 RC1 —
    иначе те же адреса, предложенные ссылками, отбиваются как «схемный дубль»."""
    queue = _sitemap_queue(crawler, 3)
    assert crawler._crawled_scheme_keys
    asyncio.run(crawler._purge_sitemap_tail(queue, 'Тест.'))
    assert crawler._crawled_scheme_keys == set()
    assert asyncio.run(crawler._should_add_to_queue_parallel('https://a.ru/catalog/p0/', 1)) is True


# ==================== Счётчики отказов ====================

def test_quota_skips_counted_separately(crawler):
    """D222: отказ по квоте больше не считается «дубликатом» и копится для страховки."""
    crawler.max_product_pages_per_site = 2
    rows = [(f'https://a.ru/catalog/p{i}/', f'https://a.ru/catalog/p{i}', 'product', 10)
            for i in range(5)]
    queue = deque()
    added = asyncio.run(crawler._add_sitemap_urls_to_queue(queue, rows, 'https://a.ru/'))
    assert added == 2
    assert len(crawler._quota_rejected_urls) == 3


def test_queue_gate_rejections_counted(crawler):
    """D193/D143/D212: немой return False гейта очереди теперь виден в сводке."""
    crawler.max_product_pages_per_site = 0
    assert asyncio.run(crawler._should_add_to_queue_parallel('https://a.ru/catalog/p1/', 1)) is False
    assert crawler._queue_gate_rejections.get('товарная квота') == 1
    assert crawler._quota_rejected_urls == ['https://a.ru/catalog/p1/']


# ==================== Страховка: второй проход ====================

def test_second_pass_runs_when_no_product_pages(crawler):
    """P02 U1 п.5: 0 сохранённых товарных при непустом множестве отбитых по квоте —
    квота освобождается и отбитые адреса обходятся вторым проходом."""
    crawler._quota_rejected_urls = ['https://a.ru/catalog/live-1/', 'https://a.ru/catalog/live-2/']
    crawler.product_urls = {'https://a.ru/catalog/dead-1', 'https://a.ru/catalog/dead-2'}
    seen = []

    async def fake_process(url, depth, category, company_name, domain_dirs, queue, stored_pages=None):
        seen.append(url)
        return None

    crawler._process_page_with_storage = fake_process
    asyncio.run(crawler._second_pass_for_quota_rejected('ООО Тест', {}, [], deque()))
    assert seen == ['https://a.ru/catalog/live-1/', 'https://a.ru/catalog/live-2/']
    assert crawler.product_urls == set()


def test_second_pass_skipped_when_products_saved(crawler):
    """Страховка не срабатывает, если товарные страницы всё-таки сохранились."""
    crawler.stats['product_pages'] = 4
    crawler._quota_rejected_urls = ['https://a.ru/catalog/live-1/']
    seen = []

    async def fake_process(url, depth, category, company_name, domain_dirs, queue, stored_pages=None):
        seen.append(url)
        return None

    crawler._process_page_with_storage = fake_process
    asyncio.run(crawler._second_pass_for_quota_rejected('ООО Тест', {}, [], deque()))
    assert seen == []
