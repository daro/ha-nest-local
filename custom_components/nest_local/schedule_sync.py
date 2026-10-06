"""Keeps the thermostat's own weekly schedule in line with a Schedule helper.

The schedule is stored on the thermostat, so it keeps working while Home
Assistant is down. Home Assistant is the source of truth: edits made on the
thermostat are replaced by the helper's schedule.
"""

from __future__ import annotations

from collections import deque
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_state_report_event,
)

from .const import MAX_TEMP, MIN_TEMP
from .nest_schedule import (
    WEEKDAYS,
    build_setpoints,
    fingerprint,
    nest_schedule_value,
    period_temperature,
    same_schedule,
)

if TYPE_CHECKING:
    from .hub import NestLocalHub

_LOGGER = logging.getLogger(__name__)

# The thermostat applies only the last of several schedule pushes within 15 s,
# so edits made in quick succession are sent as one.
REQUEST_COOLDOWN = 15.0
# A schedule the thermostat dropped or rewrote is sent again after a pause, a
# few times a day at most: it may store schedules its own way.
RESEND_AFTER = 60.0
MAX_PUSHES_PER_DAY = 6
DAY = 24 * 3600

_MODE_OF_TYPE = {"heat": "HEAT", "emergency": "HEAT", "cool": "COOL", "range": "RANGE"}


def active_schedule_mode(shared: dict[str, Any], device: dict[str, Any]) -> str | None:
    """The schedule mode the thermostat follows: HEAT, COOL or RANGE.

    A real thermostat reports it as ``current_schedule_mode`` in its device
    bucket; ``schedule_mode`` in the shared bucket is what a server may set.
    """
    for value in (shared.get("schedule_mode"), device.get("current_schedule_mode")):
        if isinstance(value, str) and value:
            return value.upper()
    return _MODE_OF_TYPE.get(str(shared.get("target_temperature_type", "")).lower())


class ScheduleSync:
    """Pushes a Schedule helper to every thermostat that follows a heating schedule."""

    def __init__(
        self,
        hass: HomeAssistant,
        hub: NestLocalHub,
        entity_id: str,
        heat_temperature: float,
        setback_temperature: float,
    ) -> None:
        self.hass = hass
        self.hub = hub
        self.entity_id = entity_id
        self.heat_temperature = float(heat_temperature)
        self.setback_temperature = float(setback_temperature)
        self.last_error: str | None = None
        # The helper's periods, read again whenever the helper is written.
        self._helper: dict[str, Any] | None = None
        # serial -> (fingerprint of the schedule sent, monotonic send times)
        self._sent: dict[str, tuple[str, deque[float]]] = {}
        self._gave_up: set[str] = set()
        self._unsubs: list[Any] = []
        self._stopped = False
        self._debouncer: Debouncer[Any] = Debouncer(
            hass,
            _LOGGER,
            cooldown=REQUEST_COOLDOWN,
            immediate=True,
            function=self.async_sync_all,
        )

    @callback
    def async_start(self) -> None:
        """Follow the helper (an edit fires a state change or a state report)."""
        self._unsubs = [
            async_track_state_change_event(self.hass, [self.entity_id], self._helper_written),
            async_track_state_report_event(self.hass, [self.entity_id], self._helper_written),
        ]
        self.async_request()

    @callback
    def async_stop(self) -> None:
        """Stop following the helper; nothing is sent after this."""
        self._stopped = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        self._debouncer.async_shutdown()

    @callback
    def _helper_written(self, _event: Event[Any]) -> None:
        self._helper = None
        self.async_request()

    @callback
    def async_request(self) -> None:
        """Check soon (triggers that come in a burst are combined)."""
        if not self._stopped:
            self._debouncer.async_schedule_call()

    def _error(self, message: str) -> None:
        if message != self.last_error:
            _LOGGER.warning("Weekly schedule: %s", message)
        self.last_error = message

    async def _async_read_helper(self) -> dict[str, Any] | None:
        if self._helper is not None:
            return self._helper
        if self.hass.states.get(self.entity_id) is None:
            # Asking for a missing entity would log a warning on every check.
            self._error(f"{self.entity_id} does not exist; choose another helper in the options")
            return None
        try:
            response = await self.hass.services.async_call(
                "schedule",
                "get_schedule",
                {"entity_id": self.entity_id},
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError as err:
            self._error(f"cannot read {self.entity_id}: {err}")
            return None
        config = (response or {}).get(self.entity_id)
        if not isinstance(config, dict):
            self._error(f"{self.entity_id} returned no schedule")
            return None
        for weekday in WEEKDAYS:
            for period in config.get(weekday) or ():
                own = period_temperature(period)
                if own is not None and not MIN_TEMP <= own <= MAX_TEMP:
                    _LOGGER.warning(
                        "Weekly schedule: %s on %s has temperature %s, outside %s-%s °C; "
                        "the nearest limit is used",
                        self.entity_id,
                        weekday,
                        own,
                        MIN_TEMP,
                        MAX_TEMP,
                    )
        self._helper = config
        return config

    async def async_sync_all(self) -> None:
        """Send the helper's schedule to thermostats that do not follow it yet."""
        if self._stopped:
            return
        serials = [serial for serial in self.hub.store.serials if self.hub.is_ready(serial)]
        if not serials:
            return
        helper = await self._async_read_helper()
        if helper is None or self._stopped:
            return
        self.last_error = None
        setpoints = build_setpoints(
            helper,
            self.heat_temperature,
            self.setback_temperature,
            (MIN_TEMP, MAX_TEMP),
        )
        wanted = nest_schedule_value(setpoints)
        for serial in serials:
            self._sync(serial, wanted)

    def _sync(self, serial: str, wanted: dict[str, Any]) -> None:
        record = self.hub.record(serial)
        if record is None:
            return
        shared = record.buckets.get(f"shared.{serial}")
        device = record.buckets.get(f"device.{serial}")
        # The thermostat ignores a schedule whose mode is not its own, so this
        # looks at the mode it reported, not at a change still on its way.
        if (
            active_schedule_mode(shared.value if shared else {}, device.value if device else {})
            != "HEAT"
        ):
            return
        # What the thermostat holds, plus our copy if it is still on its way.
        if same_schedule(wanted, record.bucket_value("schedule")):
            self.hub.ensure_learning_off(serial)
            return

        key = fingerprint(wanted)
        now = time.monotonic()
        sent_key, times = self._sent.get(serial, ("", deque()))
        if sent_key != key:
            times = deque()
        while times and now - times[0] > DAY:
            times.popleft()
        if times and now - times[-1] < RESEND_AFTER:
            return
        if len(times) >= MAX_PUSHES_PER_DAY:
            if serial not in self._gave_up:
                self._gave_up.add(serial)
                _LOGGER.warning(
                    "Thermostat %s keeps changing the weekly schedule from %s; it will "
                    "be sent again later today or when the helper changes",
                    serial,
                    self.entity_id,
                )
            return
        self._gave_up.discard(serial)
        times.append(now)
        self._sent[serial] = (key, times)
        _LOGGER.info("Sending the weekly schedule from %s to thermostat %s", self.entity_id, serial)
        self.hub.push_schedule(serial, wanted)

    def diagnostics(self) -> dict[str, Any]:
        """State for the diagnostics download."""
        return {
            "entity_id": self.entity_id,
            "heat_temperature": self.heat_temperature,
            "setback_temperature": self.setback_temperature,
            "last_error": self.last_error,
            "sent": {
                serial: {"schedule": key, "times_sent_last_day": len(times)}
                for serial, (key, times) in self._sent.items()
            },
        }
