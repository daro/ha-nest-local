"""Unit tests for the bucket store (no HTTP, no Home Assistant)."""

from __future__ import annotations

from custom_components.nest_local.protocol.store import (
    MAX_RESEND_ATTEMPTS,
    SCHEDULE_APPLY_SECONDS,
    BucketStore,
)

SERIAL = "02AA01AB501203EQ"
SHARED = f"shared.{SERIAL}"
DEVICE = f"device.{SERIAL}"
SCHEDULE = f"schedule.{SERIAL}"


class Clock:
    """Controllable clock."""

    def __init__(self, start: float = 1_790_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_store(clock: Clock | None = None) -> tuple[BucketStore, Clock]:
    clock = clock or Clock()
    return BucketStore(clock=clock), clock


def listing(store: BucketStore, ts_override: dict[str, int] | None = None) -> list[dict]:
    """Subscribe objects as a thermostat in sync with the store would send."""
    record = store.device(SERIAL)
    assert record is not None
    result = []
    for key, bucket in record.buckets.items():
        if not bucket.has_data:
            continue
        ts = (ts_override or {}).get(key, bucket.timestamp)
        result.append(
            {"object_key": key, "object_revision": bucket.revision, "object_timestamp": ts}
        )
    return result


def put(store: BucketStore, key: str, fields: dict, if_rev: int | None = None) -> dict:
    return store.handle_put(
        SERIAL, [{"object_key": key, "if_object_revision": if_rev, "value": fields}]
    )[0]


def paired_store() -> tuple[BucketStore, Clock]:
    """Store with a thermostat that uploaded its state and is in sync."""
    store, clock = make_store()
    pushes = store.handle_subscribe(SERIAL, [])
    assert {p.key for p in pushes} == {"user.homeassistant", "structure.homeassistant"}
    put(store, SHARED, {"target_temperature": 20.0, "target_temperature_type": "heat"}, 0)
    put(store, DEVICE, {"current_humidity": 45})
    assert store.handle_subscribe(SERIAL, listing(store)) == []
    return store, clock


def test_put_receipt_never_contains_value() -> None:
    store, _ = make_store()
    receipt = put(store, DEVICE, {"current_humidity": 45})
    assert set(receipt) == {"object_revision", "object_timestamp", "object_key"}
    assert receipt["object_timestamp"] > 0


def test_put_continues_device_revision_numbering() -> None:
    store, _ = make_store()
    store.handle_subscribe(
        SERIAL, [{"object_key": DEVICE, "object_revision": 122, "object_timestamp": 1}]
    )
    receipt = put(store, DEVICE, {"current_humidity": 45})
    assert receipt["object_revision"] == 123


def test_conditional_write_conflict_keeps_server_data() -> None:
    store, _ = make_store()
    put(store, SHARED, {"target_temperature": 20.0}, if_rev=0)
    bucket = store.device(SERIAL).buckets[SHARED]
    receipt = put(store, SHARED, {"target_temperature": 25.0}, if_rev=bucket.revision - 1)
    assert receipt == bucket.receipt()
    assert bucket.value["target_temperature"] == 20.0


def test_conflict_on_unknown_bucket_returns_zero_sentinel() -> None:
    store, _ = make_store()
    receipt = put(store, SHARED, {"target_temperature": 21.0}, if_rev=457)
    assert receipt == {"object_revision": 0, "object_timestamp": 0, "object_key": SHARED}


def test_unchanged_put_does_not_bump() -> None:
    store, _ = make_store()
    first = put(store, DEVICE, {"current_humidity": 45})
    second = put(store, DEVICE, {"current_humidity": 45})
    assert first == second


def test_server_update_pushes_only_changed_fields_in_firmware_order() -> None:
    store, _ = paired_store()
    push = store.server_update(SERIAL, SHARED, {"target_temperature": 21.5})
    assert push.value == {"target_temperature": 21.5}
    assert list(push.wire()) == ["object_revision", "object_timestamp", "object_key", "value"]
    bucket = store.device(SERIAL).buckets[SHARED]
    assert push.timestamp > bucket.device_timestamp
    # Home Assistant already sees the new value while it is on its way.
    assert bucket.effective["target_temperature"] == 21.5
    assert bucket.value["target_temperature"] == 20.0


def test_delivery_and_confirmation() -> None:
    store, _ = paired_store()
    push = store.server_update(SERIAL, SHARED, {"target_temperature": 21.5})
    store.mark_delivered(SERIAL, [push])
    bucket = store.device(SERIAL).buckets[SHARED]
    assert bucket.pending == {}
    assert bucket.inflight == {"target_temperature": 21.5}
    # The thermostat resubscribes with the pushed timestamp: confirmed.
    assert store.handle_subscribe(SERIAL, listing(store)) == []
    assert bucket.inflight == {}
    assert bucket.value["target_temperature"] == 21.5


def test_lost_push_is_sent_again() -> None:
    store, _ = paired_store()
    bucket = store.device(SERIAL).buckets[SHARED]
    old_ts = bucket.timestamp
    push = store.server_update(SERIAL, SHARED, {"target_temperature": 21.5})
    store.mark_delivered(SERIAL, [push])
    # Written to a dead connection: the thermostat still reports the old one.
    pushes = store.handle_subscribe(SERIAL, listing(store, {SHARED: old_ts}))
    assert [p.value for p in pushes if p.key == SHARED] == [{"target_temperature": 21.5}]


def test_undelivered_change_reaches_thermostat_on_next_subscribe() -> None:
    store, _ = paired_store()
    bucket = store.device(SERIAL).buckets[SHARED]
    old_ts = bucket.timestamp
    store.server_update(SERIAL, SHARED, {"target_temperature_type": "off"})
    pushes = store.handle_subscribe(SERIAL, listing(store, {SHARED: old_ts}))
    assert [p.value for p in pushes] == [{"target_temperature_type": "off"}]


def test_receipt_timestamp_without_data_forces_bump() -> None:
    """A write receipt can give the thermostat our timestamp without the data."""
    store, _ = paired_store()
    store.server_update(SERIAL, SHARED, {"target_temperature": 22.0})
    bucket = store.device(SERIAL).buckets[SHARED]
    # Thermostat PUTs something else and adopts the receipt timestamp.
    put(store, SHARED, {"hvac_heater_state": True}, if_rev=bucket.revision)
    receipt_ts = bucket.timestamp
    pushes = store.handle_subscribe(SERIAL, listing(store))
    assert len(pushes) == 1
    assert pushes[0].value == {"target_temperature": 22.0}
    assert pushes[0].timestamp > receipt_ts


def test_device_change_overrides_pending_and_related_fields() -> None:
    store, _ = paired_store()
    store.server_update(SERIAL, SHARED, {"target_temperature": 21.5, "target_change_pending": True})
    bucket = store.device(SERIAL).buckets[SHARED]
    put(store, SHARED, {"target_temperature": 19.0}, if_rev=bucket.revision)
    assert bucket.pending == {}
    assert bucket.effective["target_temperature"] == 19.0
    assert store.handle_subscribe(SERIAL, listing(store)) == []


def test_ignored_change_is_given_up() -> None:
    store, _ = paired_store()
    bucket = store.device(SERIAL).buckets[SHARED]
    old_ts = bucket.timestamp
    store.server_update(SERIAL, SHARED, {"bogus_field": 1})
    sent = 0
    for _ in range(MAX_RESEND_ATTEMPTS + 3):
        pushes = store.handle_subscribe(SERIAL, listing(store, {SHARED: old_ts}))
        if pushes:
            sent += 1
            store.mark_delivered(SERIAL, pushes)
    assert sent == MAX_RESEND_ATTEMPTS
    assert bucket.pending == {} and bucket.inflight == {}


def test_stale_pending_expires() -> None:
    store, clock = paired_store()
    store.server_update(SERIAL, SHARED, {"target_temperature": 23.0})
    clock.advance(7 * 3600)
    store.expire_stale()
    bucket = store.device(SERIAL).buckets[SHARED]
    assert bucket.pending == {}
    assert bucket.effective["target_temperature"] == 20.0


def test_pairing_buckets_only_until_thermostat_has_them() -> None:
    store, _ = paired_store()
    # In sync: nothing more to send, even after many subscribes.
    for _ in range(3):
        assert store.handle_subscribe(SERIAL, listing(store)) == []


def test_structure_resent_once_after_restart() -> None:
    store, clock = paired_store()
    saved = store.as_dict()
    restarted = BucketStore(clock=clock)
    restarted.load(saved)
    pushes = restarted.handle_subscribe(SERIAL, listing(restarted))
    assert [p.key for p in pushes] == ["structure.homeassistant"]
    structure = restarted.device(SERIAL).buckets["structure.homeassistant"]
    assert pushes[0].timestamp == structure.timestamp
    # Thermostat applied it; the next subscribe is quiet.
    assert restarted.handle_subscribe(SERIAL, listing(restarted)) == []


def test_adopts_pairing_from_previous_server() -> None:
    store, _ = make_store()
    objects = [
        {"object_key": "user.abc123", "object_revision": 1, "object_timestamp": 5},
        {"object_key": "structure.abc123", "object_revision": 3, "object_timestamp": 7},
    ]
    assert store.handle_subscribe(SERIAL, objects) == []
    record = store.device(SERIAL)
    assert record.adopted_user and record.adopted_structure
    assert store.structure_key_for(SERIAL) == "structure.abc123"
    push = store.server_update(SERIAL, "structure.abc123", {"manual_eco_all": True})
    assert push.value == {"manual_eco_all": True}
    assert push.revision > 3 and push.timestamp > 7


def test_eco_timestamp_refreshed_when_resent() -> None:
    store, clock = paired_store()
    key = store.structure_key_for(SERIAL)
    structure = store.device(SERIAL).buckets[key]
    old_ts = structure.timestamp
    store.server_update(SERIAL, key, {"manual_eco_all": True, "manual_eco_timestamp": 1})
    clock.advance(1200)
    pushes = store.handle_subscribe(SERIAL, listing(store, {key: old_ts}))
    eco = next(p for p in pushes if p.key == key)
    assert eco.value["manual_eco_timestamp"] == int(clock.now)
    assert eco.value["devices"] == [SERIAL]


def test_zero_timestamp_reply_is_throttled() -> None:
    store, clock = make_store()
    unknown = [{"object_key": f"schedule.{SERIAL}", "object_revision": 0, "object_timestamp": 0}]
    first = store.handle_subscribe(SERIAL, unknown)
    assert any(p.key == f"schedule.{SERIAL}" and p.timestamp == 0 for p in first)
    second = store.handle_subscribe(SERIAL, unknown)
    assert not any(p.key == f"schedule.{SERIAL}" for p in second)
    clock.advance(301)
    third = store.handle_subscribe(SERIAL, unknown)
    assert any(p.key == f"schedule.{SERIAL}" for p in third)


def test_inline_update_in_subscribe() -> None:
    store, _ = paired_store()
    objects = [
        *listing(store),
        {
            "object_key": SHARED,
            "object_revision": 0,
            "object_timestamp": 0,
            "value": {"target_temperature": 22.0},
        },
    ]
    assert store.handle_subscribe(SERIAL, objects) == []
    assert store.device(SERIAL).bucket_value("shared")["target_temperature"] == 22.0


def test_future_device_timestamps_are_respected() -> None:
    store, _ = paired_store()
    bucket = store.device(SERIAL).buckets[SHARED]
    future = bucket.timestamp + 10_000_000
    store.handle_subscribe(SERIAL, listing(store, {SHARED: future}))
    push = store.server_update(SERIAL, SHARED, {"target_temperature": 18.0})
    assert push.timestamp > future


def test_persistence_round_trip_keeps_pending() -> None:
    store, clock = paired_store()
    store.server_update(SERIAL, SHARED, {"target_temperature": 21.0})
    restored = BucketStore(clock=clock)
    restored.load(store.as_dict())
    bucket = restored.device(SERIAL).buckets[SHARED]
    assert bucket.pending == {"target_temperature": 21.0}
    assert bucket.value["target_temperature"] == 20.0


def test_entry_key_is_stable_and_long_lived() -> None:
    store, clock = make_store()
    code, expires = store.get_entry_key(SERIAL)
    assert len(code) == 7
    assert expires >= (clock.now + 30 * 60) * 1000
    clock.advance(10 * 60)
    assert store.get_entry_key(SERIAL)[0] == code
    clock.advance(20 * 60)
    _, expires2 = store.get_entry_key(SERIAL)
    assert expires2 >= (clock.now + 30 * 60) * 1000


def test_put_based_on_older_revision_requeues_inflight() -> None:
    """A write receipt must not count as proof that a push was applied."""
    store, _ = paired_store()
    bucket = store.device(SERIAL).buckets[SHARED]
    old_rev = bucket.revision
    push = store.server_update(SERIAL, SHARED, {"target_temperature": 22.5})
    store.mark_delivered(SERIAL, [push])
    # Thermostat's PUT was sent before it had the push -> conflict receipt.
    receipt = put(store, SHARED, {"hvac_heater_state": True}, if_rev=old_rev)
    assert receipt["object_revision"] == push.revision
    assert bucket.pending == {"target_temperature": 22.5}
    # It adopts the receipt, drops the (now "equal") push, retries the PUT ...
    put(store, SHARED, {"hvac_heater_state": True}, if_rev=receipt["object_revision"])
    # ... and resubscribes: the change is sent again with a newer timestamp.
    pushes = store.handle_subscribe(SERIAL, listing(store))
    assert [p.value for p in pushes] == [{"target_temperature": 22.5}]
    assert pushes[0].timestamp > receipt["object_timestamp"]


def test_unconditional_bucket_put_on_old_base_requeues() -> None:
    store, _ = paired_store()
    bucket = store.device(SERIAL).buckets[DEVICE]
    old_rev = bucket.revision
    push = store.server_update(SERIAL, DEVICE, {"hot_water_boost_time_to_end": 123})
    store.mark_delivered(SERIAL, [push])
    store.handle_put(
        SERIAL,
        [{"object_key": DEVICE, "base_object_revision": old_rev, "value": {"rssi": 50}}],
    )
    assert bucket.pending == {"hot_water_boost_time_to_end": 123}


def test_zero_timestamp_never_pushes_stale_copy() -> None:
    store, _ = paired_store()
    objects = [{"object_key": SHARED, "object_revision": 0, "object_timestamp": 0}]
    pushes = store.handle_subscribe(SERIAL, objects)
    shared = [p for p in pushes if p.key == SHARED]
    assert shared and shared[0].value == {} and shared[0].timestamp == 0


def test_setpoint_expires_sooner_than_other_changes() -> None:
    store, clock = paired_store()
    store.server_update(SERIAL, SHARED, {"target_temperature": 23.0})
    store.server_update(SERIAL, DEVICE, {"hot_water_mode": "off"})
    clock.advance(31 * 60)
    store.expire_stale()
    record = store.device(SERIAL)
    assert record.buckets[SHARED].pending == {}
    assert record.buckets[DEVICE].pending == {"hot_water_mode": "off"}


def test_adopts_user_but_creates_missing_structure() -> None:
    store, _ = make_store()
    objects = [{"object_key": "user.abc123", "object_revision": 1, "object_timestamp": 5}]
    pushes = store.handle_subscribe(SERIAL, objects)
    record = store.device(SERIAL)
    assert record.adopted_user and not record.adopted_structure
    assert [p.key for p in pushes] == ["structure.homeassistant"]
    # Eco on our structure bucket is delivered.
    store.mark_delivered(SERIAL, pushes)
    push = store.server_update(SERIAL, "structure.homeassistant", {"manual_eco_all": True})
    assert push.value["manual_eco_all"] is True


def test_prune_forgets_devices_that_never_uploaded() -> None:
    store, clock = paired_store()
    store.touch("09AA01AB12345678")
    clock.advance(25 * 3600)
    store.touch(SERIAL)
    assert store.prune_unready(24 * 3600) == ["09AA01AB12345678"]
    assert store.serials == [SERIAL]


def test_weekly_schedule_waits_for_the_thermostat() -> None:
    store, clock = paired_store()
    store.server_update(SERIAL, SCHEDULE, {"ver": 2, "days": {}})
    clock.advance(30 * 24 * 3600)
    store.expire_stale()
    assert store.device(SERIAL).buckets[SCHEDULE].pending == {"ver": 2, "days": {}}


def test_schedule_is_not_resent_while_the_thermostat_applies_it() -> None:
    store, clock = paired_store()
    put(store, SCHEDULE, {"ver": 2, "days": {"0": {}}})
    assert store.handle_subscribe(SERIAL, listing(store)) == []
    bucket = store.device(SERIAL).buckets[SCHEDULE]
    old_ts = bucket.timestamp
    days = {"0": {"0": {"type": "HEAT", "time": 0, "entry_type": "setpoint", "temp": 20.0}}}
    push = store.server_update(SERIAL, SCHEDULE, {"days": days})
    store.mark_delivered(SERIAL, [push])

    # The thermostat combines schedule pushes for a while and still lists the old one.
    clock.advance(10)
    pushes = store.handle_subscribe(SERIAL, listing(store, {SCHEDULE: old_ts}))
    assert [p for p in pushes if p.key == SCHEDULE] == []
    assert bucket.inflight == {"days": days}

    # Not applied after the grace period: sent again.
    clock.advance(SCHEDULE_APPLY_SECONDS)
    pushes = store.handle_subscribe(SERIAL, listing(store, {SCHEDULE: old_ts}))
    assert [p.value for p in pushes if p.key == SCHEDULE] == [{"days": days}]
