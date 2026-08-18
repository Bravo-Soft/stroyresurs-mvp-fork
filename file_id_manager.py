# file_id_manager.py  1.0.0
import hashlib
import os
import re
import logging
import json
from typing import Optional, Dict, Any
import aiofiles
from datetime import datetime

log = logging.getLogger("file_id_manager")

class FileIDManager:
    """Менеджер для генерации и валидации ID файлов"""
    
    def __init__(self):
        self.processed_ids = set()
    
    async def generate_file_id(self, file_path: str, product_id: Optional[str] = None) -> str:
        """Генерация уникального ID файла на основе содержимого"""
        try:
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"Файл не существует: {file_path}")
            
            # Получаем метаданные файла
            file_stats = os.stat(file_path)
            file_size = file_stats.st_size
            
            # Читаем начало файла для хеширования (первые 64KB для баланса скорости/точности)
            chunk_size = min(65536, file_size)
            async with aiofiles.open(file_path, 'rb') as f:
                file_chunk = await f.read(chunk_size)
            
            # Создаем данные для хеширования
            hash_data = {
                'content_hash': hashlib.sha256(file_chunk).hexdigest(),
                'file_size': file_size,
                'file_name': os.path.basename(file_path)
            }
            
            # Если есть product_id, добавляем его в хеш
            if product_id:
                hash_data['product_id'] = product_id
            
            data_string = json.dumps(hash_data, sort_keys=True)
            file_hash = hashlib.sha256(data_string.encode()).hexdigest()[:28]
            
            # Формат: f_{hash}
            file_id = f"f_{file_hash}"
            
            # Валидация сгенерированного ID
            if not self.validate_file_id(file_id):
                raise ValueError(f"Сгенерирован невалидный file_id: {file_id}")
                
            return file_id
            
        except Exception as e:
            log.error(f"Ошибка генерации file_id для {file_path}: {e}")
            # Fallback - генерация на основе имени файла и размера
            fallback_data = f"{os.path.basename(file_path)}_{file_size}_{hash(file_path)}"
            return f"f_fallback_{hashlib.sha256(fallback_data.encode()).hexdigest()[:20]}"
    
    def validate_file_id(self, file_id: str) -> bool:
        """Валидация формата File ID"""
        if not file_id or not isinstance(file_id, str):
            return False
        
        # Проверяем формат: f_{28 hex chars} или f_fallback_{20 hex chars}
        pattern = r'^(f_[a-f0-9]{28}|f_fallback_[a-f0-9]{20})$'
        if not re.match(pattern, file_id):
            return False
            
        return True
    
    async def get_file_info(self, file_path: str, product_id: Optional[str] = None) -> Dict[str, Any]:
        """Получение полной информации о файле"""
        try:
            if not os.path.exists(file_path):
                return {
                    'exists': False,
                    'error': 'Файл не существует'
                }
            
            # Генерируем file_id
            file_id = await self.generate_file_id(file_path, product_id)
            
            # Получаем полный hash файла
            file_hash = await self._get_file_hash(file_path)
            
            # Получаем метаданные файла
            file_stats = os.stat(file_path)
            
            return {
                'id': file_id,
                'path': file_path,
                'hash': file_hash,
                'download_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'file_size': file_stats.st_size,
                'last_modified': datetime.fromtimestamp(file_stats.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                'exists': True
            }
            
        except Exception as e:
            log.error(f"Ошибка получения информации о файле {file_path}: {e}")
            return {
                'exists': False,
                'error': str(e)
            }
        
    async def _get_file_hash(self, file_path: str) -> str:
        """Получение SHA256 хеша всего файла"""
        sha256_hash = hashlib.sha256()
        async with aiofiles.open(file_path, 'rb') as f:
            # Читаем файл частями для больших файлов
            chunk_size = 65536
            while chunk := await f.read(chunk_size):
                sha256_hash.update(chunk)
        return sha256_hash.hexdigest()
    
    def add_processed_id(self, file_id: str):
        """Добавление ID в кэш обработанных"""
        if self.validate_file_id(file_id):
            self.processed_ids.add(file_id)
    
    def is_processed(self, file_id: str) -> bool:
        """Проверка, обработан ли уже файл с данным ID"""
        return file_id in self.processed_ids
    
    def load_existing_ids(self, existing_ids: set):
        """Загрузка существующих ID из базы данных"""
        valid_ids = {id_ for id_ in existing_ids if self.validate_file_id(id_)}
        self.processed_ids.update(valid_ids)
        log.info(f"Загружено {len(valid_ids)} существующих file_id")
    
    @staticmethod
    def get_relative_path(full_path: str, base_dir: str) -> str:
        """Получение относительного пути от базовой директории"""
        try:
            # Нормализуем пути для кроссплатформенности
            full_path = os.path.normpath(full_path)
            base_dir = os.path.normpath(base_dir)
            
            if full_path.startswith(base_dir):
                # Возвращаем путь относительно base_dir
                return os.path.relpath(full_path, base_dir)
            else:
                # Если файл не в base_dir, возвращаем полный путь
                return full_path
        except Exception as e:
            log.error(f"Ошибка получения относительного пути для {full_path}: {e}")
            return full_path