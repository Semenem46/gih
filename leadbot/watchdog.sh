#!/usr/bin/env bash
# watchdog.sh — проверяет что parser_apex_ai жив и пишет в корпус.
# Если последняя запись в messages_corpus старше WATCHDOG_MAX_AGE_MIN минут — рестарт.
# Запускается через cron каждые 30 минут.

set -e

LEADBOT_DIR="${LEADBOT_DIR:-/root/leadbot}"
DB="$LEADBOT_DIR/apex_ai.db"
PYTHON="$LEADBOT_DIR/venv/bin/python"
LOG="$LEADBOT_DIR/watchdog.log"
INDEXER_LOG="$LEADBOT_DIR/logs_indexer.log"
WATCHDOG_MAX_AGE_MIN="${WATCHDOG_MAX_AGE_MIN:-60}"  # минут

ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "$(ts) [WATCHDOG] $*" | tee -a "$LOG"; }

# 1. Проверка процесса
PARSER_PID=$(pgrep -f "parser_apex_ai.py" | head -1 || true)

# 2. Проверка свежести корпуса
LAST_MSG_AGE_SEC=$(sqlite3 "$DB" \
    "SELECT CAST((julianday('now') - julianday(MAX(indexed_at))) * 86400 AS INTEGER) FROM messages_corpus" \
    2>/dev/null || echo 999999)

LAST_MSG_AGE_MIN=$((LAST_MSG_AGE_SEC / 60))

NEEDS_RESTART=0

if [ -z "$PARSER_PID" ]; then
    log "❌ parser_apex_ai.py не запущен"
    NEEDS_RESTART=1
elif [ "$LAST_MSG_AGE_MIN" -gt "$WATCHDOG_MAX_AGE_MIN" ]; then
    log "⚠️  Последняя запись в корпус ${LAST_MSG_AGE_MIN}мин назад (>${WATCHDOG_MAX_AGE_MIN}мин). PID=$PARSER_PID"
    NEEDS_RESTART=1
else
    log "✅ Parser OK (PID=$PARSER_PID, last_msg=${LAST_MSG_AGE_MIN}мин назад)"
fi

if [ "$NEEDS_RESTART" -eq 1 ]; then
    if [ -n "$PARSER_PID" ]; then
        log "🔪 Убиваю PID $PARSER_PID"
        kill -9 "$PARSER_PID" 2>/dev/null || true
        sleep 3
    fi

    if [ ! -f "$LEADBOT_DIR/parser_apex_ai.py" ]; then
        log "❌ Нет файла $LEADBOT_DIR/parser_apex_ai.py"
        exit 1
    fi
    if [ ! -x "$PYTHON" ]; then
        log "❌ venv-python не найден: $PYTHON"
        exit 1
    fi

    log "🚀 Стартую парсер заново"
    cd "$LEADBOT_DIR"
    nohup "$PYTHON" parser_apex_ai.py > "$INDEXER_LOG" 2>&1 &
    NEW_PID=$!
    echo "$NEW_PID" > "$LEADBOT_DIR/.pid_indexer"
    sleep 5
    if kill -0 "$NEW_PID" 2>/dev/null; then
        log "✅ Парсер живой (PID=$NEW_PID)"
        # Уведомление в TG
        if [ -n "$BOT_TOKEN_FOR_NOTIFY" ] && [ -n "$ADMIN_ID_FOR_NOTIFY" ]; then
            curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN_FOR_NOTIFY/sendMessage" \
                -d "chat_id=$ADMIN_ID_FOR_NOTIFY" \
                -d "text=🛠 Watchdog: парсер был мёртв ${LAST_MSG_AGE_MIN}мин, перезапустил. Новый PID=$NEW_PID" \
                > /dev/null || true
        fi
    else
        log "❌ Не запустился. Смотри $INDEXER_LOG"
    fi
fi
