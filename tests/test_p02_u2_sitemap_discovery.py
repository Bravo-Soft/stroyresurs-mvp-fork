"""P02 U2: обнаружение и разбор карт сайта.

Коды: D214 (пропущенная запятая в sitemap_paths, HEAD без редиректов), D154 (жёсткий
срез [:5] подкарт индекса), D196 (gzip), D164/D240 (недетерминированный порядок карт,
чужой хост, мультилокальные карты, кэп по объединённому списку), D143 (карта чужого
домена), плюс P04 U2 п.7 (роль other при пустой товарной части карты).

Сетевые ветки (редирект на HEAD, 405, .gz) проверяются на локальном aiohttp-сервере,
который поднимается на 127.0.0.1 с произвольным портом — внешней сети тесты не требуют.
"""
import asyncio
import gzip
from collections import deque
from pathlib import Path

import pytest
from aiohttp import web

from config import Config
from web_crawler import WebCrawler

FIXTURES = Path(__file__).parent / 'fixtures' / 'sitemaps'
URLSET = ('<?xml version="1.0" encoding="UTF-8"?>'
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
          '<url><loc>https://a.ru/catalog/beton-m300/</loc></url>'
          '<url><loc>https://a.ru/catalog/beton-m400/</loc></url>'
          '</urlset>')


@pytest.fixture
def crawler(tmp_path):
    config = Config()
    config.base_dir = str(tmp_path)
    return WebCrawler(config)


async def _serve(routes):
    """Локальный HTTP-сервер на 127.0.0.1 с произвольным портом."""
    app = web.Application()
    for path, handler in routes.items():
        app.router.add_route('*', path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    host, port = runner.addresses[0][:2]
    return runner, f'http://{host}:{port}'


def _with_server(routes, body):
    async def scenario():
        runner, base = await _serve(routes)
        try:
            return await body(base)
        finally:
            await runner.cleanup()
    return asyncio.run(scenario())


# ==================== D214: полнота путей и проверка существования ====================

def test_sitemap_paths_have_no_glued_literal(crawler):
    """D214: пропущенная запятая склеивала '/sitemap1.xml' и '/rss.xml' в один путь."""
    paths = crawler.sitemap_parser.sitemap_paths
    assert '/sitemap1.xml' in paths
    assert '/rss.xml' in paths
    assert '/sitemap1.xml/rss.xml' not in paths


def test_wordpress_sitemap_paths_present(crawler):
    """Штатные пути WordPress >= 5.5 и индексов добавлены."""
    paths = crawler.sitemap_parser.sitemap_paths
    for path in ('/wp-sitemap.xml', '/sitemap-index.xml', '/wp-sitemap-index.xml'):
        assert path in paths


def test_head_redirect_gives_effective_url(crawler):
    """D214: 301 на живую карту раньше читался как «карты нет»."""
    async def moved(request):
        raise web.HTTPMovedPermanently('/real-sitemap.xml')

    async def real(request):
        return web.Response(text=URLSET, content_type='application/xml')

    result = _with_server(
        {'/sitemap.xml': moved, '/real-sitemap.xml': real},
        lambda base: crawler.sitemap_parser._check_sitemap_exists(base + '/sitemap.xml'))
    assert result.endswith('/real-sitemap.xml')


def test_head_405_falls_back_to_get(crawler):
    """Часть хостеров отвечает на HEAD 405 — карта всё равно должна быть найдена."""
    async def handler(request):
        if request.method == 'HEAD':
            raise web.HTTPMethodNotAllowed('HEAD', ['GET'])
        return web.Response(text=URLSET, content_type='application/xml')

    result = _with_server({'/sitemap.xml': handler},
                          lambda base: crawler.sitemap_parser._check_sitemap_exists(base + '/sitemap.xml'))
    assert result and result.endswith('/sitemap.xml')


def test_missing_sitemap_is_none(crawler):
    """Регресс: отсутствующая карта по-прежнему не считается найденной."""
    async def missing(request):
        raise web.HTTPNotFound()

    result = _with_server({'/sitemap.xml': missing},
                          lambda base: crawler.sitemap_parser._check_sitemap_exists(base + '/sitemap.xml'))
    assert result is None


# ==================== D196: gzip ====================

def test_decode_gzip_by_signature(crawler):
    """Тело распаковывается по сигнатуре 0x1f 0x8b, даже если адрес без .gz."""
    raw = gzip.compress(URLSET.encode('utf-8'))
    assert crawler.sitemap_parser._decode_sitemap_body(raw, 'https://a.ru/sitemap.xml') == URLSET


def test_decode_plain_body(crawler):
    """Обычная карта читается как прежде."""
    assert crawler.sitemap_parser._decode_sitemap_body(URLSET.encode('utf-8'),
                                                       'https://a.ru/sitemap.xml') == URLSET


def test_broken_gz_is_none(crawler):
    assert crawler.sitemap_parser._decode_sitemap_body(b'not a gzip', 'https://a.ru/sitemap.xml.gz') is None


def test_gz_sitemap_parsed_end_to_end(crawler):
    """D196: .gz-карта разбирается целиком, а не валит парсер на первом байте."""
    async def gz(request):
        return web.Response(body=gzip.compress(URLSET.encode('utf-8')),
                            content_type='application/octet-stream')

    rows = _with_server({'/sitemap.xml.gz': gz},
                        lambda base: crawler.sitemap_parser.parse_sitemap(base + '/sitemap.xml.gz'))
    assert [row[0] for row in rows] == ['https://a.ru/catalog/beton-m300/',
                                        'https://a.ru/catalog/beton-m400/']


def test_broken_gz_falls_back_to_plain_address(crawler):
    """При неудаче .gz пробуется тот же адрес без расширения."""
    async def broken(request):
        return web.Response(body=b'not a gzip at all', content_type='application/octet-stream')

    async def plain(request):
        return web.Response(text=URLSET, content_type='application/xml')

    rows = _with_server({'/sitemap.xml.gz': broken, '/sitemap.xml': plain},
                        lambda base: crawler.sitemap_parser.parse_sitemap(base + '/sitemap.xml.gz'))
    assert len(rows) == 2


# ==================== D154: индекс подкарт ====================

def test_index_submaps_all_parsed_by_default(crawler):
    """D154: срез [:5] снят — из индекса brozex.ru (12 подкарт) разбираются все."""
    parsed = []

    async def fake_parse(loc, depth=0, max_depth=3):
        parsed.append(loc)
        return []

    crawler.sitemap_parser.parse_sitemap = fake_parse
    content = (FIXTURES / 'brozex_index.xml').read_text(encoding='utf-8')
    asyncio.run(crawler.sitemap_parser._parse_xml_sitemap(content, 'https://brozex.ru/sitemap.xml', 0, 3))
    assert len(parsed) == 12


def test_index_limit_prefers_product_submaps(crawler):
    """При ненулевом sitemap_max_submaps первыми берутся товарные карты."""
    parsed = []

    async def fake_parse(loc, depth=0, max_depth=3):
        parsed.append(loc)
        return []

    crawler.sitemap_parser.parse_sitemap = fake_parse
    crawler.config.sitemap_max_submaps = 2
    content = ('<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               '<sitemap><loc>https://a.ru/sitemap-news.xml</loc></sitemap>'
               '<sitemap><loc>https://a.ru/sitemap-attachment.xml</loc></sitemap>'
               '<sitemap><loc>https://a.ru/sitemap-catalog.xml</loc></sitemap>'
               '<sitemap><loc>https://a.ru/sitemap-iblock-81.xml</loc></sitemap>'
               '</sitemapindex>')
    asyncio.run(crawler.sitemap_parser._parse_xml_sitemap(content, 'https://a.ru/sitemap.xml', 0, 3))
    assert sorted(parsed) == ['https://a.ru/sitemap-catalog.xml', 'https://a.ru/sitemap-iblock-81.xml']


# ==================== D143/D164: принадлежность и детерминизм ====================

def _stub_discovery(crawler, standard=(), robots=(), html=()):
    async def check(url):
        return url if url in standard else None

    async def from_robots(robots_url):
        return list(robots)

    async def from_html(base_url):
        return list(html)

    crawler.sitemap_parser._check_sitemap_exists = check
    crawler.sitemap_parser._extract_sitemaps_from_robots = from_robots
    crawler.sitemap_parser._find_sitemap_links_in_html = from_html


def test_foreign_host_sitemap_dropped(crawler):
    """D143: директива Sitemap: на чужой registrable-домен отбрасывается."""
    _stub_discovery(crawler,
                    standard={'https://a.ru/sitemap.xml'},
                    robots=['https://dorway.example/sitemap.xml', 'https://a.ru/sitemap-catalog.xml'])
    found = asyncio.run(crawler.sitemap_parser.discover_sitemap_urls('https://a.ru/'))
    assert 'https://dorway.example/sitemap.xml' not in found
    assert 'https://a.ru/sitemap-catalog.xml' in found


def test_holding_domain_sitemap_kept(crawler):
    """Домены холдинга из config.equivalent_domains остаются разрешёнными."""
    crawler.config.equivalent_domains = {'vmp-holding.ru': ['vmp-anticor.ru']}
    _stub_discovery(crawler, robots=['https://vmp-anticor.ru/sitemap.xml'])
    found = asyncio.run(crawler.sitemap_parser.discover_sitemap_urls('https://vmp-holding.ru/'))
    assert found == ['https://vmp-anticor.ru/sitemap.xml']


def test_ownership_filter_fails_open_without_equivalency(crawler):
    """Fail-open: при выключенной эквивалентности доменов фильтр не отбрасывает
    собственные карты (is_main_domain в этом режиме возвращает None)."""
    crawler.config.domain_equivalency_enabled = False
    _stub_discovery(crawler, robots=['https://a.ru/sitemap.xml', 'https://dorway.example/sitemap.xml'])
    found = asyncio.run(crawler.sitemap_parser.discover_sitemap_urls('https://a.ru/'))
    assert found == ['https://a.ru/sitemap.xml']


def test_discovery_order_is_deterministic_and_product_first(crawler):
    """D164/D240: порядок карт больше не задаёт хеш-таблица; товарная карта первая."""
    _stub_discovery(crawler, robots=['https://a.ru/sitemap-attachment.xml',
                                     'https://a.ru/sitemap-catalog.xml',
                                     'https://a.ru/sitemap.xml'])
    first = asyncio.run(crawler.sitemap_parser.discover_sitemap_urls('https://a.ru/'))
    second = asyncio.run(crawler.sitemap_parser.discover_sitemap_urls('https://a.ru/'))
    assert first == second
    assert first[0] == 'https://a.ru/sitemap-catalog.xml'
    assert first[-1] == 'https://a.ru/sitemap-attachment.xml'


# ==================== D240: локаль и кэп по картам ====================

def test_locale_prefix_detected(crawler):
    assert crawler._sitemap_locale_prefix('https://www.dormakaba.com/ru-ru') == 'ru-ru'
    assert crawler._sitemap_locale_prefix('https://byrpex.com/ru/') == 'ru'
    assert crawler._sitemap_locale_prefix('https://a.ru/') is None
    assert crawler._sitemap_locale_prefix('https://a.ru/catalog/') is None


def test_foreign_locale_url_filtered(crawler):
    assert crawler._url_has_locale('https://x.com/ru-ru/products/', 'ru-ru') is True
    assert crawler._url_has_locale('https://x.com/de-de/products/', 'ru-ru') is False
    assert crawler._url_has_locale('https://x.com/', 'ru-ru') is True
    # обычный раздел без локали не отсекается
    assert crawler._url_has_locale('https://x.com/products/', 'ru-ru') is True


def test_cap_is_shared_between_maps(crawler):
    """D144/D240: кэп квотируется по картам — большая карта не съедает его целиком."""
    big = [(f'https://a.ru/news/{i}', f'https://a.ru/news/{i}', 'category', 9) for i in range(50)]
    small = [('https://a.ru/catalog/x1', 'https://a.ru/catalog/x1', 'product', 10)]
    merged = crawler._merge_sitemap_maps([big, small], 4)
    assert len(merged) == 4
    assert ('https://a.ru/catalog/x1', 'https://a.ru/catalog/x1', 'product', 10) in merged


def test_merge_keeps_order_within_map(crawler):
    rows = [('https://a.ru/1', 'https://a.ru/1', 'product', 10),
            ('https://a.ru/2', 'https://a.ru/2', 'product', 10)]
    assert crawler._merge_sitemap_maps([rows], 10) == rows


# ==================== Сквозная фаза карты ====================

def _stub_maps(crawler, maps):
    """maps: {адрес карты: [четвёрки]}."""
    async def discover(base_url):
        return list(maps)

    async def parse(sitemap_url, depth=0, max_depth=3):
        return maps[sitemap_url]

    crawler.sitemap_parser.discover_sitemap_urls = discover
    crawler.sitemap_parser.parse_sitemap = parse


def test_locale_filter_applied_before_cap(crawler):
    """D240: у компании с локалью в Site_list чужие локали до кэпа не доходят."""
    rows = [('https://x.com/ru-ru/products/p1', 'https://x.com/ru-ru/products/p1', 'product', 10),
            ('https://x.com/de-de/products/p1', 'https://x.com/de-de/products/p1', 'product', 10),
            ('https://x.com/en-us/products/p1', 'https://x.com/en-us/products/p1', 'product', 10)]
    _stub_maps(crawler, {'https://x.com/sitemap.xml': rows})
    result = asyncio.run(crawler._discover_and_parse_sitemap('https://www.dormakaba.com/ru-ru'))
    assert [row[0] for row in result] == ['https://x.com/ru-ru/products/p1']


def test_other_role_kept_when_no_products(crawler):
    """P04 U2 п.7: карта без товарных адресов не выбрасывается целиком —
    служебные адреса ставятся с низким приоритетом."""
    rows = [('https://a.ru/o-nas', 'https://a.ru/o-nas', 'other', 1),
            ('https://a.ru/uslugi', 'https://a.ru/uslugi', 'other', 1)]
    _stub_maps(crawler, {'https://a.ru/sitemap.xml': rows})
    result = asyncio.run(crawler._discover_and_parse_sitemap('https://a.ru/'))
    assert len(result) == 2
    assert all(row[3] == 1 for row in result)


def test_other_role_dropped_when_products_present(crawler):
    """Регресс: при живой товарной части карты служебные адреса по-прежнему отсекаются."""
    rows = [('https://a.ru/catalog/x1', 'https://a.ru/catalog/x1', 'product', 10),
            ('https://a.ru/o-nas', 'https://a.ru/o-nas', 'other', 1)]
    _stub_maps(crawler, {'https://a.ru/sitemap.xml': rows})
    result = asyncio.run(crawler._discover_and_parse_sitemap('https://a.ru/'))
    assert [row[0] for row in result] == ['https://a.ru/catalog/x1']
