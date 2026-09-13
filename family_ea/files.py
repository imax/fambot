"""Files that came with messages: photos of things, for the inventory, and the photos the
notes page was written from.

The bytes live on disk under FILES_DIR, named by their SHA-256 (`ab/ab12….jpg`), so the
same scan sent twice is one file, nothing is ever rewritten, and any sync is «list the
hashes, fetch the missing ones». The database keeps one row per file (`attachments`)
pointing at the message it came with; that is the only link. An item shows the files of
the message that created it and of every later message whose ops touched it (the
`applied` log); the notes page shows the files of every message that changed it; so no
LLM op ever mentions a file. Nothing describes or indexes a file: those pages show their
photos as thumbnails, and that is the whole feature (a «Документи» tab over every file
was built and removed on 2026-09-12: too much).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Protocol

from .db import Attachment, Database, Message

EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FileStore:
    """Content-addressed files under one directory; a file is written once and never changed."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def path(self, sha256: str, mime: str) -> Path:
        return self.root / sha256[:2] / f"{sha256}{EXTENSIONS.get(mime, '.bin')}"

    def has(self, sha256: str, mime: str) -> bool:
        return self.path(sha256, mime).is_file()

    def put(self, data: bytes, mime: str) -> str:
        """Store `data` and return its hash; a file already there is left as it is."""
        sha = sha256_hex(data)
        path = self.path(sha, mime)
        if path.is_file():
            return sha
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)  # a reader never sees a half-written file
        return sha


def applied_of(message: Message) -> list[dict]:
    """The `applied` log of a message: which records its ops created or changed."""
    if not message.llm_result:
        return []
    try:
        applied = json.loads(message.llm_result).get("applied", [])
    except (ValueError, AttributeError):
        return []
    return [a for a in applied if isinstance(a, dict)]


class Record(Protocol):
    id: int
    source_message_id: int


def files_for(db: Database, kind: str, records: list) -> dict[int, list[Attachment]]:
    """The files under each record, by record id: those of the message that created it and
    of every message whose `applied` log names it (`kind` is 'item'), in the
    order they arrived. Records without files are absent from the result."""
    rows = db.attachments_with_messages()
    if not rows:
        return {}
    by_message: dict[int, list[Attachment]] = {}
    by_record: dict[int, list[Attachment]] = {}
    for a, m in rows:
        by_message.setdefault(a.message_id, []).append(a)
        for ap in applied_of(m):
            if ap.get("kind") == kind and ap.get("ok") and ap.get("id"):
                by_record.setdefault(int(ap["id"]), []).append(a)
    out: dict[int, list[Attachment]] = {}
    for r in records:
        found = {a.id: a for a in by_message.get(r.source_message_id, []) + by_record.get(r.id, [])}
        if found:
            out[r.id] = [found[k] for k in sorted(found)]
    return out


def files_of_kind(db: Database, kind: str) -> list[Attachment]:
    """Every file of a message whose `applied` log has an ok op of `kind`, in the order they
    arrived: the sources of the one notes page (`kind` is 'notes')."""
    return [
        a
        for a, m in db.attachments_with_messages()
        if any(ap.get("kind") == kind and ap.get("ok") for ap in applied_of(m))
    ]
