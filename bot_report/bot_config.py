# bot_config.py 1.2.0
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import List
import logging
from dotenv import load_dotenv

log = logging.getLogger("bot_config")

@dataclass
class BotConfig:
    """Конфигурация бота (Telegram → Pachca)"""
            
    # Базовые пути
    base_dir: Path = Path('/home/user/stroy-resurs/mvp')
    logs_dir: str = '/home/user/stroy-resurs/mvp/logs'

    # Директории системы
    reports_dir: Path = None
    documents_dir: Path = None
    archive_temp_dir: Path = None
    
    # Директории бота
    bot_dir: Path = None
    users_dir: Path = None
    temp_dir: Path = None

    # Имена отчетов
    general_report_name: str = "Таблица общего отчёта об изменениях на сайтах компаний производителей.xlsx"
    detailed_report_name: str = "Таблица детального отчёта об изменениях на сайте компаний производителей.xlsx"
    archive_name: str = "Отчёт_об_изменениях.zip"
    
    # API эндпоинты (бэкенд)
    api_base_url: str = "http://192.168.0.123:4242"
    report_upload_url: str = None
    report_delete_file_url: str = None
    report_delete_dir_url: str = None
    report_download_url: str = None
    update_url: str = None

    # Kafka настройки
    kafka_bootstrap_servers: str = "192.168.0.15:9092"
    kafka_topic_urgent_tasks: str = "urgent_tasks"
    kafka_topic_task_status: str = "task_status"
    kafka_consumer_group: str = "monitoring_pipeline"
    
    # Graph DB
    graph_db_api_url: str = None
    graph_db_request_timeout: int = 30
    
    # Ограничения
    max_archive_size_mb: int = 5000
    max_excel_size_mb: int = 10
    processing_timeout: int = 3600
    max_companies_per_report: int = 100
    min_companies_per_report: int = 1
    
    # Токен бота (Pachca access_token)
    bot_token: str = ""          # access_token Pachca
    signing_secret: str = ""     # для проверки подписи вебхуков
    
    # Список администраторов (из .env)
    admin_ids: List[int] = field(default_factory=list)
    
    # Путь к файлу инвайтов
    invites_file: Path = None

    # Webhook URL (куда Пачка будет стучаться)
    webhook_url: str = "http://80.64.17.26:5500/webhook"
    
    def __post_init__(self):
        """Инициализация путей и загрузка переменных окружения"""        
        env_path = self.base_dir / '.env'
        log.info(f"Загрузка переменных окружения из: {env_path}")
        
        if env_path.exists():
            load_dotenv(dotenv_path=env_path)
            log.info("Файл .env успешно загружен")
        else:
            log.warning(f"Файл .env не найден по пути: {env_path}")
            load_dotenv(dotenv_path=Path('.env'))
        
        self.bot_token = os.getenv("PACHKA_ACCESS_TOKEN", "")
        self.signing_secret = os.getenv("PACHKA_SIGNING_SECRET", "")
        
        # Загрузка администраторов
        admin_ids_str = os.getenv("PACHKA_ADMIN_ID", "")
        if admin_ids_str:
            try:
                self.admin_ids = [int(id.strip()) for id in admin_ids_str.split(",") if id.strip()]
                log.info(f"Загружены администраторы: {self.admin_ids}")
            except ValueError as e:
                log.error(f"Ошибка парсинга PACHKA_ADMIN_ID: {e}")
        
        # Пути
        self.reports_dir = self.base_dir / "Documents/Reports"
        self.documents_dir = self.base_dir / "Documents"
        self.archive_temp_dir = self.base_dir / "ArchiveTemp"
        
        self.bot_dir = self.base_dir / "Bot"
        self.users_dir = self.bot_dir / "Users"
        self.temp_dir = self.bot_dir / "temp"
        self.invites_file = self.bot_dir / "invites.json"
        
        # URL API бэкенда
        self.report_upload_url = f"{self.api_base_url}/api/reports/upload"
        self.report_delete_file_url = f"{self.api_base_url}/api/reports/delete/file"
        self.report_delete_dir_url = f"{self.api_base_url}/api/reports/delete/directory"
        self.graph_db_api_url = f"{self.api_base_url}/api/companies"
        self.report_download_url = "https://192.168.0.123:4243/api/reports/download"
        self.update_url = f"{self.api_base_url}/api/companies/add"
        
        # Создание директорий
        os.makedirs(self.users_dir, exist_ok=True)
        os.makedirs(self.temp_dir, exist_ok=True)
        os.makedirs(self.archive_temp_dir, exist_ok=True)
        
        if not self.bot_token:
            log.error("PACHKA_ACCESS_TOKEN не установлен!")
        else:
            log.info("Токен Пачки успешно загружен")
            
        # Kafka
        self.kafka_bootstrap_servers = os.getenv('KAFKA_BOOTSTRAP_SERVERS', self.kafka_bootstrap_servers)
        self.kafka_topic_urgent_tasks = os.getenv('KAFKA_TOPIC_URGENT_TASKS', self.kafka_topic_urgent_tasks)
        self.kafka_topic_task_status = os.getenv('KAFKA_TOPIC_TASK_STATUS', self.kafka_topic_task_status)

        # Разрешённые MIME-типы для загружаемых файлов
        allowed_mime_types: List[str] = field(default_factory=lambda: [
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        ])
        # Разрешённые расширения
        allowed_extensions: List[str] = field(default_factory=lambda: ['.xlsx'])