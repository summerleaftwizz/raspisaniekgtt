"""Парсер Excel-расписания (якорный подход по точным координатам группы).

Принцип — никаких поисков по ключевым словам («Дисциплина», «Предмет» и т.п.):
1. Читаем Excel с header=None («сырая» сетка).
2. Ищем ячейку, точно совпадающую с маской названия группы
   (например, «1ОГР-25-9»). Запоминаем её координаты (row, col).
3. Ориентация:
   * группы идут в строку (одинаковый row) → данные берём из столбца группы,
     читая вниз;
   * группы идут в столбик (одинаковый col) → данные берём из строки группы,
     читая вправо.
4. Номер пары — это колонка, где больше всего целых чисел 1..12 (ищем без
   ключевых слов).
5. Преподаватель под предметом — обычный учитель пары, а не «1 п/г». Подгруппы
   — только если явно есть вторая ячейка с тем же предметом / вторая аудитория
   в той же строке.

Если группа не найдена или данные выглядят подозрительно (все пары «окно» /
пустые) — ошибка в лог, а не молча пустое расписание.
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


# ------------------------------------------------------- низкоуровневые ----


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


def _is_subject_like(value: str) -> bool:
    """Похоже ли на название предмета, а не на кабинет/ФИО."""
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


# ------------------------------------------------------ поиск колонки пары ----


def _find_lesson_col(df: pd.DataFrame) -> int:
    """Колонка с номерами пар: та, где больше всего целых чисел 1..12.

    Ищем без ключевых слов — только по содержимому ячеек.
    """
    best_col, best_score = 0, 0
    nrows = len(df)
    for c in range(df.shape[1]):
        score = 0
        for r in range(nrows):
            v = _to_int(df.iat[r, c])
            if v is not None and 1 <= v <= 12:
                score += 1
        if score > best_score:
            best_col, best_score = c, score
    return best_col if best_score > 0 else 0


# --------------------------------------------------------グループ координаты ----


def _find_group_cells(df: pd.DataFrame, cfg: ParserConfig) -> list[tuple[int, int, str]]:
    """Все ячейки, точно совпадающие с маской названия группы (row, col, name)."""
    cells: list[tuple[int, int, str]] = []
    for r in range(len(df)):
        for c in range(df.shape[1]):
            v = _cell_to_str(df.iat[r, c])
            if _is_group_name(v, cfg):
                cells.append((r, c, v))
    return cells


# --------------------------------------------- чтение сетки с merge-ячейками ---


def _read_sheet_grid(content: bytes, sheet_name: str) -> pd.DataFrame:
    """Читает лист как «сырую» сетку с РАЗВЁРНУТЫМИ объединёнными ячейками.

    openpyxl хранит значение только в левой-верхней ячейке merged-области,
    остальные возвращает как None. Здесь мы протягиваем значение каждой
    объединённой области на все её ячейки — чтобы предмет/преподаватель,
    занимающие несколько строк, читались на каждой строке пары.
    """
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    ws = wb[sheet_name]

    grid: list[list] = [[cell.value for cell in row] for row in ws.iter_rows()]

    # Разворачиваем каждую объединённую область вниз и вправо.
    for merged in ws.merged_cells.ranges:
        value = ws.cell(row=merged.min_row, column=merged.min_col).value
        if value is None:
            continue
        for ri in range(merged.min_row - 1, min(merged.max_row, len(grid))):
            for ci in range(merged.min_col - 1, min(merged.max_col, len(grid[ri]))):
                grid[ri][ci] = value

    return pd.DataFrame(grid, dtype=object)


def _parse_sheet_by_groups(
    df: pd.DataFrame, cfg: ParserConfig, content: bytes, sheet_name: str
) -> dict[str, list[Lesson]]:
    """Якорный парсинг: у каждой группы свой блок столбцов справа.

    Схема файла (подтверждена на реальных данных):
      строка N:  1ИСИП-25-9 (J) | 1ОГР-25-9 (N) | 1ЭП-26-9 (S)
      строка N+1: данные пар — по 3 столбца на группу:
                  [предмет] [преподаватель] [кабинет]

    Блок группы = от её столбца до столбца следующей группы (или конца листа).
    Данные читаются ТОЛЬКО из этого блока — в столбцы соседей не залезаем.
    """
    group_cells = _find_group_cells(df, cfg)
    if not group_cells:
        raise ParseError(
            "не найдены названия групп на листе %s (маска: %s)"
            % (sheet_name, cfg.GROUP_NAME_PATTERN)
        )

    # Строка с названиями групп: та, где их больше всего.
    by_row: dict[int, list[tuple[int, str]]] = {}
    for r, c, name in group_cells:
        by_row.setdefault(r, []).append((c, name))
    header_row = max(by_row, key=lambda k: len(by_row[k]))
    cells = sorted(by_row[header_row], key=lambda x: x[0])

    ncols = df.shape[1]
    groups: dict[str, list[Lesson]] = {}

    for i, (sc, name) in enumerate(cells):
        # Границы блока группы: до столбца следующей группы.
        block_end = cells[i + 1][0] if i + 1 < len(cells) else ncols
        block_cols = list(range(sc, block_end))

        lessons: list[Lesson] = []
        last_subject = ""
        last_teacher = ""
        last_room = ""
        seen_numbers: set[int] = set()

        for r in range(header_row + 1, len(df)):
            # Номер пары — только в первом столбце блока группы.
            lesson_number = _to_int(df.iat[r, sc])
            if lesson_number is None or lesson_number in seen_numbers:
                continue
            seen_numbers.add(lesson_number)

            # Читаем ровно три столбца блока: предмет, преподаватель, кабинет.
            subject = _cell_to_str(df.iat[r, block_cols[0]]) if block_cols else ""
            teacher = (
                _normalize_teacher(_cell_to_str(df.iat[r, block_cols[1]]))
                if len(block_cols) > 1 else ""
            )
            room = (
                _clean_room(_cell_to_str(df.iat[r, block_cols[2]]))
                if len(block_cols) > 2 else ""
            )

            # Если ячейка пустая (не было merge) — берём предыдущее значение:
            # так «размазанные» пары читаются один раз.
            if not subject:
                subject = last_subject
            else:
                last_subject = subject
            if not teacher:
                teacher = last_teacher
            else:
                last_teacher = teacher
            if not room:
                room = last_room
            else:
                last_room = room

            lessons.append(
                Lesson(
                    lesson_number=lesson_number,
                    time="",
                    subject=subject,
                    room=room,
                    teacher=teacher,
                    subgroups=[],
                )
            )

        groups.setdefault(name, []).extend(lessons)

    # Строгая проверка: если у ВСЕХ групп нет ни одного непустого предмета —
    # это ошибка формата, а не «пустое расписание».
    filled = [
        l for lessons in groups.values() for l in lessons
        if l.subject.strip() and l.subject.strip().lower() not in {"окно", "-", "—"}
    ]
    if not filled:
        logger.error(
            "лист %s: все пары пустые/«окно» у групп %s — проверь структуру файла",
            sheet_name, list(groups.keys()),
        )
        raise ParseError(
            "лист %s: не найдено ни одной заполненной пары у групп %s"
            % (sheet_name, ", ".join(groups.keys()))
        )

    return groups


# ------------------------------------------------------ чтение одной группы ---


def _find_room_right(df: pd.DataFrame, r: int, sc: int, boundary: int) -> str:
    """Ближайшая ячейка справа от sc (до boundary), похожая на аудиторию."""
    for c in range(sc + 1, boundary):
        cell = _cell_to_str(df.iat[r, c])
        if not cell:
            continue
        if _is_fio(cell):
            continue
        if _is_subject_like(cell):
            continue
        room = _clean_room(cell)
        if room:
            return room
    return ""


def _find_teacher_below(df: pd.DataFrame, r: int, sc: int) -> str:
    """ФИО в ячейке (r, sc) — преподаватель пары из строки ниже."""
    if r >= len(df):
        return ""
    cell = _cell_to_str(df.iat[r, sc])
    return _normalize_teacher(cell) if _is_fio(cell) else ""


def _extract_subgroups(
    df, r: int, sc: int, block_end: int, lesson_col: int, cfg: ParserConfig,
    stop_cols: frozenset = frozenset(), main_subject: str = "",
    col_limit: Optional[int] = None,
) -> list[SubgroupInfo]:
    """Собирает подгруппы из ячеек строки r, начиная с колонки sc.

    Сохранено для обратной совместимости с тестами. col_limit — жёсткая
    граница по колонкам (столбец следующей группы), чтобы подгруппы не
    «перетекали» в соседние группы.
    """
    ncols = df.shape[1] if col_limit is None else min(col_limit, df.shape[1])
    has_teacher_row = (
        r + 1 < block_end and _to_int(df.iat[r + 1, lesson_col]) is None
    )

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

    start(teacher=teacher_under(sc))

    empty_gap = 0
    for c in range(sc + 1, ncols):
        cell = _cell_to_str(df.iat[r, c])
        under = teacher_under(c)

        if c in stop_cols and not (
            _is_subject_like(cell) and _same_subject(cell, main_subject)
        ) and not _looks_like_data(cell):
            break

        if cell and not _is_group_name(cell, cfg):
            if _is_fio(cell):
                if current is not None and not current.teacher:
                    current.teacher = cell
                else:
                    start(teacher=cell)
            elif _is_subject_like(cell) and (
                _same_subject(cell, main_subject) or not _looks_like_data(cell)
            ):
                start(teacher=under)
            else:
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

    result: list[SubgroupInfo] = []
    for sub in subs:
        if not sub.room and not sub.teacher:
            continue
        if result and result[-1].room == sub.room and result[-1].teacher == sub.teacher:
            continue
        result.append(sub)

    for i, sub in enumerate(result, 1):
        sub.number = i
    return result


def _looks_like_data(value: str) -> bool:
    if not value:
        return False
    if _is_fio(value):
        return True
    return any(ch.isdigit() for ch in value)


def _read_pair(
    df: pd.DataFrame,
    r: int,
    sc: int,
    next_sc: int,
    lesson_col: int,
    cfg: ParserConfig,
) -> tuple[str, str, list]:
    """Читает одну пару (предмет, каб, преподаватель, подгруппы) из строки r.

    Якорь — колонка sc группы. Данные только из этой строки и колонки sc.
    """
    subject = _cell_to_str(df.iat[r, sc])

    # Подгруппы: сканируем ячейки справа от sc до границы следующей группы.
    stop_cols = frozenset(
        c for c in range(sc + 1, next_sc) if _is_group_name(_cell_to_str(df.iat[r, c]), cfg)
    )
    subgroups = _extract_subgroups(
        df, r, sc, len(df), lesson_col, cfg,
        stop_cols=stop_cols, main_subject=subject, col_limit=next_sc,
    )

    if len(subgroups) >= 2:
        room = subgroups[0].room or ""
        teacher = subgroups[0].teacher or ""
    elif len(subgroups) == 1:
        room = subgroups[0].room or ""
        teacher = subgroups[0].teacher or ""
        subgroups = []
    else:
        room = _find_room_right(df, r, sc, next_sc)
        teacher = _find_teacher_below(df, r + 1, sc)
        subgroups = []

    return room, teacher, subgroups


def _parse_group_column(
    df: pd.DataFrame,
    header_row: int,
    sc: int,
    next_sc: int,
    lesson_col: int,
    cfg: ParserConfig,
) -> list[Lesson]:
    """Читает все пары группы из столбца sc (группы идут в строку)."""
    name = _cell_to_str(df.iat[header_row, sc])
    lessons: list[Lesson] = []
    for r in range(header_row + 1, len(df)):
        lesson_number = _to_int(df.iat[r, lesson_col])
        if lesson_number is None:
            continue  # строка с ФИО преподавателя или пустая
        room, teacher, subgroups = _read_pair(df, r, sc, next_sc, lesson_col, cfg)
        lessons.append(
            Lesson(
                lesson_number=lesson_number,
                time="",
                subject=_cell_to_str(df.iat[r, sc]),
                room=room,
                teacher=teacher,
                subgroups=subgroups,
            )
        )
    return lessons


def _parse_group_row(
    df: pd.DataFrame,
    header_col: int,
    sr: int,
    next_sr: int,
    lesson_col: int,
    cfg: ParserConfig,
) -> list[Lesson]:
    """Читает все пары группы из строки sr (группы идут в столбик)."""
    name = _cell_to_str(df.iat[sr, header_col])
    lessons: list[Lesson] = []
    for c in range(header_col + 1, df.shape[1]):
        lesson_number = _to_int(df.iat[sr, c])  # не используется для строки
        # Для вертикальной ориентации данные идут вправо: число пары,
        # затем предмет, каб, преподаватель — в同じ строке. Читаем пары
        # последовательностью: [номер][предмет][каб][преподаватель]...
        # Упрощённо: каждая пара занимает 4 ячейки вправо.
        idx = c
        while idx < next_sr:
            num_cell = _cell_to_str(df.iat[sr, idx])
            ln = _to_int(num_cell)
            if ln is None:
                break
            subject = _cell_to_str(df.iat[sr, idx + 1]) if idx + 1 < next_sr else ""
            room = _clean_room(_cell_to_str(df.iat[sr, idx + 2])) if idx + 2 < next_sr else ""
            teacher = _normalize_teacher(_cell_to_str(df.iat[sr, idx + 3])) if idx + 3 < next_sr else ""
            lessons.append(
                Lesson(
                    lesson_number=ln,
                    time="",
                    subject=subject,
                    room=room,
                    teacher=teacher,
                    subgroups=[],
                )
            )
            idx += 4
        break  # одна строка — одна группа, выходим
    return lessons


# ----------------------------------------------------------- основной парсинг ----


def _parse_sheet(df: pd.DataFrame, cfg: ParserConfig) -> dict[str, list[Lesson]]:
    """Якорный парсинг листа по точным координатам названий групп."""
    ncols = df.shape[1]
    lesson_col = _find_lesson_col(df)
    group_cells = _find_group_cells(df, cfg)

    if not group_cells:
        raise ParseError("не найдены названия групп на листе (маска: %s)" % cfg.GROUP_NAME_PATTERN)

    rows = {rc for rc, _, _ in group_cells}
    cols = {cc for _, cc, _ in group_cells}

    groups: dict[str, list[Lesson]] = {}
    all_suspicious = True

    if len(rows) == 1:
        # Группы идут в строку → читаем каждый столбец группы вниз.
        header_row = list(rows)[0]
        sorted_cells = sorted(group_cells, key=lambda x: x[1])
        for i, (r, sc, name) in enumerate(sorted_cells):
            next_sc = sorted_cells[i + 1][1] if i + 1 < len(sorted_cells) else ncols
            lessons = _parse_group_column(df, r, sc, next_sc, lesson_col, cfg)
            groups.setdefault(name, []).extend(lessons)
            if lessons and any(l.subject or l.room or l.teacher for l in lessons):
                all_suspicious = False
    elif len(cols) == 1:
        # Группы идут в столбик → читаем каждую строку группы вправо.
        header_col = list(cols)[0]
        sorted_cells = sorted(group_cells, key=lambda x: x[0])
        for i, (r, cc, name) in enumerate(sorted_cells):
            next_r = sorted_cells[i + 1][0] if i + 1 < len(sorted_cells) else len(df)
            lessons = _parse_group_row(df, cc, r, next_r, lesson_col, cfg)
            groups.setdefault(name, []).extend(lessons)
            if lessons and any(l.subject or l.room or l.teacher for l in lessons):
                all_suspicious = False
    else:
        # Смешанная разметка: несколько строк с названиями групп — каждая строка
        # — отдельный блок. Читаем как «группы в строке» для каждой такой строки.
        by_row: dict[int, list[tuple[int, str]]] = {}
        for r, c, name in group_cells:
            by_row.setdefault(r, []).append((c, name))
        for r in sorted(by_row):
            cells = sorted(by_row[r], key=lambda x: x[0])
            for i, (sc, name) in enumerate(cells):
                next_sc = cells[i + 1][0] if i + 1 < len(cells) else ncols
                lessons = _parse_group_column(df, r, sc, next_sc, lesson_col, cfg)
                groups.setdefault(name, []).extend(lessons)
                if lessons and any(l.subject or l.room or l.teacher for l in lessons):
                    all_suspicious = False

    if not groups:
        raise ParseError("не удалось извлечь пары ни для одной группы на листе")

    # Строгая проверка: если все пары во всех группах — «окно»/пустые,
    # это подозрительно — пишем ошибку в лог (не молчим).
    if all_suspicious:
        logger.error(
            "расписание выглядит подозрительно: все пары — окна/пустые, "
            "группы: %s", list(groups.keys())
        )

    return groups


def parse_schedule(content: bytes, cfg: ParserConfig) -> dict[str, list[Lesson]]:
    """Парсит xlsx. Возвращает {группа: [Lesson, ...]}.

    Основной путь — якорный парсинг по координатам названий групп с
    разворотом объединённых ячеек (_parse_sheet_by_groups). Если он не смог
    разобрать лист, пробуем универсальный _parse_sheet.
    """
    try:
        excel = pd.ExcelFile(io.BytesIO(content), engine="openpyxl")
    except Exception as exc:
        raise ParseError(f"не удалось открыть Excel: {exc}") from exc

    groups: dict[str, list[Lesson]] = {}
    errors: list[str] = []
    for sheet_name in excel.sheet_names:
        sheet_groups: dict[str, list[Lesson]] = {}
        # 1) основной путь: сетка с развёрнутыми merged-ячейками
        try:
            df_full = _read_sheet_grid(content, sheet_name)
            sheet_groups = _parse_sheet_by_groups(df_full, cfg, content, sheet_name)
        except Exception as exc:
            errors.append(f"{sheet_name} (grid): {exc}")
            logger.warning("лист %s (grid) не распарсился: %s", sheet_name, exc)
        # 2) резервный путь: обычное чтение pandas + универсальный разбор
        if not sheet_groups:
            try:
                df_plain = pd.read_excel(
                    excel, sheet_name=sheet_name, header=None, dtype=object
                )
                sheet_groups = _parse_sheet(df_plain, cfg)
            except Exception as exc:
                errors.append(f"{sheet_name} (plain): {exc}")
                logger.warning("лист %s (plain) не распарсился: %s", sheet_name, exc)
        for g, lessons in sheet_groups.items():
            groups.setdefault(g, []).extend(lessons)

    if not groups:
        raise ParseError("ни один лист не удалось распарсить: " + "; ".join(errors))
    return groups
