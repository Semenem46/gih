"""
discoverer_v2.py — расширение target_chats через DuckDuckGo + seed-файл.

ВАЖНО: НЕ использует Telegram API / Telethon. Только HTTP к публичным сайтам.

Источники (в порядке надёжности):
  1. chat_seeds.txt — твой ручной seed-файл с t.me ссылками (рекомендую)
  2. DuckDuckGo Lite — поиск site:t.me "ищу подрядчика" и т.п. (~50 запросов)
  3. Telega.in (резервно)
  4. Кастомные URL через ENV TGSTAT_URLS

Что делает:
  1. Читает чаты из всех источников
  2. Регексом вытаскивает все t.me/<username>
  3. Применяет blacklist (работа/вакансии/etc)
  4. Дедуп против target_chats
  5. Вставляет новые со status is_processed=0,
     парсер постепенно начнёт их сканить (по 50 новых/день)

Запуск:
    cd /root/leadbot && source venv/bin/activate
    python3 discoverer_v2.py

ENV (опц.):
    APEX_DB=/path/to/apex_ai.db
    SEED_FILE=/path/to/chat_seeds.txt
    TGSTAT_URLS="url1,url2,url3"
    DISCOVERER_LIMIT=2000
    SKIP_DDG=1                      — пропустить DuckDuckGo
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
SEED_FILE = os.environ.get("SEED_FILE") or str(Path(__file__).resolve().parent / "chat_seeds.txt")
LIMIT = int(os.environ.get("DISCOVERER_LIMIT") or "2000")
DELAY_BETWEEN = float(os.environ.get("DISCOVERER_DELAY") or "3.0")
SKIP_DDG = bool(os.environ.get("SKIP_DDG"))

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# DuckDuckGo Lite — без JS и без Cloudflare. Принимает GET ?q=...
DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
DDG_HTML_URL = "https://html.duckduckgo.com/html/"

# Поисковые запросы — каждый даёт 10-30 t.me ссылок
DDG_QUERIES = [
    # Прямые интент-фразы (целевые лиды)
    'site:t.me "ищу подрядчика"',
    'site:t.me "нужен директолог"',
    'site:t.me "ищем разработчика"',
    'site:t.me "ищу маркетолога"',
    'site:t.me "нужен seo"',
    'site:t.me "посоветуйте подрядчика"',
    'site:t.me "ищу команду"',
    # B2B-чаты по нишам
    'site:t.me "бизнес чат"',
    'site:t.me "чат предпринимателей"',
    'site:t.me "маркетинг чат"',
    'site:t.me "стартап чат"',
    'site:t.me "нетворкинг чат"',
    'site:t.me "B2B чат"',
    # Тематические
    'site:t.me "разработка сайтов"',
    'site:t.me "контекстная реклама"',
    'site:t.me "продвижение сайта"',
    'site:t.me "линкбилдинг"',
    'site:t.me "трафик и реклама"',
    'site:t.me "wildberries чат"',
    'site:t.me "ozon селлеры"',
    'site:t.me "маркетплейсы"',
    'site:t.me "e-commerce чат"',
    # Города
    'site:t.me москва предприниматели',
    'site:t.me спб бизнес чат',
    'site:t.me екатеринбург бизнес',
    'site:t.me новосибирск бизнес',
    'site:t.me краснодар бизнес',
    'site:t.me казань бизнес',
    'site:t.me ростов бизнес',
    'site:t.me челябинск бизнес',
    'site:t.me пермь бизнес',
    'site:t.me самара бизнес',
    # Профессии заказчиков
    'site:t.me "малый бизнес"',
    'site:t.me "ИП и ООО"',
    'site:t.me "франшизы чат"',
    'site:t.me "ритейл"',
    'site:t.me "строительный чат"',
    'site:t.me "ремонт чат"',
    'site:t.me "юристы для бизнеса"',
    # Дополнительные тематики
    'site:t.me "smm"',
    'site:t.me "telegram реклама"',
    'site:t.me "арбитраж трафика"',
    'site:t.me "автоматизация бизнеса"',
    'site:t.me "ai в бизнесе"',
    'site:t.me "crm чат"',
]

# Резервные источники (telega.in работает плохо, но пусть будет как запасной)
FALLBACK_SOURCES = [
    ("Telega.in / Бизнес",     "https://telega.in/catalog/ru/business"),
    ("Telega.in / Маркетинг",  "https://telega.in/catalog/ru/marketing"),
]

# Пользовательские URL через ENV
extra_urls = os.environ.get("TGSTAT_URLS", "").strip()
CUSTOM_SOURCES = []
if extra_urls:
    for i, u in enumerate(extra_urls.split(",")):
        u = u.strip()
        if u:
            CUSTOM_SOURCES.append((f"CUSTOM-{i+1}", u))


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
def fetch_html(url: str, timeout: int = 25, post_data: bytes | None = None) -> str | None:
    req = urllib.request.Request(
        url,
        data=post_data,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded" if post_data else "",
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


def search_duckduckgo(query: str) -> str | None:
    """Поиск через DuckDuckGo Lite (HTML, без JS и Cloudflare)."""
    import urllib.parse as up
    # Сначала пробуем lite (быстрее), если упадёт — html (более стабильный)
    for url in (DDG_LITE_URL, DDG_HTML_URL):
        # Лучше POST на lite, чтобы не словить редирект
        post = up.urlencode({"q": query, "kl": "ru-ru"}).encode()
        html = fetch_html(url, post_data=post)
        if html and "t.me/" in html:
            return html
        # GET fallback
        url_get = url + "?" + up.urlencode({"q": query, "kl": "ru-ru"})
        html = fetch_html(url_get)
        if html and "t.me/" in html:
            return html
    return None


def load_seed_chats(path: str) -> list[str]:
    """
    Читает chat_seeds.txt — каждая строка это t.me ссылка / @username / просто username.
    Строки с # игнорируются (комментарии).
    """
    p = Path(path)
    if not p.exists():
        return []
    chats: list[str] = []
    for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # извлекаем username из любого формата
        m = T_ME_RE.search(line)
        if m:
            chats.append("@" + m.group(1).lower())
            continue
        if line.startswith("@"):
            uname = line[1:].split()[0]
            if 5 <= len(uname) <= 32 and uname.replace("_", "").isalnum():
                chats.append("@" + uname.lower())
            continue
        # просто username без @
        if 5 <= len(line) <= 32 and line.replace("_", "").isalnum():
            chats.append("@" + line.lower())
    # дедуп
    seen = set()
    out = []
    for c in chats:
        if c.lower() in seen:
            continue
        seen.add(c.lower())
        out.append(c)
    return out


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
    print("    Discoverer v2 — DuckDuckGo + seed + telega.in")
    print("════════════════════════════════════════════════════════")
    print(f"📂 БД:           {APEX_DB}")
    print(f"🌱 Seed-файл:    {SEED_FILE} {'(есть)' if Path(SEED_FILE).exists() else '(нет)'}")
    print(f"🦆 DDG-запросов: {len(DDG_QUERIES) if not SKIP_DDG else 'SKIP'}")
    print(f"🛑 Лимит/прогон: {LIMIT}")
    print()

    if not Path(APEX_DB).exists():
        print(f"❌ БД не найдена: {APEX_DB}")
        return 1

    all_candidates: list[str] = []
    seen: set[str] = set()
    source_stats: list[tuple[str, int, int]] = []

    # ── 1. Seed-файл (приоритет) ─────────────────────
    print("─" * 56)
    print("🌱 Этап 1: чтение seed-файла")
    print("─" * 56)
    seed_chats = load_seed_chats(SEED_FILE)
    seed_added = 0
    for c in seed_chats:
        cl = c.lower()
        if cl in seen:
            continue
        seen.add(cl)
        all_candidates.append(c)
        seed_added += 1
    print(f"  → {seed_added} чатов из seed-файла")
    source_stats.append(("Seed-файл", seed_added, seed_added))

    # ── 2. DuckDuckGo поиск ──────────────────────────
    if not SKIP_DDG:
        print()
        print("─" * 56)
        print("🦆 Этап 2: поиск через DuckDuckGo Lite")
        print("─" * 56)
        for i, q in enumerate(DDG_QUERIES, start=1):
            print(f"  [{i:2d}/{len(DDG_QUERIES)}] q='{q[:55]}'", end=" ", flush=True)
            html = search_duckduckgo(q)
            if not html:
                print("⚠️  no results")
                source_stats.append((f"DDG: {q[:30]}", 0, 0))
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
            print(f"→ найдено {len(chats):3d}, новых: {new_unique:3d}")
            source_stats.append((f"DDG: {q[:30]}", len(chats), new_unique))
            time.sleep(DELAY_BETWEEN)

    # ── 3. Резервные источники + кастомные ───────────
    fallback = FALLBACK_SOURCES + CUSTOM_SOURCES
    if fallback:
        print()
        print("─" * 56)
        print(f"🌐 Этап 3: резервные источники ({len(fallback)})")
        print("─" * 56)
        for name, url in fallback:
            print(f"  {name}: {url}")
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
            print(f"    → найдено {len(chats)}, новых: {new_unique}")
            source_stats.append((name, len(chats), new_unique))
            time.sleep(DELAY_BETWEEN)

    print()
    print(f"📦 Всего уникальных кандидатов: {len(all_candidates)}")
    print(f"\n💾 Вставляю в БД (blacklist + dedup, лимит={LIMIT})...")
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

    db = sqlite3.connect(APEX_DB)
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM target_chats")
    total = cur.fetchone()[0]
    db.close()
    print()
    print(f"📈 Всего в target_chats сейчас: {total}")
    print()

    if stats["inserted"] == 0:
        print("⚠️  Ничего не добавлено. Если DuckDuckGo тоже падает — заполни вручную")
        print(f"   файл {SEED_FILE} (по одному t.me-username в строке) и перезапусти.")
    else:
        print("✅ Готово. Парсер постепенно (по 50 новых/день) подхватит новые чаты.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
