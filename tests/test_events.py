from datetime import UTC, datetime

from fastapi.testclient import TestClient

from family_ea.context import (
    Agenda,
    bucket_todos,
    build_agenda,
    build_context,
    digest_text,
    event_line,
    fmt_event_when,
)
from family_ea.db import Database, Event, Member
from family_ea.family import Family
from family_ea.ical import event_ics, ics_filename
from family_ea.llm import LlmResult
from family_ea.ops import apply_ops
from family_ea.web import build_web
from tests.conftest import KYIV
from tests.test_context import _c
from tests.test_web import _auth, _settings

NOW = datetime(2026, 9, 10, 8, 30, tzinfo=KYIV)  # Thursday morning


def _e(id: int, **kw) -> Event:
    base = dict(
        text=f"e{id}",
        who=None,
        status="planned",
        starts_at=None,
        until=None,
        date_from=None,
        date_to=None,
        created_by="oleh",
        created_at="2026-09-01T00:00:00Z",
        source_message_id=1,
        cancelled_at=None,
    )
    base.update(kw)
    return Event(id=id, **base)


def test_event_line_and_when(family: Family) -> None:
    timed = _e(
        1,
        text="Стоматолог",
        who="anna",
        starts_at="2026-09-11T12:30:00Z",
        until="2026-09-11T13:30:00Z",
    )
    assert fmt_event_when(timed, KYIV) == "11.09 15:30–16:30"
    assert event_line(timed, family, KYIV) == "[подія #1] 11.09 15:30–16:30 Стоматолог (Анна)"
    assert (
        event_line(timed, family, KYIV, with_id=False, with_date=False)
        == "15:30–16:30 Стоматолог (Анна)"
    )
    no_end = _e(2, text="Сніданок", starts_at="2026-09-10T07:00:00Z")
    assert fmt_event_when(no_end, KYIV) == "10.09 10:00"
    assert event_line(no_end, family, KYIV, with_id=False) == "10.09 10:00 Сніданок"
    camp = _e(3, text="Оля в таборі", date_from="2026-09-12", date_to="2026-09-19")
    assert fmt_event_when(camp, KYIV) == "12.09–19.09"
    assert event_line(camp, family, KYIV) == "[подія #3] 12.09–19.09 Оля в таборі"
    assert (
        event_line(camp, family, KYIV, with_id=False, with_date=False) == "Оля в таборі (до 19.09)"
    )
    day = _e(4, text="День народження Марії", date_from="2026-09-10")
    assert fmt_event_when(day, KYIV) == "10.09"
    assert event_line(day, family, KYIV, with_id=False, with_date=False) == "День народження Марії"


def test_build_agenda() -> None:
    items = [
        _e(1, starts_at="2026-09-10T07:00:00Z"),  # today 10:00
        _e(2, starts_at="2026-09-10T04:00:00Z", until="2026-09-10T05:00:00Z"),  # today, over
        _e(3, starts_at="2026-09-11T12:30:00Z"),  # tomorrow
        _e(4, date_from="2026-09-10", date_to="2026-09-19"),  # covers today: today only
        _e(5, date_from="2026-09-11"),  # tomorrow, all-day
        _e(6, date_from="2026-09-20"),  # later
        _e(7, starts_at="2026-09-05T10:00:00Z"),  # five days ago: recent
        _e(8, starts_at="2026-08-20T10:00:00Z"),  # too old: dropped
        _e(9, starts_at="2026-09-10T15:00:00Z", status="cancelled"),  # ignored
        _e(10, starts_at="2026-09-09T22:00:00Z", until="2026-09-10T00:30:00Z"),  # 01:00 Kyiv
    ]
    a = build_agenda(items, NOW)
    assert [e.id for e in a.today] == [4, 10, 2, 1]  # by start; all-day starts at midnight
    assert [e.id for e in a.tomorrow] == [5, 3]
    assert [e.id for e in a.later] == [6]
    assert [e.id for e in a.recent] == [7]
    assert [e.id for e in a.upcoming] == [4, 10, 2, 1, 5, 3, 6]


def test_digest_with_events(family: Family) -> None:
    a = build_agenda(
        [
            _e(
                1,
                text="Сніданок з командою",
                who="oleh",
                starts_at="2026-09-10T07:00:00Z",
                until="2026-09-10T08:00:00Z",
            ),
            _e(2, text="Стоматолог", who="anna", starts_at="2026-09-11T12:30:00Z"),
        ],
        NOW,
    )
    b = bucket_todos(
        [_c(1, text="Забрати форму", owner="anna", due="2026-09-10")],
        NOW,
    )
    assert digest_text(a, b, family, KYIV) == (
        "Сьогодні:\n- 10:00–11:00 Сніданок з командою (Олег)\n"
        "Завтра:\n- 15:30 Стоматолог (Анна)\n"
        "Задачі на сьогодні:\n- Забрати форму (Анна, до 10.09)"
    )
    assert digest_text(Agenda(), bucket_todos([], NOW), family, KYIV) is None


def test_event_ops(db: Database, family: Family) -> None:
    mid = db.insert_message("oleh", "oleh", "...")
    r = LlmResult.model_validate(
        {
            "reply": "",
            "events": [
                {
                    "op": "create",
                    "text": "Сніданок з командою",
                    "who": "oleh",
                    "starts_at": "2026-09-11T10:00:00+03:00",
                    "until": "2026-09-11T11:00:00+03:00",
                },
                {
                    "op": "create",
                    "text": "Оля в таборі",
                    "who": "olia",
                    "date_from": "2026-09-19",
                    "date_to": "2026-09-12",
                },
                {"op": "create", "text": "Без дати"},
                {
                    "op": "create",
                    "text": "Кінець раніше початку",
                    "starts_at": "2026-09-11T10:00:00+03:00",
                    "until": "2026-09-11T09:00:00+03:00",
                },
                {"op": "create", "text": "Лише кінець", "date_to": "2026-09-12"},
            ],
        }
    )
    applied = apply_ops(db, r, author_id="oleh", message_id=mid, family=family, tz=KYIV)
    assert [(a.kind, a.op, a.ok) for a in applied] == [
        ("event", "create", True),
        ("event", "create", True),
        ("event", "create", False),
        ("event", "create", True),
        ("event", "create", True),
    ]
    e1 = db.get_event(applied[0].id)
    assert e1 and e1.starts_at == "2026-09-11T07:00:00Z" and e1.until == "2026-09-11T08:00:00Z"
    assert e1.who == "oleh" and e1.date_from is None and e1.is_planned and not e1.all_day
    e2 = db.get_event(applied[1].id)
    assert e2 and e2.who is None and (e2.date_from, e2.date_to) == ("2026-09-19", "2026-09-19")
    assert "unknown who" in applied[1].note and "date_to" in applied[1].note
    assert applied[2].note == "event without a date"
    e4 = db.get_event(applied[3].id)
    assert e4 and e4.until is None and "until" in applied[3].note
    e5 = db.get_event(applied[4].id)
    assert e5 and (e5.date_from, e5.date_to) == ("2026-09-12", "2026-09-12") and e5.all_day

    r2 = LlmResult.model_validate(
        {
            "reply": "",
            "events": [
                {"op": "update", "id": e1.id, "starts_at": "2026-09-11T11:00:00+03:00"},
                {"op": "update", "id": e1.id, "date_from": "2026-09-13"},
                {"op": "update", "id": 999, "text": "x"},
                {"op": "update", "id": e2.id},
                {"op": "cancel", "id": e2.id},
                {"op": "cancel", "id": e2.id},
                {"op": "update", "id": e2.id, "text": "after cancel"},
            ],
        }
    )
    applied = apply_ops(db, r2, author_id="oleh", message_id=mid, family=family, tz=KYIV)
    assert [(a.op, a.ok) for a in applied] == [
        ("update", True),
        ("update", True),
        ("update", False),
        ("update", False),
        ("cancel", True),
        ("cancel", False),
        ("update", False),
    ]
    assert "until" in applied[0].note  # start moved past the old end: end dropped
    e1 = db.get_event(e1.id)
    assert e1 and e1.starts_at is None and e1.until is None
    assert (e1.date_from, e1.date_to) == ("2026-09-13", "2026-09-13")
    e2 = db.get_event(e2.id)
    assert e2 and e2.status == "cancelled" and e2.cancelled_at and e2.text == "Оля в таборі"
    assert [e.id for e in db.planned_events()] == [e1.id, e4.id, e5.id]
    assert [e.id for e in db.search_events(r"\bтабор")] == [e2.id]


def test_context_lists_events(db: Database, family: Family, oleh: Member) -> None:
    mid = db.insert_message("oleh", "oleh", "...")
    db.create_event(
        "Стоматолог",
        who="anna",
        created_by="oleh",
        source_message_id=mid,
        starts_at="2026-09-11T12:30:00Z",
    )
    db.create_event(
        "Старе",
        who=None,
        created_by="oleh",
        source_message_id=mid,
        starts_at="2026-08-01T10:00:00Z",
    )
    ctx = build_context(db, family, NOW, oleh, "що завтра?")
    assert (
        "## Події (минулі за 7 днів і всі майбутні)\n- [подія #1] 11.09 15:30 Стоматолог (Анна)"
        in ctx
    )
    assert "Старе" not in ctx
    assert "Завтра:\n- [подія #1] 15:30 Стоматолог (Анна)" in ctx


def test_event_ics() -> None:
    timed = _e(1, text="Стоматолог", starts_at="2026-09-11T12:30:00Z", until="2026-09-11T14:00:00Z")
    lines = event_ics(timed, now=datetime(2026, 9, 10, 5, 0, tzinfo=UTC)).decode().split("\r\n")
    assert "UID:event-1@family-ea" in lines and "DTSTAMP:20260910T050000Z" in lines
    assert "DTSTART:20260911T123000Z" in lines and "DTEND:20260911T140000Z" in lines
    assert "DTEND:20260911T133000Z" in event_ics(_e(2, starts_at="2026-09-11T12:30:00Z")).decode()
    camp = event_ics(
        _e(3, text="Оля в таборі", date_from="2026-09-12", date_to="2026-09-19")
    ).decode()
    assert "DTSTART;VALUE=DATE:20260912" in camp and "DTEND;VALUE=DATE:20260920" in camp
    assert ics_filename("Оля в таборі") == "olia_v_tabori.ics"


def test_web_events(db: Database, family: Family) -> None:
    mid = db.insert_message("oleh", "oleh", "...")
    eid = db.create_event(
        "Стоматолог",
        who="anna",
        created_by="oleh",
        source_message_id=mid,
        starts_at="2099-09-11T12:30:00Z",
    )
    cancelled = db.create_event(
        "Скасоване", who=None, created_by="oleh", source_message_id=mid, date_from="2099-09-12"
    )
    db.cancel_event(cancelled)
    client = TestClient(build_web(_settings(), family, db))

    home = client.get("/", headers=_auth())
    assert "Стоматолог" in home.text and f'href="/events/{eid}.ics"' in home.text
    assert "Скасоване" not in home.text
    search = client.get("/", params={"q": "скасован"}, headers=_auth())
    assert "Скасоване" in search.text and "status-cancelled" in search.text

    ics = client.get(f"/events/{eid}.ics", headers=_auth())
    assert ics.status_code == 200 and "UID:event-" in ics.text
    assert 'filename="stomatoloh.ics"' in ics.headers["content-disposition"]
    assert client.get("/events/999.ics", headers=_auth()).status_code == 404
    assert client.get(f"/events/{eid}.ics").status_code == 401
