"""Диспетчер кнопок главного меню для отмены FSM-состояний.

Когда пользователь в состоянии ожидания ввода (добавление долга/напоминания,
жалоба, фамилия преподавателя, рассылка) нажимает кнопку Reply-клавиатуры, бот должен:
1) очистить состояние (state.clear()),
2) выполнить действие нажатой кнопки,
3) написать «Действие отменено».
"""
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

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


async def dispatch_menu_button(message: Message, state: FSMContext | None = None) -> None:
    """Выполняет действие кнопки главного меню по её тексту."""
    text = message.text or ""
    if text == BTN_TODAY:
        from handlers import schedule as schedule_handlers

        await schedule_handlers.btn_today(message, message.bot)
    elif text == BTN_TOMORROW:
        from handlers import schedule as schedule_handlers

        await schedule_handlers.btn_tomorrow(message, message.bot)
    elif text == BTN_WEEK:
        from handlers import schedule as schedule_handlers

        await schedule_handlers.btn_week(message)
    elif text == BTN_NOW:
        from handlers import schedule as schedule_handlers

        await schedule_handlers.btn_now(message, message.bot)
    elif text == BTN_OTHER_GROUP:
        from handlers import other as other_handlers

        await other_handlers.btn_other_group(message, state)
    elif text == BTN_TEACHERS:
        from handlers import teachers as teachers_handlers

        await teachers_handlers.btn_teachers(message, state)
    elif text == BTN_BELLS:
        from handlers import schedule as schedule_handlers

        await schedule_handlers.btn_bells(message)
    elif text == BTN_PROFILE:
        from handlers import profile as profile_handlers

        await profile_handlers.btn_profile(message)
    elif text == BTN_SUPPORT:
        from handlers import profile as profile_handlers

        await profile_handlers.btn_support(message)


async def cancel_fsm_on_menu(message: Message, state: FSMContext) -> bool:
    """Если текст сообщения — кнопка главного меню: отменяет FSM, выполняет
    действие кнопки и пишет «Действие отменено». Возвращает True, если так."""
    if not message.text or message.text not in MENU_BUTTONS:
        return False
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=build_main_menu())
    await dispatch_menu_button(message, state)
    return True