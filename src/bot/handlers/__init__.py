"""Регистрация всех роутеров."""
from aiogram import Dispatcher

from . import admin, funnel, niche, review


def register(dp: Dispatcher) -> None:
    # niche до funnel: чтобы FSM-стейты ловились раньше общих text-хендлеров
    dp.include_router(niche.router)
    dp.include_router(funnel.router)
    dp.include_router(admin.router)
    dp.include_router(review.router)
