"""Open long-poll (subscribe) connections, one queue per connection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import itertools
import logging
import time

from .store import Push

_LOGGER = logging.getLogger(__name__)

# Sentinel placed on a queue to make a held connection close (server stop).
CLOSE = None


@dataclass
class Subscription:
    """A subscribe request whose response is being held open."""

    id: int
    serial: str
    session: str
    created: float = field(default_factory=time.monotonic)
    queue: asyncio.Queue[list[Push] | None] = field(default_factory=asyncio.Queue)


class SubscriptionManager:
    """Track held subscribe connections per thermostat.

    A thermostat that wakes early may open a new subscribe connection before
    the old one is closed, so several subscriptions per serial are normal.
    Each one gets its own server-side id; the device's ``session`` field is
    reused across requests and is not a connection identifier.
    """

    def __init__(self, max_per_device: int = 10) -> None:
        self._subs: dict[str, dict[int, Subscription]] = {}
        self._ids = itertools.count(1)
        self._max_per_device = max_per_device

    def add(self, serial: str, session: str) -> Subscription:
        """Register a held connection; the oldest is dropped past the limit."""
        device_subs = self._subs.setdefault(serial, {})
        if len(device_subs) >= self._max_per_device:
            oldest = min(device_subs.values(), key=lambda s: s.created)
            _LOGGER.debug("%s: closing stale subscription %s", serial, oldest.id)
            oldest.queue.put_nowait(CLOSE)
            del device_subs[oldest.id]
        sub = Subscription(id=next(self._ids), serial=serial, session=session)
        device_subs[sub.id] = sub
        if len(device_subs) > 1:
            _LOGGER.debug(
                "%s: %d subscribe connections open (thermostat reconnected early)",
                serial,
                len(device_subs),
            )
        return sub

    def remove(self, sub: Subscription) -> None:
        """Forget a connection once its response is finished."""
        device_subs = self._subs.get(sub.serial)
        if not device_subs:
            return
        device_subs.pop(sub.id, None)
        if not device_subs:
            del self._subs[sub.serial]

    def notify(self, serial: str, pushes: list[Push]) -> int:
        """Hand pushes to every open connection of a thermostat."""
        device_subs = self._subs.get(serial)
        if not device_subs or not pushes:
            return 0
        for sub in device_subs.values():
            sub.queue.put_nowait(list(pushes))
        return len(device_subs)

    def count(self, serial: str) -> int:
        """Number of open connections for a thermostat."""
        return len(self._subs.get(serial, ()))

    def close_all(self) -> None:
        """Make every held connection finish (used when stopping)."""
        for device_subs in self._subs.values():
            for sub in device_subs.values():
                sub.queue.put_nowait(CLOSE)
