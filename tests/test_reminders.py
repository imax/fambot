"""Reminders: a message to someone at a given moment. Own table, own ops, a per-minute job."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from family_ea.bot import deliver_due_reminders, reminder_recipients
from family_ea.context import build_context, reminder_line
from family_ea.db import Database, Member
from family_ea.family import Family
from family_ea.llm import LlmResult
from family_ea.main import llm_result_lines
from family_ea.ops import apply_ops
from family_ea.web import build_web
from tests.conftest import KYIV
from tests.test_web import _auth, _settings, freeze_web_clock


def _apply(db: Database, family: Family, ops: list[dict], author: str = "oleh") -> list:
    mid = db.insert_message(author, author, "...")
    result = LlmResult.model_validate({"reply": "", "reminders": ops})
    return apply_ops(db, result, author_id=author, message_id=mid, family=family, tz=KYIV)


def test_reminder_ops(db: Database, family: Family) -> None:
    applied = _apply(
        db,
        family,
        [
            {
                "op": "create",
                "text": "Зустріч з пані Марією о 16:00",
                "at": "2026-09-11T15:00+03:00",
            },
            {
                "op": "create",
                "text": "Купити квіти",
                "who": "anna",
                "at": "2026-09-12T09:00:00+03:00",
            },
            {"op": "create", "text": "Хтось чужий", "who": "olia", "at": "2026-09-12T09:00:00Z"},
            {"op": "create", "text": "Без часу", "at": "завтра"},
            {"op": "create", "text": "  ", "at": "2026-09-12T09:00:00Z"},
        ],
    )
    assert [(a.ok, a.id) for a in applied] == [
        (True, 1),
        (True, 2),
        (True, 3),
        (False, None),
        (False, None),
    ]
    assert applied[2].note == "unknown who 'olia' -> null"
    assert applied[3].note == "no time"
    first = db.get_reminder(1)
    assert first and first.who is None and first.at == "2026-09-11T12:00:00Z" and first.is_pending
    second = db.get_reminder(2)
    assert second and second.who == "anna" and second.at == "2026-09-12T06:00:00Z"
    third = db.get_reminder(3)
    assert third and third.who is None

    # «перенесли на 17» moves the reminder; «скасували» cancels it; nothing touches a cancelled one
    applied = _apply(
        db,
        family,
        [
            {"op": "update", "id": 1, "at": "2026-09-11T16:00:00+03:00", "text": "Зустріч о 17:00"},
            {"op": "cancel", "id": 2},
            {"op": "update", "id": 2, "at": "2026-09-13T09:00:00Z"},
            {"op": "cancel", "id": 2},
            {"op": "update", "id": 3},
            {"op": "cancel", "id": 99},
        ],
    )
    assert [(a.op, a.ok) for a in applied] == [
        ("update", True),
        ("cancel", True),
        ("update", False),
        ("cancel", False),
        ("update", False),
        ("cancel", False),
    ]
    first = db.get_reminder(1)
    assert first and first.at == "2026-09-11T13:00:00Z" and first.text == "Зустріч о 17:00"
    second = db.get_reminder(2)
    assert second and second.status == "cancelled" and second.at == "2026-09-12T06:00:00Z"
    assert [r.id for r in db.pending_reminders()] == [1, 3]
    assert [r.id for r in db.due_reminders("2026-09-11T13:00:00Z")] == [1]
    assert db.due_reminders("2026-09-11T12:59:59Z") == []


def test_reminder_in_context_and_log(
    db: Database, family: Family, oleh: Member, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-09-10T12:00:00Z")
    mid = db.insert_message("oleh", "oleh", "Нагадай нам за годину")
    db.create_reminder(
        "Зустріч з пані Марією о 16:00",
        who=None,
        at="2026-09-11T12:00:00Z",
        created_by="oleh",
        source_message_id=mid,
    )
    db.create_reminder(
        "Купити квіти",
        who="anna",
        at="2026-09-12T06:00:00Z",
        created_by="oleh",
        source_message_id=mid,
    )
    db.finish_reminder(2, "sent")
    db.create_reminder(
        "Забрати Олю",
        who="anna",
        at="2026-09-12T13:00:00Z",
        created_by="oleh",
        source_message_id=mid,
    )
    r = db.get_reminder(1)
    assert r and reminder_line(r, family, KYIV) == (
        "[нагадування #1] 11.09 15:00 Зустріч з пані Марією о 16:00 (усім)"
    )
    ctx = build_context(db, family, datetime(2026, 9, 10, 15, 0, tzinfo=KYIV), oleh, "?")
    assert (
        "## Нагадування (заплановані, ще не надіслані)\n"
        "- [нагадування #1] 11.09 15:00 Зустріч з пані Марією о 16:00 (усім)\n"
        "- [нагадування #3] 12.09 16:00 Забрати Олю (Анна)"
    ) in ctx
    assert "Купити квіти" not in ctx  # sent: no longer something to plan around

    raw = (
        '{"output": {"reply": "Нагадаю.", "reminders": [{"op": "create", "text": "Квіти",'
        ' "at": "2026-09-12T09:00:00+03:00"}]},'
        ' "applied": [{"kind": "reminder", "op": "create", "id": 4, "ok": true}]}'
    )
    assert llm_result_lines(raw) == [
        "reminder create: text='Квіти', at='2026-09-12T09:00:00+03:00'",
        "[ok] reminder create #4",
    ]


async def test_deliver_due_reminders(db: Database, family: Family) -> None:
    family.add("Оля", None, member_id="olia")  # in the family, but no Telegram: unreachable
    mid = db.insert_message("oleh", "oleh", "...")

    def create(text: str, who: str | None, at: str) -> int:
        return db.create_reminder(text, who=who, at=at, created_by="oleh", source_message_id=mid)

    create("Зустріч о 16:00", None, "2026-09-11T12:00:00Z")  # due, for everyone
    create("Квіти", "anna", "2026-09-11T11:30:00Z")  # due, for Anna only
    create("Ще не час", None, "2026-09-11T12:01:00Z")  # not yet
    create("Проспали", "oleh", "2026-09-11T08:59:00Z")  # 3h01m late: missed
    create("Нікому", "olia", "2026-09-11T12:00:00Z")  # no Telegram id: missed
    sent_to: list[tuple[int, str]] = []

    async def send(member: Member, text: str) -> int:
        assert member.telegram_id is not None
        if member.id == "oleh" and text == "⏰ Зустріч о 16:00":
            raise RuntimeError("blocked the bot")  # one recipient failing does not lose the rest
        sent_to.append((member.telegram_id, text))
        return 100 + len(sent_to)

    now = datetime(2026, 9, 11, 12, 0, 5, tzinfo=UTC)
    sent = await deliver_due_reminders(db, family, now, send)
    assert [r.id for r in sent] == [2, 1]  # by time
    assert sent_to == [(2, "⏰ Квіти"), (2, "⏰ Зустріч о 16:00")]
    statuses = {r.id: r.status for r in (db.get_reminder(i) for i in range(1, 6)) if r}
    assert statuses == {1: "sent", 2: "sent", 3: "pending", 4: "missed", 5: "missed"}
    assert db.get_reminder(1).sent_at and db.get_reminder(4).sent_at is None  # type: ignore[union-attr]
    # each delivery is a bot message in that person's chat, so a reply to it has context
    bot_messages = [(m.chat_with, m.raw_text, m.tg_message_id) for m in db.recent_messages(10)]
    assert bot_messages[1:] == [("anna", "⏰ Квіти", 101), ("anna", "⏰ Зустріч о 16:00", 102)]
    # a second tick sends nothing new until the next one comes due
    assert await deliver_due_reminders(db, family, now, send) == []
    later = datetime(2026, 9, 11, 12, 1, 30, tzinfo=UTC)
    assert [r.id for r in await deliver_due_reminders(db, family, later, send)] == [3]
    assert sent_to[2:] == [(1, "⏰ Ще не час"), (2, "⏰ Ще не час")]


def test_reminder_recipients(db: Database, family: Family) -> None:
    mid = db.insert_message("oleh", "oleh", "...")
    rid = db.create_reminder(
        "x", who=None, at="2026-09-11T12:00:00Z", created_by="oleh", source_message_id=mid
    )
    r = db.get_reminder(rid)
    assert r and [m.id for m in reminder_recipients(r, family)] == ["oleh", "anna"]
    db.update_reminder(rid, who="anna")
    r = db.get_reminder(rid)
    assert r and [m.id for m in reminder_recipients(r, family)] == ["anna"]


def test_web_lists_pending_reminders(
    db: Database, family: Family, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze_web_clock(monkeypatch, datetime(2026, 9, 10, 15, 0, tzinfo=KYIV))
    mid = db.insert_message("oleh", "oleh", "...")
    db.create_reminder(
        "Зустріч з пані Марією о 16:00",
        who=None,
        at="2026-09-11T12:00:00Z",
        created_by="oleh",
        source_message_id=mid,
    )
    db.create_reminder(
        "Квіти", who="anna", at="2026-09-12T06:00:00Z", created_by="oleh", source_message_id=mid
    )
    db.finish_reminder(2, "sent")
    client = TestClient(build_web(_settings(), family, db))
    page = client.get("/", headers=_auth()).text
    # Not in the calendar: its own block under it, «⏰» where a todo has «☐».
    assert page.index("<h2>Нагадування</h2>") < page.index("⏰</span>Зустріч з пані Марією о 16:00")
    assert "· завтра 15:00 · усім" in page and "15:00</span>" not in page
    assert "Квіти" not in page


def test_repeat_ops(db: Database, family: Family) -> None:
    applied = _apply(
        db,
        family,
        [
            {"op": "create", "text": "Планка", "at": "2026-09-17T19:30+03:00", "repeat": "daily"},
            {"op": "create", "text": "Басейн", "at": "2026-09-22T17:00+03:00", "repeat": "hourly"},
        ],
    )
    assert [a.ok for a in applied] == [True, True]
    assert "bad repeat" in applied[1].note  # an unknown rhythm: filed as a one-off, and said so
    assert [r.repeat for r in db.pending_reminders()] == ["daily", None]
    assert reminder_line(db.pending_reminders()[0], family, KYIV).endswith("Планка (усім, щодня)")
    _apply(db, family, [{"op": "update", "id": 2, "repeat": "weekly"}])
    _apply(db, family, [{"op": "update", "id": 1, "repeat": "-"}])  # «більше не повторюй»
    assert [r.repeat for r in db.pending_reminders()] == [None, "weekly"]


async def test_repeating_reminder_files_the_next(db: Database, family: Family) -> None:
    mid = db.insert_message("oleh", "oleh", "...")

    def create(text: str, at: str, repeat: str) -> int:
        return db.create_reminder(
            text, who="oleh", at=at, created_by="oleh", source_message_id=mid, repeat=repeat
        )

    async def send(member: Member, text: str) -> int:
        return 1

    create("Планка", "2026-10-24T16:30:00Z", "daily")  # 19:30 in Kyiv, the clocks change on 25.10
    create("Басейн", "2026-10-24T16:30:00Z", "weekly")
    create("Проспали", "2026-10-20T06:00:00Z", "daily")  # days of downtime: missed
    now = datetime(2026, 10, 24, 16, 30, 20, tzinfo=UTC)
    assert len(await deliver_due_reminders(db, family, now, send, KYIV)) == 2
    pending = {r.text: r for r in db.pending_reminders()}
    # the same wall-clock time after the change to winter time: 19:30 is now 17:30Z
    assert pending["Планка"].at == "2026-10-25T17:30:00Z"
    assert pending["Басейн"].at == "2026-10-31T17:30:00Z"
    # a missed one goes on too, from the first time still ahead, not one a tick
    assert pending["Проспали"].at == "2026-10-25T07:00:00Z"
    assert {r.repeat for r in pending.values()} == {"daily", "weekly"}
    assert all(r.who == "oleh" and r.source_message_id == mid for r in pending.values())

    # cancelling the pending one ends the chain
    assert db.cancel_reminder(pending["Планка"].id)
    week_later = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    await deliver_due_reminders(db, family, week_later, send, KYIV)
    assert "Планка" not in {r.text for r in db.pending_reminders()}
