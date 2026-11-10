"""Кнопка «👨‍🏫 Преподаватели»: расписание преподавателя на СЕГОДНЯ.

Пользователь вводит фамилию, бот ищет все пары преподавателя на текущий
день и выводит в том же красивом формате, что и расписание группы
(🕓 время, 📚 предмет, 🚪 кабинет, группы), через formatting.format_schedule
с teacher_mode=True. Основная группа пользователя не меняется.
"""
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from db import SessionLocal
from db import crud
from handlers import menu_dispatch
from keyboards.menu import BTN_TEACHERS, build_main_menu
from services import formatting, watcher
from utils import now, today_ddmm

logger = logging.getLogger(__name__)
router = Router()

SEARCH_PROMPT = "Введите фамилию преподавателя для поиска расписания:"


class TeacherSearchStates(StatesGroup):
    waiting_name = State()


async def _teacher_day_text(name: str, bot) -> str:
    """Красивое расписание преподавателя на сегодня (как расписание группы)."""
    label = today_ddmm()
    async with SessionLocal() as session:
        entries = await crud.get_teacher_schedule(session, label, name)
    if not entries:
        # Возможно, расписание ещё не скачано — пробуем принудительно.
        try:
            await watcher.run_check(bot)
        except Exception:
            logger.exception("ошибка принудительной загрузки расписания")
        async with SessionLocal() as session:
            entries = await crud.get_teacher_schedule(session, label, name)

    return formatting.format_schedule(
        label,
        name,
        entries,
        now=now(),
        with_state=True,
        empty_text="Сегодня пар нет 🎉",
        teacher_mode=True,
    )


@router.message(F.text == BTN_TEACHERS)
async def btn_teachers(message: Message, state: FSMContext) -> None:
    await state.set_state(TeacherSearchStates.waiting_name)
    await message.answer(
        SEARCH_PROMPT,
        reply_markup=build_main_menu(),
    )


@router.message(TeacherSearchStates.waiting_name)
async def on_teacher_name(message: Message, state: FSMContext, bot) -> None:
    if await menu_dispatch.cancel_fsm_on_menu(message, state):
        return

    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer(
            "Фамилия слишком короткая. Введите ещё раз:",
            reply_markup=build_main_menu(),
        )
        return

    await state.clear()

    text = await _teacher_day_text(name, bot)
    from services.formatting import split_message

    for chunk in split_message(text):
        await message.answer(chunk, reply_markup=build_main_menu())
