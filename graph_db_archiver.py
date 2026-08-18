# graph_db_archiver.py 1.0.0
import os
import json
import logging
import hashlib
from datetime import datetime
from typing import Dict, Any
from product_utils import sanitize_filename, sanitize_company_name

log = logging.getLogger("graph_db_archiver")

class GraphDBDataArchiver:
    """Класс для архивации всех данных, отправляемых в Graph DB"""
    
    def __init__(self, archive_dir: str):
        self.archive_dir = archive_dir
        self._ensure_archive_structure()
    
    def _ensure_archive_structure(self):
        """Создание структуры директорий для архива"""
        directories = [
            self.archive_dir,
            os.path.join(self.archive_dir, "companies"),
            os.path.join(self.archive_dir, "products"),
            os.path.join(self.archive_dir, "distributors"),
            os.path.join(self.archive_dir, "failed"),
            os.path.join(self.archive_dir, "logs")
        ]
        
        for dir_path in directories:
            os.makedirs(dir_path, exist_ok=True)
            log.debug(f"Создана директория архива: {dir_path}")
    
    def _generate_file_hash(self, data: Dict[str, Any]) -> str:
        """Генерация хэша данных для уникального имени файла"""
        data_str = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.md5(data_str.encode('utf-8')).hexdigest()[:12]
    
    def _sanitize_for_filename(self, text: str, max_length: int = 100) -> str:
        """Очистка текста для использования в имени файла"""
        if not text:
            return "unknown"
        
        # Используем функцию из product_utils
        sanitized = sanitize_company_name(text)
        
        # Дополнительная обработка: обрезка до нужной длины
        if len(sanitized) > max_length:
            sanitized = sanitized[:max_length].rstrip('_')
        
        return sanitized
    
    def archive_company_data(self, data: Dict[str, Any], company_id: str, 
                           company_name: str, metadata: Dict[str, Any] = None) -> str:
        """Архивация данных компании"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        file_hash = self._generate_file_hash(data)
        
        # Используем функцию очистки из product_utils
        safe_company_name = self._sanitize_for_filename(company_name)
        
        # Формируем имя файла
        filename = f"company_{company_id}_{safe_company_name}_{timestamp}_{file_hash}.json"
        filepath = os.path.join(self.archive_dir, "companies", filename)
        
        # Добавляем метаданные
        archived_data = {
            "archived_at": datetime.now().isoformat(),
            "data_type": "company",
            "company_id": company_id,
            "company_name": company_name,  # Оригинальное имя сохраняем внутри файла
            "metadata": metadata or {},
            "data": data
        }
        
        return self._save_archive_file(filepath, archived_data)
    
    def archive_products_data(self, data: Dict[str, Any], company_id: str, 
                            company_name: str, chunk_num: int, 
                            total_chunks: int, metadata: Dict[str, Any] = None) -> str:
        """Архивация данных продуктов"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        file_hash = self._generate_file_hash(data)
        
        # Определяем количество продуктов в чанке
        products_count = len(data.get("companies", [{}])[0].get("products", []))
        
        # Используем функцию очистки из product_utils
        safe_company_name = self._sanitize_for_filename(company_name)
        
        # Формируем имя файла
        filename = f"products_{company_id}_{safe_company_name}_chunk{chunk_num:03d}_of_{total_chunks:03d}_{products_count}items_{timestamp}_{file_hash}.json"
        filepath = os.path.join(self.archive_dir, "products", filename)
        
        # Добавляем метаданные
        archived_data = {
            "archived_at": datetime.now().isoformat(),
            "data_type": "products",
            "company_id": company_id,
            "company_name": company_name,  # Оригинальное имя сохраняем внутри файла
            "chunk_info": {
                "chunk_number": chunk_num,
                "total_chunks": total_chunks,
                "products_in_chunk": products_count
            },
            "metadata": metadata or {},
            "data": data
        }
        
        return self._save_archive_file(filepath, archived_data)
    
    def archive_distributors_data(self, data: Dict[str, Any], company_id: str, 
                                company_name: str, chunk_num: int, 
                                total_chunks: int, metadata: Dict[str, Any] = None) -> str:
        """Архивация данных дистрибьюторов"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        file_hash = self._generate_file_hash(data)
        
        # Определяем количество дистрибьюторов в чанке
        distributors_count = len(data.get("companies", [{}])[0].get("suppliers", []))
        
        # Используем функцию очистки из product_utils
        safe_company_name = self._sanitize_for_filename(company_name)
        
        # Формируем имя файла
        filename = f"distributors_{company_id}_{safe_company_name}_chunk{chunk_num:03d}_of_{total_chunks:03d}_{distributors_count}items_{timestamp}_{file_hash}.json"
        filepath = os.path.join(self.archive_dir, "distributors", filename)
        
        # Добавляем метаданные
        archived_data = {
            "archived_at": datetime.now().isoformat(),
            "data_type": "distributors",
            "company_id": company_id,
            "company_name": company_name,  # Оригинальное имя сохраняем внутри файла
            "chunk_info": {
                "chunk_number": chunk_num,
                "total_chunks": total_chunks,
                "distributors_in_chunk": distributors_count
            },
            "metadata": metadata or {},
            "data": data
        }
        
        return self._save_archive_file(filepath, archived_data)
    
    def archive_failed_data(self, data: Dict[str, Any], data_type: str, 
                          company_id: str, company_name: str, 
                          error_info: Dict[str, Any] = None) -> str:
        """Архивация неудачных данных"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        file_hash = self._generate_file_hash(data)
        
        # Используем функцию очистки из product_utils
        safe_company_name = self._sanitize_for_filename(company_name)
        
        # Формируем имя файла
        filename = f"failed_{data_type}_{company_id}_{safe_company_name}_{timestamp}_{file_hash}.json"
        filepath = os.path.join(self.archive_dir, "failed", filename)
        
        # Добавляем метаданные об ошибке
        archived_data = {
            "archived_at": datetime.now().isoformat(),
            "data_type": data_type,
            "status": "failed",
            "company_id": company_id,
            "company_name": company_name,  # Оригинальное имя сохраняем внутри файла
            "error_info": error_info or {},
            "data": data
        }
        
        return self._save_archive_file(filepath, archived_data)
    
    def archive_sent_data(self, data: Dict[str, Any], data_type: str,
                         company_id: str, company_name: str, 
                         response_info: Dict[str, Any] = None) -> str:
        """Архивация успешно отправленных данных"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        file_hash = self._generate_file_hash(data)
        
        # Используем функцию очистки из product_utils
        safe_company_name = self._sanitize_for_filename(company_name)
        
        # Формируем имя файла
        filename = f"sent_{data_type}_{company_id}_{safe_company_name}_{timestamp}_{file_hash}.json"
        filepath = os.path.join(self.archive_dir, data_type, filename)
        
        # Добавляем метаданные
        archived_data = {
            "archived_at": datetime.now().isoformat(),
            "data_type": data_type,
            "status": "sent",
            "company_id": company_id,
            "company_name": company_name,  # Оригинальное имя сохраняем внутри файла
            "response_info": response_info or {},
            "data": data
        }
        
        return self._save_archive_file(filepath, archived_data)
    
    def _save_archive_file(self, filepath: str, data: Dict[str, Any]) -> str:
        """Сохранение файла архива"""
        try:
            # Проверяем, что путь существует
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            
            log.debug(f"Архивировано: {os.path.basename(filepath)}")
            return filepath
        except Exception as e:
            log.error(f"Ошибка архивации данных: {e}")
            # Пробуем создать резервное имя файла
            try:
                backup_name = f"backup_{hashlib.md5(str(data).encode()).hexdigest()[:12]}.json"
                backup_path = os.path.join(self.archive_dir, "failed", backup_name)
                with open(backup_path, 'w', encoding='utf-8') as f:
                    json.dump({
                        "archived_at": datetime.now().isoformat(),
                        "original_path": filepath,
                        "error": str(e),
                        "data": data
                    }, f, ensure_ascii=False, indent=2)
                log.info(f"Создан резервный файл: {backup_name}")
                return backup_path
            except:
                return ""
    
    def log_upload_attempt(self, data_type: str, company_id: str, 
                          company_name: str, attempt_num: int, 
                          total_attempts: int, success: bool,
                          error_message: str = "", response_code: int = None):
        """Логирование попытки отправки"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        log_entry = {
            "timestamp": timestamp,
            "data_type": data_type,
            "company_id": company_id,
            "company_name": company_name,
            "attempt": attempt_num,
            "total_attempts": total_attempts,
            "success": success,
            "error_message": error_message,
            "response_code": response_code
        }
        
        # Добавляем в общий лог-файл
        log_file = os.path.join(self.archive_dir, "logs", "upload_log.jsonl")
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
        except Exception as e:
            log.error(f"Ошибка записи в лог: {e}")
        
        # Также создаем отдельный лог для каждого дня
        date_str = datetime.now().strftime('%Y-%m-%d')
        daily_log_file = os.path.join(self.archive_dir, "logs", f"upload_log_{date_str}.jsonl")
        try:
            os.makedirs(os.path.dirname(daily_log_file), exist_ok=True)
            with open(daily_log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
        except Exception as e:
            log.error(f"Ошибка записи в дневной лог: {e}")
    
    def get_archive_stats(self) -> Dict[str, Any]:
        """Получение статистики архива"""
        stats = {
            "total_archived": 0,
            "by_type": {},
            "by_company": {},
            "failed_count": 0,
            "sent_count": 0
        }
        
        try:
            # Подсчет по типам данных
            for data_type in ["companies", "products", "distributors", "failed"]:
                type_dir = os.path.join(self.archive_dir, data_type)
                if os.path.exists(type_dir):
                    count = len([f for f in os.listdir(type_dir) if f.endswith('.json')])
                    stats["by_type"][data_type] = count
                    stats["total_archived"] += count
                    
                    if data_type == "failed":
                        stats["failed_count"] = count
                    elif data_type != "failed":
                        stats["sent_count"] += count
            
            # Подсчет по компаниям
            companies = {}
            for data_type in ["companies", "products", "distributors"]:
                type_dir = os.path.join(self.archive_dir, data_type)
                if os.path.exists(type_dir):
                    for filename in os.listdir(type_dir):
                        if filename.endswith('.json'):
                            # Извлекаем ID компании из имени файла
                            parts = filename.split('_')
                            if len(parts) > 1:
                                company_id = parts[1]
                                companies[company_id] = companies.get(company_id, 0) + 1
            
            stats["by_company"] = companies
            
            # Размер архива
            total_size = 0
            for root, dirs, files in os.walk(self.archive_dir):
                for file in files:
                    if file.endswith('.json'):
                        filepath = os.path.join(root, file)
                        total_size += os.path.getsize(filepath)
            
            stats["total_size_bytes"] = total_size
            stats["total_size_mb"] = total_size / (1024 * 1024)
            
        except Exception as e:
            log.error(f"Ошибка получения статистики архива: {e}")
        
        return stats
    
    def cleanup_old_archives(self, days_to_keep: int = 30):
        """Очистка старых архивов"""
        try:
            cutoff_date = datetime.now().timestamp() - (days_to_keep * 24 * 60 * 60)
            deleted_count = 0
            
            for root, dirs, files in os.walk(self.archive_dir):
                for file in files:
                    if file.endswith('.json'):
                        filepath = os.path.join(root, file)
                        file_time = os.path.getmtime(filepath)
                        
                        if file_time < cutoff_date:
                            os.remove(filepath)
                            deleted_count += 1
                            log.info(f"Удален старый архив: {filepath}")
            
            log.info(f"Очистка архивов: удалено {deleted_count} файлов старше {days_to_keep} дней")
            return deleted_count
            
        except Exception as e:
            log.error(f"Ошибка очистки архивов: {e}")
            return 0