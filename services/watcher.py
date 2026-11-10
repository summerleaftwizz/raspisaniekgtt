"""Фоновая задача поллинга Яндекс.Диска и рассылка расписания.

Фактическая структура публичной папки — файлы лежат в корне:
  /studentam_2026-10-23.xlsx
  /studentam_2026-10-24.xlsx
  /prepodovatelyam_2026-10-23.xlsx   (преподаватели — игнорируем)

Для студентов берём файлы `studentam_ГГГГ-ММ-ДД.xlsx`; дата — из имени файла.
Каждый файл содержит несколько листов, имя листа = группа.
"""
import asyncio
import logging
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError

from config import settings
from db import SessionLocal
from db import crud
from keyboards.menu import build_main_menu
from services import formatting, parser
from services.parser import ParseError
from services.yadisk import YaDiskClient

logger = logging.getLogger(__name__)

STUDENT_FILE_PREFIX = "studentam_"
STUDENT_FILE_RE = re.compile(
    rf"^{re.escape(STUDENT_FILE_PREFIX)}(\d{{4}}-\d{{2}}-\d{{2}})\.xlsx$", re.IGNORECASE
)


def _date_from_filename(name: str) -> Optional[str]:
    """'studentam_2026-10-23.xlsx' -> '23.10' (DD.MM)."""
    m = STUDENT_FILE_RE.match((name or "").strip())
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").strftime("%d.%m")
    except ValueError:
        return None


async def _mark_user_inactive(telegram_id: int) -> None:
    async with SessionLocal() as session:
        await crud.mark_inactive(session, telegram_id)
        await session.commit()


async def _send(bot: Bot, telegram_id: int, text: str) -> bool:
    try:
        await bot.send_message(
            telegram_id, text, parse_mode="HTML", reply_markup=build_main_menu()
        )
        return True
    except (TelegramForbiddenError, TelegramBadRequest):
        # пользователь заблокировал бота / чат не найден / аккаунт удалён
        await _mark_user_inactive(telegram_id)
        logger.info("пользователь %s недоступен — помечен inactive", telegram_id)
        return False
    except TelegramAPIError as exc:
        logger.warning("ошибка Telegram для %s: %s", telegram_id, exc)
        return False
    except Exception:
        logger.exception("неожиданная ошибка отправки для %s", telegram_id)
        return False


async def _broadcast(bot: Bot, date_label: str, groups: dict) -> int:
    async with SessionLocal() as session:
        users = await crud.list_subscribed_users(session)
        targets = [
            (u.telegram_id, u.group, False, "")
            for u in users
            if not u.is_teacher
        ]

    delay = settings.BROADCAST_DELAY_MS / 1000
    sent = 0
    for telegram_id, group, is_teacher, teacher_name in targets:
        lessons = groups.get(group)
        if not lessons:
            continue
        text = formatting.format_notification(date_label, group, lessons)
        if await _send(bot, telegram_id, text):
            sent += 1
        await asyncio.sleep(delay)
    sent += await _broadcast_teachers(bot, date_label, groups)
    logger.info("рассылка по дате %s: отправлено %d", date_label, sent)
    return sent


async def _broadcast_teachers(bot: Bot, date_label: str, groups: dict) -> int:
    """Уведомления преподавателям: пары преподавателя по всем группам."""
    async with SessionLocal() as session:
        teachers = [
            (u.telegram_id, u.teacher_name or "")
            for u in await crud.list_teachers(session)
            if u.subscribed and not u.inactive
        ]

    delay = settings.BROADCAST_DELAY_MS / 1000
    sent = 0
    for telegram_id, teacher_name in teachers:
        async with SessionLocal() as session:
            entries = await crud.get_teacher_schedule(session, date_label, teacher_name)
        if not entries:
            continue
        text = formatting.format_notification(
            date_label, teacher_name, entries, teacher_mode=True
        )
        if await _send(bot, telegram_id, text):
            sent += 1
        await asyncio.sleep(delay)
    return sent


async def _process_file(
    client: YaDiskClient, bot: Bot, item: dict, force: bool = False
) -> Optional[str]:
    """Обрабатывает один файл studentam_*.xlsx. Возвращает дату 'DD.MM' или None."""
    name = (item.get("name") or "").strip()
    date_label = _date_from_filename(name)
    if date_label is None:
        return None

    async with SessionLocal() as session:
        known = await crud.get_known(session, name)
    if known is not None and not force:
        logger.info("файл %s уже обработан — пропускаю", name)
        return None

    logger.info("скачиваю %s", name)
    content = await client.download_item(item)
    try:
        groups = parser.parse_schedule(content, settings.parser)
    except ParseError as exc:
        logger.error("не удалось распарсить %s: %s", name, exc)
        return None

    # 1) сохраняем расписание в БД
    async with SessionLocal() as session:
        for group, lessons in groups.items():
            rows = [
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
            ]
            await crud.replace_schedule(session, date_label, group, rows)
        await session.commit()

    # 2) рассылаем; при падении откатываем записи, чтобы повторить в след. цикле
    try:
        await _broadcast(bot, date_label, groups)
    except Exception:
        logger.exception("рассылка по дате %s упала — откатываю записи", date_label)
        async with SessionLocal() as session:
            await crud.delete_schedule_for_date(session, date_label)
            await session.commit()
        return None

    # 3) помечаем файл обработанным
    async with SessionLocal() as session:
        await crud.mark_known(session, name)
        await session.commit()

    logger.info("файл %s обработан (дата %s)", name, date_label)
    return date_label


async def run_check(bot: Bot) -> list[str]:
    """Один цикл поллинга. Возвращает список обработанных дат ('DD.MM')."""
    processed: list[str] = []
    async with YaDiskClient(
        settings.YADISK_PUBLIC_URL, settings.REQUEST_TIMEOUT, settings.REQUEST_RETRIES
    ) as client:
        items = await client.list_resources()
        for item in items:
            name = (item.get("name") or "").strip()
            if item.get("type") != "file" or not name.lower().startswith(STUDENT_FILE_PREFIX):
                continue
            if not name.lower().endswith(".xlsx"):
                continue
            try:
                result = await _process_file(client, bot, item, force=False)
                if result:
                    processed.append(result)
            except Exception:
                logger.exception("ошибка обработки файла %s", name)
    return processed


async def reparse_latest(bot: Bot) -> Optional[str]:
    """Перепарсить самый свежий файл studentam_*.xlsx и разослать заново (для админа)."""
    async with YaDiskClient(
        settings.YADISK_PUBLIC_URL, settings.REQUEST_TIMEOUT, settings.REQUEST_RETRIES
    ) as client:
        items = await client.list_resources()
        student_files = [
            it
            for it in items
            if it.get("type") == "file" and STUDENT_FILE_RE.match((it.get("name") or "").strip())
        ]
        if not student_files:
            logger.warning("нет файлов studentam_*.xlsx для перепарсинга")
            return None

        def _sort_key(it: dict) -> str:
            m = STUDENT_FILE_RE.match((it.get("name") or "").strip())
            return m.group(1) if m else ""

        student_files.sort(key=_sort_key)
        latest = student_files[-1]
        logger.info("принудительный перепарсинг %s", latest.get("name"))
        return await _process_file(client, bot, latest, force=True)


async def watcher_loop(bot: Bot) -> None:
    logger.info("watcher запущен, интервал %d мин", settings.POLL_INTERVAL_MINUTES)
    while True:
        try:
            await run_check(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ошибка в цикле watcher")
        await asyncio.sleep(settings.POLL_INTERVAL_MINUTES * 60)
