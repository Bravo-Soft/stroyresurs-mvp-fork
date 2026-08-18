# state_manager.py
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any

log = logging.getLogger("state_manager")

class StateManager:
    """Хранение состояний FSM пользователей в JSON-файле"""
    
    def __init__(self, config):
        self.config = config
        self.states_file = config.bot_dir / "fsm_states.json"
        self._states: Dict[int, Dict[str, Any]] = {}
        self._load()
    
    def _load(self):
        if self.states_file.exists():
            try:
                with open(self.states_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # конвертируем ключи из строк в int
                    self._states = {int(k): v for k, v in data.items()}
                log.info(f"Загружено состояний: {len(self._states)}")
            except Exception as e:
                log.error(f"Ошибка загрузки состояний: {e}")
                self._states = {}
    
    def _save(self):
        try:
            with open(self.states_file, 'w', encoding='utf-8') as f:
                json.dump(self._states, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log.error(f"Ошибка сохранения состояний: {e}")
    
    def get_state(self, user_id: int) -> Optional[str]:
        """Возвращает текущее состояние пользователя (имя класса State)"""
        return self._states.get(user_id, {}).get('state')
    
    def get_data(self, user_id: int) -> dict:
        """Возвращает данные состояния"""
        return self._states.get(user_id, {}).get('data', {})
    
    def set_state(self, user_id: int, state_name: str, data: dict = None):
        """Установить состояние и опциональные данные"""
        if state_name is None:
            # очищаем состояние
            if user_id in self._states:
                del self._states[user_id]
        else:
            self._states[user_id] = {
                'state': state_name,
                'data': data or {}
            }
        self._save()
    
    def clear(self, user_id: int):
        """Полностью очистить состояние пользователя"""
        if user_id in self._states:
            del self._states[user_id]
            self._save()
    
    def update_data(self, user_id: int, data: dict):
        """Обновить данные состояния (merge)"""
        if user_id not in self._states:
            self._states[user_id] = {'state': None, 'data': {}}
        self._states[user_id]['data'].update(data)
        self._save()