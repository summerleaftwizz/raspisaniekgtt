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
    """Похоже ли на название предмета, а не на кабинет/ФИО/номер."""
    value = _normalize_teacher(value)
    if not value or _is_fio(value):
        return False
    if any(ch.isdigit() for ch in value):
        return False
    return True


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
    """Якорный парсинг по координатам названий групп — БЕЗ ключевых слов.

    Алгоритм:
    1. Находим ВСЕ ячейки с названиями групп (regex-маска). В реальном файле
       они раскиданы по нескольким строкам (блоки групп друг под другом) —
       обрабатываем каждую строку-заголовок, а не только одну.
    2. Для каждой группы её блок столбцов = от её столбца до столбца
       следующей группы в той же строке (или до конца листа).
    3. Данные читаем из строк ниже заголовка (до следующей строки-заголовка),
       ТОЛЬКО из столбцов этого блока — в соседей не залезаем.
    4. Номер пары: общий столбец с числами 1..12, иначе первый столбец блока.
    5. Объединённые ячейки уже развёрнуты (_read_sheet_grid), но если ячейка
       всё же пустая — подставляем предыдущее значение (пара «размазана»
       по строкам, читается один раз).
    6. Ошибка по одной группе (пустой блок) пишется в лог, остальные группы
       обрабатываются дальше.
    """
    group_cells = _find_group_cells(df, cfg)
    if not group_cells:
        raise ParseError(
            "не найдены названия групп на листе %s (маска: %s)"
            % (sheet_name, cfg.GROUP_NAME_PATTERN)
        )

    # Группируем названия групп по строкам; сортируем строки сверху вниз.
    by_row: dict[int, list[tuple[int, str]]] = {}
    for r, c, name in group_cells:
        by_row.setdefault(r, []).append((c, name))
    header_rows = sorted(by_row)

    ncols = df.shape[1]
    nrows = len(df)
    lesson_col = _find_lesson_col(df)
    groups: dict[str, list[Lesson]] = {}

    for hi, hr in enumerate(header_rows):
        # Данные этого блока — до следующей строки-заголовка (или конца листа).
        block_rows_end = header_rows[hi + 1] if hi + 1 < len(header_rows) else nrows
        cells = sorted(by_row[hr], key=lambda x: x[0])

        for i, (sc, name) in enumerate(cells):
            # Столбцы блока группы: от sc до столбца следующей группы.
            block_end = cells[i + 1][0] if i + 1 < len(cells) else ncols
            block_cols = list(range(sc, block_end))
            if not block_cols:
                continue
            logger.info(
                "лист %s, группа %s: блок столбцов %d..%d, строки %d..%d",
                sheet_name, name, sc + 1, block_end, hr + 2, block_rows_end,
            )

            lessons: list[Lesson] = []
            seen_numbers: set[int] = set()
            last_subject = last_teacher = last_room = ""

            for r in range(hr + 1, block_rows_end):
                # Номер пары: общий столбец чисел, иначе первый столбец блока.
                lesson_number = _to_int(df.iat[r, lesson_col])
                if lesson_number is None:
                    lesson_number = _to_int(df.iat[r, sc])
                if lesson_number is None or lesson_number in seen_numbers:
                    continue
                seen_numbers.add(lesson_number)

                # Читаем блок: предмет — первый subject-like столбец,
                # преподаватель — первое ФИО, кабинет — первая аудитория.
                subject = teacher = room = ""
                for c in block_cols:
                    cell = _cell_to_str(df.iat[r, c])
                    if not cell:
                        continue
                    if not subject and _is_subject_like(cell):
                        subject = cell
                    elif not teacher and _is_fio(cell):
                        teacher = _normalize_teacher(cell)
                    elif not room and not _is_fio(cell) and not _is_subject_like(cell):
                        room = _clean_room(cell)

                # Кабинета нет в строке пары — ищем строкой ниже (под
                # предметом/преподавателем), в пределах блока.
                if not room:
                    room = _find_room_nearby(
                        df, r, sc, block_end, block_rows_end, lesson_col
                    )
                    if room:
                        logger.debug(
                            "лист %s, %s: кабинет %r найден строкой ниже для r=%d",
                            sheet_name, name, room, r + 1,
                        )
                    else:
                        logger.info(
                            "лист %s, %s: пара r=%d без кабинета; ячейки блока: %s",
                            sheet_name, name, r + 1,
                            [_cell_to_str(df.iat[r, c]) for c in block_cols],
                        )

                # Пустая ячейка (пара продолжается со строки выше) — берём
                # предыдущее значение, чтобы пара не потерялась.
                subject = subject or last_subject
                teacher = teacher or last_teacher
                room = room or last_room
                last_subject, last_teacher, last_room = subject, teacher, room

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

            # Ошибка по одной группе не останавливает остальные.
            if not lessons or not any(l.subject.strip() for l in lessons):
                logger.error(
                    "лист %s: у группы %s (столбец %d) нет заполненных пар — "
                    "проверь структуру файла",
                    sheet_name, name, sc + 1,
                )
            groups.setdefault(name, []).extend(lessons)

    # Строгая проверка: если ни у одной группы нет ни одной заполненной пары —
    # формат файла не распознан, возвращаем пустой результат (следующий
    # резервный путь попробует иначе).
    filled = [
        l for ls in groups.values() for l in ls
        if l.subject.strip() and l.subject.strip().lower() not in {"окно", "-", "—"}
    ]
    if not filled and groups:
        logger.warning(
            "лист %s: все пары пустые/«окно» у групп %s — "
            "пробуем другой метод парсинга",
            sheet_name, list(groups.keys()),
        )
        return {}

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


def _find_room_nearby(
    df: pd.DataFrame, r: int, sc: int, next_sc: int, block_end: int,
    lesson_col: int = 0,
) -> str:
    """Ищет аудиторию рядом с парой: строка r (справа от sc) и 2 строки ниже.

    Нужно для файлов, где кабинет стоит НЕ в строке предмета, а строкой
    ниже (под предметом) или в объединённой ячейке. Аудитория = ячейка
    с цифрами, которая не ФИО и не предмет. Новую пару (число в колонке
    номеров) не пересекаем.
    """
    # 1) та же строка, правее предмета
    room = _find_room_right(df, r, sc, next_sc)
    if room:
        return room
    # 2) строки ниже: r+1, r+2 — кабинет под предметом/преподавателем
    for rr in range(r + 1, min(r + 3, block_end, len(df))):
        # строка с новым номером пары — это уже следующая пара, не лезем
        if lesson_col < df.shape[1] and _to_int(df.iat[rr, lesson_col]) is not None:
            continue
        for c in range(sc, next_sc):
            cell = _cell_to_str(df.iat[rr, c])
            if not cell or _is_fio(cell) or _is_subject_like(cell):
                continue
            room = _clean_room(cell)
            if room:
                return room
    return ""


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
    block_end: int = 0,
) -> tuple[str, str, list]:
    """Читает одну пару (предмет, каб, преподаватель, подгруппы) из строки r.

    Якорь — колонка sc группы. Данные только из этой строки и колонки sc.
    block_end — граница блока по строкам (для поиска кабинета ниже).
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

    # Кабинета нет в строке пары — возможно, он строкой ниже (под предметом)
    # или в объединённой ячейке. Ищем в окрестности пары.
    if not room:
        room = _find_room_nearby(
            df, r, sc, next_sc, block_end or len(df), lesson_col
        )
        if room:
            logger.debug(
                "пара r=%d col=%d: кабинет найден строкой ниже: %r", r, sc, room
            )

    return room, teacher, subgroups


def _parse_group_column(
    df: pd.DataFrame,
    header_row: int,
    sc: int,
    next_sc: int,
    lesson_col: int,
    cfg: ParserConfig,
    *,
    block_end: int = 0,
) -> list[Lesson]:
    """Читает все пары группы из столбца sc (группы идут в строку).

    block_end — граница по строкам (строка следующего заголовка групп или
    конец листа). Без неё парсер залезает в данные нижележащих блоков и
    приписывает чужие пары к группе (баг «Химия вместо предметов»).
    """
    if block_end == 0:
        block_end = len(df)
    name = _cell_to_str(df.iat[header_row, sc])
    logger.info(
        "группа %s: строка заголовка %d, столбец %d, границы блока: "
        "cols %d..%d, rows %d..%d",
        name, header_row + 1, sc + 1, sc + 1, next_sc, header_row + 2, block_end,
    )
    lessons: list[Lesson] = []
    prev_number: Optional[int] = None
    for r in range(header_row + 1, block_end):
        lesson_number = _to_int(df.iat[r, lesson_col])
        if lesson_number is None:
            # Номера нет в общем столбце — ищем число 1..12 левее группы
            # (у нижних блоков свой столбец номеров).
            for c in range(0, sc):
                v = _to_int(df.iat[r, c])
                if v is not None and 1 <= v <= 12:
                    lesson_number = v
                    break
        if lesson_number is None:
            continue  # строка с ФИО преподавателя или пустая
        # Пара «размазана» merge-ячейкой на 2+ строк с одним номером —
        # добавляем только если предмет ИЗМЕНИЛСЯ (иная пара, а не дубль).
        if lesson_number == prev_number:
            prev_lesson = lessons[-1] if lessons else None
            prev_subject = prev_lesson.subject if prev_lesson else ""
            cur_subject = _cell_to_str(df.iat[r, sc])
            if prev_subject and cur_subject == prev_subject:
                continue
        prev_number = lesson_number
        room, teacher, subgroups = _read_pair(
            df, r, sc, next_sc, lesson_col, cfg, block_end
        )
        if not room and _cell_to_str(df.iat[r, sc]):
            logger.info(
                "группа %s: пара r=%d (номер %d) — кабинет не найден; "
                "ячейки блока cols %d..%d в строке: %s",
                name, r + 1, lesson_number, sc + 1, next_sc,
                [_cell_to_str(df.iat[r, c]) for c in range(sc, next_sc)],
            )
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
    # Финальный дубль-фильтр: одинаковые подряд идущие пары (same number,
    # subject, room, teacher) схлопываем в одну.
    deduped: list[Lesson] = []
    for l in lessons:
        if (
            deduped
            and deduped[-1].lesson_number == l.lesson_number
            and deduped[-1].subject == l.subject
            and deduped[-1].room == l.room
            and deduped[-1].teacher == l.teacher
        ):
            continue
        deduped.append(l)
    return deduped


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
        # — отдельный блок. Границы блока — до следующей строки-заголовка,
        # чтобы парсер не «подбирал» данные из нижних блоков.
        by_row: dict[int, list[tuple[int, str]]] = {}
        for rc, cc, name in group_cells:
            by_row.setdefault(rc, []).append((cc, name))
        header_rows = sorted(by_row)
        for hi, (hr, cells) in enumerate(
            (hr, sorted(by_row[hr], key=lambda x: x[0])) for hr in header_rows
        ):
            block_end = header_rows[hi + 1] if hi + 1 < len(header_rows) else len(df)
            for i, (sc, name) in enumerate(cells):
                next_sc = cells[i + 1][0] if i + 1 < len(cells) else ncols
                lessons = _parse_group_column(
                    df, hr, sc, next_sc, lesson_col, cfg, block_end=block_end,
                )
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

    Основной путь — универсальный якорный парсер (_parse_sheet) на сетке с
    РАЗВЁРНУТЫМИ объединёнными ячейками: он поддерживает подгруппы, ФИО-строки
    под парами и несколько блоков групп друг под другом. Резерв — plain-чтение
    и блочный _parse_sheet_by_groups.
    """
    try:
        excel = pd.ExcelFile(io.BytesIO(content), engine="openpyxl")
    except Exception as exc:
        raise ParseError(f"не удалось открыть Excel: {exc}") from exc

    groups: dict[str, list[Lesson]] = {}
    errors: list[str] = []
    for sheet_name in excel.sheet_names:
        sheet_groups: dict[str, list[Lesson]] = {}
        # 1) основной путь: merge-развёрнутая сетка + универсальный парсер
        try:
            df_full = _read_sheet_grid(content, sheet_name)
            sheet_groups = _parse_sheet(df_full, cfg)
        except Exception as exc:
            errors.append(f"{sheet_name} (grid): {exc}")
            logger.warning("лист %s (grid) не распарсился: %s", sheet_name, exc)
        # 2) резерв 1: строгий блочный разбор (по 3 столбца на группу).
        #    Ставим его ПЕРЕД plain-pandas: он подходит для реальных файлов,
        #    где у каждой группы предмет|ФИО|кабинет в соседних колонках.
        if not sheet_groups:
            try:
                df_full = _read_sheet_grid(content, sheet_name)
                sheet_groups = _parse_sheet_by_groups(df_full, cfg, content, sheet_name)
            except Exception as exc:
                errors.append(f"{sheet_name} (blocks): {exc}")
                logger.warning("лист %s (blocks) не распарсился: %s", sheet_name, exc)
        # 3) резерв 2: обычное чтение pandas (без merge-развёртки) + _parse_sheet
        if not sheet_groups:
            try:
                df_plain = pd.read_excel(
                    excel, sheet_name=sheet_name, header=None, dtype=object
                )
                sheet_groups = _parse_sheet(df_plain, cfg)
            except Exception as exc:
                errors.append(f"{sheet_name} (plain): {exc}")
                logger.warning("лист %s (plain) не распарсился: %s", sheet_name, exc)
        # 3) резерв 2: строгий блочный разбор по трём столбцам на группу
        if not sheet_groups:
            try:
                df_full = _read_sheet_grid(content, sheet_name)
                sheet_groups = _parse_sheet_by_groups(df_full, cfg, content, sheet_name)
            except Exception as exc:
                errors.append(f"{sheet_name} (blocks): {exc}")
                logger.warning("лист %s (blocks) не распарсился: %s", sheet_name, exc)
        for g, lessons in sheet_groups.items():
            groups.setdefault(g, []).extend(lessons)

    if not groups:
        raise ParseError("ни один лист не удалось распарсить: " + "; ".join(errors))
    return groups
