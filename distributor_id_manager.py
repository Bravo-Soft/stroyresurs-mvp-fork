# distributor_id_manager.py
import hashlib
import json
import re
import logging
from typing import Dict, Any

log = logging.getLogger("distributor_id_manager")

class DistributorIDManager:
    """Менеджер для генерации и валидации ID дистрибьюторов"""
    
    def __init__(self):
        self.processed_ids = set()
    
    def generate_distributor_id(self, name: str, region: str = "") -> str:
        """Генерация уникального ID дистрибьютора на основе названия и региона"""
        try:
            # Нормализуем данные для стабильности
            normalized_name = self._normalize_name(name)
            normalized_region = self._normalize_region(region)
            
            # Создаем стабильную строку для хеширования
            stable_data = {
                'name': normalized_name,
                'region': normalized_region
            }
            
            data_string = json.dumps(stable_data, sort_keys=True, ensure_ascii=False)
            content_hash = hashlib.sha256(data_string.encode()).hexdigest()[:28]
            
            # Формат: d_{hash}
            distributor_id = f"d_{content_hash}"
            
            # Валидация сгенерированного ID
            if not self.validate_distributor_id(distributor_id):
                raise ValueError(f"Сгенерирован невалидный distributor_id: {distributor_id}")
                
            return distributor_id
            
        except Exception as e:
            log.error(f"Ошибка генерации distributor_id: {e}")
            # Fallback - генерация на основе текущего времени
            fallback_string = f"{name}{region}{str(hash(name))}"
            return f"d_fallback_{hashlib.sha256(fallback_string.encode()).hexdigest()[:20]}"
    
    def _normalize_name(self, name: str) -> str:
        """Нормализация названия дистрибьютора"""
        if not name:
            return ""
        
        # Приводим к нижнему регистру и удаляем лишние пробелы
        normalized = name.lower().strip()
        normalized = ' '.join(normalized.split())
        
        return normalized
    
    def _normalize_region(self, region: str) -> str:
        """Нормализация региона"""
        if not region:
            return ""
        
        # Приводим к нижнему регистру и удаляем лишние пробелы
        normalized = region.lower().strip()
        normalized = ' '.join(normalized.split())
        
        return normalized
    
    def validate_distributor_id(self, distributor_id: str) -> bool:
        """Валидация формата Distributor ID"""
        if not distributor_id or not isinstance(distributor_id, str):
            return False
        
        # Проверяем формат: d_{28 hex chars} или d_fallback_{20 hex chars}
        pattern = r'^(d_[a-f0-9]{28}|d_fallback_[a-f0-9]{20})$'
        if not re.match(pattern, distributor_id):
            return False
            
        return True
    
    def add_processed_id(self, distributor_id: str):
        """Добавление ID в кэш обработанных"""
        if self.validate_distributor_id(distributor_id):
            self.processed_ids.add(distributor_id)
    
    def is_processed(self, distributor_id: str) -> bool:
        """Проверка, обработан ли уже дистрибьютор с данным ID"""
        return distributor_id in self.processed_ids
    
    def load_existing_ids(self, existing_ids: set):
        """Загрузка существующих ID из базы данных"""
        valid_ids = {id_ for id_ in existing_ids if self.validate_distributor_id(id_)}
        self.processed_ids.update(valid_ids)
        log.info(f"Загружено {len(valid_ids)} существующих distributor_id")