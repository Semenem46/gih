"""Точка входа. Запуск: python bot.py"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import db
import handlers
import scheduler
import apex_bridge as eng
from config import BOT_TOKEN, LOG_LEVEL


async def main() -> None:
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    db.init()
    # apex_ai.db: создаём claimed_leads/subscriptions если ещё нет
    # (messages_corpus + FTS5 канонически создаёт парсер).
    await eng.init_apex_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    handlers.register(dp)

    # фоновый воркер drip-цепочки
    drip_task = asyncio.create_task(scheduler.run_drip_loop(bot))

    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        drip_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
