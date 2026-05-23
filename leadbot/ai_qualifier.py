"""
ai_qualifier.py v2 — поддержка двух режимов:

1. mode='lead-finder' — как было: ищет горячих заказчиков услуг для платящего
   клиента. AI ставит score, pain, fit_service. score>=80 → pending.

2. mode='dm-outreach' (НОВЫЙ) — ищет фрилансеров и агентства которым ты можешь
   продать парсер. AI ставит score И генерирует персональный DM-текст.
   score>=80 → pending с готовым DM в ai_dm_text.

Промпт переключается автоматически по полю mode в paid_clients.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import aiosqlite
import httpx
from openai import AsyncOpenAI

# ==========================================
# ⚙️ НАСТРОЙКИ
# ==========================================
APEX_DB = os.environ.get("APEX_DB") or str(
    Path(__file__).resolve().parent / "apex_ai.db"
)
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY") or "sk-d75f7d76a50c49648aaf061611ce62b5"
BOT_TOKEN = os.environ.get("BOT_TOKEN") or "8565672652:AAGpwT7Lg50bSL-SDBgwG15ci0BcSydNAU4"
ADMIN_ID = int(os.environ.get("APEX_ADMIN_ID") or "7531405698")

QUALIFIER_INTERVAL = 60
BATCH_SIZE = 5
MAX_BATCHES_PER_TICK = 4
MIN_SCORE_TO_PROMOTE = 80
DAILY_FAIL_THRESHOLD = 10
LOW_BALANCE_NOTIFY_INTERVAL = 6 * 3600

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ai_qualifier")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

ai_client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com/v1",
    max_retries=0,
    timeout=60.0,
)


# ============================================
#         ВЫБОРКА КАНДИДАТОВ
# ============================================
async def fetch_active_clients() -> list[dict]:
    async with aiosqlite.connect(APEX_DB) as db:
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute(
                """
                SELECT user_id, client_name, niche_text, niche_keywords,
                       COALESCE(mode, 'lead-finder') AS mode
                FROM paid_clients
                WHERE status = 'active'
                  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                """
            ) as cur:
                rows = await cur.fetchall()
        except Exception:
            async with db.execute(
                """
                SELECT user_id, client_name, niche_text, niche_keywords
                FROM paid_clients
                WHERE status = 'active'
                  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                """
            ) as cur:
                rows = await cur.fetchall()
            return [dict(r, **{"mode": "lead-finder"}) for r in rows]
    return [dict(r) for r in rows]


async def fetch_ai_pending_for_client(user_id: int, limit: int) -> list[dict]:
    async with aiosqlite.connect(APEX_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT pr.id AS pending_id, pr.corpus_id,
                   mc.chat_key, mc.chat_title, mc.text, mc.msg_date,
                   mc.sender_username
            FROM pending_review pr
            JOIN messages_corpus mc ON mc.id = pr.corpus_id
            WHERE pr.client_user_id = ?
              AND pr.status = 'ai_pending'
            ORDER BY pr.id ASC
            LIMIT ?
            """,
            (user_id, limit),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ============================================
#               ПРОМПТЫ
# ============================================
def parse_keywords(kw_field: str) -> list[str]:
    if not kw_field:
        return []
    raw = kw_field.strip()
    if raw.startswith("["):
        try:
            return [str(x).strip() for x in json.loads(raw) if str(x).strip()]
        except Exception:
            pass
    return [p.strip() for p in raw.split(",") if p.strip()]


# ── Промпт #1: lead-finder ─────────────────
LEAD_FINDER_SYSTEM = """Ты — Lead Qualifier для B2B-лидген сервиса.

Задача: оценить является ли каждое сообщение из Telegram-чата ЗАПРОСОМ ОТ ЗАКАЗЧИКА,
готового платить за услугу прямо сейчас.

КЛИЕНТ: {client_name}
НИША: {niche_text}
УСЛУГИ КЛИЕНТА: {keywords}

═══ КРИТЕРИИ (score 0-100) ═══

✅ ГОРЯЧИЙ ЛИД (85-100): прямой запрос подрядчика + конкретная задача в нише + бюджет/сроки
✅ ТЁПЛЫЙ ЛИД (80-84): запрос подрядчика без полной конкретики, явный интент купить
❌ НЕ ЛИД (0-79):
  - Соискатели работы / резюме / "ищу проект как фрилансер"
  - Продавцы услуг / "пишите в ЛС" / "бесплатный аудит"
  - Вакансии / "ищем в штат"
  - Просьбы советов от исполнителей
  - Обсуждения, новости, флуд, инфоцыгане
  - Не наша ниша

ВАЖНО: fit_service ОБЯЗАТЕЛЬНО заполняется когда score >= 80. Это конкретная услуга
из списка УСЛУГ. Если не можешь подобрать — снижай score.

ФОРМАТ:
{{
  "items": [
    {{
      "idx": 0,
      "score": 85,
      "is_lead": true,
      "pain": "Ищет директолога для интернет-магазина в Москве",
      "fit_service": "настройка директа",
      "reason": "прямой запрос + ниша + локация"
    }}
  ]
}}
"""


# ── Промпт #2: dm-outreach ──────────────────
DM_OUTREACH_SYSTEM = """Ты — DM Outreach Qualifier. Задача — найти ФРИЛАНСЕРОВ или
сотрудников АГЕНТСТВ в TG-чатах, которым можно отправить персональный DM с предложением
AI-парсера для лидов.

═══ КОНТЕКСТ ═══

КТО ОТПРАВИТЕЛЬ DM:
Никита (@nikita_Apex), 20 лет. Сделал AI-парсер: читает Telegram-чаты 24/7 и ловит людей
которые ИЩУТ услуги (директологов, SEO-специалистов, веб-разработчиков, дизайнеров,
маркетологов). Через AI-фильтр выдаёт горячих лидов клиенту в личку.

ОФФЕР:
- Бесплатно покажу одного живого лида по нише собеседника (через бот, 30 секунд)
- Дальше — подписка 15 000 ₽/мес, 5-15 свежих лидов/неделю в личку
- Гарантия: меньше 5 лидов за месяц → следующий бесплатно

═══ КОГО ИЩЕМ (score 80-100) ═══

✅ ГОДЕН: фрилансер/представитель агентства/специалист по тематике совпадающей с тем
что ловит парсер (директ, SEO, веб, SMM, дизайн, разработка). Признаки:
  - Предлагает свои услуги ("делаю сайты", "настраиваю директ", "веду SEO")
  - Описывает кейсы / опыт
  - Активный в чате
  - Имеет username (можно написать в DM)

❌ НЕ ГОДЕН (0-79):
  - Соискатель работы (ищет работодателя как сотрудника)
  - Корпоративный спикер / представитель крупного бренда
  - Инфоцыган (продаёт курсы, наставничество, "обучу за 30 дней")
  - Конкурент (другой лидген-сервис, парсер чатов)
  - Школьник / новичок без опыта
  - Тематика не совпадает с парсером

═══ ВАЖНО ═══

Когда score >= 80, ОБЯЗАТЕЛЬНО генерируй персональный DM-текст в поле dm_message.

ПРАВИЛА DM-ТЕКСТА:
1. На "ты". Тон — коллега коллеге, не продавец.
2. Длина 350-500 знаков.
3. Открой упоминанием КОНКРЕТНОГО контекста (видел тебя в @чат, читал твоё про X)
   — без копипасты, чтобы человек видел что не массовая рассылка.
4. Объясни что ты делаешь в одно простое предложение.
5. Заканчивай предложением показать ОДНОГО лида бесплатно через бот.
6. Без гонева, без "уникальное предложение", без "только сегодня".
7. НИКОГДА не упоминай конкретное число чатов в базе.

═══ ФОРМАТ ОТВЕТА (СТРОГО JSON) ═══

Возврати JSON-объект следующего вида:
{{
  "items": [
    {{
      "idx": 0,
      "score": 87,
      "is_lead": true,
      "summary": "Веб-разработчик, делает сайты на Next.js, активен в маркетинг-чатах",
      "fit_service": "разработка / веб",
      "reason": "опытный фрилансер по теме что ловит парсер",
      "dm_message": "Привет! Видел тебя в @marketing_chat — пишешь про Next.js и кейсы. Я делаю AI-парсер для Telegram: ловит в чатах людей которые ищут разработчиков. Хочешь покажу одного живого по твоему профилю бесплатно? Через бот, 30 секунд. Решишь сам надо или нет."
    }}
  ]
}}

idx — индекс сообщения в массиве (0-based, ровно как пришло).
"""


def build_user_prompt(items: list[dict]) -> str:
    lines = [f"Оцени каждое из {len(items)} сообщений ниже:\n"]
    for i, it in enumerate(items):
        chat = it.get("chat_title") or it.get("chat_key") or "—"
        text = (it.get("text") or "")[:1500]
        date = it.get("msg_date") or "—"
        sender = it.get("sender_username") or "anon"
        lines.append(
            f"\n[{i}] Чат: {chat} | Автор: @{sender} | Дата: {date}\n"
            f"    Текст: {text}"
        )
    return "\n".join(lines)


# ============================================
#              ВЫЗОВ DEEPSEEK
# ============================================
async def call_deepseek(client_meta: dict, items: list[dict]) -> list[dict]:
    keywords = parse_keywords(client_meta["niche_keywords"])
    mode = client_meta.get("mode") or "lead-finder"

    if mode == "dm-outreach":
        sys_prompt = DM_OUTREACH_SYSTEM
    else:
        sys_prompt = LEAD_FINDER_SYSTEM.format(
            client_name=client_meta["client_name"] or "клиент",
            niche_text=client_meta["niche_text"] or "—",
            keywords=", ".join(keywords[:30]) or "—",
        )

    user_prompt = build_user_prompt(items)

    res = await ai_client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.2 if mode == "dm-outreach" else 0.0,
    )
    content = res.choices[0].message.content
    data = json.loads(content)
    return data.get("items", [])


# ============================================
#         АПДЕЙТ pending_review
# ============================================
async def apply_verdicts(
    items: list[dict], verdicts: list[dict], mode: str
) -> tuple[int, int]:
    by_idx = {v["idx"]: v for v in verdicts if isinstance(v.get("idx"), int)}
    promoted = 0
    filtered = 0
    now = datetime.utcnow().isoformat()

    async with aiosqlite.connect(APEX_DB) as db:
        for i, it in enumerate(items):
            v = by_idx.get(i)
            if not v:
                continue
            score = int(v.get("score", 0))
            is_lead = bool(v.get("is_lead", False))
            pain = (v.get("pain") or v.get("summary") or "")[:300]
            fit = (v.get("fit_service") or v.get("fit") or "")[:80]
            reason = (v.get("reason") or "")[:300]
            dm_text = (v.get("dm_message") or "")[:1500] if mode == "dm-outreach" else None
            dm_username = it.get("sender_username") if mode == "dm-outreach" else None

            if score >= MIN_SCORE_TO_PROMOTE and is_lead:
                if mode == "dm-outreach" and not dm_text:
                    new_status = "ai_filtered"
                    filtered += 1
                else:
                    new_status = "pending"
                    promoted += 1
            else:
                new_status = "ai_filtered"
                filtered += 1

            try:
                await db.execute(
                    """
                    UPDATE pending_review
                    SET status = ?,
                        ai_score = ?,
                        ai_pain = ?,
                        ai_fit_service = ?,
                        ai_reason = ?,
                        ai_processed_at = ?,
                        ai_dm_text = ?,
                        ai_dm_username = ?,
                        ai_dm_fit = ?
                    WHERE id = ?
                    """,
                    (new_status, score, pain, fit, reason, now,
                     dm_text, dm_username, fit if mode == "dm-outreach" else None,
                     it["pending_id"]),
                )
            except aiosqlite.OperationalError:
                await db.execute(
                    """
                    UPDATE pending_review
                    SET status = ?,
                        ai_score = ?,
                        ai_pain = ?,
                        ai_fit_service = ?,
                        ai_reason = ?,
                        ai_processed_at = ?
                    WHERE id = ?
                    """,
                    (new_status, score, pain, fit, reason, now, it["pending_id"]),
                )
        await db.commit()
    return (promoted, filtered)


# ============================================
#           NOTIFY
# ============================================
async def notify_admin(text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10.0) as cli:
        try:
            await cli.post(
                url,
                json={"chat_id": ADMIN_ID, "text": text, "parse_mode": "HTML"},
            )
        except Exception as e:
            logger.warning(f"notify_admin failed: {e}")


# ============================================
#                 MAIN LOOP
# ============================================
async def main_loop() -> None:
    logger.info(
        "🤖 ai_qualifier v2 запущен. interval=%ds, batch=%d, threshold=%d",
        QUALIFIER_INTERVAL,
        BATCH_SIZE,
        MIN_SCORE_TO_PROMOTE,
    )
    consecutive_errors = 0
    last_low_balance_alert = 0.0
    paused_until = 0.0
    last_summary_notify = datetime.utcnow().timestamp()
    summary_promoted = 0
    summary_filtered = 0

    while True:
        try:
            now_ts = datetime.utcnow().timestamp()
            if paused_until > now_ts:
                await asyncio.sleep(min(60, paused_until - now_ts))
                continue

            clients = await fetch_active_clients()
            if not clients:
                await asyncio.sleep(QUALIFIER_INTERVAL)
                continue

            tick_promoted = 0
            tick_filtered = 0

            for client in clients:
                mode = client.get("mode") or "lead-finder"
                for batch_idx in range(MAX_BATCHES_PER_TICK):
                    items = await fetch_ai_pending_for_client(
                        client["user_id"], BATCH_SIZE
                    )
                    if not items:
                        break
                    try:
                        verdicts = await call_deepseek(client, items)
                        promoted, filtered = await apply_verdicts(items, verdicts, mode)
                        tick_promoted += promoted
                        tick_filtered += filtered
                        consecutive_errors = 0
                        logger.info(
                            f"  '{client['client_name']}' [{mode}] batch[{batch_idx}]: "
                            f"+{promoted} pending, +{filtered} filtered"
                        )
                    except Exception as e:
                        consecutive_errors += 1
                        err_str = str(e)
                        logger.error(
                            f"DeepSeek error [{client['client_name']}] "
                            f"(подряд={consecutive_errors}): {err_str}"
                        )
                        is_balance = (
                            "402" in err_str
                            or "insufficient" in err_str.lower()
                            or "balance" in err_str.lower()
                        )
                        if is_balance:
                            if now_ts - last_low_balance_alert > LOW_BALANCE_NOTIFY_INTERVAL:
                                await notify_admin(
                                    "🚨 <b>DeepSeek-баланс закончился</b>\n"
                                    "AI-qualifier на паузе.\n"
                                    f"<code>{err_str[:200]}</code>"
                                )
                                last_low_balance_alert = now_ts
                            paused_until = now_ts + 30 * 60
                            break
                        if consecutive_errors >= DAILY_FAIL_THRESHOLD:
                            await notify_admin(
                                f"🚨 <b>ai_qualifier:</b> {consecutive_errors} ошибок подряд, пауза 1ч.\n"
                                f"<code>{err_str[:200]}</code>"
                            )
                            paused_until = now_ts + 60 * 60
                            consecutive_errors = 0
                            break

            if tick_promoted or tick_filtered:
                summary_promoted += tick_promoted
                summary_filtered += tick_filtered
                logger.info(
                    f"🧮 Тик AI: +{tick_promoted} pending, +{tick_filtered} filtered"
                )

                if (
                    summary_promoted > 0
                    and (now_ts - last_summary_notify) > 30 * 60
                ):
                    await notify_admin(
                        f"🤖 <b>ai_qualifier:</b>\n"
                        f"+{summary_promoted} новых лидов в /review\n"
                        f"⏭ {summary_filtered} отсеяно"
                    )
                    last_summary_notify = now_ts
                    summary_promoted = 0
                    summary_filtered = 0

        except Exception as e:
            logger.exception(f"Loop crash: {e}")

        await asyncio.sleep(QUALIFIER_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("👋 Остановка")
