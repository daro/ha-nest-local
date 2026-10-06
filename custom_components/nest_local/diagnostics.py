"""Diagnostics download: what the server knows about each thermostat."""

from __future__ import annotations

import time
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import NestLocalConfigEntry
from .const import REDACT_FIELDS


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NestLocalConfigEntry
) -> dict[str, Any]:
    """Return buckets, pending changes and connection state."""
    hub = entry.runtime_data
    now = time.time()
    devices: dict[str, Any] = {}
    for serial in hub.store.serials:
        record = hub.store.device(serial)
        if record is None:
            continue
        devices[serial] = {
            "online": hub.is_online(serial),
            "seconds_since_seen": round(now - record.last_seen, 1),
            "open_connections": hub.server.subscriptions.count(serial),
            "info": record.info,
            "user_key": record.user_key,
            "structure_key": record.structure_key,
            "adopted_user": record.adopted_user,
            "adopted_structure": record.adopted_structure,
            "eco_mode": hub.eco_mode(serial),
            "buckets": {
                key: {
                    "revision": bucket.revision,
                    "timestamp": bucket.timestamp,
                    "device_timestamp": bucket.device_timestamp,
                    "pending": bucket.pending,
                    "inflight": bucket.inflight,
                    "value": bucket.value,
                }
                for key, bucket in record.buckets.items()
            },
        }
    return {
        "server": {
            "origin": hub.server.origin,
            "port": hub.server.port,
        },
        "options": dict(entry.options),
        "devices": async_redact_data(devices, REDACT_FIELDS),
    }
