"""Config flow for Nest Local."""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)
import voluptuous as vol

from .const import (
    CONF_HEAT_TEMPERATURE,
    CONF_HOST,
    CONF_HOT_WATER_BOOST,
    CONF_PORT,
    CONF_SCHEDULE_ENTITY,
    CONF_SETBACK_TEMPERATURE,
    CONF_WEATHER,
    DEFAULT_HEAT_TEMPERATURE,
    DEFAULT_HOT_WATER_BOOST,
    DEFAULT_PORT,
    DEFAULT_SETBACK_TEMPERATURE,
    DEFAULT_WEATHER,
    DOMAIN,
    MAX_TEMP,
    MIN_TEMP,
    TEMP_STEP,
)

TEMPERATURE_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_TEMP,
        max=MAX_TEMP,
        step=TEMP_STEP,
        unit_of_measurement="°C",
        mode=NumberSelectorMode.BOX,
    )
)

_LOGGER = logging.getLogger(__name__)


def _valid_ipv4(host: str) -> bool:
    """The thermostat cannot resolve mDNS names, so an IPv4 address is required."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.version == 4 and not address.is_loopback and not address.is_unspecified


def _port_free(port: int) -> bool:
    """Return True if nothing else listens on the port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", port))
        except OSError:
            return False
    return True


async def _detect_ip(hass: HomeAssistant) -> str:
    """Best guess of the address the thermostat can reach."""
    try:
        from homeassistant.components import network  # noqa: PLC0415

        return await network.async_get_source_ip(hass)
    except Exception:
        _LOGGER.debug("Could not detect the local IP address", exc_info=True)
        return ""


def _connection_schema(host: str, port: int) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=host): str,
            vol.Required(CONF_PORT, default=port): vol.All(
                vol.Coerce(int), vol.Range(min=1024, max=65535)
            ),
        }
    )


class NestLocalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up the local Nest server."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options for an existing entry."""
        return NestLocalOptionsFlow()

    async def _validate(
        self, user_input: dict[str, Any], current_port: int | None = None
    ) -> dict[str, str]:
        errors: dict[str, str] = {}
        if not _valid_ipv4(user_input[CONF_HOST].strip()):
            errors[CONF_HOST] = "invalid_host"
        port = int(user_input[CONF_PORT])
        if port != current_port and not await self.hass.async_add_executor_job(_port_free, port):
            errors[CONF_PORT] = "port_in_use"
        return errors

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the address the thermostat should use."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._validate(user_input)
            if not errors:
                host = user_input[CONF_HOST].strip()
                port = int(user_input[CONF_PORT])
                return self.async_create_entry(
                    title="Nest Local", data={CONF_HOST: host, CONF_PORT: port}
                )
        host = user_input[CONF_HOST] if user_input else await _detect_ip(self.hass)
        port = int(user_input[CONF_PORT]) if user_input else DEFAULT_PORT
        return self.async_show_form(
            step_id="user",
            data_schema=_connection_schema(host, port),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the address or port."""
        entry = self._get_reconfigure_entry()
        current_port = int(entry.data.get(CONF_PORT, DEFAULT_PORT))
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._validate(user_input, current_port)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_HOST: user_input[CONF_HOST].strip(),
                        CONF_PORT: int(user_input[CONF_PORT]),
                    },
                )
        host = user_input[CONF_HOST] if user_input else entry.data[CONF_HOST]
        port = int(user_input[CONF_PORT]) if user_input else current_port
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema(host, port),
            errors=errors,
        )


class NestLocalOptionsFlow(OptionsFlowWithReload):
    """Behaviour options (the entry reloads when they change)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Weather proxy, hot water boost length and the weekly schedule."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if (
                user_input.get(CONF_SCHEDULE_ENTITY)
                and user_input[CONF_HEAT_TEMPERATURE] <= user_input[CONF_SETBACK_TEMPERATURE]
            ):
                errors[CONF_HEAT_TEMPERATURE] = "heat_not_above_setback"
            else:
                return self.async_create_entry(data=user_input)
        values = user_input or self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(CONF_WEATHER, default=values.get(CONF_WEATHER, DEFAULT_WEATHER)): bool,
                vol.Required(
                    CONF_HOT_WATER_BOOST,
                    default=values.get(CONF_HOT_WATER_BOOST, DEFAULT_HOT_WATER_BOOST),
                ): vol.All(vol.Coerce(int), vol.Range(min=15, max=240)),
                # Optional and cleared by emptying the field, hence no default.
                vol.Optional(
                    CONF_SCHEDULE_ENTITY,
                    description={"suggested_value": values.get(CONF_SCHEDULE_ENTITY)},
                ): EntitySelector(EntitySelectorConfig(domain="schedule")),
                vol.Required(
                    CONF_HEAT_TEMPERATURE,
                    default=values.get(CONF_HEAT_TEMPERATURE, DEFAULT_HEAT_TEMPERATURE),
                ): TEMPERATURE_SELECTOR,
                vol.Required(
                    CONF_SETBACK_TEMPERATURE,
                    default=values.get(CONF_SETBACK_TEMPERATURE, DEFAULT_SETBACK_TEMPERATURE),
                ): TEMPERATURE_SELECTOR,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
