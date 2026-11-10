"""Функции доступа к БД. Все операции асинхронные, принимают готовую сессию."""
import json
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db import models


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- users ---

async def get_user(session: AsyncSession, telegram_id: int) -> Optional[models.User]:
    res = await session.execute(select(models.User).where(models.User.telegram_id == telegram_id))
    return res.scalar_one_or_none()


async def get_or_create_user(
    session: AsyncSession, telegram_id: int, username: Optional[str] = None
) -> models.User:
    user = await get_user(session, telegram_id)
    if user is None:
        user = models.User(telegram_id=telegram_id, subscribed=True, inactive=False)
        session.add(user)
        await session.flush()
    if username:
        user.username = username
    return user


async def list_all_users(session: AsyncSession) -> list[models.User]:
    res = await session.execute(select(models.User))
    return list(res.scalars())


async def count_users(session: AsyncSession) -> int:
    res = await session.execute(select(models.User.id))
    return len(list(res.scalars()))


async def group_stats(session: AsyncSession) -> list[tuple[str, int]]:
    """Разбивка пользователей по группам: [(название группы, кол-во), ...]."""
    res = await session.execute(
        select(models.User.group, func.count(models.User.id))
        .group_by(models.User.group)
        .order_by(func.count(models.User.id).desc())
    )
    return list(res.all())


async def list_users_in_group(session: AsyncSession, group: str) -> list[models.User]:
    res = await session.execute(
        select(models.User).where(models.User.group == group)
    )
    return list(res.scalars())


async def set_group(session: AsyncSession, telegram_id: int, group: str) -> None:
    user = await get_or_create_user(session, telegram_id)
    user.group = group


async def set_subscribed(session: AsyncSession, telegram_id: int, value: bool) -> None:
    user = await get_or_create_user(session, telegram_id)
    user.subscribed = value


async def mark_inactive(session: AsyncSession, telegram_id: int) -> None:
    user = await get_user(session, telegram_id)
    if user is not None:
        user.inactive = True


# ------------------------------------------------------------ teacher ---

async def set_teacher(session: AsyncSession, telegram_id: int, teacher_name: str) -> None:
    """Переводит пользователя в режим преподавателя."""
    user = await get_or_create_user(session, telegram_id)
    user.is_teacher = True
    user.teacher_name = teacher_name
    user.group = None


async def unset_teacher(session: AsyncSession, telegram_id: int) -> None:
    """Возврат в режим студента (фамилия сохраняется на случай возврата)."""
    user = await get_or_create_user(session, telegram_id)
    user.is_teacher = False


async def list_teachers(session: AsyncSession) -> list[models.User]:
    res = await session.execute(
        select(models.User).where(
            models.User.is_teacher.is_(True),
            models.User.teacher_name.isnot(None),
        )
    )
    return list(res.scalars())


async def get_teacher_schedule(
    session: AsyncSession, date: str, teacher_name: str
) -> list[models.ScheduleEntry]:
    """Пары преподавателя по всем группам на дату (по подстроке фамилии)."""
    res = await session.execute(
        select(models.ScheduleEntry)
        .where(models.ScheduleEntry.date == date)
        .order_by(models.ScheduleEntry.lesson_number, models.ScheduleEntry.group)
    )
    needle = (teacher_name or "").strip().lower()
    rows = list(res.scalars())
    if not needle:
        return rows
    return [e for e in rows if needle in (e.teacher or "").lower()]


async def list_subscribed_users(session: AsyncSession) -> list[models.User]:
    res = await session.execute(
        select(models.User).where(
            models.User.subscribed.is_(True),
            models.User.inactive.is_(False),
            models.User.group.isnot(None),
        )
    )
    return list(res.scalars())


# ------------------------------------------------------------ favorites ---

FAVORITE_SLOTS = (1, 2, 3)


def _fav_attr(slot: int) -> str:
    if slot not in FAVORITE_SLOTS:
        raise ValueError(f"недопустимый слот избранного: {slot}")
    return f"favorite_{slot}"


async def get_favorites(session: AsyncSession, telegram_id: int) -> dict[int, Optional[str]]:
    """Возвращает {1: группа|None, 2: ..., 3: ...}."""
    user = await get_user(session, telegram_id)
    if user is None:
        return {slot: None for slot in FAVORITE_SLOTS}
    return {slot: getattr(user, _fav_attr(slot), None) for slot in FAVORITE_SLOTS}


async def set_favorite(session: AsyncSession, telegram_id: int, slot: int, group: str) -> None:
    user = await get_or_create_user(session, telegram_id)
    setattr(user, _fav_attr(slot), group)


async def clear_favorite(session: AsyncSession, telegram_id: int, slot: int) -> None:
    user = await get_or_create_user(session, telegram_id)
    setattr(user, _fav_attr(slot), None)


# ------------------------------------------------------------- schedule ---

async def get_groups(session: AsyncSession) -> list[str]:
    res = await session.execute(
        select(models.ScheduleEntry.group).distinct().order_by(models.ScheduleEntry.group)
    )
    return list(res.scalars())


async def list_teacher_catalog(
    session: AsyncSession,
    group: Optional[str] = None,
) -> list[tuple[str, str, str]]:
    """Каталог преподавателей из спарсенного расписания: [(ФИО, предмет, кабинет), …].

    Если задана group — только пары этой группы. Уникальные комбинации,
    ФИО без дубликатов-пробелов, отсортировано по алфавиту. Включает и
    преподавателей из подгрупп (JSON-поле).
    """
    stmt = select(
        models.ScheduleEntry.teacher,
        models.ScheduleEntry.subject,
        models.ScheduleEntry.room,
        models.ScheduleEntry.subgroups,
    )
    if group:
        stmt = stmt.where(models.ScheduleEntry.group == group)
    res = await session.execute(stmt)
    seen: set[tuple[str, str, str]] = set()
    for teacher, subject, room, subgroups_json in res.all():
        subject = (subject or "").strip()
        room = (room or "").strip()
        teacher = re.sub(r"\s+", " ", (teacher or "")).strip()
        if teacher and subject:
            seen.add((teacher, subject, room))
        if subgroups_json:
            try:
                data = json.loads(subgroups_json)
            except (TypeError, ValueError):
                data = []
            if isinstance(data, list):
                for sub in data:
                    sub_teacher = re.sub(
                        r"\s+", " ", str(sub.get("teacher") or "")
                    ).strip() if isinstance(sub, dict) else ""
                    if sub_teacher and subject:
                        seen.add((sub_teacher, subject, (sub.get("room") or "").strip()))
    return sorted(seen, key=lambda t: t[0])


async def get_schedule(session: AsyncSession, date: str, group: str) -> list[models.ScheduleEntry]:
    res = await session.execute(
        select(models.ScheduleEntry)
        .where(models.ScheduleEntry.date == date, models.ScheduleEntry.group == group)
        .order_by(models.ScheduleEntry.lesson_number)
    )
    return list(res.scalars())


# ------------------------------------------------------------- snapshots ---
# Снапшоты для детектора изменений расписания (services/changes.py)


async def get_snapshot(session: AsyncSession, date: str, group: str) -> Optional[models.ScheduleSnapshot]:
    res = await session.execute(
        select(models.ScheduleSnapshot).where(
            models.ScheduleSnapshot.date == date,
            models.ScheduleSnapshot.group == group,
        )
    )
    return res.scalar_one_or_none()


async def upsert_snapshot(
    session: AsyncSession, date: str, group: str, fingerprint: str
) -> None:
    """Создаёт или обновляет отпечаток расписания группы на дату."""
    snap = await get_snapshot(session, date, group)
    if snap is None:
        session.add(models.ScheduleSnapshot(date=date, group=group, fingerprint=fingerprint))
    else:
        snap.fingerprint = fingerprint
    await session.flush()


async def list_snapshot_groups(session: AsyncSession) -> list[tuple[str, str]]:
    """Все пары (date, group), для которых есть снапшоты."""
    res = await session.execute(
        select(models.ScheduleSnapshot.date, models.ScheduleSnapshot.group)
    )
    return list(res.all())


async def replace_schedule(
    session: AsyncSession, date: str, group: str, lessons: list[dict]
) -> None:
    """Полностью заменяет расписание группы на дату (delete + insert)."""
    await session.execute(
        delete(models.ScheduleEntry).where(
            models.ScheduleEntry.date == date, models.ScheduleEntry.group == group
        )
    )
    for l in lessons:
        subs = l.get("subgroups") or []
        session.add(
            models.ScheduleEntry(
                date=date,
                group=group,
                lesson_number=l["lesson_number"],
                time=l.get("time", ""),
                subject=l.get("subject", ""),
                room=l.get("room", ""),
                teacher=l.get("teacher") or None,
                subgroups=json.dumps(subs, ensure_ascii=False) if subs else None,
            )
        )
    await session.flush()


async def delete_schedule_for_date(session: AsyncSession, date: str) -> None:
    await session.execute(delete(models.ScheduleEntry).where(models.ScheduleEntry.date == date))


# ---------------------------------------------------------------- known ---

async def get_known(session: AsyncSession, date_folder: str) -> Optional[models.KnownSchedule]:
    res = await session.execute(
        select(models.KnownSchedule).where(models.KnownSchedule.date_folder == date_folder)
    )
    return res.scalar_one_or_none()


async def mark_known(session: AsyncSession, date_folder: str, notified_at: Optional[datetime] = None) -> None:
    known = await get_known(session, date_folder)
    if known is None:
        known = models.KnownSchedule(date_folder=date_folder, notified_at=notified_at or _utcnow())
        session.add(known)
    else:
        known.notified_at = notified_at or _utcnow()
    await session.flush()


async def list_known(session: AsyncSession) -> list[models.KnownSchedule]:
    res = await session.execute(
        select(models.KnownSchedule).order_by(models.KnownSchedule.date_folder)
    )
    return list(res.scalars())


# --------------------------------------------------------------- debts -----

async def add_debt(session: AsyncSession, telegram_id: int, text: str) -> models.Debt:
    debt = models.Debt(telegram_id=telegram_id, text=text)
    session.add(debt)
    await session.flush()
    return debt


async def list_debts(session: AsyncSession, telegram_id: int) -> list[models.Debt]:
    res = await session.execute(
        select(models.Debt)
        .where(models.Debt.telegram_id == telegram_id)
        .order_by(models.Debt.created_at)
    )
    return list(res.scalars())


async def delete_debt(session: AsyncSession, telegram_id: int, debt_id: int) -> bool:
    """Удаляет долг, но только если он принадлежит этому пользователю."""
    res = await session.execute(
        select(models.Debt).where(
            models.Debt.id == debt_id, models.Debt.telegram_id == telegram_id
        )
    )
    debt = res.scalar_one_or_none()
    if debt is None:
        return False
    await session.execute(delete(models.Debt).where(models.Debt.id == debt_id))
    return True


# ------------------------------------------------------------ deadlines ----

async def add_deadline(session: AsyncSession, telegram_id: int, text: str, due_ts: int) -> models.Deadline:
    deadline = models.Deadline(telegram_id=telegram_id, text=text, due_ts=due_ts)
    session.add(deadline)
    await session.flush()
    return deadline


async def list_deadlines(session: AsyncSession, telegram_id: int) -> list[models.Deadline]:
    res = await session.execute(
        select(models.Deadline)
        .where(models.Deadline.telegram_id == telegram_id, models.Deadline.notified.is_(False))
        .order_by(models.Deadline.due_ts)
    )
    return list(res.scalars())


async def claim_due_deadlines(session: AsyncSession, now_ts: int) -> list[models.Deadline]:
    """Атомарно забирает наступившие дедлайны (помечает notified=True),
    чтобы напоминание ушло ровно один раз."""
    res = await session.execute(
        select(models.Deadline)
        .where(models.Deadline.notified.is_(False), models.Deadline.due_ts <= now_ts)
        .order_by(models.Deadline.due_ts)
    )
    due = list(res.scalars())
    for d in due:
        d.notified = True
    await session.flush()
    return due
