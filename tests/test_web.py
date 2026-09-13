import json
import tempfile
from datetime import UTC, datetime, time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from family_ea.auth import BACKUP_TTL, LINK_TTL, SESSION_TTL, sign
from family_ea.config import Settings
from family_ea.db import Database, Member
from family_ea.family import Family
from family_ea.files import FileStore
from family_ea.web import build_web
from tests.conftest import KYIV

JPEG = b"\xff\xd8\xff\xe0not-really-a-jpeg"


def _settings(**kw) -> Settings:
    base = dict(
        telegram_token=None,
        anthropic_api_key=None,
        openai_api_key=None,
        database_path=":memory:",
        files_dir=Path(tempfile.mkdtemp(prefix="family-ea-files-")),
        admin_user_id=1,
        web_secret="s",
        web_url=None,
        llm_model="m",
        llm_effort="medium",
        digest_time=time(8, 30),
        port=8080,
        tz=KYIV,
    )
    base.update(kw)
    return Settings(**base)


def _auth(member: str = "oleh") -> dict[str, str]:
    """A request header carrying a valid session cookie for `member`."""
    return {"Cookie": f"session={sign('s', 'session', member, SESSION_TTL)}"}


def freeze_web_clock(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    """The home page places things relative to now; pin it so tests do not drift."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return now.astimezone(tz) if tz else now

    monkeypatch.setattr("family_ea.web.datetime", _Frozen)


def test_web_pages(db: Database, family: Family) -> None:
    mid = db.insert_message("oleh", "oleh", "Газовик Петро", tg_message_id=1)
    db.set_llm_result(mid, '{"output": {"reply": "Записав."}}')
    db.insert_message("bot", "oleh", "Записав.")
    db.create_todo(
        "Стоматолог",
        owner="anna",
        created_by="anna",
        source_message_id=mid,
        due="2000-01-01",
    )
    db.create_todo("Купити дітям взуття", owner=None, created_by="oleh", source_message_id=mid)
    db.create_event(
        "Колі до стоматолога",
        who="anna",
        created_by="anna",
        source_message_id=mid,
        date_from="2026-09-20",
    )
    iid = db.create_item(
        "Паспорт Олі",
        owner="Оля",
        place="квартира",
        spot="білий комод",
        note=None,
        created_by="oleh",
        source_message_id=mid,
    )
    db.create_item(
        "Мерч",
        owner=None,
        place=None,
        spot=None,
        note="худі 2025",
        created_by="anna",
        source_message_id=mid,
    )
    db.create_dream("Поїхати в Японію з Олею", created_by="anna", source_message_id=mid)
    done = db.create_dream("Пройти Camino de Santiago", created_by="oleh", source_message_id=mid)
    db.close_dream(done, "fulfilled")
    gone = db.create_dream("Купити яхту", created_by="oleh", source_message_id=mid)
    db.close_dream(gone, "dropped")
    client = TestClient(build_web(_settings(), family, db))

    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/").status_code == 401
    assert client.get("/", headers={"Cookie": "session=garbage"}).status_code == 401

    home = client.get("/", headers=_auth())
    assert home.status_code == 200
    assert "Стоматолог" in home.text and "Анна" in home.text
    assert ">Задачі</a>" in home.text and 'class="current">Задачі' in home.text
    assert ">Речі</a>" in home.text and ">Користувачі</a>" in home.text
    assert ">Мрії</a>" in home.text
    assert ">Нотатки</a>" in home.text  # the one page since 2026-09-13; /journal was the log
    dreams = client.get("/dreams", headers=_auth())
    assert dreams.status_code == 200 and 'class="current">Мрії' in dreams.text
    assert "Поїхати в Японію з Олею" in dreams.text and "Анна" in dreams.text
    assert "<h2>Здійснилось</h2>" in dreams.text and "Camino" in dreams.text
    assert "яхту" not in dreams.text  # let go: shown nowhere
    assert client.get("/dreams").status_code == 401
    assert client.get("/journal", headers=_auth()).status_code == 404
    items = client.get("/items", headers=_auth())
    assert items.status_code == 200 and 'class="current">Речі' in items.text
    assert "Паспорт Олі" in items.text and "квартира" in items.text and "Без місця" in items.text
    assert "<h2>Фото</h2>" not in items.text  # no photos yet: no index
    place = client.get("/items", params={"place": "квартира"}, headers=_auth()).text
    assert "<h3>білий комод</h3>" in place and "Мерч" not in place
    assert "Мерч" in client.get("/items", params={"place": ""}, headers=_auth()).text
    assert "Паспорт" in client.get("/items", params={"owner": "оля"}, headers=_auth()).text
    page = client.get(f"/items/{iid}", headers=_auth()).text
    assert "квартира / білий комод" in page and "з'явилось" in page and "Олег" in page
    assert client.get("/items/999", headers=_auth()).status_code == 404
    assert client.get("/items").status_code == 401
    assert "Паспорт Олі" in client.get("/", params={"q": "паспорта"}, headers=_auth()).text

    search = client.get("/", params={"q": "котл"}, headers=_auth())
    assert "нічого" in search.text  # nothing mentions "котл..."
    search = client.get("/", params={"q": "стомат"}, headers=_auth())
    assert "Стоматолог" in search.text and 'class="id"' not in search.text
    search = client.get("/", params={"q": "діти"}, headers=_auth())  # inflection
    assert "дітям взуття" in search.text and "Колі" not in search.text
    search = client.get("/", params={"q": "Коля"}, headers=_auth())
    assert "Колі до стоматолога" in search.text and "дітям" not in search.text
    assert "нічого" in client.get("/", params={"q": "що це"}, headers=_auth()).text

    messages = client.get("/messages", headers=_auth())
    assert "бот → Олег" in messages.text and "Записав." in messages.text


def test_web_files(db: Database, family: Family, tmp_path: Path) -> None:
    settings = _settings(files_dir=tmp_path / "files")
    sha = FileStore(settings.files_dir).put(JPEG, "image/jpeg")
    mid = db.insert_message("oleh", "oleh", "це в бардачку", photo_file_id="f")
    db.add_attachment(mid, sha, "image/jpeg", len(JPEG))
    iid = db.create_item(
        "Сервісна книжка",
        owner=None,
        place="авто",
        spot="бардачок",
        note=None,
        created_by="oleh",
        source_message_id=mid,
    )
    applied = [{"kind": "item", "op": "create", "id": iid, "ok": True}]
    db.set_llm_result(mid, json.dumps({"applied": applied}))
    lost = db.insert_message("anna", "anna", "", photo_file_id="g")
    db.add_attachment(lost, "0" * 64, "image/jpeg", 1)  # a row whose bytes are not on disk
    client = TestClient(build_web(settings, family, db))

    # the file itself: for a logged-in browser, cached for good; for pull, with its token
    r = client.get(f"/files/{sha}", headers=_auth())
    assert r.status_code == 200 and r.content == JPEG
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert client.get(f"/files/{sha}").status_code == 401
    assert client.get("/files/" + "1" * 64, headers=_auth()).status_code == 404
    assert client.get("/files/" + "0" * 64, headers=_auth()).status_code == 404
    assert client.get("/files/not-a-hash", headers=_auth()).status_code == 404
    bearer = {"Authorization": "Bearer " + sign("s", "backup", "cli", BACKUP_TTL)}
    assert client.get(f"/files/{sha}", headers=bearer).content == JPEG
    index = client.get("/files.json", headers=bearer)
    assert index.status_code == 200
    assert [(f["sha256"], f["mime"], f["size"]) for f in index.json()] == [
        (sha, "image/jpeg", len(JPEG)),
        ("0" * 64, "image/jpeg", 1),
    ]
    assert client.get("/files.json", headers=_auth()).status_code == 401  # a cookie is not enough
    assert client.get("/files.json").status_code == 401

    # a «Фото» block of thumbnails on the item page, apart from the text; a mark in item rows
    item = client.get(f"/items/{iid}", headers=_auth()).text
    photos = f'<div class="photos"><a href="/files/{sha}" data-image><img src='
    assert "<h2>Фото</h2>" in item and photos in item and "бардачок" in item
    assert item.index("<h2>Фото</h2>") < item.index("<h2>Історія</h2>")
    assert '<dialog class="lightbox">' in item and "showModal" in item
    assert "e.key === 'Escape'" in item
    gallery = client.get("/items", headers=_auth()).text  # the visual index, at the bottom
    assert "📎" in gallery
    assert gallery.index("<h2>Нещодавно змінені</h2>") < gallery.index("<h2>Фото</h2>")
    assert f'<a href="/files/{sha}" data-image><img src="/files/{sha}"' in gallery
    assert f'<span><a href="/items/{iid}">Сервісна книжка</a></span>' in gallery
    assert "📎" in client.get("/items", params={"place": "авто"}, headers=_auth()).text
    search = client.get("/", params={"q": "авто"}, headers=_auth()).text
    assert "Сервісна книжка" in search and "📎" in search
    assert "Документи" not in search and "Документи" not in item  # no such tab any more
    assert client.get("/documents", headers=_auth()).status_code == 404


def test_web_refuses_without_configured_auth(db: Database, family: Family) -> None:
    client = TestClient(build_web(_settings(web_secret=None), family, db))
    assert client.get("/", headers=_auth()).status_code == 503


def test_family_web_add_and_edit(db: Database, family: Family) -> None:
    client = TestClient(build_web(_settings(), family, db))
    page = client.get("/family", params={"name": "Оля", "telegram_id": "3"}, headers=_auth())
    assert page.status_code == 200
    assert 'value="Оля"' in page.text and "oleh" in page.text and "Анна" in page.text

    def post(data: dict[str, str]):
        return client.post("/family", data=data, headers=_auth(), follow_redirects=False)

    r = post({"name": " Оля ", "telegram_id": " 3 "})
    assert r.status_code == 303 and r.headers["location"] == "/family"
    assert family.by_telegram_id(3) == Member("olia", "Оля", 3)

    r = post({"id": "olia", "name": "Ольга", "telegram_id": ""})
    assert r.headers["location"] == "/family"
    assert family.get("olia") == Member("olia", "Ольга", None)

    assert "error=" in post({"name": "Дубль", "telegram_id": "1"}).headers["location"]
    assert "error=" in post({"name": "Хтось", "telegram_id": "abc"}).headers["location"]
    assert "error=" in post({"name": "", "telegram_id": "5"}).headers["location"]
    assert post({"id": "ghost", "name": "x"}).status_code == 404
    assert [m.id for m in family.members] == ["oleh", "anna", "olia"]

    page = client.get("/family", params={"error": "Тест"}, headers=_auth())
    assert 'class="error">Тест' in page.text
    assert client.post("/family", data={"name": "x"}).status_code == 401


def test_web_ics(db: Database, family: Family) -> None:
    mid = db.insert_message("anna", "anna", "...")
    dated = db.create_todo(
        "Стоматолог", owner="anna", created_by="anna", source_message_id=mid, due="2026-09-10"
    )
    undated = db.create_todo("Без дати", owner=None, created_by="anna", source_message_id=mid)
    client = TestClient(build_web(_settings(), family, db))

    home = client.get("/", headers=_auth())
    assert f'href="/todos/{dated}.ics"' in home.text
    assert f'href="/todos/{undated}.ics"' not in home.text

    ics = client.get(f"/todos/{dated}.ics", headers=_auth())
    assert ics.status_code == 200 and ics.headers["content-type"].startswith("text/calendar")
    assert "DTSTART;VALUE=DATE:20260910" in ics.text
    assert 'filename="stomatoloh.ics"' in ics.headers["content-disposition"]
    assert client.get(f"/todos/{undated}.ics", headers=_auth()).status_code == 404
    assert client.get("/todos/999.ics", headers=_auth()).status_code == 404
    assert client.get(f"/todos/{dated}.ics").status_code == 401


def test_login_from_a_bot_link(db: Database, family: Family) -> None:
    app = build_web(_settings(web_url="https://ea.example"), family, db)
    client = TestClient(app, base_url="https://testserver")  # a Secure cookie needs https
    r = client.get("/")  # not logged in: a page for a person, not JSON
    assert r.status_code == 401 and "напиши боту /web" in r.text

    token = sign("s", "link", "anna", LINK_TTL)
    r = client.get("/login", params={"t": token, "next": "/facts"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/facts"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("session=") and "HttpOnly" in cookie and "Secure" in cookie
    assert "Max-Age=31536000" in cookie and "SameSite=lax" in cookie
    assert client.get("/").status_code == 200  # the client keeps the cookie
    assert "Анна" in client.get("/family").text

    # once the browser is logged in, a stale or foreign link just opens the page
    r = client.get("/login", params={"t": "garbage"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_login_rejects_bad_links(db: Database, family: Family) -> None:
    client = TestClient(build_web(_settings(), family, db))
    stale = sign("s", "link", "anna", LINK_TTL, now=datetime(2000, 1, 1, tzinfo=UTC))
    bad = [
        "",
        "garbage",
        stale,
        sign("s", "session", "anna", SESSION_TTL),  # a cookie is not a link
        sign("s", "link", "ghost", LINK_TTL),  # not in the family
        sign("other", "link", "anna", LINK_TTL),
    ]
    for t in bad:
        r = client.get("/login", params={"t": t})
        assert r.status_code == 403 and "застаріло" in r.text, t
    assert not client.cookies
    assert client.get("/", headers=_auth("ghost")).status_code == 401
    link_as_cookie = {"Cookie": f"session={sign('s', 'link', 'anna', LINK_TTL)}"}
    assert client.get("/", headers=link_as_cookie).status_code == 401
    # no open redirects
    good = sign("s", "link", "anna", LINK_TTL)
    r = client.get("/login", params={"t": good, "next": "//evil.example/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
