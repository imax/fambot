from datetime import date, datetime, time

import httpx

from family_ea import outages
from family_ea.outages import (
    Source,
    day_line,
    fetch,
    fetch_all,
    outage_lines,
    parse_sources,
    spans,
    with_outages,
)
from tests.conftest import KYIV

TODAY = date(2026, 10, 27)
HOME = Source("kyiv-region", "3.2")
FLAT = Source("kyiv", "12.1", "Квартира")


def _ts(day: date) -> str:
    return str(int(datetime.combine(day, time(), KYIV).timestamp()))


def _hours(**changed: str) -> dict[str, str]:
    hours = {str(h): "yes" for h in range(1, 25)}
    hours.update({k.lstrip("h"): v for k, v in changed.items()})
    return hours


def _data(*days: tuple[date, dict[str, str]], group: str = "GPV3.2") -> dict:
    return {"fact": {"data": {_ts(d): {group: h, "GPV3.1": _hours()} for d, h in days}}}


def test_parse_sources() -> None:
    assert parse_sources(None, "kyiv-region") == ()
    assert parse_sources("3.2", "kyiv-region") == (HOME,)
    assert parse_sources(" Дім = kyiv-region/3.2 , Квартира=kyiv/12.1,", "x") == (
        Source("kyiv-region", "3.2", "Дім"),
        FLAT,
    )


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
    assert outage_lines(data, HOME, TODAY, KYIV) == [
        "Світло, черга 3.2:",
        "- сьогодні немає 13:00–14:00",
    ]
    data = _data((TODAY, _hours(h14="no")), (date(2026, 10, 28), _hours()))
    assert outage_lines(data, HOME, TODAY, KYIV)[-1] == "- завтра без відключень"
    flat = _data((TODAY, _hours()), group="GPV12.1")
    assert outage_lines(flat, FLAT, TODAY, KYIV)[0] == "Світло, Квартира, черга 12.1:"


def test_no_schedule_for_today_says_nothing() -> None:
    stale = _data((date(2026, 10, 26), _hours(h14="no")))
    assert outage_lines(stale, HOME, TODAY, KYIV) == []
    assert outage_lines(_data((TODAY, _hours())), Source("x", "9.9"), TODAY, KYIV) == []
    assert outage_lines(None, HOME, TODAY, KYIV) == []
    assert outage_lines({"oops": 1}, HOME, TODAY, KYIV) == []


def test_with_outages_puts_the_blocks_on_top_and_keeps_a_quiet_morning_silent() -> None:
    off = _data((TODAY, _hours(h14="no")))
    light = _data((TODAY, _hours()))
    assert with_outages("Сьогодні:\n- x", [(HOME, off)], TODAY, KYIV) == (
        "Світло, черга 3.2:\n- сьогодні немає 13:00–14:00\nСьогодні:\n- x"
    )
    assert with_outages(None, [(HOME, off)], TODAY, KYIV) == (
        "Світло, черга 3.2:\n- сьогодні немає 13:00–14:00"
    )
    assert with_outages(None, [(HOME, light)], TODAY, KYIV) is None
    assert with_outages("Сьогодні:\n- x", [(HOME, light)], TODAY, KYIV).startswith(
        "Світло, черга 3.2:\n- сьогодні без відключень\n"
    )
    assert with_outages("Сьогодні:\n- x", [(HOME, None)], TODAY, KYIV) == "Сьогодні:\n- x"


def test_two_addresses_one_block_each_and_one_outage_is_enough_to_send() -> None:
    flat = _data((TODAY, _hours(h10="no")), group="GPV12.1")
    text = with_outages(None, [(HOME, _data((TODAY, _hours()))), (FLAT, flat)], TODAY, KYIV)
    assert text == (
        "Світло, черга 3.2:\n- сьогодні без відключень\n"
        "Світло, Квартира, черга 12.1:\n- сьогодні немає 09:00–10:00"
    )
    # One address unavailable: the other one still shows.
    assert with_outages(None, [(HOME, None), (FLAT, flat)], TODAY, KYIV).startswith(
        "Світло, Квартира"
    )


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


async def test_fetch_all_fetches_each_region_once() -> None:
    seen = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"r": request.url.path})

    other = Source("kyiv-region", "3.1", "Дача")
    found = await fetch_all((HOME, FLAT, other), httpx.MockTransport(ok))
    assert seen == ["kyiv-region.json", "kyiv.json"]
    assert [s for s, _ in found] == [HOME, FLAT, other]
    assert found[0][1] == found[2][1]
