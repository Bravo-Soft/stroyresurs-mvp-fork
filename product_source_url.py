"""Ссылки на товары Салаватстекло после перехода сайта на ЧПУ."""
import re
from urllib.parse import parse_qs, urlparse


# Соответствия подтверждены страницами каталога salstek.ru 12.10.2026.
# Ключ — название из HTML старой страницы, а не ненадёжный element.php?id=…
_SALSTEK_PRODUCT_PATHS = {
    'MultiComfort Select One': 'arkhitekturnoe-steklo/multicomfort-select-one/',
    'MultiComfort Neutral PRO': 'arkhitekturnoe-steklo/multicomfort-neutral-pro/',
    'MultiComfort Next': 'arkhitekturnoe-steklo/multicomfort-next/',
    'MultiComfort Neutral': 'arkhitekturnoe-steklo/multicomfort-neutral/',
    'MultiComfort Blue': 'arkhitekturnoe-steklo/multicomfort-blue/',
    'MultiComfort Silver': 'arkhitekturnoe-steklo/multicomfort-silver/',
    'MultiComfort Light Bronze': 'arkhitekturnoe-steklo/multicomfort-light-bronze/',
    'Comfort Neutral / Elit+': 'arkhitekturnoe-steklo/comfort-neutral-elit/',
    'Comfort Blue': 'arkhitekturnoe-steklo/comfort-blue/',
    'Comfort Silver / Comfort Grey': 'arkhitekturnoe-steklo/comfort-silver-comfort-grey/',
    'Comfort Green': 'arkhitekturnoe-steklo/comfort-green/',
    'Comfort Bronze': 'arkhitekturnoe-steklo/comfort-bronze/',
    'Standart ST Plus': 'arkhitekturnoe-steklo/standart-st-plus/',
    'Ultraview R': 'interernoe-steklo/ultraview-r/',
    'Extraview': 'okonnoe-steklo/extraview/',
    'Privaline': 'interernoe-steklo/privaline/',
    'Clearview': 'interernoe-steklo/clearview/',
    'Прозрачное стекло': 'okonnoe-steklo/prozrachnoe-steklo/',
    'Standart Low-E': 'okonnoe-steklo/standart-low-e/',
    'MF': 'okonnoe-steklo/mf/',
    'Стекло закаленное для наземного транспорта':
        'bezopasnoe-steklo-i-steklopakety/steklo-zakalennoe-dlya-nazemnogo-transporta/',
    'Стекло закаленное строительного и бытового назначения':
        'bezopasnoe-steklo-i-steklopakety/steklo-zakalennoe-stroitelnogo-i-bytovogo-naznacheniya/',
    'Огнестойкое закаленное стекло':
        'bezopasnoe-steklo-i-steklopakety/ognestoykoe-zakalennoe-steklo/',
    'Стекло защитное многослойное пулестойкое':
        'bezopasnoe-steklo-i-steklopakety/steklo-zaschitnoe-mnogosloynoe-pulestoykoe/',
    'Стекло многослойное безопасное для наземного транспорта':
        'bezopasnoe-steklo-i-steklopakety/steklo-mnogosloynoe-bezopasnoe-dlya-nazemnogo-transporta/',
    'Стеклопакеты для наземного транспорта':
        'bezopasnoe-steklo-i-steklopakety/steklopakety-dlya-nazemnogo-transporta/',
    'Стеклопакеты строительного назначения':
        'bezopasnoe-steklo-i-steklopakety/steklopakety-stroitelnogo-naznacheniya/',
    'Силикат натрия растворимый': 'silikat-natriya/silikat-natriya-rastvorimyy/',
}


def _title_key(title: str) -> str:
    """Убирает оформление торговой марки, сохраняя точное название и вариант."""
    return re.sub(r'[^\w]+', '', title.casefold().replace('ё', 'е'))


_SALSTEK_PRODUCT_PATHS = {_title_key(k): v for k, v in _SALSTEK_PRODUCT_PATHS.items()}


def resolve_product_source_url(url: str, html_content: str) -> str:
    """Исправляет только старые ссылки salstek.ru с известным товаром в HTML."""
    parsed = urlparse(url)
    if (parsed.scheme not in ('http', 'https')
            or parsed.hostname not in ('salstek.ru', 'www.salstek.ru')
            or not re.fullmatch(r'/products/(arh|inter|window|saveglass|silikaty)/element\.php', parsed.path)
            or not parse_qs(parsed.query).get('id')):
        return url

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_content, 'html.parser')
    breadcrumb = soup.select_one('ul.breadcrumb')
    items = breadcrumb.find_all('li') if breadcrumb else []
    if not items:
        return url
    product_path = _SALSTEK_PRODUCT_PATHS.get(_title_key(items[-1].get_text(' ', strip=True)))
    if not product_path:
        return url
    return 'https://salstek.ru/products/' + product_path
