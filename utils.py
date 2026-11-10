"""Вспомогательные функции: локальное время и работа с датами."""
import re
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from config import settings

WEEKDAY_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def now() -> datetime:
    """Текущее время в часовом поясе бота (TZ_NAME)."""
    return datetime.now(ZoneInfo(settings.TZ_NAME))


def ddmm(d: date) -> str:
    return d.strftime("%d.%m")


def today_ddmm() -> str:
    return ddmm(now().date())


def tomorrow_ddmm() -> str:
    return ddmm(now().date() + timedelta(days=1))


def parse_folder_date(name: str) -> Optional[date]:
    """Парсит имя папки вида '25.10', '25-10', '25.10.2026' в дату.

    Если год не указан, берётся текущий; если получившаяся дата «в прошлом»
    больше чем на полгода — считается, что это следующий год.
    """
    s = (name or "").strip()
    m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?", s)
    if not m:
        return None
    day = int(m.group(1))
    month = int(m.group(2))
    year: Optional[int] = None
    if m.group(3):
        y = int(m.group(3))
        year = 2000 + y if y < 100 else y

    today = now().date()
    if year is None:
        year = today.year
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if d < today - timedelta(days=180):
            year += 1
    try:
        return date(year, month, day)
    except ValueError:
        return None


def looks_like_date(name: str) -> bool:
    return parse_folder_date(name) is not None


def week_days() -> list[tuple[str, str]]:
    """Пн..Сб текущей недели: [(название дня, 'DD.MM'), ...]."""
    today = now().date()
    monday = today - timedelta(days=today.weekday())
    return [(WEEKDAY_NAMES[i], ddmm(monday + timedelta(days=i))) for i in range(6)]


def parse_due_datetime(text: str) -> Optional[datetime]:
    """Парсит дедлайн из строки в aware datetime (TZ_NAME).

    Поддерживаются: '31.10.2026 18:00', '31.10 18:00', '2026-10-31 18:00'.
    """
    s = (text or "").strip()
    m = re.fullmatch(
        r"(?:(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?|(\d{4})-(\d{1,2})-(\d{1,2}))"
        r"\s+(\d{1,2})[:.](\d{2})",
        s,
    )
    if not m:
        return None
    if m.group(1):
        day, month = int(m.group(1)), int(m.group(2))
        year = None
        if m.group(3):
            y = int(m.group(3))
            year = 2000 + y if y < 100 else y
    else:
        year, month, day = int(m.group(4)), int(m.group(5)), int(m.group(6))
    hour, minute = int(m.group(7)), int(m.group(8))

    tz = ZoneInfo(settings.TZ_NAME)
    if year is None:
        year = now().year
        try:
            candidate = datetime(year, month, day, hour, minute, tzinfo=tz)
        except ValueError:
            return None
        if candidate.date() < now().date() - timedelta(days=180):
            year += 1
    try:
        return datetime(year, month, day, hour, minute, tzinfo=tz)
    except ValueError:
        return None

