from datetime import date, datetime, time

import httpx

from family_ea import outages
from family_ea.outages import day_line, fetch, outage_lines, spans, with_outages
from tests.conftest import KYIV

TODAY = date(2026, 10, 27)


def _ts(day: date) -> str:
    return str(int(datetime.combine(day, time(), KYIV).timestamp()))


def _hours(**changed: str) -> dict[str, str]:
    hours = {str(h): "yes" for h in range(1, 25)}
    hours.update({k.lstrip("h"): v for k, v in changed.items()})
    return hours


def _data(*days: tuple[date, dict[str, str]]) -> dict:
    return {"fact": {"data": {_ts(d): {"GPV3.2": h, "GPV3.1": _hours()} for d, h in days}}}


def test_spans_join_hours_and_halves() -> None:
    # 13:00-17:00 off, 17:30-18:00 off (second half), 20:00-21:30 maybe.
    hours = _hours(h14="no", h15="no", h16="no", h17="no", h18="second", h21="maybe", h22="mfirst")
    assert spans(hours) == [
        ("off", "13:00", "17:00"),
        ("off", "17:30", "18:00"),
        ("maybe", "20:00", "21:30"),
    ]
    assert spans(_hours(h24="no")) == [("off", "23:00", "24:00")]
    assert spans(_hours(h1="first", h24="msecond")) == [
        ("off", "00:00", "00:30"),
        ("maybe", "23:30", "24:00"),
    ]


def test_day_line() -> None:
    assert day_line(_hours()) == "без відключень"
    assert day_line(_hours(h14="no", h21="maybe")) == "немає 13:00–14:00, можливо 20:00–21:00"


def test_the_block_has_today_and_tomorrow_once_published() -> None:
    data = _data((TODAY, _hours(h14="no")))
    assert outage_lines(data, "3.2", TODAY, KYIV) == [
        "Світло, черга 3.2:",
        "- сьогодні немає 13:00–14:00",
    ]
    data = _data((TODAY, _hours(h14="no")), (date(2026, 10, 28), _hours()))
    assert outage_lines(data, "3.2", TODAY, KYIV)[-1] == "- завтра без відключень"


def test_no_schedule_for_today_says_nothing() -> None:
    stale = _data((date(2026, 10, 26), _hours(h14="no")))
    assert outage_lines(stale, "3.2", TODAY, KYIV) == []
    assert outage_lines(_data((TODAY, _hours())), "9.9", TODAY, KYIV) == []
    assert outage_lines(None, "3.2", TODAY, KYIV) == []
    assert outage_lines({"oops": 1}, "3.2", TODAY, KYIV) == []


def test_with_outages_puts_the_block_on_top_and_keeps_a_quiet_morning_silent() -> None:
    off = _data((TODAY, _hours(h14="no")))
    light = _data((TODAY, _hours()))
    assert with_outages("Сьогодні:\n- x", off, "3.2", TODAY, KYIV) == (
        "Світло, черга 3.2:\n- сьогодні немає 13:00–14:00\nСьогодні:\n- x"
    )
    assert with_outages(None, off, "3.2", TODAY, KYIV) == (
        "Світло, черга 3.2:\n- сьогодні немає 13:00–14:00"
    )
    assert with_outages(None, light, "3.2", TODAY, KYIV) is None
    assert with_outages("Сьогодні:\n- x", light, "3.2", TODAY, KYIV).startswith(
        "Світло, черга 3.2:\n- сьогодні без відключень\n"
    )
    assert with_outages("Сьогодні:\n- x", None, "3.2", TODAY, KYIV) == "Сьогодні:\n- x"


async def test_fetch_reads_the_region_file_and_survives_failure() -> None:
    seen = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"fact": {}})

    assert await fetch("kyiv-region", httpx.MockTransport(ok)) == {"fact": {}}
    assert seen == [outages.SOURCE.format(region="kyiv-region")]
    down = httpx.MockTransport(lambda r: httpx.Response(503))
    assert await fetch("kyiv-region", down) is None
    junk = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))
    assert await fetch("kyiv-region", junk) is None
