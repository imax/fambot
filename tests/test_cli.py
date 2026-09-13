"""`GET /backup.db`, `pull` and `log`: getting the production database onto a laptop."""

import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from family_ea.auth import BACKUP_TTL, sign, verify
from family_ea.db import Database
from family_ea.family import Family
from family_ea.files import FileStore
from family_ea.main import files_next_to, llm_result_lines, pull, show_log, web_login
from family_ea.web import build_web
from tests.test_web import JPEG, _auth, _settings

LLM_RESULT = (
    '{"model": "m", "usage": {"input_tokens": 10, "output_tokens": 2},'
    ' "output": {"reply": "Записав.", "todos": [{"op": "create", "text": "Стоматолог",'
    ' "owner": "anna", "due": "2000-01-01"}],'
    ' "dreams": [{"op": "close", "id": 3, "status": "fulfilled"}], "notes": [{"text": "## X"}]},'
    ' "applied": [{"kind": "todo", "op": "create", "id": 1, "ok": true, "note": ""}]}'
)

OP_LINE = "todo create: text='Стоматолог', owner='anna', due='2000-01-01'"
DREAM_LINE = "dream close: id=3, status='fulfilled'"
NOTES_LINE = "notes None: text='## X'"


def _bearer() -> dict[str, str]:
    return {"Authorization": "Bearer " + sign("s", "backup", "cli", BACKUP_TTL)}


def _seed(db: Database) -> None:
    mid = db.insert_message("oleh", "oleh", "Стоматолог завтра о 15:30", tg_message_id=1)
    db.set_llm_result(mid, LLM_RESULT)
    db.insert_message("bot", "oleh", "Записав.")
    db.create_todo("Стоматолог", owner="anna", created_by="oleh", source_message_id=mid)


def _rows(path: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT user_id, raw_text FROM messages ORDER BY id").fetchall()
    finally:
        conn.close()


def test_backup_endpoint_is_a_consistent_sqlite_file(
    db: Database, family: Family, tmp_path: Path
) -> None:
    _seed(db)
    client = TestClient(build_web(_settings(), family, db))
    assert client.get("/backup.db").status_code == 401
    assert client.get("/backup.db", headers=_auth()).status_code == 401  # a cookie is not enough
    assert client.get("/backup.db", headers={"Authorization": "Bearer nope"}).status_code == 401

    response = client.get("/backup.db", headers=_bearer())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.sqlite3")
    assert 'filename="family-' in response.headers["content-disposition"]
    copy = tmp_path / "copy.db"
    copy.write_bytes(response.content)
    assert _rows(copy) == [("oleh", "Стоматолог завтра о 15:30"), ("bot", "Записав.")]
    # a snapshot, not a link: writing to the copy leaves the live database alone
    sqlite3.connect(copy).execute("DELETE FROM messages").connection.commit()
    assert len(db.list_messages()) == 2


def test_pull_downloads_the_snapshot_and_drops_sftp_leftovers(
    db: Database, family: Family, tmp_path: Path
) -> None:
    _seed(db)
    server = _settings(files_dir=tmp_path / "server-files")
    sha = FileStore(server.files_dir).put(JPEG, "image/jpeg")
    mid = db.insert_message("anna", "anna", "скан", photo_file_id="f")
    db.add_attachment(mid, sha, "image/jpeg", len(JPEG))
    app = build_web(server, family, db)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        with TestClient(app) as client:
            r = client.get(request.url.path, headers=dict(request.headers))
        return httpx.Response(r.status_code, content=r.content, headers=dict(r.headers))

    dest = tmp_path / "data" / "prod.db"
    dest.parent.mkdir()
    Path(f"{dest}-wal").write_bytes(b"stale")  # what `fly ssh sftp get` used to leave behind
    pull(
        _settings(web_url="https://ea.example/"),
        dest,
        transport=httpx.MockTransport(handler),
    )
    assert [str(r.url) for r in seen] == [
        "https://ea.example/backup.db",
        "https://ea.example/files.json",
        f"https://ea.example/files/{sha}",
    ]
    assert all(r.headers["authorization"].startswith("Bearer ") for r in seen)
    assert _rows(dest)[0] == ("oleh", "Стоматолог завтра о 15:30")
    assert not Path(f"{dest}-wal").exists()
    mirrored = files_next_to(dest) / sha[:2] / f"{sha}.jpg"
    assert mirrored == tmp_path / "data" / "prod-files" / sha[:2] / f"{sha}.jpg"
    assert mirrored.read_bytes() == JPEG

    # the next pull fetches the database again but no file it already has
    seen.clear()
    pull(_settings(web_url="https://ea.example/"), dest, transport=httpx.MockTransport(handler))
    assert [r.url.path for r in seen] == ["/backup.db", "/files.json"]


def test_pull_needs_a_url_and_the_secret(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="WEB_URL"):
        pull(_settings(web_url=None), tmp_path / "prod.db")
    with pytest.raises(SystemExit, match="WEB_SECRET"):
        pull(_settings(web_url="https://ea.example", web_secret=None), tmp_path / "prod.db")


def test_llm_result_lines() -> None:
    assert llm_result_lines(LLM_RESULT) == [
        "m: 10 in, 2 out",
        OP_LINE,
        DREAM_LINE,
        NOTES_LINE,
        "[ok] todo create #1",
    ]
    assert llm_result_lines('{"error": "boom"}') == ["error: boom"]
    cached = '{"model": "m", "usage": {"input_tokens": 5, "output_tokens": 1,'
    cached += ' "cache_read_input_tokens": 2000}}'
    assert llm_result_lines(cached) == ["m: 5 in, 1 out, 2000 from cache"]


def test_show_log_prints_messages_with_ops(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    path = tmp_path / "family.db"
    db = Database(path)
    Family(db, admin_telegram_id=1).add("Олег", 1, member_id="oleh")
    _seed(db)
    db.close()

    show_log(_settings(database_path=path), path, last=10)
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("Олег: Стоматолог завтра о 15:30")
    assert out[1:6] == [
        "    m: 10 in, 2 out",
        f"    {OP_LINE}",
        f"    {DREAM_LINE}",
        f"    {NOTES_LINE}",
        "    [ok] todo create #1",
    ]
    assert out[6].endswith("bot -> Олег: Записав.")


def test_web_login_picks_the_admin_or_the_named_member(db: Database, family: Family) -> None:
    settings = _settings(admin_user_id=2, port=8765)  # anna's telegram id in the fixture
    member, link = web_login(settings, family, None)
    assert member.id == "anna" and link.startswith("http://127.0.0.1:8765/login?t=")
    token = link.split("t=")[1]
    assert verify("s", token, "link") == "anna"
    assert web_login(settings, family, "oleh")[0].id == "oleh"
    with pytest.raises(SystemExit):
        web_login(settings, family, "nobody")
    with pytest.raises(SystemExit):
        web_login(_settings(web_secret=None), family, None)
