"""SQLite-хранилище лидов, событий воронки и отложенных сообщений."""
from __future__ import annotations

import datetime as dt
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from config import DB_PATH


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


def init() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with connect() as cx:
        cx.executescript(SCHEMA)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    cx = sqlite3.connect(
        DB_PATH,
        detect_types=sqlite3.PARSE_DECLTYPES,
        isolation_level=None,  # autocommit
    )
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA journal_mode = WAL")
    cx.execute("PRAGMA foreign_keys = ON")
    cx.execute("PRAGMA busy_timeout = 5000")
    try:
        yield cx
    finally:
        cx.close()


# ── Leads ────────────────────────────────────────────────────────────────────

def upsert_lead(
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    utm_source: str | None,
) -> bool:
    """Создаёт лид, если ещё нет. Возвращает True, если лид новый."""
    with connect() as cx:
        cur = cx.execute("SELECT user_id FROM leads WHERE user_id = ?", (user_id,))
        if cur.fetchone():
            cx.execute(
                """UPDATE leads
                   SET username = COALESCE(?, username),
                       first_name = COALESCE(?, first_name),
                       last_name = COALESCE(?, last_name)
                 WHERE user_id = ?""",
                (username, first_name, last_name, user_id),
            )
            return False
        cx.execute(
            """INSERT INTO leads (user_id, username, first_name, last_name, utm_source)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, username, first_name, last_name, utm_source),
        )
        return True


def set_phone(user_id: int, phone: str) -> None:
    with connect() as cx:
        cx.execute("UPDATE leads SET phone = ? WHERE user_id = ?", (phone, user_id))


def mark_file_sent(user_id: int) -> None:
    with connect() as cx:
        cx.execute(
            "UPDATE leads SET file_sent_at = ? WHERE user_id = ? AND file_sent_at IS NULL",
            (dt.datetime.utcnow(), user_id),
        )


def set_subscribed(user_id: int, value: bool) -> None:
    with connect() as cx:
        cx.execute(
            "UPDATE leads SET is_subscribed = ? WHERE user_id = ?",
            (1 if value else 0, user_id),
        )


def get_lead(user_id: int) -> sqlite3.Row | None:
    with connect() as cx:
        return cx.execute("SELECT * FROM leads WHERE user_id = ?", (user_id,)).fetchone()


def all_leads() -> list[sqlite3.Row]:
    with connect() as cx:
        return list(cx.execute("SELECT * FROM leads ORDER BY created_at DESC"))


def subscribed_user_ids() -> list[int]:
    with connect() as cx:
        return [r[0] for r in cx.execute(
            "SELECT user_id FROM leads WHERE is_subscribed = 1"
        )]


# ── Events ───────────────────────────────────────────────────────────────────

def log_event(user_id: int, event: str, payload: str | None = None) -> None:
    with connect() as cx:
        cx.execute(
            "INSERT INTO events (user_id, event, payload) VALUES (?, ?, ?)",
            (user_id, event, payload),
        )


# ── Scheduler ────────────────────────────────────────────────────────────────

def schedule_message(user_id: int, step: int, run_at: dt.datetime) -> None:
    with connect() as cx:
        cx.execute(
            """INSERT OR IGNORE INTO scheduled_messages (user_id, step, run_at)
               VALUES (?, ?, ?)""",
            (user_id, step, run_at),
        )


def due_messages(now: dt.datetime) -> list[sqlite3.Row]:
    with connect() as cx:
        return list(cx.execute(
            """SELECT id, user_id, step, run_at FROM scheduled_messages
               WHERE sent = 0 AND run_at <= ?
               ORDER BY run_at""",
            (now,),
        ))


def mark_message_sent(message_id: int) -> None:
    with connect() as cx:
        cx.execute(
            "UPDATE scheduled_messages SET sent = 1, sent_at = ? WHERE id = ?",
            (dt.datetime.utcnow(), message_id),
        )


def cancel_drip(user_id: int) -> None:
    """Отменить ещё не отправленные follow-up для лида (например, при /unsubscribe)."""
    with connect() as cx:
        cx.execute(
            "UPDATE scheduled_messages SET sent = 1, sent_at = ? "
            "WHERE user_id = ? AND sent = 0",
            (dt.datetime.utcnow(), user_id),
        )


# ── Stats ────────────────────────────────────────────────────────────────────

def stats() -> dict[str, int]:
    with connect() as cx:
        total = cx.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        got_file = cx.execute(
            "SELECT COUNT(*) FROM leads WHERE file_sent_at IS NOT NULL"
        ).fetchone()[0]
        with_phone = cx.execute(
            "SELECT COUNT(*) FROM leads WHERE phone IS NOT NULL"
        ).fetchone()[0]
        unsubscribed = cx.execute(
            "SELECT COUNT(*) FROM leads WHERE is_subscribed = 0"
        ).fetchone()[0]
        today = cx.execute(
            "SELECT COUNT(*) FROM leads WHERE date(created_at) = date('now')"
        ).fetchone()[0]
        return {
            "total":        total,
            "got_file":     got_file,
            "with_phone":   with_phone,
            "unsubscribed": unsubscribed,
            "today":        today,
        }


def stats_by_utm() -> list[tuple[str, int]]:
    with connect() as cx:
        return [
            (r["utm_source"] or "—", r["c"])
            for r in cx.execute(
                "SELECT utm_source, COUNT(*) AS c FROM leads "
                "GROUP BY utm_source ORDER BY c DESC"
            )
        ]
