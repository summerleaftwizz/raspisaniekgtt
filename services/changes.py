"""Уведомления об изменениях в расписании (замены, отмены, переносы).

Логика:
1. Периодическая задача (APScheduler в app.py) скачивает свежие файлы
   studentam_*.xlsx с Яндекс.Диска и парсит их.
2. Для каждой пары (дата, группа) считается sha256-отпечаток содержимого пар
   и сравнивается со снапшотом в БД (schedule_snapshots).
3. Если отпечаток изменился — расписание в БД обновляется, считается diff
   (какие пары поменялись) и уведомление рассылается ТОЛЬКО подписанным
   пользователям этой группы (и преподавателям, чьи пары затронуты).
4. Нет изменений — ничего не отправляется.

Первый раз для пары (дата, группа) просто сохраняется снапшот — без рассылки,
чтобы не дублировать уведомления watcher'а о новом расписании.
"""
import asyncio
import hashlib
import logging
from dataclasses import asdict, is_dataclass
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from config import settings
from db import SessionLocal
from db import crud
from db.models import ScheduleEntry
from services import parser
from services.parser import ParseError
from services.watcher import (
    STUDENT_FILE_RE,
    YaDiskClient,
    _date_from_filename,
    _send,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------- fingerprint


def _entry_fingerprint(e) -> str:
    """Отпечаток одной пары: всё, что может измениться."""
    subgroups = e.subgroups or ""
    if is_dataclass(e) and not isinstance(e, type):
        subgroups = str(asdict(e.subgroups)) if e.subgroups else ""
    raw = "|".join(
        str(x or "")
        for x in (e.lesson_number, e.time, e.subject, e.room, e.teacher, subgroups)
    )
    return raw


def fingerprint_of_lessons(lessons) -> str:
    """sha256 списка пар (объектов parser.LessonInfo или dict-строк)."""
    parts = []
    for l in sorted(lessons, key=lambda x: x.get("lesson_number", 0) if isinstance(x, dict) else x.lesson_number):
        if isinstance(l, dict):
            raw = "|".join(
                str(l.get(k) or "")
                for k in ("lesson_number", "time", "subject", "room", "teacher", "subgroups")
            )
        else:
            raw = _entry_fingerprint(l)
        parts.append(raw)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def fingerprint_of_rows(rows: list[ScheduleEntry]) -> str:
    """sha256 уже сохранённых в БД строк ScheduleEntry."""
    parts = [
        "|".join(str(x or "") for x in (r.lesson_number, r.time, r.subject, r.room, r.teacher, r.subgroups or ""))
        for r in sorted(rows, key=lambda r: r.lesson_number)
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- diff-текст


def _day_name(date_label: str) -> str:
    """'05.11' -> 'Завтра/Сегодня/05.11' относительно текущей даты."""
    from utils import now, today_ddmm, tomorrow_ddmm

    if date_label == today_ddmm():
        return "Сегодня"
    if date_label == tomorrow_ddmm():
        return "Завтра"
    return date_label


def diff_text(date_label: str, group: str, old_rows: list[ScheduleEntry], new_lessons: list) -> str:
    """Человекочитаемое описание изменений: «3 пара перенесена в каб. 317 (было 315)»."""
    old_by_num = {r.lesson_number: r for r in old_rows}
    lines: list[str] = []

    for l in new_lessons:
        if isinstance(l, dict):
            num = l.get("lesson_number")
            subject, room, teacher = l.get("subject"), l.get("room"), l.get("teacher")
            time = l.get("time")
        else:
            num = l.lesson_number
            subject, room, teacher = l.subject, l.room, l.teacher
            time = l.time
        old = old_by_num.get(num)
        if old is None:
            subject_e = (subject or "").strip()
            room_e = (room or "").strip()
            lines.append(
                f"➕ Добавлена <b>{num} пара</b>"
                + (f" ({time})" if time else "")
                + f": {subject_e}"
                + (f", каб. {room_e}" if room_e else "")
            )
            continue
        old_by_num.pop(num)
        changes = []
        if (old.room or "") != (room or ""):
            changes.append(
                f"🚪 перенесена в каб. {room} (было {old.room or '—'})"
            )
        if (old.subject or "") != (subject or ""):
            changes.append(f"📚 теперь {subject} (было {old.subject or '—'})")
        if (old.teacher or "") != (teacher or ""):
            changes.append(f"👤 преподаватель: {teacher} (было {old.teacher or '—'})")
        if changes:
            head = f"{_day_name(date_label)} <b>{num} пара</b>" + (f" ({time})" if time else "")
            lines.append(head + " — " + "; ".join(changes))

    # Пары, которые были, а стали нет — отмены.
    for num, old in sorted(old_by_num.items()):
        lines.append(
            f"❌ Отменена <b>{num} пара</b> ({old.time or ''}): {old.subject or ''}".rstrip()
        )

    return "\n".join(lines)


# ------------------------------------------------------- проверка изменений


async def _fetch_groups(client: YaDiskClient, item: dict) -> Optional[dict]:
    """Скачивает и парсит один studentam_*.xlsx -> {группа: [LessonInfo, ...]}."""
    try:
        content = await client.download_item(item)
        return parser.parse_schedule(content, settings.parser)
    except ParseError as exc:
        logger.error("не удалось распарсить %s: %s", item.get("name"), exc)
        return None


async def _notify_group_users(bot: Bot, date_label: str, group: str, text: str) -> int:
    """Рассылка изменений подписанным пользователям конкретной группы."""
    async with SessionLocal() as session:
        users = await crud.list_subscribed_users(session)
        targets = [
            u.telegram_id
            for u in users
            if u.group == group and not u.is_teacher
        ]

    header = (
        f"⚠️ <b>Внимание!</b> Для группы <b>{group}</b> "
        f"произошли изменения в расписании ({_day_name(date_label)}):\n\n"
    )
    sent = 0
    delay = settings.BROADCAST_DELAY_MS / 1000
    for telegram_id in targets:
        if await _send(bot, telegram_id, header + text):
            sent += 1
        await asyncio.sleep(delay)
    return sent


async def _notify_teachers(bot: Bot, date_label: str, group: str, diff: str, new_lessons: list) -> int:
    """Уведомления преподавателям, чьи пары в этой группе изменились."""
    touched = {l.teacher for l in new_lessons if not isinstance(l, dict) and l.teacher}
    if not touched:
        return 0
    async with SessionLocal() as session:
        teachers = [
            (u.telegram_id, u.teacher_name or "")
            for u in await crud.list_teachers(session)
            if u.subscribed and not u.inactive
        ]
    header = (
        f"⚠️ <b>Внимание!</b> Изменения в расписании группы <b>{group}</b> "
        f"({_day_name(date_label)}):\n\n"
    )
    sent = 0
    delay = settings.BROADCAST_DELAY_MS / 1000
    for telegram_id, teacher_name in teachers:
        if teacher_name not in touched:
            continue
        if await _send(bot, telegram_id, header + diff):
            sent += 1
        await asyncio.sleep(delay)
    return sent


async def check_for_changes(bot: Bot) -> int:
    """Один цикл проверки. Возвращает число групп с отправленными изменениями.

    Сравнивает отпечатки свежих файлов со снапшотами; при изменении:
    обновляет расписание и снапшот, рассылает diff по затронутым группам.
    """
    changed_groups = 0
    async with YaDiskClient(
        settings.YADISK_PUBLIC_URL, settings.REQUEST_TIMEOUT, settings.REQUEST_RETRIES
    ) as client:
        items = await client.list_resources()
        student_files = [
            it
            for it in items
            if it.get("type") == "file" and STUDENT_FILE_RE.match((it.get("name") or "").strip())
        ]
        # Сравниваем только свежие файлы (последние 3 даты).
        student_files.sort(key=lambda it: STUDENT_FILE_RE.match(it["name"]).group(1))
        for item in student_files[-3:]:
            name = (item.get("name") or "").strip()
            date_label = _date_from_filename(name)
            if date_label is None:
                continue
            groups = await _fetch_groups(client, item)
            if not groups:
                continue

            for group, lessons in groups.items():
                new_fp = fingerprint_of_lessons(lessons)
                async with SessionLocal() as session:
                    snap = await crud.get_snapshot(session, date_label, group)
                    if snap is not None and snap.fingerprint == new_fp:
                        continue  # нет изменений
                    old_rows = await crud.get_schedule(session, date_label, group)
                    if snap is None and not old_rows:
                        # Первый раз видим эту группу на эту дату — просто фиксируем.
                        await crud.replace_schedule(
                            session,
                            date_label,
                            group,
                            [
                                {
                                    "lesson_number": l.lesson_number,
                                    "time": l.time,
                                    "subject": l.subject,
                                    "room": l.room,
                                    "teacher": l.teacher,
                                    "subgroups": [
                                        asdict(s) if is_dataclass(s) and not isinstance(s, type) else s
                                        for s in (l.subgroups or [])
                                    ],
                                }
                                for l in lessons
                            ],
                        )
                        await crud.upsert_snapshot(session, date_label, group, new_fp)
                        await session.commit()
                        continue

                    diff = diff_text(date_label, group, old_rows, lessons)

                # Обновляем БД и снапшот, затем рассылаем.
                async with SessionLocal() as session:
                    await crud.replace_schedule(
                        session,
                        date_label,
                        group,
                        [
                            {
                                "lesson_number": l.lesson_number,
                                "time": l.time,
                                "subject": l.subject,
                                "room": l.room,
                                "teacher": l.teacher,
                                "subgroups": [
                                    asdict(s) if is_dataclass(s) and not isinstance(s, type) else s
                                    for s in (l.subgroups or [])
                                ],
                            }
                            for l in lessons
                        ],
                    )
                    await crud.upsert_snapshot(session, date_label, group, new_fp)
                    await session.commit()

                if not diff:
                    continue
                try:
                    await _notify_group_users(bot, date_label, group, diff)
                    await _notify_teachers(bot, date_label, group, diff, lessons)
                    changed_groups += 1
                    logger.info("изменения %s / %s разосланы", date_label, group)
                except TelegramAPIError:
                    logger.exception("ошибка рассылки изменений %s / %s", date_label, group)
    return changed_groups


async def changes_loop(bot: Bot) -> None:
    """Периодическая проверка изменений (fallback, если APScheduler не используется)."""
    while True:
        try:
            await check_for_changes(bot)
        except Exception:
            logger.exception("ошибка в цикле проверки изменений")
        await asyncio.sleep(settings.CHANGE_CHECK_INTERVAL_MINUTES * 60)
