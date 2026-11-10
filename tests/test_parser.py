"""Unit-тест парсера на фиктивном .xlsx в реальном формате (группы блоками)."""
import io

import pandas as pd
import pytest

from config import settings
from services.parser import ParseError, _normalize_teacher, parse_schedule


def make_xlsx() -> bytes:
    """Строит файл: колонка 0 = номер пары, по горизонтали группы,
    у каждой группы колонки «Дисциплина» и «Ауд.», строки ФИО чередуются."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # 12 колонок: 0=Пара, (1=Дисциплина, 5=Ауд.) — группа 1,
        # (7=Дисциплина, 11=Ауд.) — группа 2.
        df = pd.DataFrame(
            [
                ["Расписание на 26.10.2026", None, None, None, None, None, None, None, None, None, None, None],
                ["Пара", "Дисциплина", None, None, None, "Ауд.", None, "Дисциплина", None, None, None, "Ауд."],
                ["№", "Преподаватель", None, None, None, None, None, "Преподаватель", None, None, None, None],
                [None, 1, None, 2, None, 3, None, 1, None, 2, None, 3],
                [None, "1ОГР-26-9", None, None, None, None, None, "БУХ-26-9", None, None, None, None],
                [1, "История", None, None, None, "304", None, "Химия", None, None, None, "205"],
                [None, "Иванов  И.  И.", None, None, None, None, None, "Петров\nП. П.", None, None, None, None],
                [2, "Математика", None, None, None, "307", None, None, None, None, None, None],
                [None, "Иванов И. И.", None, None, None, None, None, None, None, None, None, None],
                [3, None, None, None, None, None, None, "Физика", None, None, None, "310"],
            ]
        )
        df.to_excel(writer, sheet_name="Понедельник", index=False, header=False)
    return buf.getvalue()


def test_parse_horizontal_groups():
    groups = parse_schedule(make_xlsx(), settings.parser)
    assert set(groups.keys()) == {"1ОГР-26-9", "БУХ-26-9"}

    g1 = groups["1ОГР-26-9"]
    assert [l.lesson_number for l in g1] == [1, 2, 3]
    assert g1[0].subject == "История" and g1[0].room == "304"
    assert g1[0].teacher == "Иванов И. И."  # лишние пробелы схлопнуты
    assert g1[1].subject == "Математика" and g1[1].room == "307"
    assert g1[1].teacher == "Иванов И. И."
    assert g1[2].subject == "" and g1[2].room == ""
    assert g1[2].teacher == ""  # последняя строка файла — ФИО нет

    g2 = groups["БУХ-26-9"]
    assert [l.lesson_number for l in g2] == [1, 2, 3]
    assert g2[0].subject == "Химия" and g2[0].room == "205"
    assert g2[0].teacher == "Петров П. П."  # перенос строки схлопнут
    assert g2[1].subject == "" and g2[1].room == ""
    assert g2[1].teacher == ""  # ФИО нет
    assert g2[2].subject == "Физика" and g2[2].room == "310"
    assert g2[2].teacher == ""


def test_normalize_teacher():
    assert _normalize_teacher("Иванов   И.  И.") == "Иванов И. И."
    assert _normalize_teacher("Петров\nП. П.\n") == "Петров П. П."
    assert _normalize_teacher(None) == ""
    assert _normalize_teacher("   ") == ""


def test_group_pattern_accepts_english_subgroups():
    """Подгруппы латиницей: 1ИСИП-25-9A и т.п."""
    pat = settings.parser.GROUP_NAME_PATTERN
    assert __import__("re").match(pat, "1ИСИП-25-9A")
    assert __import__("re").match(pat, "1ИСИП-25-9B")
    assert __import__("re").match(pat, "ОПИр-25-9C")
    assert __import__("re").match(pat, "1ОГР-26-9")
    assert not __import__("re").match(pat, "Иванов И.И.")


def test_is_fio_patterns():
    """ФИО-детект: разные фамилии, никакой хардкод."""
    from services.parser import _is_fio
    assert _is_fio("Платицына Е. В.")
    assert _is_fio("Байшева Я. В.")
    assert _is_fio("Ощепкова А. С.")
    assert _is_fio("Грабанова И. И.")
    assert _is_fio("Petrov A. B.")
    assert not _is_fio("121")
    assert not _is_fio("Иностранный язык")
    assert not _is_fio("каб. 304")


def test_extract_subgroups_two_pairs():
    """Тест 1: предмет | ауд.1 | ФИО1 | ауд.2 | ФИО2 → 2 подгруппы."""
    import pandas as pd
    from services.parser import ParserConfig, _extract_subgroups
    df = pd.DataFrame(
        [
            ["Иностранный язык в профессиональной деятельности",
             "Ино 319", "Кобзова С. В.", "Ино 202", "Ощепкова А. С."],
        ],
        dtype=object,
    )
    subs = _extract_subgroups(df, 0, sc=0, block_end=1, lesson_col=0, cfg=ParserConfig())
    assert len(subs) == 2, subs
    assert subs[0].number == 1
    assert subs[0].room == "319" and subs[0].teacher == "Кобзова С. В."
    assert subs[1].number == 2
    assert subs[1].room == "202" and subs[1].teacher == "Ощепкова А. С."


def test_extract_subgroups_single_pair():
    """Тест 2: одна пара → один элемент (вывод без «п/г»)."""
    import pandas as pd
    from services.parser import ParserConfig, _extract_subgroups
    df = pd.DataFrame([["Иностранный язык", "Ино 319", "Кобзова С. В.", None, None]],
                      dtype=object)
    subs = _extract_subgroups(df, 0, sc=0, block_end=1, lesson_col=0, cfg=ParserConfig())
    assert len(subs) == 1
    assert subs[0].room == "319" and subs[0].teacher == "Кобзова С. В."


def test_extract_subgroups_none_in_middle():
    """Тест 3: пустая ячейка между парами не ломает группировку."""
    import pandas as pd
    from services.parser import ParserConfig, _extract_subgroups
    df = pd.DataFrame(
        [["Иностранный язык", "Ино 319", "Кобзова С. В.", None, "Ино 202",
          "Ощепкова А. С."]],
        dtype=object,
    )
    subs = _extract_subgroups(df, 0, sc=0, block_end=1, lesson_col=0, cfg=ParserConfig())
    assert len(subs) == 2, subs
    assert subs[0].room == "319" and subs[0].teacher == "Кобзова С. В."
    assert subs[1].room == "202" and subs[1].teacher == "Ощепкова А. С."


def test_extract_subgroups_three_pairs():
    """Тест 4: три пары в строке → три подгруппы."""
    import pandas as pd
    from services.parser import ParserConfig, _extract_subgroups
    df = pd.DataFrame(
        [["Иностранный язык", "Ино 319", "Кобзова С. В.", "Ино 202",
          "Ощепкова А. С.", "Ино 105", "Грабанова И. И."]],
        dtype=object,
    )
    subs = _extract_subgroups(df, 0, sc=0, block_end=1, lesson_col=0, cfg=ParserConfig())
    assert len(subs) == 3, subs
    assert [s.room for s in subs] == ["319", "202", "105"]
    assert [s.number for s in subs] == [1, 2, 3]


def test_extract_subgroups_stops_at_next_group_subject():
    """Колонка предмета другой группы не попадает в подгруппы."""
    import pandas as pd
    from services.parser import ParserConfig, _extract_subgroups
    df = pd.DataFrame(
        [["Иностранный язык", "Ино 319", "Кобзова С. В.", "Химия", "201"]],
        dtype=object,
    )
    subs = _extract_subgroups(
        df, 0, sc=0, block_end=1, lesson_col=0, cfg=ParserConfig(), stop_cols=frozenset({3})
    )
    assert [s.room for s in subs] == ["319"]
    assert [s.teacher for s in subs] == ["Кобзова С. В."]


def test_parse_full_lesson_with_two_subgroups():
    """Сквозной тест: пара с двумя подгруппами из xlsx-строки."""
    import pandas as pd
    from services.parser import parse_schedule
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df = pd.DataFrame(
            [
                ["Пара", "Дисциплина", None, None, None, None, "Ауд."],
                ["№", "Преподаватель", None, None, None, None, None],
                [None, "1ИСИП-25-9", None, None, None, None, None],
                [3, "Иностранный язык в профессиональной деятельности",
                 "Ино... 319", "Кобзова С. В.", "Ино... 202", "Ощепкова А. С.", None],
            ]
        )
        df.to_excel(writer, sheet_name="Понедельник", index=False, header=False)
    groups = parse_schedule(buf.getvalue(), settings.parser)
    lessons = groups["1ИСИП-25-9"]
    assert len(lessons) == 1
    subs = lessons[0].subgroups
    assert len(subs) == 2, subs
    assert subs[0].room == "319" and subs[0].teacher == "Кобзова С. В."
    assert subs[1].room == "202" and subs[1].teacher == "Ощепкова А. С."
    # обратная совместимость: room/teacher заполнены данными первой подгруппы
    assert lessons[0].room == "319" and lessons[0].teacher == "Кобзова С. В."


def test_extract_subgroups_two_column_pairs_real_layout():
    """Реальный формат: предмет|каб|предмет|каб, ФИО — строкой ниже под своим
    предметом (Байшева под 1-м, Ощепкова под 2-м)."""
    import pandas as pd
    from services.parser import ParserConfig, _extract_subgroups
    df = pd.DataFrame(
        [
            ["Иностранный язык", "121", "Иностранный язык", "202", None],
            ["Байшева Я. В.", None, "Ощепкова А. С.", None, None],
        ],
        dtype=object,
    )
    subs = _extract_subgroups(
        df, 0, sc=0, block_end=2, lesson_col=0, cfg=ParserConfig(),
        main_subject="Иностранный язык",
    )
    assert len(subs) == 2, [(s.number, s.room, s.teacher) for s in subs]
    assert (subs[0].room, subs[0].teacher) == ("121", "Байшева Я. В.")
    assert (subs[1].room, subs[1].teacher) == ("202", "Ощепкова А. С.")


def test_parse_two_row_subgroup_layout_end_to_end():
    """Сквозной парсинг xlsx с двухрядной разбивкой на подгруппы."""
    import pandas as pd
    from services.parser import parse_schedule
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(
            [
                ["Пара", "Дисциплина", None, "Дисциплина", None, "Ауд."],
                [None, "1ИСИП-25-9", None, None, None, None],
                [3, "Иностранный язык", "121", "Иностранный язык", "202", None],
                [None, "Байшева Я. В.", None, "Ощепкова А. С.", None, None],
            ]
        ).to_excel(writer, sheet_name="Понедельник", index=False, header=False)
    lessons = parse_schedule(buf.getvalue(), settings.parser)["1ИСИП-25-9"]
    subs = lessons[0].subgroups
    assert len(subs) == 2, [(s.number, s.room, s.teacher) for s in subs]
    assert (subs[0].room, subs[0].teacher) == ("121", "Байшева Я. В.")
    assert (subs[1].room, subs[1].teacher) == ("202", "Ощепкова А. С.")


def test_subgroups_work_for_every_group_on_sheet():
    """Разбивка на подгруппы работает в любой группе и не течёт в соседние.

    Лист: 3 группы по 4 колонки (предмет|каб|предмет|каб). В 1-й и 3-й —
    «Иностранный язык» с двумя подгруппами, во 2-й — обычная пара.
    """
    import pandas as pd
    from services.parser import parse_schedule
    buf = io.BytesIO()
    header = ["Пара"] + ["Дисциплина", "Ауд."] * 6
    names = [None, "1ИСИП-25-9", None, None, None,
             "2ИСИП-25-9", None, None, None,
             "3ИСИП-25-9", None, None, None]
    row1 = [1,
            "Иностранный язык", "121", "Иностранный язык", "202",
            "Математика", "307", None, None,
            "Иностранный язык", "111", "Иностранный язык", "222"]
    t1 = [None,
          "Байшева Я. В.", None, "Ощепкова А. С.", None,
          "Иванов И. И.", None, None, None,
          "Петрова А. Б.", None, "Сидорова В. Г.", None]
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame([header, names, row1, t1]).to_excel(
            writer, sheet_name="Понедельник", index=False, header=False
        )

    groups = parse_schedule(buf.getvalue(), settings.parser)

    g1 = groups["1ИСИП-25-9"][0].subgroups
    assert [(s.room, s.teacher) for s in g1] == [
        ("121", "Байшева Я. В."), ("202", "Ощепкова А. С.")
    ], [(s.room, s.teacher) for s in g1]

    g2 = groups["2ИСИП-25-9"][0]
    assert g2.subgroups == []                      # обычная пара — без подгрупп
    assert (g2.room, g2.teacher) == ("307", "Иванов И. И.")

    g3 = groups["3ИСИП-25-9"][0].subgroups
    assert [(s.room, s.teacher) for s in g3] == [
        ("111", "Петрова А. Б."), ("222", "Сидорова В. Г.")
    ], [(s.room, s.teacher) for s in g3]


def test_parse_raises_on_unknown_format():
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame({"foo": [1, 2, 3]}).to_excel(writer, sheet_name="S", index=False)
    with pytest.raises(ParseError):
        parse_schedule(buf.getvalue(), settings.parser)
