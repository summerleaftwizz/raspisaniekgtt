"""Telegram Mini App: backend API.

Что здесь:
* validate_init_data()  — проверка подписи initData (HMAC-SHA256, секрет
  "WebAppData" от токена бота). Защищает от подделки user_id.
* build_app()           — aiohttp-приложение с эндпоинтами /api/* и раздачей
  статики из папки mini-app/.

Запускается в app.py рядом с ботом (тот же asyncio-цикл, тот же сервер,
что и бот на bothost). Схема авторизации:
  1. Фронтенд открывает Mini App и получает initData от Telegram SDK.
  2. Каждый запрос фронт шлёт заголовок X-Init-Data с этим initData.
  3. Здесь initData валидируется; если он не подписан токеном бота —
     возвращаем 401.
"""
import hashlib
import json
import logging
import urllib.parse
from pathlib import Path
from typing import Optional

from aiohttp import web

from config import settings
from db import SessionLocal
from db import crud
from services.initdata import validate_init_data
from utils import today_ddmm

logger = logging.getLogger(__name__)

# Срок жизни подписанного initData (сек). Бот перезапускается реже, чем
# пользователь переоткрывает Mini App, поэтому берём с запасом — 24 часа.
INIT_DATA_MAX_AGE = 24 * 3600

# Папка со статикой фронтенда.
MINI_APP_DIR = Path(__file__).resolve().parent.parent / "mini-app"


# ------------------------------------------------------------- валидация ----


def _user_from_init_data(request: web.Request) -> Optional[dict]:
    """Достаёт и валидирует пользователя из заголовка X-Init-Data."""
    init_data = request.headers.get("X-Init-Data", "")
    data = validate_init_data(init_data, settings.BOT_TOKEN)
    if not data:
        return None
    # data["user"] — JSON-строка с id, username, first_name...
    try:
        user = json.loads(data.get("user") or "{}")
    except json.JSONDecodeError:
        return None
    if not user.get("id"):
        return None
    return user


# --------------------------------------------------------------- хендлеры ---


async def _api_auth(request: web.Request) -> web.Response:
    """GET /api/auth — проверка, что initData валиден, и текущий user."""
    user = _user_from_init_data(request)
    if user is None:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    return web.json_response({"ok": True, "user": user})


async def _api_schedule(request: web.Request) -> web.Response:
    """GET /api/schedule?date=DD.MM — расписание группы пользователя."""
    user = _user_from_init_data(request)
    if user is None:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    telegram_id = user["id"]
    date_label = request.query.get("date") or today_ddmm()
    # group можно передать явно (просмотр чужой группы), иначе — своя.
    group = request.query.get("group")

    async with SessionLocal() as session:
        db_user = await crud.get_user(session, telegram_id)
        if not group:
            group = db_user.group if db_user else None
        if not group:
            return web.json_response(
                {"ok": False, "error": "no_group", "hint": "Сначала выбери группу в боте"},
                status=200,
            )
        entries = await crud.get_schedule(session, date_label, group)

    lessons = [
        {
            "lesson_number": e.lesson_number,
            "time": e.time or "",
            "subject": e.subject or "",
            "room": e.room or "",
            "teacher": e.teacher or "",
            "subgroups": json.loads(e.subgroups) if e.subgroups else [],
        }
        for e in entries
    ]
    return web.json_response(
        {"ok": True, "date": date_label, "group": group, "lessons": lessons}
    )


async def _api_user(request: web.Request) -> web.Response:
    """GET /api/user — текущая группа и настройки уведомлений."""
    user = _user_from_init_data(request)
    if user is None:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    async with SessionLocal() as session:
        db_user = await crud.get_user(session, user["id"])
        group = db_user.group if db_user else None
        subscribed = db_user.subscribed if db_user else True
        is_teacher = db_user.is_teacher if db_user else False
        teacher_name = db_user.teacher_name if db_user else None
    return web.json_response(
        {
            "ok": True,
            "group": group,
            "subscribed": subscribed,
            "is_teacher": is_teacher,
            "teacher_name": teacher_name,
        }
    )


async def _api_subscribe(request: web.Request) -> web.Response:
    """POST /api/subscribe {subscribe: bool} — вкл/выкл уведомления."""
    user = _user_from_init_data(request)
    if user is None:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad_json"}, status=400)

    async with SessionLocal() as session:
        await crud.get_or_create_user(
            session, user["id"], username=user.get("username")
        )
        await crud.set_subscribed(session, user["id"], bool(body.get("subscribe")))
        await session.commit()
    return web.json_response({"ok": True})


async def _api_groups(request: web.Request) -> web.Response:
    """GET /api/groups — список групп (для выбора в Mini App)."""
    user = _user_from_init_data(request)
    if user is None:
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    async with SessionLocal() as session:
        groups = await crud.get_groups(session)
    return web.json_response({"ok": True, "groups": groups})


# ------------------------------------------------------------------ сборка --


def build_app() -> web.Application:
    """Собирает aiohttp-приложение с API и статикой Mini App."""
    app = web.Application()
    app.router.add_get("/api/auth", _api_auth)
    app.router.add_get("/api/schedule", _api_schedule)
    app.router.add_get("/api/user", _api_user)
    app.router.add_post("/api/subscribe", _api_subscribe)
    app.router.add_get("/api/groups", _api_groups)

    # Статика Mini App: / -> index.html, /app.js, /style.css.
    if MINI_APP_DIR.is_dir():
        app.router.add_static("/", MINI_APP_DIR, show_index=False)
        app.router.add_get("/", lambda r: web.FileResponse(MINI_APP_DIR / "index.html"))
        logger.info("Mini App статика: %s", MINI_APP_DIR)
    else:
        logger.warning("папка mini-app/ не найдена — статика не подключена")

    return app


async def start_webapp_server() -> web.AppRunner:
    """Запускает aiohttp-сервер Mini App и возвращает runner для остановки."""
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.WEBAPP_HOST, settings.WEBAPP_PORT)
    await site.start()
    logger.info("Mini App сервер запущен: http://%s:%d", settings.WEBAPP_HOST, settings.WEBAPP_PORT)
    return runner
