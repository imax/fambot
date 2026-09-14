"""One message end to end: store -> context -> LLM -> apply ops -> reply. Spec section 5.

`llm_result_lines` reads back what `handle` stored in `messages.llm_result`, for `log`
and the local chat page."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .context import build_context
from .db import Database, Member
from .family import Family
from .files import FileStore
from .llm import Image, LlmResult, Understander
from .ops import Applied, apply_ops, failure_note

log = logging.getLogger(__name__)

ERROR_REPLY = "Щось пішло не так, спробуй ще раз."


@dataclass(frozen=True)
class Outcome:
    message_id: int
    bot_message_id: int
    reply: str  # what to send; a voice transcript is not echoed (it is on /messages and /debug)
    result: LlmResult | None
    applied: list[Applied]
    error: str | None = None


class Pipeline:
    def __init__(
        self,
        db: Database,
        family: Family,
        llm: Understander,
        tz: ZoneInfo,
        store: FileStore | None = None,
    ) -> None:
        self.db = db
        self.family = family
        self.llm = llm
        self.tz = tz
        self.store = store  # None: a photo is read but not kept (tests)

    async def handle(
        self,
        author: Member,
        text: str,
        *,
        is_voice: bool = False,
        photo: Image | None = None,
        photo_file_id: str | None = None,
        tg_message_id: int | None = None,
    ) -> Outcome:
        """`photo` goes to the LLM with this one call; its bytes go to the file store as an
        attachment of the message (before the LLM call, so a failed call loses nothing) and
        its Telegram `photo_file_id` stays on the message."""
        message_id = self.db.insert_message(
            author.id,
            author.id,
            text,
            is_voice=is_voice,
            photo_file_id=photo_file_id,
            tg_message_id=tg_message_id,
        )
        if photo is not None and self.store is not None:
            sha = self.store.put(photo.data, photo.media_type)
            self.db.add_attachment(message_id, sha, photo.media_type, len(photo.data))
        now = datetime.now(self.tz)
        context = build_context(
            self.db, self.family, now, author, text, with_photo=photo is not None
        )

        try:
            call = await self.llm.run(context, image=photo)
        except Exception as exc:  # any LLM failure: log, tell the user, keep the message
            log.exception("LLM call failed for message %s", message_id)
            self.db.set_llm_result(message_id, json.dumps({"error": repr(exc)}, ensure_ascii=False))
            bot_message_id = self.db.insert_message("bot", author.id, ERROR_REPLY)
            return Outcome(message_id, bot_message_id, ERROR_REPLY, None, [], error=repr(exc))

        applied = apply_ops(
            self.db,
            call.result,
            author_id=author.id,
            message_id=message_id,
            family=self.family,
            tz=self.tz,
            with_photo=photo is not None,
        )
        self.db.set_llm_result(
            message_id,
            json.dumps(
                {
                    "model": call.model,
                    "usage": call.usage,
                    "request_id": call.request_id,
                    "output": call.result.model_dump(exclude_defaults=True),
                    "applied": [a.as_dict() for a in applied],
                },
                ensure_ascii=False,
            ),
        )

        reply = call.result.reply.strip() or "Ок."
        if warning := failure_note(applied):
            # The reply was written before the ops ran; an op that did not go through
            # is said under it, and stays in the stored reply for the next context.
            log.warning("message %s: %s", message_id, warning)
            reply = f"{reply}\n\n{warning}"
        bot_message_id = self.db.insert_message("bot", author.id, reply)
        return Outcome(message_id, bot_message_id, reply, call.result, applied)


_OP_KIND = {
    "items": "item",
    "journal": "entry",  # rows from 2026-09-11 and 2026-09-12, when there were notes
    "memories": "memory",  # rows from before 2026-09-11
    "events": "event",
    "todos": "todo",
    "commitments": "commitment",  # rows from before 2026-09-12
    "dreams": "dream",
    "notes": "notes",  # the one page, from 2026-09-13; `journal` above is the old log
    "today": "today",
    "remember": "remember",
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
            lines.append(f"{kind} {op.get('op', 'set')}: {fields}")  # boards, notes: no op
    for a in d.get("applied", []):
        flag = "ok" if a.get("ok") else "SKIPPED"
        note = a.get("note") or ""
        lines.append(f"[{flag}] {a.get('kind')} {a.get('op')} #{a.get('id')} {note}".rstrip())
    return lines
