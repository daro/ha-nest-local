"""Constants for the Nest Local integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "nest_local"
MANUFACTURER: Final = "Nest Labs"

CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_WEATHER: Final = "weather_proxy"
CONF_HOT_WATER_BOOST: Final = "hot_water_boost_minutes"

DEFAULT_PORT: Final = 9544
DEFAULT_WEATHER: Final = True
DEFAULT_HOT_WATER_BOOST: Final = 60

STORAGE_VERSION: Final = 1
SAVE_DELAY: Final = 30

# A thermostat counts as offline when it has not been heard from for this long.
# It normally reconnects every ~5 minutes; a low battery adds up to ~4 minutes.
OFFLINE_AFTER: Final = 600
WATCHDOG_INTERVAL: Final = 60

MIN_TEMP: Final = 9.0
MAX_TEMP: Final = 32.0
TEMP_STEP: Final = 0.5

WEATHER_URL: Final = "https://weather.nest.com/weather/v1"
WEATHER_CACHE_SECONDS: Final = 600

SIGNAL_NEW_DEVICE: Final = f"{DOMAIN}_new_device_{{}}"
SIGNAL_DEVICE_UPDATE: Final = f"{DOMAIN}_update_{{}}_{{}}"

# Nest mode (target_temperature_type) values.
NEST_MODE_OFF: Final = "off"
NEST_MODE_HEAT: Final = "heat"
NEST_MODE_COOL: Final = "cool"
NEST_MODE_RANGE: Final = "range"
NEST_MODE_EMERGENCY: Final = "emergency"

ECO_SCHEDULE: Final = "schedule"
ECO_MANUAL: Final = "manual-eco"
ECO_AUTO: Final = "auto-eco"

HOT_WATER_MODES: Final = ["schedule", "off"]

# Fields hidden from diagnostics downloads.
REDACT_FIELDS: Final = {
    "mac",
    "mac_address",
    "local_ip",
    "remote_ip",
    "postal_code",
    "weave_device_id",
    "location",
    "email",
    "serial_number",
    "ip_address",
    "wifi_mac_address",
    "thread_mac_address",
    "latitude",
    "longitude",
}
