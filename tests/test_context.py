from datetime import date, datetime

import pytest

from family_ea.context import (
    Agenda,
    bucket_todos,
    build_context,
    digest_text,
    fmt_due,
    fmt_week,
    render_digest,
    stems,
    todo_line,
    week_note,
    week_start,
    word_pattern,
)
from family_ea.db import Database, Member, Todo
from family_ea.family import Family
from tests.conftest import KYIV


def _c(id: int, **kw) -> Todo:
    base = dict(
        text=f"c{id}",
        owner=None,
        status="open",
        due=None,
        created_by="oleh",
        created_at="2026-09-01T00:00:00Z",
        source_message_id=1,
        closed_at=None,
    )
    base.update(kw)
    return Todo(id=id, **base)


def test_bucket_todos() -> None:
    now = datetime(2026, 9, 10, 8, 0, tzinfo=KYIV)
    items = [
        _c(1, due="2026-09-10"),  # today
        _c(2, due="2026-09-09"),  # yesterday -> overdue
        _c(3, due="2026-09-11"),  # tomorrow -> later
        _c(4, due="2026-09-20"),  # later
        _c(5, due="2026-09-01"),  # long overdue: first
        _c(6),  # no deadline -> open
        _c(7, status="done", due="2026-09-10"),  # closed, ignored
        _c(8, week="2026-09-07"),  # planned for this week
        _c(9, week="2026-08-31"),  # left over from the last one: still in the plan
        _c(10, week="2026-09-14"),  # the next week: ahead
    ]
    b = bucket_todos(items, now)
    assert [c.id for c in b.today] == [1]
    assert [c.id for c in b.overdue] == [5, 2]
    assert [c.id for c in b.later] == [3, 4]
    assert [c.id for c in b.week] == [8, 9]
    assert [c.id for c in b.ahead] == [10]
    assert [c.id for c in b.open] == [6]


def test_weeks_run_from_monday_to_sunday(family: Family) -> None:
    thursday = date(2026, 9, 10)
    monday = date(2026, 9, 7)
    assert week_start(thursday) == monday == week_start(monday)
    assert week_start(date(2026, 9, 13)) == monday  # Sunday ends the week
    assert week_start(date(2026, 9, 14)) == date(2026, 9, 14)  # Monday starts the next
    assert fmt_week(monday) == "07.09–13.09"
    assert fmt_week(date(2026, 9, 28)) == "28.09–04.10"

    notes = {
        "2026-09-07": ("цей тиждень", ""),
        "2026-09-14": ("наступний тиждень", ""),
        "2026-08-31": ("з минулого тижня", "з минулого тижня"),
        "2026-08-24": ("з тижня 24.08–30.08", "з тижня 24.08–30.08"),
        "2026-09-21": ("тиждень 21.09–27.09", "тиждень 21.09–27.09"),
    }
    for week, (said, in_week) in notes.items():
        assert week_note(date.fromisoformat(week), thursday) == said
        assert week_note(date.fromisoformat(week), thursday, in_week=True) == in_week
    assert week_note(monday, None) == "тиждень 07.09–13.09"  # nothing to compare with

    planned = _c(1, text="Пані Марія зустріч", owner="oleh", week="2026-09-07")
    assert todo_line(planned, family, today=thursday) == (
        "[#1] Пані Марія зустріч (Олег, цей тиждень)"
    )
    assert todo_line(planned, family, with_id=False, today=thursday, in_week=True) == (
        "Пані Марія зустріч (Олег)"
    )


def test_fmt_due() -> None:
    assert fmt_due(_c(1, due="2026-09-08")) == "08.09"
    assert fmt_due(_c(1)) == ""


def test_search_stems() -> None:
    # endings go, long words keep a 5-char prefix, function words and digits drop
    assert stems("Хто ремонтував котел?") == ["ремон", "котел"]
    assert stems("ок") == []
    assert stems('він сказав "привіт" 12345') == ["сказа", "приві"]
    assert stems("діти дітям дітьми") == ["діт"]
    assert stems("Коля Колі Колею коли") == ["кол"]  # «коли» is a function word
    assert stems("Марія Марії Марією") == ["марі"]
    assert stems("Оля Олю кум кумом") == ["ол", "кум"]
    assert stems("газовика стоматологу") == ["газов", "стома"]
    assert word_pattern("діти, Коля?") == r"\b(?:діт|кол)"
    assert word_pattern("що це") is None


def test_render_digest_caps_open_list(family: Family) -> None:
    now = datetime(2026, 9, 10, 8, 0, tzinfo=KYIV)
    b = bucket_todos([_c(i) for i in range(1, 9)], now)
    text = render_digest(Agenda(), b, family, now, max_open=5)
    assert "і ще 3" in text
    assert "Сьогодні" not in text


def test_digest_text(family: Family) -> None:
    now = datetime(2026, 9, 10, 8, 30, tzinfo=KYIV)
    items = [
        _c(1, text="Стоматолог", owner="anna", due="2026-09-10"),
        _c(2, text="Поговорити з Марією", due="2026-09-09"),
        _c(3, text="Купити лампочки"),
    ]
    b = bucket_todos(items, now)
    text = digest_text(Agenda(), b, family, now)
    assert text == (
        "Задачі на сьогодні:\n- Стоматолог (Анна, 10.09)\n"
        "Прострочено:\n- Поговорити з Марією (09.09)"
    )
    assert "Купити лампочки" not in text and "[#" not in text  # no undated ones, no ids

    only_undated = bucket_todos([_c(3), _c(4, week="2026-09-14")], now)  # or next week's
    assert digest_text(Agenda(), only_undated, family, now) is None  # nothing to say
    assert digest_text(Agenda(), bucket_todos([], now), family, now) is None


def test_context_shows_the_weeks_and_the_projects(
    db: Database, family: Family, oleh: Member, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-09-10T05:12:00Z")
    now = datetime(2026, 9, 10, 8, 0, tzinfo=KYIV)
    ctx = build_context(db, family, now, oleh, "привіт")
    assert ctx.endswith("## Нове повідомлення\nвід oleh (Олег), текстом:\nпривіт")
    spoken = build_context(db, family, now, oleh, "привіт", is_voice=True)
    assert spoken.endswith("від oleh (Олег), голосове (розпізнаний текст):\nпривіт")
    assert (
        "## Зараз\n2026-09-10 08:00 (Europe/Kyiv), четвер\n"
        "Цей тиждень (week: this): 07.09–13.09, з понеділка по неділю;"
        " наступний (week: next): 14.09–20.09\n" in ctx
    )
    home = db.create_project("Калинівка", created_by="oleh")
    db.create_project("Авто", created_by="oleh")
    mid = db.insert_message("oleh", "oleh", "...")

    def new(text: str, **kw: str | int | None) -> int:
        return db.create_todo(text, owner=None, created_by="oleh", source_message_id=mid, **kw)  # type: ignore[arg-type]

    db.create_todo(
        "Інструкція", owner="oleh", created_by="oleh", source_message_id=mid, project_id=home
    )
    new("Вільна")
    new("Свердловина", project_id=home, week="2026-09-07")  # planned, and still in its project
    new("Підвал", week="2026-09-14")
    ctx = build_context(db, family, now, oleh, "привіт")
    assert (
        "## Проєкти (projects: групи задач для вебу, лише назва; створюються,"
        " перейменовуються і закриваються лише на явне прохання)\n"
        "- [#1] Калинівка (2 відкритих)\n"
        "- [#2] Авто (0 відкритих)\n" in ctx
    )
    assert (
        "- [#4] Підвал (наступний тиждень)\n"
        "- [#3] Свердловина (цей тиждень, проєкт: Калинівка)\n"
        "- [#2] Вільна\n"
        "- [#1] Інструкція (Олег, проєкт: Калинівка)\n" in ctx
    )
    assert (
        "## Сьогодні / прострочено / цей тиждень\n"
        "Цього тижня:\n- [#3] Свердловина\n"
        "Наступні тижні:\n- [#4] Підвал (наступний тиждень)\n"
        "Без дати:\n- [#2] Вільна\n- [#1] Інструкція (Олег)" in ctx
    )
    assert "Списки на сьогодні" not in ctx  # the boards went on 2026-09-27
    assert "Не забути" not in ctx  # and the second one on 2026-09-16


def test_build_context_sections(
    db: Database, family: Family, oleh: Member, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-09-09T12:00:00Z")
    mid = db.insert_message("oleh", "oleh", "Газовик Петро ремонтував котел")
    db.create_item(
        "Паспорт Олі",
        owner="Оля",
        place="квартира",
        spot="білий комод",
        note=None,
        created_by="oleh",
        source_message_id=mid,
    )
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-07-01T12:00:00Z")
    db.create_item(  # old: search only
        "Ключі від офісу",
        owner=None,
        place="офіс",
        spot="сейф",
        note="запасні",
        created_by="anna",
        source_message_id=mid,
    )
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-09-09T12:00:00Z")
    db.create_todo(
        "Поговорити з пані Марією",
        owner="oleh",
        created_by="oleh",
        source_message_id=mid,
        due="2026-09-10",
    )
    db.insert_message("bot", "oleh", "Записав.")
    db.create_dream("Поїхати в Японію з Олею", created_by="anna", source_message_id=mid)
    done = db.create_dream("Пройти Camino de Santiago", created_by="oleh", source_message_id=mid)
    db.close_dream(done, "fulfilled")
    now = datetime(2026, 9, 10, 8, 0, tzinfo=KYIV)
    ctx = build_context(db, family, now, oleh, "Хто ремонтував котел?")
    assert "2026-09-10 08:00 (Europe/Kyiv), четвер" in ctx
    assert "## Сім'я (пишуть боту; решта людей — у фактах)\n- oleh: Олег" in ctx
    assert "[#1] Поговорити з пані Марією (Олег, 10.09)" in ctx
    assert "Задачі на сьогодні:\n- [#1]" in ctx
    assert "## Події (минулі за 7 днів і всі майбутні)\nнемає" in ctx
    assert "journal" not in ctx  # the old log; the notes page has its own section
    assert "повертай повний текст)\nпоки порожньо\n" in ctx
    assert (
        "## Мрії (dreams: спільний список, хто додав; здійснені лише на вебі)\n"
        "- [мрія #1] Поїхати в Японію з Олею (Анна)\n" in ctx
    )
    assert "Camino" not in ctx  # fulfilled: on the web only
    assert "## Речі (items), змінені за останні 2 дні\n- [#1] Паспорт Олі (Оля) → квартира" in ctx
    assert "## Речі, схожі на повідомлення\nнемає" in ctx
    assert "## Відомі місця (place), де лежать речі\nквартира (1), офіс (1)" in ctx
    ctx2 = build_context(db, family, now, oleh, "Де ключі від офісу?")
    assert "## Речі, схожі на повідомлення\n- [#2] Ключі від офісу → офіс / сейф; запасні" in ctx2
    assert "[09.09 15:00] бот → Олег: Записав." in ctx
    assert ctx.rstrip().endswith("від oleh (Олег), текстом:\nХто ремонтував котел?")
