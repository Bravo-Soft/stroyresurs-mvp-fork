"""P01 U1: нормализованный URL — только ключ дедупликации, а не адрес запроса.

Коды: D120 (снятый слеш), D219 (снятый /ru), D245 (вырезанный ?p=), D198 (дописанный
слеш числовому пути), D192 (бесслешевые жёсткие стартовые пути).

Фикстуры tests/fixtures/sitemaps/*.xml сняты с живых карт сайта компаний из досье
(11.09.2026) и обрезаны до десятка <url>: oilon-pro.ru №5, byrpex.com №1177,
gomeloboi.by №1436, silikat.ru №954, brozex.ru №292, egger-russia.ru №911.
Сети тесты не требуют.
"""
import asyncio
from collections import deque
from pathlib import Path

import pytest

from config import Config
from web_crawler import WebCrawler

FIXTURES = Path(__file__).parent / 'fixtures' / 'sitemaps'


def _fixture(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


@pytest.fixture
def crawler(tmp_path):
    config = Config()
    config.base_dir = str(tmp_path)
    return WebCrawler(config)


@pytest.fixture
def normalizer(crawler):
    return crawler.url_normalizer


def _parse(crawler, fixture_name, sitemap_url):
    return asyncio.run(crawler.sitemap_parser._parse_xml_sitemap(
        _fixture(fixture_name), sitemap_url, 0, 1))


# ==================== Нормализатор: роль ключа ====================

def test_trailing_slash_collapses_in_key(normalizer):
    """Обе формы дают ОДИН ключ — дедуп по-прежнему работает (цель D120 не сломана)."""
    with_slash = normalizer.normalize_url('https://oilon-pro.ru/catalog/gazovye_gorelki/')
    without = normalizer.normalize_url('https://oilon-pro.ru/catalog/gazovye_gorelki')
    assert with_slash == without == 'https://oilon-pro.ru/catalog/gazovye_gorelki'


def test_numeric_path_no_forced_slash(normalizer):
    """D198: числовому пути слеш больше не дописывается, обе формы сходятся к бесслешевой."""
    assert normalizer.normalize_url('http://silikat.ru/product/102') == 'http://silikat.ru/product/102'
    assert normalizer.normalize_url('http://silikat.ru/product/102/') == 'http://silikat.ru/product/102'
    assert normalizer.normalize_url('http://silikat.ru/product/103-885') == 'http://silikat.ru/product/103-885'


def test_ru_prefix_stripped_only_in_key(normalizer):
    """D219: срез /ru остаётся в КЛЮЧЕ (цель D41), адрес запроса берётся из карты сайта."""
    # www в ключе срезается канонизатором доменов — это прежнее поведение, не часть U1
    assert normalizer.normalize_url('https://www.byrpex.com/ru/cat/truby') == 'https://byrpex.com/cat/truby'


def test_p_param_survives_normalization(normalizer):
    """D245: ?p= — идентификатор записи WordPress, а не пагинация: URL не схлопывается."""
    assert normalizer.normalize_url('https://gomeloboi.by/?p=5845') == 'https://gomeloboi.by/?p=5845'
    assert (normalizer.normalize_url('https://gomeloboi.by/?p=5845')
            != normalizer.normalize_url('https://gomeloboi.by/'))


def test_deep_pagination_param_not_silently_dropped(normalizer):
    """D245: параметр глубокой пагинации сохраняется в ключе, а не вырезается молча."""
    assert normalizer.normalize_url('https://a.ru/catalog/?page=999') == 'https://a.ru/catalog?page=999'
    assert normalizer.is_over_pagination_limit('https://a.ru/catalog/?page=999') is True
    assert normalizer.is_over_pagination_limit('https://a.ru/catalog/?page=3') is False
    assert normalizer.is_over_pagination_limit('https://gomeloboi.by/?p=5845') is False
    assert normalizer.is_over_pagination_limit('https://a.ru/catalog/') is False


def test_shallow_pagination_unchanged(normalizer):
    """Регресс: неглубокая пагинация как и раньше остаётся в ключе."""
    assert normalizer.normalize_url('https://a.ru/catalog/?page=2') == 'https://a.ru/catalog?page=2'


# ==================== Парсер карты сайта: пара «адрес + ключ» ====================

def test_xml_sitemap_returns_published_and_key(crawler):
    """D120 (Oilon): в четвёрке опубликованный адрес со слешем, ключ — без."""
    rows = _parse(crawler, 'oilon_pro_iblock81.xml', 'https://oilon-pro.ru/sitemap-iblock-81.xml')
    assert rows, 'фикстура карты сайта пуста'
    assert all(len(row) == 4 for row in rows)
    published = [row[0] for row in rows]
    keys = [row[1] for row in rows]
    assert all(url.endswith('/') for url in published)
    assert not any(key.endswith('/') for key in keys)
    assert 'https://oilon-pro.ru/catalog/gazovye_gorelki/gazovye_gorelki_oilon_gp/' in published


def test_xml_sitemap_keeps_ru_prefix_in_published(crawler):
    """D219 (БИР ПЕКС): /ru/ остаётся в адресе запроса и исчезает только в ключе."""
    rows = _parse(crawler, 'byrpex_ru.xml', 'https://www.byrpex.com/sitemap.xml')
    assert rows
    assert all(row[0].startswith('https://www.byrpex.com/ru/') for row in rows)
    assert all('/ru/' not in row[1] for row in rows)


def test_xml_sitemap_keeps_p_param(crawler):
    """D245 (Гомельобои): 12 пермалинков ?p= дают 12 РАЗНЫХ ключей, а не один корень."""
    rows = _parse(crawler, 'gomeloboi_p.xml', 'https://gomeloboi.by/sitemap.xml')
    assert len(rows) == 12
    assert all('?p=' in row[0] for row in rows)
    assert len({row[1] for row in rows}) == 12


def test_xml_sitemap_numeric_paths_untouched(crawler):
    """D198 (Костромской силикатный): адрес карточки остаётся без дописанного слеша."""
    rows = _parse(crawler, 'silikat_numeric.xml', 'http://www.silikat.ru/sitemap.xml')
    published = [row[0] for row in rows]
    assert 'http://www.silikat.ru/product/103-885' in published
    assert not any(url.endswith('/') for url in published)


def test_html_and_text_sitemaps_return_quads(crawler):
    """Контракт четвёрки одинаков для HTML- и текстовой карты сайта."""
    html = ('<html><body><a href="/catalog/gate_valves/364/">Задвижка</a>'
            '<a href="/catalog/zhbi/">ЖБИ</a></body></html>')
    rows = asyncio.run(crawler.sitemap_parser._parse_html_sitemap(html, 'https://kpp33.ru/sitemap.html'))
    assert rows and all(len(row) == 4 for row in rows)
    assert 'https://kpp33.ru/catalog/gate_valves/364/' in [row[0] for row in rows]

    text = 'https://kpp33.ru/catalog/zhbi/\nhttps://kpp33.ru/catalog/gate_valves/364/\n'
    rows = asyncio.run(crawler.sitemap_parser._parse_text_sitemap(text, 'https://kpp33.ru/sitemap.txt'))
    assert rows and all(len(row) == 4 for row in rows)
    assert 'https://kpp33.ru/catalog/zhbi/' in [row[0] for row in rows]


# ==================== Очередь: в неё уходит опубликованный адрес ====================

def test_queue_gets_published_url_not_key(crawler):
    """D120: фетч берёт адрес из очереди дословно — там должен лежать адрес из <loc>."""
    rows = _parse(crawler, 'oilon_pro_iblock81.xml', 'https://oilon-pro.ru/sitemap-iblock-81.xml')
    queue = deque()
    asyncio.run(crawler._add_sitemap_urls_to_queue(queue, rows, 'https://oilon-pro.ru/'))
    queued = [item[0] for item in queue]
    assert queued, 'ни один URL карты сайта не попал в очередь'
    assert all(url.endswith('/') for url in queued)
    # ключи — в множествах дедупа, адреса — в очереди
    assert all(not key.endswith('/') for key in crawler.visited_urls)
    assert crawler.visited_urls & {crawler.url_normalizer.normalize_url(u) for u in queued}


def test_queue_keeps_all_p_permalinks(crawler):
    """D245-регресс: раньше 11 из 12 адресов гасились как «дубликаты» корня сайта."""
    rows = _parse(crawler, 'gomeloboi_p.xml', 'https://gomeloboi.by/sitemap.xml')
    queue = deque()
    added = asyncio.run(crawler._add_sitemap_urls_to_queue(queue, rows, 'https://gomeloboi.by/'))
    assert added == 12
    assert len({item[0] for item in queue}) == 12


def test_queue_skips_deep_pagination(crawler):
    """Предохранитель бюджета: глубокая пагинация отбраковывается URL целиком."""
    rows = [('https://a.ru/catalog/?page=999', 'https://a.ru/catalog?page=999', 'category', 9),
            ('https://a.ru/catalog/?page=2', 'https://a.ru/catalog?page=2', 'category', 9)]
    queue = deque()
    added = asyncio.run(crawler._add_sitemap_urls_to_queue(queue, rows, 'https://a.ru/'))
    assert added == 1
    assert [item[0] for item in queue] == ['https://a.ru/catalog/?page=2']


def test_published_url_remembered_for_retry(crawler):
    """D120 п.7а: карта помнит опубликованный адрес для повтора при no_content."""
    rows = _parse(crawler, 'byrpex_ru.xml', 'https://www.byrpex.com/sitemap.xml')
    queue = deque()
    asyncio.run(crawler._add_sitemap_urls_to_queue(queue, rows, 'https://www.byrpex.com/'))
    key = crawler.url_normalizer.normalize_url('https://www.byrpex.com/ru/info/interror')
    assert crawler._sitemap_published_urls[key] == 'https://www.byrpex.com/ru/info/interror'


# ==================== Стартовые URL ====================

def test_start_urls_have_slash_variant_first(crawler):
    """D192 (Egger): у каждого жёсткого пути есть слеш-вариант, и он идёт первым —
    дедуп очереди по ключу оставит именно каноническую форму."""
    urls = crawler._get_start_urls('https://www.egger-russia.ru/')
    assert 'https://www.egger-russia.ru/products/' in urls
    assert 'https://www.egger-russia.ru/products' in urls
    assert urls.index('https://www.egger-russia.ru/products/') < urls.index('https://www.egger-russia.ru/products')
    # корень сайта не обрастает вторым слешем
    assert 'https://www.egger-russia.ru//' not in urls
