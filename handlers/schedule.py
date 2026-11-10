"""Расписание: /today /tomorrow /week, кнопки меню и расписание звонков."""
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from db import SessionLocal
from db import crud
from aiogram.types import CallbackQuery
from keyboards.groups import build_specialties_keyboard
from keyboards.menu import (
    BTN_BELLS,
    BTN_NOW,
    BTN_TODAY,
    BTN_TOMORROW,
    BTN_WEEK,
    build_main_menu,
)
from keyboards.week import WEEK_LENGTH, build_week_nav_keyboard
from services import formatting, watcher
from utils import now, today_ddmm, tomorrow_ddmm, week_days

logger = logging.getLogger(__name__)
router = Router()

NOT_LOADED_TEXT = "📅 Расписание на эту дату пока не загружено. Загляни позже! ⏳"


async def _require_group(message: Message) -> str | None:
    async with SessionLocal() as session:
        user = await crud.get_user(session, message.from_user.id)
    if not user or (not user.group and not user.is_teacher):
        await message.answer(
            "Сначала выбери группу: /start", reply_markup=build_main_menu()
        )
        return None
    return user.group


async def _send(message: Message, text: str, *, menu: bool = True) -> None:
    """Отправка текста с закреплением Reply-меню."""
    for chunk in formatting.split_message(text):
        await message.answer(
            chunk,
            parse_mode="HTML",
            reply_markup=build_main_menu() if menu else None,
        )


async def _load(date: str, group: str):
    async with SessionLocal() as session:
        return await crud.get_schedule(session, date, group)


async def _load_teacher(date: str, teacher_name: str):
    async with SessionLocal() as session:
        return await crud.get_teacher_schedule(session, date, teacher_name)


async def _force_load_if_empty(label: str, group: str, bot: Bot):
    entries = await _load(label, group)
    if not entries:
        try:
            await watcher.run_check(bot)
        except Exception:
            logger.exception("ошибка принудительной загрузки расписания")
        entries = await _load(label, group)
    return entries


async def _force_load_teacher(label: str, teacher_name: str, bot: Bot):
    entries = await _load_teacher(label, teacher_name)
    if not entries:
        try:
            await watcher.run_check(bot)
        except Exception:
            logger.exception("ошибка принудительной загрузки расписания")
        entries = await _load_teacher(label, teacher_name)
    return entries


async def _known_dates() -> set[str]:
    """Даты 'DD.MM', файлы которых уже обработаны (есть в known_schedules)."""
    async with SessionLocal() as session:
        known = await crud.list_known(session)
    result: set[str] = set()
    for k in known:
        d = watcher._date_from_filename(k.date_folder)
        if d:
            result.add(d)
    return result


async def _day_text(message: Message, bot: Bot, label: str, empty_text: str, *, with_state: bool) -> str | None:
    """Текст расписания на день для студента или преподавателя."""
    async with SessionLocal() as session:
        user = await crud.get_user(session, message.from_user.id)
    if user is None or (not user.group and not user.is_teacher):
        await message.answer(
            "Сначала выбери группу: /start", reply_markup=build_main_menu()
        )
        return None

    if user.is_teacher:
        name = user.teacher_name or ""
        entries = await _force_load_teacher(label, name, bot)
        if label not in await _known_dates():
            return NOT_LOADED_TEXT
        return formatting.format_schedule(
            label,
            name,
            entries,
            now=now(),
            with_state=with_state,
            empty_text=empty_text,
            teacher_mode=True,
        )

    entries = await _force_load_if_empty(label, user.group, bot)
    if label not in await _known_dates():
        return NOT_LOADED_TEXT
    return formatting.format_schedule(
        label, user.group, entries, now=now(), with_state=with_state, empty_text=empty_text
    )


async def _answer_day(message: Message, bot: Bot, label: str, empty_text: str, *, with_state: bool) -> None:
    text = await _day_text(message, bot, label, empty_text, with_state=with_state)
    if text is not None:
        await _send(message, text, menu=True)


# --- команды ---------------------------------------------------------------


@router.message(Command("today"))
async def cmd_today(message: Message, bot: Bot) -> None:
    await _answer_day(message, bot, today_ddmm(), "Сегодня пар нет 🎉", with_state=True)


@router.message(Command("tomorrow"))
async def cmd_tomorrow(message: Message, bot: Bot) -> None:
    await _answer_day(message, bot, tomorrow_ddmm(), "Завтра пар нет 🎉", with_state=False)


async def _week_day_text(user, label: str, bot: Bot, known: set[str]) -> str:
    """Красивый текст одного дня недели (как у кнопки «Сегодня»).

    Пустой день, у которого есть файл в known_schedules, — «Пар нет 🎉».
    Если данных вообще нет — сообщение о том, что день не загружен.
    """
    if user.is_teacher:
        title = user.teacher_name or ""
        entries = await _load_teacher(label, title)
    else:
        title = user.group or ""
        entries = await _load(label, title)

    if not entries:
        # Возможно, расписание ещё не скачано — пробуем принудительно (раз).
        try:
            await watcher.run_check(bot)
        except Exception:
            logger.exception("ошибка принудительной загрузки расписания")
        if user.is_teacher:
            entries = await _load_teacher(label, title)
        else:
            entries = await _load(label, title)

    if entries:
        return formatting.format_schedule(
            label,
            title,
            entries,
            now=now(),
            with_state=False,
            empty_text="Пар нет 🎉",
            teacher_mode=bool(user.is_teacher),
        )
    if label in known:
        return f"📅 <b>{title} — {label}</b>\n\nПар нет 🎉"
    return f"📅 <b>{title} — {label}</b>\n\nРасписание на этот день не загружено."


async def send_week(message: Message, bot: Bot) -> None:
    """Кнопка «🗓 На неделю»: расписание на ТЕКУЩИЙ день + листалка Пн–Сб.

    Под сообщением — инлайн-кнопки «◀️ Назад» / «Вперед ▶️» (листание
    в пределах Пн..Сб текущей недели, на границах кнопка исчезает)
    и «🏠 В меню» для выхода.
    """
    async with SessionLocal() as session:
        user = await crud.get_user(session, message.from_user.id)
    if user is None or (not user.group and not user.is_teacher):
        await message.answer(
            "Сначала выбери группу: /start", reply_markup=build_main_menu()
        )
        return

    days = week_days()
    known = await _known_dates()
    # Стартуем с текущего дня (если сегодня воскресенье — с понедельника).
    index = next((i for i, (_n, lbl) in enumerate(days) if lbl == today_ddmm()), 0)
    text = await _week_day_text(user, days[index][1], bot, known)
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=build_week_nav_keyboard(index),
    )


async def _edit_week_day(callback: CallbackQuery, index: int) -> None:
    """Редактирует сообщение листалки на день index (с проверкой границ)."""
    index = max(0, min(WEEK_LENGTH - 1, index))
    async with SessionLocal() as session:
        user = await crud.get_user(session, callback.from_user.id)
    if user is None or (not user.group and not user.is_teacher):
        await callback.answer("Сначала выбери группу: /start", show_alert=True)
        return

    days = week_days()
    known = await _known_dates()
    text = await _week_day_text(user, days[index][1], callback.bot, known)
    await callback.answer()
    if callback.message:
        try:
            await callback.message.edit_text(
                text, parse_mode="HTML", reply_markup=build_week_nav_keyboard(index)
            )
        except Exception:
            # Текст не изменился (одинаковый день) — просто игнорируем.
            pass


@router.callback_query(F.data.startswith("wk:nav:"))
async def cb_week_nav(callback: CallbackQuery) -> None:
    """Листание: предыдущий/следующий день недели."""
    try:
        index = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    await _edit_week_day(callback, index)


@router.callback_query(F.data == "wk:home")
async def cb_week_home(callback: CallbackQuery) -> None:
    """«🏠 В меню»: убираем листалку и показываем обычное Reply-меню."""
    await callback.answer()
    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(
            "Главное меню 👇", reply_markup=build_main_menu()
        )


@router.message(Command("week"))
async def cmd_week(message: Message, bot: Bot) -> None:
    await send_week(message, bot)


# --- кнопки главного меню (реагируют на ТЕКСТ кнопки) -----------------------


@router.message(F.text == BTN_TODAY)
async def btn_today(message: Message, bot: Bot) -> None:
    await _answer_day(message, bot, today_ddmm(), "Сегодня пар нет 🎉", with_state=True)


@router.message(F.text == BTN_TOMORROW)
async def btn_tomorrow(message: Message, bot: Bot) -> None:
    await _answer_day(message, bot, tomorrow_ddmm(), "Завтра пар нет 🎉", with_state=False)


@router.message(F.text == BTN_WEEK)
async def btn_week(message: Message, bot: Bot) -> None:
    await send_week(message, bot)


@router.message(F.text == BTN_NOW)
async def btn_now(message: Message, bot: Bot) -> None:
    async with SessionLocal() as session:
        user = await crud.get_user(session, message.from_user.id)
    if user is None or (not user.group and not user.is_teacher):
        await message.answer(
            "Сначала выбери группу: /start", reply_markup=build_main_menu()
        )
        return

    label = today_ddmm()
    if user.is_teacher:
        name = user.teacher_name or ""
        entries = await _force_load_teacher(label, name, bot)
        text = formatting.format_what_now(label, name, entries, now=now(), teacher_mode=True)
    else:
        entries = await _force_load_if_empty(label, user.group, bot)
        text = formatting.format_what_now(label, user.group, entries, now=now())
    await _send(message, text, menu=True)


@router.message(F.text == BTN_BELLS)
async def btn_bells(message: Message) -> None:
    await _send(message, formatting.format_bells(), menu=True)


# --- change_group, подписка ------------------------------------------------


@router.message(Command("change_group"))
async def cmd_change_group(message: Message) -> None:
    await message.answer(
        "Выбери специальность 👇",
        reply_markup=build_specialties_keyboard(),
    )


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message) -> None:
    async with SessionLocal() as session:
        await crud.set_subscribed(session, message.from_user.id, True)
        await session.commit()
    await message.answer(
        "🔔 Готово! Теперь я буду присылать уведомление, когда появится новое расписание.",
        reply_markup=build_main_menu(),
    )


@router.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message) -> None:
    async with SessionLocal() as session:
        await crud.set_subscribed(session, message.from_user.id, False)
        await session.commit()
    await message.answer(
        "🔕 Уведомления о расписании выключены. "
        "Расписание можно посмотреть кнопками «Сегодня» и «Завтра».",
        reply_markup=build_main_menu(),
    )