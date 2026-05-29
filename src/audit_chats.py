"""
Audit lead-magnet chats against the live corpus.

Reads the 88-chat Excel file, queries the corpus DB for each chat's
recent activity & intent-matching messages, then prints a ranked report
and writes the result to audit_result.xlsx.

Run on the production server where apex_ai.db has data:

    cd ~/leadbot && source venv/bin/activate && python3 audit_chats.py

Safe to run while parser is live (read-only on the corpus DB).
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import DATA_DIR, SRC_DIR

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

DB_PATH = DATA_DIR / "apex_ai.db"
XLSX_IN = SRC_DIR / "bot" / "assets" / "База_TG-чатов_для_лидгена_2026.xlsx"
XLSX_OUT = DATA_DIR / "audit_result.xlsx"
TOP_88_SHEET = "💎 ТОП-88 чатов"  # с пробелом после ромбика

# Triggers — слова, которые в реальной заявке на услугу почти всегда есть
INTENT_PATTERNS = [
    r"\bищу\b", r"\bнужен\b", r"\bнужна\b", r"\bнужны\b",
    r"\bпосовет", r"\bкто\s+может\b", r"\bкуда\s+обратит",
    r"\bпорекоменд", r"\bподскажите\b", r"\bтребуется\b",
    r"\bподряд", r"\bбригад", r"\bподскажите\b",
    r"кто\s+делает", r"делал\s+кто", r"\bищу\s+",
]
INTENT_RE = re.compile("|".join(INTENT_PATTERNS), re.IGNORECASE)

# Спам-маркеры (если в сообщении есть — почти всегда мусор)
SPAM_PATTERNS = [
    r"купи\s+курс", r"приглашаю\s+на", r"бесплатный\s+вебинар",
    r"@@", r"казино", r"став(ки|ка)\s+на\s+спорт",
]
SPAM_RE = re.compile("|".join(SPAM_PATTERNS), re.IGNORECASE)


def load_chats_from_xlsx() -> list[dict]:
    """Read the 88-chat list from the lead-magnet Excel."""
    if not XLSX_IN.exists():
        raise FileNotFoundError(f"Не найден файл {XLSX_IN}")
    wb = openpyxl.load_workbook(XLSX_IN, data_only=True)
    if TOP_88_SHEET not in wb.sheetnames:
        raise RuntimeError(f"В файле нет листа «{TOP_88_SHEET}»")
    ws = wb[TOP_88_SHEET]
    chats: list[dict] = []
    for row in ws.iter_rows(min_row=3, values_only=True):
        if not row or row[0] is None:
            continue
        idx, category, name, username, _link, members, descr = (
            (row + (None,) * 7)[:7]
        )
        if not username:
            continue
        chats.append({
            "n": idx,
            "category": (category or "").strip(),
            "name": (name or "").strip(),
            "username": str(username).lstrip("@").strip(),
            "members": members or 0,
            "descr": (descr or "").strip(),
        })
    return chats


def query_corpus_stats(chat_username: str) -> dict:
    """Return activity stats for a single chat over last 7d/30d."""
    if not DB_PATH.exists():
        return {"err": "no_db"}
    now = datetime.now(timezone.utc)
    cutoff_7 = now - timedelta(days=7)
    cutoff_30 = now - timedelta(days=30)

    chat_keys = [chat_username, f"@{chat_username}", chat_username.lower()]
    placeholders = ",".join("?" for _ in chat_keys)

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                f"SELECT msg_date, text FROM messages_corpus "
                f"WHERE chat_key IN ({placeholders}) AND msg_date >= ?",
                (*chat_keys, cutoff_30.isoformat()),
            )
            rows = cur.fetchall()
        except sqlite3.OperationalError as e:
            return {"err": f"db: {e}"}

    total_30 = len(rows)
    total_7 = 0
    intent_30 = 0
    intent_7 = 0
    spam_30 = 0
    last_msg = None

    for msg_date_str, text in rows:
        try:
            msg_date = datetime.fromisoformat(msg_date_str)
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if last_msg is None or msg_date > last_msg:
            last_msg = msg_date
        if msg_date >= cutoff_7:
            total_7 += 1
            if INTENT_RE.search(text or ""):
                intent_7 += 1
        if INTENT_RE.search(text or ""):
            intent_30 += 1
        if SPAM_RE.search(text or ""):
            spam_30 += 1

    return {
        "total_30": total_30,
        "total_7": total_7,
        "intent_30": intent_30,
        "intent_7": intent_7,
        "spam_30": spam_30,
        "last_msg": last_msg.isoformat() if last_msg else None,
    }


def score_chat(chat: dict, stats: dict) -> tuple[float, str]:
    """Return (score, verdict)."""
    if stats.get("err"):
        return 0.0, "❓ нет данных"

    intent_30 = stats.get("intent_30", 0)
    total_30 = stats.get("total_30", 0)
    last_msg = stats.get("last_msg")
    members = chat.get("members", 0) or 0

    if total_30 == 0:
        return 0.0, "⏳ корпус ещё пуст по этому чату"

    # сколько % сообщений «с интентом»
    intent_share = (intent_30 / total_30) if total_30 else 0
    score = (
        intent_30 * 1.0
        + (total_30 / 100.0)
        + intent_share * 50
        + (5 if members and members > 1000 else 0)
    )

    if not last_msg:
        return score, "⚠️ дата неизвестна"

    last_dt = datetime.fromisoformat(last_msg)
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - last_dt).days

    if age_days > 14:
        return score / 2, f"💀 мёртвый ({age_days} дн)"
    if intent_30 >= 5:
        return score, "🔥 живой, есть лиды"
    if intent_30 >= 1:
        return score, "🟡 средний"
    return score, "⚪ активный, но без лидов"


def main() -> None:
    chats = load_chats_from_xlsx()
    print(f"Загружено {len(chats)} чатов из xlsx")
    print(f"Корпус: {DB_PATH.resolve()}")
    print()

    if not DB_PATH.exists():
        print("⚠️  Файл apex_ai.db не найден в текущей папке.")
        print("    Запусти скрипт из ~/leadbot/")
        return

    # Проверяем что таблица корпуса существует
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='messages_corpus'"
        )
        if not cur.fetchone():
            print("⚠️  В базе ещё нет таблицы messages_corpus.")
            print("    Парсер либо не запущен, либо ещё не успел")
            print("    дойти до записи (FloodWait).")
            print("    Подожди час-два и запусти снова.")
            return
        cur.execute("SELECT COUNT(*) FROM messages_corpus")
        total = cur.fetchone()[0]
        print(f"В корпусе сейчас: {total:,} сообщений всего")
        print()

    rated: list[dict] = []
    for i, chat in enumerate(chats, 1):
        stats = query_corpus_stats(chat["username"])
        score, verdict = score_chat(chat, stats)
        rated.append({**chat, **stats, "score": score, "verdict": verdict})
        if i % 10 == 0 or i == len(chats):
            print(f"[{i}/{len(chats)}] обработано")

    rated.sort(key=lambda x: -x.get("score", 0))

    by_verdict: dict[str, int] = defaultdict(int)
    for r in rated:
        by_verdict[r["verdict"]] += 1

    print()
    print("=" * 72)
    print("ИТОГ ПО ВЕРДИКТАМ:")
    for v, c in sorted(by_verdict.items(), key=lambda x: -x[1]):
        print(f"  {v:30}  {c}")
    print("=" * 72)
    print()
    print("ТОП-30 ЛИВЫХ ЧАТОВ:")
    print(f"{'#':>3} {'username':<28} {'msg30':>5} {'lead30':>6} {'verdict'}")
    for i, r in enumerate(rated[:30], 1):
        print(
            f"{i:>3} @{r['username']:<27} "
            f"{r.get('total_30', 0) or 0:>5} "
            f"{r.get('intent_30', 0) or 0:>6} "
            f"{r.get('verdict', '')}"
        )
    print()

    # --- Save to xlsx ---
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Audit"
    headers = [
        "#", "Категория", "Название", "Юзернейм", "Участники",
        "Сообщ 30д", "Сообщ 7д", "Лидов 30д", "Лидов 7д", "Спам 30д",
        "Последнее сообщ", "Score", "Вердикт",
    ]
    ws.append(headers)
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="1F2937")
    for c in ws[1]:
        c.font = head_font
        c.fill = head_fill
        c.alignment = Alignment(vertical="center")

    for i, r in enumerate(rated, 1):
        ws.append([
            i,
            r.get("category", ""),
            r.get("name", ""),
            f"@{r['username']}",
            r.get("members", 0),
            r.get("total_30", 0) or 0,
            r.get("total_7", 0) or 0,
            r.get("intent_30", 0) or 0,
            r.get("intent_7", 0) or 0,
            r.get("spam_30", 0) or 0,
            (r.get("last_msg") or "")[:19].replace("T", " "),
            round(r.get("score", 0), 2),
            r.get("verdict", ""),
        ])

    # column widths
    widths = [4, 24, 38, 22, 10, 10, 8, 10, 10, 10, 20, 8, 24]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[chr(64 + i) if i <= 26 else "AA"].width = w

    ws.freeze_panes = "A2"
    wb.save(XLSX_OUT)
    print(f"📄 Результат записан: {XLSX_OUT.resolve()}")


if __name__ == "__main__":
    main()
