"""Старт бота, выбор группы в два шага и режим преподавателя."""
import html

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from db import SessionLocal
from db import crud
from handlers import menu_dispatch
from keyboards.groups import (
    build_specialties_keyboard,
    build_specialty_groups_keyboard,
    specialty_of,
)
from keyboards.menu import build_main_menu

router = Router()

WELCOME_TEXT = (
    "⭐ Расписание КГТТ\n\n"
    "Слежу за расписанием техникума и присылаю его, как только выложат.\n\n"
    "<blockquote>⚡ пары на сегодня и завтра в один тап\n"
    "⚠️ новое расписание и замены сразу уведомлением</blockquote>\n\n"
    "Выбери специальность или напиши цифру своей группы 👇"
)

TEACHER_MODE_TEXT = (
    "Режим преподавателя. Покажу ваши пары по всем группам и буду присылать их, "
    "как только выложат расписание."
)


class StartStates(StatesGroup):
    teacher_name = State()


def _esc(value) -> str:
    return html.escape(str(value or ""), quote=False)


async def _groups_for_specialty(spec: str) -> list[str]:
    async with SessionLocal() as session:
        groups = await crud.get_groups(session)
    return [g for g in groups if specialty_of(g) == spec]


def _specialties_text() -> str:
    return WELCOME_TEXT


async def _show_welcome(message: Message, telegram_id: int | None = None) -> None:
    """Стандартное приветствие: меню для выбравших группу, иначе специальности.

    telegram_id нужен явно для callback: у callback.message.from_user — сам бот.
    """
    uid = telegram_id if telegram_id is not None else message.from_user.id

    async with SessionLocal() as session:
        user = await crud.get_user(session, uid)

    if user is not None and (user.group or user.is_teacher):
        await message.answer(
            WELCOME_TEXT, parse_mode="HTML", reply_markup=build_main_menu()
        )
        return

    async with SessionLocal() as session:
        groups = await crud.get_groups(session)

    if not groups:
        await message.answer(
            "Расписание ещё не загружено. Как только появится первый файл — "
            "сможешь выбрать специальность и группу.",
            parse_mode="HTML",
            reply_markup=build_specialties_keyboard(),
        )
        return

    await message.answer(
        WELCOME_TEXT, parse_mode="HTML", reply_markup=build_specialties_keyboard()
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    async with SessionLocal() as session:
        await crud.get_or_create_user(
            session, message.from_user.id, username=message.from_user.username
        )
        await session.commit()

    await _show_welcome(message)


# --- шаг 1: специальность --------------------------------------------------


@router.callback_query(F.data.startswith("spec:"))
async def on_specialty(callback: CallbackQuery) -> None:
    value = callback.data[len("spec:"):]

    if value == "back":
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                _specialties_text(), reply_markup=build_specialties_keyboard()
            )
        return

    groups = await _groups_for_specialty(value)
    await callback.answer()
    if not callback.message:
        return
    if not groups:
        await callback.message.edit_text(
            f"По специальности <b>{_esc(value)}</b> пока нет групп.\n\nВыбери другую 👇",
            reply_markup=build_specialties_keyboard(),
        )
        return
    await callback.message.edit_text(
        f"<b>{_esc(value)}</b> — выбери группу 👇",
        reply_markup=build_specialty_groups_keyboard(value, groups),
    )


# --- шаг 2: группа ---------------------------------------------------------


@router.callback_query(F.data.startswith("g:"))
async def on_group_selected(callback: CallbackQuery) -> None:
    group = callback.data[len("g:"):]
    async with SessionLocal() as session:
        await crud.set_group(session, callback.from_user.id, group)
        user = await crud.get_user(session, callback.from_user.id)
        if user is not None:
            user.is_teacher = False
            if callback.from_user.username:
                user.username = callback.from_user.username
        await session.commit()

    await callback.answer("Группа сохранена")
    if callback.message:
        await callback.message.edit_text(
            f"Группа: <b>{_esc(group)}</b> ✅", reply_markup=None
        )
        await callback.message.answer(
            WELCOME_TEXT, parse_mode="HTML", reply_markup=build_main_menu()
        )


# --- режим преподавателя ---------------------------------------------------


@router.callback_query(F.data == "teacher:start")
async def on_teacher_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(StartStates.teacher_name)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Напиши свою фамилию (как в расписании), и я запомню её:",
            reply_markup=build_main_menu(),
        )


@router.message(StartStates.teacher_name)
async def on_teacher_name(message: Message, state: FSMContext) -> None:
    if await menu_dispatch.cancel_fsm_on_menu(message, state):
        return

    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer(
            "Фамилия слишком короткая. Напиши ещё раз:",
            reply_markup=build_main_menu(),
        )
        return

    await state.clear()
    async with SessionLocal() as session:
        await crud.set_teacher(session, message.from_user.id, name[:255])
        user = await crud.get_user(session, message.from_user.id)
        if user is not None and message.from_user.username:
            user.username = message.from_user.username
        await session.commit()

    await message.answer(TEACHER_MODE_TEXT, reply_markup=build_main_menu())
