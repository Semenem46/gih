# Apex Leadbot

Telegram-парсер лидов с AI-квалификацией (DeepSeek + Groq).

## Структура проекта

```
leadbot/
├── src/                    # Python-код
│   ├── paths.py            # Единый конфиг путей
│   ├── query_engine.py     # Движок поиска лидов (Groq + DeepSeek)
│   ├── parser_apex_ai.py   # Индексер сообщений из чатов
│   ├── live_push.py        # Live-мониторинг + push лидов
│   ├── ai_qualifier.py     # AI-квалификация сообщений
│   ├── blacklist.py        # Чёрный список чатов
│   ├── discoverer_v4.py    # Поиск новых целевых чатов (Groq)
│   ├── auto_crawler.py     # Автокраулер чатов
│   ├── make_lead_magnet.py # Генерация лид-магнита (xlsx)
│   ├── audit_chats.py      # Аудит чатов
│   └── bot/                # Telegram-бот (aiogram)
│       ├── bot.py          # Точка входа бота
│       ├── config.py       # Конфигурация из .env
│       ├── db.py           # Работа с leads.db
│       ├── apex_bridge.py  # Мост к query_engine
│       ├── keyboards.py    # Клавиатуры
│       ├── texts.py        # Тексты
│       ├── scheduler.py    # Drip-кампания
│       ├── .env.example    # Пример конфига
│       └── handlers/       # Хендлеры бота
├── data/                   # БД, сессии, JSON-стейт (не в git)
├── logs/                   # Логи (не в git)
├── tests/                  # Тесты
├── archive/                # Старые версии скриптов
├── start.sh                # Запуск всего в tmux
├── setup.sh                # Установка на чистый сервер
├── run_discoverer.sh       # Запуск discoverer с перезапуском парсера
├── watchdog.sh             # Watchdog (cron)
└── requirements.txt        # Python-зависимости
```

## Быстрый старт

```bash
git clone <repo> /root/leadbot
cd /root/leadbot
chmod +x setup.sh start.sh
./setup.sh
# Заполни src/bot/.env
# Положи .session файлы в data/
./start.sh
```

## Переменные окружения

```
GROQ_API_KEY=...           # Groq API (бесплатный, для анализа чатов)
DEEPSEEK_API_KEY=...       # DeepSeek API (для квалификации сообщений)
APEX_DB=...                # Путь к apex_ai.db (по умолчанию data/apex_ai.db)
```
