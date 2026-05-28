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

APEX_DB = "/root/leadbot/apex_ai.db"
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '') or "sk-d75f7d76a50c49648aaf061611ce62b5"
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
QUALIFIER_THRESHOLD = 85

GLOBAL_STOP_PHRASES = [
    "доступно в pro", "доступно в про", "pro подписк", "про подписк", "pro-участник", "про-участник", 
    "получили этот контакт", "получили эту вакансию", "контакт автора скрыт", "автор скрыт", 
    "хочешь быть в числе первых", "оформи подписку чтобы", "только для платных", "premium участник", 
    "премиум участник", "forms.gle", "google.com/forms", "при поддержк", "заполните анкету", 
    "заполните форму", "отклик через форму", "фриланс кот", "#вакансия", "#вакансии", "вакансий чат", 
    "работа удалённо", "удаленка"
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
    "подскажите как сделать", "как реализовать", "как настроить", "какой выбрать стек", "посоветуйте библиотек", 
    "посоветуйте сервис", "у меня не работает", "выдаёт ошибку", "не получается", "ребят, такой вопрос", 
    "коллеги, вопрос", "поделитесь опытом", "делаю под ключ", "берусь за", "веду проекты", "оказываю услуги", 
    "запишитесь на курс", "приходи на марафон", "наставничество", "обучу за", "мастер-группа"
]

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
    return any(m in text.lower() for m in ANTI_INTENT_MARKERS)

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
    sql = "SELECT mc.id, mc.text, mc.link, mc.chat_title, mc.chat_key, mc.sender_username, mc.sender_id, mc.msg_date FROM messages_corpus mc JOIN messages_fts fts ON mc.id = fts.rowid WHERE messages_fts MATCH ? AND mc.msg_date >= ? AND mc.id NOT IN (SELECT corpus_id FROM claimed_leads) ORDER BY mc.msg_date DESC LIMIT ?"
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


async def find_leads(user_id: int, niche_text: str, n: int = DEFAULT_LIMIT, niche_info: Optional[dict] = None) -> dict:
    if niche_info is None: niche_info = await analyze_niche(niche_text)
    all_qualified: list[dict] = []
    seen_ids: set[int] = set()
    total_corpus = 0
    funnel_level = "exact"

    # ═══ УРОВЕНЬ 1: ТОЧНЫЙ ПОИСК (все ключевые + гео) ═══
    candidates_l1 = await _fetch_fts_candidates(niche_info, days=LOOKBACK_DAYS, exclude_claimed_for_user=user_id, limit=FTS_CANDIDATES)
    total_corpus += len(candidates_l1)
    if candidates_l1:
        q1 = await _qualify_candidates(candidates_l1, niche_info, seen_ids)
        for lead in q1:
            lead["funnel_level"] = "exact"
            seen_ids.add(lead["corpus_id"])
        all_qualified.extend(q1)
        logger.info("Funnel L1 (exact): %d candidates -> %d qualified", len(candidates_l1), len(q1))

    # ═══ УРОВЕНЬ 2: ШИРОКИЙ ПОИСК (без гео, только service_keywords) ═══
    if len(all_qualified) < n:
        broad_info = dict(niche_info)
        broad_info["geo"] = None
        broad_info["geo_strict"] = False
        broad_info["vertical_strict"] = False
        broad_info["vertical_keywords"] = []
        candidates_l2 = await _fetch_fts_candidates(broad_info, days=LOOKBACK_DAYS, exclude_claimed_for_user=user_id, limit=FTS_CANDIDATES)
        new_candidates_l2 = [c for c in candidates_l2 if c["id"] not in seen_ids]
        total_corpus += len(new_candidates_l2)
        if new_candidates_l2:
            q2 = await _qualify_candidates(new_candidates_l2, niche_info, seen_ids)
            for lead in q2:
                lead["funnel_level"] = "broad"
                seen_ids.add(lead["corpus_id"])
            all_qualified.extend(q2)
            funnel_level = "broad"
            logger.info("Funnel L2 (broad): %d candidates -> %d qualified", len(new_candidates_l2), len(q2))

    # ═══ УРОВЕНЬ 3: СМЕЖНЫЕ НИШИ (Groq генерирует adjacent keywords) ═══
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
                candidates_l3 = await _fetch_fts_candidates(adjacent_info, days=LOOKBACK_DAYS, exclude_claimed_for_user=user_id, limit=FTS_CANDIDATES)
                new_candidates_l3 = [c for c in candidates_l3 if c["id"] not in seen_ids]
                total_corpus += len(new_candidates_l3)
                if new_candidates_l3:
                    adj_niche_for_qualify = dict(niche_info)
                    adj_niche_for_qualify["service_keywords"] = list(set(niche_info.get("service_keywords", []) + adjacent_kw))
                    adj_niche_for_qualify["vertical_strict"] = False
                    q3 = await _qualify_candidates(new_candidates_l3, adj_niche_for_qualify, seen_ids)
                    for lead in q3:
                        lead["funnel_level"] = "adjacent"
                        lead["text"] = "\ud83d\udd04 [Смежная ниша] " + lead["text"]
                        seen_ids.add(lead["corpus_id"])
                    all_qualified.extend(q3)
                    funnel_level = "adjacent"
                    logger.info("Funnel L3 (adjacent): %d candidates -> %d qualified", len(new_candidates_l3), len(q3))
        except Exception as e:
            logger.warning("Funnel L3 (adjacent) failed: %s", e)

    # ═══ FALLBACK: ДЕМО-ЛИД ИЗ КЭША ═══
    if not all_qualified:
        async with aiosqlite.connect(APEX_DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT mc.id, mc.text, mc.link, mc.chat_title, mc.chat_key, mc.sender_username, mc.sender_id, mc.msg_date, pr.ai_score, pr.ai_pain, pr.ai_fit_service "
                "FROM pending_review pr JOIN messages_corpus mc ON mc.id = pr.corpus_id WHERE pr.ai_score >= 85 AND pr.status = 'pending' ORDER BY pr.id DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
                if row:
                    all_qualified.append({
                        "corpus_id": row["id"],
                        "text": "\u26a0\ufe0f [В текущем окне парсинга живых запросов нет. Это демонстрационный пример эталонного лида из нашего кэша, чтобы вы увидели качество ИИ-анализа боли клиента]:\n\n" + (row["text"] or ""),
                        "link": row["link"], "chat_title": row["chat_title"], "chat_key": row["chat_key"],
                        "sender_username": row["sender_username"], "sender_id": row["sender_id"], "msg_date": row["msg_date"],
                        "score": row["ai_score"], "pain": "\ud83d\udd25 [ДЕМО-КЭШ]: " + (row["ai_pain"] or ""), "fit_service": row["ai_fit_service"],
                        "funnel_level": "demo_cache"
                    })

    all_qualified.sort(key=lambda x: (x.get("score", 0), x.get("msg_date", "")), reverse=True)
    return {"niche_info": niche_info, "stats": {"corpus_match": total_corpus, "qualified": len(all_qualified), "lookback_days": LOOKBACK_DAYS, "funnel_level": funnel_level}, "leads": all_qualified[:n]}

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
