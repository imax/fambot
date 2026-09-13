"""Process entry points: `serve` (bot + web in one event loop), `chat` (local REPL),
`pull` (snapshot of the deployed database) and `log` (messages with what the LLM did)."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import httpx
import uvicorn

from .auth import BACKUP_TTL, LINK_TTL, sign
from .backup import write_archive
from .bot import build_bot
from .config import Settings
from .context import fmt_dt
from .db import Database, Member
from .family import Family
from .files import FileStore, sha256_hex
from .llm import Image, Llm
from .pipeline import Pipeline
from .transcribe import Transcriber
from .web import build_web

log = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for name in ("httpx", "httpx2"):  # the anthropic SDK logs requests via httpx2
        logging.getLogger(name).setLevel(logging.WARNING)


_IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def build_pipeline(settings: Settings, db: Database, family: Family) -> Pipeline:
    llm = Llm(settings.llm_model, settings.llm_effort, api_key=settings.anthropic_api_key)
    return Pipeline(db, family, llm, settings.tz, store=FileStore(settings.files_dir))


async def serve(settings: Settings) -> None:
    settings.require("telegram_token", "anthropic_api_key", "web_secret")
    if settings.admin_user_id is None:
        # Bootstrap mode: nobody is let in, but the bot answers strangers with their id.
        log.warning("ADMIN_USER_ID is not set: write to the bot to learn your id, then set it")
    db = Database(settings.database_path)
    family = Family(db, settings.admin_user_id)
    pipeline = build_pipeline(settings, db, family)
    transcriber = Transcriber(settings.openai_api_key) if settings.openai_api_key else None

    tg_app = build_bot(settings, family, db, pipeline, transcriber)
    web_app = build_web(settings, family, db)
    server = uvicorn.Server(
        uvicorn.Config(web_app, host="0.0.0.0", port=settings.port, log_level="info")
    )

    log.info(
        "starting: model=%s db=%s members=%s voice=%s digest=%s %s",
        settings.llm_model,
        settings.database_path,
        [p.id for p in family.members],
        "on" if transcriber else "off",
        settings.digest_time.strftime("%H:%M"),
        settings.tz.key,
    )
    async with tg_app:
        await tg_app.start()
        assert tg_app.updater
        await tg_app.updater.start_polling()
        try:
            await server.serve()  # returns on SIGINT/SIGTERM
        finally:
            await tg_app.updater.stop()
            await tg_app.stop()
            db.close()


def web_login(settings: Settings, family: Family, as_user: str | None) -> tuple[Member, str]:
    """Who `web` logs in and the link that does it: `as_user`, else the admin, else the
    first member. The link is local (127.0.0.1:PORT), not WEB_URL."""
    settings.require("web_secret")
    assert settings.web_secret
    if as_user:
        member = family.get(as_user)
        if member is None:
            known = [p.id for p in family.members]
            raise SystemExit(f"unknown family member: {as_user} (have: {known})")
    else:
        admin = family.by_telegram_id(settings.admin_user_id) if settings.admin_user_id else None
        member = admin or (family.members[0] if family.members else None)
        if member is None:
            raise SystemExit("no members yet: add one with `chat --as id --name Name`")
    token = sign(settings.web_secret, "link", member.id, LINK_TTL)
    return member, f"http://127.0.0.1:{settings.port}/login?t={token}"


async def web(settings: Settings, as_user: str | None = None) -> None:
    """The web view alone on the local db, no bot: a look at a change in a browser before
    it ships (`make local` first for production data). Prints a login link, then serves."""
    db = Database(settings.database_path)
    family = Family(db, settings.admin_user_id)
    member, link = web_login(settings, family, as_user)
    print(
        f"web on http://127.0.0.1:{settings.port}, db={settings.database_path};"
        f" log in as {member.id} ({member.name}):\n{link}",
        flush=True,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            build_web(settings, family, db),
            host="127.0.0.1",
            port=settings.port,
            log_level="warning",
        )
    )
    try:
        await server.serve()
    finally:
        db.close()


async def chat(settings: Settings, as_user: str, name: str | None = None) -> None:
    """Talk to the pipeline from the terminal, no Telegram. Same database, same code.

    `name` creates the member when `as_user` does not exist yet (handy on a fresh db).
    """
    settings.require("anthropic_api_key")
    db = Database(settings.database_path)
    family = Family(db, settings.admin_user_id)
    person = family.get(as_user)
    if person is None:
        if name is None:
            known = [p.id for p in family.members]
            raise SystemExit(
                f"unknown family member: {as_user} (have: {known});"
                f" add with --name or on the web at /family"
            )
        person = family.add(name, member_id=as_user)
        print(f"created member {person.id} ({person.name})")
    pipeline = build_pipeline(settings, db, family)
    print(
        f"chatting as {person.name}; db={settings.database_path}; empty line to quit;"
        " `/photo path/to.jpg caption` sends a photo"
    )
    while True:
        try:
            text = await asyncio.to_thread(input, f"{person.id}> ")
        except (EOFError, KeyboardInterrupt):
            break
        if not text.strip():
            break
        photo = None
        if text.startswith("/photo "):
            path, _, text = text[len("/photo ") :].strip().partition(" ")
            photo = Image(
                Path(path).read_bytes(), _IMAGE_TYPES.get(Path(path).suffix.lower(), "image/jpeg")
            )
        outcome = await pipeline.handle(
            person, text, photo=photo, photo_file_id="local" if photo else None
        )
        print(f"bot> {outcome.reply}")
        for a in outcome.applied:
            flag = "ok" if a.ok else "SKIPPED"
            print(f"     [{flag}] {a.kind} {a.op} #{a.id} {a.note}")
    db.close()


def pull(
    settings: Settings,
    dest: Path,
    url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Path:
    """Download a consistent snapshot of the deployed database (its `GET /backup.db`) and
    the files it refers to that are not here yet (`GET /files.json`, then each missing
    `GET /files/<sha256>`) into `<dest stem>-files/` next to it.

    Replaces `fly ssh sftp get`, which copies the main file but not the -wal file with
    the latest writes. `transport` is for tests.
    """
    base = (url or settings.web_url or "").rstrip("/")
    if not base:
        raise SystemExit("where is the web view? set WEB_URL in .env or pass --url")
    settings.require("web_secret")
    assert settings.web_secret
    token = sign(settings.web_secret, "backup", "cli", BACKUP_TTL)
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=60, transport=transport) as client:
        response = client.get(f"{base}/backup.db", headers=headers)
        response.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(response.content)
        for sidecar in (Path(f"{dest}-wal"), Path(f"{dest}-shm")):
            sidecar.unlink(missing_ok=True)  # leftovers of an sftp copy would apply to this file
        print(f"{len(response.content)} bytes -> {dest}")
        _pull_files(client, base, headers, FileStore(dest.with_name(f"{dest.stem}-files")))
    return dest


def _pull_files(client: httpx.Client, base: str, headers: dict[str, str], store: FileStore) -> None:
    """Mirror the deployed files: fetch by hash what the local store lacks. Nothing here is
    deleted or rewritten, so the directory only grows; rsync would do the same."""
    listing = client.get(f"{base}/files.json", headers=headers)
    listing.raise_for_status()
    wanted = listing.json()
    new = 0
    for f in wanted:
        if store.has(f["sha256"], f["mime"]):
            continue
        r = client.get(f"{base}/files/{f['sha256']}", headers=headers)
        r.raise_for_status()
        if sha256_hex(r.content) != f["sha256"]:
            raise SystemExit(f"{base}/files/{f['sha256']}: content does not match its hash")
        store.put(r.content, f["mime"])
        new += 1
    print(f"{len(wanted)} files, {new} new -> {store.root}")


_OP_KIND = {
    "items": "item",
    "journal": "entry",  # rows from 2026-09-11 and 2026-09-12, when there were notes
    "memories": "memory",  # rows from before 2026-09-11
    "events": "event",
    "todos": "todo",
    "commitments": "commitment",  # rows from before 2026-09-12
    "dreams": "dream",
    "notes": "notes",  # the one page, from 2026-09-13; `journal` above is the old log
    "reminders": "reminder",
}


def llm_result_lines(raw: str) -> list[str]:
    """`messages.llm_result` as short lines: model and tokens, each op, each applied result."""
    d = json.loads(raw)
    if "error" in d:
        return [f"error: {d['error']}"]
    lines: list[str] = []
    usage = d.get("usage") or {}
    if usage:
        line = f"{d.get('model')}: {usage.get('input_tokens')} in, {usage.get('output_tokens')} out"
        if cached := usage.get("cache_read_input_tokens"):
            line += f", {cached} from cache"
        lines.append(line)
    output = d.get("output") or {}
    for key, kind in _OP_KIND.items():
        for op in output.get(key, []):
            fields = ", ".join(f"{k}={v!r}" for k, v in op.items() if k != "op" and v is not None)
            lines.append(f"{kind} {op.get('op')}: {fields}")
    for a in d.get("applied", []):
        flag = "ok" if a.get("ok") else "SKIPPED"
        note = a.get("note") or ""
        lines.append(f"[{flag}] {a.get('kind')} {a.get('op')} #{a.get('id')} {note}".rstrip())
    return lines


def files_next_to(db_path: Path) -> Path:
    """Where `pull` mirrors the files of a snapshot: `data/prod.db` -> `data/prod-files/`."""
    return db_path.with_name(f"{db_path.stem}-files")


def backup(settings: Settings, db_path: Path, files_dir: Path, dest_dir: Path) -> Path:
    """Write `family-YYYY-MM-DD.zip` (a database snapshot and the files under `files/`) to
    `dest_dir`. Works on any database file, e.g. the one `pull` just fetched, with the
    files it mirrored next to it."""
    db = Database(db_path)
    try:
        path, missing = write_archive(db, settings.tz, dest_dir, FileStore(files_dir))
    finally:
        db.close()
    print(f"{path.stat().st_size} bytes -> {path}")
    if missing:
        print(f"WARNING: {len(missing)} file(s) in the database are not under {files_dir}:")
        for sha in missing:
            print(f"  {sha}")
    return path


def show_log(settings: Settings, db_path: Path, last: int) -> None:
    """Print the last N messages, oldest first, with what the LLM returned and what was applied.

    Works on any database file, e.g. the one `pull` just fetched.
    """
    db = Database(db_path)
    family = Family(db, settings.admin_user_id)
    for m in db.recent_messages(last):
        if m.user_id == "bot":
            who = f"bot -> {family.display_name(m.chat_with)}"
        else:
            who = family.display_name(m.user_id)
        voice = " (voice)" if m.is_voice else " (photo)" if m.photo_file_id else ""
        print(f"#{m.id} {fmt_dt(m.created_at, settings.tz)} {who}{voice}: {m.raw_text}")
        if m.llm_result:
            for line in llm_result_lines(m.llm_result):
                print(f"    {line}")
    db.close()
