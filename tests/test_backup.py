"""The backup archive: a checked database snapshot plus the files, zipped."""

import sqlite3
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path

from family_ea.backup import build_archive, dreams_markdown, notes_markdown, snapshot_bytes
from family_ea.db import Database
from family_ea.files import FileStore
from family_ea.main import backup
from tests.conftest import KYIV
from tests.test_web import JPEG, _settings


def _seed(db: Database) -> None:
    mid = db.insert_message("oleh", "oleh", "...")
    db.create_todo("Замінити масло", owner="oleh", created_by="oleh", source_message_id=mid)
    db.create_todo("Купити чайник", owner="anna", created_by="anna", source_message_id=mid)
    done = db.create_todo("Фільтр у котлі", owner=None, created_by="oleh", source_message_id=mid)
    db.close_todo(done, "done")


def test_snapshot_is_a_checked_copy(db: Database, tmp_path: Path) -> None:
    _seed(db)
    data = snapshot_bytes(db)
    copy = tmp_path / "copy.db"
    copy.write_bytes(data)
    conn = sqlite3.connect(copy)
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert conn.execute("SELECT count(*) FROM todos").fetchone() == (3,)
    conn.close()


def test_archive_holds_the_snapshot(db: Database) -> None:
    _seed(db)
    name, data, missing = build_archive(db, KYIV, now=datetime(2026, 9, 11, 3, 30, tzinfo=KYIV))
    assert name == "family-2026-09-11.zip" and missing == []
    with zipfile.ZipFile(BytesIO(data)) as zf:
        assert zf.namelist() == ["family-2026-09-11.db"]
        assert zf.read("family-2026-09-11.db")[:16] == b"SQLite format 3\x00"


def test_archive_holds_the_files_and_names_the_missing(db: Database, tmp_path: Path) -> None:
    store = FileStore(tmp_path / "files")
    sha = store.put(JPEG, "image/jpeg")
    m1 = db.insert_message("oleh", "oleh", "чек", photo_file_id="f")
    m2 = db.insert_message("anna", "anna", "той самий чек", photo_file_id="g")
    db.add_attachment(m1, sha, "image/jpeg", len(JPEG))
    db.add_attachment(m2, sha, "image/jpeg", len(JPEG))  # the same bytes twice: one entry
    db.add_attachment(m2, "0" * 64, "image/png", 1)  # never mirrored
    name, data, missing = build_archive(db, KYIV, store=store)
    assert missing == ["0" * 64]
    with zipfile.ZipFile(BytesIO(data)) as zf:
        assert zf.namelist()[1:] == [f"files/{sha[:2]}/{sha}.jpg"]
        assert zf.read(f"files/{sha[:2]}/{sha}.jpg") == JPEG
        assert zf.getinfo(f"files/{sha[:2]}/{sha}.jpg").compress_type == zipfile.ZIP_STORED


def test_backup_command_writes_the_archive(tmp_path: Path, capsys) -> None:
    src = Database(tmp_path / "src.db")
    _seed(src)
    sha = FileStore(tmp_path / "src-files").put(JPEG, "image/jpeg")
    mid = src.insert_message("oleh", "oleh", "чек", photo_file_id="f")
    src.add_attachment(mid, sha, "image/jpeg", len(JPEG))
    src.add_attachment(mid, "0" * 64, "image/png", 1)
    src.close()
    path = backup(_settings(), tmp_path / "src.db", tmp_path / "src-files", tmp_path / "backups")
    assert path.parent == tmp_path / "backups"
    assert path.name.startswith("family-") and path.suffix == ".zip"
    with zipfile.ZipFile(path) as zf:
        assert f"files/{sha[:2]}/{sha}.jpg" in zf.namelist()
    out = capsys.readouterr().out
    assert str(path) in out and "WARNING: 1 file(s)" in out and "0" * 64 in out


def test_archive_holds_the_notes_and_the_dreams_as_markdown(db: Database, monkeypatch) -> None:
    _seed(db)
    assert notes_markdown(db, KYIV) == "" and dreams_markdown(db) == ""
    mid = db.insert_message("oleh", "oleh", "...")
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-09-13T09:00:00Z")
    db.save_notes("## Канікули Олі\n- осінні: 26.10–01.11\n", "anna")
    db.create_dream("Поїхати в Японію з Олею", created_by="anna", source_message_id=mid)
    done = db.create_dream("Пройти Camino de Santiago", created_by="oleh", source_message_id=mid)
    db.close_dream(done, "fulfilled")
    gone = db.create_dream("Купити яхту", created_by="oleh", source_message_id=mid)
    db.close_dream(gone, "dropped")
    assert notes_markdown(db, KYIV) == (
        "## Канікули Олі\n- осінні: 26.10–01.11\n\n---\nОновлено 13.09 12:00, anna\n"
    )
    assert dreams_markdown(db) == (  # no dates, as on the page
        "# Мрії\n- Поїхати в Японію з Олею (anna)\n\n"
        "# Здійснилось\n- Пройти Camino de Santiago (oleh)\n"
    )
    name, data, _ = build_archive(db, KYIV, now=datetime(2026, 9, 13, 3, 30, tzinfo=KYIV))
    with zipfile.ZipFile(BytesIO(data)) as zf:
        assert zf.namelist() == ["family-2026-09-13.db", "notes.md", "dreams.md"]
        assert zf.read("notes.md").decode().startswith("## Канікули Олі")
