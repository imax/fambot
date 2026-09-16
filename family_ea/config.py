"""Settings from the environment. A local `.env` is loaded if present (dev only)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    telegram_token: str | None
    anthropic_api_key: str | None
    openai_api_key: str | None
    database_path: Path
    files_dir: Path  # photos that came with messages, by content hash
    admin_user_id: int | None  # Telegram id of the admin; everyone else is in the db
    web_secret: str | None  # signs web links and cookies; no passwords
    web_url: str | None
    llm_model: str
    llm_effort: str
    digest_time: time  # local time of the morning push, in `tz`
    nudge_time: time  # local time of the nudge about a loose end (bot.deliver_due_nudges)
    port: int
    tz: ZoneInfo

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            env_names = ", ".join(n.upper() for n in missing)
            raise SystemExit(f"missing required settings: {env_names}")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_int(name: str) -> int | None:
    raw = _env(name)
    if raw is None:
        return None
    if not raw.isdigit():
        raise SystemExit(f"{name} must be a number (a Telegram user id), got {raw!r}")
    return int(raw)


def _env_time(name: str, default: str) -> time:
    raw = _env(name, default) or default
    try:
        return time.fromisoformat(raw)
    except ValueError:
        raise SystemExit(f"{name} must be HH:MM, got {raw!r}") from None


def load_settings() -> Settings:
    database_path = Path(_env("DATABASE_PATH", "./data/family.db") or "")
    return Settings(
        telegram_token=_env("TELEGRAM_BOT_TOKEN"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        openai_api_key=_env("OPENAI_API_KEY"),
        database_path=database_path,
        files_dir=Path(_env("FILES_DIR") or database_path.parent / "files"),
        admin_user_id=_env_int("ADMIN_USER_ID"),
        web_secret=_env("WEB_SECRET"),
        web_url=_env("WEB_URL"),
        llm_model=_env("LLM_MODEL", "claude-sonnet-5") or "",
        llm_effort=_env("LLM_EFFORT", "medium") or "",
        digest_time=_env_time("DIGEST_TIME", "08:30"),
        nudge_time=_env_time("NUDGE_TIME", "10:00"),
        port=int(_env("PORT", "8080") or 8080),
        tz=ZoneInfo(_env("TZ", "Europe/Kyiv") or "Europe/Kyiv"),
    )
