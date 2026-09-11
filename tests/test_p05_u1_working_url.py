"""P05 U1: выбор рабочего URL компании (domain_equivalency.find_working_url).

Чистая логика без сети: нормализация адреса из Site_list, порядок кандидатов с путём,
гейт содержательности и маркеры заглушек хостера, ранжирование по TLS, фолбэк.
HTTP-слой подменяется заглушкой _probe_http, поэтому тесты офлайновые.
"""
import asyncio
import types

import pytest

import main
from config import Config
from domain_equivalency import (DomainEquivalencyManager, HttpAnswer, hosts_equivalent,
                                normalize_site_url, strip_www)

# Тело «живого сайта»: длиннее порога 300 симв. и с внутренней навигацией.
LIVE_BODY = ('<html><body><a href="/catalog/">Каталог</a><a href="/contacts">Контакты</a>'
             + 'т' * 400 + '</body></html>')
REG_RU_STUB = ('<html><head><title>домен не привязан к хостингу</title></head><body>'
               '<a href="https://www.reg.ru">reg.ru</a>' + 'x' * 500 + '</body></html>')
BEGET_STUB = ('<html><body>Домен не прилинкован ни к одной из директорий на сервере!'
              '<a href="https://beget.com">beget</a>' + 'x' * 500 + '</body></html>')
ISPMANAGER_STUB = ('<html><head><title>Authorization</title></head><body>'
                   '<script>var binary = "/ispmgr";</script>' + 'x' * 500 + '</body></html>')


@pytest.fixture
def manager():
    return DomainEquivalencyManager(Config())


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _fake_http(responses):
    """Заглушка _probe_http: словарь (variant, verify_tls) -> HttpAnswer.

    Отсутствующий ключ = отказ соединения. Записывает порядок запросов в calls."""
    calls = []

    async def probe(variant, verify_tls, use_range):
        calls.append((variant, verify_tls, use_range))
        answer = responses.get((variant, verify_tls))
        if answer is None:
            return HttpAnswer(None, '', variant, 'отказ соединения: тест', False)
        return answer

    return probe, calls


# ==================== Нормализация адреса из Site_list (D147) ====================

@pytest.mark.parametrize('raw, expected', [
    ('www.berghome.ru', 'https://www.berghome.ru'),
    ('berghome.ru/produkciya/', 'https://berghome.ru/produkciya/'),
    ('http://baltur-russia.ru/', 'http://baltur-russia.ru/'),
    ('  https://www.dkc.ru/ru/  ', 'https://www.dkc.ru/ru/'),
    ('//example.ru', 'https://example.ru'),
    ('', ''),
    (None, ''),
    ('нет сайта', ''),
    ('https://', ''),
])
def test_normalize_site_url(raw, expected):
    assert normalize_site_url(raw) == expected


@pytest.mark.parametrize('host, expected', [
    ('www.a.ru', 'a.ru'),
    ('nowww.ru', 'nowww.ru'),      # наивный replace('www.','') давал 'noru'
    ('WWW.A.RU', 'a.ru'),
    ('a.ru:8080', 'a.ru'),
])
def test_strip_www(host, expected):
    assert strip_www(host) == expected


def test_hosts_equivalent():
    assert hosts_equivalent('www.a.ru', 'a.ru')
    assert not hosts_equivalent('a.ru', 'b.ru')
    assert not hosts_equivalent('', 'a.ru')


def test_read_company_list_normalizes_and_skips_hostless(monkeypatch, tmp_path):
    """read_company_list: адрес без схемы нормализуется, строка без хоста отбрасывается."""
    import pandas as pd
    rows = pd.DataFrame([
        {'Наименование': 'ДРАЙМИКС', 'Website': 'www.berghome.ru', 'ID производителя': '1'},
        {'Наименование': 'Балтур', 'Website': 'http://baltur-russia.ru/', 'ID производителя': '2'},
        {'Наименование': 'Без сайта', 'Website': 'нет сайта', 'ID производителя': '3'},
    ])
    monkeypatch.setattr(main.pd, 'read_excel', lambda path: rows)
    stub = types.SimpleNamespace(config=Config())
    companies = main.MonitoringSystem.read_company_list(stub)
    assert [c['website'] for c in companies] == ['https://www.berghome.ru',
                                                 'http://baltur-russia.ru/']


# ==================== Порядок кандидатов и путь (D140, D148) ====================

def test_candidates_keep_path_and_sitelist_pair_first(manager):
    assert manager._build_candidates('https://www.dkc.ru/ru/') == [
        'https://www.dkc.ru/ru/', 'http://www.dkc.ru/ru/',
        'https://dkc.ru/ru/', 'http://dkc.ru/ru/']


def test_candidates_http_from_sitelist_first(manager):
    """Схема из Site_list идёт первой: https-заглушка хостера не перебивает живой http."""
    assert manager._build_candidates('http://www.oooeti.ru')[0] == 'http://www.oooeti.ru'


def test_candidates_add_www_variant(manager):
    assert manager._build_candidates('https://rmz.by') == [
        'https://rmz.by', 'http://rmz.by', 'https://www.rmz.by', 'http://www.rmz.by']


# ==================== Гейт содержательности и маркеры заглушек ====================

@pytest.mark.parametrize('body, expect_status', [
    (REG_RU_STUB, 'hoster_stub'),
    (BEGET_STUB, 'hoster_stub'),
    (ISPMANAGER_STUB, 'hoster_stub'),
])
def test_hoster_stub_rejected(manager, body, expect_status):
    reason, status = manager._content_verdict(body, 'https://www.oooeti.ru')
    assert reason and status == expect_status


def test_short_body_rejected(manager):
    reason, status = manager._content_verdict('Welcome!', 'https://www.supra-kamin.ru')
    assert 'короче порога' in reason and status is None


def test_body_without_internal_links_rejected(manager):
    body = '<html><body>' + 'т' * 500 + '<a href="https://hoster.tld">хостер</a></body></html>'
    reason, _ = manager._content_verdict(body, 'https://a.ru')
    assert 'нет ссылок на собственный хост' in reason


def test_live_body_accepted(manager):
    assert manager._content_verdict(LIVE_BODY, 'https://a.ru') == ('', None)


def test_relative_and_www_links_count_as_internal(manager):
    body = ('<html><body>' + 'т' * 400 +
            '<a href="https://www.a.ru/catalog/">Каталог</a></body></html>')
    assert manager._content_verdict(body, 'https://a.ru') == ('', None)


# ==================== Подтверждение кандидата: только 200 ====================

def test_206_reprobed_without_range(manager):
    """206 на наш Range — подозрительный ответ: проба повторяется без Range (D241)."""
    responses = {
        ('https://www.supra-kamin.ru', True): HttpAnswer(206, '!', 'https://www.supra-kamin.ru',
                                                         None, False),
    }
    probe, calls = _fake_http(responses)
    manager._probe_http = probe
    result = _run(manager._probe_candidate('https://www.supra-kamin.ru', verify_tls=True))
    assert [c[2] for c in calls] == [True, False]     # сначала с Range, затем без
    assert not result.ok


def test_status_404_not_confirmed(manager):
    responses = {('https://a.ru', True): HttpAnswer(404, LIVE_BODY, 'https://a.ru', None, False)}
    probe, _ = _fake_http(responses)
    manager._probe_http = probe
    assert not _run(manager._probe_candidate('https://a.ru', verify_tls=True)).ok


# ==================== Ранжирование по TLS ====================

def test_tls_verified_candidate_wins_over_ssl_false(manager):
    """Кандидат с валидным TLS подтверждается раньше, чем чужой vhost без проверки TLS."""
    responses = {
        ('https://www.oooeti.ru', True): HttpAnswer(None, '', 'https://www.oooeti.ru',
                                                    'TLS-имя не совпало', True),
        ('https://www.oooeti.ru', False): HttpAnswer(200, REG_RU_STUB, 'https://www.oooeti.ru',
                                                     None, False),
        ('http://www.oooeti.ru', True): HttpAnswer(200, LIVE_BODY, 'http://www.oooeti.ru/',
                                                   None, False),
    }
    probe, _ = _fake_http(responses)
    manager._probe_http = probe
    assert _run(manager.find_working_url('http://www.oooeti.ru/')) == 'http://www.oooeti.ru/'


def test_ssl_false_second_pass_saves_expired_cert_site(manager):
    """Сайт с непрошедшим проверку сертификатом на СВОЁМ домене не теряется: второй проход."""
    responses = {
        ('https://a.ru', True): HttpAnswer(None, '', 'https://a.ru', 'TLS-имя не совпало', True),
        ('https://a.ru', False): HttpAnswer(200, LIVE_BODY, 'https://a.ru/', None, False),
    }
    probe, calls = _fake_http(responses)
    manager._probe_http = probe
    assert _run(manager.find_working_url('https://a.ru')) == 'https://a.ru/'
    # второй проход идёт ПОСЛЕ всех кандидатов первого
    assert [c[1] for c in calls] == [True, True, True, True, False]


# ==================== Фолбэк ====================

def test_fallback_returns_sitelist_url(manager):
    """Ни один кандидат не ответил -> исходный адрес из Site_list, а не https-без-www (D115)."""
    probe, _ = _fake_http({})
    manager._probe_http = probe
    assert _run(manager.find_working_url('http://baltur-russia.ru/')) == 'http://baltur-russia.ru/'


def test_fallback_keeps_www_and_path(manager):
    probe, _ = _fake_http({})
    manager._probe_http = probe
    assert _run(manager.find_working_url('https://www.dkc.ru/ru/')) == 'https://www.dkc.ru/ru/'


def test_fallback_to_responding_candidate(manager):
    """Адрес из Site_list мёртв, а соседний кандидат хотя бы ответил — берём его."""
    responses = {('http://a.ru', True): HttpAnswer(200, 'Welcome!', 'http://a.ru', None, False)}
    probe, _ = _fake_http(responses)
    manager._probe_http = probe
    assert _run(manager.find_working_url('https://a.ru')) == 'http://a.ru'


def test_hostless_url_gives_error_status(manager):
    assert _run(manager.find_working_url('нет сайта')) == 'нет сайта'
    assert manager.last_site_status == 'no_host'


def test_http_only_status(manager):
    responses = {('http://a.ru', True): HttpAnswer(200, LIVE_BODY, 'http://a.ru/', None, False)}
    probe, _ = _fake_http(responses)
    manager._probe_http = probe
    assert _run(manager.find_working_url('https://a.ru')) == 'http://a.ru/'
    assert manager.last_site_status == 'http_only'


def test_hoster_stub_status_on_total_failure(manager):
    responses = {('https://a.ru', True): HttpAnswer(200, REG_RU_STUB, 'https://a.ru', None, False)}
    probe, _ = _fake_http(responses)
    manager._probe_http = probe
    assert _run(manager.find_working_url('https://a.ru')) == 'https://a.ru'
    assert manager.last_site_status == 'hoster_stub'
