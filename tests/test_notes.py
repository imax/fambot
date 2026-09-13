"""The one «Нотатки» page: a Markdown reference the LLM rewrites whole on request."""

from datetime import datetime

from fastapi.testclient import TestClient

from family_ea.context import build_context, notes_sections, search_notes
from family_ea.db import Database, Member
from family_ea.family import Family
from family_ea.files import files_of_kind
from family_ea.llm import LlmResult
from family_ea.ops import apply_ops
from family_ea.web import build_web
from tests.conftest import KYIV
from tests.test_files import _applied
from tests.test_web import _auth, _settings

PAGE = "## Канікули Олі\n\n| Коли | Що |\n|---|---|\n| 26.10–01.11 | осінні |\n"


def test_notes_versions(db: Database) -> None:
    assert db.current_notes() is None and db.notes_versions() == 0
    db.save_notes("## Канікули Олі\n- осінні: 26.10–01.11", "oleh")
    db.save_notes(PAGE, "anna")
    current = db.current_notes()
    assert current and current.text == PAGE and current.created_by == "anna"
    assert db.notes_versions() == 2


def test_notes_op_replaces_the_page_whole(db: Database, family: Family) -> None:
    mid = db.insert_message("oleh", "oleh", "...")

    def run(payload: list[dict]) -> list:
        result = LlmResult.model_validate({"reply": "Ок.", "notes": payload})
        return apply_ops(db, result, author_id="oleh", message_id=mid, family=family, tz=KYIV)

    assert LlmResult.model_validate({"reply": "Ок."}).notes == []
    applied = run([{"text": " ## Канікули Олі\r\n- осінні: 26.10–01.11 "}])
    assert [(a.kind, a.op, a.ok, a.note) for a in applied] == [("notes", "set", True, "")]
    current = db.current_notes()
    assert current and current.text == "## Канікули Олі\n- осінні: 26.10–01.11"
    assert current.created_by == "oleh"

    applied = run([{"text": "## Канікули Олі\n- осінні: 26.10–01.11"}, {"text": ""}])
    assert [(a.ok, a.note) for a in applied] == [(False, "unchanged"), (True, "cleared")]
    current = db.current_notes()
    assert current and current.text == "" and db.notes_versions() == 2


def test_context_shows_the_page_or_a_placeholder(
    db: Database, family: Family, oleh: Member, monkeypatch
) -> None:
    head = (
        "## Нотатки (notes: одна довідкова сторінка сім'ї в Markdown, ведеш ти; змінюється лише"
        " на явне прохання, повертай повний текст)\n"
    )
    now = datetime(2026, 9, 10, 8, 0, tzinfo=KYIV)
    ctx = build_context(db, family, now, oleh, "привіт")
    assert head + "поки порожньо\n" in ctx
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-09-09T12:00:00Z")
    db.save_notes(PAGE, "anna")
    ctx = build_context(db, family, now, oleh, "коли канікули?")
    assert head + "(оновлено 09.09 15:00, Анна)\n" + PAGE.strip() + "\n" in ctx
    db.save_notes("", "oleh")  # cleared: the placeholder again
    assert head + "поки порожньо\n" in build_context(db, family, now, oleh, "привіт")


def test_notes_web_page_renders_markdown_and_its_photos(db: Database, family: Family) -> None:
    # the photo the page was written from, and one that changed nothing («unchanged»)
    m1 = db.insert_message("oleh", "oleh", "розклад канікул, в нотатки", photo_file_id="f1")
    a1 = db.add_attachment(m1, "a" * 64, "image/jpeg", 3)
    nid = db.save_notes(PAGE + "\n- пункт <b>x</b>\n", "oleh")
    db.set_llm_result(m1, _applied(("notes", "set", nid, True)))
    m2 = db.insert_message("anna", "anna", "ось ще раз", photo_file_id="f2")
    db.add_attachment(m2, "b" * 64, "image/jpeg", 3)
    db.set_llm_result(m2, _applied(("notes", "set", None, False)))
    assert [a.id for a in files_of_kind(db, "notes")] == [a1]

    client = TestClient(build_web(_settings(), family, db))
    assert client.get("/notes").status_code == 401
    page = client.get("/notes", headers=_auth())
    assert page.status_code == 200 and 'class="current">Нотатки' in page.text
    assert "<h2>Канікули Олі</h2>" in page.text and "<td>26.10–01.11</td>" in page.text
    assert "&lt;b&gt;x&lt;/b&gt;" in page.text  # raw HTML from the model stays text
    assert "версія 1" in page.text and "Олег" in page.text
    assert "<h2>Фото</h2>" in page.text and ("a" * 64) in page.text
    assert ("b" * 64) not in page.text  # changed nothing: not a source


def test_notes_web_page_when_empty(db: Database, family: Family) -> None:
    client = TestClient(build_web(_settings(), family, db))
    page = client.get("/notes", headers=_auth())
    assert page.status_code == 200 and "поки порожньо" in page.text
    assert "<h2>Фото</h2>" not in page.text and ">Нотатки</a>" in page.text


def test_notes_search_finds_the_section_with_the_word(db: Database, family: Family) -> None:
    text = "Вступ без заголовка.\n\n" + PAGE + "\n## Авто\n\n- зимова гума в шиномонтажі\n"
    assert [sec.splitlines()[0] for sec in notes_sections(text)] == [
        "Вступ без заголовка.",
        "## Канікули Олі",
        "## Авто",
    ]
    assert [sec.splitlines()[0] for sec in search_notes(text, r"\b(?:гум)")] == ["## Авто"]
    assert search_notes(text, r"\b(?:котл)") == []
    assert notes_sections("") == []

    db.save_notes(text, "oleh")
    client = TestClient(build_web(_settings(), family, db))
    page = client.get("/", params={"q": "гуми"}, headers=_auth()).text  # inflected
    assert "<h2>Авто</h2>" in page and "Канікули" not in page
    assert 'href="/notes"' in page
    page = client.get("/", params={"q": "канікули"}, headers=_auth()).text
    assert "<td>26.10–01.11</td>" in page and "гума" not in page
