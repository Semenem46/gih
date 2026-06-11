import os
import sys
import logging
import requests
from dotenv import load_dotenv

# Load environment variables from the .env file located next to this module.
load_dotenv()

# --- API credentials -------------------------------------------------------
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# --- Telegram notifications ------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BOT_TOKEN = TELEGRAM_BOT_TOKEN
ADMIN_ID = TELEGRAM_CHAT_ID

# --- Email (Gmail SMTP) ----------------------------------------------------
EMAIL_SENDER = os.getenv("EMAIL_SENDER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

SENDER_NAME = "Pavel @ Apex AI"
FROM_EMAIL = EMAIL_SENDER
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = EMAIL_SENDER
SMTP_PASSWORD = EMAIL_PASSWORD

PHYSICAL_ADDRESS = "Austin, TX, USA" # Update this to your real address
UNSUBSCRIBE_LINK = "https://apex.ai/unsubscribe" # Placeholder

# --- Groq (OpenAI-compatible) configuration --------------------------------
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_MODEL = "llama-3.3-70b-versatile"

# --- Database --------------------------------------------------------------
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agency.db")
APEX_DB = DB_PATH

# --- Lead filtering rules --------------------------------------------------
MIN_SUBSCRIBERS = 10_000
MAX_SUBSCRIBERS = 100_000

MIN_ENGLISH_RATIO = 0.85
REQUIRE_CONTACT = True
ALLOWED_COUNTRIES = {"US", "GB", "CA", "AU", "NZ", "IE"}
ALLOWED_REGION_HINTS = [
    "usa", "united states", "canada", "uk", "united kingdom", "england",
    "scotland", "wales", "ireland", "australia", "new zealand",
    "europe", "european", "california", "texas", "florida", "toronto",
    "vancouver", "london"
]
BANNED_REGION_HINTS = [
    "sinhala", "hindi", "indonesia", "indonesian", "brasil", "brazil",
    "español", "espanol", "français", "francais", "deutsch", "рус", "russia",
    "arabic", "urdu", "tamil", "serbia", "croatia", "hungary"
]

# A "short" is any video whose duration is <= 60 seconds.
SHORT_MAX_SECONDS = 60

# Window (in days) used to decide whether the channel is "ignoring" Shorts.
RECENT_WINDOW_DAYS = 60

# Max number of Shorts in the recent window for the channel to still be a lead.
MAX_RECENT_SHORTS = 2

# How many of the most recent uploads we inspect per channel.
RECENT_VIDEOS_TO_CHECK = 30

# How many channels (max) to pull from search per keyword (YouTube caps at 50).
MAX_CHANNELS_PER_KEYWORD = 50

# --- Manual control flag ---------------------------------------------------
# When False: leads are collected and reported to Telegram, but NO emails are
# sent automatically (calibration mode). Flip to True once you trust the output.
AUTO_SEND_EMAILS = False
REQUIRE_PRODUCTION_PORTFOLIO = True
PORTFOLIO_LINK = "https://clck.ru/3BvXyz"

# --- Search keywords (real, profitable English-speaking niches) ------------
KEYWORDS_TO_SEARCH = [
    "talking head day trader studio",
    "crypto guru face camera camera solo",
    "business coach studio talking head",
    "entrepreneur vlog face camera solo",
    "talking head personal finance expert",
    "solo trading mentor studio face",
    "finance advice camera talking head",
    "vlog business mentor solo face"
]
KEYWORDS = KEYWORDS_TO_SEARCH

# Telegram hard message-length cap.
_TELEGRAM_MAX_LEN = 4096

def _build_logger() -> logging.Logger:
    """Configure and return the project-wide logger writing to the console."""
    logger = logging.getLogger("youtube_shorts_agency")

    if logger.handlers:
        # Avoid attaching duplicate handlers when modules are re-imported.
        return logger

    logger.setLevel(logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False
    return logger

logger = _build_logger()

def validate_config() -> None:
    """Fail fast if required credentials are missing."""
    missing = []

    if not YOUTUBE_API_KEY or YOUTUBE_API_KEY == "your_youtube_api_key_here":
        missing.append("YOUTUBE_API_KEY")
    if not GROQ_API_KEY or GROQ_API_KEY == "your_groq_api_key_here":
        missing.append("GROQ_API_KEY")

    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Fill them in the .env file."
        )

def send_telegram_notification(message: str) -> bool:
    """
    Send a plain-text notification to the configured Telegram chat.
    Returns True on success, False otherwise. Never raises.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials missing; skipping notification")
        return False

    # Telegram rejects messages longer than 4096 chars.
    if len(message) > _TELEGRAM_MAX_LEN:
        message = message[: _TELEGRAM_MAX_LEN - 20] + "\n...[truncated]"

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(url, data=payload, timeout=15)
        if response.status_code != 200:
            logger.error(
                "Telegram API error %s: %s",
                response.status_code,
                response.text,
            )
            return False
        return True
    except requests.RequestException as exc:
        logger.error("Telegram request failed: %s", exc)
        return False
