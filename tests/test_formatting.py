"""Тесты Этапа 2: сетки звонков, get_current_state, форматирование."""
import json
from datetime import datetime
from types import SimpleNamespace

from config import BELLS_SATURDAY, BELLS_WEEKDAY, bells_for_weekday
from services.formatting import (
    format_bells,
    format_profile,
    get_current_state,
    format_notification,
    format_schedule,
    format_what_now,
    split_message,
)


def entry(num, subject="Предмет", room="", teacher="", date="26.10", group=""):
    return SimpleNamespace(
        lesson_number=num, subject=subject, room=room, teacher=teacher, date=date, group=group
    )


# --- выбор сетки звонков ---------------------------------------------------

def test_bells_for_weekday():
    assert bells_for_weekday(0) is BELLS_WEEKDAY          # понедельник
    assert bells_for_weekday(4) is BELLS_WEEKDAY          # пятница
    assert bells_for_weekday(5) is BELLS_SATURDAY         # суббота
    assert bells_for_weekday(6) == {}                     # воскресенье


def test_saturday_grid_differs_from_weekday():
    assert BELLS_SATURDAY[2] == ("10:10", "11:40")
    assert BELLS_WEEKDAY[2] == ("10:20", "11:50")


def test_tomorrow_from_friday_uses_saturday_grid():
    # 23.10.2026 — пятница, 24.10.2026 — суббота (сетка субботы: 2 пара в 10:10)
    now = datetime(2026, 10, 23, 12, 0)
    text = format_schedule("24.10", "ГР-1", [entry(2, date="24.10")], now=now)
    assert "10:10 – 11:40" in text
    assert "10:20" not in text


# --- get_current_state ------------------------------------------------------

def test_state_ongoing():
    now = datetime(2026, 10, 26, 9, 0)  # понедельник, внутри 1 пары
    state = get_current_state([entry(1), entry(2)], now)
    assert state.kind == "ongoing"
    assert state.current.lesson_number == 1
    assert state.until == "10:00"
    assert state.next_lesson.lesson_number == 2


def test_state_break():
    now = datetime(2026, 10, 26, 10, 5)  # между 1 и 2 парами
    state = get_current_state([entry(1), entry(2)], now)
    assert state.kind == "break"
    assert state.until == "10:20"


def test_state_window():
    now = datetime(2026, 10, 26, 11, 0)  # слот 2 пары пуст, 3-я заполнена
    state = get_current_state([entry(1), entry(2, subject=""), entry(3)], now)
    assert state.kind == "window"
    assert state.next_lesson.lesson_number == 3


def test_state_before():
    now = datetime(2026, 10, 26, 7, 30)
    state = get_current_state([entry(1), entry(2)], now)
    assert state.kind == "before"
    assert state.next_lesson.lesson_number == 1


def test_state_after():
    now = datetime(2026, 10, 26, 20, 0)
    state = get_current_state([entry(1), entry(2)], now)
    assert state.kind == "after"


def test_state_empty():
    now = datetime(2026, 10, 26, 12, 0)
    assert get_current_state([], now).kind == "empty"
    assert get_current_state([entry(1, subject="")], now).kind == "empty"


# --- форматирование ---------------------------------------------------------

def test_format_html_escaping():
    text = format_schedule("26.10", "ГР<1>", [entry(1, subject="Мат<ем>атика", room="&304")],
                           now=datetime(2026, 10, 26, 12, 0))
    assert "Мат&lt;ем&gt;атика" in text
    assert "каб. &amp;304" in text
    assert "ГР&lt;1&gt;" in text
    assert "<em>" not in text


def test_format_blockquote_layout():
    """Студент: blockquote с 🕒/📚/🚪·👤."""
    text = format_schedule(
        "26.10", "ГР-1", [entry(1, room="304", teacher="Иванов И. И.")],
        now=datetime(2026, 10, 26, 12, 0),
    )
    assert "<blockquote>" in text and "</blockquote>" in text
    assert "🕒 <b>1 пара</b> · 08:30 – 10:00" in text
    assert "📚 Предмет" in text
    assert "🚪 каб. 304 · Иванов И. И." in text
    # без кабинета — 👤; без ФИО — 👤 нет
    text = format_schedule("26.10", "ГР-1", [entry(2, room="", teacher="Петров П. П.")],
                           now=datetime(2026, 10, 26, 12, 0))
    assert "👤 Петров П. П." in text
    text = format_schedule("26.10", "ГР-1", [entry(3, room="9", teacher="")],
                           now=datetime(2026, 10, 26, 12, 0))
    assert "🚪 каб. 9" in text and "👤" not in text


def test_format_teacher_mode():
    """Преподаватель: группа в строке предмета, ФИО не дублируется."""
    text = format_schedule(
        "26.10", "Иванов И. И.", [entry(1, room="304", teacher="Иванов И. И.", group="1ИСИП-25-9")],
        now=datetime(2026, 10, 26, 12, 0), teacher_mode=True,
    )
    assert "📚 1ИСИП-25-9 — Предмет" in text
    assert "🚪 каб. 304" in text
    assert "Иванов И. И. ·" not in text  # ФИО не в месте проведения
    assert "👤 Иванов И. И." not in text


def test_format_no_teacher_line_when_empty():
    text = format_schedule("26.10", "ГР-1", [entry(1, teacher="")],
                           now=datetime(2026, 10, 26, 12, 0))
    assert "👤" not in text
    text = format_schedule("26.10", "ГР-1", [entry(1, teacher="Иванов И. И.")],
                           now=datetime(2026, 10, 26, 12, 0))
    assert "👤 Иванов И. И." in text


def test_format_today_with_state_tomorrow_without():
    entries = [entry(1), entry(2, date="26.10")]
    text_today = format_schedule("26.10", "ГР-1", entries,
                                 now=datetime(2026, 10, 26, 9, 0), with_state=True)
    assert "Сейчас:" in text_today
    text_tomorrow = format_schedule("26.10", "ГР-1", entries,
                                    now=datetime(2026, 10, 26, 9, 0), with_state=False)
    assert "Сейчас:" not in text_tomorrow


def test_format_empty_pairs_shown_with_emoji():
    text = format_schedule(
        "26.10", "ГР-1",
        [entry(1, subject=""), entry(2, subject=""), entry(3)],
        now=datetime(2026, 10, 26, 12, 0),
    )
    assert "1️⃣ <i>пара — окно</i>" in text
    assert "2️⃣ <i>пара — окно</i>" in text
    assert "🕒 <b>3 пара</b>" in text
    assert "— нет" not in text
    assert "⏳" not in text


def test_format_max_room():
    text = format_schedule("26.10", "ГР-1", [entry(1, room="max", teacher="Иванов И. И.")],
                           now=datetime(2026, 10, 26, 12, 0))
    assert "🚪 пара в максе" in text
    assert "каб. max" not in text


def test_format_bells():
    text = format_bells()
    assert "Расписание звонков" in text
    assert "1 пара: 8:30–10:00" in text       # ведущий ноль срезан
    assert "2 пара: 10:20–11:50" in text
    assert "Суббота" in text and "2 пара: 10:10–11:40" in text
    assert "Воскресенье" in text


def test_format_profile_teacher_mode():
    text = format_profile("user1", None, True, is_teacher=True, teacher_name="Иванова А. А.")
    assert "Преподаватель" in text and "Иванова А. А." in text
    assert "Уч. группа" not in text
    text = format_profile("user1", "1ИСИП-25-9", False)
    assert "Студент" in text and "1ИСИП-25-9" in text
    assert "выключены" in text


def test_split_message_long():
    entries = [entry(1), entry(2)]
    text = format_notification("26.10", "ГР-1", entries, now=datetime(2026, 10, 26, 9, 0))
    assert "Начало занятий в 08:30, всего 2 пар" in text
    assert "Сейчас:" not in text


def test_what_now_states():
    # ongoing: 9:00 пн, 1 пара идёт
    text = format_what_now("26.10", "ГР-1", [entry(1), entry(2)],
                           now=datetime(2026, 10, 26, 9, 0))
    assert "Сейчас:</b> 1 пара" in text and "До конца 60 мин" in text
    # break: 10:05
    text = format_what_now("26.10", "ГР-1", [entry(1), entry(2)],
                           now=datetime(2026, 10, 26, 10, 5))
    assert "Перемена" in text and "Дальше: 2 пара" in text
    # before: 07:30
    text = format_what_now("26.10", "ГР-1", [entry(1)],
                           now=datetime(2026, 10, 26, 7, 30))
    assert "Занятия начнутся в 08:30" in text
    # after: 20:00
    text = format_what_now("26.10", "ГР-1", [entry(1)],
                           now=datetime(2026, 10, 26, 20, 0))
    assert "закончились" in text
    # empty
    text = format_what_now("26.10", "ГР-1", [], now=datetime(2026, 10, 26, 12, 0))
    assert "пар нет" in text


def test_what_now_max_room():
    text = format_what_now("26.10", "ГР-1", [entry(1, room="max", teacher="")],
                           now=datetime(2026, 10, 26, 9, 0))
    assert "Сейчас:</b> 1 пара — Предмет, пара в максе" in text


def test_split_message_long():
    entries = [entry(i, room=str(100 + i), teacher="Преподаватель") for i in range(1, 320)]
    text = format_schedule("26.10", "ГР-1", entries, now=datetime(2026, 10, 26, 12, 0))
    assert len(text) > 4096
    chunks = split_message(text)
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)
    assert "\n\n".join(chunks) == text  # ничего не обрезано и не потеряно


# --- подгруппы (английский и др.) ------------------------------------------


def test_subgroups_rendered_as_separate_lines():
    """2+ подгруппы: отдельные строки «N п/г · ауд. X · ФИО»."""
    e = entry(3, subject="Иностранный язык")
    e.subgroups = [{"number": 1, "room": "121", "teacher": "Платицына Е. В."},
                   {"number": 2, "room": "202", "teacher": "Байшева Я. В."}]
    text = format_schedule("26.10", "1ИСИП-25-9", [e])
    assert "📚 Иностранный язык" in text
    assert "1 п/г · ауд. 121 · Платицына Е. В." in text
    assert "2 п/г · ауд. 202 · Байшева Я. В." in text


def test_subgroups_dataclass_objects():
    """SubgroupInfo (датакласс) тоже печатается — как из парсера."""
    from services.parser import SubgroupInfo
    e = entry(3, subject="Иностранный язык")
    e.subgroups = [SubgroupInfo(1, "319", "Кобзова С. В."),
                   SubgroupInfo(2, "202", "Ощепкова А. С.")]
    text = format_schedule("26.10", "1ИСИП-25-9", [e])
    assert "1 п/г · ауд. 319 · Кобзова С. В." in text
    assert "2 п/г · ауд. 202 · Ощепкова А. С." in text


def test_subgroups_dirty_room_cells():
    """«Ино... 319» / «ауд. 202» приводятся к «ауд. 319» / «ауд. 202»."""
    e = entry(3, subject="Иностранный язык в профессиональной деятельности")
    e.subgroups = [{"number": 1, "room": "Ино... 319", "teacher": "Кобзова С. В."},
                   {"number": 2, "room": "ауд. 202", "teacher": "Ощепкова А. С."}]
    text = format_schedule("26.10", "1ИСИП-25-9", [e])
    assert "📚 Иностранный язык в профессиональной деятельности" in text
    assert "1 п/г · ауд. 319 · Кобзова С. В." in text
    assert "2 п/г · ауд. 202 · Ощепкова А. С." in text


def test_single_subgroup_uses_plain_format():
    """Одна подгруппа → обычный формат без «п/г» и без дублей."""
    e = entry(3, subject="Иностранный язык", room="319", teacher="Кобзова С. В.")
    e.subgroups = [{"number": 1, "room": "319", "teacher": "Кобзова С. В."}]
    text = format_schedule("26.10", "1ИСИП-25-9", [e])
    assert "п/г" not in text
    assert "🚪 каб. 319 · Кобзова С. В." in text


def test_subgroups_missing_teacher_part():
    """Нет преподавателя — строка без «None» и без висящего разделителя."""
    e = entry(3, subject="Иностранный язык")
    e.subgroups = [{"number": 1, "room": "319", "teacher": "Кобзова С. В."},
                   {"number": 2, "room": "202", "teacher": None}]
    text = format_schedule("26.10", "1ИСИП-25-9", [e])
    assert "2 п/г · ауд. 202" in text
    assert "None" not in text
    assert "2 п/г · ауд. 202 ·" not in text


def test_subgroups_json_string_from_db():
    """entry.subgroups приходит из БД JSON-строкой — парсится корректно."""
    e = entry(3, subject="Иностранный язык")
    e.subgroups = json.dumps(
        [{"number": 1, "room": "319", "teacher": "Кобзова С. В."},
         {"number": 2, "room": "202", "teacher": "Ощепкова А. С."}],
        ensure_ascii=False,
    )
    text = format_schedule("26.10", "1ИСИП-25-9", [e])
    assert "1 п/г · ауд. 319 · Кобзова С. В." in text
    assert "2 п/г · ауд. 202 · Ощепкова А. С." in text


def test_no_subgroups_field_is_ok():
    """Обычная пара без подгрупп — без строк «п/г»."""
    text = format_schedule("26.10", "1ИСИП-25-9", [entry(3, room="307", teacher="Иванов И. И.")])
    assert "п/г" not in text
    assert "🚪 каб. 307 · Иванов И. И." in text
