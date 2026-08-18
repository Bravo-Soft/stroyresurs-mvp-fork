DOMAIN = 'r-vent.ru'

import re
from bs4 import BeautifulSoup
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from text_extractor import MarkdownConverter


# Блоки-шум, специфичные для r-vent.ru (Drupal 8, тема с мегаменю tb-megamenu).
# Полезное содержимое лежит в <article> > div > .field--name-body (заголовок H1,
# описание, таблицы характеристик, изображения). Всё остальное — шум.
NOISE_SELECTORS = [
    # Шапка: автоскрывающийся нав-бар с телефонами и логотипом
    '#autoHidingNavbar',
    # Drupal-регионы шапки/меню
    '.region-first-menu',
    '.region-second-menu',
    # Блок каталога/поиска в шапке
    '.region-catalog',
    '.region-search',
    '#block-rvent-search',
    # Мегаменю tb-megamenu
    '.tb-megamenu',
    # Боковая навигация «Продукция»
    '.region-sidebar-first',
    '#block-sidebarprodukcia',
    # Хлебные крошки
    '.breadcrumb',
    '[class*="breadcrumb"]',
    # Блок хлебных крошек с уникальным id
    '[id^="block-breadcrumbs"]',
    # Ссылки-якоря «Подробнее» (навигация внутри длинной страницы — дубль заголовков)
    '.item-icons',
    # Баннер
    '.region-full-width-banner',
    # Подвал и примыкающие регионы
    '.region-pre-footer',
    '.region-footer',
    '.region-copyright',
    'footer',
    # Блоки контактной формы
    '#block-contactusblock',
    '.contact-row',
    '.contact-form',
    # Служебное
    'script', 'style', 'noscript', 'iframe', 'svg', 'form',
]


def _fix_size_matrix_tables(soup: BeautifulSoup):
    """Исправляет размерные матрицы на страницах воздушных клапанов.

    На сайте r-vent.ru встречаются таблицы формата «H × L», где строки —
    высоты клапана, столбцы — длины, а тело таблицы занято одной большой
    объединённой ячейкой с пояснительным текстом и изображением (colspan=13,
    rowspan=19 и т. п.). MarkdownConverter раскрывает такую ячейку в каждую
    логическую позицию, засоряя Markdown тысячами копий одного абзаца.

    Функция находит такие таблицы (признак: ячейка в tbody с colspan ≥ 3 и
    rowspan ≥ 3) и удаляет эти объединённые ячейки-легенды, оставляя только
    строки с числовыми значениями H.
    """
    for table in soup.find_all('table'):
        tbody = table.find('tbody')
        if not tbody:
            continue
        for cell in tbody.find_all(['td', 'th']):
            try:
                cs = int(cell.get('colspan', 1))
                rs = int(cell.get('rowspan', 1))
            except (ValueError, TypeError):
                continue
            # Крупная объединённая ячейка-легенда или ячейка с изображением
            if cs >= 3 and rs >= 3:
                cell.decompose()


def clean(soup: BeautifulSoup):
    """Удаляет специфичные для r-vent.ru шумовые блоки.

    Сайт построен на Drupal 8 с мегаменю tb-megamenu. Полезный контент
    находится в теге <article> внутри .field--name-body: заголовок H1,
    текстовое описание (div.description / div.pdp), таблицы характеристик
    (div.content-table), технические примечания (div.note) и изображения.
    Всё остальное (меню, хлебные крошки, сайдбар, подвал, контактная форма)
    объявляется шумом и удаляется.
    """
    for sel in NOISE_SELECTORS:
        for tag in soup.select(sel):
            tag.decompose()
    # Исправление размерных матриц с объединёнными ячейками-легендами
    _fix_size_matrix_tables(soup)


def extract(html: str, base_url: str = '') -> str | None:
    """Извлекает карточку товара r-vent.ru в виде Markdown.

    Drupal-страница типа produkcia содержит тег <article> с H1 и блоком
    .field--name-body (описание + таблицы). Адаптер после очистки шума
    конвертирует именно этот блок, а не весь DOM.

    Возвращает None, если структура не распознана — тогда отработает
    общий конвертер text_extractor.html_to_markdown().
    """
    soup = BeautifulSoup(html, 'lxml')
    clean(soup)

    # Основной контейнер — тег <article> страниц типа produkcia
    article = soup.find('article')

    if article:
        conv = MarkdownConverter(heading_style='ATX', bullets='-')
        md = conv.convert(str(article)).strip()
        md = re.sub(r'\n{3,}', '\n\n', md)
        return md.strip() or None

    # Запасной вариант: Drupal-регион основного контента
    region = soup.select_one('.region-content')
    if region:
        conv = MarkdownConverter(heading_style='ATX', bullets='-')
        md = conv.convert(str(region)).strip()
        md = re.sub(r'\n{3,}', '\n\n', md)
        return md.strip() or None

    return None
