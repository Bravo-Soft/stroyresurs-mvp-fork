"""P01 U4: санация href и <loc> до склейки.

Коды: D257 (пробельные края в href — 403 на «испорченном адресе»), D233 (в путь ссылки
вклеен ещё один абсолютный URL: глубина считается по мангленному пути, и шлюз очереди
выбрасывает всю навигацию), D151 (<loc> с пустым хостом занимает товарную квоту).

Фикстура tests/fixtures/sitemaps/electroservis_relative_loc.xml — срез живой карты
electroservis.ru (№1437, снята 11.09.2026): все <loc> записаны относительными путями
без схемы и хоста. Сети тесты не требуют.
"""
import asyncio
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from config import Config
from web_crawler import WebCrawler, sanitize_href

FIXTURES = Path(__file__).parent / 'fixtures' / 'sitemaps'


@pytest.fixture
def crawler(tmp_path):
    config = Config()
    config.base_dir = str(tmp_path)
    crawler = WebCrawler(config)
    crawler.current_base_url = 'https://plastrub.ru/'
    return crawler


# ==================== D257: пробельные края ====================

def test_whitespace_trimmed():
    assert sanitize_href(' /catalog/truba-pnd/ ') == '/catalog/truba-pnd/'
    assert sanitize_href('\n/catalog/truba-pnd/\t') == '/catalog/truba-pnd/'


def test_normalizer_trims_url(crawler):
    """Ключ дедупликации не должен расходиться из-за хвостового пробела."""
    normalize = crawler.url_normalizer.normalize_url
    assert normalize('https://plastrub.ru/catalog/truba ') == normalize('https://plastrub.ru/catalog/truba')


def test_clean_href_untouched():
    """Регресс: нормальный адрес санитайзер не трогает."""
    for href in ('/catalog/x/', 'https://a.ru/catalog/x/', '//cdn.a.ru/x.js', '../x', '#tab'):
        assert sanitize_href(href) == href


# ==================== D233: вклеенный в путь абсолютный URL ====================

def test_glued_absolute_url_in_path_cut():
    assert sanitize_href('/catalog/https:/titmgn.ru/tovar-1') == 'https://titmgn.ru/tovar-1'
    assert sanitize_href('https://titmgn.ru/catalog/https://titmgn.ru/tovar-1') == 'https://titmgn.ru/tovar-1'


def test_last_occurrence_wins():
    """Тройное вклеивание сводится к последнему адресу."""
    mangled = '/a/http:/b.ru/c/https:/titmgn.ru/tovar-1'
    assert sanitize_href(mangled) == 'https://titmgn.ru/tovar-1'


def test_absolute_url_in_query_untouched():
    """Ограничение эвристики: в query абсолютный адрес легален (редиректоры, share)."""
    href = 'https://a.ru/go?url=https://b.ru/page'
    assert sanitize_href(href) == href
    assert sanitize_href('/share?to=http://vk.com/x') == '/share?to=http://vk.com/x'


def test_depth_counted_by_sanitized_path(crawler):
    """D233: глубина считается по санированному пути, а не по мангленному."""
    depth_before = crawler.url_categorizer.calculate_url_depth(
        'https://a.ru/catalog/https:/titmgn.ru/tovar-1')
    depth_after = crawler.url_categorizer.calculate_url_depth(
        sanitize_href('/catalog/https:/titmgn.ru/tovar-1'))
    assert depth_before > depth_after == 1


def test_links_extracted_from_mangled_href(crawler):
    """Сквозная проверка: ссылка с вклеенным адресом резолвится в нормальный URL."""
    html = ('<html><body>'
            '<a href=" /catalog/truba-pnd/ ">Труба ПНД</a>'
            '<a href="/catalog/https:/plastrub.ru/catalog/mufta-1">Муфта</a>'
            '</body></html>')
    links = asyncio.run(crawler._extract_additional_links(
        BeautifulSoup(html, 'html.parser'), 'https://plastrub.ru/catalog/'))
    urls = {url for url, _category, _priority in links}
    assert 'https://plastrub.ru/catalog/truba-pnd/' in urls
    assert 'https://plastrub.ru/catalog/mufta-1' in urls
    assert not any(' ' in url for url in urls)


def test_mangled_href_metric_recorded(crawler):
    """Счётчик mangled_href_rate в метриках прогона (fail-open)."""
    from site_profiles.profile_metrics import RunMetricsCollector
    crawler.metrics_collector = RunMetricsCollector('plastrub.ru')
    html = ('<html><body><a href=" /a/ ">1</a>'
            '<a href="/b/https:/plastrub.ru/c">2</a><a href="/d/">3</a></body></html>')
    asyncio.run(crawler._extract_additional_links(
        BeautifulSoup(html, 'html.parser'), 'https://plastrub.ru/'))
    metrics = crawler.metrics_collector.to_dict()
    assert metrics['mangled_href_kinds'] == {'whitespace': 1, 'glued': 1}
    assert metrics['mangled_href_rate'] == round(2 / 3, 4)


# ==================== D151: <loc> без хоста ====================

def test_repair_empty_netloc_bitrix(crawler):
    """Bitrix с пустым SERVER_NAME: 'https:///catalog/…' чинится по адресу карты."""
    repair = crawler.sitemap_parser._repair_loc
    assert repair('https:///catalog/gate_valves/364/',
                  'https://kpp33.ru/sitemap.xml') == 'https://kpp33.ru/catalog/gate_valves/364/'


def test_repair_relative_loc(crawler):
    repair = crawler.sitemap_parser._repair_loc
    assert repair('/catalog/zhbi/', 'https://kpp33.ru/sitemap.xml') == 'https://kpp33.ru/catalog/zhbi/'


def test_repair_keeps_absolute_loc(crawler):
    repair = crawler.sitemap_parser._repair_loc
    assert repair('https://kpp33.ru/catalog/zhbi/',
                  'https://kpp33.ru/sitemap.xml') == 'https://kpp33.ru/catalog/zhbi/'


def test_unrepairable_loc_dropped(crawler):
    assert crawler.sitemap_parser._repair_loc('', 'https://kpp33.ru/sitemap.xml') is None


def test_relative_locs_repaired_in_sitemap(crawler):
    """D151 (Рязанский завод кабельной арматуры, №1437): 3988 <loc> записаны без хоста;
    раньше все они шли в очередь как 'https:///catalog/…' и жгли товарную квоту."""
    content = (FIXTURES / 'electroservis_relative_loc.xml').read_text(encoding='utf-8')
    rows = asyncio.run(crawler.sitemap_parser._parse_xml_sitemap(
        content, 'https://electroservis.ru/sitemap.xml', 0, 1))
    assert rows
    assert all(row[0].startswith('https://electroservis.ru/catalog/') for row in rows)
    assert not any(row[0].startswith('https:///') for row in rows)
