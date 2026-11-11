"""Точка входа бота: запуск поллинга, фонового watcher'а и graceful shutdown."""
import asyncio
import contextlib
import logging
import signal

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from config import settings
from db import dispose_db, init_db
from handlers import admin, other, profile, schedule, start, teachers
from services import reminders, watcher

# APScheduler — периодическая проверка изменений расписания (замены/отмены).
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    from services import changes
    _HAS_APSCHEDULER = True
except ImportError:  # APScheduler не установлен — используем fallback-цикл
    _HAS_APSCHEDULER = False

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not settings.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан — заполни .env")
    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан — заполни .env")

    await init_db()

    bot = Bot(
        token=settings.BOT_TOKEN, 
        default=DefaultBotProperties(parse_mode="HTML")
    )
    
    dp = Dispatcher()
    dp.include_routers(
        other.router,        # до start: перехватывает g:/spec: в режиме «Другая группа»
        teachers.router,     # FSM поиска преподавателя
        start.router,
        profile.router,
        schedule.router,
        admin.router,
    )

    watcher_task = asyncio.create_task(watcher.watcher_loop(bot))
    reminders_task = asyncio.create_task(reminders.reminders_loop(bot))

    # --- APScheduler: периодическая проверка изменений расписания -----------
    scheduler = None
    changes_task = None
    if _HAS_APSCHEDULER:
        scheduler = AsyncIOScheduler(timezone=settings.TZ_NAME)
        scheduler.add_job(
            changes.check_for_changes,
            trigger=IntervalTrigger(minutes=settings.CHANGE_CHECK_INTERVAL_MINUTES),
            args=[bot],
            id="changes_check",
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        logger.info(
            "APScheduler запущен: проверка изменений каждые %d мин",
            settings.CHANGE_CHECK_INTERVAL_MINUTES,
        )
    else:
        # Fallback без APScheduler — простой asyncio-цикл с тем же интервалом.
        changes_task = asyncio.create_task(changes.changes_loop(bot))
        logger.warning("APScheduler не установлен — используется fallback-цикл изменений")

    background_tasks = [watcher_task, reminders_task]
    if changes_task is not None:
        background_tasks.append(changes_task)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def request_shutdown() -> None:
        logger.info("Получен сигнал завершения")
        stop.set()

    if hasattr(loop, "add_signal_handler"):
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, request_shutdown)

    polling_task = asyncio.create_task(dp.start_polling(bot))
    stop_task = asyncio.create_task(stop.wait())

    try:
        done, _ = await asyncio.wait(
            {polling_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            if task is polling_task:
                exc = polling_task.exception()
                if exc is not None:
                    logger.error("polling завершился с ошибкой: %s", exc)
    finally:
        logger.info("Останавливаю бота…")
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        for task in (polling_task, stop_task, *background_tasks):
            task.cancel()
        for task in (polling_task, stop_task, *background_tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await bot.session.close()
        await dispose_db()
    logger.info("Остановлен")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(main())