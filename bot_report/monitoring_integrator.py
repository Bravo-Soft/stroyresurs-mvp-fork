# monitoring_integrator.py 1.1.0
import logging
from datetime import datetime
from typing import Dict, Optional, Tuple
import os
import sys
from pathlib import Path

# Добавляем пути для импорта основного пайплайна
project_root = Path("/home/user/stroy-resurs/mvp/")
sys.path.insert(0, str(project_root))

from config import Config as MainConfig
from kafka_manager import KafkaTaskManager, TaskMessage, TaskStatus
from product_utils import sanitize_company_name

log = logging.getLogger("monitoring_integrator")

class MonitoringIntegrator:
    """Интегратор для отправки задач в Kafka из бота"""
    
    def __init__(self, bot_config):
        self.bot_config = bot_config
        self.main_config = self._create_main_config()
        self.kafka_manager = None
        
    def _create_main_config(self) -> MainConfig:
        """Создание конфигурации для Kafka"""
        # Загружаем основную конфигурацию из переменных окружения
        config = MainConfig()
        
        # Корректируем пути, если нужно
        config.base_dir = str(self.bot_config.base_dir / "Base")
        config.reports_dir = str(self.bot_config.reports_dir)
        config.documents_dir = str(self.bot_config.documents_dir)
        
        # Убеждаемся, что настройки Kafka загружены
        if not config.kafka_bootstrap_servers:
            # Если не загрузились, берем из переменных окружения вручную
            import os
            config.kafka_bootstrap_servers = os.getenv('KAFKA_BOOTSTRAP_SERVERS', '192.168.0.123:9092')
            config.kafka_topic_urgent_tasks = os.getenv('KAFKA_TOPIC_URGENT_TASKS', 'urgent_tasks')
            config.kafka_topic_task_status = os.getenv('KAFKA_TOPIC_TASK_STATUS', 'task_status')
            config.kafka_consumer_group = 'telegram_bot'        
        
        return config
    
    async def initialize(self):
        """Инициализация Kafka менеджера"""
        try:
            self.kafka_manager = KafkaTaskManager(self.main_config)
            await self.kafka_manager.initialize()
            log.info("Kafka менеджер инициализирован")
        except Exception as e:
            log.error(f"Ошибка инициализации Kafka: {e}")
            self.kafka_manager = None
            raise
    
    async def send_urgent_task(self, company_data: Dict, user_id: int) -> Tuple[bool, str, Optional[str]]:
        """
        Отправка срочной задачи в Kafka
        
        Returns:
            Tuple[bool, str, Optional[str]]: (успех, сообщение, task_id)
        """
        if not self.kafka_manager:
            error_msg = "Kafka менеджер не инициализирован"
            log.error(error_msg)
            return False, error_msg, None
        
        try:
            # Подготавливаем данные компании в нужном формате
            required_fields = ['original_name', 'safe_name', 'website', 'company_id']
            for field in required_fields:
                if field not in company_data:
                    company_data[field] = ''
            
            if not company_data.get('safe_name'):
                company_data['safe_name'] = sanitize_company_name(company_data.get('original_name', ''))
            
            if not company_data.get('generated_company_id'):
                company_data['generated_company_id'] = f"company_{int(datetime.now().timestamp())}"
            
            # Отправляем задачу
            task_id = await self.kafka_manager.send_urgent_task(company_data, user_id)
            log.info(f"Urgent задача отправлена: {task_id} для пользователя {user_id}")
            
            return True, "Задача успешно отправлена в очередь", task_id
            
        except Exception as e:
            log.error(f"Ошибка отправки urgent задачи: {e}")
            return False, f"Ошибка отправки задачи: {str(e)}", None
    
    async def wait_for_task_completion(self, task_id: str, timeout: float = 14400) -> bool:
        """
        Ожидание завершения задачи (опционально, может использоваться ботом для синхронного ожидания)
        """
        if not self.kafka_manager:
            log.error("Kafka менеджер не инициализирован")
            return False
        
        try:
            return await self.kafka_manager.wait_for_task_completion(task_id, timeout)
        except Exception as e:
            log.error(f"Ошибка ожидания задачи {task_id}: {e}")
            return False

    
    async def close(self):
        """Закрытие ресурсов"""
        if self.kafka_manager:
            await self.kafka_manager.close()
            log.info("Kafka менеджер закрыт")