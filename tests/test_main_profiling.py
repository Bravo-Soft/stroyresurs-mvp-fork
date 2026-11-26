"""Интеграция обвязки профилирования в main.py: _setup_profiling / _finalize_profiling /
_resolve_streaming_mode без запуска пайплайна (методы вызываются на заглушке self)."""
import types

import pytest

import text_extractor
from config import Config
from main import MonitoringSystem, CompanyStatistics
from site_profiles import reset_resolver
from site_profiles.resolver import DEFAULT_PROFILES_DIR


@pytest.fixture
def system_stub(tmp_path):
    """Минимальный self для методов MonitoringSystem + чистый резолвер."""
    reset_resolver()
    config = Config()
    config.profiles_dir = str(DEFAULT_PROFILES_DIR)
    config.profiles_drafts_dir = str(tmp_path / 'drafts')
    config.profile_metrics_dir = str(tmp_path / 'metrics')
    config.census_enabled = True
    stub = types.SimpleNamespace(config=config,
                                 crawler=types.SimpleNamespace(metrics_collector=None))
    yield stub
    text_extractor.set_census_sink(None)
    reset_resolver()


def test_setup_and_finalize_census(system_stub, tmp_path):
    company = {'website': 'https://example-census.ru/', 'original_name': 'Тест, ООО'}
    MonitoringSystem._setup_profiling(system_stub, company)
    metrics = system_stub._profile_metrics
    assert metrics is not None
    assert system_stub.crawler.metrics_collector is metrics
    assert system_stub._company_profile is None  # профиля для домена нет

    # телеметрия извлечения идёт через sink text_extractor
    text_extractor.html_to_markdown(
        '<html><body><article><h1>Товар</h1><p>' + 'т' * 300 + '</p></article></body></html>',
        'https://example-census.ru/catalog/p1/')
    assert sum(metrics.extraction_paths.values()) >= 1

    crawl_result = {'stored_pages': [
        {'url': 'https://example-census.ru/catalog/p1/', 'category': 'product'},
        {'url': 'https://example-census.ru/contacts/', 'category': 'contacts'},
    ]}
    stats = CompanyStatistics(company)
    stats.products_processed = 3
    stats.cards_generated = 3
    MonitoringSystem._finalize_profiling(system_stub, crawl_result, stats)

    metric_files = list((tmp_path / 'metrics' / 'example-census.ru').glob('*.json'))
    assert len(metric_files) == 1
    draft = tmp_path / 'drafts' / 'example-census.ru.yaml'
    assert draft.exists()
    assert system_stub._profile_metrics is None
    assert text_extractor._census_sink is None


def test_setup_resolves_repo_profile(system_stub):
    """Для домена с профилем в репозитории профиль подхватывается (chelaz: streaming=false)."""
    company = {'website': 'https://chelaz.ru/', 'original_name': 'ЧЕЛАЗ'}
    MonitoringSystem._setup_profiling(system_stub, company)
    profile = system_stub._company_profile
    assert profile is not None and profile.domain == 'chelaz.ru'
    assert profile.extract.streaming is False

    # _resolve_streaming_mode уважает профиль (не доходя до хардкода chelaz)
    system_stub.config.pipeline_streaming_enabled = True
    system_stub._load_cached_actual_name = lambda cd: 'ЧЕЛАЗ ООО'
    use_streaming, cached = MonitoringSystem._resolve_streaming_mode(system_stub, company)
    assert use_streaming is False
    assert cached == 'ЧЕЛАЗ ООО'


def test_finalize_without_setup_is_noop(system_stub):
    stats = CompanyStatistics({'website': 'https://x.ru/', 'original_name': 'X'})
    MonitoringSystem._finalize_profiling(system_stub, None, stats)  # не падает


def test_record_specs_yields_avg_specs_and_card_yield(system_stub, tmp_path):
    """W1: avg_specs_per_card (по товарам, прошедшим гейт >=3 ТХ) и card_yield
    (карточки / товарные страницы) попадают в JSON метрик и в baseline черновика."""
    import json
    company = {'website': 'https://example-w1.ru/', 'original_name': 'W1, ООО'}
    MonitoringSystem._setup_profiling(system_stub, company)
    metrics = system_stub._profile_metrics
    metrics.record_specs(7, True)
    metrics.record_specs(4, True)
    metrics.record_specs(1, False)      # не прошёл гейт — в среднее не входит
    metrics.record_specs('bad', True)   # fail-open: телеметрия не падает
    crawl_result = {'stored_pages': [
        {'url': f'https://example-w1.ru/catalog/p{i}/', 'category': 'product'} for i in range(4)
    ] + [{'url': 'https://example-w1.ru/contacts/', 'category': 'contacts'}]}
    stats = CompanyStatistics(company)
    stats.products_processed = 2
    stats.products_below_min_specs = 1
    stats.cards_generated = 2
    MonitoringSystem._finalize_profiling(system_stub, crawl_result, stats)

    metric_file = next((tmp_path / 'metrics' / 'example-w1.ru').glob('*.json'))
    data = json.loads(metric_file.read_text(encoding='utf-8'))
    assert data['avg_specs_per_card'] == 5.5
    assert data['card_yield'] == 0.5
    assert data['gate3_pass_rate'] == round(2 / 3, 4)
    assert not any(a.startswith('card_yield') for a in data['alerts'])
    assert not any(a.startswith('avg_specs') for a in data['alerts'])

    import yaml
    draft = yaml.safe_load((tmp_path / 'drafts' / 'example-w1.ru.yaml').read_text(encoding='utf-8'))
    assert draft['baseline']['avg_specs_per_card'] == 5.5
    assert draft['baseline']['card_yield'] == 0.5


def test_record_specs_alerts_when_low():
    from site_profiles.profile_metrics import RunMetricsCollector
    m = RunMetricsCollector('low.ru')
    m.record_specs(3, True)
    m.record_pages([{'url': f'https://low.ru/p{i}', 'category': 'product'} for i in range(10)])
    m.record_output({'cards_generated': 1})
    data = m.to_dict()
    assert data['card_yield'] == 0.1 and data['avg_specs_per_card'] == 3
    assert any(a.startswith('card_yield 0.1 < 0.35') for a in data['alerts'])
    assert any(a.startswith('avg_specs_per_card 3') for a in data['alerts'])
    # без товаров и карточек — None, без ложных алертов
    empty = RunMetricsCollector('empty.ru').to_dict()
    assert empty['avg_specs_per_card'] is None and empty['card_yield'] is None
