"""Server-side copy of each thermostat's state ("buckets").

The thermostat keeps its state in named buckets (``shared.SERIAL``,
``device.SERIAL``, ``structure.ID`` ...). Each bucket carries a revision and a
millisecond timestamp. The timestamp is the only authority for deciding which
side is newer; the revision is only used for conditional writes on the
``shared`` bucket.

Changes coming from Home Assistant are never mixed into the device's own data
straight away. They live in ``pending`` until they have been written to an open
subscribe connection, then in ``inflight`` until the thermostat reports a
timestamp that proves it has them. Only then are they merged into ``value``.
This lets the server re-send a change that got lost, and it means only fields
that Home Assistant actually changed are ever pushed - never a stale copy of
the whole bucket, which would override the thermostat's own schedule.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import logging
import random
import string
import time
from typing import Any

from .util import split_object_key

_LOGGER = logging.getLogger(__name__)

DEFAULT_USER_ID = "homeassistant"
DEFAULT_STRUCTURE_NAME = "Home"
PENDING_TTL_SECONDS = 6 * 3600
# A setpoint or mode change that could not be delivered within this time is
# dropped rather than applied late (it would override the schedule).
SHARED_PENDING_TTL_SECONDS = 30 * 60
ENTRY_KEY_TTL_SECONDS = 3600
# The thermostat rejects entry keys that expire in less than 30 minutes.
ENTRY_KEY_MIN_REMAINING_SECONDS = 35 * 60
MAX_DEVICES = 20
# A change the thermostat keeps ignoring is given up after this many re-sends,
# so a rejected value can never turn into a subscribe/push loop.
MAX_RESEND_ATTEMPTS = 5
# Unsolicited copies of server data (pairing buckets, zero-timestamp replies)
# are not repeated more often than this per bucket.
UNTRACKED_PUSH_INTERVAL = 300.0
# The thermostat applies a new weekly schedule only after a quiet period
# (pushes within 15 s are combined), so for a while it may still list its old
# schedule. Re-sending during that time would only restart its wait.
SCHEDULE_APPLY_SECONDS = 45.0

# If the thermostat itself writes the key field, a not-yet-confirmed server
# change of the companion fields is obsolete as well.
RELATED_FIELDS: dict[str, frozenset[str]] = {
    "target_temperature": frozenset({"target_change_pending", "touched_by"}),
    "target_temperature_low": frozenset({"target_change_pending", "touched_by"}),
    "target_temperature_high": frozenset({"target_change_pending", "touched_by"}),
    "manual_eco_all": frozenset({"manual_eco_timestamp", "away"}),
}


class TooManyDevicesError(Exception):
    """Raised when a new serial would exceed the device limit."""


def _now() -> float:
    return time.time()


@dataclass
class Push:
    """One object sent to the thermostat on a subscribe connection."""

    key: str
    revision: int
    timestamp: int
    value: dict[str, Any]
    # True when the push carries Home Assistant changes that move from
    # "pending" to "inflight" once the write succeeded.
    tracked: bool = False

    def wire(self) -> dict[str, Any]:
        """Return the JSON object in the field order the firmware requires."""
        return {
            "object_revision": self.revision,
            "object_timestamp": self.timestamp,
            "object_key": self.key,
            "value": self.value,
        }


@dataclass
class Bucket:
    """A single bucket as known to the server."""

    key: str
    revision: int = 0
    timestamp: int = 0
    value: dict[str, Any] = field(default_factory=dict)
    pending: dict[str, Any] = field(default_factory=dict)
    pending_since: float = 0.0
    inflight: dict[str, Any] = field(default_factory=dict)
    inflight_timestamp: int = 0
    inflight_revision: int = 0
    inflight_since: float = 0.0
    # Runtime only.
    device_revision: int = 0
    device_timestamp: int = 0
    attempts: int = 0
    last_untracked_push: float = 0.0

    @property
    def effective(self) -> dict[str, Any]:
        """Device data overlaid with server changes that are on their way."""
        if not self.pending and not self.inflight:
            return self.value
        return {**self.value, **self.inflight, **self.pending}

    @property
    def has_data(self) -> bool:
        """Return True if the server holds real data for this bucket."""
        return self.timestamp > 0

    def receipt(self) -> dict[str, Any]:
        """Write receipt for a PUT: revision, timestamp and key - never a value."""
        return {
            "object_revision": self.revision,
            "object_timestamp": self.timestamp,
            "object_key": self.key,
        }

    def as_dict(self) -> dict[str, Any]:
        """Serialise for persistent storage."""
        data: dict[str, Any] = {
            "rev": self.revision,
            "ts": self.timestamp,
            "value": self.value,
        }
        if self.pending:
            data["pending"] = self.pending
            data["pending_since"] = self.pending_since
        if self.inflight:
            data["inflight"] = self.inflight
            data["inflight_ts"] = self.inflight_timestamp
            data["inflight_rev"] = self.inflight_revision
            data["inflight_since"] = self.inflight_since
        return data

    @classmethod
    def from_dict(cls, key: str, data: dict[str, Any]) -> Bucket:
        """Restore from persistent storage."""
        return cls(
            key=key,
            revision=int(data.get("rev", 0)),
            timestamp=int(data.get("ts", 0)),
            value=dict(data.get("value") or {}),
            pending=dict(data.get("pending") or {}),
            pending_since=float(data.get("pending_since", 0.0)),
            inflight=dict(data.get("inflight") or {}),
            inflight_timestamp=int(data.get("inflight_ts", 0)),
            inflight_revision=int(data.get("inflight_rev", 0)),
            inflight_since=float(data.get("inflight_since", 0.0)),
        )


@dataclass
class DeviceRecord:
    """Everything the server knows about one thermostat."""

    serial: str
    buckets: dict[str, Bucket] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)
    user_key: str | None = None
    structure_key: str | None = None
    # True when a user/structure key was taken over from the thermostat (it
    # was paired with another server before) rather than created by us.
    adopted_user: bool = False
    adopted_structure: bool = False
    # Runtime only.
    last_seen: float = 0.0
    remote_ip: str | None = None
    structure_sent: bool = False
    entry_key: str | None = None
    entry_key_expires: float = 0.0

    def bucket_value(self, kind: str) -> dict[str, Any]:
        """Return the effective value of ``{kind}.{serial}``."""
        bucket = self.buckets.get(f"{kind}.{self.serial}")
        return bucket.effective if bucket else {}

    def structure_value(self) -> dict[str, Any]:
        """Return the effective value of the thermostat's structure bucket."""
        if self.structure_key is None:
            return {}
        bucket = self.buckets.get(self.structure_key)
        return bucket.effective if bucket else {}

    @property
    def is_ready(self) -> bool:
        """True once the thermostat has uploaded its HVAC mode."""
        return "target_temperature_type" in self.bucket_value("shared")

    def first_bucket_value(self, kind: str) -> dict[str, Any] | None:
        """Return the first bucket of a kind with data (e.g. ``hvac_partner``)."""
        prefix = f"{kind}."
        for key, bucket in self.buckets.items():
            if key.startswith(prefix) and bucket.has_data:
                return bucket.effective
        return None

    def as_dict(self) -> dict[str, Any]:
        """Serialise for persistent storage."""
        return {
            "info": self.info,
            "user_key": self.user_key,
            "structure_key": self.structure_key,
            "adopted_user": self.adopted_user,
            "adopted_structure": self.adopted_structure,
            "buckets": {
                key: bucket.as_dict()
                for key, bucket in self.buckets.items()
                if bucket.has_data or bucket.pending
            },
        }

    @classmethod
    def from_dict(cls, serial: str, data: dict[str, Any]) -> DeviceRecord:
        """Restore from persistent storage."""
        record = cls(
            serial=serial,
            info=dict(data.get("info") or {}),
            user_key=data.get("user_key"),
            structure_key=data.get("structure_key"),
            adopted_user=bool(data.get("adopted_user", data.get("adopted_pairing", False))),
            adopted_structure=bool(
                data.get("adopted_structure", data.get("adopted_pairing", False))
            ),
        )
        for key, bucket_data in (data.get("buckets") or {}).items():
            record.buckets[key] = Bucket.from_dict(key, bucket_data)
        return record


class BucketStore:
    """All thermostats served by this integration."""

    def __init__(
        self,
        *,
        user_id: str = DEFAULT_USER_ID,
        structure_name: str = DEFAULT_STRUCTURE_NAME,
        clock: Callable[[], float] = _now,
        pending_ttl: float = PENDING_TTL_SECONDS,
        max_devices: int = MAX_DEVICES,
    ) -> None:
        self._devices: dict[str, DeviceRecord] = {}
        self._user_id = user_id
        self._structure_name = structure_name
        self._clock = clock
        self._pending_ttl = pending_ttl
        self._max_devices = max_devices
        self.on_device_added: Callable[[str], None] | None = None
        self.on_update: Callable[[str, set[str]], None] | None = None
        self.on_seen: Callable[[str], None] | None = None

    # ------------------------------------------------------------------ basics

    @property
    def own_user_key(self) -> str:
        """User bucket key this server creates when pairing a thermostat."""
        return f"user.{self._user_id}"

    @property
    def own_structure_key(self) -> str:
        """Structure bucket key this server creates when pairing a thermostat."""
        return f"structure.{self._user_id}"

    @property
    def serials(self) -> list[str]:
        """All known serial numbers."""
        return list(self._devices)

    def device(self, serial: str) -> DeviceRecord | None:
        """Return the record for a serial, if known."""
        return self._devices.get(serial)

    def now_ms(self) -> int:
        """Current time in milliseconds according to the store clock."""
        return int(self._clock() * 1000)

    def ensure_device(self, serial: str) -> DeviceRecord:
        """Return the record for a serial, creating it on first contact."""
        record = self._devices.get(serial)
        if record is not None:
            return record
        if len(self._devices) >= self._max_devices:
            raise TooManyDevicesError(serial)
        record = DeviceRecord(serial=serial, last_seen=self._clock())
        self._devices[serial] = record
        _LOGGER.info("New Nest thermostat %s", serial)
        if self.on_device_added:
            self.on_device_added(serial)
        return record

    def remove_device(self, serial: str) -> bool:
        """Forget a thermostat completely."""
        return self._devices.pop(serial, None) is not None

    def touch(self, serial: str, remote_ip: str | None = None) -> DeviceRecord:
        """Mark a thermostat as seen just now."""
        record = self.ensure_device(serial)
        record.last_seen = self._clock()
        if remote_ip and record.remote_ip != remote_ip:
            record.remote_ip = remote_ip
        if self.on_seen:
            self.on_seen(serial)
        return record

    def update_info(self, serial: str, info: dict[str, Any]) -> None:
        """Store details the thermostat sent with its entry request."""
        record = self.ensure_device(serial)
        changed = {k: v for k, v in info.items() if v and record.info.get(k) != v}
        if changed:
            record.info.update(changed)
            self._notify(serial, {"info"})

    def mark_all_seen(self) -> None:
        """Give every known thermostat a grace period after a restart."""
        now = self._clock()
        for record in self._devices.values():
            record.last_seen = now

    def get_entry_key(self, serial: str) -> tuple[str, int]:
        """Return (code, expiry in ms) for the pairing screen."""
        record = self.ensure_device(serial)
        now = self._clock()
        if (
            record.entry_key is None
            or record.entry_key_expires - now < ENTRY_KEY_MIN_REMAINING_SECONDS
        ):
            digits = "".join(random.choices(string.digits, k=3))
            letters = "".join(random.choices(string.ascii_uppercase, k=4))
            record.entry_key = f"{digits}{letters}"
            record.entry_key_expires = now + ENTRY_KEY_TTL_SECONDS
        return record.entry_key, int(record.entry_key_expires * 1000)

    def structure_key_for(self, serial: str) -> str:
        """Structure bucket used for eco control of a thermostat."""
        record = self.ensure_device(serial)
        if record.structure_key is None:
            record.structure_key = self.own_structure_key
        return record.structure_key

    # -------------------------------------------------------------- internals

    def _notify(self, serial: str, keys: set[str]) -> None:
        if keys and self.on_update:
            self.on_update(serial, keys)

    def _next_timestamp(self, bucket: Bucket) -> int:
        return max(self.now_ms(), bucket.timestamp + 1, bucket.device_timestamp + 1)

    def _bump(self, bucket: Bucket) -> None:
        bucket.revision = max(bucket.revision, bucket.device_revision) + 1
        bucket.timestamp = self._next_timestamp(bucket)

    @staticmethod
    def _bucket(record: DeviceRecord, key: str) -> Bucket:
        bucket = record.buckets.get(key)
        if bucket is None:
            bucket = Bucket(key=key)
            record.buckets[key] = bucket
        return bucket

    def _refresh_time_fields(self, fields: dict[str, Any]) -> None:
        """Keep time-validated fields fresh when (re)sending a change.

        The firmware ignores ``manual_eco_timestamp`` values that are more
        than 600 s away from its clock, so a change that waited for the
        thermostat to come back online must carry the current time.
        """
        now_s = int(self._clock())
        if "manual_eco_timestamp" in fields:
            fields["manual_eco_timestamp"] = now_s
        eco = fields.get("eco")
        if isinstance(eco, dict) and "mode_update_timestamp" in eco:
            fields["eco"] = {**eco, "mode_update_timestamp": now_s}
        touched = fields.get("touched_by")
        if isinstance(touched, dict) and "touched_at" in touched:
            fields["touched_by"] = {**touched, "touched_at": now_s}

    def _pending_push(self, bucket: Bucket) -> Push:
        self._refresh_time_fields(bucket.pending)
        return Push(
            key=bucket.key,
            revision=bucket.revision,
            timestamp=bucket.timestamp,
            value=dict(bucket.pending),
            tracked=True,
        )

    def _may_push_untracked(self, bucket: Bucket) -> bool:
        now = self._clock()
        if now - bucket.last_untracked_push < UNTRACKED_PUSH_INTERVAL:
            return False
        bucket.last_untracked_push = now
        return True

    @staticmethod
    def _drop_overridden(bucket: Bucket, fields: Iterable[str]) -> bool:
        """Forget server changes the thermostat has since overwritten.

        Returns True if anything was dropped.
        """
        dropped = False
        for name in fields:
            for target in (bucket.pending, bucket.inflight):
                for key in (name, *RELATED_FIELDS.get(name, ())):
                    if key in target:
                        del target[key]
                        dropped = True
        if not bucket.inflight:
            bucket.inflight_timestamp = 0
            bucket.inflight_revision = 0
            bucket.inflight_since = 0.0
        if not bucket.pending:
            bucket.pending_since = 0.0
        return dropped

    def _apply_device_fields(self, bucket: Bucket, fields: dict[str, Any], created: bool) -> bool:
        """Merge data written by the thermostat.

        Returns True if what Home Assistant sees changed. The revision and
        timestamp only move when the stored data itself changed.
        """
        dropped = self._drop_overridden(bucket, fields)
        merged = {**bucket.value, **fields}
        if merged == bucket.value and not created:
            return dropped
        bucket.value = merged
        self._bump(bucket)
        return True

    @staticmethod
    def _clear_inflight(bucket: Bucket) -> None:
        bucket.inflight = {}
        bucket.inflight_timestamp = 0
        bucket.inflight_revision = 0
        bucket.inflight_since = 0.0

    def _requeue_inflight(self, bucket: Bucket) -> None:
        """Send changes again that the thermostat may not have applied."""
        _LOGGER.debug("%s may not have been applied by the thermostat, re-sending", bucket.key)
        bucket.pending = {**bucket.inflight, **bucket.pending}
        bucket.pending_since = bucket.pending_since or bucket.inflight_since or self._clock()
        self._clear_inflight(bucket)

    def _settle_inflight(self, bucket: Bucket, client_ts: int) -> None:
        """Confirm, or re-queue, changes written to an earlier connection."""
        if not bucket.inflight:
            return
        if client_ts >= bucket.inflight_timestamp:
            bucket.value = {**bucket.value, **bucket.inflight}
            bucket.attempts = 0
            self._clear_inflight(bucket)
        elif not self._still_applying(bucket):
            self._requeue_inflight(bucket)

    def _still_applying(self, bucket: Bucket) -> bool:
        """True while the thermostat may still be applying a weekly schedule."""
        return (
            bucket.key.startswith("schedule.")
            and self._clock() - bucket.inflight_since < SCHEDULE_APPLY_SECONDS
        )

    def _resend_allowed(self, serial: str, bucket: Bucket) -> bool:
        bucket.attempts += 1
        if bucket.attempts <= MAX_RESEND_ATTEMPTS:
            return True
        _LOGGER.warning(
            "%s: the thermostat keeps ignoring a change to %s (%s), giving up",
            serial,
            bucket.key,
            sorted(bucket.pending),
        )
        bucket.pending = {}
        bucket.pending_since = 0.0
        bucket.attempts = 0
        return False

    # ------------------------------------------------------- device requests

    def handle_put(self, serial: str, objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Process a PUT from the thermostat and return the write receipts."""
        record = self.ensure_device(serial)
        receipts: list[dict[str, Any]] = []
        changed: set[str] = set()
        for obj in objects:
            key = obj.get("object_key")
            fields = obj.get("value")
            if not isinstance(key, str) or not key:
                continue
            if not isinstance(fields, dict) or not fields:
                continue
            bucket = record.buckets.get(key)
            if_revision = obj.get("if_object_revision")
            base_revision = if_revision
            if base_revision is None:
                base_revision = obj.get("base_object_revision")
            if (
                bucket is not None
                and bucket.inflight
                and isinstance(base_revision, int)
                and base_revision < bucket.inflight_revision
            ):
                # The thermostat wrote this before it had our push. If it
                # takes our timestamp from the receipt it will discard the
                # push as stale, so the change has to be sent again.
                self._requeue_inflight(bucket)
                changed.add(key)
            if if_revision is not None:
                current = bucket.revision if bucket else 0
                if if_revision != current:
                    # Conditional write conflict: keep our data and tell the
                    # thermostat the current revision so it retries. No value.
                    _LOGGER.debug(
                        "%s: PUT %s rejected (if_object_revision %s != %s)",
                        serial,
                        key,
                        if_revision,
                        current,
                    )
                    receipts.append(
                        bucket.receipt()
                        if bucket
                        else {
                            "object_revision": 0,
                            "object_timestamp": 0,
                            "object_key": key,
                        }
                    )
                    continue
            created = bucket is None or not bucket.has_data
            bucket = self._bucket(record, key)
            if self._apply_device_fields(bucket, fields, created):
                changed.add(key)
            receipts.append(bucket.receipt())
        self._notify(serial, changed)
        return receipts

    def handle_subscribe(self, serial: str, objects: list[dict[str, Any]]) -> list[Push]:
        """Process a subscribe request and return what must be sent at once."""
        record = self.ensure_device(serial)
        changed: set[str] = set()
        listed: dict[str, int] = {}

        for obj in objects:
            key = obj.get("object_key")
            if not isinstance(key, str) or not key:
                continue
            try:
                client_rev = int(obj.get("object_revision") or 0)
                client_ts = int(obj.get("object_timestamp") or 0)
            except (TypeError, ValueError):
                continue
            value = obj.get("value")
            bucket = self._bucket(record, key)

            if isinstance(value, dict) and value:
                if client_ts == 0 and client_rev == 0:
                    # Inline update: a local change sent with the subscribe.
                    if self._apply_device_fields(bucket, value, not bucket.has_data):
                        changed.add(key)
                    client_ts = bucket.timestamp
                elif client_ts > bucket.timestamp:
                    self._drop_overridden(bucket, value)
                    bucket.value = {**bucket.value, **value}
                    bucket.revision = max(bucket.revision, client_rev)
                    bucket.timestamp = client_ts
                    changed.add(key)

            bucket.device_revision = max(bucket.device_revision, client_rev)
            bucket.device_timestamp = max(bucket.device_timestamp, client_ts)
            listed[key] = client_ts

        self._resolve_pairing_keys(record, listed)
        own_pairing: set[str | None] = set()
        if not record.adopted_user:
            own_pairing.add(record.user_key)
        if not record.adopted_structure:
            own_pairing.add(record.structure_key)

        pushes: list[Push] = []
        for key, client_ts in listed.items():
            if key in own_pairing:
                continue  # handled by _pairing_pushes
            push = self._push_for(serial, record.buckets[key], client_ts)
            if push is not None:
                pushes.append(push)

        pushes.extend(self._pairing_pushes(record, listed))
        self._notify(serial, changed)
        return pushes

    def _resolve_pairing_keys(self, record: DeviceRecord, listed: dict[str, int]) -> None:
        """Decide which user/structure buckets this thermostat uses.

        A thermostat that was paired with another server (e.g. NoLongerEvil
        hosted) lists that server's keys; those are kept rather than creating
        a second home. Anything it does not list is created by us.
        """
        if record.user_key is None:
            foreign = [k for k in listed if k.startswith("user.") and k != self.own_user_key]
            if foreign:
                record.user_key = foreign[0]
                record.adopted_user = True
                _LOGGER.info("%s: keeping its existing pairing %s", record.serial, foreign[0])
            else:
                record.user_key = self.own_user_key
        if record.structure_key is None:
            foreign = [
                k for k in listed if k.startswith("structure.") and k != self.own_structure_key
            ]
            if foreign:
                record.structure_key = foreign[0]
                record.adopted_structure = True
                _LOGGER.info("%s: keeping its existing home %s", record.serial, foreign[0])
            else:
                record.structure_key = self.own_structure_key

    def _push_for(self, serial: str, bucket: Bucket, client_ts: int) -> Push | None:
        """Work out what (if anything) to send for one listed bucket."""
        self._settle_inflight(bucket, client_ts)

        if bucket.pending:
            if not self._resend_allowed(serial, bucket):
                return None
            if client_ts >= bucket.timestamp:
                # The thermostat already holds our current timestamp (for
                # example from a write receipt) without this data. A push
                # with an equal timestamp would be ignored, so bump it.
                self._bump(bucket)
            return self._pending_push(bucket)

        if client_ts == 0 and self._may_push_untracked(bucket):
            # The thermostat has no data for this bucket (e.g. after a factory
            # reset). A zero timestamp asks it to upload its own state; our
            # possibly stale copy is never pushed back.
            return Push(bucket.key, 0, 0, {})
        return None

    def _pairing_pushes(self, record: DeviceRecord, listed: dict[str, int]) -> list[Push]:
        """User and structure buckets that complete pairing on the thermostat."""
        pushes: list[Push] = []

        if not record.adopted_user:
            user_key = record.user_key or self.own_user_key
            user = self._bucket(record, user_key)
            if user.value.get("name") != self._user_id or not user.has_data:
                user.value = {"name": self._user_id}
                self._bump(user)
            if listed.get(user_key, 0) < user.timestamp and self._may_push_untracked(user):
                pushes.append(Push(user_key, user.revision, user.timestamp, dict(user.value)))

        if record.adopted_structure:
            return pushes
        structure_key = record.structure_key or self.own_structure_key
        structure = self._bucket(record, structure_key)
        base = {"name": self._structure_name, "devices": [record.serial]}
        if not structure.has_data or any(structure.value.get(k) != v for k, v in base.items()):
            structure.value = {**structure.value, **base}
            self._bump(structure)
        client_ts = listed.get(structure_key, 0)
        self._settle_inflight(structure, client_ts)

        send = False
        if structure.pending:
            send = self._resend_allowed(record.serial, structure)
        if not record.structure_sent:
            send = True
        elif client_ts < structure.timestamp and self._may_push_untracked(structure):
            send = True
        if send:
            if client_ts >= structure.timestamp:
                # First contact since start-up (the thermostat may have reset
                # its state while keeping the timestamp): bump so it is not
                # discarded as stale.
                self._bump(structure)
            self._refresh_time_fields(structure.pending)
            pushes.append(
                Push(
                    structure_key,
                    structure.revision,
                    structure.timestamp,
                    {**base, **structure.pending},
                    tracked=bool(structure.pending),
                )
            )
            record.structure_sent = True
        return pushes

    def mark_delivered(self, serial: str, pushes: list[Push]) -> None:
        """Record that tracked pushes were written to an open connection."""
        record = self._devices.get(serial)
        if record is None:
            return
        now = self._clock()
        changed: set[str] = set()
        for push in pushes:
            if not push.tracked:
                continue
            bucket = record.buckets.get(push.key)
            if bucket is None:
                continue
            moved = False
            for name, value in push.value.items():
                if name in bucket.pending and bucket.pending[name] == value:
                    del bucket.pending[name]
                    bucket.inflight[name] = value
                    moved = True
            if moved:
                bucket.inflight_timestamp = max(bucket.inflight_timestamp, push.timestamp)
                bucket.inflight_revision = max(bucket.inflight_revision, push.revision)
                bucket.inflight_since = bucket.inflight_since or now
                changed.add(bucket.key)
            if not bucket.pending:
                bucket.pending_since = 0.0
        self._notify(serial, changed)

    # -------------------------------------------------------- server changes

    def server_update(self, serial: str, key: str, fields: dict[str, Any]) -> Push:
        """Queue a change made in Home Assistant and return the push for it."""
        record = self.ensure_device(serial)
        bucket = self._bucket(record, key)
        if not bucket.pending:
            bucket.pending_since = self._clock()
        bucket.pending.update(fields)
        bucket.attempts = 0
        self._bump(bucket)
        push = self._pending_push(bucket)
        if key == record.structure_key and not record.adopted_structure:
            # Keep the pairing fields together with any structure change.
            push.value = {
                "name": self._structure_name,
                "devices": [serial],
                **push.value,
            }
        self._notify(serial, {key})
        return push

    def _ttl(self, key: str) -> float:
        if key.startswith("schedule."):
            # The weekly schedule is wanted state rather than a command: it is
            # kept until the thermostat comes back, however long that takes.
            return float("inf")
        if key.startswith("shared."):
            return min(self._pending_ttl, SHARED_PENDING_TTL_SECONDS)
        return self._pending_ttl

    def expire_stale(self) -> None:
        """Drop server changes the thermostat never picked up."""
        now = self._clock()
        for serial, record in self._devices.items():
            changed: set[str] = set()
            for bucket in record.buckets.values():
                cutoff = now - self._ttl(bucket.key)
                if bucket.pending and 0 < bucket.pending_since < cutoff:
                    _LOGGER.warning(
                        "%s: dropping a change to %s that never reached the thermostat: %s",
                        serial,
                        bucket.key,
                        sorted(bucket.pending),
                    )
                    bucket.pending = {}
                    bucket.pending_since = 0.0
                    changed.add(bucket.key)
                if bucket.inflight and 0 < bucket.inflight_since < cutoff:
                    self._clear_inflight(bucket)
                    changed.add(bucket.key)
            self._notify(serial, changed)

    def cancel_pending(self, serial: str, key: str, names: Iterable[str]) -> None:
        """Withdraw queued server changes of some fields (superseded commands)."""
        record = self._devices.get(serial)
        bucket = record.buckets.get(key) if record else None
        if bucket is None:
            return
        for name in names:
            bucket.pending.pop(name, None)
            bucket.inflight.pop(name, None)
        if not bucket.pending:
            bucket.pending_since = 0.0
        if not bucket.inflight:
            self._clear_inflight(bucket)

    def has_pending(self, serial: str, key: str, name: str) -> bool:
        """True if a server change of the field is still on its way."""
        record = self._devices.get(serial)
        bucket = record.buckets.get(key) if record else None
        return bucket is not None and (name in bucket.pending or name in bucket.inflight)

    def prune_unready(self, max_age: float) -> list[str]:
        """Forget records that never uploaded any state (stray requests)."""
        cutoff = self._clock() - max_age
        stale = [
            serial
            for serial, record in self._devices.items()
            if not record.is_ready and record.last_seen < cutoff
        ]
        for serial in stale:
            del self._devices[serial]
        return stale

    # ------------------------------------------------------------ inspection

    def object_list(self, serial: str) -> list[dict[str, Any]]:
        """Bucket metadata for the transport ``device`` listing."""
        record = self._devices.get(serial)
        if record is None:
            return []
        return [b.receipt() for b in record.buckets.values() if b.has_data]

    def bucket_kind_keys(self, serial: str, kind: str) -> list[str]:
        """All bucket keys of one kind for a thermostat."""
        record = self._devices.get(serial)
        if record is None:
            return []
        return [k for k in record.buckets if split_object_key(k)[0] == kind]

    # ----------------------------------------------------------- persistence

    def as_dict(self) -> dict[str, Any]:
        """Serialise everything for persistent storage."""
        return {"devices": {serial: rec.as_dict() for serial, rec in self._devices.items()}}

    def load(self, data: dict[str, Any] | None) -> None:
        """Restore from persistent storage (called once at start-up)."""
        if not data:
            return
        for serial, device_data in (data.get("devices") or {}).items():
            self._devices[serial] = DeviceRecord.from_dict(serial, device_data)
