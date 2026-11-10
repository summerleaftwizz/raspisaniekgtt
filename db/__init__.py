import logging
import os

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import settings
from db.base import Base
from db import models  # noqa: F401  — регистрирует модели в Base.metadata

logger = logging.getLogger(__name__)


def _ensure_sqlite_dir(url: str) -> None:
    """Создаёт каталог для файла SQLite, если его ещё нет (например, /app/data)."""
    if not url.startswith("sqlite"):
        return
    if ":///" not in url:
        return
    path = url.split(":///", 1)[1]
    if not path or path == ":memory:" or path.startswith("file:"):
        return
    directory = os.path.dirname(path)
    if not directory:
        return
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        logger.warning("не удалось создать каталог БД %s: %s", directory, exc)


def _normalize_database_url(url: str) -> str:
    """Приводит DATABASE_URL к асинхронному драйверу asyncpg.

    Railway/Amvera часто выдают URL вида 'postgresql://...' (без +asyncpg),
    а create_async_engine требует 'postgresql+asyncpg://...'.
    """
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


_ensure_sqlite_dir(_normalize_database_url(settings.DATABASE_URL))
engine = create_async_engine(
    _normalize_database_url(settings.DATABASE_URL), echo=False, pool_pre_ping=True
)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Создаёт таблицы, если их ещё нет, и применяет миграции схемы."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_schema)


def _migrate_schema(sync_conn) -> None:
    """Применяет безопасные миграции схемы при старте (без потери данных)."""
    _fix_users_id_sqlite(sync_conn)
    _add_column_if_missing(sync_conn, "schedule_entries", "teacher", "VARCHAR(255)")
    _add_column_if_missing(sync_conn, "schedule_entries", "subgroups", "VARCHAR(1024)")
    _add_column_if_missing(sync_conn, "users", "username", "VARCHAR(255)")
    _add_column_if_missing(sync_conn, "users", "auto_delete", "BOOLEAN")
    _add_column_if_missing(sync_conn, "users", "is_teacher", "BOOLEAN")
    _add_column_if_missing(sync_conn, "users", "teacher_name", "VARCHAR(255)")
    for slot in (1, 2, 3):
        _add_column_if_missing(sync_conn, "users", f"favorite_{slot}", "VARCHAR(255)")


def _fix_users_id_sqlite(sync_conn) -> None:
    """Исправляет устаревшую схему SQLite: users.id был BIGINT, а SQLite
    автоинкрементит только INTEGER PRIMARY KEY. Пересоздаём таблицу через
    RENAME + копирование строк (данные сохраняются, без DROP существующих строк).
    """
    if sync_conn.dialect.name != "sqlite":
        return
    # PRAGMA table_info(users) -> (cid, name, type, notnull, dflt_value, pk)
    rows = sync_conn.exec_driver_sql("PRAGMA table_info(users)").fetchall()
    id_col = next((r for r in rows if r[1] == "id"), None)
    if id_col is None or str(id_col[2]).upper() != "BIGINT":
        return
    sync_conn.exec_driver_sql("ALTER TABLE users RENAME TO users_old")
    models.User.__table__.create(sync_conn)
    sync_conn.exec_driver_sql(
        'INSERT INTO users (id, telegram_id, "group", subscribed, inactive, created_at) '
        'SELECT id, telegram_id, "group", subscribed, inactive, created_at FROM users_old'
    )
    sync_conn.exec_driver_sql("DROP TABLE users_old")


def _add_column_if_missing(sync_conn, table_name: str, column: str, col_type: str) -> None:
    """Добавляет колонку в таблицу, если её ещё нет (без потери данных)."""
    insp = sa_inspect(sync_conn)
    existing = {c["name"] for c in insp.get_columns(table_name)}
    if column in existing:
        return
    sync_conn.exec_driver_sql(f'ALTER TABLE "{table_name}" ADD COLUMN "{column}" {col_type}')


async def dispose_db() -> None:
    await engine.dispose()
