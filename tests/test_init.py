"""End-to-end tests: Home Assistant + protocol server + fake thermostat."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import socket
import time

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.components.climate import (
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODES,
    ATTR_PRESET_MODE,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_local.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_WEATHER,
    DOMAIN,
)
from custom_components.nest_local.entity import registry_device

from .fake_nest import SERIAL, FakeNest

CLIMATE = "climate.nest_living_room"
SHARED = f"shared.{SERIAL}"


@pytest.fixture
async def entry(hass: HomeAssistant, port: int) -> AsyncGenerator[MockConfigEntry]:
    """Set up the integration listening on a free port."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "127.0.0.1", CONF_PORT: port},
        options={CONF_WEATHER: False},
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
async def thermostat(
    hass: HomeAssistant, entry: MockConfigEntry, port: int, session: aiohttp.ClientSession
) -> FakeNest:
    """A fake thermostat that has booted against Home Assistant."""
    nest = FakeNest(session, f"http://127.0.0.1:{port}")
    await nest.boot()
    await hass.async_block_till_done()
    return nest


async def test_setup_shows_instructions_until_thermostat_connects(
    hass: HomeAssistant, entry: MockConfigEntry, port: int, session: aiohttp.ClientSession
) -> None:
    notifications = persistent_notification._async_get_or_create_notifications(hass)
    assert "nest_local_setup" in notifications
    assert f":{port}/entry" in notifications["nest_local_setup"]["message"]
    nest = FakeNest(session, f"http://127.0.0.1:{port}")
    await nest.entry()
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE) is None  # no state uploaded yet
    assert "nest_local_setup" in notifications
    await nest.subscribe()
    await nest.upload_full_state()
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE) is not None
    assert "nest_local_setup" not in notifications


async def test_climate_state(
    hass: HomeAssistant, entry: MockConfigEntry, thermostat: FakeNest
) -> None:
    state = hass.states.get(CLIMATE)
    assert state is not None
    assert state.state == HVACMode.HEAT
    assert state.attributes["current_temperature"] == 20.4  # rounded by HA
    assert state.attributes["temperature"] == 20.0
    assert state.attributes["current_humidity"] == 47
    assert state.attributes[ATTR_HVAC_MODES] == [HVACMode.OFF, HVACMode.HEAT]
    assert state.attributes[ATTR_HVAC_ACTION] == HVACAction.IDLE
    assert state.attributes[ATTR_PRESET_MODE] == "none"
    assert state.attributes["eco_mode"] == "schedule"

    assert hass.states.get("sensor.nest_living_room_temperature").state == "20.43"
    assert hass.states.get("sensor.nest_living_room_humidity").state == "47"
    assert hass.states.get("sensor.nest_living_room_battery").state == "88"
    assert hass.states.get("binary_sensor.nest_living_room_heating").state == STATE_OFF
    assert hass.states.get("binary_sensor.nest_living_room_connectivity").state == STATE_ON
    assert hass.states.get("select.nest_living_room_hot_water_mode").state == "schedule"

    device = registry_device(hass, entry.entry_id, SERIAL)
    assert device is not None
    assert device.sw_version == "5.9.3-5"
    assert device.model_id == "Diamond-2.6"
    assert device.serial_number == SERIAL


async def test_set_temperature_reaches_thermostat(
    hass: HomeAssistant, thermostat: FakeNest
) -> None:
    held = await thermostat.open_subscribe()
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {ATTR_ENTITY_ID: CLIMATE, "temperature": 21.5},
        blocking=True,
    )
    objects = await held.body(timeout=5)
    assert objects[0]["object_key"] == SHARED
    assert objects[0]["value"] == {"target_temperature": 21.5, "target_change_pending": True}
    assert thermostat.value("shared")["target_temperature"] == 21.5
    assert hass.states.get(CLIMATE).attributes["temperature"] == 21.5

    # Thermostat acknowledges the display wake; Home Assistant keeps 21.5.
    await thermostat.put(SHARED, {"target_change_pending": False})
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).attributes["temperature"] == 21.5
    assert hass.states.get(CLIMATE).attributes["waiting_for_thermostat"] is False


async def test_change_while_thermostat_sleeps_between_connections(
    hass: HomeAssistant, thermostat: FakeNest
) -> None:
    await hass.services.async_call(
        "climate", "set_hvac_mode", {ATTR_ENTITY_ID: CLIMATE, "hvac_mode": "off"}, blocking=True
    )
    assert hass.states.get(CLIMATE).state == HVACMode.OFF
    assert hass.states.get(CLIMATE).attributes["waiting_for_thermostat"] is True
    objects = await thermostat.subscribe()
    assert objects[0]["value"] == {"target_temperature_type": "off"}
    assert thermostat.value("shared")["target_temperature_type"] == "off"
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).attributes["waiting_for_thermostat"] is False


async def test_thermostat_updates_home_assistant(hass: HomeAssistant, thermostat: FakeNest) -> None:
    await thermostat.put(SHARED, {"hvac_heater_state": True, "current_temperature": 19.5})
    await hass.async_block_till_done()
    state = hass.states.get(CLIMATE)
    assert state.attributes[ATTR_HVAC_ACTION] == HVACAction.HEATING
    assert state.attributes["current_temperature"] == 19.5
    assert hass.states.get("binary_sensor.nest_living_room_heating").state == STATE_ON

    # A dial turn on the thermostat wins over nothing pending.
    await thermostat.put(SHARED, {"target_temperature": 22.5})
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).attributes["temperature"] == 22.5


async def test_eco_preset(hass: HomeAssistant, thermostat: FakeNest) -> None:
    await hass.services.async_call(
        "climate",
        "set_preset_mode",
        {ATTR_ENTITY_ID: CLIMATE, "preset_mode": "eco"},
        blocking=True,
    )
    objects = await thermostat.subscribe()
    structure = next(o for o in objects if o["object_key"] == "structure.homeassistant")
    assert structure["value"]["manual_eco_all"] is True
    assert abs(structure["value"]["manual_eco_timestamp"] - time.time()) < 5
    # The thermostat reports that it entered eco.
    await thermostat.put(f"device.{SERIAL}", {"eco": {"mode": "manual-eco", "touched_by": 3}})
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).attributes[ATTR_PRESET_MODE] == "eco"

    await hass.services.async_call(
        "climate",
        "set_preset_mode",
        {ATTR_ENTITY_ID: CLIMATE, "preset_mode": "none"},
        blocking=True,
    )
    objects = await thermostat.subscribe()
    by_key = {o["object_key"]: o["value"] for o in objects}
    assert by_key["structure.homeassistant"]["manual_eco_all"] is False
    assert by_key["structure.homeassistant"]["away"] is False
    assert by_key[f"device.{SERIAL}"]["eco"]["mode"] == "schedule"
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).attributes[ATTR_PRESET_MODE] == "none"


async def test_unsupported_mode_is_rejected(hass: HomeAssistant, thermostat: FakeNest) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {ATTR_ENTITY_ID: CLIMATE, "hvac_mode": "cool"},
            blocking=True,
        )


async def test_hot_water_boost(hass: HomeAssistant, thermostat: FakeNest) -> None:
    await hass.services.async_call(
        "switch",
        "turn_on",
        {ATTR_ENTITY_ID: "switch.nest_living_room_hot_water_boost"},
        blocking=True,
    )
    assert hass.states.get("switch.nest_living_room_hot_water_boost").state == STATE_ON
    objects = await thermostat.subscribe()
    end = objects[0]["value"]["hot_water_boost_time_to_end"]
    assert 3500 < end - time.time() <= 3600

    await hass.services.async_call(
        "select",
        "select_option",
        {ATTR_ENTITY_ID: "select.nest_living_room_hot_water_mode", "option": "off"},
        blocking=True,
    )
    objects = await thermostat.subscribe()
    assert objects[0]["value"] == {"hot_water_mode": "off"}


async def test_offline_and_back(
    hass: HomeAssistant, entry: MockConfigEntry, thermostat: FakeNest
) -> None:
    hub = entry.runtime_data
    hub.store.device(SERIAL).last_seen -= 3600
    await hub._async_watchdog()
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).state == STATE_UNAVAILABLE
    assert hass.states.get("binary_sensor.nest_living_room_connectivity").state == STATE_OFF

    await thermostat.entry()
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).state == HVACMode.HEAT


async def test_state_survives_reload(
    hass: HomeAssistant, entry: MockConfigEntry, thermostat: FakeNest
) -> None:
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(CLIMATE)
    assert state is not None
    assert state.state == HVACMode.HEAT
    # The thermostat reconnects and the server still knows its revisions.
    assert await thermostat.subscribe() != []  # structure is re-sent once
    held = await thermostat.open_subscribe()
    assert await held.still_open_after(0.3)
    await held.close()


async def test_unload_frees_port(hass: HomeAssistant, entry: MockConfigEntry, port: int) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", port))


async def test_port_in_use_retries_later(hass: HomeAssistant, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("", port))
        blocker.listen()
        config_entry = MockConfigEntry(
            domain=DOMAIN, data={CONF_HOST: "127.0.0.1", CONF_PORT: port}
        )
        config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        assert config_entry.state is ConfigEntryState.SETUP_RETRY
    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_diagnostics(
    hass: HomeAssistant, entry: MockConfigEntry, thermostat: FakeNest
) -> None:
    from custom_components.nest_local.diagnostics import (  # noqa: PLC0415
        async_get_config_entry_diagnostics,
    )

    data = await async_get_config_entry_diagnostics(hass, entry)
    device = data["devices"][SERIAL]
    assert device["online"] is True
    assert device["buckets"][f"device.{SERIAL}"]["value"]["local_ip"] == "**REDACTED**"
    assert device["buckets"][SHARED]["value"]["target_temperature"] == 20.0


async def test_eco_toggled_before_delivery_keeps_last_choice(
    hass: HomeAssistant, thermostat: FakeNest
) -> None:
    for preset in ("eco", "none", "eco"):
        await hass.services.async_call(
            "climate",
            "set_preset_mode",
            {ATTR_ENTITY_ID: CLIMATE, "preset_mode": preset},
            blocking=True,
        )
        # The preset follows the request at once, before the thermostat reports.
        assert hass.states.get(CLIMATE).attributes[ATTR_PRESET_MODE] == preset
    objects = await thermostat.subscribe()
    by_key = {o["object_key"]: o["value"] for o in objects}
    assert by_key["structure.homeassistant"]["manual_eco_all"] is True
    assert "away" not in by_key["structure.homeassistant"]
    assert f"device.{SERIAL}" not in by_key  # no "leave eco" write slipped through


async def test_time_to_target_is_anchored(hass: HomeAssistant, thermostat: FakeNest) -> None:
    await thermostat.put(f"device.{SERIAL}", {"time_to_target": 1200})
    await hass.async_block_till_done()
    first = hass.states.get("sensor.nest_living_room_target_reached_at").state
    await thermostat.put(f"device.{SERIAL}", {"current_humidity": 50})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.nest_living_room_target_reached_at").state == first
