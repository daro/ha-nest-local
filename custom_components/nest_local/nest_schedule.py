"""Weekly schedules: Home Assistant's Schedule helper and the Nest schedule bucket.

The thermostat keeps its weekly schedule in the ``schedule.<serial>`` bucket::

    {"ver": 2, "name": "...", "schedule_mode": "HEAT",
     "days": {"0": {"0": {"type": "HEAT", "time": 36000,
                          "entry_type": "setpoint", "temp": 21.0}, ...},
              ...
              "6": {...}}}

Day "0" is Monday, ``time`` counts seconds after midnight and a setpoint holds
until the next one, also across days. Entries the thermostat adds itself have
``entry_type`` "continuation" and are not part of the schedule proper.

A Schedule helper lists the periods to heat on each weekday. Each period
becomes a setpoint at its start (the heating temperature, or the period's own
``temperature`` from its additional data) and one at its end (the
temperature outside the periods).

Nothing here imports Home Assistant, so it can be tested on its own.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import time
import hashlib
import json
from typing import Any

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DAY_SECONDS = 24 * 3600
SCHEDULE_NAME = "Home Assistant"
SCHEDULE_VERSION = 2
TEMPERATURE_KEY = "temperature"

# (day 0-6 from Monday, seconds after midnight, temperature in °C)
Setpoint = tuple[int, int, float]


def _seconds(value: Any) -> int:
    """Seconds after midnight for a time, or a "HH:MM[:SS]" string (24:00 allowed)."""
    if isinstance(value, time):
        if value == time.max:
            return DAY_SECONDS
        return value.hour * 3600 + value.minute * 60 + value.second
    parts = [int(part) for part in str(value).split(":")]
    parts += [0] * (3 - len(parts))
    return min(parts[0] * 3600 + parts[1] * 60 + parts[2], DAY_SECONDS)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def period_temperature(period: Mapping[str, Any]) -> float | None:
    """A period's own temperature (°C) from its additional data, if it has one."""
    data = period.get("data")
    if not isinstance(data, Mapping):
        return None
    value = data.get(TEMPERATURE_KEY)
    if isinstance(value, str):
        try:
            return float(value.replace(",", ".").strip())
        except ValueError:
            return None
    return _number(value)


def build_setpoints(
    helper_schedule: Mapping[str, Sequence[Mapping[str, Any]] | None],
    heat_temperature: float,
    setback_temperature: float,
    limits: tuple[float, float] = (9.0, 32.0),
) -> list[Setpoint]:
    """Turn Schedule helper periods into Nest setpoints, in week order."""

    def clamp(value: float) -> float:
        return round(min(max(value, limits[0]), limits[1]), 1)

    starts: dict[tuple[int, int], float] = {}
    ends: set[tuple[int, int]] = set()
    for day, weekday in enumerate(WEEKDAYS):
        for period in helper_schedule.get(weekday) or ():
            start = _seconds(period["from"])
            end = _seconds(period["to"])
            if end <= start:
                continue
            own = period_temperature(period)
            starts[(day, start)] = clamp(heat_temperature if own is None else own)
            # A period that runs to midnight ends at 00:00 the next day.
            ends.add((day, end) if end < DAY_SECONDS else ((day + 1) % 7, 0))

    points = dict(starts)
    for moment in ends:
        # A period starting exactly when another ends keeps its own setpoint.
        points.setdefault(moment, clamp(setback_temperature))
    if not points:
        # No periods at all: keep the lower temperature all week.
        return [(0, 0, clamp(setback_temperature))]

    ordered = sorted((day, seconds, temp) for (day, seconds), temp in points.items())
    # A setpoint equal to the one before it (the week wraps around) changes nothing.
    kept = [point for i, point in enumerate(ordered) if point[2] != ordered[i - 1][2]]
    return kept or ordered[:1]


def nest_schedule_value(setpoints: Sequence[Setpoint], mode: str = "HEAT") -> dict[str, Any]:
    """The complete ``schedule`` bucket value for a list of setpoints."""
    days: dict[str, dict[str, dict[str, Any]]] = {str(day): {} for day in range(7)}
    for day, seconds, temp in sorted(setpoints):
        entries = days[str(day)]
        entries[str(len(entries))] = {
            "type": mode,
            "time": seconds,
            "entry_type": "setpoint",
            "temp": temp,
        }
    return {
        "ver": SCHEDULE_VERSION,
        "name": SCHEDULE_NAME,
        "schedule_mode": mode,
        "days": days,
    }


def _entries(value: Mapping[str, Any]) -> list[tuple[int, Mapping[str, Any]]]:
    """(day, entry) for every real setpoint in a schedule bucket value."""
    days = value.get("days")
    if not isinstance(days, Mapping):
        return []
    result: list[tuple[int, Mapping[str, Any]]] = []
    for day_key, entries in days.items():
        try:
            day = int(day_key)
        except (TypeError, ValueError):
            continue
        if not 0 <= day <= 6 or not isinstance(entries, Mapping):
            continue
        for entry in entries.values():
            if not isinstance(entry, Mapping):
                continue
            if entry.get("entry_type", "setpoint") != "setpoint":
                continue
            if _number(entry.get("time")) is None:
                continue
            result.append((day, entry))
    return result


def read_setpoints(value: Mapping[str, Any]) -> list[Setpoint]:
    """Setpoints of a HEAT or COOL schedule, rounded for comparison."""
    points: list[Setpoint] = []
    for day, entry in _entries(value):
        temp = _number(entry.get("temp"))
        if temp is not None:
            points.append((day, int(entry["time"]), round(temp, 1)))
    return sorted(points)


def same_schedule(wanted: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """True if the thermostat already follows the wanted schedule."""
    return wanted.get("schedule_mode") == current.get("schedule_mode") and read_setpoints(
        wanted
    ) == read_setpoints(current)


def fingerprint(value: Mapping[str, Any]) -> str:
    """Short stable id of a schedule, to tell repeated pushes apart."""
    payload = json.dumps([value.get("schedule_mode"), read_setpoints(value)])
    return hashlib.sha1(payload.encode(), usedforsecurity=False).hexdigest()[:12]


def setpoint_in_effect(value: Mapping[str, Any], weekday: int, seconds: int) -> float | None:
    """The scheduled temperature at a moment (weekday 0 = Monday, seconds after midnight).

    A setpoint holds until the next one, also across the end of the week.
    """
    points = read_setpoints(value)
    if not points:
        return None
    current = [point for point in points if (point[0], point[1]) <= (weekday, seconds)]
    return (current or points)[-1][2]


def device_clock_offsets(value: Mapping[str, Any], since: float) -> list[tuple[int, int]]:
    """(touched_at, touched_tzo) of setpoints edited on the thermostat after ``since``.

    The thermostat stamps every setpoint it writes with its own clock and UTC
    offset, which shows what time zone it believes it is in.
    """
    found: list[tuple[int, int]] = []
    for _, entry in _entries(value):
        at = _number(entry.get("touched_at"))
        tzo = _number(entry.get("touched_tzo"))
        if at is not None and tzo is not None and at > since:
            found.append((int(at), int(tzo)))
    return sorted(found)


def _clock(seconds: int) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}" + (f":{secs:02d}" if secs else "")


def describe_schedule(value: Mapping[str, Any]) -> dict[str, Any]:
    """The schedule in a readable form (service response)."""
    mode = str(value.get("schedule_mode") or "").lower() or None
    days: dict[str, list[dict[str, Any]]] = {weekday: [] for weekday in WEEKDAYS}
    for day, entry in sorted(_entries(value), key=lambda item: (item[0], item[1]["time"])):
        point: dict[str, Any] = {"time": _clock(int(entry["time"]))}
        if (temp := _number(entry.get("temp"))) is not None:
            point["temperature"] = round(temp, 1)
        if (low := _number(entry.get("temp-min"))) is not None:
            point["temperature_low"] = round(low, 1)
        if (high := _number(entry.get("temp-max"))) is not None:
            point["temperature_high"] = round(high, 1)
        days[WEEKDAYS[day]].append(point)
    return {"mode": mode, "days": days}
