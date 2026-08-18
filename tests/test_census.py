"""Тесты census-коллектора и метрик профилирования."""
import json

import pytest
from bs4 import BeautifulSoup

from site_profiles import profile_from_dict
from site_profiles.census_collector import CensusCollector, dom_skeleton_hash
from site_profiles.profile_metrics import RunMetricsCollector


def _filled_metrics():
    metrics = RunMetricsCollector('example.ru', 'Пример, ООО')
    for i in range(8):
        metrics.record_fetch(f'https://example.ru/catalog/p{i}/', 'product', 'http_first')
    metrics.record_fetch('https://example.ru/contacts/', 'contacts', 'dynamic')
    metrics.record_fetch_fail('https://example.ru/bad/', 'product')
    metrics.record_challenge('https://example.ru/catalog/p1/')
    metrics.record_warmup(True)
    metrics.record_sitemap(120)
    metrics.record_pages(
        [{'url': f'https://example.ru/catalog/p{i}/', 'category': 'product'} for i in range(8)]
        + [{'url': 'https://example.ru/catalog/', 'category': 'category'},
           {'url': 'https://example.ru/kontakty/', 'category': 'contacts'},
           {'url': 'https://example.ru/dealers/', 'category': 'distributor'}])
    for i in range(8):
        metrics.sink('extraction', url=f'https://example.ru/catalog/p{i}/', page_type='',
                     path='generic', md_len=5000, structure_hash='abc')
        metrics.sink('universal_detail', url=f'https://example.ru/catalog/p{i}/',
                     jsonld=(i % 2 == 0), cms_how='cms:bitrix')
    metrics.record_output({'products_processed': 8, 'products_below_min_specs': 2,
                           'cards_generated': 8, 'errors': ['x']})
    return metrics


def test_metrics_to_dict():
    data = _filled_metrics().to_dict()
    assert data['pages_crawled'] == 11
    assert data['pages_by_category']['product'] == 8
    assert data['product_cards'] == 8
    assert data['fetch_methods']['http_first'] == 8
    assert data['fetch_fail_rate'] == pytest.approx(0.1)
    assert data['challenge_rate'] == pytest.approx(0.1)
    assert data['gate3_pass_rate'] == pytest.approx(0.8)
    assert data['jsonld_share'] == pytest.approx(0.5)
    assert data['selector_hit_rate'] == pytest.approx(1.0)
    assert data['extraction_path_share']['generic'] == pytest.approx(1.0)
    assert data['structure_hash'] == 'abc'
    assert data['sitemap_url_count'] == 120


def test_metrics_fail_open():
    """Кривые аргументы телеметрии не бросают исключений (fail-open)."""
    metrics = RunMetricsCollector('example.ru')
    metrics.record_fetch(None, None, None)
    metrics.record_pages(None)
    metrics.record_pages([{'no_url': 1}])
    metrics.sink('extraction', md_len='не число')
    metrics.record_output(None)
    metrics.to_dict()  # свод тоже не падает


def test_alerts_against_baseline():
    metrics = _filled_metrics()
    metrics.baseline = {'product_cards': 100, 'sitemap_url_count': 500,
                        'structure_hash': 'другая-вёрстка'}
    alerts = metrics.to_dict()['alerts']
    joined = ' | '.join(alerts)
    assert 'product_cards' in joined
    assert 'sitemap diff' in joined
    assert 'structure_hash' in joined


def test_metrics_write(tmp_path):
    path = _filled_metrics().write(str(tmp_path))
    data = json.loads(open(path, encoding='utf-8').read())
    assert data['domain'] == 'example.ru'
    assert 'alerts' in data


# ==================== CensusCollector ====================

def test_census_draft_fields():
    census = CensusCollector(_filled_metrics())
    draft = census.build_draft()
    assert draft['domain'] == 'example.ru'
    assert draft['source'] == 'census'
    assert 0 <= draft['confidence'] <= 1
    assert draft['crawl']['render'] == 'html'          # http_first доминирует
    assert draft['sections']['product_url_patterns']   # 8 товарных URL по шаблону
    assert draft['sections']['contacts_urls'] == ['/kontakty/']
    assert draft['baseline']['product_cards'] == 8
    assert draft['census_evidence']['top_urls']['product']
    # черновик проходит валидацию схемы профиля
    _, errors = profile_from_dict(draft)
    assert errors == []


def test_census_keeps_custom_tier():
    profile, _ = profile_from_dict({'domain': 'example.ru',
                                    'extract': {'tier': 'custom', 'custom_module': 'x_ru'}})
    census = CensusCollector(_filled_metrics(), profile=profile)
    draft = census.build_draft()
    assert draft['extract'] == {'tier': 'custom', 'custom_module': 'x_ru'}
    assert draft['profile_version'] == 2


def test_census_write_draft_and_review(tmp_path):
    metrics = RunMetricsCollector('empty.ru')   # пустые метрики -> низкая confidence
    census = CensusCollector(metrics)
    path = census.write_draft(str(tmp_path))
    assert path.endswith('empty.ru.yaml')
    assert (tmp_path / '_review_queue.md').exists()


# ==================== dom_skeleton_hash ====================

def test_dom_skeleton_hash_stability():
    html1 = '<html><body><div class="card"><h1>Товар А</h1><p>текст 1</p></div></body></html>'
    html2 = '<html><body><div class="card"><h1>Товар Б</h1><p>совсем другой текст</p></div></body></html>'
    html3 = '<html><body><div class="totally-new-layout"><span>x</span></div></body></html>'
    h1 = dom_skeleton_hash(BeautifulSoup(html1, 'lxml'))
    h2 = dom_skeleton_hash(BeautifulSoup(html2, 'lxml'))
    h3 = dom_skeleton_hash(BeautifulSoup(html3, 'lxml'))
    assert h1 == h2          # смена текста не меняет скелет
    assert h1 != h3          # смена вёрстки меняет
