"""Protocol tests: a fake thermostat talks to the server over real HTTP."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time

import aiohttp
from yarl import URL

from custom_components.nest_local.protocol import BucketStore, NestServer

from .fake_nest import SERIAL, FakeNest

SHARED = f"shared.{SERIAL}"


async def test_entry_urls_have_explicit_port(nest: FakeNest, server: NestServer) -> None:
    for path in ("/entry", "/nest/entry"):
        data = await nest.entry(path)
        for key in ("transport_url", "czfe_url", "passphrase_url", "weather_url", "upload_url"):
            assert URL(data[key]).explicit_port == server.port, key
        assert data["software_update_url"] == ""
    info = server.store.device(SERIAL).info
    assert info["model"] == "Diamond-2.6"
    assert info["software_version"] == "5.9.3-5"


async def test_passphrase_expires_is_a_number(nest: FakeNest) -> None:
    first = await nest.passphrase()
    assert isinstance(first["expires"], int)
    assert first["expires"] > (time.time() + 30 * 60) * 1000
    assert re.fullmatch(r"[0-9A-Z]{7}", first["value"])
    assert (await nest.passphrase())["value"] == first["value"]


async def test_first_subscribe_completes_pairing(nest: FakeNest) -> None:
    await nest.entry()
    objects = await nest.subscribe()
    keys = {o["object_key"]: o for o in objects}
    assert keys["user.homeassistant"]["value"] == {"name": "homeassistant"}
    assert keys["structure.homeassistant"]["value"]["devices"] == [SERIAL]
    # Field order matters to the firmware's parser.
    raw = nest.raw_bodies[-1]
    for match in re.finditer(
        r"\{\"object_revision\": \d+, \"object_timestamp\": \d+, \"object_key\"", raw
    ):
        assert match
    assert raw.count('"object_revision"') == raw.count('"object_revision": ')
    first_obj = json.loads(raw)["objects"][0]
    assert list(first_obj)[:3] == ["object_revision", "object_timestamp", "object_key"]


async def test_headers_arrive_immediately_and_connection_is_held(nest: FakeNest) -> None:
    await nest.boot()
    started = time.monotonic()
    held = await nest.open_subscribe()
    assert time.monotonic() - started < 1.0
    assert held.headers["Transfer-Encoding"] == "chunked"
    assert held.headers["X-nl-suspend-time-max"] == "300"
    assert int(held.headers["X-nl-service-timestamp"]) > 0
    assert await held.still_open_after(0.5)
    await held.close()


async def test_server_change_is_pushed_on_open_connection(
    nest: FakeNest, server: NestServer
) -> None:
    await nest.boot()
    held = await nest.open_subscribe()
    assert await held.still_open_after(0.2)

    push = server.store.server_update(
        SERIAL, SHARED, {"target_temperature": 21.5, "target_change_pending": True}
    )
    assert server.push(SERIAL, [push]) == 1
    objects = await held.body(timeout=3)
    assert objects == [push.wire()]
    assert objects[0]["value"] == {"target_temperature": 21.5, "target_change_pending": True}
    assert nest.value("shared")["target_temperature"] == 21.5

    # The thermostat acknowledges the display wake and resubscribes.
    await nest.put(SHARED, {"target_change_pending": False})
    held = await nest.open_subscribe()
    assert await held.still_open_after(0.3)
    bucket = server.store.device(SERIAL).buckets[SHARED]
    assert bucket.pending == {} and bucket.inflight == {}
    assert bucket.value["target_temperature"] == 21.5
    await held.close()


async def test_rapid_changes_are_batched_on_one_connection(
    nest: FakeNest, server: NestServer
) -> None:
    await nest.boot()
    held = await nest.open_subscribe()
    for temperature in (21.0, 21.5):
        server.push(
            SERIAL,
            [server.store.server_update(SERIAL, SHARED, {"target_temperature": temperature})],
        )
        await asyncio.sleep(0.05)
    objects = await held.body(timeout=3)
    assert len(objects) == 2
    assert nest.value("shared")["target_temperature"] == 21.5


async def test_change_made_while_disconnected_is_delivered(
    nest: FakeNest, server: NestServer
) -> None:
    await nest.boot()
    push = server.store.server_update(SERIAL, SHARED, {"target_temperature_type": "off"})
    assert server.push(SERIAL, [push]) == 0
    objects = await nest.subscribe()
    assert [o["value"] for o in objects] == [{"target_temperature_type": "off"}]
    assert nest.value("shared")["target_temperature_type"] == "off"


async def test_put_after_lost_change_still_delivers_it(nest: FakeNest, server: NestServer) -> None:
    """The thermostat PUTs before it hears about our change (race)."""
    await nest.boot()
    server.store.server_update(SERIAL, SHARED, {"target_temperature": 22.0})
    # The thermostat's PUT uses its old revision -> conflict, it adopts ours.
    await nest.put(SHARED, {"hvac_heater_state": True})
    await nest.put(SHARED, {"hvac_heater_state": True})
    await nest.subscribe()
    assert nest.value("shared")["target_temperature"] == 22.0
    assert server.store.device(SERIAL).bucket_value("shared")["hvac_heater_state"] is True


async def test_put_response_has_no_value(nest: FakeNest) -> None:
    await nest.boot()
    receipts = await nest.put(f"device.{SERIAL}", {"current_humidity": 52})
    assert receipts and all(
        set(r) == {"object_revision", "object_timestamp", "object_key"} for r in receipts
    )


async def test_hold_timeout_closes_without_body(port: int, session: aiohttp.ClientSession) -> None:
    srv = NestServer(
        BucketStore(),
        advertise_host="127.0.0.1",
        port=port,
        bind_host="127.0.0.1",
        suspend_time_max=11,  # hold for 1 s
    )
    await srv.start()
    try:
        nest = FakeNest(session, srv.origin)
        await nest.boot()
        held = await nest.open_subscribe()
        assert await held.body(timeout=4) == []
        assert srv.subscriptions.count(SERIAL) == 0
    finally:
        await srv.stop()


async def test_stop_releases_held_connections(nest: FakeNest, server: NestServer) -> None:
    await nest.boot()
    held = await nest.open_subscribe()
    assert await held.still_open_after(0.2)
    started = time.monotonic()
    await server.stop()
    assert time.monotonic() - started < 3
    assert await held.body(timeout=2) == []


async def test_legacy_paths(nest: FakeNest) -> None:
    await nest.entry()
    held = await nest.open_subscribe(path="/transport")
    objects = await held.body()
    assert {o["object_key"] for o in objects} >= {"user.homeassistant"}
    async with nest.session.get(
        nest.base + f"/nest/transport/device/device.{SERIAL}", headers=nest._headers()
    ) as response:
        assert response.status == 200
        listing = (await response.json())["objects"]
        assert all("value" not in o for o in listing)
    async with nest.session.get(nest.base + "/nest/ping") as response:
        assert response.status == 200


async def test_requests_without_serial_are_rejected(
    server: NestServer, session: aiohttp.ClientSession
) -> None:
    async with session.post(
        server.origin + "/nest/transport", json={"chunked": True, "objects": []}
    ) as response:
        assert response.status == 400
    auth = "Basic " + base64.b64encode(f"d.{SERIAL}.X:y".encode()).decode()
    async with session.post(
        server.origin + "/nest/transport/put",
        data="not json",
        headers={"Authorization": auth},
    ) as response:
        assert response.status == 400


async def test_weather_proxy(port: int, session: aiohttp.ClientSession) -> None:
    calls: list[str] = []

    async def fetch(query: str) -> dict:
        calls.append(query)
        return {"now": {"current_temperature": 11.5}}

    srv = NestServer(
        BucketStore(),
        advertise_host="127.0.0.1",
        port=port,
        bind_host="127.0.0.1",
        weather_fetcher=fetch,
    )
    await srv.start()
    try:
        async with session.get(srv.origin + "/nest/weather/v1?query=M1%201AA,GB") as response:
            assert response.status == 200
            assert (await response.json())["now"]["current_temperature"] == 11.5
        assert calls == ["query=M1%201AA,GB"]  # forwarded unchanged
    finally:
        await srv.stop()


async def test_too_many_devices(port: int, session: aiohttp.ClientSession) -> None:
    srv = NestServer(
        BucketStore(max_devices=1), advertise_host="127.0.0.1", port=port, bind_host="127.0.0.1"
    )
    await srv.start()
    try:
        await FakeNest(session, srv.origin).entry()
        other = FakeNest(session, srv.origin, serial="09AA01AB12345678")
        async with session.post(
            srv.origin + "/entry", headers=other._headers(), data={}
        ) as response:
            assert response.status == 403
    finally:
        await srv.stop()


async def test_deferred_push_dropped_after_put_is_resent(
    nest: FakeNest, server: NestServer
) -> None:
    """Concurrent PUT and subscribe: the firmware may drop the push."""
    await nest.boot()
    held = await nest.open_subscribe()
    server.push(SERIAL, [server.store.server_update(SERIAL, SHARED, {"target_temperature": 22.5})])
    deferred = await held.body(timeout=3, apply=False)
    # PUT sent before the push was processed -> conflict, receipt adopted.
    await nest.put(SHARED, {"hvac_heater_state": True})
    nest.apply(deferred)  # equal revision and timestamp: discarded
    assert nest.value("shared")["target_temperature"] == 20.0
    await nest.put(SHARED, {"hvac_heater_state": True})  # retry succeeds
    await nest.subscribe()
    assert nest.value("shared")["target_temperature"] == 22.5


async def test_trailing_slash_and_status_paths(nest: FakeNest) -> None:
    await nest.entry(path="/entry/")
    for path in ("/ping/", "/pro_info/ABC", "/nest/passphrase/status", "/passphrase/"):
        async with nest.session.get(nest.base + path, headers=nest._headers()) as response:
            assert response.status == 200, path


async def test_stray_requests_do_not_create_devices(
    server: NestServer, session: aiohttp.ClientSession
) -> None:
    async with session.get(server.origin + f"/nest/transport/device/device.{SERIAL}") as resp:
        assert resp.status == 200
        assert (await resp.json())["objects"] == []
    async with session.get(server.origin + "/nest/ping?serial=GARBAGESERIAL1") as resp:
        assert resp.status == 200
    assert server.store.serials == []
