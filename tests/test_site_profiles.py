"""Юниты пакета site_profiles: модели, валидация, резолюция доменов."""
import textwrap

import pytest

from site_profiles import (SiteProfile, profile_from_dict, ProfileResolver,
                           normalize_domain)
from site_profiles.resolver import DEFAULT_PROFILES_DIR


# ==================== normalize_domain ====================

@pytest.mark.parametrize('raw, expected', [
    ('https://www.tizol.com/product/1', 'tizol.com'),
    ('http://TIZOL.com', 'tizol.com'),
    ('www.hms.ru', 'hms.ru'),
    ('nasos.hms.ru', 'nasos.hms.ru'),          # поддомен НЕ схлопывается без профиля
    ('example.ru:8080/path', 'example.ru'),
    ('//xn----7sbegqnkyhbtn.xn--p1ai/', 'xn----7sbegqnkyhbtn.xn--p1ai'),
    ('', ''),
])
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


# ==================== profile_from_dict / валидация ====================

def test_minimal_profile_is_default():
    profile, errors = profile_from_dict({'domain': 'example.ru'})
    assert errors == []
    assert profile.is_default()
    assert profile == SiteProfile.default('example.ru')


def test_domain_required():
    _, errors = profile_from_dict({})
    assert any('domain' in e for e in errors)


def test_unknown_key_rejected():
    _, errors = profile_from_dict({'domain': 'a.ru', 'crwal': {}})
    assert any('crwal' in e for e in errors)


def test_census_evidence_ignored():
    _, errors = profile_from_dict({'domain': 'a.ru', 'census_evidence': {'x': 1}})
    assert errors == []


def test_enum_validation():
    _, errors = profile_from_dict({'domain': 'a.ru', 'crawl': {'render': 'spa'}})
    assert any('crawl.render' in e for e in errors)
    _, errors = profile_from_dict({'domain': 'a.ru', 'crawl': {'antibot': 'proxy'}})
    assert any('crawl.antibot' in e for e in errors)
    _, errors = profile_from_dict({'domain': 'a.ru', 'extract': {'tier': 'magic'}})
    assert any('extract.tier' in e for e in errors)


def test_custom_tier_requires_module():
    _, errors = profile_from_dict({'domain': 'a.ru', 'extract': {'tier': 'custom'}})
    assert any('custom_module' in e for e in errors)
    profile, errors = profile_from_dict(
        {'domain': 'a.ru', 'extract': {'tier': 'custom', 'custom_module': 'a_ru'}})
    assert errors == []
    assert profile.extract.custom_module == 'a_ru'


def test_confidence_range():
    _, errors = profile_from_dict({'domain': 'a.ru', 'confidence': 1.5})
    assert any('confidence' in e for e in errors)


def test_full_profile_roundtrip():
    profile, errors = profile_from_dict({
        'domain': 'Tizol.com',
        'profile_version': 3,
        'confidence': 0.9,
        'source': 'census',
        'notes': ['D01'],
        'crawl': {
            'render': 'browser', 'antibot': 'warmup', 'ajax_tabs': True,
            'equivalent_domains': ['tizol.ru'], 'subdomain_collapse': ['*.tizol.com'],
            'exclude_paths': ['/news/'],
            'limits': {'pages': 100}, 'load': {'page_delay_ms': 500, 'fetch_timeout_s': 60},
        },
        'sections': {'catalog_roots': ['/catalog/'], 'documents_urls': ['/certificates/']},
        'extract': {'tier': 'llm', 'streaming': False,
                    'markdown': {'product_container': '.product', 'min_len': 150}},
        'baseline': {'product_cards': 38},
    })
    assert errors == []
    assert profile.domain == 'tizol.com'
    assert profile.crawl.render == 'browser'
    assert profile.crawl.limits.pages == 100
    assert profile.crawl.load.page_delay_ms == 500
    assert profile.sections.catalog_roots == ['/catalog/']
    assert profile.extract.streaming is False
    assert profile.extract.markdown.min_len == 150
    assert profile.baseline['product_cards'] == 38
    assert not profile.is_default()


# ==================== ProfileResolver ====================

@pytest.fixture
def resolver(tmp_path):
    (tmp_path / 'tizol.com.yaml').write_text(textwrap.dedent('''\
        domain: tizol.com
        crawl:
          ajax_tabs: true
    '''), encoding='utf-8')
    (tmp_path / 'hms.ru.yaml').write_text(textwrap.dedent('''\
        domain: hms.ru
        crawl:
          subdomain_collapse: ['*.hms.ru']
    '''), encoding='utf-8')
    (tmp_path / 'vmp-holding.ru.yaml').write_text(textwrap.dedent('''\
        domain: vmp-holding.ru
        crawl:
          equivalent_domains: [vmp-anticor.ru, vmp-plamcor.ru]
    '''), encoding='utf-8')
    (tmp_path / '_schema.yaml').write_text('domain: не-профиль\n', encoding='utf-8')
    (tmp_path / 'broken.yaml').write_text('domain: [unclosed\n', encoding='utf-8')
    (tmp_path / 'invalid.yaml').write_text('domain: bad.ru\ncrawl: {render: spa}\n',
                                           encoding='utf-8')
    return ProfileResolver(tmp_path)


def test_resolve_exact_and_www(resolver):
    assert resolver.resolve('https://tizol.com/x').crawl.ajax_tabs is True
    assert resolver.resolve('https://www.tizol.com/x').crawl.ajax_tabs is True


def test_resolve_equivalent_domain(resolver):
    assert resolver.resolve('http://vmp-anticor.ru/').domain == 'vmp-holding.ru'
    assert resolver.resolve('http://vmp-plamcor.ru/').domain == 'vmp-holding.ru'


def test_resolve_subdomain_collapse(resolver):
    assert resolver.resolve('https://nasos.hms.ru/p/1').domain == 'hms.ru'
    assert resolver.resolve('https://hms.ru/').domain == 'hms.ru'


def test_resolve_unknown_is_default(resolver):
    profile = resolver.resolve('https://unknown-site.ru/page')
    assert profile.is_default()
    assert profile.domain == 'unknown-site.ru'


def test_broken_and_invalid_yaml_skipped(resolver):
    # битый/невалидный YAML не валит загрузку и даёт default
    assert resolver.resolve('https://bad.ru/').is_default()
    assert 'broken.yaml' in resolver.load_errors
    assert 'invalid.yaml' in resolver.load_errors
    # служебные _*.yaml не считаются профилями
    assert set(resolver.known_domains()) == {'tizol.com', 'hms.ru', 'vmp-holding.ru'}


def test_missing_dir_is_default(tmp_path):
    resolver = ProfileResolver(tmp_path / 'нет-такой-папки')
    assert resolver.resolve('https://tizol.com/').is_default()


# ==================== Реальные профили репозитория ====================

def test_all_repo_profiles_valid():
    """Каждый mvp/profiles/*.yaml обязан проходить валидацию (гейт шага 2)."""
    resolver = ProfileResolver(DEFAULT_PROFILES_DIR)
    resolver._ensure_loaded()
    assert resolver.load_errors == {}, f'невалидные профили: {resolver.load_errors}'
