DOMAIN = 'elconnect.ru'

import re
from bs4 import BeautifulSoup
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from text_extractor import MarkdownConverter


# Блоки-шум на elconnect.ru (Bootstrap-CMS «Softart»).
# Полезный контент карточки товара лежит в .col-sm-9.col-xs-12:
#   h1 + .row.good.clearfix (изображение + краткое описание)
#   + .page_product_box (таблица характеристик, расширенное описание, документы).
# Последний .page_product_box «Другие товары из этого раздела» — шум, он
# удаляется по ключевому тексту заголовка.
# На страницах-категориях полезен .row_category (описание группы товаров).
NOISE_SELECTORS = [
    # шапка: верхняя панель навигации с вкладками (бренды / категории)
    '.nav-tabs-container',
    # шапка: меню, поиск, корзина, логотип
    '.container-wrap',
    # основная навигация (дерево категорий)
    '.nav-container',
    # мета-шапка (header) и подвал
    'header', 'footer',
    # хлебные крошки
    '.breadcrumbs',
    # всплывающее окно «Позвоните нам»
    '.win-tcall',
    # форма обратного звонка
    '.win-price', '.win-price-ul',
    # фильтр товаров в боковой колонке
    '.filterbox',
    # строка сортировки и переключатель вида (сетка / список)
    '.shop_box_row',
    # блок «Мы в соцсетях»
    '.blocksocial',
    # нижняя правовая панель (куки)
    '.legal-panel',
    # скрытая форма удаления
    'form.hide',
    # ссылка «Скачать прайс-лист» внутри h1 (встроена в .pricelist span)
    '.pricelist',
    # служебное
    'script', 'style', 'noscript', 'iframe', 'svg',
]


def clean(soup):
    """Удаляет специфичные для elconnect.ru блоки (меню, хлебные крошки,
    фильтр, «другие товары», соцсети, куки и т.п.)."""
    for sel in NOISE_SELECTORS:
        for tag in soup.select(sel):
            tag.decompose()

    # Блок «Другие товары из этого раздела» — .page_product_box с таким заголовком
    for box in soup.select('.page_product_box'):
        h = box.find(['h2', 'h3', 'h4'])
        if h and 'другие товары' in h.get_text(strip=True).lower():
            box.decompose()

    # Кнопка добавления в корзину в карточках товаров
    for tag in soup.select('.add_cart_box'):
        tag.decompose()


def extract(html, base_url=''):
    """Извлечение контента карточки / группы товаров elconnect.ru.

    Структура одиночной карточки:
      .col-sm-9.col-xs-12
        h1
        .row.good.clearfix
          .col-sm-5  — изображение(я)
          .col-sm-7  — краткое описание + цена
        .page_product_box   — «Характеристики» (таблица tbl-cat)
        .page_product_box   — «Подробнее» (полная таблица + текст)
        .page_product_box   — документы для скачивания

    Структура страницы-категории:
      .col-sm-9.col-xs-12
        h1
        .row -> .col-sm-9.col-xs-12
          .row_category  — описание группы
          ul.row.grid    — сетка товаров с мини-карточками (tbl-cat)

    Возвращает None, если структура не распознана (тогда отработает общий метод).
    """
    soup = BeautifulSoup(html, 'lxml')
    clean(soup)

    # Ищем основной контентный столбец
    all_col9 = soup.select('.col-sm-9.col-xs-12')
    if not all_col9:
        return None

    main_col = all_col9[0]

    # Заголовок страницы
    h1 = main_col.find('h1') or soup.find('h1')
    title_text = h1.get_text(strip=True) if h1 else ''

    parts = []
    if title_text:
        parts.append('# ' + title_text)

    conv = MarkdownConverter(heading_style='ATX', bullets='-')

    # Одиночная карточка товара (признак: есть .row.good)
    good_row = main_col.select_one('.row.good')
    if good_row:
        # Краткое описание (правая колонка)
        desc_col = good_row.select_one('.col-xs-12.col-sm-7') or good_row.select_one('.col-xs-12.col-md-8')
        if desc_col:
            for price_div in desc_col.select('.good_price'):
                price_div.decompose()
            desc_md = conv.convert(str(desc_col)).strip()
            if desc_md:
                parts.append(desc_md)

        # Изображения (левая колонка): только крупные (не миниатюры 73x73)
        img_col = good_row.select_one('.col-xs-12.col-sm-5') or good_row.select_one('.col-xs-12.col-md-4')
        if img_col:
            seen_imgs = set()
            img_lines = []
            for img in img_col.find_all('img'):
                src = img.get('src', '')
                if '/73x' in src or '/50x' in src or '/100x' in src:
                    continue
                if src and src not in seen_imgs:
                    seen_imgs.add(src)
                    alt = img.get('alt', '').strip()
                    img_lines.append('![' + alt + '](' + src + ')')
            if img_lines:
                parts.append('\n'.join(img_lines))

        # .page_product_box — характеристики, расширенное описание, документы
        for box in main_col.select('.page_product_box'):
            box_md = conv.convert(str(box)).strip()
            if box_md:
                parts.append(box_md)

        if len(parts) > 1:
            markdown = '\n\n'.join(parts)
            markdown = re.sub(r'\n{3,}', '\n\n', markdown)
            return markdown.strip()

    # Страница-категория (нет .row.good, но есть .row_category или ul.row.grid)
    inner_row = main_col.select_one('.row')
    content_col = inner_row.select_one('.col-sm-9.col-xs-12') if inner_row else None

    if content_col:
        cat_desc = content_col.select_one('.row_category')
        if cat_desc:
            desc_md = conv.convert(str(cat_desc)).strip()
            if desc_md:
                parts.append(desc_md)

        grid = content_col.select_one('ul.row.grid')
        if grid:
            grid_md = conv.convert(str(grid)).strip()
            if grid_md:
                parts.append(grid_md)

        if len(parts) > 1:
            markdown = '\n\n'.join(parts)
            markdown = re.sub(r'\n{3,}', '\n\n', markdown)
            return markdown.strip()

    # Запасной вариант: весь main_col целиком
    fallback_md = conv.convert(str(main_col)).strip()
    if fallback_md:
        markdown = re.sub(r'\n{3,}', '\n\n', fallback_md)
        return markdown.strip()

    return None