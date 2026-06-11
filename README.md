# Apex Automation — YouTube Shorts Agency Lead Parser

Парсер лидов для агентства коротких вертикальных видео. Находит YouTube-авторов в нишах бизнеса, крипты, ИИ и финансов, которые НЕ делают Shorts — и предлагает им услуги по производству коротких видео.

## Архитектура

### Двухфазный процесс
1. **`--scan`** (ночной cron) — поиск каналов по ключевым словам через YouTube Search API → складывает в `raw_leads_queue`
2. **`--process`** (часовой cron) — берёт N лидов из очереди, каскадно валидирует, генерирует питч, отправляет

### Step-by-Step Gatekeeping (`validate_lead`)
Порядок проверок оптимизирован для максимальной экономии квоты API:

| Шаг | Проверка | Стоимость |
|-----|----------|-----------|
| 1 | Подписчики (10k-100k) + Гео (US/GB/CA/AU) | 1 YouTube API unit |
| 2 | Жёсткий текстовый блеклист (инфобиз, крипто-фермы, монтажёры) | 0 (данные из шага 1) |
| 3 | Наличие Instagram/Twitter/X в описании | 0 (данные из шага 1) |
| 4 | Длина видео 5-25 мин по последним 3 | 2 YouTube API units |
| 5 | Vision AI скоринг thumbnail (соло-спикер + студия) | 1 Groq Vision API call |
| 6 | Глубокий поиск контактов + OSINT | varies |

## Запуск

```bash
cp .env.example .env  # заполни ключи
pip install -r requirements.txt
python main.py --scan     # ночной поиск
python main.py --process  # обработка очереди
```

## Файлы
- `config.py` — конфигурация, ключи API, параметры фильтрации
- `youtube_parser.py` — парсер + каскадная валидация + Vision AI
- `main.py` — CLI-оркестратор (scan / process)
- `pitch_generator.py` — генерация питчей через Groq (Llama 3.3)
- `db.py` — утилиты работы с SQLite
- `email_sender.py` — отправка холодных писем через Gmail SMTP
- `browser_helper.sh` — OSINT-скрапер для поиска email на внешних сайтах
