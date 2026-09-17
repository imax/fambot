"""The web home: the boards, the agenda (events and reminders by day) and the todo lists."""

import html
import re
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from family_ea.context import Group, build_calendar, build_todo_lists, day_title
from family_ea.db import Database, Reminder
from family_ea.family import Family
from family_ea.web import build_web
from tests.conftest import KYIV
from tests.test_context import _c
from tests.test_events import _e
from tests.test_web import _auth, _settings, freeze_web_clock

NOW = datetime(2026, 9, 10, 15, 0, tzinfo=KYIV)  # Thursday afternoon


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


def test_build_calendar(family: Family) -> None:
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
    ]
    reminders = [
        _r(1, text="Квіти о 9", who="anna", at="2026-09-11T05:00:00Z"),  # tomorrow 08:00
        _r(2, text="Давно", at="2026-09-01T05:00:00Z"),  # pending but past: today
        _r(3, text="Надіслане", at="2026-09-11T05:00:00Z", status="sent"),
    ]
    cal = build_calendar(events, reminders, NOW, family)

    # two weeks in full; 07.10 is past the horizon, so it waits under «далі»
    assert [d.title for d in cal.days] == [
        "Сьогодні, четвер 10.09",
        "Завтра, п'ятниця 11.09",
        "Субота 12.09",
    ]
    assert [d.today for d in cal.days] == [True, False, False]
    assert [d.title for d in cal.later] == ["Середа 07.10"]
    assert cal.later_line == "07.10 Стрижка"
    today, tomorrow, saturday = cal.days
    (october,) = cal.later
    assert [(r.kind, r.id, r.time, r.note) for r in today.rows] == [
        ("event", 3, "весь день", "до 19.09"),
        ("reminder", 2, "08:00", ""),
        ("event", 2, "10:00", ""),
    ]
    assert [(r.kind, r.id, r.time, r.note, r.who) for r in tomorrow.rows] == [
        ("reminder", 1, "08:00", "", "Анна"),
        ("event", 1, "15:30", "до 16:30", "Анна"),
    ]
    assert today.rows[0].all_day and not today.rows[2].all_day
    assert today.rows[1].who == "усім"
    assert tomorrow.rows[1].ics_url == "/events/1.ics"
    assert [(r.id, r.note) for r in saturday.rows] == [(4, "")]
    assert [(r.kind, r.id, r.time) for r in october.rows] == [("event", 5, "15:30")]


def test_build_todo_lists(family: Family) -> None:
    todos = [
        _c(1, text="Квіти", owner="anna", due="2026-09-11"),  # tomorrow
        _c(2, text="Проспали", due="2026-09-09"),  # yesterday: overdue
        _c(3, text="Сьогодні", due="2026-09-10"),
        _c(4, text="Було давно", due="2026-09-01"),  # overdue, and first: the oldest deadline
        _c(5, text="Далі", due="2026-09-14"),
        _c(6, text="Без дати", owner="oleh"),
        _c(7, text="Закрите", status="done"),
    ]
    t = build_todo_lists(todos, NOW, family)

    assert [(r.id, r.note) for r in t.overdue] == [(4, "до 01.09"), (2, "до 09.09")]
    assert t.overdue[1].ics_url == "/todos/2.ics" and t.overdue[1].time == ""
    assert [(r.id, r.note, r.who) for r in t.dated] == [
        (3, "сьогодні", ""),
        (1, "завтра", "Анна"),
        (5, "до 14.09", ""),
    ]
    assert [g.name for g in t.undated] == [""]  # no projects: one unnamed group
    assert [(r.id, r.who, r.ics_url) for r in t.undated[0].rows] == [(6, "Олег", None)]


def test_empty_lists_and_calendar_keep_today(family: Family) -> None:
    t = build_todo_lists([], NOW, family)
    assert t.overdue == [] and t.dated == [] and t.undated == [Group("", [])]
    cal = build_calendar([], [], NOW, family)
    assert [(d.title, d.rows, d.today) for d in cal.days] == [("Сьогодні, четвер 10.09", [], True)]
    assert cal.later == [] and cal.later_line == ""


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
    db.create_event(  # past the two-week horizon: under «далі», not a day of its own
        "Стрижка", who=None, created_by="oleh", source_message_id=mid, date_from="2026-10-07"
    )
    done = db.create_todo("Замовити воду", owner="anna", created_by="anna", source_message_id=mid)
    db.close_todo(done, "done")
    client = TestClient(build_web(_settings(), family, db))

    home = html.unescape(client.get("/", headers=_auth()).text)  # «п'ятниця» is escaped
    assert home.index('<h2 class="overdue">Прострочено</h2>') < home.index("Купити квіти")
    assert "· до 09.09 · Анна" in home
    assert "<h2>З дедлайном</h2>" not in home  # nothing due from today on
    # the board first, then the agenda, then the todos
    assert (
        home.index("<h2>На сьогодні</h2>")
        < home.index("<h2>Календар</h2>")
        < home.index("Прострочено")
    )

    # The agenda: a heading per day with a planned event or a pending reminder, today
    # always and marked; the time on the left; no todos, no owner.
    agenda = home[home.index("<h2>Календар</h2>") : home.index("Прострочено")]
    assert '<h3 class="now">Сьогодні, четвер 10.09</h3>' in agenda
    assert agenda.index("Сьогодні") < agenda.index("Відпочиваємо :-)")  # empty today, listed
    assert agenda.index("Відпочиваємо") < agenda.index("<h3>Завтра, п'ятниця 11.09</h3>")
    assert agenda.index("<h3>Завтра") < agenda.index("14:30</span>") < agenda.index("15:30</span>")
    assert "Стоматолог<a" in agenda and "до 16:30" not in agenda and "Анна" not in agenda
    assert '<span class="mark">⏰</span>Стоматолог о 15:30' in agenda and "усім" not in agenda
    assert '<li class="event" data-id="1">' in agenda and 'href="/events/1.ics"' in agenda
    assert "<h3>Вівторок 15.09</h3>" in agenda  # the all-day event on 15.09
    assert re.search(r'<span class="time allday">весь день</span>\s*<span>Буріння', agenda)
    assert "Купити квіти" not in agenda
    # what comes after two weeks is one line that unfolds into the same days
    summary = agenda.index("<summary>далі: 07.10 Стрижка</summary>")
    assert agenda.index("<h3>Вівторок 15.09</h3>") < summary < agenda.index("<h3>Середа 07.10</h3>")
    assert "Стоматолог" not in home[home.index("Прострочено") :]
    assert home.index("<h2>Без дати</h2>") < home.index(">Подзвонити газовику Петру</span>")
    assert 'class="id"' not in home  # database ids are not for people
    tail = home[home.index("<h2>Зроблено</h2>") :]  # the last done ones, at the very bottom
    assert home.index("<h2>Без дати</h2>") < home.index("<h2>Зроблено</h2>")
    assert "✓</span>Замовити воду" in tail and "· Анна ·" in tail
    assert "Замовити воду" not in home[: home.index("<h2>Зроблено</h2>")]
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
    client = TestClient(build_web(_settings(), family, db))
    home = client.get("/", headers=_auth()).text
    assert '<span class="mark" title="Торкнись, коли зроблено">☐</span>' in home

    url = f"/todos/{cid}/done"
    assert client.post(url).status_code == 401
    assert client.post(url, headers=_auth()).status_code == 204
    c = db.get_todo(cid)
    assert c is not None and c.status == "done" and c.closed_at == "2026-09-10T12:00:00Z"
    home = client.get("/", headers=_auth()).text
    assert home.index("<h2>Зроблено</h2>") < home.index("Купити хліб")
    assert client.post(url, headers=_auth()).status_code == 404  # once; nothing reopens
    assert client.post("/todos/999/done", headers=_auth()).status_code == 404


def test_web_home_boards_own_first(
    db: Database, family: Family, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze_web_clock(monkeypatch, NOW)
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-09-09T18:00:00Z")  # yesterday
    db.save_today_list("anna", "помити пічку\nзамовити воду", "anna")
    client = TestClient(build_web(_settings(), family, db))

    def head(page: str) -> str:
        return page[page.index("<h2>На сьогодні</h2>") : page.index("<h2>Без дати</h2>")]

    boards = head(client.get("/", headers=_auth("anna")).text)
    assert boards.index("Анна") < boards.index("Олег")
    assert "Анна · оновлено вчора" in boards and "помити пічку\nзамовити воду" in boards
    assert boards.count("порожньо") == 1  # Олег has no board yet
    boards = head(client.get("/", headers=_auth()).text)
    assert boards.index("Олег") < boards.index("Анна")

    # One board only: «Не забути» went on 2026-09-16; the todos come right after it.
    home = client.get("/", headers=_auth("anna")).text
    assert "Не забути" not in home
    assert home.index("<h2>На сьогодні</h2>") < home.index("<h2>Без дати</h2>")


def test_web_home_empty(db: Database, family: Family, monkeypatch: pytest.MonkeyPatch) -> None:
    freeze_web_clock(monkeypatch, NOW)
    client = TestClient(build_web(_settings(), family, db))
    home = client.get("/", headers=_auth()).text
    assert "Прострочено" not in home and "<h2>Сьогодні" not in home
    assert home.count("нічого") == 1  # undated, empty
    assert "<h2>Зроблено</h2>" not in home
    assert "Відпочиваємо :-)" in home  # today, empty, is always there
    assert home.count("<h3") == 1


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
    new("Віза", empty, due="2026-09-20")  # dated: in «З дедлайном», tagged, not in the group
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
    dated = page[page.index("<h2>З дедлайном</h2>") : page.index("<h2>Калинівка")]
    assert "Віза" in dated and "· Документи" in dated

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
