# config.py 1.0.0
import os
from dataclasses import dataclass
from pathlib import Path

@dataclass
class ReportConfig:
    """Конфигурация для формирования отчетов"""
    # Базовый путь
    base_dir: Path = Path('/home/user/stroy-resurs/mvp')
    logs_dir: str = '/home/user/stroy-resurs/mvp/logs'

    # Параметры Graph DB API
    graph_db_api_url: str = "http://192.168.0.123:4242"
    graph_db_request_timeout: int = 600
    
    # Параметры отчетов
    general_report_filename: str = "Таблица общего отчёта об изменениях на сайтах компаний производителей.xlsx"
    detailed_report_filename: str = "Таблица детального отчёта об изменениях на сайте компаний производителей.xlsx"
    
    # Лимиты
    max_companies_per_batch: int = 50
    max_retries: int = 3
    
    # Настройки Excel
    excel_engine_path: str = "/usr/bin/soffice"
    
    # Пути для сохранения
    output_dir: str = None
    
    def __post_init__(self):
        """Создание директорий при инициализации"""
        # Инициализируем пути
        self.output_dir = self.base_dir / 'Documents/Reports'
        
        # Создаем директории
        os.makedirs(self.output_dir, exist_ok=True)
