# kafka_manager.py
import json
import asyncio
import logging
import uuid
import os
import aiofiles
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from aiokafka import AIOKafkaProducer, AIOKafkaConsumer, TopicPartition
from config import Config

log = logging.getLogger("kafka_manager")

class TaskType(Enum):
    REGULAR = "regular"
    URGENT = "urgent"

class TaskStatus(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass
class TaskMessage:
    task_id: str
    task_type: TaskType
    company_data: Dict[str, Any]
    user_id: Optional[int] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    priority: int = 1
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type.value,
            "company_data": self.company_data,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "priority": self.priority
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TaskMessage':
        return cls(
            task_id=data["task_id"],
            task_type=TaskType(data["task_type"]),
            company_data=data["company_data"],
            user_id=data.get("user_id"),
            created_at=data.get("created_at", datetime.now().isoformat()),
            priority=data.get("priority", 1)
        )

@dataclass
class StatusMessage:
    task_id: str
    status: TaskStatus
    message: str
    progress: Optional[float] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "message": self.message,
            "progress": self.progress,
            "timestamp": self.timestamp
        }

class KafkaTaskManager:
    def __init__(self, config: Config):
        self.config = config
        self.producer: Optional[AIOKafkaProducer] = None
        self.urgent_consumer: Optional[AIOKafkaConsumer] = None
        self.status_consumer: Optional[AIOKafkaConsumer] = None
        self.last_offset_file = os.path.join(config.base_dir, 'urgent_offset.json')
        self.current_offset = None

    async def _load_offset(self) -> Optional[int]:
        """Загрузить последний сохранённый offset из файла."""
        try:
            if os.path.exists(self.last_offset_file):
                async with aiofiles.open(self.last_offset_file, 'r', encoding='utf-8') as f:
                    data = json.loads(await f.read())
                    return data.get('offset')
        except Exception as e:
            log.warning(f"Не удалось загрузить offset: {e}")
        return None

    async def _save_offset(self, offset: int):
        """Сохранить offset в файл."""
        try:
            async with aiofiles.open(self.last_offset_file, 'w', encoding='utf-8') as f:
                await f.write(json.dumps({'offset': offset}))
        except Exception as e:
            log.warning(f"Не удалось сохранить offset: {e}")

    async def initialize(self, need_urgent_consumer: bool = False, need_status_consumer: bool = False):
        """Инициализация Kafka клиентов с возможностью выборочного создания потребителей."""
        try:
            self.producer = AIOKafkaProducer(
                bootstrap_servers=self.config.kafka_bootstrap_servers,
                value_serializer=lambda v: json.dumps(v).encode('utf-8'),
                key_serializer=lambda k: k.encode('utf-8') if k else None
            )
            await self.producer.start()
            log.info("Kafka producer инициализирован")

            if need_urgent_consumer:
                self.urgent_consumer = AIOKafkaConsumer(
                    bootstrap_servers=self.config.kafka_bootstrap_servers,
                    auto_offset_reset='earliest',
                    enable_auto_commit=False,
                    session_timeout_ms=self.config.kafka_session_timeout_ms,
                    heartbeat_interval_ms=self.config.kafka_heartbeat_interval_ms,
                    max_poll_interval_ms=self.config.kafka_max_poll_interval_ms,
                    max_poll_records=1
                )
                await self.urgent_consumer.start()
                tp = TopicPartition(self.config.kafka_topic_urgent_tasks, 0)
                self.urgent_consumer.assign([tp])

                saved_offset = await self._load_offset()
                if saved_offset is not None:
                    self.urgent_consumer.seek(tp, saved_offset)
                    log.info(f"Восстановлен offset urgent consumer: {saved_offset}")
                else:
                    log.info("Сохранённый offset отсутствует, начинаем с earliest")
                log.info("Kafka urgent consumer инициализирован (ручное назначение)")

            if need_status_consumer:
                self.status_consumer = AIOKafkaConsumer(
                    self.config.kafka_topic_task_status,
                    bootstrap_servers=self.config.kafka_bootstrap_servers,
                    group_id=f"{self.config.kafka_consumer_group}_status",
                    auto_offset_reset="latest",
                    enable_auto_commit=True,
                    session_timeout_ms=self.config.kafka_session_timeout_ms,
                    heartbeat_interval_ms=self.config.kafka_heartbeat_interval_ms,
                    max_poll_interval_ms=self.config.kafka_max_poll_interval_ms
                )
                await self.status_consumer.start()
                log.info("Kafka status consumer инициализирован")

        except Exception as e:
            log.error(f"Ошибка инициализации Kafka: {e}")
            raise

    async def close(self):
        if self.producer:
            await self.producer.stop()
        if self.urgent_consumer:
            await self.urgent_consumer.stop()
        if self.status_consumer:
            await self.status_consumer.stop()
        log.info("Соединения Kafka закрыты")

    async def send_urgent_task(self, company_data: Dict[str, Any], user_id: int = None) -> str:
        task_id = f"urgent_{uuid.uuid4().hex[:8]}"
        task_message = TaskMessage(
            task_id=task_id,
            task_type=TaskType.URGENT,
            company_data=company_data,
            user_id=user_id,
            priority=10
        )
        try:
            await self.producer.send_and_wait(
                self.config.kafka_topic_urgent_tasks,
                value=task_message.to_dict(),
                key=task_id
            )
            log.info(f"Urgent задача отправлена: {task_id} для пользователя {user_id}")
            return task_id
        except Exception as e:
            log.error(f"Не удалось отправить urgent задачу: {e}")
            raise

    async def send_regular_task(self, company_data: Dict[str, Any]) -> str:
        task_id = f"regular_{uuid.uuid4().hex[:8]}"
        task_message = TaskMessage(
            task_id=task_id,
            task_type=TaskType.REGULAR,
            company_data=company_data,
            priority=1
        )
        try:
            await self.producer.send_and_wait(
                self.config.kafka_topic_regular_tasks,
                value=task_message.to_dict(),
                key=task_id
            )
            log.info(f"Regular задача отправлена: {task_id}")
            return task_id
        except Exception as e:
            log.error(f"Не удалось отправить regular задачу: {e}")
            raise

    async def send_task_status(self, task_id: str, status: TaskStatus, 
                               message: str, progress: float = None):
        status_message = StatusMessage(
            task_id=task_id,
            status=status,
            message=message,
            progress=progress
        )
        try:
            await self.producer.send_and_wait(
                self.config.kafka_topic_task_status,
                value=status_message.to_dict(),
                key=task_id
            )
            log.debug(f"Статус отправлен для задачи {task_id}: {status.value}")
        except Exception as e:
            log.error(f"Не удалось отправить статус: {e}")

    async def get_urgent_task(self) -> Optional[TaskMessage]:
        """Получение одной urgent задачи (без группы, ручное управление offset)."""
        if not self.urgent_consumer:
            log.error("Urgent consumer не инициализирован")
            return None
        try:
            msg = await asyncio.wait_for(self.urgent_consumer.getone(), timeout=2.0)
            if msg:
                task_data = json.loads(msg.value.decode('utf-8'))
                task = TaskMessage.from_dict(task_data)
                next_offset = msg.offset + 1
                await self._save_offset(next_offset)
                log.info(f"Получена urgent задача {task.task_id}, следующий offset {next_offset}")
                return task
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            log.error(f"Ошибка получения urgent задачи: {e}")
            return None
        
    async def get_next_task(self) -> Optional[TaskMessage]:
        urgent = await self.get_urgent_task()
        if urgent:
            return urgent
        # Здесь можно добавить получение regular задач, если потребуется
        return None

    async def wait_for_task_completion(self, task_id: str, timeout: float = 14400) -> bool:
        """
        Ожидание завершения задачи по статусам.
        Использует ручное назначение партиции, без группы потребителей.
        """
        consumer = None
        try:
            log.info(f"Запуск ожидания завершения задачи {task_id}, таймаут {timeout} секунд")

            consumer = AIOKafkaConsumer(
                bootstrap_servers=self.config.kafka_bootstrap_servers,
                auto_offset_reset='latest',          # начинаем читать только новые сообщения
                enable_auto_commit=False,
                session_timeout_ms=30000,
                heartbeat_interval_ms=10000,
                max_poll_interval_ms=60000,
                max_poll_records=1
            )
            await consumer.start()

            # Назначаем партицию 0 топика task_status вручную
            tp = TopicPartition(self.config.kafka_topic_task_status, 0)
            consumer.assign([tp])
            # Перемещаемся в конец очереди, чтобы читать только новые сообщения
            await consumer.seek_to_end()

            log.info(f"Потребитель для задачи {task_id} назначен на партицию 0, ожидаем статус...")

            start_time = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start_time < timeout:
                # Используем getmany с таймаутом 1 секунда (в aiokafka 0.13.0 нет poll)
                result = await consumer.getmany(timeout_ms=1000, max_records=1)
                
                for tp, messages in result.items():
                    for msg in messages:
                        try:
                            status_data = json.loads(msg.value.decode('utf-8'))
                            if status_data.get("task_id") == task_id:
                                status = TaskStatus(status_data.get("status"))
                                log.info(f"Получен статус для задачи {task_id}: {status.value} - {status_data.get('message', '')}")
                                if status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]:
                                    return status == TaskStatus.COMPLETED
                        except Exception as e:
                            log.warning(f"Ошибка обработки сообщения статуса: {e}")
                            continue
                # Небольшая пауза, чтобы не нагружать CPU
                await asyncio.sleep(0.1)

            log.warning(f"Таймаут ожидания задачи {task_id} (превышено {timeout} секунд)")
            return False

        except Exception as e:
            log.error(f"Ошибка в wait_for_task_completion для задачи {task_id}: {e}")
            return False
        finally:
            if consumer:
                await consumer.stop()
                log.info(f"Потребитель для задачи {task_id} закрыт")

    async def initialize_regular_tasks(self, companies: List[Dict[str, Any]]):
        log.info(f"Инициализация {len(companies)} regular задач")
        for company in companies:
            try:
                await self.send_regular_task(company)
            except Exception as e:
                log.error(f"Не удалось инициализировать задачу для компании {company.get('original_name')}: {e}")
        log.info("Regular задачи инициализированы")

    async def check_urgent_queue_size(self) -> int:
        return 0  # заглушка