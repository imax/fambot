"""Deterministic context for the LLM, the event agenda, todo buckets, the digest,
the calendar and the todo lists of the web home.

Everything here is plain code: what is "today", what is "overdue", which items to
show. The LLM only sees the result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .db import Board, Database, Dream, Event, Item, Member, Message, Notes, Project, Reminder, Todo
from .family import Family

RECENT_WINDOW_DAYS = 2  # items changed this recently are in every LLM context; older: search
ITEM_HITS = 20  # items found by the message's words
RECENT_MESSAGES = 20
PAST_EVENT_DAYS = 7  # ended events stay in the LLM context this long ("коли був стоматолог?")
ALL_DAY = "весь день"  # the calendar's label where a time would be
HORIZON_DAYS = 14  # the web calendar shows this many days ahead in full; the rest is a line
DEFAULT_EVENT_DURATION = timedelta(hours=1)
WEEKDAYS_UK = ("понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя")


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
    open: list[Todo] = field(default_factory=list)  # no deadline
    later: list[Todo] = field(default_factory=list)  # a deadline after today


def bucket_todos(items: list[Todo], now: datetime) -> Buckets:
    """Split open todos into today / overdue / open / later relative to `now`.

    `now` must be timezone-aware in the family timezone; "today" is its date. Dated ones
    are sorted by deadline, the undated keep their order (the hand-set one).
    """
    today = now.date().isoformat()
    b = Buckets()
    for t in items:
        if not t.is_open:
            continue
        if not t.due:
            b.open.append(t)
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
    t: Todo, family: Family, with_id: bool = True, projects: dict[int, str] | None = None
) -> str:
    """`projects` (id -> name) adds «проєкт: Авто» to the meta; the digest passes none."""
    parts = [f"[#{t.id}] " if with_id else "", t.text]
    meta = []
    if t.owner:
        meta.append(family.display_name(t.owner))
    if t.due:
        meta.append(fmt_due(t))
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


# --- boards: «на сьогодні» and «Не забути» ----------------------------------


def stale_label(iso_utc: str, now: datetime) -> str:
    """How old a board is: '' when changed today, 'вчора', else '09.09'."""
    day = parse_iso(iso_utc).astimezone(now.tzinfo).date()
    if day == now.date():
        return ""
    return "вчора" if day == now.date() - timedelta(days=1) else day.strftime("%d.%m")


@dataclass(frozen=True)
class BoardBlock:
    """One member's board, ready to render; the web and the digest only lay it out."""

    member: str
    name: str
    text: str  # '' when there is no board or it was cleared
    stale: str  # '' when empty or changed today; 'вчора'; '09.09'


def board_blocks(
    lists: dict[str, Board], family: Family, viewer: str | None, now: datetime
) -> list[BoardBlock]:
    """Every member's board (of one kind), the viewer's own first."""
    blocks = []
    for m in sorted(family.members, key=lambda m: m.id != viewer):
        board = lists.get(m.id)
        text = board.text.strip() if board else ""
        stale = stale_label(board.created_at, now) if board and text else ""
        blocks.append(BoardBlock(m.id, m.name, text, stale))
    return blocks


def today_lines(blocks: list[BoardBlock], viewer: str) -> list[str]:
    """The digest's head: 'На сьогодні (твоє):' then the others', a line per line of text.
    Empty boards are skipped."""
    lines: list[str] = []
    for b in blocks:
        if not b.text:
            continue
        who = "твоє" if b.member == viewer else b.name
        note = f", оновлено {b.stale}" if b.stale else ""
        lines.append(f"На сьогодні ({who}{note}):")
        lines += [f"- {ln.strip()}" for ln in b.text.splitlines() if ln.strip()]
    return lines


def board_context_lines(lists: dict[str, Board], family: Family, tz: ZoneInfo) -> list[str]:
    """For the LLM: every member's board (of one kind) with id, name and when it changed;
    text as kept."""
    lines = []
    for m in family.members:
        board = lists.get(m.id)
        text = board.text.strip() if board else ""
        if not board or not text:
            lines.append(f"- {m.id} ({m.name}): порожньо")
            continue
        body = "\n".join(f"  {ln}" for ln in text.splitlines())
        lines.append(f"- {m.id} ({m.name}), оновлено {fmt_dt(board.created_at, tz)}:\n{body}")
    return lines


# --- notes --------------------------------------------------------------------


def notes_context_lines(notes: Notes | None, family: Family, tz: ZoneInfo) -> list[str]:
    """For the LLM: when and by whom the page last changed, then its text as kept."""
    if notes is None or not notes.text.strip():
        return []
    who = family.display_name(notes.created_by)
    return [f"(оновлено {fmt_dt(notes.created_at, tz)}, {who})", notes.text.strip()]


# --- digest -------------------------------------------------------------------


def _digest_lines(
    a: Agenda,
    b: Buckets,
    family: Family,
    tz: ZoneInfo,
    *,
    with_ids: bool,
    include_open: bool,
    max_open: int,
    today: list[str],
) -> list[str]:
    def ev(e: Event) -> str:
        return f"- {event_line(e, family, tz, with_id=with_ids, with_date=False)}"

    def td(t: Todo) -> str:
        return f"- {todo_line(t, family, with_id=with_ids)}"

    lines: list[str] = [*today]
    if a.today:
        lines += ["Сьогодні:", *map(ev, a.today)]
    if a.tomorrow:
        lines += ["Завтра:", *map(ev, a.tomorrow)]
    if b.today:
        lines += ["Задачі на сьогодні:", *map(td, b.today)]
    if b.overdue:
        lines += ["Прострочено:", *map(td, b.overdue)]
    if include_open and b.open:
        lines += ["Без дати:", *map(td, b.open[:max_open])]
        if len(b.open) > max_open:
            lines.append(f"- і ще {len(b.open) - max_open}")
    return lines


def render_digest(a: Agenda, b: Buckets, family: Family, tz: ZoneInfo, max_open: int = 5) -> str:
    """Today / tomorrow / due / overdue / open, with ids, for the LLM context."""
    lines = _digest_lines(
        a, b, family, tz, with_ids=True, include_open=True, max_open=max_open, today=[]
    )
    return "\n".join(lines) if lines else "нічого"


def digest_text(
    a: Agenda,
    b: Buckets,
    family: Family,
    tz: ZoneInfo,
    *,
    today: list[str] | None = None,
) -> str | None:
    """The morning push, and /today: the boards (`today`, from today_lines, the recipient's
    own first), today's and tomorrow's events, today's and overdue todos. Never the
    undated ones: they live on the web. None when there is nothing to say: an empty morning
    stays silent. Deterministic on purpose: presentation, not understanding."""
    lines = _digest_lines(
        a, b, family, tz, with_ids=False, include_open=False, max_open=0, today=today or []
    )
    if not lines:
        return None
    return "\n".join(lines)


# --- the calendar and the todo lists (the web) ----------------------------------


@dataclass(frozen=True)
class Row:
    """One line of the web home (the calendar or a todo list), ready to render: a planned event, a
    pending reminder or an open todo. Everything is formatted here; the template only lays
    it out."""

    kind: str  # 'event' | 'reminder' | 'todo'
    id: int
    text: str
    time: str = ""  # '16:00' (a start); ALL_DAY for an all-day event; '' for a todo
    all_day: bool = False  # the template styles the label, not a time
    note: str = ""  # 'до 17:00' / 'до 19.09': an end still ahead; a todo's deadline
    who: str = ""  # display name; 'усім' for a reminder to everyone; '' when nobody in particular
    ics_url: str | None = None  # «📅»: an event or a dated todo
    project: str = ""  # a dated or overdue todo's project name; undated ones sit in its group


@dataclass
class Day:
    """One day of the web calendar: its heading ('Сьогодні, четвер 10.09', marked when
    today) and its rows."""

    when: date
    title: str
    rows: list[Row] = field(default_factory=list)
    today: bool = False


@dataclass
class Calendar:
    """The calendar on the web home: the next two weeks day by day, and everything after
    them as one line that unfolds into the same days ('далі: 07.10 Стрижка · …'). Most days
    hold one row, so a long tail of single events would push the todos off the screen."""

    days: list[Day] = field(default_factory=list)
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


@dataclass
class TodoLists:
    """The open todos on the web home: past their day, with a day from today on (and the
    pending reminders among them), and without one, in the hand-set order, a group per
    open project (in the order the projects were made) and the ones without a project
    last; one group with no name when there are no projects. The calendar is `build_calendar`."""

    overdue: list[Row] = field(default_factory=list)
    dated: list[Row] = field(default_factory=list)
    undated: list[Group] = field(default_factory=list)


def day_title(d: date, today: date) -> str:
    """'Сьогодні, четвер 10.09', 'Завтра, п'ятниця 11.09', 'Середа 07.10'."""
    name = WEEKDAYS_UK[d.weekday()]
    if d == today:
        return f"Сьогодні, {name} {d:%d.%m}"
    if d == today + timedelta(days=1):
        return f"Завтра, {name} {d:%d.%m}"
    return f"{name.capitalize()} {d:%d.%m}"


def due_note(due: date, today: date) -> str:
    """A todo's day next to its text: 'сьогодні', 'завтра', else '19.09'. No «до»: a todo
    has one day, there is no deadline apart from it (2026-09-17)."""
    if due == today:
        return "сьогодні"
    if due == today + timedelta(days=1):
        return "завтра"
    return f"{due:%d.%m}"


def build_calendar(events: list[Event], now: datetime, family: Family) -> Calendar:
    """The calendar on the web home: every day with a planned event, today always, even
    empty; the days past `HORIZON_DAYS` go to `later`, shown as one line until someone
    unfolds them. Events only, where someone has to be: a reminder is a push to do
    something and sits with the dated todos (`build_todo_lists`, 2026-09-17).

    A multi-day event still running sits on today with «до …». Within a day: all-day
    events, then timed ones by time.
    """
    tz = now.tzinfo
    assert isinstance(tz, ZoneInfo)
    today = now.date()
    by_day: dict[date, list[tuple[tuple, Row]]] = {today: []}

    def place(day: date, key: tuple, row: Row) -> None:
        by_day.setdefault(day, []).append((key, row))

    def until_note(last: date, day: date) -> str:
        return f"до {last:%d.%m}" if last > day else ""

    for e in events:
        if not e.is_planned:
            continue
        start, end = event_span(e, tz)
        first, last = start.date(), (end - timedelta(seconds=1)).date()
        if last < today:
            continue
        day = max(first, today)
        if not e.starts_at:
            note = until_note(last, day)
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
            ics_url=f"/events/{e.id}.ics",
        )
        place(day, (1, start.timestamp(), 0, e.id) if e.starts_at else (0, 0.0, 0, e.id), row)

    horizon = today + timedelta(days=HORIZON_DAYS)
    cal = Calendar()
    for day in sorted(by_day):
        d = Day(
            day,
            day_title(day, today),
            [row for _, row in sorted(by_day[day], key=lambda p: p[0])],
            today=day == today,
        )
        (cal.days if day <= horizon else cal.later).append(d)
    return cal


def build_todo_lists(
    todos: list[Todo],
    now: datetime,
    family: Family,
    projects: list[Project] | None = None,
    reminders: list[Reminder] | None = None,
) -> TodoLists:
    """Sort the open todos out for the home page.

    Todos past their day are overdue, oldest first; the others with a day come by day,
    nearest first («Не забути» on the web); the undated keep the order they come in:
    `open_todos()` gives the hand-set one.

    The pending reminders sit among the dated todos, «⏰» for «☐»: on their day after its
    todos, by time, the note 'завтра 19:30 · щодня'. One whose time passed but is still
    pending (about to be sent) counts as today's.
    """
    tz = now.tzinfo
    assert isinstance(tz, ZoneInfo)
    today = now.date()
    overdue: list[tuple[tuple, Row]] = []
    dated: list[tuple[tuple, Row]] = []
    projects = projects or []
    names = {p.id: p.name for p in projects}
    groups: dict[int | None, list[Row]] = {}  # project id -> its undated rows, in order
    t = TodoLists()

    for td in todos:
        if not td.is_open:
            continue
        who = family.display_name(td.owner) if td.owner else ""
        if not td.due:
            groups.setdefault(td.project_id, []).append(Row("todo", td.id, td.text, who=who))
            continue
        due = date.fromisoformat(td.due)
        late = due < today
        row = Row(
            "todo",
            td.id,
            td.text,
            note=f"{due:%d.%m}" if late else due_note(due, today),
            who=who,
            ics_url=f"/todos/{td.id}.ics",
            project=names.get(td.project_id, "") if td.project_id is not None else "",
        )
        (overdue if late else dated).append(((due, 0, td.id), row))

    for r in reminders or []:
        if not r.is_pending:
            continue
        at = parse_iso(r.at).astimezone(tz)
        day = max(at.date(), today)
        note = " · ".join(
            filter(None, [f"{due_note(day, today)} {at:%H:%M}", REPEAT_LABELS.get(r.repeat or "")])
        )
        who = family.display_name(r.who) if r.who else "усім"
        dated.append(((day, 1, at.timestamp()), Row("reminder", r.id, r.text, note=note, who=who)))

    t.overdue = [row for _, row in sorted(overdue, key=lambda pair: pair[0])]
    t.dated = [row for _, row in sorted(dated, key=lambda pair: pair[0])]
    # A todo of a closed project would have been detached; one of an unknown project (never
    # the case) falls in with the ones without.
    by_project = [Group(p.name, groups.pop(p.id, []), p.id) for p in projects]
    rest = [row for pid, rows in groups.items() for row in rows]
    t.undated = [Group("", rest), *by_project]  # the loose ends first, then the projects
    return t


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
            [f"{now.strftime('%Y-%m-%d %H:%M')} ({tz.key}), {WEEKDAYS_UK[now.weekday()]}"],
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
            "Списки на сьогодні (today: дошка кожного, змінюється лише на явне прохання)",
            board_context_lines(db.current_today_lists(), family, tz),
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
            "Відкриті задачі (todos, усі; без дати — у порядку з вебу)",
            [f"- {todo_line(t, family, projects=project_names)}" for t in open_todos],
        ),
        section("Сьогодні / прострочено", [render_digest(agenda, buckets, family, tz)]),
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
