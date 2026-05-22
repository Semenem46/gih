# AI-Qualifier Deploy

Одна команда на сервере:

```bash
cd /tmp && rm -rf gih_pkg && \
git clone --depth 1 -b ai-qualifier https://github.com/Semenem46/gih.git gih_pkg && \
bash /tmp/gih_pkg/leadbot/deploy_ai.sh
```

## Что встанет
- `live_push.py` v2 — с blacklist чатов и эксклюзивностью лидов
- `ai_qualifier.py` (новый) — DeepSeek-фильтр между live_push и /review
- `blacklist.py` — regex-фильтр работа/вакансии чатов
- `bot/handlers/review.py` v2 — карточка показывает AI score / pain / fit_service
- `watchdog.sh` + cron — раз в 30 мин проверяет парсер, рестартит если мёртв 60+ мин

## После деплоя

В TG-боте проверь:
```
/qstats     ← новая команда: полная статистика по всем статусам
/queue      ← старая, простая
/review     ← теперь видишь AI-оценку в карточке
```

## Восстановление если сломалось

`deploy_ai.sh` автоматом делает .bak файлы перед заменой. Откат:
```bash
cd /root/leadbot
ls *.bak_*  # увидишь когда был бэкап
cp live_push.py.bak_<TS> live_push.py
cp bot/handlers/review.py.bak_<TS> bot/handlers/review.py
cp apex_ai.db.backup_<TS> apex_ai.db
# рестарт процессов как обычно
```

## Что AI отсеивает (score < 80)

- Соискатели вакансий, продавцы услуг
- Просьбы советов, обсуждения, флуд
- Инфоцыгане, накрутка, крипта
- Сообщения не по теме клиента (даже с триггер-словом)

## Логи

- `live_push.log` — что ловится по FTS5
- `ai_qualifier.log` — что AI пропустил/отсеял + причины
- `watchdog.log` — кто и когда рестартанул парсер
