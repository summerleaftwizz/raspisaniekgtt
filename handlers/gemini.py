"""Хендлер Gemini: /gemini, обычный текст и фото — с памятью диалога.

Логика:
* /gemini <вопрос> — разовый запрос (или подсказка, если без текста).
* Текст в режиме Gemini — продолжение диалога с памятью (последние 10 сообщений).
* Фото (с подписью или без) — мультимодальный запрос: картинка + подпись.
* Кнопки управления: «🧹 Очистить историю» и «🚪 Выключить ИИ».
* Нажатие любой кнопки главного меню (Сегодня/Завтра и т.д.) выходит
  из режима ИИ и выполняет действие этой кнопки.

Ответы Gemini приводятся к HTML (clean_markdown) и шлются с parse_mode="HTML".
"""
import contextlib
import logging
import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from db import SessionLocal
from db import crud
from keyboards.menu import (
    BTN_BELLS,
    BTN_NOW,
    BTN_OTHER_GROUP,
    BTN_PROFILE,
    BTN_SUPPORT,
    BTN_TEACHERS,
    BTN_TODAY,
    BTN_TOMORROW,
    BTN_WEEK,
    MENU_BUTTONS,
    build_main_menu,
)
from services import formatting
from services.gemini_client import (
    HISTORY_LIMIT,
    GeminiError,
    GeminiLimitError,
    gemini_client,
)

logger = logging.getLogger(__name__)
router = Router()

# --- кнопки управления режимом ИИ ------------------------------------------
BTN_GEM_CLEAR = "🧹 Очистить историю"
BTN_GEM_OFF = "🚪 Выключить ИИ"

GEMINI_HELP = (
    "🤖 <b>Gemini на связи</b>\n\n"
    "Просто напиши вопрос — отвечу с учётом нашей переписки.\n"
    "Можно прислать <b>фото</b> (с подписью или без) — разберу, что на нём.\n\n"
    "Кнопки внизу:\n"
    f"{BTN_GEM_CLEAR} — очистить историю\n"
    f"{BTN_GEM_OFF} — выйти из ИИ"
)


class GeminiStates(StatesGroup):
    active = State()


# ------------------------------------------------------- clean_markdown ----


def clean_markdown(text: str) -> str:
    """Приводит ответ Gemini (Markdown) к безопасному HTML.

    * **текст** -> <b>текст</b> (жирный шрифт)
    * $ вокруг формул удаляются (просто убираем символы $)
    * лишние символы \\ и _ убираются (Markdown-экранирование)
    * амперсанды/уголки экранируются, чтобы не сломать HTML
    """
    text = (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # **жирный** -> <b>жирный</b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # одиночные *курсив* -> <i>курсив</i> (тоже распространено в ответах)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", text)
    # убираем символы формул, экранирование Markdown
    text = text.replace("$", "").replace("\\", "").replace("_", "")
    return text


# ----------------------------------------------------------- клавиатура ----


def build_gemini_keyboard() -> ReplyKeyboardMarkup:
    """Reply-клавиатура режима ИИ: управление + основные кнопки меню.

    Основные кнопки оставлены, чтобы пользователь мог в любой момент
    переключиться на расписание — при этом режим ИИ сбросится.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_GEM_CLEAR), KeyboardButton(text=BTN_GEM_OFF)],
            [
                KeyboardButton(text=BTN_TODAY),
                KeyboardButton(text=BTN_TOMORROW),
                KeyboardButton(text=BTN_WEEK),
            ],
            [KeyboardButton(text=BTN_NOW), KeyboardButton(text=BTN_OTHER_GROUP)],
            [KeyboardButton(text=BTN_TEACHERS)],
            [
                KeyboardButton(text=BTN_PROFILE),
                KeyboardButton(text=BTN_BELLS),
                KeyboardButton(text=BTN_SUPPORT),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Спроси ИИ или выбери действие…",
    )


# ------------------------------------------------------------- вспомогательное


async def _ask_gemini(message: Message, prompt: str, image_bytes: bytes | None = None) -> None:
    """Общий путь: история -> запрос -> ответ -> сохранение в историю.

    Любая ошибка внутри обрабатывается здесь: бот не падает и продолжает
    обслуживать следующие сообщения.
    """
    user_id = message.from_user.id

    try:
        async with SessionLocal() as session:
            history = await crud.get_history(session, user_id, HISTORY_LIMIT)

        if not gemini_client.api_key:
            await message.answer(
                "⚠️ Gemini не настроен: добавь <code>GEMINI_API_KEY</code> в .env "
                "(ключ берётся на https://aistudio.google.com/apikey).",
                reply_markup=build_main_menu(),
            )
            return

        await message.bot.send_chat_action(message.chat.id, "typing")
        try:
            answer = await gemini_client.generate(prompt, image_bytes, history=history)
        except GeminiLimitError:
            # 429/503: лимит исчерпан или сервер перегружен — вежливо и без падения.
            logger.warning("Gemini: лимит/перегрузка для user %s", user_id)
            await message.answer(
                "😔 Извините, лимит запросов к ИИ на сегодня исчерпан "
                "(или сервер перегружен). Попробуйте позже или используйте "
                "обычные команды расписания.",
                reply_markup=build_gemini_keyboard(),
            )
            return
        except GeminiError as exc:
            logger.warning("Gemini недоступен: %s", exc)
            await message.answer(
                f"⚠️ Не удалось получить ответ от Gemini.\n"
                f"<code>{formatting.esc(str(exc))[:300]}</code>",
                reply_markup=build_gemini_keyboard(),
            )
            return

        # В историю сохраняем исходный ответ (без правок разметки).
        user_text = prompt or ("[фото]" if image_bytes else "")
        async with SessionLocal() as session:
            await crud.add_history(session, user_id, "user", user_text)
            await crud.add_history(session, user_id, "model", answer)
            await session.commit()

        # В чат — чистый HTML-ответ.
        html_answer = clean_markdown(answer)
        for chunk in formatting.split_message(html_answer):
            await message.answer(
                chunk,
                parse_mode="HTML",
                reply_markup=build_gemini_keyboard(),
            )
    except Exception:
        # Последняя страховка: что бы ни случилось, бот остаётся на связи.
        logger.exception("непредвиденная ошибка в Gemini-хендлере")
        with contextlib.suppress(Exception):
            await message.answer(
                "⚠️ Что-то пошло не так. Попробуйте ещё раз позже.",
                reply_markup=build_gemini_keyboard(),
            )


# ------------------------------------------------------------------- команды


@router.message(Command("gemini"))
async def cmd_gemini(message: Message, state: FSMContext) -> None:
    """/gemini — включить режим; /gemini <вопрос> — сразу спросить."""
    await state.set_state(GeminiStates.active)
    prompt = (message.text or "").partition(" ")[2].strip()
    if not prompt:
        await message.answer(GEMINI_HELP, reply_markup=build_gemini_keyboard())
        return
    await _ask_gemini(message, prompt)


@router.message(Command("gemini_reset"))
async def cmd_gemini_reset(message: Message) -> None:
    """Очищает историю диалога с Gemini."""
    async with SessionLocal() as session:
        await crud.clear_history(session, message.from_user.id)
        await session.commit()
    await message.answer("🧹 История диалога очищена.", reply_markup=build_gemini_keyboard())


@router.message(Command("gemini_off"))
async def cmd_gemini_off(message: Message, state: FSMContext) -> None:
    """Выход из режима Gemini."""
    await state.clear()
    await message.answer(
        "👋 Режим Gemini выключен. Кнопки меню снова работают как обычно.",
        reply_markup=build_main_menu(),
    )


# ------------------------------------------------ кнопки управления режимом


@router.message(F.text == BTN_GEM_CLEAR)
async def btn_gem_clear(message: Message, state: FSMContext) -> None:
    """«🧹 Очистить историю» — сброс памяти диалога."""
    async with SessionLocal() as session:
        await crud.clear_history(session, message.from_user.id)
        await session.commit()
    # Не выходим из режима — остаёмся в ИИ.
    await message.answer(
        "🧹 История очищена!",
        reply_markup=build_gemini_keyboard(),
    )


@router.message(F.text == BTN_GEM_OFF)
async def btn_gem_off(message: Message, state: FSMContext) -> None:
    """«🚪 Выключить ИИ» — выход из FSM и возврат главной клавиатуры."""
    await state.clear()
    await message.answer(
        "🚪 ИИ выключен. Возвращаюсь в главное меню 👇",
        reply_markup=build_main_menu(),
    )


# ------------------------------------------------------- текст и фото в режиме


@router.message(GeminiStates.active, F.photo)
async def on_photo(message: Message) -> None:
    """Фото с подписью или без — отправляем картинку в Gemini."""
    photo = message.photo[-1]  # самое большое разрешение
    try:
        file = await message.bot.get_file(photo.file_id)
        buffer = await message.bot.download_file(file.file_path)
        image_bytes = buffer.read()
    except Exception:
        logger.exception("не удалось скачать фото")
        await message.answer(
            "⚠️ Не удалось скачать фото, попробуй ещё раз.",
            reply_markup=build_gemini_keyboard(),
        )
        return
    await _ask_gemini(message, (message.caption or "").strip(), image_bytes)


@router.message(GeminiStates.active, F.text)
async def on_text(message: Message, state: FSMContext) -> None:
    """Любой текст в режиме Gemini — вопрос к модели (кроме кнопок меню)."""
    text = (message.text or "").strip()
    if not text:
        return
    # Кнопки управления ИИ уже перехвачены отдельными хендлерами выше,
    # а кнопка главного меню — выходим из режима и отдаём управление боту.
    if text in MENU_BUTTONS:
        await state.clear()
        from handlers import menu_dispatch

        await menu_dispatch.dispatch_menu_button(message, state)
        return
    await _ask_gemini(message, text)
