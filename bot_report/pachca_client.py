# pachca_client.py (нет работы с файлами, кроме отправки ссылок)
import aiohttp
import hashlib
import hmac
import os
import logging
from typing import Dict, Any, Optional, List, Tuple
from pathlib import Path

log = logging.getLogger("pachca_client")

class PachcaClient:
    API_BASE = "https://api.pachca.com/api/shared/v1"

    def __init__(self, config):
        self.config = config
        self.access_token = config.bot_token
        self.signing_secret = config.signing_secret
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def verify_webhook(self, payload_body: bytes, signature_header: str) -> bool:
        if not self.signing_secret:
            log.warning("Signing secret не задан, проверка подписи отключена")
            return True
        try:
            computed = hmac.new(
                key=self.signing_secret.encode('utf-8'),
                msg=payload_body,
                digestmod=hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(computed, signature_header)
        except Exception as e:
            log.error(f"Ошибка проверки подписи: {e}")
            return False

    async def send_message(self, user_id: int, text: str,
                           reply_markup: Optional[List[List[Dict]]] = None) -> bool:
        success, _ = await self.send_message_and_get_id(user_id, text, reply_markup)
        return success

    async def send_message_and_get_id(self, user_id: int, text: str,
                                      reply_markup: Optional[List[List[Dict]]] = None) -> Tuple[bool, Optional[str]]:
        url = f"{self.API_BASE}/messages"
        message_payload = {
            "entity_id": str(user_id),
            "entity_type": "user",
            "content": text
        }
        if reply_markup:
            buttons = []
            for row in reply_markup:
                button_row = []
                for btn in row:
                    button = {"text": btn["text"]}
                    if "url" in btn:
                        button["url"] = btn["url"]
                    elif "callback_data" in btn:
                        button["data"] = btn["callback_data"]
                    button_row.append(button)
                if button_row:
                    buttons.append(button_row)
            if buttons:
                message_payload["buttons"] = buttons

        payload = {"message": message_payload}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=self.headers, json=payload) as resp:
                    if resp.status in (200, 201):
                        data = await resp.json()
                        msg_id = data.get("data", {}).get("id")
                        return True, msg_id
                    else:
                        text_err = await resp.text()
                        log.error(f"Ошибка отправки сообщения {resp.status}: {text_err}")
                        return False, None
        except Exception as e:
            log.error(f"Сетевая ошибка: {e}")
            return False, None

    async def edit_message(self, user_id: int, message_id: str, new_text: str,
                           reply_markup: Optional[List[List[Dict]]] = None) -> bool:
        url = f"{self.API_BASE}/messages/{message_id}"
        message_payload = {"content": new_text}
        if reply_markup:
            buttons = []
            for row in reply_markup:
                button_row = []
                for btn in row:
                    button = {"text": btn["text"]}
                    if "url" in btn:
                        button["url"] = btn["url"]
                    elif "callback_data" in btn:
                        button["data"] = btn["callback_data"]
                    button_row.append(button)
                if button_row:
                    buttons.append(button_row)
            if buttons:
                message_payload["buttons"] = buttons

        payload = {"message": message_payload}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.put(url, headers=self.headers, json=payload) as resp:
                    if resp.status == 200:
                        return True
                    else:
                        log.error(f"Ошибка редактирования {resp.status}: {await resp.text()}")
                        return False
        except Exception as e:
            log.error(f"Сетевая ошибка редактирования: {e}")
            return False