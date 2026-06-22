import sqlite3
from datetime import datetime, timezone

from config import DB_PATH, logger

def get_connection() -> sqlite3.Connection:
    """Open a SQLite connection with row access by column name."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    """Create the agency.db database and the channels table if needed."""
    conn = get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                channel_id       TEXT PRIMARY KEY,
                channel_name     TEXT,
                custom_url       TEXT,
                description      TEXT,
                contact_email    TEXT,
                processed_at     TIMESTAMP,
                pitch_generated  TEXT,
                status           TEXT DEFAULT 'parsed'
            )
            """
        )
        conn.commit()
        logger.info("Database initialized at %s", DB_PATH)
    finally:
        conn.close()

def is_channel_processed(channel_id: str) -> bool:
    """Return True if the channel already exists in the database."""
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT 1 FROM channels WHERE channel_id = ? LIMIT 1",
            (channel_id,),
        )
        return cursor.fetchone() is not None
    finally:
        conn.close()

def save_channel(channel_data: dict) -> None:
    """
    Insert or update a channel record.
    Expected keys in channel_data:
        channel_id (required), channel_name, custom_url, description,
        contact_email, pitch_generated, status, processed_at (optional ISO str).
    """
    processed_at = channel_data.get("processed_at") or datetime.now(
        timezone.utc
    ).isoformat()

    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO channels (
                channel_id, channel_name, custom_url, description,
                contact_email, processed_at, pitch_generated, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                channel_name    = excluded.channel_name,
                custom_url      = excluded.custom_url,
                description     = excluded.description,
                contact_email   = excluded.contact_email,
                processed_at    = excluded.processed_at,
                pitch_generated = excluded.pitch_generated,
                status          = excluded.status
            """,
            (
                channel_data["channel_id"],
                channel_data.get("channel_name"),
                channel_data.get("custom_url"),
                channel_data.get("description"),
                channel_data.get("contact_email"),
                processed_at,
                channel_data.get("pitch_generated"),
                channel_data.get("status", "parsed"),
            ),
        )
        conn.commit()
        logger.info(
            "Saved channel %s (%s) with status '%s'",
            channel_data["channel_id"],
            channel_data.get("channel_name"),
            channel_data.get("status", "parsed"),
        )
    finally:
        conn.close()
