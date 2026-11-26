"""Тесты crawl-роутера: профиль управляет категоризацией URL (sections/exclude_paths),
пер-хостовым троттлингом; без профиля поведение не меняется."""
import asyncio
import time

import pytest

from config import Config
from site_profiles import profile_from_dict
from site_profiles.host_throttle import HostThrottle
from web_crawler import URLCategorizer


@pytest.fixture
def categorizer():
    return URLCategorizer(Config())


def _profile(domain='example.ru', **sections_or_crawl):
    data = {'domain': domain}
    data.update(sections_or_crawl)
    profile, errors = profile_from_dict(data)
    assert errors == [], errors
    return profile


# ==================== Без профиля == старое поведение ====================

def test_no_profile_baseline(categorizer):
    """Снимок категоризации без профиля — контрольные точки старого поведения."""
    assert categorizer.categorize_url('https://a.ru/product/x1')[0] == 'product'
    assert categorizer.categorize_url('https://a.ru/contacts/')[0] == 'contacts'
    assert categorizer.categorize_url('https://a.ru/news/2020/')[0] == 'excluded2'
    assert categorizer.categorize_url('https://a.ru/')[0] == 'main_page'


def test_set_profile_none_is_noop(categorizer):
    categorizer.set_profile(None)
    assert categorizer.categorize_url('https://a.ru/product/x1')[0] == 'product'


# ==================== sections профиля ====================

def test_profile_exclude_paths(categorizer):
    categorizer.set_profile(_profile(crawl={'exclude_paths': ['/practice/']}))
    assert categorizer.categorize_url('https://a.ru/practice/article-1/')[0] == 'excluded'
    assert categorizer.categorize_url('https://a.ru/product/x1')[0] == 'product'


def test_profile_product_patterns_and_antipatterns(categorizer):
    categorizer.set_profile(_profile(sections={
        'product_url_patterns': [r'/wares/[a-z0-9-]+/$'],
        'product_url_antipatterns': [r'/product/catalog-page/'],
    }))
    # паттерн: URL, который эвристика не считала товаром, стал товаром
    assert categorizer.categorize_url('https://a.ru/wares/beton-m300/')[0] == 'product'
    # антипаттерн: перекрывает и паттерн, и балльную эвристику
    category, _ = categorizer.categorize_url('https://a.ru/product/catalog-page/x1')
    assert category != 'product'


def test_profile_section_urls(categorizer):
    categorizer.set_profile(_profile(sections={
        'contacts_urls': ['/o-firme/rekvizity/'],
        'distributor_urls': ['/set-prodazh/'],
        'certificates_urls': ['/sertifikaty/'],
        'documents_urls': ['/dokumentacija/'],
        'instructions_urls': ['/instrukcii/'],
        'price_list_urls': ['/stoimost-produkcii/'],
        'catalog_roots': ['/produkcija/'],
    }))
    assert categorizer.categorize_url('https://a.ru/o-firme/rekvizity/')[0] == 'contacts'
    assert categorizer.categorize_url('https://a.ru/set-prodazh/moskva/')[0] == 'distributor'
    # 4 блока общих документов: certificates/documents/instructions -> other с приоритетом
    for doc_path in ('/sertifikaty/', '/dokumentacija/', '/instrukcii/'):
        category, priority = categorizer.categorize_url(f'https://a.ru{doc_path}')
        assert category == 'other' and priority >= 7, doc_path
    assert categorizer.categorize_url('https://a.ru/stoimost-produkcii/')[0] == 'price_list'
    # корень каталога — category; подстраницы корня идут обычной классификацией
    assert categorizer.categorize_url('https://a.ru/produkcija/')[0] == 'category'
    assert categorizer.categorize_url('https://a.ru/produkcija/product/x1')[0] == 'product'


def test_profile_sections_beat_global_exclude(categorizer):
    """Назначение sections — дотянуться до страниц, которые глобальные эвристики режут."""
    # '/media/' — в глобальном exclude; профиль объявляет там документы
    categorizer.set_profile(_profile(sections={'documents_urls': ['/media/sertifikaty/']}))
    assert categorizer.categorize_url('https://a.ru/media/sertifikaty/')[0] == 'other'
    categorizer.set_profile(None)
    assert categorizer.categorize_url('https://a.ru/media/sertifikaty/')[0] == 'excluded2'


def test_overlapping_prefixes_longest_wins(categorizer):
    """/contacts/branches/ = дилеры, хотя /contacts/ объявлен контактами."""
    categorizer.set_profile(_profile(sections={
        'contacts_urls': ['/contacts/'],
        'distributor_urls': ['/contacts/branches/'],
    }))
    assert categorizer.categorize_url('https://a.ru/contacts/')[0] == 'contacts'
    assert categorizer.categorize_url('https://a.ru/contacts/requisites/')[0] == 'contacts'
    assert categorizer.categorize_url('https://a.ru/contacts/branches/')[0] == 'distributor'
    assert categorizer.categorize_url('https://a.ru/contacts/branches/moskva/')[0] == 'distributor'


def test_belcolor_repo_profile(categorizer):
    """Функциональная проверка принятого профиля belcolor.ru (правки оператора)."""
    from site_profiles import ProfileResolver
    from site_profiles.resolver import DEFAULT_PROFILES_DIR
    profile = ProfileResolver(DEFAULT_PROFILES_DIR).resolve('https://www.belcolor.ru/')
    assert not profile.is_default()
    categorizer.set_profile(profile)
    cat = lambda u: categorizer.categorize_url(u)[0]
    # дилеры: филиалы/представительства
    assert cat('https://www.belcolor.ru/contacts/branches/') == 'distributor'
    # контакты: реквизиты, контакты, index_area.php
    assert cat('https://www.belcolor.ru/company/requisites/') == 'contacts'
    assert cat('https://www.belcolor.ru/contacts/') == 'contacts'
    assert cat('http://belcolor.ru/company/index_area.php') == 'contacts'
    # лишние страницы исключены из обхода
    for path in ('/company/partners/postavshchiki-materialov/basf.html',
                 '/company/history/2002.html', '/company/clients',
                 '/company/o-nas', '/company/o-nas/index_area.php', '/company/managment'):
        assert cat(f'http://belcolor.ru{path}') == 'excluded', path
    # документация: /services/ и комплекты документов (категория other, приоритет >= 7)
    for path in ('/services/', '/services/pasporta-bezopasnosti/',
                 '/services/deklaratsii-sootvetstviya/',
                 '/services/svidetelstva-o-gosregistratsii/',
                 '/services/informatsiya-o-sout/'):
        category, priority = categorizer.categorize_url(f'https://www.belcolor.ru{path}')
        assert category == 'other' and priority >= 7, path
    # товары не задеты
    assert cat('http://belcolor.ru/catalog/khv-16/16sn50.html') == 'product'


# ==================== HostThrottle ====================

def test_host_throttle_delczero_noop():
    throttle = HostThrottle()

    async def run():
        t0 = time.monotonic()
        for _ in range(5):
            await throttle.acquire('a.ru', 0)
        return time.monotonic() - t0

    assert asyncio.run(run()) < 0.05


def test_host_throttle_enforces_interval():
    throttle = HostThrottle()

    async def run():
        t0 = time.monotonic()
        for _ in range(3):
            await throttle.acquire('a.ru', 100)  # 100 мс
        return time.monotonic() - t0

    elapsed = asyncio.run(run())
    assert elapsed >= 0.2  # два интервала после первого запроса


def test_host_throttle_channels_and_hosts_independent():
    throttle = HostThrottle()

    async def run():
        t0 = time.monotonic()
        await throttle.acquire('a.ru', 200, 'pages')
        await throttle.acquire('b.ru', 200, 'pages')   # другой хост — без ожидания
        await throttle.acquire('a.ru', 200, 'files')   # другой канал — без ожидания
        return time.monotonic() - t0

    assert asyncio.run(run()) < 0.1
