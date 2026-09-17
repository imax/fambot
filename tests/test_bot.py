from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

from telegram import Chat, Message, Update, User

from family_ea.auth import verify
from family_ea.bot import build_bot, family_filter, help_text, login_link, open_keyboard
from family_ea.db import Database, Member
from family_ea.family import Family
from family_ea.pipeline import Pipeline
from tests.conftest import KYIV
from tests.test_web import _settings


def _update(user_id: int) -> Update:
    user = User(id=user_id, first_name="X", is_bot=False)
    chat = Chat(id=user_id, type="private")
    msg = Message(message_id=1, date=datetime.now(UTC), chat=chat, from_user=user, text="hi")
    return Update(update_id=1, message=msg)


def test_family_filter_is_live(db: Database) -> None:
    fam = Family(db, admin_telegram_id=1)
    fam.add("Анна", 2)
    allowed = family_filter(fam)
    assert allowed.check_update(_update(1))  # admin, before their row exists
    assert allowed.check_update(_update(2))
    assert not allowed.check_update(_update(3))
    assert (~allowed).check_update(_update(3))
    fam.add("Оля", 3)  # added on the web while the bot runs
    assert allowed.check_update(_update(3))


def test_build_bot_registers_handlers_and_digest_job(db: Database, family: Family) -> None:
    pipeline = Pipeline(db, family, None, KYIV)  # type: ignore[arg-type]
    app = build_bot(_settings(telegram_token="123:abc"), family, db, pipeline, None)
    assert pipeline.announce is not None  # a new event is told to the others through the bot
    assert len(app.handlers[0]) == 12
    assert app.job_queue
    assert sorted(j.name for j in app.job_queue.jobs()) == ["digest", "nudges", "reminders"]


def test_login_link_signs_the_member_in() -> None:
    settings = _settings(web_url="https://ea.example/")
    member = Member("anna", "Анна", 2)
    link = login_link(settings, member)
    assert link and link.startswith("https://ea.example/login?t=")
    token = parse_qs(urlparse(link).query)["t"][0]
    assert verify("s", token, "link") == "anna"
    assert verify("s", token, "session") is None  # a link cannot be pasted in as a cookie
    with_next = login_link(settings, member, "/facts")
    assert with_next and parse_qs(urlparse(with_next).query)["next"] == ["/facts"]
    kb = open_keyboard(link)
    assert kb and kb.inline_keyboard[0][0].text == "Відкрити"
    assert kb.inline_keyboard[0][0].url == link
    assert open_keyboard(None) is None
    assert login_link(_settings(web_url=None), member) is None
    assert login_link(_settings(web_url="https://x", web_secret=None), member) is None


def test_help_text_lists_commands() -> None:
    text = help_text(_settings())
    assert "08:30" in text and "/web — " in text and "/help — " in text
    assert "10:00" in text and "«Завтра»" in text
    assert "Фото" in text and "під річчю" in text
    assert "/start" not in text
