from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    group: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_teacher: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    teacher_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    subscribed: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    auto_delete: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    inactive: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    favorite_1: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    favorite_2: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    favorite_3: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ScheduleEntry(Base):
    __tablename__ = "schedule_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10), index=True, nullable=False)   # 'DD.MM'
    group: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    lesson_number: Mapped[int] = mapped_column(Integer, nullable=False)
    time: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    subject: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    room: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    teacher: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Подгруппы: JSON-список {"room": "...", "teacher": "..."} (или NULL).
    subgroups: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)


class KnownSchedule(Base):
    __tablename__ = "known_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date_folder: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    notified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ScheduleSnapshot(Base):
    """Предыдущее состояние расписания группы — для уведомлений об изменениях.

    fingerprint — sha256 от нормализованного содержимого пар (номер, время,
    предмет, кабинет, преподаватель, подгруппы). Если новый файл даёт другой
    отпечаток — значит в расписании что-то поменялось (перенос, отмена, замена).
    """

    __tablename__ = "schedule_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10), index=True, nullable=False)   # 'DD.MM'
    group: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Debt(Base):
    """Долги пользователя (строго индивидуальны, привязка по telegram_id)."""

    __tablename__ = "debts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    text: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Deadline(Base):
    """Дедлайны пользователя: due_ts — Unix timestamp, напоминание строго этому telegram_id."""

    __tablename__ = "deadlines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    text: Mapped[str] = mapped_column(String(512), nullable=False)
    due_ts: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    notified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
