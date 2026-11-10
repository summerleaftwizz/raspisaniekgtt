"""Конфигурация бота. Все значения берутся из переменных окружения (.env)."""
import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value is not None else default


def _get_int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


@dataclass(frozen=True)
class ParserConfig:
    """Настройки парсера Excel.

    Каждое поле COL_* может быть:
      * int  — 0-based индекс колонки (если файл без строки-шапки);
      * str  — точное/частичное название колонки в шапке;
      * None — авто-определение по синонимам (см. *_SYNONYMS).

    COL_DAY оставлен для будущих форматов, где внутри одного листа несколько
    дней; сейчас дата берётся из имени папки на Яндекс.Диске.
    """
    COL_DAY: Optional[object] = None
    COL_LESSON_NUM: Optional[object] = None
    COL_TIME: Optional[object] = None
    COL_SUBJECT: Optional[object] = None
    COL_ROOM: Optional[object] = None

    DAY_SYNONYMS: tuple = ("день", "дата", "day")
    LESSON_NUM_SYNONYMS: tuple = ("пара", "№", "номер", "номер пары", "№ пары", "lesson")
    TIME_SYNONYMS: tuple = ("время", "время пары", "часы", "time")
    SUBJECT_SYNONYMS: tuple = ("предмет", "дисциплина", "предмет/дисциплина", "название", "subject")
    ROOM_SYNONYMS: tuple = ("кабинет", "каб", "ауд", "аудитория", "ауд.", "room")

    # Regex названия группы: "1ОГР-26-9", "БУХ-26-9", "ОПИр-25-9", "ОГР-23",
    # включая английские подгруппы: "1ИСИП-25-9A" (A/B/C).
    GROUP_NAME_PATTERN: str = r"^[А-Яа-яЁёA-Za-z0-9]+-\d{2,4}(-\d+)?[A-Za-zА-Яа-яЁё]?$"

    # Сколько первых строк листа проверять при поиске строки-шапки.
    HEADER_SCAN_ROWS: int = 6


@dataclass(frozen=True)
class Settings:
    BOT_TOKEN: str = field(default_factory=lambda: _get("BOT_TOKEN"))
    DATABASE_URL: str = field(
        default_factory=lambda: _get("DATABASE_URL", "sqlite+aiosqlite:////app/data/bot.db")
    )
    ADMIN_ID: int = field(default_factory=lambda: _get_int("ADMIN_ID", 1129975467))
    POLL_INTERVAL_MINUTES: int = field(default_factory=lambda: _get_int("POLL_INTERVAL_MINUTES", 3))
    YADISK_PUBLIC_URL: str = field(default_factory=lambda: _get("YADISK_PUBLIC_URL"))
    SUBFOLDER_NAME: str = field(default_factory=lambda: _get("SUBFOLDER_NAME", "студентам"))
    TZ_NAME: str = field(default_factory=lambda: _get("TZ_NAME", "Asia/Novosibirsk"))
    REQUEST_TIMEOUT: float = field(default_factory=lambda: float(_get("REQUEST_TIMEOUT", "15")))
    REQUEST_RETRIES: int = field(default_factory=lambda: _get_int("REQUEST_RETRIES", 3))
    BROADCAST_DELAY_MS: int = field(default_factory=lambda: _get_int("BROADCAST_DELAY_MS", 50))
    # Интервал проверки изменений расписания (замены/отмены/переносы), минут.
    CHANGE_CHECK_INTERVAL_MINUTES: int = field(
        default_factory=lambda: _get_int("CHANGE_CHECK_INTERVAL_MINUTES", 60)
    )
    SHOW_TIME: bool = field(default_factory=lambda: _get_bool("SHOW_TIME", True))
    parser: ParserConfig = field(default_factory=ParserConfig)


settings = Settings()


# --- Сетки звонков (время в HH:MM) -----------------------------------------

# Пн–Пт
BELLS_WEEKDAY: dict[int, tuple[str, str]] = {
    1: ("08:30", "10:00"),
    2: ("10:20", "11:50"),
    3: ("12:10", "13:40"),
    4: ("14:00", "15:30"),
    5: ("15:40", "17:10"),
    6: ("17:15", "18:45"),
}

# Суббота
BELLS_SATURDAY: dict[int, tuple[str, str]] = {
    1: ("08:30", "10:00"),
    2: ("10:10", "11:40"),
    3: ("11:50", "13:20"),
    4: ("13:30", "15:00"),
    5: ("15:10", "16:40"),
    6: ("16:45", "18:15"),
}


def bells_for_weekday(weekday: int) -> dict[int, tuple[str, str]]:
    """weekday: 0=Пн ... 6=Вс. Возвращает сетку звонков на этот день."""
    if weekday == 5:
        return BELLS_SATURDAY
    if weekday == 6:
        return {}
    return BELLS_WEEKDAY
