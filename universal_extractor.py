# -*- coding: utf-8 -*-
"""
Универсальный markdown-экстрактор товарных страниц (Этап «универсал»).

Назначение: для page_type='' (товарная страница) заменить наивный выбор
контейнера article→main→.content в text_extractor на доменно-независимый
конвейер, устойчивый к разным CMS (Bitrix, WooCommerce/WP, OpenCart, Tilda,
Drupal, Joomla, generic) и к структурированной разметке (JSON-LD, Microdata,
OpenGraph).

Зависимости: только bs4/lxml/re/json — без новых пакетов. Конвертер markdown,
канон степеней, чистка шума и постпроцессинг — переиспользуются из
text_extractor (НЕ дублируются), чтобы поведение степеней/таблиц совпадало.

Контракт функций (имена фиксированы заданием):
  - extract_structured_data(soup, base_url='') -> dict
  - select_content_container(soup) -> (tag, способ)
  - extract_product_markdown(html, base_url='') -> Optional[str]
"""
import re
import json
import logging
from typing import Optional, Tuple

from bs4 import BeautifulSoup

# Переиспользуем конвертер и чистку из основного модуля — без дублирования логики
# степеней/таблиц/постпроцессинга. Импорт защищённый: если text_extractor по
# какой-то причине не импортируется, модуль не должен ронять основной поток.
import text_extractor as _T


# ==================== СТРУКТУРИРОВАННЫЕ ДАННЫЕ ТОВАРА ====================

def _normalize_ws(s) -> str:
    """Схлопывает пробелы и обрезает края; None/нестроки → ''."""
    if s is None:
        return ''
    return re.sub(r'\s+', ' ', str(s)).strip()


def _unescape_amp(s: str) -> str:
    """Минимальное снятие HTML-сущностей, встречающихся в значениях разметки
    (&gt; в category и т.п.). bs4 уже снимает большинство, но JSON-LD строки
    приходят сырыми."""
    if not s:
        return s
    return (s.replace('&gt;', '>').replace('&lt;', '<')
             .replace('&amp;', '&').replace('&quot;', '"').replace('&#039;', "'"))


def _coerce_price(val) -> str:
    """Приводит цену к человекочитаемой строке. offers.price может быть числом,
    строкой, либо вложенным dict (AggregateOffer)."""
    if val is None:
        return ''
    if isinstance(val, (int, float)):
        # Не печатаем .0 у целых
        return str(int(val)) if float(val).is_integer() else str(val)
    if isinstance(val, dict):
        for k in ('price', 'lowPrice', 'highPrice'):
            if val.get(k) not in (None, ''):
                return _coerce_price(val.get(k))
        return ''
    return _normalize_ws(val)


def _brand_to_str(val) -> str:
    """brand может быть строкой или объектом {@type:Brand, name:…}."""
    if isinstance(val, dict):
        return _normalize_ws(val.get('name') or '')
    if isinstance(val, list):
        for it in val:
            r = _brand_to_str(it)
            if r:
                return r
        return ''
    return _normalize_ws(val)


def _iter_jsonld_objects(data):
    """Разворачивает разобранный JSON-LD в плоский список dict-объектов:
    учитывает @graph, массивы верхнего уровня и вложенные массивы графа."""
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, dict):
            yield cur
            graph = cur.get('@graph')
            if isinstance(graph, list):
                stack.extend(graph)


def _type_matches(node, wanted: str) -> bool:
    """@type бывает строкой или списком; ищем подстроку без регистра."""
    t = node.get('@type')
    if isinstance(t, list):
        return any(wanted.lower() in str(x).lower() for x in t)
    return wanted.lower() in str(t or '').lower()


def _extract_from_jsonld(soup) -> dict:
    """Извлекает товар из всех script[type=application/ld+json]. Битый JSON
    (висячие запятые, одинарные кавычки) не должен ронять разбор — пытаемся
    распарсить, при неудаче чиним мягко, иначе пропускаем блок."""
    for tag in soup.find_all('script', attrs={'type': re.compile(r'ld\+json', re.I)}):
        raw = tag.string or tag.get_text() or ''
        if not raw.strip():
            continue
        data = None
        try:
            data = json.loads(raw)
        except Exception:
            # Мягкая починка: убрать висячие запятые перед }/], заменить
            # одинарные кавычки ключей/строк на двойные. Если и это не вышло —
            # пропускаем блок, не роняя страницу.
            try:
                fixed = re.sub(r',\s*([}\]])', r'\1', raw)
                fixed = re.sub(r"'", '"', fixed)
                data = json.loads(fixed)
            except Exception:
                continue
        try:
            product = None
            for node in _iter_jsonld_objects(data):
                if _type_matches(node, 'Product'):
                    product = node
                    break
            if product is None:
                continue

            offers = product.get('offers')
            offer = None
            if isinstance(offers, dict):
                offer = offers
            elif isinstance(offers, list) and offers:
                offer = offers[0] if isinstance(offers[0], dict) else None

            price = ''
            currency = ''
            if offer:
                price = _coerce_price(offer.get('price') if offer.get('price') is not None else offer)
                currency = _normalize_ws(offer.get('priceCurrency'))

            props = []
            ap = product.get('additionalProperty')
            ap_list = ap if isinstance(ap, list) else ([ap] if isinstance(ap, dict) else [])
            for p in ap_list:
                if not isinstance(p, dict):
                    continue
                pname = _normalize_ws(p.get('name'))
                pval = p.get('value')
                if isinstance(pval, (dict, list)):
                    pval = ''
                pval = _normalize_ws(pval)
                unit = _normalize_ws(p.get('unitText'))
                if unit and pval and unit not in pval:
                    pval = f"{pval} {unit}"
                if pname and pval:
                    props.append((pname, pval))

            result = {
                'name': _unescape_amp(_normalize_ws(product.get('name'))),
                'price': (f"{price} {currency}".strip() if price else ''),
                'sku': _normalize_ws(product.get('sku') or product.get('mpn')),
                'brand': _brand_to_str(product.get('brand')),
                'description': _unescape_amp(_normalize_ws(product.get('description'))),
                'properties': props,
                'source': 'json-ld',
            }
            # Считаем результат пригодным, если есть имя ИЛИ хотя бы свойства/цена.
            if result['name'] or result['properties'] or result['price']:
                return result
        except Exception as e:
            logging.debug(f"universal_extractor: ошибка разбора JSON-LD: {e}")
            continue
    return {}


def _microdata_value(el) -> str:
    """Значение itemprop: приоритет у content/атрибутов (meta), иначе видимый текст."""
    if el is None:
        return ''
    if el.get('content'):
        return _normalize_ws(el.get('content'))
    name = (el.name or '').lower()
    if name == 'meta':
        return _normalize_ws(el.get('content'))
    if name in ('a', 'link') and el.get('href') and not el.get_text(strip=True):
        return _normalize_ws(el.get('href'))
    if name in ('img',) and el.get('alt'):
        return _normalize_ws(el.get('alt'))
    return _normalize_ws(el.get_text(' ', strip=True))


def _scoped_itemprop(container, prop):
    """Находит itemprop ВНУТРИ container, но не во вложенных itemscope-объектах
    (чтобы name товара не перепутать с name из breadcrumb/offer). Упрощённо:
    берём первый itemprop с нужным именем, чей ближайший родитель-itemscope —
    это сам container."""
    for el in container.select(f'[itemprop~="{prop}"]'):
        parent_scope = el.find_parent(attrs={'itemscope': True})
        if parent_scope is None or parent_scope is container:
            return el
    return None


def _extract_from_microdata(soup) -> dict:
    """Извлекает товар из microdata-контейнера [itemtype*=Product]."""
    container = soup.select_one('[itemtype*="Product"]')
    if container is None:
        return {}
    try:
        name = _microdata_value(_scoped_itemprop(container, 'name'))
        description = _microdata_value(_scoped_itemprop(container, 'description'))
        sku = _microdata_value(_scoped_itemprop(container, 'sku') or _scoped_itemprop(container, 'mpn'))
        brand_el = _scoped_itemprop(container, 'brand')
        brand = _microdata_value(brand_el)

        price_el = container.select_one('[itemprop~="price"]')
        currency_el = container.select_one('[itemprop~="priceCurrency"]')
        price = _coerce_price(_microdata_value(price_el)) if price_el else ''
        currency = _microdata_value(currency_el) if currency_el else ''

        props = []
        for ap in container.select('[itemprop~="additionalProperty"]'):
            pn = ap.select_one('[itemprop~="name"]')
            pv = ap.select_one('[itemprop~="value"]')
            pu = ap.select_one('[itemprop~="unitText"]')
            pname = _microdata_value(pn)
            pval = _microdata_value(pv)
            unit = _microdata_value(pu)
            if unit and pval and unit not in pval:
                pval = f"{pval} {unit}"
            if pname and pval:
                props.append((pname, pval))

        result = {
            'name': _unescape_amp(name),
            'price': (f"{price} {currency}".strip() if price else ''),
            'sku': sku,
            'brand': brand,
            'description': _unescape_amp(description),
            'properties': props,
            'source': 'microdata',
        }
        if result['name'] or result['properties'] or result['price']:
            return result
    except Exception as e:
        logging.debug(f"universal_extractor: ошибка разбора microdata: {e}")
    return {}


def _extract_from_og(soup) -> dict:
    """OpenGraph как фолбэк: og:title и product:price:amount."""
    def meta(prop):
        el = (soup.find('meta', attrs={'property': prop})
              or soup.find('meta', attrs={'name': prop}))
        return _normalize_ws(el.get('content')) if el and el.get('content') else ''

    name = meta('og:title')
    price = meta('product:price:amount') or meta('og:price:amount')
    currency = meta('product:price:currency') or meta('og:price:currency')
    description = meta('og:description')
    if not (name or price):
        return {}
    return {
        'name': name,
        'price': (f"{price} {currency}".strip() if price else ''),
        'sku': '',
        'brand': '',
        'description': description,
        'properties': [],
        'source': 'og',
    }


def extract_structured_data(soup, base_url: str = '') -> dict:
    """Собирает структурированные данные товара из разметки страницы.

    Приоритет источников: JSON-LD → Microdata → OpenGraph. Каждый блок в своём
    try/except (битый JSON/разметка не роняют). Возврат:
      {'name','price','sku','brand','description','properties':[(имя,значение)…],
       'source':'json-ld'|'microdata'|'og'}  либо  {} если ничего не найдено.
    """
    for fn in (_extract_from_jsonld, _extract_from_microdata, _extract_from_og):
        try:
            data = fn(soup)
        except Exception as e:
            logging.debug(f"universal_extractor.extract_structured_data: {fn.__name__}: {e}")
            data = {}
        if data:
            return data
    return {}


# ==================== ВЫБОР КОНТЕЙНЕРА КОНТЕНТА ====================

# CMS-селекторы, подтверждённые на локальном корпусе (см. отчёт 10_universal_extractor.md).
# Порядок внутри списка — приоритет. Каждый кортеж (селектор, метка-CMS).
_CMS_SELECTORS = [
    # Bitrix: подтверждено .bx_item_detail (Полигон), [class*=catalog-element] (ГМС).
    ('.bx_item_detail', 'bitrix'),
    ('.bx-catalog-element', 'bitrix'),
    ('[class*="catalog-element"]', 'bitrix'),
    ('[id^="bx_"] .catalog-detail', 'bitrix'),
    ('.catalog-detail', 'bitrix'),
    # WooCommerce / WordPress: .single-product .summary, div.product, .entry-content.
    ('.single-product .summary', 'woocommerce'),
    ('div.product.type-product', 'woocommerce'),
    ('.woocommerce div.product', 'woocommerce'),
    ('.entry-content', 'wordpress'),
    # Joomla / JoomShopping.
    ('.productfull', 'joomla'),
    ('.jshop_prod_description', 'joomla'),
    ('.jshop', 'joomla'),
    # OpenCart: подтверждено #product / .product-information (ТСС, Воскресенский).
    ('#product', 'opencart'),
    ('.product-information', 'opencart'),
    ('#content .product-info', 'opencart'),
    # Drupal: подтверждено #main-content / .field--name-body (Р-ВЕНТ).
    ('.node--type-product', 'drupal'),
    ('article.node--type-produkcia', 'drupal'),
    ('#main-content', 'drupal'),
    # Tilda: подтверждено #allrecords (Kolpa, Калита).
    ('#allrecords', 'tilda'),
    # InSales.
    ('.product-page', 'insales'),
]

# Generic-селекторы (последний неоценочный уровень перед скорингом).
_GENERIC_SELECTORS = [
    ('article', 'generic-article'),
    ('main', 'generic-main'),
    ('[role="main"]', 'generic-role-main'),
    ('#content', 'generic-id-content'),
    ('.content', 'generic-content'),
    ('.product-detail', 'generic-product-detail'),
    ('.product-card', 'generic-product-card'),
]

# Минимальная адекватная длина текста контейнера, чтобы считать его пригодным.
_MIN_TEXT = 120

# Шумовые виджеты конкретных CMS без «говорящих» классов (дополняется по корпусу):
# t657 — типовой блок Tilda «Политика Cookie».
_EXTRA_NOISE_SELECTORS = ['[class~="t657"]']

_DISPLAY_NONE_RE = re.compile(r'display\s*:\s*none', re.I)
# Классы, у которых display:none — это действительно скрытый шум, а не вкладка.
_HIDDEN_NOISE_CLASS_RE = re.compile(
    r'^(modal|popup|overlay|cart|basket|menu|nav|search|callback|cookie|form)([-_].*)?$', re.I)


def _unhide_content_blocks(soup):
    """Снимает inline display:none с содержательных блоков (вкладки «Характеристики»
    и т.п. у Bitrix/WP скрыты до клика). Базовая clean_noise удаляет display:none
    целиком — вместе со спеками. Расскрываем только блоки с таблицей либо с
    плотным текстом (не меню из ссылок) и без шумовых классов."""
    for el in soup.find_all(style=_DISPLAY_NONE_RE):
        try:
            classes = el.get('class') or []
            if any(_HIDDEN_NOISE_CLASS_RE.match(c) for c in classes):
                continue
            if el.find('table') is not None:
                pass  # таблица — почти наверняка характеристики
            else:
                text_len = len(el.get_text(' ', strip=True))
                if text_len < 200:
                    continue
                link_len = sum(len(a.get_text(' ', strip=True)) for a in el.find_all('a'))
                if link_len * 2 > text_len:
                    continue  # в основном ссылки — скрытое меню, не контент
            el['style'] = _DISPLAY_NONE_RE.sub('', el.get('style') or '')
        except Exception:
            continue


def _text_len(tag) -> int:
    try:
        return len(tag.get_text(' ', strip=True))
    except Exception:
        return 0


def _link_text_len(tag) -> int:
    try:
        return sum(len(a.get_text(' ', strip=True)) for a in tag.find_all('a'))
    except Exception:
        return 0


def _has_page_h1(container, soup) -> bool:
    """Содержит ли контейнер h1 страницы (если h1 на странице есть)."""
    page_h1 = soup.find('h1')
    if page_h1 is None:
        return True  # h1 нет вовсе — не штрафуем контейнер
    h1_text = page_h1.get_text(strip=True)
    if not h1_text:
        return True
    for h in container.find_all('h1'):
        if h.get_text(strip=True) == h1_text:
            return True
    return False


def _adequate(container, soup) -> bool:
    """Контейнер адекватен, если в нём есть h1 страницы ИЛИ достаточно текста."""
    if container is None:
        return False
    if _text_len(container) >= _MIN_TEXT:
        return True
    return _has_page_h1(container, soup)


def _score_block(tag) -> float:
    """Readability-light скоринг текстовой плотности крупного блока:
    длина текста + 200×таблиц + 50×(h1..h3) − 2×длина текста ссылок."""
    text_len = _text_len(tag)
    tables = len(tag.find_all('table'))
    heads = len(tag.find_all(['h1', 'h2', 'h3']))
    links = _link_text_len(tag)
    return text_len + 200 * tables + 50 * heads - 2 * links


def select_content_container(soup) -> Tuple[object, str]:
    """Выбирает контейнер основного контента товарной страницы.

    Приоритет:
      a) microdata-контейнер [itemtype*=Product] (если содержит достаточно текста);
      b) CMS-селекторы (_CMS_SELECTORS);
      c) generic-селекторы (article/main/#content/…);
      d) скоринг текстовой плотности среди крупных блоков body;
      e) body.

    Если выбранный контейнер не содержит h1 страницы и текста мало — поднимаемся
    на уровень выше, пока не станет адекватным (или до body).

    Возвращает (tag, способ).
    """
    body = soup.body or soup

    # a) microdata Product
    md_container = soup.select_one('[itemtype*="Product"]')
    if md_container is not None and _text_len(md_container) >= _MIN_TEXT:
        return _ensure_adequate(md_container, soup), 'microdata-product'

    # b) CMS-селекторы
    for selector, label in _CMS_SELECTORS:
        try:
            cand = soup.select_one(selector)
        except Exception:
            cand = None
        if cand is not None and _adequate(cand, soup):
            return _ensure_adequate(cand, soup), f'cms:{label}'

    # c) generic-селекторы. Перебираем ВСЕ совпадения селектора и требуем, чтобы
    # контейнер содержал h1 страницы. На сеточных вёрстках (Next.js/Tailwind и т.п.)
    # <article> — это карточка ПОХОЖЕГО товара, а не карточка страницы: первый
    # попавшийся article схлопывал весь товар до соседнего (ekontaktor.ru, D80).
    # _has_page_h1 не штрафует страницы вовсе без h1 — там поведение прежнее.
    for selector, label in _GENERIC_SELECTORS:
        try:
            cands = soup.select(selector)
        except Exception:
            cands = []
        for cand in cands:
            if _adequate(cand, soup) and _has_page_h1(cand, soup):
                return _ensure_adequate(cand, soup), label

    # d) скоринг текстовой плотности среди прямых/неглубоких потомков body
    best, best_score = None, 0.0
    try:
        candidates = body.find_all(['div', 'section', 'article', 'main'], recursive=True)
    except Exception:
        candidates = []
    for tag in candidates:
        # Не рассматриваем слишком мелкие блоки.
        if _text_len(tag) < _MIN_TEXT:
            continue
        score = _score_block(tag)
        if score > best_score:
            best, best_score = tag, score
    if best is not None and best_score > 0:
        return best, 'readability'

    # e) body
    return body, 'body'


def _ensure_adequate(container, soup):
    """Если контейнер не содержит h1 страницы и беден текстом — поднимаемся к
    родителю, пока не станет адекватным или не дойдём до body."""
    body = soup.body or soup
    cur = container
    guard = 0
    while cur is not None and cur is not body and guard < 6:
        if _has_page_h1(cur, soup) or _text_len(cur) >= _MIN_TEXT:
            return cur
        cur = cur.parent
        guard += 1
    return cur if cur is not None else container


# ==================== СБОРКА MARKDOWN ТОВАРА ====================

def _norm_for_dedup(s: str) -> str:
    """Нормализация для проверки вхождения в основной markdown: убираем регистр,
    схлопываем пробелы, выкидываем неалфанумерику кроме цифр/букв."""
    s = re.sub(r'\s+', '', s.lower())
    s = re.sub(r'[^0-9a-zа-яё]', '', s)
    return s


def _structured_block(data: dict, body_markdown: str) -> str:
    """Формирует блок «Данные товара (структурированные)» только из непустых
    полей, КОТОРЫХ ещё нет в основном markdown (дедуп по нормализованному
    вхождению). Возвращает '' если добавлять нечего."""
    norm_body = _norm_for_dedup(body_markdown)

    def absent(value: str) -> bool:
        nv = _norm_for_dedup(value)
        return bool(nv) and nv not in norm_body

    lines = []
    if data.get('name') and absent(data['name']):
        lines.append(f"- Название: {data['name']}")
    if data.get('price') and absent(data['price']):
        lines.append(f"- Цена: {data['price']}")
    if data.get('sku') and absent(data['sku']):
        lines.append(f"- Артикул: {data['sku']}")
    if data.get('brand') and absent(data['brand']):
        lines.append(f"- Бренд: {data['brand']}")
    for pname, pval in data.get('properties', []):
        # Свойство добавляем, только если его значение ещё не встречается в тексте.
        if absent(pval):
            lines.append(f"- {pname}: {pval}")

    if not lines:
        return ''
    return "## Данные товара (структурированные)\n\n" + "\n".join(lines)


# Описание в разметке может прийти сырым HTML (Bitrix кладёт в JSON-LD содержимое поля).
_HTML_TAG_RE = re.compile(r'<[a-zA-Z!/]')


def _description_block(data: dict, body_markdown: str) -> str:
    """Формирует блок «## Описание» из разметки товара — для страниц, где описания
    в DOM нет вовсе: вкладка «Описание» рендерится по клику, а её текст лежит только
    в JSON-LD/RSC-payload (ekontaktor.ru на Next.js — D80).

    OpenGraph как источник НЕ берём: og:description — это SEO-врезка страницы
    («КТ6033Б. 250А. 3 полюса. Цена от…»), а не описание товара.
    Возвращает '' если описания нет, оно слишком короткое или уже есть в тексте."""
    if data.get('source') not in ('json-ld', 'microdata'):
        return ''
    desc = (data.get('description') or '').strip()
    if _HTML_TAG_RE.search(desc):
        try:
            desc = re.sub(r'\s+', ' ', BeautifulSoup(desc, 'html.parser').get_text(' ')).strip()
        except Exception:
            return ''
    if len(desc) < 40:
        return ''
    # Дедуп по нормализованному ПРЕФИКСУ: в тексте страницы то же описание может
    # отличаться хвостом (ссылка, перенос, «Подробнее»), сравнение целиком его не ловит.
    prefix = _norm_for_dedup(desc)[:60]
    if not prefix or prefix in _norm_for_dedup(body_markdown):
        return ''
    return "## Описание\n\n" + desc


def extract_product_markdown(html: str, base_url: str = '') -> Optional[str]:
    """Конвейер извлечения markdown товарной страницы:
      parse → clean_noise_product → select_content_container →
      MarkdownConverter(text_extractor) → префикс-блок структурированных данных.

    Возвращает None при пустом/мусорном результате (тогда вызывающий код
    откатится к старому generic-пути)."""
    try:
        soup = BeautifulSoup(html, 'lxml')
    except Exception:
        try:
            soup = BeautifulSoup(html, 'html.parser')
        except Exception:
            return None

    # Структурированные данные собираем ДО чистки шума (разметка может жить в
    # script/meta, которые чистка может затронуть, и в любом контейнере).
    try:
        structured = extract_structured_data(soup, base_url)
    except Exception:
        structured = {}

    # Расскрываем содержательные скрытые вкладки ДО clean_noise (иначе она
    # удалит display:none-блоки вместе с характеристиками).
    try:
        _unhide_content_blocks(soup)
    except Exception as e:
        logging.debug(f"universal_extractor: _unhide_content_blocks: {e}")

    # Базовая универсальная чистка из text_extractor (script/style/noscript,
    # .contact-form, скрытые display:none и т.п.). В старом generic-пути её
    # выполнял html_to_markdown; здесь soup парсится заново из сырого HTML,
    # поэтому без этого вызова сырой JavaScript и формы утекали в markdown.
    try:
        _T.clean_noise(soup, base_url)
    except Exception as e:
        logging.debug(f"universal_extractor: clean_noise: {e}")
    # template markdownify тоже не вычищает (strip убирает теги, но не текст детей)
    for t in soup.find_all('template'):
        try:
            t.decompose()
        except Exception:
            pass

    # Чистка не-товарного шума (переиспользуем из text_extractor — на месте).
    try:
        _T.clean_noise_product(soup)
    except Exception as e:
        logging.debug(f"universal_extractor: clean_noise_product: {e}")

    # CMS-специфичные шумовые виджеты, не имеющие «говорящих» классов.
    for sel in _EXTRA_NOISE_SELECTORS:
        for t in soup.select(sel):
            try:
                t.decompose()
            except Exception:
                pass

    # Доменный адаптер (opt-in через CLEAN_IN_UNIVERSAL): применяем его clean() к soup,
    # как это делает html_to_markdown. Иначе доменные правила очистки не действуют на
    # universal-пути (он парсит сырой HTML заново) — напр. вырезание экспликации деталей
    # чертежа во вкладке «Устройство …» на chelaz.ru (D86).
    try:
        _dom = _T._normalize_domain(base_url) if base_url else ''
        _ad = _T._get_adapters().get(_dom)
        if _ad is not None and getattr(_ad, 'CLEAN_IN_UNIVERSAL', False) and callable(getattr(_ad, 'clean', None)):
            _ad.clean(soup)
    except Exception as e:
        logging.debug(f"universal_extractor: adapter.clean: {e}")

    # Выбор контейнера.
    try:
        container, _how = select_content_container(soup)
    except Exception as e:
        logging.debug(f"universal_extractor: select_content_container: {e}")
        container = soup.body or soup

    # Конвертация тем же конвертером (степени/таблицы как в основном модуле).
    conv = _T.MarkdownConverter(heading_style='ATX', bullets='-', strip=['script', 'style'])
    try:
        body_md = conv.convert(str(container)).strip()
    except Exception as e:
        logging.debug(f"universal_extractor: конвертация контейнера: {e}")
        try:
            body_md = conv.convert(str(soup.body or soup)).strip()
        except Exception:
            return None

    if not body_md.strip():
        # Пустой основной markdown — но если есть структурированные данные с
        # именем, отдадим хотя бы их (лучше, чем ничего). Иначе None.
        if structured.get('name') or structured.get('properties'):
            sb = _structured_block(structured, '')
            return sb or None
        return None

    # Гарантируем заголовок товара: если в markdown контейнера нет '# '-строки,
    # добавляем её из h1 страницы или имени из разметки. Иначе постпроцессор
    # увидит имя в структурированном блоке и не добавит '# h1' сам.
    heading = ''
    if not re.search(r'(?m)^#\s', body_md):
        h1 = soup.find('h1')
        h1_text = h1.get_text(strip=True) if h1 else ''
        title = (h1_text or structured.get('name') or '').strip()
        if title:
            heading = f"# {title}"

    # Префикс-блок структурированных данных (только непустое и без дублей).
    parts = []
    if heading:
        parts.append(heading)
    if structured:
        sb = _structured_block(structured, body_md)
        if sb:
            parts.append(sb)
        db = _description_block(structured, body_md)
        if db:
            parts.append(db)
    parts.append(body_md)
    result = "\n\n".join(parts).strip()
    return result or None
