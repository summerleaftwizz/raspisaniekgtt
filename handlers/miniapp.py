"""Telegram Mini App: кнопка в главном меню и inline-кнопка с web_app URL."""
import logging

from aiogram import F, Router
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from config import settings
from handlers import menu_dispatch
from keyboards.menu import BTN_MINIAPP, build_main_menu

logger = logging.getLogger(__name__)
router = Router()


def build_webapp_keyboard() -> InlineKeyboardMarkup:
    """Inline-клавиатура с кнопкой открытия Mini App."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📱 Открыть приложение",
                    web_app=WebAppInfo(url=settings.WEBAPP_URL),
                )
            ]
        ]
    )


@router.message(F.text == BTN_MINIAPP)
async def btn_miniapp(message: Message) -> None:
    if not settings.WEBAPP_URL:
        await message.answer(
            "📱 Mini App пока не настроен: задай WEBAPP_URL в .env "
            "(например, https://твой-домен.vercel.app).",
            reply_markup=build_main_menu(),
        )
        return
    await message.answer(
        "📱 <b>Расписание КГТТ</b>\n\n"
        "Открой приложение, чтобы листать расписание красиво:",
        reply_markup=build_webapp_keyboard(),
    )