"""Общий каркас тестов mvp: добавляет корень mvp в sys.path.

Запуск (dev-окружение вне контейнера):
    /ai/projects/stroy-resurs/claude_work/.venv-dev/bin/python -m pytest mvp/tests -m "not live"
Live-тесты (реальный краулинг) помечены @pytest.mark.live и по умолчанию не запускаются.
"""
import sys
from pathlib import Path

import pytest

MVP_ROOT = Path(__file__).resolve().parents[1]
if str(MVP_ROOT) not in sys.path:
    sys.path.insert(0, str(MVP_ROOT))

# Корпус сохранённых страниц прошлых прогонов (bind-mount контейнера).
CORPUS_DIR = Path('/ai/projects/stroy-resurs/mvp_directories/Base/temp_html_storage')


def pytest_configure(config):
    config.addinivalue_line('markers', 'live: тесты с реальным краулингом (запуск вручную)')


def pytest_collection_modifyitems(config, items):
    if config.getoption('-m', default=''):
        return
    skip_live = pytest.mark.skip(reason='live-тесты запускаются явно: -m live')
    for item in items:
        if 'live' in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(scope='session')
def corpus_dir():
    if not CORPUS_DIR.is_dir():
        pytest.skip(f'корпус недоступен: {CORPUS_DIR}')
    return CORPUS_DIR
