"""Админ-команды: /stats, /broadcast, /export."""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

import db
import apex_bridge as eng
from config import ADMIN_IDS

router = Router(name="admin")
log = logging.getLogger(__name__)


def _is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMIN_IDS)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not _is_admin(message):
        return
    s = db.stats()
    by_utm = db.stats_by_utm()
    utm_lines = "\n".join(f"  • <code>{src}</code> — {n}" for src, n in by_utm[:10])
    text = (
        "<b>📊 Статистика воронки</b>\n\n"
        f"Всего лидов: <b>{s['total']}</b>\n"
        f"Получили файл: <b>{s['got_file']}</b>\n"
        f"Оставили телефон: <b>{s['with_phone']}</b>\n"
        f"Отписались: <b>{s['unsubscribed']}</b>\n"
        f"Сегодня: <b>{s['today']}</b>\n\n"
        f"<b>Источники (UTM):</b>\n{utm_lines or '  —'}"
    )
    await message.answer(text)


@router.message(Command("export"))
async def cmd_export(message: Message) -> None:
    if not _is_admin(message):
        return
    rows = db.all_leads()
    if not rows:
        await message.answer("Лидов пока нет.")
        return

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow([
        "user_id", "username", "first_name", "last_name", "phone",
        "utm_source", "file_sent_at", "is_subscribed", "created_at",
    ])
    for r in rows:
        writer.writerow([
            r["user_id"], r["username"] or "", r["first_name"] or "",
            r["last_name"] or "", r["phone"] or "", r["utm_source"] or "",
            r["file_sent_at"] or "", r["is_subscribed"], r["created_at"],
        ])
    data = buf.getvalue().encode("utf-8-sig")
    await message.answer_document(
        BufferedInputFile(data, filename="leads.csv"),
        caption=f"Экспорт лидов: {len(rows)} строк",
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject) -> None:
    """/broadcast <текст> — массовая рассылка по подписанным лидам.

    Можно ответить (reply) на любое сообщение и написать /broadcast
    — тогда переслётся именно то сообщение (с медиа, форматированием).
    """
    if not _is_admin(message):
        return

    user_ids = db.subscribed_user_ids()
    if not user_ids:
        await message.answer("Нет подписанных лидов.")
        return

    await message.answer(f"Запускаю рассылку по {len(user_ids)} получателям…")

    sent = failed = 0
    bot: Bot = message.bot

    if message.reply_to_message:
        async def send_one(uid: int) -> bool:
            try:
                await bot.copy_message(
                    chat_id=uid,
                    from_chat_id=message.chat.id,
                    message_id=message.reply_to_message.message_id,
                )
                return True
            except Exception as e:
                log.info("broadcast fail to %s: %s", uid, e)
                return False
    else:
        text = command.args
        if not text:
            await message.answer(
                "Использование: <code>/broadcast текст</code> "
                "или ответь на сообщение командой <code>/broadcast</code>."
            )
            return

        async def send_one(uid: int) -> bool:
            try:
                await bot.send_message(uid, text)
                return True
            except Exception as e:
                log.info("broadcast fail to %s: %s", uid, e)
                return False

    # ~25 сообщений/сек — лимит Bot API
    for i, uid in enumerate(user_ids, 1):
        ok = await send_one(uid)
        if ok:
            sent += 1
        else:
            failed += 1
        if i % 25 == 0:
            await asyncio.sleep(1)

    await message.answer(
        f"Рассылка завершена.\n"
        f"✅ Отправлено: <b>{sent}</b>\n"
        f"❌ Не доставлено: <b>{failed}</b>"
    )


@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject, bot: Bot) -> None:
    """/grant <user_id> <дней> <текст ниши>
    Активирует подписку Парсер Pro для пользователя на N дней.
    Свежие лиды по нише будут приходить автоматически через cron.
    """
    if not _is_admin(message):
        return
    args_raw = command.args or ""
    parts = args_raw.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Использование:\n"
            "<code>/grant &lt;user_id&gt; &lt;дней&gt; &lt;ниша&gt;</code>\n\n"
            "Пример:\n"
            "<code>/grant 12345678 30 ремонт квартир в Москве</code>"
        )
        return

    try:
        target_user_id = int(parts[0])
        days = int(parts[1])
    except ValueError:
        await message.answer("user_id и дни должны быть числами.")
        return

    niche_text = parts[2].strip()
    if not niche_text:
        await message.answer("Ниша не задана.")
        return

    await message.answer(f"⏳ Анализирую нишу <i>{niche_text}</i>...")
    try:
        niche_info = await eng.analyze_niche(niche_text)
    except Exception as e:
        log.exception("grant: analyze_niche failed")
        await message.answer(f"Ошибка анализа ниши: {e}")
        return

    try:
        sub = await eng.set_subscription(
            user_id=target_user_id, niche_text=niche_text,
            niche_info=niche_info, duration_days=days,
        )
    except Exception as e:
        log.exception("grant: set_subscription failed")
        await message.answer(f"Ошибка сохранения подписки: {e}")
        return

    canonical = niche_info.get("niche_canonical", niche_text)
    await message.answer(
        f"✅ Подписка активирована\n\n"
        f"User: <code>{target_user_id}</code>\n"
        f"Ниша (нормализована): <b>{canonical}</b>\n"
        f"Действует до: {sub['expires_at']}"
    )

    # Уведомляем клиента
    try:
        await bot.send_message(
            target_user_id,
            f"✅ <b>Подписка Парсер Pro активирована!</b>\n\n"
            f"Ниша: <b>{canonical}</b>\n"
            f"Срок: {days} дней\n\n"
            f"Свежие лиды будут приходить сюда каждый день в 10:00 по МСК.\n"
            f"Гарантия: меньше 5 лидов за месяц — следующий месяц бесплатно."
        )
    except Exception as e:
        await message.answer(f"⚠️ Не смог уведомить клиента: {e}")


@router.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject) -> None:
    """/revoke <user_id> — отключает подписку клиенту."""
    if not _is_admin(message):
        return
    args_raw = (command.args or "").strip()
    if not args_raw.isdigit():
        await message.answer("Использование: <code>/revoke &lt;user_id&gt;</code>")
        return
    target_user_id = int(args_raw)
    ok = await eng.cancel_subscription(target_user_id)
    if ok:
        await message.answer(f"✅ Подписка для <code>{target_user_id}</code> отключена.")
    else:
        await message.answer(f"Не нашёл активной подписки для {target_user_id}.")


@router.message(Command("subinfo"))
async def cmd_subinfo(message: Message, command: CommandObject) -> None:
    """/subinfo <user_id> — показать инфу о подписке."""
    if not _is_admin(message):
        return
    args_raw = (command.args or "").strip()
    if not args_raw.isdigit():
        await message.answer("Использование: <code>/subinfo &lt;user_id&gt;</code>")
        return
    sub = await eng.get_subscription(int(args_raw))
    if not sub:
        await message.answer("Подписка не найдена.")
        return
    await message.answer(
        f"<b>Подписка {args_raw}</b>\n"
        f"Status: <code>{sub['status']}</code>\n"
        f"Active: <b>{sub.get('is_active')}</b>\n"
        f"Niche: {sub.get('niche_text')}\n"
        f"Started: {sub.get('started_at')}\n"
        f"Expires: {sub.get('expires_at')}\n"
        f"Delivered: {sub.get('leads_delivered', 0)}"
    )


@router.message(Command("help"), F.from_user.id.in_(ADMIN_IDS) if ADMIN_IDS else F.text)
async def cmd_admin_help(message: Message) -> None:
    if not _is_admin(message):
        return
    await message.answer(
        "<b>Админ-команды:</b>\n"
        "/stats — статистика воронки\n"
        "/export — выгрузить лидов в CSV\n"
        "/broadcast &lt;текст&gt; — рассылка (или reply на сообщение + /broadcast)\n"
        "/grant &lt;user_id&gt; &lt;дней&gt; &lt;ниша&gt; — активировать подписку\n"
        "/revoke &lt;user_id&gt; — отключить подписку\n"
        "/subinfo &lt;user_id&gt; — статус подписки\n"
        "/unsubscribe — отписать самого себя"
    )
