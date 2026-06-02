import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import aiosqlite
from openai import AsyncOpenAI
import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from blacklist import should_skip_chat
except ImportError:
    def should_skip_chat(_): return False

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_not_exception_type
from paths import APEX_DB as _PATHS_APEX_DB
from source_policy import ensure_source_policy_columns

APEX_DB = os.environ.get("APEX_DB") or _PATHS_APEX_DB
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '') or "sk-d75f7d76a50c49648aaf061611ce62b5"
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
QUALIFIER_THRESHOLD = 85

GLOBAL_STOP_PHRASES = [
    # PRO / Premium / VIP / платный доступ
    "доступно в pro", "доступно в про", "pro подписк", "про подписк",
    "pro-участник", "про-участник", "premium", "vip",
    "только для платных", "оформи подписку", "доступ после оплаты",
    # Скрытый контакт / посредник
    "получили этот контакт", "получили эту вакансию",
    "контакт автора скрыт", "автор скрыт",
    "хочешь быть в числе первых", "премиум участник",
    # Биржи / тендеры / агрегаторы
    "биржа", "тендер", "агрегатор",
    # Формы / анкеты
    "forms.gle", "google.com/forms", "при поддержк",
    "заполните анкету", "заполните форму", "отклик через форму",
    # Вакансии / работа
    "#вакансия", "#вакансии", "вакансий чат",
    "работа удалённо", "удаленка",
    # Фриланс-свалка
    "фриланс кот",
]

INTENT_MARKERS = [
    "ищу", "ищем", "нужен", "нужна", "нужны", "нужно", "посоветуйте подрядчик", "посоветуйте специалист", 
    "посоветуйте агентств", "подскажите подрядчик", "подскажите специалист", "хочу заказать", "хочу найти", 
    "хочу нанять", "кто может сделать", "кто умеет делать", "кто возьм", "кто берёт", "возьму в работу", 
    "приму в работу", "требуется подрядчик", "требуется агентств", "требуется специалист", "необходим подрядчик", 
    "необходим специалист", "ищу исполнител", "ищу подрядчик", "ищу агентств", "порекомендуйте", 
    "рекомендуйте", "готов оплатить", "готовы заплатить"
]

ANTI_INTENT_MARKERS = [
    "подскажите как сделать", "как реализовать", "как настроить", "какой выбрать стек",
    "посоветуйте библиотек", "посоветуйте сервис",
    "у меня не работает", "выдаёт ошибку", "не получается",
    "ребят, такой вопрос", "коллеги, вопрос", "поделитесь опытом",
    "запишитесь на курс", "приходи на марафон", "наставничество",
    "обучу за", "мастер-группа",
]

# Регулярные выражения для фильтра самопиара (SELF_PROMO)
# Ловят «я дизайнер», «мы — студия», «мои услуги», «портфолио» и т.д.
SELF_PROMO_RX = re.compile(
    r"(?:"
    # «я/мы + профессия»
    r"\b(?:я|мы)\s+(?:—\s*)?(?:дизайнер|маркетолог|директолог|таргетолог|сеошник|seo|smm|копирайтер|разработчик|программист|фрилансер|специалист|студия|агентство|команда)\w*"
    r"|"
    # прямое предложение услуг
    r"(?:предлагаю|оказываю|делаю\s+под\s+ключ|берусь\s+за|веду\s+проекты|оказываем|предлагаем)\s+\w+"
    r"|"
    # самореклама
    r"мои?\s+(?:услуги|портфолио|работы|кейсы|проекты)"
    r"|"
    # призыв в личку
    r"(?:пишите|пиши|обращайтесь|обращайся)\s+(?:в\s+)?(?:лс|л\.с\.|личк|дм|dm|директ)"
    r"|"
    # портфолио / отзывы / кейсы в чистом виде
    r"\b(?:портфолио|отзывы\s+клиентов|мой\s+сайт|моё?\s+портфолио)\b"
    r"|"
    # «создам / сделаю / настрою + услугу»
    r"\b(?:создам|сделаю|настрою|разработаю|запущу|помогу\s+с)\s+(?:дизайн|сайт|лендинг|рекламу|воронку|бот|приложение|логотип|фирменный|маркетинг)\w*"
    r")",
    re.IGNORECASE | re.UNICODE,
)

HIRING_MARKERS = [
    "в команду", "в нашу команду", "в нашу студию", "в наш отдел", "в штат", "штатно", "оформление по тк", 
    "оформление тк", "официальное трудоустрой", "официальная зп", "белая зарплата", "зарплата от", "вилка зп", 
    "вилка зарплат", "ставка от", "пишите hr", "наш hr", "контакт hr", "наш менеджер свяжется", 
    "присылайте резюме", "присылай резюме", "отправляйте резюме", "резюме на почт", "cv на почт", 
    "график 5/2", "график 2/2", "сменный график", "испытательный срок", "соцпакет", "кидирамиз", 
    "кидирвомиз", "кидирилади", "командага", "талаб килин", "талаб этил", "иш буш", "иш хакки", 
    "we are hiring", "we're hiring", "looking to hire", "join our team", "send your cv", "send resume", "salary range"
]

HIRING_REGEX = re.compile(
    r"\b(ищем|ищу|нужен|нужна|требуется|разыскиваем|разыскивается)\s+(в\s+(штат|команду|отдел|студию)\b|\w*(маркетолог|таргетолог|директолог|seo|smm|разработчик|программист|дизайнер|менеджер|продажник|hr|оператор|курьер|сммщик|копирайтер)\w*)",
    re.IGNORECASE | re.UNICODE
)

DEFAULT_LIMIT = 10
FTS_CANDIDATES = 150
LOOKBACK_DAYS = 30
MIN_TEXT_LEN = 40
logger = logging.getLogger(__name__)

ai_client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com/v1", max_retries=0, timeout=45.0)
groq_client = AsyncOpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1", max_retries=2, timeout=30.0)

_CYRILLIC_RX = re.compile(r"[\u0400-\u04FF]")
_LETTERS_RX = re.compile(r"[a-zA-Zа-яА-Я\u0400-\u04FF]")
_WORD_SPLIT_RX = re.compile(r"\W+", re.UNICODE)

def _has_intent_markers(text: str) -> bool:
    if not text: return False
    return any(m in text.lower() for m in INTENT_MARKERS)

def _has_anti_intent(text: str) -> bool:
    if not text: return False
    t = text.lower()
    if any(m in t for m in ANTI_INTENT_MARKERS): return True
    if SELF_PROMO_RX.search(t): return True
    return False

def _is_hiring_post(text: str) -> bool:
    if not text: return False
    t = text.lower()
    if any(m in t for m in HIRING_MARKERS): return True
    return bool(HIRING_REGEX.search(t))

def _cyrillic_ratio(text: str) -> float:
    if not text: return 0.0
    letters = _LETTERS_RX.findall(text)
    if not letters: return 0.0
    cyrillic = sum(1 for ch in letters if _CYRILLIC_RX.match(ch))
    return cyrillic / len(letters)

def _normalize_for_dedup(text: str) -> str:
    if not text: return ""
    words = [w for w in _WORD_SPLIT_RX.split(text.lower()) if w]
    return " ".join(words)[:200]

def _passes_pre_ai_filter(text: str, min_cyrillic: float = 0.4) -> tuple[bool, str]:
    if not text or len(text.strip()) < MIN_TEXT_LEN: return False, "too_short"
    if _cyrillic_ratio(text) < min_cyrillic: return False, "non_cyrillic"
    if _is_hiring_post(text): return False, "hiring_post"
    if _has_anti_intent(text): return False, "anti_intent"
    if not _has_intent_markers(text): return False, "no_intent_marker"
    return True, ""

async def init_apex_db() -> None:
    async with aiosqlite.connect(APEX_DB) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("CREATE TABLE IF NOT EXISTS claimed_leads (corpus_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_claimed_user ON claimed_leads(user_id)")
        await db.execute("CREATE TABLE IF NOT EXISTS subscriptions (user_id INTEGER PRIMARY KEY, niche_text TEXT NOT NULL, niche_keywords TEXT, started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, expires_at TIMESTAMP, leads_delivered INTEGER DEFAULT 0, status TEXT DEFAULT 'active')")
        await ensure_source_policy_columns(db)
        await db.commit()

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=3, max=30), retry=retry_if_not_exception_type(RuntimeError))
async def analyze_niche(niche_text: str) -> dict:
    prompt = f"""Ты — эксперт по B2B-лидгену и синонимам в Telegram. Клиент описал свою нишу свободной фразой: "{niche_text}".
Твоя задача — сгенерировать глубокое семантическое облако для поиска лидов по их БОЛИ и СЛЕНГУ.

Верни СТРОГО JSON следующего вида:
{{
"niche_canonical": "директолог москва",
"service": "директолог",
"vertical": "контекстная реклама",
"vertical_strict": false,
"geo": "москва",
"geo_strict": false,
"service_keywords": ["директолог", "контекстолог", "яндекс директ", "настройка рекламы", "реклама в яндексе", "кампании в директе", "трафик из яндекса", "дорогие лиды", "упали заявки", "слив бюджета", "аудит рекламы", "контекст"],
"vertical_keywords": [],
"negative_verticals": ["обучу", "курс", "вакансия"],
"stop_keywords": [],
"what_we_sell": "настройка и аудит контекстной рекламы Яндекс.Директ"
}}

ПРАВИЛА ДЛЯ VERTICAL_STRICT:
1. Если клиент указал КОНКРЕТНУЮ сферу/отрасль (например "smm для ресторанов", "сайт для стоматологии", "юрист по недвижимости") — ставь vertical_strict: true и заполни vertical_keywords словами этой сферы.
2. Если ниша ОБЩАЯ без привязки к сфере (например "директолог", "seo", "smm") — ставь vertical_strict: false.
3. При vertical_strict=true ОБЯЗАТЕЛЬНО заполни negative_verticals — антонимы сферы (если клиент = рестораны, negative_verticals = ["стоматолог", "медицин", "клиник", "авто", "недвижимост"]).

ПРАВИЛА ДЛЯ SERVICE_KEYWORDS:
1. Включай название профессии и смежный сленг (контекстолог, директ, контекст).
2. ОБЯЗАТЕЛЬНО добавляй фразы боли бизнеса: "упал трафик", "дорогие лиды", "сливаем бюджет", "настроить рекламу", "заявки из яндекса".
3. Покрывай разные части речи (настройка, настроить). Опечатки исправляй."""
    res = await groq_client.chat.completions.create(model="llama-3.3-70b-versatile", messages=[{"role": "user", "content": prompt}], response_format={"type": "json_object"}, temperature=0.0)
    data = json.loads(res.choices[0].message.content)
    data.setdefault("service_keywords", data.get("keywords") or [])
    data.setdefault("vertical_keywords", [])
    data.setdefault("negative_verticals", [])
    data.setdefault("stop_keywords", [])
    data.setdefault("vertical", None)
    data.setdefault("vertical_strict", False)
    data.setdefault("geo", None)
    data.setdefault("geo_strict", False)
    data.setdefault("service", data.get("niche_canonical"))
    data["service_keywords"] = [k.lower().strip() for k in data["service_keywords"] if k]
    data["vertical_keywords"] = [k.lower().strip() for k in data["vertical_keywords"] if k]
    data["negative_verticals"] = [k.lower().strip() for k in data["negative_verticals"] if k]
    data["stop_keywords"] = [k.lower().strip() for k in data["stop_keywords"] if k]
    data["keywords"] = data["service_keywords"] + data["vertical_keywords"]
    # Если vertical указан и есть vertical_keywords — принудительно strict
    if data.get("vertical") and data.get("vertical_keywords"):
        data["vertical_strict"] = True
    return data

def _safe_kw(kw: str) -> str: return re.sub(r'["()*]', '', kw or '').strip()

def _fts_group(keywords: list[str]) -> str:
    parts = []
    for kw in keywords:
        k = _safe_kw(kw)
        if not k: continue
        parts.append(f'"{k}"' if " " in k else f'{k}*')
    return " OR ".join(parts)

def _fts_query_from_niche(niche_info: dict) -> str:
    svc = _fts_group(niche_info.get("service_keywords") or niche_info.get("keywords") or [])
    if not svc: return '""'
    if niche_info.get("vertical_strict") and niche_info.get("vertical_keywords"):
        vert = _fts_group(niche_info["vertical_keywords"])
        if vert: return f"({svc}) AND ({vert})"
    return f"({svc})"

def _fts_query_from_keywords(keywords: list[str]) -> str: return _fts_group(keywords) or '""'

async def count_potential_leads(keywords: list[str], days: int = LOOKBACK_DAYS) -> int:
    if not keywords: return 0
    fts_q = _fts_query_from_keywords(keywords)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(APEX_DB) as db:
        try:
            async with db.execute("SELECT COUNT(*) FROM messages_corpus mc JOIN messages_fts fts ON mc.id = fts.rowid WHERE messages_fts MATCH ? AND mc.msg_date >= ?", (fts_q, cutoff)) as cur:
                row = await cur.fetchone()
                return row[0] if row else 0
        except Exception as e:
            logger.error(f"FTS COUNT error: {e}")
            return 0

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=3, max=30), retry=retry_if_not_exception_type(RuntimeError))
async def qualify_message_for_niche(text: str, niche_info: dict) -> dict:
    prompt = f"""Ты — СТРОГИЙ Lead Qualifier. По умолчанию REJECT. APPROVE только если все 6 GATE пройдены подряд.
ПРОФИЛЬ КЛИЕНТА:
service: {niche_info.get('service') or niche_info.get('niche_canonical')}
vertical: {niche_info.get('vertical') or 'не указано'}
vertical_strict: {niche_info.get('vertical_strict', False)}
service_keywords: {niche_info.get('service_keywords', [])}
vertical_keywords: {niche_info.get('vertical_keywords', [])}
negative_verticals: {niche_info.get('negative_verticals', [])}
СООБЩЕНИЕ: "{text[:1500]}"
GATE 1 — ЯЗЫК: не русский → REJECT.
GATE 2a — VERTICAL (если strict): нет слов из vertical_keywords или есть из negative_verticals → REJECT.
GATE 2b — SERVICE (ВСЕГДА): искомая услуга ДОЛЖНА совпадать с service клиента или его service_keywords. Если клиент = директолог, а просят чат-бота/CRM/SEO/SMM -> REJECT, service_match=false.
GATE 3 — ТИП: вакансия/резюме/новость/вопрос на форуме/продажа услуг → REJECT.
GATE 4 — ИНТЕНТ: "ищу", "нам нужен", "ищем команду", "хочу заказать". Без этого → REJECT.
GATE 5 — РОЛЬ: автор = заказчик.
СКОРИНГ: 95-100 = запрос + конкретика; 90-94 = запрос без бюджета; 85-89 = запрос + service match без деталей. <85 = REJECT. 85-89 — нормальный скор!
Верни СТРОГО JSON: {{"status": "APPROVE"|"REJECT", "score": 0-100, "service_match": true|false, "vertical_match": true|false, "commercial_intent": true|false, "pain": "фраза", "fit_service": "услуга", "reason": "почему"}}"""
    res = await ai_client.chat.completions.create(model="deepseek-chat", messages=[{"role": "user", "content": prompt}], response_format={"type": "json_object"}, temperature=0.0)
    out = json.loads(res.choices[0].message.content)
    if niche_info.get("vertical_strict") and out.get("vertical_match") is False:
        out["status"] = "REJECT"; out["score"] = 0
    if out.get("service_match") is False:
        out["status"] = "REJECT"; out["score"] = 0
    if out.get("commercial_intent") is False:
        out["status"] = "REJECT"; out["score"] = min(out.get("score", 0), 49)
    return out

async def _fetch_fts_candidates(niche_info: dict, days: int, exclude_claimed_for_user: Optional[int], limit: int) -> list[dict]:
    fts_q = _fts_query_from_niche(niche_info)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    stop_keywords = niche_info.get("stop_keywords", [])
    neg_verts = niche_info.get("negative_verticals", [])
    sql = (
        "SELECT mc.id, mc.text, mc.link, mc.chat_title, mc.chat_key, "
        "mc.sender_username, mc.sender_id, mc.msg_date, "
        "COALESCE(tc.source_policy, 'mixed') AS source_policy "
        "FROM messages_corpus mc "
        "JOIN messages_fts fts ON mc.id = fts.rowid "
        "LEFT JOIN target_chats tc ON tc.chat_identifier = mc.chat_key "
        "WHERE messages_fts MATCH ? AND mc.msg_date >= ? "
        "AND mc.id NOT IN (SELECT corpus_id FROM claimed_leads) "
        "AND COALESCE(tc.source_policy, 'mixed') != 'block' "
        "ORDER BY mc.msg_date DESC LIMIT ?"
    )
    rows: list[dict] = []
    seen_hashes: set[str] = set()
    async with aiosqlite.connect(APEX_DB) as db:
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute(sql, (fts_q, cutoff, limit * 3)) as cur:
                async for row in cur:
                    chat_key = row["chat_key"] or ""
                    if should_skip_chat(chat_key): continue
                    text = row["text"] or ""
                    text_lower = text.lower()
                    if any(sk in text_lower for sk in stop_keywords + GLOBAL_STOP_PHRASES): continue
                    if any(nv in text_lower for nv in neg_verts): continue
                    text_hash = _normalize_for_dedup(text)
                    if text_hash and text_hash in seen_hashes: continue
                    passed, _ = _passes_pre_ai_filter(text)
                    if not passed: continue
                    seen_hashes.add(text_hash)
                    rows.append(dict(row))
                    if len(rows) >= limit: break
        except Exception as e: logger.error(f"FTS Search Error: {e}")
    return rows

@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=2, min=3, max=15), retry=retry_if_not_exception_type(RuntimeError))
async def generate_adjacent_keywords(niche_info: dict) -> list[str]:
    """Groq генерирует смежные ключевые слова для воронки (уровень 3)."""
    service = niche_info.get("service") or niche_info.get("niche_canonical") or ""
    existing = niche_info.get("service_keywords", [])
    prompt = f"""Ты — эксперт по B2B-лидгену в Telegram.
Основная услуга клиента: "{service}"
Текущие ключевые слова: {existing[:10]}

Сгенерируй 10-15 СМЕЖНЫХ ключевых слов для РОДСТВЕННЫХ ниш, которые могут заинтересовать этого специалиста.
Например, если клиент — директолог, смежные: "настройка воронки", "лендинг", "аналитика сайта", "конверсия сайта", "CPA", "лидогенерация".
Это НЕ те же слова, а СМЕЖНЫЕ услуги/темы, которые часто нужны тем же заказчикам.

Верни СТРОГО JSON: {{"adjacent_keywords": ["слово1", "слово2", ...]}}"""
    res = await groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}, temperature=0.3
    )
    data = json.loads(res.choices[0].message.content)
    return [k.lower().strip() for k in data.get("adjacent_keywords", []) if k]


async def _qualify_candidates(candidates: list[dict], niche_info: dict, existing_ids: set[int]) -> list[dict]:
    """AI-квалификация кандидатов через DeepSeek."""
    filtered = [c for c in candidates if c["id"] not in existing_ids]
    if not filtered:
        return []
    sem = asyncio.Semaphore(5)
    async def qualify_one(cand):
        async with sem:
            try: return await qualify_message_for_niche(cand["text"], niche_info)
            except: return None
    results = await asyncio.gather(*[qualify_one(c) for c in filtered])
    qualified = []
    for i, r in enumerate(results):
        if r and r.get("status") == "APPROVE" and r.get("score", 0) >= QUALIFIER_THRESHOLD:
            c = filtered[i]
            qualified.append({"corpus_id": c["id"], "text": c["text"], "link": c["link"], "chat_title": c["chat_title"], "chat_key": c["chat_key"], "sender_username": c["sender_username"], "sender_id": c.get("sender_id"), "msg_date": c["msg_date"], "score": r.get("score"), "pain": r.get("pain"), "fit_service": r.get("fit_service")})
    return qualified


# ═══ Словарь семантического расширения для IT/tech-ниш ═══
SEMANTIC_EXPANSION: dict[str, list[str]] = {
    "next.js": ["разработка сайтов", "веб-разработка", "frontend", "react", "лендинг", "корпоративный сайт", "сайт под ключ"],
    "nextjs": ["разработка сайтов", "веб-разработка", "frontend", "react", "лендинг", "корпоративный сайт"],
    "react": ["frontend", "разработка сайтов", "веб-разработка", "лендинг", "spa", "веб приложение"],
    "vue": ["frontend", "разработка сайтов", "веб-разработка", "лендинг", "spa", "веб приложение"],
    "angular": ["frontend", "разработка сайтов", "веб-разработка", "корпоративный портал"],
    "frontend": ["разработка сайтов", "веб-разработка", "лендинг", "react", "вёрстка", "корпоративный сайт"],
    "фронтенд": ["разработка сайтов", "веб-разработка", "лендинг", "вёрстка", "корпоративный сайт"],
    "backend": ["разработка сайтов", "веб-разработка", "api", "серверная разработка", "python разработка"],
    "бэкенд": ["разработка сайтов", "веб-разработка", "api", "серверная разработка"],
    "python": ["разработка", "автоматизация", "бот", "парсинг", "backend", "data science"],
    "node.js": ["backend", "разработка", "api", "бот", "веб-разработка"],
    "php": ["разработка сайтов", "wordpress", "битрикс", "веб-разработка", "интернет-магазин"],
    "wordpress": ["разработка сайтов", "лендинг", "корпоративный сайт", "интернет-магазин", "cms"],
    "битрикс": ["разработка сайтов", "корпоративный портал", "интернет-магазин", "crm"],
    "1с-битрикс": ["разработка сайтов", "корпоративный портал", "интернет-магазин", "crm"],
    "тильда": ["лендинг", "сайт", "корпоративный сайт", "сайт под ключ", "лендинг под ключ"],
    "tilda": ["лендинг", "сайт", "корпоративный сайт", "сайт под ключ"],
    "flutter": ["мобильное приложение", "разработка приложений", "ios", "android"],
    "swift": ["ios разработка", "мобильное приложение", "apple"],
    "kotlin": ["android разработка", "мобильное приложение"],
    "мобильное приложение": ["ios разработка", "android разработка", "flutter", "react native"],
    "сайты": ["разработка сайтов", "лендинг", "корпоративный сайт", "интернет-магазин", "сайт под ключ", "веб-разработка"],
    "сайт": ["разработка сайтов", "лендинг", "корпоративный сайт", "интернет-магазин", "сайт под ключ"],
    "лендинг": ["разработка сайтов", "сайт под ключ", "тильда", "посадочная страница", "конверсия"],
    "интернет-магазин": ["разработка сайтов", "e-commerce", "shopify", "woocommerce", "opencart"],
    "бот": ["чат-бот", "телеграм бот", "автоматизация", "разработка ботов"],
    "чат-бот": ["бот", "телеграм бот", "автоматизация", "разработка ботов", "воронка"],
    "crm": ["автоматизация", "amocrm", "битрикс24", "внедрение crm", "интеграция"],
    "amocrm": ["crm", "автоматизация", "воронка продаж", "внедрение crm"],
    "автоматизация": ["crm", "бот", "интеграция", "api", "бизнес-процессы"],
}

def _get_semantic_expansion(niche_info: dict) -> list[str]:
    service = (niche_info.get("service") or "").lower().strip()
    canonical = (niche_info.get("niche_canonical") or "").lower().strip()
    existing_kw = set(k.lower() for k in niche_info.get("service_keywords", []))
    expanded: list[str] = []
    for key, synonyms in SEMANTIC_EXPANSION.items():
        if key in service or key in canonical or key in existing_kw:
            for s in synonyms:
                if s.lower() not in existing_kw:
                    expanded.append(s)
    return list(dict.fromkeys(expanded))


async def find_leads(user_id: int, niche_text: str, n: int = DEFAULT_LIMIT, niche_info: Optional[dict] = None) -> dict:
    if niche_info is None: niche_info = await analyze_niche(niche_text)
    all_qualified: list[dict] = []
    seen_ids: set[int] = set()
    total_corpus = 0
    match_type = "no-result"

    # ═══ УРОВЕНЬ 1: EXACT (точное совпадение ниши + гео) ═══
    candidates_l1 = await _fetch_fts_candidates(niche_info, days=LOOKBACK_DAYS, exclude_claimed_for_user=user_id, limit=FTS_CANDIDATES)
    total_corpus += len(candidates_l1)
    if candidates_l1:
        q1 = await _qualify_candidates(candidates_l1, niche_info, seen_ids)
        for lead in q1:
            lead["match_type"] = "exact"
            seen_ids.add(lead["corpus_id"])
        all_qualified.extend(q1)
        if q1:
            match_type = "exact"
        logger.info("Cascade L1 (exact): %d candidates -> %d qualified", len(candidates_l1), len(q1))

    # ═══ УРОВЕНЬ 2: GEO_RELAXED (точная ниша, без гео) ═══
    if len(all_qualified) < n:
        geo_relaxed_info = dict(niche_info)
        geo_relaxed_info["geo"] = None
        geo_relaxed_info["geo_strict"] = False
        candidates_l2 = await _fetch_fts_candidates(geo_relaxed_info, days=LOOKBACK_DAYS, exclude_claimed_for_user=user_id, limit=FTS_CANDIDATES)
        new_l2 = [c for c in candidates_l2 if c["id"] not in seen_ids]
        total_corpus += len(new_l2)
        if new_l2:
            q2 = await _qualify_candidates(new_l2, niche_info, seen_ids)
            for lead in q2:
                lead["match_type"] = "geo_relaxed"
                seen_ids.add(lead["corpus_id"])
            all_qualified.extend(q2)
            if q2 and match_type == "no-result":
                match_type = "geo_relaxed"
            logger.info("Cascade L2 (geo_relaxed): %d candidates -> %d qualified", len(new_l2), len(q2))

    # ═══ УРОВЕНЬ 3: BROAD_SERVICE (без гео, без vertical_strict + семантическое расширение) ═══
    if len(all_qualified) < n:
        broad_info = dict(niche_info)
        broad_info["geo"] = None
        broad_info["geo_strict"] = False
        broad_info["vertical_strict"] = False
        broad_info["vertical_keywords"] = []
        # Семантическое расширение для IT-ниш
        sem_expand = _get_semantic_expansion(niche_info)
        if sem_expand:
            broad_info["service_keywords"] = list(set(broad_info.get("service_keywords", []) + sem_expand))
            broad_info["keywords"] = broad_info["service_keywords"]
            logger.info("Semantic expansion: +%d keywords -> %s", len(sem_expand), sem_expand[:5])
        candidates_l3 = await _fetch_fts_candidates(broad_info, days=LOOKBACK_DAYS, exclude_claimed_for_user=user_id, limit=FTS_CANDIDATES)
        new_l3 = [c for c in candidates_l3 if c["id"] not in seen_ids]
        total_corpus += len(new_l3)
        if new_l3:
            broad_qualify = dict(niche_info)
            broad_qualify["vertical_strict"] = False
            if sem_expand:
                broad_qualify["service_keywords"] = list(set(niche_info.get("service_keywords", []) + sem_expand))
            q3 = await _qualify_candidates(new_l3, broad_qualify, seen_ids)
            for lead in q3:
                lead["match_type"] = "broad_service"
                seen_ids.add(lead["corpus_id"])
            all_qualified.extend(q3)
            if q3 and match_type == "no-result":
                match_type = "broad_service"
            logger.info("Cascade L3 (broad_service): %d candidates -> %d qualified", len(new_l3), len(q3))

    # ═══ УРОВЕНЬ 4: ADJACENT_USEFUL (Groq генерирует смежные ключевые слова) ═══
    if len(all_qualified) < n:
        try:
            adjacent_kw = await generate_adjacent_keywords(niche_info)
            if adjacent_kw:
                adjacent_info = dict(niche_info)
                adjacent_info["service_keywords"] = adjacent_kw
                adjacent_info["keywords"] = adjacent_kw
                adjacent_info["vertical_strict"] = False
                adjacent_info["vertical_keywords"] = []
                adjacent_info["geo"] = None
                adjacent_info["geo_strict"] = False
                candidates_l4 = await _fetch_fts_candidates(adjacent_info, days=LOOKBACK_DAYS, exclude_claimed_for_user=user_id, limit=FTS_CANDIDATES)
                new_l4 = [c for c in candidates_l4 if c["id"] not in seen_ids]
                total_corpus += len(new_l4)
                if new_l4:
                    adj_qualify = dict(niche_info)
                    adj_qualify["service_keywords"] = list(set(niche_info.get("service_keywords", []) + adjacent_kw))
                    adj_qualify["vertical_strict"] = False
                    q4 = await _qualify_candidates(new_l4, adj_qualify, seen_ids)
                    for lead in q4:
                        lead["match_type"] = "adjacent_useful"
                        seen_ids.add(lead["corpus_id"])
                    all_qualified.extend(q4)
                    if q4 and match_type == "no-result":
                        match_type = "adjacent_useful"
                    logger.info("Cascade L4 (adjacent_useful): %d candidates -> %d qualified", len(new_l4), len(q4))
        except Exception as e:
            logger.warning("Cascade L4 (adjacent_useful) failed: %s", e)

    all_qualified.sort(key=lambda x: (x.get("score", 0), x.get("msg_date", "")), reverse=True)
    return {
        "niche_info": niche_info,
        "stats": {
            "corpus_match": total_corpus,
            "qualified": len(all_qualified),
            "lookback_days": LOOKBACK_DAYS,
            "match_type": match_type,
        },
        "leads": all_qualified[:n],
    }

async def claim_lead(corpus_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(APEX_DB) as db:
        try:
            await db.execute("INSERT INTO claimed_leads (corpus_id, user_id) VALUES (?, ?)", (corpus_id, user_id))
            await db.commit(); return True
        except: return False

async def get_claimed_lead(corpus_id: int) -> Optional[dict]:
    async with aiosqlite.connect(APEX_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM claimed_leads WHERE corpus_id = ?", (corpus_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def set_subscription(user_id: int, niche_text: str, niche_info: dict, duration_days: int = 30) -> dict:
    expires = datetime.now(timezone.utc) + timedelta(days=duration_days)
    async with aiosqlite.connect(APEX_DB) as db:
        await db.execute(
            """INSERT INTO subscriptions (user_id, niche_text, niche_keywords, expires_at, status)
               VALUES (?, ?, ?, ?, 'active')
               ON CONFLICT(user_id) DO UPDATE SET
                 niche_text=excluded.niche_text,
                 niche_keywords=excluded.niche_keywords,
                 expires_at=excluded.expires_at,
                 status='active',
                 leads_delivered=0""",
            (user_id, niche_text, json.dumps(niche_info, ensure_ascii=False), expires.isoformat()),
        )
        await db.commit()
    return {"user_id": user_id, "expires_at": expires.isoformat(), "niche_info": niche_info}

async def get_subscription(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(APEX_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            if not row: return None
            sub = dict(row)
            sub["niche_info"] = json.loads(sub["niche_keywords"]) if sub["niche_keywords"] else {}
            if sub.get("expires_at"):
                expires = datetime.fromisoformat(sub["expires_at"])
                if expires.tzinfo is None: expires = expires.replace(tzinfo=timezone.utc)
                sub["is_active"] = (sub["status"] == "active" and expires > datetime.now(timezone.utc))
            else:
                sub["is_active"] = False
            return sub

async def cancel_subscription(user_id: int) -> bool:
    async with aiosqlite.connect(APEX_DB) as db:
        cur = await db.execute("UPDATE subscriptions SET status='cancelled' WHERE user_id = ?", (user_id,))
        await db.commit()
        return cur.rowcount > 0

async def find_fresh_leads_for_subscriber(user_id: int, hours: int = 24, limit: int = 5) -> list[dict]:
    sub = await get_subscription(user_id)
    if not sub or not sub.get("is_active"): return []
    niche_info = sub.get("niche_info", {})
    if not niche_info or not niche_info.get("service_keywords"): return []
    fts_q = _fts_query_from_niche(niche_info)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    sql = "SELECT mc.id, mc.text, mc.link, mc.chat_title, mc.chat_key, mc.sender_username, mc.msg_date FROM messages_corpus mc JOIN messages_fts fts ON mc.id = fts.rowid WHERE messages_fts MATCH ? AND mc.indexed_at >= ? AND mc.id NOT IN (SELECT corpus_id FROM claimed_leads) ORDER BY mc.msg_date DESC LIMIT ?"
    candidates: list[dict] = []
    seen_hashes: set[str] = set()
    async with aiosqlite.connect(APEX_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, (fts_q, cutoff, limit * 10)) as cur:
            async for row in cur:
                chat_key = row["chat_key"] or ""
                if should_skip_chat(chat_key): continue
                text = row["text"] or ""
                text_lower = text.lower()
                if any(sk in text_lower for sk in niche_info.get("stop_keywords", []) + GLOBAL_STOP_PHRASES): continue
                text_hash = _normalize_for_dedup(text)
                if text_hash and text_hash in seen_hashes: continue
                passed, _ = _passes_pre_ai_filter(text)
                if not passed: continue
                seen_hashes.add(text_hash)
                candidates.append(dict(row))
                if len(candidates) >= limit * 5: break
    sem = asyncio.Semaphore(5)
    async def qualify_one(cand):
        async with sem:
            try: return await qualify_message_for_niche(cand["text"], niche_info)
            except: return None
    results = await asyncio.gather(*[qualify_one(c) for c in candidates])
    qualified = []
    for i, r in enumerate(results):
        if r and r.get("status") == "APPROVE" and r.get("score", 0) >= QUALIFIER_THRESHOLD:
            c = candidates[i]
            qualified.append({"corpus_id": c["id"], "text": c["text"], "link": c["link"], "chat_title": c["chat_title"], "chat_key": c["chat_key"], "sender_username": c["sender_username"], "msg_date": c["msg_date"], "score": r.get("score"), "pain": r.get("pain"), "fit_service": r.get("fit_service")})
    qualified.sort(key=lambda x: x["score"], reverse=True)
    return qualified[:limit]
