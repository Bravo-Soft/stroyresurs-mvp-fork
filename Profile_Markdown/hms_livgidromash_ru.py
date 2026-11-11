"""Видимое содержимое насоса без общего каталожного меню перед карточкой."""
import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from text_extractor import MarkdownConverter

DOMAIN = 'hms-livgidromash.ru'


def extract(html, base_url):
    path = urlparse(base_url).path
    if not path.startswith('/catalog/'):
        return None
    soup = BeautifulSoup(html, 'lxml')
    main = soup.select_one('#maincontent')
    if main is None or main.select_one('#catalogElement') is None:
        return None
    # У детальной модели эта секция содержит соседние насосы. На странице
    # серии собственная таблица #marki-harakteristiki-nasosa остаётся целиком.
    selectors = ['#right-bar', '#svyazannye-materialy-nasosa', 'nav', 'form']
    if re.search(r'_model_\d+\.html$', path):
        selectors.append('#vse-marki')
    for selector in selectors:
        for node in main.select(selector):
            node.decompose()
    return MarkdownConverter(heading_style='ATX', bullets='-',
                             strip=['script', 'style']).convert(str(main)).replace('˜', '~')


def restore_model_name(ai_data, html, url):
    """Одно обозначение насоса может иметь несколько опубликованных режимов."""
    parsed = urlparse(url)
    if (parsed.hostname or '').lower().removeprefix('www.') != DOMAIN:
        return
    # В старых сохранённых ответах модели этот знак ещё встречается в ключах.
    ai_data.update(json.loads(json.dumps(ai_data, ensure_ascii=False).replace('˜', '~')))
    if not re.search(r'_model_\d+\.html$', parsed.path):
        return
    product = ai_data.get('product')
    if not isinstance(product, dict):
        return
    soup = BeautifulSoup(html, 'lxml')
    heading = soup.select_one('#maincontent h1')
    if heading is None:
        return
    from source_product_tables import extract_source_tables
    labels = [('Подача', 'Q'), ('Напор', 'H'),
              ('Давление на выходе', 'P'), ('Частота вращения', 'n')]
    qualifiers = []
    series = ''
    for table in extract_source_tables(html, url):
        if table['headers'] != ['Характеристика', 'Значение']:
            continue
        rpm = any(key.startswith('Частота вращения') and 'об/мин' in key for key, _ in table['rows'])
        for key, value in table['rows']:
            if key == 'Серия':
                series = value
            for prefix, symbol in labels:
                if key.startswith(prefix) and value and ',' in key:
                    if symbol == 'n' and rpm and 'об/мин' not in key:
                        continue
                    unit = re.sub(r'\s+не (?:менее|более)$', '', key.split(',')[1].strip())
                    # В заголовках карточек '/' заменяется на '-', поэтому единицы
                    # режима записываем словами; исходные подписи таблицы не меняются.
                    unit = unit.replace('м³/ч', 'м^3 в час').replace('кгс/см²', 'кгс на см^2')
                    unit = unit.replace('об/мин', 'об в мин').replace('¹', '^1')
                    operator = '>=' if 'не менее' in key else '<=' if 'не более' in key else '='
                    qualifiers.append(f'{symbol}{operator}{value} {unit}')
    name = re.split(r'\s+-\s+(?=[а-яё]{3})', heading.get_text(' ', strip=True), maxsplit=1)[0].rstrip(' -')
    if series:
        name += ', серия ' + series
    suffix = re.search(r'\s+(\([^()]+\))\s*$', product.get('product_name', ''))
    mode = ' (' + '; '.join(qualifiers) + ')' if qualifiers else ''
    product['product_name'] = name + mode + (suffix.group(0) if suffix else '')
