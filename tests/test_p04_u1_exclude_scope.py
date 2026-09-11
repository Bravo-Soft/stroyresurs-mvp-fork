"""P04 U1: область применения фильтров, вырезающих сайт целиком.

Exclude-токены сопоставляются посегментно (без хоста, '_' не граница, значения
маршрутных ключей CMS пропускаются), каталожный сигнал сильнее exclude-токена,
базовая локаль сайта не режется языковым фильтром, предохранитель fail-open
возвращает отложенные URL, если фильтр оставил обход без работы.

Коды: D139 (зона .info в хосте), D158 ('info'/'service'/'post' сегментом),
D197 (Joomla 'view=article'), D207 ('profile-catalog'), D251 ('/services/' =
раздел продукции), D283 ('product_info.php'), D211/D260 (локаль сайта).
"""
from collections import deque

import pytest

from config import Config
from web_crawler import SmartURLNormalizer, URLCategorizer, WebCrawler


@pytest.fixture
def categorizer():
    cat = URLCategorizer(Config())
    cat.set_profile(None)
    return cat


# ==================== п.1: exclude только по сегментам пути ====================

def test_d139_host_is_out_of_exclude_scope(categorizer):
    """Зона '.info' в ИМЕНИ ДОМЕНА больше не вырезает сайт целиком."""
    assert categorizer.categorize_url(
        'https://cheboksarsky-trubnyy-zavod.inni.info/produkt/truby-stalnye')[0] == 'product'
    assert categorizer.categorize_url('https://diteks.info/kraski/kraska-fasadnaya')[0] == 'product'
    # контроль: тот же путь на .ru вёл себя так и раньше
    assert categorizer.categorize_url(
        'https://cheboksarsky-trubnyy-zavod.ru/produkt/truby-stalnye')[0] == 'product'


def test_d158_underscore_is_not_a_token_boundary(categorizer):
    """'service' внутри 'service_product' и 'info' внутри 'product_info.php' — не exclude."""
    assert categorizer.categorize_url(
        'https://yokogawa.ru/service_product/detail.php?ID=10276')[0] == 'product'
    assert categorizer.categorize_url(
        'https://etpribor.ru/product_info.php?products_id=123')[0] == 'product'


def test_d158_catalog_signal_beats_exclude_segment(categorizer):
    """'/info/catalog/' и '/post/produkcziya/' — каталог сильнее exclude-токена."""
    role, priority = categorizer.categorize_url('https://www.renar.nnov.ru/info/catalog/')
    assert role == 'product' and priority < categorizer.priority_levels['product']
    role, priority = categorizer.categorize_url('https://okna-visla.ru/post/produkcziya/okna-pvh/')
    assert role == 'product' and priority < categorizer.priority_levels['product']


def test_d207_industry_homonyms_only_as_whole_segment(categorizer):
    """'profile' вырезает URL только целым сегментом; 'profile-catalog' — каталог ПВХ."""
    assert categorizer.categorize_url(
        'https://exprof.pro/proizvoditelyam/kupit-profil/profile-catalog/'
        'okonnye-pvkh-profili-s680-polarika/')[0] == 'product'
    assert categorizer.categorize_url('https://exprof.pro/profile/')[0] == 'excluded2'
    assert categorizer.categorize_url('https://exprof.pro/personal/profile.php')[0] == 'excluded2'


def test_d251_link_text_catalog_signal_beats_exclude(categorizer):
    """Якорь «Продукция» отменяет exclude-токен '/services/' у ЖБИ."""
    assert categorizer.categorize_url(
        'https://www.blok-gbi.ru/services/plity-perekrytiya/', 'Продукция')[0] == 'product'
    # без якоря и без каталожного слага сайт по-прежнему режется — его спасает
    # предохранитель fail-open (см. ниже) либо профиль
    assert categorizer.categorize_url('https://gelios.su/services/gazobetonnye-bloki/')[0] == 'excluded2'


def test_last_segment_matched_without_extension(categorizer):
    """Последний сегмент сверяется по basename без расширения."""
    assert categorizer.categorize_url('https://a.ru/news.html')[0] == 'excluded2'
    assert categorizer.categorize_url('https://a.ru/blog.php')[0] == 'excluded2'


def test_d58_behaviour_preserved(categorizer):
    """Регресс D58: 'press' режет '/press/' и 'press-tsentr', но не 'pressure'."""
    assert categorizer.categorize_url('https://a.ru/press/')[0] == 'excluded2'
    assert categorizer.categorize_url('https://a.ru/press-tsentr/')[0] == 'excluded2'
    assert categorizer.categorize_url('https://a.ru/catalog/datchik-pressure-100/')[0] == 'product'


def test_global_excludes_still_work(categorizer):
    """Страховка от чрезмерного сужения: обычные служебные разделы режутся как прежде."""
    for url in ('https://a.ru/news/2020/', 'https://a.ru/media/sertifikaty/',
                'https://a.ru/about/', 'https://a.ru/blog/kak-vybrat/',
                'https://a.ru/policy/', 'https://a.ru/files/'):
        assert categorizer.categorize_url(url)[0] == 'excluded2', url


# ==================== п.2: query — только значения, вне маршрутных ключей ====

def test_d197_cms_route_query_keys_are_skipped(categorizer):
    assert categorizer.categorize_url(
        'https://www.kinef.ru/index.php?option=com_content&view=article&id=56')[0] == 'product'
    assert categorizer.categorize_url(
        'https://www.kinef.ru/index.php?option=com_content&id=56')[0] == 'product'


def test_query_values_outside_route_keys_still_excluded(categorizer):
    """Не-маршрутный ключ: значение по-прежнему участвует в exclude."""
    assert categorizer.categorize_url('https://a.ru/index.php?section=news')[0] == 'excluded2'


def test_query_keys_themselves_are_not_matched(categorizer):
    """Сопоставляются только ЗНАЧЕНИЯ query, не имена параметров."""
    assert categorizer.categorize_url('https://a.ru/catalog/?news=1')[0] != 'excluded2'


# ==================== п.4: базовая локаль обхода ====================

def test_d211_base_locale_not_excluded(categorizer):
    assert categorizer.categorize_url('https://www.agru.at/en/products/agruline')[0] == 'excluded'
    categorizer.set_base_locale(categorizer.detect_locale_prefix('https://www.agru.at/en/'))
    assert categorizer.categorize_url('https://www.agru.at/en/products/agruline')[0] == 'product'
    # чужая локаль на том же сайте по-прежнему режется
    assert categorizer.categorize_url('https://www.agru.at/de/products/agruline')[0] == 'excluded'


def test_d260_compound_locale_forms(categorizer):
    categorizer.set_base_locale(categorizer.detect_locale_prefix('https://www.bolix.pl/pl/'))
    assert categorizer.base_locale == 'pl'
    for url in ('https://www.bolix.pl/pl/produkt/farby/bolix-sig-kolor/',
                'https://www.bolix.pl/pl-PL/produkt/farby/bolix-sig-kolor/',
                'https://www.bolix.pl/pl_PL/produkt/farby/bolix-sig-kolor/'):
        assert categorizer.categorize_url(url)[0] == 'product', url


def test_detect_locale_prefix(categorizer):
    assert categorizer.detect_locale_prefix('https://a.ru/en/catalog') == 'en'
    assert categorizer.detect_locale_prefix('https://a.pl/pl-PL/produkt') == 'pl-pl'
    assert categorizer.detect_locale_prefix('https://a.ru/catalog/truby') is None
    assert categorizer.detect_locale_prefix('https://a.ru/') is None


def test_base_locale_reset_between_companies(categorizer):
    categorizer.set_base_locale('pl')
    categorizer.set_profile(None)
    assert categorizer.base_locale is None


# ==================== п.5: предохранитель fail-open ====================

def _starve(categorizer, count=25, prefix='https://a.ru/services/'):
    for i in range(count):
        categorizer.categorize_url(f'{prefix}razdel-{i}/')


def test_failopen_releases_exclude_filter(categorizer):
    """Фильтр вырезал почти весь сайт — при пустой очереди он снимается."""
    _starve(categorizer)
    assert categorizer.categorize_url('https://a.ru/services/razdel-0/')[0] == 'excluded2'
    released, deferred = categorizer.release_starved_filters()
    assert released is True
    assert categorizer.exclude_filter_disabled is True
    assert len(deferred) >= 20
    # после снятия фильтра те же URL классифицируются заново
    assert categorizer.categorize_url('https://a.ru/services/razdel-0/')[0] != 'excluded2'


def test_failopen_not_triggered_below_threshold(categorizer):
    """Здоровый сайт: доля отсева ниже порога — фильтр остаётся включённым."""
    _starve(categorizer, count=5)
    for i in range(50):
        categorizer.categorize_url(f'https://a.ru/catalog/truba-{i}/')
    released, deferred = categorizer.release_starved_filters()
    assert released is False and deferred == []
    assert categorizer.exclude_filter_disabled is False


def test_failopen_needs_minimum_sample(categorizer):
    """На трёх URL доля недостоверна — предохранитель не срабатывает."""
    _starve(categorizer, count=3)
    assert categorizer.release_starved_filters() == (False, [])


def test_failopen_releases_language_filter(categorizer):
    for i in range(25):
        categorizer.categorize_url(f'https://a.ru/en/catalog/truba-{i}/')
    released, deferred = categorizer.release_starved_filters()
    assert released is True
    assert categorizer.language_filter_disabled is True
    assert categorizer.categorize_url('https://a.ru/en/catalog/truba-0/')[0] == 'product'


def test_filter_stats_for_metrics(categorizer):
    _starve(categorizer, count=20)
    stats = categorizer.filter_stats()
    assert stats['urls_categorized'] == 20
    assert stats['excluded2_rate'] == 1.0
    assert stats['lang_excluded_rate'] == 0.0
    assert stats['exclude_filter_disabled'] is False


class _QueueOnlyCrawler:
    """Минимальный стенд для хука _apply_exclude_failopen: WebCrawler целиком в
    dev-окружении не конструируется (нужны каталоги контейнера), а хуку нужны только
    категоризатор, нормализатор, visited_urls и метрики."""

    _apply_exclude_failopen = WebCrawler._apply_exclude_failopen

    def __init__(self, categorizer, metrics=None):
        self.url_categorizer = categorizer
        self.url_normalizer = SmartURLNormalizer(categorizer)
        self.visited_urls = set()
        self.metrics_collector = metrics


def test_failopen_hook_requeues_deferred_urls(categorizer):
    """Хук при пустой очереди снимает фильтр и возвращает отложенные URL в обход."""
    _starve(categorizer)
    crawler = _QueueOnlyCrawler(categorizer)
    queue = deque()
    assert crawler._apply_exclude_failopen(queue) is True
    assert len(queue) >= 20
    assert all(category != 'excluded2' for _url, _depth, category, _prio in queue)
    # повторный вызов ничего не делает: фильтры уже сняты
    assert crawler._apply_exclude_failopen(deque()) is False


def test_failopen_hook_reports_rates_to_metrics(categorizer):
    from site_profiles.profile_metrics import RunMetricsCollector

    metrics = RunMetricsCollector('a.ru', 'Тест')
    _starve(categorizer, count=10)
    crawler = _QueueOnlyCrawler(categorizer, metrics)
    crawler._apply_exclude_failopen(deque())
    assert metrics.to_dict()['excluded2_rate'] == 1.0
    assert metrics.to_dict()['lang_excluded_rate'] == 0.0


def test_filter_state_reset_between_companies(categorizer):
    _starve(categorizer)
    categorizer.release_starved_filters()
    categorizer.set_profile(None)
    assert categorizer.exclude_filter_disabled is False
    assert categorizer.filter_stats()['urls_categorized'] == 0
    assert categorizer.categorize_url('https://a.ru/services/razdel-0/')[0] == 'excluded2'
