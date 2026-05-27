"""
query_engine.py — Поиск лидов в корпусе по нише клиента

Главные публичные функции (всё async):
  • analyze_niche(niche_text)             — AI разбирает свободный текст ниши
                                            на ключевые слова и canonical-ключ.
  • count_potential_leads(keys, days)     — быстрый SQL-счётчик «сколько в нише
                                            за N дней» (для pre-check до оплаты).
  • find_leads(user_id, niche_text, n)    — главный поиск: FTS + AI-квалификатор,
                                            возвращает топ-N релевантных лидов.
  • claim_lead(corpus_id, user_id)        — атомарная пометка лида как купленного
                                            (эксклюзивность).
  • set_subscription / get_subscription / cancel_subscription — управление подпиской.
  • find_fresh_leads_for_subscriber(user_id, hours=24) — для cron-рассылки новых
                                            лидов подписчикам.

Архитектура:
  - Парсер пишет в `messages_corpus` (с FTS5 индексом).
  - Никакого AI на стороне парсера.
  - AI вызывается ТОЛЬКО на запросе клиента → сильно экономит DeepSeek.
"""
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

# Blacklist чатов (для отсечки work/vakansii/aggregator)
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from blacklist import should_skip_chat
except ImportError:
    def should_skip_chat(_): return False

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_not_exception_type

# ==========================================
# Настройки (общие с парсером)
# ==========================================
# БД лежит рядом с этим файлом — чтобы бот, парсер и cli использовали одну.
APEX_DB = os.environ.get("APEX_DB") or str(Path(__file__).resolve().parent / 'apex_ai.db')
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')

QUALIFIER_THRESHOLD = 85

# Глобальные стоп-фразы (отсекают агрегаторы / биржи где контакт за PRO-подпиской)
GLOBAL_STOP_PHRASES = [
    "доступно в pro",
    "доступно в про",
    "pro подписк",
    "про подписк",
    "pro-участник",
    "про-участник",
    "получили этот контакт",
    "получили эту вакансию",
    "контакт автора скрыт",
    "автор скрыт",
    "хочешь быть в числе первых",
    "оформи подписку чтобы",
    "только для платных",
    "premium участник",
    "премиум участник",
    "forms.gle",
    "google.com/forms",
    "при поддержк",
    "заполните анкету",
    "заполните форму",
    "отклик через форму",
    "фриланс кот",
    "#ищу #таргетолог",
    "#ищу #директолог",
    "#ищу #контекстолог",
    "#ищу #seo",
    "#ищу #разработчик",
    "#вакансия",
    "#вакансии",
    "вакансий чат",
    "работа удалённо",
    "удаленка",  # часто в вакансиях
]

# Маркеры запроса от первого лица (если хоть один есть в тексте — может быть лид).
# Если НЕТ ни одного — сообщение почти точно НЕ запрос на услугу (отсекаем без AI).
INTENT_MARKERS = [
    # русский — запрос
    "ищу", "ищем", "нужен", "нужна", "нужны", "нужно",
    "посоветуйте", "посоветуете", "подскажите",
    "хочу заказать", "хочу найти", "хочу нанять",
    "кто может", "кто умеет", "кто делает", "кто возьм",
    "возьму", "беру в работу", "примем", "приму",
    "требуется", "требуются", "необходим", "необходима", "необходимо",
    "интересует", "интересно",
    "помогите", "помоги ",
    "рекоменду", "порекоменду",
    # вопросы с "?"
    "?",
]

# Маркеры HIRING-постов (фирма ищет сотрудника в штат). Если есть хоть один —
# почти точно вакансия, не запрос подрядчика. ОТСЕКАЕМ ДО AI.
HIRING_MARKERS = [
    # русский — найм
    "в команду", "в нашу команду", "в нашу студию", "в наш отдел",
    "ищем в команду", "ищем сотрудник", "в штат", "штатно",
    "оформление по тк", "оформление тк", "официальное трудоустрой",
    "официальная зп", "белая зарплата", "зарплата от",
    "вилка зп", "вилка зарплат", "ставка от",
    "пишите hr", "наш hr", "контакт hr",
    "наш менеджер свяжется",
    "присылайте резюме", "присылай резюме", "отправляйте резюме",
    "резюме на почт", "cv на почт",
    "график 5/2", "график 2/2", "график 5\\2", "сменный график",
    "испытательный срок",
    "от 40 000 руб", "от 50 000 руб", "от 60 000 руб",
    "от 70 000 руб", "от 80 000 руб", "от 100 000",
    "соцпакет",
    # узбекский — найм («ищем», «требуется», «в команду»)
    "кидирамиз", "кидирвомиз", "кидирилади", "кидирамыз",
    "командага", "командаг", "командамиз",
    "талаб килин", "талаб этил",
    "иш буш", "иш хакки",
    # английский
    "we are hiring", "we're hiring", "looking to hire",
    "join our team", "join the team", "send your cv", "send cv",
    "send resume", "salary range", "monthly salary",
]

      # минимальный score для показа клиенту
DEFAULT_LIMIT = 10            # сколько лидов возвращать по умолчанию
FTS_CANDIDATES = 150          # сколько кандидатов брать из FTS перед AI-фильтром
LOOKBACK_DAYS = 30            # окно поиска (свежесть лидов)
MIN_TEXT_LEN = 40             # короче — почти всегда мусор/стикер/реакция

logger = logging.getLogger(__name__)
ai_client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com/v1",
    max_retries=0,
    timeout=45.0,
)


# ==========================================
# 🛡️ Pre-AI фильтры (бесплатные регулярки до DeepSeek)
# ==========================================
_CYRILLIC_RX = re.compile(r"[\u0400-\u04FF]")
_LETTERS_RX = re.compile(r"[a-zA-Zа-яА-Я\u0400-\u04FF]")
_WORD_SPLIT_RX = re.compile(r"\W+", re.UNICODE)


def _has_intent_markers(text: str) -> bool:
    """True если есть маркер запроса от первого лица.
    Дёшево — экономит DeepSeek-токены: если нет ни одного маркера, точно не лид."""
    if not text:
        return False
    t = text.lower()
    return any(m in t for m in INTENT_MARKERS)


def _is_hiring_post(text: str) -> bool:
    """True если сообщение похоже на найм-вакансию (фирма ищет сотрудника)."""
    if not text:
        return False
    t = text.lower()
    return any(m in t for m in HIRING_MARKERS)


def _cyrillic_ratio(text: str) -> float:
    """Доля кириллицы среди БУКВ (не всего текста — игнорируем эмодзи/пунктуацию).
    1.0 = чисто русский, 0.0 = чисто латиница/арабский/китайский."""
    if not text:
        return 0.0
    letters = _LETTERS_RX.findall(text)
    if not letters:
        return 0.0
    cyrillic = sum(1 for ch in letters if _CYRILLIC_RX.match(ch))
    return cyrillic / len(letters)


def _normalize_for_dedup(text: str) -> str:
    """Нормализует текст для дедупликации (lower + слова через пробел, без знаков).
    Возвращает первые ~200 значимых символов — нормально для repost-detection."""
    if not text:
        return ""
    words = [w for w in _WORD_SPLIT_RX.split(text.lower()) if w]
    return " ".join(words)[:200]


def _passes_pre_ai_filter(text: str, min_cyrillic: float = 0.4) -> tuple[bool, str]:
    """Главный pre-AI фильтр.
    Возвращает (passed, reject_reason). Если passed=False — не отдаём в AI.

    Применяется ДО DeepSeek чтобы экономить токены и не пропускать очевидную чушь.
    Не используется в `count_potential_leads` (там FTS-COUNT нужен честный)."""
    if not text or len(text.strip()) < MIN_TEXT_LEN:
        return False, "too_short"
    if _cyrillic_ratio(text) < min_cyrillic:
        return False, "non_cyrillic"
    if _is_hiring_post(text):
        return False, "hiring_post"
    if not _has_intent_markers(text):
        return False, "no_intent_marker"
    return True, ""


# ==========================================
# 🗄️ Idempotent DB init (на случай если бот запустится раньше парсера)
# ==========================================
async def init_apex_db() -> None:
    """Создаёт таблицы если их ещё нет. Не пересоздаёт messages_corpus/FTS —
    их канонически создаёт парсер. Здесь — только claimed_leads и subscriptions."""
    async with aiosqlite.connect(APEX_DB) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS claimed_leads (
                corpus_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_claimed_user ON claimed_leads(user_id)")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                niche_text TEXT NOT NULL,
                niche_keywords TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                leads_delivered INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active'
            )
        """)
        await db.commit()


# ==========================================
# 🧠 АНАЛИЗ НИШИ (AI extract keywords)
# ==========================================
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=3, max=30),
    retry=retry_if_not_exception_type(RuntimeError),
)
async def analyze_niche(niche_text: str) -> dict:
    """
    Принимает свободный текст «у меня стройка в Москве, ремонт квартир под ключ».
    Возвращает структуру:
      {
        "niche_canonical": "ремонт квартир",
        "geo": "Москва" | null,
        "service_type": "ремонт под ключ",
        "keywords": ["ремонт", "отделка", "квартир", "под ключ"],   ← для FTS
        "stop_keywords": ["обои самостоятельно"],                    ← опционально
        "what_we_sell": "услуги ремонта/отделки квартир"
      }
    """
    prompt = f"""Ты — ассистент по AI-лидгену. Клиент описал свою нишу свободным текстом:

"{niche_text}"

Разбери эту нишу на структурированные данные для поиска по корпусу сообщений.

Верни строго JSON:
{{
  "niche_canonical": "короткое имя ниши (1-3 слова) для отображения",
  "geo": "город/регион если упомянут, иначе null",
  "service_type": "тип услуги (1 фраза)",
  "keywords": ["7-15 ключевых слов и словосочетаний на русском, которые ОДНОЗНАЧНО относятся к этой нише; писать в нижнем регистре, без знаков пунктуации; могут быть фрагменты слов (ремонт, отделк, маляр)"],
  "stop_keywords": ["слова, которые наоборот ОТСЕКАЮТ нерелевантное (например для ремонта это 'ремонт авто', 'ремонт техники')"],
  "what_we_sell": "что клиент продаёт (1 фраза)"
}}

ВАЖНО:
- Keywords должны покрывать синонимы и формы слов. Например для «ремонт квартир»: ["ремонт", "отделк", "ремонтн", "под ключ", "квартир", "отделочн", "косметическ ремонт"].
- Если ниша широкая (e.g. "услуги"), keywords должны помочь сузить.
- Если упомянут город, обязательно добавь его в keywords тоже.
- Если в тексте есть очевидная опечатка (директорог→директолог, дирктолог→директолог, сматарт→стартап) — исправь и используй каноническую форму.
- Если ты НЕ ПОНИМАЕШЬ нишу (бессмысленный/слишком общий текст без контекста типа "лиды", "продажа", "клиенты", или абракадабра) — верни keywords: [] и niche_canonical: "не понял".
- Если в тексте есть очевидная опечатка (директорог→директолог, дирктолог→директолог, сматарт→стартап) — исправь и используй каноническую форму.
- Если ты НЕ ПОНИМАЕШЬ нишу (бессмысленный/слишком общий текст без контекста типа "лиды", "продажа", "клиенты", или абракадабра) — верни keywords: [] и niche_canonical: "не понял".
"""
    res = await ai_client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    data = json.loads(res.choices[0].message.content)
    # Нормализация
    data.setdefault("keywords", [])
    data.setdefault("stop_keywords", [])
    data["keywords"] = [k.lower().strip() for k in data["keywords"] if k]
    data["stop_keywords"] = [k.lower().strip() for k in data["stop_keywords"] if k]
    return data


# ==========================================
# 🔎 БЫСТРЫЙ SQL-СЧЁТЧИК (для pre-check)
# ==========================================
def _fts_query_from_keywords(keywords: list[str]) -> str:
    """Строит FTS5 MATCH-запрос: (kw1 OR kw2 OR ...) с поддержкой префиксов.
    Сложные фразы заворачиваем в кавычки."""
    parts = []
    for kw in keywords:
        kw = re.sub(r'["()*]', '', kw).strip()
        if not kw:
            continue
        if " " in kw:
            parts.append(f'"{kw}"')
        else:
            # префиксный поиск: ремонт* поймает «ремонтник», «ремонтный»
            parts.append(f'{kw}*')
    return " OR ".join(parts) if parts else '""'


async def count_potential_leads(keywords: list[str], days: int = LOOKBACK_DAYS) -> int:
    """Быстрый SQL-COUNT по корпусу за N дней. Без AI, без квалификации.
    Используется ДО оплаты, чтобы клиент видел: «в твоей нише найдено N сообщений»."""
    if not keywords:
        return 0
    fts_q = _fts_query_from_keywords(keywords)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(APEX_DB) as db:
        try:
            async with db.execute(
                """SELECT COUNT(*)
                   FROM messages_corpus mc
                   JOIN messages_fts fts ON mc.id = fts.rowid
                   WHERE messages_fts MATCH ?
                     AND mc.msg_date >= ?""",
                (fts_q, cutoff),
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row else 0
        except Exception as e:
            logger.error(f"FTS COUNT error: {e}, query={fts_q!r}")
            return 0


# ==========================================
# 🧠 AI-КВАЛИФИКАТОР НА ЗАПРОСЕ КЛИЕНТА (а не парсера!)
# ==========================================
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=3, max=30),
    retry=retry_if_not_exception_type(RuntimeError),
)
async def qualify_message_for_niche(text: str, niche_info: dict) -> dict:
    """Один AI-вызов: насколько это сообщение подходит под нишу клиента.
    Возвращает {status, score, pain, fit_service}."""
    prompt = f"""Ты — Lead Qualifier. Клиент продаёт услуги, и я нашёл сообщение в чате,
которое возможно — горячий лид для него.

НИША КЛИЕНТА:
- Что продаёт: {niche_info.get('what_we_sell', niche_info.get('niche_canonical'))}
- Гео: {niche_info.get('geo') or 'любой'}
- Тип услуги: {niche_info.get('service_type', '—')}

СООБЩЕНИЕ ИЗ ЧАТА:
"{text[:1500]}"

ОЦЕНИ score 0-100:
[90-100] ГОРЯЧИЙ-ЯВНЫЙ: автор от ПЕРВОГО лица прямо ищет такую услугу как у клиента, готов к контакту.
[75-89]  ГОРЯЧИЙ-НЕЯВНЫЙ: автор от ПЕРВОГО лица описывает СВОЮ боль/задачу которая решается услугой клиента, явно lpr (владелец/руководитель).
[50-74]  ТЁПЛЫЙ: интерес есть, но боль размыта или это не основная услуга клиента.
[0-49]   НЕЦЕЛЕВОЙ: не подходит.

═══ КЛЮЧЕВОЙ ПРИНЦИП ═══

ЛИД = ЗАКАЗЧИК ИЩЕТ ИСПОЛНИТЕЛЯ ОТ ПЕРВОГО ЛИЦА.
Должны быть фразы: "я ищу", "нам нужен", "мы ищем", "хочу заказать", "посоветуйте подрядчика", "кто может сделать".

═══ ЖЁСТКО ОТКЛОНЯЙ (score 0-30) ═══

❌ ВАКАНСИЯ / ОБЪЯВЛЕНИЕ О НАЙМЕ от лица сторонней компании или команды:
   "Застройщик X ищет директора по продажам"
   "ООО Туч ищет руководителя"
   "Компания N ищет сотрудника в штат"
   "Наша команда расширяется, ищем опытного маркетолога"
   "Команда расширение... ищем" (любые формы "команда + ищем/расширяемся")
   "Присылайте резюме / портфолио и прайс"
   "Зарплата от X / Вилка / Соцпакет / 5/2"
   → Это объявление о найме, НЕ запрос подрядчика. score = 0-20.

❌ ВАКАНСИЯ НА УЗБЕКСКОМ / ТАДЖИКСКОМ / СМЕШАННОМ ЯЗЫКЕ:
   Любой текст с узбекскими словами найма:
   "кидирамиз" / "кидирвомиз" / "кидирилади" (= узб. «ищем» / «требуется»)
   "командага" / "командамиз" (= узб. «в команду» / «наша команда»)
   "талаб килин" / "иш буш" (= узб. «требуется» / «вакансия»)
   "Ассаламу алейкум, команда расширение..." — это всегда найм
   → даже если слово «таргетолог» есть — это компания ищет таргетолога В ШТАТ.
   → score = 0.

❌ Текст НЕ на русском или с русскоязычными вкраплениями (узбекский, таджикский,
   киргизский, арабский, английский HR-пост) — score = 0 если язык не русский.

❌ АНАЛИТИКА / ОБЗОР РЫНКА (экспертный пост):
   Признаки: "О чём это говорит?", "Это сигнал", "Тенденция", "Функция X становится...",
             "В 2026 будет...", "Показательная ситуация", "Рынок переходит к...",
             "Это означает что...", "Делаем выводы".
   → Это разбор/мнение, НЕ запрос услуги.

❌ НОВОСТЬ / ПРЕСС-РЕЛИЗ:
   Признаки: третье лицо, имена компаний без личного "я",
             объявления о событиях, "сегодня X сделал Y".

❌ ОБРАЗОВАТЕЛЬНЫЙ ПОСТ:
   "3 ошибки", "5 советов", "Как настроить...", "Чек-лист", "Гайд",
   "Что делать если...".

❌ АВТОР САМ ПРОДАЁТ УСЛУГУ (а не покупает):
   "делаю", "оказываю", "беру проекты", "пишите в ЛС за услугами",
   "веду", "настраиваю".

❌ РЕЗЮМЕ / ПОИСК РАБОТЫ:
   "ищу проект", "буду рад заказам", "опыт N лет", "моё портфолио".

❌ ИНФОЦЫГАНСКИЕ КУРСЫ:
   "обучу за 30 дней", "наставничество", "мастер-группа", "приходи на марафон".

❌ КАНАЛ-АГРЕГАТОР / БИРЖА (контакт скрыт за PRO-подпиской):
   Признаки в тексте: "Доступно в Pro подписке", "PRO-участники получили",
                      "Хочешь быть в числе первых", "Контакт автора скрыт",
                      "оформи подписку чтобы".
   → Это перепродажа лидов, не реальный заказчик. ОТКЛОНЯЙ.

❌ Бот / реклама / новостной канал.
❌ Гео не совпадает (если клиент локально).

Верни строго JSON:
{{
  "status": "APPROVE" | "REJECT",
  "score": 0-100,
  "pain": "Краткое описание запроса/боли автора (1 предложение)",
  "fit_service": "Какая конкретно услуга клиента закрывает эту боль",
  "reject_reason": "Только если REJECT — почему"
}}

═══ КРИТИЧЕСКИ ВАЖНО ═══

Тема сообщения ОБЯЗАНА совпадать с нишей клиента.
Если клиент = директолог, а сообщение про пошив одежды / стройку / юристов — score 0.
НЕ ВАЖНО что в сообщении есть слово из гео клиента (Москва и т.п.).
Совпадение по гео БЕЗ совпадения по теме = score 0.

Правило: APPROVE только если score >= 85 И тема сообщения = тема ниши клиента.
"""
    res = await ai_client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    return json.loads(res.choices[0].message.content)


# ==========================================
# 🎯 ГЛАВНЫЙ ПОИСК
# ==========================================
async def _fetch_fts_candidates(keywords: list[str], stop_keywords: list[str],
                                days: int, exclude_claimed_for_user: Optional[int],
                                limit: int) -> list[dict]:
    fts_q = _fts_query_from_keywords(keywords)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    sql = """
        SELECT mc.id, mc.text, mc.link, mc.chat_title, mc.chat_key,
               mc.sender_username, mc.sender_id, mc.msg_date
        FROM messages_corpus mc
        JOIN messages_fts fts ON mc.id = fts.rowid
        WHERE messages_fts MATCH ?
          AND mc.msg_date >= ?
          AND mc.id NOT IN (SELECT corpus_id FROM claimed_leads)
        ORDER BY mc.msg_date DESC
        LIMIT ?
    """
    rows: list[dict] = []
    seen_hashes: set[str] = set()
    skipped = {"blacklist": 0, "stopwords": 0, "too_short": 0,
               "non_cyrillic": 0, "hiring_post": 0, "no_intent_marker": 0,
               "dup_text": 0}
    async with aiosqlite.connect(APEX_DB) as db:
        db.row_factory = aiosqlite.Row
        try:
            # Берём в 3× больше чем нужно — после фильтров останется примерно limit.
            async with db.execute(sql, (fts_q, cutoff, limit * 3)) as cur:
                async for row in cur:
                    chat_key = row["chat_key"] or ""
                    if should_skip_chat(chat_key):
                        skipped["blacklist"] += 1
                        continue
                    text = row["text"] or ""
                    text_lower = text.lower()
                    if any(sk in text_lower for sk in stop_keywords + GLOBAL_STOP_PHRASES):
                        skipped["stopwords"] += 1
                        continue
                    # Дедуп по нормализованному тексту (репосты в разных чатах = 1 лид)
                    text_hash = _normalize_for_dedup(text)
                    if text_hash and text_hash in seen_hashes:
                        skipped["dup_text"] += 1
                        continue
                    # Pre-AI фильтр (язык, hiring, intent)
                    passed, reason = _passes_pre_ai_filter(text)
                    if not passed:
                        skipped[reason] = skipped.get(reason, 0) + 1
                        continue
                    seen_hashes.add(text_hash)
                    rows.append(dict(row))
                    if len(rows) >= limit:
                        break
        except Exception as e:
            logger.error(f"FTS SEARCH error: {e}, query={fts_q!r}")
    if any(v > 0 for v in skipped.values()):
        logger.info(f"pre-AI filter: {skipped}; kept={len(rows)}")
    return rows


async def find_leads(user_id: int, niche_text: str, n: int = DEFAULT_LIMIT,
                     niche_info: Optional[dict] = None) -> dict:
    """
    Главная функция для бота. Возвращает структуру:
      {
        "niche_info": {...},
        "stats": {"corpus_match": N, "qualified": M},
        "leads": [
            {"corpus_id": 123, "text": "...", "link": "...", "score": 87,
             "pain": "...", "fit_service": "...", "chat_title": "...",
             "sender_username": "@user", "msg_date": "..."}, ...
        ]
      }
    """
    if niche_info is None:
        niche_info = await analyze_niche(niche_text)

    candidates = await _fetch_fts_candidates(
        niche_info["keywords"], niche_info.get("stop_keywords", []),
        days=LOOKBACK_DAYS, exclude_claimed_for_user=user_id, limit=FTS_CANDIDATES,
    )

    # Параллельный AI-проход (по 5 за раз чтобы не перегрузить DeepSeek)
    qualified: list[dict] = []
    sem = asyncio.Semaphore(5)

    async def qualify_one(cand: dict) -> Optional[dict]:
        async with sem:
            try:
                q = await qualify_message_for_niche(cand["text"], niche_info)
            except Exception as e:
                logger.error(f"qualify_message_for_niche failed: {e}")
                return None
        if (q.get("status") == "APPROVE"
                and q.get("score", 0) >= QUALIFIER_THRESHOLD
                and q.get("fit_service", "").strip()):
            return {
                "corpus_id": cand["id"],
                "text": cand["text"],
                "link": cand["link"],
                "chat_title": cand["chat_title"],
                "chat_key": cand["chat_key"],
                "sender_username": cand["sender_username"],
                "sender_id": cand["sender_id"],
                "msg_date": cand["msg_date"],
                "score": q.get("score"),
                "pain": q.get("pain"),
                "fit_service": q.get("fit_service"),
            }
        return None

    results = await asyncio.gather(*[qualify_one(c) for c in candidates])
    qualified = [r for r in results if r is not None]
    qualified.sort(key=lambda x: (x["score"], x["msg_date"]), reverse=True)

    return {
        "niche_info": niche_info,
        "stats": {
            "corpus_match": len(candidates),
            "qualified": len(qualified),
            "lookback_days": LOOKBACK_DAYS,
        },
        "leads": qualified[:n],
    }


# ==========================================
# 🔒 CLAIM-ЛОГИКА (эксклюзивность лида)
# ==========================================
async def claim_lead(corpus_id: int, user_id: int) -> bool:
    """Атомарно помечает лид как купленный пользователем.
    Возвращает True если успех, False если уже куплен другим."""
    async with aiosqlite.connect(APEX_DB) as db:
        try:
            await db.execute(
                "INSERT INTO claimed_leads (corpus_id, user_id) VALUES (?, ?)",
                (corpus_id, user_id),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def get_claimed_lead(corpus_id: int) -> Optional[dict]:
    """Возвращает claim-инфу или None если не куплен."""
    async with aiosqlite.connect(APEX_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM claimed_leads WHERE corpus_id = ?", (corpus_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ==========================================
# 💳 ПОДПИСКИ
# ==========================================
async def set_subscription(user_id: int, niche_text: str, niche_info: dict,
                            duration_days: int = 30) -> dict:
    """Сохраняет/продлевает подписку клиента."""
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
        async with db.execute(
            "SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            sub = dict(row)
            sub["niche_info"] = json.loads(sub["niche_keywords"]) if sub["niche_keywords"] else {}
            # Проверка валидности
            if sub.get("expires_at"):
                expires = datetime.fromisoformat(sub["expires_at"])
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                sub["is_active"] = (sub["status"] == "active"
                                    and expires > datetime.now(timezone.utc))
            else:
                sub["is_active"] = False
            return sub


async def cancel_subscription(user_id: int) -> bool:
    async with aiosqlite.connect(APEX_DB) as db:
        cur = await db.execute(
            "UPDATE subscriptions SET status='cancelled' WHERE user_id = ?", (user_id,)
        )
        await db.commit()
        return cur.rowcount > 0


# ==========================================
# 📨 ДЛЯ CRON: свежие лиды для подписчика
# ==========================================
async def find_fresh_leads_for_subscriber(user_id: int, hours: int = 24,
                                           limit: int = 5) -> list[dict]:
    """Возвращает свежие (за N часов) лиды для активного подписчика."""
    sub = await get_subscription(user_id)
    if not sub or not sub.get("is_active"):
        return []
    niche_info = sub.get("niche_info", {})
    if not niche_info or not niche_info.get("keywords"):
        return []

    fts_q = _fts_query_from_keywords(niche_info["keywords"])
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    sql = """
        SELECT mc.id, mc.text, mc.link, mc.chat_title, mc.chat_key,
               mc.sender_username, mc.msg_date
        FROM messages_corpus mc
        JOIN messages_fts fts ON mc.id = fts.rowid
        WHERE messages_fts MATCH ?
          AND mc.indexed_at >= ?
          AND mc.id NOT IN (SELECT corpus_id FROM claimed_leads)
        ORDER BY mc.msg_date DESC
        LIMIT ?
    """
    candidates: list[dict] = []
    seen_hashes: set[str] = set()
    async with aiosqlite.connect(APEX_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, (fts_q, cutoff, limit * 10)) as cur:
            async for row in cur:
                chat_key = row["chat_key"] or ""
                if should_skip_chat(chat_key):
                    continue
                text = row["text"] or ""
                text_lower = text.lower()
                if any(sk in text_lower for sk in niche_info.get("stop_keywords", []) + GLOBAL_STOP_PHRASES):
                    continue
                # Дедуп репостов
                text_hash = _normalize_for_dedup(text)
                if text_hash and text_hash in seen_hashes:
                    continue
                # Pre-AI фильтр (язык/найм/intent)
                passed, _ = _passes_pre_ai_filter(text)
                if not passed:
                    continue
                seen_hashes.add(text_hash)
                candidates.append(dict(row))
                if len(candidates) >= limit * 5:
                    break

    sem = asyncio.Semaphore(5)

    async def qualify_one(cand: dict) -> Optional[dict]:
        async with sem:
            try:
                q = await qualify_message_for_niche(cand["text"], niche_info)
            except Exception:
                return None
        if (q.get("status") == "APPROVE"
                and q.get("score", 0) >= QUALIFIER_THRESHOLD
                and q.get("fit_service", "").strip()):
            return {
                "corpus_id": cand["id"], "text": cand["text"], "link": cand["link"],
                "chat_title": cand["chat_title"], "chat_key": cand["chat_key"],
                "sender_username": cand["sender_username"],
                "msg_date": cand["msg_date"],
                "score": q.get("score"), "pain": q.get("pain"),
                "fit_service": q.get("fit_service"),
            }
        return None

    results = await asyncio.gather(*[qualify_one(c) for c in candidates])
    qualified = [r for r in results if r is not None]
    qualified.sort(key=lambda x: x["score"], reverse=True)
    return qualified[:limit]


# ==========================================
# 🧪 САМОТЕСТ (если запустить файл напрямую)
# ==========================================
async def _selftest():
    print("Selftest: anal niche")
    info = await analyze_niche("у меня стройка в Москве, ремонт квартир под ключ")
    print(json.dumps(info, ensure_ascii=False, indent=2))

    cnt = await count_potential_leads(info["keywords"], days=30)
    print(f"Candidates in last 30 days: {cnt}")

    if cnt > 0:
        out = await find_leads(user_id=999_999, niche_text="ремонт квартир в Москве",
                                n=5, niche_info=info)
        print(f"Qualified leads: {out['stats']['qualified']}")
        for lead in out["leads"]:
            print(f"  [{lead['score']}] {lead['text'][:120]}...")
            print(f"      → {lead['fit_service']} | {lead['link']}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_selftest())
