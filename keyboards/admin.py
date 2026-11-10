"""Клавиатуры админ-панели."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def build_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm:broadcast")],
            [InlineKeyboardButton(text="👥 Кто в группе", callback_data="adm:groupmembers")],
            [InlineKeyboardButton(text="📊 Статистика групп", callback_data="adm:stats")],
        ]
    )
