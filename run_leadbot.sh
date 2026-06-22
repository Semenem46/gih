#!/bin/bash
# run_leadbot.sh — обёртка для запуска через cron.
# Гарантирует: cd в нужную папку, активация venv, загрузка .env.
# Все ошибки пишутся в cron_errors.log.

set -euo pipefail

PROJECT_DIR="/root/youtube_shorts_agency"
VENV_PYTHON="${PROJECT_DIR}/venv/bin/python"
LOG_FILE="${PROJECT_DIR}/cron_errors.log"

cd "$PROJECT_DIR"

# Загружаем переменные окружения из .env
set -a
source "${PROJECT_DIR}/.env"
set +a

# Запуск с перенаправлением stderr в лог
exec "$VENV_PYTHON" "${PROJECT_DIR}/main.py" "$@" >> "$LOG_FILE" 2>&1
