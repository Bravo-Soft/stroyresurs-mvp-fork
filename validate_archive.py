# validate_archive.py 1.0.0
# -*- coding: utf-8 -*-
"""
QA-гейт конечного продукта: проверка ZIP «Отчёт_об_изменениях.zip» ДО выдачи пользователю.

Спецификация: "Анализ системы/_agents/07_metrics_qa.md" (метрики C1–C5, привязка к
критериям приёмки ТЗ I.1.4 и Е-кодам редакторского классификатора).

Запуск:
    python3 validate_archive.py <путь к .zip или к распакованной папке> [--json out.json]

Выход: человекочитаемая таблица + quality_report.json.
Код возврата: 0 = PASS, 1 = WARN (выдать можно, нужен взгляд редактора), 2 = BLOCK (не выдавать).

Зависимости: только stdlib + lxml (openpyxl/pandas НЕ требуются — гейт должен работать
и в контейнере, и на голом питоне). Дедупликация файлов по хэшу сознательно НЕ выполняется.
"""
import argparse
import json
import re
import sys
import tempfile
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from lxml import etree

# ==================== Константы проверок ====================

REPORTS_DIR_NAME = "Отчёт об изменениях сайтов компаний производителей"
FILES_DIR_NAME = "Дополнительные файлы к отчёту"
GENERAL_REPORT_NAME = "Таблица общего отчёта об изменениях на сайтах компаний производителей.xlsx"
DETAILED_REPORT_NAME = "Таблица детального отчёта об изменениях на сайте компаний производителей.xlsx"

# Телефон по ТЗ: «+7 ХХХ ххх хх хх», префикс 3-5 цифр
PHONE_RE = re.compile(r'^\+7 \d{3,5} \d{2,3} \d{2} \d{2}$')
# Литеральная каретка перед нечислом (Е1/Е13): «^» допустима только перед цифрой или {
CARET_BAD_RE = re.compile(r'\^[^0-9{]')
# Числовой токен для подсчёта «≥3 числовых характеристик»
NUMBER_RE = re.compile(r'\d+(?:[.,]\d+)?')

# Справочники ТЗ
CHANGE_TYPE_ENUM = {
    "Новые данные", "Данные не найдены", "Редактирование",
    "Ошибка, сайт не работает", "Изменений не найдено",
}
# ТЗ I.4.1.3: «Есть изменения» / «Нет изменений». «Новая компания» — известное отклонение
# (решение заказчика по маппингу — этап 5.7 плана), поэтому отдельный WARN, а не BLOCK.
PRESENCE_ENUM = {"Есть изменения", "Нет изменений"}
PRESENCE_KNOWN_EXTRA = {"Новая компания"}

# Допустимые расширения в «Дополнительные файлы к отчёту» (ТЗ I.4.1.2)
ALLOWED_EXT = {".pdf", ".rtf"}
# Подстроки имён мусорных файлов (синхронно с archive_builder.blacklist_substrings)
NAME_BLACKLIST = ("crawling_stats", "politika-v-otnosenii", "obrabotki-pdn")

# Обязательные колонки общего отчёта для метрики полноты C1.1
GENERAL_MANDATORY_COLS = [
    "Дата проверки сайта",
    "Наличие изменений на сайте компании производителя",
    "Наименование компании производителя",
    "Адрес компании производителя",
    "Телефоны компании производителя",
    "Сайт компании производителя",
    "E-mail компании производителя",
    "Регион производства",
    "Количество поставщиков",
    "Количество товаров",
    "Количество файлов производителя",
]
DETAILED_MANDATORY_COLS = [
    "п/п изм.",
    "Название компании производителя",
    "Тип изменения",
]

# Какие проверки блокируют выдачу (настраивается заказчиком по мере ужесточения к 100%)
THRESHOLDS = {
    "block": {"C2.4", "C2.5", "C3.1", "C3.6", "C4.1", "C5.3", "C5.4", "C5.5"},
    # остальные провалы => WARN
}


# ==================== Разбор xlsx (lxml, без openpyxl) ====================

_NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
       'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}


def _col_idx(ref: str) -> int:
    m = re.match(r'([A-Z]+)', ref)
    n = 0
    for ch in m.group(1):
        n = n * 26 + ord(ch) - 64
    return n - 1


def parse_xlsx_rows(xlsx_path: Path):
    """Первый лист xlsx -> (headers, rows: list[dict header->str]). Поддержка sharedStrings и inlineStr."""
    z = zipfile.ZipFile(xlsx_path)
    shared = []
    if 'xl/sharedStrings.xml' in z.namelist():
        root = etree.fromstring(z.read('xl/sharedStrings.xml'))
        for si in root.findall('m:si', _NS):
            shared.append(''.join(t.text or '' for t in si.iter('{%s}t' % _NS['m'])))
    wb = etree.fromstring(z.read('xl/workbook.xml'))
    rels = etree.fromstring(z.read('xl/_rels/workbook.xml.rels'))
    relmap = {rel.get('Id'): rel.get('Target') for rel in rels}
    sheet = wb.find('m:sheets', _NS)[0]
    target = relmap[sheet.get('{%s}id' % _NS['r'])].lstrip('/')
    if not target.startswith('xl/'):
        target = 'xl/' + target
    root = etree.fromstring(z.read(target))
    raw_rows = []
    for row in root.find('m:sheetData', _NS):
        cells = {}
        for c in row:
            v = c.find('m:v', _NS)
            if v is None:
                isn = c.find('m:is', _NS)
                val = ''.join(t.text or '' for t in isn.iter('{%s}t' % _NS['m'])) if isn is not None else ''
            else:
                val = v.text or ''
                if c.get('t') == 's':
                    val = shared[int(val)]
            cells[_col_idx(c.get('r', 'A1'))] = val
        raw_rows.append(cells)
    if not raw_rows:
        return [], []
    width = max((max(r) + 1) for r in raw_rows if r) if any(raw_rows) else 0
    headers = [str(raw_rows[0].get(j, '')).strip() for j in range(width)]
    rows = []
    for r in raw_rows[1:]:
        if not r:
            continue
        rows.append({headers[j]: str(r.get(j, '')).strip() for j in range(width) if headers[j]})
    return headers, rows


# ==================== Лёгкий RTF -> текст ====================

_RTF_SKIP_GROUPS = (r'\fonttbl', r'\colortbl', r'\stylesheet', r'\info', r'\pict',
                    r'\*\generator', r'\*')


def rtf_to_text(data: bytes) -> str:
    """Грубое извлечение видимого текста из RTF: cp1251-эскейпы, \\uN, пропуск служебных групп."""
    try:
        s = data.decode('latin-1', errors='ignore')
    except Exception:
        return ''
    out = []
    i, n = 0, len(s)
    skip_depth = 0          # глубина внутри пропускаемой группы
    depth = 0
    while i < n:
        ch = s[i]
        if ch == '{':
            depth += 1
            # пропускаемая служебная группа?
            rest = s[i + 1:i + 16]
            if skip_depth == 0 and any(rest.startswith(g) for g in _RTF_SKIP_GROUPS):
                skip_depth = depth
            i += 1
        elif ch == '}':
            if skip_depth and depth == skip_depth:
                skip_depth = 0
            depth -= 1
            i += 1
        elif ch == '\\':
            if i + 1 < n and s[i + 1] == "'":
                hexv = s[i + 2:i + 4]
                if skip_depth == 0:
                    try:
                        out.append(bytes([int(hexv, 16)]).decode('cp1251', errors='ignore'))
                    except ValueError:
                        pass
                i += 4
            elif i + 1 < n and s[i + 1] in '\\{}':
                if skip_depth == 0:
                    out.append(s[i + 1])
                i += 2
            else:
                m = re.match(r'\\([a-zA-Z]+)(-?\d+)? ?', s[i:])
                if m:
                    word, num = m.group(1), m.group(2)
                    if skip_depth == 0:
                        if word == 'u' and num is not None:
                            code = int(num)
                            if code < 0:
                                code += 65536
                            out.append(chr(code))
                            # после \uN идёт замещающий символ — пропускаем его
                            tail = s[i + m.end():]
                            if tail[:2] == "\\'":
                                i += m.end() + 4
                                continue
                            if tail[:1] not in ('\\', '{', '}'):
                                i += m.end() + 1
                                continue
                        elif word in ('par', 'line', 'row', 'cell', 'tab'):
                            out.append('\n' if word in ('par', 'line', 'row') else ' ')
                    i += m.end()
                else:
                    i += 1
        else:
            if skip_depth == 0 and ch not in '\r\n':
                out.append(ch)
            i += 1
    text = ''.join(out)
    return re.sub(r'[ \t]+', ' ', text)


# ==================== Инфраструктура проверок ====================

class Check:
    def __init__(self, cid, criterion, passed, score, msg, examples=None):
        self.cid = cid
        self.criterion = criterion          # 'c1'..'c5'
        self.passed = bool(passed)
        self.score = max(0.0, min(1.0, float(score)))
        self.msg = msg
        self.examples = list(examples or [])[:5]
        self.blocking = cid in THRESHOLDS["block"]

    def to_dict(self):
        return {"check": self.cid, "passed": self.passed, "score": round(self.score, 3),
                "blocking": self.blocking, "msg": self.msg, "examples": self.examples}


def load_archive(path: Path):
    """ZIP -> распаковка во временную папку; папка -> как есть. Возвращает (root, tmp|None)."""
    if path.is_dir():
        return path, None
    tmp = tempfile.TemporaryDirectory(prefix="validate_archive_")
    with zipfile.ZipFile(path) as z:
        z.extractall(tmp.name)
    return Path(tmp.name), tmp


def _phones_list(cell: str):
    return [p.strip() for p in cell.replace('\r', '').split('\n') if p.strip()]


def _norm_phone(p: str) -> str:
    return re.sub(r'\D', '', p)[-10:]


def _to_int(v: str):
    try:
        return int(float(str(v).replace(',', '.')))
    except (ValueError, TypeError):
        return None


def _company_id_norm(v: str) -> str:
    v = str(v).strip()
    if v.endswith('.0') and v[:-2].isdigit():
        v = v[:-2]
    return v


# ==================== Проверки ====================

def check_tables_complete(general, detailed):
    checks = []
    # C1.1 полнота обязательных ячеек общего отчёта
    total = filled = 0
    empty_examples = []
    for row in general:
        for col in GENERAL_MANDATORY_COLS:
            if col not in row:
                continue
            total += 1
            if row[col]:
                filled += 1
            elif len(empty_examples) < 5:
                empty_examples.append(f"{row.get('Наименование компании производителя', '?')}: «{col}» пусто")
    score = filled / total if total else 1.0
    checks.append(Check("C1.1", "c1", score >= 1.0, score,
                        f"полнота обязательных ячеек общего отчёта {filled}/{total}", empty_examples))
    # C1.2 полнота детального
    total = filled = 0
    examples = []
    for row in detailed:
        for col in DETAILED_MANDATORY_COLS:
            if col not in row:
                continue
            total += 1
            if row[col]:
                filled += 1
            elif len(examples) < 5:
                examples.append(f"строка {row.get('п/п изм.', '?')}: «{col}» пусто")
    score = filled / total if total else 1.0
    checks.append(Check("C1.2", "c1", score >= 1.0, score,
                        f"полнота обязательных ячеек детального отчёта {filled}/{total}", examples))
    # C1.3 «Путь в архиве» заполнен у строк товара/файла
    need = have = 0
    examples = []
    for row in detailed:
        if row.get("Наименование товара") or row.get("Наименование доп. материала"):
            need += 1
            if row.get("Путь в архиве к файлу"):
                have += 1
            elif len(examples) < 5:
                examples.append(f"строка {row.get('п/п изм.', '?')}: путь пуст")
    score = have / need if need else 1.0
    checks.append(Check("C1.3", "c1", score >= 1.0, score,
                        f"«Путь в архиве» заполнен {have}/{need}", examples))
    # C1.4 счётчики числовые
    total = ok = 0
    for row in general:
        for col in ("Количество поставщиков", "Количество товаров", "Количество файлов производителя"):
            if col in row:
                total += 1
                if _to_int(row[col]) is not None:
                    ok += 1
    score = ok / total if total else 1.0
    checks.append(Check("C1.4", "c1", score >= 1.0, score, f"числовые счётчики {ok}/{total}"))
    return checks


def check_cell_values(general, detailed, files_root: Path):
    checks = []
    # C2.1 формат телефонов + C2.2 дедуп/лимит/разделитель
    total = valid = 0
    fmt_examples = []
    dedup_ok = True
    dedup_examples = []
    for row in general:
        cell = row.get("Телефоны компании производителя", "")
        if not cell:
            continue
        if ',' in cell and '\n' not in cell:
            dedup_ok = False
            dedup_examples.append(f"{row.get('Наименование компании производителя', '?')}: телефоны через запятую")
        phones = _phones_list(cell)
        for p in phones:
            total += 1
            if PHONE_RE.match(p):
                valid += 1
            elif len(fmt_examples) < 5:
                fmt_examples.append(p)
        norm = [_norm_phone(p) for p in phones]
        if len(set(norm)) != len(norm) or len(phones) > 4:
            dedup_ok = False
            if len(dedup_examples) < 5:
                dedup_examples.append(
                    f"{row.get('Наименование компании производителя', '?')}: {len(phones)} ном., уникальных {len(set(norm))}")
    score = valid / total if total else 1.0
    checks.append(Check("C2.1", "c2", score >= 1.0, score,
                        f"телефоны в формате «+7 ХХХ ххх хх хх»: {valid}/{total}", fmt_examples))
    checks.append(Check("C2.2", "c2", dedup_ok, 1.0 if dedup_ok else 0.0,
                        "телефоны: ≤4, уникальные, каждый с новой строки", dedup_examples))
    # C2.3 адрес без хвостовой точки
    bad = []
    n = 0
    for row in general:
        addr = row.get("Адрес компании производителя", "")
        if addr:
            n += 1
            if addr.rstrip().endswith('.'):
                bad.append(addr[-40:])
    score = (n - len(bad)) / n if n else 1.0
    checks.append(Check("C2.3", "c2", not bad, score, "адрес без хвостовой точки", bad))
    # C2.4 справочники
    bad_types = sorted({row.get("Тип изменения", "") for row in detailed
                        if row.get("Тип изменения") and row["Тип изменения"] not in CHANGE_TYPE_ENUM})
    checks.append(Check("C2.4", "c2", not bad_types, 0.0 if bad_types else 1.0,
                        "«Тип изменения» ∈ справочнику ТЗ", bad_types))
    bad_presence = sorted({row.get("Наличие изменений на сайте компании производителя", "") for row in general}
                          - PRESENCE_ENUM - PRESENCE_KNOWN_EXTRA - {""})
    known_extra = sorted({row.get("Наличие изменений на сайте компании производителя", "") for row in general}
                         & PRESENCE_KNOWN_EXTRA)
    checks.append(Check("C2.4b", "c2", not (bad_presence or known_extra),
                        0.0 if bad_presence else (0.5 if known_extra else 1.0),
                        "«Наличие изменений» ∈ {Есть изменения, Нет изменений}",
                        bad_presence + [f"известное отклонение: {v}" for v in known_extra]))
    # C2.5 пути резолвятся в ZIP
    need = ok = 0
    examples = []
    for row in detailed:
        p = row.get("Путь в архиве к файлу", "")
        if not p:
            continue
        need += 1
        if (files_root / p).exists():
            ok += 1
        elif len(examples) < 5:
            examples.append(p)
    score = ok / need if need else 1.0
    checks.append(Check("C2.5", "c2", score >= 1.0, score,
                        f"«Путь в архиве» резолвится в ZIP: {ok}/{need}", examples))
    # C2.7 ё и «ёлочки» в наименованиях
    bad = []
    for row in detailed:
        name = row.get("Наименование товара", "")
        if name and ('ё' in name or 'Ё' in name or '«' in name or '»' in name):
            bad.append(name[:60])
    checks.append(Check("C2.7", "c2", not bad, 1.0 if not bad else 0.0,
                        "нет «ё»/ёлочек в наименованиях товаров", bad))
    # C2.9 дедуп поставщиков (casefold)
    suppliers = [row.get("Наименование компании поставщика", "") for row in detailed
                 if row.get("Наименование компании поставщика")]
    seen = {}
    dups = []
    for s in suppliers:
        key = s.casefold()
        if key in seen and seen[key] != s:
            dups.append(f"{seen[key]} ~ {s}")
        seen.setdefault(key, s)
    checks.append(Check("C2.9", "c2", not dups, 1.0 if not dups else 0.0,
                        "нет дублей поставщиков с разным регистром", dups))
    return checks


def check_rtf_cards(files_root: Path):
    checks = []
    cards = sorted(files_root.glob("*/Товары/*/*.rtf"))
    caret_hits = []
    url_hits = []
    few_numbers = []
    pict_hits = []
    empty_cards = []
    for rtf in cards:
        text = rtf_to_text(rtf.read_bytes())
        for m in CARET_BAD_RE.finditer(text):
            caret_hits.append(f"{rtf.name}: …{text[max(0, m.start() - 20):m.end() + 10]}…")
        if "URL страницы" in text:
            url_hits.append(rtf.name)
        if len(NUMBER_RE.findall(text)) < 3:
            few_numbers.append(rtf.name)
        if b'\\pict' in rtf.read_bytes():
            pict_hits.append(rtf.name)
        if len(text.strip()) < 200:
            empty_cards.append(rtf.name)
    n = len(cards)
    checks.append(Check("C3.1", "c3", not caret_hits, 1.0 if not caret_hits else 0.0,
                        f"нет литеральной каретки перед нечислом ({len(caret_hits)} вхожд. в {n} картах)",
                        caret_hits))
    checks.append(Check("C3.6", "c3", not url_hits, 1.0 if not url_hits else 0.0,
                        f"нет блока «URL страницы:» ({len(url_hits)}/{n} карт)", url_hits))
    score = (n - len(few_numbers)) / n if n else 1.0
    checks.append(Check("C3.3", "c3", not few_numbers, score,
                        f"в карточке ≥3 числовых значений ({n - len(few_numbers)}/{n})", few_numbers))
    checks.append(Check("C3.4", "c3", not pict_hits, 1.0 if not pict_hits else 0.0,
                        "таблица характеристик не картинкой (нет \\pict)", pict_hits))
    score = (n - len(empty_cards)) / n if n else 1.0
    checks.append(Check("C3.12", "c3", not empty_cards, score,
                        f"карточка не пустая ({n - len(empty_cards)}/{n})", empty_cards))
    return checks


def _company_folders(files_root: Path):
    if not files_root.exists():
        return {}
    res = {}
    for d in sorted(files_root.iterdir()):
        if d.is_dir():
            m = re.match(r'^(\d+)_', d.name)
            res[m.group(1) if m else d.name] = d
    return res


def check_files_and_structure(general, files_root: Path):
    checks = []
    folders = _company_folders(files_root)
    # C4.1 счётчик файлов производителя ↔ факт; C5.3 счётчик товаров ↔ факт
    f_bad, p_bad = [], []
    f_pairs = p_pairs = 0
    for row in general:
        cid = _company_id_norm(row.get("ID компании производителя", ""))
        folder = folders.get(cid)
        if folder is None:
            continue        # компании без изменений в архив не попадают — это норма
        cnt_files = _to_int(row.get("Количество файлов производителя", ""))
        if cnt_files is not None:
            fact = len([f for f in (folder / "Файлы производителя").rglob("*") if f.is_file()]) \
                if (folder / "Файлы производителя").exists() else 0
            f_pairs += 1
            if fact != cnt_files:
                f_bad.append(f"{folder.name}: счётчик {cnt_files}, в архиве {fact}")
        cnt_prod = _to_int(row.get("Количество товаров", ""))
        if cnt_prod is not None:
            fact = len([d for d in (folder / "Товары").iterdir() if d.is_dir()]) \
                if (folder / "Товары").exists() else 0
            p_pairs += 1
            if fact != cnt_prod:
                p_bad.append(f"{folder.name}: счётчик {cnt_prod}, папок товаров {fact}")
    checks.append(Check("C4.1", "c4", not f_bad,
                        (f_pairs - len(f_bad)) / f_pairs if f_pairs else 1.0,
                        f"«Количество файлов» == факт ({f_pairs - len(f_bad)}/{f_pairs})", f_bad))
    checks.append(Check("C5.3", "c5", not p_bad,
                        (p_pairs - len(p_bad)) / p_pairs if p_pairs else 1.0,
                        f"«Количество товаров» == папок товаров ({p_pairs - len(p_bad)}/{p_pairs})", p_bad))
    # C5.1 имя корневой папки
    bad = [d.name for d in folders.values() if not re.match(r'^\d+_\S', d.name)]
    checks.append(Check("C5.1", "c5", not bad, 1.0 if not bad else 0.0,
                        "корневые папки вида <ID>_<имя>", bad))
    # C5.2 ровно 1 RTF в папке товара
    bad = []
    total = 0
    for folder in folders.values():
        t = folder / "Товары"
        if not t.exists():
            continue
        for pd in t.iterdir():
            if pd.is_dir():
                total += 1
                cnt = len(list(pd.glob("*.rtf")))
                if cnt != 1:
                    bad.append(f"{pd.name}: {cnt} RTF")
    checks.append(Check("C5.2", "c5", not bad, (total - len(bad)) / total if total else 1.0,
                        f"в папке товара ровно 1 RTF ({total - len(bad)}/{total})", bad))
    # C5.4 расширения / C5.5 без расширения / C5.6 blacklist
    bad_ext, no_ext, blacklisted = [], [], []
    nfiles = 0
    if files_root.exists():
        for f in files_root.rglob("*"):
            if not f.is_file():
                continue
            nfiles += 1
            name_l = f.name.lower()
            rel = str(f.relative_to(files_root))
            if any(s in name_l for s in NAME_BLACKLIST):
                blacklisted.append(rel)
                continue
            ext = f.suffix.lower()
            if not ext:
                no_ext.append(rel)
            elif ext not in ALLOWED_EXT:
                bad_ext.append(rel)
    checks.append(Check("C5.4", "c5", not bad_ext,
                        (nfiles - len(bad_ext)) / nfiles if nfiles else 1.0,
                        f"только допустимые расширения ({len(bad_ext)} наруш. из {nfiles})", bad_ext))
    checks.append(Check("C5.5", "c5", not no_ext, 1.0 if not no_ext else 0.0,
                        f"нет файлов без расширения ({len(no_ext)})", no_ext))
    checks.append(Check("C5.6", "c5", not blacklisted, 1.0 if not blacklisted else 0.0,
                        f"нет мусорных файлов по чёрному списку ({len(blacklisted)})", blacklisted))
    # C5.7 ожидаемые подпапки
    bad = [d.name for d in folders.values()
           if not (d / "Товары").exists() and not (d / "Файлы производителя").exists()]
    checks.append(Check("C5.7", "c5", not bad, 1.0 if not bad else 0.0,
                        "у компании есть «Товары/» или «Файлы производителя/»", bad))
    return checks


# ==================== Агрегация и вывод ====================

CRITERIA_TITLES = {
    "c1": "К1 Таблицы заполнены полностью",
    "c2": "К2 Значения соответствуют ТЗ",
    "c3": "К3 RTF-карточки",
    "c4": "К4 Полнота файлов",
    "c5": "К5 Структура архива",
}


def score_and_verdict(all_checks):
    by_crit = defaultdict(list)
    for c in all_checks:
        by_crit[c.criterion].append(c)
    criteria = {}
    for crit in ("c1", "c2", "c3", "c4", "c5"):
        chs = by_crit.get(crit, [])
        score = sum(c.score for c in chs) / len(chs) if chs else 1.0
        criteria[crit] = {"title": CRITERIA_TITLES[crit], "score": round(score, 3),
                          "weight": 0.2, "checks": [c.to_dict() for c in chs]}
    total = sum(c["score"] * c["weight"] for c in criteria.values())
    blocking = [c for c in all_checks if c.blocking and not c.passed]
    warned = [c for c in all_checks if not c.blocking and not c.passed]
    verdict = "BLOCK" if blocking else ("WARN" if warned else "PASS")
    return total, criteria, verdict, blocking, warned


def e_code_hits(all_checks):
    """Грубая привязка провалов к Е-кодам редакторского классификатора."""
    mapping = {"C3.1": "E1", "C2.1": "E5", "C2.2": "E5", "C1.1": "E5",
               "C4.1": "E3", "C5.3": "E3", "C3.3": "E7", "C3.12": "E7",
               "C5.6": "E8", "C2.9": "E8", "C2.7": "E6"}
    hits = defaultdict(int)
    for c in all_checks:
        if not c.passed and c.cid in mapping:
            hits[mapping[c.cid]] += max(1, len(c.examples))
    return dict(hits)


def main(argv=None):
    ap = argparse.ArgumentParser(description="QA-гейт архива «Отчёт_об_изменениях.zip»")
    ap.add_argument("archive", help="путь к .zip или распакованной папке архива")
    ap.add_argument("--json", dest="json_out", default="quality_report.json",
                    help="куда писать JSON-отчёт (по умолчанию quality_report.json)")
    args = ap.parse_args(argv)

    path = Path(args.archive)
    if not path.exists():
        print(f"Не найдено: {path}")
        return 2
    root, _tmp = load_archive(path)

    reports_dir = root / REPORTS_DIR_NAME
    files_root = root / FILES_DIR_NAME
    general_path = reports_dir / GENERAL_REPORT_NAME
    detailed_path = reports_dir / DETAILED_REPORT_NAME

    if not general_path.exists():
        print(f"BLOCK: общий отчёт не найден: {general_path}")
        return 2
    _, general = parse_xlsx_rows(general_path)
    detailed = []
    if detailed_path.exists():
        _, detailed = parse_xlsx_rows(detailed_path)

    checks = []
    checks += check_tables_complete(general, detailed)
    checks += check_cell_values(general, detailed, files_root)
    checks += check_rtf_cards(files_root)
    checks += check_files_and_structure(general, files_root)

    total, criteria, verdict, blocking, warned = score_and_verdict(checks)

    report = {
        "archive": str(path),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "verdict": verdict,
        "quality_total": round(total, 3),
        "criteria": criteria,
        "blocking_failures": [c.to_dict() for c in blocking],
        "warnings": [c.to_dict() for c in warned],
        "e_code_hits": e_code_hits(checks),
    }
    Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # человекочитаемый вывод
    print("=" * 72)
    print(f"QA-гейт: {path.name}   ВЕРДИКТ: {verdict}   Q = {total:.1%}")
    print("=" * 72)
    for crit in ("c1", "c2", "c3", "c4", "c5"):
        c = criteria[crit]
        print(f"\n{c['title']}: {c['score']:.1%}")
        for ch in c["checks"]:
            mark = "OK " if ch["passed"] else ("БЛОК" if ch["blocking"] else "ВНИМ")
            print(f"  [{mark}] {ch['check']}: {ch['msg']}")
            for ex in ch["examples"]:
                print(f"         · {ex}")
    print(f"\nJSON: {args.json_out}")
    return {"PASS": 0, "WARN": 1, "BLOCK": 2}[verdict]


if __name__ == "__main__":
    sys.exit(main())
