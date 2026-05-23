"""
discoverer_v3.py — поиск чатов через Telegram API (Telethon).

ВАЖНО: использует ту же сессию что и парсер (apex_discoverer_v.session).
Парсер должен быть остановлен на время работы дискаверера, чтобы не было
конфликта SQLite на сессионном файле.

Используй обёртку run_discoverer_v3.sh — она сама останавливает парсер,
запускает дискаверер, потом стартует парсер обратно.

Что делает:
  1. Подключается к TG через сессию
  2. Через contacts.Search ищет публичные чаты по 30+ ключевым словам
  3. Фильтрует: только мегагруппы (чаты, не каналы), публичные, >200 участников
  4. Применяет blacklist (работа/вакансии/etc)
  5. Дедуп против target_chats
  6. Вставляет новые

Запуск:
    bash run_discoverer_v3.sh

Только дискаверер (без обёртки):
    cd /root/leadbot && source venv/bin/activate && python3 discoverer_v3.py

ENV:
    APEX_DB=/path/to/apex_ai.db
    DISCOVERER_SESSION=/path/to/session
    API_ID=...
    API_HASH=...
    MIN_PARTICIPANTS=200
"""
from __future__ import annotations

import asyncio
import os
import random
import sqlite3
import sys
from pathlib import Path

from telethon import TelegramClient, functions, types
from telethon.errors import FloodWaitError

# Импорт blacklist (рядом)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from blacklist import should_skip_chat


# ============================================
#                  НАСТРОЙКИ
# ============================================
HERE = Path(__file__).resolve().parent
APEX_DB = os.environ.get("APEX_DB") or str(HERE / "apex_ai.db")
SESSION_PATH = os.environ.get("DISCOVERER_SESSION") or str(HERE / "apex_discoverer_v.session")
API_ID = int(os.environ.get("API_ID") or "37992056")
API_HASH = os.environ.get("API_HASH") or "60613234526a75894811075d80c5b7f3"
MIN_PARTICIPANTS = int(os.environ.get("MIN_PARTICIPANTS") or "200")

# Поисковые запросы для contacts.Search.
# Каждый возвращает до 20 чатов. Всего ~30 запросов = ~5-10 минут работы.
QUERIES = [
    # B2B-чаты
    "бизнес чат",
    "предприниматели",
    "стартап",
    "нетворкинг",
    "b2b чат",
    "малый бизнес",
    # Маркетинг и реклама
    "маркетинг",
    "smm",
    "контекстная реклама",
    "директ чат",
    "директологи",
    "seo чат",
    "линкбилдинг",
    "трафик арбитраж",
    "телеграм реклама",
    # Веб и разработка
    "веб разработка",
    "разработка сайтов",
    "next.js",
    "frontend чат",
    # E-commerce
    "wildberries селлеры",
    "ozon селлеры",
    "маркетплейсы",
    "ecommerce",
    # Города (топовые по объёму бизнеса)
    "москва бизнес",
    "спб бизнес",
    "екатеринбург бизнес",
    "краснодар бизнес",
    "новосибирск бизнес",
    "казань бизнес",
    # Прочее B2B
    "автоматизация бизнеса",
    "ai для бизнеса",
    "франшизы",
]


# ============================================
#                  УТИЛИТЫ
# ============================================
def get_existing_chats(db_path: str) -> set[str]:
    db = sqlite3.connect(db_path)
    cur = db.cursor()
    try:
        cur.execute("SELECT chat_identifier FROM target_chats")
        return {r[0].lstrip("@").lower() for r in cur.fetchall() if r[0]}
    finally:
        db.close()


def get_target_chats_columns(db_path: str) -> list[str]:
    db = sqlite3.connect(db_path)
    cur = db.cursor()
    try:
        cur.execute("PRAGMA table_info(target_chats)")
        return [r[1] for r in cur.fetchall()]
    finally:
        db.close()


def insert_chats_batch(chats: list[str], db_path: str) -> tuple[int, int, int]:
    """Возвращает (inserted, duplicates, blacklisted)."""
    columns = get_target_chats_columns(db_path)
    has_is_processed = "is_processed" in columns
    sql = (
        "INSERT INTO target_chats (chat_identifier, is_processed) VALUES (?, 0)"
        if has_is_processed
        else "INSERT INTO target_chats (chat_identifier) VALUES (?)"
    )

    existing = get_existing_chats(db_path)
    db = sqlite3.connect(db_path)
    cur = db.cursor()
    inserted = 0
    duplicates = 0
    blacklisted = 0
    seen_in_run: set[str] = set()
    try:
        for chat in chats:
            key = chat.lstrip("@").lower()
            if key in seen_in_run:
                continue
            seen_in_run.add(key)
            if key in existing:
                duplicates += 1
                continue
            if should_skip_chat(chat):
                blacklisted += 1
                continue
            try:
                cur.execute(sql, (chat,))
                inserted += 1
                existing.add(key)
            except sqlite3.IntegrityError:
                duplicates += 1
        db.commit()
    finally:
        db.close()
    return (inserted, duplicates, blacklisted)


# ============================================
#                  ПОИСК ЧАТОВ
# ============================================
async def search_chats_for_query(client: TelegramClient, query: str) -> list[str]:
    """Через contacts.Search ищет публичные чаты-мегагруппы."""
    out: list[str] = []
    try:
        result = await client(functions.contacts.SearchRequest(q=query, limit=20))
    except FloodWaitError as e:
        wait = min(e.seconds, 600)
        print(f"\n  ⚠️  FloodWait {e.seconds}с — спим {wait}с...")
        await asyncio.sleep(wait)
        return []
    except Exception as e:
        print(f"\n  ⚠️  err: {type(e).__name__}: {e}")
        return []

    for chat in result.chats:
        # Только Channel-объекты (могут быть мегагруппой или каналом)
        if not isinstance(chat, types.Channel):
            continue
        # Только мегагруппы (чаты, не одностороний канал)
        if not getattr(chat, "megagroup", False):
            continue
        # Только публичные (с username)
        username = getattr(chat, "username", None)
        if not username:
            continue
        # Размер
        pcount = getattr(chat, "participants_count", None)
        if pcount is not None and pcount < MIN_PARTICIPANTS:
            continue
        out.append("@" + username)
    return out


# ============================================
#                  ОСНОВНОЕ
# ============================================
async def main() -> int:
    print("════════════════════════════════════════════════════════")
    print("    Discoverer v3 — Telegram API discovery")
    print("════════════════════════════════════════════════════════")
    print(f"📂 БД:         {APEX_DB}")
    print(f"🔑 Сессия:     {SESSION_PATH}")
    print(f"🔍 Запросов:   {len(QUERIES)}")
    print(f"🛑 Min уч-в:   {MIN_PARTICIPANTS}")
    print()

    if not Path(APEX_DB).exists():
        print(f"❌ БД не найдена: {APEX_DB}")
        return 1
    if not Path(SESSION_PATH).exists():
        print(f"❌ Сессия не найдена: {SESSION_PATH}")
        return 1

    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print("❌ Сессия не авторизована.")
            return 1
        me = await client.get_me()
        print(f"👤 Сессия активна: {me.first_name} (@{getattr(me, 'username', '?') or '?'})")
        print()

        all_chats: set[str] = set()
        per_query: list[tuple[str, int]] = []

        for i, q in enumerate(QUERIES, 1):
            print(f"  [{i:2d}/{len(QUERIES)}] q='{q}' ...", end="", flush=True)
            chats = await search_chats_for_query(client, q)
            new_unique = 0
            for c in chats:
                if c.lower() in {x.lower() for x in all_chats}:
                    continue
                all_chats.add(c)
                new_unique += 1
            print(f" → {len(chats)} найдено, {new_unique} новых")
            per_query.append((q, new_unique))
            # Анти-флуд: 5-12 сек между запросами + случайные «кофе-брейки»
            sleep_for = random.uniform(5, 12)
            if random.random() < 0.1:
                sleep_for = random.uniform(30, 60)
                print(f"     ☕ кофе-брейк: {sleep_for:.0f}с")
            await asyncio.sleep(sleep_for)

    finally:
        await client.disconnect()

    print()
    print(f"📦 Всего уникальных чатов найдено: {len(all_chats)}")
    print()

    inserted, duplicates, blacklisted = insert_chats_batch(list(all_chats), APEX_DB)

    print("════════════════════════════════════════════════════════")
    print("                    ИТОГИ")
    print("════════════════════════════════════════════════════════")
    print(f"✅ Добавлено новых:    {inserted}")
    print(f"⏭ Уже в базе:          {duplicates}")
    print(f"🚫 Blacklist отсёк:     {blacklisted}")

    db = sqlite3.connect(APEX_DB)
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM target_chats")
    total = cur.fetchone()[0]
    db.close()
    print()
    print(f"📈 Всего в target_chats сейчас: {total}")
    print()
    print("По запросам (новых):")
    for q, n in sorted(per_query, key=lambda x: -x[1]):
        if n > 0:
            print(f"  {n:3d}  {q}")
    print()
    print("✅ Готово. Парсер постепенно подхватит новые чаты (по 50/день).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
