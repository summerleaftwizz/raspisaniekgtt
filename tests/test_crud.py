"""Тесты дедупликации строк расписания в БД (дубли из старых парсингов)."""
from types import SimpleNamespace

from db.crud import _dedupe_rows


def row(num, subject="", room="", teacher="", subgroups=None):
    return SimpleNamespace(
        lesson_number=num, subject=subject, room=room, teacher=teacher,
        subgroups=subgroups,
    )


def test_dedupe_rows_keeps_one_row_per_number():
    rows = [
        row(1, "История", "310"),
        row(2, "Математика", "307"),
        row(1, "История", "310"),   # дубль
        row(2, "Математика", "307"),  # дубль
    ]
    out = _dedupe_rows(rows)
    assert [r.lesson_number for r in out] == [1, 2]
    assert out[0].subject == "История"
    assert out[1].room == "307"


def test_dedupe_rows_prefers_row_with_subject():
    """Пустой дубль не должен вытеснить настоящую пару."""
    out = _dedupe_rows([row(1, ""), row(1, "История", "310")])
    assert len(out) == 1
    assert out[0].subject == "История"
    # и в обратном порядке тоже
    out = _dedupe_rows([row(1, "История", "310"), row(1, "")])
    assert out[0].subject == "История"


def test_dedupe_rows_prefers_more_complete_row():
    out = _dedupe_rows([row(1, "История"), row(1, "История", "310", "Савкина А. А.")])
    assert out[0].room == "310"
    assert out[0].teacher == "Савкина А. А."
