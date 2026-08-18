DOMAIN = 'zavoduniversal.ru'

import re
from bs4 import BeautifulSoup
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from text_extractor import MarkdownConverter


# Блоки-шум, специфичные для zavoduniversal.ru (самописная CMS «Завод Универсал»).
# Полезное лежит в #content > .production_main_content (заголовок, цена, таблица
# характеристик) и .production_add_content (подробное описание). Всё остальное —
# меню, хлебные крошки, мобильные статьи-дубли, «Похожие товары» — шум.
NOISE_SELECTORS = [
    # шапка / меню / навигация / подвал
    'header', 'footer', 'nav', '.header', '.footer', '.top', '.top_menu',
    '.menu', '.main_menu', '.left_menu', '.phone', '.phones', '.social',
    '.social_wrapper', '.search', '.basket',
    # хлебные крошки
    '.lesenka', '.breadcrumb', '.breadcrumbs',
    # кнопки «задать вопрос / отзыв», блок «преимущества» с иконками
    '.btn_wrapper', '.preim_vanna',
    # мобильные дубли статей, «похожие товары», кнопка возврата, скрытые ссылки
    '.vanny_articles', '.mobile_block', '.sim_product_list', '.back_btn',
    '.lblink', '#footer_marginer',
    # служебное
    'script', 'style', 'noscript', 'iframe', 'svg', 'form',
]


def clean(soup: BeautifulSoup):
    """Удаляет специфичные для zavoduniversal.ru блоки (меню, крошки, «похожие товары», мобильные статьи)."""
    for sel in NOISE_SELECTORS:
        for tag in soup.select(sel):
            tag.decompose()
    # Подпись «Увеличить» у фото товара — отдельные span без класса
    for span in soup.find_all('span'):
        if span.get_text(strip=True) == 'Увеличить':
            span.decompose()


def extract(html: str, base_url: str = '') -> str | None:
    """Карточка товара zavoduniversal.ru: заголовок + основной блок (цена, характеристики) + подробное описание.

    None — если структура не распознана (тогда отработает общий конвертер).
    """
    soup = BeautifulSoup(html, 'lxml')
    clean(soup)

    content = soup.select_one('#content')
    if not content:
        return None

    # Карточка товара: основной блок (цена, характеристики) + подробное описание.
    # Если их нет (категория/обзорная страница) — берём весь #content целиком.
    blocks = content.select('.production_main_content, .production_add_content')
    if not blocks:
        blocks = [content]

    conv = MarkdownConverter(heading_style='ATX', bullets='-')
    parts = []
    for b in blocks:
        md = conv.convert(str(b)).strip()
        if md:
            parts.append(md)

    markdown = '\n\n'.join(parts)
    markdown = re.sub(r'\n{3,}', '\n\n', markdown)
    return markdown.strip() or None
