from datetime import datetime

import pytest

from family_ea.context import (
    Agenda,
    bucket_todos,
    build_context,
    digest_text,
    fmt_due,
    render_digest,
    stale_label,
    stems,
    today_blocks,
    today_lines,
    word_pattern,
)
from family_ea.db import Database, Member, TodayList, Todo
from family_ea.family import Family
from tests.conftest import KYIV


def _c(id: int, **kw) -> Todo:
    base = dict(
        text=f"c{id}",
        owner=None,
        status="open",
        due=None,
        created_by="oleh",
        created_at="2026-09-01T00:00:00Z",
        source_message_id=1,
        closed_at=None,
    )
    base.update(kw)
    return Todo(id=id, **base)


def test_bucket_todos() -> None:
    now = datetime(2026, 9, 10, 8, 0, tzinfo=KYIV)
    items = [
        _c(1, due="2026-09-10"),  # today
        _c(2, due="2026-09-09"),  # yesterday -> overdue
        _c(3, due="2026-09-11"),  # tomorrow -> later
        _c(4, due="2026-09-20"),  # later
        _c(5, due="2026-09-01"),  # long overdue: first
        _c(6),  # no deadline -> open
        _c(7, status="done", due="2026-09-10"),  # closed, ignored
    ]
    b = bucket_todos(items, now)
    assert [c.id for c in b.today] == [1]
    assert [c.id for c in b.overdue] == [5, 2]
    assert [c.id for c in b.later] == [3, 4]
    assert [c.id for c in b.open] == [6]


def test_fmt_due() -> None:
    assert fmt_due(_c(1, due="2026-09-08")) == "08.09"
    assert fmt_due(_c(1)) == ""


def test_search_stems() -> None:
    # endings go, long words keep a 5-char prefix, function words and digits drop
    assert stems("Хто ремонтував котел?") == ["ремон", "котел"]
    assert stems("ок") == []
    assert stems('він сказав "привіт" 12345') == ["сказа", "приві"]
    assert stems("діти дітям дітьми") == ["діт"]
    assert stems("Коля Колі Колею коли") == ["кол"]  # «коли» is a function word
    assert stems("Марія Марії Марією") == ["марі"]
    assert stems("Оля Олю кум кумом") == ["ол", "кум"]
    assert stems("газовика стоматологу") == ["газов", "стома"]
    assert word_pattern("діти, Коля?") == r"\b(?:діт|кол)"
    assert word_pattern("що це") is None


def test_render_digest_caps_open_list(family: Family) -> None:
    now = datetime(2026, 9, 10, 8, 0, tzinfo=KYIV)
    b = bucket_todos([_c(i) for i in range(1, 9)], now)
    text = render_digest(Agenda(), b, family, KYIV, max_open=5)
    assert "і ще 3" in text
    assert "Сьогодні" not in text


def test_digest_text(family: Family) -> None:
    now = datetime(2026, 9, 10, 8, 30, tzinfo=KYIV)
    items = [
        _c(1, text="Стоматолог", owner="anna", due="2026-09-10"),
        _c(2, text="Поговорити з Марією", due="2026-09-09"),
        _c(3, text="Купити лампочки"),
    ]
    b = bucket_todos(items, now)
    text = digest_text(Agenda(), b, family, KYIV)
    assert text == (
        "Задачі на сьогодні:\n- Стоматолог (Анна, до 10.09)\n"
        "Прострочено:\n- Поговорити з Марією (до 09.09)"
    )
    assert "Купити лампочки" not in text and "[#" not in text  # no undated ones, no ids

    only_undated = bucket_todos([_c(3)], now)
    assert digest_text(Agenda(), only_undated, family, KYIV) is None  # nothing to say
    assert digest_text(Agenda(), bucket_todos([], now), family, KYIV) is None


def test_today_blocks_and_lines(family: Family) -> None:
    now = datetime(2026, 9, 11, 8, 0, tzinfo=KYIV)
    lists = {
        "oleh": TodayList(1, "oleh", "сходити на НП\nпланка\n", "2026-09-10T18:00:00Z", "oleh"),
        "anna": TodayList(2, "anna", "вода", "2026-09-11T04:30:00Z", "anna"),  # this morning
    }
    blocks = today_blocks(lists, family, "anna", now)  # the viewer's own first
    assert [(b.member, b.text, b.stale) for b in blocks] == [
        ("anna", "вода", ""),
        ("oleh", "сходити на НП\nпланка", "вчора"),
    ]
    assert today_lines(blocks, "anna") == [
        "На сьогодні (твоє):",
        "- вода",
        "На сьогодні (Олег, оновлено вчора):",
        "- сходити на НП",
        "- планка",
    ]
    assert stale_label("2026-09-01T10:00:00Z", now) == "01.09"
    empty = today_blocks({}, family, "oleh", now)
    assert [(b.member, b.text, b.stale) for b in empty] == [("oleh", "", ""), ("anna", "", "")]
    assert today_lines(empty, "oleh") == []

    head = today_lines(blocks, "anna")[:2]
    nothing = bucket_todos([], now)
    assert digest_text(Agenda(), nothing, family, KYIV, today=head) == (
        "На сьогодні (твоє):\n- вода"
    )
    assert digest_text(Agenda(), nothing, family, KYIV, today=[]) is None


def test_context_shows_today_boards(
    db: Database, family: Family, oleh: Member, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-09-10T05:12:00Z")
    db.save_today_list("oleh", "сходити на НП\nпланка", "oleh")
    now = datetime(2026, 9, 10, 8, 0, tzinfo=KYIV)
    ctx = build_context(db, family, now, oleh, "привіт")
    assert (
        "## Списки на сьогодні (today: дошка кожного, змінюється лише на явне прохання)\n"
        "- oleh (Олег), оновлено 10.09 08:12:\n  сходити на НП\n  планка\n"
        "- anna (Анна): порожньо\n" in ctx
    )


def test_build_context_sections(
    db: Database, family: Family, oleh: Member, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-09-09T12:00:00Z")
    mid = db.insert_message("oleh", "oleh", "Газовик Петро ремонтував котел")
    db.create_item(
        "Паспорт Олі",
        owner="Оля",
        place="квартира",
        spot="білий комод",
        note=None,
        created_by="oleh",
        source_message_id=mid,
    )
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-07-01T12:00:00Z")
    db.create_item(  # old: search only
        "Ключі від офісу",
        owner=None,
        place="офіс",
        spot="сейф",
        note="запасні",
        created_by="anna",
        source_message_id=mid,
    )
    monkeypatch.setattr("family_ea.db.utc_now_iso", lambda: "2026-09-09T12:00:00Z")
    db.create_todo(
        "Поговорити з пані Марією",
        owner="oleh",
        created_by="oleh",
        source_message_id=mid,
        due="2026-09-10",
    )
    db.insert_message("bot", "oleh", "Записав.")
    db.create_dream("Поїхати в Японію з Олею", created_by="anna", source_message_id=mid)
    done = db.create_dream("Пройти Camino de Santiago", created_by="oleh", source_message_id=mid)
    db.close_dream(done, "fulfilled")
    now = datetime(2026, 9, 10, 8, 0, tzinfo=KYIV)
    ctx = build_context(db, family, now, oleh, "Хто ремонтував котел?")
    assert "2026-09-10 08:00 (Europe/Kyiv), четвер" in ctx
    assert "## Сім'я (пишуть боту; решта людей — у фактах)\n- oleh: Олег" in ctx
    assert "[#1] Поговорити з пані Марією (Олег, до 10.09)" in ctx
    assert "Задачі на сьогодні:\n- [#1]" in ctx
    assert "## Події (минулі за 7 днів і всі майбутні)\nнемає" in ctx
    assert "Нотатки" not in ctx and "journal" not in ctx
    assert (
        "## Мрії (dreams: спільний список, хто додав; здійснені лише на вебі)\n"
        "- [мрія #1] Поїхати в Японію з Олею (Анна)\n" in ctx
    )
    assert "Camino" not in ctx  # fulfilled: on the web only
    assert "## Речі (items), змінені за останні 2 дні\n- [#1] Паспорт Олі (Оля) → квартира" in ctx
    assert "## Речі, схожі на повідомлення\nнемає" in ctx
    assert "## Відомі місця (place), де лежать речі\nквартира (1), офіс (1)" in ctx
    ctx2 = build_context(db, family, now, oleh, "Де ключі від офісу?")
    assert "## Речі, схожі на повідомлення\n- [#2] Ключі від офісу → офіс / сейф; запасні" in ctx2
    assert "[09.09 15:00] бот → Олег: Записав." in ctx
    assert ctx.rstrip().endswith("від oleh (Олег):\nХто ремонтував котел?")
