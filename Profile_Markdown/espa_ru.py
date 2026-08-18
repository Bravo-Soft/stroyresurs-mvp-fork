DOMAIN = 'espa.ru'

import re
from bs4 import BeautifulSoup
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from text_extractor import MarkdownConverter


# Блоки-шум, специфичные для espa.ru.
# Полезное лежит в section.content-main > .card-product:
#   – вкладка «Описание» (.card-product__znch, .card-product__slider-for с фото)
#   – вкладка «Характеристики» (.card-product__tbl — HTML-таблицы со спецификациями)
#   – вкладка «Размеры и вес» (.card-product__rzm — изображение габаритов)
#   – вкладка «Документация» (.card-product__doc — ссылки на документы)
#   – вкладка «FAQ» (.card-product__faq — частые вопросы)
# Всё остальное — шапка, меню, хлебные крошки, карта дилеров, «похожие товары»,
# JS-графики, навигация по вкладкам, попапы, слайдеры — шум.
NOISE_SELECTORS = [
    # Шапка и мобильная шапка
    'header', '.header', '.header-mob',
    # Нижний колонтитул
    'footer', '.footer',
    # Хлебные крошки
    'section.bread', '.bread', '.breadcrumb', '.breadcrumbs',
    # Баннер с заголовком H1 (только название — удаляем после извлечения в extract())
    'section.banner-p', '.banner-p',
    # Навигация по вкладкам (текстовые метки «Описание / Характеристики / …»)
    'ul.tabs', '.tabs',
    # Вкладка «Где купить» — карта дилеров
    '.card-product__map', '.service-map', '.map',
    # «Похожие товары» / «Рекомендуемая автоматика» внутри и вне карточки
    'section.ctlg', '.ctlg',
    # JS-график гидравлических характеристик (Highcharts, данные дублируют таблицы)
    '.card-product__grfc', '.highcharts-figure',
    # Миниатюры-навигация слайдера
    '.card-product__slider-nav',
    # Кнопки прокрутки слайдера
    '.swiper-button-prev', '.swiper-button-next',
    '.swiper-button-pagination', '.swiper-pagination',
    # Гарантийный бейдж (SVG-иконка «5 лет») — декоративный элемент
    '.card-product__label',
    # Всплывающее окно выбора модели
    '.dialogs',
    # Служебные теги
    'script', 'style', 'noscript', 'iframe', 'svg', 'form',
]


# Название товара, извлечённое из section.banner-p до её удаления в clean().
# Используется в extract(), когда banner-p уже удалён из soup.
_saved_title = ''


def _fix_table_attrs(soup: BeautifulSoup):
    """Исправляет некорректные атрибуты colspan/rowspan в таблицах.

    В HTML espa.ru встречается ``colspan='9"'`` — значение обрезано HTML-парсером
    из-за незакрытой кавычки. MarkdownConverter падает с ValueError при попытке
    привести такое значение к int(). Функция удаляет нечисловые суффиксы и
    гарантирует, что colspan/rowspan — целые числа ≥ 1.
    """
    for cell in soup.find_all(['td', 'th']):
        for attr in ('colspan', 'rowspan'):
            val = cell.get(attr)
            if val is None:
                continue
            # Оставляем только ведущие цифры, остальное отбрасываем
            digits = re.match(r'^\d+', str(val))
            if digits:
                cell[attr] = digits.group(0)
            else:
                del cell[attr]


def _pseudo_table_to_md(block) -> str:
    """div.card-product__tbl БЕЗ <table> — псевдотаблица на div-ах
    («Комплектация», «Опции и аксессуары»). Общий конвертер сплющивал её в
    отдельные абзацы («Комплектация\\n\\n1\\n\\nКабельные вводы\\n\\n1 шт.»),
    из-за чего в поле complectation попадал мусор с переводами строк.

    Структура одной позиции:
      .card-product__tbl-item
        .card-product__tbl-item-numb  — порядковый номер (нумерация с пропусками, отбрасываем)
        .card-product__tbl-item-in
          .card-product__tbl-item-tx  — наименование
          .card-product__tbl-item-tx  — количество (может отсутствовать)

    Конвертируем в аккуратные строки «<наименование> — <количество>» под заголовком
    (если у позиции только один tx без количества — просто наименование).
    Возвращает '' если позиций нет."""
    items = block.select('.card-product__tbl-item')
    if not items:
        return ''
    title_el = block.select_one('.card-product__tbl-title')
    title = title_el.get_text(strip=True) if title_el else ''

    lines = []
    for it in items:
        txs = it.select('.card-product__tbl-item-tx')
        # Внутри tx бывают литеральные переводы строк (напр. «"американка":\\nKIT 06 - 1"\\n
        # KIT 08 - 1"1/4») — схлопываем любой пробельный шум в один пробел, чтобы не плодить
        # абзацы в markdown.
        vals = [re.sub(r'\s+', ' ', x.get_text(' ', strip=True)).strip() for x in txs]
        vals = [v for v in vals if v]
        if not vals:
            continue
        if len(vals) >= 2:
            # Первый tx — наименование, остальные (обычно один) — количество.
            # Формат позиции (требование заказчика): «- Название N шт.;» — короткое тире
            # в начале, количество через пробел, точка с запятой в конце строки.
            name = vals[0]
            qty = ' '.join(vals[1:])
            lines.append(f"- {name} {qty};")
        else:
            lines.append(f"- {vals[0]};")

    if not lines:
        return ''
    parts = []
    if title:
        parts.append(f"### {title}")
    parts.append("\n".join(lines))
    return "\n\n".join(parts)


def clean(soup: BeautifulSoup):
    """Удаляет специфичные для espa.ru блоки: шапку, меню, хлебные крошки,
    карту дилеров, «похожие товары», JS-графики, попапы, слайдерные кнопки.
    Также исправляет некорректные colspan/rowspan в таблицах.
    Сохраняет название товара из section.banner-p в _saved_title до её удаления."""
    global _saved_title
    # Сохраняем H1 из баннера ДО его удаления (вызывается из html_to_markdown)
    bp = soup.select_one('section.banner-p')
    if bp:
        h1_bp = bp.find('h1')
        _saved_title = h1_bp.get_text(strip=True) if h1_bp else ''
    else:
        _saved_title = ''
    for sel in NOISE_SELECTORS:
        for tag in soup.select(sel):
            tag.decompose()
    _fix_table_attrs(soup)


def extract(html: str, base_url: str = '') -> str | None:
    """Карточка товара espa.ru: заголовок + описание + таблицы характеристик
    + изображение габаритов + документация + FAQ.

    Структура страницы:
      section.banner-p   — содержит H1 с названием товара (удаляется clean)
      section.content-main > .card-product > .tabs_container > .tab_content (5 вкладок)
        – вкладка 0 (Описание):       .card-product__slider (фото) + .card-product__znch (описание)
        – вкладка 1 (Характеристики):  .card-product__tbl — HTML-таблицы спецификаций
        – вкладка 2 (Размеры и вес):   .card-product__rzm + таблица размеров
        – вкладка 3 (Документация):    .card-product__doc + .card-product__faq
        – вкладка 4 (Где купить):      удалена через NOISE_SELECTORS

    H1 извлекается из section.banner-p ДО вызова clean(), чтобы не потерять название товара.
    Возвращает None, если основной контейнер не найден (тогда отработает общий конвертер).
    """
    soup = BeautifulSoup(html, 'lxml')

    # ---- Заголовок товара ----
    # Когда extract() вызывается из html_to_markdown, clean() уже выполнялась ранее
    # и сохранила H1 из banner-p в _saved_title. Если вызов прямой (тесты),
    # clean() вызывается ниже и тоже заполняет _saved_title.
    # Сначала ищем H1 напрямую (прямой вызов), потом берём _saved_title (через pipeline).
    bp = soup.select_one('section.banner-p')
    if bp:
        h1_in_bp = bp.find('h1')
        title_text = h1_in_bp.get_text(strip=True) if h1_in_bp else ''
    else:
        # banner-p уже удалён — берём сохранённое значение
        title_text = _saved_title

    if not title_text:
        h1 = soup.find('h1')
        if h1:
            title_text = h1.get_text(strip=True)
    if not title_text and soup.title and soup.title.string:
        title_text = soup.title.string.strip()

    clean(soup)

    conv = MarkdownConverter(heading_style='ATX', bullets='-')

    parts = []
    if title_text:
        parts.append(f'# {title_text}')

    # ---- Основной контейнер карточки ----
    card = soup.select_one('.card-product')
    if not card:
        # Запасной вариант — весь основной раздел
        fallback = soup.select_one('section.content-main') or soup.select_one('.content-main')
        if not fallback:
            return None
        md = conv.convert(str(fallback)).strip()
        md = re.sub(r'\n{3,}', '\n\n', md)
        if parts:
            parts.append(md)
            return re.sub(r'\n{3,}', '\n\n', '\n\n'.join(parts)).strip() or None
        return md.strip() or None

    # ---- Фотографии товара ----
    slider = card.select_one('.card-product__slider-for')
    if slider:
        imgs = slider.find_all('img')
        img_lines = []
        for img in imgs:
            src = img.get('src', '').strip()
            alt = img.get('alt', '').strip()
            if src and not src.endswith('.svg'):
                # Строим абсолютный URL, если src относительный
                if src.startswith('/') and base_url:
                    src = base_url.rstrip('/') + src
                label = alt if alt else title_text
                img_lines.append(f'![{label}]({src})')
        if img_lines:
            parts.extend(img_lines)

    # ---- Вкладки с контентом ----
    tab_contents = card.select('.tab_content')
    for tab in tab_contents:
        # Вкладка «Где купить» (.card-product__map) уже удалена; пропускаем пустые
        tab_text = tab.get_text(strip=True)
        if not tab_text:
            continue

        # Фото в слайдере уже обработаны выше — удаляем дубль слайдера из вкладки
        for sl in tab.select('.card-product__slider-for'):
            sl.decompose()

        # div-псевдотаблицы «Комплектация» / «Опции и аксессуары» свёрстаны div-ами,
        # а не <table>: общий конвертер сплющивал их в отдельные абзацы. Извлекаем их в
        # аккуратные строки и УБИРАЕМ из вкладки, чтобы конвертер не трогал их разметку.
        # Настоящие таблицы характеристик (внутри есть <table>) и блок изображения габаритов
        # (без .card-product__tbl-item) не трогаем — их обрабатывает конвертер ниже.
        pseudo_md = []
        for block in tab.select('div.card-product__tbl'):
            if not block.select('.card-product__tbl-item'):
                continue
            md_pt = _pseudo_table_to_md(block)
            if md_pt:
                pseudo_md.append(md_pt)
            block.decompose()

        md = conv.convert(str(tab)).strip()
        if md:
            parts.append(md)
        # Псевдотаблицы выводим после сконвертированного содержимого вкладки —
        # у каждой свой заголовок, поэтому порядок внутри вкладки не критичен.
        parts.extend(pseudo_md)

    markdown = '\n\n'.join(parts)
    markdown = re.sub(r'\n{3,}', '\n\n', markdown)
    return markdown.strip() or None
