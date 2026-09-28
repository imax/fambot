"""The web home: the weeks (events and todos by day, the week's plan), the reminders and
the lists of the todos without a day or a week."""

import html
import re
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from family_ea.context import Group, build_plan, day_title, reminder_rows, undated_groups
from family_ea.db import Database, Member, Project, Reminder
from family_ea.family import Family
from family_ea.web import build_web
from tests.conftest import KYIV
from tests.test_context import _c
from tests.test_events import _e
from tests.test_web import _auth, _pipeline, _settings, freeze_web_clock

NOW = datetime(2026, 9, 10, 15, 0, tzinfo=KYIV)  # Thursday afternoon; the week: 07.09–13.09


def _r(id: int, **kw) -> Reminder:
    base = dict(
        text=f"r{id}",
        who=None,
        at="2026-09-11T05:00:00Z",
        status="pending",
        created_by="oleh",
        created_at="2026-09-01T00:00:00Z",
        source_message_id=1,
        sent_at=None,
    )
    base.update(kw)
    return Reminder(id=id, **base)


def test_day_title() -> None:
    today = date(2026, 9, 10)
    assert day_title(today, today) == "Сьогодні, четвер 10.09"
    assert day_title(date(2026, 9, 11), today) == "Завтра, п'ятниця 11.09"
    assert day_title(date(2026, 10, 7), today) == "Середа 07.10"


def test_plan_puts_events_into_weeks(family: Family) -> None:
    events = [
        _e(
            1,
            text="Стоматолог",
            who="anna",
            starts_at="2026-09-11T12:30:00Z",
            until="2026-09-11T13:30:00Z",
        ),
        _e(2, text="Ранкова", starts_at="2026-09-10T07:00:00Z"),  # today 10:00, over: still today
        _e(3, text="Табір", date_from="2026-09-08", date_to="2026-09-19"),  # running: today, «до»
        _e(4, text="Гості", date_from="2026-09-12"),  # Saturday, all-day
        _e(5, text="Стрижка", starts_at="2026-10-07T12:30:00Z"),  # far ahead
        _e(6, text="Минуле", starts_at="2026-09-05T10:00:00Z"),  # over: not on the page
        _e(7, text="Скасоване", starts_at="2026-09-11T10:00:00Z", status="cancelled"),
        _e(8, text="Буріння", date_from="2026-09-15"),  # Tuesday of the next week
        _e(9, text="Неділя", date_from="2026-09-20"),  # the last day of the next week
        _e(10, text="Понеділок", date_from="2026-09-21"),  # the first day past the two weeks
    ]
    plan = build_plan(events, [], NOW, family)

    this, nxt = plan.weeks
    assert (this.title, this.span) == ("Цей тиждень", "07.09–13.09")
    assert (nxt.title, nxt.span) == ("Наступний тиждень", "14.09–20.09")
    assert [d.title for d in this.days] == [
        "Сьогодні, четвер 10.09",
        "Завтра, п'ятниця 11.09",
        "Субота 12.09",
    ]
    assert [d.today for d in this.days] == [True, False, False]
    assert [d.title for d in nxt.days] == ["Вівторок 15.09", "Неділя 20.09"]
    # what comes after the two weeks stands under «Далі»
    assert [d.title for d in plan.later] == ["Понеділок 21.09", "Середа 07.10"]
    today, tomorrow, saturday = this.days
    assert [(r.kind, r.id, r.time, r.note) for r in today.rows] == [
        ("event", 3, "весь день", "до 19.09"),
        ("event", 2, "10:00", ""),
    ]
    assert [(r.kind, r.id, r.time, r.note, r.who) for r in tomorrow.rows] == [
        ("event", 1, "15:30", "до 16:30", "Анна"),
    ]
    assert today.rows[0].all_day and not today.rows[1].all_day
    assert [(r.id, r.note) for r in saturday.rows] == [(4, "")]
    assert [(r.kind, r.id, r.time) for r in plan.later[1].rows] == [("event", 5, "15:30")]
    assert this.overdue == [] and this.rows == [] and nxt.rows == []


def test_plan_puts_todos_into_weeks(family: Family) -> None:
    car = Project(1, "Авто", "open", "oleh", "2026-09-01T00:00:00Z", None)
    todos = [
        _c(1, text="Квіти", owner="anna", due="2026-09-11"),  # tomorrow
        _c(2, text="Проспали", due="2026-09-09"),  # yesterday: overdue
        _c(3, text="Сьогодні", due="2026-09-10"),
        _c(4, text="Було давно", due="2026-09-01"),  # overdue, and first: the oldest deadline
        _c(5, text="Далі", due="2026-09-14"),  # Monday of the next week
        _c(6, text="Без дати", owner="oleh"),
        _c(7, text="Закрите", status="done", week="2026-09-07"),
        _c(8, text="Газовик пінг", owner="oleh", week="2026-09-07"),
        _c(9, text="Вікна", week="2026-08-31"),  # last week's, not done: still in the plan
        _c(10, text="Масло", week="2026-09-14", project_id=1),  # the next week; keeps its project
        _c(11, text="Колись", week="2026-09-28"),  # a week the page does not show yet
        _c(12, text="Віза", due="2026-10-07"),
    ]
    events = [_e(1, text="Стоматолог", starts_at="2026-09-11T12:30:00Z")]
    plan = build_plan(events, todos, NOW, family, [car])

    this, nxt = plan.weeks
    assert [(r.id, r.note) for r in this.overdue] == [(4, "01.09"), (2, "09.09")]
    today, tomorrow = this.days
    assert [(r.kind, r.id, r.note, r.time) for r in today.rows] == [("todo", 3, "", "")]
    # within a day the events come first, then what is to be done that day
    assert [(r.kind, r.id, r.who) for r in tomorrow.rows] == [
        ("event", 1, ""),
        ("todo", 1, "Анна"),
    ]
    assert [(r.id, r.note, r.who) for r in this.rows] == [
        (8, "", "Олег"),
        (9, "з минулого тижня", ""),
    ]
    assert [(d.title, [r.id for r in d.rows]) for d in nxt.days] == [("Понеділок 14.09", [5])]
    assert [(r.id, r.note, r.project) for r in nxt.rows] == [(10, "", "Авто")]
    assert [(d.title, [(r.id, r.note) for r in d.rows]) for d in plan.later] == [
        ("Понеділок 28.09", [(11, "тиждень 28.09–04.10")]),
        ("Середа 07.10", [(12, "")]),
    ]

    # The lists under the weeks hold what has neither a day nor a week.
    (loose, by_project) = undated_groups(todos, family, [car])
    assert (loose.name, [(r.id, r.who) for r in loose.rows]) == ("", [(6, "Олег")])
    assert (by_project.name, by_project.project_id, by_project.rows) == ("Авто", 1, [])
    assert [g.name for g in undated_groups(todos, family)] == [""]  # no projects: one group


def test_plan_shows_what_the_week_got_done(family: Family) -> None:
    """Done since this Monday: at the foot of this week, newest first, whatever it was;
    the tally counts them against the week's open todos (2026-09-28)."""
    todos = [
        _c(1, text="Пошта", due="2026-09-11"),
        _c(2, text="Газовик пінг", owner="oleh", week="2026-09-07"),
        _c(3, text="Проспали", due="2026-09-09"),  # overdue: counted
        _c(4, text="Наступного", week="2026-09-14"),  # the next week's: not
        _c(5, text="Без дати"),  # not in the week: not
    ]
    done = [
        _c(6, text="Масло", status="done", project_id=1, closed_at="2026-09-08T10:00:00Z"),
        _c(7, text="Вікна", owner="anna", status="done", closed_at="2026-09-10T09:00:00Z"),
        _c(8, text="Минулого", status="done", closed_at="2026-09-06T20:59:59Z"),  # Sun 23:59
        _c(9, text="Знята", status="dropped", closed_at="2026-09-10T09:00:00Z"),
    ]
    car = Project(1, "Авто", "open", "oleh", "2026-09-01T00:00:00Z", None)
    this, nxt = build_plan([], todos, NOW, family, [car], done).weeks
    assert [(r.kind, r.id, r.who, r.project) for r in this.done] == [
        ("done", 7, "Анна", ""),
        ("done", 6, "", "Авто"),
    ]
    assert this.progress == "зроблено 2 з 5" and nxt.progress == "" and nxt.done == []
    # Only done ones left: the week stays, to show it; nothing at all: no tally.
    (this,) = build_plan([], [], NOW, family, [car], done[:1]).weeks
    assert not this.empty and this.progress == "зроблено 1 з 1"
    assert build_plan([], [], NOW, family).weeks[0].progress == ""


def test_reminder_rows(family: Family) -> None:
    # Their own block: by time, the repeating ones last; never overdue: one whose time has
    # passed is about to be sent, so today's.
    reminders = [
        _r(1, text="Квіти о 9", who="anna", at="2026-09-11T05:00:00Z"),  # tomorrow 08:00
        _r(2, text="Давно", at="2026-09-01T05:00:00Z"),  # pending but past: today
        _r(3, text="Надіслане", at="2026-09-11T05:00:00Z", status="sent"),
        _r(4, text="Планка", at="2026-09-10T16:30:00Z", repeat="daily"),  # today 19:30
        _r(5, text="Далі", at="2026-09-14T09:00:00Z"),  # 12:00 on 14.09
    ]
    rows = reminder_rows(reminders, NOW, family)
    assert [(r.kind, r.id, r.note, r.who) for r in rows] == [
        ("reminder", 2, "сьогодні 08:00", "усім"),
        ("reminder", 1, "завтра 08:00", "Анна"),
        ("reminder", 5, "14.09 12:00", "усім"),
        ("reminder", 4, "сьогодні 19:30 · щодня", "усім"),  # repeats: after everything
    ]
    assert all(r.time == "" for r in rows)
    assert reminder_rows([], NOW, family) == []


def test_an_empty_plan_keeps_this_week(family: Family) -> None:
    assert undated_groups([], family) == [Group("", [])]
    plan = build_plan([], [], NOW, family)
    (this,) = plan.weeks  # nothing anywhere: this week stays, as the page's empty state
    assert this.title == "Цей тиждень" and this.empty
    assert this.days == [] and this.overdue == [] and this.rows == []
    assert plan.later == []

    # On a Sunday this week is that one day; Monday belongs to the next. With nothing
    # left in it, the week is not shown.
    sunday = datetime(2026, 9, 13, 20, 0, tzinfo=KYIV)
    monday = [_e(1, text="Буріння", date_from="2026-09-14")]
    (nxt,) = build_plan(monday, [], sunday, family).weeks
    assert nxt.title == "Наступний тиждень" and nxt.span == "14.09–20.09"
    assert [d.title for d in nxt.days] == ["Завтра, понеділок 14.09"]
    # Anything in it brings it back; a day with nothing in it, today too, stays out.
    plan = build_plan(monday, [_c(1, text="Газовик пінг", week="2026-09-07")], sunday, family)
    this, nxt = plan.weeks
    assert this.span == "07.09–13.09" and [r.id for r in this.rows] == [1]
    assert this.days == []
    todos = [_c(2, text="Пошта", due="2026-09-13")]
    (this, nxt) = build_plan(monday, todos, sunday, family).weeks
    assert [(d.title, d.today) for d in this.days] == [("Сьогодні, неділя 13.09", True)]


def test_web_home(db: Database, family: Family, monkeypatch: pytest.MonkeyPatch) -> None:
    freeze_web_clock(monkeypatch, NOW)
    mid = db.insert_message("oleh", "oleh", "...")
    db.create_event(
        "Стоматолог",
        who="anna",
        created_by="oleh",
        source_message_id=mid,
        starts_at="2026-09-11T12:30:00Z",
        until="2026-09-11T13:30:00Z",
    )
    db.create_reminder(
        "Стоматолог о 15:30",
        who=None,
        at="2026-09-11T11:30:00Z",
        created_by="oleh",
        source_message_id=mid,
    )
    db.create_todo(
        "Купити квіти", owner="anna", created_by="oleh", source_message_id=mid, due="2026-09-09"
    )
    db.create_todo(
        "Подзвонити газовику Петру", owner=None, created_by="oleh", source_message_id=mid
    )
    db.create_event(
        "Буріння", who=None, created_by="oleh", source_message_id=mid, date_from="2026-09-15"
    )
    db.create_event(
        "Стрижка", who=None, created_by="oleh", source_message_id=mid, date_from="2026-10-07"
    )
    db.create_todo(
        "Газовик пінг", owner="oleh", created_by="oleh", source_message_id=mid, week="2026-09-07"
    )
    db.create_todo(
        "Забрати форму", owner="anna", created_by="oleh", source_message_id=mid, due="2026-09-11"
    )
    db.create_todo(
        "Пошта", owner="oleh", created_by="oleh", source_message_id=mid, due="2026-09-10"
    )
    old = db.create_todo("Вікна", owner="oleh", created_by="oleh", source_message_id=mid)
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-09-06T20:59:00Z")  # Sunday
    db.close_todo(old, "done")  # last week's: not on the page any more
    done = db.create_todo("Замовити воду", owner="anna", created_by="anna", source_message_id=mid)
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-09-06T21:00:00Z")  # Mon 00:00
    db.close_todo(done, "done")
    client = TestClient(build_web(_settings(), family, db))

    home = html.unescape(client.get("/", headers=_auth()).text)  # «п'ятниця» is escaped
    # this week (with what it got done out of its todos), the next one, the tail, the
    # reminders, then the lists
    heads = [
        '<h2>Цей тиждень <span class="meta">· 07.09–13.09 · зроблено 1 з 5</span></h2>',
        '<h2>Наступний тиждень <span class="meta">· 14.09–20.09</span></h2>',
        "<h2>Далі</h2>",
        "<h2>Нагадування</h2>",
        "<h2>Без дати</h2>",
    ]
    assert [home.index(h) for h in heads] == sorted(home.index(h) for h in heads)
    assert "На сьогодні" not in home and "<h2>Календар</h2>" not in home  # until 2026-09-27
    assert "<h2>Задачі</h2>" not in home and '<h2 class="overdue">' not in home

    # This week: what is late, then a heading per day from today on (today marked; only
    # the days that hold something), then what is planned for the week without a day.
    week = home[home.index(heads[0]) : home.index(heads[1])]
    parts = [
        '<h3 class="overdue">Прострочено</h3>',
        "Купити квіти",
        '<h3 class="now">Сьогодні, четвер 10.09</h3>',
        "Пошта",
        "<h3>Завтра, п'ятниця 11.09</h3>",
        "15:30</span>",
        "Забрати форму",  # the todo of that day, after its events
        "<h3>Протягом тижня</h3>",
        "Газовик пінг",
        "Замовити воду",  # done this week: at the foot of its plan (2026-09-28)
    ]
    assert [week.index(x) for x in parts] == sorted(week.index(x) for x in parts)
    assert re.search(
        r'<li class="done" data-id="\d+">\s*<span class="mark">✓</span>\s*'
        r'<span><span class="text">Замовити воду</span> <span class="meta">· Анна<',
        week,
    )
    assert "Вікна" not in home and "<h2>Зроблено</h2>" not in home  # the page's tail until then
    assert "· 09.09 · Анна" in week  # late since, whose
    # An event: the time on the left, the text, no owner, no end.
    assert '<li class="event" data-id="1">' in week and ".ics" not in home
    assert "Стоматолог</span>" in week and "до 16:30" not in week
    # A todo: its «☐» where the time would be, whose it is after the text.
    assert re.search(
        r'<li class="todo" data-id="\d+">\s*<span class="mark" title="Торкнись, коли зроблено">'
        r'☐</span>\s*<span><span class="text">Газовик пінг</span> <span class="meta">· Олег<',
        week,
    )
    assert week.count("☐") == 4 and week.count('class="edit"') == 4
    assert "Відпочиваємо" not in home and "<h3>Субота" not in week  # no empty days
    assert 'class="grip"' not in week  # the order within a week is not set by hand
    assert "⏰" not in week and "14:30" not in week

    following = home[home.index(heads[1]) : home.index(heads[2])]
    assert "<h3>Вівторок 15.09</h3>" in following  # the all-day event on 15.09
    assert re.search(r'<span class="time allday">весь день</span>\s*<span>Буріння', following)
    assert "Протягом тижня" not in following and "Прострочено" not in following
    # what comes after the two weeks: «Далі», the same days, in full (no line to unfold)
    tail = home[home.index(heads[2]) : home.index(heads[3])]
    assert "<h3>Середа 07.10</h3>" in tail and "Стрижка" in tail
    assert "<details>" not in home and "далі:" not in home

    # «Нагадування»: its own block under the weeks, «⏰» for «☐»
    ahead = home[home.index("<h2>Нагадування</h2>") : home.index("<h2>Без дати</h2>")]
    assert re.search(r'<span class="mark">⏰</span>\s*<span>Стоматолог о 15:30', ahead)
    assert "· завтра 14:30 · усім" in ahead and "☐" not in ahead

    undated = home[home.index("<h2>Без дати</h2>") :]
    assert ">Подзвонити газовику Петру</span>" in undated
    assert "Стоматолог" not in undated and "Газовик пінг" not in undated
    assert "Замовити воду" not in undated
    assert 'class="id"' not in home  # database ids are not for people
    assert (
        client.get("/calendar", headers=_auth()).status_code == 404
    )  # its own tab until 2026-09-16


def test_web_undated_order_by_dragging(
    db: Database, family: Family, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two or more undated rows get a «⋮⋮» handle; the drag posts the ids in their new order."""
    freeze_web_clock(monkeypatch, NOW)
    mid = db.insert_message("oleh", "oleh", "...")
    db.create_todo(
        "Квіти",
        owner="anna",
        created_by="oleh",
        source_message_id=mid,
        due="2026-09-11",
    )
    first = db.create_todo("Перша", owner=None, created_by="oleh", source_message_id=mid)
    second = db.create_todo("Друга", owner=None, created_by="oleh", source_message_id=mid)
    client = TestClient(build_web(_settings(), family, db))

    home = client.get("/", headers=_auth()).text
    undated = home[home.index("<h2>Без дати</h2>") :]
    assert undated.index("Друга") < undated.index("Перша")  # newest first until dragged
    assert '<ul class="rows sortable" data-project="">' in undated
    assert f'data-id="{first}"' in undated
    assert home.count('class="grip"') == 2 == undated.count('class="grip"')  # dated rows: none

    r = client.post("/todos/order", data={"ids": [first, second]}, headers=_auth())
    assert r.status_code == 204
    home = client.get("/", headers=_auth()).text
    undated = home[home.index("<h2>Без дати</h2>") :]
    assert undated.index("Перша") < undated.index("Друга")
    assert client.post("/todos/order", data={"ids": [first]}).status_code == 401

    db.close_todo(second, "done")  # one row left: nothing to drag
    home = client.get("/", headers=_auth()).text
    assert 'class="rows sortable"' not in home and 'class="grip"' not in home


def test_web_todo_text_edit(db: Database, family: Family, monkeypatch: pytest.MonkeyPatch) -> None:
    freeze_web_clock(monkeypatch, NOW)
    mid = db.insert_message("oleh", "oleh", "хліб")
    cid = db.create_todo("Купити хліб", owner=None, created_by="oleh", source_message_id=mid)
    client = TestClient(build_web(_settings(), family, db))

    home = client.get("/", headers=_auth()).text
    assert f'<li class="todo" data-id="{cid}">' in home
    assert '<span class="text">Купити хліб</span>' in home
    assert '<button class="edit" type="button" title="Змінити текст">✎</button>' in home

    url = f"/todos/{cid}/text"
    r = client.post(url, data={"text": "  Купити хліб і молоко\n"}, headers=_auth())
    assert r.status_code == 204
    assert db.get_todo(cid).text == "Купити хліб і молоко"  # type: ignore[union-attr]
    assert "Купити хліб і молоко" in client.get("/", headers=_auth()).text
    assert client.post(url, data={"text": "  "}, headers=_auth()).status_code == 400
    missing = client.post("/todos/999/text", data={"text": "x"}, headers=_auth())
    assert missing.status_code == 404
    assert client.post(url, data={"text": "x"}).status_code == 401

    db.close_todo(cid, "done")  # closed ones are not editable
    assert client.post(url, data={"text": "x"}, headers=_auth()).status_code == 404
    assert db.get_todo(cid).text == "Купити хліб і молоко"  # type: ignore[union-attr]


def test_web_todo_done(db: Database, family: Family, monkeypatch: pytest.MonkeyPatch) -> None:
    freeze_web_clock(monkeypatch, NOW)
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-09-10T12:00:00Z")
    mid = db.insert_message("oleh", "oleh", "хліб")
    cid = db.create_todo("Купити хліб", owner=None, created_by="oleh", source_message_id=mid)
    heard: list[tuple[str, list[tuple[str, str, int | None, bool]]]] = []

    async def announce(author: Member, applied: list) -> None:
        heard.append((author.id, [(a.kind, a.op, a.id, a.ok) for a in applied]))

    pipeline = _pipeline(db, family)
    pipeline.announce = announce
    client = TestClient(build_web(_settings(), family, db, pipeline=pipeline))
    home = client.get("/", headers=_auth()).text
    assert '<span class="mark" title="Торкнись, коли зроблено">☐</span>' in home

    url = f"/todos/{cid}/done"
    assert client.post(url).status_code == 401
    assert client.post(url, headers=_auth()).status_code == 204
    c = db.get_todo(cid)
    assert c is not None and c.status == "done" and c.closed_at == "2026-09-10T12:00:00Z"
    assert heard == [("oleh", [("todo", "close:done", cid, True)])]  # the others hear of it
    home = client.get("/", headers=_auth()).text  # done this week: at the foot of its plan
    assert home.index("<h3>Протягом тижня</h3>") < home.index("Купити хліб")
    assert '<li class="done" data-id="1">' in home and '<li class="todo"' not in home
    assert client.post(url, headers=_auth()).status_code == 404  # once; nothing reopens
    assert client.post("/todos/999/done", headers=_auth()).status_code == 404


def test_web_week_is_one_list_for_everyone(
    db: Database, family: Family, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan of the week is shared: both see the same rows, each with its owner, and
    either one marks the other's done."""
    freeze_web_clock(monkeypatch, NOW)
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-09-10T12:00:00Z")
    mid = db.insert_message("anna", "anna", "...")
    hers = db.create_todo(
        "Помити пічку", owner="anna", created_by="anna", source_message_id=mid, week="2026-09-07"
    )
    db.create_todo(
        "Планка", owner="oleh", created_by="oleh", source_message_id=mid, week="2026-09-07"
    )
    client = TestClient(build_web(_settings(), family, db))

    def week(page: str) -> str:
        return page[page.index("<h3>Протягом тижня</h3>") : page.index("<h2>Без дати</h2>")]

    mine = week(client.get("/", headers=_auth()).text)
    assert mine == week(client.get("/", headers=_auth("anna")).text)
    assert "Помити пічку" in mine and "· Анна<" in mine and "· Олег<" in mine
    assert client.post(f"/todos/{hers}/done", headers=_auth()).status_code == 204  # Олег's tap
    theirs = week(client.get("/", headers=_auth("anna")).text)  # still there, done
    assert re.search(
        r'<li class="done" data-id="1">\s*<span class="mark">✓</span>\s*'
        r'<span><span class="text">Помити пічку',
        theirs,
    )


def test_web_home_empty(db: Database, family: Family, monkeypatch: pytest.MonkeyPatch) -> None:
    freeze_web_clock(monkeypatch, NOW)
    client = TestClient(build_web(_settings(), family, db))
    home = client.get("/", headers=_auth()).text
    assert "Прострочено" not in home and "<h2>Сьогодні" not in home
    assert home.count("нічого") == 1  # undated, empty
    assert "<h2>Зроблено</h2>" not in home
    # Nothing in either week: this one stays and says so, without a day under it.
    assert '<h2>Цей тиждень <span class="meta">· 07.09–13.09</span></h2>' in home
    assert home.count("Відпочиваємо :-)") == 1 and "<h3" not in home
    assert "Наступний тиждень" not in home and "<h2>Далі</h2>" not in home


def test_web_home_groups_undated_todos_by_project(
    db: Database, family: Family, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze_web_clock(monkeypatch, NOW)
    mid = db.insert_message("oleh", "oleh", "...")
    home = db.create_project("Калинівка", created_by="oleh")
    car = db.create_project("Авто", created_by="oleh")
    empty = db.create_project("Документи", created_by="oleh")

    def new(text: str, project: int | None = None, due: str | None = None) -> int:
        return db.create_todo(
            text, owner=None, created_by="oleh", source_message_id=mid, due=due, project_id=project
        )

    new("Інструкція по дому", home)
    new("Свердловина", home)
    new("XC90", car)
    new("Окрема")
    new("Віза", empty, due="2026-09-20")  # dated: on its day in a week, tagged, not in the group
    client = TestClient(build_web(_settings(), family, db))
    page = client.get("/", headers=_auth()).text
    lists = page[page.index("<h2>Без дати</h2>") : page.index("<script>")]
    # «Без дати» first, for the ones without a project, then an h2 per project in their order.
    heads = ["<h2>Без дати</h2>", "<h2>Калинівка", "<h2>Авто", "<h2>Документи"]
    assert [lists.index(h) for h in heads] == sorted(lists.index(h) for h in heads)
    assert "<h3>" not in lists and "Без проєкту" not in lists
    assert lists.index("Свердловина") < lists.index("Інструкція")  # newest first, unplaced
    assert lists.index("<h2>Авто") < lists.index("XC90") < lists.index("<h2>Документи")
    assert lists.index("<h2>Без дати</h2>") < lists.index("Окрема")
    # Every list is a drop target, with a placeholder shown only while it is empty; a «⋮⋮»
    # on each of the 4 rows and on the 3 project headings.
    assert lists.count('class="rows sortable"') == 4 and lists.count('class="grip"') == 7
    assert lists.count('<li class="empty">нічого') == 1  # Документи
    assert lists.count('<li class="empty" hidden>нічого') == 3
    assert f'data-project="{car}"' in lists and 'data-project=""' in lists
    dated = page[page.index("<h2>Наступний тиждень") : page.index("<h2>Без дати</h2>")]
    assert "<h3>Неділя 20.09</h3>" in dated and "Віза" in dated and "· Документи" in dated

    # A drag into another list posts that list's order with its project: the todo moves.
    xc90 = next(t for t in db.open_todos() if t.text == "XC90")
    r = client.post(
        "/todos/order", data={"ids": [xc90.id, 1], "project": str(home)}, headers=_auth()
    )
    assert r.status_code == 204
    assert db.get_todo(xc90.id).project_id == home and db.get_todo(1).project_id == home
    assert [t.id for t in db.open_todos() if t.position] == [xc90.id, 1]  # placed, in order
    r = client.post("/todos/order", data={"ids": [xc90.id], "project": ""}, headers=_auth())
    assert r.status_code == 204 and db.get_todo(xc90.id).project_id is None
    r = client.post("/todos/order", data={"ids": [xc90.id], "project": "999"}, headers=_auth())
    assert r.status_code == 404 and db.get_todo(xc90.id).project_id is None
    page = client.get("/", headers=_auth()).text
    rest = page[page.index("<h2>Без дати</h2>") : page.index("<h2>Калинівка")]
    assert "XC90" in rest and "Окрема" in rest

    # A «⋮⋮» on every project heading, none on «Без дати»; the dragged order is posted whole.
    lists = page[page.index("<h2>Без дати</h2>") : page.index("<script>")]
    handle = '<span class="grip" title="Потягни, щоб переставити проєкт">'
    for name in ("Калинівка", "Авто", "Документи"):
        assert f"<h2>{name}{handle}" in lists
    assert "grip" not in lists[lists.index("<h2>Без дати") :].split("</h2>")[0]
    assert f'<section class="project" data-project="{car}">' in lists
    r = client.post("/projects/order", data={"ids": [car, home, empty]}, headers=_auth())
    assert r.status_code == 204
    assert [p.id for p in db.open_projects()] == [car, home, empty]
    assert client.post("/projects/order", data={"ids": [car]}).status_code == 401
    page = client.get("/", headers=_auth()).text
    assert page.index("<h2>Авто") < page.index("<h2>Калинівка")

    db.close_project(home)
    page = client.get("/", headers=_auth()).text
    assert "<h2>Калинівка" not in page
    rest = page[page.index("<h2>Без дати</h2>") : page.index("<h2>Авто")]
    assert "Інструкція" in rest  # detached

    # Without any project the list is plain, as before: one «Без дати», no project handle.
    for p in db.open_projects():
        db.close_project(p.id)
    page = client.get("/", headers=_auth()).text
    assert page.count("<h2>Без дати</h2>") == 1 and handle not in page
