"""Schedule helper periods -> Nest schedule bucket (no Home Assistant needed)."""

from __future__ import annotations

from datetime import time

from custom_components.nest_local.nest_schedule import (
    build_setpoints,
    describe_schedule,
    fingerprint,
    nest_schedule_value,
    read_setpoints,
    same_schedule,
)

H = 3600


def period(start: str, end: str, **data: float) -> dict:
    result: dict = {"from": start, "to": end}
    if data:
        result["data"] = data
    return result


def test_one_period_per_day() -> None:
    helper = {
        "monday": [period("10:00:00", "18:00:00")],
        "tuesday": [period("07:00:00", "09:00:00"), period("17:00:00", "22:30:00")],
    }
    assert build_setpoints(helper, 21, 16) == [
        (0, 10 * H, 21.0),
        (0, 18 * H, 16.0),
        (1, 7 * H, 21.0),
        (1, 9 * H, 16.0),
        (1, 17 * H, 21.0),
        (1, 22 * H + 1800, 16.0),
    ]


def test_period_temperature_and_adjacent_periods() -> None:
    helper = {
        "wednesday": [
            period("06:00:00", "08:00:00", temperature=22.5),
            period("08:00:00", "12:00:00"),
            period("12:00:00", "13:00:00"),  # same temperature: no extra setpoint
        ]
    }
    assert build_setpoints(helper, 20, 15) == [
        (2, 6 * H, 22.5),
        (2, 8 * H, 20.0),
        (2, 13 * H, 15.0),
    ]


def test_period_to_midnight_continues_into_next_day() -> None:
    helper = {
        "sunday": [period("20:00:00", "24:00:00")],
        "monday": [period("00:00:00", "07:00:00")],
    }
    # Sunday 20:00 to Monday 07:00 is one warm stretch across the week's end.
    assert build_setpoints(helper, 21, 16) == [(0, 7 * H, 16.0), (6, 20 * H, 21.0)]


def test_time_objects_from_the_helper() -> None:
    helper = {"friday": [{"from": time(9, 15), "to": time.max}]}
    assert build_setpoints(helper, 21, 16) == [(4, 9 * H + 900, 21.0), (5, 0, 16.0)]


def test_period_temperature_as_text() -> None:
    helper = {
        "monday": [
            {"from": "10:00", "to": "11:00", "data": {"temperature": "22,5"}},
            {"from": "12:00", "to": "13:00", "data": {"temperature": "warm"}},
        ]
    }
    assert build_setpoints(helper, 21, 16) == [
        (0, 10 * H, 22.5),
        (0, 11 * H, 16.0),
        (0, 12 * H, 21.0),
        (0, 13 * H, 16.0),
    ]


def test_limits_and_empty_helper() -> None:
    assert build_setpoints({}, 21, 16) == [(0, 0, 16.0)]
    helper = {"monday": [period("10:00", "11:00", temperature=40)]}
    assert build_setpoints(helper, 21, 3) == [(0, 10 * H, 32.0), (0, 11 * H, 9.0)]
    # Zero-length and reversed periods are ignored.
    assert build_setpoints({"monday": [period("10:00", "10:00")]}, 21, 16) == [(0, 0, 16.0)]


def test_bucket_value_and_comparison() -> None:
    points = [(0, 10 * H, 21.0), (0, 18 * H, 16.0), (3, 7 * H, 20.0)]
    value = nest_schedule_value(points)
    assert value["ver"] == 2
    assert value["schedule_mode"] == "HEAT"
    assert set(value["days"]) == {"0", "1", "2", "3", "4", "5", "6"}
    assert value["days"]["0"] == {
        "0": {"type": "HEAT", "time": 36000, "entry_type": "setpoint", "temp": 21.0},
        "1": {"type": "HEAT", "time": 64800, "entry_type": "setpoint", "temp": 16.0},
    }
    assert value["days"]["1"] == {}
    assert read_setpoints(value) == points

    # The thermostat adds continuation entries and its own float noise.
    stored = nest_schedule_value(points)
    stored["days"]["1"]["0"] = {
        "type": "HEAT",
        "time": 0,
        "entry_type": "continuation",
        "temp": 16.0,
    }
    stored["days"]["3"]["0"]["temp"] = 20.000001
    assert same_schedule(value, stored)
    assert fingerprint(value) == fingerprint(stored)

    stored["days"]["3"]["0"]["time"] = 8 * H
    assert not same_schedule(value, stored)
    assert not same_schedule(value, {**value, "schedule_mode": "COOL"})
    assert not same_schedule(value, {})


def test_describe() -> None:
    value = nest_schedule_value([(0, 10 * H, 21.0), (0, 18 * H + 30, 16.0)])
    value["days"]["2"] = {
        "0": {"type": "RANGE", "time": 0, "entry_type": "setpoint", "temp-min": 18, "temp-max": 24}
    }
    described = describe_schedule(value)
    assert described["mode"] == "heat"
    assert described["days"]["monday"] == [
        {"time": "10:00", "temperature": 21.0},
        {"time": "18:00:30", "temperature": 16.0},
    ]
    assert described["days"]["wednesday"] == [
        {"time": "00:00", "temperature_low": 18.0, "temperature_high": 24.0}
    ]
    assert described["days"]["sunday"] == []
    assert describe_schedule({}) == {
        "mode": None,
        "days": {
            day: []
            for day in (
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            )
        },
    }
