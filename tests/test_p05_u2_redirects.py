"""P05 U2: редиректы и смена хоста при выборе базы обхода.

Без сети: эквивалентность хостов и мобильные зеркала, детектор редирект-шимов и парковок,
заголовки пробы, путь Site_list в _get_start_urls, сверка хоста в не-товарной ветке фетча,
canonical с чужим хостом, website компании на чужом домене, счётчики периметра в метриках.
"""
import asyncio
import types

import pytest
from bs4 import BeautifulSoup

import main
from config import Config
from domain_equivalency import (DomainEquivalencyManager, HttpAnswer, desktop_host,
                                hosts_equivalent, is_mobile_mirror, redirect_shim_target,
                                registrable_domain, same_registrable_domain)
from site_profiles.profile_metrics import RunMetricsCollector
from web_crawler import SmartURLNormalizer, URLCategorizer, WebCrawler

META_SHIM = ('<html><head><meta http-equiv="Refresh" content="1; url=https://vostokcement.ru">'
             '</head><body><a href="https://vostokcement.ru">You will be redirected now...</a>'
             '</body></html>')
JS_SHIM = ("<script language='javascript' type='text/javascript'>setTimeout( "
           "'location=\"http://www.aviasales.ru/?marker=35544.tormax.ru\";', 0 );</script>")
LIVE_BODY = ('<html><body><a href="/catalog/">Каталог</a>' + 'т' * 400 + '</body></html>')


@pytest.fixture
def manager():
    return DomainEquivalencyManager(Config())


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _fake_http(responses):
    async def probe(variant, verify_tls, use_range):
        answer = responses.get((variant, verify_tls))
        if answer is None:
            return HttpAnswer(None, '', variant, 'отказ соединения: тест', False)
        return answer
    return probe


# ==================== Эквивалентность хостов и мобильные зеркала ====================

@pytest.mark.parametrize('host, expected', [
    ('m.rmz.by', 'rmz.by'),
    ('mobile.a.ru', 'a.ru'),
    ('touch.a.ru', 'a.ru'),
    ('www.m.a.ru', 'a.ru'),
    ('mail.a.ru', 'mail.a.ru'),     # не мобильный префикс
])
def test_desktop_host(host, expected):
    assert desktop_host(host) == expected


def test_mobile_mirror_equivalence():
    assert hosts_equivalent('m.rmz.by', 'rmz.by')
    assert is_mobile_mirror('m.rmz.by', 'rmz.by')
    assert not is_mobile_mirror('rmz.by', 'rmz.by')
    assert not hosts_equivalent('power.dkc.ru', 'www.dkc.ru')


@pytest.mark.parametrize('host, expected', [
    ('www.tulasm.ru', 'tulasm.ru'),
    ('vh444.timeweb.ru', 'timeweb.ru'),
    ('rmz.by', 'rmz.by'),
    ('localhost', 'localhost'),
])
def test_registrable_domain(host, expected):
    assert registrable_domain(host) == expected


def test_same_registrable_domain():
    assert same_registrable_domain('https://m.rmz.by/', 'https://rmz.by/')
    assert not same_registrable_domain('https://vh444.timeweb.ru/parking/', 'https://www.tulasm.ru/')


# ==================== Редирект-шимы и парковки ====================

def test_meta_refresh_shim_detected():
    assert redirect_shim_target(META_SHIM, 2000) == 'https://vostokcement.ru'


def test_js_shim_detected():
    assert redirect_shim_target(JS_SHIM, 2000) == 'http://www.aviasales.ru/?marker=35544.tormax.ru'


def test_long_page_with_meta_refresh_is_not_shim():
    """Живой сайт с meta refresh в шапке шимом не считается — гейт по длине тела."""
    body = META_SHIM + 'т' * 5000
    assert redirect_shim_target(body, 2000) is None


def test_shim_rejects_candidate_with_status(manager):
    reason, status = manager._content_verdict(META_SHIM, 'https://www.parkgroup.ru')
    assert status == 'redirect_shim' and 'vostokcement.ru' in reason


def test_parking_host_status(manager):
    assert manager._foreign_host_status(
        'https://vh444.timeweb.ru/parking/?ref=www.tulasm.ru') == 'parked_domain'
    assert manager._foreign_host_status('https://aviasales.ru/?marker=1') == 'foreign_host'


# ==================== Финальный URL после редиректа ====================

def test_cross_host_redirect_not_adopted(manager):
    """Парковка хостера не становится базой обхода; фолбэк — адрес из Site_list (D167)."""
    parking = HttpAnswer(200, LIVE_BODY, 'https://vh444.timeweb.ru/parking/?ref=www.tulasm.ru',
                         None, False)
    responses = {(variant, True): parking for variant in
                 ('https://www.tulasm.ru', 'http://www.tulasm.ru',
                  'https://tulasm.ru', 'http://tulasm.ru')}
    manager._probe_http = _fake_http(responses)
    # фолбэк — адрес из Site_list как он записан в файле (со слэшем)
    assert _run(manager.find_working_url('https://www.tulasm.ru/')) == 'https://www.tulasm.ru/'
    assert manager.last_site_status == 'parked_domain'


def test_mobile_mirror_rolls_back_to_desktop(manager):
    """UA-редирект на m.<домен> подтверждает кандидата, но базой остаётся десктоп (D272)."""
    responses = {('https://rmz.by', True): HttpAnswer(200, LIVE_BODY, 'https://m.rmz.by/',
                                                      None, False)}
    manager._probe_http = _fake_http(responses)
    assert _run(manager.find_working_url('https://rmz.by/')) == 'https://rmz.by'


def test_www_redirect_is_accepted(manager):
    """Штатная канонизация апекс -> www базу обхода не ломает."""
    responses = {('https://a.ru', True): HttpAnswer(200, LIVE_BODY, 'https://www.a.ru/',
                                                    None, False)}
    manager._probe_http = _fake_http(responses)
    assert _run(manager.find_working_url('https://a.ru')) == 'https://www.a.ru/'


def test_probe_headers_are_ru_desktop(manager):
    headers = manager._probe_headers()
    assert headers['Accept-Language'] == 'ru-RU,ru;q=0.9'
    assert headers['sec-ch-ua-mobile'] == '?0'
    assert 'Windows NT' in headers['User-Agent']


# ==================== Путь Site_list в стартовых URL ====================

def _bare_crawler(profile=None):
    crawler = WebCrawler.__new__(WebCrawler)
    crawler.profile = profile
    crawler.url_categorizer = URLCategorizer(Config())
    return crawler


def test_start_urls_keep_sitelist_path():
    urls = WebCrawler._get_start_urls(_bare_crawler(), 'https://www.dkc.ru/ru/')
    assert urls[0] == 'https://www.dkc.ru/ru'
    assert 'https://www.dkc.ru/ru/catalog' in urls
    # ревью слияния F2: корень сайта при базе с локалью не теряется
    assert 'https://www.dkc.ru/' in urls


def test_start_urls_without_path_unchanged():
    urls = WebCrawler._get_start_urls(_bare_crawler(), 'https://a.ru/')
    assert urls[0] == 'https://a.ru'
    assert 'https://a.ru/catalog' in urls


def test_start_urls_redirect_path_is_entry_point_not_base():
    """Ревью слияния F2: рабочий URL после 301 на /home/ — точка входа, а не база разделов."""
    urls = WebCrawler._get_start_urls(_bare_crawler(), 'https://a.ru/home/')
    assert urls[0] == 'https://a.ru/home/'
    assert 'https://a.ru/catalog' in urls and 'https://a.ru/home/catalog' not in urls
    assert 'https://a.ru' in urls


def test_start_urls_profile_sections_are_root_relative():
    """Ревью слияния F2: пути секций профиля клеятся от корня даже при базе с локалью."""
    from site_profiles.models import SiteProfile
    profile = SiteProfile(domain='a.ru')
    profile.sections.catalog_roots = ['/produkcija/']
    urls = WebCrawler._get_start_urls(_bare_crawler(profile), 'https://a.ru/ru/')
    assert 'https://a.ru/produkcija/' in urls
    assert 'https://a.ru/ru/produkcija/' not in urls


# ==================== canonical с чужим хостом ====================

@pytest.fixture
def normalizer():
    return SmartURLNormalizer(URLCategorizer(Config()))


def test_canonical_from_foreign_host_dropped(normalizer):
    soup = BeautifulSoup('<link rel="canonical" href="https://rmz.by/catalog/x/">', 'html.parser')
    current = 'https://other-site.ru/catalog/x/'
    assert normalizer.extract_canonical_url(soup, current) == normalizer.normalize_url(current)


def test_canonical_from_www_accepted(normalizer):
    soup = BeautifulSoup('<link rel="canonical" href="https://www.a.ru/catalog/x/">', 'html.parser')
    result = normalizer.extract_canonical_url(soup, 'https://a.ru/catalog/x/')
    assert result == normalizer.normalize_url('https://www.a.ru/catalog/x/')


def test_og_url_without_host_dropped(normalizer):
    soup = BeautifulSoup('<meta property="og:url" content="/catalog/x/">', 'html.parser')
    current = 'https://a.ru/catalog/x/'
    # относительный og:url резолвится от текущего адреса и остаётся своим хостом
    assert normalizer.extract_canonical_url(soup, current) == normalizer.normalize_url(current)


# ==================== Сверка хоста в не-товарной ветке фетча ====================

def test_non_product_branch_rejects_foreign_final_url(monkeypatch):
    """JS-редиректор: Playwright ушёл на чужой хост — страница не сохраняется (D238)."""
    config = Config()
    config.http_first_enabled = False
    crawler = WebCrawler.__new__(WebCrawler)
    crawler.config = config
    crawler.profile = None
    crawler.host_throttle = None
    crawler.metrics_collector = None
    crawler.current_base_url = 'http://tormax.ru'
    crawler.url_categorizer = URLCategorizer(config)
    crawler.url_normalizer = SmartURLNormalizer(crawler.url_categorizer)
    crawler.permanent_errors_cache = set()
    crawler._errors_cache_lock = asyncio.Lock()

    async def no_aiohttp(url):
        return None

    async def playwright(url):
        return '<html><body>чужой сайт</body></html>', False, 'https://www.aviasales.ru/?marker=1'

    crawler._fetch_with_aiohttp = no_aiohttp
    crawler._fetch_with_playwright = playwright
    crawler._census_fetch = lambda *a, **kw: None
    result = _run(WebCrawler._fetch_page_content(crawler, 'http://tormax.ru/dealers', 'contacts'))
    assert result is None


# ==================== website компании на чужом домене ====================

def test_company_website_not_overwritten_by_foreign_host():
    assert not same_registrable_domain('https://vh444.timeweb.ru/parking/', 'https://www.tulasm.ru/')
    # смена www -> апекс того же домена по-прежнему разрешена
    assert same_registrable_domain('http://cemtorg.com/', 'http://www.cemtorg.com/')


def test_main_imports_registrable_check():
    assert main.same_registrable_domain is same_registrable_domain


# ==================== Счётчики периметра в метриках ====================

def test_perimeter_alert_on_skipped_links():
    collector = RunMetricsCollector('rmz.by')
    collector.record_link_gate(found=900, skipped=850)
    metrics = collector.to_dict()
    assert metrics['links_found'] == 900 and metrics['links_skipped_perimeter'] == 850
    assert any('гейт периметра' in a for a in metrics['alerts'])


def test_sitemap_queue_alert():
    collector = RunMetricsCollector('rmz.by')
    collector.record_sitemap_queue(queued=0, filtered=553)
    metrics = collector.to_dict()
    assert any('из карты сайта в очередь добавлено 0' in a for a in metrics['alerts'])


def test_perimeter_counters_silent_without_calls():
    metrics = RunMetricsCollector('a.ru').to_dict()
    assert metrics['links_found'] == 0 and metrics['sitemap_queued'] is None
    assert not any('периметра' in a for a in metrics['alerts'])
