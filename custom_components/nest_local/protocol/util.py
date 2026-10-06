"""Small helpers shared by the Nest protocol server.

Nothing in this package imports Home Assistant, so it can be tested on its own.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from typing import Any

MIN_SERIAL_LENGTH = 10
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")

# Fields that make the thermostat light up / count as a setpoint change.
TEMPERATURE_FIELDS = frozenset(
    {
        "target_temperature",
        "target_temperature_high",
        "target_temperature_low",
        "target_temperature_type",
    }
)


def now_ms() -> int:
    """Return the current time in milliseconds since the epoch."""
    return int(time.time() * 1000)


def sanitize_serial(value: str | None) -> str | None:
    """Normalise a serial number (upper-case alphanumerics, min. 10 chars)."""
    if not value:
        return None
    cleaned = _NON_ALNUM.sub("", value).upper()
    if len(cleaned) < MIN_SERIAL_LENGTH:
        return None
    return cleaned


def serial_from_user_id(user_id: str | None) -> str | None:
    """Extract the serial from a Nest client id.

    Production firmware uses ``d.{SERIAL}.{suffix}``; older builds use
    ``nest.{SERIAL}`` or the bare serial.
    """
    if not user_id:
        return None
    if "." in user_id:
        parts = user_id.split(".")
        candidate = parts[1] if len(parts) > 1 and parts[1] else parts[0]
    else:
        candidate = user_id
    return sanitize_serial(candidate)


def parse_basic_auth(header: str | None) -> tuple[str, str] | None:
    """Decode an HTTP Basic ``Authorization`` header into (user, password)."""
    if not header or not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if ":" not in decoded:
        return None
    user, password = decoded.split(":", 1)
    return user, password


def dumps(data: Any) -> str:
    """Serialise JSON for the device.

    Python dicts keep insertion order, which matters: the thermostat expects
    ``object_revision`` and ``object_timestamp`` before ``object_key``.
    """
    return json.dumps(data, ensure_ascii=False)


def split_object_key(object_key: str) -> tuple[str, str]:
    """Split ``shared.SERIAL`` into (``shared``, ``SERIAL``)."""
    kind, _, ident = object_key.partition(".")
    return kind, ident


def contains_temperature_fields(objects: list[dict[str, Any]]) -> bool:
    """Return True if any pushed object changes a setpoint or the HVAC mode."""
    for obj in objects:
        value = obj.get("value")
        if isinstance(value, dict) and TEMPERATURE_FIELDS.intersection(value):
            return True
    return False


def parse_json_field(value: Any) -> dict[str, Any] | None:
    """Return a dict for a field that may hold a dict or a JSON string."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.startswith("{"):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None
