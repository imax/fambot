"""Apply LLM operations to the database: items, events, todos, projects, dreams, reminders,
today and remember boards, the notes page.

Invalid ops (unknown ids, closed items, bad dates, an event without a date, a reminder
without a time) are ignored and logged, never fatal; `failure_note` puts them under the
reply, so what the LLM claimed and what happened do not drift apart unnoticed. An empty
field means «not given»; a lone dash (`CLEAR`) clears an optional one: the LLM cannot send
null (see llm.py). Closing a todo goes through
`close_todo`, a dream through `close_dream`, cancelling an event through `cancel_event`, a
reminder through `cancel_reminder`; nothing else closes or cancels.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import db as db_module
from .db import Database
from .family import Family
from .llm import EventOp, ItemOp, LlmResult, ReminderOp, TodoOp

log = logging.getLogger(__name__)

CLEAR = "-"  # in an optional field: clear it («прибери примітку»)
# For the ⚠️ line under the reply: the kind in the accusative, the op as a verb.
KIND_UK = {
    "item": "річ",
    "event": "подію",
    "todo": "задачу",
    "project": "проєкт",
    "dream": "мрію",
    "reminder": "нагадування",
    "today": "список на сьогодні",
    "remember": "список «Не забути»",
    "notes": "нотатки",
}
OP_UK = {
    "create": "створити",
    "update": "змінити",
    "remove": "прибрати",
    "cancel": "скасувати",
    "close": "закрити",
    "set": "записати",
}


@dataclass(frozen=True)
class Applied:
    kind: str  # item | event | todo | project | dream | reminder | today | remember | notes
    op: str
    id: int | None
    ok: bool
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_datetime(value: str | None, tz: ZoneInfo) -> str | None:
    """LLM datetime (any offset, or naive = family tz) -> ISO UTC 'Z'. None if unparsable."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def normalize_member(member_id: str, family: Family) -> str | None:
    if not member_id:
        return None
    return member_id if any(p.id == member_id for p in family.members) else None


def _text(value: str) -> str | None:
    """A required text field: trimmed, or None when nothing (or a lone dash) was given."""
    value = value.strip()
    return value if value and value != CLEAR else None


def _given(value: str) -> tuple[bool, str | None]:
    """An optional field: (given, value); a lone dash is given and means None, «clear it»."""
    value = value.strip()
    if not value:
        return False, None
    return True, (None if value == CLEAR else value)


def failures(applied: list[Applied]) -> list[Applied]:
    """Ops that meant a change and made none. Not «unchanged»: the same text again is fine."""
    return [a for a in applied if not a.ok and a.note != "unchanged"]


def failure_note(applied: list[Applied]) -> str:
    """The line under the reply when an op did not go through: the reply was written before
    the ops ran, so its claim («прибрав») must not stand alone. '' when all went through."""
    parts = []
    for a in failures(applied):
        what = f"{OP_UK.get(a.op.partition(':')[0], a.op)} {KIND_UK.get(a.kind, a.kind)}"
        parts.append(f"{what} #{a.id}" if a.id else what)
    return f"⚠️ Не вийшло: {', '.join(parts)}." if parts else ""


def _item_fields(i: ItemOp) -> tuple[dict, list[str]]:
    """Fields given on the op, trimmed; a dash clears one (not the name). A new place
    without a spot clears the old spot: «переклав у квартиру» rarely means the same shelf."""
    fields: dict[str, str | None] = {}
    notes: list[str] = []
    if (name := _text(i.name)) is not None:
        fields["name"] = name
    for field in ("owner", "place", "spot", "note"):
        given, value = _given(getattr(i, field))
        if given:
            fields[field] = value
    if "place" in fields and "spot" not in fields:
        fields["spot"] = None
    return fields, notes


class OpError(Exception):
    """An op that must not go through as given: the message meant something the code
    cannot resolve (a project that does not exist), and changing anything would guess."""


def normalize_project(value: str, db: Database) -> int | None:
    """An open project's id from what the LLM sent: its id, or its name (the context lists
    both). None when there is no such open project."""
    value = value.strip()
    if value.isdigit():
        p = db.get_project(int(value))
        return p.id if p and p.is_open else None
    p = db.open_project_by_name(value) if value else None
    return p.id if p else None


def _todo_fields(t: TodoOp, family: Family, db: Database) -> tuple[dict, list[str]]:
    """Validated fields present on the op, plus notes about anything dropped. An unknown
    project raises OpError: the todo must not land in the wrong group or silently in none.

    A nudge (`remind_on`) is for a loose end, a todo with neither a day nor a project; a
    day or a project given here takes the pending nudge with it, unless the op sets one."""
    fields: dict[str, str | int | None] = {}
    notes: list[str] = []
    given, project = _given(t.project)
    if given:
        if project is not None and (pid := normalize_project(project, db)) is None:
            raise OpError(f"unknown project {t.project!r}")
        fields["project_id"] = pid if project is not None else None
    if (text := _text(t.text)) is not None:
        fields["text"] = text
    given, owner = _given(t.owner)
    if given:
        if owner is not None and (owner := normalize_member(owner, family)) is None:
            notes.append(f"unknown owner {t.owner!r} -> null")
        fields["owner"] = owner
    given, due = _given(t.due)
    if given:
        if due is not None and (due := normalize_date(due)) is None:
            notes.append(f"bad due {t.due!r} dropped")
        else:
            fields["due"] = due  # None: the deadline goes («без дати»)
    given, remind_on = _given(t.remind_on)
    if given:
        if remind_on is not None and (remind_on := normalize_date(remind_on)) is None:
            notes.append(f"bad remind_on {t.remind_on!r} dropped")
        else:
            fields["remind_on"] = remind_on  # None: no nudge («не нагадуй»)
    elif fields.get("due") or fields.get("project_id"):
        fields["remind_on"] = None
    return fields, notes


def nudge_day(tz: ZoneInfo) -> str:
    """The day a new loose end gets its nudge: tomorrow, in the family's timezone. The
    clock is `db.utc_now_iso`, the one the tests freeze."""
    now = datetime.fromisoformat(db_module.utc_now_iso().replace("Z", "+00:00"))
    return (now.astimezone(tz).date() + timedelta(days=1)).isoformat()


def _event_fields(e: EventOp, family: Family, tz: ZoneInfo) -> tuple[dict, list[str]]:
    """Validated fields present on the op. A timed event has no all-day dates, and vice versa."""
    fields: dict[str, str | None] = {}
    notes: list[str] = []
    if (text := _text(e.text)) is not None:
        fields["text"] = text
    given, who = _given(e.who)
    if given:
        if who is not None and (who := normalize_member(who, family)) is None:
            notes.append(f"unknown who {e.who!r} -> null")
        fields["who"] = who
    for name in ("starts_at", "until"):
        given, raw = _given(getattr(e, name))
        if not given:
            continue
        if raw is None:  # a dash: only the end can go (one hour again); the start cannot
            if name == "until":
                fields["until"] = None
            else:
                notes.append("starts_at cannot be cleared")
            continue
        value = normalize_datetime(raw, tz)
        if value is None:
            notes.append(f"bad {name} {raw!r} dropped")
        else:
            fields[name] = value
    for name in ("date_from", "date_to"):
        given, raw = _given(getattr(e, name))
        if not given:
            continue
        if raw is None:
            notes.append(f"{name} cannot be cleared")
            continue
        value = normalize_date(raw)
        if value is None:
            notes.append(f"bad {name} {raw!r} dropped")
        else:
            fields[name] = value
    if "starts_at" in fields:
        fields["date_from"] = fields["date_to"] = None
    elif "date_from" in fields or "date_to" in fields:
        fields["starts_at"] = fields["until"] = None
        fields.setdefault("date_from", fields.get("date_to"))
        fields.setdefault("date_to", fields.get("date_from"))
        if (fields["date_from"] or "") > (fields["date_to"] or ""):
            notes.append(f"date_to {fields['date_to']} before date_from dropped")
            fields["date_to"] = fields["date_from"]
    return fields, notes


def _reminder_fields(r: ReminderOp, family: Family, tz: ZoneInfo) -> tuple[dict, list[str]]:
    fields: dict[str, str | None] = {}
    notes: list[str] = []
    if (text := _text(r.text)) is not None:
        fields["text"] = text
    given, who = _given(r.who)
    if given:
        if who is not None and (who := normalize_member(who, family)) is None:
            notes.append(f"unknown who {r.who!r} -> null")
        fields["who"] = who  # None: to everyone
    given, at = _given(r.at)
    if given:
        if at is None:
            notes.append("at cannot be cleared")
        elif (at := normalize_datetime(at, tz)) is None:
            notes.append(f"bad at {r.at!r} dropped")
        else:
            fields["at"] = at
    return fields, notes


def _check_until(
    fields: dict, starts_at: str | None, existing_until: str | None, notes: list[str]
) -> None:
    """`until` must come after the start; otherwise it is dropped (one-hour default applies).

    Covers a new `until` on the op and an existing one the op's new start moves past.
    """
    until = fields.get("until", existing_until)
    if until and starts_at and until <= starts_at:
        notes.append(f"until {until} not after starts_at dropped")
        fields["until"] = None


def apply_ops(
    db: Database,
    result: LlmResult,
    *,
    author_id: str,
    message_id: int,
    family: Family,
    tz: ZoneInfo,
    with_photo: bool = False,
) -> list[Applied]:
    applied: list[Applied] = []

    for i in result.items:
        fields, notes = _item_fields(i)
        if i.op == "create":
            if "name" not in fields:
                applied.append(Applied("item", "create", None, False, "empty name"))
                continue
            iid = db.create_item(
                fields["name"] or "",
                owner=fields.get("owner"),
                place=fields.get("place"),
                spot=fields.get("spot"),
                note=fields.get("note"),
                created_by=author_id,
                source_message_id=message_id,
            )
            applied.append(Applied("item", "create", iid, True, "; ".join(notes)))
        elif i.op == "update":
            kind = None
            if i.id:
                kind = db.update_item(i.id, fields, who=author_id, source_message_id=message_id)
            if kind:
                applied.append(Applied("item", "update", i.id, True, "; ".join([kind, *notes])))
                continue
            current = db.get_item(i.id) if i.id else None
            if current and not current.removed_at and not fields and not with_photo:
                # No fields and no photo: the LLM meant something it could not say (a
                # field it wanted to clear without the dash); flagged, not silently ok.
                applied.append(Applied("item", "update", i.id, False, "nothing to update"))
            elif current and not current.removed_at:
                # Nothing differs, still a hit: a photo sent with the message lands under the
                # item («ось ще фото коробки»), through the applied log.
                note = "; ".join(["unchanged", *notes])
                applied.append(Applied("item", "update", i.id, True, note))
            else:
                applied.append(Applied("item", "update", i.id or None, False, "not found or gone"))
        elif i.op == "remove":
            ok = bool(i.id) and db.remove_item(i.id, who=author_id, source_message_id=message_id)
            applied.append(
                Applied(
                    "item", "remove", i.id or None, ok, "" if ok else "not found or already gone"
                )
            )

    for e in result.events:
        fields, notes = _event_fields(e, family, tz)
        if e.op == "create":
            if "text" not in fields:
                applied.append(Applied("event", "create", None, False, "empty text"))
                continue
            if not (fields.get("starts_at") or fields.get("date_from")):
                applied.append(Applied("event", "create", None, False, "event without a date"))
                continue
            _check_until(fields, fields.get("starts_at"), None, notes)
            eid = db.create_event(
                fields["text"] or "",
                who=fields.get("who"),
                created_by=author_id,
                source_message_id=message_id,
                starts_at=fields.get("starts_at"),
                until=fields.get("until"),
                date_from=fields.get("date_from"),
                date_to=fields.get("date_to"),
            )
            applied.append(Applied("event", "create", eid, True, "; ".join(notes)))
        elif e.op == "update":
            existing = db.get_event(e.id) if e.id else None
            if existing is None or not existing.is_planned:
                applied.append(
                    Applied("event", "update", e.id or None, False, "not found or not planned")
                )
                continue
            if not fields:
                note = "; ".join([*notes, "nothing to update"])
                applied.append(Applied("event", "update", e.id or None, False, note))
                continue
            _check_until(fields, fields.get("starts_at", existing.starts_at), existing.until, notes)
            ok = db.update_event(existing.id, **fields)
            applied.append(Applied("event", "update", e.id or None, ok, "; ".join(notes)))
        elif e.op == "cancel":
            ok = bool(e.id) and db.cancel_event(e.id)
            applied.append(
                Applied(
                    "event", "cancel", e.id or None, ok, "" if ok else "not found or not planned"
                )
            )

    # Projects before todos: «новий проєкт Авто, в нього: XC90» names the project in the
    # todo op of the same message, by name.
    def move_todos(ids: list[int], pid: int) -> None:
        """`todos` of a project op: each one an update of that todo, said so in the log."""
        for tid in ids:
            ok = db.update_todo(tid, project_id=pid)
            note = f"-> project #{pid}" if ok else "not found or not open"
            applied.append(Applied("todo", "update", tid, ok, note))

    for pr in result.projects:
        name = _text(pr.name)
        if pr.op == "create":
            if name is None:
                applied.append(Applied("project", "create", None, False, "empty name"))
                continue
            if (dup := db.open_project_by_name(name)) is not None:
                applied.append(
                    Applied("project", "create", dup.id, False, f"already exists as #{dup.id}")
                )
                continue
            pid = db.create_project(name, created_by=author_id)
            applied.append(Applied("project", "create", pid, True))
            move_todos(pr.todos, pid)
        elif pr.op == "update":
            current = db.get_project(pr.id) if pr.id else None
            if current is None or not current.is_open:
                applied.append(
                    Applied("project", "update", pr.id or None, False, "not found or not open")
                )
                continue
            if name is None and not pr.todos:
                applied.append(Applied("project", "update", pr.id, False, "nothing to update"))
                continue
            if name is not None:
                db.rename_project(pr.id, name)
            applied.append(Applied("project", "update", pr.id, True))
            move_todos(pr.todos, pr.id)
        elif pr.op == "close":
            ok = bool(pr.id) and db.close_project(pr.id)
            note = "" if ok else "not found or not open"
            applied.append(Applied("project", "close", pr.id or None, ok, note))

    for t in result.todos:
        try:
            fields, notes = _todo_fields(t, family, db)
        except OpError as exc:
            applied.append(Applied("todo", t.op, t.id or None, False, str(exc)))
            continue
        note = "; ".join(notes)
        if t.op == "create":
            if "text" not in fields:
                applied.append(Applied("todo", "create", None, False, "empty text"))
                continue
            if "remind_on" not in fields:
                # A loose end (no day, no project): the bot brings it up tomorrow at noon,
                # once (see bot.deliver_due_nudges).
                fields["remind_on"] = nudge_day(tz)
            tid = db.create_todo(
                str(fields["text"] or ""),
                owner=fields.get("owner"),  # type: ignore[arg-type]
                created_by=author_id,
                source_message_id=message_id,
                due=fields.get("due"),  # type: ignore[arg-type]
                project_id=fields.get("project_id"),  # type: ignore[arg-type]
                remind_on=fields.get("remind_on"),  # type: ignore[arg-type]
            )
            applied.append(Applied("todo", "create", tid, True, note))
        elif t.op == "update":
            if not t.id or not fields:
                note = "; ".join([*notes, "nothing to update"])
                applied.append(Applied("todo", "update", t.id or None, False, note))
                continue
            ok = db.update_todo(t.id, **fields)
            applied.append(
                Applied("todo", "update", t.id or None, ok, note if ok else "not found or not open")
            )
        elif t.op == "close":
            status = t.status or "done"
            ok = bool(t.id) and db.close_todo(t.id, status)
            note = "" if ok else "not found or not open"
            applied.append(Applied("todo", f"close:{status}", t.id or None, ok, note))

    for d in result.dreams:
        text = _text(d.text)
        if d.op == "create":
            if text is None:
                applied.append(Applied("dream", "create", None, False, "empty text"))
                continue
            did = db.create_dream(text, created_by=author_id, source_message_id=message_id)
            applied.append(Applied("dream", "create", did, True))
        elif d.op == "update":
            if not d.id or text is None:
                applied.append(Applied("dream", "update", d.id or None, False, "nothing to update"))
                continue
            ok = db.update_dream(d.id, text)
            note = "" if ok else "not found or not open"
            applied.append(Applied("dream", "update", d.id, ok, note))
        elif d.op == "close":
            status = d.status or "fulfilled"
            ok = bool(d.id) and db.close_dream(d.id, status)
            note = "" if ok else "not found or not open"
            applied.append(Applied("dream", f"close:{status}", d.id or None, ok, note))

    for r in result.reminders:
        fields, notes = _reminder_fields(r, family, tz)
        note = "; ".join(notes)
        if r.op == "create":
            if "text" not in fields:
                applied.append(Applied("reminder", "create", None, False, "empty text"))
                continue
            if "at" not in fields:
                applied.append(Applied("reminder", "create", None, False, "no time"))
                continue
            rid = db.create_reminder(
                fields["text"] or "",
                who=fields.get("who"),
                at=fields["at"] or "",
                created_by=author_id,
                source_message_id=message_id,
            )
            applied.append(Applied("reminder", "create", rid, True, note))
        elif r.op == "update":
            if not r.id or not fields:
                note = "; ".join([*notes, "nothing to update"])
                applied.append(Applied("reminder", "update", r.id or None, False, note))
                continue
            ok = db.update_reminder(r.id, **fields)
            applied.append(
                Applied(
                    "reminder",
                    "update",
                    r.id or None,
                    ok,
                    note if ok else "not found or not pending",
                )
            )
        elif r.op == "cancel":
            ok = bool(r.id) and db.cancel_reminder(r.id)
            applied.append(
                Applied(
                    "reminder", "cancel", r.id or None, ok, "" if ok else "not found or not pending"
                )
            )

    # The two boards of a member: the same shape, each replaced whole.
    boards = (
        ("today", result.today, db.current_today_lists, db.save_today_list),
        ("remember", result.remember, db.current_remember_lists, db.save_remember_list),
    )
    for kind, board_ops, current_boards, save_board in boards:
        for t in board_ops:
            member = author_id if not t.member else normalize_member(t.member, family)
            if member is None:
                applied.append(Applied(kind, "set", None, False, f"unknown member {t.member!r}"))
                continue
            text = t.text.strip()
            current = current_boards().get(member)
            if (current.text if current else "") == text:
                applied.append(Applied(kind, "set", None, False, "unchanged"))
                continue
            bid = save_board(member, text, author_id)
            note = "" if member == author_id else f"for {member}"
            applied.append(Applied(kind, "set", bid, True, note))

    for n in result.notes:
        text = n.text.replace("\r\n", "\n").strip()
        current = db.current_notes()
        if (current.text if current else "") == text:
            applied.append(Applied("notes", "set", None, False, "unchanged"))
            continue
        nid = db.save_notes(text, author_id)
        applied.append(Applied("notes", "set", nid, True, "cleared" if not text else ""))

    for a in applied:
        if not a.ok or a.note:
            log.info("op %s %s id=%s ok=%s %s", a.kind, a.op, a.id, a.ok, a.note)
    return applied
