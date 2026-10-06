"""Nest Local: Home Assistant acts as the cloud for Nest thermostats.

Works with Nest Learning Thermostats (gen 1/2) flashed with the
NoLongerEvil firmware. The thermostat's ``cloudregisterurl`` is pointed at
the small HTTP server this integration runs; no MQTT broker or separate
server is needed.
"""

from __future__ import annotations

import logging

from homeassistant.components import persistent_notification
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr, service
from homeassistant.helpers.typing import ConfigType

from .const import CONF_HOST, CONF_PORT, DEFAULT_PORT, DOMAIN
from .hub import NestLocalHub

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SERVICE_GET_SCHEDULE = "get_schedule"

type NestLocalConfigEntry = ConfigEntry[NestLocalHub]

_SETUP_MESSAGE = {
    "en": (
        "Nest Local is listening on port {port}.\n\n"
        "Point the thermostat at Home Assistant by setting its cloudregisterurl to\n\n"
        "`{url}`\n\n"
        "The easiest way is the `tools/nest-to-ha.sh` script from the integration's "
        "repository: it uses the thermostat's local API or SSH. By hand: set "
        '`<a key="cloudregisterurl" value="{url}"/>` in '
        "`/etc/nestlabs/client.config` on the thermostat and reboot it. "
        "The thermostat sends its full state after a restart."
    ),
    "pl": (
        "Nest Local nasłuchuje na porcie {port}.\n\n"
        "Skieruj termostat na Home Assistanta, ustawiając jego cloudregisterurl na\n\n"
        "`{url}`\n\n"
        "Najprościej zrobi to skrypt `tools/nest-to-ha.sh` z repozytorium integracji "
        "(przez lokalne API termostatu albo SSH). Ręcznie: w pliku "
        "`/etc/nestlabs/client.config` na termostacie ustaw "
        '`<a key="cloudregisterurl" value="{url}"/>` i zrestartuj termostat. '
        "Pełny stan termostat wysyła po restarcie."
    ),
}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions of the integration."""
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_GET_SCHEDULE,
        entity_domain=CLIMATE_DOMAIN,
        schema=None,
        func="async_get_schedule",
        supports_response=SupportsResponse.ONLY,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: NestLocalConfigEntry) -> bool:
    """Start the Nest server for this config entry."""
    hub = NestLocalHub(hass, entry)
    port = int(entry.data.get(CONF_PORT, DEFAULT_PORT))
    try:
        await hub.async_start()
    except OSError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="port_in_use",
            translation_placeholders={"port": str(port), "error": str(err)},
        ) from err
    entry.runtime_data = hub

    async def _async_stop(_event: Event) -> None:
        await hub.async_stop()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not any(hub.is_ready(serial) for serial in hub.store.serials):
        url = f"http://{entry.data[CONF_HOST]}:{port}/entry"
        message = _SETUP_MESSAGE.get(hass.config.language, _SETUP_MESSAGE["en"])
        persistent_notification.async_create(
            hass,
            message.format(port=port, url=url),
            title="Nest Local",
            notification_id=f"{DOMAIN}_setup",
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NestLocalConfigEntry) -> bool:
    """Stop the server and unload the platforms."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: NestLocalConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Allow deleting a thermostat that no longer connects."""
    hub = entry.runtime_data
    for domain, serial in device_entry.identifiers:
        if domain != DOMAIN:
            continue
        if hub.is_online(serial):
            return False
        hub.forget_device(serial)
    return True
