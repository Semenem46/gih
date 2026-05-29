"""Клавиатуры — inline и reply."""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from config import AUTHOR_USERNAME, LINK_BOOK_CALL, LINK_CASES, LINK_CHANNEL, SUPPORT_USERNAME


# ── Reply keyboards ──────────────────────────────────────────────────────────

def main_menu_kb() -> ReplyKeyboardMarkup:
    """Главное меню. AI-демо магнит — основной CTA."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎯 Разбор моей ниши")],
            [
                KeyboardButton(text="ℹ️ О боте"),
                KeyboardButton(text="✉️ Связаться"),
            ],
        ],
        resize_keyboard=True,
    )


def share_phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
            [KeyboardButton(text="⏭ Пропустить")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def remove_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


# ── Inline keyboards ─────────────────────────────────────────────────────────

def get_lead_magnet_kb() -> InlineKeyboardMarkup:
    """Старый xlsx-магнит (deprecated, оставлен для совместимости).

    После переезда на AI-демо больше не используется в /start, но handler
    остался для тех, кто пришёл по старой кнопке.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Получить базу чатов", callback_data="get_file")],
            [InlineKeyboardButton(text="ℹ️ Подробнее", callback_data="about")],
        ]
    )


def _has(url: str | None) -> bool:
    """Кнопка показывается, только если ссылка реально задана и не плейсхолдер."""
    if not url:
        return False
    if "example.com" in url:
        return False
    return True


def after_file_kb() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if _has(LINK_BOOK_CALL):
        rows.append([InlineKeyboardButton(text="📞 Бесплатный разбор", url=LINK_BOOK_CALL)])
    if _has(LINK_CASES):
        rows.append([InlineKeyboardButton(text="📁 Кейсы", url=LINK_CASES)])
    if _has(LINK_CHANNEL):
        rows.append([InlineKeyboardButton(text="📣 Канал автора", url=LINK_CHANNEL)])
    if SUPPORT_USERNAME:
        rows.append([InlineKeyboardButton(
            text="✉️ Написать автору",
            url=f"https://t.me/{SUPPORT_USERNAME}",
        )])
    if not rows:
        rows = [[InlineKeyboardButton(text="ℹ️ О боте", callback_data="about")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def drip_kb(step: int) -> InlineKeyboardMarkup:
    """Кнопки follow-up. Под каждый шаг свой набор."""
    rows: list[list[InlineKeyboardButton]] = []

    book = InlineKeyboardButton(text="📞 Бесплатный разбор", url=LINK_BOOK_CALL) if _has(LINK_BOOK_CALL) else None
    cases = InlineKeyboardButton(text="📁 Кейсы", url=LINK_CASES) if _has(LINK_CASES) else None
    contact = InlineKeyboardButton(
        text="✉️ Написать лично",
        url=f"https://t.me/{SUPPORT_USERNAME}",
    ) if SUPPORT_USERNAME else None
    unsub = InlineKeyboardButton(text="🔕 Не присылать больше", callback_data="unsub")

    desired = {
        0: [contact],
        1: [book, contact],
        2: [book, contact],
        3: [book, contact, cases],
    }
    for btn in desired.get(step, []):
        if btn is not None:
            rows.append([btn])

    rows.append([unsub])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Niche flow ───────────────────────────────────────────────────────────────

def niche_paywall_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="💎 Подключить за 15 000 ₽/мес",
                              callback_data="niche_subscribe")],
    ]
    contact = SUPPORT_USERNAME or AUTHOR_USERNAME
    if contact:
        rows.append([InlineKeyboardButton(
            text="✉️ Написать в личку",
            url=f"https://t.me/{contact}",
        )])
    rows.append([InlineKeyboardButton(text="🔄 Сменить нишу",
                                       callback_data="niche_restart")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def niche_after_request_kb() -> InlineKeyboardMarkup:
    """Клавиатура после того как клиент уже оставил заявку — кнопка прямого
    контакта чтобы он сразу написал автору без ожиданий."""
    rows: list[list[InlineKeyboardButton]] = []
    contact = SUPPORT_USERNAME or AUTHOR_USERNAME
    if contact:
        rows.append([InlineKeyboardButton(
            text=f"✉️ Написать @{contact}",
            url=f"https://t.me/{contact}",
        )])
    rows.append([InlineKeyboardButton(text="🔄 Сменить нишу",
                                       callback_data="niche_restart")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def niche_empty_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Попробовать другую нишу",
                              callback_data="niche_restart")],
    ])


def niche_active_sub_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Сменить нишу", callback_data="niche_restart")],
    ])
