"""
backfill_source_policy.py — разовый скрипт для категоризации существующих чатов.

Запуск:
    cd /root/leadbot/src && python3 backfill_source_policy.py

Читает все записи из target_chats, классифицирует по title/chat_identifier
и обновляет source_policy + source_reason.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import APEX_DB as _PATHS_APEX_DB

import aiosqlite
from source_policy import classify_chat, ensure_source_policy_columns

APEX_DB = os.environ.get("APEX_DB") or _PATHS_APEX_DB


async def main():
    print(f"📂 БД: {APEX_DB}")

    async with aiosqlite.connect(APEX_DB) as db:
        await ensure_source_policy_columns(db)
        await db.commit()

        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT chat_identifier, title FROM target_chats") as cur:
            rows = await cur.fetchall()

        if not rows:
            print("⚠️ Таблица target_chats пуста.")
            return

        stats = {"allow": 0, "mixed": 0, "block": 0}
        for row in rows:
            chat_id = row["chat_identifier"] or ""
            title = row["title"] or ""
            policy, reason = classify_chat(title, chat_id)
            stats[policy] += 1

            await db.execute(
                "UPDATE target_chats SET source_policy = ?, source_reason = ? WHERE chat_identifier = ?",
                (policy, reason, chat_id),
            )

        await db.commit()

    print(f"✅ Backfill завершён: {len(rows)} чатов")
    print(f"   allow={stats['allow']}, mixed={stats['mixed']}, block={stats['block']}")


if __name__ == "__main__":
    asyncio.run(main())
