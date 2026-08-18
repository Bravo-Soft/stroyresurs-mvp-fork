DOMAIN = 'pgsoyuz.ru'

import re
from urllib.parse import urlparse
from bs4 import BeautifulSoup
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from text_extractor import MarkdownConverter


# Блоки-шум, специфичные для pgsoyuz.ru (Промышленная группа Союз).
# Полезный контент лежит в:
#   -- карточке товара: .pd-title (заголовок), .pd-props (таблица характеристик),
#      .pd-docs (документы/ссылки), .pd-view__img (главное фото товара);
#   -- страницах-каталогах: .catalog-content (сетка карточек).
# Всё остальное — шапка, навигация, хлебные крошки, галерея thumbnail-ов,
# «рекомендуемые/похожие», лайтбоксы, баннеры, куки — шум.
NOISE_SELECTORS = [
    # Шапка, логотип, меню, поиск, языковой переключатель
    '.pg-header', '#header', 'header',
    'nav', '.bx-top-nav-container',
    # Хлебные крошки (два варианта разметки)
    '.pd-breadcrumbs', '.bx-breadcrumb',
    # Баннер раздела и его текстовый оверлей
    '.section-banner', '.slider-text-overlay',
    # Шапка раздела каталога (повторяет название без содержимого)
    '.section-head',
    # Боковая колонка с фильтрами (каталог)
    '.catalog-sidebar', '.catalog-filter', '#catalogFilter',
    # Thumbnail-слайдер (дублирует главное фото в мелком виде)
    '.pd-thumbs',
    # Навигационные кнопки внутри просмотрщика изображений
    '.pd-view__nav', '.pd-loupe',
    # Лайтбокс, видеомодал, всплывающий тип текстуры, PDF-просмотрщик
    '.pd-lightbox', '#pdLb',
    '.pd-video-modal', '#pdVideoModal',
    '.pd-texture-tooltip', '#pdTextureTooltip',
    '.pdfbox', '#pdf-lightbox',
    # Кнопка «Вернуться в каталог»
    '.pd-back',
    # «Рекомендуемые сочетания», «Может быть интересно» и аналогичные блоки
    '.pd-rel-wrap', '.rec',
    # Кнопка «Показать все характеристики» и декоративная иконка
    '.pd-props__more', '.pd-props__icon',
    # Декоративные бейджи в заголовке товара («НОВИНКА», «ФОРМАТ» и т.п.)
    '.pd-badge',
    # Кнопка прокрутки вверх, баннер cookie
    '.scroll-top', '.cookie-banner',
    # Подвал
    '#footer', 'footer',
    # Служебные теги
    'script', 'style', 'noscript', 'iframe', 'svg', 'form',
]


def clean(soup: BeautifulSoup):
    """Удаляет специфичные для pgsoyuz.ru блоки-шум (меню, хлебные крошки,
    лайтбоксы, похожие/рекомендуемые, баннеры, куки, подвал)."""
    for sel in NOISE_SELECTORS:
        for tag in soup.select(sel):
            tag.decompose()

    # Скрытый дубль заголовка страницы (#pagetitle) -- на детальных страницах товара
    # заголовок уже есть в .pd-title; удаляем дубль, чтобы не повторять H1 дважды.
    pt = soup.find(id='pagetitle')
    if pt and soup.select_one('.pd-title'):
        pt.decompose()


def _build_image_url(src, base_url):
    """Дополняет относительный путь к изображению схемой и доменом.

    base_url может содержать фрагмент (#...) или путь -- берём только схему и хост.
    """
    if not src.startswith('/'):
        return src
    if not base_url:
        return src
    parsed = urlparse(base_url)
    origin = (parsed.scheme + '://' + parsed.netloc) if parsed.netloc else ''
    return (origin + src) if origin else src


def extract(html, base_url=''):
    """Специализированное извлечение для страниц pgsoyuz.ru.

    Карточка товара: заголовок + главное изображение + таблица характеристик
    (.pd-props__table) + ссылки на документацию (.pd-docs).
    Страница каталога: сетка товаров (.catalog-content).
    Fallback -- весь #content, если структура нестандартная.

    Возвращает Markdown или None (тогда отрабатывает общий конвертер).
    """
    soup = BeautifulSoup(html, 'lxml')
    clean(soup)

    conv = MarkdownConverter(heading_style='ATX', bullets='-')

    # --- Карточка товара (детальная страница) ---
    # Признак: есть .pd-title (H1 с классом) и .pd-props (таблица характеристик).
    # Бейджи .pd-badge уже удалены в clean(), заголовок чист.
    pd_title = soup.select_one('.pd-title')
    pd_props = soup.select_one('.pd-props')

    if pd_title and pd_props:
        parts = []

        # Заголовок товара
        title_text = pd_title.get_text(strip=True)
        if title_text:
            parts.append('# ' + title_text)

        # Главное изображение товара
        img_main = soup.select_one('.pd-view__img')
        if img_main:
            src = img_main.get('src', '') or img_main.get('data-src', '')
            if src:
                src = _build_image_url(src, base_url)
                parts.append('![](' + src + ')')

        # Таблица характеристик (.pd-props содержит .pd-props__table)
        tbl = soup.select_one('.pd-props__table')
        if tbl:
            tbl_md = conv.convert(str(tbl)).strip()
            if tbl_md:
                parts.append('## Характеристики\n\n' + tbl_md)

        # Документация (.pd-docs: ссылки на PDF и нормы загрузки транспорта)
        pd_docs = soup.select_one('.pd-docs')
        if pd_docs:
            docs_md = conv.convert(str(pd_docs)).strip()
            if docs_md:
                parts.append('## Документация\n\n' + docs_md)

        if parts:
            markdown = '\n\n'.join(parts)
            markdown = re.sub(r'\n{3,}', '\n\n', markdown)
            return markdown.strip()

    # --- Страница каталога / листинг ---
    catalog = soup.select_one('.catalog-content')
    if catalog:
        md = conv.convert(str(catalog)).strip()
        if md:
            md = re.sub(r'\n{3,}', '\n\n', md)
            return md.strip()

    # --- Общий fallback: весь #content ---
    content = soup.select_one('#content')
    if content:
        md = conv.convert(str(content)).strip()
        if md:
            md = re.sub(r'\n{3,}', '\n\n', md)
            return md.strip()

    return None