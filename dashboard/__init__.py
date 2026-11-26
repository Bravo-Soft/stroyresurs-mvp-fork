# dashboard — «Диспетчерская»: операторская консоль пайплайна.
# Реализация спецификации «Диспетчерская: панель управления и дашборд» в. 1.1.
# Точка входа — dashboard/app.py (FastAPI, порт :5510). Запуск и локальная
# проверка — dashboard/README.md.
#
# Модули пакета импортируют друг друга по короткому имени (import journal), как это
# принято во всей кодовой базе mvp. Чтобы это работало и при запуске пакетом
# (uvicorn dashboard.app:app), и при запуске файлом (python dashboard/fake_pipeline.py),
# оба каталога — mvp и dashboard — кладутся в sys.path здесь, до любых импортов.

import os
import sys

_DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))
_MVP_DIR = os.path.dirname(_DASHBOARD_DIR)
for _p in (_MVP_DIR, _DASHBOARD_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)
