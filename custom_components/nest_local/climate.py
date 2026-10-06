"""Climate entity for a Nest thermostat."""

from __future__ import annotations

import time
from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    FAN_AUTO,
    FAN_ON,
    PRESET_ECO,
    PRESET_NONE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, ServiceResponse, callback
from homeassistant.exceptions import ServiceValidationError

from . import NestLocalConfigEntry
from .const import (
    DOMAIN,
    ECO_AUTO,
    ECO_MANUAL,
    MAX_TEMP,
    MIN_TEMP,
    NEST_MODE_COOL,
    NEST_MODE_EMERGENCY,
    NEST_MODE_HEAT,
    NEST_MODE_OFF,
    NEST_MODE_RANGE,
    TEMP_STEP,
)
from .entity import (
    AddConfigEntryEntitiesCallback,
    NestLocalEntity,
    async_setup_device_entities,
)
from .hub import NestLocalHub
from .nest_schedule import describe_schedule

PARALLEL_UPDATES = 0

NEST_TO_HVAC: dict[str, HVACMode] = {
    NEST_MODE_OFF: HVACMode.OFF,
    NEST_MODE_HEAT: HVACMode.HEAT,
    NEST_MODE_COOL: HVACMode.COOL,
    NEST_MODE_RANGE: HVACMode.HEAT_COOL,
    NEST_MODE_EMERGENCY: HVACMode.HEAT,
}
HVAC_TO_NEST: dict[HVACMode, str] = {
    HVACMode.OFF: NEST_MODE_OFF,
    HVACMode.HEAT: NEST_MODE_HEAT,
    HVACMode.COOL: NEST_MODE_COOL,
    HVACMode.HEAT_COOL: NEST_MODE_RANGE,
}
HEAT_STATES = (
    "hvac_heater_state",
    "hvac_heat_x2_state",
    "hvac_heat_x3_state",
    "hvac_aux_heater_state",
    "hvac_alt_heat_state",
    "hvac_alt_heat_x2_state",
    "hvac_emer_heat_state",
)
COOL_STATES = ("hvac_ac_state", "hvac_cool_x2_state", "hvac_cool_x3_state")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NestLocalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a climate entity per thermostat once its state is known."""

    def build(hub: NestLocalHub, serial: str, added: set[str]) -> list[NestLocalEntity]:
        if f"{serial}-climate" in added:
            return []
        if "target_temperature_type" not in hub.shared(serial):
            return []
        return [NestClimate(hub, serial)]

    async_setup_device_entities(hass, entry, async_add_entities, build)


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class NestClimate(NestLocalEntity, ClimateEntity):
    """A Nest Learning Thermostat."""

    _attr_name = None
    _attr_translation_key = "thermostat"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = TEMP_STEP
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    _attr_preset_modes = [PRESET_NONE, PRESET_ECO]
    _attr_fan_modes = [FAN_AUTO, FAN_ON]

    def __init__(self, hub: NestLocalHub, serial: str) -> None:
        super().__init__(hub, serial, "climate")
        self._last_on_mode: str | None = None
        self._remember_mode()

    def _remember_mode(self) -> None:
        mode = self._nest_mode
        if mode in (NEST_MODE_HEAT, NEST_MODE_COOL, NEST_MODE_RANGE):
            self._last_on_mode = mode

    @callback
    def _handle_update(self) -> None:
        self._remember_mode()
        super()._handle_update()

    @property
    def _shared(self) -> dict[str, Any]:
        return self.hub.shared(self.serial)

    @property
    def _nest_mode(self) -> str:
        return str(self._shared.get("target_temperature_type", NEST_MODE_OFF)).lower()

    @property
    def available(self) -> bool:
        """Available while online and the mode is known."""
        return super().available and "target_temperature_type" in self._shared

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Features depend on the wiring the thermostat detected."""
        can_heat, can_cool = self.hub.capabilities(self.serial)
        features = (
            ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.PRESET_MODE
        )
        if can_heat or can_cool:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if can_heat and can_cool:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        if self.hub.device(self.serial).get("has_fan"):
            features |= ClimateEntityFeature.FAN_MODE
        return features

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Modes the wiring supports."""
        can_heat, can_cool = self.hub.capabilities(self.serial)
        modes = [HVACMode.OFF]
        if can_heat:
            modes.append(HVACMode.HEAT)
        if can_cool:
            modes.append(HVACMode.COOL)
        if can_heat and can_cool:
            modes.append(HVACMode.HEAT_COOL)
        return modes

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Current mode."""
        return NEST_TO_HVAC.get(self._nest_mode)

    @property
    def hvac_action(self) -> HVACAction:
        """What the HVAC equipment is doing right now."""
        shared = self._shared
        if self._nest_mode == NEST_MODE_OFF:
            return HVACAction.OFF
        if any(shared.get(state) for state in HEAT_STATES):
            return HVACAction.HEATING
        if any(shared.get(state) for state in COOL_STATES):
            return HVACAction.COOLING
        if shared.get("hvac_fan_state") or self._fan_timer_active:
            return HVACAction.FAN
        return HVACAction.IDLE

    @property
    def current_temperature(self) -> float | None:
        """Measured temperature."""
        return _as_float(self._shared.get("current_temperature"))

    @property
    def current_humidity(self) -> float | None:
        """Measured relative humidity."""
        return _as_float(self.hub.device(self.serial).get("current_humidity"))

    @property
    def target_temperature(self) -> float | None:
        """Setpoint in heat, cool and emergency modes."""
        if self._nest_mode in (NEST_MODE_RANGE, NEST_MODE_OFF):
            return None
        return _as_float(self._shared.get("target_temperature"))

    @property
    def target_temperature_low(self) -> float | None:
        """Lower setpoint in heat-cool mode."""
        if self._nest_mode != NEST_MODE_RANGE:
            return None
        return _as_float(self._shared.get("target_temperature_low"))

    @property
    def target_temperature_high(self) -> float | None:
        """Upper setpoint in heat-cool mode."""
        if self._nest_mode != NEST_MODE_RANGE:
            return None
        return _as_float(self._shared.get("target_temperature_high"))

    @property
    def preset_mode(self) -> str:
        """``eco`` while the thermostat is in either eco mode."""
        if self.hub.eco_mode(self.serial) in (ECO_MANUAL, ECO_AUTO):
            return PRESET_ECO
        return PRESET_NONE

    @property
    def _fan_timer_active(self) -> bool:
        timeout = self.hub.device(self.serial).get("fan_timer_timeout")
        return isinstance(timeout, (int, float)) and timeout > time.time()

    @property
    def fan_mode(self) -> str | None:
        """``on`` while the fan timer runs."""
        if not self.hub.device(self.serial).get("has_fan"):
            return None
        return FAN_ON if self._fan_timer_active else FAN_AUTO

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Eco details and whether a change is still on its way."""
        record = self.hub.record(self.serial)
        waiting = bool(record) and any(bucket.pending for bucket in record.buckets.values())
        return {
            "eco_mode": self.hub.eco_mode(self.serial),
            "waiting_for_thermostat": waiting,
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the setpoint(s), optionally switching mode first."""
        if (hvac_mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            await self.async_set_hvac_mode(hvac_mode)
        await self.hub.async_set_temperature(
            self.serial,
            temperature=kwargs.get(ATTR_TEMPERATURE),
            low=kwargs.get(ATTR_TARGET_TEMP_LOW),
            high=kwargs.get(ATTR_TARGET_TEMP_HIGH),
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Change the mode."""
        await self.hub.async_set_mode(self.serial, HVAC_TO_NEST[HVACMode(hvac_mode)])

    async def async_turn_on(self) -> None:
        """Return to the last mode that was not off (heat by default)."""
        can_heat, _ = self.hub.capabilities(self.serial)
        mode = self._last_on_mode
        if mode not in (NEST_MODE_HEAT, NEST_MODE_COOL, NEST_MODE_RANGE):
            mode = NEST_MODE_HEAT if can_heat else NEST_MODE_COOL
        await self.hub.async_set_mode(self.serial, mode)

    async def async_turn_off(self) -> None:
        """Switch the thermostat off."""
        await self.hub.async_set_mode(self.serial, NEST_MODE_OFF)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Enter or leave eco mode."""
        await self.hub.async_set_eco(self.serial, preset_mode == PRESET_ECO)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Run the fan timer or stop it."""
        await self.hub.async_set_fan(self.serial, fan_mode == FAN_ON)

    async def async_get_schedule(self) -> ServiceResponse:
        """The weekly schedule stored on the thermostat (nest_local.get_schedule)."""
        schedule = self.hub.schedule(self.serial)
        if not schedule.get("days"):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_schedule",
                translation_placeholders={"serial": self.serial},
            )
        return describe_schedule(schedule)
