"""Фоновая задача: напоминания о сроках и праздничные поздравления.

Напоминания строго индивидуальны: уведомление уходит ровно тому telegram_id,
который его создал. Поздравления (Новый год, День студента, 23 февраля,
8 Марта, 1 сентября, Хэллоуин) уходят всем пользователям с включёнными
уведомлениями ровно один раз за год.
"""
import asyncio
import logging
import time

from aiogram import Bot

from config import settings
from db import SessionLocal
from db import crud
from utils import now

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 30

HOLIDAYS = {
    (1, 1): (
        "🎄 С Новым годом! Пусть новый год принесет только отличные оценки, "
        "легкие сессии и море позитива! 🎅"
    ),
    (1, 25): (
        "🎓 С Днем студента! Сдавай все зачеты вовремя, "
        "а сессия пусть будет самой легкой! 🍻"
    ),
    (2, 23): (
        "🎖 С 23 февраля! Желаем крепкого здоровья, силы духа "
        "и мирного неба над головой! 💪"
    ),
    (3, 8): (
        "🌷 С 8 Марта! Пусть весна приносит только радость, "
        "а учеба дается легко! 💐"
    ),
    (9, 1): (
        "📚 С 1 сентября! Пусть новый учебный год будет продуктивным, "
        "а пары пролетают незаметно! 🤓"
    ),
    (10, 31): (
        "🎃 <b>С Хэллоуином!</b>\n\n"
        "Желаем лёгких дверей, сладких пар и никаких долгов! 👻\n\n"
        "Created with ❤️ love by @x6anax6"
    ),
}


async def _send_safely(bot: Bot, telegram_id: int, text: str) -> bool:
    try:
        await bot.send_message(telegram_id, text)
        return True
    except Exception:
        logger.exception("не удалось отправить напоминание пользователю %s", telegram_id)
        return False


async def _check_deadlines(bot: Bot) -> None:
    now_ts = int(time.time())
    async with SessionLocal() as session:
        due = await crud.claim_due_deadlines(session, now_ts)
        await session.commit()

    for deadline in due:
        await _send_safely(
            bot, deadline.telegram_id, f"⏰ <b>Напоминание!</b>\n\n{deadline.text}"
        )


async def _check_holiday(bot: Bot) -> None:
    """Поздравления по праздникам — только в свой день, один раз за год,
    только тем, у кого включены уведомления."""
    today = now()
    text = HOLIDAYS.get((today.month, today.day))
    if text is None:
        return

    key = f"holiday:{today.year}-{today.month:02d}-{today.day:02d}"
    async with SessionLocal() as session:
        if await crud.get_known(session, key) is not None:
            return
        users = await crud.list_subscribed_users(session)

    sent = 0
    for user in users:
        if await _send_safely(bot, user.telegram_id, text):
            sent += 1
        await asyncio.sleep(settings.BROADCAST_DELAY_MS / 1000)

    # помечаем как отправленное только после попытки рассылки
    async with SessionLocal() as session:
        await crud.mark_known(session, key)
        await session.commit()
    logger.info("поздравление отправлено %d пользователям", sent)


async def reminders_loop(bot: Bot) -> None:
    """Основной цикл: каждые CHECK_INTERVAL_SECONDS секунд проверяет напоминания."""
    logger.info("старт цикла напоминаний")
    while True:
        try:
            await _check_deadlines(bot)
            await _check_holiday(bot)
        except Exception:
            logger.exception("ошибка в цикле напоминаний")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
