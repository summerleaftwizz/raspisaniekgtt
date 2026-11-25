"""Кнопка «🔎 Другая группа»: просмотр расписания чужой группы.

Основная группа пользователя НЕ меняется: выбор группы происходит в FSM-состоянии
OtherStates.choosing, и в этом состоянии обработчик g: из handlers/other.py
перехватывает выбор ДО обработчика из start.py (роутер подключён раньше).
Выход — любая кнопка главного меню (cancel_fsm_on_menu).
"""
import html
import logging
from datetime import date, timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from db import SessionLocal
from db import crud
from handlers import menu_dispatch
from keyboards.groups import build_specialties_keyboard
from keyboards.menu import BTN_OTHER_GROUP, build_main_menu
from services import formatting, watcher
from utils import ddmm, now, today_ddmm

logger = logging.getLogger(__name__)
router = Router()


class OtherStates(StatesGroup):
    choosing = State()
    browsing = State()


def _other_nav_keyboard(day: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"og:day:{(day - timedelta(days=1)).isoformat()}"),
            InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"og:day:{(day + timedelta(days=1)).isoformat()}"),
        ],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="og:home")],
    ])


async def _other_day_text(group: str, day: date, bot) -> str:
    label = ddmm(day)
    async with SessionLocal() as session:
        entries = await crud.get_schedule(session, label, group)
    if not entries:
        try:
            await watcher.run_check(bot)
        except Exception:
            logger.exception("ошибка принудительной загрузки расписания другой группы")
        async with SessionLocal() as session:
            entries = await crud.get_schedule(session, label, group)
            known = await crud.list_known(session)
    else:
        async with SessionLocal() as session:
            known = await crud.list_known(session)

    if entries:
        return formatting.format_schedule(
            label, group, entries, now=now() if day == now().date() else None,
            with_state=False, empty_text="Пар нет 🎉",
        )
    known_dates = {watcher._date_from_filename(item.date_folder) for item in known}
    if label in known_dates:
        return f"📅 <b>Расписание на {label}</b>\n👥 {html.escape(group, quote=False)}\n\nПар нет 🎉"
    return f"📅 <b>Расписание на {label}</b>\n👥 {html.escape(group, quote=False)}\n\nРасписание на этот день не загружено."


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
    await state.update_data(other_group=group, other_date=now().date().isoformat())
    await state.set_state(OtherStates.browsing)
    await callback.answer(f"Просмотр: {group}")
    day = now().date()
    text = await _other_day_text(group, day, bot)
    if callback.message:
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_other_nav_keyboard(day))
        except Exception:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=_other_nav_keyboard(day))


@router.callback_query(OtherStates.browsing, F.data.startswith("og:day:"))
async def cb_other_day(callback: CallbackQuery, state: FSMContext, bot) -> None:
    try:
        day = date.fromisoformat(callback.data.removeprefix("og:day:"))
    except ValueError:
        await callback.answer("Не удалось определить дату", show_alert=True)
        return
    data = await state.get_data()
    group = data.get("other_group")
    if not group:
        await callback.answer("Сначала выбери группу", show_alert=True)
        await state.clear()
        return
    await callback.answer()
    text = await _other_day_text(group, day, bot)
    if callback.message:
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_other_nav_keyboard(day))
        except Exception:
            pass


@router.callback_query(OtherStates.browsing, F.data == "og:home")
async def cb_other_home(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer("Главное меню 👇", reply_markup=build_main_menu())


@router.message(OtherStates.choosing)
@router.message(OtherStates.browsing)
async def other_state_message(message: Message, state: FSMContext) -> None:
    """Любой текст в режиме просмотра: кнопка меню — выйти и выполнить её."""
    if await menu_dispatch.cancel_fsm_on_menu(message, state):
        return
    await message.answer(
        "Выбери специальность из списка 👇",
        reply_markup=build_specialties_keyboard(),
    )
