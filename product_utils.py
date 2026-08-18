# product_utils.py
from typing import  Dict, Any, List
import logging
import hashlib
import time
import json
import re

log = logging.getLogger("product_utils")


def _is_cp1251(ch: str) -> bool:
    """True, если символ представим в Windows-1251 (иначе имя файла ломает cp1251-ФС заказчика)."""
    try:
        ch.encode('cp1251')
        return True
    except UnicodeEncodeError:
        return False


# Утверждённые заказчиком (2026-07-28, Анализ системы/Символы_под_замену_cp1251.xlsx) замены
# символов вне Windows-1251 на cp1251-безопасные. Применяются в трёх точках-выходах —
# text_extractor._postprocess_markdown, docx_generator._clean_text и sanitize_card_filename —
# как и × ≥ ≤ / дроби.
_CP1251_REPLACEMENTS = {
    # Диаметр → d
    'Ø': 'd', 'ø': 'd', 'ϕ': 'd', 'φ': 'd', '⌀': 'd',
    # Стрелки
    '→': '->', '←': '<-',
    # Деление / разделитель диапазонов «25÷400»
    '÷': '-',
    # Дюйм (двойной штрих) → обычная кавычка «"» (напр. «2″»→«2"»). В docx_generator._clean_text
    # дюймовая кавычка после цифры сохраняется (общая зачистка кавычек её щадит).
    '″': '"',
    # Греческие буквы → слово
    'Σ': 'сумма', 'Ω': 'Ом', 'δ': 'дельта', 'Δ': 'дельта', 'τ': 'тау', 'λ': 'лямбда',
    # Латинская Ü (бренды)
    'Ü': 'U',
    # Однозначные: минус, приближённо, искажённая «е»
    '−': '-', '≈': '~', 'ѐ': 'е',
    # Полноширинные → обычные ASCII
    '（': '(', '）': ')', '；': ';',
    # Мусор / невидимое / линии таблиц — удалить
    '─': '', '​': '', '˙': '', '¸': '', 'ŋ': '',
}
_CP1251_TRANS = {ord(k): v for k, v in _CP1251_REPLACEMENTS.items()}


def apply_cp1251_replacements(text: str) -> str:
    """Заменяет символы вне Windows-1251 на утверждённые cp1251-безопасные (Ø→d, →→->, Ω→Ом …).
    Синхронно применяется в text_extractor._postprocess_markdown, docx_generator._clean_text и
    в именах файлов (sanitize_card_filename)."""
    if not text:
        return text
    return text.translate(_CP1251_TRANS)


def extract_product_name_from_ai_text(ai_text: str) -> str:
    """Извлечение наименования товара из AI текста в формате JSON"""
    if not ai_text:
        return "Неизвестный товар"
    
    try:        
        json_start = ai_text.find('{')
        json_end = ai_text.rfind('}') + 1
        
        if json_start >= 0 and json_end > json_start:
            json_str = ai_text[json_start:json_end]
            data = json.loads(json_str)
            
            # Извлекаем название товара из JSON структуры
            product_name = data.get('products', {}).get('product_name')
            
            if product_name:
                # Возвращаем полное наименование без изменений
                return product_name.strip()
    
    except json.JSONDecodeError:        
        log.warning("Не удалось распарсить JSON, используем другие методы поиска")
        pass
    except Exception as e:
        log.error(f"Ошибка извлечения названия товара из JSON: {e}")

def sanitize_card_filename(filename: str) -> str:
    """
    Создание безопасного имени файла карточки товара.
    1. Сохраняет производителя в скобках.
    2. Заменяет слеши на тире.
    3. Сохраняет точки.
    4. Удаляет только запрещённые символы.
    """
    if not filename:
        return "product"

    # 1. Заменяем слеши на тире
    filename = filename.replace('/', '-').replace('\\', '-')

    # «ё»→«е» (редактор: буква «ё» в именах файлов не допускается)
    filename = filename.replace('ё', 'е').replace('Ё', 'Е')

    # Утверждённые замены символов вне cp1251 (Ø→d, →→->, Ω→Ом …) — как в тексте карточки.
    filename = apply_cp1251_replacements(filename)

    # Знак умножения × → строчная русская х (как в тексте карточки, _clean_text): × (U+00D7)
    # в Windows-1251 не представим, и файловая система заказчика (cp1251) на нём падает —
    # «Труба 40×20×2» → «40??20??2», ошибка DOS 123. Размерные разделители ∙ • между цифрами — тоже.
    filename = filename.replace('×', 'х')
    filename = re.sub(r'(?<=\d)[∙•](?=\d)', 'х', filename)

    # Каретку оставляем как степень (^N перед цифрой); ошибочную «галочку перед тире»
    # (Е1: ^ НЕ перед цифрой) убираем.
    filename = re.sub(r'\^(?!\d)', '', filename)

    # 2. Удаляем запрещённые символы: все варианты значка градуса (° º ˚ ∘ + комбинир.
    #    кольцо U+030A), ® ™ и пунктуацию ФС.
    invalid_chars = '°º˚∘̊®™«»<>:"|?*\n\r\t'
    for ch in invalid_chars:
        filename = filename.replace(ch, '')

    # 3. Убираем двойные пробелы
    while '  ' in filename:
        filename = filename.replace('  ', ' ')

    # 4. Страховка cp1251: имя файла попадает в файловую систему заказчика (Windows-1251).
    #    Любой оставшийся символ вне cp1251 (Ø, →, греч. и т.п.) → «_», иначе файл не открыть.
    #    Осмысленные замены (Ø→d и пр.) — по согласованному списку символов, отдельной задачей.
    filename = ''.join(ch if _is_cp1251(ch) else '_' for ch in filename)

    return filename.strip()


def _truncate_to_bytes(s: str, max_bytes: int = 255) -> str:
    """Обрезает строку до указанного числа байт в кодировке UTF-8 без разрыва символов"""
    encoded = s.encode('utf-8')
    if len(encoded) <= max_bytes:
        return s
    # Обрезаем байтовую строку и декодируем, игнорируя ошибки на разрыве символа
    truncated = encoded[:max_bytes].decode('utf-8', errors='ignore')
    return truncated

def sanitize_filename(filename: str, product_data: Dict[str, Any] = None) -> str:
    """Создание безопасного имени файла с учётом байтового ограничения (макс. 255 байт)"""
    
    if not filename or filename == 'Неизвестный товар':
        unique_id = hashlib.md5(
            f"{product_data.get('source_url', '') if product_data else ''}_{time.time_ns()}".encode()
        ).hexdigest()[:8]
        return f"product_{unique_id}"
    
    # Заменяем все виды слешей на тире
    filename = filename.replace('\\', '-').replace('/', '-')

    # «ё»→«е» (как и в имени файла карточки)
    filename = filename.replace('ё', 'е').replace('Ё', 'Е')
    # Каретку оставляем как степень (^N перед цифрой); ошибочную «галочку перед тире» убираем.
    filename = re.sub(r'\^(?!\d)', '', filename)

    invalid_chars = '°º˚∘̊®™«»<>:"|?*\n\r'  # Слеши уже обработали; варианты градуса
    for char in invalid_chars:
        filename = filename.replace(char, '')
    
    filename = filename.replace(' ', '_').replace('.', '_').replace(',', '_')
    while '__' in filename:
        filename = filename.replace('__', '_')
    
    # Дополнительно убираем возможные двойные тире, если они появились
    while '--' in filename:
        filename = filename.replace('--', '-')
    
    # Убираем тире и подчёркивания в начале и конце
    filename = filename.strip('_').strip('-')
    
    # Если после всех чисток имя стало пустым – генерируем уникальное
    if not filename:
        unique_id = hashlib.md5(
            f"{product_data.get('source_url', '') if product_data else ''}_{time.time_ns()}".encode()
        ).hexdigest()[:8]
        return f"product_{unique_id}"
    
    # Обрезаем по байтам (максимум 255 байт)
    filename = _truncate_to_bytes(filename, 255)
    
    return filename

def sanitize_company_name(name: str) -> str:
    """Создание безопасного имени компании"""
    if not name:
        return "Неизвестная компания"
    
    name = name.replace('ё', 'е').replace('Ё', 'Е')
    invalid_chars = '°º˚∘̊®™«»<>:"/\\|?*^'
    for char in invalid_chars:
        name = name.replace(char, '')
    
    name = name.replace(' ', '_').replace('.', '_').replace(',', '_')
    while '__' in name:
        name = name.replace('__', '_')
    
    return name.strip('_')

def normalize_address_for_deduplication(address: str) -> str:
    """
    Нормализация адреса для дедупликации.
    Приводит к нижнему регистру, удаляет почтовый индекс, сокращения.
    """
    if not address:
        return ""
    
    # Приводим к нижнему регистру
    normalized = address.lower()
    
    # Удаляем почтовый индекс (6 цифр в начале)
    normalized = re.sub(r'^\d{6}\s*,?\s*', '', normalized)
    
    # Заменяем сокращения на полные слова
    replacements = {
        r'ул\.': 'улица',
        r'пр\.': 'проспект',
        r'пр-т\.': 'проспект',
        r'пер\.': 'переулок',
        r'д\.': 'дом',
        r'корп\.': 'корпус',
        r'стр\.': 'строение',
        r'лит\.': 'литера',
        r',': ' ',
        r'\.': ' ',
        r'\s+': ' '  # Множественные пробелы в один
    }
    
    for pattern, replacement in replacements.items():
        normalized = re.sub(pattern, replacement, normalized)
    
    # Удаляем лишние пробелы и обрезаем
    normalized = ' '.join(normalized.split()).strip()
    
    return normalized

def deduplicate_distributors(distributors: List[Dict]) -> List[Dict]:
    """
    Дедупликация дистрибьюторов по нормализованному адресу.
    Приоритет у записей с более полными данными.
    """
    seen_addresses = {}
    unique_distributors = []
    
    for distributor in distributors:
        address = distributor.get('address', '')
        if not address:
            # Если адреса нет, добавляем как уникального
            unique_distributors.append(distributor)
            continue
        
        normalized_address = normalize_address_for_deduplication(address)
        
        # Определяем, является ли текущий дистрибьютор более полным
        if normalized_address in seen_addresses:
            existing = seen_addresses[normalized_address]
            existing_fields = sum(1 for v in existing.values() if v)
            current_fields = sum(1 for v in distributor.values() if v)
            
            # Приоритет у более полных данных
            if current_fields > existing_fields:
                # Заменяем существующий
                idx = next(i for i, d in enumerate(unique_distributors) 
                         if d.get('address', '') == existing.get('address', ''))
                unique_distributors[idx] = distributor
                seen_addresses[normalized_address] = distributor
        else:
            seen_addresses[normalized_address] = distributor
            unique_distributors.append(distributor)
    
    return unique_distributors

def extract_actual_company_name_from_ai_text(ai_text: Any, default_name: str = "") -> str:
    """
    Извлечение актуального наименования компании из AI-ответа.
    Поддерживает разные форматы ответов.
    """
    if not ai_text:
        return default_name
    
    try:
        # Если это строка, пытаемся распарсить как JSON
        if isinstance(ai_text, str):
            try:
                ai_text = json.loads(ai_text)
            except json.JSONDecodeError:
                # Если не JSON, ищем название в тексте
                import re
                patterns = [
                    r'"Наименование компании"\s*:\s*"([^"]+)"',
                    r"'Наименование компании'\s*:\s*'([^']+)'",
                    r'Наименование компании[:\s]+([^\n\r]+)'
                ]
                
                for pattern in patterns:
                    match = re.search(pattern, ai_text, re.IGNORECASE)
                    if match:
                        name = match.group(1).strip()
                        if name:
                            return name
                return default_name
        
        # Если это словарь
        if isinstance(ai_text, dict):
            # Формат 1: {"company": {"Наименование компании": "..."}}
            if 'company' in ai_text and isinstance(ai_text['company'], dict):
                name = ai_text['company'].get('Наименование компании', '')
                if name and name.strip():
                    return name.strip()
            
            # Формат 2: {"Наименование компании": "..."} напрямую
            name = ai_text.get('Наименование компании', '')
            if name and name.strip():
                return name.strip()
            
            # Формат 3: другие возможные ключи
            for key in ['company_name', 'name', 'Наименование', 'Company']:
                if key in ai_text:
                    name = ai_text[key]
                    if name and name.strip():
                        return name.strip()
    
    except Exception as e:
        log.error(f"Ошибка извлечения названия компании из AI-ответа: {e}")
    
    return default_name
