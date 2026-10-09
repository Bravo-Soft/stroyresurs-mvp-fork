#!/usr/bin/env bash
# Локальный запуск Диспетчерской с НАСТОЯЩИМ пайплайном (main.py), а не эмулятором.
# Из каталога mvp (Git Bash):  ./dashboard/_run_local_real.sh
# Требует: py -3.10 с зависимостями из requirements.txt + `playwright install chromium`,
# SITE_LIST_DSN и ключи LLM в mvp/.env, доступ к Kafka.
MVP="$(cd "$(dirname "$0")/.." && pwd)"

export PIPELINE_CMD=main.py
export LIBREOFFICE_PATH="${LIBREOFFICE_PATH:-/usr/bin/soffice}"
# Сторож D101: для реального краула порог как на сервере (эмулятору хватало 120 с)
export COMPANY_IDLE_TIMEOUT_SECONDS="${COMPANY_IDLE_TIMEOUT_SECONDS:-1800}"
# Своя очередь срочных задач и своя consumer group: серверный пайплайн работает
# с тем же брокером, и общий топик urgent_tasks делил бы задачи между двумя пайплайнами
export KAFKA_TOPIC_URGENT_TASKS="${KAFKA_TOPIC_URGENT_TASKS:-urgent_tasks_local}"
export KAFKA_CONSUMER_GROUP="${KAFKA_CONSUMER_GROUP:-monitoring_pipeline_local}"

exec bash "$MVP/dashboard/_run_local.sh" "$@"
