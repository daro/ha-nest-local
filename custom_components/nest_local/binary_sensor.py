"""Binary sensors for a Nest thermostat."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from . import NestLocalConfigEntry
from .climate import COOL_STATES, HEAT_STATES
from .entity import (
    AddConfigEntryEntitiesCallback,
    NestLocalEntity,
    async_setup_device_entities,
)
from .hub import NestLocalHub

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class NestBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a Nest binary sensor."""

    value_fn: Callable[[NestLocalHub, str], bool | None]
    exists_fn: Callable[[NestLocalHub, str], bool]
    always_available: bool = False


def _occupied(hub: NestLocalHub, serial: str) -> bool | None:
    away = hub.field(serial, "auto_away")
    if not isinstance(away, (int, float)):
        return None
    return away <= 0


BINARY_SENSORS: tuple[NestBinarySensorDescription, ...] = (
    NestBinarySensorDescription(
        key="connectivity",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda hub, s: hub.is_online(s),
        exists_fn=lambda hub, s: True,
        always_available=True,
    ),
    NestBinarySensorDescription(
        key="heating",
        translation_key="heating",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda hub, s: any(hub.shared(s).get(k) for k in HEAT_STATES),
        exists_fn=lambda hub, s: "hvac_heater_state" in hub.shared(s),
    ),
    NestBinarySensorDescription(
        key="cooling",
        translation_key="cooling",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda hub, s: any(hub.shared(s).get(k) for k in COOL_STATES),
        exists_fn=lambda hub, s: hub.capabilities(s)[1],
    ),
    NestBinarySensorDescription(
        key="fan",
        translation_key="fan",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda hub, s: bool(hub.shared(s).get("hvac_fan_state")),
        exists_fn=lambda hub, s: bool(hub.device(s).get("has_fan")),
    ),
    NestBinarySensorDescription(
        key="occupancy",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        value_fn=_occupied,
        exists_fn=lambda hub, s: hub.field(s, "auto_away") is not None,
    ),
    NestBinarySensorDescription(
        key="hot_water",
        translation_key="hot_water",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda hub, s: bool(hub.device(s).get("hot_water_active")),
        exists_fn=lambda hub, s: bool(hub.device(s).get("has_hot_water_control")),
    ),
    NestBinarySensorDescription(
        key="boiler_flame",
        translation_key="boiler_flame",
        device_class=BinarySensorDeviceClass.HEAT,
        value_fn=lambda hub, s: bool((hub.hvac_partner(s) or {}).get("flame_on")),
        exists_fn=lambda hub, s: "flame_on" in (hub.hvac_partner(s) or {}),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NestLocalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add binary sensors for each thermostat."""

    def build(hub: NestLocalHub, serial: str, added: set[str]) -> list[NestLocalEntity]:
        return [
            NestBinarySensor(hub, serial, description)
            for description in BINARY_SENSORS
            if f"{serial}-{description.key}" not in added and description.exists_fn(hub, serial)
        ]

    async_setup_device_entities(hass, entry, async_add_entities, build)


class NestBinarySensor(NestLocalEntity, BinarySensorEntity):
    """An on/off state reported by the thermostat."""

    entity_description: NestBinarySensorDescription

    def __init__(
        self, hub: NestLocalHub, serial: str, description: NestBinarySensorDescription
    ) -> None:
        super().__init__(hub, serial, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """The connectivity sensor stays available to show 'disconnected'."""
        return self.entity_description.always_available or super().available

    @property
    def is_on(self) -> bool | None:
        """Current state."""
        return self.entity_description.value_fn(self.hub, self.serial)
