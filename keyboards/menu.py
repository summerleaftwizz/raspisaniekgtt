"""Главное меню бота: постоянная (закреплённая) Reply-клавиатура.

Раскладка:
  [📅 Сегодня] [📅 Завтра] [🗓 На неделю]
  [🔔 Что сейчас] [🔎 Другая группа] [👨‍🏫 Преподаватели]
  [👤 Профиль] [📞 Звонки] [🛠 Поддержка]

Кнопки реагируют на ТЕКСТ сообщения, обработчики вешаются на F.text == BTN_*.
"""
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

BTN_TODAY = "📅 Сегодня"
BTN_TOMORROW = "📅 Завтра"
BTN_WEEK = "🗓 На неделю"
BTN_NOW = "🔔 Что сейчас"
BTN_OTHER_GROUP = "🔎 Другая группа"
BTN_TEACHERS = "👨‍🏫 Преподаватели"
BTN_PROFILE = "👤 Профиль"
BTN_BELLS = "📞 Звонки"
BTN_SUPPORT = "🛠 Поддержка"

MENU_BUTTONS = (
    BTN_TODAY,
    BTN_TOMORROW,
    BTN_WEEK,
    BTN_NOW,
    BTN_OTHER_GROUP,
    BTN_TEACHERS,
    BTN_PROFILE,
    BTN_BELLS,
    BTN_SUPPORT,
)


def build_main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=BTN_TODAY),
                KeyboardButton(text=BTN_TOMORROW),
                KeyboardButton(text=BTN_WEEK),
            ],
            [
                KeyboardButton(text=BTN_NOW),
                KeyboardButton(text=BTN_OTHER_GROUP),
                KeyboardButton(text=BTN_TEACHERS),
            ],
            [
                KeyboardButton(text=BTN_PROFILE),
                KeyboardButton(text=BTN_BELLS),
                KeyboardButton(text=BTN_SUPPORT),            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие…",
    )
