"""
discoverer_v4.py — Groq-динамический поиск чатов под ЛЮБУЮ нишу.

Архитектура:
  1. Раз в 3 часа Groq генерит поисковые запросы на основе:
     - ниш платных клиентов (из paid_clients)
     - популярных бизнес-ниш (fallback)
  2. Ищет чаты через Telegram contacts.Search
  3. Groq валидирует качество каждого чата (B2B / не спам)
  4. Новые чаты → target_chats → парсер начинает их краулить

Использует Groq (бесплатный API) для генерации запросов и валидации.
DeepSeek не трогаем — он только для квалификации сообщений.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from telethon import TelegramClient, functions, types
from telethon.errors import FloodWaitError
from openai import AsyncOpenAI

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from paths import APEX_DB as _PATHS_APEX_DB, DISCOVERER_V4_SESSION as _PATHS_V4_SESSION, DISCOVERER_BUDGET_FILE as _PATHS_BUDGET
from blacklist import should_skip_chat
from source_policy import classify_chat, ensure_source_policy_columns

APEX_DB = os.environ.get("APEX_DB") or _PATHS_APEX_DB
SESSION_PATH = os.environ.get("DISCOVERER_V4_SESSION") or _PATHS_V4_SESSION
API_ID = 37992056
API_HASH = "60613234526a75894811075d80c5b7f3"
MIN_PARTICIPANTS = 150

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
groq = AsyncOpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1", max_retries=2, timeout=30.0)

FALLBACK_QUERIES = [
    # Только целевые запросы с высоким коммерческим интентом
    "маркетинг чат", "smm чат", "seo чат",
    "контекстная реклама", "директ чат", "таргет чат",
    "подрядчики чат", "ищу подрядчика",
    "нужен маркетолог", "реклама яндекс",
    "лидогенерация", "лидген чат",
    "директологи чат", "таргетологи чат",
    "веб разработка чат", "дизайн заказ",
]


async def get_client_niches() -> list[str]:
    """Собирает ниши платных клиентов для генерации запросов."""
    niches = []
    try:
        with sqlite3.connect(APEX_DB) as db:
            rows = db.execute(
                "SELECT niche_text FROM paid_clients WHERE status = 'active'"
            ).fetchall()
            niches = [r[0] for r in rows if r[0]]
    except Exception:
        pass
    return niches


async def generate_search_queries(niches: list[str]) -> list[str]:
    """
    Groq генерит поисковые запросы на основе ниш клиентов.
    Возвращает список запросов для Telegram contacts.Search.
    """
    if not niches:
        return FALLBACK_QUERIES

    niche_list = "\n".join(f"- {n}" for n in niches[:5])
    prompt = f"""Ты — эксперт по поиску Telegram-чатов для B2B-лидгена.

Клиенты работают в следующих нишах:
{niche_list}

Сгенерируй 20 поисковых запросов (на русском) для поиска Telegram-чатов,
где тусуются потенциальные клиенты этих ниш.

Правила:
- Каждый запрос — 2-4 слова
- Ищи ТОЛЬКО узкоцелевые B2B-чаты, где публикуются КОММЕРЧЕСКИЕ ЗАПРОСЫ на услуги
- Примеры хороших: "маркетинг чат", "seo заказчики", "директ подрядчики", "лидогенерация чат"
- НЕ ищи: "работа", "вакансии", "резюме", "фриланс", "нетворкинг", "предприниматели", "стартап"
- НЕ ищи: "заработок", "крипта", "инвестиции", "биржа"

Ответь СТРОГО JSON: {{"queries": ["запрос1", "запрос2", ...]}}"""

    try:
        resp = await groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.7,
        )
        data = json.loads(resp.choices[0].message.content)
        queries = data.get("queries", [])
        print(f"🧠 Groq сгенерил {len(queries)} запросов под ниши клиентов")
    except Exception as e:
        print(f"⚠️ Groq query-gen error: {e}, fallback")
        queries = FALLBACK_QUERIES

    queries.extend(FALLBACK_QUERIES)
    seen = set()
    out = []
    for q in queries:
        q = q.strip().lower()
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out


async def validate_chat_with_groq(username: str, title: str, sample_msgs: list[str]) -> tuple[bool, str]:
    """Groq валидирует чат: B2B / спам / вакансии."""
    if not sample_msgs:
        return False, "пустой чат"

    msgs_text = "\n".join(sample_msgs[:12])
    prompt = f"""Ты — аудитор Telegram-чатов.

Чат: @{username} («{title}»)
Последние сообщения:
{msgs_text[:1500]}

Определи:
1. Это живой чат с реальными людьми? (не бот-спам, не канал-помойка)
2. Здесь общаются предприниматели/бизнесмены/специалисты? (не только школьники)
3. Есть ли коммерческие обсуждения? (заказы, услуги, сотрудничество)

ОТВЕТЬ СТРОГО JSON: {{"is_good": true/false, "category": "b2b"/"b2c"/"job_board"/"spam"/"inactive", "reason": "кратко по-русски"}}"""

    try:
        resp = await groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        data = json.loads(resp.choices[0].message.content)
        return data.get("is_good", False), data.get("reason", "?")
    except Exception as e:
        return False, f"ошибка: {e}"


async def discover_chats(client: TelegramClient, queries: list[str]) -> list[dict]:
    """Ищет чаты через Telegram API по заданным запросам."""
    found = []
    known = set()

    with sqlite3.connect(APEX_DB) as db:
        rows = db.execute("SELECT chat_identifier FROM target_chats").fetchall()
        known = {r[0] for r in rows}

    random.shuffle(queries)
    for query in queries[:30]:
        try:
            result = await client(functions.contacts.SearchRequest(
                q=query,
                limit=15,
            ))

            for chat in result.chats:
                if not isinstance(chat, (types.Channel, types.Chat)):
                    continue
                if chat.broadcast:
                    continue
                username = getattr(chat, "username", None)
                if not username:
                    continue
                if username.lower() in known:
                    continue

                participants = getattr(chat, "participants_count", 0) or 0
                if participants < MIN_PARTICIPANTS:
                    continue

                title = getattr(chat, "title", "") or username
                if should_skip_chat(title):
                    continue

                found.append({
                    "username": username.lower(),
                    "title": title,
                    "query": query,
                    "participants": participants,
                    "chat_obj": chat,
                })
                known.add(username.lower())

            await asyncio.sleep(random.uniform(1.5, 3.0))
        except FloodWaitError as e:
            print(f"⏳ FloodWait {e.seconds}s — sleep")
            await asyncio.sleep(e.seconds + 5)
        except Exception as e:
            print(f"⚠️ Search error '{query}': {e}")
            await asyncio.sleep(2)

    return found


async def sample_messages(client: TelegramClient, username: str) -> list[str]:
    try:
        msgs = await client.get_messages(username, limit=20)
        return [m.message for m in msgs if m.message]
    except Exception:
        return []


async def main_loop():
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.start()
    print("📡 Discoverer v4 подключён к Telegram")

    while True:
        try:
            niches = await get_client_niches()
            queries = await generate_search_queries(niches)
            print(f"🔍 Поиск: {len(queries)} запросов, ниши клиентов: {niches[:3]}")

            candidates = await discover_chats(client, queries)
            print(f"📋 Найдено {len(candidates)} кандидатов")

            approved = 0
            for c in candidates[:25]:
                msgs = await sample_messages(client, c["username"])
                is_good, reason = await validate_chat_with_groq(
                    c["username"], c["title"], msgs
                )
                status = "approved" if is_good else "rejected"
                print(f"  @{c['username']}: {status} — {reason}")

                policy, policy_reason = classify_chat(c["title"], c["username"])
                if not is_good:
                    policy = "block"
                    policy_reason = f"groq_rejected: {reason}"

                with sqlite3.connect(APEX_DB) as db:
                    db.execute(
                        """INSERT OR IGNORE INTO target_chats
                           (chat_identifier, title, source_query, members_count, is_processed,
                            source_policy, source_reason)
                           VALUES (?, ?, ?, ?, 0, ?, ?)""",
                        (c["username"], c["title"], c["query"],
                         c.get("participants", 0), policy, policy_reason),
                    )
                    db.commit()

                if is_good and policy != "block":
                    approved += 1
                    with sqlite3.connect(APEX_DB) as db:
                        db.execute(
                            "INSERT OR IGNORE INTO chat_offsets (chat_key, last_id) VALUES (?, 0)",
                            (c["username"],),
                        )
                        db.commit()

                await asyncio.sleep(random.uniform(3, 7))

            print(f"✅ Одобрено {approved} новых чатов из {len(candidates)} кандидатов")

        except Exception as e:
            print(f"💥 Ошибка цикла: {e}")

        wait = random.uniform(10800, 14400)
        print(f"💤 Сон {wait/3600:.1f} часов до следующего цикла")
        await asyncio.sleep(wait)


if __name__ == "__main__":
    asyncio.run(main_loop())
