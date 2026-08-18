DOMAIN = 'kolpa-san.ru'

import re
import json
from bs4 import BeautifulSoup
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from text_extractor import MarkdownConverter

# Адаптеру нужен СЫРОЙ HTML (со <script>): данные карточки товара (характеристики
# и варианты Размер/Цвет/Угол/Тип стекла) лежат в JS-объекте `var product`, который
# общая очистка вырезает вместе со скриптами. С этим флагом раздатчик отдаёт сырой HTML.
NEEDS_RAW_HTML = True


# Блоки-шум, специфичные для kolpa-san.ru (сайт на Tilda Publishing).
# Полезное содержимое лежит в блоке data-record-type="744" (карточка товара:
# изображения-слайдер, заголовок h1, цена, описание) и data-record-type="585"
# (аккордеон «Характеристики и схемы»). Всё остальное — шум.
NOISE_SELECTORS = [
    # Шапка и подвал Tilda
    'header', 'footer',
    # Глобальные меню/навигация (Tilda-компоненты)
    '[data-record-type="2084"]',   # верхняя строка с телефоном и логотипом
    '[data-record-type="966"]',    # горизонтальное меню категорий (Ванны, Шторки…)
    '[data-record-type="1272"]',   # мобильное / бургер-меню
    '[data-record-type="1036"]',   # технические/вспомогательные записи Tilda
    '[data-record-type="131"]',    # вспомогательные записи Tilda
    '[data-record-type="212"]',    # вспомогательные записи Tilda
    '[data-record-type="396"]',    # вспомогательные записи Tilda
    '[data-record-type="985"]',    # вспомогательные записи Tilda
    # Хлебные крошки
    '[data-record-type="758"]',
    # Форма оформления заказа
    '[data-record-type="706"]',
    # Всплывающее уведомление о cookie
    '[data-record-type="657"]',    # t657 — cookie-баннер
    # Блоки апселла и «похожие товары» (все Tilda t795-блоки)
    '.t795',
    # «Вам может понравиться», «Добавить в комплект», «Добавить гидромассаж» и т.п.
    '[data-record-type="776"]',    # пустые разделители между апселл-блоками
    # Блок «О фабрике» (текстовая статья с историей компании)
    '[data-record-type="30"]',
    # Блок «Акриловые ванны Kolpa-San» (маркетинговая вставка)
    '[data-record-type="347"]',
    # Связанные/рекомендуемые товары (Tilda t756 — карусели других продуктов)
    '[data-record-type="756"]',
    # Служебные теги
    'script', 'style', 'noscript', 'iframe', 'svg', 'form',
]


def clean(soup: BeautifulSoup):
    """Удаляет специфичные для kolpa-san.ru блоки (шапка, подвал, меню,
    хлебные крошки, апселл, «похожие», видео, «О фабрике», cookie, формы)."""
    for sel in NOISE_SELECTORS:
        for tag in soup.select(sel):
            tag.decompose()


def _extract_bg_images(container) -> list:
    """Извлекает URL картинок из CSS background-image в слайдере Tilda.

    Tilda хранит изображения товара не в <img>, а в div-элементах со стилем
    ``background-image: url('...')``. Функция возвращает список уникальных URL
    в исходном (не-thumbnail) разрешении.
    """
    urls = []
    seen = set()
    for div in container.select('[style]'):
        style = div.get('style', '')
        if 'background-image' not in style:
            continue
        found = re.findall(r"url\(['\"]?([^'\")\s]+)", style)
        for raw_url in found:
            # Убираем суффикс ресайза /-/resizeb/Nx/ → берём оригинал
            url = re.sub(r'/-/resizeb/\d+x?/', '/', raw_url)
            # Пропускаем заглушки размером 20x (превью-placeholder)
            if '/-/resizeb/20x/' in raw_url:
                # Берём оригинал без суффикса resizeb
                url = re.sub(r'/-/resizeb/20x/', '/', raw_url)
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def _specs_to_markdown(t585_block) -> str:
    """Преобразует блок характеристик t585 (аккордеон) в Markdown-таблицу.

    Tilda хранит характеристики в трёх форматах (в зависимости от товара):

    Формат A — ключ в <strong>, значение в тексте рядом:
        <strong>Тип:</strong> Прямоугольная ванна<br/>

    Формат B — целая строка «ключ: значение» внутри одного <strong>:
        <strong>Размер (см): 180x160</strong><br/>

    Формат C — без <strong>, просто текстовые строки, разделённые <br/>:
        Тип: Шторка на ванну<br/>Высота (см): 140<br/>

    Ссылки (PDF-схемы) сохраняются отдельной строкой после таблицы.
    """
    text_div = t585_block.select_one('.t585__text')
    if not text_div:
        text_div = t585_block.select_one('.t585__content')
    if not text_div:
        return ''

    rows = []
    links = []

    # Обходим дерево поузлово
    current_key = ''
    current_val_parts = []

    def flush_row():
        nonlocal current_key, current_val_parts
        if current_key:
            val = ' '.join(current_val_parts).strip().strip(':').strip()
            rows.append((current_key.strip().rstrip(':').strip(), val))
        current_key = ''
        current_val_parts = []

    def handle_text_chunk(raw: str):
        """Обрабатывает текстовый фрагмент «ключ: значение» или просто значение."""
        nonlocal current_key, current_val_parts
        txt = raw.strip()
        if not txt:
            return
        if current_key:
            # Уже есть ключ — это значение
            current_val_parts.append(txt)
        else:
            # Ключа нет — пробуем разобрать «ключ: значение» из самого текста
            if ':' in txt:
                sep = txt.index(':')
                key = txt[:sep].strip()
                val = txt[sep + 1:].strip()
                if key:
                    rows.append((key, val))
                    return
            # Нет разделителя — просто текст, пропускаем
            pass

    for node in text_div.children:
        # NavigableString.name == None; Tag.name == 'strong'/'br'/etc.
        if not getattr(node, 'name', None):
            # Текстовый узел (NavigableString)
            handle_text_chunk(str(node))
        elif node.name == 'strong':
            strong_txt = node.get_text(strip=True)
            # Внутри <strong> может быть ссылка (<a>)
            inner_a = node.find('a')
            if inner_a:
                href = inner_a.get('href', '')
                link_txt = inner_a.get_text(strip=True)
                if href:
                    links.append(f'[{link_txt}]({href})')
                continue
            # Формат B: вся строка «Параметр: значение» в одном <strong>
            if ':' in strong_txt and not strong_txt.endswith(':'):
                sep = strong_txt.index(':')
                key = strong_txt[:sep].strip()
                val = strong_txt[sep + 1:].strip()
                if key and val:
                    # Полная пара — сбрасываем предыдущий ключ и добавляем строку
                    flush_row()
                    rows.append((key, val))
                    continue
            # Формат A: <strong> содержит только ключ (с двоеточием или без)
            flush_row()
            current_key = strong_txt
        elif node.name == 'br':
            flush_row()
        elif node.name == 'a':
            href = node.get('href', '')
            link_txt = node.get_text(strip=True)
            if href:
                links.append(f'[{link_txt}]({href})')
            else:
                handle_text_chunk(link_txt)
        else:
            # Прочие теги — берём текст (только если это Tag, а не NavigableString)
            if hasattr(node, 'select'):
                inner_links = node.select('a')
                for a in inner_links:
                    href = a.get('href', '')
                    link_txt = a.get_text(strip=True)
                    if href:
                        links.append(f'[{link_txt}]({href})')
                node_txt = node.get_text(strip=True)
                if node_txt and not inner_links:
                    handle_text_chunk(node_txt)
    flush_row()

    if not rows and not links:
        # Вообще ничего не распозналось — отдаём сырой текст
        return text_div.get_text(separator='\n', strip=True)

    parts = ['## Характеристики\n', '| Параметр | Значение |', '| --- | --- |']
    for k, v in rows:
        if k:
            parts.append(f'| {k} | {v} |')

    if links:
        parts.append('')
        parts.extend(links)

    return '\n'.join(parts)


def _extract_var_product(html: str) -> dict | None:
    """Извлекает объект Tilda Store ``var product = {...};`` из сырого HTML.

    Это авторитетные данные СО СТРАНИЦЫ ТОВАРА: характеристики
    (json_chars/characteristics) и — главное — ВАРИАНТЫ выбора
    (json_options: Размер, Цвет, Угол, Тип стекла). Значения вариантов
    отрисовываются кнопками из JS и в статический DOM не попадают, поэтому без
    разбора этого скрипта цвета и типоразмеры теряются (colors/variants пусты).
    """
    m = re.search(r'var\s+product\s*=\s*(\{.*?\})\s*;', html, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _var_product_specs_markdown(vp: dict) -> str:
    """Строит таблицу «## Характеристики» из ``var product``.

    Объединяет характеристики (json_chars) и варианты (json_options). Варианты
    выводятся СТРОКАМИ — перечень значений через «; » (не массивом объектов:
    массив объектов без id граф отбрасывает). Значения-ссылки (PDF-схемы)
    выносятся отдельной строкой после таблицы.
    """
    def _as_list(value):
        # Всегда возвращает список: строка-JSON «null»/объект/None → [].
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return []
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return []
        return value if isinstance(value, list) else []

    chars = vp.get('characteristics')
    if not isinstance(chars, list) or not chars:
        chars = _as_list(vp.get('json_chars'))
    options = _as_list(vp.get('json_options'))

    rows = []            # (ключ, значение)
    links = []
    seen = set()

    for ch in chars:
        if not isinstance(ch, dict):
            continue
        key = str(ch.get('title', '')).strip().rstrip(':').strip()
        val = str(ch.get('value', '')).strip()
        if not key or not val:
            continue
        if val.startswith('http://') or val.startswith('https://'):
            links.append(f'[{key}]({val})')
        else:
            rows.append((key, val))
            seen.add(key.lower())

    # Варианты: перечень значений строкой. Пропускаем те, что уже присутствуют
    # среди характеристик единственным совпадающим значением (напр. «Тип стекла»).
    for opt in options:
        if not isinstance(opt, dict):
            continue
        key = str(opt.get('title', '')).strip().rstrip(':').strip()
        values = [str(v).strip() for v in (opt.get('values') or []) if str(v).strip()]
        if not key or not values:
            continue
        if key.lower() in seen and len(values) == 1:
            continue
        rows.append((key, '; '.join(values)))

    if not rows and not links:
        return ''

    parts = ['## Характеристики\n', '| Параметр | Значение |', '| --- | --- |']
    parts.extend(f'| {k} | {v} |' for k, v in rows)
    if links:
        parts.append('')
        parts.extend(links)
    return '\n'.join(parts)


def _build_card_from_var_product(vp: dict) -> str:
    """Собирает markdown-карточку из Tilda `var product` (данные со страницы товара):
    заголовок + цена + описание + изображения + характеристики с вариантами.

    Возвращает '' если данных недостаточно (тогда сработает фолбэк по DOM/общий метод).
    """
    parts = []

    title = str(vp.get('title', '')).strip()
    if title:
        parts.append(f'# {title}')

    # Цена: '154000.0000' → '154000'
    price_raw = str(vp.get('price', '')).strip()
    if price_raw:
        try:
            price_num = int(round(float(price_raw)))
        except (ValueError, TypeError):
            price_num = 0
        if price_num > 0:
            parts.append(f'**Цена:** {price_num} р.')

    # Описание: поле text содержит HTML (<br/>), приводим к тексту
    text = str(vp.get('text', '')).strip()
    if text:
        text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)
        text = BeautifulSoup(text, 'lxml').get_text('\n')
        text = re.sub(r'\n{2,}', '\n', text).strip()
        if text:
            parts.append(text)

    # Изображения из галереи
    gallery = vp.get('gallery')
    if not isinstance(gallery, list):
        gallery = []
    seen = set()
    img_urls = []
    for item in gallery:
        url = item.get('img') if isinstance(item, dict) else None
        if url and url not in seen:
            seen.add(url)
            img_urls.append(url)
    if img_urls:
        parts.append('\n'.join(f'![]({u})' for u in img_urls[:10]))

    # Характеристики + варианты
    specs_md = _var_product_specs_markdown(vp)
    if specs_md:
        parts.append(specs_md)

    if len(parts) <= 1:
        return ''

    markdown = '\n\n'.join(parts)
    markdown = re.sub(r'\n{3,}', '\n\n', markdown)
    return markdown.strip()


def extract(html: str, base_url: str = '') -> str | None:
    """Карточка товара kolpa-san.ru: H1 + описание + галерея + характеристики.

    Сайт построен на Tilda Publishing. Полезный контент сосредоточен в двух
    блоках фиксированных типов:
      • data-record-type="744" (t744) — слайдер изображений + текстовая
        панель с h1, ценой и описанием товара.
      • data-record-type="585" (t585) — аккордеон «Характеристики и схемы»
        со списком параметров (материал, размеры, цвет…) и ссылками на PDF.

    Изображения хранятся как CSS background-image (не <img>), поэтому
    функция явно их извлекает и вставляет как Markdown-ссылки на изображения.

    ПРИОРИТЕТНЫЙ ИСТОЧНИК — объект Tilda `var product` (данные со страницы товара):
    он содержит характеристики И варианты (Размер/Цвет/Угол/Тип стекла), которых нет
    в статическом DOM (рендерятся кнопками из JS). Разбор t744/t585 остаётся фолбэком
    для страниц без `var product` (минибассейны, душевые панели).

    Возвращает None, если ни один из ключевых блоков не найден
    (тогда сработает общий конвертер).
    """
    # Приоритет — данные СО СТРАНИЦЫ ТОВАРА (Tilda `var product`). Требует сырого
    # HTML (адаптер объявляет NEEDS_RAW_HTML=True).
    var_product = _extract_var_product(html)
    if var_product:
        card = _build_card_from_var_product(var_product)
        if card:
            return card

    soup = BeautifulSoup(html, 'lxml')
    clean(soup)

    # --- Карточка товара (type 744) ---
    product_block = soup.select_one('[data-record-type="744"]')

    # --- Блок характеристик (type 585) ---
    specs_block = soup.select_one('[data-record-type="585"]')

    if not product_block and not specs_block:
        return None

    parts = []

    # Заголовок товара
    if product_block:
        h1 = product_block.select_one('h1') or soup.find('h1')
    else:
        h1 = soup.find('h1')
    if h1:
        parts.append(f'# {h1.get_text(strip=True)}')

    # Цена — берём только числовое значение из .t744__price-value, чтобы избежать
    # дублирования знака валюты (Tilda хранит «36300р.» и «р.» в разных элементах)
    if product_block:
        price_val = product_block.select_one('.t744__price-value, .js-store-prod-price-val')
        if price_val:
            price_txt = price_val.get_text(strip=True)
            if price_txt and price_txt not in ('', '0'):
                parts.append(f'**Цена:** {price_txt} р.')
        else:
            # Запасной вариант: основной ценовой блок
            price_el = product_block.select_one('.t744__price.t744__price-item')
            if price_el:
                price_txt = price_el.get_text(strip=True).rstrip('р.').strip()
                if price_txt:
                    parts.append(f'**Цена:** {price_txt} р.')

    # Описание товара (короткое, под заголовком)
    if product_block:
        descr_els = product_block.select('.t744__descr')
        for d in descr_els:
            txt = d.get_text(strip=True)
            if txt:
                parts.append(txt)

    # Изображения (из CSS background-image в слайдере)
    if product_block:
        img_urls = _extract_bg_images(product_block)
        if img_urls:
            img_lines = [f'![]({u})' for u in img_urls[:10]]  # не более 10 изображений
            parts.append('\n'.join(img_lines))

    # Характеристики (фолбэк-путь без var product): видимый аккордеон t585.
    if specs_block:
        specs_md = _specs_to_markdown(specs_block)
        if specs_md:
            parts.append(specs_md)

    # Если кроме заголовка ничего не нашли — отдаём управление общему методу
    if len(parts) <= 1:
        return None

    markdown = '\n\n'.join(parts)
    markdown = re.sub(r'\n{3,}', '\n\n', markdown)
    return markdown.strip() or None
