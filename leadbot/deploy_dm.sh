#!/usr/bin/env bash
# deploy_dm.sh — раскатка DM-Outreach режима поверх существующего leadbot.
#
# Что делает:
# 1. Бэкап БД и старых файлов
# 2. Применяет миграцию 002_dm_outreach (idempotent)
# 3. Заменяет ai_qualifier.py и bot/handlers/review.py
# 4. Перезапускает ai_qualifier и bot
# 5. Регистрирует тебя как DM-outreach клиента в БД
#
# Безопасно для повторного запуска.

set -e

LEADBOT_DIR="${LEADBOT_DIR:-/root/leadbot}"
PKG_DIR="$(cd "$(dirname "$0")" && pwd)"
DB="$LEADBOT_DIR/apex_ai.db"
PYTHON="$LEADBOT_DIR/venv/bin/python"
TS="$(date +%Y%m%d_%H%M%S)"

# Твой Telegram user_id из .env (для DM-outreach клиента)
ADMIN_ID="${APEX_ADMIN_ID:-7531405698}"

echo "════════════════════════════════════════════════════════"
echo "  Apex DM-Outreach deploy"
echo "════════════════════════════════════════════════════════"
echo "📁 Проект: $LEADBOT_DIR"
echo "📦 Пакет:  $PKG_DIR"
echo "🐍 Python: $PYTHON"
echo "👤 Admin:  $ADMIN_ID"
echo

# Sanity
[ -d "$LEADBOT_DIR" ] || { echo "❌ $LEADBOT_DIR не существует"; exit 1; }
[ -f "$DB" ] || { echo "❌ Не нашёл $DB"; exit 1; }
[ -x "$PYTHON" ] || { echo "❌ venv-python не найден: $PYTHON"; exit 1; }
[ -f "$PKG_DIR/ai_qualifier.py" ] || { echo "❌ Нет $PKG_DIR/ai_qualifier.py"; exit 1; }

# 1. Бэкап БД
BACKUP="$DB.backup_$TS"
cp "$DB" "$BACKUP"
echo "💾 Бэкап БД: $BACKUP"

# 2. Миграции
echo
echo "🗄  Применяю миграцию 002_dm_outreach..."
"$PYTHON" - <<EOF
import sqlite3
db = sqlite3.connect("$DB")
cur = db.cursor()

# paid_clients.mode
try:
    cur.execute("ALTER TABLE paid_clients ADD COLUMN mode TEXT DEFAULT 'lead-finder'")
    print("  + paid_clients.mode")
except sqlite3.OperationalError as e:
    if "duplicate" in str(e).lower():
        print("  · paid_clients.mode уже есть")
    else:
        print(f"  ! {e}")

# pending_review.ai_dm_*
for col, typ in [
    ("ai_dm_text", "TEXT"),
    ("ai_dm_username", "TEXT"),
    ("ai_dm_fit", "TEXT"),
    ("dm_sent_at", "TIMESTAMP"),
]:
    try:
        cur.execute(f"ALTER TABLE pending_review ADD COLUMN {col} {typ}")
        print(f"  + pending_review.{col}")
    except sqlite3.OperationalError as e:
        if "duplicate" in str(e).lower():
            print(f"  · pending_review.{col} уже есть")
        else:
            print(f"  ! {col}: {e}")

cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_dm_status ON pending_review(status, ai_dm_text)")
db.commit()
db.close()
print("✅ Миграция применена")
EOF

# 3. Копирование файлов
echo
echo "📋 Копирую файлы..."
[ -f "$LEADBOT_DIR/ai_qualifier.py" ] && cp "$LEADBOT_DIR/ai_qualifier.py" "$LEADBOT_DIR/ai_qualifier.py.bak_$TS"
[ -f "$LEADBOT_DIR/bot/handlers/review.py" ] && cp "$LEADBOT_DIR/bot/handlers/review.py" "$LEADBOT_DIR/bot/handlers/review.py.bak_$TS"

cp "$PKG_DIR/ai_qualifier.py"        "$LEADBOT_DIR/ai_qualifier.py"
cp "$PKG_DIR/bot/handlers/review.py" "$LEADBOT_DIR/bot/handlers/review.py"
echo "  ✅ ai_qualifier.py (v2 с DM-outreach)"
echo "  ✅ bot/handlers/review.py (v3 с DM-карточками)"

# 4. Останов старых
echo
echo "🛑 Перезапуск процессов..."
OLD_AI=$(pgrep -f "ai_qualifier.py" || true)
[ -n "$OLD_AI" ] && { echo "  kill ai_qualifier: $OLD_AI"; kill -9 $OLD_AI 2>/dev/null || true; sleep 1; }

OLD_BOT=$(pgrep -f "bot/bot.py" || true)
[ -n "$OLD_BOT" ] && { echo "  kill bot: $OLD_BOT"; kill -9 $OLD_BOT 2>/dev/null || true; sleep 1; }

# 5. Старт новых
cd "$LEADBOT_DIR"

nohup "$PYTHON" ai_qualifier.py > "$LEADBOT_DIR/ai_qualifier.log" 2>&1 &
AI_PID=$!
echo "$AI_PID" > "$LEADBOT_DIR/.pid_ai_qualifier"
echo "  ✅ ai_qualifier (PID $AI_PID)"

nohup "$PYTHON" bot/bot.py > "$LEADBOT_DIR/bot.log" 2>&1 &
BOT_PID=$!
echo "$BOT_PID" > "$LEADBOT_DIR/.pid_bot"
echo "  ✅ bot (PID $BOT_PID)"

# 6. Регистрация DM-outreach клиента
echo
echo "👤 Регистрирую DM-outreach клиента (твой ID=$ADMIN_ID)..."
"$PYTHON" - <<EOF
import sqlite3, json
db = sqlite3.connect("$DB")
cur = db.cursor()

keywords = [
    # Фрилансеры/спецы кто продаёт услуги совпадающие с парсером
    "делаю сайты", "делаю лендинги", "разработка под ключ", "веб-разработчик",
    "разработчик сайтов", "next.js", "react", "frontend",
    "директолог", "настраиваю директ", "веду директ", "контекстная реклама",
    "трафик", "арбитраж",
    "seo-специалист", "сео продвижение", "продвижение сайта", "линкбилдинг",
    "ссылочное", "обратные ссылки",
    "smm-щик", "веду smm", "smm под ключ", "smm-агентство",
    "дизайнер", "веб-дизайн", "ui ux",
    "маркетолог", "маркетинг под ключ", "помогаю с лидами", "настрою воронку",
    "агентство", "студия", "интернет-маркетинг",
    # Активные участники
    "опыт работы", "кейсы", "портфолио", "примеры работ",
]

cur.execute("""
    INSERT INTO paid_clients (user_id, client_name, niche_text, niche_keywords, status, mode)
    VALUES (?, ?, ?, ?, 'active', 'dm-outreach')
    ON CONFLICT(user_id) DO UPDATE SET
        client_name    = excluded.client_name,
        niche_text     = excluded.niche_text,
        niche_keywords = excluded.niche_keywords,
        mode           = 'dm-outreach',
        status         = 'active'
""", (
    int("$ADMIN_ID"),
    "Apex-DM-Outreach",
    "Поиск фрилансеров и агентств для DM-аутрича",
    json.dumps(keywords, ensure_ascii=False),
))
db.commit()
db.close()
print(f"✅ DM-Outreach клиент зарегистрирован (user_id=$ADMIN_ID)")
print(f"   Ключей: {len(keywords)}")
EOF

# 7. Sanity-проверка
sleep 4
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
echo "В TG-боте:"
echo "  /listclients   ← должен показать 2 клиентов: Pavel-test (lead-finder) и Apex-DM-Outreach (dm-outreach)"
echo "  /qstats        ← новая статистика по обоим режимам"
echo "  /review        ← покажет следующего pending кандидата"
echo
echo "Через 1-2 часа когда AI прогонит первые батчи в DM-режиме,"
echo "ты увидишь карточки '🔵 DM-кандидат' с готовым текстом DM."
echo
echo "Логи:"
echo "  tail -f $LEADBOT_DIR/ai_qualifier.log"
echo "  tail -f $LEADBOT_DIR/bot.log"
echo
echo "Откат:"
echo "  cp $LEADBOT_DIR/ai_qualifier.py.bak_$TS $LEADBOT_DIR/ai_qualifier.py"
echo "  cp $LEADBOT_DIR/bot/handlers/review.py.bak_$TS $LEADBOT_DIR/bot/handlers/review.py"
echo "  cp $BACKUP $DB"
echo
