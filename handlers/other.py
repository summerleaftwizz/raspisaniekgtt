"""Кнопка «🔎 Другая группа»: просмотр расписания чужой группы.

Основная группа пользователя НЕ меняется: выбор группы происходит в FSM-состоянии
OtherStates.choosing, и в этом состоянии обработчик g: из handlers/other.py
перехватывает выбор ДО обработчика из start.py (роутер подключён раньше).
Выход — любая кнопка главного меню (cancel_fsm_on_menu).
"""
import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from db import SessionLocal
from db import crud
from handlers import menu_dispatch
from keyboards.groups import build_specialties_keyboard
from keyboards.menu import BTN_OTHER_GROUP, build_main_menu
from services import formatting, watcher
from utils import today_ddmm

logger = logging.getLogger(__name__)
router = Router()


class OtherStates(StatesGroup):
    choosing = State()


@router.message(F.text == BTN_OTHER_GROUP)
async def btn_other_group(message: Message, state: FSMContext) -> None:
    await state.set_state(OtherStates.choosing)
    await message.answer(
        "🔎 Выбери специальность и группу, расписание которой хочешь посмотреть.\n"
        "<i>Твоя основная группа не изменится. Чтобы вернуться, нажми любую кнопку меню.</i>",
        parse_mode="HTML",
        reply_markup=build_specialties_keyboard(with_teacher=False),
    )


@router.callback_query(OtherStates.choosing, F.data.startswith("spec:"))
async def cb_other_spec(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 1 в режиме просмотра: тот же интерфейс специальностей, без сохранения."""
    value = callback.data[len("spec:"):]
    from keyboards.groups import build_specialty_groups_keyboard, specialty_of

    if value == "back":
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                "🔎 Выбери специальность 👇",
                reply_markup=build_specialties_keyboard(with_teacher=False),
            )
        return

    async with SessionLocal() as session:
        groups = await crud.get_groups(session)
    groups = [g for g in groups if specialty_of(g) == value]
    await callback.answer()
    if not callback.message:
        return
    if not groups:
        await callback.message.edit_text(
            f"По специальности <b>{html.escape(value, quote=False)}</b> пока нет групп.\n\n"
            "Выбери другую 👇",
            reply_markup=build_specialties_keyboard(with_teacher=False),
        )
        return
    await callback.message.edit_text(
        f"<b>{html.escape(value, quote=False)}</b> — выбери группу 👇",
        reply_markup=build_specialty_groups_keyboard(value, groups),
    )


@router.callback_query(OtherStates.choosing, F.data.startswith("g:"))
async def cb_other_group(callback: CallbackQuery, state: FSMContext, bot) -> None:
    """Шаг 2 в режиме просмотра: показываем расписание, НЕ сохраняя группу."""
    group = callback.data[len("g:"):]
    await state.update_data(other_group=group)
    await callback.answer(f"Просмотр: {group}")

    label = today_ddmm()
    async with SessionLocal() as session:
        entries = await crud.get_schedule(session, label, group)
    if not entries:
        try:
            await watcher.run_check(bot)
        except Exception:
            logger.exception("ошибка принудительной загрузки расписания")
        async with SessionLocal() as session:
            entries = await crud.get_schedule(session, label, group)

    if entries:
        text = formatting.format_schedule(
            label,
            group,
            entries,
            now=None,
            with_state=False,
            empty_text="Пар нет 🎉",
        )
    else:
        text = f"📅 <b>{html.escape(group, quote=False)} — {label}</b>\n\nРасписание не загружено."

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=None)
        except Exception:
            await callback.message.answer(text, reply_markup=None)
        # Тихо выходим из режима: расписание показано, ничего лишнего не шлём.
        await state.clear()


@router.message(OtherStates.choosing)
async def other_state_message(message: Message, state: FSMContext) -> None:
    """Любой текст в режиме просмотра: кнопка меню — выйти и выполнить её."""
    if await menu_dispatch.cancel_fsm_on_menu(message, state):
        return
    await message.answer(
        "Выбери специальность из списка 👇",
        reply_markup=build_specialties_keyboard(),
    )
