"""Nest Local: Home Assistant acts as the cloud for Nest thermostats.

Works with Nest Learning Thermostats (gen 1/2) flashed with the
NoLongerEvil firmware. The thermostat's ``cloudregisterurl`` is pointed at
the small HTTP server this integration runs; no MQTT broker or separate
server is needed.
"""

from __future__ import annotations

import logging

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

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

type NestLocalConfigEntry = ConfigEntry[NestLocalHub]

_SETUP_MESSAGE = {
    "en": (
        "Nest Local is listening on port {port}.\n\n"
        "Point the thermostat at Home Assistant: SSH to the thermostat "
        "(`ssh root@<thermostat IP>`), edit `/etc/nestlabs/client.config` and set\n\n"
        '`<a key="cloudregisterurl" value="{url}"/>`\n\n'
        "then reboot the thermostat. It sends its full state only after a reboot."
    ),
    "pl": (
        "Nest Local nasłuchuje na porcie {port}.\n\n"
        "Skieruj termostat na Home Assistanta: zaloguj się przez SSH "
        "(`ssh root@<IP termostatu>`), w pliku `/etc/nestlabs/client.config` ustaw\n\n"
        '`<a key="cloudregisterurl" value="{url}"/>`\n\n'
        "i zrestartuj termostat. Pełny stan wysyła tylko po restarcie."
    ),
}


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
