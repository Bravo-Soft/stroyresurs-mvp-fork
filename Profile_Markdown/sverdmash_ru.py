DOMAIN = 'sverdmash.ru'

# sverdmash.ru — старый сайт, свёрстанный ВЛОЖЕННЫМИ layout-таблицами без классов/id
# (шапка, левое меню, подвал — тоже <table>). Универсальный конвертер превращал такие
# layout-таблицы в «простыни» из пустых «| | | … |», а полезные таблицы характеристик
# и таблица РАЗМЕРОВ ДЛЯ ЧЕРТЕЖА шли вперемешку. Этот адаптер:
#   1) выделяет контентный контейнер (предок <h1>, ещё не захватывающий меню/подвал);
#   2) собирает markdown из ЧАСТЕЙ (h1 + описание + полезные таблицы), НЕ конвертируя
#      layout-таблицы целиком;
#   3) выбрасывает таблицу «Размеры, в мм» с буквенными обозначениями L/L1/…/D5/d/n/с —
#      это данные, осмысленные только на чертеже (в карточку чертёж не идёт).
# Объединённые ячейки полезных таблиц (шапка «Насосы»/«Электродвигатели» на colspan)
# раскрываются штатным convert_table (значение размножается по всем покрытым ячейкам).

import re
import copy
from bs4 import BeautifulSoup
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from text_extractor import MarkdownConverter


def _is_nav_or_footer(text: str) -> bool:
    """Признак, что элемент захватил левое меню или подвал (а не только карточку)."""
    return (('ГЛАВНАЯ' in text and 'КАТАЛОГ' in text)
            or 'Создание сайта' in text
            or '© ООО' in text)


def _content_container(h1):
    """Наибольший предок <h1>, ещё НЕ содержащий навигацию/подвал.
    Сайт табличной вёрстки: карточка лежит в правой ячейке, меню — в левой; поднимаемся
    вверх, пока в тексте предка не появится меню/подвал."""
    node = h1
    container = h1
    while node.parent is not None and node.parent.name not in ('body', 'html', '[document]'):
        if _is_nav_or_footer(node.parent.get_text(' ', strip=True)):
            break
        container = node.parent
        node = node.parent
    return container


def _span_int(cell, attr) -> int:
    m = re.match(r'\s*(\d+)', str(cell.get(attr, 1)))
    return int(m.group(1)) if m else 1


def _is_drawing_table(table) -> bool:
    """Таблица РАЗМЕРОВ ДЛЯ ЧЕРТЕЖА (в карточку не выводим). Признаки sverdmash.ru:
      · заголовочная ячейка «Размеры…» на большом colspan (>=6); ИЛИ
      · строка, где >=6 ячеек — одиночные размерные обозначения (L, L1, B2, D5, d, n, с …),
        осмысленные только вместе с чертежом.
    Обычные таблицы характеристик (Марка/Подача/Напор/Мощность…) под правило не попадают:
    их заголовки — слова, а не одиночные буквы."""
    for c in table.find_all(['td', 'th']):
        if _span_int(c, 'colspan') >= 6 and re.search(r'Размер', c.get_text(' ', strip=True)):
            return True
    for tr in table.find_all('tr'):
        codes = sum(1 for c in tr.find_all(['td', 'th'])
                    if re.fullmatch(r'[A-Za-zА-Яа-я]\s*\d?', c.get_text(' ', strip=True) or ''))
        if codes >= 6:
            return True
    return False


def _row_has_hspan(row) -> bool:
    """В строке есть соседние ОДИНАКОВЫЕ непустые ячейки — признак сгруппированной шапки
    (значение colspan-ячейки размножено денормализацией). У баннера на всю ширину такого нет
    (текст только в первой ячейке), поэтому баннеры под правило не попадают."""
    return any(row[i] and row[i] == row[i - 1] for i in range(1, len(row)))


def _row_looks_like_data(row) -> bool:
    """Строка похожа на ДАННЫЕ (а не на подзаголовки): половина+ непустых ячеек — чисто
    числовые/прочерк. Нужна, чтобы не «склеить» шапку со строкой данных."""
    vals = [c.strip() for c in row if c and c.strip()]
    if not vals:
        return False
    nums = sum(1 for c in vals if re.fullmatch(r'[\d\s.,\-–—]+', c))
    return nums >= max(2, len(vals) // 2)


def _spec_table_md(table, conv) -> str:
    """Таблица характеристик → markdown с ОБЪЕДИНЕНИЕМ двухуровневой шапки (группа +
    подстолбец) в один ряд УНИКАЛЬНЫХ заголовков «Группа: Подстолбец». Так одноимённые
    подстолбцы под разными группами (напр. «Частота вращения» и у насоса, и у
    электродвигателя) не сливаются и LLM не теряет значения (потеря значений недопустима).
    Таблицы с обычной одноуровневой шапкой не меняются. Объединённые ячейки данных
    (rowspan/colspan) уже развёрнуты денормализацией."""
    flat = conv._denormalize_table_matrix(conv._parse_table(table))
    if not flat:
        return ''
    if len(flat) >= 3 and _row_has_hspan(flat[0]) and not _row_looks_like_data(flat[1]):
        group, sub = flat[0], flat[1]
        header = []
        for g, s in zip(group, sub):
            g = (g or '').strip()
            s = (s or '').strip()
            header.append(f"{g}: {s}" if (g and s and g != s) else (s or g))
        body = flat[2:]
    else:
        header, body = flat[0], flat[1:]

    def _row_md(r):
        return "| " + " | ".join(c if c else " " for c in r) + " |"

    lines = [_row_md(header), "| " + " | ".join("---" for _ in header) + " |"]
    lines += [_row_md(r) for r in body]
    return "\n".join(lines)


def extract(html: str, base_url: str = '') -> str | None:
    """Карточка товара sverdmash.ru → Markdown. None, если <h1> не найден
    (тогда отработает общий конвертер html_to_markdown)."""
    soup = BeautifulSoup(html, 'lxml')
    h1 = soup.find('h1')
    if h1 is None:
        return None

    container = _content_container(h1)
    conv = MarkdownConverter(heading_style='ATX', bullets='-')
    h1_text = h1.get_text(' ', strip=True)
    parts = [f"# {h1_text}"]

    # --- Описание: текст контейнера БЕЗ h1 и БЕЗ листовых (данных) таблиц ---
    # Листовые таблицы (характеристики/чертёж) убираем из копии — остаётся связный текст.
    desc_soup = copy.copy(container)
    for hh in desc_soup.find_all('h1'):
        hh.decompose()
    for t in desc_soup.find_all('table'):
        if not t.find('table'):        # листовая таблица данных
            t.decompose()
    desc = re.sub(r'\n{2,}', '\n\n', desc_soup.get_text('\n', strip=True)).strip()
    # Убираем строки-дубликаты названия товара (layout-«хлебные крошки»).
    desc = '\n'.join(ln for ln in desc.split('\n')
                     if ln.strip() and ln.strip() != h1_text).strip()
    if desc:
        parts.append(desc)

    # --- Полезные таблицы: листовые (не layout-обёртки) и не чертёжные ---
    for t in container.find_all('table'):
        if t.find('table'):            # layout-обёртка — не выводим как таблицу
            continue
        if _is_drawing_table(t):        # размеры для чертежа — выбрасываем
            continue
        md = _spec_table_md(t, conv).strip()
        if md:
            parts.append(md)

    markdown = re.sub(r'\n{3,}', '\n\n', '\n\n'.join(p for p in parts if p)).strip()
    return markdown or None
