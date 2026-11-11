"""Единый журнал всех модулей процесса бота, включая веб-сервер и предупреждения."""

import logging
import os


def configure_bot_logging(logs_dir=None):
    logs_dir = logs_dir or os.environ.get('LOGS_DIR') or '/home/user/stroy-resurs/mvp/logs'
    os.makedirs(logs_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.FileHandler(os.path.join(logs_dir, 'pachka_bot.log'), encoding='utf-8'),
                  logging.StreamHandler()],
        force=True,
    )
    for name in ('uvicorn', 'uvicorn.error', 'uvicorn.access'):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.setLevel(logging.INFO)
        logger.propagate = True
    logging.captureWarnings(True)
