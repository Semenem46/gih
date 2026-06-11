import re
import base64
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone

import httpx
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import (
    YOUTUBE_API_KEY,
    GROQ_API_KEY,
    GROQ_BASE_URL,
    MIN_SUBSCRIBERS,
    MAX_SUBSCRIBERS,
    SHORT_MAX_SECONDS,
    RECENT_WINDOW_DAYS,
    MAX_RECENT_SHORTS,
    RECENT_VIDEOS_TO_CHECK,
    MAX_CHANNELS_PER_KEYWORD,
    APEX_DB,
    logger,
)

# ==========================================================================
# КОНСТАНТЫ / РЕГУЛЯРКИ
# ==========================================================================
EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
SOCIAL_REGEX = re.compile(
    r"(https?://(?:www\.)?(?:instagram|twitter|x|tiktok|t\.me|telegram|discord|patreon)\.[^\s)]+)",
    re.IGNORECASE,
)

EMAIL_VALIDATION_REGEX = re.compile(r"^[A-Za-z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[A-Za-z]{2,}$")

DISPOSABLE_EMAIL_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "temp-mail.org", "throwawaymail.com", "yopmail.com", "getnada.com",
    "trashmail.com", "sharklasers.com", "dispostable.com", "maildrop.cc",
    "fakeinbox.com", "mailnesia.com", "mintemail.com", "tempmailo.com",
    "emailondeck.com", "moakt.com", "spam4.me", "mailcatch.com",
}

ROLE_LOCALPARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "postmaster",
    "abuse", "mailer-daemon", "webmaster", "hostmaster", "support",
    "help", "helpdesk",
}

# Визуальные соцсети (признак Talking Head — качает личный бренд)
VISUAL_SOCIAL_DOMAINS = {"instagram.com", "tiktok.com", "twitter.com", "x.com"}

# --------------------------------------------------------------------------
# STEP 1 CONSTANTS: Подписчики + Гео
# --------------------------------------------------------------------------
ALLOWED_COUNTRIES = {"US", "GB", "CA", "AU", "NZ", "IE"}

# --------------------------------------------------------------------------
# STEP 2 CONSTANTS: Жёсткий текстовый блеклист
# --------------------------------------------------------------------------
# Инфобиз по продвижению каналов, крипто-фермы, монтажёры, нецелевые языки/гео
STRICT_BLACKLIST = [
    # Нецелевые языки / гео
    "brasil", "brazil", "deutsch", "germany", "taller", "español", "espanol",
    "lanka", "sinhala", "india", "philippines", "tamiles", "arabic", "urdu",
    "hindi", "indonesia", "français", "francais", "рус", "russia",
    # Инфобиз по продвижению каналов (мусор)
    "grow channel", "grow your channel", "youtube tips", "youtube growth",
    "get more subscribers", "video editing tips", "content creator tips",
    "personal brand coach", "content creator", "how to grow on youtube",
    "youtube algorithm", "youtube secrets", "youtube strategy",
    "faceless channel", "faceless youtube", "automation channel",
    # Крипто-фермы и мусорные крипто-каналы
    "crypto club", "crypto farm", "mining rig", "mining setup",
    "crypto signals", "pump and dump", "bitcoin mining",
    # Монтажёры / фрилансеры (не авторы контента)
    "video editor for hire", "editing portfolio", "freelance editor",
    "motion graphics", "after effects tutorial",
    # Мусорные форматы
    "clips", "shorts", "reels", "compilation", "full show",
    "weekly", "daily", "empire", "zone", "news", "tv",
    "podcast clips", "funny clips", "car review",
]

# Кириллица, арабица, CJK, хангыль
NON_LATIN_REGEX = re.compile(
    r"[\u0400-\u04FF\u0600-\u06FF\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]"
)

# Стоп-токены стримов
STREAM_WORD_TOKENS = {"live", "stream", "podcast", "webinar", "livestream"}

# Маркеры скриншер-туториалов (нет лица в кадре)
SCREENSHARE_MARKERS = {
    "guide", "tutorial", "review", "how to trade", "how to", "chart",
    "setup", "analysis", "walkthrough", "step by step",
}

NON_ENGLISH_TITLE_WORDS = {
    "der", "die", "das", "und", "ist", "mit", "von", "wie",
    "el", "la", "los", "las", "en", "y", "para", "por", "con",
}
_WORD_TOKEN_REGEX = re.compile(r"[a-zA-Z\u00c0-\u00ff]+")

# --------------------------------------------------------------------------
# STEP 4 CONSTANTS: Длина видео (строго 5-25 минут)
# --------------------------------------------------------------------------
MIN_VIDEO_SECONDS = 300   # 5 минут
MAX_VIDEO_SECONDS = 1500  # 25 минут

# ==========================================================================
# VISION AI: Промпт для анализа студийности thumbnail
# ==========================================================================
VISION_SCORING_PROMPT = """You are an expert visual quality assessor for a premium video agency that works ONLY with high-end solo creators (Talking Heads) who film in professional studios.

Analyze this YouTube video thumbnail and score it on a strict pass/fail basis.

MANDATORY CRITERIA (ALL must be true to PASS):
1. SOLO SPEAKER: Exactly ONE person is visible, facing the camera directly (eye contact with viewer). NOT a group, NOT a side profile, NOT a back view.
2. PROFESSIONAL STUDIO/STYLISH INTERIOR: The background must clearly show ONE of:
   - A professional studio setup (ring lights, acoustic panels, LED panels, clean backdrop, branded set)
   - A high-end, stylish interior (modern office, expensive furniture, clean minimalist design)
   
   INSTANT FAIL backgrounds:
   - Blurred hallway, corridor, or doorway
   - Bedroom, messy room, kitchen
   - Car interior (filming from driver seat)
   - Generic webcam look (low angle, bad lighting, ceiling visible)
   - Outdoor/street scenes
   - Plain white/colored wall with nothing else

3. NO SCREEN DOMINANCE: The thumbnail must NOT be dominated by:
   - Screenshots of websites, apps, or software
   - Trading charts, candlestick graphs, stock tickers
   - Spreadsheets, tables, or data visualizations
   - Text overlays covering more than 30% of the image
   - Multiple small images/collages

RESPOND IN EXACTLY THIS JSON FORMAT:
{"pass": true/false, "confidence": 0-100, "reason": "one-line explanation"}

Be EXTREMELY strict. When in doubt, FAIL. We only want premium, studio-quality creators."""

# ==========================================================================
# БД: единое соединение с таймаутом 30с
# ==========================================================================
def _connect(db_path: str = APEX_DB) -> sqlite3.Connection:
    return sqlite3.connect(db_path, timeout=30)


def init_lead_tables(db_path: str = APEX_DB) -> None:
    with _connect(db_path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""
            CREATE TABLE IF NOT EXISTS raw_leads_queue (
                channel_id     TEXT PRIMARY KEY,
                channel_name   TEXT,
                status         TEXT DEFAULT 'pending',
                discovered_via TEXT,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at   TIMESTAMP
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS blacklisted_channels (
                channel_id TEXT PRIMARY KEY,
                reason     TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_queue_status ON raw_leads_queue(status)")
        db.commit()


def is_channel_blacklisted(channel_id: str, db_path: str = APEX_DB) -> bool:
    with _connect(db_path) as db:
        cur = db.execute(
            "SELECT 1 FROM blacklisted_channels WHERE channel_id = ?", (channel_id,)
        )
        return cur.fetchone() is not None


def add_to_blacklist(channel_id: str, reason: str, db_path: str = APEX_DB) -> None:
    with _connect(db_path) as db:
        db.execute(
            "INSERT OR IGNORE INTO blacklisted_channels (channel_id, reason) VALUES (?, ?)",
            (channel_id, reason),
        )
        db.commit()


def is_known_channel(channel_id: str, db_path: str = APEX_DB) -> bool:
    with _connect(db_path) as db:
        for table in ("raw_leads_queue", "blacklisted_channels"):
            cur = db.execute(f"SELECT 1 FROM {table} WHERE channel_id = ?", (channel_id,))
            if cur.fetchone():
                return True
        try:
            cur = db.execute("SELECT 1 FROM outreach_log WHERE channel_id = ?", (channel_id,))
            if cur.fetchone():
                return True
        except sqlite3.OperationalError:
            pass
        return False


def enqueue_channel(channel_id: str, channel_name: str, via: str, db_path: str = APEX_DB) -> None:
    with _connect(db_path) as db:
        db.execute(
            """INSERT OR IGNORE INTO raw_leads_queue
                   (channel_id, channel_name, status, discovered_via)
               VALUES (?, ?, 'pending', ?)""",
            (channel_id, channel_name, via),
        )
        db.commit()


def fetch_pending_leads(limit: int, db_path: str = APEX_DB) -> list:
    with _connect(db_path) as db:
        cur = db.execute(
            """SELECT channel_id, channel_name FROM raw_leads_queue
               WHERE status = 'pending' ORDER BY created_at ASC LIMIT ?""",
            (limit,),
        )
        return [{"channel_id": r[0], "channel_name": r[1]} for r in cur.fetchall()]


def mark_queue_status(channel_id: str, status: str, db_path: str = APEX_DB) -> None:
    with _connect(db_path) as db:
        db.execute(
            "UPDATE raw_leads_queue SET status = ?, processed_at = ? WHERE channel_id = ?",
            (status, datetime.utcnow().isoformat(), channel_id),
        )
        db.commit()


# ==========================================================================
# ОСИНТ-СВЕРХСИЛЫ
# ==========================================================================
def extract_email_from_external_link(url: str):
    if not url or any(x in url.lower() for x in ["youtube.com", "twitter.com", "x.com", "instagram.com", "facebook.com", "tiktok.com"]):
        return None
    try:
        logger.info(f"OSINT: Scanning external site {url}")
        res = subprocess.check_output(
            ["bash", "/home/workspace/youtube_shorts_agency/browser_helper.sh", url],
            timeout=30,
        ).decode().strip()
        emails = [e for e in res.split("\n") if e]
        return emails[0] if emails else None
    except Exception as e:
        logger.warning(f"OSINT: Failed to scan {url}: {e}")
        return None


def get_pinned_comments_email(youtube, video_id: str):
    try:
        resp = youtube.commentThreads().list(
            part="snippet", videoId=video_id, maxResults=20, order="relevance"
        ).execute()
        for item in resp.get("items", []):
            snippet = item["snippet"]["topLevelComment"]["snippet"]
            text = snippet.get("textDisplay", "").lower()
            m = EMAIL_REGEX.search(text)
            if m:
                logger.info(f"OSINT: Found email in comments of video {video_id}")
                return m.group(0)
    except Exception as e:
        logger.warning(f"OSINT: Failed to fetch comments for {video_id}: {e}")
    return None


def web_search_enrichment(channel_name: str):
    query_url = f"https://duckduckgo.com/?q=%22{channel_name.replace(' ', '+')}%22+contact+email"
    return extract_email_from_external_link(query_url)


# ==========================================================================
# ВАЛИДАЦИЯ ПОЧТЫ
# ==========================================================================
def validate_email_address(email: str):
    if not email:
        return False, "empty"
    email = email.strip().lower()

    if not EMAIL_VALIDATION_REGEX.match(email):
        return False, "bad_syntax"

    local, _, domain = email.partition("@")

    if ".." in email or domain.startswith(".") or domain.endswith("."):
        return False, "bad_domain"
    tld = domain.rsplit(".", 1)[-1]
    if len(tld) < 2 or not tld.isalpha():
        return False, "bad_tld"

    if domain in DISPOSABLE_EMAIL_DOMAINS:
        return False, "disposable"

    base_local = local.split("+", 1)[0]
    if base_local in ROLE_LOCALPARTS:
        return False, "role_address"

    return True, "ok"


# ==========================================================================
# YOUTUBE API
# ==========================================================================
def get_youtube_client():
    return build("youtube", "v3", developerKey=YOUTUBE_API_KEY, cache_discovery=False)


def parse_duration_seconds(iso_duration: str) -> int:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_duration or "")
    if not m:
        return 0
    hours, minutes, seconds = (int(x) if x else 0 for x in m.groups())
    return hours * 3600 + minutes * 60 + seconds


def search_channels(youtube, keyword: str, max_results: int = MAX_CHANNELS_PER_KEYWORD) -> list:
    found = []
    try:
        resp = youtube.search().list(
            part="snippet",
            q=keyword,
            type="channel",
            maxResults=min(max_results, 50),
            relevanceLanguage="en",
        ).execute()
        for item in resp.get("items", []):
            cid = item["snippet"].get("channelId") or item.get("id", {}).get("channelId")
            if cid:
                found.append({"channel_id": cid, "channel_name": item["snippet"].get("title")})
    except HttpError as exc:
        logger.error("search.list failed for '%s': %s", keyword, exc)
    return found


def get_channel_details(youtube, channel_ids: list) -> dict:
    details = {}
    for i in range(0, len(channel_ids), 50):
        chunk = channel_ids[i:i + 50]
        try:
            resp = youtube.channels().list(
                part="snippet,statistics,contentDetails",
                id=",".join(chunk),
            ).execute()
        except HttpError as exc:
            logger.error("channels.list failed: %s", exc)
            continue
        for item in resp.get("items", []):
            stats = item.get("statistics", {})
            snip = item.get("snippet", {})
            content = item.get("contentDetails", {})
            subs = None
            if not stats.get("hiddenSubscriberCount"):
                try:
                    subs = int(stats.get("subscriberCount", 0))
                except (TypeError, ValueError):
                    subs = None
            details[item["id"]] = {
                "channel_id": item["id"],
                "channel_name": snip.get("title"),
                "description": snip.get("description", ""),
                "country": snip.get("country"),
                "custom_url": snip.get("customUrl"),
                "subscriber_count": subs,
                "uploads_playlist": content.get("relatedPlaylists", {}).get("uploads"),
            }
    return details


def get_recent_video_ids(youtube, uploads_playlist: str, max_results: int = RECENT_VIDEOS_TO_CHECK) -> list:
    if not uploads_playlist:
        return []
    try:
        resp = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_playlist,
            maxResults=min(max_results, 50),
        ).execute()
    except HttpError as exc:
        logger.error("playlistItems.list failed: %s", exc)
        return []
    return [
        it["contentDetails"]["videoId"]
        for it in resp.get("items", [])
        if it.get("contentDetails", {}).get("videoId")
    ]


def get_video_metadata(youtube, video_ids: list) -> list:
    if not video_ids:
        return []
    out = []
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        try:
            resp = youtube.videos().list(
                part="snippet,contentDetails",
                id=",".join(chunk),
            ).execute()
        except HttpError as exc:
            logger.error("videos.list failed: %s", exc)
            continue
        for item in resp.get("items", []):
            snip = item.get("snippet", {})
            thumbs = snip.get("thumbnails", {})
            thumb_url = thumbs.get("maxres", thumbs.get("high", thumbs.get("default", {}))).get("url")
            out.append({
                "video_id": item["id"],
                "title": snip.get("title", ""),
                "description": snip.get("description", ""),
                "published_at": snip.get("publishedAt"),
                "duration_seconds": parse_duration_seconds(
                    item.get("contentDetails", {}).get("duration", "")
                ),
                "thumbnail_url": thumb_url,
            })
    return out


# ==========================================================================
# ФИЛЬТРЫ (вспомогательные)
# ==========================================================================
def is_latin_only(text: str) -> bool:
    return not NON_LATIN_REGEX.search(text or "")


def hits_strict_blacklist(*texts: str) -> bool:
    blob = " ".join(t.lower() for t in texts if t)
    return any(word in blob for word in STRICT_BLACKLIST)


def has_stream_tokens(title: str) -> bool:
    tokens = {t.lower() for t in _WORD_TOKEN_REGEX.findall(title or "")}
    return bool(tokens & STREAM_WORD_TOKENS)


def has_screenshare_markers(title: str) -> bool:
    title_lower = (title or "").lower()
    return any(marker in title_lower for marker in SCREENSHARE_MARKERS)


def has_non_english_title_words(title: str) -> bool:
    tokens = {t.lower() for t in _WORD_TOKEN_REGEX.findall(title or "")}
    return bool(tokens & NON_ENGLISH_TITLE_WORDS)


# ==========================================================================
# ИЗВЛЕЧЕНИЕ КОНТАКТОВ
# ==========================================================================
def extract_contacts(text: str):
    text = text or ""
    email_match = EMAIL_REGEX.search(text)
    email = email_match.group(0) if email_match else None
    socials = list(dict.fromkeys(SOCIAL_REGEX.findall(text)))
    return email, socials


def extract_contacts_deep(channel_description: str, video_descriptions: list, max_videos: int = 5):
    email = None
    socials = []

    if channel_description:
        e, s = extract_contacts(channel_description)
        email = e
        socials.extend(s)

    for desc in (video_descriptions or [])[:max_videos]:
        if not desc:
            continue
        e, s = extract_contacts(desc)
        if email is None:
            email = e
        socials.extend(s)

    socials = list(dict.fromkeys(socials))
    return email, socials


# ==========================================================================
# VISION AI: Скоринг thumbnail через Groq Vision
# ==========================================================================
def score_thumbnail_vision(thumbnail_url: str) -> tuple:
    """
    Анализирует thumbnail через Groq Vision API (llama-3.2-90b-vision-preview).
    Возвращает (passed: bool, reason: str).
    Если API недоступен или ошибка — возвращает (True, "vision_skip") чтобы не блокировать.
    """
    if not thumbnail_url:
        return False, "no_thumbnail_url"

    if not GROQ_API_KEY:
        logger.warning("GROQ_API_KEY missing, skipping vision scoring")
        return True, "vision_skip_no_key"

    try:
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": "llama-3.2-90b-vision-preview",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": VISION_SCORING_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": thumbnail_url},
                        },
                    ],
                }
            ],
            "temperature": 0.1,
            "max_tokens": 200,
        }

        resp = httpx.post(
            f"{GROQ_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=30.0,
        )

        if resp.status_code != 200:
            logger.warning("Vision API returned %d: %s", resp.status_code, resp.text[:200])
            return True, "vision_api_error"

        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()

        # Парсим JSON из ответа
        import json
        # Ищем JSON в ответе (может быть обёрнут в markdown)
        json_match = re.search(r'\{[^}]+\}', content)
        if not json_match:
            logger.warning("Vision: no JSON in response: %s", content[:100])
            return True, "vision_parse_error"

        result = json.loads(json_match.group(0))
        passed = result.get("pass", False)
        confidence = result.get("confidence", 0)
        reason = result.get("reason", "unknown")

        logger.info(
            "Vision score: pass=%s, confidence=%d, reason='%s'",
            passed, confidence, reason,
        )

        # Дополнительный порог уверенности: если pass=True но confidence < 70, дропаем
        if passed and confidence < 70:
            return False, f"vision_low_confidence_{confidence}: {reason}"

        return passed, reason

    except Exception as e:
        logger.warning("Vision scoring failed: %s", e)
        return True, "vision_exception"


# ==========================================================================
# НОЧНОЙ SCAN
# ==========================================================================
def scan_and_enqueue(youtube, keywords: list, db_path: str = APEX_DB) -> int:
    init_lead_tables(db_path)
    discovered, enqueued = 0, 0
    for kw in keywords:
        for cand in search_channels(youtube, kw):
            discovered += 1
            cid = cand["channel_id"]
            if is_known_channel(cid, db_path):
                continue
            enqueue_channel(cid, cand.get("channel_name"), kw, db_path)
            enqueued += 1
    logger.info("Scan done. discovered=%d, newly enqueued=%d", discovered, enqueued)
    return enqueued


# ==========================================================================
# ВАЛИДАЦИЯ ОДНОГО ЛИДА — STEP-BY-STEP GATEKEEPING
# ==========================================================================
# Порядок проверок оптимизирован для максимальной экономии квоты:
#   STEP 1: Подписчики + Гео (1 дешёвый API call — channels.list)
#   STEP 2: Жёсткий текстовый блеклист (БЕСПЛАТНО, по данным из Step 1)
#   STEP 3: Наличие Instagram/Twitter/X в описании (БЕСПЛАТНО)
#   STEP 4: Длина видео 5-25 мин по последним 3 (2 API calls — playlistItems + videos)
#   STEP 5: Vision AI скоринг thumbnail (1 Groq API call — САМЫЙ дорогой)
#   STEP 6: Поиск контактов + OSINT (опционально дорого)
# ==========================================================================
def validate_lead(youtube, channel_id: str, db_path: str = APEX_DB):
    """
    Каскадная валидация канала. Каждый шаг — гейт.
    Если канал не проходит — мгновенный дроп БЕЗ перехода к дорогим шагам.
    Возвращает (lead_dict, None) при успехе или (None, reason) при отказе.
    """

    # ======================================================================
    # STEP 1: Подписчики (10k-100k) + Гео (US, GB, CA, AU, NZ, IE)
    # Стоимость: 1 unit YouTube API (channels.list)
    # ======================================================================
    ch = get_channel_details(youtube, [channel_id]).get(channel_id)
    if not ch:
        return None, "channel_not_found"

    name = ch["channel_name"] or ""

    subs = ch["subscriber_count"]
    if subs is None or subs < MIN_SUBSCRIBERS or subs > MAX_SUBSCRIBERS:
        logger.info("STEP1 DROP '%s': subs=%s (need %d-%d)", name, subs, MIN_SUBSCRIBERS, MAX_SUBSCRIBERS)
        return None, "step1_subs_out_of_range"

    country = ch.get("country")
    if country and country not in ALLOWED_COUNTRIES:
        logger.info("STEP1 DROP '%s': country=%s", name, country)
        return None, f"step1_geo_{country}"

    # ======================================================================
    # STEP 2: Жёсткий текстовый блеклист
    # Стоимость: 0 (работаем с данными из Step 1)
    # Убираем: инфобиз по продвижению каналов, крипто-фермы, монтажёров,
    #           нелатиницу, нецелевые гео/языки
    # ======================================================================
    if not is_latin_only(name):
        logger.info("STEP2 DROP '%s': non-latin characters in name", name)
        return None, "step2_non_latin_name"

    description = ch.get("description", "")
    if hits_strict_blacklist(name, description):
        logger.info("STEP2 DROP '%s': hit strict blacklist", name)
        return None, "step2_blacklist_hit"

    # ======================================================================
    # STEP 3: Наличие Instagram или Twitter/X в метаданных
    # Стоимость: 0 (парсим description из Step 1)
    # Логика: премиум соло-авторы ВСЕГДА качают личный бренд через Инсту/X.
    #         Если ссылок нет — это НЕ наш клиент.
    # ======================================================================
    _, socials_from_desc = extract_contacts(description)
    visual_socials = [
        s for s in socials_from_desc
        if any(domain in s.lower() for domain in VISUAL_SOCIAL_DOMAINS)
    ]

    if not visual_socials:
        logger.info("STEP3 DROP '%s': no Instagram/Twitter/X in channel description", name)
        return None, "step3_no_visual_socials"

    # ======================================================================
    # STEP 4: Длина видео — строго от 5 до 25 минут (последние 3 видео)
    # Стоимость: 2 units YouTube API (playlistItems.list + videos.list)
    # Логика: качественные Talking Head авторы записывают 8-20 мин контент.
    #         < 5 мин = шортсы/нарезки, > 25 мин = стримы/подкасты/лекции.
    # ======================================================================
    video_ids = get_recent_video_ids(youtube, ch["uploads_playlist"], max_results=10)
    metas = get_video_metadata(youtube, video_ids)
    if not metas:
        return None, "step4_no_recent_videos"

    metas.sort(key=lambda m: m.get("published_at") or "", reverse=True)

    # Берём последние 3 видео для проверки длины
    last_3 = metas[:3]
    for vid in last_3:
        dur = vid["duration_seconds"]
        if dur < MIN_VIDEO_SECONDS or dur > MAX_VIDEO_SECONDS:
            logger.info(
                "STEP4 DROP '%s': video '%s' duration=%ds (need %d-%d)",
                name, vid["title"][:40], dur, MIN_VIDEO_SECONDS, MAX_VIDEO_SECONDS,
            )
            return None, f"step4_duration_{dur}s"

    # Дополнительно: проверяем заголовки на стримы/скриншеры/не-англ.
    latest = metas[0]
    latest_title = latest["title"]

    if hits_strict_blacklist(latest_title):
        return None, "step4_title_blacklist"
    if has_stream_tokens(latest_title):
        return None, "step4_stream_tokens"
    if has_screenshare_markers(latest_title):
        return None, "step4_screenshare_markers"
    if has_non_english_title_words(latest_title):
        return None, "step4_non_english_title"

    # Считаем недавние шортсы
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_WINDOW_DAYS)
    recent_shorts = 0
    for m in metas:
        pub = m.get("published_at")
        try:
            pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00")) if pub else None
        except ValueError:
            pub_dt = None
        if pub_dt and pub_dt >= cutoff and 0 < m["duration_seconds"] <= SHORT_MAX_SECONDS:
            recent_shorts += 1
    if recent_shorts > MAX_RECENT_SHORTS:
        return None, "step4_already_does_shorts"

    # ======================================================================
    # STEP 5: Vision AI — скоринг thumbnail последнего видео
    # Стоимость: 1 Groq Vision API call (самый дорогой шаг)
    # Критерии: соло-спикер + дорогая студия + без экранов/графиков
    # ======================================================================
    thumbnail_url = latest.get("thumbnail_url")
    vision_passed, vision_reason = score_thumbnail_vision(thumbnail_url)

    if not vision_passed:
        logger.info("STEP5 DROP '%s': Vision AI rejected — %s", name, vision_reason)
        return None, f"step5_vision_fail: {vision_reason}"

    logger.info("STEP5 PASS '%s': Vision AI approved — %s", name, vision_reason)

    # ======================================================================
    # STEP 6: Глубокий поиск контактов + OSINT
    # Стоимость: варьируется (comments API + browser scraping)
    # ======================================================================
    video_descs = [m["description"] for m in metas[:5]]
    email, all_socials = extract_contacts_deep(description, video_descs, max_videos=5)

    # OSINT: если почты нет, ищем в комментах и на внешних сайтах
    if email is None:
        email = get_pinned_comments_email(youtube, latest["video_id"])
    if email is None:
        for s in all_socials:
            if any(dom in s.lower() for dom in ["linktr.ee", "beacons.ai", name.lower().replace(" ", "")]):
                email = extract_email_from_external_link(s)
                if email:
                    break
    if email is None:
        email = web_search_enrichment(name)

    # Валидация почты (не блокирующая — лид может пройти без email)
    if email:
        ok, why = validate_email_address(email)
        if not ok:
            logger.info("Email '%s' for '%s' rejected: %s (keeping lead without email)", email, name, why)
            email = None

    # ======================================================================
    # РЕЗУЛЬТАТ: Канал прошёл ВСЕ гейты — формируем lead dict
    # ======================================================================
    lead = {
        "channel_id": channel_id,
        "channel_name": name,
        "custom_url": ch["custom_url"],
        "description": description,
        "contact_email": email,
        "social_links": all_socials,
        "subscriber_count": subs,
        "recent_shorts": recent_shorts,
        "latest_video_title": latest_title,
        "thumbnail_url": thumbnail_url,
        "vision_score": vision_reason,
    }

    logger.info(
        "✅ VALIDATED '%s' | subs=%d | country=%s | socials=%d | email=%s",
        name, subs, country or "N/A", len(visual_socials), bool(email),
    )
    return lead, None
