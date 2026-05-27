"""Конфигурация бота. Все секреты — через переменные окружения / .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _required(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Не задана переменная окружения {name}. Проверь .env (см. .env.example)."
        )
    return val


def _int_list(raw: str | None) -> list[int]:
    if not raw:
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


# ── Telegram ─────────────────────────────────────────────────────────────────
BOT_TOKEN: str = _required("BOT_TOKEN")
ADMIN_IDS: list[int] = _int_list(os.getenv("ADMIN_IDS"))

# ── Брендинг / автор воронки ─────────────────────────────────────────────────
AUTHOR_NAME: str = os.getenv("AUTHOR_NAME", "Никита")
AUTHOR_USERNAME: str = os.getenv("AUTHOR_USERNAME", "")  # без @, для t.me/<username>
SUPPORT_USERNAME: str = os.getenv("SUPPORT_USERNAME", AUTHOR_USERNAME)

# Ссылки в кнопках follow-up — подставь свои.
# Если ссылки нет (например, ещё нет кейсов) — оставь пусто,
# соответствующая кнопка не появится в боте.
LINK_CASES: str = os.getenv("LINK_CASES", "")
LINK_BOOK_CALL: str = os.getenv("LINK_BOOK_CALL", "")
LINK_CHANNEL: str = os.getenv("LINK_CHANNEL", "")

# ── Файлы ────────────────────────────────────────────────────────────────────
ASSETS_DIR: Path = ROOT / "assets"
LEAD_MAGNET_FILE: Path = ASSETS_DIR / os.getenv(
    "LEAD_MAGNET_FILE", "База_TG-чатов_для_лидгена_2026.xlsx"
)
LEAD_MAGNET_CAPTION: str = (
    "🎁 Лови — <b>88 целевых Telegram-чатов</b> для B2B-лидгена.\n"
    "Каждый проверен вручную: только живые, активные, с реальными заказчиками.\n\n"
    "Что внутри файла:\n"
    "• ТОП-88 чатов с числом участников и описанием\n"
    "• 19 категорий: города + ниши (веб, SEO, директ, дизайн, лидген)\n"
    "• Шаблон первого сообщения (3 шага без бана)\n"
    "• Правила игры — что НЕЛЬЗЯ писать в чатах\n"
    "• «Старт за 10 минут» — пошаговый план\n\n"
    "Открой в Google Sheets:\n"
    "<i>Drive → New → File upload → ПКМ → Open with → Google Sheets</i>"
)

# ── База данных ──────────────────────────────────────────────────────────────
DB_PATH: Path = ROOT / "leads.db"

# ── Drip-кампания (минуты после получения файла) ─────────────────────────────
# Подбирай тайминги под свою аудиторию. По умолчанию: 30 мин / 1д / 3д / 7д.
DRIP_SCHEDULE_MINUTES: list[int] = [
    int(x) for x in os.getenv("DRIP_SCHEDULE_MINUTES", "30,1440,4320,10080").split(",")
]

# ── Прочее ───────────────────────────────────────────────────────────────────
TIMEZONE: str = os.getenv("TIMEZONE", "Europe/Moscow")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
