from family_ea.db import Database
from family_ea.family import Family
from family_ea.llm import LlmResult
from family_ea.ops import apply_ops, normalize_datetime
from tests.conftest import KYIV

SPEC_EXAMPLE = {
    "reply": "Записав.",
    "events": [
        {
            "op": "create",
            "text": "Стоматолог Олі",
            "who": "anna",
            "starts_at": "2026-09-10T15:30:00+03:00",
        },
        {"op": "cancel", "id": 9},
    ],
    "todos": [
        {"op": "create", "text": "Попрати форму Олі", "owner": "anna", "due": "2026-09-10"},
        {"op": "create", "text": "Купити лампочки", "owner": "anna"},
        {"op": "update", "id": 12, "due": "2026-09-23"},
        {"op": "close", "id": 7, "status": "done"},
    ],
}


def test_schema_accepts_spec_example() -> None:
    r = LlmResult.model_validate(SPEC_EXAMPLE)
    assert r.reply == "Записав."
    assert [e.op for e in r.events] == ["create", "cancel"]
    assert not hasattr(r, "journal")  # the notes went on 2026-09-12
    assert LlmResult.model_validate({"reply": "Ок."}).events == []
    assert r.todos[0].due == "2026-09-10" and r.todos[1].due == ""
    assert LlmResult.model_validate({"reply": "Ок."}).todos == []
    assert LlmResult.model_validate({"reply": "Ок."}).dreams == []


def test_today_op_replaces_a_board(db: Database, family: Family) -> None:
    mid = db.insert_message("oleh", "oleh", "...")

    def run(payload: list[dict]) -> list:
        result = LlmResult.model_validate({"reply": "Ок.", "today": payload})
        return apply_ops(db, result, author_id="oleh", message_id=mid, family=family, tz=KYIV)

    applied = run(
        [
            {"text": " сходити на НП, планка "},
            {"member": "anna", "text": "вода"},
            {"member": "nobody", "text": "x"},
        ]
    )
    assert [(a.kind, a.op, a.ok, a.note) for a in applied] == [
        ("today", "set", True, ""),
        ("today", "set", True, "for anna"),
        ("today", "set", False, "unknown member 'nobody'"),
    ]
    boards = db.current_today_lists()
    assert boards["oleh"].text == "сходити на НП, планка" and boards["oleh"].created_by == "oleh"
    assert boards["anna"].text == "вода" and boards["anna"].created_by == "oleh"

    applied = run([{"text": "сходити на НП, планка"}, {"member": "anna", "text": ""}])
    assert [(a.ok, a.note) for a in applied] == [(False, "unchanged"), (True, "for anna")]
    assert db.current_today_lists()["anna"].text == ""  # cleared


def test_normalize_datetime() -> None:
    assert normalize_datetime("2026-09-10T15:30:00+03:00", KYIV) == "2026-09-10T12:30:00Z"
    assert normalize_datetime("2026-09-10T15:30:00", KYIV) == "2026-09-10T12:30:00Z"
    assert normalize_datetime("2026-09-10T12:30:00Z", KYIV) == "2026-09-10T12:30:00Z"
    assert normalize_datetime("завтра", KYIV) is None
    assert normalize_datetime("", KYIV) is None


def test_apply_ops_spec_example(db: Database, family: Family) -> None:
    mid = db.insert_message("oleh", "oleh", "...")
    applied = apply_ops(
        db,
        LlmResult.model_validate(SPEC_EXAMPLE),
        author_id="oleh",
        message_id=mid,
        family=family,
        tz=KYIV,
    )
    by = {(a.kind, a.op): a for a in applied}
    assert by[("event", "create")].ok and db.get_event(1).starts_at == "2026-09-10T12:30:00Z"
    assert by[("event", "cancel")].ok is False  # id 9 never existed
    assert by[("todo", "update")].ok is False
    assert by[("todo", "close:done")].ok is False
    created = [a for a in applied if a.kind == "todo" and a.op == "create"]
    assert all(a.ok for a in created) and len(created) == 2
    open_items = db.open_todos()  # newest first
    assert [c.text for c in open_items] == ["Купити лампочки", "Попрати форму Олі"]
    assert open_items[1].owner == "anna" and open_items[1].due == "2026-09-10"
    assert open_items[0].due is None


def test_apply_ops_validates_and_updates(db: Database, family: Family) -> None:
    mid = db.insert_message("oleh", "oleh", "...")
    r = LlmResult.model_validate(
        {
            "reply": "",
            "todos": [
                {"op": "create", "text": "Щось", "owner": "olia", "due": "коли-небудь"},
                {"op": "create", "text": "   "},
            ],
        }
    )
    applied = apply_ops(db, r, author_id="oleh", message_id=mid, family=family, tz=KYIV)
    assert [a.ok for a in applied] == [True, False]
    first = applied[0]
    assert "unknown owner" in first.note and "bad due" in first.note
    c = db.get_todo(first.id)
    assert c and c.owner is None and c.due is None

    r2 = LlmResult.model_validate(
        {
            "reply": "",
            "todos": [
                {"op": "update", "id": c.id, "due": "2026-09-10", "owner": "oleh"},
                {"op": "close", "id": c.id},
                {"op": "update", "id": c.id, "text": "after close"},
            ],
        }
    )
    applied = apply_ops(db, r2, author_id="oleh", message_id=mid, family=family, tz=KYIV)
    assert [(a.op, a.ok) for a in applied] == [
        ("update", True),
        ("close:done", True),
        ("update", False),
    ]
    c = db.get_todo(c.id)
    assert c and c.due == "2026-09-10" and c.owner == "oleh" and c.status == "done"


def test_todo_deadline_moves(db: Database, family: Family) -> None:
    """«Замовити воду до п'ятниці», then «перенесли на наступний тиждень»: the deadline
    moves; a text-only update leaves it; a time of day in `due` is just its day."""
    mid = db.insert_message("oleh", "oleh", "...")

    def apply(ops: list[dict]) -> list:
        r = LlmResult.model_validate({"reply": "", "todos": ops})
        return apply_ops(db, r, author_id="oleh", message_id=mid, family=family, tz=KYIV)

    assert all(
        a.ok for a in apply([{"op": "create", "text": "Замовити воду", "due": "2026-09-11"}])
    )
    assert all(a.ok for a in apply([{"op": "update", "id": 1, "due": "2026-09-20"}]))
    t = db.get_todo(1)
    assert t and t.due == "2026-09-20"

    assert all(a.ok for a in apply([{"op": "update", "id": 1, "text": "Замовити воду й каву"}]))
    t = db.get_todo(1)
    assert t and t.due == "2026-09-20" and t.text == "Замовити воду й каву"

    (ok,) = apply([{"op": "update", "id": 1, "due": "2026-09-22T10:00:00+03:00"}])
    assert ok.ok  # a todo has no clock: the day is kept, the time goes
    t = db.get_todo(1)
    assert t and t.due == "2026-09-22"
    (bad,) = apply([{"op": "update", "id": 1, "due": "коли-небудь"}])
    assert bad.ok is False and "bad due" in bad.note  # nothing left to update
    t = db.get_todo(1)
    assert t and t.due == "2026-09-22"


def test_apply_item_ops(db: Database, family: Family) -> None:
    mid = db.insert_message("oleh", "oleh", "...")
    r = LlmResult.model_validate(
        {
            "reply": "",
            "items": [
                {
                    "op": "create",
                    "name": " Паспорт Олі ",
                    "owner": "Оля",
                    "place": "офіс",
                    "spot": "сейф",
                },
                {"op": "create", "name": "  "},
                {"op": "update", "id": 99, "place": "x"},
            ],
        }
    )
    applied = apply_ops(db, r, author_id="oleh", message_id=mid, family=family, tz=KYIV)
    assert [(a.ok, a.note) for a in applied] == [
        (True, ""),
        (False, "empty name"),
        (False, "not found or gone"),
    ]
    iid = applied[0].id or 0
    assert db.get_item(iid).name == "Паспорт Олі"

    r2 = LlmResult.model_validate(
        {
            "reply": "",
            "items": [
                {"op": "update", "id": iid, "place": "квартира"},  # the spot goes with the place
                {"op": "update", "id": iid},  # «ось ще фото»: a hit, the file lands under it
                {"op": "update", "id": iid, "place": "квартира"},  # nothing new
                {"op": "update", "id": iid, "name": "", "owner": " "},  # blanks mean «not given»
                {"op": "remove", "id": iid},
                {"op": "remove", "id": iid},
            ],
        }
    )
    applied = apply_ops(db, r2, author_id="anna", message_id=mid, family=family, tz=KYIV)
    assert [(a.ok, a.note) for a in applied] == [
        (True, "moved"),
        (True, "unchanged"),
        (True, "unchanged"),
        (True, "unchanged"),
        (True, ""),
        (False, "not found or already gone"),
    ]
    item = db.get_item(iid)
    assert item and item.place == "квартира" and item.spot is None and item.owner == "Оля"
    assert item.name == "Паспорт Олі" and item.removed_at


def test_apply_dream_ops(db: Database, family: Family) -> None:
    """«Мрію пройти Каміно»: a dream with its author; reworded, fulfilled, let go."""
    mid = db.insert_message("oleh", "oleh", "...")

    def apply(ops: list[dict], author: str = "oleh") -> list:
        r = LlmResult.model_validate({"reply": "", "dreams": ops})
        return apply_ops(db, r, author_id=author, message_id=mid, family=family, tz=KYIV)

    applied = apply(
        [
            {"op": "create", "text": " Пройти Camino de Santiago "},
            {"op": "create", "text": "  "},
            {"op": "update", "id": 99, "text": "x"},
            {"op": "update", "text": "no id"},
        ]
    )
    assert [(a.kind, a.op, a.ok, a.note) for a in applied] == [
        ("dream", "create", True, ""),
        ("dream", "create", False, "empty text"),
        ("dream", "update", False, "not found or not open"),
        ("dream", "update", False, "nothing to update"),
    ]
    did = applied[0].id or 0
    d = db.get_dream(did)
    assert d and d.text == "Пройти Camino de Santiago" and d.created_by == "oleh"

    applied = apply(
        [
            {"op": "update", "id": did, "text": "Пройти Camino de Santiago разом"},
            {"op": "update", "id": did},  # nothing given
            {"op": "close", "id": did},  # fulfilled by default
            {"op": "close", "id": did, "status": "dropped"},
        ],
        author="anna",
    )
    assert [(a.op, a.ok, a.note) for a in applied] == [
        ("update", True, ""),
        ("update", False, "nothing to update"),
        ("close:fulfilled", True, ""),
        ("close:dropped", False, "not found or not open"),
    ]
    d = db.get_dream(did)
    assert d and d.status == "fulfilled" and d.text == "Пройти Camino de Santiago разом"
    assert d.created_by == "oleh"  # the author stays whoever dreamt it up
