import os
import re
import json
import shutil
import logging
import asyncio
import tempfile
import subprocess
from typing import Dict, Any, List, Union, Tuple, Optional
from collections import Counter

from docx import Document
from docx.shared import Inches, Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import RGBColor

from product_utils import sanitize_card_filename
from product_utils import _truncate_to_bytes
from product_utils import apply_cp1251_replacements

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    log = logging.getLogger('docx_generator')
    log.warning("PIL/Pillow не установлен. Работа с размерами изображений будет ограничена.")

log = logging.getLogger('docx_generator')

# Литеральные надстрочные цифры/знаки (²³⁻¹ …) → каретная форма степени: «см³»→«см^3».
# Без скобок ^{...} (их вырезает удаление служебных {} ниже). (синхронно с
# text_extractor._literal_superscripts_to_caret; ⁰ исключён — в тексте это чаще искажённый
# значок градуса «300⁰», а не степень нуля.)
_SUP_POW_MAP = {'¹': '1', '²': '2', '³': '3', '⁴': '4', '⁵': '5', '⁶': '6',
                '⁷': '7', '⁸': '8', '⁹': '9', '⁺': '+', '⁻': '-'}
_SUP_POW_RE = re.compile('[' + ''.join(_SUP_POW_MAP) + ']+')

# Юникод-дроби → обычная запись N/M («½»→«1/2»). Windows-1251 их не содержит, а скрипт загрузки
# карточек их не читает. (синхронно с text_extractor._UNICODE_FRACTIONS — дублируется в обоих
# файлах, как и _SUP_POW_MAP: на markdown-стадии дроби уже давятся, здесь — защита в глубину.)
_UNICODE_FRACTIONS = {
    '¼': '1/4', '½': '1/2', '¾': '3/4',
    '⅓': '1/3', '⅔': '2/3',
    '⅕': '1/5', '⅖': '2/5', '⅗': '3/5', '⅘': '4/5',
    '⅙': '1/6', '⅚': '5/6',
    '⅛': '1/8', '⅜': '3/8', '⅝': '5/8', '⅞': '7/8',
    '⅐': '1/7', '⅑': '1/9', '⅒': '1/10', '↉': '0/3',
}
_FRACTION_CHARS = ''.join(_UNICODE_FRACTIONS.keys())

# Содержимое пустой строки карточки — 5 пробелов: у заказчика совсем пустые строки RTF
# схлопываются (требование заказчика 2026-09-29). См. DOCXGenerator._fill_empty_lines.
_EMPTY_LINE = ' ' * 5


def _literal_superscripts_to_caret(text: str) -> str:
    """Литеральные надстрочные символы (³, ⁻¹ …) → КАРЕТНАЯ степень (^3, ^-1).
    Требование заказчика: степень всегда через «^» (надстрочные ³ и слитное 1м3 запрещены;
    правильно 1м^3, г/см^3). Каретка НЕ преобразуется в надстрочный текст при рендере."""
    return _SUP_POW_RE.sub(lambda m: '^' + ''.join(_SUP_POW_MAP[ch] for ch in m.group(0)), text)


def _recode_rtf_to_cp1251(rtf_text: str) -> str:
    """Приводит RTF (вывод LibreOffice) к кодовой странице Windows-1251.

    LibreOffice пишет ANSI-RTF без \\ansicpg: весь нелатинский текст выходит парами
    «\\uNNNN\\'3f» — Unicode-эскейп плюс запасной байт '?' (в cp1252 кириллицы нет).

    Здесь строим «классический» cp1251-RTF: (1) каждую пару \\uN\\'XX заменяем на ОДИН
    реальный байт cp1251 \\'XX (не представимый в cp1251 символ → \\'3f = '?'); (2) объявляем
    \\ansicpg1251. Word/LibreOffice читают \\'XX как cp1251 по \\ansicpg — кириллица рендерится.

    ВАЖНО — \\uN УДАЛЯЕМ, не сохраняем. По RTF-стандарту Unicode-читатель берёт \\uN и
    пропускает запасной \\'XX (\\uc1), но ридер заказчика \\uN не пропускает и читает ОБА →
    каждый символ удваивался («Насос»→«ННаассоосс»). Оставляем только байт \\'XX.
    Одиночные \\'XX (имена шрифтов под \\fcharset128, префиксы длины \\leveltext) — служебные,
    их не трогаем."""

    def _fallback(m: 're.Match') -> str:
        code = int(m.group(1))
        if code < 0:                       # RTF пишет \uN как знаковое 16-битное целое
            code += 65536
        try:
            byte = chr(code).encode('cp1251')
        except (UnicodeEncodeError, ValueError):
            # символ не представим в cp1251 — запасной байт '?'. Нельзя сохранять исходный
            # cp1252-байт: напр. ½=\'bd под \ansicpg1251 прочиталось бы как «Ѕ».
            return "\\'3f"
        return "\\'%02x" % byte[0]

    rtf_text = re.sub(r"\\u(-?\d+)\\'[0-9a-fA-F]{2}", _fallback, rtf_text)

    if re.search(r"\\ansicpg\d+", rtf_text):
        rtf_text = re.sub(r"\\ansicpg\d+", r"\\ansicpg1251", rtf_text, count=1)
    else:
        rtf_text = re.sub(r"\\ansi(?![a-zA-Z])", r"\\ansi\\ansicpg1251", rtf_text, count=1)

    # Шрифты помечаем кириллическим charset 204 (было 0 = ANSI). Без \uN обычный RTF-читатель
    # (Word/LibreOffice) берёт кодовую страницу байтов \'XX из \fcharset шрифта, а при charset 0
    # в НЕ-русской локали это cp1252 → мохибейк «Íàñîñ». charset 204 = cp1251 в любой локали.
    # (Байтовый загрузчик заказчика на \fcharset не смотрит — читает \'XX как cp1251 напрямую.)
    rtf_text = re.sub(r"\\fcharset0(?![0-9])", r"\\fcharset204", rtf_text)

    return rtf_text


class DOCXGenerator:
    """Генератор карточек товаров в формате DOCX с конвертацией в RTF"""

    def __init__(self):
        self.libreoffice_path = "/usr/bin/soffice"
        self.max_image_height_cm = 4.5
        self.min_image_height_cm = 4.5
        self.max_image_height_inches = self.max_image_height_cm / 2.54
        # --- Читаемость широких таблиц характеристик (портретный A4) ---
        # Вертикальная таблица вариантов (модификации в строках, характеристики в шапке)
        # при большом числе характеристик даёт нечитаемо узкие столбцы (напр. насосы —
        # до 20 характеристик => 21 столбец). Если столбцов больше порога, переходим на
        # горизонтальную раскладку и режем модификации на чанки, повторяя столбец «Параметр».
        self.max_spec_table_cols = 6     # порог «широкой» таблицы (столбцов, включая столбец-подпись)
        self.spec_table_chunk_size = 4   # модификаций в одной таблице при разбиении

    def _clean_text(self, value: Any) -> str:
        """
        Очистка текста:
        - удаление служебных символов {}, [], ®, ©
        - сохранение переносов строк
        - схлопывание лишних пробелов внутри строки
        - замена разделителей размеров (* • · ∙ и букв х/Х/x/X) на строчную русскую х между числами
        - знак умножения × → русская х, неравенства ≥ ≤ → >= <= (скрипт загрузки карточек их не читает)
        - точка-произведение между буквами (Ом·м) → « х »; литеральные надстрочные (³) → ^3
        - слитная степень метрических единиц (1м3, см2) → каретная (1м^3, см^2)
        Степень ВСЕГДА в каретной форме «^» (требование заказчика: надстрочные ³ и слитное 1м3
        запрещены; правильно 1м^3, г/см^3). Надстрочный текст в карточке не используется.
        """
        if value is None:
            return ""
        text = str(value)
        text = text.replace('ё', 'е').replace('Ё', 'Е')  # Е11 (D19): ё→е, орфо-нормализация текста карточки
        # Утверждённые заказчиком замены символов вне Windows-1251 (Ø→d, →→->, ÷→-, Ω→Ом, λ→лямбда …).
        text = apply_cp1251_replacements(text)

        # Разделители размеров между числами (* • · ∙ ×, лат. x/X, строчная кир. х) → строчная русская х (U+0445).
        # Заказчик: знак умножения × скрипт загрузки карточек не читает — целевой разделитель размеров = русская х.
        # ВАЖНО: заглавная кир. Х (U+0425) НЕ заменяется — это марки сталей/сплавов (20Х13, 12Х18Н10Т,
        # где Х = хром), а не умножение. Размеры пишутся строчной х/x, марки — заглавной Х.
        text = re.sub(r'(?<=\d)[^\S\n]*[*•·∙хxX×][^\S\n]*(?=\d)', 'х', text)  # опц. пробелы: «200 x 600»→«200х600» (D19)
        # Лишние пробелы у дефиса/тире в КОДАХ и числах (хотя бы одна сторона — цифра):
        # «ГОСТ 948 - 84»→«ГОСТ 948-84», «ТУ 33 - 46»→«ТУ 33-46», «D — 850»→«D—850», «d - 600»→«d-600».
        # Тип тире сохраняем, убираем только пробелы. Прозу не трогаем — тире между словами без
        # цифр остаётся («материал — основа»). [^\S\n] = пробел/таб, но не перенос строки.
        text = re.sub(r'(?<=\d)[^\S\n]*([-—–])[^\S\n]*(?=\w)', r'\1', text)
        text = re.sub(r'(?<=\w)[^\S\n]*([-—–])[^\S\n]*(?=\d)', r'\1', text)
        # Лишний пробел после десятичной запятой между цифрами: «0, 50»→«0,50», «1, 90»→«1,90».
        text = re.sub(r'(?<=\d),[^\S\n]+(?=\d)', ',', text)
        # Литеральные надстрочные символы (³ ² ⁻¹ …) → каретная степень: «г/см³»→«г/см^3».
        text = _literal_superscripts_to_caret(text)
        # Юникод-дроби (½ ¾ ⅓ …) → обычная запись N/M: cp1251 их не содержит, скрипт загрузки не читает.
        # Перед дробью цифра → отделяем пробелом («G1½»→«G1 1/2»), иначе склеится в «G11/2».
        # (синхронно с text_extractor; на markdown-стадии уже давятся, здесь — защита в глубину.)
        text = re.sub(r'(?<=\d)(?=[' + _FRACTION_CHARS + r'])', ' ', text)
        for _frac, _val in _UNICODE_FRACTIONS.items():
            text = text.replace(_frac, _val)
        # Степень метрич. единиц — слитная (1м3, см2) И раздельная («м 3», «м 2») → каретная
        # (1м^3, см^2, м^3). Заказчик: степень всегда через «^», лишний пробел перед цифрой убираем.
        # Узкий скоуп — только строчные метрич. единицы (м/мм/см/дм/км)+опц.пробел+(2/3) вне слов/кодов.
        text = re.sub(r'(?<![А-Яа-яёЁA-Za-z])(мм|см|дм|км|м)[ ]?([23])(?![А-Яа-яёЁA-Za-z0-9])', r'\1^\2', text)
        # Точка-произведение между буквами (Ом·м, Н·м) → « х » с пробелами; между цифрами —
        # плотная х (см. выше). (синхронно с text_extractor._postprocess_markdown).
        # Значок градуса °(˚º⁰) считаем допустимым соседом точки: «Вт/(м·°C)» — здесь · это
        # произведение, даём «м х °C», а сам ° ниже штатно сводится к « C» (см. степень-цикл).
        # Иначе · застревал бы (° не буква), оставляя «м· C». (после зачистки ° порядок сохранён)
        text = re.sub(r'(?<=[A-Za-zА-Яа-яёЁ°˚º⁰̊])[·∙](?=[A-Za-zА-Яа-яёЁ°˚º⁰̊])', ' х ', text)

        lines = text.splitlines()
        cleaned_lines = []

        for line in lines:
            original_empty = (line.strip() == "")
            # Значок градуса убираем в любом виде (вкл. U+030A — комбинир. кольцо сверху, как в
            # «1150 ̊ С»), температуру сводим к « C» (пробел + латинская C), синхронно с
            # text_extractor._postprocess_markdown (целевая система не читает такие значки).
            line = line.replace('℃', '°C').replace('℉', 'F')
            line = re.sub(r'(?<=\d)\^[oоOО]', '°', line)  # extract-артефакт «80^oС»: градус как каретка → ° (далее сводится к ' C') (D19)
            # Градус, набранный НУЛЁМ в суперскрипте («уставок, ^0С» с сайтов с <sup>0</sup>С):
            # каретка с нулём перед С/C — значок градуса, не степень (далее сводится к ' C').
            line = re.sub(r'\^0(?=[ \t]*[СCсc](?![А-Яа-яёЁA-Za-z]))', '°', line)
            line = re.sub('[ \t]*[°˚º⁰\u030a][ \t]*[СCсc](?![А-Яа-яёЁA-Za-z])', ' C', line)
            line = re.sub(r"[{}\[\]º˚®©°⁰#*∗™«»" "\u030a" "]", "", line)
            # Кавычки убираем, НО дюймовую метку после цифры («2"», «3/4"», ← из «2″») щадим.
            line = line.replace("'", "")
            line = re.sub(r'(?<!\d)"', '', line)
            # Заказчик: скрипт загрузки карточек (на стороне заказчика) не читает × ≥ ≤.
            # Любой оставшийся × (напр. габариты «390×90×188» из LLM, не попавшие под правило
            # разделителей выше) → русская х; неравенства → ASCII >= <=.
            # (синхронно с text_extractor._postprocess_markdown).
            line = line.replace('×', 'х').replace('≥', '>=').replace('≤', '<=')
            line = re.sub(r"[ \t]+", " ", line)
            if not original_empty:
                line = line.strip()
            cleaned_lines.append(line)

        return "\n".join(cleaned_lines)

    def _strip_dup_suffix(self, name: Any) -> str:
        """
        Удаляет суффикс-дубликатор вида ' (N)', добавляемый при дедупликации одинаковых
        наименований/марок/серий (ключи словаря должны быть уникальными).
        Примеры: 'Серия 700 (2)' → 'Серия 700', 'Воздуховоды дымоудаления (4)' → 'Воздуховоды дымоудаления'.
        Затрагивает только завершающие ' (цифры)'; внутренние скобки (например '(1,0 - Zn)') не трогает.
        """
        if name is None:
            return ""
        return re.sub(r'\s*\(\d+\)\s*$', '', str(name))

    def _add_text_runs(self, paragraph, text: Any, bold: bool = False,
                       name: str = 'Calibri', size=None, clean: bool = True):
        """Добавляет текст в параграф. Степень остаётся в КАРЕТОЧНОЙ форме ^N (по требованию
        заказчика: надстрочные ³ и слитное 1м3 запрещены; правильно 1м^3, г/см^3 — каретка
        НЕ преобразуется в надстрочный текст). Многострочный текст (\\n) сохраняется
        (add_run переводит \\n в разрыв строки). clean=True прогоняет _clean_text."""
        if size is None:
            size = Pt(12)
        text = "" if text is None else str(text)
        if clean:
            text = self._clean_text(text)
        run = paragraph.add_run(text)
        run.font.name = name
        run.font.size = size
        run.bold = bold
        return paragraph

    def _set_table_full_width(self, table) -> None:
        """Растягивает таблицу на всю ширину страницы (tblW=100%, autofit off) — ТЗ I.4.1.5 (5.3)."""
        try:
            table.autofit = False
            table.allow_autofit = False
            tblPr = table._tbl.tblPr
            for el in tblPr.findall(qn('w:tblW')):
                tblPr.remove(el)
            tblW = OxmlElement('w:tblW')
            tblW.set(qn('w:type'), 'pct')
            tblW.set(qn('w:w'), '5000')  # 5000 = 100% (в "fiftieths of a percent")
            tblPr.append(tblW)
        except Exception as e:
            log.warning(f"Не удалось задать ширину таблицы на 100%: {e}")

    async def generate_product_card(self, product_data: Dict[str, Any],
                                company_data: Dict[str, Any],
                                product_folder: str) -> str:
        """Генерация карточки товара в формате DOCX с последующей конвертацией в RTF"""
        try:
            if isinstance(product_folder, tuple):
                log.warning(f"product_folder передан как кортеж, берем первый элемент {product_folder}")
                product_folder = product_folder[0]
            log.info(f"Начинаем генерацию карточки товара для {product_data.get('name', 'Unknown')}")

            ai_data = product_data.get('json_data') or product_data.get('ai_raw_text')

            if not isinstance(ai_data, dict):
                log.error(f"AI данные должны быть словарем, получен {type(ai_data)}. Генерация карточки пропущена.")
                return ""

            if not ai_data:
                log.error("Отсутствуют AI данные для генерации карточки")
                return ""

            products_data = ai_data.get('product', {})
            if not products_data:
                log.error("В AI данных отсутствует информация о продукте")
                return ""

            # Проверка на пустоту
            raw_product_name = products_data.get('product_name', product_data.get('name', ''))
            cleaned_name = self._clean_text(raw_product_name).strip()
            is_name_empty = not cleaned_name or cleaned_name == "Неизвестный товар"

            specifications = products_data.get('specifications', {})
            is_specs_empty = not self._is_specifications_meaningful(specifications)

            if is_name_empty and is_specs_empty:
                log.warning(f"Пропуск генерации карточки: пустое имя товара и пустые характеристики для {product_data.get('product_id', 'unknown')}")
                return ""

            doc = Document()
            self._setup_document_styles(doc)

            # Наименование товара
            product_name = products_data.get('product_name', product_data.get('name', 'Неизвестный товар'))
            display_product_name = re.sub(r'\s*\([^)]*\)\s*$', '', product_name).strip()
            display_product_name = display_product_name.replace('/', '-').replace('\\', '-')
            if not display_product_name:
                display_product_name = product_name
            # Пустая строка в начале карточки (перед наименованием) — как в эталоне
            doc.add_paragraph()
            self._add_product_title(doc, display_product_name)

            # ТУ (без пустой строки между названием и ТУ)
            tu = products_data.get('tu')
            tu_added = False
            if tu and tu != "Отсутствует":
                self._add_tu(doc, tu)
                tu_added = True

            # Одна пустая строка после блока "название + ТУ"
            doc.add_paragraph()

            # Описание
            description = products_data.get('description', '')
            if description and description != "Отсутствует":
                self._add_product_description(doc, description)

            # Технические характеристики
            if specifications and self._is_valid_specifications(specifications):
                self._add_specifications(doc, specifications)

            # Варианты
            variants = products_data.get('variants', [])
            if variants:
                self._add_variants(doc, variants)

            # Дополнительные блоки (complectation, applications, ...)
            fields_order = [
                ("Комплектация", "complectation"),
                ("Область применения", "applications"),
                ("Преимущества", "advantages"),
                ("Совместимость", "compatibility"),
                ("Инструкции по применению", "instructions_manuals"),
                ("Условия эксплуатации", "exploitation"),
                ("Условия хранения", "storage"),
                ("Меры безопасности", "security_measures"),
                ("Габариты", "dimensions"),
                ("Вес", "weight"),
            ]

            for display_name, key in fields_order:
                value = products_data.get(key, '')
                if value and value != "Отсутствует":
                    self._add_generic_field(doc, display_name, value)

            # Добавляем источник
            source_url = product_data.get('source_url', '')
            if source_url:
                self._add_source_url(doc, source_url)

            # Пустые строки — по 5 пробелов (у заказчика пустые строки схлопываются)
            self._fill_empty_lines(doc)

            product_id = product_data.get('product_id', 'unknown')

            docx_path = self._save_document(doc, product_folder, product_name, product_id)

            rtf_path = await self._convert_to_rtf(docx_path)

            if rtf_path and os.path.exists(rtf_path):
                try:
                    if os.path.exists(docx_path):
                        os.remove(docx_path)
                        log.info(f"Оригинальный DOCX файл удален {docx_path}")
                    else:
                        log.warning(f"DOCX файл не найден для удаления {docx_path}")
                except Exception as e:
                    log.error(f"Ошибка при удалении DOCX файла {docx_path}: {e}")

            if rtf_path:
                log.info(f"Карточка товара успешно сгенерирована и сконвертирована в RTF: {rtf_path}")
                return rtf_path
            else:
                log.info(f"Карточка товара сгенерирована в формате DOCX (конвертация в RTF не выполнена): {docx_path}")
                return docx_path

        except Exception as e:
            log.error(f"Ошибка генерации карточки товара: {e}")
            return ""

    def _is_valid_specifications(self, specifications: Dict[str, Any]) -> bool:
        """Проверка, что спецификации валидны для отображения (есть хотя бы одна непустая пара)"""
        return self._is_specifications_meaningful(specifications)
    
    def _is_specifications_meaningful(self, specs: Any) -> bool:
        """
        Проверяет, содержат ли спецификации хотя бы одну осмысленную пару ключ-значение.
        Возвращает False для пустого словаря, словаря вида {"": ""}, а также если все значения пусты.
        """
        if not isinstance(specs, dict):
            return False
        if not specs:
            return False
        
        for key, value in specs.items():
            # Очищаем ключ и значение
            clean_key = self._clean_text(str(key)).strip()
            clean_value = self._clean_text(str(value)).strip()
            if clean_key and clean_value:
                return True
        return False

    def _is_valid_models(self, models: Union[Dict[str, Any], List]) -> bool:
        """Проверка, что модели валидны для отображения"""
        if not models:
            return False
        if isinstance(models, (dict, list)) and not models:
            return False
        return True

    def _extract_product_info(self, ai_data: Any) -> Dict[str, Any]:
        """Извлечение информации о продукте из AI данных с поддержкой разных форматов"""
        if not ai_data:
            return {}
        try:
            if isinstance(ai_data, str):
                try:
                    ai_data = json.loads(ai_data)
                except json.JSONDecodeError:
                    log.error(f"AI данные не являются валидным JSON: {ai_data[:100]}...")
                    return {}
            if isinstance(ai_data, dict):
                product_info = ai_data.get('product') or ai_data.get('products') or ai_data
                if isinstance(product_info, list) and product_info:
                    product_info = product_info[0]
                if isinstance(product_info, dict):
                    return product_info
                else:
                    log.error(f"Информация о продукте не является словарем: {type(product_info)}")
                    return {}
            log.error(f"Неизвестный формат AI данных: {type(ai_data)}")
            return {}
        except Exception as e:
            log.error(f"Ошибка извлечения информации о продукте: {e}")
            return {}
    # Новые вспомогательные методы
    def _is_empty_dict_values(self, d: dict) -> bool:
        """
        Проверяет, является ли словарь "пустым" – все его значения (включая вложенные) равны пустой строке, None или пустому словарю.
        """
        if not isinstance(d, dict):
            return False
        for v in d.values():
            if isinstance(v, dict):
                if not self._is_empty_dict_values(v):
                    return False
            elif v is not None and str(v).strip() != "":
                return False
        return True

    def _classify_spec_dicts(self, dict_specs: Dict[str, Any]) -> Tuple[List[Tuple[str, Dict]], List[Tuple[str, Dict]], List[Tuple[str, Dict]]]:
        """
        Разделяет вложенные словари на три категории:
        - variants: несколько словарей с одинаковым набором непустых ключей (для многостолбцовых таблиц)
        - single_nonempty: одиночные словари, содержащие хотя бы одно непустое значение (будут развёрнуты в плоскую таблицу)
        - text_blocks: словари, все значения которых пусты (будут выведены как текстовые блоки)
        """
        variants = []
        single_nonempty = []
        text_blocks = []
        
        # Сначала сгруппируем по ключам для поиска вариантов
        key_groups = {}
        for outer_key, inner_dict in dict_specs.items():
            if not isinstance(inner_dict, dict):
                continue
            # Если внутренний словарь пустой или все значения пусты – это текстовый блок
            if self._is_empty_dict_values(inner_dict):
                text_blocks.append((outer_key, inner_dict))
                continue
            
            # Собираем сигнатуру (набор ключей) для возможных вариантов
            key_set = frozenset(inner_dict.keys())
            if key_set:
                key_groups.setdefault(key_set, []).append((outer_key, inner_dict))
        
        # Обрабатываем группы: если в группе больше одного элемента – это варианты
        for key_set, items in key_groups.items():
            if len(items) > 1:
                variants.extend(items)
            else:
                single_nonempty.extend(items)
        
        return variants, single_nonempty, text_blocks

    def _is_flat_dict(self, d: Dict[str, Any]) -> bool:
        """Проверяет, что все значения в словаре не являются словарями (т.е. словарь плоский)."""
        if not isinstance(d, dict):
            return False
        for v in d.values():
            if isinstance(v, dict):
                return False
        return True

    def _flatten_nested_dict(self, nested_dict: Dict[str, Any], parent_key: str = "") -> List[Tuple[str, str]]:
        """
        Рекурсивно разворачивает вложенный словарь в список пар (ключ, значение) для двухстолбцовой таблицы.
        Если значение – словарь, то рекурсивно обходит его, формируя составной ключ через разделитель " → ".
        Возвращает список кортежей (key, value), где value – уже строка (может быть многострочной).
        """
        items = []
        for k, v in nested_dict.items():
            full_key = f"{parent_key} → {k}" if parent_key else k
            if isinstance(v, dict):
                # Если вложенный словарь не пустой и не все значения пустые, разворачиваем дальше
                if self._is_empty_dict_values(v):
                    # Пустой словарь – выводим только ключ? В спецификациях таких не встречается.
                    items.append((full_key, ""))
                else:
                    items.extend(self._flatten_nested_dict(v, full_key))
            else:
                # Преобразуем значение в многострочный текст, если нужно
                clean_value = self._clean_text(str(v))
                items.append((full_key, clean_value))
        return items

    def _add_text_block_from_dict(self, doc: Document, outer_key: str, inner_dict: Dict[str, Any]):
        """
        Выводит словарь с пустыми значениями как текстовый блок.
        - Если один ключ: "ВнешнийКлюч: ВнутреннийКлюч;"
        - Если несколько ключей: заголовок (обычный шрифт), затем каждый ключ с новой строки с отступом.
        - Вертикальные отступы между параграфами отсутствуют.
        """
        if not inner_dict:
            return

        # Утилита для создания параграфа с нулевыми отступами
        def add_paragraph_without_spacing():
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(0)
            return p

        if len(inner_dict) == 1:
            inner_key = next(iter(inner_dict.keys()))
            p = add_paragraph_without_spacing()
            self._add_text_runs(p, f"{self._strip_dup_suffix(outer_key)}: {inner_key};")
            return

        # Много ключей
        p = add_paragraph_without_spacing()
        self._add_text_runs(p, self._strip_dup_suffix(outer_key))

        for inner_key in inner_dict.keys():
            p = add_paragraph_without_spacing()
            p.paragraph_format.left_indent = Inches(0.25)
            self._add_text_runs(p, f"{inner_key};")

    def _add_variants_list(self, doc: Document, variant_names: List[str]):
        """Выводит маркированный список названий вариантов (без атрибутов)."""
        for name in variant_names:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            p.paragraph_format.left_indent = Inches(0.25)
            run = p.add_run(f"- {self._clean_text(self._strip_dup_suffix(name))}")
            run.font.name = 'Calibri'
            run.font.size = Pt(12)
                
    # -------------------------------------------------------------------------
    # НАСТРОЙКА СТИЛЕЙ
    # -------------------------------------------------------------------------
    def _setup_document_styles(self, doc: Document):
        """Настройка стилей документа"""
        style = doc.styles['Normal']
        style.font.name = 'Calibri'
        style.font.size = Pt(12)
        style.font.color.rgb = None

        try:
            heading_style = doc.styles.add_style('ProductHeading', 1)
            heading_style.font.name = 'Calibri'
            heading_style.font.size = Pt(12)
            heading_style.font.bold = True
        except Exception:
            pass

    def _add_section_heading(self, doc: Document, heading_text: str):
        """
        Добавление заголовка раздела (выравнивание по центру, жирный, 12 pt).
        После заголовка добавляется одна пустая строка — разделитель между заголовком блока
        и его содержимым (как в эталоне).
        """
        heading_paragraph = doc.add_paragraph()
        heading_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER

        heading_run = heading_paragraph.add_run(self._clean_text(heading_text))
        heading_run.font.name = 'Calibri'
        heading_run.font.size = Pt(12)
        heading_run.bold = True

        # Пустая строка между заголовком и содержимым блока (как в эталоне)
        doc.add_paragraph()

    def _add_subheading(self, doc: Document, heading_text: str):
        """Добавление подзаголовка (жирный, по центру, 12 pt)"""
        heading_paragraph = doc.add_paragraph()
        heading_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        heading_run = heading_paragraph.add_run(self._clean_text(heading_text))
        heading_run.font.name = 'Calibri'
        heading_run.font.size = Pt(12)
        heading_run.bold = True
        doc.add_paragraph()

    def _add_product_title(self, doc: Document, product_name: str):
        """Добавление заголовка товара"""
        title_paragraph = doc.add_paragraph()
        title_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER

        # «ё»→«е» в названии. Каретку ОСТАВЛЯЕМ как степень (^N перед цифрой); ошибочную
        # «галочку перед тире / в произвольном месте» (Е1: ^ НЕ перед цифрой) — убираем.
        title_text = re.sub(r'\^(?!\d)', '',
                            self._clean_text(product_name).replace('ё', 'е').replace('Ё', 'Е'))
        title_run = title_paragraph.add_run(title_text)
        title_run.font.name = 'Calibri'
        title_run.font.size = Pt(12)
        title_run.bold = True

    def _add_tu(self, doc: Document, tu: str):
        """Добавление ТУ под названием товара (выравнивание влево, жирный). Пустая строка после ТУ НЕ добавляется."""
        if not tu or tu == "Отсутствует":
            return
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        run = p.add_run(self._clean_text(tu))
        run.font.name = 'Calibri'
        run.font.size = Pt(12)
        run.bold = True

    async def _add_product_image(self, doc: Document, product_folder: str):
        """Добавление изображения товара с ограничением высоты 5 см (без изменений)"""
        try:
            images_dir = os.path.join(product_folder, "Images")
            if not os.path.exists(images_dir):
                log.warning(f"Папка с изображениями не найдена: {images_dir}")
                return

            image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']
            image_files = []

            for ext in image_extensions:
                for file in os.listdir(images_dir):
                    if file.lower().endswith(ext):
                        image_files.append(os.path.join(images_dir, file))

            if not image_files:
                log.warning("Не найдено подходящих изображений для добавления в карточку")
                return

            selected_image_path = None

            if HAS_PIL:
                for image_path in image_files:
                    try:
                        with Image.open(image_path) as img:
                            width_px, height_px = img.size
                            dpi = img.info.get('dpi', (72, 72))[0]
                            if dpi == 0:
                                dpi = 72
                            height_inches = height_px / dpi
                            height_cm = height_inches * 2.54
                            log.info(f"Изображение {os.path.basename(image_path)}: {height_px}px, DPI: {dpi}, Высота: {height_cm:.2f} см")
                            if height_cm >= self.min_image_height_cm:
                                selected_image_path = image_path
                                log.info(f"Выбрано изображение {os.path.basename(image_path)} (высота {height_cm:.2f} см)")
                                break
                            else:
                                log.info(f"Изображение {os.path.basename(image_path)} пропущено (высота {height_cm:.2f} см < {self.min_image_height_cm} см)")
                    except Exception as e:
                        log.warning(f"Не удалось проанализировать изображение {image_path}: {e}")
                        continue
            else:
                selected_image_path = image_files[0]
                log.warning("PIL не установлен, используется первое доступное изображение без проверки размера")

            if selected_image_path and os.path.exists(selected_image_path):
                image_paragraph = doc.add_paragraph()
                image_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER

                try:
                    run = image_paragraph.add_run()
                    run.add_picture(selected_image_path, height=Inches(self.max_image_height_inches))
                    log.info(f"Добавлено изображение с ограничением высоты {self.max_image_height_cm} см: {selected_image_path}")
                except Exception as e:
                    log.warning(f"Не удалось добавить изображение {selected_image_path}: {e}")
                    try:
                        run = image_paragraph.add_run()
                        run.add_picture(selected_image_path)
                        log.info(f"Изображение добавлено без ограничения высоты: {selected_image_path}")
                    except Exception as e2:
                        log.error(f"Не удалось добавить изображение даже без ограничения: {e2}")

                doc.add_paragraph()
            else:
                log.warning(f"Не найдено изображений высотой ≥{self.min_image_height_cm} см. Изображение не добавлено.")

        except Exception as e:
            log.error(f"Ошибка при добавлении изображения: {e}")

    def _add_product_description(self, doc: Document, description: str):
        """Добавление описания товара одним абзацем-прозой. В конце блока — одна пустая строка.
        В описании ЗАПРЕЩЕНЫ список и знак «-» в начале строк (требование заказчика): снимаем
        маркеры списка и схлопываем многострочное описание в один абзац, как в эталоне."""
        if not description or description == "Отсутствует":
            return

        # Снимаем маркеры списка в начале строк и собираем прозу в один абзац
        parts = []
        for line in str(description).split('\n'):
            line = re.sub(r'^\s*[-–—•*·]+\s+', '', line).strip()
            if line:
                parts.append(line)
        text = ' '.join(parts)
        if not text:
            return

        desc_paragraph = doc.add_paragraph()
        desc_paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
        self._add_text_runs(desc_paragraph, text)

        # Одна пустая строка после блока
        doc.add_paragraph()

    def _add_product_field(self, doc: Document, field_name: str, field_value: str):
        """Добавление поля товара (артикул, цена и т.д.)"""
        if not field_value or field_value == "Отсутствует":
            return

        field_paragraph = doc.add_paragraph()
        field_paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT

        name_run = field_paragraph.add_run(self._clean_text(f"{field_name}: "))
        name_run.font.name = 'Calibri'
        name_run.font.size = Pt(12)
        name_run.bold = True

        value_run = field_paragraph.add_run(self._clean_text(field_value))
        value_run.font.name = 'Calibri'
        value_run.font.size = Pt(12)

        # Одна пустая строка после блока
        doc.add_paragraph()

    def _add_generic_field(self, doc: Document, field_name: str, field_value: str):
        """
        Добавление текстового блока с сохранением форматирования + поддержка таблиц.
        В конце блока — одна пустая строка.
        """
        if not field_value or field_value == "Отсутствует":
            return

        # Габариты/Вес без единой цифры — это заглушка-фраза («не более», «уточняйте»), не выводим (5.4).
        if field_name in ("Габариты", "Вес") and not re.search(r'\d', str(field_value)):
            log.info(f"Поле '{field_name}' без числовых значений — пропускаем")
            return

        try:
            # Пробуем преобразовать в число (строки вида "0", "0.0", "0 кг" -> отбросим).
            # Десятичная запятая: «0,5 кг» — это 0.5 (не 0), такое поле НЕ отбрасываем.
            numeric_part = re.search(r'\d+(?:[.,]\d+)?', str(field_value))
            if numeric_part:
                num = float(numeric_part.group().replace(',', '.'))
                if num == 0.0:
                    log.info(f"Поле '{field_name}' имеет значение 0 – пропускаем")
                    return
        except (ValueError, TypeError):
            pass

        # Заголовок блока
        self._add_section_heading(doc, field_name)

        # Какие блоки должны быть выровнены строго слева
        left_aligned_fields = [
            "Комплектация", "Область применения", "Преимущества",
            "Совместимость", "Инструкции по применению", "Условия эксплуатации", 
            "Условия хранения", "Меры безопасности", "Габариты", "Вес"
        ]

        # --- Вспомогательные функции ---------------------------------------------

        def add_text_paragraph(text_block: str):
            """Добавляет текстовый параграф с прежним форматированием"""
            if not text_block.strip():
                return

            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT

            # Отступ первой строки
            if field_name in left_aligned_fields:
                p.paragraph_format.first_line_indent = Inches(0)
            else:
                p.paragraph_format.first_line_indent = Inches(0.25)

            self._add_text_runs(p, text_block)

        def is_table_line(line: str) -> bool:
            """Определяет, является ли строка строкой таблицы Markdown"""
            stripped = line.strip()
            return stripped.startswith("|") and stripped.count("|") >= 2

        def parse_markdown_table(lines: List[str]) -> List[List[str]]:
            """Разбор Markdown-таблицы в список строк"""
            rows = []
            for raw in lines:
                stripped = raw.strip()
                if not is_table_line(stripped):
                    continue

                cells = [c.strip() for c in stripped.strip("|").split("|")]

                # Пропускаем строку-разделитель |---|---|
                if all(re.fullmatch(r"-+", c) for c in cells):
                    continue

                rows.append(cells)
            return rows

        def add_table_to_doc(rows: List[List[str]]):
            """Создаёт таблицу в DOCX с центрированием данных (без завершающей пустой строки)"""
            if not rows:
                return

            max_cols = max(len(r) for r in rows)
            table = doc.add_table(rows=0, cols=max_cols)
            table.style = 'Table Grid'
            self._set_table_full_width(table)

            for r_idx, row in enumerate(rows):
                row_cells = table.add_row().cells
                row_key = row[0] if row else ""  # D49: первая ячейка строки — ключ-параметр
                for c_idx in range(max_cols):
                    text = row[c_idx] if c_idx < len(row) else ""

                    cell = row_cells[c_idx]
                    cell.text = ""
                    p = cell.paragraphs[0]
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER

                    cell_text = self._separate_multivalues(text)  # перечни «0,5 0,7» -> «0,5; 0,7»
                    if c_idx > 0 and r_idx > 0:
                        # D49: ключ-перечень в первой ячейке («…: — …; — …») → «3 1» -> «3; 1»
                        cell_text = self._separate_subitem_values(row_key, cell_text)
                    self._add_text_runs(p, cell_text, bold=(r_idx == 0))  # первая строка — заголовок

        # --- Основная логика ------------------------------------------------------

        lines = str(field_value).splitlines()

        current_text_block = []
        current_table_block = []
        in_table = False

        def flush_text():
            nonlocal current_text_block
            if current_text_block:
                add_text_paragraph("\n".join(current_text_block))
                current_text_block = []

        def flush_table():
            nonlocal current_table_block
            if current_table_block:
                rows = parse_markdown_table(current_table_block)
                add_table_to_doc(rows)
                current_table_block = []

        for line in lines:
            if is_table_line(line):
                if not in_table:
                    flush_text()
                    in_table = True
                current_table_block.append(line)
            else:
                if in_table:
                    flush_table()
                    in_table = False
                current_text_block.append(line)

        # Финальный сброс
        if in_table:
            flush_table()
        else:
            flush_text()

        # Одна пустая строка после блока
        doc.add_paragraph()

    # МНОГОСТОЛБЧАТЫЕ ТАБЛИЦЫ
    def _is_multi_column_candidate(self, spec: Dict[str, Any]) -> bool:
        """
        Определяет, является ли spec подходящим для многостолбчатой таблицы.
        Формат:
        {
          "Марка 1": { "Документация": "...", "Вес, тн": "...", ... },
          "Марка 2": { ... },
          ...
        }
        Все значения должны быть словарями с одинаковым набором ключей.
        Требуем минимум 2 элемента, чтобы не путать с обертками вроде {"Плиты": {...}}.
        """
        if not isinstance(spec, dict) or not spec:
            return False

        if len(spec) < 2:
            return False

        if not all(isinstance(v, dict) for v in spec.values()):
            return False

        key_sets = [set(v.keys()) for v in spec.values()]
        if not key_sets:
            return False
        first = key_sets[0]
        return all(s == first for s in key_sets)

    def _set_cell_style(self, cell, text: str, bold: bool = False):
        """Установка стиля ячейки таблицы: многострочный текст (\\n) + надстрочная степень (^N).
        Текст приходит уже очищенным выше по стеку, поэтому clean=False."""
        cell.text = ""
        paragraph = cell.paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        self._add_text_runs(paragraph, text, bold=bold, clean=False)

    def _group_specs_by_keys(self, specs: Dict[str, Dict[str, Any]]) -> List[Dict[str, Dict[str, Any]]]:
        """
        Группирует словари по набору ключей.
        На выходе список групп, каждая группа — dict {name -> attrs} с одинаковым набором характеристик.
        """
        groups: Dict[frozenset, Dict[str, Dict[str, Any]]] = {}

        for name, attrs in specs.items():
            if not isinstance(attrs, dict):
                continue
            key_set = frozenset(attrs.keys())
            if not key_set:
                continue
            if key_set not in groups:
                groups[key_set] = {}
            groups[key_set][name] = attrs

        return list(groups.values())
    
    def _separate_multivalues(self, text: str) -> str:
        """Перечень числовых значений в одной ячейке таблицы -> разделитель "; ".
        «0,55 0,7 1,0» -> «0,55; 0,7; 1,0». Скоуп узкий и безопасный: ТОЛЬКО запятично-десятичные
        токены (\\d+,\\d+) через пробел — это однозначно несколько значений. Целые через пробел НЕ
        трогаем намеренно: это время/дата («07 11») и разделитель тысяч («10 000» -> НЕ «10; 000»).
        Применяется во всех табличных путях (спецификации и markdown-таблицы описаний)."""
        return re.sub(r'(\d+,\d+)\s+(?=\d+,\d+)', r'\1; ', str(text))

    def _separate_subitem_values(self, key: Any, value: str) -> str:
        """Ячейка с НЕСКОЛЬКИМИ значениями под ключ-перечень (ВЭМЗ, требование заказчика 2026-07-07):
        «Время протекания…, с: — для главных ножей; — для заземляющих ножей» + значение «3 1» → «3; 1».
        Срабатывает ТОЛЬКО если сам ключ перечисляет ≥2 подпункта (после ':' есть ';' либо ≥2 маркера
        «—/–»), поэтому целые вне таких ключей («10 000» — тысячи, «07 11» — даты) не затрагиваются
        (в отличие от _separate_multivalues, у которого скоуп — только десятичные с запятой)."""
        k = str(key or "")
        if ':' not in k:
            return value
        tail = k.split(':', 1)[1]
        if tail.count(';') < 1 and len(re.findall(r'[—–]', tail)) < 2:
            return value
        return re.sub(r'(?<=\d)[ \t]+(?=\d)', '; ', str(value))

    def _split_spec_value(self, value: str) -> List[str]:
        """
        Преобразует строку для отображения в ячейке таблицы:
        - добавляет пробелы между числом и единицей измерения (10см → 10 см);
        - разделяет слитные группы вида "Р2Р3Р5" → "Р2; Р3; Р5" (через точку с запятой и пробел),
        НО не трогает строки, похожие на размеры (содержат 'х');
        - не вставляет лишних переносов строк (сохраняет только исходные);
        - обрабатывает степени (² → ^2).
        Возвращает список строк (по исходным переносам строк).
        """
        if not value:
            return [""]

        text = str(value)

        # ---- 1. Защита степеней (временные маркеры) ----
        power_markers = {}
        def replace_power(match):
            marker = f"__POWER_{len(power_markers)}__"
            power_markers[marker] = match.group(0)
            return marker
        text = re.sub(r'\^[0-9-]+', replace_power, text)

        # ---- 2. Добавление пробела между числом и единицей измерения ----
        units = [
            'мм', 'см', 'м', 'км', 'дм', 'мкм',
            'кг', 'г', 'т', 'л', 'мл',
            'с', 'сек', 'мин', 'ч', 'сут',
            'МПа', 'бар', 'Вт', 'вт',
            'С', '°С', 'м2', 'м3', 'м²', 'м³',
            'слой', 'тн', 'г/м2', 'г/м²'
        ]
        for unit in sorted(units, key=len, reverse=True):
            pattern = rf'(\d+(?:[.,]\d+)?)({re.escape(unit)})(?![а-яА-Яa-zA-Z0-9])'
            text = re.sub(pattern, r'\1 \2', text, flags=re.IGNORECASE)

        # ---- 2.5. Несколько числовых значений в одной ячейке -> разделитель "; " ----
        text = self._separate_multivalues(text)

        # ---- 3. Разделение слитных буквенно-цифровых групп через "; " ----
        # НЕ выполняем, если строка похожа на размер (содержит разделитель размеров: х/Х/x/X/×)
        if not re.search(r'[хХxX×]', text):
            prev = None
            while prev != text:
                prev = text
                text = re.sub(r'([A-Za-zА-Яа-я]\d+)([A-Za-zА-Яа-я]\d+)', r'\1; \2', text)

        # ---- 4. Восстановление степеней ----
        for marker, original in power_markers.items():
            text = text.replace(marker, original)

        # ---- 5. Разбиение по исходным переносам строк ----
        lines = text.splitlines()
        lines = [line.strip() for line in lines if line.strip()]
        return lines if lines else [""]


    def _format_table_value(self, value: Any) -> str:
        """
        Форматирование значения для ячейки многостолбчатой таблицы.
        В конце применяется _clean_text для нормализации степеней.
        """
        if value is None:
            return ""
        if isinstance(value, list):
            return "\n".join(self._clean_text(str(v)) for v in value if str(v).strip() != "")
        if isinstance(value, dict):
            parts = []
            for k, v in value.items():
                if v is None or str(v).strip() == "":
                    continue
                if isinstance(v, list):
                    sub = "\n".join(self._clean_text(str(x)) for x in v if str(x).strip() != "")
                    parts.append(f"{k}: {sub}")
                else:
                    # D49: внутренний ключ-перечень («…: — для главных ножей; — для заземляющих»)
                    # → значения «3 1» разделяются «; » и во вложенной форме «ключ: значение»
                    parts.append(f"{k}: {self._separate_subitem_values(k, self._clean_text(str(v)))}")
            return "\n".join(parts)
        return self._clean_text(str(value))

    def _build_multi_column_table(self, doc: Document, title: str, specs: Dict[str, Dict[str, Any]]) -> None:
        """
        Многостолбчатые таблицы для 2+ вложенных словарей.
        Новая логика:
        - Сначала группируем словари по набору ключей.
        - Для каждой группы:
            * если 1 элемент  -> двухколоночная таблица (Марка/Вариант + атрибуты)
            * если 2–5 элементов -> горизонтальная таблица (характеристики слева, марки в шапке)
            * если 6+ элементов  -> вертикальная таблица (марки слева, характеристики в шапке)
        """
        if not specs:
            return

        groups = self._group_specs_by_keys(specs)

        # Предопределённый порядок характеристик, если он у тебя уже есть
        preferred_order = [
            "Документация",
            "Вес, тн",
            "Вес, т",
            "Объем, м3",
            "Длина, l",
            "Ширина, b",
            "Высота, h",
            # сюда можно добавить твой текущий preferred_order из старой реализации
        ]

        def order_keys(keys: List[str]) -> List[str]:
            """Упорядочивание ключей: сначала preferred_order, потом остальные в исходном порядке."""
            keys_set = list(keys)
            ordered = [k for k in preferred_order if k in keys_set]
            for k in keys_set:
                if k not in ordered:
                    ordered.append(k)
            return ordered
        
        first_group = True

        for group in groups:
            if first_group:
                first_group = False
            else:
                doc.add_paragraph()

            names = list(group.keys())
            count = len(names)

            # Группа из одного словаря -> двухколоночная таблица
            if count == 1:
                name = names[0]
                attrs = group[name]
                rows = [("Марка", self._clean_text(self._strip_dup_suffix(str(name))))]
                for k, v in attrs.items():
                    if v is None or not str(v).strip():
                        continue
                    rows.append((k, v))
                if rows:
                    self._add_two_column_table(doc, rows, add_header=True)
                continue

            # Все характеристики в группе одинаковые, можно взять из первого
            first_attrs = next(iter(group.values()))
            param_keys = order_keys(list(first_attrs.keys()))

            # 2–5 марок -> горизонтальная таблица
            if 2 <= count <= 7:
                # Колонки: 1 (Параметр) + N марок
                table = doc.add_table(rows=0, cols=1 + count)
                table.style = 'Table Grid'
                self._set_table_full_width(table)

                # Шапка
                header_row = table.add_row().cells
                # Первый столбец — "Параметр"
                self._set_cell_style(header_row[0], "Параметр", bold=True)
                for idx, name in enumerate(names, start=1):
                    self._set_cell_style(header_row[idx], self._clean_text(self._strip_dup_suffix(str(name))), bold=True)

                # Строки по характеристикам
                for param in param_keys:
                    row_cells = table.add_row().cells
                    # Название характеристики слева
                    self._set_cell_style(row_cells[0], self._clean_text(str(param)), bold=False)

                    for idx, name in enumerate(names, start=1):
                        value = group[name].get(param, "")
                        formatted = self._separate_subitem_values(param, self._format_table_value(value))
                        lines = self._split_spec_value(formatted)
                        cell_text = "\n".join(lines)
                        self._set_cell_style(row_cells[idx], cell_text, bold=False)

                # Нет завершающей пустой строки
                continue

            # 6+ марок.
            # Вертикальная таблица (модификации в строках, характеристики в шапке) остаётся,
            # пока характеристик немного: столбцов 1 + len(param_keys). Если их много (насосы —
            # до 20 => 21 столбец), портретный A4 даёт нечитаемо узкие столбцы — тогда переходим
            # на горизонтальную раскладку и режем модификации на чанки, повторяя столбец «Параметр».
            elif 1 + len(param_keys) <= self.max_spec_table_cols:
                # Колонки: 1 (Наименование) + характеристики
                table = doc.add_table(rows=0, cols=1 + len(param_keys))
                table.style = 'Table Grid'
                self._set_table_full_width(table)

                # Шапка
                header_row = table.add_row().cells
                self._set_cell_style(header_row[0], "Наименование", bold=True)
                for idx, param in enumerate(param_keys, start=1):
                    self._set_cell_style(header_row[idx], self._clean_text(str(param)), bold=True)

                # Строки по маркам
                for name in names:
                    attrs = group[name]
                    row_cells = table.add_row().cells
                    self._set_cell_style(row_cells[0], self._clean_text(self._strip_dup_suffix(str(name))), bold=False)

                    for idx, param in enumerate(param_keys, start=1):
                        value = attrs.get(param, "")
                        formatted = self._separate_subitem_values(param, self._format_table_value(value))
                        lines = self._split_spec_value(formatted)
                        cell_text = "\n".join(lines)
                        self._set_cell_style(row_cells[idx], cell_text, bold=False)

            # Слишком много характеристик для вертикальной таблицы -> горизонтальная раскладка,
            # разбитая на чанки по модификациям (в каждой таблице повторяется столбец «Параметр»).
            else:
                chunk = self.spec_table_chunk_size
                for start in range(0, count, chunk):
                    if start > 0:
                        doc.add_paragraph()
                    chunk_names = names[start:start + chunk]

                    table = doc.add_table(rows=0, cols=1 + len(chunk_names))
                    table.style = 'Table Grid'
                    self._set_table_full_width(table)

                    # Шапка: «Параметр» + модификации этого чанка
                    header_row = table.add_row().cells
                    self._set_cell_style(header_row[0], "Параметр", bold=True)
                    for idx, name in enumerate(chunk_names, start=1):
                        self._set_cell_style(header_row[idx], self._clean_text(self._strip_dup_suffix(str(name))), bold=True)

                    # Строки по характеристикам
                    for param in param_keys:
                        row_cells = table.add_row().cells
                        self._set_cell_style(row_cells[0], self._clean_text(str(param)), bold=False)
                        for idx, name in enumerate(chunk_names, start=1):
                            value = group[name].get(param, "")
                            formatted = self._separate_subitem_values(param, self._format_table_value(value))
                            lines = self._split_spec_value(formatted)
                            self._set_cell_style(row_cells[idx], "\n".join(lines), bold=False)

    def _build_specifications_table(self, doc: Document, specifications: Dict[str, Any]):
        """
        Построение вертикальной таблицы характеристик (2 столбца: параметр | значение)
        с поддержкой многострочного текста внутри ячейки.
        Завершающая пустая строка НЕ добавляется.
        """
        if not specifications:
            return

        table = doc.add_table(rows=0, cols=2)
        table.style = 'Table Grid'
        self._set_table_full_width(table)

        for key, value in specifications.items():
            if value is None:
                continue

            text_value = str(value).strip()
            if text_value == "":
                continue

            # Очищаем текст от нежелательных символов и нормализуем степени
            clean_value = self._clean_text(text_value)
            clean_value = self._separate_subitem_values(key, clean_value)
            # Разбиваем значение на строки
            formatted_lines = self._split_spec_value(clean_value)
            formatted_text = "\n".join(formatted_lines)

            # Создаём строку таблицы
            row_cells = table.add_row().cells

            # Левая ячейка — название параметра
            self._set_cell_style(row_cells[0], str(key), bold=True)

            # Правая ячейка — многострочное значение
            self._set_cell_style(row_cells[1], formatted_text, bold=False)

    def _add_two_column_table(self, doc: Document, rows: List[Tuple[str, Optional[str]]],
                          add_header: bool = True) -> None:
        """
        Построение двухстолбцовой таблицы.
        Если value == None, то создаётся строка с объединённой ячейкой (заголовок группы).
        Заголовок группы выводится обычным шрифтом (не жирным).
        """
        if not rows:
            return
        table = doc.add_table(rows=0, cols=2)
        table.style = 'Table Grid'
        self._set_table_full_width(table)

        if add_header:
            header_cells = table.add_row().cells
            self._set_cell_style(header_cells[0], "Параметр", bold=True)
            self._set_cell_style(header_cells[1], "Значение", bold=True)

        for key, value in rows:
            row_cells = table.add_row().cells
            if value is None:
                # Объединённая ячейка на две колонки – обычный шрифт
                merged_cell = row_cells[0]
                merged_cell.merge(row_cells[1])
                self._set_cell_style(merged_cell, self._clean_text(str(key)), bold=False)
                merged_cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.LEFT
            else:
                # Обычная строка
                self._set_cell_style(row_cells[0], self._clean_text(str(key)), bold=False)
                if value is None or str(value).strip() == "":
                    formatted = ""
                else:
                    formatted = self._separate_subitem_values(key, self._format_table_value(value))
                    lines = self._split_spec_value(formatted)
                    formatted = "\n".join(lines)
                self._set_cell_style(row_cells[1], formatted, bold=False)

    def _add_specifications(self, doc: Document, specifications: Dict[str, Any]) -> None:
        """
        Добавление блока технических характеристик.
        - Все плоские одиночные словари объединяются в одну таблицу со скалярными.
        - Между разными частями (основная таблица, отдельные таблицы, варианты, текстовые блоки)
        добавляется ровно одна пустая строка.
        - Внутри текстовых блоков пустые строки отсутствуют.
        - После всего раздела добавляется одна пустая строка.
        """
        if not specifications:
            return

        dict_specs = {k: v for k, v in specifications.items() if isinstance(v, dict)}
        scalar_specs = {k: v for k, v in specifications.items() if not isinstance(v, dict)}

        self._add_section_heading(doc, "Технические характеристики")

        # Классифицируем вложенные словари
        variants, single_nonempty, text_blocks = self._classify_spec_dicts(dict_specs)

        flat_singles = [(k, v) for k, v in single_nonempty if self._is_flat_dict(v)]
        nested_singles = [(k, v) for k, v in single_nonempty if not self._is_flat_dict(v)]

        parts_output = []  # для отслеживания, было ли что-то выведено

        # 1. Основная таблица (скалярные + все плоские одиночные словари)
        all_rows = []
        for k, v in scalar_specs.items():
            all_rows.append((k, v))
        for outer_key, inner_dict in flat_singles:
            all_rows.append((self._strip_dup_suffix(outer_key), None))
            for ik, iv in inner_dict.items():
                all_rows.append((ik, iv))

        if all_rows:
            self._add_two_column_table(doc, all_rows, add_header=True)
            parts_output.append('main')

        # 2. Неплоские одиночные словари (каждый отдельной таблицей)
        for outer_key, inner_dict in nested_singles:
            if parts_output:
                doc.add_paragraph()
            flattened = self._flatten_nested_dict(inner_dict)
            if flattened:
                rows = [("Марка", self._clean_text(self._strip_dup_suffix(outer_key)))] + flattened
                self._add_two_column_table(doc, rows, add_header=True)
                parts_output.append('nested')

        # 3. Варианты (многостолбцовая таблица)
        if variants:
            if parts_output:
                doc.add_paragraph()
            variants_dict = {name: attrs for name, attrs in variants}
            self._build_multi_column_table(doc, "Технические характеристики", variants_dict)
            parts_output.append('variants')

        # 4. Текстовые блоки (пустые словари)
        for idx, (outer_key, inner_dict) in enumerate(text_blocks):
            # Добавляем пустую строку только если предыдущий блок НЕ текстовый
            if parts_output and parts_output[-1] != 'text':
                doc.add_paragraph()

            self._add_text_block_from_dict(doc, outer_key, inner_dict)
            parts_output.append('text')

        # 5. Заключительная пустая строка после всего раздела
        if parts_output:
            doc.add_paragraph()


    # ВАРИАНТЫ (variants) КАК МНОГОСТОЛБЧАТЫЕ ТАБЛИЦЫ
    def _add_variants(self, doc: Document, variants: Any):
        """
        Добавление раздела "Варианты".
        - Варианты без атрибутов (пустые словари) выводятся маркированным списком.
        - Варианты с атрибутами обрабатываются как раньше (таблицы).
        """
        if not variants:
            return

        # ---- 1. Нормализация входных данных в словарь {name: attrs} ----
        # ВАЖНО: имя варианта используется downstream как ключ словаря и как подпись
        # столбца/строки в таблице. Прежняя логика писала normalized[key] = val, где
        # key — это ключ-обёртка элемента. Когда несколько вариантов приходят под
        # ОДНОЙ обёрткой (напр. три {"Другие параметры": {...}}), записи затирали друг
        # друга — оставался только последний вариант, а обёртка "Другие параметры"
        # ошибочно шла в подпись. Плюс плоские варианты ({"Цвет": ..., "Размер": ...})
        # и одиночные строки ({"Модификация": "..."}) вовсе игнорировались.
        def _flatten_variant(src: Dict[str, Any]) -> Dict[str, Any]:
            """Плоский словарь атрибутов одного варианта: вложенный контейнер-словарь
            (например "Другие параметры": {"Вид": "узкий"}) разворачивается в обычные
            атрибуты, пустые значения отбрасываются."""
            flat = {}
            for k, v in src.items():
                if isinstance(v, dict):
                    for ik, iv in v.items():
                        if iv is not None and str(iv).strip():
                            flat[ik] = iv
                elif v is not None and str(v).strip():
                    flat[k] = v
            return flat

        parsed = []   # список (candidate_name|None, flat_attrs) в исходном порядке
        if isinstance(variants, list):
            for item in variants:
                if not isinstance(item, dict):
                    continue
                # Предпочтительный формат: {"name": "...", "attributes": {...}}
                name = item.get("name")
                attrs = item.get("attributes")
                if isinstance(name, str) and isinstance(attrs, dict):
                    parsed.append((self._clean_text(name).strip(), _flatten_variant(attrs)))
                    continue
                # Формат {"Название варианта": {атрибуты}} — обёртка как кандидат-имя.
                if len(item) == 1 and isinstance(next(iter(item.values())), dict):
                    key = next(iter(item.keys()))
                    parsed.append((key, _flatten_variant(item[key])))
                    continue
                # Плоский вариант ({"Цвет": ..., "Размер": ...}) или {"Модификация": "..."}
                # — собственного различающего имени нет.
                flat = _flatten_variant(item)
                if flat:
                    parsed.append((None, flat))
        elif isinstance(variants, dict):
            for key, val in variants.items():
                if isinstance(val, dict):
                    parsed.append((key, _flatten_variant(val)))

        # Имя-обёртка становится подписью варианта ТОЛЬКО если оно уникально (реально
        # различает варианты). Совпадающие обёртки ("Другие параметры" у всех) — это
        # контейнеры, а не имена: им, как и безымянным плоским вариантам, присваивается
        # порядковый номер, чтобы записи не затирали друг друга.
        name_counts = Counter(n for n, _ in parsed if n)
        normalized = {}
        for n, attrs in parsed:
            if not n or name_counts[n] > 1 or n in normalized:
                n = str(len(normalized) + 1)
            normalized[n] = attrs

        if not normalized:
            return

        # Проверка на осмысленность (есть ли хотя бы одно непустое название или атрибут)
        has_meaningful = False
        for name, attrs in normalized.items():
            clean_name = self._clean_text(name).strip()
            if clean_name:
                has_meaningful = True
                break
            for attr_val in attrs.values():
                if self._clean_text(str(attr_val)).strip():
                    has_meaningful = True
                    break
            if has_meaningful:
                break
        if not has_meaningful:
            log.info("Варианты присутствуют, но все названия/атрибуты пусты – раздел не добавляется")
            return

        # ---- 2. Заголовок раздела ----
        self._add_section_heading(doc, "Варианты")

        # ---- 3. Классификация вариантов ----
        variants_group, single_nonempty, text_blocks = self._classify_spec_dicts(normalized)

        # Отделяем пустые варианты (без атрибутов) от прочих текстовых блоков
        empty_variants = []
        non_empty_text_blocks = []
        for name, attrs in text_blocks:
            if not attrs:   # пустой словарь
                empty_variants.append(name)
            else:
                non_empty_text_blocks.append((name, attrs))

        parts_output = []

        # ---- 4. Пустые варианты → маркированный список ----
        if empty_variants:
            self._add_variants_list(doc, empty_variants)
            parts_output.append('empty_list')

        # ---- 5. Обработка одиночных непустых вариантов (single_nonempty) ----
        for outer_key, inner_dict in single_nonempty:
            if parts_output:
                doc.add_paragraph()   # пустая строка между блоками
            if self._is_flat_dict(inner_dict):
                rows = [("Вариант", self._clean_text(self._strip_dup_suffix(str(outer_key))))]
                for k, v in inner_dict.items():
                    if v is not None and str(v).strip():
                        rows.append((k, v))
                if rows:
                    self._add_two_column_table(doc, rows, add_header=True)
            else:
                flattened = self._flatten_nested_dict(inner_dict)
                if flattened:
                    rows = [("Вариант", self._clean_text(self._strip_dup_suffix(str(outer_key))))] + flattened
                    self._add_two_column_table(doc, rows, add_header=True)
            parts_output.append('single')

        # ---- 6. Группы вариантов с одинаковыми ключами (многостолбцовые таблицы) ----
        if variants_group:
            if parts_output:
                doc.add_paragraph()
            variants_dict_for_multi = {name: attrs for name, attrs in variants_group}
            self._build_multi_column_table(doc, "Варианты", variants_dict_for_multi)
            parts_output.append('multi')

        # ---- 7. Остальные текстовые блоки (непустые словари, если такие есть) ----
        for idx, (outer_key, inner_dict) in enumerate(non_empty_text_blocks):
            if parts_output and parts_output[-1] != 'text':
                doc.add_paragraph()
            self._add_text_block_from_dict(doc, outer_key, inner_dict)
            parts_output.append('text')

        # ---- 8. Заключительная пустая строка после всего раздела ----
        if parts_output:
            doc.add_paragraph()
            
    # -------------------------------------------------------------------------
    # ДОПОЛНИТЕЛЬНЫЕ БЛОКИ
    # -------------------------------------------------------------------------
    def _parse_and_add_additional_blocks(self, doc: Document, additional_text: str):
        """Парсинг и добавление дополнительных блоков информации (оставлено для совместимости)"""
        if not additional_text:
            return

        block_keywords = [
            "Область применения",
            "Комплектация",
            "Область применения",
            "Преимущества",
            "Совместимость",
            "Инструкции по применению",
	        "Условия эксплуатации",
            "Условия хранения",
            "Меры безопасности",
            "Габариты",
            "Вес"
        ]

        lines = additional_text.split('\n')
        current_block = None
        current_content = []

        for line in lines:
            line = line.strip()
            if not line:
                continue

            is_block_header = False
            for keyword in block_keywords:
                if keyword.lower() in line.lower():
                    if current_block and current_content:
                        self._add_additional_block(doc, current_block, '\n'.join(current_content))
                    current_block = keyword
                    current_content = []
                    is_block_header = True
                    break

            if not is_block_header and current_block:
                current_content.append(line)

        if current_block and current_content:
            self._add_additional_block(doc, current_block, '\n'.join(current_content))

    def _add_additional_block(self, doc: Document, block_name: str, block_content: str):
        """Добавление отдельного блока дополнительной информации. В конце блока — одна пустая строка."""
        if not block_content or block_content == "Отсутствует":
            return

        self._add_section_heading(doc, block_name)

        lines = block_content.strip().split('\n')

        if len(lines) > 1:
            for line in lines:
                line = line.strip()
                if line:
                    content_paragraph = doc.add_paragraph()
                    content_paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
                    content_paragraph.paragraph_format.first_line_indent = Inches(0.25)
                    self._add_text_runs(content_paragraph, f"- {line}")
        else:
            content_paragraph = doc.add_paragraph()
            content_paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
            content_paragraph.paragraph_format.first_line_indent = Inches(0.25)
            self._add_text_runs(content_paragraph, block_content)

        # Одна пустая строка после блока
        doc.add_paragraph()

    def _add_additional_info(self, doc: Document, additional_text: str):
        """
        Добавление блока 'Дополнительная информация' в самом конце карточки.
        Логика форматирования текста такая же, как у описания товара:
        - многострочный текст -> список с маркером -
        - одна строка -> обычный абзац
        В конце блока — одна пустая строка.
        """
        if not additional_text:
            return

        # Заголовок
        self._add_section_heading(doc, "Дополнительная информация")

        lines = additional_text.strip().split('\n')

        if len(lines) > 1:
            for line in lines:
                line = line.strip()
                if line:
                    list_paragraph = doc.add_paragraph()
                    list_paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
                    self._add_text_runs(list_paragraph, f"- {line}")
                    list_paragraph.paragraph_format.space_after = Pt(0)
                    list_paragraph.paragraph_format.space_before = Pt(0)
        else:
            content_paragraph = doc.add_paragraph()
            content_paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
            self._add_text_runs(content_paragraph, additional_text)

        # Одна пустая строка после блока
        doc.add_paragraph()
    
    def _add_source_url(self, doc: Document, source_url: str):
        """
        Добавляет блок с URL исходной страницы в конец карточки.
        Перед блоком — одна пустая строка (в сумме с пустой строкой предыдущего блока получается две).
        """
        # Бессхемный URL («www.site.ru/...») приводим к абсолютному https-URL, чтобы в карточке
        # была корректная кликабельная ссылка (целевая система ожидает абсолютный URL).
        if source_url and not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', source_url):
            source_url = 'https://' + source_url.lstrip('/')

        # Одна пустая строка перед блоком (предыдущий блок уже оставил одну, итого две)
        doc.add_paragraph()

        # Параграф с текстом и гиперссылкой
        url_paragraph = doc.add_paragraph()
        url_paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT

        # Run для URL
        url_run = url_paragraph.add_run(f"URL страницы: {source_url}")
        url_run.font.name = 'Calibri'
        url_run.font.size = Pt(12)

    def _fill_empty_lines(self, doc: Document) -> None:
        """Каждая пустая строка карточки — 5 пробелов (_EMPTY_LINE): у заказчика пустые строки
        схлопываются. Пустой абзац-разделитель получает run из 5 пробелов; пустая строка внутри
        многострочного абзаца (между разрывами строки — «Инструкции по применению» и т. п.) —
        тоже. Число и места пустых строк не меняются. Ячейки таблиц не трогаем: doc.paragraphs
        их не содержит."""
        for paragraph in doc.paragraphs:
            if not paragraph.text:
                paragraph.add_run(_EMPTY_LINE)
                continue
            for run in paragraph.runs:
                if '\n' in run.text:
                    run.text = '\n'.join(line if line.strip() else _EMPTY_LINE
                                         for line in run.text.split('\n'))

    # -------------------------------------------------------------------------
    # СОХРАНЕНИЕ И КОНВЕРТАЦИЯ
    # -------------------------------------------------------------------------
    def _save_document(self, doc: Document, product_folder: str, product_name: str, product_id: str) -> str:
        """Сохранение документа в формате DOCX с использованием названия товара"""
        try:
            safe_filename = sanitize_card_filename(product_name)
            safe_filename = safe_filename.replace('_', ' ')
            while '  ' in safe_filename:
                safe_filename = safe_filename.replace('  ', ' ')
            safe_filename = safe_filename.strip()

            if safe_filename.lower().endswith('.docx'):
                safe_filename = safe_filename[:-5]

            # Обрезаем по байтам С РЕЗЕРВОМ под расширение «.docx» и суффикс дедупликации « (N)».
            # Раньше резали ровно до 255, ПОТОМ добавляли «.docx» → имя > 255 байт → на Linux
            # doc.save() падает OSError, папка товара остаётся без RTF (баг «папка без файла»,
            # типично для длинных кириллических имён диапазонов/групп типоразмеров).
            _name_byte_budget = 255 - len('.docx'.encode('utf-8')) - len(' (100)'.encode('utf-8'))
            safe_filename = _truncate_to_bytes(safe_filename, _name_byte_budget)
            if not safe_filename.strip():
                safe_filename = f"card_{product_id}"

            base_filename = f"{safe_filename}.docx"
            file_path = os.path.join(product_folder, base_filename)

            counter = 1
            original_file_path = file_path

            while os.path.exists(file_path):
                name_part = safe_filename
                extension = ".docx"

                match = re.search(r' \((\d+)\)$', name_part)
                if match:
                    name_part = name_part[:match.start()]

                new_filename = f"{name_part} ({counter}){extension}"
                file_path = os.path.join(product_folder, new_filename)
                counter += 1

                if counter > 100:
                    log.warning(f"Не удалось создать уникальное имя для {original_file_path}")
                    file_path = os.path.join(product_folder, f"card_{product_id}.docx")
                    break

            doc.save(file_path)
            log.info(f"DOCX документ сохранен: {file_path}")
            return file_path

        except Exception as e:
            log.error(f"Ошибка сохранения DOCX документа: {e}")
            return ""

    async def _convert_to_rtf(self, docx_path: str) -> str:
        """Конвертация DOCX→RTF через LibreOffice.

        Конвертируем в КОРОТКОМ временном каталоге (LibreOffice не может записать RTF, когда
        итоговый путь/имя у лимита ФС — длинные кириллические имена диапазонов/групп типоразмеров,
        напр. «Подстанции комплектные… КТПм ТК-25÷400…»), затем переносим результат в финальный
        (возможно длинный) путь средствами Python. Закрывает остаток бага «папка без RTF» (N4):
        docx сохранялся, а LibreOffice-конвертация падала Io-ошибкой, оставляя папку без .rtf."""
        if not os.path.exists(docx_path):
            log.error(f"Файл для конвертации не найден: {docx_path}")
            return ""

        if not os.path.exists(self.libreoffice_path):
            log.warning(f"LibreOffice не найден по пути: {self.libreoffice_path}")
            return ""

        rtf_path = docx_path[:-5] + '.rtf' if docx_path.lower().endswith('.docx') else docx_path + '.rtf'
        work_dir = tempfile.mkdtemp(prefix="lo_conv_")
        user_profile_dir = tempfile.mkdtemp(prefix="lo_p_")
        try:
            tmp_docx = os.path.join(work_dir, "card.docx")
            tmp_rtf = os.path.join(work_dir, "card.rtf")
            shutil.copy(docx_path, tmp_docx)
            user_profile_url = f"file://{user_profile_dir}"

            cmd = [
                str(self.libreoffice_path),
                '--headless',
                '--invisible',
                '--nodefault',
                '--nofirststartwizard',
                '--nolockcheck',
                '--nologo',
                f'-env:UserInstallation={user_profile_url}',
                '--convert-to', 'rtf',
                '--outdir', work_dir,
                tmp_docx
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()

            if process.returncode == 0 and os.path.exists(tmp_rtf):
                try:
                    with open(tmp_rtf, 'rb') as f:
                        # вывод LibreOffice — чистый 7-битный ASCII (кириллица в \uN\'XX-эскейпах);
                        # latin-1 даёт обратимое побайтовое чтение и запись без потерь
                        recoded = _recode_rtf_to_cp1251(f.read().decode('latin-1'))
                    with open(tmp_rtf, 'wb') as f:
                        f.write(recoded.encode('latin-1'))
                except Exception as e:
                    log.warning(f"Не удалось перекодировать RTF в Windows-1251 ({tmp_rtf}): {e}")
                shutil.move(tmp_rtf, rtf_path)
                return rtf_path
            else:
                log.error(f"Ошибка конвертации: {stderr.decode(errors='ignore')}")
                return ""
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            shutil.rmtree(user_profile_dir, ignore_errors=True)