"""The DTEK power outage schedule for one queue («Черга 3.2»), for the morning digest.

DTEK's own pages (dtek-krem.com.ua and the like) sit behind bot protection, so the data
comes from github.com/Baskerville42/outage-data-ua, which scrapes them every few minutes and
publishes the page's `DisconSchedule` object as is: `fact.data` maps the Unix time of a
Kyiv midnight to `{"GPV3.2": {"1": "yes", ..., "24": "no"}}`, hour "1" being 00:00-01:00.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

SOURCE = "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/{region}.json"

# Each hour as two halves, (first, second): "off", "maybe" or None (light is on).
HALVES: dict[str, tuple[str | None, str | None]] = {
    "yes": (None, None),
    "no": ("off", "off"),
    "first": ("off", None),
    "second": (None, "off"),
    "maybe": ("maybe", "maybe"),
    "mfirst": ("maybe", None),
    "msecond": (None, "maybe"),
}


async def fetch(region: str, transport: httpx.AsyncBaseTransport | None = None) -> dict | None:
    """The region's schedule as published; None when it cannot be had (logged, not fatal:
    the digest goes out without it)."""
    try:
        async with httpx.AsyncClient(timeout=20, transport=transport) as client:
            r = await client.get(SOURCE.format(region=region))
            r.raise_for_status()
            return r.json()
    except (httpx.HTTPError, ValueError):
        log.warning("outages: could not fetch %s", region, exc_info=True)
        return None


def _hhmm(slot: int) -> str:
    return f"{slot // 2:02d}:{30 * (slot % 2):02d}"


def spans(hours: dict[str, str]) -> list[tuple[str, str, str]]:
    """(kind, start, end) for each run of half-hours without light: kind "off" or "maybe",
    times as "13:00" and "17:30" ("24:00" for the end of the day)."""
    slots: list[str | None] = []
    for h in range(1, 25):
        slots += HALVES.get(hours.get(str(h), "yes"), (None, None))
    out: list[tuple[str, str, str]] = []
    start = 0
    for i in range(1, 49):
        if i == 48 or slots[i] != slots[start]:
            if slots[start] is not None:
                out.append((slots[start], _hhmm(start), _hhmm(i)))  # type: ignore[arg-type]
            start = i
    return out


def day_hours(data: dict, group: str, day: date, tz: ZoneInfo) -> dict[str, str] | None:
    """The hours of one queue on one day, or None when the schedule has no such day yet
    (tomorrow's comes out in the evening) or no such queue."""
    key = str(int(datetime.combine(day, time(), tz).timestamp()))
    try:
        hours = data["fact"]["data"][key][f"GPV{group}"]
    except (KeyError, TypeError):
        return None
    return hours if isinstance(hours, dict) else None


def day_line(hours: dict[str, str]) -> str:
    """«без відключень» or «немає 13:00–17:00, можливо 20:00–21:30»."""
    found = spans(hours)
    if not found:
        return "без відключень"
    off = [f"{a}–{b}" for kind, a, b in found if kind == "off"]
    maybe = [f"{a}–{b}" for kind, a, b in found if kind == "maybe"]
    parts = []
    if off:
        parts.append("немає " + ", ".join(off))
    if maybe:
        parts.append("можливо " + ", ".join(maybe))
    return ", ".join(parts)


def outage_lines(data: dict | None, group: str, today: date, tz: ZoneInfo) -> list[str]:
    """The digest block: today's line, and tomorrow's once DTEK has published it. Empty
    when there is no schedule for today: nothing is better than yesterday's."""
    if data is None:
        return []
    hours = day_hours(data, group, today, tz)
    if hours is None:
        return []
    lines = [f"Світло, черга {group}:", f"- сьогодні {day_line(hours)}"]
    tomorrow = day_hours(data, group, today + timedelta(days=1), tz)
    if tomorrow is not None:
        lines.append(f"- завтра {day_line(tomorrow)}")
    return lines


def has_outages(data: dict | None, group: str, today: date, tz: ZoneInfo) -> bool:
    """Whether today holds anything but light: enough to send the digest on its own."""
    hours = day_hours(data, group, today, tz) if data else None
    return bool(hours and spans(hours))


def with_outages(
    digest: str | None, data: dict | None, group: str, today: date, tz: ZoneInfo
) -> str | None:
    """The digest with the outage block on top. A morning with nothing else to say still
    gets one when the light goes off today; a quiet one stays silent."""
    lines = outage_lines(data, group, today, tz)
    if not lines or (digest is None and not has_outages(data, group, today, tz)):
        return digest
    return "\n".join([*lines, digest] if digest else lines)
