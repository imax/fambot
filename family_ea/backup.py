"""The backup archive: a consistent snapshot of the database, the files it refers to, and
the two things worth reading without a database (`notes.md`, the notes page as kept;
`dreams.md`, the dreams with who and when), zipped. Built by code only (no LLM);
`python -m family_ea backup` writes one locally, the nightly mail job will send the same
bytes. Restore: the .db to DATABASE_PATH, `files/` to FILES_DIR; the .md files are for
people, everything in them is in the .db."""

from __future__ import annotations

import io
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .context import fmt_dt
from .db import Database
from .files import FileStore


def snapshot_bytes(db: Database) -> bytes:
    """The online backup as bytes, checked with `integrity_check` before it goes anywhere."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "snapshot.db"
        db.backup_to(path)
        copy = sqlite3.connect(path)
        try:
            (verdict,) = copy.execute("PRAGMA integrity_check").fetchone()
        finally:
            copy.close()
        if verdict != "ok":
            raise RuntimeError(f"database snapshot failed integrity_check: {verdict}")
        return path.read_bytes()


def notes_markdown(db: Database, tz: ZoneInfo) -> str:
    """The notes page as kept, with when and by whom it last changed; '' when empty."""
    notes = db.current_notes()
    if notes is None or not notes.text.strip():
        return ""
    when = fmt_dt(notes.created_at, tz)
    return f"{notes.text.strip()}\n\n---\nОновлено {when}, {notes.created_by}\n"


def dreams_markdown(db: Database, tz: ZoneInfo) -> str:
    """The open dreams, then the fulfilled ones, each with who and when; '' when none."""
    lines = []
    if open_ones := db.open_dreams():
        lines.append("# Мрії")
        lines += [f"- {d.text} ({d.created_by}, {fmt_dt(d.created_at, tz)[:5]})" for d in open_ones]
    if done := db.fulfilled_dreams():
        lines += ["", "# Здійснилось"] if lines else ["# Здійснилось"]
        for d in done:
            day = fmt_dt(d.closed_at or d.created_at, tz)[:5]
            lines.append(f"- {d.text} ({d.created_by}, здійснилось {day})")
    return "\n".join(lines) + "\n" if lines else ""


def build_archive(
    db: Database,
    tz: ZoneInfo,
    now: datetime | None = None,
    store: FileStore | None = None,
) -> tuple[str, bytes, list[str]]:
    """(file name, zip bytes, missing files): `family-YYYY-MM-DD.db`, `notes.md` and
    `dreams.md` when there is anything in them, and, with a `store`, `files/ab/ab12….jpg`
    for every row of `attachments` whose bytes are there; the hashes of those that are
    not come back so the caller can say so."""
    stamp = (now or datetime.now(tz)).astimezone(tz).strftime("%Y-%m-%d")
    buf = io.BytesIO()
    missing: list[str] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"family-{stamp}.db", snapshot_bytes(db))
        for name, text in (
            ("notes.md", notes_markdown(db, tz)),
            ("dreams.md", dreams_markdown(db, tz)),
        ):
            if text:
                zf.writestr(name, text)
        seen: set[Path] = set()
        for a in db.list_attachments() if store else []:
            assert store
            path = store.path(a.sha256, a.mime)
            if path in seen:
                continue  # the same bytes sent twice: one file
            seen.add(path)
            if not path.is_file():
                missing.append(a.sha256)
                continue
            # already compressed (jpeg, png, pdf): stored as is
            zf.write(path, f"files/{path.relative_to(store.root)}", zipfile.ZIP_STORED)
    return f"family-{stamp}.zip", buf.getvalue(), missing


def write_archive(
    db: Database, tz: ZoneInfo, dest_dir: Path, store: FileStore | None = None
) -> tuple[Path, list[str]]:
    name, data, missing = build_archive(db, tz, store=store)
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / name
    path.write_bytes(data)
    return path, missing
