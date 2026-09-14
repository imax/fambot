"""Web view: what the system actually stored. Server-rendered; identity comes from the bot.

There is no password. `/web` in Telegram (and «Відкрити» under the digest and /today) sends
a member a link to `/login?t=…`; opening it sets a long-lived signed cookie. Read-only except
`/facts` and `/family`, the two things a human edits by hand, and three things about a
todo, all through the db methods the LLM ops use: done («☐»), the text («✎») and the
order of the undated ones (dragged), on the home page. Dreams (`/dreams`) and the notes
page (`/notes`, the LLM's Markdown rendered, its source photos under it) are only read.

`/chat` exists only when `build_web` gets a pipeline, which `python -m family_ea web` does
on the laptop: a message typed there goes through the very pipeline the bot runs, as the
logged-in member, and the page shows the reply and what was applied. Production runs
without it; the bot is the chat there.
"""

import json
import logging
import re
import tempfile
from datetime import datetime
from itertools import groupby
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from .auth import SESSION_TTL, sign, verify
from .config import Settings
from .context import (
    board_blocks,
    build_timeline,
    fmt_date,
    fmt_dt,
    fmt_due,
    fmt_event_when,
    search_notes,
    word_pattern,
)
from .db import Attachment, Database, Item, Member
from .family import Family
from .files import FileStore, files_for, files_of_kind
from .ical import event_ics, ics_filename, todo_ics
from .llm import Image
from .pipeline import Pipeline, llm_result_lines

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
SESSION_COOKIE = "session"
DONE_SHOWN = 10  # the «Зроблено» tail of the home page
CHAT_SHOWN = 30  # the tail of the member's chat on /chat
SHA256 = re.compile(r"[0-9a-f]{64}")
# The notes page as the LLM writes it: headings, lists, tables; raw HTML stays text.
MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("table")


class NotLoggedIn(Exception):
    """Rendered as a small page telling the person to ask the bot for a link."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def build_web(
    settings: Settings, family: Family, db: Database, pipeline: Pipeline | None = None
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    store = FileStore(settings.files_dir)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["chat_enabled"] = pipeline is not None
    templates.env.filters["dt"] = lambda iso: fmt_dt(iso, settings.tz)
    templates.env.filters["date"] = fmt_date
    templates.env.filters["day"] = lambda iso: fmt_dt(iso, settings.tz)[:5]
    templates.env.filters["due"] = fmt_due
    templates.env.filters["when"] = lambda e: fmt_event_when(e, settings.tz)
    templates.env.filters["person"] = family.display_name
    templates.env.filters["pretty_json"] = lambda s: (
        json.dumps(json.loads(s), ensure_ascii=False, indent=2) if s else ""
    )
    templates.env.filters["llm_lines"] = lambda s: llm_result_lines(s) if s else []

    def secret() -> str:
        if not settings.web_secret:
            raise HTTPException(status_code=503, detail="web auth not configured: set WEB_SECRET")
        return settings.web_secret

    def member_from_cookie(request: Request) -> Member | None:
        token = request.cookies.get(SESSION_COOKIE)
        if not settings.web_secret or not token:
            return None
        subject = verify(settings.web_secret, token, "session")
        return family.get(subject) if subject else None

    def authed(request: Request) -> Member:
        secret()
        member = member_from_cookie(request)
        if member is None:
            raise NotLoggedIn("Щоб увійти, напиши боту /web.", 401)
        return member

    def bearer_ok(request: Request) -> bool:
        """A `backup` token, signed by `pull` itself, sent as a bearer token."""
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and verify(secret(), token.strip(), "backup") is not None

    def backup_only(request: Request) -> None:
        if not bearer_ok(request):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.exception_handler(NotLoggedIn)
    async def not_logged_in(request: Request, exc: NotLoggedIn) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "login.html", {"message": exc.message}, status_code=exc.status_code
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/login")
    async def login(
        request: Request, t: str = "", next_path: Annotated[str, Query(alias="next")] = "/"
    ) -> Response:
        """The link the bot sent: set the session cookie and go where the link pointed.

        Someone already logged in on this browser stays who they are, whatever the link
        says; that is how the digest button keeps working after its link expired.
        """
        key = secret()
        target = next_path if next_path.startswith("/") and not next_path.startswith("//") else "/"
        if member_from_cookie(request) is not None:
            return RedirectResponse(target, status_code=303)
        subject = verify(key, t, "link")
        member = family.get(subject) if subject else None
        if member is None:
            raise NotLoggedIn("Посилання застаріло. Напиши боту /web, він дасть нове.", 403)
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            sign(key, "session", member.id, SESSION_TTL),
            max_age=int(SESSION_TTL.total_seconds()),
            httponly=True,
            secure=bool(settings.web_url and settings.web_url.startswith("https")),
            samesite="lax",
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request, member: Annotated[Member, Depends(authed)], q: str | None = None
    ) -> HTMLResponse:
        """The boards (the viewer's own first), the timeline, the last done todos;
        `?q=` searches instead: events, todos, items, and the sections of the notes page."""
        if q and q.strip():
            q = q.strip()
            pattern = word_pattern(q)
            items = db.search_items(pattern, limit=50) if pattern else []
            notes = db.current_notes()
            sections = search_notes(notes.text, pattern) if notes and pattern else []
            return templates.TemplateResponse(
                request,
                "search.html",
                {
                    "q": q,
                    "events": db.search_events(pattern) if pattern else [],
                    "todos": db.search_todos(pattern) if pattern else [],
                    "items": items,
                    "item_files": files_for(db, "item", items),
                    "notes": [MARKDOWN.render(sec) for sec in sections],
                },
            )
        now = datetime.now(settings.tz)
        timeline = build_timeline(
            db.planned_events(),
            db.open_todos(),
            db.pending_reminders(),
            now,
            family,
            projects=db.open_projects(),
        )
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "q": "",
                "timeline": timeline,
                "today": board_blocks(db.current_today_lists(), family, member.id, now),
                "remember": board_blocks(db.current_remember_lists(), family, member.id, now),
                "done": db.recent_done_todos(DONE_SHOWN),
            },
        )

    @app.get("/items", response_class=HTMLResponse, dependencies=[Depends(authed)])
    async def items_page(
        request: Request, place: str | None = None, owner: str | None = None
    ) -> HTMLResponse:
        """Where things are: places with counts and what changed lately; `?place=` (empty:
        no place known) lists a place by spot, `?owner=` one person's things."""
        if place is None and not owner:
            recent = db.recent_items(10)
            # The visual index: every photo once (one photo of a shelf makes several items),
            # with the items on it under it, in the place / spot / name order of the lists.
            everything = db.list_items()
            files = files_for(db, "item", everything)
            by_photo: dict[str, tuple[Attachment, list[Item]]] = {}
            for i in everything:
                for a in files.get(i.id, []):
                    if a.is_image:
                        by_photo.setdefault(a.sha256, (a, []))[1].append(i)
            gallery = list(by_photo.values())
            return templates.TemplateResponse(
                request,
                "items.html",
                {
                    "gallery": gallery,
                    "recent": recent,
                    "places": db.places(),
                    "item_files": {i.id: files[i.id] for i in recent if i.id in files},
                },
            )
        items = db.list_items(place=place, owner=owner)
        if place is not None:
            title = place or "Без місця"
            groups = [(spot, list(g)) for spot, g in groupby(items, key=lambda i: i.spot)]
        else:
            title = f"Речі: {owner}"
            groups = [(None, items)] if items else []
        return templates.TemplateResponse(
            request,
            "place.html",
            {"title": title, "groups": groups, "item_files": files_for(db, "item", items)},
        )

    @app.get("/items/{iid:int}", response_class=HTMLResponse, dependencies=[Depends(authed)])
    async def item_page(request: Request, iid: int) -> HTMLResponse:
        item = db.get_item(iid)
        if item is None:
            raise HTTPException(status_code=404, detail="no such item")
        return templates.TemplateResponse(
            request,
            "item.html",
            {
                "item": item,
                "history": db.item_history(iid),
                "files": files_for(db, "item", [item]).get(iid, []),
            },
        )

    @app.get("/dreams", response_class=HTMLResponse, dependencies=[Depends(authed)])
    async def dreams_page(request: Request) -> HTMLResponse:
        """The family's dreams, the latest first, then the ones that came true. No dates:
        a dream has none. Read-only: it is added, reworded, fulfilled or let go in the chat."""
        return templates.TemplateResponse(
            request,
            "dreams.html",
            {"dreams": db.open_dreams(), "fulfilled": db.fulfilled_dreams()},
        )

    @app.get("/notes", response_class=HTMLResponse, dependencies=[Depends(authed)])
    async def notes_page(request: Request) -> HTMLResponse:
        """The one reference page the LLM keeps, rendered from its Markdown, with the photos
        it was written from. Read-only: «запиши в нотатки …» in the chat changes it."""
        notes = db.current_notes()
        return templates.TemplateResponse(
            request,
            "notes.html",
            {
                "notes": notes,
                "html": MARKDOWN.render(notes.text) if notes and notes.text.strip() else "",
                "versions": db.notes_versions(),
                "files": files_of_kind(db, "notes"),
            },
        )

    @app.get("/files/{sha256}")
    async def file(request: Request, sha256: str) -> Response:
        """One stored file by content hash: for the pages (session cookie) and for `pull`
        (bearer token). The content never changes, so the browser may keep it for a year."""
        secret()
        if member_from_cookie(request) is None and not bearer_ok(request):
            raise NotLoggedIn("Щоб увійти, напиши боту /web.", 401)
        a = db.attachment_by_sha(sha256) if SHA256.fullmatch(sha256) else None
        if a is None or not store.has(a.sha256, a.mime):
            raise HTTPException(status_code=404, detail="no such file")
        return FileResponse(
            store.path(a.sha256, a.mime),
            media_type=a.mime,
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )

    @app.get("/files.json", dependencies=[Depends(backup_only)])
    async def files_index() -> list[dict]:
        """What `pull` mirrors: every stored file's hash, type and size."""
        return [
            {
                "sha256": a.sha256,
                "mime": a.mime,
                "size": a.size,
                "message_id": a.message_id,
                "created_at": a.created_at,
            }
            for a in db.list_attachments()
        ]

    def ics_response(data: bytes, text: str) -> Response:
        """Open the file and the phone calendar offers to add the event."""
        return Response(
            data,
            media_type="text/calendar; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{ics_filename(text)}"'},
        )

    @app.get("/events/{eid:int}.ics", dependencies=[Depends(authed)])
    async def event_ics_file(eid: int) -> Response:
        e = db.get_event(eid)
        if e is None:
            raise HTTPException(status_code=404, detail="no such event")
        return ics_response(event_ics(e), e.text)

    @app.get("/todos/{cid:int}.ics", dependencies=[Depends(authed)])
    async def todo_ics_file(cid: int) -> Response:
        c = db.get_todo(cid)
        if c is None or not c.has_due:
            raise HTTPException(status_code=404, detail="no such dated todo")
        return ics_response(todo_ics(c), c.text)

    @app.post("/todos/{cid:int}/done", dependencies=[Depends(authed)])
    async def todo_done(cid: int) -> Response:
        """«☐» tapped on the home page: the todo is done, through the same db method
        the LLM's close op uses. Dropping stays with the LLM; nothing reopens."""
        if not db.close_todo(cid, "done"):
            raise HTTPException(status_code=404, detail="no such open todo")
        return Response(status_code=204)

    @app.post("/todos/{cid:int}/text", dependencies=[Depends(authed)])
    async def todo_text(cid: int, text: Annotated[str, Form()] = "") -> Response:
        """The text after an edit in place on the home page; open todos only, through
        the same db method the LLM's update op uses. Closing stays with the LLM."""
        text = text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="empty text")
        if not db.update_todo(cid, text=text):
            raise HTTPException(status_code=404, detail="no such open todo")
        return Response(status_code=204)

    @app.post("/todos/order", dependencies=[Depends(authed)])
    async def todos_order(
        ids: Annotated[list[int], Form()], project: Annotated[str, Form()] = ""
    ) -> Response:
        """One undated list (a project's, or «Інше») after a drag: every id in its new
        place; a todo dragged in from another list goes into this list's project
        (`project`: its id, '' for none) through the db method the LLM's update op uses.
        With the text, the two things about a todo the web writes; the LLM never sets the
        order."""
        pid: int | None = None
        if project:
            found = db.get_project(int(project)) if project.isdigit() else None
            if found is None or not found.is_open:
                raise HTTPException(status_code=404, detail="no such open project")
            pid = found.id
        for tid in ids:
            todo = db.get_todo(tid)
            if todo is not None and todo.is_open and todo.project_id != pid:
                db.update_todo(tid, project_id=pid)
        db.reorder_todos(ids)
        return Response(status_code=204)

    @app.post("/projects/order", dependencies=[Depends(authed)])
    async def projects_order(ids: Annotated[list[int], Form()]) -> Response:
        """The project headings of the home page after a drag by «⋮⋮»: every project's id
        in its new place, saved for everyone. Nothing else about a project is done on the
        web."""
        db.reorder_projects(ids)
        return Response(status_code=204)

    @app.get("/facts", response_class=HTMLResponse, dependencies=[Depends(authed)])
    async def facts_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "facts.html",
            {"facts": db.current_facts(), "versions": db.facts_versions()},
        )

    @app.post("/facts", dependencies=[Depends(authed)])
    async def facts_save(text: Annotated[str, Form()] = "") -> RedirectResponse:
        text = text.replace("\r\n", "\n").strip()
        current = db.current_facts()
        if text != (current.text if current else ""):
            db.save_facts(text, "web")
        return RedirectResponse("/facts", status_code=303)

    @app.get("/family", response_class=HTMLResponse, dependencies=[Depends(authed)])
    async def family_page(
        request: Request, name: str = "", telegram_id: str = "", error: str = ""
    ) -> HTMLResponse:
        """`name` and `telegram_id` prefill the add form (the bot links here with them)."""
        return templates.TemplateResponse(
            request,
            "family.html",
            {
                "members": family.members,
                "admin_id": family.admin_telegram_id,
                "prefill": {"name": name, "telegram_id": telegram_id},
                "error": error,
            },
        )

    @app.post("/family", dependencies=[Depends(authed)])
    async def family_save(
        member_id: Annotated[str, Form(alias="id")] = "",
        name: Annotated[str, Form()] = "",
        telegram_id: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        """Add a member (no id) or update one (id given). Errors go back as `?error=`."""

        def failed(message: str) -> RedirectResponse:
            return RedirectResponse("/family?" + urlencode({"error": message}), status_code=303)

        tg_raw = telegram_id.strip()
        if tg_raw and not tg_raw.isdigit():
            return failed("Telegram id — це число.")
        tg = int(tg_raw) if tg_raw else None
        try:
            if member_id:
                if not family.update(member_id, name=name, telegram_id=tg):
                    raise HTTPException(status_code=404, detail="no such member")
            else:
                family.add(name, tg)
        except ValueError as exc:
            log.info("family form rejected: %s", exc)
            return failed("Не збережено: порожнє ім'я або такий Telegram id уже є.")
        return RedirectResponse("/family", status_code=303)

    @app.get("/messages", response_class=HTMLResponse, dependencies=[Depends(authed)])
    async def messages(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "messages.html", {"messages": db.list_messages(limit=200)}
        )

    if pipeline is not None:

        @app.get("/chat", response_class=HTMLResponse)
        async def chat_page(
            request: Request, member: Annotated[Member, Depends(authed)]
        ) -> HTMLResponse:
            """The tail of the member's chat, oldest first, what the LLM did under each
            message, and a form to send the next one."""
            mine = [m for m in db.list_messages(CHAT_SHOWN * 4) if m.chat_with == member.id]
            return templates.TemplateResponse(
                request,
                "chat.html",
                {"messages": list(reversed(mine[:CHAT_SHOWN])), "model": settings.llm_model},
            )

        @app.post("/chat")
        async def chat_send(
            member: Annotated[Member, Depends(authed)],
            text: Annotated[str, Form()] = "",
            photo: UploadFile | None = None,
        ) -> RedirectResponse:
            """One message through the pipeline as the logged-in member, a photo with it if
            one was attached; then back to the page, which shows the reply."""
            text = text.replace("\r\n", "\n").strip()
            image = None
            if photo is not None and photo.filename:
                data = await photo.read()
                if data:
                    image = Image(data, photo.content_type or "image/jpeg")
            if text or image is not None:
                await pipeline.handle(
                    member, text, photo=image, photo_file_id="web" if image else None
                )
            return RedirectResponse("/chat", status_code=303)

    @app.get("/backup.db", dependencies=[Depends(backup_only)])
    async def backup() -> Response:
        """The whole database as one consistent file; `python -m family_ea pull` fetches it
        with a `backup` token it signs itself, sent as a bearer token."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "family.db"
            db.backup_to(path)
            data = path.read_bytes()
        stamp = datetime.now(settings.tz).strftime("%Y-%m-%d-%H%M")
        return Response(
            data,
            media_type="application/vnd.sqlite3",
            headers={"Content-Disposition": f'attachment; filename="family-{stamp}.db"'},
        )

    return app
