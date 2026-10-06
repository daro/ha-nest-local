"""A small model of a Nest thermostat talking to the server over real HTTP.

It follows the sync rules of the firmware closely enough to test the server:
pushed objects are accepted only when their timestamp is newer (revision
breaks ties), write receipts update the local revision/timestamp, and the
thermostat always keeps the values it wrote itself.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from typing import Any

import aiohttp

SERIAL = "02AA01AB501203EQ"


def initial_state(serial: str = SERIAL) -> dict[str, dict[str, Any]]:
    """Typical UK gen-2 thermostat with a Heat Link and hot water control."""
    return {
        f"shared.{serial}": {
            "current_temperature": 20.43,
            "target_temperature": 20.0,
            "target_temperature_type": "heat",
            "target_change_pending": False,
            "can_heat": True,
            "can_cool": False,
            "hvac_heater_state": False,
            "hvac_ac_state": False,
            "hvac_fan_state": False,
            "auto_away": 0,
            "name": "",
        },
        f"device.{serial}": {
            "current_humidity": 47,
            "battery_level": 3.94,
            "backplate_temperature": 21.25,
            "has_fan": False,
            "has_hot_water_control": True,
            "hot_water_active": False,
            "hot_water_mode": "schedule",
            "hot_water_boost_time_to_end": 0,
            "rssi": 52,
            "local_ip": "192.168.1.50",
            "mac_address": "18b430abcdef",
            "current_version": "5.9.3-5",
            "away_temperature_low": 12.0,
            "time_to_target": 0,
            "where_id": "00000000-0000-0000-0000-00010000000c",
            "eco": {"mode": "schedule", "touched_by": 1, "mode_update_timestamp": 0},
            "learning_mode": True,
        },
        f"schedule.{serial}": own_schedule(),
    }


def own_schedule(morning: int = 7, evening: int = 22) -> dict[str, Any]:
    """The thermostat's own schedule: warm from morning to evening every day."""
    return {
        "ver": 2,
        "name": "Current Schedule",
        "schedule_mode": "HEAT",
        "days": {
            str(day): {
                "0": {
                    "type": "HEAT",
                    "time": morning * 3600,
                    "entry_type": "setpoint",
                    "temp": 20.0,
                },
                "1": {
                    "type": "HEAT",
                    "time": evening * 3600,
                    "entry_type": "setpoint",
                    "temp": 16.0,
                },
            }
            for day in range(7)
        },
    }


def parse_documents(text: str) -> list[dict[str, Any]]:
    """Split a chunked body made of several JSON documents."""
    decoder = json.JSONDecoder()
    docs: list[dict[str, Any]] = []
    index = 0
    text = text.strip()
    while index < len(text):
        doc, end = decoder.raw_decode(text, index)
        docs.append(doc)
        index = end
        while index < len(text) and text[index].isspace():
            index += 1
    return docs


class HeldSubscribe:
    """A subscribe request whose headers have arrived."""

    def __init__(self, nest: FakeNest, response: aiohttp.ClientResponse) -> None:
        self.nest = nest
        self.response = response
        self._text = asyncio.ensure_future(response.text())

    @property
    def headers(self) -> Any:
        return self.response.headers

    async def body(self, timeout: float = 5.0, *, apply: bool = True) -> list[dict[str, Any]]:
        """Wait for the server to finish the response; apply what it pushed.

        With ``apply=False`` the caller decides when the thermostat processes
        the data (the firmware defers it while a PUT is in flight).
        """
        raw = await asyncio.wait_for(asyncio.shield(self._text), timeout)
        self.response.release()
        self.nest.raw_bodies.append(raw)
        objects: list[dict[str, Any]] = []
        for doc in parse_documents(raw):
            objects.extend(doc.get("objects", []))
        if apply:
            self.nest.apply(objects)
        return objects

    async def close(self) -> None:
        """Drop the connection (like a thermostat losing Wi-Fi)."""
        self._text.cancel()
        with contextlib.suppress(asyncio.CancelledError, aiohttp.ClientError):
            await self._text
        self.response.close()

    async def still_open_after(self, seconds: float) -> bool:
        """True if the server is still holding the response after ``seconds``."""
        done, _ = await asyncio.wait({self._text}, timeout=seconds)
        return not done


class FakeNest:
    """Thermostat side of the protocol."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        serial: str = SERIAL,
        *,
        accept_pushes: bool = True,
    ) -> None:
        self.session = session
        self.base = base_url.rstrip("/")
        self.serial = serial
        self.accept_pushes = accept_pushes
        self.auth = "Basic " + base64.b64encode(f"d.{serial}.BC7C9039:apikey".encode()).decode()
        self.buckets: dict[str, dict[str, Any]] = {}
        self.raw_bodies: list[str] = []
        self.applied: list[dict[str, Any]] = []

    # ----------------------------------------------------------- local state

    def local(self, key: str) -> dict[str, Any]:
        return self.buckets.setdefault(key, {"rev": 0, "ts": 0, "value": {}})

    def value(self, kind: str) -> dict[str, Any]:
        return self.local(f"{kind}.{self.serial}")["value"]

    def apply(self, objects: list[dict[str, Any]]) -> None:
        """Accept pushed objects the way the firmware does."""
        if not self.accept_pushes:
            return
        for obj in objects:
            local = self.local(obj["object_key"])
            ts, rev = obj["object_timestamp"], obj["object_revision"]
            if ts > local["ts"] or (ts == local["ts"] and rev > local["rev"]):
                local["ts"], local["rev"] = ts, rev
                local["value"].update(obj.get("value") or {})
                self.applied.append(obj)

    # --------------------------------------------------------------- requests

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self.auth, "X-nl-protocol-version": "1"}

    async def entry(self, path: str = "/entry") -> dict[str, Any]:
        async with self.session.post(
            self.base + path,
            data={
                "reset": "FALSE",
                "mac": "18B430ABCDEF",
                "model": "Diamond-2.6",
                "request_id": "1",
                "software_version": "5.9.3-5",
                "backplate_model": "Backplate-2.1",
            },
            headers=self._headers(),
        ) as response:
            response.raise_for_status()
            return await response.json()

    async def passphrase(self) -> dict[str, Any]:
        async with self.session.get(
            self.base + "/nest/passphrase", headers=self._headers()
        ) as response:
            return await response.json()

    def subscribe_body(self, extra: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        objects = [
            {"object_key": key, "object_revision": b["rev"], "object_timestamp": b["ts"]}
            for key, b in self.buckets.items()
        ]
        return {
            "chunked": True,
            "session": f"18b430{self.serial}",
            "objects": objects + (extra or []),
        }

    async def open_subscribe(
        self,
        path: str = "/nest/transport/v7/subscribe",
        extra: list[dict[str, Any]] | None = None,
        timeout: float = 5.0,
    ) -> HeldSubscribe:
        response = await asyncio.wait_for(
            self.session.post(
                self.base + path, json=self.subscribe_body(extra), headers=self._headers()
            ),
            timeout,
        )
        return HeldSubscribe(self, response)

    async def subscribe(self, timeout: float = 5.0) -> list[dict[str, Any]]:
        """Subscribe and wait for the response to finish."""
        held = await self.open_subscribe()
        return await held.body(timeout)

    async def put(self, key: str, fields: dict[str, Any]) -> list[dict[str, Any]]:
        """Upload local changes; the thermostat keeps its own values."""
        local = self.local(key)
        revision_field = (
            "if_object_revision" if key.startswith("shared.") else "base_object_revision"
        )
        body = {
            "session": f"18b430{self.serial}",
            key: {"object_key": key, revision_field: local["rev"], **fields},
        }
        local["value"].update(fields)
        async with self.session.post(
            self.base + "/nest/transport/put", json=body, headers=self._headers()
        ) as response:
            response.raise_for_status()
            receipts = (await response.json())["objects"]
        for receipt in receipts:
            target = self.local(receipt["object_key"])
            target["rev"] = receipt["object_revision"]
            target["ts"] = receipt["object_timestamp"]
        return receipts

    async def put_many(self, buckets: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Upload several buckets in one PUT, as the firmware does."""
        body: dict[str, Any] = {"session": f"18b430{self.serial}"}
        for key, fields in buckets.items():
            local = self.local(key)
            revision_field = (
                "if_object_revision" if key.startswith("shared.") else "base_object_revision"
            )
            body[key] = {"object_key": key, revision_field: local["rev"], **fields}
            local["value"].update(fields)
        async with self.session.post(
            self.base + "/nest/transport/put", json=body, headers=self._headers()
        ) as response:
            response.raise_for_status()
            receipts = (await response.json())["objects"]
        for receipt in receipts:
            target = self.local(receipt["object_key"])
            target["rev"] = receipt["object_revision"]
            target["ts"] = receipt["object_timestamp"]
        return receipts

    async def upload_full_state(self) -> None:
        """What the thermostat does after a reboot (retries a CAS conflict)."""
        state = initial_state(self.serial)
        receipts = await self.put_many(state)
        rejected = {r["object_key"] for r in receipts if r["object_timestamp"] == 0}
        if rejected:
            await self.put_many({key: state[key] for key in rejected})

    async def boot(self) -> list[dict[str, Any]]:
        """Entry, pairing code, first subscribe, full upload, settle."""
        await self.entry()
        await self.passphrase()
        pushed = await self.subscribe()
        await self.upload_full_state()
        return pushed
