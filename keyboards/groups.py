"""Клавиатуры выбора группы: специальность -> группа, и режим преподавателя."""
import re

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Специальности в том порядке, в котором их показываем.
SPECIALTIES = (
    "БУХ",
    "ДП",
    "ИСИП",
    "КСК",
    "ОГР",
    "ОПИ",
    "ОПИР",
    "ПРУМ",
    "РГО",
    "РУПО",
    "СА",
    "ТЭО",
    "ЭМ",
    "ЭО",
    "ЭП",
)

_GROUP_RE = re.compile(r"^\d*([А-Яа-яЁёA-Za-z]+)")


def specialty_of(group: str) -> str | None:
    """'1ИСИП-25-9' -> 'ИСИП'. Возвращает None, если специальность неизвестна."""
    m = _GROUP_RE.match((group or "").strip())
    if not m:
        return None
    letters = m.group(1).upper()
    if letters in SPECIALTIES:
        return letters
    for spec in sorted(SPECIALTIES, key=len, reverse=True):
        if letters.startswith(spec):
            return spec
    return None


def group_sort_key(group: str) -> tuple:
    """Естественная сортировка: сначала курс/номер, потом название."""
    m = re.match(r"^(\d*)([А-Яа-яЁёA-Za-z]+)[- ]?(\d+)", (group or "").strip())
    if not m:
        return (99, (group or "").upper())
    return (int(m.group(1) or 0), m.group(2).upper(), int(m.group(3)))


def build_specialties_keyboard(*, with_teacher: bool = True) -> InlineKeyboardMarkup:
    """Сетка специальностей; «Я преподаватель» — только по запросу.

    with_teacher=False используется в режиме «🔎 Другая группа», чтобы
    не путать студентов: выбор преподавателя остаётся в /start.
    """
    rows = [
        [InlineKeyboardButton(text=s, callback_data=f"spec:{s}") for s in SPECIALTIES[i : i + 3]]
        for i in range(0, len(SPECIALTIES), 3)
    ]
    if with_teacher:
        rows.append(
            [InlineKeyboardButton(text="💡 Я преподаватель", callback_data="teacher:start")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_specialty_groups_keyboard(spec: str, groups: list[str]) -> InlineKeyboardMarkup:
    """Группы одной специальности + кнопка «Назад» к специальностям."""
    rows = [
        [InlineKeyboardButton(text=g, callback_data=f"g:{g}")]
        for g in sorted(groups, key=group_sort_key)
    ]
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="spec:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_groups_keyboard(groups: list[str]) -> InlineKeyboardMarkup:
    """Плоский список групп (на случай, если нужен запасной вариант)."""
    rows = [
        [InlineKeyboardButton(text=g, callback_data=f"g:{g}")]
        for g in sorted(groups, key=group_sort_key)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)