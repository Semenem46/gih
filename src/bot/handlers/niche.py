"""Хендлер «🎯 Разбор моей ниши» — взаимодействие с query_engine.

Поток:
  1. /start или меню → пользователь жмёт «🎯 Разбор моей ниши»
  2. Бот спрашивает текстом нишу (FSM state: NicheStates.awaiting_niche)
  3. Бот вызывает analyze_niche → count_potential_leads → pre-check сообщение
  4. Бот вызывает find_leads (n=1 для бесплатного превью) → показывает 1 лид
  5. Бот показывает paywall: «Подключить 15k ₽/мес»
  6. Клик на «Подключить» → отправляет заявку админу + подтверждение клиенту
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import db
import keyboards as kb
import texts
import apex_bridge as eng
from config import ADMIN_IDS, AUTHOR_NAME, SUPPORT_USERNAME

router = Router(name="niche")
log = logging.getLogger(__name__)


class NicheStates(StatesGroup):
    awaiting_niche = State()


# ── Точка входа ──────────────────────────────────────────────────────────────
@router.message(F.text == "🎯 Разбор моей ниши")
@router.message(Command("niche"))
async def cmd_niche_start(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    user_id = message.from_user.id

    # Если у пользователя уже активная подписка — другая ветка
    sub = await eng.get_subscription(user_id)
    if sub and sub.get("is_active"):
        niche_text = sub.get("niche_text", "—")
        delivered = sub.get("leads_delivered", 0)
        expires_iso = sub.get("expires_at", "—")
        try:
            expires = dt.datetime.fromisoformat(expires_iso).strftime("%d.%m.%Y")
        except Exception:
            expires = expires_iso
        await message.answer(
            texts.NICHE_SUB_ACTIVE.format(
                niche=niche_text, expires=expires, delivered=delivered
            ),
            reply_markup=kb.niche_active_sub_kb(),
        )
        return

    db.log_event(user_id, "niche_start")
    await state.set_state(NicheStates.awaiting_niche)
    await message.answer(texts.NICHE_INTRO)


# ── Сменить нишу (callback из inline-кнопки) ────────────────────────────────
@router.callback_query(F.data == "niche_restart")
async def cb_niche_restart(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    if not query.from_user or not query.message:
        return
    db.log_event(query.from_user.id, "niche_restart")
    await state.clear()
    await state.set_state(NicheStates.awaiting_niche)
    await query.message.answer(texts.NICHE_INTRO)


# ── Получили текст ниши ──────────────────────────────────────────────────────
# Если в состоянии awaiting_niche пришёл текст — он точно про нишу.
# Кнопки главного меню (📊, 🎯, ℹ️, ✉️) пропускаем — пусть их обрабатывает funnel.
@router.message(
    NicheStates.awaiting_niche,
    F.text,
    ~F.text.startswith("📊"),
    ~F.text.startswith("🎯"),
    ~F.text.startswith("ℹ️"),
    ~F.text.startswith("✉️"),
    ~F.text.startswith("/"),
)
async def on_niche_text(message: Message, state: FSMContext) -> None:
    if not message.from_user or not message.text:
        return
    user_id = message.from_user.id
    niche_text = message.text.strip()

    if len(niche_text) < 3:
        await message.answer("Слишком коротко. Опиши нишу одной фразой.")
        return
    if len(niche_text) > 500:
        await message.answer("Слишком длинно. До 500 символов.")
        return

    db.log_event(user_id, "niche_text", payload=niche_text[:200])
    await state.update_data(niche_text=None, niche_info=None)
    status_msg = await message.answer(texts.NICHE_ANALYZING)

    try:
        niche_info = await eng.analyze_niche(niche_text)
    except Exception as e:
        log.exception("analyze_niche failed: %s", e)
        await status_msg.edit_text(
            "Упс, ошибка при разборе ниши. Попробуй ещё раз через минуту."
        )
        await state.clear()
        return

    keywords = niche_info.get("keywords") or []
    canonical = niche_info.get("niche_canonical", niche_text)
    geo = niche_info.get("geo")
    geo_line = f"📍 Гео: {geo}\n" if geo else ""

    try:
        count = await eng.count_potential_leads(keywords, days=30)
    except Exception as e:
        log.exception("count_potential_leads failed: %s", e)
        count = 0

    log.info("niche=%r canonical=%r keywords=%s count=%d",
             niche_text, canonical, keywords, count)

    # Слишком пусто — отказываем мягко (порог 5: меньше шансов получить мусорный лид)
    if count < 5:
        await status_msg.edit_text(
            texts.NICHE_EMPTY.format(niche=canonical, count=count),
            reply_markup=kb.niche_empty_kb(),
        )
        await state.clear()
        return

    # Pre-check информация + сразу запускаем квалификацию
    await status_msg.edit_text(
        texts.NICHE_PRECHECK.format(
            niche=canonical, geo_line=geo_line, days=30, count=count
        )
    )
    qualifying_msg = await message.answer(texts.NICHE_QUALIFYING)

    try:
        result = await eng.find_leads(
            user_id=user_id, niche_text=niche_text, n=1, niche_info=niche_info
        )
    except Exception as e:
        log.exception("find_leads failed: %s", e)
        await qualifying_msg.edit_text(
            "Упс, AI-квалификатор сейчас недоступен. Попробуй позже."
        )
        await state.clear()
        return

    qualified_count = result.get("stats", {}).get("qualified", 0)
    leads = result.get("leads", [])

    if not leads:
        await qualifying_msg.edit_text(
            texts.NICHE_NO_QUALIFIED.format(count=count),
            reply_markup=kb.niche_empty_kb(),
        )
        await state.clear()
        return

    lead = leads[0]
    text_preview = lead["text"][:600] + ("..." if len(lead["text"]) > 600 else "")
    try:
        msg_date = dt.datetime.fromisoformat(lead["msg_date"]).strftime("%d.%m.%Y %H:%M")
    except Exception:
        msg_date = lead.get("msg_date", "—")

    await qualifying_msg.delete()
    await message.answer(
        texts.NICHE_FREE_LEAD.format(
            score=lead["score"], text_preview=text_preview,
            chat_title=lead["chat_title"] or "—",
            date=msg_date, pain=lead["pain"], fit_service=lead["fit_service"],
            link=lead["link"],
        ),
        disable_web_page_preview=False,
    )

    # Прогноз для paywall: экстраполируем qualified из 1 sample на корпус.
    # qualified_count считается на FTS_CANDIDATES (~150). Экстраполируем на 30 дней.
    forecast = max(qualified_count, 5)  # консервативная оценка
    await message.answer(
        texts.NICHE_PAYWALL.format(forecast=f"~{forecast}"),
        reply_markup=kb.niche_paywall_kb(),
    )

    # Сохраняем нишу клиента в state на случай subscribe, сбрасываем FSM
    await state.set_state(None)
    await state.update_data(niche_text=niche_text, niche_info=niche_info)
    db.log_event(user_id, "niche_free_lead_shown", payload=str(lead["corpus_id"]))


# ── Подключить подписку ──────────────────────────────────────────────────────
@router.callback_query(F.data == "niche_subscribe")
async def cb_niche_subscribe(query: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await query.answer()
    if not query.from_user or not query.message:
        return
    user_id = query.from_user.id

    data = await state.get_data()
    niche_text = data.get("niche_text", "—")

    db.log_event(user_id, "niche_subscribe_request", payload=niche_text)

    # Уведомляем админа — он свяжется и активирует через /grant
    user = query.from_user
    user_name = user.full_name or "—"
    user_username = f"@{user.username}" if user.username else "—"
    admin_text = (
        "💎 <b>Заявка на подписку Парсер Pro</b>\n\n"
        f"User ID: <code>{user_id}</code>\n"
        f"Имя: {user_name}\n"
        f"Username: {user_username}\n"
        f"Ниша: <b>{niche_text}</b>\n\n"
        f"Активировать: <code>/grant {user_id} 30 {niche_text}</code>"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_text)
        except Exception as e:
            log.error("Failed to notify admin %s: %s", admin_id, e)

    await query.message.answer(
        texts.NICHE_REQUEST_SENT,
        reply_markup=kb.niche_after_request_kb(),
    )
    await state.clear()
