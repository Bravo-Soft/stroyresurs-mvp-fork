"""Тесты extract-роутера: профиль сайта управляет выбором адаптера и markdown-полями,
отсутствие профиля == прежнее поведение (байт-в-байт).
"""
import json
import textwrap
from pathlib import Path

import pytest

import text_extractor
from site_profiles import get_resolver, reset_resolver
from site_profiles.resolver import DEFAULT_PROFILES_DIR

DISPATCH_MAP = Path(__file__).parent / 'golden' / 'dispatch_map.json'

SAMPLE_HTML = textwrap.dedent('''\
    <html><head><title>Тест</title></head><body>
    <header>шапка</header>
    <article>
      <h1>Изделие Т-100</h1>
      <table><tr><td>Масса</td><td>10 кг</td></tr>
      <tr><td>Длина</td><td>1000 мм</td></tr></table>
      <p>Описание изделия для проверки конвертации в markdown. Абзац достаточно
      длинный, чтобы пройти пороги минимальной длины текста без откатов.</p>
    </article>
    <div class="reklama">мусорный блок</div>
    <footer>подвал</footer>
    </body></html>
''')


@pytest.fixture
def clean_resolver():
    """Сбрасывает синглтон-резолвер до и после теста (не протекает в другие тесты)."""
    reset_resolver()
    yield
    reset_resolver()


def _write_profile(dir_path: Path, domain: str, body: str = ''):
    (dir_path / f'{domain}.yaml').write_text(f'domain: {domain}\n{body}', encoding='utf-8')


# ==================== Эквивалентность диспетчеризации ====================

@pytest.mark.skipif(not DISPATCH_MAP.exists(), reason='нет golden/dispatch_map.json')
def test_dispatch_matches_legacy_map(clean_resolver):
    """Для каждого адаптерного домена профиль выбирает ТОТ ЖЕ модуль и флаги,
    что старый диспетчер (снимок в dispatch_map.json)."""
    legacy = json.loads(DISPATCH_MAP.read_text(encoding='utf-8'))
    resolver = get_resolver(DEFAULT_PROFILES_DIR)
    for domain, expected in legacy.items():
        clean_domain = text_extractor._normalize_domain(f'https://{domain.strip("/")}/')
        if clean_domain != domain:
            # мёртвый ключ старого диспетчера (agroskon) — адаптер не применялся;
            # профиль обязан сохранять generic-поведение (tier != custom)
            profile = resolver.resolve(clean_domain)
            assert profile.extract.tier != 'custom', domain
            continue
        profile = resolver.resolve(f'https://{domain}/')
        assert profile.extract.tier == 'custom', domain
        assert profile.extract.custom_module == expected['module'], domain
        adapter = text_extractor._get_adapter_by_module(profile.extract.custom_module)
        assert adapter is not None, domain
        # эффективные флаги (профиль ИЛИ атрибут модуля) == флагам старого пути
        for attr, key in (('KEEP_HIDDEN_TABS', 'keep_hidden_tabs'),
                          ('NEEDS_RAW_HTML', 'needs_raw_html'),
                          ('CLEAN_IN_UNIVERSAL', 'clean_in_universal')):
            effective = getattr(profile.extract, key) or bool(getattr(adapter, attr, False))
            assert effective == expected[key], f'{domain}.{key}'


@pytest.mark.parametrize('domain', [
    'aerobel.ru', 'tizol.com', 'cooltech.ru', 'pktmt.ru', 'kolpa-san.ru',
    'chelaz.ru', 'espa.ru', 'tss.ru', 'belcolor.ru',  # belcolor — без профиля
])
def test_profile_output_equals_legacy_output(tmp_path, clean_resolver, domain):
    """Главный гейт: вывод html_to_markdown с профилями == вывод без профилей."""
    url = f'https://{domain}/catalog/item-1/'
    # 1) без профилей (пустая папка) — прежний код-путь
    get_resolver(tmp_path)
    legacy_md = text_extractor.html_to_markdown(SAMPLE_HTML, url)
    legacy_company = text_extractor.html_to_markdown(SAMPLE_HTML, url, 'company')
    # 2) с реальными профилями репозитория
    reset_resolver()
    get_resolver(DEFAULT_PROFILES_DIR)
    profile_md = text_extractor.html_to_markdown(SAMPLE_HTML, url)
    profile_company = text_extractor.html_to_markdown(SAMPLE_HTML, url, 'company')
    assert profile_md == legacy_md
    assert profile_company == legacy_company


def test_failing_resolver_is_fail_open(clean_resolver, monkeypatch):
    """Исключение в резолвере не роняет конвертацию — работает старый путь."""
    import site_profiles

    def boom(*args, **kwargs):
        raise RuntimeError('санитарная проверка fail-open')

    monkeypatch.setattr(site_profiles, 'get_resolver', boom)
    md = text_extractor.html_to_markdown(SAMPLE_HTML, 'https://aerobel.ru/x/')
    assert 'Изделие Т-100' in md


# ==================== Markdown-поля профиля ====================

def test_product_container_override(tmp_path, clean_resolver):
    _write_profile(tmp_path, 'a.ru', textwrap.dedent('''\
        extract:
          markdown:
            product_container: '.only-this'
    '''))
    get_resolver(tmp_path)
    html = ('<html><body><article><p>не это</p></article>'
            '<div class="only-this"><h2>Только этот блок и его содержимое, длинное '
            'достаточно для порога минимальной длины markdown в двести символов, '
            'чтобы страховка от переочистки не откатила результат к базовому варианту '
            'со всем содержимым страницы целиком</h2></div></body></html>')
    md = text_extractor.html_to_markdown(html, 'https://a.ru/p/1')
    assert 'Только этот блок' in md
    assert 'не это' not in md


def test_remove_selectors(tmp_path, clean_resolver):
    _write_profile(tmp_path, 'a.ru', textwrap.dedent('''\
        extract:
          markdown:
            remove_selectors: ['.reklama']
    '''))
    get_resolver(tmp_path)
    md = text_extractor.html_to_markdown(SAMPLE_HTML, 'https://a.ru/p/1')
    assert 'мусорный блок' not in md
    assert 'Изделие Т-100' in md


def test_max_len_truncates(tmp_path, clean_resolver):
    _write_profile(tmp_path, 'a.ru', textwrap.dedent('''\
        extract:
          markdown:
            max_len: 50
    '''))
    get_resolver(tmp_path)
    md = text_extractor.html_to_markdown(SAMPLE_HTML, 'https://a.ru/p/1')
    assert len(md) <= 50


def test_custom_module_via_profile(tmp_path, clean_resolver):
    """tier=custom по профилю вызывает именно указанный модуль Profile_Markdown."""
    _write_profile(tmp_path, 'zzz-новый-домен.ru', textwrap.dedent('''\
        extract:
          tier: custom
          custom_module: areopag_spb_ru
    '''))
    get_resolver(tmp_path)
    adapter = text_extractor._get_adapter_by_module('areopag_spb_ru')
    assert adapter is not None and adapter.DOMAIN == 'areopag-spb.ru'
    # страница без структуры areopag -> адаптер вернёт None -> generic-фолбэк работает
    md = text_extractor.html_to_markdown(SAMPLE_HTML, 'https://zzz-новый-домен.ru/p/1')
    assert 'Изделие Т-100' in md
