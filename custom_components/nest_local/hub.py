"""Connects the Nest protocol server to Home Assistant."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
from typing import Any

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

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
    ECO_AUTO,
    ECO_MANUAL,
    ECO_SCHEDULE,
    MAX_TEMP,
    MIN_TEMP,
    NEST_MODE_COOL,
    NEST_MODE_HEAT,
    NEST_MODE_RANGE,
    OFFLINE_AFTER,
    SAVE_DELAY,
    SIGNAL_DEVICE_UPDATE,
    SIGNAL_NEW_DEVICE,
    STORAGE_VERSION,
    WATCHDOG_INTERVAL,
    WEATHER_CACHE_SECONDS,
    WEATHER_URL,
)
from .entity import device_info, registry_device
from .nest_schedule import device_clock_offsets, setpoint_in_effect
from .protocol import BucketStore, DeviceRecord, NestServer, Push
from .protocol.util import parse_json_field
from .schedule_sync import ScheduleSync

_LOGGER = logging.getLogger(__name__)

SERVER_VERSION = "nest_local-1"
# Records created by requests that never led to a state upload are dropped.
PRUNE_AFTER = 24 * 3600
# Auto-Schedule is switched off again at most this often if the thermostat
# keeps turning it back on.
LEARNING_OFF_RETRY = 3600.0

# Who changed the setpoint (shared bucket ``touched_by``). The thermostat does
# not fill this in itself; it only shows a hold ("until ...") correctly when
# the server tells it that the dial was turned.
TOUCHED_BY_DIAL = 2
TOUCHED_BY_REMOTE = 3
# A setpoint this close to the schedule's temperature is a schedule transition.
SETPOINT_TOLERANCE = 0.3

# Room names for the thermostat's ``where_id`` (used for the device name).
WHERE_NAMES: dict[str, str] = {
    "00000000-0000-0000-0000-000100000000": "Entryway",
    "00000000-0000-0000-0000-000100000001": "Basement",
    "00000000-0000-0000-0000-000100000002": "Hallway",
    "00000000-0000-0000-0000-000100000003": "Den",
    "00000000-0000-0000-0000-000100000004": "Attic",
    "00000000-0000-0000-0000-000100000005": "Master Bedroom",
    "00000000-0000-0000-0000-000100000006": "Downstairs",
    "00000000-0000-0000-0000-000100000007": "Garage",
    "00000000-0000-0000-0000-000100000009": "Bathroom",
    "00000000-0000-0000-0000-00010000000a": "Kitchen",
    "00000000-0000-0000-0000-00010000000b": "Family Room",
    "00000000-0000-0000-0000-00010000000c": "Living Room",
    "00000000-0000-0000-0000-00010000000d": "Bedroom",
    "00000000-0000-0000-0000-00010000000e": "Office",
    "00000000-0000-0000-0000-00010000000f": "Upstairs",
    "00000000-0000-0000-0000-000100000010": "Dining Room",
    "00000000-0000-0000-0000-00010000001a": "Guest Room",
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def entry_option(entry: ConfigEntry, key: str, default: Any) -> Any:
    """Read a setting, preferring options over the original data."""
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)


class NestLocalHub:
    """Owns the protocol server and exposes thermostat state to entities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.store = BucketStore()
        self.server = NestServer(
            self.store,
            advertise_host=entry.data[CONF_HOST],
            port=int(entry.data.get(CONF_PORT, DEFAULT_PORT)),
            bind_host="0.0.0.0",
            weather_fetcher=self._fetch_weather,
            server_version=SERVER_VERSION,
        )
        self.hot_water_boost_minutes = int(
            entry_option(entry, CONF_HOT_WATER_BOOST, DEFAULT_HOT_WATER_BOOST)
        )
        self._weather_enabled = bool(entry_option(entry, CONF_WEATHER, DEFAULT_WEATHER))
        self._weather_cache: dict[str, tuple[float, Any]] = {}
        self._storage: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._online: dict[str, bool] = {}
        self._unsub_watchdog: CALLBACK_TYPE | None = None
        self._setup_done = False
        self._learning_off_sent: dict[str, float] = {}
        # Last setpoint confirmed by each thermostat, and the last one sent from HA.
        self._device_target: dict[str, float | None] = {}
        self._ha_target: dict[str, float] = {}
        # UTC offset (seconds) the thermostat last used when it edited its schedule.
        self.device_clock_offset: dict[str, int] = {}
        self._clock_checked: dict[str, int] = {}
        self.schedule_sync: ScheduleSync | None = None
        if schedule_entity := entry.options.get(CONF_SCHEDULE_ENTITY):
            self.schedule_sync = ScheduleSync(
                hass,
                self,
                schedule_entity,
                entry_option(entry, CONF_HEAT_TEMPERATURE, DEFAULT_HEAT_TEMPERATURE),
                entry_option(entry, CONF_SETBACK_TEMPERATURE, DEFAULT_SETBACK_TEMPERATURE),
            )

    # ------------------------------------------------------------- lifecycle

    async def async_start(self) -> None:
        """Load saved state and start listening for thermostats."""
        self.store.load(await self._storage.async_load())
        # Known thermostats get a grace period to reconnect after a restart.
        self.store.mark_all_seen()
        for serial in self.store.serials:
            self._online[serial] = True
        self.store.on_device_added = self._on_device_added
        self.store.on_update = self._on_update
        self.store.on_seen = self._on_seen
        await self.server.start()
        self._unsub_watchdog = async_track_time_interval(
            self.hass, self._async_watchdog, timedelta(seconds=WATCHDOG_INTERVAL)
        )
        if self.schedule_sync:
            self.schedule_sync.async_start()

    async def async_stop(self) -> None:
        """Stop the server and save state."""
        if self._unsub_watchdog:
            self._unsub_watchdog()
            self._unsub_watchdog = None
        if self.schedule_sync:
            self.schedule_sync.async_stop()
        await self.server.stop()
        await self._storage.async_save(self.store.as_dict())

    @callback
    def _schedule_save(self) -> None:
        self._storage.async_delay_save(self.store.as_dict, SAVE_DELAY)

    # ---------------------------------------------------------------- events

    @callback
    def _on_device_added(self, serial: str) -> None:
        self._online[serial] = True
        self._schedule_save()
        async_dispatcher_send(self.hass, SIGNAL_NEW_DEVICE.format(self.entry.entry_id), serial)

    @callback
    def forget_device(self, serial: str) -> None:
        """Remove a thermostat that is no longer used."""
        self.store.remove_device(serial)
        self._online.pop(serial, None)
        self._schedule_save()

    @callback
    def _on_update(self, serial: str, keys: set[str]) -> None:
        self._schedule_save()
        if keys & {"info", f"device.{serial}", f"shared.{serial}"}:
            self._update_device_registry(serial)
            if not self._setup_done and self.is_ready(serial):
                self._setup_done = True
                persistent_notification.async_dismiss(self.hass, f"{DOMAIN}_setup")
        if f"shared.{serial}" in keys:
            self._note_device_setpoint(serial)
        if f"schedule.{serial}" in keys:
            self._check_device_clock(serial)
        if self.schedule_sync and keys & {f"shared.{serial}", f"schedule.{serial}"}:
            # New state, a mode change or the thermostat's own schedule edit.
            self.schedule_sync.async_request()
        self._dispatch(serial)

    @callback
    def _note_device_setpoint(self, serial: str) -> None:
        """Tell the thermostat that a setpoint it wrote itself came from the dial.

        The thermostat changes ``target_temperature`` for two reasons: a
        schedule transition, or someone turning the dial. Only the latter is a
        hold, and the thermostat shows it as one only once the server says so.
        """
        record = self.store.device(serial)
        bucket = record.buckets.get(f"shared.{serial}") if record else None
        target = _number(bucket.value.get("target_temperature")) if bucket else None
        last = self._device_target.get(serial)
        self._device_target[serial] = target
        if last is None or target is None or target == last:
            return
        if self._ha_target.get(serial) == target:
            return  # the thermostat applied our own change
        if self._matches_schedule(serial, target):
            return  # a schedule transition: nothing to mark
        _LOGGER.debug("%s: dial turned to %.1f", serial, target)
        self._send(serial, [(f"shared.{serial}", {"touched_by": self._touched(TOUCHED_BY_DIAL)})])

    def _matches_schedule(self, serial: str, target: float) -> bool:
        now = dt_util.now()
        seconds = now.hour * 3600 + now.minute * 60 + now.second
        scheduled = setpoint_in_effect(self.schedule(serial), now.weekday(), seconds)
        return scheduled is not None and abs(scheduled - target) <= SETPOINT_TOLERANCE

    @staticmethod
    def _touched(who: int) -> dict[str, Any]:
        now = dt_util.now()
        offset = now.utcoffset()
        return {
            "touched_by": who,
            "touched_at": int(now.timestamp()),
            "touched_tzo": int(offset.total_seconds()) if offset else 0,
        }

    @callback
    def _check_device_clock(self, serial: str) -> None:
        """Compare the thermostat's UTC offset with Home Assistant's.

        Schedules run on the thermostat's clock, so a different time zone
        would shift every setpoint. The offset is visible only on setpoints
        the user edits on the thermostat itself.
        """
        since = self._clock_checked.get(serial, time.time() - 7 * 24 * 3600)
        edits = device_clock_offsets(self.schedule(serial), since)
        if not edits:
            return
        touched_at, offset = edits[-1]
        self._clock_checked[serial] = touched_at
        self.device_clock_offset[serial] = offset
        moment = dt_util.utc_from_timestamp(touched_at).astimezone(dt_util.get_default_time_zone())
        ours = moment.utcoffset()
        ours_seconds = int(ours.total_seconds()) if ours else 0
        if offset != ours_seconds:
            _LOGGER.warning(
                "Thermostat %s keeps time at UTC%+d h while Home Assistant (%s) is at UTC%+d h; "
                "its schedule runs on its own clock",
                serial,
                offset // 3600,
                self.hass.config.time_zone,
                ours_seconds // 3600,
            )

    @callback
    def _on_seen(self, serial: str) -> None:
        if not self._online.get(serial):
            _LOGGER.info("Nest thermostat %s is back online", serial)
            self._online[serial] = True
            self._dispatch(serial)

    @callback
    def _dispatch(self, serial: str) -> None:
        entry_id = self.entry.entry_id
        async_dispatcher_send(self.hass, SIGNAL_DEVICE_UPDATE.format(entry_id, serial))
        # Lets platforms add entities for capabilities that just appeared.
        async_dispatcher_send(self.hass, SIGNAL_DEVICE_UPDATE.format(entry_id, "any"), serial)

    @callback
    def _update_device_registry(self, serial: str) -> None:
        """Keep firmware version and model in the device registry current."""
        registry = dr.async_get(self.hass)
        device = registry_device(self.hass, self.entry.entry_id, serial)
        if device is None:
            return
        info = device_info(self, serial)
        changes = {
            key: info[key]  # type: ignore[literal-required]
            for key in ("sw_version", "hw_version", "model_id")
            if key in info and getattr(device, key) != info[key]  # type: ignore[literal-required]
        }
        if changes:
            registry.async_update_device(device.id, **changes)

    async def _async_watchdog(self, _now: Any = None) -> None:
        """Mark silent thermostats offline and drop stale queued changes."""
        self.store.expire_stale()
        for serial in self.store.prune_unready(PRUNE_AFTER):
            _LOGGER.debug("Forgetting %s: it never uploaded any state", serial)
            self._online.pop(serial, None)
            self._schedule_save()
        for serial in self.store.serials:
            online = self._compute_online(serial)
            if online != self._online.get(serial):
                self._online[serial] = online
                if not online:
                    _LOGGER.warning(
                        "Nest thermostat %s has not connected for %d minutes",
                        serial,
                        OFFLINE_AFTER // 60,
                    )
                self._dispatch(serial)
        if self.schedule_sync:
            # Catches helper edits that did not touch its state.
            self.schedule_sync.async_request()

    def _compute_online(self, serial: str) -> bool:
        record = self.store.device(serial)
        if record is None:
            return False
        if self.server.subscriptions.count(serial) > 0:
            return True
        return time.time() - record.last_seen < OFFLINE_AFTER

    # --------------------------------------------------------------- reading

    def is_ready(self, serial: str) -> bool:
        """True once the thermostat has uploaded its mode (entities can be made)."""
        record = self.store.device(serial)
        return record is not None and record.is_ready

    def is_online(self, serial: str) -> bool:
        """Return True if the thermostat is currently connected."""
        return self._online.get(serial, False)

    def record(self, serial: str) -> DeviceRecord | None:
        """Return the protocol record for a thermostat."""
        return self.store.device(serial)

    def shared(self, serial: str) -> dict[str, Any]:
        """Effective ``shared`` bucket (setpoints, mode, HVAC state)."""
        record = self.store.device(serial)
        return record.bucket_value("shared") if record else {}

    def device(self, serial: str) -> dict[str, Any]:
        """Effective ``device`` bucket (sensors, settings, capabilities)."""
        record = self.store.device(serial)
        return record.bucket_value("device") if record else {}

    def structure(self, serial: str) -> dict[str, Any]:
        """Effective structure bucket (eco control)."""
        record = self.store.device(serial)
        return record.structure_value() if record else {}

    def hvac_partner(self, serial: str) -> dict[str, Any] | None:
        """Heat Link / boiler data, if the thermostat reports any."""
        record = self.store.device(serial)
        return record.first_bucket_value("hvac_partner") if record else None

    def field(self, serial: str, name: str, default: Any = None) -> Any:
        """Read a field from the shared bucket, falling back to device."""
        shared = self.shared(serial)
        if name in shared:
            return shared[name]
        return self.device(serial).get(name, default)

    def eco_mode(self, serial: str) -> str | None:
        """``schedule``, ``manual-eco`` or ``auto-eco``.

        A change requested from Home Assistant that is still on its way wins,
        so the preset follows the user's choice straight away.
        """
        record = self.store.device(serial)
        structure_key = record.structure_key if record else None
        if structure_key and self.store.has_pending(serial, structure_key, "manual_eco_all"):
            return ECO_MANUAL if self.structure(serial).get("manual_eco_all") else ECO_SCHEDULE
        device = self.device(serial)
        for key in ("eco", "eco_mode"):
            raw = device.get(key)
            parsed = parse_json_field(raw)
            if parsed and isinstance(parsed.get("mode"), str):
                return parsed["mode"]
            if isinstance(raw, str) and raw in (ECO_SCHEDULE, ECO_MANUAL, ECO_AUTO):
                return raw
        structure = self.structure(serial)
        if "manual_eco_all" in structure:
            return ECO_MANUAL if structure["manual_eco_all"] else ECO_SCHEDULE
        return None

    def device_name(self, serial: str) -> str:
        """Human friendly name for the device registry."""
        shared = self.shared(serial)
        for key in ("label", "name"):
            if isinstance(shared.get(key), str) and shared[key].strip():
                return shared[key].strip()
        where = WHERE_NAMES.get(str(self.device(serial).get("where_id", "")))
        if where:
            return f"Nest {where}"
        return "Nest Thermostat"

    # -------------------------------------------------------------- commands

    def _send(self, serial: str, changes: list[tuple[str, dict[str, Any]]]) -> None:
        if not self.is_online(serial):
            _LOGGER.info(
                "Nest thermostat %s is offline; the change will be sent when it reconnects",
                serial,
            )
        pushes: list[Push] = [
            self.store.server_update(serial, key, fields) for key, fields in changes
        ]
        delivered = self.server.push(serial, pushes)
        _LOGGER.debug(
            "%s: queued %s (%d open connections)",
            serial,
            [key for key, _ in changes],
            delivered,
        )

    def _require(self, serial: str, condition: bool, translation_key: str) -> None:
        if not condition:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key=translation_key,
                translation_placeholders={"serial": serial},
            )

    def capabilities(self, serial: str) -> tuple[bool, bool]:
        """Return (can_heat, can_cool). Unknown cooling counts as absent."""
        can_heat = self.field(serial, "can_heat")
        can_cool = self.field(serial, "can_cool")
        return (can_heat is not False, can_cool is True)

    async def async_set_temperature(
        self,
        serial: str,
        *,
        temperature: float | None = None,
        low: float | None = None,
        high: float | None = None,
    ) -> None:
        """Change the setpoint (single, or low/high in heat-cool mode)."""
        fields: dict[str, Any] = {}
        for name, value in (
            ("target_temperature", temperature),
            ("target_temperature_low", low),
            ("target_temperature_high", high),
        ):
            if value is not None:
                fields[name] = round(min(max(float(value), MIN_TEMP), MAX_TEMP), 2)
        if not fields:
            return
        if "target_temperature" in fields:
            self._ha_target[serial] = fields["target_temperature"]
        # Wakes the display so the new setpoint is shown.
        fields["target_change_pending"] = True
        fields["touched_by"] = self._touched(TOUCHED_BY_REMOTE)
        self._send(serial, [(f"shared.{serial}", fields)])

    async def async_set_mode(self, serial: str, nest_mode: str) -> None:
        """Change the HVAC mode (target_temperature_type)."""
        can_heat, can_cool = self.capabilities(serial)
        if nest_mode == NEST_MODE_HEAT:
            self._require(serial, can_heat, "cannot_heat")
        elif nest_mode == NEST_MODE_COOL:
            self._require(serial, can_cool, "cannot_cool")
        elif nest_mode == NEST_MODE_RANGE:
            self._require(serial, can_heat and can_cool, "cannot_heat_cool")
        self._send(serial, [(f"shared.{serial}", {"target_temperature_type": nest_mode})])

    async def async_set_eco(self, serial: str, enabled: bool) -> None:
        """Enter or leave eco mode."""
        now_s = int(time.time())
        structure_key = self.store.structure_key_for(serial)
        if enabled:
            # Withdraw the parts of an earlier "leave eco" that have not
            # reached the thermostat yet; they would cancel this request.
            self.store.cancel_pending(serial, structure_key, ["away"])
            self.store.cancel_pending(serial, f"device.{serial}", ["eco"])
            self._send(
                serial,
                [(structure_key, {"manual_eco_all": True, "manual_eco_timestamp": now_s})],
            )
            return
        # Leaving eco: the structure fields are checked against the device
        # clock; the device-bucket eco mode is applied unconditionally.
        self._send(
            serial,
            [
                (
                    structure_key,
                    {"manual_eco_all": False, "manual_eco_timestamp": now_s, "away": False},
                ),
                (
                    f"device.{serial}",
                    {
                        "eco": {
                            "mode": ECO_SCHEDULE,
                            "touched_by": 3,
                            "mode_update_timestamp": now_s,
                        }
                    },
                ),
            ],
        )

    async def async_set_fan(self, serial: str, on: bool) -> None:
        """Run the fan timer or stop it."""
        self._require(serial, bool(self.device(serial).get("has_fan")), "no_fan")
        now_s = int(time.time())
        if on:
            duration = self.device(serial).get("fan_timer_duration")
            if not isinstance(duration, int) or duration < 900:
                duration = 3600
            fields = {"fan_timer_duration": duration, "fan_timer_timeout": now_s + duration}
        else:
            fields = {"fan_timer_timeout": 0}
        self._send(serial, [(f"device.{serial}", fields)])

    async def async_set_hot_water_boost(self, serial: str, minutes: int) -> None:
        """Start (minutes > 0) or cancel a hot water boost."""
        self._require(
            serial,
            bool(self.device(serial).get("has_hot_water_control")),
            "no_hot_water",
        )
        end = int(time.time()) + minutes * 60 if minutes > 0 else 0
        self._send(serial, [(f"device.{serial}", {"hot_water_boost_time_to_end": end})])

    @callback
    def push_schedule(self, serial: str, schedule: dict[str, Any]) -> None:
        """Replace the thermostat's weekly schedule (always the whole week)."""
        self._send(serial, [(f"schedule.{serial}", schedule)])
        self.ensure_learning_off(serial)

    @callback
    def ensure_learning_off(self, serial: str) -> None:
        """Turn Auto-Schedule off: it would rewrite the schedule after dial turns."""
        if self.device(serial).get("learning_mode") is not True:
            return
        now = time.monotonic()
        last = self._learning_off_sent.get(serial)
        if last is not None and now - last < LEARNING_OFF_RETRY:
            return
        self._learning_off_sent[serial] = now
        self._send(serial, [(f"device.{serial}", {"learning_mode": False})])

    def schedule(self, serial: str) -> dict[str, Any]:
        """Effective ``schedule`` bucket (the thermostat's weekly schedule)."""
        record = self.store.device(serial)
        return record.bucket_value("schedule") if record else {}

    async def async_set_hot_water_mode(self, serial: str, mode: str) -> None:
        """Set the hot water mode (``schedule`` or ``off``)."""
        self._require(
            serial,
            bool(self.device(serial).get("has_hot_water_control")),
            "no_hot_water",
        )
        self._send(serial, [(f"device.{serial}", {"hot_water_mode": mode})])

    # --------------------------------------------------------------- weather

    async def _fetch_weather(self, query_string: str) -> Any:
        """Proxy the thermostat's weather request to Nest's weather service."""
        if not self._weather_enabled:
            return None
        cached = self._weather_cache.get(query_string)
        if cached and time.monotonic() - cached[0] < WEATHER_CACHE_SECONDS:
            return cached[1]
        # weather.nest.com uses Nest's private certificate authority.
        session = async_get_clientsession(self.hass, verify_ssl=False)
        url = f"{WEATHER_URL}?{query_string}" if query_string else WEATHER_URL
        try:
            async with asyncio.timeout(15):
                async with session.get(url) as response:
                    response.raise_for_status()
                    data = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.debug("Weather request failed: %s", err)
            return cached[1] if cached else None
        if len(self._weather_cache) > 20:
            self._weather_cache.clear()
        self._weather_cache[query_string] = (time.monotonic(), data)
        return data
