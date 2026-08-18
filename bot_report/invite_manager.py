# invite_manager.py
import json
import os
import secrets
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict, field
import logging

log = logging.getLogger("invite_manager")

class InviteStatus(str, Enum):
    ACTIVE = "active"
    USED = "used"
    EXPIRED = "expired"

@dataclass
class InviteCode:
    """Структура инвайт-кода"""
    code: str
    created_by: int  # ID администратора
    created_at: str
    expires_at: str
    status: InviteStatus = InviteStatus.ACTIVE
    used_by: Optional[int] = None
    used_at: Optional[str] = None
    username: Optional[str] = None  # Имя пользователя, которому выдан инвайт
    
    def is_expired(self) -> bool:
        """Проверка истечения срока действия инвайта"""
        try:
            expires_dt = datetime.fromisoformat(self.expires_at)
            return datetime.now() > expires_dt
        except ValueError:
            return True
    
    def mark_used(self, user_id: int, username: Optional[str] = None) -> None:
        """Пометить инвайт как использованный"""
        self.status = InviteStatus.USED
        self.used_by = user_id
        self.used_at = datetime.now().isoformat()
        if username:
            self.username = username

class InviteManager:
    """Менеджер инвайт-кодов (хранение в JSON файле)"""
    
    def __init__(self, config):
        self.config = config
        self.invites_file = Path(config.bot_dir) / "invites.json"
        self.invites_file.parent.mkdir(exist_ok=True)
        self._load_invites()
    
    def _load_invites(self) -> None:
        """Загрузка инвайтов из файла"""
        if not self.invites_file.exists():
            self.invites = {}
            self._save_invites()
        else:
            try:
                with open(self.invites_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # Конвертируем строки обратно в объекты InviteCode
                    self.invites = {}
                    for code, invite_data in data.items():
                        invite_data['status'] = InviteStatus(invite_data['status'])
                        self.invites[code] = InviteCode(**invite_data)
            except Exception as e:
                log.error(f"Ошибка загрузки инвайтов: {e}")
                self.invites = {}
                self._save_invites()
    
    def _save_invites(self) -> None:
        """Сохранение инвайтов в файл"""
        try:
            # Конвертируем объекты InviteCode в словари
            data = {}
            for code, invite in self.invites.items():
                data[code] = asdict(invite)
            
            with open(self.invites_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"Ошибка сохранения инвайтов: {e}")
    
    def _generate_unique_code(self, length: int = 10) -> str:
        """Генерация уникального инвайт-кода"""
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        
        for _ in range(50):
            code = ''.join(secrets.choice(alphabet) for _ in range(length))
            if code not in self.invites:
                return code
        
        raise RuntimeError("Не удалось сгенерировать уникальный инвайт-код")
    
    def create_invite(self, admin_id: int, username: str = None, days_valid: int = 30) -> Tuple[bool, str, Optional[str]]:
        """Создание нового инвайт-кода"""
        try:
            # Проверяем, является ли создатель администратором
            # Этот метод вызывается только из админ-панели, но на всякий случай проверяем
            from bot_report.user_manager import UserManager
            user_manager = UserManager(self.config)
            
            if not user_manager.is_admin(admin_id):
                return False, "Только администраторы могут создавать инвайты", None
            
            code = self._generate_unique_code()
            created_at = datetime.now()
            expires_at = created_at + timedelta(days=days_valid)
            
            invite = InviteCode(
                code=code,
                created_by=admin_id,
                created_at=created_at.isoformat(),
                expires_at=expires_at.isoformat(),
                username=username
            )
            
            self.invites[code] = invite
            self._save_invites()
            
            log.info(f"Создан инвайт {code} администратором {admin_id}")
            
            # Формируем информацию о создателе для логов
            creator_user = user_manager.load_user(admin_id)
            creator_name = creator_user.display_name if creator_user else f"ID: {admin_id}"
            
            return True, f"Инвайт-код создан администратором {creator_name}", code
            
        except Exception as e:
            log.error(f"Ошибка создания инвайта: {e}")
            return False, f"Ошибка создания инвайта: {str(e)}", None
    
    def validate_invite(self, code: str) -> Tuple[bool, str, Optional[InviteCode]]:
        """Проверка валидности инвайт-кода"""
        # Автоматическая очистка устаревших инвайтов при проверке
        self.cleanup_expired_invites()
        
        invite = self.invites.get(code.upper())
        
        if not invite:
            return False, "Инвайт-код не найден", None
        
        if invite.status == InviteStatus.USED:
            return False, "Инвайт-код уже использован", None
        
        if invite.is_expired():
            invite.status = InviteStatus.EXPIRED
            self._save_invites()
            return False, "Срок действия инвайт-кода истек", None
        
        return True, "Инвайт-код действителен", invite
    
    def use_invite(self, code: str, user_id: int, username: str = None) -> bool:
        """Использование инвайт-кода"""
        success, message, invite = self.validate_invite(code)
        
        if not success:
            return False
        
        invite.mark_used(user_id, username)
        self._save_invites()
        
        log.info(f"Инвайт {code} использован пользователем {user_id}")
        return True
    
    def delete_invite(self, code: str) -> bool:
        """Удаление инвайт-кода"""
        if code.upper() in self.invites:
            del self.invites[code.upper()]
            self._save_invites()
            log.info(f"Инвайт {code} удален")
            return True
        return False
    
    def get_invite(self, code: str) -> Optional[InviteCode]:
        """Получение инвайта по коду"""
        return self.invites.get(code.upper())
    
    def get_all_invites(self) -> List[InviteCode]:
        """Получение всех инвайтов"""
        return list(self.invites.values())
    
    def get_invites_by_creator(self, admin_id: int) -> List[InviteCode]:
        """Получение инвайтов, созданных определенным администратором"""
        return [invite for invite in self.invites.values() if invite.created_by == admin_id]
    
    def get_active_invites(self) -> List[InviteCode]:
        """Получение активных инвайтов"""
        return [invite for invite in self.invites.values() 
                if invite.status == InviteStatus.ACTIVE and not invite.is_expired()]
    
    def get_used_invites(self) -> List[InviteCode]:
        """Получение использованных инвайтов"""
        return [invite for invite in self.invites.values() 
                if invite.status == InviteStatus.USED]
    
    def get_expired_invites(self) -> List[InviteCode]:
        """Получение истекших инвайтов"""
        return [invite for invite in self.invites.values() 
                if invite.status == InviteStatus.EXPIRED or invite.is_expired()]
    
    def cleanup_expired_invites(self) -> int:
        """Автоматическая очистка устаревших инвайтов"""
        expired_count = 0
        
        for code, invite in list(self.invites.items()):
            if invite.status == InviteStatus.ACTIVE and invite.is_expired():
                invite.status = InviteStatus.EXPIRED
                expired_count += 1
        
        if expired_count > 0:
            self._save_invites()
            log.info(f"Очищено {expired_count} устаревших инвайтов")
        
        return expired_count
    
    def get_statistics(self) -> Dict[str, any]:
        """Получение статистики инвайтов"""
        self.cleanup_expired_invites()
        
        all_invites = list(self.invites.values())
        active = self.get_active_invites()
        used = self.get_used_invites()
        expired = self.get_expired_invites()
        
        # Статистика по дням создания
        creation_stats = {}
        for invite in all_invites:
            date = datetime.fromisoformat(invite.created_at).strftime("%Y-%m-%d")
            creation_stats[date] = creation_stats.get(date, 0) + 1
        
        return {
            "total": len(all_invites),
            "active": len(active),
            "used": len(used),
            "expired": len(expired),
            "creation_stats": creation_stats,
            "last_cleanup": datetime.now().isoformat()
        }