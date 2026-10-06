"""Sensors for a Nest thermostat."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import NestLocalConfigEntry
from .entity import (
    AddConfigEntryEntitiesCallback,
    NestLocalEntity,
    async_setup_device_entities,
)
from .hub import NestLocalHub

PARALLEL_UPDATES = 0

# Nest batteries are charged from the wiring; ~3.5 V is empty, ~4.0 V full.
BATTERY_EMPTY_V = 3.5
BATTERY_FULL_V = 4.0


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _battery_percent(hub: NestLocalHub, serial: str) -> float | None:
    voltage = _number(hub.device(serial).get("battery_level"))
    if voltage is None:
        return None
    ratio = (voltage - BATTERY_EMPTY_V) / (BATTERY_FULL_V - BATTERY_EMPTY_V)
    return round(max(0.0, min(1.0, ratio)) * 100)


def _time_to_target(hub: NestLocalHub, serial: str) -> datetime | None:
    value = _number(hub.device(serial).get("time_to_target"))
    if not value or value <= 0:
        return None
    if value > 1_000_000_000:  # some firmware reports an epoch timestamp
        return dt_util.utc_from_timestamp(value)
    # Otherwise it is seconds from when the thermostat reported it.
    return dt_util.utcnow() + timedelta(seconds=value)


def _outdoor(hub: NestLocalHub, serial: str) -> float | None:
    device = hub.device(serial)
    for key in ("outdoor_temperature", "outside_temperature"):
        if (value := _number(device.get(key))) is not None:
            return value
    return _number(hub.shared(serial).get("outside_temperature"))


def _partner(name: str) -> Callable[[NestLocalHub, str], Any]:
    def read(hub: NestLocalHub, serial: str) -> Any:
        return _number((hub.hvac_partner(serial) or {}).get(name))

    return read


def _partner_has(name: str) -> Callable[[NestLocalHub, str], bool]:
    def exists(hub: NestLocalHub, serial: str) -> bool:
        return name in (hub.hvac_partner(serial) or {})

    return exists


def _device_has(name: str) -> Callable[[NestLocalHub, str], bool]:
    def exists(hub: NestLocalHub, serial: str) -> bool:
        return name in hub.device(serial)

    return exists


@dataclass(frozen=True, kw_only=True)
class NestSensorDescription(SensorEntityDescription):
    """Describes a Nest sensor."""

    value_fn: Callable[[NestLocalHub, str], Any]
    exists_fn: Callable[[NestLocalHub, str], bool]


SENSORS: tuple[NestSensorDescription, ...] = (
    NestSensorDescription(
        key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda hub, s: _number(hub.shared(s).get("current_temperature")),
        exists_fn=lambda hub, s: "current_temperature" in hub.shared(s),
    ),
    NestSensorDescription(
        key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda hub, s: _number(hub.device(s).get("current_humidity")),
        exists_fn=_device_has("current_humidity"),
    ),
    NestSensorDescription(
        key="outdoor_temperature",
        translation_key="outdoor_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=_outdoor,
        exists_fn=lambda hub, s: _outdoor(hub, s) is not None,
    ),
    NestSensorDescription(
        key="time_to_target",
        translation_key="time_to_target",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_time_to_target,
        exists_fn=_device_has("time_to_target"),
    ),
    NestSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_battery_percent,
        exists_fn=_device_has("battery_level"),
    ),
    NestSensorDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda hub, s: _number(hub.device(s).get("battery_level")),
        exists_fn=_device_has("battery_level"),
    ),
    NestSensorDescription(
        key="backplate_temperature",
        translation_key="backplate_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda hub, s: _number(hub.device(s).get("backplate_temperature")),
        exists_fn=_device_has("backplate_temperature"),
    ),
    NestSensorDescription(
        key="wifi_signal",
        translation_key="wifi_signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda hub, s: (
            -abs(v) if (v := _number(hub.device(s).get("rssi"))) is not None else None
        ),
        exists_fn=_device_has("rssi"),
    ),
    NestSensorDescription(
        key="ip_address",
        translation_key="ip_address",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda hub, s: (
            hub.device(s).get("local_ip") or (hub.record(s).remote_ip if hub.record(s) else None)
        ),
        exists_fn=lambda hub, s: True,
    ),
    NestSensorDescription(
        key="eco_temperature_low",
        translation_key="eco_temperature_low",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda hub, s: _number(hub.device(s).get("away_temperature_low")),
        exists_fn=_device_has("away_temperature_low"),
    ),
    NestSensorDescription(
        key="boiler_water_temperature",
        translation_key="boiler_water_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=_partner("water_temp"),
        exists_fn=_partner_has("water_temp"),
    ),
    NestSensorDescription(
        key="boiler_setpoint",
        translation_key="boiler_setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_partner("boiler_setpoint"),
        exists_fn=_partner_has("boiler_setpoint"),
    ),
    NestSensorDescription(
        key="boiler_modulation",
        translation_key="boiler_modulation",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_partner("modulation"),
        exists_fn=_partner_has("modulation"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NestLocalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add sensors for the data each thermostat reports."""

    def build(hub: NestLocalHub, serial: str, added: set[str]) -> list[NestLocalEntity]:
        return [
            (NestTimeToTargetSensor if description.key == "time_to_target" else NestSensor)(
                hub, serial, description
            )
            for description in SENSORS
            if f"{serial}-{description.key}" not in added and description.exists_fn(hub, serial)
        ]

    async_setup_device_entities(hass, entry, async_add_entities, build)


class NestSensor(NestLocalEntity, SensorEntity):
    """A value reported by the thermostat."""

    entity_description: NestSensorDescription

    def __init__(self, hub: NestLocalHub, serial: str, description: NestSensorDescription) -> None:
        super().__init__(hub, serial, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Current value."""
        return self.entity_description.value_fn(self.hub, self.serial)


class NestTimeToTargetSensor(NestSensor):
    """Estimated time the target is reached, anchored to when it was reported."""

    _last_raw: Any = None
    _last_value: datetime | None = None

    @property
    def native_value(self) -> datetime | None:
        """Recompute only when the thermostat reports a new estimate."""
        raw = self.hub.device(self.serial).get("time_to_target")
        if raw != self._last_raw:
            self._last_raw = raw
            self._last_value = _time_to_target(self.hub, self.serial)
        return self._last_value
