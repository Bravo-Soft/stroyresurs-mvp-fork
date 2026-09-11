# data_island.py
"""
Извлечение структурированных данных товара из HTML БЕЗ запуска браузера
и эвристический детектор «нужен ли JS-рендер».

Многие «динамические» сайты (React/Vue/Next/Nuxt, Bitrix/1С) кладут полные данные
товара прямо в HTML внутри <script> (JSON-LD, __NEXT_DATA__, __NUXT__,
window.__INITIAL_STATE__, inline application/json). В таких случаях запускать
Playwright не нужно — данные уже пришли обычным GET-запросом.

Модуль синхронный, без сетевого/файлового I/O — работает с уже полученным HTML.
Используется в лестнице эскалации web_crawler._fetch_page_content.
"""
import re
import json
import logging
from typing import Optional, Dict, Any, List

from bs4 import BeautifulSoup

log = logging.getLogger("crawler")

# Типы JSON-LD, которые несут данные товара
_PRODUCT_LD_TYPES = {"product", "offer", "aggregateoffer", "itemlist", "individualproduct"}

# Ключи, по которым опознаём «товарный» объект в произвольном JSON (Next/Nuxt/Redux)
_PRODUCT_KEY_HINTS = {
    "sku", "price", "offers", "characteristics", "specifications", "specs",
    "артикул", "цена", "характеристики", "properties", "params",
}


def _safe_json_loads(text: str) -> Optional[Any]:
    """json.loads с мягкой обработкой хвостовых запятых и обёрток."""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    # Частый случай: хвостовые запятые перед } или ]
    try:
        cleaned = re.sub(r",\s*([}\]])", r"\1", text)
        return json.loads(cleaned)
    except Exception:
        return None


def _iter_dicts(obj: Any, _depth: int = 0):
    """Рекурсивный обход вложенных dict/list (с ограничением глубины)."""
    if _depth > 12:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts(v, _depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_dicts(v, _depth + 1)


def _looks_like_product(d: Dict[str, Any]) -> bool:
    """Эвристика: похож ли dict на карточку товара."""
    if not isinstance(d, dict):
        return False
    keys = {str(k).lower() for k in d.keys()}
    has_name = bool(keys & {"name", "title", "наименование", "название"})
    has_signal = bool(keys & _PRODUCT_KEY_HINTS)
    return has_name and has_signal


def _extract_json_ld(soup: BeautifulSoup) -> List[dict]:
    out = []
    for tag in soup.find_all("script", type=lambda t: t and "ld+json" in t.lower()):
        data = _safe_json_loads(tag.string or tag.get_text() or "")
        if data is None:
            continue
        # JSON-LD может быть объектом, списком или иметь @graph
        candidates = []
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            candidates = data.get("@graph", [data]) if "@graph" in data else [data]
        for c in candidates:
            if isinstance(c, dict):
                t = c.get("@type", "")
                types = {t.lower()} if isinstance(t, str) else {str(x).lower() for x in (t or [])}
                if types & _PRODUCT_LD_TYPES:
                    out.append(c)
    return out


def _extract_tagged_json(soup: BeautifulSoup, tag_id: str) -> Optional[dict]:
    tag = soup.find("script", id=tag_id)
    if tag:
        data = _safe_json_loads(tag.string or tag.get_text() or "")
        if isinstance(data, dict):
            return data
    return None


def _extract_window_assign(html: str, var_pattern: str) -> Optional[Any]:
    """Достаёт window.<VAR> = {...}; из inline-скрипта по сбалансированным скобкам."""
    m = re.search(var_pattern, html)
    if not m:
        return None
    start = html.find("{", m.end() - 1)
    if start == -1:
        return None
    depth = 0
    for i in range(start, min(len(html), start + 2_000_000)):
        ch = html[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return _safe_json_loads(html[start:i + 1])
    return None


def _extract_inline_json(soup: BeautifulSoup) -> List[dict]:
    out = []
    for tag in soup.find_all("script", type="application/json"):
        data = _safe_json_loads(tag.string or tag.get_text() or "")
        if isinstance(data, (dict, list)):
            out.append(data)
    return out


def _normalize_product(sources_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Сводит первый найденный товарный объект к единой схеме."""
    # JSON-LD приоритетнее (стандартизирован)
    for ld in sources_data.get("json_ld", []):
        offers = ld.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = offers.get("price") if isinstance(offers, dict) else None
        specs = {}
        for prop in ld.get("additionalProperty", []) or []:
            if isinstance(prop, dict) and prop.get("name"):
                specs[str(prop["name"])] = prop.get("value", "")
        return {
            "name": ld.get("name"),
            "price": price,
            "currency": offers.get("priceCurrency") if isinstance(offers, dict) else None,
            "sku": ld.get("sku") or ld.get("mpn"),
            "brand": (ld.get("brand") or {}).get("name") if isinstance(ld.get("brand"), dict) else ld.get("brand"),
            "description": ld.get("description"),
            "images": ld.get("image") if isinstance(ld.get("image"), list) else ([ld["image"]] if ld.get("image") else []),
            "specs": specs,
            "_source": "json_ld",
        }
    # Иначе — ищем товарный dict в произвольных островах
    for key in ("next_data", "nuxt", "initial_state"):
        blob = sources_data.get(key)
        if blob:
            for d in _iter_dicts(blob):
                if _looks_like_product(d):
                    return {
                        "name": d.get("name") or d.get("title"),
                        "price": d.get("price"),
                        "sku": d.get("sku") or d.get("article") or d.get("артикул"),
                        "description": d.get("description"),
                        "specs": d.get("characteristics") or d.get("properties") or {},
                        "_source": key,
                    }
    for blob in sources_data.get("inline_json", []):
        for d in _iter_dicts(blob):
            if _looks_like_product(d):
                return {
                    "name": d.get("name") or d.get("title"),
                    "price": d.get("price"),
                    "sku": d.get("sku") or d.get("article"),
                    "description": d.get("description"),
                    "specs": d.get("characteristics") or d.get("properties") or {},
                    "_source": "inline_json",
                }
    return None


def extract_data_islands(html: str, soup: Optional[BeautifulSoup] = None) -> Dict[str, Any]:
    """
    Достаёт структурированные данные товара из HTML без рендера.

    Возвращает dict:
      found    : bool                — нашли ли осмысленные данные товара
      product  : dict | None         — нормализованный товар
      sources  : list[str]           — какие источники сработали
      raw      : dict                — сырые острова (для отладки/доп.обработки)
    """
    if not html:
        return {"found": False, "product": None, "sources": [], "raw": {}}
    soup = soup or BeautifulSoup(html, "html.parser")

    raw = {
        "json_ld": _extract_json_ld(soup),
        "next_data": _extract_tagged_json(soup, "__NEXT_DATA__"),
        "nuxt": _extract_window_assign(html, r"window\.__NUXT__\s*="),
        "initial_state": (
            _extract_window_assign(html, r"window\.__INITIAL_STATE__\s*=")
            or _extract_window_assign(html, r"window\.__PRELOADED_STATE__\s*=")
        ),
        "inline_json": _extract_inline_json(soup),
    }
    sources = [k for k, v in raw.items() if v]
    product = _normalize_product(raw)
    # found=True только если есть имя И хотя бы один товарный сигнал
    found = bool(product and product.get("name") and (
        product.get("price") is not None or product.get("specs") or product.get("description")
    ))
    return {"found": found, "product": product, "sources": sources, "raw": raw}


def has_product_island(html: str, soup: Optional[BeautifulSoup] = None) -> bool:
    """Быстрая проверка наличия валидного товарного острова в HTML."""
    try:
        return extract_data_islands(html, soup)["found"]
    except Exception as e:
        log.debug(f"has_product_island: ошибка разбора островов: {e}")
        return False


def needs_javascript(html: str, *, text_threshold: int = 300,
                     spa_empty_chars: int = 50, min_bundles: int = 1,
                     soup: Optional[BeautifulSoup] = None) -> bool:
    """
    True  -> HTML «пустой», контент рисуется клиентом -> нужен Playwright.
    False -> контент уже в HTML (или есть data-island) -> рендер не нужен.
    """
    if not html:
        return True
    try:
        soup = soup or BeautifulSoup(html, "html.parser")
        # Данные товара в <script> — рендер не нужен
        if has_product_island(html, soup):
            return False
        body = soup.body
        if body is None:
            return True
        # Объём осмысленного текста без script/style/noscript
        for tag in body(["script", "style", "noscript", "template"]):
            tag.extract()
        text_len = len(body.get_text(strip=True))
        if text_len < spa_empty_chars:
            return True
        # Признаки SPA: пустой контейнер фреймворка
        spa_root = soup.select_one("#root, #app, [data-reactroot], #__next, #__nuxt")
        spa_empty = spa_root is not None and len(spa_root.get_text(strip=True)) < spa_empty_chars
        # Бандлы при пустом контенте
        bundles = len(soup.find_all("script", src=re.compile(r"\.(bundle|chunk|vendor|main)\.[\w]*\.?js")))
        if text_len < text_threshold and (spa_empty or bundles >= min_bundles):
            return True
        return False
    except Exception as e:
        log.debug(f"needs_javascript: ошибка анализа HTML: {e}")
        return True


def looks_like_product_shell(html: str, *, min_text: int = 1000,
                             soup: Optional[BeautifulSoup] = None) -> bool:
    """
    True -> товарная страница пришла «оболочкой»: разметка на месте, а видимого текста
    почти нет, значит контент дорисовывает JS -> нужен Playwright.

    Зачем отдельно от needs_javascript: тот требует ЕЩЁ и признак SPA (пустой #root/#app
    либо бандлы), которого на серверных CMS нет, — поэтому оболочки таких сайтов он
    пропускал, и HTTP-first отдавал страницу без товара. Прогон 20-21.08: 2333 товарные
    страницы ушли без браузера против 287 через него.

    Порог 1000 обоснован тем же прогоном: у 46 компаний, давших карточки, минимум
    видимого текста товарной страницы = 1307 симв. (медиана 5211); у компаний, потерявших
    товары на этом гейте, — 287..903 (АргументПласт: HTML 47 КБ при 776 симв. текста).
    """
    if not html:
        return True
    try:
        soup = soup or BeautifulSoup(html, "html.parser")
        # Данные товара уже в разметке (JSON-LD/микроразметка) -> рендер не нужен
        if has_product_island(html, soup):
            return False
        body = soup.body
        if body is None:
            return True
        for tag in body(["script", "style", "noscript", "template"]):
            tag.extract()
        return len(body.get_text(strip=True)) < min_text
    except Exception as e:
        # Fail-open: при ошибке разбора не эскалируем, остаётся прежнее поведение
        log.debug(f"looks_like_product_shell: ошибка анализа HTML: {e}")
        return False


def is_unrendered_store_listing(html: str) -> bool:
    """True для Tilda store-ЛИСТИНГА (каталога), плитки товаров которого рисуются JS.

    Признак: в HTML есть контейнер сетки магазина (js-store-grid-cont / t-store__grid),
    но ссылок на карточки товаров (…/tproduct/…) в статическом HTML НЕТ, и это не
    страница одиночного товара (нет объекта `var product`). Такой листинг нужно
    рендерить браузером (Playwright), иначе плитки-ссылки не попадут во фронтир и
    целый раздел каталога теряется (D96, kolpa-san.ru /dushevye_ograzhdeniya).
    """
    if not html:
        return False
    # Признаки магазина Tilda: контейнер сетки ЛИБО карточная разметка. камзавод.рф рисует
    # каталог как t-store__card / data-product-lid БЕЗ грид-контейнера — иначе весь Tilda-store
    # каталог терялся (0 товаров), см. память kamensk-tilda-store-0-products.
    _store_markers = ('js-store-grid-cont', 't-store__grid', 't-store__card', 'data-product-lid')
    if not any(m in html for m in _store_markers):
        return False
    if 'var product' in html:
        return False  # страница одиночного товара, а не листинг
    return re.search(r'tproduct[/-]\d', html) is None
