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
import html
import logging
import re
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


def _esc(value) -> str:
    return html.escape(str(value or ""), quote=False)


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


# Пометки «пустой» пары: окна и прочерки изменениями не считаются.
_WINDOW_SUBJECTS = frozenset({"окно", "окна", "-", "—", "–", "нет", "нет пар"})


def _is_real_subject(subject: str) -> bool:
    """Пара настоящая: предмет не пустой и не пометка «окно»."""
    s = _clean(subject).lower().strip(" .,:;!")
    return bool(s) and s not in _WINDOW_SUBJECTS


def _pair_fields(item) -> dict:
    """Нормализованные поля пары из Lesson/ScheduleEntry/dict."""
    if isinstance(item, dict):
        return {
            "num": item.get("lesson_number"),
            "subject": _clean(item.get("subject")),
            "room": _clean(item.get("room")),
            "teacher": _clean(item.get("teacher")),
            "time": _clean(item.get("time")),
        }
    return {
        "num": getattr(item, "lesson_number", None),
        "subject": _clean(getattr(item, "subject", "")),
        "room": _clean(getattr(item, "room", "")),
        "teacher": _clean(getattr(item, "teacher", "")),
        "time": _clean(getattr(item, "time", "")),
    }


def _real_pairs(items) -> dict[int, dict]:
    """Реальные пары по НОМЕРУ ПАРЫ: только записи с предметом.

    Пустые пары (без предмета / «окно») в сравнении не участвуют —
    изменения только среди них уведомлений не создают.
    """
    result: dict[int, dict] = {}
    for item in items:
        f = _pair_fields(item)
        if f["num"] is None or not _is_real_subject(f["subject"]):
            continue
        result.setdefault(int(f["num"]), f)
    return result


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


def _diff(
    date_label: str, old_rows: list[ScheduleEntry], new_lessons: list
) -> tuple[list[str], set[str]]:
    """Сравнение расписаний: (строки diff, затронутые преподаватели).

    Пары сравниваются по НОМЕРУ ПАРЫ и ПРЕДМЕТУ:
    * номер и предмет совпали, кабинет тот же — изменения НЕТ (даже если
      изменилось ФИО);
    * номер и предмет совпали, кабинет другой — «перемещена»;
    * номер и предмет совпали, кабинет в новом файле не найден — об этой
      паре НЕ сообщаем (иначе ложное «перемещена в каб. (было 301)»);
    * пары с номером, которого не было, — «Добавлена»;
    * пары, которых больше нет, — «Отменена».
    Пустые пары (без предмета) игнорируются с обеих сторон.
    """
    old = _real_pairs(old_rows)
    new = _real_pairs(new_lessons)
    lines: list[str] = []
    touched: set[str] = set()

    for num in sorted(new):
        nf = new[num]
        of = old.get(num)
        if of is None:
            line = f"➕ Добавлена <b>{num} пара</b>"
            if nf["time"]:
                line += f" ({nf['time']})"
            line += f": {_esc(nf['subject'])}"
            if nf["room"]:
                line += f", каб. {_esc(nf['room'])}"
            lines.append(line)
            if nf["teacher"]:
                touched.add(nf["teacher"])
            continue

        changes: list[str] = []
        if nf["subject"] != of["subject"]:
            changes.append(
                f"📚 теперь {_esc(nf['subject'])} (было {_esc(of['subject']) or '—'})"
            )
        if nf["room"] != of["room"] and nf["room"]:
            changes.append(
                f"🚪 перенесена в каб. {_esc(nf['room'])} "
                f"(было {_esc(of['room']) or '—'})"
            )
        # nf["room"] пуст, а старый был — уведомление об этой паре не шлём.
        if changes:
            head = f"{_day_name(date_label)} <b>{num} пара</b>"
            if nf["time"]:
                head += f" ({nf['time']})"
            lines.append(head + " — " + "; ".join(changes))
            for t in (nf["teacher"], of["teacher"]):
                if t:
                    touched.add(t)

    # Пары, которые были, а стали нет — отмены (только реальные пары).
    for num in sorted(set(old) - set(new)):
        of = old[num]
        line = f"❌ Отменена <b>{num} пара</b>"
        if of["time"]:
            line += f" ({of['time']})"
        line += f": {_esc(of['subject'])}"
        lines.append(line.rstrip())
        if of["teacher"]:
            touched.add(of["teacher"])

    return lines, touched


def diff_text(
    date_label: str, group: str, old_rows: list[ScheduleEntry], new_lessons: list
) -> str:
    """Человекочитаемое описание изменений: «3 пара перенесена в каб. 317 (было 315)»."""
    lines, _touched = _diff(date_label, old_rows, new_lessons)
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


async def _notify_teachers(
    bot: Bot, date_label: str, group: str, diff: str, touched: set[str]
) -> int:
    """Уведомления преподавателям, чьи пары в этой группе изменились."""
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

                    diff_lines, touched_teachers = _diff(date_label, old_rows, lessons)
                    diff = "\n".join(diff_lines)

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
                    await _notify_teachers(bot, date_label, group, diff, touched_teachers)
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
