"""Инлайн-клавиатура листалки расписания на неделю (Пн–Сб).

Кнопки:
  [◀️ Назад] [Вперед ▶️]  — соседний день; на границах кнопка не показывается
  [🏠 В меню]             — выйти из листалки

Callback data: 'wk:nav:<index>' и 'wk:home'.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Сколько дней в неделе показываем (Пн..Сб).
WEEK_LENGTH = 6


def build_week_nav_keyboard(index: int) -> InlineKeyboardMarkup:
    """Клавиатура листалки для дня с номером index (0 = понедельник)."""
    nav_row: list[InlineKeyboardButton] = []
    if index > 0:
        nav_row.append(
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"wk:nav:{index - 1}")
        )
    if index < WEEK_LENGTH - 1:
        nav_row.append(
            InlineKeyboardButton(text="Вперед ▶️", callback_data=f"wk:nav:{index + 1}")
        )
    rows = []
    if nav_row:
        rows.append(nav_row)
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="wk:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
