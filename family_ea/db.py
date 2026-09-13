"""SQLite storage: members, messages, items, events, todos, dreams, reminders, facts,
notes, today lists.

One connection, one process, one writer. Original messages are never mutated; items are
removed (gone) and every change to one writes an item_history row; todos and dreams are
closed, events are cancelled and reminders are sent, missed or cancelled, never removed (a
past event simply passes); facts (the human-maintained standing context) and the notes (the one
reference page the LLM maintains) keep every version;
members (who talks to the bot) are edited by the admin on the web.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  user_id TEXT NOT NULL,            -- author: family member id or 'bot'
  chat_with TEXT NOT NULL,          -- family member whose Telegram chat this is
  tg_message_id INTEGER,
  created_at TEXT NOT NULL,         -- ISO UTC
  raw_text TEXT NOT NULL,           -- for voice: the transcript
  is_voice INTEGER NOT NULL DEFAULT 0,
  photo_file_id TEXT,               -- Telegram file id of the photo sent with the message
  llm_result TEXT                   -- JSON: what the LLM returned and what was applied
);

CREATE TABLE IF NOT EXISTS todos (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,
  owner TEXT,                       -- family member id; NULL = both / unclear
  status TEXT NOT NULL,             -- 'open' | 'done' | 'dropped'
  due TEXT,                         -- ISO date: the deadline day; NULL = none (a time of day
                                    --   makes it an event, not a todo)
  position INTEGER,                 -- hand-set order of the undated ones (web); NULL = after them
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  source_message_id INTEGER NOT NULL,
  closed_at TEXT
);

CREATE TABLE IF NOT EXISTS dreams (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,               -- the dream as the person put it: «Поїхати в Японію з Олею»
  status TEXT NOT NULL,             -- 'open' | 'fulfilled' | 'dropped'
  created_by TEXT NOT NULL,         -- who dreamt it up; the list itself is the family's
  created_at TEXT NOT NULL,
  source_message_id INTEGER NOT NULL,
  closed_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,
  who TEXT,                         -- family member id; NULL = the whole family
  status TEXT NOT NULL,             -- 'planned' | 'cancelled'; a past event just passes
  starts_at TEXT,                   -- ISO UTC datetime: a timed event
  until TEXT,                       -- ISO UTC datetime: its end; NULL = one hour
  date_from TEXT,                   -- ISO date: an all-day event (one or more days)
  date_to TEXT,                     -- ISO date, inclusive; NULL = date_from
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  source_message_id INTEGER NOT NULL,
  cancelled_at TEXT
);

CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,               -- what to send, self-contained
  who TEXT,                         -- family member id; NULL = everyone
  at TEXT NOT NULL,                 -- ISO UTC datetime: when to send
  status TEXT NOT NULL,             -- 'pending' | 'sent' | 'missed' | 'cancelled'
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  source_message_id INTEGER NOT NULL,
  sent_at TEXT
);

CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,               -- as people call it; not unique: two boxes of merch, two rows
  owner TEXT,                       -- whose, a name as written (a child is no member); NULL: shared
  place TEXT,                       -- coarse location as written («офіс», «дім»); NULL: unknown
  spot TEXT,                        -- where exactly, free text («сейф», «білий комод на 2 поверсі»)
  note TEXT,                        -- what else matters: the variant, what is inside, its state
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  source_message_id INTEGER NOT NULL,
  removed_at TEXT                   -- gone: thrown away, given away, lost
);

CREATE TABLE IF NOT EXISTS item_history (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL,
  kind TEXT NOT NULL,               -- 'created' | 'moved' | 'corrected' | 'gone'
  place TEXT,                       -- the location after the change
  spot TEXT,
  detail TEXT,                      -- what else changed, for people («власник: Оля»)
  who TEXT NOT NULL,                -- the family member who said it
  at TEXT NOT NULL,
  source_message_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL,      -- the message it came with; the only link to anything
  sha256 TEXT NOT NULL,             -- content hash; the bytes are FILES_DIR/ab/ab…<ext>
  mime TEXT NOT NULL,
  size INTEGER NOT NULL,
  name TEXT,                        -- the original file name when Telegram gives one
  created_at TEXT NOT NULL,
  description TEXT                  -- what is on it, written by the LLM (`photo` in its result)
);
CREATE INDEX IF NOT EXISTS attachments_message ON attachments (message_id);

CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY,           -- every save is a new row; the latest one is current
  text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL          -- 'web' or a family member id
);

CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY,           -- every change is a new row; the latest one is current
  text TEXT NOT NULL,               -- the «Нотатки» page, Markdown as the LLM keeps it; '' cleared
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL          -- the family member whose message changed it
);

CREATE TABLE IF NOT EXISTS today_lists (
  id INTEGER PRIMARY KEY,           -- every change is a new row; the latest per member is current
  member TEXT NOT NULL,             -- whose «на сьогодні» board
  text TEXT NOT NULL,               -- free text, as the person keeps it; '' = cleared
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL          -- the member who asked for the change
);

CREATE TABLE IF NOT EXISTS members (
  id TEXT PRIMARY KEY,              -- latin slug (oleh); what the LLM uses as owner
  name TEXT NOT NULL,
  telegram_id INTEGER UNIQUE,       -- the allowlist; NULL until known
  created_at TEXT NOT NULL
);
"""


@lru_cache(maxsize=64)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _regexp(pattern: str, text: str | None) -> bool:
    """SQLite `text REGEXP pattern` via Python's re: Unicode-aware, unlike LIKE."""
    return text is not None and _compiled(pattern).search(text) is not None


def _ufold(text: str | None) -> str | None:
    """Unicode casefold for SQL; NULL stays NULL, as with SQLite's own functions."""
    return text.casefold() if text is not None else None


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Message:
    id: int
    user_id: str
    chat_with: str
    tg_message_id: int | None
    created_at: str
    raw_text: str
    is_voice: bool
    photo_file_id: str | None
    llm_result: str | None


@dataclass(frozen=True)
class Attachment:
    """A file that came with a message; the bytes are in FILES_DIR under `sha256`."""

    id: int
    message_id: int
    sha256: str
    mime: str
    size: int
    name: str | None  # photos have none; a document keeps the name it was sent with
    created_at: str
    description: str | None  # what is on it, in the LLM's words; None until it answered

    @property
    def is_image(self) -> bool:
        return self.mime.startswith("image/")


@dataclass(frozen=True)
class Todo:
    id: int
    text: str
    owner: str | None
    status: str
    due: str | None  # ISO date, the deadline day; None = undated
    created_by: str
    created_at: str
    source_message_id: int
    closed_at: str | None
    position: int | None = None  # set by dragging on the web; meaningful for undated ones

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def has_due(self) -> bool:
        return bool(self.due)


@dataclass(frozen=True)
class Dream:
    """Something the family wants some day: no date, never overdue; fulfilled or let go."""

    id: int
    text: str
    status: str
    created_by: str
    created_at: str
    source_message_id: int
    closed_at: str | None

    @property
    def is_open(self) -> bool:
        return self.status == "open"


@dataclass(frozen=True)
class Event:
    id: int
    text: str
    who: str | None
    status: str
    starts_at: str | None
    until: str | None
    date_from: str | None
    date_to: str | None
    created_by: str
    created_at: str
    source_message_id: int
    cancelled_at: str | None

    @property
    def is_planned(self) -> bool:
        return self.status == "planned"

    @property
    def all_day(self) -> bool:
        return self.starts_at is None


@dataclass(frozen=True)
class Reminder:
    id: int
    text: str
    who: str | None
    at: str
    status: str
    created_by: str
    created_at: str
    source_message_id: int
    sent_at: str | None

    @property
    def is_pending(self) -> bool:
        return self.status == "pending"


@dataclass(frozen=True)
class Facts:
    id: int
    text: str
    created_at: str
    created_by: str


@dataclass(frozen=True)
class Notes:
    """The one «Нотатки» page: a Markdown reference the LLM rewrites whole on request; a
    new row per change, the latest is current."""

    id: int
    text: str
    created_at: str
    created_by: str


@dataclass(frozen=True)
class TodayList:
    """One member's «на сьогодні» board: free text kept through the bot, a new row per change."""

    id: int
    member: str
    text: str
    created_at: str
    created_by: str


@dataclass(frozen=True)
class Member:
    id: str
    name: str
    telegram_id: int | None = None


@dataclass(frozen=True)
class Item:
    """A tracked thing: one object, a box, a pile of the same stuff; where it is now."""

    id: int
    name: str
    owner: str | None
    place: str | None
    spot: str | None
    note: str | None
    created_by: str
    created_at: str
    updated_at: str
    source_message_id: int
    removed_at: str | None

    @property
    def location(self) -> str:
        """'офіс / сейф', 'офіс', or '' when unknown."""
        return " / ".join(p for p in (self.place, self.spot) if p)


@dataclass(frozen=True)
class ItemChange:
    """One row of an item's history."""

    id: int
    item_id: int
    kind: str  # 'created' | 'moved' | 'corrected' | 'gone'
    place: str | None
    spot: str | None
    detail: str | None
    who: str
    at: str
    source_message_id: int


def _message(row: sqlite3.Row) -> Message:
    d = dict(row)
    d["is_voice"] = bool(d["is_voice"])
    return Message(**d)


def _attachment(row: sqlite3.Row) -> Attachment:
    return Attachment(**dict(row))


def _item(row: sqlite3.Row) -> Item:
    return Item(**dict(row))


def _item_change(row: sqlite3.Row) -> ItemChange:
    return ItemChange(**dict(row))


def _todo(row: sqlite3.Row) -> Todo:
    return Todo(**dict(row))


def _dream(row: sqlite3.Row) -> Dream:
    return Dream(**dict(row))


def _event(row: sqlite3.Row) -> Event:
    return Event(**dict(row))


def _reminder(row: sqlite3.Row) -> Reminder:
    return Reminder(**dict(row))


ITEM_UPDATABLE = ("name", "owner", "place", "spot", "note")
ITEM_LABELS = {"name": "назва", "owner": "власник", "note": "примітка"}  # history detail
TODO_UPDATABLE = ("text", "owner", "due")
EVENT_UPDATABLE = ("text", "who", "starts_at", "until", "date_from", "date_to")
REMINDER_UPDATABLE = ("text", "who", "at")


class Database:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        # SQLite's LIKE and lower() are ASCII-only; Ukrainian text needs Python's casefold.
        self.conn.create_function("ufold", 1, _ufold, deterministic=True)
        self.conn.create_function("regexp", 2, _regexp, deterministic=True)
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """What CREATE TABLE IF NOT EXISTS cannot express. Idempotent, runs at every start."""
        tables = {
            r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        # The notes went on 2026-09-12: a `journal` table (with its FTS index and triggers)
        # from before that stays in the file untouched, no longer created or read.
        if "commitments" in tables:
            # 2026-09-12: commitments became todos. A todo has a deadline day, not a time or
            # a window (a thing with a time of day is an event): due_at -> its day in Kyiv,
            # a window -> its last day. Same ids; the old table goes.
            kyiv = ZoneInfo("Europe/Kyiv")
            for r in self.conn.execute("SELECT * FROM commitments ORDER BY id").fetchall():
                due = r["due_to"] or r["due_from"] or None
                if r["due_at"]:
                    at = datetime.fromisoformat(r["due_at"].replace("Z", "+00:00"))
                    due = at.astimezone(kyiv).date().isoformat()
                self.conn.execute(
                    "INSERT OR IGNORE INTO todos (id, text, owner, status, due, position,"
                    " created_by, created_at, source_message_id, closed_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        r["id"],
                        r["text"],
                        r["owner"],
                        r["status"],
                        due,
                        r["position"] if "position" in r.keys() else None,  # noqa: SIM118
                        r["created_by"],
                        r["created_at"],
                        r["source_message_id"],
                        r["closed_at"],
                    ),
                )
            self.conn.execute("DROP TABLE commitments")
            self.conn.commit()
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(messages)")}
        if "photo_file_id" not in columns:
            # 2026-09-11: photos; the message keeps the Telegram file id, not the file.
            self.conn.execute("ALTER TABLE messages ADD COLUMN photo_file_id TEXT")
            self.conn.commit()
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(attachments)")}
        if "description" not in columns:
            # 2026-09-11, later that evening: the LLM describes each photo for the web.
            self.conn.execute("ALTER TABLE attachments ADD COLUMN description TEXT")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def backup_to(self, path: Path | str) -> None:
        """A consistent copy of the whole database via SQLite's online backup.

        Copying the file with sftp misses whatever still sits in the -wal file; this does not.
        """
        dst = sqlite3.connect(str(path))
        try:
            self.conn.backup(dst)
        finally:
            dst.close()

    # --- messages -----------------------------------------------------------

    def insert_message(
        self,
        user_id: str,
        chat_with: str,
        raw_text: str,
        *,
        is_voice: bool = False,
        photo_file_id: str | None = None,
        tg_message_id: int | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO messages"
            " (user_id, chat_with, tg_message_id, created_at, raw_text, is_voice, photo_file_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                chat_with,
                tg_message_id,
                utc_now_iso(),
                raw_text,
                int(is_voice),
                photo_file_id,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def set_llm_result(self, message_id: int, llm_result: str) -> None:
        self.conn.execute(
            "UPDATE messages SET llm_result = ? WHERE id = ?", (llm_result, message_id)
        )
        self.conn.commit()

    def set_tg_message_id(self, message_id: int, tg_message_id: int) -> None:
        self.conn.execute(
            "UPDATE messages SET tg_message_id = ? WHERE id = ?", (tg_message_id, message_id)
        )
        self.conn.commit()

    def get_message(self, message_id: int) -> Message | None:
        row = self.conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return _message(row) if row else None

    def recent_messages(self, limit: int = 20) -> list[Message]:
        """Last N messages across all chats, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_message(r) for r in reversed(rows)]

    def list_messages(self, limit: int = 200) -> list[Message]:
        """Newest first, for the web debug view."""
        rows = self.conn.execute(
            "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_message(r) for r in rows]

    def last_user_message(self, chat_with: str) -> Message | None:
        row = self.conn.execute(
            "SELECT * FROM messages WHERE chat_with = ? AND user_id != 'bot'"
            " ORDER BY id DESC LIMIT 1",
            (chat_with,),
        ).fetchone()
        return _message(row) if row else None

    # --- attachments --------------------------------------------------------
    # The bytes are in files.FileStore; the row says which message brought them.

    def add_attachment(
        self, message_id: int, sha256: str, mime: str, size: int, name: str | None = None
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO attachments (message_id, sha256, mime, size, name, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, sha256, mime, size, name, utc_now_iso()),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def attachment_by_sha(self, sha256: str) -> Attachment | None:
        row = self.conn.execute(
            "SELECT * FROM attachments WHERE sha256 = ? ORDER BY id LIMIT 1", (sha256,)
        ).fetchone()
        return _attachment(row) if row else None

    def list_attachments(self) -> list[Attachment]:
        """Every file, oldest first: what `pull` mirrors."""
        rows = self.conn.execute("SELECT * FROM attachments ORDER BY id").fetchall()
        return [_attachment(r) for r in rows]

    def attachments_with_messages(
        self, limit: int | None = None
    ) -> list[tuple[Attachment, Message]]:
        """Every file with the message it came with, newest first."""
        sql = "SELECT * FROM attachments ORDER BY id DESC"
        rows = self.conn.execute(sql + (" LIMIT ?" if limit else ""), (limit,) if limit else ())
        return self._with_messages([_attachment(r) for r in rows.fetchall()])

    def _with_messages(self, files: list[Attachment]) -> list[tuple[Attachment, Message]]:
        ids = {a.message_id for a in files}
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        messages = {
            m.id: m
            for m in map(
                _message,
                self.conn.execute(f"SELECT * FROM messages WHERE id IN ({marks})", tuple(ids)),
            )
        }
        return [(a, messages[a.message_id]) for a in files if a.message_id in messages]

    # --- items --------------------------------------------------------------
    # Every change goes through here and writes item_history; callers never touch it.

    def _item_change(
        self,
        item_id: int,
        kind: str,
        place: str | None,
        spot: str | None,
        detail: str | None,
        who: str,
        at: str,
        source_message_id: int,
    ) -> None:
        self.conn.execute(
            "INSERT INTO item_history (item_id, kind, place, spot, detail, who, at,"
            " source_message_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, kind, place, spot, detail, who, at, source_message_id),
        )

    def create_item(
        self,
        name: str,
        *,
        owner: str | None,
        place: str | None,
        spot: str | None,
        note: str | None,
        created_by: str,
        source_message_id: int,
    ) -> int:
        now = utc_now_iso()
        cur = self.conn.execute(
            "INSERT INTO items (name, owner, place, spot, note, created_by, created_at,"
            " updated_at, source_message_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, owner, place, spot, note, created_by, now, now, source_message_id),
        )
        item_id = int(cur.lastrowid or 0)
        self._item_change(item_id, "created", place, spot, None, created_by, now, source_message_id)
        self.conn.commit()
        return item_id

    def update_item(
        self, item_id: int, fields: dict[str, str | None], *, who: str, source_message_id: int
    ) -> str | None:
        """Apply what differs from the current row. Returns the history kind written: 'moved'
        when the location changed, else 'corrected'; None if the item is unknown, gone, or
        nothing differs."""
        current = self.get_item(item_id)
        if current is None or current.removed_at:
            return None
        changed = {
            k: v for k, v in fields.items() if k in ITEM_UPDATABLE and v != getattr(current, k)
        }
        if not changed:
            return None
        now = utc_now_iso()
        assignments = ", ".join(f"{k} = ?" for k in changed)
        self.conn.execute(
            f"UPDATE items SET {assignments}, updated_at = ? WHERE id = ?",
            (*changed.values(), now, item_id),
        )
        kind = "moved" if "place" in changed or "spot" in changed else "corrected"
        detail = "; ".join(
            f"{ITEM_LABELS[k]}: {v or '—'}" for k, v in changed.items() if k in ITEM_LABELS
        )
        self._item_change(
            item_id,
            kind,
            changed.get("place", current.place),
            changed.get("spot", current.spot),
            detail or None,
            who,
            now,
            source_message_id,
        )
        self.conn.commit()
        return kind

    def remove_item(self, item_id: int, *, who: str, source_message_id: int) -> bool:
        """The thing is gone: thrown away, given away, lost. False if unknown or already gone."""
        current = self.get_item(item_id)
        if current is None or current.removed_at:
            return False
        now = utc_now_iso()
        self.conn.execute(
            "UPDATE items SET removed_at = ?, updated_at = ? WHERE id = ?", (now, now, item_id)
        )
        self._item_change(
            item_id, "gone", current.place, current.spot, None, who, now, source_message_id
        )
        self.conn.commit()
        return True

    def get_item(self, item_id: int) -> Item | None:
        row = self.conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return _item(row) if row else None

    def item_history(self, item_id: int) -> list[ItemChange]:
        """Newest first."""
        rows = self.conn.execute(
            "SELECT * FROM item_history WHERE item_id = ? ORDER BY id DESC", (item_id,)
        ).fetchall()
        return [_item_change(r) for r in rows]

    def items_changed_since(self, since_iso: str) -> list[Item]:
        """Items (not gone) touched at or after `since_iso`, most recently touched first."""
        rows = self.conn.execute(
            "SELECT * FROM items WHERE removed_at IS NULL AND updated_at >= ?"
            " ORDER BY updated_at DESC, id DESC",
            (since_iso,),
        ).fetchall()
        return [_item(r) for r in rows]

    def recent_items(self, limit: int = 10) -> list[Item]:
        rows = self.conn.execute(
            "SELECT * FROM items WHERE removed_at IS NULL ORDER BY updated_at DESC, id DESC"
            " LIMIT ?",
            (limit,),
        ).fetchall()
        return [_item(r) for r in rows]

    def list_items(self, *, place: str | None = None, owner: str | None = None) -> list[Item]:
        """Items (not gone) at a place ('' = no place known) or of an owner; all when neither."""
        where, params = ["removed_at IS NULL"], []
        if place == "":
            where.append("place IS NULL")
        elif place is not None:
            where.append("ufold(place) = ufold(?)")
            params.append(place)
        if owner:
            where.append("ufold(owner) = ufold(?)")
            params.append(owner)
        rows = self.conn.execute(
            f"SELECT * FROM items WHERE {' AND '.join(where)} ORDER BY place, spot, name",
            params,
        ).fetchall()
        return [_item(r) for r in rows]

    def search_items(self, pattern: str, limit: int = 20) -> list[Item]:
        """`pattern` is a casefolded regex (context.word_pattern) over name, owner, place,
        spot and note of items that are not gone; most recently touched first."""
        rows = self.conn.execute(
            "SELECT * FROM items WHERE removed_at IS NULL AND ufold(name || ' '"
            " || coalesce(owner, '') || ' ' || coalesce(place, '') || ' ' || coalesce(spot, '')"
            " || ' ' || coalesce(note, '')) REGEXP ? ORDER BY updated_at DESC, id DESC LIMIT ?",
            (pattern, limit),
        ).fetchall()
        return [_item(r) for r in rows]

    def places(self) -> list[tuple[str | None, int]]:
        """Where things are, with counts, fullest first; None is the count without a place."""
        rows = self.conn.execute(
            "SELECT place, COUNT(*) FROM items WHERE removed_at IS NULL GROUP BY place"
            " ORDER BY COUNT(*) DESC, place"
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    # --- facts --------------------------------------------------------------

    def current_facts(self) -> Facts | None:
        row = self.conn.execute("SELECT * FROM facts ORDER BY id DESC LIMIT 1").fetchone()
        return Facts(**dict(row)) if row else None

    def save_facts(self, text: str, created_by: str) -> int:
        """Store a new version. Returns its id."""
        cur = self.conn.execute(
            "INSERT INTO facts (text, created_at, created_by) VALUES (?, ?, ?)",
            (text, utc_now_iso(), created_by),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def facts_versions(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])

    # --- notes ----------------------------------------------------------------

    def current_notes(self) -> Notes | None:
        row = self.conn.execute("SELECT * FROM notes ORDER BY id DESC LIMIT 1").fetchone()
        return Notes(**dict(row)) if row else None

    def save_notes(self, text: str, created_by: str) -> int:
        """Store a new version of the page. Returns its id."""
        cur = self.conn.execute(
            "INSERT INTO notes (text, created_at, created_by) VALUES (?, ?, ?)",
            (text, utc_now_iso(), created_by),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def notes_versions(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0])

    # --- today lists --------------------------------------------------------

    def current_today_lists(self) -> dict[str, TodayList]:
        """The latest board of every member who ever had one, by member id."""
        rows = self.conn.execute(
            "SELECT * FROM today_lists"
            " WHERE id IN (SELECT max(id) FROM today_lists GROUP BY member)"
        ).fetchall()
        return {r["member"]: TodayList(**dict(r)) for r in rows}

    def save_today_list(self, member: str, text: str, created_by: str) -> int:
        """Store a new version of `member`'s board. Returns its id."""
        cur = self.conn.execute(
            "INSERT INTO today_lists (member, text, created_at, created_by) VALUES (?, ?, ?, ?)",
            (member, text, utc_now_iso(), created_by),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    # --- members ------------------------------------------------------------

    def list_members(self) -> list[Member]:
        rows = self.conn.execute(
            "SELECT id, name, telegram_id FROM members ORDER BY rowid"
        ).fetchall()
        return [Member(**dict(r)) for r in rows]

    def get_member(self, member_id: str) -> Member | None:
        row = self.conn.execute(
            "SELECT id, name, telegram_id FROM members WHERE id = ?", (member_id,)
        ).fetchone()
        return Member(**dict(row)) if row else None

    def member_by_telegram_id(self, telegram_id: int) -> Member | None:
        row = self.conn.execute(
            "SELECT id, name, telegram_id FROM members WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return Member(**dict(row)) if row else None

    def add_member(self, member_id: str, name: str, telegram_id: int | None) -> Member:
        """Raises ValueError when the id or the telegram_id is already taken."""
        try:
            self.conn.execute(
                "INSERT INTO members (id, name, telegram_id, created_at) VALUES (?, ?, ?, ?)",
                (member_id, name, telegram_id, utc_now_iso()),
            )
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise ValueError(f"member {member_id!r} or telegram_id {telegram_id} taken") from exc
        self.conn.commit()
        return Member(member_id, name, telegram_id)

    def update_member(self, member_id: str, *, name: str, telegram_id: int | None) -> bool:
        """Returns False if there is no such member. Raises ValueError on a taken telegram_id."""
        try:
            cur = self.conn.execute(
                "UPDATE members SET name = ?, telegram_id = ? WHERE id = ?",
                (name, telegram_id, member_id),
            )
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            raise ValueError(f"telegram_id {telegram_id} taken") from exc
        self.conn.commit()
        return cur.rowcount == 1

    # --- events -------------------------------------------------------------

    def create_event(
        self,
        text: str,
        *,
        who: str | None,
        created_by: str,
        source_message_id: int,
        starts_at: str | None = None,
        until: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO events (text, who, status, starts_at, until, date_from, date_to,"
            " created_by, created_at, source_message_id)"
            " VALUES (?, ?, 'planned', ?, ?, ?, ?, ?, ?, ?)",
            (
                text,
                who,
                starts_at,
                until,
                date_from,
                date_to,
                created_by,
                utc_now_iso(),
                source_message_id,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def update_event(self, event_id: int, **fields: str | None) -> bool:
        """Update text/who/when of a planned event. Returns False if not planned."""
        fields = {k: v for k, v in fields.items() if k in EVENT_UPDATABLE}
        if not fields:
            return False
        assignments = ", ".join(f"{k} = ?" for k in fields)
        cur = self.conn.execute(
            f"UPDATE events SET {assignments} WHERE id = ? AND status = 'planned'",
            (*fields.values(), event_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def cancel_event(self, event_id: int) -> bool:
        """Returns False if the event does not exist or is already cancelled."""
        cur = self.conn.execute(
            "UPDATE events SET status = 'cancelled', cancelled_at = ?"
            " WHERE id = ? AND status = 'planned'",
            (utc_now_iso(), event_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def get_event(self, event_id: int) -> Event | None:
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return _event(row) if row else None

    def planned_events(self) -> list[Event]:
        """All planned events, past ones included; callers slice by time (see context)."""
        rows = self.conn.execute(
            "SELECT * FROM events WHERE status = 'planned' ORDER BY id"
        ).fetchall()
        return [_event(r) for r in rows]

    def search_events(self, pattern: str, limit: int = 50) -> list[Event]:
        """`pattern` is a casefolded regex, see `context.word_pattern`."""
        rows = self.conn.execute(
            "SELECT * FROM events WHERE ufold(text) REGEXP ? ORDER BY id DESC LIMIT ?",
            (pattern, limit),
        ).fetchall()
        return [_event(r) for r in rows]

    # --- todos --------------------------------------------------------

    def create_todo(
        self,
        text: str,
        *,
        owner: str | None,
        created_by: str,
        source_message_id: int,
        due: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO todos (text, owner, status, due, created_by, created_at,"
            " source_message_id) VALUES (?, ?, 'open', ?, ?, ?, ?)",
            (text, owner, due, created_by, utc_now_iso(), source_message_id),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def update_todo(self, todo_id: int, **fields: str | None) -> bool:
        """Update text/owner/due of an open todo. Returns False if not open."""
        fields = {k: v for k, v in fields.items() if k in TODO_UPDATABLE}
        if not fields:
            return False
        assignments = ", ".join(f"{k} = ?" for k in fields)
        cur = self.conn.execute(
            f"UPDATE todos SET {assignments} WHERE id = ? AND status = 'open'",
            (*fields.values(), todo_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def close_todo(self, todo_id: int, status: str) -> bool:
        """Close an open todo as 'done' or 'dropped'. Returns False if not open."""
        if status not in ("done", "dropped"):
            raise ValueError(f"bad status: {status}")
        cur = self.conn.execute(
            "UPDATE todos SET status = ?, closed_at = ? WHERE id = ? AND status = 'open'",
            (status, utc_now_iso(), todo_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def get_todo(self, todo_id: int) -> Todo | None:
        row = self.conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
        return _todo(row) if row else None

    def open_todos(self) -> list[Todo]:
        """Open ones in the family's order: the unplaced ones first, newest first (a new todo
        goes on top until someone drags it), then the hand-set positions (see reorder_todos).
        The timeline, the digest and the LLM context all take this order for the undated
        ones, so what someone dragged on the web holds everywhere; dated ones are shown by
        deadline wherever they appear."""
        rows = self.conn.execute(
            "SELECT * FROM todos WHERE status = 'open'"
            " ORDER BY position IS NOT NULL, position, id DESC"
        ).fetchall()
        return [_todo(r) for r in rows]

    def recent_done_todos(self, limit: int) -> list[Todo]:
        """The last ones closed as done, newest first: the tail of the home page."""
        rows = self.conn.execute(
            "SELECT * FROM todos WHERE status = 'done' ORDER BY closed_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_todo(r) for r in rows]

    def reorder_todos(self, ids: list[int]) -> None:
        """The undated list as someone dragged it on the web: `ids` in this order; every other
        open todo (new since that page was drawn, or missing from a stale one) loses its
        position and goes on top, newest first. Ids that are not open are ignored."""
        self.conn.execute("UPDATE todos SET position = NULL WHERE status = 'open'")
        self.conn.executemany(
            "UPDATE todos SET position = ? WHERE id = ? AND status = 'open'",
            [(n, tid) for n, tid in enumerate(ids, start=1)],
        )
        self.conn.commit()

    def search_todos(self, pattern: str, limit: int = 50) -> list[Todo]:
        """`pattern` is a casefolded regex, see `context.word_pattern`."""
        rows = self.conn.execute(
            "SELECT * FROM todos WHERE ufold(text) REGEXP ? ORDER BY id DESC LIMIT ?",
            (pattern, limit),
        ).fetchall()
        return [_todo(r) for r in rows]

    # --- dreams ---------------------------------------------------------------

    def create_dream(self, text: str, *, created_by: str, source_message_id: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO dreams (text, status, created_by, created_at, source_message_id)"
            " VALUES (?, 'open', ?, ?, ?)",
            (text, created_by, utc_now_iso(), source_message_id),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def update_dream(self, dream_id: int, text: str) -> bool:
        """Reword an open dream. Returns False if not open."""
        cur = self.conn.execute(
            "UPDATE dreams SET text = ? WHERE id = ? AND status = 'open'", (text, dream_id)
        )
        self.conn.commit()
        return cur.rowcount == 1

    def close_dream(self, dream_id: int, status: str) -> bool:
        """Close an open dream as 'fulfilled' or 'dropped'. Returns False if not open."""
        if status not in ("fulfilled", "dropped"):
            raise ValueError(f"bad status: {status}")
        cur = self.conn.execute(
            "UPDATE dreams SET status = ?, closed_at = ? WHERE id = ? AND status = 'open'",
            (status, utc_now_iso(), dream_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def get_dream(self, dream_id: int) -> Dream | None:
        row = self.conn.execute("SELECT * FROM dreams WHERE id = ?", (dream_id,)).fetchone()
        return _dream(row) if row else None

    def open_dreams(self) -> list[Dream]:
        """The family's dreams, oldest first: the list grows at the bottom."""
        rows = self.conn.execute(
            "SELECT * FROM dreams WHERE status = 'open' ORDER BY id"
        ).fetchall()
        return [_dream(r) for r in rows]

    def fulfilled_dreams(self) -> list[Dream]:
        """The ones that came true, the latest first."""
        rows = self.conn.execute(
            "SELECT * FROM dreams WHERE status = 'fulfilled' ORDER BY closed_at DESC, id DESC"
        ).fetchall()
        return [_dream(r) for r in rows]

    # --- reminders ------------------------------------------------------------

    def create_reminder(
        self,
        text: str,
        *,
        who: str | None,
        at: str,
        created_by: str,
        source_message_id: int,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO reminders (text, who, at, status, created_by, created_at,"
            " source_message_id) VALUES (?, ?, ?, 'pending', ?, ?, ?)",
            (text, who, at, created_by, utc_now_iso(), source_message_id),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def update_reminder(self, reminder_id: int, **fields: str | None) -> bool:
        """Update text/who/at of a pending reminder. Returns False if not pending."""
        fields = {k: v for k, v in fields.items() if k in REMINDER_UPDATABLE}
        if not fields:
            return False
        assignments = ", ".join(f"{k} = ?" for k in fields)
        cur = self.conn.execute(
            f"UPDATE reminders SET {assignments} WHERE id = ? AND status = 'pending'",
            (*fields.values(), reminder_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def cancel_reminder(self, reminder_id: int) -> bool:
        cur = self.conn.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
            (reminder_id,),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def finish_reminder(self, reminder_id: int, status: str) -> bool:
        """'sent', or 'missed' when it came due while the bot was down. False if not pending."""
        if status not in ("sent", "missed"):
            raise ValueError(f"bad status: {status}")
        cur = self.conn.execute(
            "UPDATE reminders SET status = ?, sent_at = ? WHERE id = ? AND status = 'pending'",
            (status, utc_now_iso() if status == "sent" else None, reminder_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def get_reminder(self, reminder_id: int) -> Reminder | None:
        row = self.conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return _reminder(row) if row else None

    def pending_reminders(self) -> list[Reminder]:
        rows = self.conn.execute(
            "SELECT * FROM reminders WHERE status = 'pending' ORDER BY at, id"
        ).fetchall()
        return [_reminder(r) for r in rows]

    def due_reminders(self, now_iso: str) -> list[Reminder]:
        """Pending reminders whose time has come: `at` <= now, both ISO UTC."""
        rows = self.conn.execute(
            "SELECT * FROM reminders WHERE status = 'pending' AND at <= ? ORDER BY at, id",
            (now_iso,),
        ).fetchall()
        return [_reminder(r) for r in rows]
