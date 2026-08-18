# user_manager.py
import json
import os
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
import logging
from dataclasses import dataclass, asdict, field

log = logging.getLogger("user_manager")

@dataclass
class UserData:
    """Данные пользователя"""
    user_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    registered_at: str = ""
    last_request: str = ""
    total_requests: int = 0
    reports_generated: int = 0
    companies_added: int = 0
    last_report_date: Optional[str] = None
    last_company_update_date: Optional[str] = None
    
    # Новые поля для системы инвайтов
    invite_code: Optional[str] = None
    registered_via_invite: bool = False
    is_admin: bool = False
    admin_since: Optional[str] = None
    
    def __post_init__(self):
        if not self.registered_at:
            self.registered_at = datetime.now().isoformat()
    
    @property
    def is_registered(self) -> bool:
        """Проверка, зарегистрирован ли пользователь"""
        return self.registered_via_invite
    
    @property
    def display_name(self) -> str:
        """Имя для отображения"""
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        elif self.first_name:
            return self.first_name
        elif self.username:
            return f"@{self.username}"
        else:
            return f"User_{self.user_id}"

class UserManager:
    """Менеджер пользователей (хранение в файлах)"""
    
    def __init__(self, config):
        self.config = config
        self.users_dir = Path(config.users_dir)
        self.users_dir.mkdir(exist_ok=True)
    
    def get_user_file_path(self, user_id: int) -> Path:
        """Получение пути к файлу пользователя"""
        return self.users_dir / f"{user_id}.json"
    
    def load_user(self, user_id: int) -> Optional[UserData]:
        """Загрузка данных пользователя"""
        user_file = self.get_user_file_path(user_id)
        
        if not user_file.exists():
            return None
        
        try:
            with open(user_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return UserData(**data)
        except Exception as e:
            log.error(f"Ошибка загрузки пользователя {user_id}: {e}")
            return None
    
    def save_user(self, user_data: UserData) -> bool:
        """Сохранение данных пользователя"""
        try:
            user_file = self.get_user_file_path(user_data.user_id)
            
            # Обновляем время последнего запроса
            user_data.last_request = datetime.now().isoformat()
            
            with open(user_file, 'w', encoding='utf-8') as f:
                json.dump(asdict(user_data), f, ensure_ascii=False, indent=2)
            
            return True
        except Exception as e:
            log.error(f"Ошибка сохранения пользователя {user_data.user_id}: {e}")
            return False
    
    def create_or_update_user(self, user_id: int, username: str = None, 
                            first_name: str = None, last_name: str = None) -> UserData:
        """Создание или обновление пользователя"""
        user = self.load_user(user_id)
        
        if user is None:
            # Новый пользователь
            user = UserData(
                user_id=user_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                registered_at=datetime.now().isoformat()
            )
        else:
            # Обновление данных существующего пользователя
            if username:
                user.username = username
            if first_name:
                user.first_name = first_name
            if last_name:
                user.last_name = last_name
        
        self.save_user(user)
        return user
    
    def register_user_with_invite(self, user_id: int, username: str, 
                                first_name: str, last_name: str, 
                                invite_code: str) -> Tuple[bool, UserData]:
        """Регистрация пользователя с инвайт-кодом"""
        try:
            # Создаем/обновляем пользователя
            user = self.create_or_update_user(
                user_id=user_id,
                username=username,
                first_name=first_name,
                last_name=last_name
            )
            
            # Устанавливаем данные инвайта
            user.invite_code = invite_code
            user.registered_via_invite = True
            
            # Проверяем, является ли администратором
            if user_id in self.config.admin_ids:
                user.is_admin = True
                user.admin_since = datetime.now().isoformat()
            
            self.save_user(user)
            
            log.info(f"Пользователь {user_id} зарегистрирован с инвайтом {invite_code}")
            return True, user
            
        except Exception as e:
            log.error(f"Ошибка регистрации пользователя {user_id}: {e}")
            return False, None
    
    def is_admin(self, user_id: int) -> bool:
        """Проверка, является ли пользователь администратором"""
        # Сначала проверяем по конфигу (список из .env)
        if user_id in self.config.admin_ids:
            # Если администратор из .env, но нет в базе - создаем запись
            user = self.load_user(user_id)
            if not user:
                user = self.create_or_update_user(user_id)
                user.is_admin = True
                user.admin_since = datetime.now().isoformat()
                user.registered_via_invite = True  # Администратор считается зарегистрированным
                self.save_user(user)
            return True
        
        # Проверяем в базе данных
        user = self.load_user(user_id)
        return user.is_admin if user else False
    
    def get_admins(self) -> List[UserData]:
        """Получение всех администраторов"""
        admins = []
        
        # Проверяем пользователей из файлов
        for user_file in self.users_dir.glob("*.json"):
            try:
                with open(user_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if data.get('is_admin', False):
                        admins.append(UserData(**data))
            except Exception:
                continue
        
        # Добавляем администраторов из конфига, если их нет в файлах
        for admin_id in self.config.admin_ids:
            if not any(admin.user_id == admin_id for admin in admins):
                admin_user = UserData(
                    user_id=admin_id,
                    is_admin=True,
                    admin_since=datetime.now().isoformat()
                )
                admins.append(admin_user)
        
        return admins
    
    def get_user_by_invite(self, invite_code: str) -> Optional[UserData]:
        """Получение пользователя по инвайт-коду"""
        for user_file in self.users_dir.glob("*.json"):
            try:
                with open(user_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if data.get('invite_code') == invite_code:
                        return UserData(**data)
            except Exception:
                continue
        
        return None
    
    def increment_requests(self, user_id: int) -> bool:
        """Увеличение счетчика запросов"""
        user = self.load_user(user_id)
        if user:
            user.total_requests += 1
            return self.save_user(user)
        return False
    
    def increment_reports(self, user_id: int) -> bool:
        """Увеличение счетчика отчетов"""
        user = self.load_user(user_id)
        if user:
            user.reports_generated += 1
            return self.save_user(user)
        return False
    
    def increment_companies(self, user_id: int) -> bool:
        """Увеличение счетчика добавленных компаний"""
        user = self.load_user(user_id)
        if user:
            user.companies_added += 1
            return self.save_user(user)
        return False
    
    def update_last_report_date(self, user_id: int, report_date: str) -> bool:
        """Обновление даты последнего отчета"""
        user = self.load_user(user_id)
        if user:
            user.last_report_date = report_date
            return self.save_user(user)
        return False
    
    def update_last_company_update_date(self, user_id: int, update_date: str) -> bool:
        """Обновление даты последнего обновления компании"""
        user = self.load_user(user_id)
        if user:
            user.last_company_update_date = update_date
            return self.save_user(user)
        return False
    
    def get_all_users(self) -> List[UserData]:
        """Получение всех пользователей"""
        users = []
        
        for user_file in self.users_dir.glob("*.json"):
            try:
                user_id = int(user_file.stem)
                user = self.load_user(user_id)
                if user:
                    users.append(user)
            except ValueError:
                continue
        
        return sorted(users, key=lambda x: x.user_id)
    
    def get_statistics(self) -> Dict[str, Any]:
        """Получение статистики пользователей"""
        users = self.get_all_users()
        registered_users = [u for u in users if u.registered_via_invite]
        
        return {
            "total_users": len(users),
            "registered_users": len(registered_users),
            "admins": len([u for u in users if u.is_admin]),
            "total_requests": sum(u.total_requests for u in users),
            "total_reports": sum(u.reports_generated for u in users),
            "total_companies_added": sum(u.companies_added for u in users),
            "last_report_date": max([u.last_report_date for u in users if u.last_report_date], default=None),
            "last_company_update_date": max([u.last_company_update_date for u in users if u.last_company_update_date], default=None)
        }