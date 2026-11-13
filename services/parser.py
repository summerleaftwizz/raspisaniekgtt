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
# ВАЖНО: варианты взаимоисключающие — «Фамилия Имя И. В.» (4 токена) это
# НЕ ФИО, а предмет с прилипшим ФИО (ловилось ранее → ФИО в subject).
FIO_RE = re.compile(
    r"^[А-ЯЁA-Z][а-яёa-z\-]+"
    r"(?:\s+[А-ЯЁA-Z]\.\s*[А-ЯЁA-Z]?\.?"          # Фамилия И. О. / Фамилия И.
    r"|\s+[А-ЯЁA-Z][а-яёa-z\-]+\s+[А-ЯЁA-Z]\.?)"  # Фамилия Имя И.
    r"$"
)
# Аудитория: служебный префикс + номер («Ино 319» -> «319»)
ROOM_NUMBER_RE = re.compile(r"\d+\s*[A-Za-zА-Яа-я]?")


def _is_fio(value: str) -> bool:
    """Похоже ли на ФИО в формате «Фамилия И. О.» (значение — из ячейки)."""
    return bool(FIO_RE.match(_normalize_teacher(value)))


# ФИО в ХВОСТЕ строки: «История Иванова Е. В.» → предмет + ФИО
FIO_TAIL_RE = re.compile(
    r"[\s,;]+([А-ЯЁA-Z][а-яёa-z\-]+(?:\s+[А-ЯЁA-Z][а-яёa-z\-]+)?"
    r"\s+[А-ЯЁA-Z]\.\s*[А-ЯЁA-Z]?\.?)\s*$"
)


def _split_subject_fio(value: str) -> tuple[str, str]:
    """Отделяет ФИО от предмета в одной ячейке: «История Иванова Е. В.»
    → («История», «Иванова Е. В.»). Если ФИО в хвосте нет — (текст, "")."""
    s = _normalize_teacher(value)
    m = FIO_TAIL_RE.search(s)
    if not m:
        return s, ""
    subject_part = s[: m.start()].strip(" .,;:-")
    return subject_part, m.group(1)


def _clean_room(value: str) -> str:
    """Убирает служебный префикс аудитории: «Ино 319» -> «319».

    Если цифр нет («Спортзал», «Акт. зал») — возвращает строку как есть.
    """
    s = _normalize_teacher(value)
    if not s:
        return ""
    m = ROOM_NUMBER_RE.search(s)
    return m.group(0).strip() if m else s


# Служебные слова шапки — их нельзя принять за предмет.
HEADER_WORDS = frozenset(
    {
        "пара", "№", "номер", "номер пары", "время", "день", "дата",
        "дисциплина", "предмет", "преподаватель", "учитель", "ауд",
        "ауд.", "кабинет", "каб", "каб.", "группа", "расписание",
    }
)

# Чистое число (номер пары/кабинета), а не текст предмета.
NUMBER_ONLY_RE = re.compile(r"^\d{1,4}$")
# Число с необязательной буквой-суффиксом: «125», «125а», «304 л».
PURE_ROOM_RE = re.compile(r"^(\d{1,4})\s*([A-Za-zА-Яа-яЁё])?$")
# Служебный префикс/слово аудитории в любом месте строки.
ROOM_HINT_RE = re.compile(r"(?:^|\s)(?:каб\.?|ауд\.?|аудитор(?:ия)?|ино)", re.I)


def _is_number_like(value: str) -> bool:
    """Ячейка — просто число («125»), без букв."""
    return bool(NUMBER_ONLY_RE.match(_normalize_teacher(value)))


def _is_header_word(value: str) -> bool:
    s = _normalize_teacher(value).strip(" .,:;-").lower()
    return bool(s) and s in HEADER_WORDS


def _is_subject_like(value: str) -> bool:
    """Похоже ли на название предмета, а не на кабинет/ФИО/номер/шапку.

    Предмет — текст с буквами. Цифры внутри допустимы («1С: Бухгалтерия»,
    «Основы 3D-моделирования»), но чистое число и явный кабинет — нет.
    """
    value = _normalize_teacher(value)
    if not value or _is_fio(value) or _is_number_like(value):
        return False
    if _is_room_like(value) or _is_header_word(value):
        return False
    return any(ch.isalpha() for ch in value)


def _is_room_like(value: str) -> bool:
    """Явно похоже на аудиторию: только цифры («125», «310»), цифры + буква
    («125а», «304л»), «каб. 125», «ауд. 310», «Ино 319», «max».

    Чистое число 1..12 НЕ считаем кабинетом — это может быть номер пары.
    """
    s = _normalize_teacher(value)
    if not s:
        return False
    low = s.lower()
    if low in {"max", "макс"}:
        return True
    if ROOM_HINT_RE.search(low) and any(ch.isdigit() for ch in s):
        return True
    m = PURE_ROOM_RE.match(s)
    if not m:
        return False
    digits = m.group(1)
    return len(digits) > 2 or int(digits) > 12


def _room_score(value: str) -> int:
    """Насколько ячейка похожа на кабинет. Чем больше — тем вероятнее.

    Приоритет: явный префикс «каб./ауд.» → трёхзначный номер → номер с
    буквой. Нужен, чтобы при нескольких числах выбрать кабинет, а не
    номер подгруппы/пары.
    """
    s = _normalize_teacher(value)
    if not _is_room_like(s):
        return -1
    low = s.lower()
    score = 0
    if ROOM_HINT_RE.search(low):
        score += 5
    if low.startswith("ино"):
        score += 1
    m = re.search(r"\d{1,4}", s)
    if m:
        digits = m.group(0)
        if len(digits) == 3:
            score += 4
        elif len(digits) == 2:
            score += 2
        elif len(digits) == 4:
            score += 1
    if re.search(r"\d\s*[A-Za-zА-Яа-яЁё]", s):
        score += 1
    return score


def _pick_room(candidates: list[str]) -> str:
    """Выбирает лучший кабинет из нескольких чисел/ячеек с цифрами."""
    best, best_score = "", -1
    for cell in candidates:
        score = _room_score(cell)
        if score > best_score:
            best, best_score = _clean_room(cell), score
    return best


def _classify_values(
    values, cfg: ParserConfig
) -> tuple[str, str, str]:
    """Классифицирует ячейки блока пары: (предмет, преподаватель, кабинет).

    * ФИО читается только ОДИН раз — первая подходящая ячейка.
    * Предмет — первый текст, который не ФИО и не число.
    * Кабинет — лучший из чисел/«каб. N» (обычно трёхзначный).
    """
    subject = teacher = ""
    room_candidates: list[str] = []

    for _col, raw in values:
        cell = _cell_to_str(raw)
        if not cell:
            continue
        if _is_group_name(cell, cfg) or _is_header_word(cell):
            continue
        # 1) Преподаватель: только первое ФИО в блоке.
        if _is_fio(cell):
            if not teacher:
                teacher = _normalize_teacher(cell)
            continue
        # 2) Кабинет: число / «каб. 310» / «Ино 319».
        if _is_room_like(cell):
            room_candidates.append(cell)
            continue
        # 3) Предмет, возможно с прилипшим ФИО в хвосте.
        subj_part, fio_part = _split_subject_fio(cell)
        if subj_part and _is_subject_like(subj_part):
            if not subject:
                subject = subj_part
            if fio_part and not teacher:
                teacher = fio_part
            continue
        if _is_subject_like(cell):
            if not subject:
                subject = cell
            continue
        # 4) Прочее с цифрами — потенциальный кабинет (выберем лучший).
        if any(ch.isdigit() for ch in cell):
            room_candidates.append(cell)

    return subject, teacher, _pick_room(room_candidates)


def _row_has_group_name(df: pd.DataFrame, r: int, cfg: ParserConfig) -> bool:
    """Есть ли в строке название группы (значит, это шапка блока)."""
    if not 0 <= r < len(df):
        return False
    for c in range(df.shape[1]):
        if _is_group_name(_cell_to_str(df.iat[r, c]), cfg):
            return True
    return False


def _classify_row(
    df: pd.DataFrame,
    r: int,
    cols,
    cfg: ParserConfig,
    skip_cols: frozenset = frozenset(),
) -> tuple[str, str, str]:
    """Классифицирует все ячейки строки r в колонках cols."""
    values = [
        (c, df.iat[r, c])
        for c in cols
        if c not in skip_cols and 0 <= c < df.shape[1]
    ]
    return _classify_values(values, cfg)


def _lesson_number_at(
    df: pd.DataFrame, r: int, sc: int, lesson_col: int
) -> Optional[int]:
    """Номер пары в строке: общий столбец, затем число левее группы."""
    if not 0 <= r < len(df):
        return None
    if 0 <= lesson_col < df.shape[1]:
        v = _to_int(df.iat[r, lesson_col])
        if v is not None and 1 <= v <= 12:
            return v
    for c in range(0, min(sc, df.shape[1])):
        v = _to_int(df.iat[r, c])
        if v is not None and 1 <= v <= 12:
            return v
    return None


def _lesson_number_strict(
    df: pd.DataFrame, r: int, lesson_col: int
) -> Optional[int]:
    """Номер пары ТОЛЬКО из столбца с числами пар (без левого скана).

    Иначе число 1..12 из чужой колонки (кабинет-«2», номер подгруппы)
    сдвигает нумерацию пар.
    """
    if not 0 <= r < len(df) or not 0 <= lesson_col < df.shape[1]:
        return None
    v = _to_int(df.iat[r, lesson_col])
    return v if v is not None and 1 <= v <= 12 else None


def _find_lesson_col_in_block(
    df: pd.DataFrame, row_start: int, row_end: int, col_end: int
) -> Optional[int]:
    """Столбец с номерами пар ДЛЯ ЭТОГО блока строк.

    В многослойных файлах у каждого блока групп свой столбец «Пара»;
    глобальный столбец (_find_lesson_col) может быть пуст в этих строках.
    Берём колонку, где в строках блока больше всего чисел 1..12.
    """
    best, best_score = None, 0
    row_end = min(row_end, len(df))
    for c in range(0, min(col_end, df.shape[1])):
        score = 0
        for r in range(max(0, row_start), row_end):
            v = _to_int(df.iat[r, c])
            if v is not None and 1 <= v <= 12:
                score += 1
        if score > best_score:
            best, best_score = c, score
    return best


def _row_belongs_to_lesson(
    df: pd.DataFrame,
    r: int,
    lesson_number: Optional[int],
    sc: int,
    lesson_col: int,
) -> bool:
    """Строка относится к той же паре (нет другого номера пары)."""
    ln = _lesson_number_at(df, r, sc, lesson_col)
    return ln is None or lesson_number is None or ln == lesson_number


def _nearby_rows(
    r: int, block_end: int, total_rows: int, *, below_first: bool = False
):
    """Соседние строки пары в разумном радиусе, без выхода за блок."""
    above = [rr for rr in (r - 1, r - 2) if rr >= 0]
    below = [rr for rr in (r + 1, r + 2, r + 3) if rr < min(block_end, total_rows)]
    order = below + above if below_first else above + below
    seen: set[int] = set()
    for rr in order:
        if rr != r and rr not in seen:
            seen.add(rr)
            yield rr


def _find_subject_nearby(
    df: pd.DataFrame,
    r: int,
    sc: int,
    cols,
    cfg: ParserConfig,
    block_end: int,
    lesson_col: int,
    lesson_number: Optional[int],
) -> str:
    """Предмет из объединённой ячейки выше/ниже, если в строке его нет."""
    for rr in _nearby_rows(r, block_end, len(df)):
        if _row_has_group_name(df, rr, cfg):
            continue
        if not _row_belongs_to_lesson(df, rr, lesson_number, sc, lesson_col):
            continue
        subject, _teacher, _room = _classify_row(df, rr, cols, cfg)
        if subject:
            return subject
    return ""


def _find_teacher_nearby(
    df: pd.DataFrame,
    r: int,
    sc: int,
    cols,
    cfg: ParserConfig,
    block_end: int,
    lesson_col: int,
    lesson_number: Optional[int],
) -> str:
    """Первое ФИО из строки НИЖЕ той же пары (ФИО под предметом).

    Выше не смотрим: там ФИО предыдущей пары, его нельзя приписывать
    пустой строке (иначе преподаватель «дублируется» через пару).
    """
    for rr in range(r + 1, min(r + 4, block_end, len(df))):
        if _row_has_group_name(df, rr, cfg):
            continue
        if not _row_belongs_to_lesson(df, rr, lesson_number, sc, lesson_col):
            continue
        _subject, teacher, _room = _classify_row(df, rr, cols, cfg)
        if teacher:
            return teacher
    return ""


def _find_room_nearby(
    df: pd.DataFrame,
    r: int,
    sc: int,
    next_sc: int,
    block_end: int,
    lesson_col: int = 0,
    cfg: Optional[ParserConfig] = None,
    lesson_number: Optional[int] = None,
) -> str:
    """Кабинет в соседней строке той же пары (обычно под предметом).

    Аудитория = ячейка с цифрами, не ФИО и не предмет. Новую пару
    (другой номер в столбце пар) не пересекаем.
    """
    cfg = cfg or ParserConfig()
    cols = range(sc, next_sc)
    for rr in _nearby_rows(r, block_end, len(df), below_first=True):
        if _row_has_group_name(df, rr, cfg):
            continue
        if not _row_belongs_to_lesson(df, rr, lesson_number, sc, lesson_col):
            continue
        _subject, _teacher, room = _classify_row(df, rr, cols, cfg)
        if room:
            logger.info(
                "кабинет найден: %s (строка %d, столбец блока %d)",
                room, rr + 1, sc + 1,
            )
            return room
    return ""


def _merge_lesson(lesson: "Lesson", subject: str, teacher: str, room: str) -> None:
    """Дополняет пару недостающими полями, не перезаписывая найденное.

    Строки-продолжения (ФИО под предметом, объединённые ячейки) не должны
    создавать вторую пару с тем же номером и затирать первую.
    """
    if not lesson.subject and _is_subject_like(subject):
        lesson.subject = subject
    if not lesson.teacher and teacher and _is_fio(teacher):
        lesson.teacher = teacher
    if not lesson.room and room and _is_room_like(room):
        lesson.room = _clean_room(room)


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
    """Все ячейки, точно совпадающие с маской названия группы (row, col, name).

    Объединённая ячейка с названием группы разворачивается на несколько
    колонок/строк (_read_sheet_grid) — без дедупликации «ПРУМ-23» давал бы
    блок 11..11, отрезая столбцы кабинетов 12–13. Оставляем только левую-
    верхнюю (первую) ячейку каждой серии одинаковых соседних названий.
    """
    cells: list[tuple[int, int, str]] = []
    for r in range(len(df)):
        last_c: Optional[int] = None
        last_name = ""
        for c in range(df.shape[1]):
            v = _cell_to_str(df.iat[r, c])
            if not _is_group_name(v, cfg):
                continue
            # Серия одинаковых названий в соседних колонках = один блок.
            if last_c is not None and v == last_name and c - last_c <= 2:
                continue
            cells.append((r, c, v))
            last_c, last_name = c, v
    # Вертикальные merge: одинаковое название в соседних строках той же
    # колонки — оставляем верхнюю ячейку.
    dedup: list[tuple[int, int, str]] = []
    for r, c, name in cells:
        if (
            dedup
            and dedup[-1][1] == c
            and dedup[-1][2] == name
            and r - dedup[-1][0] <= 2
        ):
            continue
        dedup.append((r, c, name))
    return dedup


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
            # Свой столбец номеров пар для этого блока строк (у нижних
            # блоков он свой, глобальный может быть пуст).
            pair_col = _find_lesson_col_in_block(df, hr + 1, block_rows_end, block_end)
            if pair_col is None:
                pair_col = lesson_col
            logger.info(
                "лист %s, группа %s: блок столбцов %d..%d, строки %d..%d, "
                "столбец номеров пар: %d",
                sheet_name, name, sc + 1, block_end, hr + 2, block_rows_end,
                pair_col + 1,
            )

            lessons: list[Lesson] = []
            seen_numbers: set[int] = set()
            prev_lesson: Optional[Lesson] = None

            for r in range(hr + 1, block_rows_end):
                if _row_has_group_name(df, r, cfg):
                    continue  # шапка следующего блока групп

                # Номер пары — строго из столбца с числами пар.
                lesson_number = _lesson_number_strict(df, r, pair_col)

                # Строка-продолжение без номера пары — дополняем предыдущую.
                if lesson_number is None:
                    if prev_lesson is not None:
                        subject, teacher, room = _classify_row(
                            df, r, block_cols, cfg,
                            skip_cols=(
                                frozenset({pair_col}) if pair_col != sc
                                else frozenset()
                            ),
                        )
                        _merge_lesson(prev_lesson, subject, teacher, room)
                    continue

                # Такой номер уже был — это та же пара, размазанная merge-
                # ячейкой на несколько строк. Не добавляем дубль (иначе
                # вторая запись затирает первую и предмет становится ФИО).
                if lesson_number in seen_numbers:
                    subject, room, teacher, subgroups = _read_pair(
                        df, r, sc, block_end, pair_col, cfg, block_rows_end
                    )
                    target = prev_lesson
                    if target is None or target.lesson_number != lesson_number:
                        target = next(
                            (l for l in reversed(lessons)
                             if l.lesson_number == lesson_number),
                            None,
                        )
                    if target is not None:
                        _merge_lesson(target, subject, teacher, room)
                        if subgroups and not target.subgroups:
                            target.subgroups = subgroups
                    continue

                seen_numbers.add(lesson_number)
                subject, room, teacher, subgroups = _read_pair(
                    df, r, sc, block_end, pair_col, cfg, block_rows_end
                )
                if not room:
                    logger.info(
                        "лист %s, %s: пара r=%d без кабинета; ячейки блока: %s",
                        sheet_name, name, r + 1,
                        [_cell_to_str(df.iat[r, c]) for c in block_cols],
                    )

                logger.info(
                    "лист %s, %s, пара №%d: предмет: [%s], преподаватель: [%s], кабинет: [%s]",
                    sheet_name, name, lesson_number, subject, teacher, room,
                )
                lesson = Lesson(
                    lesson_number=lesson_number,
                    time="",
                    subject=subject,
                    room=room,
                    teacher=teacher,
                    subgroups=subgroups,
                )
                lessons.append(lesson)
                prev_lesson = lesson

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
    """Ближайшая ячейка справа от sc (до boundary), похожая на аудиторию.

    Ячейки с ТОЛЬКО цифрами («125», «310») — это точно кабинет, берём их
    в первую очередь, независимо от остальных проверок.
    """
    # Проход 1: явные кабинеты (только цифры / «каб. N») — высший приоритет.
    for c in range(sc + 1, boundary):
        cell = _cell_to_str(df.iat[r, c])
        if cell and _is_room_like(cell):
            room = _clean_room(cell)
            if room:
                logger.info("кабинет найден: %s (строка %d, столбец %d)", room, r + 1, c + 1)
                return room
    # Проход 2: остальное, что не ФИО и не предмет.
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
            logger.info("кабинет найден: %s (строка %d, столбец %d)", room, r + 1, c + 1)
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
    block_end: int = 0,
) -> tuple[str, str, str, list]:
    """Читает одну пару (предмет, каб, преподаватель, подгруппы) из строки r.

    Читаем ВЕСЬ блок группы (sc..next_sc), а не только колонку-якорь:
    * предмет — первый текст, который не ФИО и не число;
    * преподаватель — только первое ФИО в блоке (остальные игнорируются);
    * кабинет — лучший из чисел/«каб. N» (обычно трёхзначный).

    Если поле пустое — ищем его в объединённой ячейке соседней строки
    той же пары (предмет выше/ниже, ФИО под предметом, кабинет рядом).
    Возвращает (subject, room, teacher, subgroups).
    """
    block_end = block_end or len(df)
    next_sc = min(max(next_sc, sc + 1), df.shape[1])
    cols = list(range(sc, next_sc))
    # Столбец номеров пар — не предмет и не кабинет.
    skip_cols = frozenset({lesson_col}) if lesson_col != sc else frozenset()
    lesson_number = _lesson_number_at(df, r, sc, lesson_col)

    subject, teacher, room = _classify_row(
        df, r, cols, cfg, skip_cols=skip_cols
    )

    # Подгруппы: ячейки справа от sc до границы следующей группы.
    stop_cols = frozenset(
        c for c in range(sc + 1, next_sc)
        if _is_group_name(_cell_to_str(df.iat[r, c]), cfg)
    )
    subgroups = _extract_subgroups(
        df, r, sc, block_end, lesson_col, cfg,
        stop_cols=stop_cols, main_subject=subject, col_limit=next_sc,
    )
    if subgroups:
        # Для разбивки на подгруппы основная пара = данные первой подгруппы,
        # но предмет берём из классификации (ФИО им быть не может).
        if subgroups[0].room:
            room = subgroups[0].room
        if subgroups[0].teacher:
            teacher = subgroups[0].teacher
        if len(subgroups) == 1:
            subgroups = []

    # Пустые поля — ищем в соседних строках той же пары.
    if not subject:
        subject = _find_subject_nearby(
            df, r, sc, cols, cfg, block_end, lesson_col, lesson_number
        )
    if not teacher:
        teacher = _find_teacher_nearby(
            df, r, sc, cols, cfg, block_end, lesson_col, lesson_number
        )
    if not room:
        room = _find_room_nearby(
            df, r, sc, next_sc, block_end, lesson_col, cfg, lesson_number
        )

    # Защита от главного бага: ФИО/число/кабинет не могут быть предметом.
    if subject and (
        _is_fio(subject) or _is_number_like(subject) or _is_room_like(subject)
    ):
        subject = ""
    if subject and teacher and _normalize_teacher(subject) == _normalize_teacher(teacher):
        subject = ""

    logger.info(
        "пара (строка %d, столбец %d): предмет: [%s], преподаватель: [%s], кабинет: [%s]",
        r + 1, sc + 1, subject, teacher, room,
    )
    return subject, room, teacher, subgroups


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
    # Свой столбец номеров пар для этого блока строк: у нижних блоков он
    # свой, а глобальный столбец в этих строках пуст. Номер пары берём
    # ТОЛЬКО из столбца с числами — иначе нумерация сдвигается.
    pair_col = _find_lesson_col_in_block(df, header_row + 1, block_end, next_sc)
    if pair_col is None:
        pair_col = lesson_col
    logger.info(
        "группа %s: строка заголовка %d, столбец %d, границы блока: "
        "cols %d..%d, rows %d..%d, столбец номеров пар: %d",
        name, header_row + 1, sc + 1, sc + 1, next_sc, header_row + 2, block_end,
        pair_col + 1,
    )
    lessons: list[Lesson] = []
    prev_lesson: Optional[Lesson] = None
    for r in range(header_row + 1, block_end):
        if _row_has_group_name(df, r, cfg):
            continue  # следующая шапка блока / название другой группы

        lesson_number = _lesson_number_strict(df, r, pair_col)
        cols = list(range(sc, min(next_sc, df.shape[1])))

        # Строка без номера пары — продолжение предыдущей пары
        # (ФИО под предметом, объединённая ячейка). Не создаём новую пару,
        # только дополняем недостающие поля.
        if lesson_number is None:
            if prev_lesson is not None:
                subject, teacher, room = _classify_row(
                    df, r, cols, cfg,
                    skip_cols=frozenset({pair_col}) if pair_col != sc else frozenset(),
                )
                _merge_lesson(prev_lesson, subject, teacher, room)
            continue

        # Тот же номер пары = та же пара, «размазанная» merge-ячейкой на
        # несколько строк (например, ФИО преподавателя). Вторую пару НЕ
        # добавляем — иначе она затрёт первую в выводе (баг: предмет = ФИО).
        if prev_lesson is not None and lesson_number == prev_lesson.lesson_number:
            subject, room, teacher, subgroups = _read_pair(
                df, r, sc, next_sc, pair_col, cfg, block_end
            )
            _merge_lesson(prev_lesson, subject, teacher, room)
            if subgroups and not prev_lesson.subgroups:
                prev_lesson.subgroups = subgroups
            continue

        subject, room, teacher, subgroups = _read_pair(
            df, r, sc, next_sc, pair_col, cfg, block_end
        )
        if not room and _cell_to_str(df.iat[r, sc]):
            logger.info(
                "группа %s: пара r=%d (номер %d) — кабинет не найден; "
                "ячейки блока cols %d..%d в строке: %s",
                name, r + 1, lesson_number, sc + 1, next_sc,
                [_cell_to_str(df.iat[r, c]) for c in range(sc, next_sc)],
            )
        lesson = Lesson(
            lesson_number=lesson_number,
            time="",
            subject=subject,
            room=room,
            teacher=teacher,
            subgroups=subgroups,
        )
        lessons.append(lesson)
        prev_lesson = lesson
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
