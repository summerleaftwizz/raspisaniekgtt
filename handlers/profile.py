"""Профиль, преподаватели, долги, напоминания, настройки и поддержка."""
import html
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config import settings
from db import SessionLocal
from db import crud
from handlers import menu_dispatch
from keyboards.groups import build_specialties_keyboard
from keyboards.menu import build_main_menu
from keyboards.profile import (
    build_debts_keyboard,
    build_profile_keyboard,
    build_reminders_keyboard,
    build_settings_keyboard,
    build_support_keyboard,
)
from services import formatting
from utils import now, parse_due_datetime

router = Router()
logger = logging.getLogger(__name__)

SUPPORT_TEXT = (
    "🛠 <b>Поддержка</b>\n\n"
    "🔹 Возникли вопросы?\n"
    "🔹 Есть предложения?\n\n"
    "Напишите в личные сообщения разработчику — @x6anax6\n\n"
    "Created with ❤️ love by @x6anax6"
)

SCHEDULE_INFO_TEXT = (
    "📖 <b>Расписание</b>\n\n"
    "Жми кнопки внизу:\n"
    "📅 <b>Сегодня</b> — пары на сегодня\n"
    "📅 <b>Завтра</b> — пары на завтра\n"
    "🔔 <b>Что сейчас</b> — идёт ли пара прямо сейчас\n\n"
    "Сменить группу можно в разделе 👤 <b>Профиль</b>."
)

GUIDE_TEXT = (
    "💻 <b>Программирование (код, ошибки, скрипты):</b>\n"
    "• Claude — хорошо объясняет сложные задачи/пишет коды почти на всех языках. "
    "Может сделать задание — делает файлы в эксель, ворд и т.д., но платный (нужен VPN).\n"
    "• DeepSeek Harness — почти такой же как Claude, дается пробный 1 доллар (VPN не требует).\n\n"
    "📝 <b>Тексты, сочинения, эссе:</b>\n"
    "• ChatGPT — универсал. Хорошо пишет тексты, но требует VPN.\n"
    "• Gemini — неплохо пишет на русском, работает через Google-аккаунт.\n\n"
    "📊 <b>Математика и анализ данных:</b>\n"
    "• DeepSeek — отлично решает задачи по математике и статистике.\n"
    "• Qwen — хорошо справляется с формулами и таблицами.\n\n"
    "🎨 <b>Картинки и дизайн:</b>\n"
    "• ChatGPT — генерирует картинки по описанию.\n"
    "• Gemini — умеет генерировать изображения.\n\n"
    "🌐 <b>Перевод и работа с английским:</b>\n"
    "• DeepSeek — переводит хорошо и бесплатно.\n"
    "• Qwen — специализируется на китайском и английском.\n\n"
    "📚 <b>Учеба и объяснения:</b>\n"
    "• Claude — лучше всех объясняет сложные темы простыми словами.\n"
    "• Gemini — хорошо ищет информацию в интернете.\n\n"
    "━━━━━━━━━━━━━━\n\n"
    "💡 <b>Совет:</b> Если не знаешь, какую выбрать — начни с DeepSeek. "
    "Он бесплатный, работает без VPN и хорошо справляется с большинством задач.\n\n"
    "Советую также от себя Gemini, в лс дам рабочий DNS, чтоб работал без впн.\n"
    "Найти меня можно в поддержке.\n\n"
    "С любовью @x6anax6 ❤️"
)


class ProfileStates(StatesGroup):
    debt_text = State()
    deadline_text = State()
    deadline_when = State()
    support_report = State()


def _esc(value) -> str:
    return html.escape(str(value or ""), quote=False)


async def _profile_text(telegram_id: int, username: str | None) -> str:
    async with SessionLocal() as session:
        user = await crud.get_or_create_user(session, telegram_id, username=username)
        group = user.group
        subscribed = user.subscribed
        is_teacher = user.is_teacher
        teacher_name = user.teacher_name
        await session.commit()
    return formatting.format_profile(
        username,
        group,
        subscribed,
        is_teacher=is_teacher,
        teacher_name=teacher_name,
    )


async def _user_role(telegram_id: int) -> bool:
    async with SessionLocal() as session:
        user = await crud.get_user(session, telegram_id)
    return bool(user and user.is_teacher)


async def _answer_tracked(
    message: Message,
    text: str,
    *,
    reply_markup=None,
    parse_mode: str = "HTML",
    menu: bool = True,
) -> None:
    """Ответ с прикреплением Reply-меню, если не задана inline-клавиатура."""
    await message.answer(
        text,
        parse_mode=parse_mode,
        reply_markup=reply_markup if reply_markup is not None else (
            build_main_menu() if menu else None
        ),
    )


async def _refresh_profile(message: Message, telegram_id: int, username: str | None) -> None:
    text = await _profile_text(telegram_id, username)
    is_teacher = await _user_role(telegram_id)
    await _answer_tracked(
        message,
        text,
        reply_markup=build_profile_keyboard(is_teacher),
    )


async def _send_debts(message: Message, telegram_id: int) -> None:
    async with SessionLocal() as session:
        debts = await crud.list_debts(session, telegram_id)

    if not debts:
        text = "📝 <b>Мои долги</b>\n\nДолгов нет 🎉"
        await _answer_tracked(message, text, reply_markup=build_debts_keyboard([]))
        return

    lines = ["📝 <b>Мои долги</b>\n"]
    for i, d in enumerate(debts, 1):
        lines.append(f"{i}. {_esc(d.text)}")
    await _answer_tracked(
        message,
        "\n".join(lines),
        reply_markup=build_debts_keyboard(debts),
    )


async def _send_reminders(message: Message, telegram_id: int) -> None:
    async with SessionLocal() as session:
        deadlines = await crud.list_deadlines(session, telegram_id)

    if not deadlines:
        text = "⏰ <b>Напоминания</b>\n\nАктивных напоминаний нет."
        await _answer_tracked(message, text, reply_markup=build_reminders_keyboard([]))
        return

    lines = ["⏰ <b>Мои напоминания</b>\n"]
    for i, d in enumerate(deadlines, 1):
        when = datetime.fromtimestamp(d.due_ts, tz=now().tzinfo).strftime("%d.%m.%Y %H:%M")
        lines.append(f"{i}. {when} — {_esc(d.text)}")
    await _answer_tracked(
        message,
        "\n".join(lines),
        reply_markup=build_reminders_keyboard(deadlines),
    )


# --- кнопки главного меню --------------------------------------------------


@router.message(F.text == "👤 Профиль")
async def btn_profile(message: Message) -> None:
    try:
        await _refresh_profile(message, message.from_user.id, message.from_user.username)
    except Exception:
        logger.exception("не удалось показать профиль для %s", message.from_user.id)
        await message.answer(
            "⚠️ Не удалось открыть профиль. Попробуй ещё раз: /profile",
            reply_markup=build_main_menu(),
        )


@router.message(F.text == "🛠 Поддержка")
async def btn_support(message: Message) -> None:
    await message.answer(SUPPORT_TEXT, reply_markup=build_support_keyboard())


@router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    try:
        await _refresh_profile(message, message.from_user.id, message.from_user.username)
    except Exception:
        logger.exception("не удалось показать профиль для %s", message.from_user.id)
        await message.answer(
            "⚠️ Не удалось открыть профиль. Попробуй ещё раз: /profile",
            reply_markup=build_main_menu(),
        )


@router.message(Command("support"))
async def cmd_support(message: Message) -> None:
    await message.answer(SUPPORT_TEXT, reply_markup=build_support_keyboard())


# --- поддержка: сообщить о проблеме ----------------------------------------


@router.callback_query(F.data == "sup:report")
async def cb_sup_report(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileStates.support_report)
    await callback.answer()
    await callback.message.answer(
        "Опиши проблему одним сообщением:", reply_markup=build_main_menu()
    )


@router.message(ProfileStates.support_report)
async def on_support_report(message: Message, state: FSMContext, bot) -> None:
    if await menu_dispatch.cancel_fsm_on_menu(message, state):
        return
    text = (message.text or "").strip()
    await state.clear()
    if not text:
        await message.answer(
            "Пустой текст. Попробуй ещё раз: /support", reply_markup=build_main_menu()
        )
        return

    user = message.from_user
    uname = f"@{_esc(user.username)}" if user.username else "—"
    report = (
        "⚠️ <b>Жалоба/проблема</b>\n\n"
        f"От: {uname} (<code>{user.id}</code>)\n\n"
        f"{_esc(text)}"
    )
    if settings.ADMIN_ID:
        try:
            await bot.send_message(
                settings.ADMIN_ID, report, reply_markup=build_main_menu()
            )
        except Exception:
            await message.answer(
                "Не удалось переслать сообщение админу. Попробуй позже.",
                reply_markup=build_main_menu(),
            )
            return
    await message.answer(
        "✅ Сообщение переслано админу. Спасибо!", reply_markup=build_main_menu()
    )


# --- профиль: inline-кнопки ------------------------------------------------


@router.callback_query(F.data == "prof:group")
async def cb_profile_group(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(
        "Выбери специальность 👇", reply_markup=build_specialties_keyboard()
    )


@router.callback_query(F.data == "prof:student")
async def cb_profile_student(callback: CallbackQuery) -> None:
    """Возврат из режима преподавателя в режим студента."""
    async with SessionLocal() as session:
        await crud.unset_teacher(session, callback.from_user.id)
        await session.commit()

    await callback.answer("Режим студента включён")
    await callback.message.answer(
        "👨‍🎓 Снова режим студента. Выбери специальность и группу 👇",
        reply_markup=build_specialties_keyboard(),
    )


@router.callback_query(F.data == "prof:back")
async def cb_profile_back(callback: CallbackQuery) -> None:
    await _refresh_profile(callback.message, callback.from_user.id, callback.from_user.username)
    await callback.answer()


@router.callback_query(F.data == "prof:settings")
async def cb_profile_settings(callback: CallbackQuery) -> None:
    async with SessionLocal() as session:
        user = await crud.get_or_create_user(
            session, callback.from_user.id, username=callback.from_user.username
        )
        subscribed = user.subscribed
        await session.commit()

    await callback.answer()
    await _answer_tracked(
        callback.message,
        _settings_text(subscribed),
        reply_markup=build_settings_keyboard(subscribed),
    )


def _settings_text(subscribed: bool) -> str:
    notify = "включены 🔔" if subscribed else "выключены 🔕"
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"🔔 Уведомления о расписании: {notify}"
    )


@router.callback_query(F.data == "set:notify")
async def cb_set_notify(callback: CallbackQuery) -> None:
    async with SessionLocal() as session:
        user = await crud.get_or_create_user(
            session, callback.from_user.id, username=callback.from_user.username
        )
        user.subscribed = not user.subscribed
        subscribed = user.subscribed
        await session.commit()

    await callback.answer()
    await _answer_tracked(
        callback.message,
        _settings_text(subscribed),
        reply_markup=build_settings_keyboard(subscribed),
    )


@router.callback_query(F.data == "set:guide")
async def cb_set_guide(callback: CallbackQuery) -> None:
    await callback.answer()
    await _answer_tracked(callback.message, GUIDE_TEXT)


# --- долги ------------------------------------------------------------------


@router.callback_query(F.data == "debt:list")
async def cb_debt_list(callback: CallbackQuery) -> None:
    await callback.answer()
    await _send_debts(callback.message, callback.from_user.id)


@router.callback_query(F.data == "debt:noop")
async def cb_debt_noop(callback: CallbackQuery) -> None:
    await callback.answer("Жми ✅, чтобы закрыть долг")


@router.callback_query(F.data == "debt:add")
async def cb_debt_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileStates.debt_text)
    await callback.answer()
    await callback.message.answer(
        "Введи текст долга (например, «Сдать лабу по физике»):",
        reply_markup=build_main_menu(),
    )


@router.message(ProfileStates.debt_text)
async def on_debt_text(message: Message, state: FSMContext) -> None:
    if await menu_dispatch.cancel_fsm_on_menu(message, state):
        return
    text = (message.text or "").strip()
    await state.clear()
    if not text:
        await message.answer(
            "Пустой текст — долг не добавлен.", reply_markup=build_main_menu()
        )
        return

    async with SessionLocal() as session:
        await crud.add_debt(session, message.from_user.id, text[:512])
        await session.commit()

    await _answer_tracked(message, f"✅ Долг добавлен: {_esc(text)}")
    await _send_debts(message, message.from_user.id)


@router.callback_query(F.data.startswith("debt:del:"))
async def cb_debt_del(callback: CallbackQuery) -> None:
    try:
        debt_id = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Неверный долг", show_alert=True)
        return

    async with SessionLocal() as session:
        deleted = await crud.delete_debt(session, callback.from_user.id, debt_id)
        await session.commit()

    if not deleted:
        await callback.answer("Долг не найден", show_alert=True)
        return

    await callback.answer("Долг закрыт ✅")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _send_debts(callback.message, callback.from_user.id)


# --- напоминания -----------------------------------------------------------


@router.callback_query(F.data == "dl:list")
async def cb_dl_list(callback: CallbackQuery) -> None:
    await callback.answer()
    await _send_reminders(callback.message, callback.from_user.id)


@router.callback_query(F.data == "dl:add")
async def cb_dl_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfileStates.deadline_text)
    await callback.answer()
    await callback.message.answer(
        "Введи текст задачи (например, «Сдать лабу по физике»):",
        reply_markup=build_main_menu(),
    )


@router.message(ProfileStates.deadline_text)
async def on_dl_text(message: Message, state: FSMContext) -> None:
    if await menu_dispatch.cancel_fsm_on_menu(message, state):
        return
    text = (message.text or "").strip()
    if not text:
        await state.clear()
        await message.answer(
            "Пустой текст — напоминание не добавлено.", reply_markup=build_main_menu()
        )
        return
    await state.update_data(dl_text=text[:512])
    await state.set_state(ProfileStates.deadline_when)
    await message.answer(
        "Когда напоминание? Формат: <code>31.10.2026 18:00</code> (или <code>31.10 18:00</code>)",
        reply_markup=build_main_menu(),
    )


@router.message(ProfileStates.deadline_when)
async def on_dl_when(message: Message, state: FSMContext) -> None:
    if await menu_dispatch.cancel_fsm_on_menu(message, state):
        return
    raw = (message.text or "").strip()
    due = parse_due_datetime(raw)
    if due is None:
        await message.answer(
            "Не понял дату. Формат: <code>31.10.2026 18:00</code> (или <code>31.10 18:00</code>). Попробуй ещё раз.",
            reply_markup=build_main_menu(),
        )
        return

    data = await state.get_data()
    text = data.get("dl_text", "")
    await state.clear()

    async with SessionLocal() as session:
        await crud.add_deadline(session, message.from_user.id, text, int(due.timestamp()))
        await session.commit()

    when = due.strftime("%d.%m.%Y %H:%M")
    await message.answer(
        f"⏰ Напоминание добавлено: {when} — {_esc(text)}\nНапомню ровно в это время.",
        reply_markup=build_main_menu(),
    )
    await _send_reminders(message, message.from_user.id)
