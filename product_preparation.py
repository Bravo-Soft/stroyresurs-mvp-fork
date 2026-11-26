"""Общие правила товарного гейта и восстановления для pipeline и контроля."""
import re
from decimal import Decimal
from typing import Any, Dict
from product_tables import restore_source_tables, TableStructureError
from source_product_tables import restore_deterministic_tables
from Profile_Markdown.tegola_ru import restore_source_product
from product_quality import normalize_product_fields
from labelled_source_instructions import restore_labelled_instructions
from quality_source_fields import restore_quality_source_fields
from labelled_product_sections import restore_labelled_product_sections
from visible_product_sections import restore_visible_product_sections
from source_detail_fields import restore_source_detail_fields
from source_image_text import scope_image_specs
from bitumex_source_sections import restore_bitumex_source_sections
from product_id_manager import restore_source_variant_name

# Числа должны быть самостоятельными значениями: код марки и показатель
# степени единицы (г/м^2) не являются границами.
_NUMBER = r'[+−-]?\d+(?:[.,]\d+)?'
_SEPARATOR = r'(?:[-‐‑‒–—−]|\.{2,3}|…|\bдо\b|\bto\b)'
_UNIT = r'(?:°\s*)?[а-яёa-zµμ%°]+(?:[²³]|\^[23])?(?:[/\\][а-яёa-z]+(?:[²³]|\^[23])?)?'
_RANGE = re.compile(
    rf'(?<![\w.,/^№+−-])(?P<lower>{_NUMBER})\s*'
    rf'(?:{_UNIT}\s*(?:\bдо\b|\bto\b)|(?:(?P<repeated>{_UNIT})\s*)?(?P<separator>{_SEPARATOR}))'
    rf'\s*(?P<upper>{_NUMBER})(?!\d|[.,]\d|[-‐‑–—]\d)'
    rf'(?(repeated)\s*(?P=repeated)(?!\w))', re.I)
_CONDITION_PREFIX = re.compile(
    r'(?:\bпри(?:\s+(?:температуре|влажности))?|\b(?:ГОСТ|ТУ|СТБ|ISO|DIN|EN)(?:\s+Р)?)'
    r'\s*(?:от\s*)?$', re.I)
_LAYER_SUFFIX = re.compile(r'\s*сло(?:й|я|е[вй]|ёв)', re.I)
_AXIS_SEPARATOR = re.compile(r'(?:×|(?<![а-яёa-z])[xх*])\s*(?=[+−-]?\d)', re.I)


def _count_value_numeric(value: Any, *, dimensions: bool = True) -> int:
    """Число зачтённых ТХ: по одной на ось, по две на явный диапазон."""
    text = str(value)
    parts = _AXIS_SEPARATOR.split(text) if dimensions else [text]
    count = 0
    for part in parts:
        if not re.search(r'\d', part):
            continue
        count += 1
        for span in _RANGE.finditer(part):
            if _CONDITION_PREFIX.search(part[:span.start()]) or _LAYER_SUFFIX.match(part[span.end():]):
                continue
            lower, upper = (Decimal(s.replace(',', '.').replace('−', '-')) for s in span.group('lower', 'upper'))
            if lower == upper:
                continue
            # В размерной колонке «900-700» бывает обозначением кронштейна.
            # Обратный порядок через простой дефис сам по себе не задаёт интервал.
            if dimensions and span['separator'] == '-' and lower > upper and span.group() == part.strip():
                continue
            count += 1
            break
    return count


def count_numeric_characteristics(product: Dict[str, Any]) -> int:
    """Числовой гейт с исключениями v37: коды, упаковка и условия не являются ТХ.

    Диапазон даёт две ТХ по его границам, перечень — одну. Только явно размерное
    поле с записью через ×/х/* даёт независимые оси габаритов.
    """
    if not isinstance(product, dict):
        return 0
    excluded = re.compile(
        r'\bгост\b|(?:^|\W)ту(?:\W|$)|стандарт|норматив|артикул|модель|код|'
        r'маркир|марка\s+(?:стали|материала|изделия|полотна|нити)|цвет|ral|гарант|срок\s+(?:хранения|годности|службы|эксплуатации)|хранени|паковк|упаков|палл[её]т|фасов|\bтара\b|'
        r'цен[аы]|стоимост|заказ|достав|телефон|адрес|инструкц|'
        r'услови[яй]|окружающ|наименован|производител|сертификат|единиц[аы] измер',
        re.IGNORECASE)
    axes = re.compile(r'габарит|размер|длин|ширин|высот|dimensions', re.IGNORECASE)

    def walk(value, key=''):
        if isinstance(value, dict):
            # Имя модели/исполнения — контейнер, а не характеристика.
            if re.search(r'паковк|упаков|палл[её]т|фасов|\bтара\b|гарант|срок\s+(?:хранения|годности|службы)', key, re.IGNORECASE):
                return 0
            return sum(walk(v, str(k)) for k, v in value.items())
        if isinstance(value, list):
            if all(not isinstance(v, (dict, list)) for v in value):
                # Несколько значений одной ТХ не создают новых параметров.
                return max((walk(v, key) for v in value), default=0)
            return sum(walk(v, key) for v in value)
        key = re.sub(r'\s*\((?:по\s+)?(?:ГОСТ|ТУ|СТБ|ISO)\b[^)]*\)', '', key, flags=re.I)
        if re.fullmatch(r'металл|материал|покрытие|замасливатель|состав стекла|нд|нтд|примечани[ея]|конструкция|устройство|принцип работы|состав и свойства материала|(?:строка|столбец)\s+\d+', key.strip(), re.I):
            return 0  # Обозначение материала/покрытия не физическая величина.
        if excluded.search(key) or re.search(
                r'температур.*(?:монтаж|окружающ|хранени)', key, re.IGNORECASE):
            return 0
        if value is None:
            return 0
        text = str(value)
        if (re.fullmatch(r'нити\s+в\s+(?:основе|утке)', key.strip(), re.I)
                and re.fullmatch(r'[А-ЯA-Z]+[\d\w(),./×х*+-]+', text.strip(), re.I)):
            return 0  # Код использованной нити, не её числовое свойство.
        if re.search(r'₽|\b(?:руб(?:ль|ля|лей)?|RUB|USD|EUR)\b', key + ' ' + text, re.I):
            return 0  # Денежные колонки не становятся ТХ без слова «цена».
        # Классы покрытий и цветовые коды — идентификаторы, не значения толщины.
        text = re.sub(r'\b(?:RAL|Zn|AlZn|RR)\s*\d+\b', '', text, flags=re.IGNORECASE)
        if not re.search(r'\d', text):
            return 0
        return _count_value_numeric(text, dimensions=bool(axes.search(key)))

    count = walk(product.get('specifications', {}))
    count += walk(product.get('dimensions'), 'dimensions')
    count += walk(product.get('weight'), 'Масса')
    return count

def prepare_product(ai_data, html, url, source_text):
    """Штатный порядок восстановления видимых блоков перед гейтом и карточкой."""
    restore_source_tables(ai_data, html, url)
    restore_deterministic_tables(ai_data, html, url)
    restore_source_product(ai_data, html, url)
    restore_labelled_instructions(ai_data, html, url)
    normalize_product_fields(ai_data, source_text)
    restore_quality_source_fields(ai_data, html, url)
    restore_labelled_product_sections(ai_data, html, url)
    restore_visible_product_sections(ai_data, html, url)
    restore_source_detail_fields(ai_data, html, url)
    scope_image_specs(ai_data, html, url, source_text)
    restore_bitumex_source_sections(ai_data, html, url)
    restore_source_variant_name(ai_data, html, url)
