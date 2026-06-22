#!/bin/bash
# browser_helper.sh — быстрый скрапер для поиска email на внешних сайтах
URL=$1
if [ -z "$URL" ]; then exit 1; fi

# Используем быстрый режим agent-browser без скриншотов
# 1. Открываем
# 2. Ждем рендеринга (особенно важно для Linktree/Beacons)
# 3. Дампим текст
# 4. Ищем email регуляркой
timeout 20s agent-browser open "$URL" > /dev/null 2>&1
sleep 3
TEXT=$(agent-browser dump 2>/dev/null)

# Регулярка для поиска email в дампе
echo "$TEXT" | grep -Eoi "[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}" | sort -u | head -n 3
