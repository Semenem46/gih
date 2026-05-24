"""Главная воронка: /start → AI-демо магнит (без xlsx-файла).

Новая логика после переезда на AI-магнит:
  • /start → WELCOME с прямым CTA «🎯 Разбор моей ниши»
  • Никакого автоматического отправки xlsx
  • Если юзер очень хочет xlsx — он может нажать «📊 Забрать базу чатов» в меню,
    тогда работает старая логика (опросит контакт, отдаст файл, запустит drip)
"""
from __future__ import annotations

import datetime as dt
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    Message,
)

import db
import keyboards as kb
import texts
from config import (
    ADMIN_IDS,
    DRIP_SCHEDULE_MINUTES,
    LEAD_MAGNET_CAPTION,
    LEAD_MAGNET_FILE,
)

router = Router(name="funnel")
log = logging.getLogger(__name__)


# ── /start ───────────────────────────────────────────────────────────────────
@router.message(CommandStart(deep_link=True))
@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject | None = None) -> None:
    user = message.from_user
    if not user:
        return

    utm = (command.args if command and command.args else None)
    is_new = db.upsert_lead(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        utm_source=utm,
    )
    db.log_event(user.id, "start", payload=utm)

    if is_new:
        await _notify_admins_new_lead(message, utm)

    # AI-магнит: только приветствие + меню. Никакого автоматического xlsx.
    await message.answer(
        texts.WELCOME.format(first_name=user.first_name or "друг"),
        reply_markup=kb.main_menu_kb(),
    )


# ── Кнопка «Забрать базу» (опциональный путь — старый xlsx-магнит) ──────────
@router.callback_query(F.data == "get_file")
async def cb_get_file(query: CallbackQuery) -> None:
    await query.answer()
    if query.message:
        await _ask_phone(query.message, query.from_user.id)


@router.message(F.text == "📊 Забрать базу чатов")
async def msg_get_file(message: Message) -> None:
    await _ask_phone(message, message.from_user.id if message.from_user else 0)


async def _ask_phone(message: Message, user_id: int) -> None:
    lead = db.get_lead(user_id)
    if lead and lead["file_sent_at"]:
        await message.answer(texts.ALREADY_GOT)
        await _send_file(message, user_id)
        return
    db.log_event(user_id, "asked_phone")
    await message.answer(texts.ASK_PHONE, reply_markup=kb.share_phone_kb())


# ── Контакт / пропуск ────────────────────────────────────────────────────────
@router.message(F.contact)
async def on_contact(message: Message) -> None:
    if not message.from_user or not message.contact:
        return
    if message.contact.user_id and message.contact.user_id != message.from_user.id:
        await message.answer("Пожалуйста, отправь СВОЙ контакт через кнопку.")
        return
    db.set_phone(message.from_user.id, message.contact.phone_number)
    db.log_event(message.from_user.id, "phone_shared", message.contact.phone_number)
    await _notify_admins_phone(message, message.contact.phone_number)
    await message.answer(texts.PHONE_THANKS, reply_markup=kb.main_menu_kb())
    await _send_file(message, message.from_user.id)


@router.message(F.text == "⏭ Пропустить")
async def on_skip_phone(message: Message) -> None:
    if not message.from_user:
        return
    db.log_event(message.from_user.id, "phone_skipped")
    await message.answer(texts.PHONE_SKIPPED, reply_markup=kb.main_menu_kb())
    await _send_file(message, message.from_user.id)


# ── Отправка файла + расписание drip ─────────────────────────────────────────
async def _send_file(message: Message, user_id: int) -> None:
    if not LEAD_MAGNET_FILE.exists():
        log.error("Lead magnet file missing: %s", LEAD_MAGNET_FILE)
        await message.answer(
            "Упс, файл временно недоступен. Я уже разбираюсь, напиши автору."
        )
        return

    await message.answer_document(
        FSInputFile(LEAD_MAGNET_FILE),
        caption=LEAD_MAGNET_CAPTION,
    )
    await message.answer(texts.AFTER_FILE, reply_markup=kb.after_file_kb())

    db.mark_file_sent(user_id)
    db.log_event(user_id, "file_sent")
    _schedule_drip(user_id)


def _schedule_drip(user_id: int) -> None:
    """Заранее кладём все шаги drip в БД с временами запуска."""
    now = dt.datetime.utcnow()
    for step, minutes in enumerate(DRIP_SCHEDULE_MINUTES):
        run_at = now + dt.timedelta(minutes=minutes)
        db.schedule_message(user_id, step, run_at)


# ── Меню: «О боте», «Связаться» ──────────────────────────────────────────────
@router.message(F.text == "ℹ️ О боте")
async def msg_about(message: Message) -> None:
    await message.answer(texts.ABOUT, reply_markup=kb.main_menu_kb())


@router.message(F.text == "✉️ Связаться")
async def msg_contact(message: Message) -> None:
    await message.answer(texts.ABOUT, reply_markup=kb.main_menu_kb())


@router.callback_query(F.data == "about")
async def cb_about(query: CallbackQuery) -> None:
    await query.answer()
    if query.message:
        await query.message.answer(texts.ABOUT)


# ── Отписка ──────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "unsub")
async def cb_unsubscribe(query: CallbackQuery) -> None:
    if not query.from_user:
        return
    db.set_subscribed(query.from_user.id, False)
    db.cancel_drip(query.from_user.id)
    db.log_event(query.from_user.id, "unsubscribed")
    await query.answer("Готово")
    if query.message:
        await query.message.answer(texts.UNSUBSCRIBED)


@router.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message) -> None:
    if not message.from_user:
        return
    db.set_subscribed(message.from_user.id, False)
    db.cancel_drip(message.from_user.id)
    db.log_event(message.from_user.id, "unsubscribed_cmd")
    await message.answer(texts.UNSUBSCRIBED)


# ── Уведомления админам ──────────────────────────────────────────────────────
async def _notify_admins_new_lead(message: Message, utm: str | None) -> None:
    if not ADMIN_IDS or not message.from_user:
        return
    user = message.from_user
    text = texts.ADMIN_NEW_LEAD.format(
        user_id=user.id,
        name=f"{user.first_name or ''} {user.last_name or ''}".strip() or "—",
        username=f"@{user.username}" if user.username else "—",
        phone="—",
        utm=utm or "—",
    )
    for admin_id in ADMIN_IDS:
        try:
            await message.bot.send_message(admin_id, text)
        except Exception as e:
            log.warning("Failed to notify admin %s: %s", admin_id, e)


async def _notify_admins_phone(message: Message, phone: str) -> None:
    if not ADMIN_IDS or not message.from_user:
        return
    user = message.from_user
    text = (
        f"📱 Лид <code>{user.id}</code> "
        f"(@{user.username or '—'}) оставил телефон: <code>{phone}</code>"
    )
    for admin_id in ADMIN_IDS:
        try:
            await message.bot.send_message(admin_id, text)
        except Exception as e:
            log.warning("Failed to notify admin %s: %s", admin_id, e)
