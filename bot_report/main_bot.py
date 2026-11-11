# main_bot.py (нет работы с файлами)
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse
import asyncio

from bot_logging import configure_bot_logging

from bot_report.bot_config import BotConfig

config = BotConfig()
configure_bot_logging(config.logs_dir)
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


async def _run_bot_action(action, user_id, message_id):
    """Результат фонового обработчика попадает в лог, который читает панель."""
    try:
        await action
        log.info('Обработка события завершена: пользователь=%s, сообщение=%s', user_id, message_id)
    except Exception:
        log.exception('Ошибка обработки события: пользователь=%s, сообщение=%s', user_id, message_id)


@app.post("/webhook")
async def webhook(request: Request, pachca_signature: str = Header(None, alias="Pachca-Signature")):
    log.info('Получен запрос на /webhook')
    body = await request.body()
    if not bot.pachca.verify_webhook(body, pachca_signature or ""):
        log.warning("Неверная подпись вебхука")
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        data = await request.json()
        event = data.get("event")
        log.info('Событие бота: тип=%s, событие=%s, пользователь=%s',
                 data.get('type'), event, data.get('user_id'))
        if not event:
            return JSONResponse({"status": "ok"})

        # Обработка нового сообщения (только текст)
        if event == "new" and data.get("type") == "message":
            user_id = int(data.get("user_id"))
            text = data.get("content", "")
            message_id = data.get("id")
            asyncio.create_task(_run_bot_action(bot.handle_message(user_id, text, message_id), user_id, message_id))

        # Обработка нажатия на кнопку
        elif event == "click" and data.get("type") == "button":
            user_id = int(data.get("user_id"))
            callback_data = data.get("data", "")
            message_id = data.get("message_id")
            asyncio.create_task(_run_bot_action(bot.handle_callback(user_id, callback_data, message_id), user_id, message_id))

        # Обработка старого формата callback
        elif event == "callback_query_created":
            callback_obj = data.get("callback_query", {})
            user_id = int(callback_obj.get("user_id"))
            callback_data = callback_obj.get("data", "")
            message_id = callback_obj.get("message", {}).get("id")
            asyncio.create_task(_run_bot_action(bot.handle_callback(user_id, callback_data, message_id), user_id, message_id))

        else:
            log.warning(f"Неизвестное событие: {event}")

        return JSONResponse({"status": "ok"})

    except Exception as e:
        log.error(f"Критическая ошибка при обработке вебхука: {e}", exc_info=True)
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


async def main():
    import uvicorn
    config_uv = uvicorn.Config(app, host="0.0.0.0", port=5500, loop="asyncio", log_config=None)
    server = uvicorn.Server(config_uv)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
