"""
source_policy.py — классификация чатов по политике источника.

Используется в:
  - backfill_source_policy.py (разовая миграция существующих чатов)
  - discoverer_v4.py (при добавлении новых чатов)
  - parser_apex_ai.py (фильтрация при парсинге)
  - query_engine.py (фильтрация при поиске лидов)

Политики:
  allow  — узкоцелевой B2B-чат (маркетинг, директ, SEO, SMM, подрядчики)
  mixed  — промежуточный, может содержать целевые запросы
  block  — мусор (PRO/Premium, биржи, вакансии, крипта, нетворкинг)
"""
from __future__ import annotations

import re

# ── BLOCK-паттерны: чат точно мусорный ──
BLOCK_KEYWORDS = [
    # PRO / Premium / VIP / платный доступ
    r"\bpro\b", r"\bpremium\b", r"\bvip\b", r"\bпремиум\b", r"\bпро\b",
    r"платн\w*\s*(доступ|подписк|участ)", r"только\s+для\s+платных",
    r"контакт\s*(автора\s+)?скрыт", r"получили\s+этот\s+контакт",
    # Биржи / агрегаторы / тендеры
    r"\bбиржа\b", r"\bтендер\b", r"\bагрегатор\b", r"\baукцион\b",
    r"\bbirzha\b", r"\btender\b",
    # Вакансии / резюме / работа
    r"\bвакан[сц]и", r"\bрезюме\b", r"\bработа\b", r"\bтрудоустрой",
    r"\bhh\b", r"\bjob\b", r"\bjobs\b", r"\bhiring\b", r"\brecruit",
    r"\bподработ", r"\bсоискател", r"\bзарплат",
    # Фриланс-свалки
    r"\bфриланс\b", r"\bfreelance\b", r"\bfreelans\b", r"\bфрилансер",
    r"fl[\._-]?ru", r"upwork", r"kwork",
    # Нетворкинг / знакомства
    r"\bнетворкинг\b", r"\bзнакомств", r"\bdating\b", r"\bnetwork(ing)?\b",
    # Крипта / инвестиции / арбитраж
    r"\bкрипт", r"\bcrypto\b", r"\bинвестиц", r"\bарбитраж\b",
    r"\btrading\b", r"\bтрейдинг\b", r"\bбинанс", r"\bbinance\b",
    r"\bp2p\b", r"\bcasino\b", r"\bказино\b", r"\bставки\b",
    # Барахолки / объявления
    r"\bбарахол", r"\bобъявлени", r"\bbaraholka\b",
    # Общие бизнес-свалки
    r"\bпредприниматели\b", r"\bстартап\w*\b",
]

# ── ALLOW-паттерны: узкоцелевой B2B-чат ──
ALLOW_KEYWORDS = [
    r"\bмаркетинг", r"\bmarketing\b",
    r"\bдирект\b", r"\bдиректолог", r"\bконтекст\w*\s*реклам",
    r"\bяндекс\s*(директ|реклам)", r"\bgoogle\s*ads\b",
    r"\bseo\b", r"\bсео\b", r"\bпродвижени",
    r"\bsmm\b", r"\bсмм\b", r"\bтаргет\w*\b",
    r"\bподрядчик", r"\bисполнител", r"\bзаказчик",
    r"\bлидоген", r"\bлидген\b", r"\bleadgen\b",
    r"\bреклам\w*\s*(агент|студ|настрой)",
    r"\bвеб[\s-]*(студи|разработ|дизайн)",
    r"\bдизайн\w*\s*(студи|агент)",
    r"\bразработк\w*\s*(сайт|приложен|мобильн)",
    r"\bit[\s-]*(подрядчик|аутсорс)",
]

_BLOCK_RX = [re.compile(p, re.IGNORECASE | re.UNICODE) for p in BLOCK_KEYWORDS]
_ALLOW_RX = [re.compile(p, re.IGNORECASE | re.UNICODE) for p in ALLOW_KEYWORDS]


def classify_chat(title: str, username: str = "", description: str = "") -> tuple[str, str]:
    """
    Классифицирует чат по title/username/description.
    Возвращает (policy, reason).
    """
    text = f"{title} {username} {description}".lower()

    # Сначала проверяем BLOCK
    for rx in _BLOCK_RX:
        m = rx.search(text)
        if m:
            return "block", f"block: {m.group()}"

    # Затем проверяем ALLOW
    for rx in _ALLOW_RX:
        m = rx.search(text)
        if m:
            return "allow", f"allow: {m.group()}"

    return "mixed", "mixed: не подошёл ни под block, ни под allow"


# ── ALTER TABLE миграция ──
MIGRATE_SQL = [
    "ALTER TABLE target_chats ADD COLUMN source_policy TEXT DEFAULT 'mixed'",
    "ALTER TABLE target_chats ADD COLUMN source_quality TEXT",
    "ALTER TABLE target_chats ADD COLUMN source_reason TEXT",
]


async def ensure_source_policy_columns(db) -> None:
    """Добавляет колонки source_policy/source_quality/source_reason если их нет."""
    for sql in MIGRATE_SQL:
        try:
            await db.execute(sql)
        except Exception:
            pass  # колонка уже существует
