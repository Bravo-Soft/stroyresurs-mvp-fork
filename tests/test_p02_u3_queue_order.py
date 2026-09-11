"""P02 U3: порядок очереди из карты сайта и приоритет.

Коды: D208 (инверсия appendleft), D144 (кэп в порядке документа, до категоризации).

Офлайн-симуляция по диагнозу batch_22/_diag_1059.json (Veka, ordinal 1059): в карте
сайта 478 адресов /partners/news/ стоят в начале, а товарные /production/... — в хвосте.
Прежний код брал первые 500 адресов в порядке документа и раскладывал их appendleft в
цикле, поэтому обход начинался с новости с самым низким рангом, а все товарные адреса
оказывались на позициях 474-479 и до бюджета max_pages_per_site не доживали.

Фикстура tests/fixtures/sitemaps/veka_news_first.xml — срез живой карты www.veka.ru
(снята 11.09.2026): 1 главная + 25 новостей + 6 товарных адресов в хвосте.
"""
import asyncio
from collections import deque
from pathlib import Path

import pytest

from config import Config
from web_crawler import WebCrawler

FIXTURES = Path(__file__).parent / 'fixtures' / 'sitemaps'


@pytest.fixture
def crawler(tmp_path):
    config = Config()
    config.base_dir = str(tmp_path)
    return WebCrawler(config)


def _veka_rows(crawler):
    content = (FIXTURES / 'veka_news_first.xml').read_text(encoding='utf-8')
    return asyncio.run(crawler.sitemap_parser._parse_xml_sitemap(
        content, 'https://www.veka.ru/sitemap.xml', 0, 1))


def _fill(crawler, rows, base='https://www.veka.ru/'):
    queue = deque()
    asyncio.run(crawler._add_sitemap_urls_to_queue(queue, rows, base))
    return queue


# ==================== D208: порядок обхода ====================

def test_first_popleft_is_top_priority_product(crawler):
    """Регресс-инвариант D208: первый popleft — товарная страница с максимальным
    приоритетом, а не новость из головы карты сайта."""
    queue = _fill(crawler, _veka_rows(crawler))
    url, depth, category, priority = queue.popleft()
    assert category == 'product', f'в голове очереди {category}: {url}'
    assert priority == max(item[3] for item in queue) or not queue
    assert '/production' in url


def test_news_never_precede_products(crawler):
    """Ни один новостной адрес не стоит в очереди раньше первого товарного."""
    queue = _fill(crawler, _veka_rows(crawler))
    order = [item[0] for item in queue]
    first_product = min(i for i, item in enumerate(queue) if item[2] == 'product')
    assert not any('/partners/news/' in url for url in order[:first_product])


def test_queue_order_matches_rank_order(crawler):
    """Блок кладётся в голову целиком: порядок popleft совпадает с порядком ранжирования."""
    rows = [('https://a.ru/catalog/beton-m300', 'https://a.ru/catalog/beton-m300', 'product', 10),
            ('https://a.ru/catalog/', 'https://a.ru/catalog', 'category', 9),
            ('https://a.ru/contacts/', 'https://a.ru/contacts', 'contacts', 8)]
    queue = _fill(crawler, list(rows), 'https://a.ru/')
    assert [item[0] for item in queue] == [row[0] for row in rows]


def test_existing_queue_tail_survives(crawler):
    """Блок карты сайта встаёт в голову, уже стоящие в очереди адреса остаются в хвосте."""
    queue = deque([('https://a.ru/start', 0, 'main_page', 5)])
    rows = [('https://a.ru/catalog/x1', 'https://a.ru/catalog/x1', 'product', 10)]
    asyncio.run(crawler._add_sitemap_urls_to_queue(queue, rows, 'https://a.ru/'))
    assert [item[0] for item in queue] == ['https://a.ru/catalog/x1', 'https://a.ru/start']


# ==================== D208: снятие насыщения приоритета ====================

def test_priority_not_saturated_in_parser(crawler):
    """Товар, дистрибьютор и главная больше не схлопываются в один приоритет 10."""
    rows = _veka_rows(crawler)
    by_category = {}
    for url, key, category, priority in rows:
        by_category.setdefault(category, set()).add(priority)
    assert by_category['product'] == {10}
    assert by_category['distributor'] == {8}
    assert max(by_category['product']) > max(by_category['distributor'])


def test_priority_not_saturated_in_queue(crawler):
    """После постановки в очередь различие приоритетов сохраняется (пересортировка
    главного цикла остаётся осмысленной)."""
    queue = _fill(crawler, _veka_rows(crawler))
    priorities = {item[2]: item[3] for item in queue}
    assert priorities['product'] == 13      # 9 (категоризатор) + 1 (карта) + 3 (очередь)
    assert priorities['distributor'] == 11  # 7 + 1 + 3


# ==================== D144: ранжирование до среза ====================

def test_rank_before_cut_keeps_tail_products(crawler):
    """Кэп sitemap_max_urls применяется после категоризации: товарные адреса из хвоста
    карты выживают, хотя в порядке документа они стоят за 25 новостями."""
    crawler.config.sitemap_max_urls = 8
    rows = _veka_rows(crawler)
    assert len(rows) == 8
    assert all(row[2] == 'product' for row in rows[:6])
    assert 'https://www.veka.ru/production/special/profilnye-sistemy-veka/veka-artline' in [r[0] for r in rows]


def test_info_segments_demoted_within_category(crawler):
    """Ключ ранжирования опускает info-разделы ниже при равном приоритете."""
    rank = crawler.sitemap_parser._sitemap_rank_key
    product = ('https://a.ru/catalog/beton-m300', 'https://a.ru/catalog/beton-m300', 'product', 10)
    news = ('https://a.ru/news/beton-m300', 'https://a.ru/news/beton-m300', 'product', 10)
    assert rank(product) < rank(news)


def test_rank_key_is_deterministic(crawler):
    """D164/D240: ранжирование не зависит от порядка прихода — сортировка устойчива
    и полностью определена адресом при прочих равных."""
    rank = crawler.sitemap_parser._sitemap_rank_key
    rows = [('https://a.ru/catalog/b', 'https://a.ru/catalog/b', 'product', 10),
            ('https://a.ru/catalog/a', 'https://a.ru/catalog/a', 'product', 10)]
    assert sorted(rows, key=rank) == sorted(reversed(rows), key=rank)


def test_scan_ceiling_reported(crawler, caplog):
    """Потолок sitemap_max_scan_urls ограничивает разбор и виден в логе."""
    crawler.config.sitemap_max_scan_urls = 5
    with caplog.at_level('WARNING'):
        rows = _veka_rows(crawler)
    assert len(rows) <= 5
    assert any('sitemap_max_scan_urls' in record.message for record in caplog.records)
