"""Клавиатуры профиля, долгов, напоминаний, преподавателей и настроек."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def build_profile_keyboard(is_teacher: bool = False) -> InlineKeyboardMarkup:
    if is_teacher:
        role_row = [InlineKeyboardButton(text="👨‍🎓 Я студент", callback_data="prof:student")]
    else:
        role_row = [InlineKeyboardButton(text="👥 Сменить группу", callback_data="prof:group")]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            role_row,
            [
                InlineKeyboardButton(text="📝 Мои долги", callback_data="debt:list"),
                InlineKeyboardButton(text="⏰ Напоминания", callback_data="dl:list"),
            ],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="prof:settings")],
        ]
    )


def build_debts_keyboard(debts) -> InlineKeyboardMarkup:
    """Список долгов: ✅ рядом с каждым + «Добавить долг»."""
    rows = [
        [
            InlineKeyboardButton(text="✅", callback_data=f"debt:del:{d.id}"),
            InlineKeyboardButton(text=d.text[:40], callback_data="debt:noop"),
        ]
        for d in debts
    ]
    rows.append([InlineKeyboardButton(text="➕ Добавить долг", callback_data="debt:add")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад в профиль", callback_data="prof:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_reminders_keyboard(deadlines) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="➕ Добавить напоминание", callback_data="dl:add")]]
    rows.append([InlineKeyboardButton(text="⬅️ Назад в профиль", callback_data="prof:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_settings_keyboard(subscribed: bool) -> InlineKeyboardMarkup:
    notify = "🔔 Уведомления: включены" if subscribed else "🔕 Уведомления: выключены"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=notify, callback_data="set:notify")],
            [InlineKeyboardButton(text="🤫 Секретный гайд", callback_data="set:guide")],
            [InlineKeyboardButton(text="⬅️ Назад в профиль", callback_data="prof:back")],
        ]
    )


def build_support_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚠️ Сообщить о проблеме", callback_data="sup:report")],
        ]
    )
