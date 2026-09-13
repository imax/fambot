# Family EA

Private family assistant in Telegram: two adults throw text and voice at the bot, it keeps
one shared state (items, events, todos, dreams, reminders, today boards, one notes page),
answers questions from it,
pushes a morning digest and sends reminders at the asked time. Deployed to Fly.io, SQLite on a volume, in real use since 2026-09-10.

`docs/spec-v3.md` is the original spec (Ukrainian). It was retired on 2026-09-10: read it
for the idea, not for what to build next. What to build next comes from real usage; the
list is in `docs/status.md` (see «Status» below).

**Start a session with `git log`, and when the question is behaviour, with a fresh copy of
production:** `pull` then `log` (see Commands).

## Commands

```bash
uv sync                                   # install (creates .venv)
uv run python -m family_ea                # run bot + web in one process
uv run python -m family_ea web            # the web alone on the local db, no bot; prints a login link
uv run python -m family_ea chat --as oleh --name Олег   # the pipeline as a REPL, no Telegram;
                                          #   `/photo path.jpg caption` sends a photo
make local                                # pull, then production becomes the local db + files (chat, web)
uv run python -m family_ea pull           # production db snapshot -> data/prod.db, files -> data/prod-files/
uv run python -m family_ea backup --db data/prod.db   # zip: db snapshot + files -> data/backups/
uv run python -m family_ea log --db data/prod.db --last 50   # messages + what the LLM did
uv run pytest                             # tests
uv run ruff check . && uv run ruff format .
fly deploy --ha=false                     # deploy; never without --ha=false (see Production)
```

`make` lists the shortcuts: `make backup` (pull + zip), `make log` (pull + log, `LAST=`),
`make local` (pull + production becomes the local data), `make web` (the web alone, a login
link), `make test`, `make lint`, `make fmt`, `make deploy` (with `--ha=false` baked in).

`pull` needs `WEB_URL` and `WEB_SECRET` in `.env`; it signs a short-lived bearer token and
downloads `GET /backup.db`, a consistent online backup, WAL included, then `GET /files.json`
and every file not yet in `data/prod-files/` (content-addressed, so a mirror that only grows).

## Layout

```
family_ea/
  config.py     env -> Settings (.env loaded in dev)
  auth.py       signed tokens (HMAC under WEB_SECRET): a `link` from the bot becomes a
                `session` cookie; `pull` signs a `backup` bearer. Nothing is stored.
  family.py     Family over the members table (+ ADMIN_USER_ID); slugify() makes ids from names
  db.py         SQLite schema + all queries; dataclasses Message/Attachment/Item/Event/
                Todo/Dream/Reminder/Notes/TodayList; item_history is written by the item methods only;
                _migrate() for what CREATE IF NOT EXISTS cannot express;
                backup_to() is the online backup behind GET /backup.db
  context.py    deterministic LLM context, event agenda (today/tomorrow/later/recent),
                todo buckets (today/overdue/open/later), today boards (blocks for the
                web and the digest head), the dream lines, the notes page for the LLM,
                the digest text, the web home
                (calendar days; overdue / dated / undated todos), the search stems
  llm.py        pydantic output schema, system prompt, the one messages.parse() call
  ops.py        apply LLM ops to db, with validation and an `applied` log
  pipeline.py   store (message, then its photo as an attachment) -> context -> LLM -> ops -> reply
  files.py      FileStore (bytes under FILES_DIR by SHA-256, `ab/ab12….jpg`, never rewritten),
                files_for() (the files under each item, from source_message_id and the
                `applied` log), files_of_kind() (the photos the notes page was written from)
  transcribe.py OpenAI gpt-4o-transcribe via httpx
  ical.py       an event (timed or all-day) or a dated todo (all-day) -> .ics bytes
  backup.py     the backup archive: a checked db snapshot + the files under files/, zipped
                (missing files are reported, not fatal)
  bot.py        python-telegram-bot handlers (/start /help /today /debug /facts /web, text,
                voice, photo), the 08:30 digest job, the per-minute reminder job, «Відкрити» (a
                login link) under the digest, /today and /web
  web.py        FastAPI + Jinja: GET /login?t= (the bot's link; sets the cookie), GET / (the
                boards, the timeline, the last done ones; ?q= searches), GET /items (Речі:
                places, recent; ?place= ?owner= list), GET /items/:id (photos, history),
                GET /dreams (Мрії: open ones, then fulfilled; read-only), GET /notes
                (Нотатки: the LLM's Markdown rendered, its source photos; read-only),
                GET /files/:sha256 (cookie or bearer),
                GET /files.json (bearer; what `pull` mirrors), GET/POST /facts, GET/POST
                /family, GET /messages, GET /events/:id.ics, GET /todos/:id.ics,
                POST /todos/:id/done and /todos/:id/text (a tap on the home
                page), POST /todos/order (the undated list after a drag), GET /backup.db
                (bearer token)
  main.py       serve() runs bot + uvicorn in one loop; chat() REPL; pull(); backup(); show_log()
tests/          deterministic; the LLM is faked, nothing hits the network
```

## Principles

- **LLM understands, code executes.** One structured-output call per incoming message
  returns `reply` plus item/event/todo/dream/reminder/today/notes ops. Everything else is
  deterministic code. The reply is written before the ops run, so it can claim what did
  not happen; when an op fails (`ops.failures`: not ok, and not merely «unchanged»), the
  pipeline appends «⚠️ Не вийшло: …» under the reply, and that goes into the stored bot
  message too (2026-09-13, after a reply said «прибрав» over three no-op updates).
- **No journal; one notes page.** «What happened», stories, chatter are not stored. A
  `journal` (Нотатки as full-text entries by day, FTS search, a web tab, `notes.md` in the
  backup) lived from 2026-09-11 to 2026-09-12 and was removed as not needed. The
  production database still holds the `journal` table, its FTS index and triggers with
  the old rows: `_migrate()` leaves them alone and nothing creates or reads them; `log`
  still prints the old `journal` ops (`git log -S JournalOp` for that code). Since
  2026-09-13 «Нотатки» is something else: **one reference page for the family** (`notes`
  table, versioned like the facts), Markdown the LLM rewrites whole, like a today board,
  and only on an explicit ask («запиши в нотатки», «запам'ятай …», a photo captioned so):
  a school holiday schedule from a screenshot, a camp packing list, a boiler note. The
  page is always in the LLM context (one page, kept short), it answers questions from it,
  and a new photo or message merges into the existing sections. The web renders it
  (`markdown-it-py`, raw HTML off) with the photos it was written from under it
  (`files_of_kind`), read-only. Facts stay the human's page; the notes are the LLM's.
- **Events and todos are separate tables, not a `kind` column.** An event happens at a
  time or on a day and then passes (never overdue, only cancelled); a todo is done or
  dropped, can be overdue, and carries at most a deadline day (`due`), never a time of
  day: anything with a clock time is an event. (Until 2026-09-12 a todo was a
  «commitment» with `due_at` or a date window, and the LLM filed appointments there; the
  single day field is what keeps the two apart.) Different lifecycles, different data. The
  same goes for any new kind of thing (reminders): its own table, its own ops.
- **A dream is a shared list entry with an author, not a todo without a date.** `dreams`
  (2026-09-13): text, who dreamt it up (`created_by`, shown to both), open / fulfilled /
  dropped; no owner, no date, never overdue, no photos. The LLM files one only on an
  explicit «мрію…» / «в мрії: …»; «хочу купити диван» stays a todo or nothing. Every open
  dream is in the LLM context (they are few); the web tab is read-only, changes go
  through the chat.
- **Items stay out of the default context.** They can be many; the LLM sees only what
  changed in the last two days plus the search hits for the incoming message, the rest is
  on the web. Which item a message is about, the LLM decides from
  those candidates (an update with an id, or a question in the reply); there is no
  matching code, and an ambiguous message must change nothing.
- **The «на сьогодні» board is free text per member, replaced whole.** One `today` op: the
  LLM returns the new text of one member's board, and only when the person addresses the
  board explicitly («на сьогодні: …», «додай у сьогодні …»); everything else stays a
  todo or an event. Code never parses the board: it is shown as kept (the web, the
  digest head, the viewer's own first) and versioned like facts. Nothing resets it;
  staleness is shown («оновлено вчора»), not acted on.
- **Original messages are never mutated.** `messages.raw_text` is append-only.
- **A file belongs to the message it came with.** A photo goes to the LLM as an image
  block before the context of that one call (`llm.Image`, `user_content()`); the caption
  is the message text. The bytes go to `FILES_DIR` (`files.FileStore`, content-addressed
  by SHA-256, written once, never changed) and a row to `attachments` with the
  `message_id`, before the LLM call, so a failed call loses nothing. That row is the only
  link: an item shows the files of the message that created it and of every
  message whose `applied` log names it (`files.files_for`); no LLM op mentions a file and
  nothing describes one. An item `update` that changes nothing is still `ok` in that log,
  so «ось ще фото коробки» (an update with the id and no fields, with a photo) puts the
  photo under it; the same update without a photo is a failure («nothing to update»),
  since the LLM then meant something it could not say. Photos are for the inventory: a «Фото» block of thumbnails on
  the item page, that is the whole feature. A «Документи» tab over every file, with an
  LLM description per photo (`photo` field, `attachments.description`), was built on
  2026-09-11 and removed on 2026-09-12 as too much; the column stays, unused. The prompt
  makes the record self-contained (the model never sees the photo again), and a photo
  without a clear caption changes nothing.
- `messages.chat_with` is the family member whose chat the row belongs to, so bot replies
  and pushes can be attributed in context; `messages.llm_result` holds
  `{model, usage, request_id, output, applied}`, not the bare LLM output.
- **The web writes about a todo only through the db methods the LLM ops use**, no
  parallel logic. Three things: done (a tap on «☐» on the home page, a few seconds to
  take it back, then `POST /todos/:id/done`, `db.close_todo`; the done/drop
  buttons of 2026-09-10 were removed as ugly, dropping stays with the LLM), the text («✎»
  on the row, `POST /todos/:id/text`, `db.update_todo`, open ones only)
  and the order of the undated ones (`position`, dragged on the home page,
  `db.reorder_todos`); the LLM never sets the order, and `open_todos()`
  returns it so the timeline, the digest and the LLM context agree. Unplaced ones (new
  since the last drag) come first, newest first. Nothing reopens a closed todo.
- **Every push is deterministic and stored.** The morning digest renders each member's
  board (own first), today's and tomorrow's events, then todos due today and overdue,
  never the undated ones (they are on the web), no LLM call, and is silent when empty;
  `/today` is the same digest now. A reminder is text the LLM wrote at request time,
  sent by a per-minute job when `at` comes, to the one member it is for or to everyone.
  Both are stored as bot messages in each recipient's chat so replies to them have
  context. Anything else the bot sends on its own must follow the same two rules.
- **Web identity comes from the bot.** No passwords: `/web` (and «Відкрити» under the digest
  and `/today`) sends a member a signed link, opening it sets a year-long signed cookie.
  Whoever is in `members` can log in; nothing is stored, so removing a member or
  rotating `WEB_SECRET` is the only revocation.
- The bot must not promise what the code cannot do. The prompt lists what the bot does
  (digest, reminders, the web link); when a capability is added or removed, that list changes in
  the same commit.
- Keep it small. Two people use this; a feature earns its place by removing a real pain.
- **Concrete models and use cases, not a Dropbox or a Google Docs.** (The notes page is
  the one free-text exception, and it is one page that the LLM keeps readable, not a
  store of files or entries.) A feature starts from
  a question someone in the family actually asks («де паспорт?», «що в мене на сьогодні?»,
  «коли стоматолог?») and a narrow data model that answers exactly it (an item with a
  place, a todo with a day, an event with a time). «Store it so it is there» (files, PDFs,
  scans, long free-text notes, exports) is not a use case: the «Документи» tab and the
  notes were built that way and both went within a day (2026-09-12).

## Production

- Fly app `family-ea`, https://family-ea.fly.dev, one `shared-cpu-1x` machine in `fra`,
  volume `data` at `/data`, database `/data/family.db`, files under `/data/files`
  (`FILES_DIR` in `fly.toml`; locally next to the database, `data/files`). Fly keeps daily
  volume snapshots for 5 days; `pull` mirrors the files to the laptop.
- **Always `fly deploy --ha=false`.** Two machines would mean two pollers on one bot token
  and two SQLite files.
- Secrets on Fly: `ADMIN_USER_ID`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `TELEGRAM_BOT_TOKEN`, `WEB_SECRET`, `WEB_URL`. The rest is in `fly.toml`.
- A deploy ships code only. Tables are created at start (`CREATE TABLE IF NOT EXISTS`);
  anything else goes into `Database._migrate()`, idempotent steps that run at every start
  (the one there copies `commitments` into `todos`, 2026-09-12; the `memories` → `journal`
  one of 2026-09-11 went with the notes).
- Local `data/family.db` and production are separate databases; nothing syncs up.
  `make local` copies production down (db and files) so `web` and `chat` run on real data:
  that is how a web change is reviewed in a browser before it ships. To look at
  production: `pull`, then `log --db data/prod.db` or `sqlite3 data/prod.db`. Fix production
  data through the bot itself where possible (tell it what changed), not with SQL.
  `fly ssh sftp get` copies the main file but not the `-wal` file with the latest writes;
  that is why `/backup.db` exists.
- Never run a local `serve` with the production bot token while Fly is up: Telegram answers
  409 on getUpdates. Locally use `chat`, or a separate dev bot token.

## Status

`docs/status.md` (Ukrainian) is the living description of the system: «Що вміє» (what it
does, by surface), «Ідеї та туду» (the backlog, found in real use) and «Зміни» (dated
releases, newest first). A commit that changes behaviour updates it in the same commit:
the capability text, the backlog item it closes, a line under «Зміни». «Що вміє» is
written for the people who use the bot: a page or a command name at most, no internals.
The backlog may name code.

## Conventions

- Code, comments, commit messages: English. Bot replies, prompts, UI copy: Ukrainian.
- Timezone `Europe/Kyiv` for everything user-facing; store ISO UTC in SQLite.
- Secrets and personal data (`.env`, `data/`) are gitignored. This repo is public. Never
  commit tokens, Telegram ids, real names, or real conversation data, including in docs,
  prompts, tests, fixtures and this file. Every name in the repo is a fictional placeholder
  (Олег, Анна, Оля, пані Марія, газовик Петро); keep it that way. Grep for real names
  before every push.
- Three kinds of knowledge, three owners: `members` table (who talks to the bot, the
  Telegram allowlist; only `ADMIN_USER_ID` is env, the admin edits the rest on the web),
  `facts` (stable background about the family; the human edits it on the web, the LLM only
  reads it), items/events/todos/dreams/reminders/today boards/the notes page (everything
  people tell the bot; the
  LLM writes them).
- Python 3.12, `uv` for deps, `ruff` for lint/format, `pytest` with `asyncio_mode=auto`.
- FastAPI modules must not use `from __future__ import annotations`: postponed `Annotated`
  dependencies referencing closure variables break dependency resolution (silent 422s).
- SQLite `LIKE`/`lower()` are ASCII-only; use the registered `ufold()` for Ukrainian text and
  `regexp()` (Python `re`) for word-prefix search. Search terms come from `context.stems()`,
  one heuristic (`word_pattern`) behind the events, todos and items search on the web and
  the item hits in the LLM context.
- Tests that check dates in the context monkeypatch `family_ea.db.utc_now_iso`; otherwise
  they drift with the calendar.

## Voice

Transcription goes through OpenAI `gpt-4o-transcribe` (`language=uk`) via a single httpx
multipart POST in `family_ea/transcribe.py`. That is the only OpenAI usage; the LLM is Claude.

## Claude API notes

- `anthropic` SDK 1.x. Structured output via `output_config.format` / `client.messages.parse()`,
  not tool_use tricks and not assistant prefill (prefill is rejected on current models).
- Model comes from `LLM_MODEL` env; default `claude-sonnet-5` at effort `medium`. Compare
  with `claude-haiku-4-5` once scenario tests exist.
- No thinking/`budget_tokens` config needed; use `thinking: {type: "adaptive"}` if enabling.
- Op fields in `llm.py` are never `X | None`: nullable fields inflate the structured-output
  grammar and the API rejects the schema («compiled grammar is too large»; hit 2026-09-11
  when items were added). `''` / `0` mean «not given»; a lone `-` (`ops.CLEAR`) clears an
  optional field (an item's note or owner, a todo's deadline, an event's end), since the
  LLM has no other way to say «прибери примітку». Test a schema change with `chat` before
  deploying: the failure is a 400 on every message.
