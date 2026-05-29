"""
live_push.py v3 — concierge live-monitor с поддержкой vertical-strict FTS.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
import aiosqlite
import httpx

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from blacklist import should_skip_chat
except ImportError:
    def should_skip_chat(_): return False

try:
    from query_engine import _passes_pre_ai_filter
except ImportError:
    def _passes_pre_ai_filter(text, min_cyrillic: float = 0.4):
        return True, ""

from paths import APEX_DB as _PATHS_APEX_DB
APEX_DB = os.environ.get("APEX_DB") or _PATHS_APEX_DB
BOT_TOKEN = os.environ.get("BOT_TOKEN") or "8565672652:AAGpwT7Lg50bSL-SDBgwG15ci0BcSydNAU4"
ADMIN_ID = int(os.environ.get("APEX_ADMIN_ID") or "7531405698")
POLL_INTERVAL = 30
NOTIFY_COOLDOWN = 30 * 60
MAX_MESSAGE_AGE_DAYS = 7
MAX_MATCHES_PER_CLIENT_PER_TICK = 200
INITIAL_STATUS = "ai_pending"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("live_push")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

async def tune_pragmas(db: aiosqlite.Connection) -> None:
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA synchronous=NORMAL;")
    await db.execute("PRAGMA wal_autocheckpoint=10000;")
    await db.execute("PRAGMA mmap_size=30000000000;")
    await db.execute("PRAGMA cache_size=-262144;")
    await db.execute("PRAGMA temp_store=MEMORY;")
    await db.execute("PRAGMA busy_timeout=10000;")

async def wal_checkpoint_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            async with aiosqlite.connect(APEX_DB) as db:
                await db.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception as e:
            logger.warning(f"wal_checkpoint failed: {e}")

async def _ensure_columns(db: aiosqlite.Connection) -> None:
    cols = [
        ("ai_score", "INTEGER"),
        ("ai_pain", "TEXT"),
        ("ai_fit_service", "TEXT"),
        ("ai_reason", "TEXT"),
        ("ai_processed_at", "TIMESTAMP"),
        ("ai_dm_text", "TEXT"),
        ("ai_dm_username", "TEXT"),
        ("ai_dm_fit", "TEXT"),
    ]
    for name, typ in cols:
        try:
            await db.execute(f"ALTER TABLE pending_review ADD COLUMN {name} {typ}")
        except Exception:
            pass

async def init_tables(db_path: str = APEX_DB) -> None:
    async with aiosqlite.connect(db_path) as db:
        await tune_pragmas(db)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS paid_clients (
                user_id          INTEGER PRIMARY KEY,
                client_name      TEXT,
                niche_text       TEXT NOT NULL,
                niche_keywords   TEXT NOT NULL,
                started_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at       TIMESTAMP,
                status           TEXT DEFAULT 'active',
                last_corpus_id   INTEGER DEFAULT 0,
                notes            TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_paid_status ON paid_clients(status)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pending_review (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                corpus_id       INTEGER NOT NULL,
                client_user_id  INTEGER NOT NULL,
                status          TEXT DEFAULT 'pending',
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reviewed_at     TIMESTAMP,
                FOREIGN KEY(corpus_id) REFERENCES messages_corpus(id),
                UNIQUE(corpus_id, client_user_id)
            )
        """)
        await _ensure_columns(db)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_review(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pending_client ON pending_review(client_user_id, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pending_ai_status ON pending_review(status, id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pending_corpus_status ON pending_review(corpus_id, status)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS delivered_leads (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                corpus_id       INTEGER NOT NULL,
                client_user_id  INTEGER NOT NULL,
                delivered_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(corpus_id, client_user_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_delivered_client ON delivered_leads(client_user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_delivered_corpus ON delivered_leads(corpus_id)")
        await db.commit()
    logger.info("📂 Таблицы готовы (v3)")

async def get_active_clients(db_path: str = APEX_DB) -> list[dict]:
    async with aiosqlite.connect(APEX_DB) as db:
        try:
            async with db.execute(
                """
                SELECT user_id, client_name, niche_text, niche_keywords, last_corpus_id,
                       expires_at, COALESCE(mode, 'lead-finder') AS mode
                FROM paid_clients
                WHERE status = 'active'
                  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                """
            ) as cur:
                rows = await cur.fetchall()
        except aiosqlite.OperationalError:
            async with db.execute(
                """
                SELECT user_id, client_name, niche_text, niche_keywords, last_corpus_id,
                       expires_at
                FROM paid_clients
                WHERE status = 'active'
                  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                """
            ) as cur:
                rows = [list(r) + ['lead-finder'] for r in await cur.fetchall()]
        return [
            {
                "user_id": r[0],
                "name": r[1] or f"user_{r[0]}",
                "niche": r[2],
                "keywords": r[3],
                "last_id": r[4] or 0,
                "expires_at": r[5],
                "mode": r[6] or "lead-finder",
            }
            for r in rows
        ]

_FTS_SAFE_RE = re.compile(r"[^\w\-\.\s]", re.UNICODE)

def parse_keywords(kw_field: str) -> list[str]:
    if not kw_field:
        return []
    raw = kw_field.strip()
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            return [str(x).strip() for x in data if str(x).strip()]
        except Exception:
            pass
    return [p.strip() for p in raw.split(",") if p.strip()]

def build_fts_query(keywords: list[str]) -> str:
    parts: list[str] = []
    for kw in keywords:
        kw = kw.lower().strip()
        if not kw:
            continue
        kw_clean = _FTS_SAFE_RE.sub(" ", kw).strip()
        if not kw_clean:
            continue
        if " " in kw_clean or "-" in kw_clean:
            parts.append(f'"{kw_clean}"')
        else:
            parts.append(f'"{kw_clean}"*')
    return " OR ".join(parts)

def _fts_parts_from_list(keywords: list[str]) -> list[str]:
    parts = []
    for kw in keywords:
        k = _FTS_SAFE_RE.sub(" ", (kw or "").lower()).strip()
        if not k:
            continue
        if " " in k or "-" in k:
            parts.append(f'"{k}"')
        else:
            parts.append(f'"{k}"*')
    return parts

def build_fts_query_from_niche(niche_info: dict) -> str:
    svc_parts = _fts_parts_from_list(niche_info.get("service_keywords") or [])
    if not svc_parts:
        return ""
    svc_q = " OR ".join(svc_parts)
    if niche_info.get("vertical_strict"):
        vert_parts = _fts_parts_from_list(niche_info.get("vertical_keywords") or [])
        if vert_parts:
            return f"({svc_q}) AND ({' OR '.join(vert_parts)})"
    return svc_q

async def scan_for_client(client: dict, db_path: str = APEX_DB) -> list[tuple[int, str | None, str | None]]:
    raw = client["keywords"] or ""
    niche_info: dict = {}
    if raw.strip().startswith("{"):
        try:
            niche_info = json.loads(raw)
        except Exception:
            niche_info = {}
    if niche_info:
        fts_query = build_fts_query_from_niche(niche_info)
    else:
        keywords = parse_keywords(raw)
        fts_query = build_fts_query(keywords) if keywords else ""
    if not fts_query:
        return []
    age_cutoff = (datetime.utcnow() - timedelta(days=MAX_MESSAGE_AGE_DAYS)).isoformat()
    async with aiosqlite.connect(db_path) as db:
        try:
            sql = """
            SELECT mc.id, mc.chat_key, mc.text
            FROM messages_fts
            JOIN messages_corpus mc ON mc.id = messages_fts.rowid
            LEFT JOIN delivered_leads dl
            ON dl.corpus_id = mc.id AND dl.client_user_id = ?
            WHERE messages_fts MATCH ?
            AND mc.id > ?
            AND mc.msg_date >= ?
            AND dl.id IS NULL
            AND NOT EXISTS (
                SELECT 1 FROM pending_review pr2
                WHERE pr2.corpus_id = mc.id
                AND pr2.status IN ('ai_pending', 'pending', 'sent')
            )
            ORDER BY mc.id ASC
            LIMIT ?
            """
            async with db.execute(
                sql,
                (
                    client["user_id"],
                    fts_query,
                    client["last_id"],
                    age_cutoff,
                    MAX_MATCHES_PER_CLIENT_PER_TICK,
                ),
            ) as cur:
                rows = await cur.fetchall()
            return [(r[0], r[1], r[2]) for r in rows]
        except Exception as e:
            logger.warning(
                f"FTS-query упал для {client['user_id']} ({client['name']}): {e} | q={fts_query!r}"
            )
            return []

async def queue_candidates(
    client_uid: int,
    candidates: list[tuple[int, str | None, str | None]],
    db_path: str = APEX_DB,
    client_mode: str = "lead-finder",
    niche_info: dict | None = None,
) -> tuple[int, int, int]:
    if not candidates:
        return (0, 0, 0)
    apply_text_filter = (client_mode == "lead-finder")
    neg_verts = (niche_info or {}).get("negative_verticals", [])
    inserted = 0
    blacklisted = 0
    text_filtered = 0
    neg_vert_filtered = 0
    max_id = 0
    async with aiosqlite.connect(db_path) as db:
        for cid, chat_key, text in candidates:
            if cid > max_id:
                max_id = cid
            if should_skip_chat(chat_key):
                blacklisted += 1
                continue
            if apply_text_filter:
                passed, _reason = _passes_pre_ai_filter(text or "")
                if not passed:
                    text_filtered += 1
                    continue
            if neg_verts:
                tl = (text or "").lower()
                if any(nv in tl for nv in neg_verts):
                    neg_vert_filtered += 1
                    continue
            try:
                cur = await db.execute(
                    "INSERT OR IGNORE INTO pending_review "
                    "(corpus_id, client_user_id, status) VALUES (?, ?, ?)",
                    (cid, client_uid, INITIAL_STATUS),
                )
                inserted += cur.rowcount
            except Exception as e:
                logger.warning(f"insert fail (cid={cid}, uid={client_uid}): {e}")
        if max_id:
            await db.execute(
                "UPDATE paid_clients SET last_corpus_id = ? WHERE user_id = ?",
                (max_id, client_uid),
            )
        await db.commit()
    if text_filtered or neg_vert_filtered:
        logger.info(
            f"  uid={client_uid} [{client_mode}]: pre-AI={text_filtered}, neg_vert={neg_vert_filtered}"
        )
    return (inserted, blacklisted, max_id)

async def notify_admin(text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10.0) as cli:
        try:
            await cli.post(
                url,
                json={"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML"},
            )
        except Exception as e:
            logger.warning(f"notify_admin failed: {e}")

async def main_loop() -> None:
    await init_tables()
    asyncio.create_task(wal_checkpoint_loop())
    logger.info(
        "📡 live_push v3 запущен. POLL=%ds, AGE=%dd, ADMIN=%s, status='%s'",
        POLL_INTERVAL, MAX_MESSAGE_AGE_DAYS, ADMIN_ID, INITIAL_STATUS,
    )
    last_notify = datetime.utcnow() - timedelta(seconds=NOTIFY_COOLDOWN)
    pending_since_notify = 0
    blacklisted_since_notify = 0
    while True:
        try:
            clients = await get_active_clients()
            if clients:
                tick_inserted = 0
                tick_blacklisted = 0
                for c in clients:
                    raw = c["keywords"] or ""
                    niche_info: dict = {}
                    if raw.strip().startswith("{"):
                        try:
                            niche_info = json.loads(raw)
                        except Exception:
                            niche_info = {}
                    cands = await scan_for_client(c)
                    if not cands:
                        continue
                    ins, bl, max_id = await queue_candidates(
                        c["user_id"], cands,
                        client_mode=c.get("mode", "lead-finder"),
                        niche_info=niche_info,
                    )
                    tick_inserted += ins
                    tick_blacklisted += bl
                    if ins > 0 or bl > 0:
                        logger.info(
                            f"  '{c['name']}' (uid={c['user_id']}): "
                            f"+{ins} ai_pending, {bl} blacklist, max_id={max_id}"
                        )
                if tick_inserted or tick_blacklisted:
                    pending_since_notify += tick_inserted
                    blacklisted_since_notify += tick_blacklisted
                    logger.info(
                        f"🧮 Тик: +{tick_inserted} ai_pending, {tick_blacklisted} blacklist"
                    )
                age = (datetime.utcnow() - last_notify).total_seconds()
                if (pending_since_notify > 0 or blacklisted_since_notify > 5) and age >= NOTIFY_COOLDOWN:
                    msg = (
                        f"📡 <b>live_push:</b>\n"
                        f"+{pending_since_notify} в очереди AI\n"
                        f"⏭ {blacklisted_since_notify} blacklist отсеяно"
                    )
                    await notify_admin(msg)
                    pending_since_notify = 0
                    blacklisted_since_notify = 0
                    last_notify = datetime.utcnow()
            else:
                logger.debug("Активных клиентов нет.")
        except Exception as e:
            logger.exception(f"Цикл упал: {e}")
        await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("👋 Остановка по Ctrl+C")
