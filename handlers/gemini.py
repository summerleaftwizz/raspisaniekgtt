"""Хендлер Gemini: /gemini, обычный текст и фото — с памятью диалога.

Логика:
* /gemini <вопрос> — разовый запрос (или подсказка, если без текста).
* Текст в режиме Gemini — продолжение диалога с памятью (последние 10 сообщений).
* Фото (с подписью или без) — мультимодальный запрос: картинка + подпись.
* /gemini_reset — очистить историю диалога.
* /gemini_off — выйти из режима Gemini (текст снова уходит другим хендлерам).

Режим хранится в users-таблице FSM? Нет — проще: флаг в FSM-состоянии
GeminiStates.active, чтобы не плодить поля в БД.
"""
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from db import SessionLocal
from db import crud
from keyboards.menu import MENU_BUTTONS, build_main_menu
from services import formatting
from services.gemini_client import HISTORY_LIMIT, GeminiError, gemini_client

logger = logging.getLogger(__name__)
router = Router()

GEMINI_HELP = (
    "🤖 <b>Gemini на связи</b>\n\n"
    "Просто напиши вопрос — отвечу с учётом нашей переписки.\n"
    "Можно прислать <b>фото</b> (с подписью или без) — разберу, что на нём.\n\n"
    "<i>Команды:</i>\n"
    "/gemini_reset — очистить историю диалога\n"
    "/gemini_off — выйти из режима Gemini"
)


class GeminiStates(StatesGroup):
    active = State()


# ------------------------------------------------------------- вспомогательное


async def _ask_gemini(message: Message, prompt: str, image_bytes: bytes | None = None) -> None:
    """Общий путь: история -> запрос -> ответ -> сохранение в историю."""
    user_id = message.from_user.id

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
    except GeminiError as exc:
        logger.warning("Gemini недоступен: %s", exc)
        await message.answer(
            f"⚠️ Не удалось получить ответ от Gemini.\n<code>{formatting.esc(str(exc))[:300]}</code>",
            reply_markup=build_main_menu(),
        )
        return

    # Сохраняем в историю: что спросили и что ответили.
    user_text = prompt or ("[фото]" if image_bytes else "")
    async with SessionLocal() as session:
        await crud.add_history(session, user_id, "user", user_text)
        await crud.add_history(session, user_id, "model", answer)
        await session.commit()

    for chunk in formatting.split_message(answer):
        await message.answer(chunk, reply_markup=build_main_menu())


# ------------------------------------------------------------------- команды


@router.message(Command("gemini"))
async def cmd_gemini(message: Message, state: FSMContext) -> None:
    """/gemini — включить режим; /gemini <вопрос> — сразу спросить."""
    await state.set_state(GeminiStates.active)
    prompt = (message.text or "").partition(" ")[2].strip()
    if not prompt:
        await message.answer(GEMINI_HELP, reply_markup=build_main_menu())
        return
    await _ask_gemini(message, prompt)


@router.message(Command("gemini_reset"))
async def cmd_gemini_reset(message: Message) -> None:
    """Очищает историю диалога с Gemini."""
    async with SessionLocal() as session:
        await crud.clear_history(session, message.from_user.id)
        await session.commit()
    await message.answer("🧹 История диалога очищена.", reply_markup=build_main_menu())


@router.message(Command("gemini_off"))
async def cmd_gemini_off(message: Message, state: FSMContext) -> None:
    """Выход из режима Gemini."""
    await state.clear()
    await message.answer(
        "👋 Режим Gemini выключен. Кнопки меню снова работают как обычно.",
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
            reply_markup=build_main_menu(),
        )
        return
    await _ask_gemini(message, (message.caption or "").strip(), image_bytes)


@router.message(GeminiStates.active, F.text)
async def on_text(message: Message, state: FSMContext) -> None:
    """Любой текст в режиме Gemini — вопрос к модели (кроме кнопок меню)."""
    text = (message.text or "").strip()
    if not text:
        return
    # Кнопка главного меню — выходим из режима и отдаём управление боту.
    if text in MENU_BUTTONS:
        await state.clear()
        from handlers import menu_dispatch

        await menu_dispatch.dispatch_menu_button(message, state)
        return
    await _ask_gemini(message, text)
