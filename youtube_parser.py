import re
import base64
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone

import httpx
import google.generativeai as genai
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from groq import Groq

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    HAS_TRANSCRIPT_API = True
except ImportError:
    HAS_TRANSCRIPT_API = False

from config import (
    YOUTUBE_API_KEYS,
    GEMINI_API_KEY,
    GROQ_API_KEY,
    GROQ_BASE_URL,
    HF_TOKEN,
    MIN_SUBSCRIBERS,
    MAX_SUBSCRIBERS,
    SHORT_MAX_SECONDS,
    RECENT_WINDOW_DAYS,
    MAX_RECENT_SHORTS,
    RECENT_VIDEOS_TO_CHECK,
    MAX_CHANNELS_PER_KEYWORD,
    SEARCH_RESULTS_PER_KEYWORD,
    ALLOWED_COUNTRIES,
    APEX_DB,
    logger,
    send_telegram_notification,
)

class QuotaExceededError(Exception):
    """Custom exception for YouTube API quota exhaustion."""
    pass

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

# --------------------------------------------------------------------------
# STEP 2 CONSTANTS: Жёсткий текстовый блеклист
# --------------------------------------------------------------------------
# Инфобиз по продвижению каналов, крипто-фермы, монтажёры, нецелевые языки/гео
STRICT_BLACKLIST = [
    # Нецелевые языки / гео
    "brasil", "brazil",
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
                channel_id        TEXT PRIMARY KEY,
                channel_name      TEXT,
                status            TEXT DEFAULT 'pending',
                discovered_via    TEXT,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at      TIMESTAMP,
                rejection_reason  TEXT,
                long_form_count   INTEGER,
                shorts_count      INTEGER,
                product_name      TEXT,
                views_gap         TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS blacklisted_channels (
                channel_id TEXT PRIMARY KEY,
                reason     TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS manual_social_outreach (
                channel_id         TEXT PRIMARY KEY,
                channel_name       TEXT,
                social_links       TEXT,
                latest_video       TEXT,
                contact_email      TEXT,
                preferred_channel  TEXT,
                x_url              TEXT,
                instagram_url      TEXT,
                website_url        TEXT,
                x_dm               TEXT,
                created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for stmt in [
            "ALTER TABLE manual_social_outreach ADD COLUMN contact_email TEXT",
            "ALTER TABLE manual_social_outreach ADD COLUMN preferred_channel TEXT",
            "ALTER TABLE manual_social_outreach ADD COLUMN x_url TEXT",
            "ALTER TABLE manual_social_outreach ADD COLUMN instagram_url TEXT",
            "ALTER TABLE manual_social_outreach ADD COLUMN website_url TEXT",
        ]:
            try:
                db.execute(stmt)
            except sqlite3.OperationalError:
                pass
        db.execute("CREATE INDEX IF NOT EXISTS idx_queue_status ON raw_leads_queue(status)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_manual_channel ON manual_social_outreach(preferred_channel)")
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
               WHERE status IN ('pending', 'pending_error')
               ORDER BY status ASC, created_at ASC LIMIT ?""",
            (limit,),
        )
        rows = cur.fetchall()
        return [{"channel_id": r[0], "channel_name": r[1]} for r in rows]


def get_pending_count(db_path: str = APEX_DB) -> int:
    """Returns the total number of leads with 'pending' or 'pending_error' status."""
    with _connect(db_path) as db:
        cur = db.execute("SELECT count(*) FROM raw_leads_queue WHERE status IN ('pending', 'pending_error')")
        return cur.fetchone()[0]


def classify_contact_channels(email: str, social_links: list) -> dict:
    """Classifies contact channels based on email and social links."""
    contact_email = email
    preferred_channel = None
    x_url = None
    instagram_url = None
    website_url = None

    if email:
        preferred_channel = "email"

    x_urls = []
    instagram_urls = []
    website_urls = []

    for link in social_links:
        if "twitter.com" in link or "x.com" in link:
            x_urls.append(link)
        if "instagram.com" in link:
            instagram_urls.append(link)
        if "youtube.com" not in link and "twitter.com" not in link and "x.com" not in link and "instagram.com" not in link and "facebook.com" not in link and "tiktok.com" not in link and "t.me" not in link and "discord.com" not in link:
            website_urls.append(link)

    if x_urls:
        x_url = x_urls[0]
        if preferred_channel is None:
            preferred_channel = "x"
    if instagram_urls:
        instagram_url = instagram_urls[0]
        if preferred_channel is None:
            preferred_channel = "instagram"
    if website_urls:
        website_url = website_urls[0]
        if preferred_channel is None:
            preferred_channel = "website"

    return {
        "contact_email": contact_email,
        "preferred_channel": preferred_channel,
        "x_url": x_url,
        "instagram_url": instagram_url,
        "website_url": website_url,
    }


def mark_queue_status(channel_id: str, status: str, db_path: str = APEX_DB, reason: str = None, extra_data: dict = None) -> None:
    with _connect(db_path) as db:
        fields = ["status = ?", "processed_at = ?"]
        params = [status, datetime.utcnow().isoformat()]
        
        if reason:
            fields.append("rejection_reason = ?")
            params.append(reason)
            
        if extra_data:
            for k, v in extra_data.items():
                fields.append(f"{k} = ?")
                params.append(v)
        
        params.append(channel_id)
        query = f"UPDATE raw_leads_queue SET {', '.join(fields)} WHERE channel_id = ?"
        db.execute(query, tuple(params))
        db.commit()


def save_manual_social(lead: dict, db_path: str = APEX_DB) -> None:
    with _connect(db_path) as db:
        import json
        contact = classify_contact_channels(lead.get("contact_email"), lead.get("social_links"))
        db.execute(
            """INSERT OR REPLACE INTO manual_social_outreach
                   (channel_id, channel_name, social_links, latest_video, contact_email,
                    preferred_channel, x_url, instagram_url, website_url, x_dm)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                lead["channel_id"],
                lead["channel_name"],
                json.dumps(lead.get("social_links", [])),
                lead.get("latest_video_title"),
                lead.get("contact_email"),
                contact["preferred_channel"],
                contact["x_url"],
                contact["instagram_url"],
                contact["website_url"],
                lead.get("x_dm"),
            ),
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
    except QuotaExceededError:
        raise
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
_current_key_index = 0

def get_youtube_client():
    """Возвращает клиент YouTube API, используя текущий активный ключ."""
    import httplib2
    global _current_key_index
    key = YOUTUBE_API_KEYS[_current_key_index % len(YOUTUBE_API_KEYS)]
    http = httplib2.Http(timeout=60)
    return build("youtube", "v3", developerKey=key, cache_discovery=False, http=http)

def rotate_youtube_key() -> bool:
    """Переключает на следующий API-ключ. Возвращает False, если ключи кончились."""
    global _current_key_index
    _current_key_index += 1
    if _current_key_index >= len(YOUTUBE_API_KEYS):
        logger.error("❌ ALL YOUTUBE API KEYS EXHAUSTED.")
        return False
    
    new_key = YOUTUBE_API_KEYS[_current_key_index % len(YOUTUBE_API_KEYS)]
    logger.info("🔄 Rotating YouTube API key to next one (index %d): %s...", 
                _current_key_index, new_key[:10])
    return True

def parse_duration_seconds(iso_duration: str) -> int:
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_duration or "")
    if not m:
        return 0
    hours, minutes, seconds = (int(x) if x else 0 for x in m.groups())
    return hours * 3600 + minutes * 60 + seconds


def handle_api_error(exc, context: str):
    """Логирует ошибку и выбрасывает QuotaExceededError при исчерпании квоты."""
    if isinstance(exc, HttpError):
        content = str(exc).lower()
        is_quota = (exc.resp.status in [403, 429]) and ("quota" in content or "limit" in content)
        
        if is_quota:
            logger.error("🛑 QUOTA EXCEEDED: %s failed.", context)
            raise QuotaExceededError(f"Quota exceeded during {context}")
        
    logger.error("%s failed: %s", context, exc)

def search_channels(youtube, keyword: str, limit: int = SEARCH_RESULTS_PER_KEYWORD) -> list:
    """Двойная стратегия поиска: каналы + видео → уникальные channel_ids.

    ВАЖНО для защиты квоты:
    - order='date' — свежие ролики маленьких авторов, а не топ-10 миллионников
    - maxResults ограничен SEARCH_RESULTS_PER_KEYWORD (15)
    - НЕ используем nextPageToken — один запрос на стратегию
    """
    found = []
    seen_cids = set()
    # Стратегия 1: Поиск каналов по ключевым словам
    try:
        resp = youtube.search().list(
            part="snippet",
            q=keyword,
            type="channel",
            maxResults=limit,
            order="date",
            relevanceLanguage="en",
        ).execute()
        for item in resp.get("items", []):
            cid = item["snippet"].get("channelId") or item.get("id", {}).get("channelId")
            if cid and cid not in seen_cids:
                seen_cids.add(cid)
                found.append({"channel_id": cid, "channel_name": item["snippet"].get("title")})
    except QuotaExceededError:
        raise
    except Exception as exc:
        handle_api_error(exc, f"search.list (channel) '{keyword}'")

    # Стратегия 2: Поиск через видео — находит маленьких авторов по контенту
    try:
        resp = youtube.search().list(
            part="snippet",
            q=keyword,
            type="video",
            maxResults=limit,
            order="date",
            relevanceLanguage="en",
            videoDuration="medium",  # 4-20 мин — наш целевой диапазон
        ).execute()
        for item in resp.get("items", []):
            cid = item["snippet"].get("channelId")
            if cid and cid not in seen_cids:
                seen_cids.add(cid)
                found.append({"channel_id": cid, "channel_name": item["snippet"].get("channelTitle")})
    except QuotaExceededError:
        raise
    except Exception as exc:
        handle_api_error(exc, f"search.list (video) '{keyword}'")

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
        except QuotaExceededError:
            raise
        except Exception as exc:
            handle_api_error(exc, "channels.list")
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
    except QuotaExceededError:
        raise
    except Exception as exc:
        handle_api_error(exc, "playlistItems.list")
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
                part="snippet,contentDetails,statistics",
                id=",".join(chunk),
            ).execute()
        except QuotaExceededError:
            raise
        except Exception as exc:
            handle_api_error(exc, "videos.list")
            continue
        for item in resp.get("items", []):
            snip = item.get("snippet", {})
            stats = item.get("statistics", {})
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
                "view_count": int(stats.get("viewCount", 0)),
            })
    return out


# ==========================================================================
# ФИЛЬТРЫ (вспомогательные)
# ==========================================================================
def is_latin_only(text: str) -> bool:
    return not NON_LATIN_REGEX.search(text or "")


def hits_strict_blacklist(*texts: str) -> str | None:
    blob = " ".join(t.lower() for t in texts if t)
    for word in STRICT_BLACKLIST:
        if word in blob:
            return word
    return None


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
# VISION AI: Scoring thumbnail via Hugging Face Inference API
# ==========================================================================
def score_lead_multimodal(thumbnail_url: str, title: str, description: str, channel_about: str) -> tuple:
    """
    Analyzes lead via Hugging Face Router API using google/gemma-4-31B-it.
    """
    if not HF_TOKEN:
        logger.warning("HF_TOKEN missing, skipping vision scoring")
        return False, "vision_no_hf_token"

    model_id = "google/gemma-4-31B-it"
    api_url = "https://router.huggingface.co/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {HF_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        # Prepare the prompt
        prompt_text = f"""You are a strict lead validator for a premium video agency. Your goal is to find INDIVIDUAL tech creators (founders, software engineers, solo-developers, tech experts) who talk about business, coding, SaaS, or digital products.

Analyze BOTH the provided thumbnail image and the text context (Title, Description, About) to make a decision.

CONTEXT:
- Video Title: {title}
- Video Description: {description[:500]}
- Channel About: {channel_about[:500]}

REJECT IMMEDIATELY IF:
- The channel belongs to a corporate brand, company, or software tool (like Riverside, HubSpot, Zoom, etc.).
- The channel is about tech reviews, gadgets, microphones, hardware, or gaming.
- There is no real human face on the thumbnail.

Respond in strict JSON:
{{
  "decision": "PASSED" or "FAILED",
  "reason": "short explanation why"
}}"""

        # Hugging Face Router API Multimodal Payload (OpenAI-compatible)
        payload = {
            "model": model_id,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": thumbnail_url
                            }
                        },
                        {
                            "type": "text",
                            "text": prompt_text
                        }
                    ]
                }
            ],
            "parameters": {
                "max_new_tokens": 512,
                "temperature": 0.1,
                "thinking": True,
                "vision_token_budget": 560
            }
        }

        response = httpx.post(api_url, headers=headers, json=payload, timeout=60.0)
        
        if response.status_code != 200:
            logger.error("HF API Error %d: %s", response.status_code, response.text)
            # 402 (payment), 429 (rate limit), 5xx (server) = temporary, retry later
            if response.status_code in (402, 429) or response.status_code >= 500:
                return None, f"pending_error_hf_api_{response.status_code}"
            return False, f"hf_api_error_{response.status_code}"

        resp_json = response.json()
        
        # Extract content from choices (OpenAI-compatible format)
        if "choices" in resp_json and len(resp_json["choices"]) > 0:
            content = resp_json["choices"][0]["message"]["content"]
        else:
            logger.error("Unexpected HF API response format: %s", resp_json)
            return False, "vision_bad_response_format"

        # Remove thinking block if present
        if "<|channel>thought" in content:
            content = content.split("<channel|>")[-1].strip()

        # Clean JSON from markdown if needed
        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        if json_match:
            content = json_match.group(0)

        import json
        try:
            result = json.loads(content)
        except Exception as json_e:
            logger.error("Failed to parse Vision AI JSON: %s | Content: %s", json_e, content)
            return False, f"vision_json_error: {str(json_e)}"
        
        passed = str(result.get("decision", "")).upper() == "PASSED"
        reason = result.get("reason") or "unknown"
        return passed, reason

    except Exception as e:
        logger.warning("Gemma 4 Vision via HF failed: %s", e)
        if "quota" in str(e).lower() or "limit" in str(e).lower():
            return None, f"pending_error_vision_quota: {str(e)}"
        return False, f"vision_api_error: {str(e)}"

# ==========================================================================
# CONTENT & PRODUCT ANALYSIS
# ==========================================================================
def analyze_content_and_product(youtube, channel_id: str, description: str, video_metas: list):
    """
    Analyzes channel content (long-form vs shorts) and extracts product info.
    """
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=60)
    
    long_form_count = 0
    shorts_count = 0
    long_form_views = 0
    shorts_views = 0
    
    for v in video_metas:
        pub_at = v.get("published_at")
        if not pub_at: continue
        pub_dt = datetime.fromisoformat(pub_at.replace("Z", "+00:00"))
        
        if pub_dt >= cutoff_date:
            duration = v.get("duration_seconds", 0)
            views = v.get("view_count", 0)
            
            if 0 < duration <= SHORT_MAX_SECONDS:
                shorts_count += 1
                shorts_views += views
            elif duration > SHORT_MAX_SECONDS:
                long_form_count += 1
                long_form_views += views
                
    # Extract product name using LLM
    product_name = "None"
    try:
        client = Groq(api_key=GROQ_API_KEY)
        extract_prompt = f"""Extract the name of the main product, SaaS, app, or course mentioned in this YouTube channel description. 
If no clear product is mentioned, respond with 'None'. Respond with ONLY the name.

DESCRIPTION:
{description[:1500]}"""
        
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": extract_prompt}],
            temperature=0,
            max_tokens=20
        )
        product_name = completion.choices[0].message.content.strip().strip('"')
    except Exception as e:
        logger.warning("Product extraction failed: %s", e)

    # Views Gap Logic
    avg_lf = long_form_views / long_form_count if long_form_count > 0 else 0
    avg_sh = shorts_views / shorts_count if shorts_count > 0 else 0
    
    if long_form_count > 2 and shorts_count > 0:
        if avg_sh < avg_lf * 0.2:
            views_gap = "shorts lagging (much lower views than long-form)"
        elif avg_lf > 5000 and shorts_count < 2:
            views_gap = "long-form is high but shorts are rare"
        else:
            views_gap = "normal"
    else:
        views_gap = "normal"

    return {
        "long_form_count": long_form_count,
        "shorts_count": shorts_count,
        "product_name": product_name,
        "views_gap": views_gap
    }

def is_english_strict(ch: dict, latest: dict) -> bool:
    """Strict English language check."""
    text = (ch.get("channel_name") or "") + " " + (ch.get("description") or "") + " " + (latest.get("title") or "")
    if NON_LATIN_REGEX.search(text):
        return False
    if has_non_english_title_words(text):
        return False
    return True


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

# ==========================================================================
# VIDEO ENRICHMENT: free captions + metadata + AI summary
# ==========================================================================
def fetch_video_captions(video_id: str, max_chars: int = 3000) -> str | None:
    """Fetch auto-generated or manual captions for free via youtube-transcript-api.
    Returns transcript text (truncated to max_chars) or None."""
    if not HAS_TRANSCRIPT_API:
        return None
    try:
        ytt_api = YouTubeTranscriptApi()
        transcript = ytt_api.fetch(video_id, languages=["en", "en-US", "en-GB"])
        text_parts = [snippet.text for snippet in transcript.snippets]
        full_text = " ".join(text_parts)
        return full_text[:max_chars] if full_text else None
    except Exception as e:
        logger.debug("Captions not available for %s: %s", video_id, type(e).__name__)
        return None


def generate_video_summary(title: str, description: str, captions: str | None,
                           views: int, duration_sec: int) -> dict:
    """Generate a short summary + hooks using Groq Llama (cheap).
    Returns dict with 'summary', 'hooks', 'has_captions'."""
    has_captions = bool(captions)

    # Build context
    desc_short = (description or "")[:500]
    duration_min = round(duration_sec / 60, 1) if duration_sec else 0

    if captions:
        context = f"Title: {title}\nDescription: {desc_short}\nViews: {views:,}\nDuration: {duration_min} min\n\nTranscript (first ~3000 chars):\n{captions}"
    else:
        context = f"Title: {title}\nDescription: {desc_short}\nViews: {views:,}\nDuration: {duration_min} min\n\n(No transcript available — summarize based on title and description only)"

    prompt = (
        "Analyze this YouTube video and provide a JSON response:\n"
        "1. summary: 1-2 sentence summary of what the video is about\n"
        "2. hooks: array of 1-3 potential short-form moments/hooks that could be clipped\n"
        "3. outreach_angle: 1 sentence on how to reference this video in a cold outreach message\n\n"
        "IMPORTANT: Only state facts from the provided data. If no transcript, keep it neutral.\n"
        "Respond in valid JSON only: {\"summary\": \"...\", \"hooks\": [\"...\"], \"outreach_angle\": \"...\"}\n\n"
        f"{context}"
    )

    try:
        import os
        from groq import Groq
        groq_key = os.getenv("GROQ_API_KEY")
        if not groq_key:
            return {"summary": f"Video: {title}", "hooks": [], "outreach_angle": "", "has_captions": has_captions}

        client = Groq(api_key=groq_key)
        resp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=300,
        )
        content = resp.choices[0].message.content.strip()

        import json
        # Extract JSON from response
        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group(0))
            result["has_captions"] = has_captions
            return result
    except Exception as e:
        logger.warning("Video summary generation failed: %s", e)

    # Fallback: neutral summary from title
    return {
        "summary": f"Video: {title}" if title else "No video data available",
        "hooks": [],
        "outreach_angle": "",
        "has_captions": has_captions,
    }


def enrich_lead_video(lead: dict) -> dict:
    """Enrichment step for validated leads. Adds video metadata + captions + summary.
    Free: captions via youtube-transcript-api, summary via Groq Llama 3.1 8B (cheap).
    Called only for leads that passed all filters."""
    video_id = lead.get("latest_video_id")
    if not video_id:
        lead["video_enrichment"] = {"summary": "No video ID", "hooks": [], "outreach_angle": "", "has_captions": False}
        return lead

    logger.info("Enriching video for '%s' (video=%s)", lead.get("channel_name"), video_id)

    # 1. Fetch free captions
    captions = fetch_video_captions(video_id)
    if captions:
        logger.info("Captions found for '%s' (%d chars)", lead.get("channel_name"), len(captions))
    else:
        logger.info("No captions for '%s', using title+description fallback", lead.get("channel_name"))

    # 2. Generate summary + hooks
    enrichment = generate_video_summary(
        title=lead.get("latest_video_title", ""),
        description=lead.get("description", ""),
        captions=captions,
        views=lead.get("video_views", 0),
        duration_sec=lead.get("video_duration", 0),
    )

    lead["video_enrichment"] = enrichment
    lead["video_summary"] = enrichment.get("summary", "")
    lead["video_hooks"] = enrichment.get("hooks", [])
    lead["video_outreach_angle"] = enrichment.get("outreach_angle", "")
    lead["video_has_captions"] = enrichment.get("has_captions", False)

    logger.info("Enrichment done for '%s': summary=%s, hooks=%d, captions=%s",
                lead.get("channel_name"),
                lead["video_summary"][:60],
                len(lead["video_hooks"]),
                lead["video_has_captions"])

    return lead


def validate_lead(youtube, channel_id: str, db_path: str = APEX_DB, channel_detail: dict = None):
    """
    Каскадная валидация канала. Каждый шаг — гейт.
    Если канал не проходит — мгновенный дроп БЕЗ перехода к дорогим шагам.
    Возвращает (lead_dict, None) при успехе или (None, reason) при отказе.
    """

    # ======================================================================
    # STEP 1: Подписчики (10k-100k) + Гео (US, GB, CA, AU, NZ, IE)
    # Стоимость: 1 unit YouTube API (channels.list) - ИСПОЛЬЗУЕМ КЭШ ЕСЛИ ЕСТЬ
    # ======================================================================
    if channel_detail:
        ch = channel_detail
    else:
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
    blacklist_hit = hits_strict_blacklist(name, description)
    if blacklist_hit:
        logger.info("STEP2 DROP '%s': hit strict blacklist term=%s", name, blacklist_hit)
        return None, f"step2_blacklist_hit:{blacklist_hit}"

    # ======================================================================
    # STEP 3: Наличие Instagram или Twitter/X в метаданных (ОПЦИОНАЛЬНО)
    # Стоимость: 0 (парсим description из Step 1)
    # Логика: премиум соло-авторы часто качают личный бренд через Инсту/X.
    #         Теперь это не блокирующий фильтр, а просто пометка.
    # ======================================================================
    _, socials_from_desc = extract_contacts(description)
    visual_socials = [
        s for s in socials_from_desc
        if any(domain in s.lower() for domain in VISUAL_SOCIAL_DOMAINS)
    ]

    # if not visual_socials:
    #     logger.info("STEP3 DROP '%s': no Instagram/Twitter/X in channel description", name)
    #     return None, "step3_no_visual_socials"

    # ======================================================================
    # STEP 4: Длина видео + Свежесть (30 дней) + English Filter
    # ======================================================================
    video_ids = get_recent_video_ids(youtube, ch["uploads_playlist"], max_results=RECENT_VIDEOS_TO_CHECK)
    metas = get_video_metadata(youtube, video_ids)
    if not metas:
        return None, "step4_no_recent_videos"

    metas.sort(key=lambda m: m.get("published_at") or "", reverse=True)
    latest = metas[0]
    
    # Recency Check: 30 days
    pub_at = latest.get("published_at")
    if pub_at:
        pub_dt = datetime.fromisoformat(pub_at.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) - pub_dt > timedelta(days=30):
            logger.info("STEP4 DROP '%s': inactive (>30 days)", name)
            return None, "step4_inactive_channel"

    # English check (STRICT)
    if not is_english_strict(ch, latest):
        logger.info("STEP4 DROP '%s': non-english", name)
        return None, "step4_non_english"

    # Считаем недавние шортсы и другие метрики
    analysis = analyze_content_and_product(youtube, channel_id, description, metas)
    
    # ======================================================================
    # STEP 5: Vision AI — ОТКЛЮЧЁН (ручной просмотр вместо автоматического)
    # ======================================================================
    thumbnail_url = latest.get("thumbnail_url")
    vision_reason = "vision_skipped"
    logger.info("STEP5 SKIP '%s': Vision AI disabled, marking as vision_skipped", name)

    # ======================================================================
    # STEP 5b: Обязательный продукт — без продукта лид не нужен
    # ======================================================================
    product_name = analysis.get("product_name", "None")
    if not product_name or product_name.lower() in ("none", "", "null"):
        logger.info("STEP5b DROP '%s': no product/SaaS detected", name)
        return None, "step5b_no_product"

    # Классификация short-form opportunity
    shorts_count = analysis.get("shorts_count", 0)
    if shorts_count <= 1:
        shorts_opportunity = "no_shorts"
    elif shorts_count <= 3:
        shorts_opportunity = "inconsistent_shorts"
    else:
        shorts_opportunity = "weak_editing_shorts"
    analysis["shorts_opportunity"] = shorts_opportunity
    logger.info("STEP5b PASS '%s': product='%s' shorts_opportunity='%s'", name, product_name, shorts_opportunity)

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
    # STEP 7: Usable contact check — email / X / Instagram / website
    # ======================================================================
    contact_info = classify_contact_channels(email, all_socials)
    has_contact = bool(email) or bool(contact_info.get("x_url")) or bool(contact_info.get("instagram_url")) or bool(contact_info.get("website_url"))
    if not has_contact:
        logger.info("STEP7 DROP '%s': no usable contact (no email, X, Instagram, or website)", name)
        return None, "step_contact_missing"

    logger.info("STEP7 PASS '%s': contact found (preferred=%s)", name, contact_info.get("preferred_channel", "none"))

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
        "long_form_count": analysis["long_form_count"],
        "shorts_count": analysis["shorts_count"],
        "shorts_opportunity": analysis.get("shorts_opportunity", "unknown"),
        "product_name": analysis["product_name"],
        "views_gap": analysis["views_gap"],
        "latest_video_title": latest["title"],
        "latest_video_id": latest["video_id"],
        "thumbnail_url": thumbnail_url,
        "video_views": latest.get("view_count", 0),
        "video_published_at": latest.get("published_at", ""),
        "video_duration": latest.get("duration_seconds", 0),
        "video_description": latest.get("description", "")[:500],
        "vision_score": vision_reason,
    }
    lead.update(contact_info)

    logger.info(
        "✅ VALIDATED '%s' | subs=%d | country=%s | socials=%d | email=%s | preferred=%s",
        name, subs, country or "N/A", len(visual_socials), bool(email), lead.get("preferred_channel"),
    )
    return lead, None