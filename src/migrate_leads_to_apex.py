#!/usr/bin/env python3
"""Миграция таблиц leads, events, scheduled_messages из leads.db → apex_ai.db.

Запуск (один раз):
    cd /root/leadbot/src && python3 migrate_leads_to_apex.py

Если таблицы уже существуют в apex_ai.db — скрипт дополнит их данными
(INSERT OR IGNORE для leads, INSERT для events/scheduled_messages).
"""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import DATA_DIR

APEX_DB = os.environ.get("APEX_DB") or str(DATA_DIR / "apex_ai.db")
LEADS_DB = str(DATA_DIR / "leads.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    user_id        INTEGER PRIMARY KEY,
    username       TEXT,
    first_name     TEXT,
    last_name      TEXT,
    phone          TEXT,
    utm_source     TEXT,
    file_sent_at   TIMESTAMP,
    is_subscribed  INTEGER NOT NULL DEFAULT 1,
    created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    event       TEXT NOT NULL,
    payload     TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id);

CREATE TABLE IF NOT EXISTS scheduled_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    step        INTEGER NOT NULL,
    run_at      TIMESTAMP NOT NULL,
    sent        INTEGER NOT NULL DEFAULT 0,
    sent_at     TIMESTAMP,
    UNIQUE(user_id, step)
);
CREATE INDEX IF NOT EXISTS idx_sched_run ON scheduled_messages(sent, run_at);
"""


def migrate():
    if not Path(LEADS_DB).exists():
        print(f"[INFO] leads.db not found at {LEADS_DB} — nothing to migrate.")
        return

    print(f"[INFO] Source: {LEADS_DB}")
    print(f"[INFO] Target: {APEX_DB}")

    dst = sqlite3.connect(APEX_DB)
    dst.execute("PRAGMA journal_mode = WAL")
    dst.execute("PRAGMA busy_timeout = 5000")
    dst.executescript(SCHEMA)

    src = sqlite3.connect(LEADS_DB)
    src.row_factory = sqlite3.Row

    # --- leads ---
    rows = src.execute("SELECT * FROM leads").fetchall()
    migrated_leads = 0
    for r in rows:
        try:
            dst.execute(
                "INSERT OR IGNORE INTO leads (user_id, username, first_name, last_name, phone, utm_source, file_sent_at, is_subscribed, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["user_id"], r["username"], r["first_name"], r["last_name"], r["phone"], r["utm_source"], r["file_sent_at"], r["is_subscribed"], r["created_at"]),
            )
            if dst.execute("SELECT changes()").fetchone()[0] > 0:
                migrated_leads += 1
        except Exception as e:
            print(f"[WARN] leads row {r['user_id']}: {e}")
    print(f"[OK] leads: {migrated_leads} new / {len(rows)} total")

    # --- events ---
    rows = src.execute("SELECT * FROM events").fetchall()
    migrated_events = 0
    for r in rows:
        try:
            dst.execute(
                "INSERT INTO events (user_id, event, payload, created_at) VALUES (?, ?, ?, ?)",
                (r["user_id"], r["event"], r["payload"], r["created_at"]),
            )
            migrated_events += 1
        except Exception as e:
            print(f"[WARN] events row: {e}")
    print(f"[OK] events: {migrated_events} / {len(rows)}")

    # --- scheduled_messages ---
    rows = src.execute("SELECT * FROM scheduled_messages").fetchall()
    migrated_sched = 0
    for r in rows:
        try:
            dst.execute(
                "INSERT OR IGNORE INTO scheduled_messages (user_id, step, run_at, sent, sent_at) VALUES (?, ?, ?, ?, ?)",
                (r["user_id"], r["step"], r["run_at"], r["sent"], r["sent_at"]),
            )
            if dst.execute("SELECT changes()").fetchone()[0] > 0:
                migrated_sched += 1
        except Exception as e:
            print(f"[WARN] scheduled_messages row: {e}")
    print(f"[OK] scheduled_messages: {migrated_sched} new / {len(rows)} total")

    dst.commit()
    dst.close()
    src.close()
    print("[DONE] Migration complete.")


if __name__ == "__main__":
    migrate()
