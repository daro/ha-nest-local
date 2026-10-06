"""Hot water mode select (Heat Link systems with hot water control)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant

from . import NestLocalConfigEntry
from .const import HOT_WATER_MODES
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
    """Add the hot water mode select where it applies."""

    def build(hub: NestLocalHub, serial: str, added: set[str]) -> list[NestLocalEntity]:
        if f"{serial}-hot_water_mode" in added:
            return []
        if not hub.device(serial).get("has_hot_water_control"):
            return []
        return [NestHotWaterMode(hub, serial)]

    async_setup_device_entities(hass, entry, async_add_entities, build)


class NestHotWaterMode(NestLocalEntity, SelectEntity):
    """Hot water follows its schedule or is off."""

    _attr_translation_key = "hot_water_mode"
    _attr_options = HOT_WATER_MODES

    def __init__(self, hub: NestLocalHub, serial: str) -> None:
        super().__init__(hub, serial, "hot_water_mode")

    @property
    def current_option(self) -> str | None:
        """Current hot water mode."""
        mode = self.hub.device(self.serial).get("hot_water_mode")
        return mode if mode in HOT_WATER_MODES else None

    async def async_select_option(self, option: str) -> None:
        """Change the hot water mode."""
        await self.hub.async_set_hot_water_mode(self.serial, option)
