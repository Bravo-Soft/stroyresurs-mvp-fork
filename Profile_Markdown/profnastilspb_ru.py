DOMAIN = 'profnastilspb.ru'

import re
import json
from bs4 import BeautifulSoup
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from text_extractor import MarkdownConverter


def clean(soup: BeautifulSoup):
    """Удаляет специфичные для profnastilspb.ru блоки.

    Сайт построен на WordPress/WooCommerce (тема jb-*). Полезный контент лежит
    в `.jb-product__tabs` (описание/параметры/назначение) и в скрытом JSON
    `#jbVariationsObj` (варианты, цены, цвета). Всё остальное — интерактивный
    Vue-конфигуратор, галереи, меню, футер — это шум.

    ВАЖНО: не удаляем `#jbVariationsObj` — из него extract() достаёт варианты и цвета.
    """
    selectors = [
        # Шапка, меню, навигация
        'header', '.jb-header', '.jb-menu', '.site-navigation',
        '.jb-menu__toggle', '.jb-menu__content', '.jb-menu__gallery',
        # Хлебные крошки
        '.jb-breadcrumbs__container', '.breadcrumbs',
        # Интерактивный конфигуратор и галерея товара (Vue-шаблоны с {{ }})
        '.jb-product__gallery', '.jb-product__props', '.jb-product__calc',
        # Кросс-сейл «С этим товаром покупают»
        '.jb-product__sidebar',
        # Всплывающие окна, баннеры, куки
        '.jb-popup', '.jbc-cookie-content', '.jbc-cookie',
        # Управление вкладками и кнопка «Показать больше»
        '.tabs__header', '.tabs__show-more',
        # Слайдеры / декоративные иконки / служебное
        '.swiper-button-prev', '.swiper-button-next', '.swiper-pagination',
        'svg', 'script', 'style', 'noscript', 'iframe',
        # Футер
        'footer', '.jb-footer',
    ]
    for sel in selectors:
        for tag in soup.select(sel):
            tag.decompose()


def _parse_variations(soup: BeautifulSoup) -> str:
    """Достаёт варианты и цвета из скрытого JSON `#jbVariationsObj`.

    На страницах-конфигураторах (например, профили ЛСТК) это единственный
    источник характеристик: толщины, покрытия, цены, масса и палитра RAL.
    Полностью защищённый разбор — при любой ошибке возвращает пустую строку.
    """
    inp = soup.select_one('#jbVariationsObj')
    if not inp or not inp.get('value'):
        return ''
    try:
        data = json.loads(inp.get('value'))
    except Exception:
        return ''

    parts = []

    # --- Варианты исполнения ---
    rows = []
    for v in (data.get('variations_map') or []):
        if not isinstance(v, dict):
            continue
        name = (v.get('name') or '').strip()
        if not name:
            continue
        price = (v.get('regular_price') or '').strip()
        # Нулевая цена на сайте означает «Под запрос» — не выводим её как «0»
        if price in ('0', '0.0', '0.00'):
            price = ''
        weight = (v.get('jb_weight') or '').strip()
        min_l = (v.get('min_length') or '').strip()
        max_l = (v.get('max_length') or '').strip()
        if min_l and max_l:
            length = f'{min_l}–{max_l}'
        else:
            length = max_l or min_l or ''
        rows.append((name, price, weight, length))
    if rows:
        # D57 (замечание заказчика, «Планка ендовы нижняя 300×300»): таблица с прочерками и
        # повторами нечитаема — редактор такую не внёс бы. Поэтому:
        #   • столбец, ПУСТОЙ у всех вариантов («Длина | — | —»), не выводим вовсе;
        #   • атрибут, ОДИНАКОВЫЙ у всех вариантов («Масса, кг: 4.5»), выносим общей парой
        #     над таблицей (вес приходит из JSON вариаций WooCommerce — jb_weight);
        #   • в таблице остаются только реально различающиеся столбцы.
        headers = ['Наименование', 'Цена, ₽', 'Масса, кг', 'Длина, мм']
        cols = list(zip(*rows))
        keep, common = [], []
        for idx in range(1, len(headers)):
            vals = [(v or '').strip() for v in cols[idx]]
            non_empty = [v for v in vals if v]
            if not non_empty:
                continue  # пуст у всех — столбец не нужен
            if len(set(non_empty)) == 1 and len(non_empty) == len(vals):
                common.append((headers[idx], non_empty[0]))  # одинаков у всех — общая пара
            else:
                keep.append(idx)
        parts.append('## Варианты исполнения\n')
        for k, v in common:
            parts.append(f'{k}: {v}')
        if common:
            parts.append('')
        if keep:
            parts.append('| ' + ' | '.join(['Наименование'] + [headers[i] for i in keep]) + ' |')
            parts.append('|' + ' --- |' * (1 + len(keep)))
            for r in rows:
                parts.append('| ' + ' | '.join([r[0]] + [(r[i] or '—') for i in keep]) + ' |')
        else:
            # различий между вариантами нет — просто перечень исполнений
            for r in rows:
                parts.append(f'- {r[0]}')
        parts.append('')

    # --- Палитра цветов (RAL) ---
    colors = []
    for c in (data.get('colors_map') or []):
        if not isinstance(c, dict):
            continue
        title = (c.get('title') or '').strip()
        if not title:
            continue
        desc = (c.get('desc') or '').strip()
        colors.append(f'- {title} — {desc}' if desc else f'- {title}')
    if colors:
        parts.append('## Доступные цвета (RAL)\n')
        parts.extend(colors)

    return '\n'.join(parts).strip()


def extract(html: str, base_url: str = '') -> str | None:
    """Специализированное извлечение для карточек товаров profnastilspb.ru.

    Возвращает Markdown: заголовок + содержимое вкладок (описание/параметры/
    назначение) + таблица вариантов и палитра цветов из JSON. None — если на
    странице нет ни осмысленного контента вкладок, ни данных вариантов
    (тогда отрабатывает общий конвертер).
    """
    soup = BeautifulSoup(html, 'lxml')
    clean(soup)

    # Заголовок товара
    h1 = soup.find('h1', class_='product_title') or soup.find('h1')
    title_text = ''
    if h1:
        title_text = h1.get_text(strip=True)
    elif soup.title and soup.title.string:
        title_text = soup.title.string.strip()

    parts = []
    if title_text:
        parts.append(f'# {title_text}')

    # Контент вкладок (описание / параметры / назначение)
    tabs = soup.select_one('.jb-product__tabs')
    tabs_md = ''
    if tabs:
        conv = MarkdownConverter(heading_style='ATX', bullets='-')
        tabs_md = conv.convert(str(tabs)).strip()
        if tabs_md:
            parts.append(tabs_md)

    # Варианты и цвета из скрытого JSON
    variations_md = _parse_variations(soup)
    if variations_md:
        parts.append(variations_md)

    # Если кроме заголовка ничего не нашли — пусть отработает общий метод
    if not tabs_md and not variations_md:
        return None

    markdown = '\n\n'.join(parts)
    markdown = re.sub(r'\n{3,}', '\n\n', markdown)
    return markdown.strip()
