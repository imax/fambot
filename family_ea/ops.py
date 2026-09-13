"""Apply LLM operations to the database: items, events, todos, dreams, reminders, today
boards, the notes page.

Invalid ops (unknown ids, closed items, bad dates, an event without a date, a reminder
without a time) are ignored and logged, never fatal. Closing a todo goes through
`close_todo`, a dream through `close_dream`, cancelling an event through `cancel_event`, a
reminder through `cancel_reminder`; nothing else closes or cancels.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .db import Database
from .family import Family
from .llm import EventOp, ItemOp, LlmResult, ReminderOp, TodoOp

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Applied:
    kind: str  # 'item' | 'event' | 'todo' | 'dream' | 'reminder' | 'today' | 'notes'
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
    """Trimmed, or None when nothing was given."""
    return value.strip() or None


def _item_fields(i: ItemOp) -> tuple[dict, list[str]]:
    """Fields given on the op, trimmed. A new place without a spot clears the old spot:
    «переклав у квартиру» rarely means the same shelf."""
    fields: dict[str, str | None] = {}
    notes: list[str] = []
    for name in ("name", "owner", "place", "spot", "note"):
        if (value := _text(getattr(i, name))) is not None:
            fields[name] = value
    if "place" in fields and "spot" not in fields:
        fields["spot"] = None
    return fields, notes


def _todo_fields(t: TodoOp, family: Family) -> tuple[dict, list[str]]:
    """Validated fields present on the op, plus notes about anything dropped."""
    fields: dict[str, str | None] = {}
    notes: list[str] = []
    if (text := _text(t.text)) is not None:
        fields["text"] = text
    if t.owner:
        owner = normalize_member(t.owner, family)
        if owner is None:
            notes.append(f"unknown owner {t.owner!r} -> null")
        fields["owner"] = owner
    if t.due:
        due = normalize_date(t.due)
        if due is None:
            notes.append(f"bad due {t.due!r} dropped")
        else:
            fields["due"] = due
    return fields, notes


def _event_fields(e: EventOp, family: Family, tz: ZoneInfo) -> tuple[dict, list[str]]:
    """Validated fields present on the op. A timed event has no all-day dates, and vice versa."""
    fields: dict[str, str | None] = {}
    notes: list[str] = []
    if (text := _text(e.text)) is not None:
        fields["text"] = text
    if e.who:
        who = normalize_member(e.who, family)
        if who is None:
            notes.append(f"unknown who {e.who!r} -> null")
        fields["who"] = who
    for name in ("starts_at", "until"):
        raw = getattr(e, name)
        if raw:
            value = normalize_datetime(raw, tz)
            if value is None:
                notes.append(f"bad {name} {raw!r} dropped")
            else:
                fields[name] = value
    for name in ("date_from", "date_to"):
        raw = getattr(e, name)
        if raw:
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
    if r.who:
        who = normalize_member(r.who, family)
        if who is None:
            notes.append(f"unknown who {r.who!r} -> null")
        fields["who"] = who
    if r.at:
        at = normalize_datetime(r.at, tz)
        if at is None:
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
            if current and not current.removed_at:
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
                applied.append(Applied("event", "update", e.id or None, False, "nothing to update"))
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

    for t in result.todos:
        fields, notes = _todo_fields(t, family)
        note = "; ".join(notes)
        if t.op == "create":
            if "text" not in fields:
                applied.append(Applied("todo", "create", None, False, "empty text"))
                continue
            tid = db.create_todo(
                fields["text"] or "",
                owner=fields.get("owner"),
                created_by=author_id,
                source_message_id=message_id,
                due=fields.get("due"),
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
                applied.append(
                    Applied("reminder", "update", r.id or None, False, "nothing to update")
                )
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

    for t in result.today:
        member = author_id if not t.member else normalize_member(t.member, family)
        if member is None:
            applied.append(Applied("today", "set", None, False, f"unknown member {t.member!r}"))
            continue
        text = t.text.strip()
        current = db.current_today_lists().get(member)
        if (current.text if current else "") == text:
            applied.append(Applied("today", "set", None, False, "unchanged"))
            continue
        tid = db.save_today_list(member, text, author_id)
        note = "" if member == author_id else f"for {member}"
        applied.append(Applied("today", "set", tid, True, note))

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
