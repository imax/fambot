"""Nudges: a todo without a day or a project comes back once, the day after, at noon, with
«Зроблено» and «Завтра» under it. One per member per day; silence unless «Завтра»."""

import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

from family_ea.bot import (
    NUDGE_PATTERN,
    deliver_due_nudges,
    nudge_keyboard,
    nudge_tap,
    pick_nudges,
)
from family_ea.context import todo_line
from family_ea.db import Database, Member, Todo
from family_ea.family import Family
from family_ea.llm import LlmResult
from family_ea.ops import apply_ops
from tests.conftest import KYIV


def _apply(db: Database, family: Family, ops: list[dict], author: str = "oleh") -> list:
    mid = db.insert_message(author, author, "...")
    result = LlmResult.model_validate({"reply": "", "todos": ops})
    return apply_ops(db, result, author_id=author, message_id=mid, family=family, tz=KYIV)


def test_a_loose_end_gets_a_nudge_for_tomorrow(
    db: Database, family: Family, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "family_ea.db.utc_now_iso", lambda: "2026-09-15T22:30:00Z"
    )  # 16.09 01:30 Kyiv
    db.create_project("Авто", created_by="oleh")
    applied = _apply(
        db,
        family,
        [
            {"op": "create", "text": "Подзвонити по клініках"},
            {"op": "create", "text": "Замовити воду", "due": "2026-09-20"},
            {"op": "create", "text": "Поміняти масло", "project": "Авто"},
            {"op": "create", "text": "Віза", "remind_on": "2026-09-25"},
            {"op": "create", "text": "Без нагадування", "remind_on": "-"},
            {"op": "create", "text": "Крива дата", "remind_on": "колись"},
        ],
    )
    assert all(a.ok for a in applied)
    assert [t.remind_on for t in sorted(db.open_todos(), key=lambda t: t.id)] == [
        "2026-09-17",  # the day after it was filed, in Kyiv
        None,  # a day: the digest has it
        None,  # a project: backlog
        "2026-09-25",  # asked for
        None,
        "2026-09-17",  # the bad day is dropped, the default stands
    ]
    assert applied[5].note == "bad remind_on 'колись' dropped"

    # A day or a project given later takes the nudge with it; an explicit day is kept;
    # «-» drops it; a text change leaves it.
    (a,) = _apply(db, family, [{"op": "update", "id": 1, "text": "Подзвонити в клініку"}])
    assert a.ok and db.get_todo(1).remind_on == "2026-09-17"
    _apply(db, family, [{"op": "update", "id": 1, "due": "2026-09-19"}])
    assert db.get_todo(1).remind_on is None
    _apply(db, family, [{"op": "update", "id": 4, "project": "Авто", "remind_on": "2026-09-30"}])
    assert db.get_todo(4).remind_on == "2026-09-30"
    _apply(db, family, [{"op": "update", "id": 4, "remind_on": "-"}])
    assert db.get_todo(4).remind_on is None
    _apply(db, family, [{"op": "update", "id": 6, "remind_on": "2026-09-18T12:00:00+03:00"}])
    assert db.get_todo(6).remind_on == "2026-09-18"  # a day, never a time
    (bad,) = _apply(db, family, [{"op": "update", "id": 6, "remind_on": "?"}])
    assert not bad.ok and "nothing to update" in bad.note


def test_old_todos_get_the_remind_on_column(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE todos (id INTEGER PRIMARY KEY, text TEXT NOT NULL, owner TEXT,
          status TEXT NOT NULL, due TEXT, position INTEGER, created_by TEXT NOT NULL,
          created_at TEXT NOT NULL, source_message_id INTEGER NOT NULL, closed_at TEXT,
          project_id INTEGER);
        INSERT INTO todos VALUES (1, 'Старе', NULL, 'open', NULL, NULL, 'oleh',
          '2026-09-10T10:00:00Z', 1, NULL, NULL);
        """
    )
    conn.close()
    db = Database(path)
    assert [(t.id, t.remind_on) for t in db.open_todos()] == [(1, None)]  # nobody is nudged
    assert db.due_nudges("2026-12-31") == []
    db.close()
    Database(path).close()


def test_the_llm_sees_the_nudge_day(db: Database, family: Family) -> None:
    tid = db.create_todo("Віза", owner="oleh", created_by="oleh", source_message_id=0)
    db.update_todo(tid, remind_on="2026-09-17")
    t = db.get_todo(tid)
    assert t and todo_line(t, family) == "[#1] Віза (Олег, нагадаю 17.09)"
    assert todo_line(t, family, with_id=False) == "Віза (Олег)"  # the digest does not


def _new(db: Database, text: str, owner: str | None, remind_on: str) -> Todo:
    tid = db.create_todo(text, owner=owner, created_by="oleh", source_message_id=0)
    db.update_todo(tid, remind_on=remind_on)
    t = db.get_todo(tid)
    assert t
    return t


async def test_one_nudge_per_member_per_day(db: Database, family: Family) -> None:
    family.add("Оля", None, member_id="olia")  # no Telegram: never nudged
    shared = _new(db, "Купити ялинку", None, "2026-09-16")
    _new(db, "Клініки", "anna", "2026-09-16")
    later = _new(db, "Віза", "oleh", "2026-09-17")
    old = _new(db, "Масаж", "oleh", "2026-09-15")
    assert [t.id for t in db.due_nudges("2026-09-16")] == [old.id, 2, shared.id]  # newest first

    # The newest one each: Oleh's «Масаж» (filed last), Anna's own «Клініки»; the shared
    # one waits, and so does Oleh's later one.
    picks = pick_nudges(db, family, "2026-09-16")
    assert {m: t.id for m, t in picks.items()} == {"oleh": old.id, "anna": 2}

    sent: list[tuple[str, int]] = []

    async def send(member: Member, t: Todo) -> int:
        if member.id == "anna":
            raise RuntimeError("blocked the bot")
        sent.append((member.id, t.id))
        return 100 + t.id

    now = datetime(2026, 9, 16, 12, 30, tzinfo=KYIV)
    delivered = await deliver_due_nudges(db, family, now, send)
    assert {m: t.id for m, t in delivered.items()} == {"oleh": old.id}
    assert sent == [("oleh", old.id)]
    assert db.get_todo(old.id).remind_on is None  # sent: silence unless «Завтра»
    assert db.get_todo(2).remind_on == "2026-09-16"  # Anna was not reached: tomorrow
    msgs = db.recent_messages()
    assert [(m.user_id, m.chat_with, m.raw_text, m.tg_message_id) for m in msgs] == [
        ("bot", "oleh", "🔔 Масаж", 100 + old.id)
    ]  # stored in his chat only: Anna got nothing

    # The next days for Oleh: the later one (newer than the shared one), then the shared
    # one, then nothing; Anna's own keeps waiting for her, one try a day.
    delivered = await deliver_due_nudges(db, family, now.replace(day=17), send)
    assert {m: t.id for m, t in delivered.items()} == {"oleh": later.id}
    delivered = await deliver_due_nudges(db, family, now.replace(day=18), send)
    assert {m: t.id for m, t in delivered.items()} == {"oleh": shared.id}
    assert db.get_todo(shared.id).remind_on is None
    assert await deliver_due_nudges(db, family, now.replace(day=19), send) == {}
    assert db.get_todo(2).remind_on == "2026-09-16"


def test_the_buttons(db: Database) -> None:
    import re

    t = _new(db, "Клініки", "anna", "2026-09-16")
    kb = nudge_keyboard(t.id)
    (row,) = kb.inline_keyboard
    assert [(b.text, b.callback_data) for b in row] == [
        ("✓ Зроблено", f"todo:done:{t.id}"),
        ("Завтра", f"todo:tomorrow:{t.id}"),
    ]
    assert all(re.match(NUDGE_PATTERN, b.callback_data or "") for b in row)
    assert not re.match(NUDGE_PATTERN, "todo:drop:1")

    today = date(2026, 9, 16)
    assert nudge_tap(db, f"todo:tomorrow:{t.id}", today) == (
        "Нагадаю завтра",
        "🔔 Клініки\nНагадаю завтра.",
    )
    assert db.get_todo(t.id).remind_on == "2026-09-17"
    assert nudge_tap(db, f"todo:done:{t.id}", today) == ("Зроблено", "✓ Клініки")
    done = db.get_todo(t.id)
    assert done and done.status == "done" and done.closed_at
    # A second tap on an old message: the buttons go, nothing reopens
    assert nudge_tap(db, f"todo:tomorrow:{t.id}", today) == ("Задача вже закрита.", None)
    assert nudge_tap(db, "todo:done:999", today) == ("Задача вже закрита.", None)
    assert db.get_todo(t.id).status == "done"


def test_the_web_shows_the_nudge_to_come(
    db: Database, family: Family, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from family_ea.web import build_web
    from tests.test_web import _auth, _settings, freeze_web_clock

    freeze_web_clock(monkeypatch, datetime(2026, 9, 16, 9, 0, tzinfo=KYIV))
    soon = _new(db, "Клініки", "anna", "2026-09-17")
    later = _new(db, "Віза", "oleh", "2026-09-25")
    missed = _new(db, "Масаж", "oleh", "2026-09-15")
    quiet = _new(db, "Тихо", "oleh", "2026-09-20")
    db.update_todo(quiet.id, remind_on=None)
    client = TestClient(build_web(_settings(), family, db))
    page = client.get("/", headers=_auth()).text
    row = lambda t: page[page.index(f'data-id="{t.id}"') :].split("</li>")[0]  # noqa: E731
    assert "· 🔔 завтра · Анна" in row(soon)
    assert "· 🔔 25.09 · Олег" in row(later)
    assert "· 🔔 сьогодні · Олег" in row(missed)
    assert "🔔" not in row(quiet)
