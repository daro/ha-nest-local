"""Weekly schedule: a Schedule helper is stored on the (fake) thermostat."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import timedelta
import logging
from typing import Any
from unittest.mock import patch

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.nest_local.const import (
    CONF_HEAT_TEMPERATURE,
    CONF_HOST,
    CONF_PORT,
    CONF_SCHEDULE_ENTITY,
    CONF_SETBACK_TEMPERATURE,
    CONF_WEATHER,
    DOMAIN,
)
from custom_components.nest_local.hub import NestLocalHub

from .fake_nest import SERIAL, FakeNest, own_schedule

# The Schedule helper keeps a timer for its next period after the test ends.
pytestmark = pytest.mark.parametrize("expected_lingering_timers", [True])

HELPER = "schedule.heating"
CLIMATE = "climate.nest_living_room"
SCHEDULE = f"schedule.{SERIAL}"
SHARED = f"shared.{SERIAL}"
H = 3600

HELPER_CONFIG: dict[str, Any] = {
    "name": "Heating",
    "monday": [{"from": "10:00:00", "to": "18:00:00"}],
    "tuesday": [
        {"from": "07:00:00", "to": "09:00:00"},
        {"from": "17:00:00", "to": "22:00:00", "data": {"temperature": 22}},
    ],
}


def setpoint(seconds: int, temp: float) -> dict[str, Any]:
    return {"type": "HEAT", "time": seconds, "entry_type": "setpoint", "temp": temp}


EXPECTED_DAYS = {
    "0": {"0": setpoint(10 * H, 21.0), "1": setpoint(18 * H, 16.0)},
    "1": {
        "0": setpoint(7 * H, 21.0),
        "1": setpoint(9 * H, 16.0),
        "2": setpoint(17 * H, 22.0),
        "3": setpoint(22 * H, 16.0),
    },
    "2": {},
    "3": {},
    "4": {},
    "5": {},
    "6": {},
}


@pytest.fixture
async def helper(hass: HomeAssistant) -> str:
    assert await async_setup_component(hass, "schedule", {"schedule": {"heating": HELPER_CONFIG}})
    await hass.async_block_till_done()
    return HELPER


@pytest.fixture
async def entry(hass: HomeAssistant, port: int, helper: str) -> AsyncGenerator[MockConfigEntry]:
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "127.0.0.1", CONF_PORT: port},
        options={
            CONF_WEATHER: False,
            CONF_SCHEDULE_ENTITY: helper,
            CONF_HEAT_TEMPERATURE: 21.0,
            CONF_SETBACK_TEMPERATURE: 16.0,
        },
        title="Nest Local",
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    yield config_entry
    if config_entry.state is ConfigEntryState.LOADED:
        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()


@pytest.fixture
async def nest(
    hass: HomeAssistant, entry: MockConfigEntry, port: int, session: aiohttp.ClientSession
) -> FakeNest:
    """A thermostat that booted, so Home Assistant has queued the schedule."""
    thermostat = FakeNest(session, f"http://127.0.0.1:{port}")
    await thermostat.boot()
    await settle(hass)
    return thermostat


async def settle(hass: HomeAssistant) -> None:
    """Let the schedule sync run (it waits a moment to combine triggers)."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=20))
    await hass.async_block_till_done()


def hub_of(entry: MockConfigEntry) -> NestLocalHub:
    return entry.runtime_data


def queued(entry: MockConfigEntry) -> dict[str, Any]:
    return hub_of(entry).store.device(SERIAL).buckets[SCHEDULE].pending


async def edit_helper(hass: HomeAssistant, config: dict[str, Any]) -> None:
    with patch(
        "homeassistant.config.load_yaml_config_file",
        autospec=True,
        return_value={"schedule": {"heating": config}},
    ):
        await hass.services.async_call("schedule", "reload", blocking=True)
    await settle(hass)


async def test_helper_schedule_is_stored_on_thermostat(
    hass: HomeAssistant, entry: MockConfigEntry, nest: FakeNest
) -> None:
    pushed = {obj["object_key"]: obj["value"] for obj in await nest.subscribe()}
    assert pushed[SCHEDULE] == {
        "ver": 2,
        "name": "Home Assistant",
        "schedule_mode": "HEAT",
        "days": EXPECTED_DAYS,
    }
    # Auto-Schedule would rewrite the schedule, so it is switched off.
    assert pushed[f"device.{SERIAL}"] == {"learning_mode": False}
    assert nest.value("schedule")["days"] == EXPECTED_DAYS
    assert nest.value("device")["learning_mode"] is False

    response = await hass.services.async_call(
        DOMAIN, "get_schedule", {ATTR_ENTITY_ID: CLIMATE}, blocking=True, return_response=True
    )
    days = response[CLIMATE]["days"]
    assert response[CLIMATE]["mode"] == "heat"
    assert days["monday"] == [
        {"time": "10:00", "temperature": 21.0},
        {"time": "18:00", "temperature": 16.0},
    ]
    assert days["tuesday"][2] == {"time": "17:00", "temperature": 22.0}
    assert days["sunday"] == []


async def test_thermostat_copy_with_continuations_is_accepted(
    hass: HomeAssistant, entry: MockConfigEntry, nest: FakeNest
) -> None:
    await nest.subscribe()
    # The thermostat writes the schedule back with its continuation entries.
    days = {key: dict(entries) for key, entries in EXPECTED_DAYS.items()}
    for day in ("2", "3", "4", "5", "6"):
        days[day] = {"0": {**setpoint(0, 16.0), "entry_type": "continuation"}}
    await nest.put(
        SCHEDULE, {"ver": 2, "name": "Home Assistant", "schedule_mode": "HEAT", "days": days}
    )
    await settle(hass)
    assert queued(entry) == {}
    assert hub_of(entry).schedule_sync.last_error is None


async def test_changes_on_the_thermostat_are_replaced(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    nest: FakeNest,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await nest.subscribe()
    with (
        patch("custom_components.nest_local.schedule_sync.RESEND_AFTER", 0),
        patch("custom_components.nest_local.schedule_sync.MAX_PUSHES_PER_DAY", 3),
    ):
        # Sent once at boot; two more corrections fit in the daily limit.
        for _ in range(2):
            await nest.put(SCHEDULE, own_schedule(6, 23))
            await settle(hass)
            assert queued(entry)["days"] == EXPECTED_DAYS
            await nest.subscribe()
            assert nest.value("schedule")["days"] == EXPECTED_DAYS
        # A thermostat that keeps changing it is left alone until the helper changes.
        with caplog.at_level(logging.WARNING):
            await nest.put(SCHEDULE, own_schedule(6, 23))
            await settle(hass)
        assert queued(entry) == {}
        assert "keeps changing the weekly schedule" in caplog.text

        await edit_helper(hass, {"name": "Heating", "monday": [{"from": "05:00", "to": "06:00"}]})
        assert queued(entry)["days"]["0"] == {
            "0": setpoint(5 * H, 21.0),
            "1": setpoint(6 * H, 16.0),
        }


async def test_helper_edit_and_heating_mode(
    hass: HomeAssistant, entry: MockConfigEntry, nest: FakeNest
) -> None:
    await nest.subscribe()
    await nest.put(SHARED, {"target_temperature_type": "off"})
    await settle(hass)

    # While the thermostat is off, a heating schedule is not sent.
    new_config = {**HELPER_CONFIG, "sunday": [{"from": "08:00", "to": "24:00"}]}
    await edit_helper(hass, new_config)
    assert queued(entry) == {}

    await nest.put(SHARED, {"target_temperature_type": "heat"})
    await settle(hass)
    days = queued(entry)["days"]
    assert days["6"] == {"0": setpoint(8 * H, 21.0)}
    # Sunday's period runs to midnight, so Monday starts cool.
    assert days["0"]["0"] == setpoint(0, 16.0)


async def test_schedule_mode_reported_by_thermostat_decides(
    hass: HomeAssistant, entry: MockConfigEntry, nest: FakeNest
) -> None:
    await nest.subscribe()
    # A thermostat on a cooling schedule ignores a heating one.
    await nest.put(SHARED, {"schedule_mode": "COOL"})
    await edit_helper(hass, {"name": "Heating", "friday": [{"from": "12:00", "to": "13:00"}]})
    assert queued(entry) == {}
    await nest.put(SHARED, {"schedule_mode": "heat"})
    await settle(hass)
    assert queued(entry)["days"]["4"] == {"0": setpoint(12 * H, 21.0), "1": setpoint(13 * H, 16.0)}


async def test_helper_is_read_only_after_it_changes(
    hass: HomeAssistant, entry: MockConfigEntry, nest: FakeNest
) -> None:
    calls = async_capture_events(hass, EVENT_CALL_SERVICE)
    await nest.subscribe()
    for minutes in (1, 2, 3):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
        await hass.async_block_till_done()
    assert [c for c in calls if c.data["service"] == "get_schedule"] == []

    await edit_helper(hass, {**HELPER_CONFIG, "saturday": [{"from": "09:00", "to": "10:00"}]})
    assert len([c for c in calls if c.data["service"] == "get_schedule"]) == 1
    assert "5" in {day for day, entries in queued(entry)["days"].items() if entries}


async def test_auto_schedule_switched_off_again(
    hass: HomeAssistant, entry: MockConfigEntry, nest: FakeNest
) -> None:
    await nest.subscribe()
    # The next connection confirms the schedule and learning_mode; nothing is
    # left to send, so the server holds it open.
    held = await nest.open_subscribe()
    assert await held.still_open_after(0.2)
    await held.close()
    device = hub_of(entry).store.device(SERIAL).buckets[f"device.{SERIAL}"]
    assert device.pending == {} and device.inflight == {}

    with patch("custom_components.nest_local.hub.LEARNING_OFF_RETRY", 0):
        await nest.put(f"device.{SERIAL}", {"learning_mode": True})
        await settle(hass)
    assert device.pending == {"learning_mode": False}
    assert queued(entry) == {}  # the schedule itself is still followed


async def test_missing_helper_and_schedule(
    hass: HomeAssistant,
    port: int,
    session: aiohttp.ClientSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "127.0.0.1", CONF_PORT: port},
        options={CONF_WEATHER: False, CONF_SCHEDULE_ENTITY: "schedule.gone"},
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    nest = FakeNest(session, f"http://127.0.0.1:{port}")
    await nest.boot()
    await settle(hass)

    for minutes in (1, 2, 3):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
        await hass.async_block_till_done()

    hub = hub_of(config_entry)
    assert "schedule.gone does not exist" in hub.schedule_sync.last_error
    assert caplog.text.count("schedule.gone does not exist") == 1
    assert "Referenced entities" not in caplog.text
    assert hub.store.device(SERIAL).buckets[SCHEDULE].pending == {}

    from custom_components.nest_local.diagnostics import (  # noqa: PLC0415
        async_get_config_entry_diagnostics,
    )

    diagnostics = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diagnostics["schedule_sync"]["entity_id"] == "schedule.gone"

    del hub.store.device(SERIAL).buckets[SCHEDULE]
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "get_schedule", {ATTR_ENTITY_ID: CLIMATE}, blocking=True, return_response=True
        )
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
