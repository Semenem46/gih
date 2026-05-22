"""
ai_qualifier.py — AI-фильтр между live_push и /review.

Что делает:
1. Каждые QUALIFIER_INTERVAL секунд берёт пачку pending_review
   со status='ai_pending' (не больше BATCH_SIZE на клиента за тик).
2. Прогоняет батч через DeepSeek с per-client промптом (с нишей).
3. По результату:
   - score >= MIN_SCORE_TO_PROMOTE → status='pending', попадает в /review
   - иначе → status='ai_filtered', не виден
4. Сохраняет ai_score, ai_pain, ai_fit_service, ai_reason, ai_processed_at.

Ключи DeepSeek хардкодим (как в parser_apex_ai.py) или берём из ENV.
Auto-stop при ошибках API > N подряд.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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

QUALIFIER_INTERVAL = 60          # сек между прогонами
BATCH_SIZE = 5                   # сколько кандидатов в одном LLM-вызове
MAX_BATCHES_PER_TICK = 4         # макс вызовов LLM на одного клиента за тик (защита от бюджета)
MIN_SCORE_TO_PROMOTE = 80        # порог: score>=80 → pending; <80 → ai_filtered
DAILY_FAIL_THRESHOLD = 10        # после N подряд ошибок API — auto-pause
LOW_BALANCE_NOTIFY_INTERVAL = 6 * 3600  # 6 часов между алёртами «закончился баланс»

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


# ==========================================
# 📂 ВЫБОРКА КАНДИДАТОВ
# ==========================================
async def fetch_active_clients() -> list[dict]:
    async with aiosqlite.connect(APEX_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT user_id, client_name, niche_text, niche_keywords
            FROM paid_clients
            WHERE status = 'active'
              AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
            """
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def fetch_ai_pending_for_client(user_id: int, limit: int) -> list[dict]:
    """Берём самых старых ai_pending для конкретного клиента."""
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


# ==========================================
# 🧠 ПРОМПТ
# ==========================================
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


SYSTEM_PROMPT_TMPL = """Ты — Lead Qualifier для B2B-лидген сервиса.

Твоя единственная задача: оценить, является ли каждое сообщение из Telegram-чата
ЗАПРОСОМ ОТ ЗАКАЗЧИКА, готового платить за услугу прямо сейчас.

КЛИЕНТ: {client_name}
НИША: {niche_text}
УСЛУГИ КЛИЕНТА (ключи): {keywords}

═══════════════ КРИТЕРИИ ОЦЕНКИ (score 0-100) ═══════════════

✅ ГОРЯЧИЙ ЛИД (score 85-100):
  - Прямой запрос подрядчика: "ищу спеца", "нужен директолог", "кому могу заказать"
  - Конкретная задача в нише клиента + бюджет / сроки / детали
  - Пост от собственника бизнеса / маркетолога / тендерщика заказчика

✅ ТЁПЛЫЙ ЛИД (score 80-84):
  - Запрос подрядчика без полной конкретики, но с ясной нишей и явным интентом купить
  - Просьба «посоветуйте» если контекст явно от заказчика, а не от новичка-фрилансера

❌ НЕ ЛИД (score 0-79):
  - Соискатели работы: «ищу работу», «возьму проект», «моё резюме», «буду рад заказам»
  - Продавцы услуг: «делаю сайты», «настраиваю директ», «пишите в ЛС», «бесплатный аудит»
  - Вакансии: «ищем в штат», «ищем в команду», «компании нужен сотрудник»
  - Просьбы советов от исполнителей: «как лучше настроить», «какой плагин использовать»
  - Обсуждения, новости, флуд, флейм
  - Инфоцыгане: «курс», «наставник», «мастер-группа», «обучу за 30 дней»
  - Не наша ниша вообще (даже если есть слово-триггер)
  - Слишком короткие/общие сообщения без деталей

═══════════════ ВАЖНО ═══════════════

1. Если в сообщении есть ИМЕННО триггер-слово, но контекст НЕ от заказчика — score < 80.
   Пример: «продам базу директологов» → score 5, не лид.

2. Если ниша клиента НЕ совпадает с темой запроса — score < 80,
   даже если запрос «горячий». Пример: клиент = SEO, сообщение про разработку Flutter-приложения → score 30.

3. Поле fit_service ОБЯЗАТЕЛЬНО заполняется когда score >= 80.
   Это конкретная услуга клиента из списка УСЛУГИ. Если не можешь подобрать — снижай score.

4. Поле pain — суть боли заказчика одной строкой (что ему нужно), русским языком.

═══════════════ ФОРМАТ ОТВЕТА ═══════════════

Возврати СТРОГО JSON:
{{
  "items": [
    {{
      "idx": 0,
      "score": 85,
      "is_lead": true,
      "pain": "Ищет директолога для интернет-магазина в Москве",
      "fit_service": "настройка директа",
      "reason": "прямой запрос, есть ниша + локация"
    }}
  ]
}}

idx — индекс сообщения в массиве (0-based, ровно как пришло).
"""


def build_user_prompt(items: list[dict]) -> str:
    """Формирует список сообщений для AI."""
    lines = [f"Оцени каждое из {len(items)} сообщений ниже:\n"]
    for i, it in enumerate(items):
        chat = it.get("chat_title") or it.get("chat_key") or "—"
        text = (it.get("text") or "")[:1500]
        date = it.get("msg_date") or "—"
        sender = it.get("sender_username") or "anon"
        lines.append(
            f"\n[{i}] Чат: {chat} | Автор: {sender} | Дата: {date}\n"
            f"    Текст: {text}"
        )
    return "\n".join(lines)


# ==========================================
# 🤖 ВЫЗОВ DEEPSEEK
# ==========================================
async def call_deepseek(client_meta: dict, items: list[dict]) -> list[dict]:
    """Возвращает список вердиктов в формате ответа модели."""
    keywords = parse_keywords(client_meta["niche_keywords"])
    sys_prompt = SYSTEM_PROMPT_TMPL.format(
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
        temperature=0.0,
    )
    content = res.choices[0].message.content
    data = json.loads(content)
    return data.get("items", [])


# ==========================================
# 💾 АПДЕЙТ pending_review
# ==========================================
async def apply_verdicts(items: list[dict], verdicts: list[dict]) -> tuple[int, int]:
    """
    Применяет вердикты к pending_review.
    Возвращает (promoted_to_pending, filtered).
    """
    by_idx = {v["idx"]: v for v in verdicts if isinstance(v.get("idx"), int)}
    promoted = 0
    filtered = 0
    now = datetime.utcnow().isoformat()

    async with aiosqlite.connect(APEX_DB) as db:
        for i, it in enumerate(items):
            v = by_idx.get(i)
            if not v:
                # Модель не вернула — оставляем как ai_pending (попадёт в следующий тик)
                continue
            score = int(v.get("score", 0))
            is_lead = bool(v.get("is_lead", False))
            pain = (v.get("pain") or "")[:300]
            fit = (v.get("fit_service") or "")[:80]
            reason = (v.get("reason") or "")[:300]

            if score >= MIN_SCORE_TO_PROMOTE and is_lead:
                new_status = "pending"
                promoted += 1
            else:
                new_status = "ai_filtered"
                filtered += 1

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


async def mark_batch_as_error(items: list[dict], err_msg: str) -> None:
    """При ошибке API оставляем как ai_pending — попадут в следующий тик."""
    pass  # явно ничего не делаем — статус уже ai_pending


# ==========================================
# 📣 НОТИФИКАЦИИ
# ==========================================
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


# ==========================================
# 🚀 ГЛАВНЫЙ ЦИКЛ
# ==========================================
async def main_loop() -> None:
    logger.info(
        "🤖 ai_qualifier запущен. interval=%ds, batch=%d, threshold=%d",
        QUALIFIER_INTERVAL,
        BATCH_SIZE,
        MIN_SCORE_TO_PROMOTE,
    )
    consecutive_errors = 0
    last_low_balance_alert = 0.0
    paused_until = 0.0  # timestamp
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
                # Берём кандидатов
                for batch_idx in range(MAX_BATCHES_PER_TICK):
                    items = await fetch_ai_pending_for_client(
                        client["user_id"], BATCH_SIZE
                    )
                    if not items:
                        break  # для этого клиента закончились
                    try:
                        verdicts = await call_deepseek(client, items)
                        promoted, filtered = await apply_verdicts(items, verdicts)
                        tick_promoted += promoted
                        tick_filtered += filtered
                        consecutive_errors = 0
                        logger.info(
                            f"  '{client['client_name']}' batch[{batch_idx}]: "
                            f"+{promoted} pending, +{filtered} filtered"
                        )
                    except Exception as e:
                        consecutive_errors += 1
                        err_str = str(e)
                        logger.error(
                            f"DeepSeek error для '{client['client_name']}' "
                            f"(подряд={consecutive_errors}): {err_str}"
                        )
                        # Распознаём «нет баланса»
                        is_balance = (
                            "402" in err_str
                            or "insufficient" in err_str.lower()
                            or "balance" in err_str.lower()
                        )
                        if is_balance:
                            if now_ts - last_low_balance_alert > LOW_BALANCE_NOTIFY_INTERVAL:
                                await notify_admin(
                                    "🚨 <b>DeepSeek-баланс закончился</b>\n"
                                    "AI-qualifier ставит себя на паузу до пополнения.\n"
                                    f"Ошибка: <code>{err_str[:200]}</code>"
                                )
                                last_low_balance_alert = now_ts
                            paused_until = now_ts + 30 * 60  # пауза на 30 мин
                            break
                        if consecutive_errors >= DAILY_FAIL_THRESHOLD:
                            await notify_admin(
                                f"🚨 <b>ai_qualifier:</b> {consecutive_errors} ошибок подряд, ухожу на паузу 1ч.\n"
                                f"Последняя: <code>{err_str[:200]}</code>"
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

                # Уведомление админу раз в 30 мин
                if (
                    summary_promoted > 0
                    and (now_ts - last_summary_notify) > 30 * 60
                ):
                    await notify_admin(
                        f"🤖 <b>ai_qualifier:</b>\n"
                        f"+{summary_promoted} новых лидов в /review\n"
                        f"⏭ {summary_filtered} отсеяно AI"
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
