"""P01 U3: канонизация хоста и схемы (www / IDN / http->https) и учёт товарного бюджета.

Коды: D87 (www-форма теряется при склейке ссылок), D180 (IDN-хост неэквивалентен своей
punycode-форме), D270 (www-дубль занимает слот товарной квоты и умирает на 301),
D134 (собранные ссылки остаются на http, редирект ломает путь).

Сети нет: проверяются нормализация домена, приведение ссылок к рабочей базе и гейты очереди.
"""
import asyncio
import textwrap
from collections import deque

import pytest
from bs4 import BeautifulSoup

from config import Config
from domain_equivalency import DomainEquivalencyManager, to_ascii_host
from site_profiles import ProfileResolver, SiteProfile
from site_profiles.resolver import normalize_domain as profile_normalize_domain
from web_crawler import ParseResult, WebCrawler

# Крышев+ (№740): в Site_list punycode, в вёрстке — кириллица.
IDN_CYRILLIC = 'крышев.рф'
IDN_PUNYCODE = 'xn--b1afoy4br.xn--p1ai'


@pytest.fixture
def manager():
    return DomainEquivalencyManager(Config())


@pytest.fixture
def crawler(tmp_path):
    config = Config()
    config.base_dir = str(tmp_path)
    crawler = WebCrawler(config)
    crawler.current_base_url = 'https://a.ru/'
    return crawler


def _page(links):
    """ParseResult с готовым списком собранных ссылок."""
    return ParseResult(BeautifulSoup('<html></html>', 'html.parser'), '<html></html>',
                       list(links), 'other', {})


# ==================== D180: IDNA в normalize_domain ====================

@pytest.mark.parametrize('raw, expected', [
    (IDN_CYRILLIC, IDN_PUNYCODE),
    ('www.' + IDN_CYRILLIC, IDN_PUNYCODE),
    (IDN_PUNYCODE, IDN_PUNYCODE),
    ('КРЫШЕВ.РФ', IDN_PUNYCODE),
])
def test_idn_host_normalized_to_a_label(manager, raw, expected):
    assert manager.normalize_domain(raw) == expected


@pytest.mark.parametrize('raw, expected', [
    ('www.tizol.com', 'tizol.com'),          # регресс из досье
    ('TIZOL.com:8080', 'tizol.com'),
    ('nowww.ru', 'nowww.ru'),
    ('', ''),
])
def test_ascii_hosts_unchanged(manager, raw, expected):
    assert manager.normalize_domain(raw) == expected


def test_to_ascii_host_fails_open():
    """Неразбираемый хост возвращается как есть — гейт периметра не должен падать."""
    broken = 'x' * 70 + '.' + 'у' * 70 + '.рф'
    assert to_ascii_host(broken) == broken


def test_idn_forms_are_equivalent(manager):
    """D180: абсолютная внутренняя ссылка IDN-сайта больше не выглядит чужим хостом."""
    assert manager.are_domains_equivalent(f'https://{IDN_CYRILLIC}/catalog/plita',
                                          f'https://{IDN_PUNYCODE}/') is True


def test_idn_main_domain_gate(crawler):
    """Гейт периметра (is_main_domain) пропускает обе формы одного IDN-домена."""
    assert crawler.url_categorizer.is_main_domain(
        f'https://{IDN_CYRILLIC}/catalog/plita', f'https://{IDN_PUNYCODE}/') is True


def test_foreign_host_still_rejected(crawler):
    """Контроль: IDNA не расширяет периметр на чужие домены."""
    assert not crawler.url_categorizer.is_main_domain('https://drugoy.рф/x',
                                                      f'https://{IDN_PUNYCODE}/')


def test_profile_resolver_understands_both_idn_forms(tmp_path):
    """D180: профиль IDN-сайта резолвится и по punycode, и по кириллической форме."""
    (tmp_path / f'{IDN_PUNYCODE}.yaml').write_text(textwrap.dedent(f"""
        domain: {IDN_PUNYCODE}
        crawl:
          limits:
            pages: 42
    """), encoding='utf-8')
    resolver = ProfileResolver(tmp_path)
    assert resolver.resolve(f'https://{IDN_PUNYCODE}/').crawl.limits.pages == 42
    assert resolver.resolve(f'https://www.{IDN_CYRILLIC}/catalog/').crawl.limits.pages == 42


def test_profile_normalize_domain_regression():
    assert profile_normalize_domain('https://www.tizol.com/product/1') == 'tizol.com'
    assert profile_normalize_domain(f'//{IDN_CYRILLIC}/') == IDN_PUNYCODE


# ==================== Рабочая форма хоста и схемы ====================

def test_working_base_keeps_www(crawler):
    """D87: подтверждённая www-форма не срезается (нормализатор срезал бы её)."""
    crawler._set_working_base('https://www.pktmt.ru/')
    assert crawler._working_host == 'www.pktmt.ru'
    assert crawler._working_scheme == 'https'


def test_working_base_ignores_non_http(crawler):
    crawler._set_working_base('ftp://a.ru/')
    assert crawler._working_host == ''


def test_strict_www_domain_forces_www_base(crawler):
    """strict_www: даже если рабочий URL пришёл апексом, базой объявляется www-форма."""
    crawler.config.strict_www_domains = ['pktmt.ru']
    crawler._set_working_base('https://pktmt.ru/')
    assert crawler._working_host == 'www.pktmt.ru'


@pytest.mark.parametrize('working, link, expected', [
    # D87: база на www — апекс-ссылка приводится к www
    ('https://www.kraspan.ru/', 'https://kraspan.ru/catalog/x', 'https://www.kraspan.ru/catalog/x'),
    # D270: база на апексе — www-дубль не создаётся
    ('https://stscom.ru/', 'https://www.stscom.ru/sts/p1', 'https://stscom.ru/sts/p1'),
    # D134: http из вёрстки поднимается до подтверждённой https
    ('https://www.nzsm.ru/', 'http://www.nzsm.ru/products/3', 'https://www.nzsm.ru/products/3'),
    # обе правки сразу
    ('https://nzsm.ru/', 'http://www.nzsm.ru/products/3', 'https://nzsm.ru/products/3'),
    # сайт живёт только по http — https-ссылку опускаем до рабочей схемы
    ('http://baltur-russia.ru/', 'https://baltur-russia.ru/x', 'http://baltur-russia.ru/x'),
])
def test_link_canonicalized_to_working_base(crawler, working, link, expected):
    crawler._set_working_base(working)
    assert crawler._canonicalize_to_working_base(link) == expected


def test_path_query_and_fragment_preserved(crawler):
    """Приводится только хост и схема: путь со слешем, параметры и якорь не трогаются."""
    crawler._set_working_base('https://a.ru/')
    assert crawler._canonicalize_to_working_base(
        'http://www.a.ru/catalog/plita/?p=2&a=1#tab') == 'https://a.ru/catalog/plita/?p=2&a=1#tab'


@pytest.mark.parametrize('link', [
    'https://vmp-anticor.ru/catalog/x',   # домен группы холдинга — переход должен остаться
    'https://other-site.ru/x',            # чужой хост
    'https://shop.a.ru/x',                # поддомен — не форма того же хоста
    'http://a.ru:8080/x',                 # явный порт
    'mailto:info@a.ru',
])
def test_foreign_and_special_links_untouched(crawler, link):
    crawler.config.equivalent_domains = {'vmp-holding.ru': ['vmp-anticor.ru']}
    crawler._set_working_base('https://a.ru/')
    assert crawler._canonicalize_to_working_base(link) == link


def test_no_working_base_is_no_op(crawler):
    """Fail-open: рабочая база не зафиксирована — адреса не меняются."""
    assert crawler._canonicalize_to_working_base('http://www.a.ru/x') == 'http://www.a.ru/x'


def test_holding_domain_still_in_perimeter(crawler):
    """Канонизация не должна ломать переход между доменами холдинга."""
    crawler.config.equivalent_domains = {'vmp-holding.ru': ['vmp-anticor.ru']}
    crawler._set_working_base('https://vmp-holding.ru/')
    assert crawler.url_categorizer.is_main_domain('https://vmp-anticor.ru/catalog/x',
                                                  'https://vmp-holding.ru/') is True


# ==================== Ссылки со страниц ====================

def test_extract_links_returns_canonical_form(crawler):
    """D270: в очередь уходит рабочая форма, www-дубль не создаётся."""
    crawler._set_working_base('https://stscom.ru/')
    links = asyncio.run(crawler._extract_links(
        'https://stscom.ru/', _page([('https://www.stscom.ru/sts/p1', 'product', 5)]),
        0, base_url='https://stscom.ru/'))
    assert [link for link, _c, _p in links] == ['https://stscom.ru/sts/p1']


def test_extract_links_upgrades_scheme(crawler):
    """D134: собранная http-ссылка поднимается до подтверждённой https."""
    crawler._set_working_base('https://www.nzsm.ru/')
    links = asyncio.run(crawler._extract_links(
        'https://www.nzsm.ru/', _page([('http://www.nzsm.ru/products/3', 'product', 5)]),
        0, base_url='https://www.nzsm.ru/'))
    assert [link for link, _c, _p in links] == ['https://www.nzsm.ru/products/3']


def test_extract_links_dedups_two_host_forms(crawler):
    """D270: апекс и www одной страницы схлопываются в ОДИН адрес, и это рабочая форма
    (раньше выживала первая встреченная — у stscom.ru www-форма, отвечающая 301 на 404)."""
    crawler._set_working_base('https://stscom.ru/')
    links = asyncio.run(crawler._extract_links(
        'https://stscom.ru/', _page([('https://www.stscom.ru/sts/p1', 'product', 5),
                                     ('https://stscom.ru/sts/p1', 'product', 5)]),
        0, base_url='https://stscom.ru/'))
    assert [link for link, _c, _p in links] == ['https://stscom.ru/sts/p1']


# ==================== Адреса карты сайта ====================

def _sitemap_row(crawler, url, category='product', priority=8):
    return (url, crawler.url_normalizer.normalize_url(url), category, priority)


def test_sitemap_url_canonicalized_and_key_recomputed(crawler):
    """Хост/схема адреса карты приводятся к рабочей форме, путь остаётся опубликованным,
    ключ дедупа пересчитывается по фактически запрашиваемому адресу."""
    crawler._set_working_base('https://stscom.ru/')
    queue = deque()
    rows = [_sitemap_row(crawler, 'http://www.stscom.ru/sts/p1/')]
    asyncio.run(crawler._add_sitemap_urls_to_queue(queue, rows, 'https://stscom.ru/'))
    queued = [item[0] for item in queue]
    assert queued == ['https://stscom.ru/sts/p1/']
    assert crawler.product_urls == {crawler.url_normalizer.normalize_url(queued[0])}


def test_sitemap_published_path_not_touched(crawler):
    """Слеш и параметры опубликованного адреса карты сохраняются (регресс P01 U1)."""
    crawler._set_working_base('https://a.ru/')
    queue = deque()
    rows = [_sitemap_row(crawler, 'https://a.ru/catalog/plita/?id=7')]
    asyncio.run(crawler._add_sitemap_urls_to_queue(queue, rows, 'https://a.ru/'))
    assert [item[0] for item in queue] == ['https://a.ru/catalog/plita/?id=7']


# ==================== Товарный бюджет ====================

def test_quota_slot_released_on_foreign_host(crawler):
    """P01 U3 п.4: адрес, занявший слот квоты и отбитый гейтом чужого хоста, слот возвращает."""
    url = 'https://drugoy.ru/catalog/x1'
    key = crawler.url_normalizer.normalize_url(url)
    crawler.product_urls.add(key)
    skipped = asyncio.run(crawler._should_skip_url(url, 0, 'ООО Тест'))
    assert skipped is True
    assert key not in crawler.product_urls
    assert crawler._product_quota_reused == 1


def test_quota_slot_not_released_for_own_host(crawler):
    """Контроль: свой адрес гейтом периметра не отбивается и слот не трогает."""
    url = 'https://a.ru/catalog/x1'
    key = crawler.url_normalizer.normalize_url(url)
    crawler.product_urls.add(key)
    asyncio.run(crawler._should_skip_url(url, 0, 'ООО Тест'))
    assert key in crawler.product_urls


# ==================== Профильный рычаг strict_www ====================

def _stub_resolver(monkeypatch, profile):
    class _Resolver:
        def resolve(self, url):
            return profile
    monkeypatch.setattr('web_crawler._get_profile_resolver', lambda: _Resolver())


def test_profile_strict_www_applied(crawler, monkeypatch):
    """P01 U3 п.6: crawl.strict_www из профиля наконец читается краулером."""
    profile = SiteProfile(domain='kraspan.ru')
    profile.crawl.strict_www = True
    _stub_resolver(monkeypatch, profile)
    crawler._apply_site_profile('https://kraspan.ru/')
    assert crawler.url_categorizer.domain_equivalency.normalize_domain('kraspan.ru') == 'www.kraspan.ru'
    crawler._set_working_base('https://kraspan.ru/')
    assert crawler._working_host == 'www.kraspan.ru'


def test_profile_strict_www_does_not_leak_to_next_company(crawler, monkeypatch):
    profile = SiteProfile(domain='kraspan.ru')
    profile.crawl.strict_www = True
    _stub_resolver(monkeypatch, profile)
    crawler._apply_site_profile('https://kraspan.ru/')
    _stub_resolver(monkeypatch, SiteProfile.default('a.ru'))
    crawler._apply_site_profile('https://a.ru/')
    assert crawler.url_categorizer.domain_equivalency.profile_strict_www is None
    assert crawler.url_categorizer.domain_equivalency.normalize_domain('kraspan.ru') == 'kraspan.ru'
