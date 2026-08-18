# product_id_manager.py
import hashlib
import json
import re
import logging
from typing import Dict, Any
from product_utils import sanitize_filename  

log = logging.getLogger("product_id_manager")

class ProductIDManager:
    """Менеджер для генерации и валидации ID товаров"""
    
    def __init__(self):
        self.processed_ids = set()
    
    def generate_product_id(self, url: str, manufacturer: str) -> str:
        """Генерация уникального ID товара на основе URL и производителя (старый метод, оставлен для совместимости)"""
        try:
            # Нормализуем данные для стабильности
            normalized_url = self._normalize_url(url)
            normalized_manufacturer = self._normalize_manufacturer(manufacturer)
            
            # Создаем стабильную строку для хеширования
            stable_data = {
                'url': normalized_url,
                'manufacturer': normalized_manufacturer
            }
            
            data_string = json.dumps(stable_data, sort_keys=True, ensure_ascii=False)
            content_hash = hashlib.sha256(data_string.encode()).hexdigest()[:28]            
            
            product_id = f"pr_{content_hash}"
            log.debug(f"ProductIDManager.generate_product_id -> {product_id} (url={url}, производитель={manufacturer})")
            
            # Валидация сгенерированного ID
            if not self.validate_product_id(product_id):
                raise ValueError(f"Сгенерирован невалидный product_id: {product_id}")
                
            return product_id
            
        except Exception as e:
            log.error(f"Ошибка генерации product_id: {e}")
            # Fallback - генерация на основе текущего времени
            fallback_string = f"{url}{manufacturer}{str(hash(url))}"
            return f"pr_fallback_{hashlib.sha256(fallback_string.encode()).hexdigest()[:20]}"
    
    def generate_product_id_from_name(self, product_name: str, manufacturer: str) -> str:
        """
        Генерация product_id на основе нормализованного имени товара (от AI) и производителя.
        Использует sanitize_filename для нормализации имени.
        """
        if not product_name or product_name == "Неизвестный товар":
            raise ValueError("Невалидное имя товара для генерации ID")
        
        # Нормализуем имя товара через sanitize_filename (уже есть в product_utils)
        normalized_name = sanitize_filename(product_name)
        if not normalized_name:
            raise ValueError("После нормализации имя товара пустое")
        
        normalized_manufacturer = self._normalize_manufacturer(manufacturer)
        
        # Строка для хеширования
        data_string = f"{normalized_name}|{normalized_manufacturer}"
        content_hash = hashlib.sha256(data_string.encode()).hexdigest()[:28]
        product_id = f"pr_{content_hash}"
        
        if not self.validate_product_id(product_id):
            raise ValueError(f"Сгенерирован невалидный product_id: {product_id}")
        
        log.debug(f"generate_product_id_from_name: {product_name} -> {product_id}")
        return product_id
    
    def generate_product_id_from_name_bag(self, product_name: str, manufacturer: str) -> str:
        """
        Генерация product_id по «мешку слов» имени товара — стабильна между прогонами.
        ТЗ разрешает менять последовательность слов имени, поэтому точный хэш имени даёт
        разные id одного товара между прогонами (в архиве накоплено ~163 таких дубля:
        перестановки слов и стиль кавычек «ёлочки»/"прямые"). Здесь слова имени
        нормализуются, сортируются (порядок не важен) и хэшируются вместе с производителем.
        """
        if not product_name or product_name == "Неизвестный товар":
            raise ValueError("Невалидное имя товара для генерации ID")

        bag = self._normalize_name_bag(product_name)
        if not bag:
            raise ValueError("После нормализации имя товара пустое")

        normalized_manufacturer = self._normalize_manufacturer(manufacturer)
        data_string = f"{bag}|{normalized_manufacturer}"
        content_hash = hashlib.sha256(data_string.encode()).hexdigest()[:28]
        product_id = f"pr_{content_hash}"

        if not self.validate_product_id(product_id):
            raise ValueError(f"Сгенерирован невалидный product_id: {product_id}")

        log.debug(f"generate_product_id_from_name_bag: {product_name} -> {product_id}")
        return product_id

    def _normalize_name_bag(self, name: str) -> str:
        """«Мешок слов» имени: нижний регистр, ё->е, ×/лат.x->кир.х (дрейф символа размера),
        токены по пробелам, краевая пунктуация токенов (кавычки, скобки, запятые, тире)
        срезается, токены сортируются. Порядок слов и стиль кавычек на результат не влияют."""
        s = name.lower().replace('ё', 'е')
        s = s.replace('×', 'х').replace('x', 'х')
        tokens = []
        for t in s.split():
            t = t.strip('«»""“”\'’()[]{},.;:!?—–-')
            if t:
                tokens.append(t)
        return ' '.join(sorted(tokens))

    def _normalize_url(self, url: str) -> str:
        """Нормализация URL для стабильности"""
        if not url:
            return ""
        
        # Приводим к нижнему регистру и удаляем протокол
        normalized = url.lower().strip()
        if '://' in normalized:
            normalized = normalized.split('://', 1)[1]
        
        # Удаляем trailing slash
        normalized = normalized.rstrip('/')
        
        return normalized
    
    def _normalize_manufacturer(self, manufacturer: str) -> str:
        """Нормализация названия производителя"""
        if not manufacturer:
            return ""
        
        # Приводим к нижнему регистру и удаляем лишние пробелы
        normalized = manufacturer.lower().strip()
        normalized = ' '.join(normalized.split())
        
        return normalized
    
    def validate_product_id(self, product_id: str) -> bool:
        """Валидация формата Product ID"""
        if not product_id or not isinstance(product_id, str):
            return False
        
        # Проверяем формат: pr_{28 hex chars}
        pattern = r'^pr_[a-f0-9]{28}$'
        if not re.match(pattern, product_id):
            return False
            
        return True
    
    def add_processed_id(self, product_id: str):
        """Добавление ID в кэш обработанных"""
        if self.validate_product_id(product_id):
            self.processed_ids.add(product_id)
    
    def is_processed(self, product_id: str) -> bool:
        """Проверка, обработан ли уже товар с данным ID"""
        return product_id in self.processed_ids
    
    def load_existing_ids(self, existing_ids: set):
        """Загрузка существующих ID из базы данных"""
        valid_ids = {id_ for id_ in existing_ids if self.validate_product_id(id_)}
        self.processed_ids.update(valid_ids)
        log.info(f"Загружено {len(valid_ids)} существующих product_id")