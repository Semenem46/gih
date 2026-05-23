#!/usr/bin/env bash
# run_discoverer_v3.sh — автоматизация:
#   1. Останавливает парсер
#   2. Запускает Discoverer v3
#   3. Стартует парсер обратно
#
# Использует ту же сессию что и парсер (apex_discoverer_v.session).
# Время работы: ~5-10 минут.

set -e

LEADBOT_DIR="${LEADBOT_DIR:-/root/leadbot}"
PYTHON="$LEADBOT_DIR/venv/bin/python"
PARSER_LOG="$LEADBOT_DIR/logs_indexer.log"
PARSER_PID_FILE="$LEADBOT_DIR/.pid_indexer"

[ -d "$LEADBOT_DIR" ] || { echo "❌ Нет $LEADBOT_DIR"; exit 1; }
[ -x "$PYTHON" ]    || { echo "❌ Нет venv: $PYTHON"; exit 1; }
[ -f "$LEADBOT_DIR/discoverer_v3.py" ] || { echo "❌ Нет discoverer_v3.py"; exit 1; }

ts() { date "+%Y-%m-%d %H:%M:%S"; }

echo "════════════════════════════════════════════════════════"
echo "  Discoverer v3 — обёртка с авто-перезапуском парсера"
echo "════════════════════════════════════════════════════════"
echo

# 1. Останов парсера
echo "[$(ts)] 🛑 Останавливаю парсер..."
PARSER_PID=$(pgrep -f "parser_apex_ai.py" | head -1 || true)
if [ -n "$PARSER_PID" ]; then
    echo "    PID найден: $PARSER_PID, посылаю SIGTERM"
    kill -TERM "$PARSER_PID" 2>/dev/null || true
    sleep 5
    if kill -0 "$PARSER_PID" 2>/dev/null; then
        echo "    Жёсткий kill -9"
        kill -9 "$PARSER_PID" 2>/dev/null || true
        sleep 2
    fi
    echo "    ✓ Парсер остановлен"
else
    echo "    · Парсер не запущен"
fi

# Доп. задержка чтобы Telethon-сессия точно отпустила SQLite-lock
sleep 3

# 2. Запуск Discoverer v3
echo
echo "[$(ts)] 🔍 Запускаю Discoverer v3..."
echo "────────────────────────────────────────────────────────"
cd "$LEADBOT_DIR"
"$PYTHON" discoverer_v3.py
DISC_EXIT=$?
echo "────────────────────────────────────────────────────────"
if [ $DISC_EXIT -ne 0 ]; then
    echo "[$(ts)] ⚠️  Discoverer завершился с кодом $DISC_EXIT"
fi

# 3. Запуск парсера обратно
echo
echo "[$(ts)] 🚀 Запускаю парсер обратно..."
nohup "$PYTHON" "$LEADBOT_DIR/parser_apex_ai.py" > "$PARSER_LOG" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PARSER_PID_FILE"
sleep 4

if kill -0 "$NEW_PID" 2>/dev/null; then
    echo "    ✓ Парсер запущен (PID=$NEW_PID)"
    echo "    лог: tail -f $PARSER_LOG"
else
    echo "    ❌ Парсер не стартанул! Смотри $PARSER_LOG"
    tail -20 "$PARSER_LOG" 2>/dev/null || true
    exit 1
fi

echo
echo "✅ Готово. Парсер крутится, новые чаты будут добавляться по 50/день."
