"""Base entity and helpers for adding entities as thermostats appear."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

try:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
except ImportError:  # Home Assistant < 2025.2
    from homeassistant.helpers.entity_platform import (  # type: ignore[assignment]
        AddEntitiesCallback as AddConfigEntryEntitiesCallback,
    )

from .const import DOMAIN, MANUFACTURER, SIGNAL_DEVICE_UPDATE, SIGNAL_NEW_DEVICE

if TYPE_CHECKING:
    from . import NestLocalConfigEntry
    from .hub import NestLocalHub


def registry_device(hass: HomeAssistant, entry_id: str, serial: str) -> dr.DeviceEntry | None:
    """Look up the thermostat in the device registry (old and new API)."""
    registry = dr.async_get(hass)
    if hasattr(registry, "async_get_device_by_identifier"):
        return registry.async_get_device_by_identifier((DOMAIN, serial), entry_id)
    return registry.async_get_device(identifiers={(DOMAIN, serial)})


def device_info(hub: NestLocalHub, serial: str) -> DeviceInfo:
    """Device registry details for a thermostat."""
    record = hub.record(serial)
    info = record.info if record else {}
    device = hub.device(serial)
    # Keep the name chosen when the device was first registered, so entity
    # ids do not change when more details arrive later.
    existing = registry_device(hub.hass, hub.entry.entry_id, serial)
    result = DeviceInfo(
        identifiers={(DOMAIN, serial)},
        name=existing.name if existing and existing.name else hub.device_name(serial),
        manufacturer=MANUFACTURER,
        model="Learning Thermostat",
        serial_number=serial,
    )
    if model := info.get("model"):
        result["model_id"] = model
    if sw := info.get("software_version") or device.get("current_version"):
        result["sw_version"] = str(sw)
    if hw := info.get("backplate_model"):
        result["hw_version"] = str(hw)
    mac = info.get("mac") or device.get("mac_address")
    if isinstance(mac, str) and len(mac.replace(":", "")) == 12:
        result["connections"] = {(CONNECTION_NETWORK_MAC, format_mac(mac))}
    return result


class NestLocalEntity(Entity):
    """Entity bound to one thermostat."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: NestLocalHub, serial: str, key: str) -> None:
        self.hub = hub
        self.serial = serial
        self._attr_unique_id = f"{serial}-{key}"
        self._attr_device_info = device_info(hub, serial)

    @property
    def available(self) -> bool:
        """Entities are unavailable while the thermostat is offline."""
        return self.hub.is_online(self.serial)

    async def async_added_to_hass(self) -> None:
        """Follow updates for this thermostat."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_DEVICE_UPDATE.format(self.hub.entry.entry_id, self.serial),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


@callback
def async_setup_device_entities(
    hass: HomeAssistant,
    entry: NestLocalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[NestLocalHub, str, set[str]], Iterable[NestLocalEntity]],
) -> None:
    """Add entities now and whenever a thermostat reports new capabilities.

    ``build`` returns the entities that should exist for a serial and are not
    in the set of unique ids already added. It is called again on every
    update, because some data (e.g. hot water control) only arrives after the
    thermostat's first full upload.
    """
    hub = entry.runtime_data
    added: set[str] = set()

    @callback
    def _add(serial: str) -> None:
        if not hub.is_ready(serial):
            return  # wait until the thermostat has uploaded its state
        new = [
            entity
            for entity in build(hub, serial, added)
            if entity.unique_id and entity.unique_id not in added
        ]
        if new:
            added.update(e.unique_id for e in new if e.unique_id)
            async_add_entities(new)

    for serial in hub.store.serials:
        _add(serial)
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_NEW_DEVICE.format(entry.entry_id), _add)
    )
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_DEVICE_UPDATE.format(entry.entry_id, "any"), _add)
    )
