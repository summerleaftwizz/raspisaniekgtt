"""Парсер Excel-расписания (реальный формат колледжа).

Формат файла (проверено на реальных данных):
  * один .xlsx = одна дата; лист(ы) называются по дню недели;
  * группы идут горизонтальными блоками (по несколько в ряд), а блоки —
    друг под другом:
        Пара | Дисциплина (гр.1) ... | Ауд. (гр.1) ... | Дисциплина (гр.2) ...
        №    | Преподаватель        |                 | ...
             | 1  2  3 (подгруппы)  |                 | ...
             | 1ОГР-26-9 | ...      |                 | ...
        1    | История  | ...       | 304 | ...       | ...
        2    | ...      | ...       | ... | ...       | ...
  * колонка 0 = номер пары; строки с ФИО преподавателя чередуются со строками
    пар и пропускаются (в колонке 0 пусто);
  * у каждой группы две колонки: предмет (заголовок «Дисциплина»/«Предмет»)
    и кабинет (заголовок «Ауд.»/«Кабинет»).

Колонки находятся по синонимам из config.ParserConfig, названия групп — по
regex GROUP_NAME_PATTERN.
"""
import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from config import ParserConfig

logger = logging.getLogger(__name__)


class ParseError(Exception):
    pass


@dataclass
class SubgroupInfo:
    """Одна подгруппа пары: номер, аудитория и преподаватель из ячеек строки."""

    number: int
    room: Optional[str] = None
    teacher: Optional[str] = None


@dataclass
class Lesson:
    lesson_number: int
    time: str
    subject: str
    room: str
    teacher: str = ""
    # Полный список подгрупп (для пар с разбивкой, например «Иностранный язык»).
    subgroups: list = field(default_factory=list)


def _cell_to_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if pd.isna(value):
            return ""
        if value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, str):
        return value.strip()
    return str(value)


def _to_int(value) -> Optional[int]:
    s = re.sub(r"[^\d]", "", _cell_to_str(value))
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _normalize_teacher(value) -> str:
    """Нормализует ФИО: схлопывает пробелы/табы/переносы строк в один пробел."""
    s = _cell_to_str(value)
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def _matches_synonym(value: str, synonyms: tuple) -> bool:
    for syn in synonyms:
        if value == syn:
            return True
    for syn in synonyms:
        if syn and syn in value:
            return True
    return False


def _find_columns(df: pd.DataFrame, cfg: ParserConfig) -> tuple[int, list[int], list[int]]:
    """Возвращает (колонка номера пары, [колонки предметов], [колонки кабинетов])."""
    ncols = df.shape[1]
    scan = min(cfg.HEADER_SCAN_ROWS, len(df))
    lesson_col: Optional[int] = None
    subject_cols: list[int] = []
    room_cols: list[int] = []

    for r in range(scan):
        for c in range(ncols):
            v = _cell_to_str(df.iat[r, c]).lower()
            if not v:
                continue
            if lesson_col is None and _matches_synonym(v, cfg.LESSON_NUM_SYNONYMS):
                lesson_col = c
            if _matches_synonym(v, cfg.SUBJECT_SYNONYMS) and c not in subject_cols:
                subject_cols.append(c)
            if _matches_synonym(v, cfg.ROOM_SYNONYMS) and c not in room_cols:
                room_cols.append(c)

    subject_cols.sort()
    room_cols.sort()

    if lesson_col is None:
        lesson_col = 0

    if not subject_cols:
        # фолбэк: колонки заданы индексами в config
        if isinstance(cfg.COL_SUBJECT, int):
            lesson_col = cfg.COL_LESSON_NUM if isinstance(cfg.COL_LESSON_NUM, int) else 0
            subject_cols = [cfg.COL_SUBJECT]
            room_cols = [cfg.COL_ROOM] if isinstance(cfg.COL_ROOM, int) else []
        else:
            raise ParseError("не найдены колонки предмета (Дисциплина/Предмет)")

    return lesson_col, subject_cols, room_cols


def _is_group_name(value: str, cfg: ParserConfig) -> bool:
    return bool(value) and re.match(cfg.GROUP_NAME_PATTERN, value) is not None


# ФИО: «Кобзова С. В.», «Ощепкова А.С.», «Иванов Иван И.»
FIO_RE = re.compile(
    r"^[А-ЯЁA-Z][а-яёa-z\-]+(?:\s+[А-ЯЁA-Z][а-яёa-z\-]+)?"
    r"\s+[А-ЯЁA-Z]\.\s*[А-ЯЁA-Z]?\.?$"
)
# Аудитория: служебный префикс + номер («Ино 319» -> «319»)
ROOM_NUMBER_RE = re.compile(r"\d+\s*[A-Za-zА-Яа-я]?")


def _is_fio(value: str) -> bool:
    """Похоже ли на ФИО в формате «Фамилия И. О.» (значение — из ячейки)."""
    return bool(FIO_RE.match(_normalize_teacher(value)))


def _clean_room(value: str) -> str:
    """Убирает служебный префикс аудитории: «Ино 319» -> «319».

    Если цифр нет («Спортзал», «Акт. зал») — возвращает строку как есть.
    """
    s = _normalize_teacher(value)
    if not s:
        return ""
    m = ROOM_NUMBER_RE.search(s)
    return m.group(0).strip() if m else s


def _looks_like_data(value: str) -> bool:
    """Ячейка похожа на данные пары (аудитория с цифрами или ФИО),
    а не на название чужого предмета («Химия», «Математика»)."""
    if not value:
        return False
    if _is_fio(value):
        return True
    return any(ch.isdigit() for ch in value)


def _is_subject_like(value: str) -> bool:
    """Похоже ли на название предмета, а не на кабинет/ФИО.

    «Иностранный язык», «Физическая культура» — да; «121», «Ино 319»,
    «Кобзова С. В.» — нет.
    """
    value = _normalize_teacher(value)
    if not value or _is_fio(value):
        return False
    if any(ch.isdigit() for ch in value):
        return False
    return " " in value or len(value) > 9


def _same_subject(a: str, b: str) -> bool:
    """Один и тот же предмет (подгруппы одного предмета пишут одинаково)."""
    a, b = _normalize_teacher(a).lower(), _normalize_teacher(b).lower()
    if not a or not b:
        return False
    return a.startswith(b) or b.startswith(a)


def _extract_subgroups(
    df, r: int, sc: int, block_end: int, lesson_col: int, cfg: ParserConfig,
    stop_cols: frozenset = frozenset(), main_subject: str = "",
) -> list[SubgroupInfo]:
    """Собирает подгруппы из ВСЕХ колонок строки расписания.

    Поддерживаются оба реальных формата:

    1) подгруппы двумя парами колонок, преподаватели — строкой ниже:
         Иностранный язык | 121 | Иностранный язык | 202
         Байшева Я. В.    |     | Ощепкова А. С.   |
    2) подгруппы в одной строке подряд:
         Иностранный язык | Ино 319 | Кобзова С. В. | Ино 202 | Ощепкова А. С.

    Каждая ячейка классифицируется динамически (ФИО / аудитория / название
    предмета подгруппы), пары собираются по порядку колонок. Ранних выходов
    нет — обрабатываются все подгруппы, сколько бы их ни было.
    """
    ncols = df.shape[1]
    has_teacher_row = (
        r + 1 < block_end and _to_int(df.iat[r + 1, lesson_col]) is None
    )

    # --- Явный признак деления на подгруппы ---------------------------------
    # Подгруппы «придумывать» нельзя: если в строке нет ВТОРОЙ ячейки с тем же
    # предметом (формат «предмет|ауд|предмет|ауд») и нет второго ФИО в самой
    # строке (инлайн-формат) — это обычная пара, возвращаем пустой список.
    # Преподаватель под предметом — это обычный учитель пары, а не «1 п/г».
    evidence = False
    for c in range(sc + 1, ncols):
        cell_probe = _cell_to_str(df.iat[r, c])
        if not cell_probe:
            continue
        if _is_subject_like(cell_probe) and _same_subject(cell_probe, main_subject):
            evidence = True
            break
        if _is_fio(cell_probe):
            evidence = True
            break
    if not evidence:
        return []

    def teacher_under(col: int) -> str:
        if not has_teacher_row:
            return ""
        fio = _normalize_teacher(df.iat[r + 1, col])
        return fio if fio and _is_fio(fio) else ""

    subs: list[SubgroupInfo] = []
    current: Optional[SubgroupInfo] = None

    def start(room: Optional[str] = None, teacher: str = "") -> SubgroupInfo:
        nonlocal current
        current = SubgroupInfo(number=0, room=room or None, teacher=teacher or None)
        subs.append(current)
        return current

    # первая подгруппа: предмет в колонке sc, преподаватель — под ним
    start(teacher=teacher_under(sc))

    empty_gap = 0
    for c in range(sc + 1, ncols):
        cell = _cell_to_str(df.iat[r, c])
        under = teacher_under(c)

        if c in stop_cols and not (
            _is_subject_like(cell) and _same_subject(cell, main_subject)
        ) and not _looks_like_data(cell):
            break  # колонка предмета другой группы

        if cell and not _is_group_name(cell, cfg):
            if _is_fio(cell):
                if current is not None and not current.teacher:
                    current.teacher = cell
                else:
                    start(teacher=cell)
            elif _is_subject_like(cell) and (
                _same_subject(cell, main_subject) or not _looks_like_data(cell)
            ):
                # предмет второй подгруппы: начинаем новую, ФИО берём под ним
                start(teacher=under)
            else:
                # аудитория
                room = _clean_room(cell) or None
                if current is not None and not current.room:
                    current.room = room
                else:
                    start(room=room)
            empty_gap = 0
            continue

        if under:
            if current is not None and not current.teacher:
                current.teacher = under
            else:
                start(teacher=under)
            empty_gap = 0
            continue

        empty_gap += 1
        if empty_gap > 3:
            break

    # отбрасываем пустые и дубли (одна и та же пара) 
    result: list[SubgroupInfo] = []
    for sub in subs:
        if not sub.room and not sub.teacher:
            continue
        if result and result[-1].room == sub.room and result[-1].teacher == sub.teacher:
            continue
        result.append(sub)

    for i, sub in enumerate(result, 1):
        sub.number = i
    # --- ВРЕМЕННЫЙ DEBUG (удалить после диагностики) ----------------------
    try:
        _subject = _cell_to_str(df.iat[r, sc]) if sc < df.shape[1] else ""
        if "иностранн" in _subject.lower() or len(result) >= 2:
            print(
                "DEBUG SUBGROUPS: row=%s sc=%s subject=%r stop_cols=%s -> %s"
                % (
                    r,
                    sc,
                    _subject,
                    sorted(stop_cols),
                    [(s.number, s.room, s.teacher) for s in result],
                ),
                flush=True,
            )
    except Exception:  # noqa: BLE001
        pass
    # ----------------------------------------------------------------------
    return result


def _parse_sheet(
    df: pd.DataFrame, cfg: ParserConfig, lesson_col: int, subject_cols: list[int], room_cols: list[int]
) -> dict[str, list[Lesson]]:
    pairs = [
        (sc, room_cols[i] if i < len(room_cols) else sc + 4)
        for i, sc in enumerate(subject_cols)
    ]
    nrows = len(df)

    # строки-границы блоков (строки с названиями групп)
    block_rows: list[int] = []
    for r in range(nrows):
        for sc, _ in pairs:
            if _is_group_name(_cell_to_str(df.iat[r, sc]), cfg):
                block_rows.append(r)
                break

    if not block_rows:
        raise ParseError("не найдены названия групп на листе")

    groups: dict[str, list[Lesson]] = {}
    for bi, block_start in enumerate(block_rows):
        block_end = block_rows[bi + 1] if bi + 1 < len(block_rows) else nrows
        names = [_cell_to_str(df.iat[block_start, sc]) for sc, _ in pairs]

        for r in range(block_start + 1, block_end):
            # --- ВРЕМЕННЫЙ DEBUG (удалить после диагностики) --------------
            try:
                _dbg_row = [
                    (c, _cell_to_str(df.iat[r, c]))
                    for c in range(df.shape[1])
                    if _cell_to_str(df.iat[r, c])
                ]
                if any("иностранн" in str(v).lower() for _, v in _dbg_row):
                    print(f"DEBUG ROW: sheet_row={r} cells={_dbg_row}", flush=True)
                    print(
                        "DEBUG COLS: lesson_col=%s pairs=%s subject_cols=%s"
                        % (lesson_col, pairs, subject_cols),
                        flush=True,
                    )
            except Exception as _dbg_exc:  # noqa: BLE001
                print(f"DEBUG ROW ERROR: {_dbg_exc}", flush=True)
            # --------------------------------------------------------------

            lesson_number = _to_int(df.iat[r, lesson_col])
            if lesson_number is None:
                continue  # строка с ФИО преподавателя или пустая
            for gi, (sc, rc) in enumerate(pairs):
                name = names[gi]
                if not name:
                    continue
                subject = _cell_to_str(df.iat[r, sc])
                room = _clean_room(_cell_to_str(df.iat[r, rc]))
                teacher = ""
                if r + 1 < block_end and _to_int(df.iat[r + 1, lesson_col]) is None:
                    # следующая строка — ФИО преподавателя (в колонке предмета)
                    teacher = _normalize_teacher(df.iat[r + 1, sc])
                # подгруппы: все ячейки справа от предмета (иностранный язык и др.)
                stop_cols = frozenset(
                    other_sc for other_sc, _ in pairs[gi + 1:] if other_sc > sc
                )
                subgroups = _extract_subgroups(
                    df, r, sc, block_end, lesson_col, cfg,
                    stop_cols=stop_cols, main_subject=subject,
                )
                # разбивка на подгруппы — только если пар две и больше
                if len(subgroups) >= 2:
                    # обратная совместимость: room/teacher — данные 1-й подгруппы
                    room = subgroups[0].room or room
                    teacher = subgroups[0].teacher or teacher
                elif len(subgroups) == 1:
                    # одна пара: обычный формат, поля берём из неё
                    room = subgroups[0].room or room
                    teacher = subgroups[0].teacher or teacher
                    subgroups = []
                else:
                    subgroups = []
                groups.setdefault(name, []).append(
                    Lesson(
                        lesson_number=lesson_number,
                        time="",
                        subject=subject,
                        room=room,
                        teacher=teacher,
                        subgroups=subgroups,
                    )
                )
    return groups


def parse_schedule(content: bytes, cfg: ParserConfig) -> dict[str, list[Lesson]]:
    """Парсит xlsx. Возвращает {группа: [Lesson, ...]}."""
    try:
        excel = pd.ExcelFile(io.BytesIO(content), engine="openpyxl")
    except Exception as exc:
        raise ParseError(f"не удалось открыть Excel: {exc}") from exc

    groups: dict[str, list[Lesson]] = {}
    errors: list[str] = []
    for sheet_name in excel.sheet_names:
        try:
            df = pd.read_excel(excel, sheet_name=sheet_name, header=None, dtype=object)
            lesson_col, subject_cols, room_cols = _find_columns(df, cfg)
            sheet_groups = _parse_sheet(df, cfg, lesson_col, subject_cols, room_cols)
            for g, lessons in sheet_groups.items():
                groups.setdefault(g, []).extend(lessons)
        except Exception as exc:
            errors.append(f"{sheet_name}: {exc}")
            logger.warning("лист %s не распарсился: %s", sheet_name, exc)

    if not groups:
        raise ParseError("ни один лист не удалось распарсить: " + "; ".join(errors))
    return groups
