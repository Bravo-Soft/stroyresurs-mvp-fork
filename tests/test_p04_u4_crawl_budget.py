"""P04 U4: товарная квота, приоритет ссылок из сеток и достижимость карточек.

Коды: D100 (листинг каталога неотличим от карточки и выбирает квоту),
D10 (ссылки из товарных сеток не имеют приоритета над листингами),
D157 (навигация через onclick/data-href не разбирается нигде),
D271 ('/buy/' — товарный паттерн, дилерские страницы съедают квоту).

Плюс хвосты соседних полос, переданные этой единице:
подключение `URLCategorizer.observe_urls` (полоса B) и понижение доверия к роли
price_list, полученной из середины пути (P01 U4 п.4, полоса A).

Сети нет: сетевой слой краулера подменяется заглушками, HTML — фикстуры в тексте.
"""
import asyncio
import re
from collections import deque
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from config import Config
from web_crawler import (ParseResult, ProductGridParser, URLCategorizer, WebCrawler,
                         extract_element_url)


@pytest.fixture
def categorizer():
    cat = URLCategorizer(Config())
    cat.set_profile(None)
    return cat


@pytest.fixture
def crawler(tmp_path):
    config = Config()
    config.base_dir = str(tmp_path)
    crawler = WebCrawler(config)
    crawler.current_base_url = 'https://a.ru/'
    return crawler


def _page(html):
    return ParseResult(BeautifulSoup(html, 'html.parser'), html, [], 'product_page', {})


# ==================== 1. Форма URL: листинг против карточки (D100) ============

LISTING_URLS = [
    'https://vacma.ru/catalog/fitingi/',            # раздел каталога (D100)
    'https://vacma.ru/catalog/fitingi',             # он же без слеша
    'https://www.elstat.ru/catalog/shkafy/',        # раздел каталога (D100)
    'https://a.ru/catalog/',                        # корень каталога
    'https://a.ru/products/',
    'https://a.ru/catalog/?PAGEN_1=2',              # пагинация Bitrix
    'https://a.ru/catalog/fitingi/?page=3',
    'https://a.ru/catalog/page/2',
    'https://a.ru/catalog/stranica-5',
    'https://te-s.msk.ru/index.php?categoryID=94',  # листинговый параметр
]

CARD_URLS = [
    'https://vacma.ru/catalog/fitingi/kran-11b27p1/',
    'https://a.ru/catalog/nasosy/nasos-cns-60/',
    'https://a.ru/catalog/section/detail.php?ID=123',   # скриптовая карточка Bitrix
    'https://a.ru/cat/?p_id=89',                        # D274
    'https://a.ru/steklo.htm',                          # плоский лист (D228)
    'https://a.ru/product/102',
]


@pytest.mark.parametrize('url', LISTING_URLS)
def test_listing_urls_detected(categorizer, url):
    assert categorizer.is_listing_url(url) is True, url


@pytest.mark.parametrize('url', CARD_URLS)
def test_card_urls_are_not_listings(categorizer, url):
    assert categorizer.is_listing_url(url) is False, url


@pytest.mark.parametrize('url', CARD_URLS)
def test_card_urls_detected(categorizer, url):
    assert categorizer.is_product_card_url(url) is True, url


@pytest.mark.parametrize('url', ['https://vacma.ru/catalog/fitingi/',
                                 'https://a.ru/catalog/?PAGEN_1=2',
                                 'https://a.ru/products/'])
def test_listing_urls_are_not_cards(categorizer, url):
    assert categorizer.is_product_card_url(url) is False, url


def test_listing_detection_is_narrow(categorizer):
    """Сознательный недобор: раздел со слагом-дефисом листингом не считается —
    перебор дороже, он раздувал бы бюджет обхода."""
    assert categorizer.is_listing_url('https://a.ru/catalog/truboprovodnaya-armatura/') is False


def test_main_page_is_not_listing(categorizer):
    assert categorizer.is_listing_url('https://a.ru/') is False


def test_listing_role_unchanged(categorizer):
    """Регресс полосы B: признак листинга роль НЕ меняет — страница по-прежнему
    обходится, сохраняется и отдаёт ссылки."""
    assert categorizer.categorize_url('https://vacma.ru/catalog/fitingi/') == ('product', 9)
    assert categorizer.categorize_url('https://www.elstat.ru/catalog/shkafy/') == ('product', 9)


# ==================== 2. '/buy/' — дилеры, а не товары (D271) =================

def test_buy_section_is_distributor(categorizer):
    """kvil.ru/buy/?id=8940 без текста ссылки: было ('product', 9)."""
    assert categorizer.categorize_url('https://kvil.ru/buy/?id=8940') == (
        'distributor', categorizer.priority_levels['distributor'])


def test_buy_regional_pages_are_distributor(categorizer):
    assert categorizer.categorize_url(
        'http://sten.ru/buy/client/altai-krai.php')[0] == 'distributor'


def test_buyer_path_still_product(categorizer):
    """Контроль: 'buy' сверяется целым сегментом, '/buyer/product.php' — карточка."""
    assert categorizer.categorize_url(
        'https://belsteel.com/buyer/product.php?id=17') == ('product', 9)


def test_bare_buy_pattern_removed(categorizer):
    assert r'/buy/' not in categorizer.product_url_patterns
    assert r'/buy/[a-z0-9-]+' in categorizer.product_url_patterns


# ==================== 3. Достижимость карточек: onclick (D157) ================

@pytest.mark.parametrize('html,expected', [
    ("<div onclick=\"window.location.href='/catalog/kabel-vvg/'\">Кабель</div>",
     '/catalog/kabel-vvg/'),
    ('<div onclick=\'document.location="/catalog/kabel-2/"\'>Кабель</div>',
     '/catalog/kabel-2/'),
    ("<tr onclick=\"location.href = '/product/102'\">Товар</tr>", '/product/102'),
    ('<div data-href="/catalog/kabel-3/">Кабель</div>', '/catalog/kabel-3/'),
    ('<li data-url="/catalog/kabel-4/">Кабель</li>', '/catalog/kabel-4/'),
    ("<div onclick=\"showTab(1)\">Вкладка</div>", ''),
    ('<div>Без перехода</div>', ''),
])
def test_extract_element_url(html, expected):
    element = BeautifulSoup(html, 'html.parser').find(True)
    assert extract_element_url(element) == expected


def test_cart_action_is_not_a_link():
    """Обработчик «в корзину» — действие, а не переход: '?cartAction=add&cartItem=7'
    балльная эвристика сочла бы товаром ('item='), и он занял бы слот квоты."""
    html = ("<div onclick=\"location='/netcat/modules/eshop/?cartAction=add&amp;"
            "cartItem=7&amp;cartItemCount=1'\">В корзину</div>")
    element = BeautifulSoup(html, 'html.parser').find(True)
    assert extract_element_url(element) == ''


ONCLICK_PAGE = """
<html><body>
  <a href="/catalog/nasosy/nasos-cns-60/">Насос ЦНС-60</a>
  <div class="catalog-item" onclick="window.location.href='/catalog/nasosy/nasos-k-100/'">
     Насос К-100
  </div>
  <div class="product-card" data-href="/catalog/nasosy/nasos-d-200/">Насос Д-200</div>
</body></html>
"""


def test_additional_links_read_onclick(crawler):
    """D157: адрес из onclick/data-href попадает в общий сборщик ссылок."""
    soup = BeautifulSoup(ONCLICK_PAGE, 'html.parser')
    links = asyncio.run(crawler._extract_additional_links(soup, 'https://a.ru/catalog/nasosy/'))
    urls = {url for url, _cat, _prio in links}
    assert 'https://a.ru/catalog/nasosy/nasos-k-100/' in urls
    assert 'https://a.ru/catalog/nasosy/nasos-d-200/' in urls


def test_grid_parser_reads_onclick(categorizer):
    """D10 + D157: плитка сетки без <a href> тоже даёт товарную ссылку."""
    soup = BeautifulSoup(ONCLICK_PAGE, 'html.parser')
    parser = ProductGridParser(categorizer)
    links = parser.extract_product_links(soup, 'https://a.ru/', 'https://a.ru/catalog/nasosy/')
    urls = {url for url, _cat, _prio in links}
    assert 'https://a.ru/catalog/nasosy/nasos-k-100/' in urls


# ==================== 4. Приоритет ссылок из сеток (D10) ======================

def test_grid_links_outrank_sitemap(categorizer):
    """Ссылка из товарной сетки получает приоритет выше диапазона карты сайта
    (10..13 после P02 U3), иначе преимущества над листингами у неё нет."""
    soup = BeautifulSoup(ONCLICK_PAGE, 'html.parser')
    parser = ProductGridParser(categorizer)
    links = parser.extract_product_links(soup, 'https://a.ru/', 'https://a.ru/catalog/nasosy/')
    assert links
    assert all(prio == Config().product_grid_link_priority for _u, _c, prio in links)
    assert Config().product_grid_link_priority > 13


def test_listing_link_priority_lowered(crawler):
    """Листинг из общего сборщика ссылок обходится после карточек."""
    html = """<html><body>
      <a href="/catalog/fitingi/">Фитинги</a>
      <a href="/catalog/fitingi/kran-11b27p1/">Кран 11б27п1</a>
    </body></html>"""
    links = asyncio.run(crawler._extract_additional_links(
        BeautifulSoup(html, 'html.parser'), 'https://a.ru/catalog/'))
    priorities = {url: prio for url, _cat, prio in links}
    assert priorities['https://a.ru/catalog/fitingi/kran-11b27p1/'] == 9
    assert priorities['https://a.ru/catalog/fitingi/'] == 8


# ==================== 5. Резерв товарной квоты под карточки (D100) ============

def test_quota_reserve_rejects_listing(crawler):
    """Последняя доля квоты (product_quota_card_reserve) отдаётся только карточкам."""
    crawler.max_product_pages_per_site = 50
    crawler.product_quota_card_reserve = 0.3
    for i in range(35):
        crawler.product_urls.add(f'https://a.ru/catalog/x{i}')
    assert crawler._product_quota_reject_reason(
        'https://a.ru/catalog/fitingi/') == 'резерв квоты под карточки'
    assert crawler._product_quota_reject_reason(
        'https://a.ru/catalog/fitingi/kran-11b27p1/') is None


def test_quota_reserve_inactive_before_threshold(crawler):
    crawler.max_product_pages_per_site = 50
    crawler.product_quota_card_reserve = 0.3
    for i in range(34):
        crawler.product_urls.add(f'https://a.ru/catalog/x{i}')
    assert crawler._product_quota_reject_reason('https://a.ru/catalog/fitingi/') is None


def test_quota_exhausted_reason(crawler):
    crawler.max_product_pages_per_site = 50
    for i in range(50):
        crawler.product_urls.add(f'https://a.ru/catalog/x{i}')
    assert crawler._product_quota_reject_reason(
        'https://a.ru/catalog/fitingi/kran-11b27p1/') == 'товарная квота'


def test_quota_reserve_can_be_switched_off(crawler):
    """0 в Config возвращает прежнее поведение (резерва нет)."""
    crawler.max_product_pages_per_site = 50
    crawler.product_quota_card_reserve = 0
    for i in range(49):
        crawler.product_urls.add(f'https://a.ru/catalog/x{i}')
    assert crawler._product_quota_reject_reason('https://a.ru/catalog/fitingi/') is None


def test_queue_gate_counts_reserve_rejection(crawler):
    crawler.max_product_pages_per_site = 50
    for i in range(40):
        crawler.product_urls.add(f'https://a.ru/catalog/x{i}')
    allowed = asyncio.run(crawler._should_add_to_queue_parallel('https://a.ru/catalog/fitingi/', 1))
    assert allowed is False
    assert crawler._queue_gate_rejections.get('резерв квоты под карточки') == 1
    assert 'https://a.ru/catalog/fitingi/' in crawler._quota_rejected_urls


# ==================== 6. Возврат слота листингу после загрузки (D100) =========

LISTING_HTML = '<html><body><h1>Фитинги</h1><ul><li>Кран</li><li>Тройник</li></ul>' + 'т' * 400 + '</body></html>'
CARD_HTML = ('<html><body><h1>Кран 11б27п1</h1><div class="product-card">'
             '<span>Цена: 1200 руб</span><button>Купить</button>'
             '<div>Артикул: 11б27п1</div><div>Характеристики</div></div>' + 'т' * 400 + '</body></html>')


def _process(crawler, url, html, category='product'):
    async def fake_fetch(u, cat=None):
        return _page(html)

    async def no_links(u, parse_result, depth, queue):
        return None

    crawler._fetch_page_content = fake_fetch
    crawler._process_links_from_parse_result = no_links
    key = crawler.url_normalizer.normalize_url(url)
    crawler.product_urls.add(key)
    asyncio.run(crawler._process_page_with_storage(
        url, 0, category, 'ООО Тест', {'base_domain_dir': crawler.config.base_dir}, deque()))
    return key


def test_listing_slot_returned_after_fetch(crawler):
    """Форма URL говорит «листинг», DOM карточку не подтверждает — слот возвращается,
    а страница при этом сохраняется как товарная (роль не менялась)."""
    key = _process(crawler, 'https://a.ru/catalog/fitingi/', LISTING_HTML)
    assert key not in crawler.product_urls
    assert crawler._product_quota_reused == 1
    assert crawler.stats['product_pages'] == 1


def test_listing_slot_kept_when_dom_confirms_card(crawler):
    """Листинг с ценами и кнопками «Купить» — это витрина товара, слот остаётся."""
    key = _process(crawler, 'https://a.ru/catalog/fitingi/', CARD_HTML)
    assert key in crawler.product_urls
    assert crawler._product_quota_reused == 0


def test_card_url_slot_kept(crawler):
    key = _process(crawler, 'https://a.ru/catalog/fitingi/kran-11b27p1/', LISTING_HTML)
    assert key in crawler.product_urls


# ==================== 7. Возврат слота по вердикту Trash_418# =================

def test_release_product_slot(crawler):
    url = 'https://a.ru/catalog/fitingi/kran-11b27p1/'
    key = crawler.url_normalizer.normalize_url(url)
    crawler.product_urls.add(key)
    assert asyncio.run(crawler.release_product_slot(url)) is True
    assert key not in crawler.product_urls
    # повторный вердикт по тому же адресу слот дважды не возвращает
    assert asyncio.run(crawler.release_product_slot(url)) is False


def test_release_product_slot_respects_reuse_limit(crawler):
    crawler.product_quota_reuse_limit = 1
    for i in (1, 2):
        url = f'https://a.ru/catalog/x{i}/kran-{i}/'
        crawler.product_urls.add(crawler.url_normalizer.normalize_url(url))
    assert asyncio.run(crawler.release_product_slot('https://a.ru/catalog/x1/kran-1/')) is True
    assert asyncio.run(crawler.release_product_slot('https://a.ru/catalog/x2/kran-2/')) is False


def test_trash_verdict_calls_release_in_streaming_mode():
    """Вызов в main.py: ветка Trash_418# возвращает слот, но только пока идёт краул
    (page_sink задан). Сама process_product_page целиком не тестируема — сверяем
    исходник, как это сделано для гейта сохранения в P04 U3."""
    source = Path(__file__).resolve().parents[1].joinpath('main.py').read_text(encoding='utf-8')
    branch = source.split('Trash_418#, сохраняем в Not_products')[1][:900]
    assert 'page_sink' in branch
    assert 'release_product_slot' in branch


# ==================== 8. Хвост полосы B: подключение observe_urls =============

def _run_sitemap_stage(crawler, urls):
    """Прогоняет фазу карты сайта в _start_crawling_with_sitemap_support: сетевой
    слой и обход подменены, наружу отдаётся список строк карты, дошедших до очереди.
    Элемент — 4-кортеж полосы A (адрес, нормализованный ключ, роль, приоритет)."""
    rows = []
    for url in urls:
        category, priority = crawler.url_categorizer.categorize_url(url)
        rows.append((url, crawler.url_normalizer.normalize_url(url), category, priority + 1))
    captured = {}

    async def discover(site_url):
        return rows

    async def add_to_queue(queue, sitemap_urls, base_url):
        captured['rows'] = list(sitemap_urls)

    async def no_page(*args, **kwargs):
        return None

    crawler._discover_and_parse_sitemap = discover
    crawler._add_sitemap_urls_to_queue = add_to_queue
    crawler._process_page_parallel = no_page
    asyncio.run(crawler._start_crawling_with_sitemap_support(
        'https://a.ru/', 'ООО Тест', {'base_domain_dir': crawler.config.base_dir}, []))
    return captured.get('rows', [])


def test_observe_urls_called_on_sitemap_flat_site(crawler):
    """Плоский сайт: товарных/каталожных сегментов в карте нет — признак включается,
    и роли адресов карты пересчитываются (до правки они оставались 'other')."""
    rows = _run_sitemap_stage(crawler, [
        'https://a.ru/gidrostrelka.html', 'https://a.ru/steklo.htm',
        'https://a.ru/kotel.html', 'https://a.ru/nasos.html',
        'https://a.ru/truba.html', 'https://a.ru/kontakty.html'])
    assert crawler.url_categorizer.flat_site is True
    roles = {url: category for url, _norm, category, _prio in rows}
    assert roles.get('https://a.ru/gidrostrelka.html') == 'product'


def test_observe_urls_no_flat_site_when_catalog_present(crawler):
    rows = _run_sitemap_stage(crawler, ['https://a.ru/catalog/nasosy/nasos-cns-60/',
                                        'https://a.ru/kontakty.html'])
    assert crawler.url_categorizer.flat_site is False
    assert crawler._flat_site_observed is True
    # роли не пересчитывались: приоритет карты сайта (+1 парсера) сохранён
    assert [prio for _u, _n, _c, prio in rows] == [10, 8]


def test_observe_urls_fallback_on_first_page(crawler):
    """Карты сайта нет: признак считается по ссылкам первой разобранной страницы."""
    html = """<html><body>
      <a href="/gidrostrelka.html">Гидрострелка</a>
      <a href="/kotel.html">Котёл</a>
      <a href="/nasos.html">Насос</a>
      <a href="/truba.html">Труба</a>
      <a href="/kontakty.html">Контакты</a>
    </body></html>"""
    assert crawler._flat_site_observed is False
    asyncio.run(crawler._extract_additional_links(
        BeautifulSoup(html, 'html.parser'), 'https://a.ru/'))
    assert crawler._flat_site_observed is True
    assert crawler.url_categorizer.flat_site is True


def test_flat_site_observation_resets_between_companies(crawler):
    crawler._flat_site_observed = True
    asyncio.run(crawler.reset_state('ООО Тест'))
    assert crawler._flat_site_observed is False


# ==================== 9. Ревью слияния: F3 (очередь ↔ гейт) и F8 =============

ANCHOR_PAGE = """<html><body>
  <a href="/plity/p1.html">Технические характеристики</a>
</body></html>"""


def test_queue_gate_uses_role_from_link_text(crawler):
    """F3: сборщик ссылок считает роль С ТЕКСТОМ ссылки, гейт допуска считал её
    заново без текста — товарная страница ложилась в очередь как товар, но слот
    квоты не резервировала (и не проходила схемный дедуп D59)."""
    soup = BeautifulSoup(ANCHOR_PAGE, 'html.parser')
    links = asyncio.run(crawler._extract_additional_links(soup, 'https://a.ru/plity/'))
    url, category, _prio = links[0]
    assert category == 'product'              # роль получена по якорю
    assert crawler.url_categorizer.categorize_url(url)[0] == 'other'   # без якоря — нет
    assert asyncio.run(crawler._should_add_to_queue_parallel(url, 1, category)) is True
    assert crawler.url_normalizer.normalize_url(url) in crawler.product_urls


def test_queue_gate_without_category_recomputes(crawler):
    """Обратная совместимость: без переданной роли гейт считает её сам (как раньше)."""
    url = 'https://a.ru/plity/p1.html'
    assert asyncio.run(crawler._should_add_to_queue_parallel(url, 1)) is True
    assert crawler.url_normalizer.normalize_url(url) not in crawler.product_urls


def test_links_from_page_reserve_quota_slot(crawler):
    """Связка целиком: страница -> ссылки -> очередь -> слот квоты."""
    queue = deque()
    parse_result = _page(ANCHOR_PAGE)
    parse_result.additional_links.extend(
        asyncio.run(crawler._extract_additional_links(parse_result.soup, 'https://a.ru/plity/')))
    asyncio.run(crawler._process_links_from_parse_result('https://a.ru/plity/', parse_result, 0, queue))
    assert [item[2] for item in queue] == ['product']
    assert len(crawler.product_urls) == 1


def _starve_exclude_filter(crawler, count=20):
    """Доводит долю отсева глобальным exclude-списком до порога предохранителя."""
    for i in range(count):
        crawler.url_categorizer.categorize_url(f'https://a.ru/test/nasos-nm-{i}/')


def test_failopen_urls_reserve_quota_slots(crawler):
    """F8: URL, возвращённые предохранителем фильтров, идут мимо гейта допуска —
    товарные из них обязаны занимать слот квоты."""
    _starve_exclude_filter(crawler)
    queue = deque()
    assert crawler._apply_exclude_failopen(queue) is True
    assert len(queue) == 20
    assert len(crawler.product_urls) == 20


def test_failopen_respects_quota(crawler):
    """Сверх квоты возвращённые URL идут в вход страховки «второй проход»."""
    crawler.max_product_pages_per_site = 5
    _starve_exclude_filter(crawler)
    queue = deque()
    crawler._apply_exclude_failopen(queue)
    assert len(crawler.product_urls) == 5
    assert len(queue) == 5
    assert len(crawler._quota_rejected_urls) == 15
    assert sum(crawler._queue_gate_rejections.values()) == 15


# ==================== 10. Хвост полосы A: роль из середины пути (P01 U4 п.4) ==

def test_price_in_last_segment_is_price_list(categorizer):
    assert categorizer.categorize_url('https://oao-skai.ru/price-plita.htm') == (
        'price_list', categorizer.priority_levels['price_list'])


def test_price_in_middle_of_path_yields_to_product(categorizer):
    """'/price/nasos-nm-100/' — карточка в разделе «Цены», а не прайс-лист."""
    assert categorizer.categorize_url('https://a.ru/price/nasos-nm-100/')[0] == 'product'


def test_price_in_middle_of_path_downgraded(categorizer):
    """Ни товар, ни категория — роль прайс-листа остаётся, но с пониженным доверием."""
    role, priority = categorizer.categorize_url('https://a.ru/price/arkhiv/2020/')
    assert role == 'price_list'
    assert priority == categorizer.priority_levels['price_list'] - 1


def test_strong_product_path_still_beats_price(categorizer):
    """Регресс D181 (полоса B)."""
    assert categorizer.categorize_url('https://itron.nt-rt.ru/price/product/235255') == (
        'product', 9)
