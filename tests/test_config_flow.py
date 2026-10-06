"""Config, reconfigure and options flows."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nest_local.const import (
    CONF_HEAT_TEMPERATURE,
    CONF_HOST,
    CONF_HOT_WATER_BOOST,
    CONF_PORT,
    CONF_SCHEDULE_ENTITY,
    CONF_SETBACK_TEMPERATURE,
    CONF_WEATHER,
    DEFAULT_PORT,
    DOMAIN,
)

PATCH_SETUP = "custom_components.nest_local.async_setup_entry"
PATCH_UNLOAD = "custom_components.nest_local.async_unload_entry"
PATCH_PORT = "custom_components.nest_local.config_flow._port_free"


async def test_user_flow(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(PATCH_PORT, return_value=True), patch(PATCH_SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: " 192.168.1.10 ", CONF_PORT: DEFAULT_PORT}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_HOST: "192.168.1.10", CONF_PORT: DEFAULT_PORT}


async def test_user_flow_errors(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with patch(PATCH_PORT, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "homeassistant.local", CONF_PORT: 9544}
        )
    assert result["errors"] == {CONF_HOST: "invalid_host"}

    with patch(PATCH_PORT, return_value=False):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.168.1.10", CONF_PORT: 9543}
        )
    assert result["errors"] == {CONF_PORT: "port_in_use"}

    with patch(PATCH_PORT, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: 9544}
        )
    assert result["errors"] == {CONF_HOST: "invalid_host"}


async def test_single_instance(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "192.168.1.10", CONF_PORT: 9544}).add_to_hass(
        hass
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_reconfigure(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "192.168.1.10", CONF_PORT: 9544})
    entry.add_to_hass(hass)
    with (
        patch(PATCH_SETUP, return_value=True) as setup,
        patch("custom_components.nest_local.async_unload_entry", return_value=True),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        result = await entry.start_reconfigure_flow(hass)
        assert result["type"] is FlowResultType.FORM
        with patch(PATCH_PORT, return_value=False):
            # Same port: not checked (our own server may be holding it).
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_HOST: "192.168.1.20", CONF_PORT: 9544}
            )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {CONF_HOST: "192.168.1.20", CONF_PORT: 9544}
    assert setup.call_count == 2  # initial set-up plus exactly one reload


async def test_options(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "192.168.1.10", CONF_PORT: 9544})
    entry.add_to_hass(hass)
    with patch(PATCH_SETUP, return_value=True), patch(PATCH_UNLOAD, return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_WEATHER: False, CONF_HOT_WATER_BOOST: 30}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_WEATHER: False,
        CONF_HOT_WATER_BOOST: 30,
        CONF_HEAT_TEMPERATURE: 21.0,
        CONF_SETBACK_TEMPERATURE: 16.0,
    }


async def test_options_weekly_schedule(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: "192.168.1.10", CONF_PORT: 9544})
    entry.add_to_hass(hass)
    chosen = {
        CONF_WEATHER: True,
        CONF_HOT_WATER_BOOST: 60,
        CONF_SCHEDULE_ENTITY: "schedule.heating",
        CONF_HEAT_TEMPERATURE: 16.0,
        CONF_SETBACK_TEMPERATURE: 16.0,
    }
    with patch(PATCH_SETUP, return_value=True), patch(PATCH_UNLOAD, return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], chosen)
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {CONF_HEAT_TEMPERATURE: "heat_not_above_setback"}

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**chosen, CONF_HEAT_TEMPERATURE: 20.5}
        )
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options[CONF_SCHEDULE_ENTITY] == "schedule.heating"
        assert entry.options[CONF_HEAT_TEMPERATURE] == 20.5

        # Emptying the field turns the weekly schedule off again.
        result = await hass.config_entries.options.async_init(entry.entry_id)
        schema = result["data_schema"].schema
        suggested = next(
            key.description["suggested_value"] for key in schema if key == CONF_SCHEDULE_ENTITY
        )
        assert suggested == "schedule.heating"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {key: value for key, value in entry.options.items() if key != CONF_SCHEDULE_ENTITY},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_SCHEDULE_ENTITY not in entry.options
