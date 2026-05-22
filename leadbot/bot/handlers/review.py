"""
/review — concierge модерация лидов для платных клиентов (v2).

Что добавилось vs v1:
- Карточка показывает AI-оценку: score, pain, fit_service, reason.
- Команда /qstats — расширенная статистика по всем статусам (включая ai_pending, ai_filtered).
- /review показывает только status='pending' (после AI-фильтра).

Команды:
- /review        — следующий pending кандидат (после AI)
- /queue         — короткая статистика
- /qstats        — расширенная статистика (с AI-полями)
- /addclient     — добавить клиента
- /listclients   — активные клиенты
- /pauseclient   /resumeclient

Только админ.
"""
from __future__ import annotations

import html
import json
import logging
from datetime import datetime
from pathlib import Path

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ADMIN_IDS

router = Router(name="review")
log = logging.getLogger(__name__)

APEX_DB = str(Path(__file__).resolve().parent.parent.parent / "apex_ai.db")


def _is_admin(user_id: int | None) -> bool:
    return bool(user_id and user_id in ADMIN_IDS)


# ────────────────────────────────────────────────────────────────────────────
# DB helpers
# ────────────────────────────────────────────────────────────────────────────
async def _fetch_next_pending() -> dict | None:
    async with aiosqlite.connect(APEX_DB) as db:
        db.row_factory = aiosqlite.Row
        sql = """
            SELECT
                pr.id              AS pending_id,
                pr.corpus_id,
                pr.client_user_id,
                pr.created_at      AS queued_at,
                pr.ai_score, pr.ai_pain, pr.ai_fit_service, pr.ai_reason,
                pr.ai_processed_at,
                mc.chat_title, mc.chat_key, mc.sender_username, mc.sender_id,
                mc.text, mc.link, mc.msg_date,
                pc.client_name, pc.niche_text, pc.niche_keywords
            FROM pending_review pr
            JOIN messages_corpus mc ON mc.id = pr.corpus_id
            LEFT JOIN paid_clients pc ON pc.user_id = pr.client_user_id
            WHERE pr.status = 'pending'
            ORDER BY pr.ai_score DESC NULLS LAST, pr.id ASC
            LIMIT 1
        """
        async with db.execute(sql) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def _queue_stats() -> list[tuple[str, int, int, int]]:
    """Совместимо со старой /queue: только pending+sent+rejected."""
    async with aiosqlite.connect(APEX_DB) as db:
        sql = """
            SELECT
                COALESCE(pc.client_name, 'user_' || pr.client_user_id) AS name,
                SUM(CASE WHEN pr.status = 'pending'  THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN pr.status = 'sent'     THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN pr.status = 'rejected' THEN 1 ELSE 0 END) AS rejected
            FROM pending_review pr
            LEFT JOIN paid_clients pc ON pc.user_id = pr.client_user_id
            GROUP BY pr.client_user_id
            ORDER BY pending DESC
        """
        async with db.execute(sql) as cur:
            rows = await cur.fetchall()
    return [(r[0], r[1] or 0, r[2] or 0, r[3] or 0) for r in rows]


async def _qstats_full() -> dict:
    """Расширенная статистика с AI-статусами."""
    out = {"per_client": [], "totals": {}}
    async with aiosqlite.connect(APEX_DB) as db:
        # Per-client
        sql = """
            SELECT
                COALESCE(pc.client_name, 'user_' || pr.client_user_id) AS name,
                pr.client_user_id,
                SUM(CASE WHEN pr.status = 'ai_pending'  THEN 1 ELSE 0 END) AS ai_pending,
                SUM(CASE WHEN pr.status = 'ai_filtered' THEN 1 ELSE 0 END) AS ai_filtered,
                SUM(CASE WHEN pr.status = 'pending'     THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN pr.status = 'sent'        THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN pr.status = 'rejected'    THEN 1 ELSE 0 END) AS rejected,
                SUM(CASE WHEN pr.status = 'skipped'     THEN 1 ELSE 0 END) AS skipped
            FROM pending_review pr
            LEFT JOIN paid_clients pc ON pc.user_id = pr.client_user_id
            GROUP BY pr.client_user_id
            ORDER BY pending DESC
        """
        async with db.execute(sql) as cur:
            rows = await cur.fetchall()
        for r in rows:
            out["per_client"].append({
                "name": r[0],
                "user_id": r[1],
                "ai_pending": r[2] or 0,
                "ai_filtered": r[3] or 0,
                "pending": r[4] or 0,
                "sent": r[5] or 0,
                "rejected": r[6] or 0,
                "skipped": r[7] or 0,
            })

        # Totals по статусам
        async with db.execute(
            "SELECT status, COUNT(*) FROM pending_review GROUP BY status"
        ) as cur:
            for status, cnt in await cur.fetchall():
                out["totals"][status] = cnt

        # Сегодняшние sent (по UTC)
        async with db.execute(
            """
            SELECT COUNT(*) FROM pending_review
            WHERE status = 'sent' AND date(reviewed_at) = date('now', 'utc')
            """
        ) as cur:
            row = await cur.fetchone()
            out["today_sent"] = row[0] if row else 0
    return out


async def _mark_status(pending_id: int, new_status: str) -> None:
    async with aiosqlite.connect(APEX_DB) as db:
        await db.execute(
            "UPDATE pending_review SET status = ?, reviewed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_status, pending_id),
        )
        await db.commit()


async def _record_delivered(corpus_id: int, client_user_id: int) -> None:
    async with aiosqlite.connect(APEX_DB) as db:
        await db.execute(
            "INSERT OR IGNORE INTO delivered_leads (corpus_id, client_user_id) VALUES (?, ?)",
            (corpus_id, client_user_id),
        )
        await db.commit()


# ────────────────────────────────────────────────────────────────────────────
# Форматирование
# ────────────────────────────────────────────────────────────────────────────
def _score_emoji(score: int | None) -> str:
    if score is None:
        return "—"
    if score >= 95:
        return "🔥"
    if score >= 85:
        return "🟢"
    if score >= 80:
        return "🟡"
    return "🔴"


def _format_card(item: dict) -> tuple[str, InlineKeyboardMarkup]:
    pending_id = item["pending_id"]
    client_name = item.get("client_name") or f"user_{item['client_user_id']}"
    niche = item.get("niche_text") or "—"
    chat_title = item.get("chat_title") or item.get("chat_key") or "—"
    sender = item.get("sender_username") or item.get("sender_id") or "—"
    msg_date = item.get("msg_date") or "—"
    text = item.get("text") or "(пусто)"
    link = item.get("link") or ""
    queued_at = item.get("queued_at") or "—"

    score = item.get("ai_score")
    pain = item.get("ai_pain") or ""
    fit = item.get("ai_fit_service") or ""
    reason = item.get("ai_reason") or ""

    text_safe = html.escape(text[:900])
    if len(text) > 900:
        text_safe += "…"

    chat_safe = html.escape(str(chat_title)[:80])
    sender_safe = html.escape(str(sender)[:60])
    client_safe = html.escape(str(client_name)[:60])
    niche_safe = html.escape(str(niche)[:60])
    pain_safe = html.escape(pain[:200])
    fit_safe = html.escape(fit[:60])
    reason_safe = html.escape(reason[:200])

    ai_block = ""
    if score is not None:
        ai_block = (
            f"\n🤖 <b>AI-оценка:</b> {_score_emoji(score)} <b>{score}/100</b>\n"
            f"🎯 Услуга: <code>{fit_safe}</code>\n"
            f"💢 Боль: <i>{pain_safe}</i>\n"
            f"🧠 Причина: <i>{reason_safe}</i>\n"
        )

    card = (
        f"📋 <b>Кандидат #{pending_id}</b>\n"
        f"👔 Клиент: <b>{client_safe}</b>\n"
        f"🎯 Ниша: <i>{niche_safe}</i>\n"
        f"⏱  В очереди с: {queued_at}\n"
        f"{ai_block}"
        f"\n💬 <b>Чат:</b> {chat_safe}\n"
        f"👤 <b>Автор:</b> {sender_safe}\n"
        f"📅 <b>Дата:</b> {msg_date}\n\n"
        f"<b>📝 Сообщение:</b>\n"
        f"<blockquote>{text_safe}</blockquote>\n"
    )
    if link:
        card += f'\n🔗 <a href="{html.escape(link)}">Открыть в Telegram</a>'

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Отправить клиенту", callback_data=f"rv:send:{pending_id}"),
            ],
            [
                InlineKeyboardButton(text="❌ Выкинуть", callback_data=f"rv:rej:{pending_id}"),
                InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"rv:skip:{pending_id}"),
            ],
            [
                InlineKeyboardButton(text="🏁 Стоп", callback_data="rv:stop"),
            ],
        ]
    )
    return card, kb


def _format_lead_for_client(item: dict) -> str:
    chat_title = item.get("chat_title") or item.get("chat_key") or "—"
    sender = item.get("sender_username") or item.get("sender_id") or "—"
    msg_date = item.get("msg_date") or "—"
    text = item.get("text") or "(пусто)"
    link = item.get("link") or ""

    text_safe = html.escape(text[:2000])
    if len(text) > 2000:
        text_safe += "…"
    chat_safe = html.escape(str(chat_title)[:120])
    sender_safe = html.escape(str(sender)[:120])

    out = (
        f"🎯 <b>Новый лид по вашей нише</b>\n\n"
        f"💬 <b>Чат:</b> {chat_safe}\n"
        f"👤 <b>Автор:</b> {sender_safe}\n"
        f"📅 <b>Дата:</b> {msg_date}\n\n"
        f"📝 <b>Сообщение:</b>\n"
        f"<blockquote>{text_safe}</blockquote>"
    )
    if link:
        out += f'\n\n🔗 <a href="{html.escape(link)}">Открыть сообщение в Telegram</a>'
    return out


# ────────────────────────────────────────────────────────────────────────────
# Commands
# ────────────────────────────────────────────────────────────────────────────
@router.message(Command("review"))
async def cmd_review(message: Message) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None):
        return
    item = await _fetch_next_pending()
    if not item:
        # Подскажем, может в ai_pending что-то висит
        async with aiosqlite.connect(APEX_DB) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM pending_review WHERE status = 'ai_pending'"
            ) as cur:
                row = await cur.fetchone()
                ai_pending = row[0] if row else 0
        if ai_pending:
            await message.answer(
                f"✨ Очередь /review пуста.\n\n"
                f"🤖 Но в AI-очереди ещё <b>{ai_pending}</b> ожидают обработки.\n"
                f"Если ai_qualifier работает — появятся в течение пары минут."
            )
        else:
            await message.answer("✨ Очередь /review пуста. Лидов нет.")
        return
    text, kb = _format_card(item)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.message(Command("queue"))
async def cmd_queue(message: Message) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None):
        return
    stats = await _queue_stats()
    if not stats:
        await message.answer("📊 Очередь пуста.")
        return
    lines = ["<b>📊 Очередь /review</b>\n"]
    total_pending = 0
    for name, pending, sent, rejected in stats:
        total_pending += pending
        lines.append(
            f"• <b>{html.escape(name)}</b>: "
            f"⏳ {pending} pending · ✅ {sent} sent · ❌ {rejected} reject"
        )
    lines.append(f"\n<b>Итого pending: {total_pending}</b>")
    await message.answer("\n".join(lines))


@router.message(Command("qstats"))
async def cmd_qstats(message: Message) -> None:
    """Расширенная статистика по всем статусам, включая AI."""
    if not _is_admin(message.from_user.id if message.from_user else None):
        return
    s = await _qstats_full()
    lines = ["<b>📊 Полная статистика очереди</b>\n"]

    if not s["per_client"]:
        await message.answer("📊 Очередь полностью пуста.")
        return

    for c in s["per_client"]:
        ratio_filter = (
            f" ({100 * c['ai_filtered'] // max(1, c['ai_filtered'] + c['pending'] + c['sent'])}%)"
            if (c['ai_filtered'] + c['pending'] + c['sent']) > 0 else ""
        )
        lines.append(
            f"\n<b>{html.escape(c['name'])}</b> (<code>{c['user_id']}</code>)"
        )
        lines.append(
            f"  🤖 AI-очередь: <b>{c['ai_pending']}</b> ждут | <b>{c['ai_filtered']}</b> отсеяно{ratio_filter}\n"
            f"  📋 К ревью: <b>{c['pending']}</b> pending\n"
            f"  📤 Отправлено: <b>{c['sent']}</b> | ❌ rejected: {c['rejected']} | ⏭ skipped: {c['skipped']}"
        )

    totals = s["totals"]
    lines.append("\n<b>Всего по системе:</b>")
    lines.append(
        f"  ai_pending: {totals.get('ai_pending', 0)} · "
        f"ai_filtered: {totals.get('ai_filtered', 0)}"
    )
    lines.append(
        f"  pending: {totals.get('pending', 0)} · "
        f"sent: {totals.get('sent', 0)} · "
        f"rejected: {totals.get('rejected', 0)}"
    )
    lines.append(f"\n📅 Сегодня sent: <b>{s['today_sent']}</b>")

    await message.answer("\n".join(lines))


# ────────────────────────────────────────────────────────────────────────────
# Callback handlers
# ────────────────────────────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("rv:"))
async def cb_review(query: CallbackQuery, bot: Bot) -> None:
    if not _is_admin(query.from_user.id if query.from_user else None):
        await query.answer("Только для админа.", show_alert=True)
        return

    data = query.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "stop":
        await query.answer("ОК, выхожу")
        if query.message:
            await query.message.edit_text("🏁 Сессия /review завершена.\nЧтобы продолжить: /review")
        return

    try:
        pending_id = int(parts[2])
    except (ValueError, IndexError):
        await query.answer("Битая кнопка", show_alert=True)
        return

    async with aiosqlite.connect(APEX_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT pr.id AS pending_id, pr.corpus_id, pr.client_user_id, pr.status,
                   mc.chat_title, mc.chat_key, mc.sender_username, mc.sender_id,
                   mc.text, mc.link, mc.msg_date,
                   pc.client_name, pc.niche_text
            FROM pending_review pr
            JOIN messages_corpus mc ON mc.id = pr.corpus_id
            LEFT JOIN paid_clients pc ON pc.user_id = pr.client_user_id
            WHERE pr.id = ?
            """,
            (pending_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        await query.answer("Не найдено", show_alert=True)
        return

    item = dict(row)
    if item["status"] != "pending":
        await query.answer(f"Этот кандидат уже {item['status']}.", show_alert=True)
        await _show_next_or_done(query)
        return

    if action == "send":
        try:
            await bot.send_message(
                item["client_user_id"],
                _format_lead_for_client(item),
                disable_web_page_preview=False,
            )
            await _record_delivered(item["corpus_id"], item["client_user_id"])
            await _mark_status(pending_id, "sent")
            await query.answer("✅ Отправлено клиенту")
            if query.message:
                await query.message.edit_text(
                    (query.message.text or "") + f"\n\n<b>✅ ОТПРАВЛЕНО</b>",
                    reply_markup=None,
                )
        except Exception as e:
            log.exception("send to client failed")
            await query.answer(f"❌ Не отправилось: {e}", show_alert=True)
            return
    elif action == "rej":
        await _mark_status(pending_id, "rejected")
        await query.answer("❌ Выкинуто")
        if query.message:
            await query.message.edit_text(
                (query.message.text or "") + "\n\n<b>❌ ВЫКИНУТО</b>",
                reply_markup=None,
            )
    elif action == "skip":
        await _mark_status(pending_id, "skipped")
        await query.answer("⏭ Пропущено")
        if query.message:
            await query.message.edit_text(
                (query.message.text or "") + "\n\n<b>⏭ ПРОПУЩЕНО</b>",
                reply_markup=None,
            )
    else:
        await query.answer("Неизвестное действие")
        return

    await _show_next_or_done(query)


async def _show_next_or_done(query: CallbackQuery) -> None:
    item = await _fetch_next_pending()
    if not item:
        if query.message:
            await query.message.answer("✨ Очередь пуста. Всё прорeviewено.")
        return
    text, kb = _format_card(item)
    if query.message:
        await query.message.answer(text, reply_markup=kb, disable_web_page_preview=True)


# ────────────────────────────────────────────────────────────────────────────
# /addclient + management
# ────────────────────────────────────────────────────────────────────────────
@router.message(Command("addclient"))
async def cmd_addclient(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None):
        return
    args = command.args or ""
    if "|" not in args:
        await message.answer(
            "Использование:\n"
            "<code>/addclient &lt;user_id&gt; &lt;имя&gt; | &lt;keyword1,keyword2,...&gt; | [ниша]</code>"
        )
        return
    head, _, tail = args.partition("|")
    head = head.strip()
    parts = head.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Не хватает user_id или имени.")
        return
    try:
        target_uid = int(parts[0])
    except ValueError:
        await message.answer("user_id должен быть числом.")
        return
    client_name = parts[1].strip()

    rest = tail.split("|")
    keywords_csv = rest[0].strip()
    niche_text = rest[1].strip() if len(rest) > 1 else client_name

    keywords_list = [k.strip() for k in keywords_csv.split(",") if k.strip()]
    if not keywords_list:
        await message.answer("Не задано ни одного ключа.")
        return
    keywords_json = json.dumps(keywords_list, ensure_ascii=False)

    async with aiosqlite.connect(APEX_DB) as db:
        await db.execute(
            """
            INSERT INTO paid_clients (user_id, client_name, niche_text, niche_keywords, status)
            VALUES (?, ?, ?, ?, 'active')
            ON CONFLICT(user_id) DO UPDATE SET
                client_name    = excluded.client_name,
                niche_text     = excluded.niche_text,
                niche_keywords = excluded.niche_keywords,
                status         = 'active'
            """,
            (target_uid, client_name, niche_text, keywords_json),
        )
        await db.commit()

    await message.answer(
        f"✅ Клиент добавлен / обновлён:\n\n"
        f"user_id: <code>{target_uid}</code>\n"
        f"Имя: <b>{html.escape(client_name)}</b>\n"
        f"Ниша: <i>{html.escape(niche_text)}</i>\n"
        f"Ключи ({len(keywords_list)}): <code>{html.escape(', '.join(keywords_list))}</code>\n\n"
        f"live_push начнёт ловить под него в течение 30 сек,\n"
        f"AI-qualifier фильтрует за следующие 1-2 минуты."
    )


@router.message(Command("listclients"))
async def cmd_listclients(message: Message) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None):
        return
    async with aiosqlite.connect(APEX_DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, client_name, niche_text, niche_keywords, status, started_at, expires_at FROM paid_clients ORDER BY started_at DESC"
        ) as cur:
            rows = await cur.fetchall()
    if not rows:
        await message.answer("Платных клиентов пока нет.")
        return
    lines = ["<b>👔 Платные клиенты:</b>\n"]
    for r in rows:
        status_emoji = "🟢" if r["status"] == "active" else "⏸"
        kw = r["niche_keywords"]
        try:
            kw_list = json.loads(kw)
            kw_show = ", ".join(kw_list[:6])
            if len(kw_list) > 6:
                kw_show += f", +{len(kw_list)-6}"
        except Exception:
            kw_show = (kw or "")[:80]
        lines.append(
            f"{status_emoji} <b>{html.escape(r['client_name'] or '?')}</b> "
            f"(<code>{r['user_id']}</code>)\n"
            f"   Ниша: <i>{html.escape(r['niche_text'] or '—')}</i>\n"
            f"   Ключи: <code>{html.escape(kw_show)}</code>"
        )
    await message.answer("\n".join(lines))


@router.message(Command("pauseclient"))
async def cmd_pauseclient(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None):
        return
    args = (command.args or "").strip()
    if not args.isdigit():
        await message.answer("Использование: <code>/pauseclient &lt;user_id&gt;</code>")
        return
    uid = int(args)
    async with aiosqlite.connect(APEX_DB) as db:
        await db.execute(
            "UPDATE paid_clients SET status = 'paused' WHERE user_id = ?", (uid,)
        )
        await db.commit()
    await message.answer(f"⏸ Клиент <code>{uid}</code> поставлен на паузу.")


@router.message(Command("resumeclient"))
async def cmd_resumeclient(message: Message, command: CommandObject) -> None:
    if not _is_admin(message.from_user.id if message.from_user else None):
        return
    args = (command.args or "").strip()
    if not args.isdigit():
        await message.answer("Использование: <code>/resumeclient &lt;user_id&gt;</code>")
        return
    uid = int(args)
    async with aiosqlite.connect(APEX_DB) as db:
        await db.execute(
            "UPDATE paid_clients SET status = 'active' WHERE user_id = ?", (uid,)
        )
        await db.commit()
    await message.answer(f"▶️ Клиент <code>{uid}</code> снова активен.")
