#!/usr/bin/env bash
# Один скрипт, который ставит всё что нужно на чистом Ubuntu-сервере.
# Запускать ОДИН РАЗ после загрузки файлов.
#   chmod +x setup.sh && ./setup.sh
set -e

cd "$(dirname "$0")"

echo "🔧 [1/4] Ставлю системные пакеты..."
sudo apt update -qq
sudo apt install -y python3 python3-venv python3-pip tmux sqlite3

echo "🐍 [2/4] Создаю venv..."
python3 -m venv venv
source venv/bin/activate

echo "📦 [3/4] Ставлю Python-зависимости..."
pip install --quiet --upgrade pip
pip install --quiet telethon openai aiosqlite tenacity httpx aiohttp
pip install --quiet -r bot/requirements.txt

echo "📂 [4/4] Создаю недостающие папки..."
mkdir -p bot/assets

if [ ! -f bot/.env ]; then
    if [ -f bot/.env.example ]; then
        cp bot/.env.example bot/.env
        echo "⚠️  Создал bot/.env из примера — открой его и заполни:"
        echo "    nano bot/.env"
    else
        cat > bot/.env <<'EOF'
BOT_TOKEN=ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER
ADMIN_IDS=ВСТАВЬ_СЮДА_СВОЙ_USER_ID
AUTHOR_NAME=Никита
AUTHOR_USERNAME=stendapik
SUPPORT_USERNAME=stendapik
LINK_BOOK_CALL=
LINK_CASES=
LINK_CHANNEL=
DRIP_SCHEDULE_MINUTES=30,1440,4320,10080
LOG_LEVEL=INFO
EOF
        echo "⚠️  Создал bot/.env — открой и заполни BOT_TOKEN и ADMIN_IDS:"
        echo "    nano bot/.env"
    fi
fi

if [ ! -f apex_discoverer_v.session ]; then
    echo ""
    echo "❗️ Файла apex_discoverer_v.session нет."
    echo "   Это Telethon-авторизация — нужно создать локально на ноуте"
    echo "   и закинуть на сервер. Без неё парсер не запустится."
    echo "   Инструкция в RUNBOOK.md, шаг 3."
fi

echo ""
echo "✅ Готово. Дальше:"
echo "   1. nano bot/.env  — заполни BOT_TOKEN и ADMIN_IDS"
echo "   2. ./start.sh     — запустит всё в tmux"
