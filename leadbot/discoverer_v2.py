"""
discoverer_v2.py — расширение target_chats через парсинг публичных каталогов.

ВАЖНО: НЕ использует Telegram API / Telethon. Никаких сессий, никакого риска
флуд-бана. Просто HTTP-запросы к публичным сайтам (как обычный браузер).

Источники:
  - tgstat.ru   (рейтинги по категориям)
  - tgstat.ru/chats/<category>
  - telega.in   (каталог чатов)
  - кастомные URL через ENV TGSTAT_URLS

Что делает:
  1. Качает HTML страниц из SOURCES
  2. Регексом вытаскивает все t.me/<username>
  3. Применяет blacklist (работа/вакансии/etc)
  4. Дедуп против target_chats
  5. Вставляет новые в target_chats со стандартным is_processed=0,
     парсер постепенно начнёт их сканить (по 50 новых/день)

Запуск:
    cd /root/leadbot && source venv/bin/activate
    python3 discoverer_v2.py

ENV (опц.):
    APEX_DB=/path/to/apex_ai.db
    TGSTAT_URLS="url1,url2,url3"   — добавить кастомные источники
    DISCOVERER_LIMIT=2000          — макс новых чатов за прогон
"""
from __future__ import annotations

import gzip
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Импорт blacklist (рядом)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from blacklist import should_skip_chat


# ============================================
#                  НАСТРОЙКИ
# ============================================
APEX_DB = os.environ.get("APEX_DB") or str(Path(__file__).resolve().parent / "apex_ai.db")
LIMIT = int(os.environ.get("DISCOVERER_LIMIT") or "2000")
DELAY_BETWEEN = float(os.environ.get("DISCOVERER_DELAY") or "3.0")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Источники — категории из TGStat и Telega.in.
DEFAULT_SOURCES = [
    # ===== TGStat — основные тематики =====
    ("TGStat / Бизнес и стартапы",         "https://tgstat.ru/business"),
    ("TGStat / Бизнес-чаты",                "https://tgstat.ru/chats/business"),
    ("TGStat / Маркетинг и PR",             "https://tgstat.ru/marketing-pr"),
    ("TGStat / Маркетинг чаты",             "https://tgstat.ru/chats/marketing-pr"),
    ("TGStat / Технологии",                 "https://tgstat.ru/tech"),
    ("TGStat / Технологии чаты",            "https://tgstat.ru/chats/tech"),
    ("TGStat / Образование",                "https://tgstat.ru/education"),
    ("TGStat / Карьера",                    "https://tgstat.ru/career"),
    ("TGStat / Экономика",                  "https://tgstat.ru/economics"),
    ("TGStat / Продажи и e-com",            "https://tgstat.ru/sales"),
    ("TGStat / Дизайн",                     "https://tgstat.ru/design"),
    ("TGStat / Недвижимость",               "https://tgstat.ru/realty"),
    ("TGStat / Право",                      "https://tgstat.ru/law"),
    # ===== TGStat — регионы (где сидят владельцы малого бизнеса) =====
    ("TGStat / Москва",                     "https://tgstat.ru/cities/moscow"),
    ("TGStat / Санкт-Петербург",            "https://tgstat.ru/cities/spb"),
    ("TGStat / Екатеринбург",               "https://tgstat.ru/cities/ekaterinburg"),
    ("TGStat / Новосибирск",                "https://tgstat.ru/cities/novosibirsk"),
    ("TGStat / Краснодар",                  "https://tgstat.ru/cities/krasnodar"),
    ("TGStat / Казань",                     "https://tgstat.ru/cities/kazan"),
    # ===== Telega.in — каталог =====
    ("Telega.in / Бизнес",                  "https://telega.in/catalog/ru/business"),
    ("Telega.in / Маркетинг",               "https://telega.in/catalog/ru/marketing"),
    ("Telega.in / Технологии",              "https://telega.in/catalog/ru/technologies-and-internet"),
    ("Telega.in / Экономика",               "https://telega.in/catalog/ru/economy"),
    ("Telega.in / Образование",             "https://telega.in/catalog/ru/education-and-self-development"),
]

# Пользователь может добавить свои URL через ENV
extra_urls = os.environ.get("TGSTAT_URLS", "").strip()
if extra_urls:
    for i, u in enumerate(extra_urls.split(",")):
        u = u.strip()
        if u:
            DEFAULT_SOURCES.append((f"CUSTOM-{i+1}", u))


# ============================================
#               REGEX
# ============================================
T_ME_RE = re.compile(
    r"(?:https?://)?t(?:elegram)?\.me/([a-zA-Z][a-zA-Z0-9_]{4,31})",
    re.IGNORECASE,
)

# Системные/служебные TG-пути — не username
TG_RESERVED = {
    "share", "iv", "joinchat", "addstickers", "addtheme",
    "proxy", "socks", "setlanguage", "addemoji", "login",
    "telegram", "contact", "android", "premium", "apps",
    "wallpapers", "gigagroup", "joinforum", "addlist",
    "img", "static", "tgstat", "telegacat",
}


# ============================================
#            HTTP fetch
# ============================================
def fetch_html(url: str, timeout: int = 25) -> str | None:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            if r.headers.get("Content-Encoding", "").lower() == "gzip":
                data = gzip.decompress(data)
            return data.decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        print(f"    ⚠️  HTTP {e.code}: {e.reason}")
        return None
    except urllib.error.URLError as e:
        print(f"    ⚠️  URL error: {e.reason}")
        return None
    except Exception as e:
        print(f"    ⚠️  fetch error: {type(e).__name__}: {e}")
        return None


# ============================================
#           Извлечение чатов из HTML
# ============================================
def extract_chats_from_html(html: str | None) -> list[str]:
    if not html:
        return []
    matches = T_ME_RE.findall(html)
    seen: set[str] = set()
    out: list[str] = []
    for m in matches:
        m_low = m.lower()
        if m_low in TG_RESERVED:
            continue
        if len(m_low) < 5:
            continue
        if m_low.startswith("_") or m_low.endswith("_"):
            continue
        if m_low in seen:
            continue
        seen.add(m_low)
        out.append("@" + m_low)
    return out


# ============================================
#           Вставка в target_chats
# ============================================
def get_existing_chats(db_path: str) -> set[str]:
    db = sqlite3.connect(db_path)
    cur = db.cursor()
    try:
        cur.execute("SELECT chat_identifier FROM target_chats")
        return {r[0].lower() for r in cur.fetchall() if r[0]}
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


def insert_new_chats(chats: list[str], db_path: str, max_insert: int) -> dict:
    existing = get_existing_chats(db_path)
    columns = get_target_chats_columns(db_path)
    has_is_processed = "is_processed" in columns
    if has_is_processed:
        sql = "INSERT INTO target_chats (chat_identifier, is_processed) VALUES (?, 0)"
    else:
        sql = "INSERT INTO target_chats (chat_identifier) VALUES (?)"

    db = sqlite3.connect(db_path)
    cur = db.cursor()

    inserted = 0
    blacklisted = 0
    duplicates = 0
    errors = 0
    seen_in_run: set[str] = set()

    try:
        for chat in chats:
            if inserted >= max_insert:
                break
            chat_low = chat.lower()
            if chat_low in seen_in_run:
                continue
            seen_in_run.add(chat_low)

            if chat_low in existing:
                duplicates += 1
                continue
            if should_skip_chat(chat):
                blacklisted += 1
                continue
            try:
                cur.execute(sql, (chat,))
                inserted += 1
            except sqlite3.IntegrityError:
                duplicates += 1
            except Exception as e:
                errors += 1
                print(f"    ! INSERT {chat}: {e}")
        db.commit()
    finally:
        db.close()

    return {
        "inserted": inserted,
        "blacklisted": blacklisted,
        "duplicates": duplicates,
        "errors": errors,
    }


# ============================================
#                  ОСНОВНОЕ
# ============================================
def main() -> int:
    print("════════════════════════════════════════════════════════")
    print("    Discoverer v2 — расширение базы чатов")
    print("════════════════════════════════════════════════════════")
    print(f"📂 БД:           {APEX_DB}")
    print(f"📋 Источников:   {len(DEFAULT_SOURCES)}")
    print(f"🛑 Лимит/прогон: {LIMIT}")
    print(f"⏱  Пауза между:  {DELAY_BETWEEN}с")
    print()

    if not Path(APEX_DB).exists():
        print(f"❌ БД не найдена: {APEX_DB}")
        return 1

    all_candidates: list[str] = []
    seen: set[str] = set()
    source_stats: list[tuple[str, int, int]] = []

    for i, (name, url) in enumerate(DEFAULT_SOURCES, start=1):
        print(f"[{i}/{len(DEFAULT_SOURCES)}] 🌐 {name}")
        print(f"    URL: {url}")
        html = fetch_html(url)
        if not html:
            source_stats.append((name, 0, 0))
            time.sleep(DELAY_BETWEEN)
            continue

        chats = extract_chats_from_html(html)
        new_unique = 0
        for c in chats:
            cl = c.lower()
            if cl in seen:
                continue
            seen.add(cl)
            all_candidates.append(c)
            new_unique += 1

        source_stats.append((name, len(chats), new_unique))
        print(f"    → найдено {len(chats)} t.me-ссылок, новых уник.: {new_unique}")
        time.sleep(DELAY_BETWEEN)

    print()
    print(f"📦 Всего уникальных кандидатов: {len(all_candidates)}")
    print(f"\n💾 Вставляю в БД (с blacklist + dedup, лимит={LIMIT})...")
    stats = insert_new_chats(all_candidates, APEX_DB, LIMIT)

    print()
    print("════════════════════════════════════════════════════════")
    print("                    ИТОГИ")
    print("════════════════════════════════════════════════════════")
    print(f"✅ Добавлено новых:    {stats['inserted']}")
    print(f"⏭ Уже в базе:          {stats['duplicates']}")
    print(f"🚫 Blacklist отсёк:     {stats['blacklisted']}")
    if stats["errors"]:
        print(f"⚠️  Ошибок INSERT:      {stats['errors']}")

    print()
    print("📊 По источникам:")
    print(f"  {'Источник':<40} {'Найдено':>10} {'Уник':>10}")
    print(f"  {'-'*40} {'-'*10:>10} {'-'*10:>10}")
    for name, found, uniq in source_stats:
        print(f"  {name:<40} {found:>10} {uniq:>10}")

    db = sqlite3.connect(APEX_DB)
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM target_chats")
    total = cur.fetchone()[0]
    db.close()
    print()
    print(f"📈 Всего в target_chats сейчас: {total}")
    print()
    print("✅ Готово. Парсер постепенно (по 50 новых/день) подхватит новые чаты.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
