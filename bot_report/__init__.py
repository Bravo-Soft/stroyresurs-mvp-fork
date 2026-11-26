# bot_report/__init__.py
"""
Пакет Bot_report - Бот для работы с компаниями-производителями (версия для Пачки)

Основные модули:
- bot_config: Конфигурация бота
- bot_handlers_pachca: Обработчики команд для Пачки (PachcaBot)
- user_manager: Управление пользователями
- invite_manager: Управление инвайт-кодами
- api_client: Клиент для работы с API сервера
- excel_validator: Валидатор Excel файлов
- report_service: Сервис формирования отчетов
- monitoring_integrator: Интеграция с Kafka
- pachca_client: Клиент для API Пачки
- state_manager: Управление состояниями FSM (JSON)

Старый модуль bot_handlers (Telegram) более не используется.
"""

__version__ = "2.0.0"

# Основные классы новой версии (Пачка)
from bot_report.bot_config import BotConfig
from bot_report.bot_handlers_pachca import PachcaBot, UserStates, CallbackData
from bot_report.user_manager import UserManager, UserData
from bot_report.invite_manager import InviteManager, InviteCode, InviteStatus
from bot_report.api_client import ApiClient
from bot_report.excel_validator import ExcelValidator
from bot_report.report_service import ReportService
from bot_report.monitoring_integrator import MonitoringIntegrator
from bot_report.pachca_client import PachcaClient
from bot_report.state_manager import StateManager


__all__ = [
    # Новая версия
    'BotConfig',
    'PachcaBot',
    'UserStates',
    'CallbackData',
    'UserManager',
    'UserData',
    'InviteManager',
    'InviteCode',
    'InviteStatus',
    'ApiClient',
    'ExcelValidator',
    'ReportService',
    'MonitoringIntegrator',
    'PachcaClient',
    'StateManager',
]