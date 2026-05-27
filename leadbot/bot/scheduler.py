"""Шедулеры: drip-цепочка лид-магнита + ежедневная отправка свежих лидов подписчикам."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sqlite3

import aiosqlite
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound

import db
import keyboards as kb
import texts as tx
import apex_bridge as eng
from texts import DRIP_STEPS

log = logging.getLogger(__name__)

# Ежедневная отправка свежих лидов: 10:00 по МСК = 07:00 UTC
DAILY_SEND_HOUR_UTC = 7
DAILY_FRESH_HOURS = 24
DAILY_FRESH_LIMIT = 5


async def run_drip_loop(bot: Bot, poll_seconds: int = 60) -> None:
    """Главный цикл шедулера. Запускается в background при старте бота."""
    log.info("Drip scheduler started (poll every %s sec)", poll_seconds)
    last_daily_run_date: dt.date | None = None
    while True:
        try:
            await _process_due(bot)
        except Exception:
            log.exception("Drip loop iteration failed")

        # Ежедневная рассылка свежих лидов подписчикам — раз в сутки в DAILY_SEND_HOUR_UTC
        try:
            now = dt.datetime.utcnow()
            if (now.hour == DAILY_SEND_HOUR_UTC
                    and (last_daily_run_date is None or last_daily_run_date != now.date())):
                await _send_daily_fresh_to_subscribers(bot)
                last_daily_run_date = now.date()
        except Exception:
            log.exception("Daily fresh-leads dispatch failed")

        await asyncio.sleep(poll_seconds)


async def _process_due(bot: Bot) -> None:
    now = dt.datetime.utcnow()
    rows = db.due_messages(now)
    if not rows:
        return
    log.info("Drip: %s messages due", len(rows))

    for r in rows:
        await _send_step(bot, message_id=r["id"], user_id=r["user_id"], step=r["step"])
        # лёгкая задержка, чтобы не упереться в TG rate limit
        await asyncio.sleep(0.05)


async def _send_step(bot: Bot, *, message_id: int, user_id: int, step: int) -> None:
    if step >= len(DRIP_STEPS):
        log.warning("Drip step %s out of range, marking sent", step)
        db.mark_message_sent(message_id)
        return

    lead = db.get_lead(user_id)
    if not lead or not lead["is_subscribed"]:
        db.mark_message_sent(message_id)
        return

    title, body = DRIP_STEPS[step]
    text = f"<b>{title}</b>\n\n{body}".format(author=lead["first_name"] or "")

    try:
        await bot.send_message(user_id, text, reply_markup=kb.drip_kb(step))
        db.log_event(user_id, "drip_sent", payload=str(step))
    except (TelegramForbiddenError, TelegramNotFound) as e:
        log.info("Drip: user %s blocked bot — unsubscribe (%s)", user_id, e)
        db.set_subscribed(user_id, False)
        db.cancel_drip(user_id)
    except Exception:
        log.exception("Drip send failed for user=%s step=%s", user_id, step)
    finally:
        db.mark_message_sent(message_id)


# ── Ежедневная отправка свежих лидов подписчикам ───────────────────────────
async def _list_active_subscribers() -> list[int]:
    """Из apex_ai.db (через query_engine) — active подписчики."""
    user_ids: list[int] = []
    async with aiosqlite.connect(eng.query_engine.APEX_DB) as cx:
        try:
            async with cx.execute(
                """SELECT user_id FROM subscriptions
                   WHERE status = 'active'
                     AND expires_at > ?""",
                (dt.datetime.now(dt.timezone.utc).isoformat(),),
            ) as cur:
                async for row in cur:
                    user_ids.append(row[0])
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                log.info("subscriptions table not initialized yet")
            else:
                raise
    return user_ids


async def _send_daily_fresh_to_subscribers(bot: Bot) -> None:
    user_ids = await _list_active_subscribers()
    if not user_ids:
        log.info("No active subscribers — skipping daily dispatch")
        return
    log.info("Daily fresh-leads dispatch to %d subscribers", len(user_ids))

    for uid in user_ids:
        try:
            leads = await eng.find_fresh_leads_for_subscriber(
                user_id=uid, hours=DAILY_FRESH_HOURS, limit=DAILY_FRESH_LIMIT,
            )
        except Exception:
            log.exception("find_fresh_leads_for_subscriber failed for %s", uid)
            continue

        if not leads:
            log.info("user=%s — no fresh leads in window", uid)
            continue

        delivered = 0
        for lead in leads:
            # Пытаемся claim. Если уже забран — пропускаем.
            try:
                claimed = await eng.claim_lead(lead["corpus_id"], uid)
            except Exception:
                log.exception("claim_lead failed")
                continue
            if not claimed:
                continue

            text_preview = lead["text"][:600] + ("..." if len(lead["text"]) > 600 else "")
            try:
                msg_date = dt.datetime.fromisoformat(lead["msg_date"]).strftime("%d.%m.%Y %H:%M")
            except Exception:
                msg_date = lead.get("msg_date", "—")

            text = tx.NICHE_FRESH_LEAD.format(
                score=lead["score"], text_preview=text_preview,
                chat_title=lead.get("chat_title") or "—",
                date=msg_date, pain=lead["pain"],
                fit_service=lead["fit_service"], link=lead["link"],
            )
            try:
                await bot.send_message(uid, text, disable_web_page_preview=False)
                delivered += 1
                await asyncio.sleep(0.1)
            except (TelegramForbiddenError, TelegramNotFound):
                log.info("Subscriber %s blocked bot — skip", uid)
                break
            except Exception:
                log.exception("Failed to send fresh lead to %s", uid)

        if delivered > 0:
            try:
                async with aiosqlite.connect(eng.query_engine.APEX_DB) as cx:
                    await cx.execute(
                        "UPDATE subscriptions SET leads_delivered = leads_delivered + ? "
                        "WHERE user_id = ?",
                        (delivered, uid),
                    )
                    await cx.commit()
            except Exception:
                log.exception("Failed to bump leads_delivered for %s", uid)
            log.info("user=%s — delivered %d fresh leads", uid, delivered)
