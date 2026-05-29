#!/usr/bin/env bash
# Один скрипт, который ставит всё что нужно на чистом Ubuntu-сервере.
# Запускать ОДИН РАЗ после загрузки файлов.
#   chmod +x setup.sh && ./setup.sh
set -e

cd "$(dirname "$0")"

echo "🔧 [1/5] Ставлю системные пакеты..."
sudo apt update -qq
sudo apt install -y python3 python3-venv python3-pip tmux sqlite3

echo "🐍 [2/5] Создаю venv..."
python3 -m venv venv
source venv/bin/activate

echo "📦 [3/5] Ставлю Python-зависимости..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "📂 [4/5] Создаю структуру папок..."
mkdir -p data logs tests archive src/bot/assets

echo "🔐 [5/5] Проверяю .env..."
if [ ! -f src/bot/.env ]; then
    if [ -f src/bot/.env.example ]; then
        cp src/bot/.env.example src/bot/.env
        echo "⚠️  Создал src/bot/.env из примера — открой его и заполни:"
        echo "    nano src/bot/.env"
    else
        cat > src/bot/.env <<'EOF'
BOT_TOKEN=ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER
ADMIN_IDS=ВСТАВЬ_СЮДА_СВОЙ_USER_ID
AUTHOR_NAME=Никита
AUTHOR_USERNAME=nikita_Apex
SUPPORT_USERNAME=nikita_Apex
LINK_BOOK_CALL=
LINK_CASES=
LINK_CHANNEL=
DRIP_SCHEDULE_MINUTES=30,1440,4320,10080
LOG_LEVEL=INFO
EOF
        echo "⚠️  Создал src/bot/.env — открой и заполни BOT_TOKEN и ADMIN_IDS:"
        echo "    nano src/bot/.env"
    fi
fi

if [ ! -f data/apex_discoverer_v.session ]; then
    echo ""
    echo "❗️ Файла data/apex_discoverer_v.session нет."
    echo "   Это Telethon-авторизация — нужно создать локально на ноуте"
    echo "   и закинь на сервер в папку data/."
    echo "   Инструкция в RUNBOOK.md, шаг 3."
fi

echo ""
echo "✅ Готово. Дальше:"
echo "   1. nano src/bot/.env  — заполни BOT_TOKEN и ADMIN_IDS"
echo "   2. ./start.sh        — запустит всё в tmux"
