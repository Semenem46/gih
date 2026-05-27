#!/usr/bin/env bash
# Запускает все 3 процесса в tmux-сессии leadbot.
#   ./start.sh         — запустить всё
#   tmux attach -t leadbot   — посмотреть что происходит
#   tmux kill-session -t leadbot   — остановить всё
set -e

cd "$(dirname "$0")"
SESSION=leadbot

if ! command -v tmux >/dev/null 2>&1; then
    echo "❌ tmux не установлен. Запусти ./setup.sh"
    exit 1
fi

if [ ! -f venv/bin/activate ]; then
    echo "❌ venv не создан. Запусти ./setup.sh"
    exit 1
fi

if [ ! -f bot/.env ]; then
    echo "❌ bot/.env нет. Запусти ./setup.sh и заполни .env"
    exit 1
fi

if [ ! -f apex_discoverer_v.session ]; then
    echo "❌ apex_discoverer_v.session отсутствует."
    echo "   Это Telethon-авторизация. Создай её на ноуте и закинь сюда."
    echo "   См. RUNBOOK.md шаг 3."
    exit 1
fi

# Если сессия уже есть — переподключаемся
if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "⚠️  Сессия leadbot уже запущена."
    echo "   Подключиться: tmux attach -t leadbot"
    echo "   Перезапустить: tmux kill-session -t leadbot && ./start.sh"
    exit 0
fi

PWDIR=$(pwd)
ACTIVATE="source $PWDIR/venv/bin/activate"

echo "🚀 Запускаю всё в tmux-сессии 'leadbot'..."

# Окно 1 — индексер (главный)
tmux new-session  -d -s "$SESSION" -n indexer \
    "$ACTIVATE && cd $PWDIR && python3 parser_apex_ai.py 2>&1 | tee -a logs_indexer.log"

# Окно 2 — бот
tmux new-window   -t "$SESSION" -n bot \
    "$ACTIVATE && cd $PWDIR/bot && python3 bot.py 2>&1 | tee -a $PWDIR/logs_bot.log"

# Окно 3 — разведчик (только если есть отдельная сессия)
if [ -f apex_discoverer_ai.session ]; then
    tmux new-window  -t "$SESSION" -n discoverer \
        "$ACTIVATE && cd $PWDIR && python3 chat_discoverer_apex.py 2>&1 | tee -a logs_discoverer.log"
    echo "🔍 Разведчик запущен (есть apex_discoverer_ai.session)"
else
    echo "⚠️  Разведчик ПРОПУЩЕН — нет apex_discoverer_ai.session"
    echo "   База уже накоплена (1412 чатов), без разведчика всё работает."
    echo "   Чтобы добавить разведчик, создай его сессию (см. RUNBOOK)."
fi

mkdir -p logs

echo ""
echo "✅ Всё запущено. Команды:"
echo "   tmux attach -t leadbot           # посмотреть логи"
echo "      → Ctrl+B затем 1/2/3 — переключиться между окнами"
echo "      → Ctrl+B затем D    — отключиться (всё продолжит работать)"
echo "   tmux kill-session -t leadbot     # остановить всё"
echo ""
echo "   Логи также пишутся в logs_*.log"
