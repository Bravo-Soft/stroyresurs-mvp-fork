DOMAIN = 'aerobel.ru'

import re
from bs4 import BeautifulSoup
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from text_extractor import MarkdownConverter


# Шумовые блоки внутри section товарной карточки aerobel.ru (1C Bitrix).
#
# КРИТИЧЕСКОЕ ОГРАНИЧЕНИЕ: lxml некорректно парсит HTML этого сайта. Элемент
# <main> оказывается вложен в .header-nav > ul.nav > li.nav-parent > .nav-submenu
# (5 уровней внутри <header>). Поэтому ни 'header', ни '.header-nav', ни 'nav',
# ни '.nav-submenu' удалять НЕЛЬЗЯ — они захватят весь полезный контент.
#
# Стратегия clean(): не трогаем внешнюю навигационную обёртку.
# Вместо этого extract() напрямую адресует section (продуктовый блок) и точечно
# вычищает шум внутри него. NOISE_IN_SECTION удаляются только внутри section.
NOISE_IN_SECTION = [
    # Строка поиска
    '.k_header_search',
    # Хлебные крошки
    '[class*="breadcrumb"]', '[class*="crumb"]',
    # Панель «поделиться» (Яндекс Share)
    '.yandex-share-panel', '[class*="share"]',
    # Иконка сравнения
    '.compare-ico',
    # Блок цены, корзины и кнопок заказа
    '.price_block', '.product-amount', '.product-item-detail-pay-block',
    '[data-entity="main-button-container"]', '.main-btn',
    # Промо-блок «Скидки от объёма»
    '.promo-discount-block', '[class*="promo"]',
    # Навигация по вкладкам (только кнопки-заголовки, содержимое вкладок нужно)
    '.product-item-detail-tabs-container', '.product-item-detail-tabs-list',
    # Слайдер (изображение извлекается до чистки)
    '.product-item-detail-slider-container', '.product-item-detail-slider-close',
    # Баннер куки и всплывающие окна
    '[class*="cookie"]', '[class*="banner"]', '[class*="modal"]',
    # Похожие/рекомендуемые товары
    '[class*="similar"]', '[class*="related"]', '[class*="recommend"]',
    # Служебное
    'script', 'style', 'noscript', 'iframe', 'svg', 'form',
]


def clean(soup: BeautifulSoup):
    """Минимальная очистка: удаляет только гарантированно безопасные элементы
    (script/style/noscript) из всего документа. Основная шумоочистка происходит
    в extract() точечно внутри section.

    lxml вкладывает <main> внутрь .header-nav из-за невалидного HTML сайта,
    поэтому навигационные блоки верхнего уровня трогать нельзя.
    """
    for sel in ('script', 'style', 'noscript', 'iframe', 'svg'):
        for tag in soup.select(sel):
            tag.decompose()


def _dl_to_md(container) -> str:
    """Преобразует список dl.product-item-detail-properties в Markdown-таблицу
    «Характеристика | Значение»."""
    rows = []
    for dl in container.select('dl.product-item-detail-properties'):
        dt = dl.find('dt')
        dd = dl.find('dd')
        if dt and dd:
            key = dt.get_text(strip=True)
            val = dd.get_text(strip=True)
            if key and val:
                rows.append(f'| {key} | {val} |')
    if not rows:
        return ''
    header = '| Характеристика | Значение |\n| --- | --- |'
    return header + '\n' + '\n'.join(rows)


def extract(html: str, base_url: str = '') -> str | None:
    """Специализированное извлечение для карточек товаров aerobel.ru (1C Bitrix).

    Из-за lxml-артефакта (весь <main> вложен в .header-nav) используем
    прямую адресацию section как корневого контейнера. Шум внутри section
    удаляется точечно по NOISE_IN_SECTION.

    Структура section:
      h1                              — название товара
      .product-item-detail-info-section — краткие характеристики (dl)
      .product-item-detail-tab-content[data-value=description]  — описание
      .product-item-detail-tab-content[data-value=properties]   — полные характеристики (dl)
      .product-item-detail-tab-content[data-value=certs]        — сертификаты (ссылки)

    Возвращает Markdown или None (тогда отрабатывает общий конвертер).
    """
    soup = BeautifulSoup(html, 'lxml')

    # Изображение товара берём до шумоочистки — слайдер будет удалён
    product_img_md = ''
    slider = soup.select_one('.product-item-detail-slider-container')
    if slider:
        img = slider.find('img')
        if img and img.get('src'):
            src = img['src']
            alt = img.get('alt', '').strip()
            product_img_md = f'![{alt}]({src})'

    # Глобальная очистка безопасных служебных элементов
    clean(soup)

    # Находим section — корневой контейнер продуктовой страницы
    sec = soup.find('section')
    if not sec:
        return None

    # Точечная чистка шума внутри section
    for sel in NOISE_IN_SECTION:
        for tag in sec.select(sel):
            tag.decompose()

    # Удаляем строку «Выберите один из подарков» внутри .main_product
    mp = sec.select_one('.main_product')
    if mp:
        for row in mp.select('.row'):
            if 'подарок' in row.get_text():
                row.decompose()

    conv = MarkdownConverter(heading_style='ATX', bullets='-')
    parts = []

    # --- Заголовок товара ---
    h1 = sec.find('h1', class_=False) or sec.find('h1')
    title = h1.get_text(strip=True) if h1 else ''
    if title:
        parts.append(f'# {title}')

    # --- Изображение ---
    if product_img_md:
        parts.append(product_img_md)

    # --- Краткие характеристики из info-section ---
    info_section = sec.select_one('.product-item-detail-info-section')
    if info_section:
        props_md = _dl_to_md(info_section)
        if props_md:
            parts.append('## Основные параметры\n\n' + props_md)

    # --- Вкладки: описание, полные характеристики, сертификаты ---
    tab_contents = sec.select('.product-item-detail-tab-content')
    for tab in tab_contents:
        data_value = tab.get('data-value', '')

        if data_value == 'description':
            md = conv.convert(str(tab)).strip()
            md = re.sub(r'\n{3,}', '\n\n', md)
            if md:
                parts.append('## Описание\n\n' + md)

        elif data_value == 'properties':
            # Полные характеристики — в dl-элементах
            props_md = _dl_to_md(tab)
            if props_md:
                parts.append('## Характеристики\n\n' + props_md)
            else:
                # Резервный вариант: конвертируем весь блок как есть
                md = conv.convert(str(tab)).strip()
                md = re.sub(r'\n{3,}', '\n\n', md)
                if md:
                    parts.append('## Характеристики\n\n' + md)

        elif data_value == 'certs':
            md = conv.convert(str(tab)).strip()
            md = re.sub(r'\n{3,}', '\n\n', md)
            if md:
                parts.append('## Сертификаты\n\n' + md)

    if len(parts) <= 1:
        # Только заголовок или пусто — конвертируем весь section как запасной вариант
        md = conv.convert(str(sec)).strip()
        md = re.sub(r'\n{3,}', '\n\n', md)
        return md.strip() or None

    markdown = '\n\n'.join(parts)
    markdown = re.sub(r'\n{3,}', '\n\n', markdown)
    return markdown.strip()


# ============================================================================
# СТРАНИЦЫ КОМПАНИИ И ДИСТРИБЬЮТОРОВ
#
# Эти функции вызываются ТОЛЬКО при обработке страниц компании/контактов
# (page_type='company') и дистрибьюторов (page_type='distributor') — см.
# text_extractor.html_to_markdown. На товарных страницах они не запускаются.
# ============================================================================

# Тэги-шум, которые в любом случае не нужны на страницах компании/дистрибьюторов
_SERVICE_TAGS = ('script', 'style', 'noscript', 'svg', 'iframe')

# Слова-признаки «это не название компании, а заголовок-описание/слоган»
_NOT_A_NAME = ('контакт', 'партн', 'производ', 'продаж', 'купить', 'каталог',
               'информац', 'регион', 'дистрибь', 'дилер')


def _company_name(soup: BeautifulSoup) -> str:
    """Пытается определить наименование компании: og:site_name → alt логотипа →
    первый сегмент <title> (если это короткий бренд, а не слоган)."""
    og = soup.find('meta', attrs={'property': 'og:site_name'})
    if og and (og.get('content') or '').strip():
        return og['content'].strip()
    logo = soup.select_one('.header__logo img, .logo img, a[rel="home"] img, header img[alt]')
    if logo and (logo.get('alt') or '').strip():
        return logo['alt'].strip()
    if soup.title and soup.title.string:
        first = re.split(r'\s[—–\-|]\s|,', soup.title.string.strip())[0].strip()
        if first and len(first.split()) <= 3 and not any(k in first.lower() for k in _NOT_A_NAME):
            return first
    return ''


def _harvest_contacts(soup: BeautifulSoup) -> str:
    """Резервный сбор телефонов и e-mail со всей страницы (по ссылкам tel:/mailto:)."""
    phones, emails = [], []
    for a in soup.select('a[href^="tel:"]'):
        t = a.get_text(strip=True) or a.get('href', '')[4:]
        t = t.strip()
        if t and t not in phones:
            phones.append(t)
    for a in soup.select('a[href^="mailto:"]'):
        t = a.get_text(strip=True) or a.get('href', '')[7:]
        t = t.strip()
        if t and t not in emails:
            emails.append(t)
    lines = []
    if phones:
        lines.append('Телефоны: ' + ', '.join(phones))
    if emails:
        lines.append('Эл. почта: ' + ', '.join(emails))
    return '\n'.join(lines)


def extract_company(html: str, base_url: str = '') -> str | None:
    """Страница компании/контактов aerobel.ru → только данные компании
    (наименование, телефоны, e-mail, адрес).

    - Страница «Контакты» (/contacts/): полезное лежит в `.contacts` (офисы:
      адрес, телефон, e-mail, режим работы + менеджеры) — чистим крошки/формы/
      декоративные ссылки и конвертируем.
    - Главная (/) и прочие: берём контактный блок подвала `footer .contact`
      (адрес + e-mail + телефон); если его нет — собираем tel:/mailto: со страницы.
    Возвращает None, если ничего не нашли (тогда отработает общий конвертер).
    """
    soup = BeautifulSoup(html, 'lxml')
    for sel in _SERVICE_TAGS:
        for tag in soup.select(sel):
            tag.decompose()

    conv = MarkdownConverter(heading_style='ATX', bullets='-')
    parts = []

    name = _company_name(soup)
    if name:
        parts.append(f'# {name}')

    # 1) Выделенная страница контактов
    region = soup.select_one('.contacts')
    used_region = False
    if region and region.select_one('a[href^="tel:"], a[href^="mailto:"]'):
        for junk in region.select('.breadcrumbs, [class*="breadcrumb"], nav, form'):
            junk.decompose()
        # Декоративные ссылки-картинки и кнопки «Подробнее» — шум
        for a in region.find_all('a'):
            txt = a.get_text(strip=True)
            if a.find('img') and not txt:
                a.decompose()
            elif txt in ('Подробнее', 'Показать на карте'):
                a.decompose()
        md = conv.convert(str(region)).strip()
        md = re.sub(r'\n{3,}', '\n\n', md)
        if md:
            parts.append(md)
            used_region = True

    # 2) Главная и прочие страницы — контактный блок подвала
    if not used_region:
        collected, seen = [], set()
        for b in soup.select('footer .contact, .footer .contact, .footer__contacts, .footer .contacts'):
            md = conv.convert(str(b)).strip()
            md = re.sub(r'\n{3,}', '\n\n', md).strip()
            if md and md not in seen:
                seen.add(md)
                collected.append(md)
        if collected:
            parts.append('## Контакты\n\n' + '\n\n'.join(collected))
        else:
            # 3) Резерв — собрать телефоны/почту со всей страницы
            harvested = _harvest_contacts(soup)
            if harvested:
                parts.append('## Контакты\n\n' + harvested)

    markdown = re.sub(r'\n{3,}', '\n\n', '\n\n'.join(parts)).strip()
    return markdown or None


def extract_distributor(html: str, base_url: str = '') -> str | None:
    """Страница дистрибьюторов/партнёров aerobel.ru (/cooperation/) → список
    партнёров: наименование, филиалы, адрес, телефон, e-mail, сайт.

    Полезное — в `.where___List` (`.where-groups`). Убираем интерфейсный шум
    («Показать все (N)», одиночные счётчики филиалов). Возвращает None, если
    список партнёров не найден (тогда отработает общий конвертер).
    """
    soup = BeautifulSoup(html, 'lxml')
    for sel in _SERVICE_TAGS:
        for tag in soup.select(sel):
            tag.decompose()

    region = (soup.select_one('.where___List')
              or soup.select_one('.where-groups')
              or soup.select_one('.where-branch__list'))
    if not region:
        return None

    # Удаляем карты и кнопки-переключатели внутри списка
    for junk in region.select('[class*="map"], button'):
        junk.decompose()

    conv = MarkdownConverter(heading_style='ATX', bullets='-')
    md = conv.convert(str(region)).strip()

    # Чистим интерфейсный шум построчно
    md = re.sub(r'(?m)^.*Показать все.*$', '', md)        # «Показать все (N)»
    md = re.sub(r'(?m)^\s*[-*]?\s*\d+\s*$', '', md)        # одиночные счётчики филиалов
    md = re.sub(r'(?m)^.*(Свернуть|Показать на карте).*$', '', md)
    md = re.sub(r'\n{3,}', '\n\n', md).strip()
    if not md:
        return None

    h1 = soup.find('h1')
    title = (h1.get_text(' ', strip=True) if h1 else '') or 'Дистрибьюторы и партнёры'
    return f'# {title}\n\n{md}'
