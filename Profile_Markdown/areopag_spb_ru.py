DOMAIN = 'areopag-spb.ru'

import re
import sys
import os
from bs4 import BeautifulSoup, Tag

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from text_extractor import MarkdownConverter


# areopag-spb.ru (Bitrix): карточка товара-семейства (D56).
# Полезное: h1, .productCard__mainParams (пары .parameter__title/.parameter__value),
# .plate-content__text (развёрнутое описание изделия), .detail-section (разделы:
# «Таблица приводов», конструкция и т.п.), модельный ряд — ссылки на подстраницы
# модификаций (/nd_..._m8l/ и т.п.). Общий конвертер терял описание и параметры:
# markdown выходил ~13 строк, и настоящая товарная страница резалась гейтом.
NOISE_SELECTORS = [
    'header', 'footer', 'nav', '.breadcrumbs', '.breadcrumb',
    '.productCard__nav',          # вкладки-ссылки «Сервис/Опросный лист/Обслуживание»
    '.productCard__order',        # кнопки заказа
    'script', 'style', 'noscript', 'iframe', 'svg', 'form',
]


def clean(soup: BeautifulSoup):
    for sel in NOISE_SELECTORS:
        for tag in soup.select(sel):
            tag.decompose()


def extract(html: str, base_url: str = '') -> str | None:
    """Сборка markdown карточки areopag-spb.ru из явных блоков страницы.
    Возвращает None, если структура не распознана (отработает общий конвертер)."""
    soup = BeautifulSoup(html, 'lxml')

    h1 = soup.find('h1')
    main_params = soup.select('.productCard__mainParams .parameter')
    plate_text = soup.select_one('.plate-content__text')
    if not h1 or not (main_params or plate_text):
        return None  # не карточка товара — общий конвертер

    # собрать ссылки модельного ряда ДО clean (могут лежать в скрытых строках hidden_row)
    seen = set()
    model_range = []
    h1_text = h1.get_text(strip=True)
    for a in soup.find_all('a'):
        href = a.get('href') or ''
        txt = a.get_text(strip=True)
        # модификации живут на подстраницах текущего раздела
        if txt and href.startswith('/') and href.rstrip('/') != '' and \
           re.search(r'/nd_[a-z0-9_]+/?$', href):
            if txt not in seen:
                seen.add(txt)
                model_range.append(txt)

    clean(soup)
    conv = MarkdownConverter()

    parts = [f'# {h1_text}', '']

    if main_params:
        parts.append('| Параметр | Значение |')
        parts.append('| --- | --- |')
        for p in main_params:
            t = p.select_one('.parameter__title')
            v = p.select_one('.parameter__value')
            if t and v:
                key = t.get_text(strip=True).replace('|', '\\|')
                val = v.get_text(strip=True).replace('|', '\\|')
                if key and val:
                    parts.append(f'| {key} | {val} |')
        parts.append('')

    if plate_text:
        desc_md = conv.convert(str(plate_text)).strip()
        if desc_md:
            parts.append(desc_md)
            parts.append('')

    for section in soup.select('.detail-section'):
        sec_md = conv.convert(str(section)).strip()
        if sec_md:
            parts.append(sec_md)
            parts.append('')

    if model_range:
        parts.append('### Модельный ряд')
        parts.append('')
        for name in model_range:
            parts.append(f'- {name}')
        parts.append('')

    markdown = '\n'.join(parts)
    markdown = re.sub(r'\n{3,}', '\n\n', markdown)
    return markdown.strip() or None
