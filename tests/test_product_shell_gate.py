"""Тесты гейта «оболочки» товарной страницы.

Проблема (прогон 20-21.08): HTTP-first отдавал товарные страницы, в которых разметка есть,
а видимого текста почти нет — контент дорисовывает JS. needs_javascript такие страницы
пропускал (ему нужен ещё признак SPA, а на серверных CMS его нет), браузер не запускался,
и LLM отвергал пустой markdown как Trash_418#. За прогон: 2333 страницы без браузера
против 287 через него; АргументПласт — HTML 47 КБ при 776 симв. текста, 50 страниц, 0 карточек.

Порог 1000 симв. проверен на корпусе прогона: у 46 компаний, давших карточки, минимум
видимого текста товарной страницы = 1307; у пострадавших — 287..903.
"""
import asyncio

import pytest

from config import Config
from data_island import looks_like_product_shell
from web_crawler import HttpFetchResult, WebCrawler

# Оболочка как в жизни (АргументПласт: HTML 47 КБ при 776 симв. текста): видимого текста
# 300..1000 симв. — достаточно, чтобы СТАРЫЕ гейты (http_min_content, needs_javascript)
# страницу приняли, и мало, чтобы в ней был товар. Именно этот зазор и закрывает новый гейт.
NAV = ''.join(f'<a href="/r{i}">Раздел каталога номер {i}</a>' for i in range(30))
SHELL = f'<html><body><div id="wrap">{NAV}</div>{"<div></div>" * 500}</body></html>'
FULL = ('<html><body><div class="content"><h1>Радиатор BASE 500</h1>'
        + '<p>Межосевое расстояние 500 мм, тепловой поток 197 Вт, масса 1,84 кг. </p>' * 30
        + '</div></body></html>')
ISLAND = ('<html><body>' + NAV + '<script type="application/ld+json">'
          '{"@context":"https://schema.org","@type":"Product","name":"Радиатор BASE 500",'
          '"offers":{"@type":"Offer","price":"1200","priceCurrency":"RUB"}}</script></body></html>')


# ==================== Детектор ====================

def test_shell_detected():
    assert looks_like_product_shell(SHELL) is True


def test_full_page_not_shell():
    assert looks_like_product_shell(FULL) is False


def test_product_island_beats_thin_text():
    """Данные товара в JSON-LD -> контент уже пришёл, рендер не нужен даже при малом тексте."""
    assert looks_like_product_shell(ISLAND) is False


def test_empty_html_is_shell():
    assert looks_like_product_shell('') is True


def test_threshold_is_configurable():
    assert looks_like_product_shell(FULL, min_text=10 ** 6) is True
    assert looks_like_product_shell(SHELL, min_text=1) is False


# ==================== Интеграция в лестницу ====================

@pytest.fixture
def crawler(tmp_path):
    config = Config()
    config.base_dir = str(tmp_path)
    return WebCrawler(config)


def _stub_fetch(crawler, html):
    async def fake(url, **kwargs):
        return HttpFetchResult(html, 200, {}, url, {})
    crawler._fetch_with_http_impersonate = fake


def test_shell_escalates_for_product_page(crawler):
    """Товарная страница-оболочка не принимается: None -> лестница идёт в браузер."""
    _stub_fetch(crawler, SHELL)
    assert asyncio.run(crawler._http_first_get('https://a.ru/p/1', expect_product=True)) is None


def test_full_page_accepted_for_product_page(crawler):
    """Полноценная страница по-прежнему отдаётся без браузера (регресс-контроль)."""
    _stub_fetch(crawler, FULL)
    assert asyncio.run(crawler._http_first_get('https://a.ru/p/1', expect_product=True)) is not None


def test_gate_does_not_touch_contacts(crawler):
    """Гейт только для товарных страниц: contacts/distributor идут прежним путём."""
    _stub_fetch(crawler, SHELL)
    assert asyncio.run(crawler._http_first_get('https://a.ru/contacts')) is not None


def test_zero_disables_gate(crawler):
    """product_min_text=0 -> прежнее поведение."""
    crawler.config.product_min_text = 0
    _stub_fetch(crawler, SHELL)
    assert asyncio.run(crawler._http_first_get('https://a.ru/p/1', expect_product=True)) is not None


# ==================== Корпус реального прогона ====================

@pytest.mark.parametrize('company, expect_shell', [
    ('АргументПласт_ООО', True),    # HTML 47 КБ, текста 776 -> оболочка, 0 карточек
    ('Метизный_Альянс_ТПК_OOO', False),  # 45 карточек -> трогать нельзя
])
def test_corpus_pages(corpus_dir, company, expect_shell):
    """Гейт на живых страницах прогона: помечает пострадавших и не трогает успешных."""
    pages = sorted((corpus_dir / company / 'Product_pages').glob('*.html'))[:8]
    if not pages:
        pytest.skip(f'нет корпуса для {company}')
    flagged = sum(looks_like_product_shell(p.read_text(encoding='utf-8', errors='ignore')) for p in pages)
    share = flagged / len(pages)
    if expect_shell:
        assert share >= 0.5, f'{company}: помечено лишь {share:.0%} страниц'
    else:
        assert share <= 0.2, f'{company}: ложно помечено {share:.0%} страниц у работающей компании'
