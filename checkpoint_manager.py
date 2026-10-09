# checkpoint_manager.py 1.0.0
import os
import json
import logging
import aiofiles
from datetime import datetime
from typing import Dict, Any, Optional, List, Set
from pathlib import Path
from dataclasses import dataclass
from config import Config
from product_utils import sanitize_filename

log = logging.getLogger("checkpoint")

@dataclass
class CheckpointData:
    """Данные чекпоинта"""
    current_company: Dict[str, str]
    processed_urls: Set[str]
    current_stage: str
    progress: Dict[str, Any]
    timestamp: str
    checkpoint_id: str
    company_stats: Optional[Dict[str, Any]] = None
    errors: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None

class CheckpointManager:
    """Менеджер чекпоинтов"""
    
    def __init__(self, config: Config):
        self.config = config
        self.checkpoint_file = os.path.join(config.base_dir, config.checkpoint_file)
        self.checkpoint_history_dir = os.path.join(config.base_dir, "checkpoint_history")
        self._last_checkpoint_time = datetime.now()
                
        os.makedirs(self.checkpoint_history_dir, exist_ok=True)
    
    def _generate_checkpoint_id(self, company_name: str, stage: str) -> str:
        """Генерация уникального ID чекпоинта"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_company = self._sanitize_filename(company_name)
        safe_stage = self._sanitize_filename(stage)
        return f"{safe_company}_{safe_stage}_{timestamp}"
    
    async def save_checkpoint(self, 
                           company_data: Dict[str, str], 
                           processed_urls: Set[str],
                           stage: str,
                           progress: Optional[Dict[str, Any]] = None,
                           company_stats: Optional[Dict[str, Any]] = None,
                           errors: Optional[List[str]] = None,
                           metadata: Optional[Dict[str, Any]] = None) -> bool:
        """Сохранение чекпоинта"""
        try:            
            current_time = datetime.now()
            time_since_last = (current_time - self._last_checkpoint_time).total_seconds() / 60
            
            if (time_since_last < self.config.checkpoint_frequency_minutes and 
                stage not in ['company_started', 'company_completed', 'critical_stage',
                              'critical_error', 'stopped_by_admin']):
                log.debug(f"Пропускаем чекпоинт {stage} - слишком частый ({time_since_last:.1f} мин)")
                return False
            
            checkpoint_id = self._generate_checkpoint_id(company_data['original_name'], stage)
            
            checkpoint_data = CheckpointData(
                current_company=company_data,
                processed_urls=processed_urls,
                current_stage=stage,
                progress=progress or {},
                timestamp=current_time.isoformat(),
                checkpoint_id=checkpoint_id,
                company_stats=company_stats,
                errors=errors,
                metadata=metadata
            )            
            
            await self._save_main_checkpoint(checkpoint_data)            
            
            if self.config.checkpoint_keep_history:
                await self._save_to_history(checkpoint_data)
            
            self._last_checkpoint_time = current_time
            log.info(f"Сохранен чекпоинт: {stage} (ID: {checkpoint_id})")
            return True
            
        except Exception as e:
            log.error(f"Ошибка сохранения чекпоинта: {e}")
            return False
    
    async def _save_main_checkpoint(self, checkpoint_data: CheckpointData):
        """Сохранение основного файла чекпоинта"""
        data_dict = {
            'current_company': checkpoint_data.current_company,
            'processed_urls': list(checkpoint_data.processed_urls),
            'current_stage': checkpoint_data.current_stage,
            'progress': checkpoint_data.progress,
            'timestamp': checkpoint_data.timestamp,
            'checkpoint_id': checkpoint_data.checkpoint_id,
            'company_stats': checkpoint_data.company_stats,
            'errors': checkpoint_data.errors,
            'metadata': checkpoint_data.metadata,
            'version': '2.0'
        }
        
        async with aiofiles.open(self.checkpoint_file, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(data_dict, ensure_ascii=False, indent=2))
    
    async def _save_to_history(self, checkpoint_data: CheckpointData):
        """Сохранение чекпоинта в историю"""
        try:
            history_file = os.path.join(
                self.checkpoint_history_dir, 
                f"{checkpoint_data.checkpoint_id}.json"
            )
            
            data_dict = {
                'current_company': checkpoint_data.current_company,
                'processed_urls_count': len(checkpoint_data.processed_urls),
                'current_stage': checkpoint_data.current_stage,
                'progress': checkpoint_data.progress,
                'timestamp': checkpoint_data.timestamp,
                'checkpoint_id': checkpoint_data.checkpoint_id,
                'company_stats': checkpoint_data.company_stats,
                'errors': checkpoint_data.errors,
                'metadata': checkpoint_data.metadata
            }
            
            async with aiofiles.open(history_file, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(data_dict, ensure_ascii=False, indent=2))            
            
            await self._cleanup_old_checkpoints()
            
        except Exception as e:
            log.warning(f"Ошибка сохранения в историю: {e}")
    
    async def _cleanup_old_checkpoints(self):
        """Очистка старых чекпоинтов из истории"""
        try:
            history_files = []
            for file_path in Path(self.checkpoint_history_dir).glob("*.json"):
                if file_path.is_file():
                    history_files.append((file_path, file_path.stat().st_mtime))            
            
            history_files.sort(key=lambda x: x[1])            
            
            while len(history_files) > self.config.checkpoint_max_history:
                old_file, _ = history_files.pop(0)
                old_file.unlink()
                log.debug(f"Удален старый чекпоинт: {old_file.name}")
                
        except Exception as e:
            log.warning(f"Ошибка очистки истории чекпоинтов: {e}")
    
    async def load_checkpoint(self) -> Optional[CheckpointData]:
        """Загрузка чекпоинта"""
        try:
            if not os.path.exists(self.checkpoint_file):
                return None
            
            async with aiofiles.open(self.checkpoint_file, 'r', encoding='utf-8') as f:
                data = json.loads(await f.read())            
            
            version = data.get('version', '1.0')
            
            checkpoint_data = CheckpointData(
                current_company=data['current_company'],
                processed_urls=set(data['processed_urls']),
                current_stage=data['current_stage'],
                progress=data.get('progress', {}),
                timestamp=data['timestamp'],
                checkpoint_id=data['checkpoint_id'],
                company_stats=data.get('company_stats'),
                errors=data.get('errors'),
                metadata=data.get('metadata')
            )
            
            log.info(f"Загружен чекпоинт: {checkpoint_data.current_stage} "
                    f"(компания: {checkpoint_data.current_company['original_name']})")
            return checkpoint_data
            
        except Exception as e:
            log.error(f"Ошибка загрузки чекпоинта: {e}")
            return None
    
    async def clear_checkpoint(self):
        """Очистка чекпоинта"""
        try:
            if os.path.exists(self.checkpoint_file):
                os.remove(self.checkpoint_file)
                log.info("Чекпоинт очищен")
        except Exception as e:
            log.error(f"Ошибка очистки чекпоинта: {e}")
    
    async def get_checkpoint_history(self) -> List[Dict[str, Any]]:
        """Получение истории чекпоинтов"""
        history = []
        try:
            for file_path in Path(self.checkpoint_history_dir).glob("*.json"):
                async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                    data = json.loads(await f.read())
                    history.append(data)            
           
            history.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            return history
            
        except Exception as e:
            log.error(f"Ошибка получения истории чекпоинтов: {e}")
            return []
    
    async def restore_from_checkpoint(self, monitoring_system) -> bool:
        """Восстановление состояния системы из чекпоинта"""
        try:
            checkpoint_data = await self.load_checkpoint()
            if not checkpoint_data:
                return False            
            
            monitoring_system._global_processed_urls.update(checkpoint_data.processed_urls)
            
            log.info(f"Восстановлено состояние из чекпоинта: "
                    f"{checkpoint_data.current_stage}, "
                    f"обработано URL: {len(checkpoint_data.processed_urls)}")
            
            return True
            
        except Exception as e:
            log.error(f"Ошибка восстановления из чекпоинта: {e}")
            return False
    
    def _sanitize_filename(self, filename: str) -> str:
        return sanitize_filename(filename)