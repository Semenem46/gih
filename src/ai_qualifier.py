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

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import APEX_DB as _PATHS_APEX_DB

APEX_DB = os.environ.get("APEX_DB") or _PATHS_APEX_DB
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY") or "sk-d75f7d76a50c49648aaf061611ce62b5"
BOT_TOKEN = os.environ.get("BOT_TOKEN") or "8565672652:AAGpwT7Lg50bSL-SDBgwG15ci0BcSydNAU4"
ADMIN_ID = int(os.environ.get("APEX_ADMIN_ID") or "7531405698")

QUALIFIER_INTERVAL = 60
BATCH_SIZE = 5
MAX_BATCHES_PER_TICK = 4
MIN_SCORE_TO_PROMOTE = 85

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ai_qualifier")
ai_client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com/v1", timeout=60.0)

async def fetch_active_clients() -> list[dict]:
    async with aiosqlite.connect(APEX_DB) as db:
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute("SELECT user_id, client_name, niche_text, niche_keywords, COALESCE(mode, 'lead-finder') AS mode FROM paid_clients WHERE status = 'active' AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)") as cur:
                return [dict(r) for r in await cur.fetchall()]
        except: return []

async def fetch_ai_pending_for_client(user_id: int, limit: int) -> list[dict]:
    async with aiosqlite.connect(APEX_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT pr.id AS pending_id, pr.corpus_id, mc.chat_key, mc.chat_title, mc.text, mc.msg_date, mc.sender_username FROM pending_review pr JOIN messages_corpus mc ON mc.id = pr.corpus_id WHERE pr.client_user_id = ? AND pr.status = 'ai_pending' ORDER BY pr.id ASC LIMIT ?", (user_id, limit)) as cur:
            return [dict(r) for r in await cur.fetchall()]

def parse_keywords(kw_field: str) -> list[str]:
    if not kw_field: return []
    try: return [str(x).strip() for x in json.loads(kw_field)]
    except: return [p.strip() for p in kw_field.split(",") if p.strip()]

LEAD_FINDER_SYSTEM = """Ты — СТРОГИЙ Lead Qualifier. По умолчанию REJECT. APPROVE только если ВСЕ 6 GATE пройдены подряд без "может быть".
ПРОФИЛЬ КЛИЕНТА:
Имя: {client_name} | Ниша: {niche_text}
service: {service} | vertical: {vertical} | vertical_strict: {vertical_strict}
service_keywords: {service_keywords} | vertical_keywords: {vertical_keywords} | negative_verticals: {negative_verticals}
═══ 6 GATE ═══
GATE 1 — ЯЗЫК: не русский → REJECT.
GATE 2a — VERTICAL: vertical_strict=true и нет слов из vertical_keywords или есть из negative_verticals → REJECT.
GATE 2b — SERVICE (ВСЕГДА): Услуга, которую ищет автор, должна совпадать с service клиента или его service_keywords. Если просят ДРУГУЮ услугу (клиент = директолог, а просят чат-бота/amoCRM/SMM/SEO) → REJECT, service_match=false.
GATE 3 — ТИП: вакансия/резюме/новость/вопрос/продажа услуг → REJECT.
GATE 4 — ИНТЕНТ: автор ЯВНО нанимает ("ищу подрядчика", "нам нужен", "ищем команду"). Без этого → REJECT.
GATE 5 — РОЛЬ: автор = заказчик.
═══ СКОРИНГ ═══
95-100: запрос + детали; 90-94: запрос без бюджета; 85-89: запрос + service match без деталей. <85: REJECT. 85-89 — нормальный диапазон для TG-лидов!
ФОРМАТ СТРОГО JSON: {{"items": [{{"idx": 0, "status": "APPROVE"|"REJECT", "is_lead": true|false, "score": 0-100, "service_match": true|false, "vertical_match": true|false, "commercial_intent": true|false, "pain": "фраза", "fit_service": "услуга", "reason": "почему"}}]}}"""

DM_OUTREACH_SYSTEM = """Ты — DM Outreach Qualifier. Мануал для Никиты @nikita_Apex..."""

def build_user_prompt(items: list[dict]) -> str:
    lines = []
    for i, it in enumerate(items):
        lines.append(f"[{i}] Чат: {it.get('chat_title')} | Автор: @{it.get('sender_username')}\nТекст: {it.get('text')}")
    return "\n".join(lines)

async def call_deepseek(client_meta: dict, items: list[dict]) -> list[dict]:
    mode = client_meta.get("mode") or "lead-finder"
    niche_info = {}
    try: niche_info = json.loads(client_meta["niche_keywords"])
    except: pass

    if mode == "dm-outreach": sys_prompt = DM_OUTREACH_SYSTEM
    else:
        sys_prompt = LEAD_FINDER_SYSTEM.format(
            client_name=client_meta["client_name"] or "клиент", niche_text=client_meta["niche_text"] or "—",
            service=niche_info.get("service", "—"), vertical=niche_info.get("vertical", "—"),
            vertical_strict=niche_info.get("vertical_strict", False), geo=niche_info.get("geo", "—"),
            service_keywords=", ".join(niche_info.get("service_keywords", [])),
            vertical_keywords=", ".join(niche_info.get("vertical_keywords", [])),
            negative_verticals=", ".join(niche_info.get("negative_verticals", []))
        )
    
    res = await ai_client.chat.completions.create(model="deepseek-chat", messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": build_user_prompt(items)}], response_format={"type": "json_object"}, temperature=0.0)
    verdicts = json.loads(res.choices[0].message.content).get("items", [])

    if mode != "dm-outreach":
        for v in verdicts:
            if niche_info.get("vertical_strict") and v.get("vertical_match") is False:
                v["status"] = "REJECT"; v["score"] = 0; v["is_lead"] = False
            elif v.get("service_match") is False:
                v["status"] = "REJECT"; v["score"] = 0; v["is_lead"] = False
            elif v.get("commercial_intent") is False:
                v["status"] = "REJECT"; v["is_lead"] = False; v["score"] = min(int(v.get("score", 0)), 49)
    return verdicts

async def apply_verdicts(items: list[dict], verdicts: list[dict], mode: str) -> tuple[int, int]:
    by_idx = {v["idx"]: v for v in verdicts if "idx" in v}
    promoted = filtered = 0
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(APEX_DB) as db:
        for i, it in enumerate(items):
            v = by_idx.get(i)
            if not v: continue
            score = int(v.get("score", 0))
            is_lead = bool(v.get("is_lead", False))
            status = "pending" if (score >= MIN_SCORE_TO_PROMOTE and is_lead) else "ai_filtered"
            if status == "pending": promoted += 1
            else: filtered += 1
            await db.execute("UPDATE pending_review SET status=?, ai_score=?, ai_pain=?, ai_fit_service=?, ai_reason=?, ai_processed_at=? WHERE id=?", (status, score, v.get("pain", ""), v.get("fit_service", ""), v.get("reason", ""), now, it["pending_id"]))
        await db.commit()
    return promoted, filtered

async def main_loop():
    while True:
        try:
            clients = await fetch_active_clients()
            for client in clients:
                items = await fetch_ai_pending_for_client(client["user_id"], BATCH_SIZE)
                if items:
                    verdicts = await call_deepseek(client, items)
                    await apply_verdicts(items, verdicts, client.get("mode", "lead-finder"))
        except Exception as e: print(f"Ошибка цикла: {e}")
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main_loop())
