"""P05 U3: периметр обхода — поддомены того же корневого домена, бренд-домены группы,
техдомен хостера.

Коды: D124 (навигация сайта ведёт на технический хост хостера), D131 (каталог на
продуктовом поддомене), D224 (каталог на sibling-доменах холдинга), D201 (домены брендов).

Сети нет: единственный сетевой вызов автодетекта (проба главной кандидата) подменяется
заглушкой.
"""
import asyncio
from collections import deque

import pytest
from bs4 import BeautifulSoup

from config import Config
from site_profiles import SiteProfile
from web_crawler import ParseResult, WebCrawler


@pytest.fixture
def crawler(tmp_path):
    config = Config()
    config.base_dir = str(tmp_path)
    crawler = WebCrawler(config)
    crawler.current_base_url = 'https://a.ru/'
    return crawler


def _page(links, html='<html><head><title>Компания А</title></head><body></body></html>'):
    return ParseResult(BeautifulSoup(html, 'html.parser'), html, list(links), 'other', {})


def _extract(crawler, links, depth=0, base='https://a.ru/', page='https://a.ru/'):
    return asyncio.run(crawler._extract_links(page, _page(links), depth, base_url=base))


def _apply_profile(crawler, monkeypatch, profile):
    class _Resolver:
        def resolve(self, url):
            return profile
    monkeypatch.setattr('web_crawler._get_profile_resolver', lambda: _Resolver())
    crawler._apply_site_profile('https://a.ru/')


# ==================== Матрица эквивалентности (п.7 состава изменения) ====================

@pytest.mark.parametrize('link, expected', [
    ('https://a.ru/catalog/x', True),            # апекс
    ('https://www.a.ru/catalog/x', True),        # www
    ('https://m.a.ru/catalog/x', True),          # мобильное зеркало (хвост P05 U2)
    ('https://touch.a.ru/catalog/x', True),
    ('https://shop.a.ru/catalog/x', False),      # поддомен — без разрешения не периметр
    ('https://a-shop.ru/catalog/x', False),      # sibling-домен
    ('https://drugoy.ru/catalog/x', False),      # чужой хост
])
def test_perimeter_matrix_without_profile(crawler, link, expected):
    assert bool(crawler._in_crawl_perimeter(link, 'https://a.ru/')) is expected


def test_idn_forms_in_perimeter(crawler):
    assert crawler._in_crawl_perimeter('https://крышев.рф/catalog/',
                                       'https://xn--b1afoy4br.xn--p1ai/') is True


# ==================== Декларативный путь: периметр из профиля ====================

def test_profile_equivalent_domains_extend_perimeter(crawler, monkeypatch):
    """D224: бренд-домены холдинга из профиля попадают в периметр (раньше поле читал
    только резолвер профиля, а гейт периметра — нет)."""
    profile = SiteProfile(domain='sst.ru')
    profile.crawl.equivalent_domains = ['warm-on.ru', 'sst-em.ru']
    crawler.current_base_url = 'https://sst.ru/'
    assert not crawler._in_crawl_perimeter('https://warm-on.ru/catalog/x', 'https://sst.ru/')
    _apply_profile(crawler, monkeypatch, profile)
    assert crawler._in_crawl_perimeter('https://warm-on.ru/catalog/x', 'https://sst.ru/') is True
    assert crawler._in_crawl_perimeter('https://sst-em.ru/catalog/', 'https://sst.ru/') is True
    assert not crawler._in_crawl_perimeter('https://drugoy.ru/x', 'https://sst.ru/')


def test_profile_hoster_mirror_in_perimeter(crawler, monkeypatch):
    """D124: технический хост хостера объявлен зеркалом декларативно."""
    profile = SiteProfile(domain='gorodproekt.ru')
    profile.crawl.equivalent_domains = ['jully84.beget.tech']
    crawler.current_base_url = 'https://gorodproekt.ru/'
    _apply_profile(crawler, monkeypatch, profile)
    assert crawler._in_crawl_perimeter('http://jully84.beget.tech/catalog/',
                                       'https://gorodproekt.ru/') is True


def test_profile_subdomain_collapse_in_perimeter(crawler, monkeypatch):
    """D131: crawl.subdomain_collapse ('*.gexa.ru') разрешает продуктовые поддомены."""
    profile = SiteProfile(domain='gexa.ru')
    profile.crawl.subdomain_collapse = ['*.gexa.ru']
    crawler.current_base_url = 'https://gexa.ru/'
    _apply_profile(crawler, monkeypatch, profile)
    assert crawler._in_crawl_perimeter('https://isospan.gexa.ru/catalog/',
                                       'https://gexa.ru/') is True
    assert not crawler._in_crawl_perimeter('https://isospan.drugoy.ru/', 'https://gexa.ru/')


def test_profile_perimeter_does_not_leak(crawler, monkeypatch):
    """Профиль прошлой компании не расширяет периметр следующей."""
    profile = SiteProfile(domain='sst.ru')
    profile.crawl.equivalent_domains = ['warm-on.ru']
    _apply_profile(crawler, monkeypatch, profile)
    _apply_profile(crawler, monkeypatch, SiteProfile.default('a.ru'))
    assert not crawler._in_crawl_perimeter('https://warm-on.ru/x', 'https://sst.ru/')


def test_config_equivalent_domains_still_work(crawler):
    """Регресс: группа config.equivalent_domains (ВМП) работает как прежде."""
    crawler.config.equivalent_domains = {'vmp-holding.ru': ['vmp-anticor.ru']}
    assert crawler._in_crawl_perimeter('https://vmp-anticor.ru/catalog/',
                                       'https://vmp-holding.ru/') is True


# ==================== Поддомены того же корневого домена ====================

def test_subdomain_allowed_from_main_page_navigation(crawler):
    """D131: ссылка на продуктовый поддомен стоит в навигации главной (глубина 0)."""
    links = _extract(crawler, [('https://flowsolutions.a.ru/', 'main_page', 5)])
    assert [link for link, _c, _p in links] == ['https://flowsolutions.a.ru/']
    assert crawler._allowed_hosts == {'flowsolutions.a.ru'}


def test_subdomain_allowed_for_catalog_url_deeper(crawler):
    """Вторая половина правила: URL категоризуется как каталожный/товарный."""
    links = _extract(crawler, [('https://shop.a.ru/catalog/plita', 'product', 9)], depth=3)
    assert len(links) == 1
    assert crawler._allowed_hosts == {'shop.a.ru'}


def test_non_catalog_subdomain_deeper_rejected(crawler):
    """Не с главной и не каталожная ссылка поддомен не открывает."""
    links = _extract(crawler, [('https://hr.a.ru/vacancy', 'other', 1)], depth=2)
    assert links == []
    assert crawler._allowed_hosts == set()


def test_foreign_registrable_domain_never_allowed(crawler):
    """Риск D114: чужой корневой домен правилом поддоменов не открывается."""
    links = _extract(crawler, [('https://drugoy.ru/catalog/plita', 'product', 9)])
    assert links == []
    assert crawler._allowed_hosts == set()


def test_subdomain_limit_respected(crawler):
    """Потолок config.max_allowed_subdomains: третий поддомен уже не разрешается."""
    crawler.config.max_allowed_subdomains = 2
    links = _extract(crawler, [('https://s1.a.ru/', 'main_page', 5),
                               ('https://s2.a.ru/', 'main_page', 5),
                               ('https://s3.a.ru/', 'main_page', 5)])
    assert len(crawler._allowed_hosts) == 2
    assert len(links) == 2


def test_service_subdomain_does_not_burn_the_limit(crawler):
    """Живая проба gexa.ru: partner./tender. занимали оба слота, а продуктовый
    isospan.gexa.ru оставался за периметром. Служебные поддомены слот не занимают."""
    crawler.config.max_allowed_subdomains = 2
    links = _extract(crawler, [('https://partner.a.ru/', 'main_page', 5),
                               ('https://tender.a.ru/', 'main_page', 5),
                               ('https://isospan.a.ru/', 'main_page', 5)])
    assert crawler._allowed_hosts == {'isospan.a.ru'}
    assert [link for link, _c, _p in links] == ['https://isospan.a.ru/']


def test_service_subdomain_allowed_when_url_is_catalog(crawler):
    """Стоп-лист не сильнее прямого товарного признака URL."""
    links = _extract(crawler, [('https://media.a.ru/catalog/plita', 'product', 9)])
    assert len(links) == 1 and crawler._allowed_hosts == {'media.a.ru'}


def test_subdomain_allowance_can_be_disabled(crawler):
    """0 = прежнее поведение: поддомены отбрасываются как раньше."""
    crawler.config.max_allowed_subdomains = 0
    links = _extract(crawler, [('https://shop.a.ru/catalog/plita', 'product', 9)])
    assert links == []
    assert crawler._allowed_hosts == set()


def test_allowed_host_is_not_transitive(crawler):
    """Разрешённый хост сам никого не разрешает: его собственный поддомен вне периметра."""
    crawler._allowed_hosts.add('shop.a.ru')
    assert not crawler._in_crawl_perimeter('https://cdn.shop.a.ru/x', 'https://shop.a.ru/')


def test_should_skip_url_honours_allowlist(crawler):
    """Гейт снятия с очереди и гейт ссылок решают согласованно."""
    url = 'https://shop.a.ru/catalog/plita'
    assert asyncio.run(crawler._should_skip_url(url, 1, 'ООО Тест')) is True
    crawler._allowed_hosts.add('shop.a.ru')
    assert asyncio.run(crawler._should_skip_url(url, 1, 'ООО Тест')) is False


def test_sitemap_gate_honours_allowlist(crawler):
    """Риск из досье: карта фильтрует по is_subdomain, очередь — по is_main_domain."""
    url = 'https://shop.a.ru/catalog/plita'
    rows = [(url, crawler.url_normalizer.normalize_url(url), 'product', 8)]
    queue = deque()
    asyncio.run(crawler._add_sitemap_urls_to_queue(queue, list(rows), 'https://a.ru/'))
    assert list(queue) == []
    crawler._allowed_hosts.add('shop.a.ru')
    asyncio.run(crawler._add_sitemap_urls_to_queue(queue, list(rows), 'https://a.ru/'))
    assert [item[0] for item in queue] == [url]


# ==================== Диагностика: разрез по хостам и метрики ====================

def test_skipped_hosts_breakdown(crawler):
    """D124/D224: без разреза по хостам «каталог на другом домене» не виден в логе."""
    _extract(crawler, [('https://drugoy.ru/a', 'other', 1),
                       ('https://drugoy.ru/b', 'other', 1),
                       ('https://third.example/c', 'other', 1)], depth=2)
    assert crawler._skipped_link_hosts == {'drugoy.ru': 2, 'third.example': 1}


def test_record_link_gate_called(crawler):
    """Счётчики периметра полосы C наконец получают вызов."""
    class _Metrics:
        def __init__(self):
            self.calls = []

        def record_link_gate(self, found, skipped):
            self.calls.append((found, skipped))

    crawler.metrics_collector = _Metrics()
    _extract(crawler, [('https://a.ru/ok', 'other', 1),
                       ('https://drugoy.ru/x', 'other', 1)], depth=2)
    assert crawler.metrics_collector.calls == [(2, 1)]


# ==================== Автодетект зеркала (за флагом) ====================

MIRROR_HTML = '<html><head><title>Компания А</title></head><body></body></html>'
FOREIGN_HTML = '<html><head><title>Чужой сайт</title></head><body></body></html>'


def _mirror_links(host='jully84.beget.tech', count=6):
    return [(f'http://{host}/page{i}', 'other', 1) for i in range(count)]


def _stub_probe(crawler, body):
    calls = []

    async def fake(url):
        calls.append(url)
        return body
    crawler.url_categorizer.domain_equivalency.fetch_page_body = fake
    return calls


def test_autodetect_off_by_default(crawler):
    """Декларативный путь основной: без флага внешний хост не опрашивается вовсе."""
    calls = _stub_probe(crawler, MIRROR_HTML)
    links = _extract(crawler, _mirror_links())
    assert calls == [] and links == [] and crawler._allowed_hosts == set()


def test_autodetect_allows_confirmed_mirror(crawler):
    """D124: у корня внешнего хоста тот же title, что у главной компании."""
    crawler.config.foreign_host_autodetect = True
    calls = _stub_probe(crawler, MIRROR_HTML)
    links = _extract(crawler, _mirror_links())
    assert calls == ['http://jully84.beget.tech/']
    assert crawler._allowed_hosts == {'jully84.beget.tech'}
    assert len(links) == 6


def test_autodetect_rejects_foreign_site(crawler):
    """Риск D114: title не совпал — хост остаётся чужим."""
    crawler.config.foreign_host_autodetect = True
    _stub_probe(crawler, FOREIGN_HTML)
    links = _extract(crawler, _mirror_links(host='drugoy.ru'))
    assert links == [] and crawler._allowed_hosts == set()


def test_autodetect_needs_enough_links(crawler):
    """Одиночная внешняя ссылка хост не открывает и запроса не стоит."""
    crawler.config.foreign_host_autodetect = True
    calls = _stub_probe(crawler, MIRROR_HTML)
    links = _extract(crawler, _mirror_links(count=2))
    assert calls == [] and links == []


def test_autodetect_skipped_when_page_has_products(crawler):
    """Условие досье: автодетект только если товарных ссылок на главной нет."""
    crawler.config.foreign_host_autodetect = True
    calls = _stub_probe(crawler, MIRROR_HTML)
    _extract(crawler, _mirror_links() + [('https://a.ru/catalog/plita', 'product', 9)])
    assert calls == [] and crawler._allowed_hosts == set()


def test_autodetect_runs_once_per_company(crawler):
    """Потолок цены: не более одной пробы за компанию."""
    crawler.config.foreign_host_autodetect = True
    calls = _stub_probe(crawler, FOREIGN_HTML)
    _extract(crawler, _mirror_links(host='drugoy.ru'))
    _extract(crawler, _mirror_links(host='drugoy.ru'))
    assert len(calls) == 1


def test_autodetect_state_reset_between_companies(crawler):
    crawler._mirror_autodetect_done = True
    crawler._allowed_hosts.add('shop.a.ru')
    crawler._skipped_link_hosts['drugoy.ru'] = 3
    asyncio.run(crawler.reset_state('ООО Тест'))
    assert crawler._allowed_hosts == set()
    assert crawler._skipped_link_hosts == {}
    assert crawler._mirror_autodetect_done is False


# ==================== Порядок: профиль до выбора рабочего URL ====================

def test_profile_applied_before_working_url(crawler, monkeypatch):
    """P05 U3 п.1: профиль резолвится по исходному адресу компании и действует уже в
    момент выбора базы обхода (раньше применялся после него)."""
    profile = SiteProfile(domain='a.ru')
    profile.crawl.equivalent_domains = ['a-mirror.ru']

    class _Resolver:
        def __init__(self):
            self.urls = []

        def resolve(self, url):
            self.urls.append(url)
            return profile

    resolver = _Resolver()
    monkeypatch.setattr('web_crawler._get_profile_resolver', lambda: resolver)
    seen = {}

    async def fake_working_url(url):
        seen['profile'] = crawler.profile.domain if crawler.profile else None
        seen['perimeter'] = crawler.url_categorizer.is_main_domain(
            'https://a-mirror.ru/catalog/', 'https://a.ru/')
        return url

    async def noop(*args, **kwargs):
        return None

    crawler.site_status = None    # обычно выставляется внутри _get_working_url (полоса C)
    crawler._get_working_url = fake_working_url
    crawler._start_crawling_with_sitemap_support = noop
    crawler._save_crawling_stats = noop
    crawler.create_site_directories = lambda *a, **kw: {'base_domain_dir': crawler.config.base_dir}
    crawler._save_permanent_errors_cache = lambda: None

    asyncio.run(crawler.crawl_site('https://a.ru/', 'ООО Тест', crawler.config.base_dir))
    assert resolver.urls[0] == 'https://a.ru/'
    assert seen == {'profile': 'a.ru', 'perimeter': True}
