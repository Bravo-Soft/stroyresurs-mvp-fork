"""Настройки сайта действуют без дублей в Config и переживают перепись."""
from dataclasses import fields

import pytest

from config import Config
from site_profiles import ProfileResolver, profile_from_dict
from site_profiles.census_collector import CensusCollector
from site_profiles.profile_metrics import RunMetricsCollector
from web_crawler import WebCrawler

DOMAINS = ('pspcom.ru', 'pktmt.ru', 'aerobel.ru', 'tizol.com', 'betar.ru', 'vmp-holding.ru')
KEYS = ('ajax_tabs', 'bitrix_offers', 'exclude_paths', 'strict_www',
        'equivalent_domains', 'tab_selector', 'tab_panel_selector')


def test_config_has_only_global_settings():
    assert not {f.name for f in fields(Config)}.intersection({
        'ajax_product_tabs_domains', 'bitrix_offers_domains',
        'exclude_url_patterns_by_domain', 'strict_www_domains', 'equivalent_domains'})
    assert '.product-page__section-content' not in Config().dynamic_tab_panel_selectors


@pytest.mark.parametrize('domain', DOMAINS)
def test_census_preserves_site_settings(domain):
    profile = ProfileResolver().resolve(domain)
    draft = CensusCollector(RunMetricsCollector(domain), profile).build_draft()
    restored, errors = profile_from_dict(draft)
    assert errors == []
    for key in KEYS:
        assert getattr(restored.crawl, key) == getattr(profile.crawl, key)
    if draft['crawl'].get('exclude_paths'):
        draft['crawl']['exclude_paths'].append('/mutation/')
        assert '/mutation/' not in profile.crawl.exclude_paths


@pytest.fixture
def crawler(tmp_path, monkeypatch):
    resolver = ProfileResolver()
    monkeypatch.setattr('web_crawler._get_profile_resolver', lambda: resolver)
    return WebCrawler(Config(base_dir=str(tmp_path)))


@pytest.mark.parametrize('domain,path', [
    ('pspcom.ru', '/catalog/build/item/'), ('pktmt.ru', '/practice/article/'),
    ('aerobel.ru', '/manufacturing/example/'),
])
def test_profile_exclusions_and_company_switch(crawler, domain, path):
    crawler._apply_site_profile('https://' + domain)
    assert crawler.url_categorizer.categorize_url('https://' + domain + path)[0] == 'excluded'
    crawler._apply_site_profile('https://unknown.test')
    assert crawler.profile is None
    assert crawler.url_categorizer.profile is None


def test_www_and_mirrors_from_profile(crawler):
    manager = crawler.url_categorizer.domain_equivalency
    crawler._apply_site_profile('https://pktmt.ru')
    assert manager.normalize_domain('pktmt.ru') == 'www.pktmt.ru'
    crawler._apply_site_profile('https://vmp-holding.ru')
    assert crawler._in_crawl_perimeter('https://vmp-anticor.ru/products/', 'https://vmp-holding.ru')
    crawler._apply_site_profile('https://unknown.test')
    assert manager.normalize_domain('pktmt.ru') == 'pktmt.ru'
    assert not crawler._in_crawl_perimeter('https://vmp-anticor.ru/products/', 'https://unknown.test')


def test_ajax_and_offers_from_profile(crawler):
    crawler._apply_site_profile('https://tizol.com')
    assert crawler.profile.crawl.ajax_tabs
    assert crawler._make_dynamic_extractor().extra_tab_panel_selector == '.product-page__section-content'
    crawler._apply_site_profile('https://betar.ru')
    assert crawler.profile.crawl.bitrix_offers
    assert not crawler.profile.crawl.ajax_tabs
