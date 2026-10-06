"""Hot water boost switch (Heat Link systems with hot water control)."""

from __future__ import annotations

import time
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant

from . import NestLocalConfigEntry
from .entity import (
    AddConfigEntryEntitiesCallback,
    NestLocalEntity,
    async_setup_device_entities,
)
from .hub import NestLocalHub

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NestLocalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the boost switch where the thermostat controls hot water."""

    def build(hub: NestLocalHub, serial: str, added: set[str]) -> list[NestLocalEntity]:
        if f"{serial}-hot_water_boost" in added:
            return []
        if not hub.device(serial).get("has_hot_water_control"):
            return []
        return [NestHotWaterBoost(hub, serial)]

    async_setup_device_entities(hass, entry, async_add_entities, build)


class NestHotWaterBoost(NestLocalEntity, SwitchEntity):
    """On while a hot water boost is running."""

    _attr_translation_key = "hot_water_boost"

    def __init__(self, hub: NestLocalHub, serial: str) -> None:
        super().__init__(hub, serial, "hot_water_boost")

    @property
    def is_on(self) -> bool:
        """True until the boost end time has passed."""
        end = self.hub.device(self.serial).get("hot_water_boost_time_to_end")
        return isinstance(end, (int, float)) and end > time.time()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Boost length used when switching on."""
        return {"boost_minutes": self.hub.hot_water_boost_minutes}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start a boost."""
        await self.hub.async_set_hot_water_boost(self.serial, self.hub.hot_water_boost_minutes)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Cancel the boost."""
        await self.hub.async_set_hot_water_boost(self.serial, 0)
