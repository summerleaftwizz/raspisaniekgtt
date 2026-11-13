"""Тесты diff-уведомлений: сравнение пар по номеру и предмету, без ложных срабатываний."""
from types import SimpleNamespace

from services.changes import _diff, diff_text


def row(num, subject, room="", teacher="", time_=""):
    """Строка БД / спарсенная пара (одно и то же представление полей)."""
    return SimpleNamespace(
        lesson_number=num, subject=subject, room=room, teacher=teacher, time=time_,
        subgroups=None,
    )


def test_same_pair_no_change_even_if_teacher_differs():
    """Номер+предмет+кабинет совпали — изменения НЕТ, даже если ФИО другое."""
    old = [row(1, "Русский язык", "301", "Савкина А. А.")]
    new = [row(1, "Русский язык", "301", "Другая Ф. И.")]
    lines, touched = _diff("13.11", old, new)
    assert lines == []
    assert touched == set()


def test_room_moved_notification():
    old = [row(1, "Русский язык", "301", "Савкина А. А.")]
    new = [row(1, "Русский язык", "310", "Савкина А. А.")]
    lines, touched = _diff("13.11", old, new)
    text = "\n".join(lines)
    assert "перенесена в каб. 310 (было 301)" in text
    assert "Добавлена" not in text and "Отменена" not in text
    assert touched == {"Савкина А. А."}


def test_room_missing_in_new_file_no_false_move():
    """Кабинет в новом файле не найден — НЕ «перемещена в каб. (было 301)»."""
    old = [row(1, "Русский язык", "301", "Савкина А. А.")]
    new = [row(1, "Русский язык", "", "Савкина А. А.")]
    lines, _touched = _diff("13.11", old, new)
    assert lines == []
    assert "перемещена" not in diff_text("13.11", "ГР", old, new)


def test_added_pair():
    old = [row(1, "Русский язык", "301")]
    new = [row(1, "Русский язык", "301"), row(2, "Физика", "310")]
    lines, _touched = _diff("13.11", old, new)
    text = "\n".join(lines)
    assert "➕ Добавлена <b>2 пара</b>: Физика, каб. 310" in text
    assert "Отменена" not in text


def test_canceled_pair():
    old = [row(1, "Русский язык", "301"), row(2, "Физика", "310")]
    new = [row(1, "Русский язык", "301")]
    lines, touched = _diff("13.11", old, new)
    text = "\n".join(lines)
    assert "❌ Отменена <b>2 пара</b>: Физика" in text
    assert touched == set()  # у отменённой пары ФИО не было


def test_empty_pairs_only_no_notification():
    """Изменения только среди пустых пар (без предмета) — уведомления нет."""
    old = [row(1, ""), row(3, "Математика", "307")]
    new = [row(1, ""), row(2, ""), row(3, "Математика", "307")]
    lines, _touched = _diff("13.11", old, new)
    assert lines == []


def test_window_rows_ignored():
    """Пары-«окна» из файла не считаются ни добавленными, ни отменёнными."""
    old = [row(1, "окно"), row(2, "Русский язык", "301")]
    new = [row(1, "", ""), row(2, "Русский язык", "301")]
    lines, _touched = _diff("13.11", old, new)
    assert lines == []


def test_subject_replacement_reported():
    """Тот же номер, другой предмет — реальная замена, сообщаем."""
    old = [row(1, "Русский язык", "301")]
    new = [row(1, "Математика", "301")]
    lines, _touched = _diff("13.11", old, new)
    text = "\n".join(lines)
    assert "теперь Математика (было Русский язык)" in text
    assert "Добавлена" not in text


def test_subject_and_room_changed_together():
    old = [row(1, "Русский язык", "301")]
    new = [row(1, "Математика", "305")]
    lines, _touched = _diff("13.11", old, new)
    text = "\n".join(lines)
    assert "теперь Математика" in text
    assert "перенесена в каб. 305 (было 301)" in text
