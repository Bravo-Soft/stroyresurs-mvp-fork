# main_bot.py (нет работы с файлами)
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse
import asyncio
import os
import json

from bot_report.bot_config import BotConfig

config = BotConfig()
logs_dir = config.logs_dir
os.makedirs(logs_dir, exist_ok=True)
log_file_path = os.path.join(logs_dir, "pachka_bot.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file_path, encoding='utf-8'),
        logging.StreamHandler()
    ],
    force=True
)
log = logging.getLogger("main_bot")

from bot_report.bot_handlers_pachca import PachcaBot
bot = PachcaBot(config)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Запуск бота Пачка...")
    await bot.monitoring_integrator.initialize()
    cleaned = bot.invite_manager.cleanup_expired_invites()
    log.info(f"Очищено {cleaned} устаревших инвайтов")
    yield
    log.info("Остановка бота...")
    await bot.monitoring_integrator.close()


app = FastAPI(lifespan=lifespan)


@app.post("/webhook")
async def webhook(request: Request, pachca_signature: str = Header(None, alias="Pachca-Signature")):
    log.info(f"Получен запрос на /webhook, signature: {pachca_signature}")
    body = await request.body()
    if not bot.pachca.verify_webhook(body, pachca_signature or ""):
        log.warning("Неверная подпись вебхука")
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        data = await request.json()
        log.info(f"FULL WEBHOOK DATA:\n{json.dumps(data, indent=2, ensure_ascii=False)}")
        event = data.get("event")
        if not event:
            return JSONResponse({"status": "ok"})

        # Обработка нового сообщения (только текст)
        if event == "new" and data.get("type") == "message":
            user_id = int(data.get("user_id"))
            text = data.get("content", "")
            message_id = data.get("id")
            asyncio.create_task(bot.handle_message(user_id, text, message_id))

        # Обработка нажатия на кнопку
        elif event == "click" and data.get("type") == "button":
            user_id = int(data.get("user_id"))
            callback_data = data.get("data", "")
            message_id = data.get("message_id")
            log.info(f"Click from user {user_id}, data: {callback_data}, msg_id: {message_id}")
            asyncio.create_task(bot.handle_callback(user_id, callback_data, message_id))

        # Обработка старого формата callback
        elif event == "callback_query_created":
            callback_obj = data.get("callback_query", {})
            user_id = int(callback_obj.get("user_id"))
            callback_data = callback_obj.get("data", "")
            message_id = callback_obj.get("message", {}).get("id")
            asyncio.create_task(bot.handle_callback(user_id, callback_data, message_id))

        else:
            log.warning(f"Неизвестное событие: {event}")

        return JSONResponse({"status": "ok"})

    except Exception as e:
        log.error(f"Критическая ошибка при обработке вебхука: {e}", exc_info=True)
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


async def main():
    import uvicorn
    config_uv = uvicorn.Config(app, host="0.0.0.0", port=5500, loop="asyncio")
    server = uvicorn.Server(config_uv)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())