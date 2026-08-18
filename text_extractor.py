import re
import logging
import pkgutil
import importlib
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from markdownify import MarkdownConverter as BaseMarkdownConverter
from product_utils import apply_cp1251_replacements

# ==================== ФУНКЦИИ ДЛЯ ОБРАБОТКИ СТЕПЕНЕЙ ====================

def _normalize_sup_text(raw: str) -> str:
    """Нормализует содержимое тега <sup> в корректную строку степени для Markdown."""
    if raw is None:
        return ''
    s = re.sub(r'\s+', '', str(raw))
    SUP_MAP = {
        '⁰': '0', '¹': '1', '²': '2', '³': '3', '⁴': '4', '⁵': '5',
        '⁶': '6', '⁷': '7', '⁸': '8', '⁹': '9',
        '⁺': '+', '⁻': '-', '⁼': '=', '⁽': '(', '⁾': ')',
        'ᵃ': 'a', 'ᵇ': 'b', 'ᶜ': 'c', 'ᵈ': 'd', 'ᵉ': 'e', 'ᶠ': 'f',
        'ᵍ': 'g', 'ʰ': 'h', 'ᶦ': 'i', 'ʲ': 'j', 'ᵏ': 'k', 'ˡ': 'l',
        'ᵐ': 'm', 'ⁿ': 'n', 'ᵒ': 'o', 'ᵖ': 'p', 'ʳ': 'r', 'ˢ': 's',
        'ᵗ': 't', 'ᵘ': 'u', 'ᵛ': 'v', 'ʷ': 'w', 'ˣ': 'x', 'ʸ': 'y',
        'ᶻ': 'z'
    }
    normalized_chars = []
    for ch in s:
        normalized_chars.append(SUP_MAP.get(ch, ch))
    normalized = ''.join(normalized_chars)
    if not normalized:
        return ''
    # Классификация содержимого <sup> ДО постановки каретки. В корпусе sup содержит не только
    # степени, но и градус (буквой о/o), сноски (*), товарные знаки (®/™) и бейджи (NEW) —
    # каретка перед ними была дефектом (Е1). Ставим '^' только для настоящих числовых степеней.
    # 1) Настоящая степень: только цифры, опционально с ведущим/внутренним +/-.
    if re.fullmatch(r'[0-9\+\-]+', normalized):
        return '^' + normalized if len(normalized) == 1 else '^{' + normalized + '}'
    # 2) Градус (набран буквой о/o или знаком °): одиночная буква о/o или строка из градус-символов.
    if normalized in ('о', 'o', 'О', 'O') or re.fullmatch(r'[°º˚]+', normalized):
        return '°'
    # 3) Товарный знак.
    if normalized == '®':
        return '®'
    if normalized in ('™',) or normalized.lower() == 'tm':
        return '™'
    # 4) Сноски: строки, состоящие только из звёздочек (*, **, ∗ U+2217) — оставляем как есть.
    if re.fullmatch(r'[*∗]+', normalized):
        return normalized.replace('∗', '*')
    # 5) Буквенные бейджи вёрстки (NEW, HIT, ЗИМА…) — мусор, удаляем.
    if len(normalized) > 1 and re.fullmatch(r'[A-Za-zА-Яа-яёЁ]+', normalized):
        return ''
    # 6) Одиночная буква (кроме о/o, обработанных выше) — буква без каретки.
    if len(normalized) == 1:
        return normalized
    # 7) Прочее (смесь цифр со скобками '1)', цифра с точкой '3.' и т.п.) — без каретки, как есть.
    return normalized


def sup_tag_to_caret(tag) -> str:
    raw = tag.get_text(strip=True) if hasattr(tag, 'get_text') else str(tag)
    return _normalize_sup_text(raw)


def attach_sup_to_prev(prev_text: str, sup_tag) -> str:
    if prev_text is None:
        prev_text = ''
    prev = str(prev_text).rstrip()
    sup_part = sup_tag_to_caret(sup_tag)
    return prev + sup_part


# ==================== ОСНОВНОЙ КЛАСС КОНВЕРТЕРА ====================

class MarkdownConverter(BaseMarkdownConverter):
    """Кастомный конвертер с поддержкой таблиц, sup, br, ATX заголовков."""
    def convert_h1(self, el, text, **kwargs):
        if not text and el:
            text = el.get_text(strip=True)
        return f"\n# {text}\n\n"

    def convert_h2(self, el, text, **kwargs):
        if not text and el:
            text = el.get_text(strip=True)
        return f"\n## {text}\n\n"

    def convert_h3(self, el, text, **kwargs):
        if not text and el:
            text = el.get_text(strip=True)
        return f"\n### {text}\n\n"

    def convert_br(self, el, text, **kwargs):
        return "  \n"

    def convert_sup(self, el, text, **kwargs):
        return sup_tag_to_caret(el)

    @staticmethod
    def _span(cell, attr):
        """Безопасно читает colspan/rowspan: в корпусе встречаются битые значения вида
        '18&quot;' → '18"', на которых int() падал и обнулял весь markdown страницы.
        Берём ведущие цифры, иначе 1."""
        raw = str(cell.get(attr, 1))
        m = re.match(r'\s*(\d+)', raw)
        return int(m.group(1)) if m else 1

    def _parse_table(self, table):
        rows = table.find_all('tr')
        if not rows:
            return []
        max_cols = 0
        for tr in rows:
            cols = 0
            for cell in tr.find_all(['td', 'th']):
                cols += self._span(cell, 'colspan')
            max_cols = max(max_cols, cols)
        matrix = [[None for _ in range(max_cols)] for _ in range(len(rows))]
        for i, tr in enumerate(rows):
            col_idx = 0
            for cell in tr.find_all(['td', 'th']):
                while col_idx < max_cols and matrix[i][col_idx] is not None:
                    col_idx += 1
                if col_idx >= max_cols:
                    break
                rowspan = self._span(cell, 'rowspan')
                colspan = self._span(cell, 'colspan')
                matrix[i][col_idx] = cell
                for r in range(i, min(i + rowspan, len(rows))):
                    for c in range(col_idx, min(col_idx + colspan, max_cols)):
                        if r == i and c == col_idx:
                            continue
                        matrix[r][c] = ''
                col_idx += colspan
        return matrix

    def _cell_to_text(self, cell):
        """Извлекает текст ячейки обходом её детей (а не cell.get_text), чтобы:
        <sup> прогонялся через _normalize_sup_text и приклеивался к предыдущему тексту
        БЕЗ пробела (м<sup>3</sup>/ч → м^3/ч, 1700±10<sup>*</sup> → 1700±10*),
        <br> давал один пробел, прочие теги — рекурсивно своё содержимое."""
        parts = []

        def walk(node):
            for child in node.children:
                name = getattr(child, 'name', None)
                if name is None:
                    # NavigableString
                    parts.append(str(child))
                elif name == 'sup':
                    raw = child.get_text(strip=True)
                    parts.append(_normalize_sup_text(raw))
                elif name == 'br':
                    parts.append(' ')
                else:
                    walk(child)

        walk(cell)
        text = ''.join(parts)
        # Схлопываем повторные пробелы и обрезаем края.
        text = re.sub(r'[ \t ]+', ' ', text).strip()
        return text

    def _denormalize_table_matrix(self, matrix):
        if not matrix:
            return []
        rows = len(matrix)
        cols = len(matrix[0]) if rows > 0 else 0
        flat = [['' for _ in range(cols)] for _ in range(rows)]
        for i in range(rows):
            for j in range(cols):
                if flat[i][j] != '':
                    continue
                cell = matrix[i][j]
                if not cell or isinstance(cell, str):
                    continue
                if hasattr(cell, 'get'):
                    rowspan = self._span(cell, 'rowspan')
                    colspan = self._span(cell, 'colspan')
                    text = self._cell_to_text(cell)
                    # Баннер на всю ширину таблицы (секционный заголовок) не размножаем во все
                    # столбцы — иначе в markdown получаются дубли «| X | X | …» (Е8). Пишем текст
                    # только в первую покрытую клетку, остальные оставляем пустыми. Частичный
                    # colspan (не на всю ширину) множим как раньше — это сохраняет привязку к колонкам.
                    if colspan >= cols:
                        flat[i][j] = text
                    else:
                        for r in range(i, min(i + rowspan, rows)):
                            for c in range(j, min(j + colspan, cols)):
                                flat[r][c] = text
                else:
                    flat[i][j] = str(cell)
        return flat

    def convert_table(self, el, text, **kwargs):
        if el is None:
            return ""
        matrix = self._parse_table(el)
        if not matrix:
            return ""
        flat_matrix = self._denormalize_table_matrix(matrix)
        if not flat_matrix:
            return ""
        def row_to_md(row):
            return "| " + " | ".join(str(cell) if cell else " " for cell in row) + " |"
        header = flat_matrix[0]
        body = flat_matrix[1:]
        md_lines = [
            row_to_md(header),
            "| " + " | ".join("---" for _ in header) + " |"
        ]
        md_lines += [row_to_md(r) for r in body]
        # Якорь-заголовок таблицы: <caption> выносим как ### перед таблицей — даёт LLM явную
        # привязку и снижает склейку соседних таблиц (Е4/Е14). Разрез таблиц по смене ширины
        # строк и детекцию «таблиц-графиков» НАМЕРЕННО не делаем (нет надёжного критерия,
        # потеря данных хуже склейки) — см. отчёт 08_stage1_implementation.md.
        prefix = ""
        caption = el.find('caption')
        if caption is not None:
            cap_text = caption.get_text(separator=' ', strip=True)
            cap_text = re.sub(r'\s+', ' ', cap_text).strip()
            if cap_text:
                prefix = f"### {cap_text}\n"
        return "\n\n" + prefix + "\n".join(md_lines) + "\n\n"


# ==================== ОБЩАЯ ОЧИСТКА (УНИВЕРСАЛЬНАЯ) ====================

def clean_noise(soup, url=None, strip_hidden=True):
    """Удаляет только универсальные шумовые блоки (script, style, общие классы).

    strip_hidden=False — НЕ удалять display:none-блоки. Нужно для доменов, где скрытые
    блоки — это НЕактивные вкладки с полезным контентом (а не шум-попапы): такой адаптер
    объявляет KEEP_HIDDEN_TABS=True и сам извлекает нужные вкладки и убирает лишнее.
    """
    common_selectors = [
        'script', 'style', 'noscript', '.bottomtext',
        '.overlay', '.contact-form', '.product-form',
        '.product-hero__cart', '.product-hero__order-btn-wrap',
        '.catalog-menu', '.modal-search', '.pum-overlay',
        '.footer__up', '.vacancies-form__success'
    ]
    for selector in common_selectors:
        for tag in soup.select(selector):
            try:
                tag.decompose()
            except Exception:
                pass

    # Удаление скрытых блоков (общая логика). Пропускаем, если strip_hidden=False
    # (домен использует display:none для неактивных вкладок с полезным контентом).
    if strip_hidden:
        for tag in soup.find_all(style=re.compile(r'display\s*:\s*none', re.I)):
            try:
                tag.decompose()
            except Exception:
                pass

    return soup


# Шумовые CSS-классы для товарных страниц. Матч по ТОКЕНУ класса (не подстроке): токен должен
# НАЧИНАТЬСЯ с ключевого слова, далее опционально '-'/'_' и что угодно. Так 'cartridge' не попадёт
# под 'cart', а 'cart'/'cart-widget'/'basket_box' — попадут. Подтверждено по корпусу
# (modal/popup/breadcrumbs/cookie встречаются массово).
_NOISE_CLASS_RE = re.compile(
    r'^(breadcrumbs?|cookies?|related|similar|recommend(?:ed|ation)?|viewed|cart|basket'
    r'|callback|popup|modal|subscribe|social|share|faq)([-_].*)?$',
    re.I,
)


def clean_noise_product(soup):
    """Чистка типового НЕ-товарного шума ТОЛЬКО для товарных страниц (page_type='').
    Удаляет навигацию/футер/корзину/cookie/«похожие»/FAQ-блоки, раздувающие контекст LLM (Е3/Е8).
    На страницах company/distributor НЕ применяется (там своя консервативная логика — там в
    подобных блоках могут быть контакты). Работает по переданному soup на месте."""
    # Структурные шумовые теги.
    for sel in ('footer', 'nav', 'aside', '[role="navigation"]'):
        for tag in soup.select(sel):
            try:
                tag.decompose()
            except Exception:
                pass
    # Элементы, у которых хотя бы один класс-токен матчится с шумовым паттерном.
    # decompose() родителя отцепляет его потомков из дерева (у них attrs становится None) —
    # поэтому проверяем, что тег ещё «живой», и оборачиваем доступ в try.
    for tag in soup.find_all(class_=True):
        try:
            if getattr(tag, 'attrs', None) is None:
                continue
            classes = tag.get('class') or []
            if isinstance(classes, str):
                classes = classes.split()
            if any(_NOISE_CLASS_RE.match(c) for c in classes):
                tag.decompose()
        except Exception:
            pass
    return soup


# ==================== ДИНАМИЧЕСКАЯ ЗАГРУЗКА АДАПТЕРОВ ====================

_adapters_cache = None

def _get_adapters():
    """Загружает все адаптеры из папки Profile_Markdown и возвращает словарь {домен: модуль}."""
    global _adapters_cache
    if _adapters_cache is not None:
        return _adapters_cache

    adapters = {}
    try:
        import Profile_Markdown
        for module_info in pkgutil.iter_modules(Profile_Markdown.__path__):
            module_name = module_info.name
            if module_name.startswith('_'):
                continue
            # Импортируем модуль
            try:
                module = importlib.import_module(f'Profile_Markdown.{module_name}')
            except Exception as e:
                logging.debug(f"Не удалось импортировать модуль {module_name}: {e}")
                continue

            # Определяем домен для этого адаптера
            domain = None
            # Приоритет: атрибут DOMAIN в модуле
            if hasattr(module, 'DOMAIN') and isinstance(module.DOMAIN, str):
                domain = module.DOMAIN
            else:
                # Иначе преобразуем имя файла: hms_ru -> hms.ru
                # Для сложных имён (xn----7sbegqnkyhbtn.xn--p1ai) это не сработает,
                # поэтому для таких сайтов надо определить DOMAIN в файле адаптера
                domain = module_name.replace('_', '.')
                # Исключение для агроскон (длинное имя)
                if module_name == 'agroskon':
                    domain = 'xn----7sbegqnkyhbtn.xn--p1ai'
            if domain:
                adapters[domain] = module
                logging.debug(f"Загружен адаптер для домена {domain} из {module_name}")
    except ImportError:
        logging.warning("Папка Profile_Markdown не найдена или не является пакетом")
    except Exception as e:
        logging.error(f"Ошибка при загрузке адаптеров: {e}")

    _adapters_cache = adapters
    return adapters


def _normalize_domain(url: str) -> str:
    """Извлекает и нормализует домен из URL."""
    if not url:
        return ''
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc.startswith('www.'):
        netloc = netloc[4:]
    # Для hms.ru обрабатываем поддомены
    if netloc.endswith('.hms.ru'):
        return 'hms.ru'
    return netloc


# ============== УНИВЕРСАЛЬНЫЙ ЭКСТРАКТОР КОНТАКТОВ/ДИСТРИБЬЮТОРОВ ==============
#
# Доменно-независимое извлечение для страниц компании/контактов и дистрибьюторов
# (page_type='company'/'distributor'). Применяется в основном потоке, когда у адаптера
# нет своих extract_company/extract_distributor. Зачем: общий конвертер берёт узкий
# article/main/.content, который на контактных страницах часто пуст → markdown выходил
# почти пустым (контакты терялись целиком). Здесь берётся весь body (минус навигация)
# плюс сводка телефонов/e-mail из ссылок и микроразметки — часть контактов есть только
# в tel:/mailto:/itemprop/data-* и не попадает в видимый текст.

# Телефон РФ: +7/8 + 10–11 цифр. Границы (?<!\d)…(?!\d) отсекают длинные числа
# (css cache-busting ?16846754264840, id, координаты карт) — частый источник мусора.
_CONTACT_PHONE_RE = re.compile(
    r'(?<!\d)(?:\+7|8)\s*\(?\d{3,5}\)?[\s-]*\d{2,3}[\s-]*\d{2}(?:[\s-]*\d{2})?(?!\d)'
)
_CONTACT_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')

# Навигационный/служебный шум — НЕ контакты. Чистка КОНСЕРВАТИВНАЯ: footer/aside не трогаем
# (там часто контакты), и не удаляем по широким подстрокам [class*="modal/popup"] или .menu/.nav —
# на конструкторах (Tilda и т.п.) контактные блоки и адреса нередко имеют такие классы, и
# агрессивная чистка съедала адрес целиком. Телефоны/e-mail подстрахованы _harvest_contacts,
# но адреса идут только через основной текст, поэтому его сохраняем максимально.
_CONTACT_NOISE_SELECTORS = [
    'script', 'style', 'noscript', 'svg', 'iframe', 'form',
    'nav', '[role="navigation"]',
    '.breadcrumbs', '[class*="breadcrumb"]',
    '.cookie', '[class*="cookie-"]',
]


def _harvest_contacts(soup):
    """Собирает телефоны и e-mail из надёжных источников: ссылок tel:/mailto:, видимого
    текста и микроразметки (itemprop telephone/email, data-phone/tel). Телефоны
    дедуплицируются по нормализованному виду (последние 10 цифр), e-mail — по нижнему
    регистру. Возвращает (phones, emails) — списки человекочитаемых значений."""
    phones, emails = {}, {}

    def add_phone(s):
        if not s:
            return
        for m in _CONTACT_PHONE_RE.findall(s):
            digits = re.sub(r'\D', '', m)
            if 10 <= len(digits) <= 11:
                phones.setdefault(digits[-10:], re.sub(r'\s+', ' ', m.strip()))

    def add_email(s):
        if not s:
            return
        for m in _CONTACT_EMAIL_RE.findall(s):
            emails.setdefault(m.lower(), m)

    for a in soup.select('a[href^="tel:"]'):
        add_phone(a.get('href', '')[4:])
        add_phone(a.get_text(' ', strip=True))
    for a in soup.select('a[href^="mailto:"]'):
        add_email(a.get('href', '')[7:].split('?')[0])
        add_email(a.get_text(' ', strip=True))

    text = soup.get_text(' ', strip=True)
    add_phone(text)
    add_email(text)

    for el in soup.select('[itemprop]'):
        ip = (el.get('itemprop') or '').lower()
        val = el.get('content') or el.get_text(' ', strip=True)
        if 'tel' in ip or 'phone' in ip:
            add_phone(val)
        if 'email' in ip:
            add_email(val)
    for el in soup.select('[data-phone], [data-tel], [data-phones]'):
        for attr in ('data-phone', 'data-tel', 'data-phones'):
            if el.get(attr):
                add_phone(el.get(attr))

    return list(phones.values()), list(emails.values())


def _extract_contacts_universal(soup, base_url=''):
    """Доменно-независимое извлечение контактов/дистрибьюторов → Markdown.
    Берёт весь body (минус навигационный шум) и добавляет сводку телефонов/e-mail.
    Работает на копии soup (исходный не меняет, чтобы фолбэк на общий метод был корректен).
    Возвращает None, если извлечь нечего (тогда отработает общий конвертер)."""
    try:
        work = BeautifulSoup(str(soup), 'lxml')
    except Exception:
        work = BeautifulSoup(str(soup), 'html.parser')

    phones, emails = _harvest_contacts(work)

    for sel in _CONTACT_NOISE_SELECTORS:
        for tag in work.select(sel):
            tag.decompose()

    root = work.body or work
    conv = MarkdownConverter(heading_style='ATX', bullets='-')
    try:
        md = conv.convert(str(root)).strip()
    except Exception:
        md = ''
    md = re.sub(r'\n{3,}', '\n\n', md).strip()

    parts = [md] if md else []
    summary = []
    if phones:
        summary.append("Телефоны: " + ", ".join(phones))
    if emails:
        summary.append("Эл. почта: " + ", ".join(emails))
    if summary:
        parts.append("## Контактные данные\n\n" + "\n".join(summary))

    result = "\n\n".join(parts).strip()
    return result or None


# ==================== ОСНОВНАЯ ФУНКЦИЯ КОНВЕРТАЦИИ ====================

def html_to_markdown(html: str, url: str = '', page_type: str = '') -> str:
    """
    Конвертирует HTML в Markdown.
    Если передан url, пытается найти адаптер для соответствующего домена.

    page_type:
      ''            — обычная страница/товар (поведение по умолчанию, не меняется);
      'company'     — страница компании/контактов: если адаптер имеет метод
                      extract_company(html, base_url) — используется он;
      'distributor' — страница дистрибьюторов/партнёров: используется метод
                      extract_distributor(html, base_url), если он есть.
    Если у адаптера нет соответствующего метода (или он вернул None), применяется
    общий конвертер (товарный extract при этом НЕ вызывается, чтобы не разбирать
    страницу компании как карточку товара).
    """
    # Парсим HTML
    try:
        soup = BeautifulSoup(html, 'lxml')
    except Exception:
        soup = BeautifulSoup(html, 'html.parser')

    # Определяем base_url для относительных ссылок
    base_url = url or ''
    if not base_url:
        can = soup.select_one('link[rel="canonical"]')
        if can and can.get('href'):
            parsed = urlparse(can['href'])
            base_url = f"{parsed.scheme}://{parsed.netloc}"
        else:
            base = soup.select_one('base')
            if base and base.get('href'):
                parsed = urlparse(base['href'])
                base_url = f"{parsed.scheme}://{parsed.netloc}"

    # Поиск адаптера по домену (ДО универсальной очистки — адаптер может попросить
    # сохранить скрытые вкладки через KEEP_HIDDEN_TABS).
    adapter = None
    domain = ''
    if url:
        domain = _normalize_domain(url)
        adapters = _get_adapters()
        if domain in adapters:
            adapter = adapters[domain]

    # Универсальная очистка (всегда). Для адаптеров с KEEP_HIDDEN_TABS не трогаем
    # display:none — на таких сайтах это неактивные вкладки с полезным контентом
    # (а не шум); адаптер сам заберёт нужные вкладки и уберёт лишнее.
    soup = clean_noise(soup, url or base_url,
                       strip_hidden=not getattr(adapter, 'KEEP_HIDDEN_TABS', False))

    # Спец-обработка страниц компании/дистрибьюторов: выделенные методы адаптера
    # extract_company / extract_distributor. Товарный путь (page_type='') не затрагивается.
    if page_type in ('company', 'distributor'):
        special_attr = 'extract_company' if page_type == 'company' else 'extract_distributor'
        special_fn = getattr(adapter, special_attr, None) if adapter else None
        if callable(special_fn):
            try:
                special_result = special_fn(str(soup), base_url)
            except Exception as e:
                logging.debug(f"Ошибка в adapter.{special_attr} для {url}: {e}")
                special_result = None
            if special_result is not None:
                return _postprocess_markdown(special_result, soup, base_url)
        # Нет спец-метода адаптера (или он вернул None) → универсальный доменно-независимый
        # экстрактор контактов/дистрибьюторов. Товарный extract НЕ вызываем.
        universal_result = _extract_contacts_universal(soup, base_url)
        if universal_result is not None:
            return _postprocess_markdown(universal_result, soup, base_url)
        # Если и он ничего не дал — общий конвертер ниже.
    else:
        # Применяем специфичную очистку адаптера (если есть)
        if adapter and hasattr(adapter, 'clean') and callable(adapter.clean):
            try:
                adapter.clean(soup)
            except Exception as e:
                logging.debug(f"Ошибка в adapter.clean для {domain}: {e}")

        # Пробуем специализированное извлечение (если адаптер имеет метод extract)
        adapter_result = None
        if adapter and hasattr(adapter, 'extract') and callable(adapter.extract):
            # Часть адаптеров (напр. kolpa-san.ru) должна видеть СЫРОЙ HTML: полезные
            # данные лежат в <script> (Tilda `var product` с вариантами товара),
            # который вырезается универсальной очисткой. Такие адаптеры объявляют
            # NEEDS_RAW_HTML=True. Прочие получают очищенный DOM, как прежде.
            extract_input = html if getattr(adapter, 'NEEDS_RAW_HTML', False) else str(soup)
            try:
                adapter_result = adapter.extract(extract_input, base_url)
            except Exception as e:
                # Адаптер может выбросить исключение – пробрасываем наверх
                raise
            if adapter_result is not None:
                # Успешно обработано адаптером
                return _postprocess_markdown(adapter_result, soup, base_url)

    # --- ОБЩИЙ МЕТОД (если адаптер не справился или его нет) ---
    converter = MarkdownConverter(
        heading_style='ATX',
        bullets='-',
        strip=['script', 'style'],
    )

    def _build_markdown(root_soup):
        """Выбирает контейнер карточки и конвертирует его в markdown."""
        main = (root_soup.select_one('[itemtype*="Product"]') or
                root_soup.select_one('article') or
                root_soup.select_one('main') or
                root_soup.select_one('.content') or
                root_soup.body)
        if not main:
            main = root_soup
        try:
            return converter.convert(str(main))
        except Exception as e:
            logging.error(f"Ошибка конвертера: {e}")
            try:
                return converter.convert(str(root_soup))
            except Exception as e2:
                logging.error(f"Ошибка fallback конвертера: {e2}")
                return ""

    # Базовый вариант — без новой товарной чистки (страховка).
    baseline_markdown = _build_markdown(soup)

    # Чистку шума применяем ТОЛЬКО для товарных страниц (page_type='') и на копии soup,
    # чтобы исходный (нужный для _postprocess_markdown: title/h1) не пострадал.
    markdown = baseline_markdown
    if page_type not in ('company', 'distributor'):
        try:
            cleaned_soup = BeautifulSoup(str(soup), 'lxml')
        except Exception:
            cleaned_soup = BeautifulSoup(str(soup), 'html.parser')
        clean_noise_product(cleaned_soup)
        cleaned_markdown = _build_markdown(cleaned_soup)
        # СТРАХОВКА: если после чистки markdown подозрительно короткий — данные могли быть
        # съедены, откатываемся к базовому варианту для этой страницы. Порог: <30% длины базового
        # или <200 символов при непустом базовом.
        base_len = len(baseline_markdown.strip())
        clean_len = len(cleaned_markdown.strip())
        if base_len and (clean_len < 200 or clean_len < base_len * 0.3):
            logging.debug(
                f"clean_noise_product откат для {url}: {base_len}->{clean_len} символов"
            )
        else:
            markdown = cleaned_markdown

        # УНИВЕРСАЛЬНЫЙ ЭКСТРАКТОР (только товарные страницы): доменно-независимый
        # выбор контейнера по CMS/microdata/скорингу + структурированные данные.
        # Импорт ленивый и защищённый — при ImportError остаётся старое поведение.
        # СТРАХОВКА КАК В ЭТАПЕ 1: если новый результат None или подозрительно короче
        # текущего (<50% длины ИЛИ <200 символов при непустом текущем) — оставляем
        # текущий (generic) результат.
        try:
            import universal_extractor as _ue
        except ImportError:
            _ue = None
        except Exception:
            _ue = None
        if _ue is not None:
            try:
                universal_markdown = _ue.extract_product_markdown(html, base_url)
            except Exception as e:
                logging.debug(f"universal_extractor откат для {url}: {e}")
                universal_markdown = None
            cur_len = len(markdown.strip())
            if universal_markdown is not None:
                uni_len = len(universal_markdown.strip())
                if cur_len and (uni_len < 200 or uni_len < cur_len * 0.5):
                    logging.debug(
                        f"universal_extractor короче — откат для {url}: {cur_len}->{uni_len} символов"
                    )
                else:
                    markdown = universal_markdown

    return _postprocess_markdown(markdown, soup, base_url)


# Юникод-дроби → обычная запись N/M (применяется в _postprocess_markdown ко всему markdown).
_UNICODE_FRACTIONS = {
    '¼': '1/4', '½': '1/2', '¾': '3/4',
    '⅓': '1/3', '⅔': '2/3',
    '⅕': '1/5', '⅖': '2/5', '⅗': '3/5', '⅘': '4/5',
    '⅙': '1/6', '⅚': '5/6',
    '⅛': '1/8', '⅜': '3/8', '⅝': '5/8', '⅞': '7/8',
    '⅐': '1/7', '⅑': '1/9', '⅒': '1/10', '↉': '0/3',
}
_FRACTION_CHARS = ''.join(_UNICODE_FRACTIONS.keys())

# Литеральные надстрочные цифры/знаки (²³⁻¹ …), встречающиеся прямо в тексте (вне <sup>),
# → каретная форма степени: «см³»→«см^3», «10⁻³»→«10^-3». Без фигурных скобок ^{...}: они
# в docx_generator._clean_text вырезаются как служебные {}, поэтому в карточку всё равно не
# попадают — брать их незачем (так обе ветви, markdown и docx, дают одинаковый результат).
# Надстрочный ⁰ НЕ включаем: в потоке текста это почти всегда искажённый значок градуса
# («300⁰»), а не степень нуля (ей занимается degree-логика).
_SUP_POW_MAP = {'¹': '1', '²': '2', '³': '3', '⁴': '4', '⁵': '5', '⁶': '6',
                '⁷': '7', '⁸': '8', '⁹': '9', '⁺': '+', '⁻': '-'}
_SUP_POW_RE = re.compile('[' + ''.join(_SUP_POW_MAP) + ']+')


def _literal_superscripts_to_caret(text: str) -> str:
    """Литеральные надстрочные символы (³, ⁻¹ …) → каретная степень (^3, ^-1)."""
    return _SUP_POW_RE.sub(lambda m: '^' + ''.join(_SUP_POW_MAP[ch] for ch in m.group(0)), text)


def _postprocess_markdown(markdown: str, soup: BeautifulSoup, base_url: str = '') -> str:
    """Добавляет title и h1, если они отсутствуют в markdown."""
    title_text = ""
    if soup.title and soup.title.string:
        title_text = soup.title.string.strip()
    h1_tag = soup.find('h1')
    h1_text = ""
    if h1_tag:
        h1_text = h1_tag.get_text(strip=True)

    result_parts = []
    if title_text:
        result_parts.append(f"<title> {title_text}\n")
    if h1_text and h1_text not in markdown:
        result_parts.append(f"\n# {h1_text}\n")
    if markdown.strip():
        result_parts.append(markdown.strip())
    result = "\n\n".join(result_parts).strip()
    # Утверждённые заказчиком замены символов вне Windows-1251 (Ø→d, →→->, ÷→-, Ω→Ом, λ→лямбда …).
    # (синхронно с docx_generator._clean_text и sanitize_card_filename).
    result = apply_cp1251_replacements(result)
    # Юникод-надстрочная x ˣ (U+02E3, MODIFIER LETTER SMALL X) как разделитель размеров → строчная русская х.
    # Встречается буквально в HTML (напр. "1250ˣ460ˣ5"); приводим к единому разделителю.
    result = result.replace('ˣ', 'х')
    # Разделители размеров между числами (* • · ∙ ×, лат. x/X, строчная кир. х) → строчная русская х (U+0445).
    # Заказчик: знак умножения × скрипт загрузки карточек не читает — целевой разделитель размеров = русская х.
    # ВАЖНО: заглавная кир. Х (U+0425) НЕ заменяется — это марки сталей/сплавов (20Х13, 12Х18Н10Т,
    # где Х = хром), а не умножение. Размеры пишутся строчной х/x, марки — заглавной Х.
    # (синхронно с docx_generator._clean_text).
    result = re.sub(r'(?<=\d)[*•·∙хxX×](?=\d)', 'х', result)
    # Точка-произведение между буквами (единицы вида Ом·м, Н·м, Вт/(м·К)) → « х » с пробелами;
    # между цифрами остаётся плотная х (выше, размеры). (синхронно с docx_generator._clean_text).
    result = re.sub(r'(?<=[A-Za-zА-Яа-яёЁ])[·∙](?=[A-Za-zА-Яа-яёЁ])', ' х ', result)
    # Значок градуса не должен оставаться НИ В КАКОМ виде — целевая система, куда грузятся карточки,
    # такие символы не читает. Убираем ° (U+00B0), ˚ (U+02DA), º (U+00BA), ̊ (U+030A, комбинир.
    # кольцо сверху — встречается как «1150 ̊ С»), ℃ (U+2103), ℉ (U+2109).
    # 1) Прекомпозированные ℃/℉ разворачиваем (внутри есть буква, нельзя просто удалить): ℃→°C, ℉→F.
    result = result.replace('℃', '°C').replace('℉', 'F')
    # 2) Температура: градус (опц. с пробелами) + С/C (кир./лат.), если дальше не буква → « C»
    #    (пробел + латинская C). Покрывает «˚С», «°С», «° C», «600°С», «1150 ̊ С». (синхронно с docx_generator._clean_text)
    # 1а) Градус, набранный НУЛЁМ в суперскрипте (<sup>0</sup>С → «^0С» после конверсии sup):
    #     каретка с нулём перед С/C — значок градуса, не степень (синхронно с docx_generator._clean_text).
    result = re.sub(r'\^0(?=[ \t]*[СCсc](?![А-Яа-яёЁA-Za-z]))', '°', result)
    result = re.sub('[ \t]*[°˚º⁰\u030a][ \t]*[СCсc](?![А-Яа-яёЁA-Za-z])', ' C', result)
    # 3) Любой оставшийся значок градуса (угол «90°», «300°ВУ» и т.п.) удаляем полностью.
    result = re.sub('[°˚º⁰\u030a]', '', result)
    # Юникод-дроби (¾, ½, ⅔ …) → обычная запись N/M. Если перед дробью цифра — отделяем пробелом
    # («1¾» → «1 3/4»), чтобы не склеить в «13/4».
    result = re.sub(r'(?<=\d)(?=[' + _FRACTION_CHARS + r'])', ' ', result)
    for _frac, _val in _UNICODE_FRACTIONS.items():
        result = result.replace(_frac, _val)
    # Литеральные надстрочные символы (³ ² ⁻¹ …) → каретная степень: «г/см³»→«г/см^3».
    # (синхронно с docx_generator._clean_text).
    result = _literal_superscripts_to_caret(result)
    # Единицы площади/объёма со степенью без каретки → каретная форма: «см2»→«см^2», «м3»→«м^3»,
    # «кгс/см2»→«кгс/см^2», «г/м2»→«г/м^2». Только строчные единицы длины (м/см/мм/дм/км) + 2/3,
    # с границами, чтобы не задеть марки/коды (заглавная М, B30 и т.п.).
    result = re.sub(r'(?<![\^\dA-Za-zА-Яа-яёЁ])(мм|см|дм|км|м)([23])(?![\dA-Za-zА-Яа-яёЁ])', r'\1^\2', result)
    # Заказчик: скрипт загрузки карточек (на стороне заказчика) не читает × ≥ ≤.
    # Любой оставшийся × (напр. габариты «390×90×188») → русская х; неравенства → ASCII >= <=.
    # (синхронно с docx_generator._clean_text).
    result = result.replace('×', 'х').replace('≥', '>=').replace('≤', '<=')
    return result


# ==================== ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ====================

def guess_url_from_filename(filename: str, html: str = "") -> str:
    """Пытается восстановить базовый URL по имени файла или HTML."""
    name = filename.lower()
    if 'hms.ru' in name or 'hms_ru' in name:
        return 'https://hms.ru'
    if 'alfapol.ru' in name or 'alfapol_ru' in name:
        return 'https://alfapol.ru'
    if 'agroskon' in name or 'xn----7sbegqnkyhbtn' in name:
        return 'https://xn----7sbegqnkyhbtn.xn--p1ai'
    if 'ventland.ru' in name or 'ventland_ru' in name:
        return 'https://ventland.ru'
    if 'tizol.com' in name or 'tizol_com' in name:
        return 'https://tizol.com'
    if html:
        try:
            soup = BeautifulSoup(html, 'lxml')
        except Exception:
            soup = BeautifulSoup(html, 'html.parser')
        can = soup.select_one('link[rel="canonical"]')
        if can and can.get('href'):
            parsed = urlparse(can['href'])
            return f"{parsed.scheme}://{parsed.netloc}"
        base = soup.select_one('base')
        if base and base.get('href'):
            parsed = urlparse(base['href'])
            return f"{parsed.scheme}://{parsed.netloc}"
    return ''