#!/usr/bin/env bash
# deploy_lead_magnet.sh — переход на AI-демо магнит без xlsx
#
# Что делает:
# 1. Бэкап старых файлов
# 2. Заменяет funnel.py, texts.py, niche.py на новые версии (без xlsx в /start)
# 3. Патчит query_engine.py: QUALIFIER_THRESHOLD 75 → 85
# 4. Перезапускает бот
#
# Безопасно для повторного запуска (idempotent).

set -e

LEADBOT_DIR="${LEADBOT_DIR:-/root/leadbot}"
PKG_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$LEADBOT_DIR/venv/bin/python"
TS="$(date +%Y%m%d_%H%M%S)"

echo "════════════════════════════════════════════════════════"
echo "  Apex AI-демо магнит — деплой"
echo "════════════════════════════════════════════════════════"
echo "📁 Проект: $LEADBOT_DIR"
echo "📦 Пакет:  $PKG_DIR"
echo

[ -d "$LEADBOT_DIR" ] || { echo "❌ Нет $LEADBOT_DIR"; exit 1; }
[ -x "$PYTHON" ] || { echo "❌ venv не найден: $PYTHON"; exit 1; }

# 1. Бэкап
echo "💾 Бэкап старых файлов..."
cp "$LEADBOT_DIR/bot/handlers/funnel.py" "$LEADBOT_DIR/bot/handlers/funnel.py.bak_$TS"
cp "$LEADBOT_DIR/bot/handlers/niche.py"  "$LEADBOT_DIR/bot/handlers/niche.py.bak_$TS"
cp "$LEADBOT_DIR/bot/texts.py"           "$LEADBOT_DIR/bot/texts.py.bak_$TS"
cp "$LEADBOT_DIR/query_engine.py"        "$LEADBOT_DIR/query_engine.py.bak_$TS"
echo "  ✓ funnel.py.bak_$TS"
echo "  ✓ niche.py.bak_$TS"
echo "  ✓ texts.py.bak_$TS"
echo "  ✓ query_engine.py.bak_$TS"
echo

# 2. Замена файлов
echo "📋 Замена файлов..."
cp "$PKG_DIR/bot/handlers/funnel.py"     "$LEADBOT_DIR/bot/handlers/funnel.py"
cp "$PKG_DIR/bot/handlers/niche.py"      "$LEADBOT_DIR/bot/handlers/niche.py"
cp "$PKG_DIR/bot/texts.py"               "$LEADBOT_DIR/bot/texts.py"
echo "  ✓ funnel.py (без xlsx в /start)"
echo "  ✓ niche.py (count >= 5 для отказа)"
echo "  ✓ texts.py (новый WELCOME, NICHE_*)"
echo

# 3. Патч query_engine.py — QUALIFIER_THRESHOLD 75 → 85
echo "🔧 Патч query_engine.py: QUALIFIER_THRESHOLD = 85..."
if grep -qE "^QUALIFIER_THRESHOLD\s*=\s*75" "$LEADBOT_DIR/query_engine.py"; then
    sed -i 's/^QUALIFIER_THRESHOLD = 75.*$/QUALIFIER_THRESHOLD = 85      # минимальный score для показа клиенту/' "$LEADBOT_DIR/query_engine.py"
    echo "  ✓ изменено 75 → 85"
elif grep -qE "^QUALIFIER_THRESHOLD\s*=\s*80" "$LEADBOT_DIR/query_engine.py"; then
    sed -i 's/^QUALIFIER_THRESHOLD = 80.*$/QUALIFIER_THRESHOLD = 85      # минимальный score для показа клиенту/' "$LEADBOT_DIR/query_engine.py"
    echo "  ✓ изменено 80 → 85"
elif grep -qE "^QUALIFIER_THRESHOLD\s*=\s*85" "$LEADBOT_DIR/query_engine.py"; then
    echo "  · уже 85, пропускаю"
else
    echo "  ⚠️  не нашёл QUALIFIER_THRESHOLD — проверь руками:"
    grep -n "QUALIFIER_THRESHOLD" "$LEADBOT_DIR/query_engine.py" || echo "  (не найдено вообще)"
fi
echo

# 4. Sanity-check: компилирует ли всё
echo "🔍 Проверка синтаксиса..."
"$PYTHON" -c "
import ast
for f in [
    '$LEADBOT_DIR/bot/handlers/funnel.py',
    '$LEADBOT_DIR/bot/handlers/niche.py',
    '$LEADBOT_DIR/bot/texts.py',
    '$LEADBOT_DIR/query_engine.py',
]:
    try:
        ast.parse(open(f).read())
        print(f'  ✓ {f.split(\"/\")[-1]}')
    except SyntaxError as e:
        print(f'  ❌ {f.split(\"/\")[-1]}: {e}')
        raise
"
echo

# 5. Рестарт бота
echo "🛑 Рестарт бота..."
OLD_BOT=$(pgrep -f "bot/bot.py" || true)
if [ -n "$OLD_BOT" ]; then
    echo "  kill bot: $OLD_BOT"
    kill -TERM $OLD_BOT 2>/dev/null || true
    sleep 2
    kill -9 $OLD_BOT 2>/dev/null || true
fi
sleep 2

cd "$LEADBOT_DIR"
nohup "$PYTHON" bot/bot.py > "$LEADBOT_DIR/bot.log" 2>&1 &
BOT_PID=$!
echo "$BOT_PID" > "$LEADBOT_DIR/.pid_bot"
echo "  ✓ bot (PID $BOT_PID)"
sleep 4

# 6. Проверка
echo
echo "🔍 Все процессы:"
ps aux | grep -E "(parser_apex|live_push|ai_qualifier|bot/bot)" | grep -v grep | while read line; do
    echo "  $line"
done

echo
echo "════════════════════════════════════════════════════════"
echo "  ✅ ДЕПЛОЙ ЗАВЕРШЁН"
echo "════════════════════════════════════════════════════════"
echo
echo "В TG-боте:"
echo "  Сначала удали себя из БД (чтобы /start снова прошёл с нуля):"
echo "    sqlite3 $LEADBOT_DIR/bot/leads.db \"DELETE FROM leads WHERE user_id=7531405698;\""
echo "  Потом /start в боте — НЕ должно быть xlsx-файла, только приветствие + меню."
echo "  Жми «🎯 Разбор моей ниши» → опиши нишу → получи лид score 85+"
echo
echo "Откат:"
echo "  cp $LEADBOT_DIR/bot/handlers/funnel.py.bak_$TS $LEADBOT_DIR/bot/handlers/funnel.py"
echo "  cp $LEADBOT_DIR/bot/handlers/niche.py.bak_$TS  $LEADBOT_DIR/bot/handlers/niche.py"
echo "  cp $LEADBOT_DIR/bot/texts.py.bak_$TS           $LEADBOT_DIR/bot/texts.py"
echo "  cp $LEADBOT_DIR/query_engine.py.bak_$TS        $LEADBOT_DIR/query_engine.py"
echo "  pkill -9 -f 'bot/bot.py' && sleep 2 && cd $LEADBOT_DIR && nohup $PYTHON bot/bot.py > bot.log 2>&1 &"
echo
