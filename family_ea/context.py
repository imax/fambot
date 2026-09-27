"""Deterministic context for the LLM, the event agenda, todo buckets, the digest,
the weeks and the todo lists of the web home.

Everything here is plain code: what is "today", what is "overdue", which items to
show. The LLM only sees the result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .db import Database, Dream, Event, Item, Member, Message, Notes, Project, Reminder, Todo
from .family import Family

RECENT_WINDOW_DAYS = 2  # items changed this recently are in every LLM context; older: search
ITEM_HITS = 20  # items found by the message's words
RECENT_MESSAGES = 20
PAST_EVENT_DAYS = 7  # ended events stay in the LLM context this long ("коли був стоматолог?")
ALL_DAY = "весь день"  # the label of an all-day event where a time would be
DEFAULT_EVENT_DURATION = timedelta(hours=1)
WEEK = timedelta(days=7)  # Monday to Sunday
WEEKDAYS_UK = ("понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя")
WEEKDAYS_SHORT_UK = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")


# --- dates -------------------------------------------------------------------


def parse_iso(value: str) -> datetime:
    """Parse an ISO datetime; naive values are treated as UTC."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt


def fmt_dt(iso_utc: str, tz: ZoneInfo) -> str:
    """'2026-09-10T12:30:00Z' -> '10.09 15:30' in the family timezone."""
    return parse_iso(iso_utc).astimezone(tz).strftime("%d.%m %H:%M")


def fmt_date(iso_date: str) -> str:
    """'2026-09-20' -> '20.09'."""
    return date.fromisoformat(iso_date).strftime("%d.%m")


def fmt_due(t: Todo) -> str:
    """The deadline as '20.09'; '' when undated."""
    return fmt_date(t.due) if t.due else ""


# --- weeks --------------------------------------------------------------------


def week_start(d: date) -> date:
    """The Monday of the week `d` is in: a week runs from Monday to Sunday."""
    return d - timedelta(days=d.weekday())


def fmt_week(monday: date) -> str:
    """'28.09–04.10': the week that starts on `monday`."""
    return f"{monday:%d.%m}–{monday + WEEK - timedelta(days=1):%d.%m}"


def week_days(monday: date) -> str:
    """'пн 2026-09-28, вт 2026-09-29, …, нд 2026-10-04': the days of a week for the LLM,
    so that it reads a weekday's date instead of counting it (2026-09-27: «(чт)» of a week
    that crosses into October came back as Friday's date)."""
    days = (monday + timedelta(days=n) for n in range(7))
    return ", ".join(f"{WEEKDAYS_SHORT_UK[d.weekday()]} {d.isoformat()}" for d in days)


def week_note(monday: date, today: date | None, in_week: bool = False) -> str:
    """The week a todo is planned for, as a note next to it: 'цей тиждень', 'наступний
    тиждень', 'тиждень 12.10–18.10'; one left over from an earlier week is still in the
    plan and says since when: 'з минулого тижня', 'з тижня 14.09–20.09'. `in_week`: the
    line sits in the list of its own week, which needs no note but the leftover's."""
    if today is None:
        return f"тиждень {fmt_week(monday)}"
    this = week_start(today)
    if monday in (this, this + WEEK):
        return "" if in_week else ("цей тиждень" if monday == this else "наступний тиждень")
    if monday == this - WEEK:
        return "з минулого тижня"
    return f"з тижня {fmt_week(monday)}" if monday < this else f"тиждень {fmt_week(monday)}"


# --- events -------------------------------------------------------------------


def event_span(e: Event, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Local start and exclusive end. Timed: `until` or one hour. All-day: midnight to midnight."""
    if e.starts_at:
        start = parse_iso(e.starts_at).astimezone(tz)
        end = parse_iso(e.until).astimezone(tz) if e.until else start + DEFAULT_EVENT_DURATION
        return start, end
    first = date.fromisoformat(e.date_from or e.date_to or "")
    last = date.fromisoformat(e.date_to or e.date_from or "")
    midnight = datetime.min.time()
    return (
        datetime.combine(first, midnight, tzinfo=tz),
        datetime.combine(last + timedelta(days=1), midnight, tzinfo=tz),
    )


def fmt_event_time(e: Event, tz: ZoneInfo) -> str:
    """'15:30–17:00' or '15:30'; '' for an all-day event."""
    if not e.starts_at:
        return ""
    start, end = event_span(e, tz)
    return f"{start:%H:%M}" + (f"–{end:%H:%M}" if e.until else "")


def fmt_event_when(e: Event, tz: ZoneInfo) -> str:
    """'11.09 15:30–17:00', '11.09 15:30', '12.09–19.09' or '12.09'."""
    start, end = event_span(e, tz)
    if e.starts_at:
        return f"{start:%d.%m} {fmt_event_time(e, tz)}"
    last = end - timedelta(days=1)
    return f"{start:%d.%m}" if last.date() == start.date() else f"{start:%d.%m}–{last:%d.%m}"


def event_line(
    e: Event, family: Family, tz: ZoneInfo, *, with_id: bool = True, with_date: bool = True
) -> str:
    """'[подія #3] 11.09 15:30 Стоматолог (Анна)'; without date: '15:30 Стоматолог (Анна)'."""
    start, end = event_span(e, tz)
    meta = [family.display_name(e.who)] if e.who else []
    if with_date:
        when = fmt_event_when(e, tz) + " "
    elif e.starts_at:
        when = fmt_event_time(e, tz) + " "
    else:
        when = ""
        last = end - timedelta(days=1)
        if last.date() != start.date():
            meta.append(f"до {last:%d.%m}")
    head = f"[подія #{e.id}] " if with_id else ""
    tail = f" ({', '.join(meta)})" if meta else ""
    return f"{head}{when}{e.text}{tail}"


@dataclass
class Agenda:
    today: list[Event] = field(default_factory=list)
    tomorrow: list[Event] = field(default_factory=list)
    later: list[Event] = field(default_factory=list)
    recent: list[Event] = field(default_factory=list)  # ended within PAST_EVENT_DAYS

    @property
    def upcoming(self) -> list[Event]:
        return self.today + self.tomorrow + self.later


def build_agenda(items: list[Event], now: datetime, past_days: int = PAST_EVENT_DAYS) -> Agenda:
    """Planned events by day relative to `now` (aware, family tz). A multi-day event lands in
    the first list it touches; ended events older than `past_days` are left out."""
    tz = now.tzinfo
    assert isinstance(tz, ZoneInfo)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    horizon = now - timedelta(days=past_days)
    a = Agenda()
    for e in sorted((e for e in items if e.is_planned), key=lambda e: event_span(e, tz)[0]):
        start, end = event_span(e, tz)
        first, last = start.date(), (end - timedelta(seconds=1)).date()
        if first <= today <= last:
            a.today.append(e)
        elif first <= tomorrow <= last:
            a.tomorrow.append(e)
        elif first > tomorrow:
            a.later.append(e)
        elif end >= horizon:
            a.recent.append(e)
    return a


# --- todo buckets ---------------------------------------------------------------


@dataclass
class Buckets:
    today: list[Todo] = field(default_factory=list)
    overdue: list[Todo] = field(default_factory=list)
    week: list[Todo] = field(default_factory=list)  # no day; planned for this week or left
    #                                                 over from an earlier one
    ahead: list[Todo] = field(default_factory=list)  # no day; planned for a week to come
    open: list[Todo] = field(default_factory=list)  # no day and no week
    later: list[Todo] = field(default_factory=list)  # a day after today


def bucket_todos(items: list[Todo], now: datetime) -> Buckets:
    """Split open todos into today / overdue / week / ahead / open / later relative to
    `now`.

    `now` must be timezone-aware in the family timezone; "today" is its date. Dated ones
    are sorted by deadline, the others keep their order (the hand-set one).
    """
    today = now.date().isoformat()
    this_week = week_start(now.date()).isoformat()
    b = Buckets()
    for t in items:
        if not t.is_open:
            continue
        if not t.due and not t.week:
            b.open.append(t)
        elif not t.due:
            (b.week if (t.week or "") <= this_week else b.ahead).append(t)
        elif t.due < today:
            b.overdue.append(t)
        elif t.due == today:
            b.today.append(t)
        else:
            b.later.append(t)
    for dated in (b.today, b.overdue, b.later):
        dated.sort(key=lambda t: (t.due or "", t.id))
    return b


def todo_line(
    t: Todo,
    family: Family,
    with_id: bool = True,
    projects: dict[int, str] | None = None,
    today: date | None = None,
    in_week: bool = False,
) -> str:
    """`projects` (id -> name) adds «проєкт: Авто» to the meta; the digest passes none.
    `today` and `in_week` word the week of a todo planned for one (see `week_note`)."""
    parts = [f"[#{t.id}] " if with_id else "", t.text]
    meta = []
    if t.owner:
        meta.append(family.display_name(t.owner))
    if t.due:
        meta.append(fmt_due(t))
    elif t.week and (note := week_note(date.fromisoformat(t.week), today, in_week)):
        meta.append(note)
    if projects and t.project_id in projects:
        meta.append(f"проєкт: {projects[t.project_id]}")
    if meta:
        parts.append(f" ({', '.join(meta)})")
    return "".join(parts)


def project_context_lines(projects: list[Project], todos: list[Todo]) -> list[str]:
    """For the LLM: every open project with its id and how many open todos it holds."""
    counts: dict[int, int] = {}
    for t in todos:
        if t.project_id is not None:
            counts[t.project_id] = counts.get(t.project_id, 0) + 1
    return [f"- [#{p.id}] {p.name} ({counts.get(p.id, 0)} відкритих)" for p in projects]


# --- dreams ---------------------------------------------------------------------


def dream_line(d: Dream, family: Family) -> str:
    """'[мрія #1] Поїхати в Японію з Олею (Олег)'."""
    return f"[мрія #{d.id}] {d.text} ({family.display_name(d.created_by)})"


# --- reminders ----------------------------------------------------------------


REPEAT_LABELS = {"daily": "щодня", "weekly": "щотижня"}


def reminder_line(r: Reminder, family: Family, tz: ZoneInfo) -> str:
    """'[нагадування #2] 11.09 15:00 Зустріч з пані Марією о 16:00 (Анна)'; '(усім)' for all;
    '(Анна, щодня)' for a repeating one: the line is its next time."""
    to = family.display_name(r.who) if r.who else "усім"
    if r.repeat:
        to = f"{to}, {REPEAT_LABELS.get(r.repeat, r.repeat)}"
    return f"[нагадування #{r.id}] {fmt_dt(r.at, tz)} {r.text} ({to})"


# --- notes --------------------------------------------------------------------


def notes_context_lines(notes: Notes | None, family: Family, tz: ZoneInfo) -> list[str]:
    """For the LLM: when and by whom the page last changed, then its text as kept."""
    if notes is None or not notes.text.strip():
        return []
    who = family.display_name(notes.created_by)
    return [f"(оновлено {fmt_dt(notes.created_at, tz)}, {who})", notes.text.strip()]


# --- digest -------------------------------------------------------------------


def week_event_line(e: Event, family: Family, tz: ZoneInfo, with_id: bool = False) -> str:
    """'ср 30.09 15:30 Стрижка (Олег)': an event later this week, its weekday first."""
    head = f"[подія #{e.id}] " if with_id else ""
    day = WEEKDAYS_SHORT_UK[event_span(e, tz)[0].weekday()]
    return f"{head}{day} {event_line(e, family, tz, with_id=False)}"


def _digest_lines(
    a: Agenda,
    b: Buckets,
    family: Family,
    now: datetime,
    *,
    with_ids: bool,
    include_open: bool,
    max_open: int,
) -> list[str]:
    tz = now.tzinfo
    assert isinstance(tz, ZoneInfo)
    today = now.date()
    sunday = week_start(today) + WEEK - timedelta(days=1)

    def ev(e: Event) -> str:
        return f"- {event_line(e, family, tz, with_id=with_ids, with_date=False)}"

    def td(t: Todo) -> str:
        return f"- {todo_line(t, family, with_id=with_ids, today=today, in_week=True)}"

    lines: list[str] = []
    if a.today:
        lines += ["Сьогодні:", *map(ev, a.today)]
    if a.tomorrow:
        lines += ["Завтра:", *map(ev, a.tomorrow)]
    if b.today:
        lines += ["Задачі на сьогодні:", *map(td, b.today)]
    if b.overdue:
        lines += ["Прострочено:", *map(td, b.overdue)]
    # The rest of the week: what happens after tomorrow, what has a day still to come,
    # what is planned for the week without one.
    week = [
        f"- {week_event_line(e, family, tz, with_ids)}"
        for e in a.later
        if event_span(e, tz)[0].date() <= sunday
    ]
    week += [td(t) for t in b.later if t.due and t.due <= sunday.isoformat()]
    week += map(td, b.week)
    if week:
        lines += ["Цього тижня:", *week]
    if include_open and b.ahead:
        ahead = [todo_line(t, family, with_id=with_ids, today=today) for t in b.ahead]
        lines += ["Наступні тижні:", *(f"- {line}" for line in ahead)]
    if include_open and b.open:
        lines += ["Без дати:", *map(td, b.open[:max_open])]
        if len(b.open) > max_open:
            lines.append(f"- і ще {len(b.open) - max_open}")
    return lines


def render_digest(a: Agenda, b: Buckets, family: Family, now: datetime, max_open: int = 5) -> str:
    """Today / tomorrow / due / overdue / this week / the weeks ahead / open, with ids, for
    the LLM context."""
    lines = _digest_lines(a, b, family, now, with_ids=True, include_open=True, max_open=max_open)
    return "\n".join(lines) if lines else "нічого"


def digest_text(a: Agenda, b: Buckets, family: Family, now: datetime) -> str | None:
    """The morning push, and /today: today's and tomorrow's events, today's and overdue
    todos, then the rest of the week: its events after tomorrow, the todos with a day still
    to come in it and the ones planned for it without a day (the leftovers of an earlier
    week among them). Never the undated ones: they live on the web. None when there is
    nothing to say: an empty morning stays silent. Deterministic on purpose: presentation,
    not understanding."""
    lines = _digest_lines(a, b, family, now, with_ids=False, include_open=False, max_open=0)
    if not lines:
        return None
    return "\n".join(lines)


# --- the weeks and the todo lists (the web) --------------------------------------


@dataclass(frozen=True)
class Row:
    """One line of the web home (a week or a todo list), ready to render: a planned event, a
    pending reminder or an open todo. Everything is formatted here; the template only lays
    it out."""

    kind: str  # 'event' | 'reminder' | 'todo'
    id: int
    text: str
    time: str = ""  # '16:00' (a start); ALL_DAY for an all-day event; '' for a todo
    all_day: bool = False  # the template styles the label, not a time
    note: str = ""  # 'до 17:00' / 'до 19.09': an end still ahead; '09.09': the day a todo
    #                 is late from; 'з минулого тижня': the week it is left over from
    who: str = ""  # display name; 'усім' for a reminder to everyone; '' when nobody in particular
    project: str = ""  # the project name of a todo in a week; undated ones sit in its group


@dataclass
class Day:
    """One day of a week on the web home: its heading ('Сьогодні, четвер 10.09', marked
    when today) and its rows, events first, then the todos of that day."""

    when: date
    title: str
    rows: list[Row] = field(default_factory=list)
    today: bool = False


@dataclass
class Week:
    """One week of the web home, Monday to Sunday: the days from today on that hold an
    event or a todo (today always, even empty), and the todos planned for the week without
    a day. This week also holds what is late: the todos past their day (`overdue`), and
    among `rows` the ones left over from an earlier week, noted so; nothing moves them on
    or out but a person."""

    start: date
    title: str  # 'Цей тиждень' | 'Наступний тиждень'
    span: str  # '28.09–04.10'
    overdue: list[Row] = field(default_factory=list)
    days: list[Day] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.overdue or self.days or self.rows)


@dataclass
class Plan:
    """The web home's view of time: this week, the next one when it holds anything, and
    everything after them as one line that unfolds into days ('далі: 07.10 Стрижка · …').
    The week is the unit the family plans in (2026-09-27); before that the page had a
    calendar of events and a list of dated todos apart."""

    weeks: list[Week] = field(default_factory=list)
    later: list[Day] = field(default_factory=list)

    @property
    def later_line(self) -> str:
        """The tail as text, for the line that unfolds it: '07.10 Стрижка · 26.10 Канікули'."""
        return " · ".join(f"{d.when:%d.%m} {r.text}" for d in self.later for r in d.rows)


@dataclass
class Group:
    """The undated todos of one project, in the hand-set order; `name` '' and `project_id`
    None for the ones without a project («Без дати» on the web, the default, first)."""

    name: str
    rows: list[Row] = field(default_factory=list)
    project_id: int | None = None


def day_title(d: date, today: date) -> str:
    """'Сьогодні, четвер 10.09', 'Завтра, п'ятниця 11.09', 'Середа 07.10'."""
    name = WEEKDAYS_UK[d.weekday()]
    if d == today:
        return f"Сьогодні, {name} {d:%d.%m}"
    if d == today + timedelta(days=1):
        return f"Завтра, {name} {d:%d.%m}"
    return f"{name.capitalize()} {d:%d.%m}"


def due_note(due: date, today: date) -> str:
    """A day next to a text: 'сьогодні', 'завтра', else '19.09'."""
    if due == today:
        return "сьогодні"
    if due == today + timedelta(days=1):
        return "завтра"
    return f"{due:%d.%m}"


def build_plan(
    events: list[Event],
    todos: list[Todo],
    now: datetime,
    family: Family,
    projects: list[Project] | None = None,
) -> Plan:
    """The weeks of the web home: planned events and open todos with a day or a week.

    An event sits on its day; a multi-day one still running sits on today. A todo with a
    day sits on it after the events, or under «Прострочено» of this week once the day has
    passed. A todo planned for a week sits in that week's `rows`, in the order
    `open_todos()` gives; one planned for a week that is over stays in this week's, noted
    («з минулого тижня»). Days past next Sunday go to `later`.
    """
    tz = now.tzinfo
    assert isinstance(tz, ZoneInfo)
    today = now.date()
    this = Week(week_start(today), "Цей тиждень", fmt_week(week_start(today)))
    nxt = Week(this.start + WEEK, "Наступний тиждень", fmt_week(this.start + WEEK))
    horizon = nxt.start + WEEK  # the first day past the two weeks
    names = {p.id: p.name for p in projects or []}
    by_day: dict[date, list[tuple[tuple, Row]]] = {today: []}
    overdue: list[tuple[tuple, Row]] = []

    def place(day: date, key: tuple, row: Row) -> None:
        by_day.setdefault(day, []).append((key, row))

    for e in events:
        if not e.is_planned:
            continue
        start, end = event_span(e, tz)
        first, last = start.date(), (end - timedelta(seconds=1)).date()
        if last < today:
            continue
        day = max(first, today)
        if not e.starts_at:
            note = f"до {last:%d.%m}" if last > day else ""
        elif not e.until:
            note = ""
        else:
            note = f"до {end:%H:%M}" if last == day else f"до {end:%d.%m %H:%M}"
        row = Row(
            "event",
            e.id,
            e.text,
            time=f"{start:%H:%M}" if e.starts_at else ALL_DAY,
            all_day=not e.starts_at,
            note=note,
            who=family.display_name(e.who) if e.who else "",
        )
        place(day, (1, start.timestamp(), e.id) if e.starts_at else (0, 0.0, e.id), row)

    for t in todos:
        if not t.is_open or not (t.due or t.week):
            continue
        who = family.display_name(t.owner) if t.owner else ""
        project = names.get(t.project_id, "") if t.project_id is not None else ""
        if t.due:
            due = date.fromisoformat(t.due)
            if due < today:
                row = Row("todo", t.id, t.text, note=f"{due:%d.%m}", who=who, project=project)
                overdue.append(((due, t.id), row))
            else:
                place(due, (2, 0.0, t.id), Row("todo", t.id, t.text, who=who, project=project))
            continue
        monday = date.fromisoformat(t.week or "")
        note = week_note(monday, today, in_week=True)
        row = Row("todo", t.id, t.text, note=note, who=who, project=project)
        if monday >= horizon:  # planned further ahead than the page shows weeks: on its Monday
            place(monday, (3, 0.0, t.id), row)
        else:
            (nxt if monday == nxt.start else this).rows.append(row)

    this.overdue = [row for _, row in sorted(overdue, key=lambda pair: pair[0])]
    plan = Plan()
    for day in sorted(by_day):
        d = Day(
            day,
            day_title(day, today),
            [row for _, row in sorted(by_day[day], key=lambda p: p[0])],
            today=day == today,
        )
        if day >= horizon:
            plan.later.append(d)
        else:
            (nxt if day >= nxt.start else this).days.append(d)
    plan.weeks = [this] if nxt.empty else [this, nxt]
    return plan


def undated_groups(
    todos: list[Todo], family: Family, projects: list[Project] | None = None
) -> list[Group]:
    """The open todos without a day or a week, for the lists under the weeks: the ones
    without a project first («Без дати», the loose ends the bot nudges about), then a group
    per open project in the projects' order, empty ones too. Within a group the order they
    come in: `open_todos()` gives the hand-set one."""
    projects = projects or []
    groups: dict[int | None, list[Row]] = {}  # project id -> its rows, in order
    for t in todos:
        if t.is_open and not t.due and not t.week:
            who = family.display_name(t.owner) if t.owner else ""
            groups.setdefault(t.project_id, []).append(Row("todo", t.id, t.text, who=who))
    # A todo of a closed project would have been detached; one of an unknown project (never
    # the case) falls in with the ones without.
    by_project = [Group(p.name, groups.pop(p.id, []), p.id) for p in projects]
    rest = [row for rows in groups.values() for row in rows]
    return [Group("", rest), *by_project]


def reminder_rows(reminders: list[Reminder], now: datetime, family: Family) -> list[Row]:
    """The pending reminders for «Нагадування» on the web home, its own block under the
    weeks (2026-09-18; they sat among the dated todos for a day and mixed badly with
    them): «⏰» rows nobody taps, by time, the note 'завтра 19:30 · щодня'; the repeating
    ones at the end, since they come round again whatever anyone does. One whose time
    passed but is still pending (about to be sent) counts as today's."""
    tz = now.tzinfo
    assert isinstance(tz, ZoneInfo)
    today = now.date()
    rows: list[tuple[tuple, Row]] = []
    for r in reminders:
        if not r.is_pending:
            continue
        at = parse_iso(r.at).astimezone(tz)
        day = max(at.date(), today)
        repeat = REPEAT_LABELS.get(r.repeat or "")
        note = " · ".join(filter(None, [f"{due_note(day, today)} {at:%H:%M}", repeat]))
        who = family.display_name(r.who) if r.who else "усім"
        rows.append(
            ((bool(repeat), at.timestamp()), Row("reminder", r.id, r.text, note=note, who=who))
        )
    return [row for _, row in sorted(rows, key=lambda pair: pair[0])]


# --- search -------------------------------------------------------------------

_WORD = re.compile(r"\w+", re.UNICODE)

# Function words: they carry no meaning for search and would match half the database.
# (Kept as text: ruff's SIM905 turns a literal `"...".split()` into a hundred-line list.)
_STOPWORDS_TEXT = """
він вона воно вони мене тебе себе мені тобі собі нам вам нас вас його них ним нею йому
мій моя моє мої твій твоя твоє твої наш наша наше наші ваш ваша ваше ваші свій своя своє
свої цей цього цієї цьому той того тієї тому але або щоб коли куди звідки хто кого кому
чого чому від для про при під над без між через після перед біля коло крім ще вже теж
також тільки лише дуже там тут так ось був була було були буде бути треба можна потім
зараз сьогодні завтра вчора якщо який яка яке які весь вся все всі усе усі ага дякую будь
ласка два дві три один одна одне одну
"""
STOPWORDS_UK = frozenset(_STOPWORDS_TEXT.split())
# Inflection endings, longest first; one is cut when enough of the word remains.
ENDINGS_UK = (
    "ами", "ями", "ові", "еві", "єві", "ого", "ому", "ему", "єму", "ими", "іми", "їми", "ьми",
    "ьої", "ьою", "ах", "ях", "ам", "ям", "ою", "ею", "єю", "ів", "їв", "ей", "ий", "ій", "им",
    "ім", "їм", "их", "іх", "ом", "ем", "єм", "ої", "а", "я", "у", "ю", "и", "і", "ї", "е",
    "є", "о", "ь",
)  # fmt: skip


def stem(word: str) -> str | None:
    """A search prefix for a Ukrainian word: casefolded, ending cut, at most 5 chars.

    A cheap stand-in for stemming, tuned for names and nouns: «діти» / «дітям» / «дітьми» →
    «діт», «Коля» / «Колі» / «Колею» → «кол», «газовик» / «газовика» → «газов». Fleeting
    vowels («котел» / «котла») are not handled. None for function words, digits and stubs.
    """
    w = word.casefold()
    if len(w) < 3 or w.isdigit() or w in STOPWORDS_UK:
        return None
    keep = 2 if len(w) == 3 else 3  # «Оля» → «ол»: three-letter names inflect too
    for ending in ENDINGS_UK:
        if w.endswith(ending) and len(w) - len(ending) >= keep:
            w = w[: -len(ending)]
            break
    return w[:5]


def stems(text: str, max_terms: int = 12) -> list[str]:
    """Distinct stems of the words in `text`, in order of appearance."""
    out: list[str] = []
    for word in _WORD.findall(text):
        s = stem(word)
        if s and s not in out:
            out.append(s)
        if len(out) >= max_terms:
            break
    return out


def word_pattern(text: str, max_terms: int = 12) -> str | None:
    """The stems as a regex for `ufold(text) REGEXP ?`: a word starting with any of them."""
    terms = stems(text, max_terms)
    return r"\b(?:" + "|".join(re.escape(s) for s in terms) + ")" if terms else None


def notes_sections(text: str) -> list[str]:
    """The page split at its «## …» headings, each section with its heading; the text
    before the first heading is a section of its own."""
    sections: list[str] = []
    for line in text.splitlines():
        if line.startswith("## ") or not sections:
            sections.append(line)
        else:
            sections[-1] += "\n" + line
    return [sec.strip() for sec in sections if sec.strip()]


def search_notes(text: str, pattern: str) -> list[str]:
    """The sections of the page with a word starting with one of the stems (`pattern` is
    from `word_pattern`, matched on the casefolded text as the db's REGEXP does)."""
    regex = re.compile(pattern)
    return [sec for sec in notes_sections(text) if regex.search(sec.casefold())]


# --- context ------------------------------------------------------------------


def item_line(i: Item, with_id: bool = True) -> str:
    """'[#3] Паспорт Олі (Оля) → квартира / білий комод; до 2031'."""
    head = f"[#{i.id}] " if with_id else ""
    owner = f" ({i.owner})" if i.owner else ""
    note = f"; {i.note}" if i.note else ""
    return f"{head}{i.name}{owner} → {i.location or 'місце невідоме'}{note}"


def _message_line(msg: Message, family: Family, tz: ZoneInfo) -> str:
    when = fmt_dt(msg.created_at, tz)
    if msg.user_id == "bot":
        who = f"бот → {family.display_name(msg.chat_with)}"
    else:
        who = family.display_name(msg.user_id)
    kind = " (голосове)" if msg.is_voice else " (з фото)" if msg.photo_file_id else ""
    return f"[{when}] {who}{kind}: {msg.raw_text}"


def _incoming_line(author: Member, text: str, with_photo: bool, is_voice: bool) -> str:
    """Voice is said so: the prompt keeps typed words as they are and only tidies a
    transcript, which is noisy."""
    who = f"від {author.id} ({author.name})"
    if with_photo:
        return f"{who}, з фото (підпис нижче):\n{text or '(без підпису)'}"
    if is_voice:
        return f"{who}, голосове (розпізнаний текст):\n{text}"
    return f"{who}, текстом:\n{text}"


def build_context(
    db: Database,
    family: Family,
    now: datetime,
    author: Member,
    text: str,
    *,
    with_photo: bool = False,
    is_voice: bool = False,
) -> str:
    """Assemble everything the LLM needs for one message. With a photo, `text` is its caption
    and the image itself is sent as a separate block before this text."""
    tz = now.tzinfo
    assert isinstance(tz, ZoneInfo)
    since = (now - timedelta(days=RECENT_WINDOW_DAYS)).astimezone(ZoneInfo("UTC"))
    since_iso = since.isoformat(timespec="seconds").replace("+00:00", "Z")

    agenda = build_agenda(db.planned_events(), now)
    open_todos = db.open_todos()
    projects = db.open_projects()
    project_names = {p.id: p.name for p in projects}
    buckets = bucket_todos(open_todos, now)
    this_week = week_start(now.date())
    # The inventory can be long; the LLM sees only what just changed and what the message
    # is about. The rest is on the web.
    recent_items = db.items_changed_since(since_iso)
    pattern = word_pattern(text)
    seen = {i.id for i in recent_items}
    item_hits = [
        i
        for i in (db.search_items(pattern, limit=ITEM_HITS + len(seen)) if pattern else [])
        if i.id not in seen
    ][:ITEM_HITS]
    places = ", ".join(f"{p} ({n})" for p, n in db.places() if p)
    recent_messages = db.recent_messages(RECENT_MESSAGES)
    facts = db.current_facts()
    facts_text = facts.text.strip() if facts else ""

    def section(title: str, lines: list[str], empty: str = "немає") -> str:
        body = "\n".join(lines) if lines else empty
        return f"## {title}\n{body}"

    parts = [
        section(
            "Зараз",
            [
                f"{now.strftime('%Y-%m-%d %H:%M')} ({tz.key}), {WEEKDAYS_UK[now.weekday()]}",
                f"Цей тиждень (week: this), з понеділка по неділю: {week_days(this_week)}",
                f"Наступний тиждень (week: next): {week_days(this_week + WEEK)}",
            ],
        ),
        section("Сім'я (пишуть боту; решта людей — у фактах)", [family.describe()]),
        section(
            "Факти про сім'ю (веде людина, стабільний фон)",
            [facts_text] if facts_text else [],
            empty="поки порожньо",
        ),
        section(
            "Нотатки (notes: одна довідкова сторінка сім'ї в Markdown, ведеш ти; змінюється лише"
            " на явне прохання, повертай повний текст)",
            notes_context_lines(db.current_notes(), family, tz),
            empty="поки порожньо",
        ),
        section(
            f"Події (минулі за {PAST_EVENT_DAYS} днів і всі майбутні)",
            [f"- {event_line(e, family, tz)}" for e in agenda.recent + agenda.upcoming],
        ),
        section(
            "Нагадування (заплановані, ще не надіслані)",
            [f"- {reminder_line(r, family, tz)}" for r in db.pending_reminders()],
        ),
        section(
            "Проєкти (projects: групи задач для вебу, лише назва; створюються, перейменовуються"
            " і закриваються лише на явне прохання)",
            project_context_lines(projects, open_todos),
            empty="поки жодного",
        ),
        section(
            "Відкриті задачі (todos, усі: з днем, з тижнем чи без дати; у порядку з вебу)",
            [
                f"- {todo_line(t, family, projects=project_names, today=now.date())}"
                for t in open_todos
            ],
        ),
        section(
            "Сьогодні / прострочено / цей тиждень", [render_digest(agenda, buckets, family, now)]
        ),
        section(
            "Мрії (dreams: спільний список, хто додав; здійснені лише на вебі)",
            [f"- {dream_line(d, family)}" for d in db.open_dreams()],
        ),
        section(
            f"Речі (items), змінені за останні {RECENT_WINDOW_DAYS} дні",
            [f"- {item_line(i)}" for i in recent_items],
        ),
        section("Речі, схожі на повідомлення", [f"- {item_line(i)}" for i in item_hits]),
        section("Відомі місця (place), де лежать речі", [places] if places else [], "поки жодного"),
        section(
            "Останні повідомлення",
            [_message_line(m, family, tz) for m in recent_messages],
        ),
        section("Нове повідомлення", [_incoming_line(author, text, with_photo, is_voice)]),
    ]
    return "\n\n".join(parts)
