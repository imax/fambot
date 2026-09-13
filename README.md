# Family EA

A private executive assistant for one family, living in Telegram.

Two people throw context at the bot during the day: text, voice or a photo with a
caption, in natural language, without deciding whether it is a task, an event or a thing.
The bot keeps one shared state, answers questions about it, and every morning writes
each person what is on today and what is still hanging. A small web page shows what the
system actually stored and lets you tidy it by hand.

The whole idea in one line: **the LLM understands, the code executes.** One
structured-output call per message returns what to write down and which records to
create, update or close; everything else, including the morning digest, the reminder job,
storage and the web view, is plain deterministic code. The model remembers nothing between
calls: all it knows is in the context the code builds for it.

## What it keeps

- **Todos** (Задачі): something to do, with an owner and at most a deadline day. Closed
  by saying so in the chat or with a tap on the web.
- **Things** (Речі): what the family has, whose it is and where it lies right now, with
  a history of moves and the photos it was described from. «Паспорт Олі лежить у сейфі».
- **Events**: something happens at a time or on a day and then passes.
- **Notes** (Нотатки): family wiki, updated by the model.
- **Dreams** (Мрії): what the family wants some day, one shared list, each with the
  person who dreamt it up. No date, never overdue; fulfilled or let go. «Мрію пройти
  Camino de Santiago».
- **Reminders**: a message to one person or everyone at a given moment.
- **Today boards** («на сьогодні»): each person's free-text list for the day, kept as one
  sentence and rewritten on request.
- **Facts**: stable background about the family, edited by a human on the web; the model
  reads it, never writes it.

What happened, stories and chatter are not kept unless someone asks: the bot answers and
does not promise to write such things down. (A journal of notes by day was built in the
first days and removed on 2026-09-12; the one page above came on 2026-09-13.)

Every push is deterministic: the 08:30 digest renders the boards, today's and tomorrow's
events, todos due today and overdue, and stays silent when there is nothing to say.

## Status

Deployed; in real use since September 2026. What it does today, the backlog and the dated
changes: [docs/status.md](docs/status.md) (Ukrainian).

## Stack

Python 3.12, `python-telegram-bot` (long polling + JobQueue), FastAPI + Jinja for the web
view, SQLite, the Anthropic SDK (structured output), OpenAI for transcription.
One process, one SQLite writer. Deployed on [Fly.io](https://fly.io) with a persistent
volume for the database and the photos.

## Running locally

```bash
uv sync
cp .env.example .env          # fill in tokens and ADMIN_USER_ID
uv run python -m family_ea    # bot + web on :8080 (never with the production token while it runs there)
make web                      # the web alone on the local db, no bot; prints a login link
make local                    # pull production and make it the local db + files, to look at real data
uv run python -m family_ea chat --as oleh --name Олег   # the pipeline as a REPL, no Telegram
make test                     # deterministic tests, the LLM is faked
make log                      # pull production, print the last messages with what the LLM did
make backup                   # pull production, zip the db and the files
```

`.env` and `data/` are gitignored. They hold the admin's Telegram id, secrets, the
database and the photos, and must never be committed. Who is who in the family is not
config either: the people who talk to the bot are added on the web (`/family`; the admin
appears there by writing to the bot, and a stranger who writes gets the admin a link to
add them). All names in this repo's docs, prompts and tests are fictional placeholders.

## Deploying

```bash
fly launch --no-deploy        # first time only
fly volumes create data --size 1 --region fra
fly secrets set ADMIN_USER_ID=... TELEGRAM_BOT_TOKEN=... ANTHROPIC_API_KEY=... OPENAI_API_KEY=... WEB_URL=https://<app>.fly.dev WEB_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
make deploy                   # fly deploy --ha=false: one machine, one poller on the bot token, one SQLite
```

The web view has no password: `/web` in Telegram (and «Відкрити» under the morning
digest) sends a signed link that logs that browser in for a year. `GET /backup.db` is a
consistent online backup of the database; `make backup` signs a short-lived bearer token
with the same `WEB_SECRET` (put it in `.env`), downloads it with the photos and zips them.

## License

MIT.
