# report_generators.py 1.0.0 (modified)
import re
import os
import logging
from typing import Dict, List, Any, Optional, Set, Tuple
from datetime import datetime

from graph_reports.data_comparator import ChangeType
from graph_reports.path_formatter import PathFormatter

log = logging.getLogger("report_generators")


class BaseReportGenerator:
    """Базовый класс для генерации отчетов"""
    
    def __init__(self, path_formatter: PathFormatter):
        self.path_formatter = path_formatter
        self.today = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.date_format = "%Y-%m-%d %H:%M:%S"
        self.date_only_format = "%Y-%m-%d"
        
        # Ключевые слова для поиска главного офиса
        self.head_office_keywords: Set[str] = {
            "Головной",
            "Главный",
            "Центральный",
            "Приемная",
            "Основной"
        }
    
    def _parse_date(self, date_str: Any) -> Optional[datetime]:
        """Парсинг даты из строки"""
        if not date_str:
            return None
        
        if isinstance(date_str, datetime):
            return date_str
        
        try:
            # Убираем лишние пробелы и кавычки
            date_str = str(date_str).strip().strip('"').strip("'")
            
            # Пробуем разные форматы
            for fmt in [self.date_format, "%Y-%m-%d", "%d.%m.%Y %H:%M:%S"]:
                try:
                    return datetime.strptime(date_str, fmt)
                except ValueError:
                    continue
            
            # Если не удалось распарсить, возвращаем None
            return None
        except Exception as e:
            log.debug(f"Ошибка парсинга даты '{date_str}': {e}")
            return None
    
    def _get_company_status(self, company: Dict[str, Any], 
                          analysis: Dict[str, Any]) -> str:
        """
        Определение статуса компании для отчета
        """
        
        # Получаем статус из анализа
        analysis_status = analysis.get("status")
        
        if isinstance(analysis_status, ChangeType):
            if analysis_status == ChangeType.NEW_COMPANY:
                return "Новая компания"
            elif analysis_status == ChangeType.ALREADY_REPORTED:
                return "Уже была в отчёте"
            elif analysis_status == ChangeType.EDITING:
                return "Есть изменения"
            elif analysis_status == ChangeType.NO_CHANGES:
                return "Нет изменений"
            elif analysis_status == ChangeType.SITE_NOT_WORKING:
                return "Сайт не найден"
            elif analysis_status == ChangeType.NEW_DATA:
                return "Новые данные"
            elif analysis_status == ChangeType.DATA_NOT_FOUND:
                return "Данные не найдены"
        
        # Если статус не ChangeType, преобразуем строку
        status_str = str(analysis_status)
        if status_str == ChangeType.NEW_COMPANY.value or status_str == "Новая компания":
            return "Новая компания"
        elif status_str == ChangeType.ALREADY_REPORTED.value or status_str == "Уже была в отчёте":
            return "Уже была в отчёте"
        elif status_str == ChangeType.EDITING.value or status_str == "Есть изменения":
            return "Есть изменения"
        elif status_str == ChangeType.NO_CHANGES.value or status_str == "Нет изменений":
            return "Нет изменений"
        elif status_str == ChangeType.SITE_NOT_WORKING.value or status_str == "Сайт не найден":
            return "Сайт не найден"
        elif status_str == ChangeType.NEW_DATA.value or status_str == "Новые данные":
            return "Новые данные"
        elif status_str == ChangeType.DATA_NOT_FOUND.value or status_str == "Данные не найдены":
            return "Данные не найдены"
        
        return "Нет изменений"
    
    def _extract_head_office_contact(self, contacts: Any, debug_info: str = "") -> str:
        """Извлечение контакта главного офиса по ключевым словам с удалением префикса"""
        if not contacts:
            return ""
        
        debug_ctx = f" ({debug_info})" if debug_info else ""
        
        def remove_prefix(text: str) -> str:
            if not text or not isinstance(text, str):
                return text or ""
            text = text.strip()
            if ':' in text:
                parts = text.split(':', 1)
                if len(parts) > 1:
                    return parts[1].strip()
            return text
        
        if isinstance(contacts, list):
            for contact in contacts:
                if not isinstance(contact, str):
                    continue
                
                contact_str = str(contact).strip()
                if not contact_str:
                    continue
                
                for keyword in self.head_office_keywords:
                    if keyword.lower() in contact_str.lower():
                        log.debug(f"Найден контакт главного офиса по ключу '{keyword}': {contact_str[:50]}...{debug_ctx}")
                        return remove_prefix(contact_str)
            
            for contact in contacts:
                if isinstance(contact, str) and contact.strip():
                    log.debug(f"Контакт главного офиса не найден, берем первый: {contact[:50]}...{debug_ctx}")
                    return remove_prefix(contact.strip())
        
        elif isinstance(contacts, str):
            contacts_str = contacts.strip()
            if not contacts_str:
                return ""
            
            for keyword in self.head_office_keywords:
                if keyword.lower() in contacts_str.lower():
                    log.debug(f"Найден контакт главного офиса в строке по ключу '{keyword}': {contacts_str[:50]}...{debug_ctx}")
                    return remove_prefix(contacts_str)
            
            return remove_prefix(contacts_str)
        
        return ""
    
    def extract_head_office_phone(self, phones: Any) -> str:
        """Извлечение телефона главного офиса с удалением префикса"""
        return self._extract_head_office_contact(phones, debug_info="телефон")
    
    def extract_head_office_email(self, emails: Any) -> str:
        """Извлечение email с удалением префикса (без поиска по ключевым словам)"""
        if not emails:
            return ""
        
        def remove_prefix(text: str) -> str:
            if not text or not isinstance(text, str):
                return text or ""
            text = text.strip()
            if ':' in text:
                parts = text.split(':', 1)
                if len(parts) > 1:
                    return parts[1].strip()
            return text
        
        if isinstance(emails, list):
            for email in emails:
                if isinstance(email, str) and email.strip():
                    return remove_prefix(email.strip())
        elif isinstance(emails, str):
            return remove_prefix(emails.strip())
        
        return ""
    
    def extract_head_office_address(self, addresses: Any) -> str:
        """Извлечение адреса главного офиса с удалением префикса"""
        return self._extract_head_office_contact(addresses, debug_info="адрес")
    
    def add_head_office_keyword(self, keyword: str):
        """Добавление нового ключевого слова для поиска главного офиса"""
        self.head_office_keywords.add(keyword)
        log.info(f"Добавлено ключевое слово для поиска главного офиса: '{keyword}'")
    
    def set_head_office_keywords(self, keywords: Set[str]):
        """Установка нового набора ключевых слов для поиска главного офиса"""
        self.head_office_keywords = keywords
        log.info(f"Установлены ключевые слова для поиска главного офиса: {keywords}")
    
    def format_company_info(self, company: Dict[str, Any]) -> str:
        """Форматирование информации о компании для общего отчета (базовая версия)"""
        if not company:
            return ""
        
        info_parts = []
        
        name = self._get_field(company, "наименование компании")
        if name:
            info_parts.append(f"{name}")
        
        address = self._get_field(company, "адрес")
        if address:
            if isinstance(address, list):
                addr_text = "\n".join(str(addr) for addr in address if addr)
                if addr_text:
                    info_parts.append(f"Адрес:\n{addr_text}")
            elif isinstance(address, str) and address.strip():
                info_parts.append(f"Адрес:\n{address.strip()}")
        
        phones = self._get_field(company, "телефон")
        if phones:
            if isinstance(phones, list):
                phones_text = ", ".join(str(phone) for phone in phones if phone)
                if phones_text:
                    info_parts.append(f"Телефон: {phones_text}")
            elif isinstance(phones, str) and phones.strip():
                info_parts.append(f"Телефон: {phones.strip()}")
        
        emails = self._get_field(company, "E-mail")
        if emails:
            emails_text = self.extract_head_office_email(emails)
            if emails_text:
                info_parts.append(f"E-mail: {emails_text}")
        
        description = self._get_field(company, "описание")
        if description:
            info_parts.append(f"Описание: {description}")
        
        requisites = self._get_field(company, "реквизиты", {})
        if requisites:
            req_parts = []
            for key, value in requisites.items():
                if value:
                    req_parts.append(f"{key.upper()}: {value}")
            if req_parts:
                info_parts.append("Реквизиты:\n" + "\n".join(req_parts))
        
        return "\n\n".join(info_parts)
    
    def get_change_status_for_entity(self, entity_data: Dict[str, Any], 
                                   report_date: Optional[str] = None,
                                   company_data: Optional[Dict[str, Any]] = None) -> str:
        """Определение статуса изменения для сущности"""
        if not report_date:
            return ChangeType.NEW_DATA.value
        
        if company_data and company_data.get("report_date") is None:
            return ChangeType.NEW_DATA.value
        
        status = entity_data.get("status")
        if isinstance(status, ChangeType):
            return status.value
        elif isinstance(status, str):
            return status
        else:
            return ChangeType.NO_CHANGES.value
    
    def safe_str(self, value: Any) -> str:
        """Безопасное преобразование в строку"""
        if value is None:
            return ""
        if isinstance(value, list):
            if value:
                return str(value[0]) if value[0] else ""
            return ""
        return str(value)
    
    def format_supplier_full_info(self, supplier: Dict[str, Any]) -> str:
        """Форматирование полной информации о поставщике (дистрибьюторе)"""
        if not supplier:
            return ""
        
        parts = []
        
        name = self.safe_str(self._get_field(supplier, "наименование"))
        if name:
            parts.append(f"Название: {name}")
        
        region = self.safe_str(self._get_field(supplier, "регион"))
        if region:
            parts.append(f"Регион: {region}")
        
        address = self.safe_str(self._get_field(supplier, "адрес"))
        if address:
            parts.append(f"Адрес: {address}")
        
        phone = self.safe_str(self._get_field(supplier, "телефон"))
        if phone:
            parts.append(f"Телефон: {phone}")
        
        url = self.safe_str(self._get_field(supplier, "url"))
        if url:
            parts.append(f"Адрес сайта: {url}")
        
        email = self.safe_str(self._get_field(supplier, "E-mail") or self._get_field(supplier, "email"))
        if email:
            parts.append(f"E-mail: {email}")
        
        return "\n".join(parts)
    
    def _get_field(self, data: Dict[str, Any], field_name: str, default: Any = None) -> Any:
        """Получение поля из данных с учетом разных вариантов написания"""
        if not data:
            return default
        
        variants = [
            field_name.lower(),
            field_name.capitalize(),
            field_name.upper()
        ]
        
        for variant in variants:
            if variant in data:
                return data[variant]
        
        return default
    
    def extract_verification_date(self, company: Dict[str, Any]) -> str:
        """
        Извлечение даты проверки сайта
        
        Приоритет:
        1. update_date (если есть)
        2. upload_date (если нет update_date)
        """
        update_date = self._get_field(company, "update_date", "")
        
        if update_date:
            try:
                dt = self._parse_date(update_date)
                if dt:
                    return dt.strftime(self.date_only_format)
                else:
                    return str(update_date).strip().split()[0] if ' ' in str(update_date).strip() else str(update_date).strip()
            except Exception:
                return str(update_date).strip().split()[0] if ' ' in str(update_date).strip() else str(update_date).strip()
        
        upload_date = self._get_field(company, "upload_date", "")
        if upload_date:
            try:
                dt = self._parse_date(upload_date)
                if dt:
                    return dt.strftime(self.date_only_format)
                else:
                    return str(upload_date).strip().split()[0] if ' ' in str(upload_date).strip() else str(upload_date).strip()
            except Exception:
                return str(upload_date).strip().split()[0] if ' ' in str(upload_date).strip() else str(upload_date).strip()
        
        return datetime.now().strftime(self.date_only_format)
    
    def extract_upload_date(self, company: Dict[str, Any]) -> str:
        """Извлечение даты верификации (upload_date) из данных компании"""
        return self.extract_verification_date(company)
    
    def has_report_date(self, company: Dict[str, Any]) -> bool:
        """Проверка наличия report_date у компании"""
        return self._get_field(company, "report_date") is not None
    
    def get_company_representation_status(self, company: Dict[str, Any]) -> str:
        """Получение статуса представительства компании в РФ"""
        representation = self._get_field(company, "представительство в рф", "")
        return self.safe_str(representation).strip()


class GeneralReportGenerator(BaseReportGenerator):
    """Генератор общего отчета"""
    
    def __init__(self, path_formatter: PathFormatter):
        super().__init__(path_formatter)
        
        self.headers = [
            "Дата проверки сайта",
            "Наличие изменений на сайте компании производителя",
            "ID компании производителя",
            "Наименование компании производителя",
            "Адрес компании производителя",
            "Телефоны компании производителя",
            "Сайт компании производителя",
            "E-mail компании производителя",
            "Регион производства",
            "Информация о компании производителе",
            "Количество поставщиков",
            "Количество товаров",
            "Количество файлов производителя"
        ]
        
        # Список городов-миллионников России (основные)
        self.million_cities = {
            "Москва", "Санкт-Петербург", "Новосибирск", "Екатеринбург", 
            "Нижний Новгород", "Казань", "Челябинск", "Омск", "Самара", 
            "Ростов-на-Дону", "Уфа", "Красноярск", "Воронеж", "Пермь", 
            "Волгоград"
        }
        
        # Паттерны для извлечения субъектов РФ
        self.subject_patterns = [
            # Области
            (r'([А-ЯЁ][а-яё]+\s*(?:область|обл\.))', 'область'),
            # Края
            (r'([А-ЯЁ][а-яё]+\s*край)', 'край'),
            # Республики
            (r'([А-ЯЁ][а-яё]+\s*(?:республика|респ\.))', 'республика'),
            (r'(республика\s+[А-ЯЁ][а-яё]+)', 'республика'),
            # Автономные округа
            (r'([А-ЯЁ][а-яё]+\s*(?:автономный округ|АО))', 'автономный округ'),
        ]
        
        # Паттерны для извлечения городов
        self.city_patterns = [
            (r'г\.\s*([А-ЯЁ][а-яё]+(?:\s*[-\s][А-ЯЁа-яё]+)*)', 'г. '),  # г. Москва
            (r'город\s+([А-ЯЁ][а-яё]+(?:\s*[-\s][А-ЯЁа-яё]+)*)', 'г. '),  # город Москва
        ]
        
        # Приоритеты для телефонов и email (по описанию)
        self.phone_priority_map = {
            "8-800": 0,               # специальный тип, проверяется по номеру
            "приёмная": 2,
            "приемная": 2,
            "главный офис": 3,
            "головной офис": 3,
            "центральный офис": 3,
            "офис": 3,
            "отдел продаж": 4,
            "отдел сбыта": 5,
            "склад готовой продукции": 6,
            "горячая линия": 7,
        }
    
    # --------------------------------------------------------------------------
    # Вспомогательные методы для обработки телефонов и email
    # --------------------------------------------------------------------------
    
    def _normalize_phone_number(self, phone: str) -> str:
        """Нормализация номера телефона: удаляем пробелы, дефисы, скобки, оставляем цифры и знак +"""
        if not phone:
            return ""
        # Удаляем всё кроме цифр и плюса
        normalized = re.sub(r'[^\d+]', '', phone)
        return normalized
    
    def _is_8800_number(self, phone: str) -> bool:
        """Проверка, является ли номер 8-800 или 8800..."""
        normalized = self._normalize_phone_number(phone)
        # Проверяем, начинается ли с 8800 или 8 800
        if normalized.startswith('8800'):
            return True
        if normalized.startswith('+7800'):
            return True
        # Также вариант с 8-800 (дефис)
        if re.match(r'8[\s\-]*800', phone):
            return True
        return False
    
    def _split_phone_string(self, phone_str: str) -> List[str]:
        """Разбивает строку с несколькими номерами через запятую на отдельные строки"""
        if not phone_str or not isinstance(phone_str, str):
            return [phone_str] if phone_str else []
        # Если есть запятая, разбиваем
        if ',' in phone_str:
            parts = [p.strip() for p in phone_str.split(',') if p.strip()]
            return parts
        return [phone_str]
    
    def _parse_phone_entry(self, phone_entry: Any) -> Tuple[str, str]:
        """
        Извлекает описание и номер телефона из строки.
        Возвращает (description, number).
        Если нет двоеточия, description = "".
        """
        if not phone_entry:
            return "", ""
        entry_str = str(phone_entry).strip()
        if ':' in entry_str:
            parts = entry_str.split(':', 1)
            desc = parts[0].strip()
            num = parts[1].strip() if len(parts) > 1 else ""
            return desc, num
        else:
            return "", entry_str
    
    def _get_phone_priority(self, description: str, number: str) -> int:
        """
        Возвращает приоритет номера (чем меньше число, тем выше приоритет).
        Приоритет 0 - для 8-800.
        Далее по карте приоритетов.
        Если не найдено, возвращает 100.
        """
        # Проверка на 8-800
        if self._is_8800_number(number):
            return 0
        
        # Ищем в описании (регистронезависимо)
        desc_lower = description.lower()
        best_priority = 100
        for keyword, priority in self.phone_priority_map.items():
            if keyword == "8-800":
                continue  # уже обработано
            if keyword in desc_lower:
                if priority < best_priority:
                    best_priority = priority
        return best_priority
    
    def _get_all_phones_with_priority(self, phones_data: Any) -> List[Tuple[int, str, str]]:
        """
        Преобразует phones_data в список кортежей (priority, description, number).
        Учитывает, что phones_data может быть списком или строкой.
        Разбивает строки с запятыми на отдельные номера.
        """
        result = []
        
        # Приводим к списку
        if not phones_data:
            return result
        if isinstance(phones_data, str):
            phones_list = [phones_data]
        elif isinstance(phones_data, list):
            phones_list = phones_data
        else:
            return result
        
        for entry in phones_list:
            if not entry:
                continue
            # Разбиваем строку на возможные части по запятой
            sub_entries = self._split_phone_string(entry)
            for sub in sub_entries:
                if not sub:
                    continue
                desc, num = self._parse_phone_entry(sub)
                if num:
                    priority = self._get_phone_priority(desc, num)
                    result.append((priority, desc, num))
        
        # Сортируем по приоритету
        result.sort(key=lambda x: x[0])
        return result
    
    def _get_top_phones_for_column(self, phones_data: Any, max_count: int = 4) -> str:
        """
        Возвращает до max_count номеров (только номера, без описаний),
        каждый с новой строки, отсортированных по приоритету.
        """
        phones_with_priority = self._get_all_phones_with_priority(phones_data)
        top_phones = phones_with_priority[:max_count]
        numbers = [item[2] for item in top_phones]  # только номер
        return "\n".join(numbers)
    
    def _get_grouped_phones_for_info(self, phones_data: Any) -> str:
        """
        Группирует номера по описанию.
        Возвращает строки вида "Описание: номер1, номер2" (без завершающей запятой).
        Пустое описание заменяется на "Телефон".
        """
        phones_with_priority = self._get_all_phones_with_priority(phones_data)
        # Группируем по описанию
        groups: Dict[str, List[Tuple[int, str]]] = {}  # desc -> list of (priority, number)
        for priority, desc, num in phones_with_priority:
            key = desc if desc else "Телефон"
            if key not in groups:
                groups[key] = []
            groups[key].append((priority, num))
        
        result_lines = []
        for desc, items in groups.items():
            # Сортируем номера внутри группы по приоритету
            items.sort(key=lambda x: x[0])
            numbers_str = ", ".join(item[1] for item in items)
            result_lines.append(f"{desc}: {numbers_str}")
        
        return "\n".join(result_lines)
    
    def _get_all_emails_with_priority(self, emails_data: Any) -> List[Tuple[int, str]]:
        """
        Возвращает список email с приоритетом (поиск по описанию).
        Возвращает список (priority, email).
        """
        if not emails_data:
            return []
        
        # Приводим к списку
        if isinstance(emails_data, str):
            emails_list = [emails_data]
        elif isinstance(emails_data, list):
            emails_list = emails_data
        else:
            return []
        
        result = []
        for entry in emails_list:
            if not entry:
                continue
            desc, email = self._parse_phone_entry(entry)  # переиспользуем парсинг
            if not email:
                email = desc
                desc = ""
            # Приоритет для email: используем те же ключевые слова, что и для телефонов
            priority = self._get_phone_priority(desc, email)  # можно использовать тот же метод
            result.append((priority, email))
        
        # Сортируем по приоритету
        result.sort(key=lambda x: x[0])
        return result
    
    def _get_single_email_by_priority(self, emails_data: Any) -> str:
        """
        Возвращает один email с наивысшим приоритетом.
        Если ничего не найдено, возвращает пустую строку.
        """
        emails_with_priority = self._get_all_emails_with_priority(emails_data)
        if emails_with_priority:
            return emails_with_priority[0][1]
        return ""
    
    def _get_all_emails_for_info(self, emails_data: Any) -> str:
        """
        Возвращает все уникальные email (без кавычек), каждый с новой строки.
        Первая строка содержит заголовок "E-mail:".
        """
        emails_with_priority = self._get_all_emails_with_priority(emails_data)
        if not emails_with_priority:
            return ""
        
        # Уникальные email с сохранением порядка
        seen = set()
        unique_emails = []
        for _, email in emails_with_priority:
            if email not in seen:
                seen.add(email)
                unique_emails.append(email)
        
        if not unique_emails:
            return ""
        
        lines = ["E-mail:"]
        lines.extend(unique_emails)
        return "\n".join(lines)
    
    def _deduplicate_and_clean(self, value: Any) -> Any:
        """
        Очищает и удаляет дубликаты:
        - Если список: фильтрует пустые строки/None, удаляет дубликаты с сохранением порядка,
          если остался один элемент, возвращает его как строку, иначе список.
        - Если строка: strip, если пустая -> None.
        - Иначе возвращает как есть.
        """
        if value is None:
            return None
        if isinstance(value, list):
            cleaned = []
            for item in value:
                if item is None:
                    continue
                if isinstance(item, str) and item.strip() == "":
                    continue
                if isinstance(item, str):
                    cleaned.append(item.strip())
                else:
                    cleaned.append(item)
            # Удаляем дубликаты с сохранением порядка
            seen = set()
            unique = []
            for item in cleaned:
                # Для строк регистронезависимое сравнение? Пока просто точное.
                if item not in seen:
                    seen.add(item)
                    unique.append(item)
            if len(unique) == 0:
                return None
            if len(unique) == 1:
                return unique[0]
            return unique
        if isinstance(value, str):
            stripped = value.strip()
            return stripped if stripped else None
        return value
    
    # --------------------------------------------------------------------------
    # Переопределение метода format_company_info
    # --------------------------------------------------------------------------
    
    def format_company_info(self, company: Dict[str, Any]) -> str:
        """
        Форматирование информации о компании для общего отчета с учетом:
        - дедупликации всех полей
        - группировки телефонов по описанию
        - всех email (каждый с новой строки)
        - пустые поля не выводятся
        """
        if not company:
            return ""
        
        parts = []
        
        # Наименование компании
        name_raw = self._get_field(company, "наименование компании")
        name = self._deduplicate_and_clean(name_raw)
        if name and isinstance(name, str):
            parts.append(name)
        
        # Адрес
        address_raw = self._get_field(company, "адрес")
        address_cleaned = self._deduplicate_and_clean(address_raw)
        if address_cleaned:
            if isinstance(address_cleaned, list):
                addr_text = "\n".join(str(addr) for addr in address_cleaned)
            else:
                addr_text = str(address_cleaned)
            if addr_text:
                parts.append(f"Адрес:\n{addr_text}")
        
        # Телефоны (группированные)
        phones_raw = self._get_field(company, "телефон")
        phones_info = self._get_grouped_phones_for_info(phones_raw)
        if phones_info:
            parts.append(f"Телефон:\n{phones_info}")
        
        # Email (все)
        emails_raw = self._get_field(company, "E-mail")
        emails_info = self._get_all_emails_for_info(emails_raw)
        if emails_info:
            parts.append(emails_info)        
        
        # Описание
        description_raw = self._get_field(company, "описание")
        description = self._deduplicate_and_clean(description_raw)
        if description:
            if isinstance(description, list):
                desc_text = "\n".join(str(d) for d in description)
            else:
                desc_text = str(description)
            if desc_text:
                parts.append(f"Описание:\n{desc_text}")
        
        # Реквизиты
        requisites_raw = self._get_field(company, "реквизиты", {})
        if requisites_raw and isinstance(requisites_raw, dict):
            req_lines = []
            for key, value in requisites_raw.items():
                cleaned_val = self._deduplicate_and_clean(value)
                if cleaned_val:
                    if isinstance(cleaned_val, list):
                        val_str = ", ".join(str(v) for v in cleaned_val)
                    else:
                        val_str = str(cleaned_val)
                    if val_str:
                        req_lines.append(f"{key.upper()}: {val_str}")
            if req_lines:
                parts.append("Реквизиты:\n" + "\n".join(req_lines))
        
        return "\n\n".join(parts)
    
    # --------------------------------------------------------------------------
    # Метод generate_report
    # --------------------------------------------------------------------------
    
    def generate_report(self, companies_data: List[Dict[str, Any]], 
                       changes_analysis: Dict[str, Any],
                       report_date: Optional[str] = None) -> List[List[Any]]:
        """Генерация общего отчета с новой логикой статусов"""
        rows = [self.headers]
        
        for company in companies_data:
            company_id = company.get("id", "")
            if not company_id:
                continue
            
            representation_status = self.get_company_representation_status(company)
            
            if representation_status.lower() == "отсутствует":
                company_name = self.safe_str(self._get_field(company, "наименование компании"))
                website = self.safe_str(self._get_field(company, "website"))
                check_date = self.extract_verification_date(company)
                
                analysis = changes_analysis.get(company_id, {})
                has_changes = self._get_company_status(company, analysis)
                
                row = [
                    check_date,
                    has_changes,
                    company_id,
                    company_name,
                    "Представительство на территории РФ отсутствует",
                    "",  # Телефоны - пусто
                    website,  # Сайт
                    "",  # Email - пусто
                    "",  # Регион - пусто
                    "",  # Информация о компании - пусто
                    0,   # Количество поставщиков - 0
                    0,   # Количество товаров - 0
                    0    # Количество файлов производителя - 0
                ]
                rows.append(row)
                continue
            
            analysis = changes_analysis.get(company_id, {})
            
            company_name = self.safe_str(self._get_field(company, "наименование компании"))
            website = self.safe_str(self._get_field(company, "website") or self._get_field(company, "Website"))
            
            address = self._get_field(company, "адрес")
            formatted_address = self.extract_head_office_address(address)
            
            phones = self._get_field(company, "телефон", [])
            # Новый метод: до 4 номеров по приоритету, каждый с новой строки
            main_phone = self._get_top_phones_for_column(phones)
            
            emails = self._get_field(company, "E-mail", "")
            # Новый метод: один email по приоритету
            main_email = self._get_single_email_by_priority(emails)
            
            region = self._extract_region(address)
            
            # Используем переопределенный format_company_info
            company_info = self.format_company_info(company)
            
            suppliers_count = len(company.get("suppliers", []))
            products_count = len(company.get("products", []))
            
            company_files = company.get("files", [])
            company_files_count = len(company_files)
            
            has_changes = self._get_company_status(company, analysis)
            
            check_date = self.extract_verification_date(company)
            
            row = [
                check_date,
                has_changes,
                company_id,
                company_name,
                formatted_address,
                main_phone,
                website,
                main_email,
                region,
                company_info,
                suppliers_count,
                products_count,
                company_files_count
            ]
            
            rows.append(row)
        
        return rows
    
    # --------------------------------------------------------------------------
    # Вспомогательные методы для региона (оставлены без изменений)
    # --------------------------------------------------------------------------
    
    def _extract_region(self, address_data: Any) -> str:
        """Извлечение региона из адреса"""
        if not address_data:
            return ""
        
        address_text = self.extract_head_office_address(address_data)
        
        if not address_text:
            return ""
        
        log.debug(f"Извлекаем регион из адреса: {address_text}")
        
        # Сначала пытаемся найти субъект РФ (область, край, республика)
        for pattern, subject_type in self.subject_patterns:
            match = re.search(pattern, address_text, re.IGNORECASE)
            if match:
                subject = match.group(1).strip()
                # Нормализуем сокращения
                if subject_type == 'область':
                    subject = re.sub(r'\s*обл\.\s*$', ' область', subject, flags=re.IGNORECASE)
                elif subject_type == 'республика':
                    subject = re.sub(r'\s*респ\.\s*$', ' республика', subject, flags=re.IGNORECASE)
                elif subject_type == 'автономный округ':
                    subject = re.sub(r'\s*АО\s*$', ' автономный округ', subject, flags=re.IGNORECASE)
                
                log.debug(f"Найден субъект РФ: {subject}")
                return subject
        
        # Если субъект РФ не найден, ищем город
        for pattern, prefix in self.city_patterns:
            match = re.search(pattern, address_text, re.IGNORECASE)
            if match:
                city = match.group(1).strip()
                full_city_name = f"{prefix}{city}"
                
                # Проверяем, является ли город миллионником
                city_lower = city.lower()
                million_cities_lower = {c.lower() for c in self.million_cities}
                
                is_million = any(million_city in city_lower for million_city in million_cities_lower)
                
                if is_million:
                    log.debug(f"Найден город-миллионник: {full_city_name}")
                    return full_city_name
        
        # Также ищем города-миллионники без префикса "г."
        for city in self.million_cities:
            pattern = r'\b' + re.escape(city) + r'\b'
            if re.search(pattern, address_text, re.IGNORECASE):
                log.debug(f"Найден город-миллионник без префикса: {city}")
                return city
        
        log.debug(f"Регион не найден в адресе: {address_text}")
        return ""
    
    def _normalize_region_name(self, region: str) -> str:
        """Нормализация названия региона"""
        if not region:
            return region
        
        region = re.sub(r'\s+', ' ', region.strip())
        
        replacements = [
            (r'\bобл\.\b', 'область'),
            (r'\bресп\.\b', 'республика'),
            (r'\bАО\b', 'автономный округ'),
            (r'\bг\.\b', 'г. '),
        ]
        
        for pattern, replacement in replacements:
            region = re.sub(pattern, replacement, region, flags=re.IGNORECASE)
        
        return region


class DetailedReportGenerator(BaseReportGenerator):
    """Генератор детального отчета"""
    
    def __init__(self, path_formatter: PathFormatter):
        super().__init__(path_formatter)
        
        self.headers = [
            "п/п изм.",
            "ID производителя",
            "Название компании производителя",
            "ID компании поставщика",
            "Наименование компании поставщика",
            "ID товара",
            "Наименование товара",
            "ID доп.материала",
            "Наименование доп. материала",
            "Было (в нашей БД)",
            "Стало (на сайте)",
            "Тип изменения",
            "Ссылка на источник",
            "Путь в архиве к файлу"
        ]
    
    def generate_report(self, companies_data: List[Dict[str, Any]], 
                       changes_analysis: Dict[str, Any],
                       report_date: Optional[str] = None) -> List[List[Any]]:
        """Генерация детального отчета с новой логикой статусов"""
        rows = [self.headers]
        row_counter = 0
        
        for company in companies_data:
            company_id = company.get("id", "")
            company_name = self.safe_str(self._get_field(company, "наименование компании"))
            
            if not company_id:
                continue
            
            representation_status = self.get_company_representation_status(company)
            
            if representation_status.lower() == "отсутствует":
                log.info(f"Пропускаем компанию {company_id} - представительство в РФ отсутствует")
                continue
            
            analysis = changes_analysis.get(company_id, {})
            
            status = self._get_company_status(company, analysis)
            
            should_include_company = False
            if status == "Новая компания" or status == "Есть изменения":
                should_include_company = True
                log.debug(f"Включаем компанию {company_id} в детальный отчет: статус {status}")
            else:
                log.debug(f"Пропускаем компанию {company_id} в детальном отчете: статус {status}")
            
            if not should_include_company:
                log.info(f"Пропускаем компанию {company_id} в детальном отчете: не соответствует критериям (статус: {status})")
                continue
            
            company_folder = self.path_formatter.format_company_folder_path(company_id, company_name)
            
            analysis = changes_analysis.get(company_id, {})
            
            row_counter = self._add_company_rows(rows, row_counter, company_id, company_name, 
                                               company, analysis, report_date)
            
            company_files = company.get("files", [])
            for file_data in company_files:
                row_counter = self._add_company_file_rows(rows, row_counter, company_id, company_name,
                                                        company_folder, file_data, analysis)
            
            suppliers = company.get("suppliers", [])
            for supplier in suppliers:
                row_counter = self._add_supplier_rows(rows, row_counter, company_id, company_name,
                                                    company, supplier, analysis.get("suppliers", {}),
                                                    analysis)
            
            products = company.get("products", [])
            for product in products:
                row_counter = self._add_product_rows(rows, row_counter, company_id, company_name,
                                                   company_folder, product, analysis.get("products", {}),
                                                   analysis)
        
        return rows
    
    def _add_company_rows(self, rows: List[List[Any]], row_counter: int,
                         company_id: str, company_name: str,
                         company_data: Dict[str, Any],
                         analysis: Dict[str, Any], report_date: Optional[str]) -> int:
        """Добавление строк для компании"""
        company_status = analysis.get("status", ChangeType.NO_CHANGES)
        old_company = analysis.get("old_company", {})
        new_company = analysis.get("new_company", {})
        
        was_data = ""
        became_data = ""
        
        has_report_date = self.has_report_date(company_data)
        
        is_new_company = self._is_new_company(company_status)
        
        if is_new_company:
            became_data = self._format_changes_for_display(new_company, is_company=True)
            change_type = ChangeType.NEW_DATA.value
        elif isinstance(company_status, ChangeType):
            if company_status == ChangeType.EDITING:
                was_data = self._format_changes_for_display(old_company, is_company=True)
                became_data = self._format_changes_for_display(new_company, is_company=True)
                change_type = ChangeType.EDITING.value
            elif company_status == ChangeType.ALREADY_REPORTED:
                became_data = self._format_changes_for_display(new_company, is_company=True)
                change_type = ChangeType.ALREADY_REPORTED.value
            elif company_status == ChangeType.NO_CHANGES:
                became_data = self._format_changes_for_display(new_company, is_company=True)
                change_type = ChangeType.NO_CHANGES.value
            else:
                became_data = self._format_changes_for_display(new_company, is_company=True)
                change_type = company_status.value
        else:
            became_data = self._format_changes_for_display(new_company, is_company=True)
            change_type = str(company_status)
        
        row_counter += 1
        rows.append([
            row_counter,
            company_id,
            company_name,
            "",  # ID поставщика
            "",  # Наименование поставщика
            "",  # ID товара
            "",  # Наименование товара
            "",  # ID доп. материала
            "",  # Наименование доп. материала
            was_data,
            became_data,
            change_type,
            self.safe_str(self._get_field(new_company, "website") if new_company else ""),
            ""  # Путь к файлу
        ])
        
        return row_counter
    
    def _add_company_file_rows(self, rows: List[List[Any]], row_counter: int,
                              company_id: str, company_name: str,
                              company_folder: str, file_data: Dict[str, Any],
                              analysis: Dict[str, Any]) -> int:
        """Добавление строк для файла компании"""
        file_path = self.safe_str(file_data.get("path", ""))
        
        if not file_path:
            return row_counter
        
        filename = self.path_formatter.extract_filename_from_path(file_path)
        formatted_path = self.path_formatter.get_full_archive_path(company_folder, file_path)
        
        was_data = ""
        became_data = ""
        
        company_status = analysis.get("status", ChangeType.NO_CHANGES)
        is_new_company = self._is_new_company(company_status)
        
        if is_new_company:
            change_type = ChangeType.NEW_DATA.value
        else:
            company_files_analysis = analysis.get("company_files", {})
            file_id = file_data.get("id")
            if file_id and file_id in company_files_analysis:
                file_status = company_files_analysis[file_id].get("status")
                if isinstance(file_status, ChangeType):
                    change_type = file_status.value
                else:
                    change_type = str(file_status)
            else:
                change_type = ChangeType.NEW_DATA.value
        
        row_counter += 1
        rows.append([
            row_counter,
            company_id,
            company_name,
            "",  # ID поставщика
            "",  # Наименование поставщика
            "",  # ID товара
            "",  # Наименование товара
            "",  # ID доп. материала
            filename,
            was_data,
            became_data,
            change_type,
            "",  # Ссылка на источник
            formatted_path
        ])
        
        return row_counter
    
    def _add_supplier_rows(self, rows: List[List[Any]], row_counter: int,
                          company_id: str, company_name: str,
                          company_data: Dict[str, Any],
                          supplier: Dict[str, Any], suppliers_analysis: Dict[str, Any],
                          analysis: Dict[str, Any]) -> int:
        """Добавление строк для поставщика"""
        supplier_name = self.safe_str(self._get_field(supplier, "наименование"))
        
        if not supplier_name:
            return row_counter
        
        supplier_id = supplier.get("id", "")
        supplier_analysis = suppliers_analysis.get(supplier_id, {})
        status = supplier_analysis.get("status", ChangeType.NO_CHANGES)
        
        company_status = analysis.get("status", ChangeType.NO_CHANGES)
        is_new_company = self._is_new_company(company_status)
        
        was_data = ""
        became_data = ""
        
        if is_new_company:
            status = ChangeType.NEW_DATA
            became_data = self.format_supplier_full_info(supplier)
        else:
            if isinstance(status, ChangeType):
                if status == ChangeType.NEW_DATA:
                    became_data = self.format_supplier_full_info(supplier)
                elif status == ChangeType.EDITING:
                    old_data = supplier_analysis.get("old_data", {})
                    new_data = supplier_analysis.get("new_data", {})
                    was_data = self._format_supplier_changes(old_data)
                    became_data = self.format_supplier_full_info(new_data) if new_data else self.format_supplier_full_info(supplier)
                elif status == ChangeType.DATA_NOT_FOUND:
                    was_data = self._format_supplier_changes(supplier_analysis.get("old_data", {}))
                else:
                    became_data = self.format_supplier_full_info(supplier)
            else:
                became_data = self.format_supplier_full_info(supplier)
        
        change_type = status.value if isinstance(status, ChangeType) else str(status)
        
        source_link = self.safe_str(self._get_field(supplier, "url страницы") or self._get_field(supplier, "URL страницы"))
        
        row_counter += 1
        rows.append([
            row_counter,
            company_id,
            company_name,
            "",  # ID поставщика
            supplier_name,
            "",  # ID товара
            "",  # Наименование товара
            "",  # ID доп. материала
            "",  # Наименование доп. материала
            was_data,
            became_data,
            change_type,
            source_link,
            ""  # Путь к файлу
        ])
        
        return row_counter
    
    def _add_product_rows(self, rows: List[List[Any]], row_counter: int,
                         company_id: str, company_name: str,
                         company_folder: str, product: Dict[str, Any],
                         products_analysis: Dict[str, Any],
                         analysis: Dict[str, Any]) -> int:
        """Добавление строк для продукта"""
        product_name = self.safe_str(self._get_field(product, "product_name"))
        source_url = self.safe_str(product.get("source_url", ""))
        
        if not product_name:
            return row_counter
        
        product_id = product.get("id", "")
        product_analysis = products_analysis.get(product_id, {})
        
        company_status = analysis.get("status", ChangeType.NO_CHANGES)
        is_new_company = self._is_new_company(company_status)
        
        was_data = ""
        became_data = ""
        
        if is_new_company:
            status = ChangeType.NEW_DATA
        else:
            status = product_analysis.get("status", ChangeType.NO_CHANGES)
        
        change_type = status.value if isinstance(status, ChangeType) else str(status)
        
        formatted_path = self.path_formatter.get_product_card_path(company_folder, product)
        
        row_counter += 1
        rows.append([
            row_counter,
            company_id,
            company_name,
            "",  # ID поставщика
            "",  # Наименование поставщика
            "",  # ID товара
            product_name,
            "",  # ID доп. материала
            "",  # Наименование доп. материала
            was_data,
            became_data,
            change_type,
            source_url,
            formatted_path
        ])
        
        files = product.get("files", [])
        for file_data in files:
            row_counter = self._add_product_file_rows(rows, row_counter, company_id, company_name,
                                                    company_folder, file_data, product_analysis,
                                                    analysis)
        
        return row_counter
    
    def _add_product_file_rows(self, rows: List[List[Any]], row_counter: int,
                              company_id: str, company_name: str,
                              company_folder: str, file_data: Dict[str, Any],
                              product_analysis: Dict[str, Any],
                              analysis: Dict[str, Any]) -> int:
        """Добавление строк для файла продукта"""
        file_path = self.safe_str(file_data.get("path", ""))
        
        if not file_path:
            return row_counter
        
        filename = self.path_formatter.extract_filename_from_path(file_path)
        
        product_folder = self.path_formatter.get_product_files_path(company_folder, {"product_name": "", "id": ""})
        full_path = f"{product_folder}/{filename}"
        
        formatted_path = self.path_formatter.get_full_archive_path(company_folder, file_path)
        
        was_data = ""
        became_data = ""
        
        company_status = analysis.get("status", ChangeType.NO_CHANGES)
        is_new_company = self._is_new_company(company_status)
        
        if is_new_company:
            change_type = ChangeType.NEW_DATA.value
        else:
            files_analysis = product_analysis.get("files", {})
            file_id = file_data.get("id")
            if file_id and file_id in files_analysis:
                file_status = files_analysis[file_id].get("status")
                if isinstance(file_status, ChangeType):
                    change_type = file_status.value
                else:
                    change_type = str(file_status)
            else:
                change_type = ChangeType.NEW_DATA.value
        
        row_counter += 1
        rows.append([
            row_counter,
            company_id,
            company_name,
            "",  # ID поставщика
            "",  # Наименование поставщика
            "",  # ID товара
            "",  # Наименование товара
            "",  # ID доп. материала
            filename,
            was_data,
            became_data,
            change_type,
            "",  # Ссылка на источник
            formatted_path
        ])
        
        return row_counter
    
    def _format_changes_for_display(self, data: Dict[str, Any], is_company: bool = False) -> str:
        """Форматирование изменений для отображения"""
        if not data:
            return ""
        
        if is_company:
            parts = []
            
            name = self.safe_str(self._get_field(data, "наименование компании"))
            if name:
                parts.append(f"Название: {name}")
            
            address = self._get_field(data, "адрес")
            if address:
                if isinstance(address, list):
                    addr_text = "; ".join(str(a) for a in address)
                    parts.append(f"Адрес: {addr_text}")
                else:
                    parts.append(f"Адрес: {address}")
            
            website = self.safe_str(self._get_field(data, "website"))
            if website:
                parts.append(f"Сайт: {website}")
            
            return "\n".join(parts)
        else:
            return str(data)
    
    def _format_supplier_changes(self, data: Dict[str, Any]) -> str:
        """Форматирование изменений поставщика (старый метод для "Было")"""
        if not data:
            return ""
        
        if isinstance(data, dict):
            parts = []
            
            name = self.safe_str(self._get_field(data, "наименование"))
            if name:
                parts.append(f"Название: {name}")
            
            address = self.safe_str(self._get_field(data, "адрес"))
            if address:
                parts.append(f"Адрес: {address}")
            
            phone = self.safe_str(self._get_field(data, "телефон"))
            if phone:
                parts.append(f"Телефон: {phone}")
            
            return "\n".join(parts)
        else:
            return str(data)
    
    def _is_new_company(self, company_status: Any) -> bool:
        """Проверка, является ли компания новой"""
        if isinstance(company_status, ChangeType):
            return company_status == ChangeType.NEW_COMPANY
        elif isinstance(company_status, str):
            return (company_status == "Новая компания" or 
                   company_status == ChangeType.NEW_COMPANY.value)
        return False