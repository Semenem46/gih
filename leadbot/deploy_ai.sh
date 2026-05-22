#!/usr/bin/env bash
# deploy_ai.sh — раскатывает AI-qualifier поверх существующего leadbot.
#
# Что делает:
# 1. Бэкап БД (apex_ai.db.backup_<timestamp>)
# 2. Применяет миграции (idempotent)
# 3. Копирует новые/обновлённые файлы
# 4. Останавливает старый live_push (если есть)
# 5. Запускает live_push (v2) и ai_qualifier (новый)
# 6. Перезапускает бота (чтобы подхватил обновлённый review.py)
# 7. Регистрирует watchdog.sh в cron (каждые 30 минут)
#
# Безопасно для повторного запуска.

set -e

# ---------- Настройки ----------
LEADBOT_DIR="${LEADBOT_DIR:-/root/leadbot}"
PKG_DIR="$(cd "$(dirname "$0")" && pwd)"
DB="$LEADBOT_DIR/apex_ai.db"
PYTHON="$LEADBOT_DIR/venv/bin/python"
TS="$(date +%Y%m%d_%H%M%S)"

echo "════════════════════════════════════════════════════════"
echo "  Apex AI-Qualifier deploy"
echo "════════════════════════════════════════════════════════"
echo "📁 Проект: $LEADBOT_DIR"
echo "📦 Пакет:  $PKG_DIR"
echo "🐍 Python: $PYTHON"
echo

# ---------- Sanity ----------
[ -d "$LEADBOT_DIR" ] || { echo "❌ $LEADBOT_DIR не существует"; exit 1; }
[ -f "$DB" ] || { echo "❌ Не нашёл $DB"; exit 1; }
[ -x "$PYTHON" ] || { echo "❌ venv-python не найден: $PYTHON"; exit 1; }
[ -f "$PKG_DIR/live_push.py" ] || { echo "❌ Не нашёл $PKG_DIR/live_push.py"; exit 1; }

# ---------- 1. Бэкап БД ----------
BACKUP="$DB.backup_$TS"
cp "$DB" "$BACKUP"
echo "💾 Бэкап БД: $BACKUP"

# ---------- 2. Миграции ----------
echo "🗄  Применяю миграции (idempotent)..."
"$PYTHON" - <<EOF
import sqlite3
db = sqlite3.connect("$DB")
cur = db.cursor()
cols = [
    ("ai_score", "INTEGER"),
    ("ai_pain", "TEXT"),
    ("ai_fit_service", "TEXT"),
    ("ai_reason", "TEXT"),
    ("ai_processed_at", "TIMESTAMP"),
]
for name, typ in cols:
    try:
        cur.execute(f"ALTER TABLE pending_review ADD COLUMN {name} {typ}")
        print(f"  + добавлена колонка {name}")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            print(f"  · колонка {name} уже есть")
        else:
            print(f"  ! {name}: {e}")
cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_ai_status ON pending_review(status, id)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_corpus_status ON pending_review(corpus_id, status)")
cur.execute("CREATE INDEX IF NOT EXISTS idx_delivered_corpus ON delivered_leads(corpus_id)")
db.commit()
db.close()
print("✅ Миграции применены")
EOF

# ---------- 3. Копирование файлов ----------
echo
echo "📋 Копирую файлы..."

# Сохраняем старые версии
[ -f "$LEADBOT_DIR/live_push.py" ] && cp "$LEADBOT_DIR/live_push.py" "$LEADBOT_DIR/live_push.py.bak_$TS"
[ -f "$LEADBOT_DIR/bot/handlers/review.py" ] && cp "$LEADBOT_DIR/bot/handlers/review.py" "$LEADBOT_DIR/bot/handlers/review.py.bak_$TS"

# Кладём новые
cp "$PKG_DIR/live_push.py"        "$LEADBOT_DIR/live_push.py"
cp "$PKG_DIR/ai_qualifier.py"     "$LEADBOT_DIR/ai_qualifier.py"
cp "$PKG_DIR/blacklist.py"        "$LEADBOT_DIR/blacklist.py"
cp "$PKG_DIR/bot/handlers/review.py" "$LEADBOT_DIR/bot/handlers/review.py"

# watchdog
cp "$PKG_DIR/watchdog.sh" "$LEADBOT_DIR/watchdog.sh"
chmod +x "$LEADBOT_DIR/watchdog.sh"

echo "  ✅ live_push.py (v2)"
echo "  ✅ ai_qualifier.py (новый)"
echo "  ✅ blacklist.py (новый)"
echo "  ✅ bot/handlers/review.py (v2)"
echo "  ✅ watchdog.sh (новый)"

# ---------- 4. Установка openai SDK если ещё нет ----------
if ! "$PYTHON" -c "import openai" 2>/dev/null; then
    echo
    echo "📦 Устанавливаю openai SDK..."
    "$PYTHON" -m pip install --quiet openai
fi
"$PYTHON" -c "import openai; print(f'  openai: {openai.__version__}')"

# ---------- 5. Останов старых процессов ----------
echo
echo "🛑 Останавливаю старые процессы..."

# Останов старого live_push
OLD_LP=$(pgrep -f "live_push.py" || true)
if [ -n "$OLD_LP" ]; then
    echo "  kill old live_push: $OLD_LP"
    kill -TERM $OLD_LP 2>/dev/null || true
    sleep 2
    kill -9 $OLD_LP 2>/dev/null || true
fi

# Останов старого ai_qualifier (на случай повторного запуска)
OLD_AI=$(pgrep -f "ai_qualifier.py" || true)
if [ -n "$OLD_AI" ]; then
    echo "  kill old ai_qualifier: $OLD_AI"
    kill -TERM $OLD_AI 2>/dev/null || true
    sleep 2
    kill -9 $OLD_AI 2>/dev/null || true
fi

# Бот — мягкий рестарт
OLD_BOT=$(pgrep -f "bot/bot.py" || true)
if [ -n "$OLD_BOT" ]; then
    echo "  kill old bot: $OLD_BOT"
    kill -TERM $OLD_BOT 2>/dev/null || true
    sleep 2
    kill -9 $OLD_BOT 2>/dev/null || true
fi

# ---------- 6. Старт ----------
echo
echo "🚀 Стартую процессы..."

cd "$LEADBOT_DIR"

# live_push
nohup "$PYTHON" live_push.py > "$LEADBOT_DIR/live_push.log" 2>&1 &
LP_PID=$!
echo "$LP_PID" > "$LEADBOT_DIR/.pid_live_push"
echo "  ✅ live_push (PID $LP_PID)"

# ai_qualifier
nohup "$PYTHON" ai_qualifier.py > "$LEADBOT_DIR/ai_qualifier.log" 2>&1 &
AI_PID=$!
echo "$AI_PID" > "$LEADBOT_DIR/.pid_ai_qualifier"
echo "  ✅ ai_qualifier (PID $AI_PID)"

# bot
nohup "$PYTHON" bot/bot.py > "$LEADBOT_DIR/bot.log" 2>&1 &
BOT_PID=$!
echo "$BOT_PID" > "$LEADBOT_DIR/.pid_bot"
echo "  ✅ bot (PID $BOT_PID)"

# ---------- 7. Cron watchdog ----------
echo
echo "🛡  Регистрирую watchdog в cron..."
CRON_LINE="*/30 * * * * $LEADBOT_DIR/watchdog.sh >> $LEADBOT_DIR/watchdog.log 2>&1"
EXISTING=$(crontab -l 2>/dev/null || true)
if echo "$EXISTING" | grep -q "watchdog.sh"; then
    echo "  · cron-задача уже есть"
else
    (echo "$EXISTING"; echo "$CRON_LINE") | crontab -
    echo "  ✅ добавлено: каждые 30 минут"
fi

# ---------- 8. Sanity-проверка ----------
sleep 5
echo
echo "🔍 Проверка процессов:"
ps aux | grep -E "(parser_apex|live_push|ai_qualifier|bot/bot)" | grep -v grep | while read line; do
    echo "  $line"
done

echo
echo "════════════════════════════════════════════════════════"
echo "  ✅ ДЕПЛОЙ ЗАВЕРШЁН"
echo "════════════════════════════════════════════════════════"
echo
echo "Логи:"
echo "  tail -f $LEADBOT_DIR/live_push.log"
echo "  tail -f $LEADBOT_DIR/ai_qualifier.log"
echo "  tail -f $LEADBOT_DIR/bot.log"
echo "  tail -f $LEADBOT_DIR/watchdog.log"
echo
echo "В TG-боте:"
echo "  /qstats   — расширенная статистика (с AI)"
echo "  /queue    — короткая очередь /review"
echo "  /review   — модерация (теперь карточки с AI score/pain/fit_service)"
echo
echo "Восстановление если что-то пошло не так:"
echo "  cp $LEADBOT_DIR/live_push.py.bak_$TS $LEADBOT_DIR/live_push.py"
echo "  cp $LEADBOT_DIR/bot/handlers/review.py.bak_$TS $LEADBOT_DIR/bot/handlers/review.py"
echo "  cp $BACKUP $DB"
echo
