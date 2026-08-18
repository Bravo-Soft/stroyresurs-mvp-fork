# bot_handlers_pachca.py 1.2.0 (полностью рабочий для Пачки)

from datetime import datetime, timedelta
from typing import List, Dict, Optional
import mimetypes
import logging
import asyncio
import aiohttp
import re
import os

from bot_report.bot_config import BotConfig
from bot_report.user_manager import UserManager, UserData
from bot_report.invite_manager import InviteManager, InviteCode, InviteStatus
from bot_report.excel_validator import ExcelValidator
from bot_report.api_client import ApiClient
from bot_report.report_service import ReportService
from bot_report.monitoring_integrator import MonitoringIntegrator
from bot_report.pachca_client import PachcaClient
from bot_report.state_manager import StateManager

log = logging.getLogger("bot_handlers_pachca")

# Константы состояний
class UserStates:
    waiting_for_company_count = "waiting_for_company_count"
    waiting_for_company_list = "waiting_for_company_list"
    waiting_for_invite_code = "waiting_for_invite_code"
    processing_report = "processing_report"
    processing_company = "processing_company"
    admin_menu = "admin_menu"
    admin_create_invite = "admin_create_invite"
    admin_delete_invite = "admin_delete_invite"
    admin_view_invites = "admin_view_invites"

# Callback data константы
class CallbackData:
    GET_REPORT = "get_report"
    ADD_COMPANY = "add_company"
    ADD_COMPANY_TEXT = "add_company_text"
    HELP = "help"
    BACK_TO_MAIN = "back_to_main"
    ADMIN_PANEL = "admin_panel"
    ADMIN_CREATE_INVITE = "admin_create_invite"
    ADMIN_LIST_INVITES = "admin_list_invites"
    ADMIN_DELETE_INVITE = "admin_delete_invite"
    ADMIN_STATS = "admin_stats"
    ADMIN_USERS = "admin_users"
    ADMIN_BACK = "admin_back"
    ADMIN_CONFIRM_DELETE = "admin_confirm_delete_"
    ADMIN_CANCEL_DELETE = "admin_cancel_delete"

class PachcaBot:
    """Основной класс бота для Пачки"""
    
    def __init__(self, config: BotConfig):
        self.config = config
        self.pachca = PachcaClient(config)
        self.state_manager = StateManager(config)
        self.user_manager = UserManager(config)
        self.invite_manager = InviteManager(config)
        self.excel_validator = ExcelValidator()
        self.api_client = ApiClient(config)
        self.report_service = ReportService(config, self.api_client)
        self.monitoring_integrator = MonitoringIntegrator(config)
        
        # Словарь для хранения сообщений, которые нужно редактировать (message_id -> user_id, text)
        self.processing_messages: Dict[str, Dict] = {}
    
    def _check_admin(self, user_id: int) -> bool:
        return self.user_manager.is_admin(user_id)
    
    async def _notify_admins(self, message: str):
        """Уведомление всех администраторов"""
        admins = self.user_manager.get_admins()
        for admin in admins:
            await self.pachca.send_message(admin.user_id, f"🔔 Уведомление администратора:\n\n{message}")
    
    # ---------- Клавиатуры (возвращают список списков кнопок) ----------
    def _get_main_menu_keyboard(self, is_admin: bool = False) -> List[List[Dict]]:
        keyboard = [
            [
                {"text": "📊 Получить отчет", "callback_data": CallbackData.GET_REPORT},
                {"text": "➕ Добавить компанию", "callback_data": CallbackData.ADD_COMPANY},
            ],
            [
                {"text": "ℹ️ Помощь", "callback_data": CallbackData.HELP},
            ]
        ]
        if is_admin:
            keyboard.append([
                {"text": "⚙️ Админ-панель", "callback_data": CallbackData.ADMIN_PANEL}
            ])
        return keyboard
    
    def _get_admin_menu_keyboard(self) -> List[List[Dict]]:
        return [
            [
                {"text": "➕ Создать инвайт", "callback_data": CallbackData.ADMIN_CREATE_INVITE},
                {"text": "📋 Список инвайтов", "callback_data": CallbackData.ADMIN_LIST_INVITES},
            ],
            [
                {"text": "🗑️ Удалить инвайт", "callback_data": CallbackData.ADMIN_DELETE_INVITE},
                {"text": "📊 Статистика", "callback_data": CallbackData.ADMIN_STATS},
            ],
            [
                {"text": "👥 Пользователи", "callback_data": CallbackData.ADMIN_USERS},
            ],
            [
                {"text": "◀️ Назад", "callback_data": CallbackData.ADMIN_BACK},
            ]
        ]
    
    def _get_help_menu_keyboard(self) -> List[List[Dict]]:
        return [
            [{"text": "📁 Скачать образец файла", "callback_data": CallbackData.SAMPLE_FILE}],
            [{"text": "◀️ Назад", "callback_data": CallbackData.BACK_TO_MAIN}]
        ]
    
    def _get_back_to_main_keyboard(self, is_admin: bool = False) -> List[List[Dict]]:
        return [[{"text": "◀️ Главное меню", "callback_data": CallbackData.BACK_TO_MAIN}]]
    
    def _get_download_keyboard(self, url: str):
        return [
            [{"text": "📥 Скачать архив", "url": url}],
            [{"text": "⬅️ В главное меню", "callback_data": "back_to_main"}]
        ]

    def _get_back_to_admin_keyboard(self) -> List[List[Dict]]:
        return [[{"text": "◀️ Назад в админ-панель", "callback_data": CallbackData.ADMIN_PANEL}]]
    
    def _get_invites_list_keyboard(self, invites: List[InviteCode], page: int = 0, page_size: int = 5) -> List[List[Dict]]:
        keyboard = []
        sorted_invites = sorted(invites, key=lambda x: x.created_at, reverse=True)
        start = page * page_size
        end = start + page_size
        for invite in sorted_invites[start:end]:
            status_emoji = "🟢" if invite.status == InviteStatus.ACTIVE else "🔵" if invite.status == InviteStatus.USED else "🔴"
            expire_date = datetime.fromisoformat(invite.expires_at).strftime("%d.%m.%Y")
            button_text = f"{status_emoji} {invite.code[:8]}... (до {expire_date})"
            keyboard.append([{"text": button_text, "callback_data": f"view_invite_{invite.code}"}])
        
        nav_buttons = []
        if page > 0:
            nav_buttons.append({"text": "◀️ Назад", "callback_data": f"invites_page_{page-1}"})
        if end < len(sorted_invites):
            nav_buttons.append({"text": "Вперед ▶️", "callback_data": f"invites_page_{page+1}"})
        if nav_buttons:
            keyboard.append(nav_buttons)
        keyboard.append([{"text": "◀️ Назад", "callback_data": CallbackData.ADMIN_BACK}])
        return keyboard
    
    # ---------- Вспомогательные методы ----------
    def _normalize_download_url(self, url: str) -> str:
        try:
            if '://' not in url:
                return url
            scheme, rest = url.split('://', 1)
            host_part, path_part = rest.split('/', 1) if '/' in rest else (rest, '')
            path_parts = path_part.split('/')
            if path_parts:
                filename = path_parts[-1]
                has_cyrillic = any('\u0400' <= char <= '\u04FF' for char in filename)
                has_percent_encoding = '%' in filename
                if has_cyrillic and not has_percent_encoding:
                    import urllib.parse
                    encoded_filename = urllib.parse.quote(filename, safe='')
                    path_parts[-1] = encoded_filename
                    normalized_url = f"{scheme}://{host_part}/{'/'.join(path_parts)}"
                    return normalized_url
            return url
        except Exception as e:
            log.warning(f"Ошибка нормализации URL {url}: {e}")
            return url
    
    def _sanitize_company_name(self, name: str) -> str:
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', name)
        safe_name = re.sub(r'\s+', '_', safe_name)
        return safe_name.strip('_')[:100]
    
    def _get_company_word(self, count: int) -> str:
        if count % 10 == 1 and count % 100 != 11:
            return "компании"
        elif 2 <= count % 10 <= 4 and (count % 100 < 10 or count % 100 >= 20):
            return "компаниям"
        else:
            return "компаниям"
    
    async def _send_processing_message(self, user_id: int, text: str) -> Optional[str]:
        """Отправляет сообщение и возвращает его ID (строка)"""
        success, msg_id = await self.pachca.send_message_and_get_id(user_id, text)
        if not success:
            log.error(f"Не удалось отправить временное сообщение пользователю {user_id}")
            return None
        return msg_id
    
    # ---------- Обработка long-running задач (Kafka) ----------
    async def _handle_urgent_task_completion(self, task_id: str, user_id: int, chat_id: int,
                                       company_data: Dict, company_data_kafka: Dict,
                                       processing_msg_id: str, is_admin: bool):
        """Фоновая обработка завершения urgent задачи"""
        try:
            log.info(f"Начало отслеживания задачи {task_id} для пользователя {user_id}")

            await self.pachca.edit_message(
                chat_id, processing_msg_id,
                f"🔄 <b>Отслеживание задачи {task_id}</b>\n\n🏢 Компания: {company_data.get('Наименование')}\n⏱️ Ожидание завершения обработки...",
                reply_markup=self._get_back_to_main_keyboard(is_admin)
            )

            task_completed = await self.monitoring_integrator.wait_for_task_completion(task_id, timeout=14400)

            if not task_completed:
                await self.pachca.edit_message(
                    chat_id, processing_msg_id,
                    f"❌ <b>Задача не завершена в установленный срок</b>\n\n🆔 ID задачи: <code>{task_id}</code>\n🏢 Компания: {company_data.get('Наименование')}\n\n<i>Обратитесь к администратору.</i>",
                    reply_markup=self._get_back_to_main_keyboard(is_admin)
                )
                return

            await self.pachca.edit_message(chat_id, processing_msg_id,
                f"✅ <b>Задача завершена успешно</b>\n\n🏢 Компания: {company_data.get('Наименование')}\n🔄 Подготовка отчета...",
                reply_markup=None)
            await asyncio.sleep(5)

            company_id = company_data_kafka.get('company_id') or company_data.get('ID производителя')
            log.info(f"Генерация отчетов для компании {company_id}")

            report_success, report_message = await self.report_service.generate_reports_for_companies([company_id])
            if not report_success:
                await self.pachca.send_message(user_id,
                    f"⚠️ <b>Обработка завершена, но отчет не сгенерирован</b>\n\n🏢 {company_data.get('Наименование')}\n<i>{report_message}</i>",
                    reply_markup=self._get_back_to_main_keyboard(is_admin))
                return

            date_str = datetime.now().strftime("%d_%m_%Y")
            user_id_str = str(user_id).zfill(4)

            await self.api_client.delete_user_directory(date_str, user_id_str)
            archive_success, archive_message, archive_path = await self.report_service.create_archive_for_user(user_id_str, date_str)
            if not archive_success:
                await self.pachca.send_message(user_id,
                    f"⚠️ <b>Обработка завершена, но архив не создан</b>\n\n{archive_message}",
                    reply_markup=self._get_back_to_main_keyboard(is_admin))
                return

            upload_success, upload_message, download_url = await self.api_client.upload_archive(archive_path, date_str, user_id_str)
            if not upload_success:
                await self.pachca.send_message(user_id,
                    f"⚠️ <b>Обработка завершена, но загрузка не удалась</b>\n\n{upload_message}",
                    reply_markup=self._get_back_to_main_keyboard(is_admin))
                return

            try:
                current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                await self.api_client.update_company_report_date(company_id, current_date)
            except Exception as e:
                log.error(f"Ошибка обновления report_date: {e}")

            normalized_url = self._normalize_download_url(download_url)

            success_text = (f"✅ <b>Компания успешно обработана</b>\n\n"
                            f"🏢 <b>{company_data.get('Наименование')}</b>\n"
                            f"🌐 {company_data.get('Website', '')}\n"
                            f"🆔 ID компании: <code>{company_id}</code>\n\n"
                            f"📥 <b>Скачать архив, нажав на кнопку ниже.</b>\n"
                            f"🕒 <i>Ссылка действительна до следующего запроса.</i>")

            await self.pachca.send_message(user_id, success_text,
                                        reply_markup=self._get_download_keyboard(normalized_url))

            self.user_manager.update_last_company_update_date(user_id, datetime.now().isoformat())
            log.info(f"Urgent задача {task_id} полностью обработана")

        except Exception as e:
            log.error(f"Ошибка в фоновой обработке: {e}")
            try:
                await self.pachca.send_message(user_id,
                    f"❌ <b>Ошибка обработки задачи</b>\n\n{str(e)}",
                    reply_markup=self._get_back_to_main_keyboard(is_admin))
            except:
                pass
    
    # ---------- Обработка команд и callback'ов ----------
    async def handle_message(self, user_id: int, text: str, message_id: str):
        """Обработка текстового сообщения"""
        log.info(f"handle_message called: user={user_id}, text='{text}'")
        
        # Обрабатываем команду /start
        if text and text.strip().lower() in ('/start', 'start'):
            await self._cmd_start(user_id, message_id)
            return
        
        # Проверяем регистрацию и инвайт
        user = self.user_manager.load_user(user_id)
        state = self.state_manager.get_state(user_id)
        
        # Если не зарегистрирован и не ждёт инвайт
        if (not user or not user.registered_via_invite) and state != UserStates.waiting_for_invite_code:
            await self.pachca.send_message(user_id,
                f"👋 Добро пожаловать! Для доступа введите инвайт-код.")
            self.state_manager.set_state(user_id, UserStates.waiting_for_invite_code)
            return
        
        # Обработка ввода инвайт-кода
        if state == UserStates.waiting_for_invite_code:
            await self._process_invite_code(user_id, text, message_id)
            return
        
        # Обработка ввода текстового списка компаний (добавление новых компаний)
        if state == UserStates.waiting_for_company_list:
            await self._process_company_list(user_id, text, message_id)
            return

        # Обработка ввода количества компаний
        if state == UserStates.waiting_for_company_count:
            await self._process_company_count_input(user_id, text, message_id)
            return
        
        # Обработка ввода инвайта для удаления (админ)
        if state == UserStates.admin_delete_invite:
            await self._process_admin_delete_invite_code(user_id, text, message_id)
            return
        
        # Остальные текстовые сообщения
        await self.pachca.send_message(user_id,
            "Используйте кнопки для взаимодействия с ботом.",
            reply_markup=self._get_main_menu_keyboard(is_admin=self._check_admin(user_id)))
    
    async def handle_callback(self, user_id: int, callback_data: str, message_id: str):
        """Обработка нажатий на inline-кнопки"""
        log.info(f"Callback from {user_id}: {callback_data}")
        
        if callback_data.startswith("view_invite_"):
            await self._handle_view_invite(user_id, callback_data, message_id)
        elif callback_data.startswith("invites_page_"):
            await self._handle_invites_pagination(user_id, callback_data, message_id)
        elif callback_data.startswith(CallbackData.ADMIN_CONFIRM_DELETE):
            await self._handle_confirm_delete_invite(user_id, callback_data, message_id)
        else:
            handler_map = {
                CallbackData.GET_REPORT: self._handle_get_report,
                CallbackData.ADD_COMPANY: self._handle_add_company,
                CallbackData.HELP: self._handle_help,
                CallbackData.BACK_TO_MAIN: self._handle_back_to_main,
                CallbackData.ADMIN_PANEL: self._handle_admin_panel,
                CallbackData.ADMIN_CREATE_INVITE: self._handle_admin_create_invite,
                CallbackData.ADMIN_LIST_INVITES: self._handle_admin_list_invites,
                CallbackData.ADMIN_DELETE_INVITE: self._handle_admin_delete_invite,
                CallbackData.ADMIN_STATS: self._handle_admin_stats,
                CallbackData.ADMIN_USERS: self._handle_admin_users,
                CallbackData.ADMIN_BACK: self._handle_admin_back,
                CallbackData.ADMIN_CANCEL_DELETE: self._handle_cancel_delete,
            }
            handler = handler_map.get(callback_data)
            if handler:
                await handler(user_id, message_id)
            else:
                log.warning(f"Неизвестный callback: {callback_data}")
    
    async def handle_document(self, user_id: int, file_id: str, file_name: str, message_id: str, file_url: Optional[str] = None):
        """Обработка полученного документа (Excel файл)"""
        state = self.state_manager.get_state(user_id)
        if state != UserStates.waiting_for_excel_file:
            await self.pachca.send_message(user_id, "❌ Сначала нажмите кнопку '➕ Добавить компанию'.")
            return

        if not file_name.lower().endswith('.xlsx'):
            await self.pachca.send_message(user_id, "❌ Неверный формат файла. Ожидается .xlsx")
            return

        # Получаем URL файла (либо из параметра, либо через API)
        download_url = file_url
        if not download_url:
            file_info = await self.pachca.get_file_info(file_id)
            if not file_info:
                await self.pachca.send_message(user_id, "❌ Не удалось получить информацию о файле.")
                return
            download_url = file_info.get('url')
            if not download_url:
                await self.pachca.send_message(user_id, "❌ Не удалось получить URL файла.")
                return

        # Сохраняем временно
        temp_file = self.config.temp_dir / f"user_{user_id}_{datetime.now().timestamp()}.xlsx"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(download_url) as resp:
                    if resp.status == 200:
                        with open(temp_file, 'wb') as f:
                            f.write(await resp.read())
                    else:
                        await self.pachca.send_message(user_id, f"❌ Ошибка загрузки файла: статус {resp.status}")
                        return
        except Exception as e:
            log.error(f"Ошибка загрузки файла: {e}")
            await self.pachca.send_message(user_id, "❌ Ошибка при загрузке файла.")
            return

        # Валидация содержимого
        processing_msg = await self._send_processing_message(user_id, "🔄 Проверка файла...")
        if not processing_msg:
            await self.pachca.send_message(user_id, "❌ Не удалось отправить сообщение о проверке.")
            temp_file.unlink(missing_ok=True)
            return

        success, validation_msg, df = self.excel_validator.validate_file(temp_file)
        if not success:
            await self.pachca.edit_message(user_id, processing_msg, f"❌ {validation_msg}")
            temp_file.unlink(missing_ok=True)
            self.state_manager.clear(user_id)
            return

        # Дальнейшая обработка 
        await self.pachca.edit_message(user_id, processing_msg, "✅ Файл проверен\n🔄 Извлечение данных компании...")
        companies_data = self.excel_validator.extract_company_data(df)
        if not companies_data:
            await self.pachca.edit_message(user_id, processing_msg, "❌ Не удалось извлечь данные компании")
            temp_file.unlink(missing_ok=True)
            self.state_manager.clear(user_id)
            return

        company_data = companies_data[0]
        company_data_for_kafka = {
            'original_name': company_data.get('Наименование', ''),
            'safe_name': self._sanitize_company_name(company_data.get('Наименование', '')),
            'website': company_data.get('Website', ''),
            'company_id': str(company_data.get('ID производителя', '')),
            'generated_company_id': f"company_{int(datetime.now().timestamp())}",
            'folder_name': self._sanitize_company_name(company_data.get('Наименование', '')),
            'original_website': company_data.get('Website', '')
        }

        if not self.monitoring_integrator or not self.monitoring_integrator.kafka_manager:
            await self.pachca.edit_message(user_id, processing_msg, "❌ Система обработки временно недоступна.")
            temp_file.unlink(missing_ok=True)
            self.state_manager.clear(user_id)
            return

        success, msg, task_id = await self.monitoring_integrator.send_urgent_task(company_data_for_kafka, user_id)
        if not success or not task_id:
            await self.pachca.edit_message(user_id, processing_msg, f"❌ Ошибка отправки задачи: {msg}")
            temp_file.unlink(missing_ok=True)
            self.state_manager.clear(user_id)
            return

        await self.pachca.edit_message(user_id, processing_msg,
            f"✅ Задача принята в обработку\n🆔 ID задачи: <code>{task_id}</code>\n⏱️ Ожидание очереди...")

        self.user_manager.increment_companies(user_id)
        log.info(f"Urgent задача отправлена: {task_id}")

        asyncio.create_task(
            self._handle_urgent_task_completion(
                task_id=task_id, user_id=user_id, chat_id=user_id,
                company_data=company_data, company_data_kafka=company_data_for_kafka,
                processing_msg_id=processing_msg, is_admin=self._check_admin(user_id)
            )
        )

        await self.pachca.edit_message(user_id, processing_msg,
            f"✅ <b>Задача принята в обработку</b>\n\n🏢 <b>{company_data.get('Наименование')}</b>\n🌐 {company_data.get('Website')}\n🆔 ID задачи: <code>{task_id}</code>\n\n<i>Вы получите уведомление по завершении.</i>",
            reply_markup=self._get_back_to_main_keyboard(self._check_admin(user_id)))

        temp_file.unlink(missing_ok=True)
        self.state_manager.clear(user_id)
    
    # ---------- Реализация обработчиков ----------
    async def _cmd_start(self, user_id: int, message_id: str):
        is_admin = self._check_admin(user_id)
        if is_admin:
            user = self.user_manager.create_or_update_user(user_id, None, None, None)
            user.registered_via_invite = True
            user.is_admin = True
            if not user.admin_since:
                user.admin_since = datetime.now().isoformat()
            self.user_manager.save_user(user)
            await self.pachca.send_message(user_id,
                f"👑 <b>Добро пожаловать, администратор!</b>\n🆔 Ваш ID: <code>{user_id}</code>",
                reply_markup=self._get_main_menu_keyboard(True))  
        else:
            user = self.user_manager.load_user(user_id)
            if user and user.registered_via_invite:
                await self.pachca.send_message(user_id,
                    f"👋 С возвращением!\n🆔 ID: <code>{user_id}</code>",
                    reply_markup=self._get_main_menu_keyboard(False))
            else:
                await self.pachca.send_message(user_id,
                    "👋 Добро пожаловать! Пожалуйста, введите инвайт-код:")  
                self.state_manager.set_state(user_id, UserStates.waiting_for_invite_code)
    
    async def _process_invite_code(self, user_id: int, code: str, message_id: str):
        log.info(f"Запрос инвайт кода для: user={user_id}")
        success, msg, invite = self.invite_manager.validate_invite(code.strip().upper())
        if not success:
            await self.pachca.send_message(user_id, f"❌ {msg}\nПопробуйте снова:")
            return
        success, user = self.user_manager.register_user_with_invite(user_id, None, None, None, code)
        if not success:
            await self.pachca.send_message(user_id, "❌ Ошибка регистрации. Обратитесь к администратору.")
            self.state_manager.clear(user_id)
            return
        self.invite_manager.use_invite(code, user_id, None)
        await self._notify_admins(f"👤 Новый пользователь зарегистрирован\nID: {user_id}\nИнвайт: {code}")
        await self.pachca.send_message(user_id,
            f"✅ Регистрация успешна!\nДобро пожаловать!",
             reply_markup=self._get_main_menu_keyboard(self._check_admin(user_id)))
        self.state_manager.clear(user_id)
    
    async def _process_company_count_input(self, user_id: int, text: str, message_id: str):
        try:
            count = int(text.strip())
            if count < 1 or count > 100:
                await self.pachca.send_message(user_id, "❌ Введите число от 1 до 100:")
                return
            self.state_manager.update_data(user_id, {"company_count": count})
            processing_msg_id = await self._send_processing_message(user_id, f"🔄 Формирование отчета по {count} компаниям...")
            if not processing_msg_id:
                await self.pachca.send_message(user_id, "❌ Не удалось начать формирование отчета.")
                self.state_manager.clear(user_id)
                return
            self.state_manager.set_state(user_id, UserStates.processing_report)
            
            date_str = datetime.now().strftime("%d_%m_%Y")
            user_id_str = str(user_id).zfill(4)
            
            await self.api_client.delete_user_directory(date_str, user_id_str)
            success, report_msg, company_ids = await self.report_service.generate_ranked_reports(limit=count)
            if not success:
                await self.pachca.edit_message(user_id, processing_msg_id, f"❌ {report_msg}")
                self.state_manager.clear(user_id)
                return
            
            await self.pachca.edit_message(user_id, processing_msg_id, "✅ Отчеты сгенерированы\n🔄 Создание архива...")
            success, archive_msg, archive_path = await self.report_service.create_archive_for_user(user_id_str, date_str)
            if not success:
                await self.pachca.edit_message(user_id, processing_msg_id, f"❌ {archive_msg}")
                self.state_manager.clear(user_id)
                return
            
            await self.pachca.edit_message(user_id, processing_msg_id, "✅ Архив создан\n🔄 Загрузка на сервер...")
            success, upload_msg, download_url = await self.api_client.upload_archive(archive_path, date_str, user_id_str)
            if not success:
                await self.pachca.edit_message(user_id, processing_msg_id, f"❌ {upload_msg}")
                self.state_manager.clear(user_id)
                return
            
            self.user_manager.increment_reports(user_id)
            response_text = (f"✅ Отчёт по {count} {self._get_company_word(count)} сформирован.\n\n"
                             f"📥 Скачать архив, нажав на кнопку ниже.\n\n"
                             f"🕒 Ссылка действительна до следующего запроса.")
            await self.pachca.edit_message(user_id, processing_msg_id,
                                            "✅ Архив загружен на сервер.\n📎 Готовлю ссылку...")
            
            await self.pachca.send_message(user_id, response_text,
                                            reply_markup=self._get_download_keyboard(download_url))
                                                    
            asyncio.create_task(self._update_report_dates_after_success(user_id, company_ids))
            self.state_manager.clear(user_id)
        except Exception as e:
            log.error(f"Ошибка ввода количества: {e}")
            await self.pachca.send_message(user_id, "❌ Ошибка. Попробуйте снова.")
            self.state_manager.clear(user_id)
    
    async def _update_report_dates_after_success(self, user_id: int, company_ids: List[str]):
        if not company_ids:
            return
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        success, msg = await self.api_client.update_companies_report_dates(company_ids, current_date)
        if success:
            self.user_manager.update_last_report_date(user_id, current_date)
    
    async def _process_admin_delete_invite_code(self, user_id: int, code: str, message_id: str):
        """Обработка ввода кода инвайта для удаления"""
        if self.invite_manager.delete_invite(code.strip().upper()):
            await self.pachca.send_message(user_id, f"✅ Инвайт {code} удален", reply_markup=self._get_back_to_admin_keyboard())
            await self._notify_admins(f"Инвайт {code} удален администратором {user_id}")
        else:
            await self.pachca.send_message(user_id, "❌ Инвайт не найден или ошибка удаления", reply_markup=self._get_back_to_admin_keyboard())
        self.state_manager.clear(user_id)
    
    async def _handle_get_report(self, user_id: int, message_id: str):
        user = self.user_manager.load_user(user_id)
        if not user or not user.registered_via_invite:
            await self.pachca.send_message(user_id, "⛔ Требуется регистрация")
            return
        self.user_manager.increment_requests(user_id)
        await self.pachca.send_message(user_id, "📊 Введите количество компаний (1-100):")
        self.state_manager.set_state(user_id, UserStates.waiting_for_company_count)
    
    async def _handle_add_company(self, user_id: int, message_id: str):
        user = self.user_manager.load_user(user_id)
        if not user or not user.registered_via_invite:
            await self.pachca.send_message(user_id, "⛔ Требуется регистрация")
            return
        self.user_manager.increment_requests(user_id)
        await self.pachca.send_message(user_id,
            "✏️ Отправьте список компаний в формате:\n\n"
            "ID компании\nНазвание\nСайт\n\n"
            "ID2\nНазвание2\nСайт2\n\n"
            "(Компании разделяйте одной пустой строкой. Максимум 50 компаний)",
            reply_markup=self._get_back_to_main_keyboard(self._check_admin(user_id)))
        self.state_manager.set_state(user_id, UserStates.waiting_for_company_list)
    
    async def _process_company_list(self, user_id: int, text: str, message_id: str):
        """Обработка текстового списка компаний"""
        # Разбиваем на непустые строки
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) % 3 != 0:
            await self.pachca.send_message(user_id, 
                "❌ Неверный формат. Количество строк должно быть кратно 3 (ID, Название, URL). "
                "Убедитесь, что каждая компания занимает 3 строки.")
            return

        # Группируем по три
        raw_companies = []
        for i in range(0, len(lines), 3):
            company_id = lines[i]
            name = lines[i+1]
            url = lines[i+2]
            raw_companies.append((company_id, name, url))

        # Проверка на дубликаты ID в рамках запроса
        seen_ids = set()
        unique_companies = []
        for company_id, name, url in raw_companies:
            if company_id in seen_ids:
                await self.pachca.send_message(user_id, f"⚠️ Пропущен дубликат ID: {company_id} (название: {name})")
                continue
            seen_ids.add(company_id)
            unique_companies.append((company_id, name, url))

        if len(unique_companies) > 50:
            await self.pachca.send_message(user_id, f"❌ Слишком много компаний: {len(unique_companies)}. Максимум 50.")
            return

        if not unique_companies:
            await self.pachca.send_message(user_id, "❌ Нет корректных компаний для обработки.")
            return

        # Валидация URL
        validated_companies = []
        for company_id, name, url in unique_companies:
            if not (url.startswith('http://') or url.startswith('https://')):
                await self.pachca.send_message(user_id, f"❌ Неверный URL для компании '{name}': {url} (пропущена)")
                continue
            validated_companies.append((company_id, name, url))

        if not validated_companies:
            await self.pachca.send_message(user_id, "❌ Нет компаний с корректными URL.")
            return

        accepted = 0
        errors = 0
        # Для каждой компании отправляем задачу
        for company_id, name, url in validated_companies:
            # Формируем company_data (с русскими ключами как в Excel)
            safe_name = self._sanitize_company_name(name)
            company_data = {
                'Наименование': name,
                'Website': url,
                'ID производителя': company_id
            }
            company_data_for_kafka = {
                'original_name': name,
                'safe_name': safe_name,
                'website': url,
                'company_id': company_id,
                'generated_company_id': f"company_{int(datetime.now().timestamp())}_{company_id}",
                'folder_name': safe_name,
                'original_website': url
            }

            # Отправка в Kafka
            success, msg, task_id = await self.monitoring_integrator.send_urgent_task(company_data_for_kafka, user_id)
            if not success or not task_id:
                errors += 1
                await self.pachca.send_message(user_id, f"❌ Ошибка отправки задачи для {name}: {msg}")
                continue

            accepted += 1
            # Отправляем сообщение о начале обработки
            processing_msg_id = await self._send_processing_message(user_id, f"🔄 Отправлена задача для {name} (ID: {task_id})")
            if processing_msg_id:
                asyncio.create_task(
                    self._handle_urgent_task_completion(
                        task_id=task_id,
                        user_id=user_id,
                        chat_id=user_id,
                        company_data=company_data,
                        company_data_kafka=company_data_for_kafka,
                        processing_msg_id=processing_msg_id,
                        is_admin=self._check_admin(user_id)
                    )
                )
            else:
                errors += 1
                await self.pachca.send_message(user_id, f"⚠️ Задача для {name} принята, но не удалось создать сообщение о прогрессе.")

        # Сводка
        summary = f"✅ Принято в обработку: {accepted}\n❌ Ошибок: {errors}"
        await self.pachca.send_message(user_id, summary, reply_markup=self._get_main_menu_keyboard(self._check_admin(user_id)))
        self.state_manager.clear(user_id)
        
    async def _handle_help(self, user_id: int, message_id: str):
        help_text = ("📋 Команды:\n📊 Получить отчет\n➕ Добавить компанию\n\nПри проблемах обратитесь к администратору.")
        await self.pachca.send_message(user_id, help_text, reply_markup=self._get_help_menu_keyboard())
    
    async def _handle_back_to_main(self, user_id: int, message_id: str):
        user = self.user_manager.load_user(user_id)
        is_admin = self._check_admin(user_id) if user else False
        await self.pachca.send_message(user_id, "Главное меню.", reply_markup=self._get_main_menu_keyboard(is_admin))
    
    async def _handle_admin_panel(self, user_id: int, message_id: str):
        if not self._check_admin(user_id):
            await self.pachca.send_message(user_id, "⛔ Нет прав.")
            return
        await self.pachca.send_message(user_id, "👑 Админ-панель", reply_markup=self._get_admin_menu_keyboard())
    
    async def _handle_admin_create_invite(self, user_id: int, message_id: str):
        if not self._check_admin(user_id):
            return
        success, msg, invite_code = self.invite_manager.create_invite(user_id, None, days_valid=30)
        if success:
            await self.pachca.send_message(user_id,
                f"✅ Инвайт создан: <code>{invite_code}</code>\nДействителен 30 дней.",
                 reply_markup=self._get_back_to_admin_keyboard())
            await self._notify_admins(f"Создан инвайт {invite_code} администратором {user_id}")
        else:
            await self.pachca.send_message(user_id, f"❌ {msg}")
    
    async def _handle_admin_list_invites(self, user_id: int, message_id: str):
        if not self._check_admin(user_id):
            return
        invites = self.invite_manager.get_all_invites()
        if not invites:
            await self.pachca.send_message(user_id, "Нет инвайтов", reply_markup=self._get_back_to_admin_keyboard())
        else:
            stats = self.invite_manager.get_statistics()
            summary = f"📋 Всего: {stats['total']} | 🟢 {stats['active']} | 🔵 {stats['used']} | 🔴 {stats['expired']}\n\nВыберите инвайт:"
            await self.pachca.send_message(user_id, summary, reply_markup=self._get_invites_list_keyboard(invites))
    
    async def _handle_admin_delete_invite(self, user_id: int, message_id: str):
        if not self._check_admin(user_id):
            return
        await self.pachca.send_message(user_id, "Введите код инвайта для удаления:")
        self.state_manager.set_state(user_id, UserStates.admin_delete_invite)
    
    async def _handle_admin_stats(self, user_id: int, message_id: str):
        if not self._check_admin(user_id):
            return
        invite_stats = self.invite_manager.get_statistics()
        user_stats = self.user_manager.get_statistics()
        text = (f"📊 Статистика\n👥 Пользователи: всего {user_stats['total_users']}, зарег {user_stats['registered_users']}\n"
                f"🔑 Инвайты: всего {invite_stats['total']}, активны {invite_stats['active']}")
        await self.pachca.send_message(user_id, text, reply_markup=self._get_back_to_admin_keyboard())
    
    async def _handle_admin_users(self, user_id: int, message_id: str):
        if not self._check_admin(user_id):
            return
        users = self.user_manager.get_all_users()
        text = "👥 Пользователи:\n" + "\n".join([f"{u.user_id}: {u.display_name}" for u in users[:15]])
        await self.pachca.send_message(user_id, text, reply_markup=self._get_back_to_admin_keyboard())
    
    async def _handle_admin_back(self, user_id: int, message_id: str):
        is_admin = self._check_admin(user_id)
        await self.pachca.send_message(user_id, "Главное меню", reply_markup=self._get_main_menu_keyboard(is_admin))
    
    async def _handle_cancel_delete(self, user_id: int, message_id: str):
        await self.pachca.send_message(user_id, "Удаление отменено", reply_markup=self._get_back_to_admin_keyboard())
    
    async def _handle_view_invite(self, user_id: int, callback_data: str, message_id: str):
        invite_code = callback_data.split("_")[2]
        invite = self.invite_manager.get_invite(invite_code)
        if not invite:
            await self.pachca.send_message(user_id, "Инвайт не найден")
            return
        text = f"Код: {invite.code}\nСтатус: {invite.status}\nСоздан: {invite.created_at}\nИстекает: {invite.expires_at}"
        keyboard = [[{"text": "🗑️ Удалить", "callback_data": f"{CallbackData.ADMIN_CONFIRM_DELETE}{invite.code}"}],
                    [{"text": "◀️ Назад", "callback_data": CallbackData.ADMIN_LIST_INVITES}]]
        await self.pachca.send_message(user_id, text, reply_markup=keyboard)
    
    async def _handle_invites_pagination(self, user_id: int, callback_data: str, message_id: str):
        page = int(callback_data.split("_")[2])
        invites = self.invite_manager.get_all_invites()
        await self.pachca.send_message(user_id, "Список инвайтов:", reply_markup=self._get_invites_list_keyboard(invites, page))
    
    async def _handle_confirm_delete_invite(self, user_id: int, callback_data: str, message_id: str):
        invite_code = callback_data.replace(CallbackData.ADMIN_CONFIRM_DELETE, "")
        if self.invite_manager.delete_invite(invite_code):
            await self.pachca.send_message(user_id, f"✅ Инвайт {invite_code} удален", reply_markup=self._get_back_to_admin_keyboard())
            await self._notify_admins(f"Инвайт {invite_code} удален администратором {user_id}")
        else:
            await self.pachca.send_message(user_id, "❌ Ошибка удаления")