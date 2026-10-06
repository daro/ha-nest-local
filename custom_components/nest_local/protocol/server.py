"""HTTP server that stands in for the Nest cloud.

The thermostat (running the NoLongerEvil firmware) is pointed at this server
through ``cloudregisterurl``. It then:

1. ``POST /entry``            - asks where the other services live,
2. ``GET  /nest/passphrase``  - fetches a pairing code for its screen,
3. ``POST /nest/transport``   - subscribes: the response headers are sent at
   once, the body only when there is something to push (long poll),
4. ``POST /nest/transport/put`` - uploads its own state changes.

All URLs handed to the thermostat carry an explicit port; without it the
firmware cannot set up Wi-Fi wake-up and pushes would never wake it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
import logging
from typing import Any

from aiohttp import web

from .store import BucketStore, Push, TooManyDevicesError
from .subscriptions import CLOSE, Subscription, SubscriptionManager
from .util import (
    contains_temperature_fields,
    dumps,
    parse_basic_auth,
    sanitize_serial,
    serial_from_user_id,
)

_LOGGER = logging.getLogger(__name__)

SUSPEND_TIME_MAX = 300
DEFER_DEVICE_WINDOW = 15
DISABLE_DEFER_WINDOW = 60
BATCH_WINDOW = 3.0
MAX_BODY = 16 * 1024 * 1024

WeatherFetcher = Callable[[str], Awaitable[dict[str, Any] | None]]

# Metadata keys that may accompany bucket fields in a PUT.
_PUT_METADATA = frozenset({"object_key", "base_object_revision", "if_object_revision"})
# Bucket names accepted in the (older) named-field subscribe format.
_NAMED_BUCKETS = frozenset(
    {
        "device",
        "shared",
        "structure",
        "schedule",
        "custom_schedule",
        "user",
        "link",
        "where",
        "message",
        "device_alert_dialog",
        "hvac_partner",
        "topaz",
        "kryptonite",
        "occupancy",
    }
)


def parse_subscribe_body(body: dict[str, Any]) -> tuple[str, bool, list[dict[str, Any]]]:
    """Return (session, chunked, objects) from a subscribe request body."""
    session = str(body.get("session") or "")
    chunked = bool(body.get("chunked", False))
    objects = body.get("objects")
    if isinstance(objects, list):
        return session, chunked, [o for o in objects if isinstance(o, dict)]
    named = [
        value
        for key, value in body.items()
        if key in _NAMED_BUCKETS and isinstance(value, dict) and "object_key" in value
    ]
    return session, chunked, named


def parse_put_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalise a PUT body into ``{object_key, if/base revision, value}`` dicts.

    Production firmware sends buckets as top-level keys with the fields
    inline; an ``objects`` array with nested ``value`` is accepted as well.
    """
    objects = body.get("objects")
    if isinstance(objects, list):
        return [o for o in objects if isinstance(o, dict)]
    result: list[dict[str, Any]] = []
    for key, item in body.items():
        if key == "session" or not isinstance(item, dict) or "object_key" not in item:
            continue
        result.append(
            {
                "object_key": item["object_key"],
                "base_object_revision": item.get("base_object_revision"),
                "if_object_revision": item.get("if_object_revision"),
                "value": {k: v for k, v in item.items() if k not in _PUT_METADATA},
            }
        )
    return result


def serial_from_request(request: web.Request, *, strict: bool = False) -> str | None:
    """Identify the thermostat that sent a request.

    The firmware always sends HTTP Basic auth with ``d.{SERIAL}.{suffix}``;
    the ``X-nl-*`` headers are fallbacks seen on non-production builds. With
    ``strict`` only these are used, so stray requests cannot create devices.
    """
    auth = parse_basic_auth(request.headers.get("Authorization"))
    if auth:
        serial = serial_from_user_id(auth[0])
        if serial:
            return serial
    serial = serial_from_user_id(request.headers.get("X-nl-client-id"))
    if serial:
        return serial
    serial = sanitize_serial(request.headers.get("X-nl-device-id"))
    if serial or strict:
        return serial
    for candidate in (
        request.headers.get("X-NL-Device-Serial"),
        request.query.get("serial"),
        request.match_info.get("serial", "").removeprefix("device."),
    ):
        serial = sanitize_serial(candidate)
        if serial:
            return serial
    return None


class NestServer:
    """aiohttp application implementing the thermostat side of the protocol."""

    def __init__(
        self,
        store: BucketStore,
        *,
        advertise_host: str,
        port: int,
        bind_host: str | None = None,
        weather_fetcher: WeatherFetcher | None = None,
        server_version: str = "1.0.0",
        suspend_time_max: int = SUSPEND_TIME_MAX,
        defer_device_window: int = DEFER_DEVICE_WINDOW,
        disable_defer_window: int = DISABLE_DEFER_WINDOW,
        batch_window: float = BATCH_WINDOW,
    ) -> None:
        self.store = store
        self.subscriptions = SubscriptionManager()
        self.advertise_host = advertise_host
        self.port = port
        self.bind_host = bind_host
        self.weather_fetcher = weather_fetcher
        self.server_version = server_version
        self.suspend_time_max = suspend_time_max
        # The server, not the thermostat's safety timer, drives reconnects.
        self.hold_timeout = float(max(suspend_time_max - 10, 1))
        self.defer_device_window = defer_device_window
        self.disable_defer_window = disable_defer_window
        self.batch_window = batch_window
        self._runner: web.AppRunner | None = None
        self._stopping = False

    # ---------------------------------------------------------------- set-up

    @property
    def origin(self) -> str:
        """Base URL the thermostat uses to reach this server (explicit port)."""
        return f"http://{self.advertise_host}:{self.port}"

    @property
    def bound_port(self) -> int | None:
        """Actual listening port (useful when started with port 0 in tests)."""
        if self._runner is None:
            return None
        for address in self._runner.addresses:
            if isinstance(address, tuple):
                return int(address[1])
        return None

    def build_app(self) -> web.Application:
        """Create the aiohttp application with all routes."""
        app = web.Application(middlewares=[self._middleware], client_max_size=MAX_BODY)
        router = app.router

        def add(method: str, path: str, handler: Any) -> None:
            # Older firmware sometimes adds a trailing slash; a 404 makes the
            # thermostat reset its connection state.
            router.add_route(method, path, handler)
            if not path.endswith("/") and "{" not in path:
                router.add_route(method, path + "/", handler)

        for path in ("/entry", "/nest/entry"):
            add("POST", path, self._handle_entry)
            add("GET", path, self._handle_entry)
        for path in ("/passphrase", "/nest/passphrase"):
            add("GET", path, self._handle_passphrase)
            add("GET", path + "/status", self._handle_passphrase_status)
        for prefix in ("/nest/transport", "/transport", "/czfe"):
            add("POST", prefix, self._handle_subscribe)
            add("POST", f"{prefix}/subscribe", self._handle_subscribe)
            add("POST", f"{prefix}/put", self._handle_put)
            add("POST", prefix + "/{czid}/subscribe", self._handle_subscribe)
            add("POST", prefix + "/{czid}/put", self._handle_put)
        add("GET", "/nest/transport", self._handle_ping)
        add("GET", "/nest/transport/device/{serial}", self._handle_object_list)
        add("GET", "/nest/transport/{tail:.*}", self._handle_object_list)
        for path in ("/ping", "/nest/ping"):
            add("GET", path, self._handle_ping)
        for path in ("/pro_info", "/nest/pro_info"):
            add("GET", path, self._handle_pro_info)
            add("GET", path + "/{code}", self._handle_pro_info)
        for path in ("/upload", "/nest/upload"):
            add("POST", path, self._handle_upload)
        add("GET", "/nest/weather/v1", self._handle_weather)
        add("GET", "/nest/weather/{tail:.*}", self._handle_weather)
        add("GET", "/weather/{tail:.*}", self._handle_weather)
        return app

    async def start(self) -> None:
        """Start listening. Raises OSError if the port is taken."""
        self._stopping = False
        runner = web.AppRunner(
            self.build_app(),
            access_log=None,
            # The thermostat cannot answer TCP keep-alive probes while asleep.
            tcp_keepalive=False,
            keepalive_timeout=self.hold_timeout + 60,
            shutdown_timeout=2.0,
        )
        await runner.setup()
        site = web.TCPSite(runner, self.bind_host, self.port)
        try:
            await site.start()
        except OSError:
            await runner.cleanup()
            raise
        self._runner = runner
        _LOGGER.debug("Nest server listening on port %s (%s)", self.port, self.origin)

    async def stop(self) -> None:
        """Close all held connections and stop listening."""
        self._stopping = True
        self.subscriptions.close_all()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------- helpers

    def push(self, serial: str, pushes: list[Push]) -> int:
        """Send pushes to every open subscribe connection of a thermostat."""
        return self.subscriptions.notify(serial, pushes)

    def _headers(self, *, disable_defer: bool = False) -> dict[str, str]:
        headers = {
            "X-nl-service-timestamp": str(self.store.now_ms()),
            "X-nl-suspend-time-max": str(self.suspend_time_max),
            "X-nl-defer-device-window": str(self.defer_device_window),
        }
        if disable_defer:
            headers["X-nl-disable-defer-window"] = str(self.disable_defer_window)
        return headers

    def _json_response(
        self, data: Any, *, status: int = 200, protocol_headers: bool = False
    ) -> web.Response:
        return web.Response(
            text=dumps(data),
            status=status,
            content_type="application/json",
            headers=self._headers() if protocol_headers else None,
        )

    @staticmethod
    async def _json_body(request: web.Request) -> dict[str, Any] | None:
        try:
            body = await request.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    @web.middleware
    async def _middleware(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        serial = serial_from_request(request, strict=True)
        if serial:
            try:
                self.store.touch(serial, request.remote)
            except TooManyDevicesError:
                _LOGGER.warning("Ignoring %s: too many thermostats", serial)
                return self._json_response({"error": "too many devices"}, status=403)
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception:
            _LOGGER.exception("Error handling %s %s", request.method, request.path)
            return self._json_response({"error": "internal error"}, status=500)

    async def _write(self, response: web.StreamResponse, serial: str, pushes: list[Push]) -> bool:
        data = dumps({"objects": [p.wire() for p in pushes]}).encode("utf-8")
        try:
            await response.write(data)
        except (ConnectionError, RuntimeError) as err:
            _LOGGER.debug("%s: push failed, will retry on next subscribe: %s", serial, err)
            return False
        self.store.mark_delivered(serial, pushes)
        _LOGGER.debug("%s: pushed %s", serial, [p.key for p in pushes])
        return True

    @staticmethod
    async def _finish(response: web.StreamResponse) -> None:
        with suppress(ConnectionError, RuntimeError):
            await response.write_eof()

    # ------------------------------------------------------------ handlers

    async def _handle_entry(self, request: web.Request) -> web.Response:
        serial = serial_from_request(request)
        if serial and request.method == "POST":
            info: dict[str, Any] = {}
            with suppress(ValueError):
                form = await request.post()
                for key in (
                    "mac",
                    "model",
                    "software_version",
                    "backplate_model",
                    "wireless_reg_domain",
                ):
                    value = form.get(key)
                    if isinstance(value, str) and value:
                        info[key] = value
            if info:
                self.store.update_info(serial, info)
        _LOGGER.debug("Entry request from %s (%s)", serial, request.remote)
        transport = f"{self.origin}/nest/transport"
        return self._json_response(
            {
                "czfe_url": transport,
                "transport_url": transport,
                "direct_transport_url": transport,
                "passphrase_url": f"{self.origin}/nest/passphrase",
                "ping_url": transport,
                "pro_info_url": f"{self.origin}/nest/pro_info",
                "weather_url": f"{self.origin}/nest/weather/v1?query=",
                "upload_url": f"{self.origin}/nest/upload",
                "software_update_url": "",
                "server_version": self.server_version,
                "tier_name": "local",
            }
        )

    async def _handle_passphrase(self, request: web.Request) -> web.Response:
        serial = serial_from_request(request)
        if not serial:
            return self._json_response({"error": "device serial required"}, status=400)
        code, expires = self.store.get_entry_key(serial)
        # ``expires`` must be a JSON number or the thermostat ignores the code.
        return self._json_response({"value": code, "expires": expires})

    async def _handle_subscribe(self, request: web.Request) -> web.StreamResponse:
        serial = serial_from_request(request)
        if not serial:
            return self._json_response({"error": "device serial required"}, status=400)
        body = await self._json_body(request)
        if body is None:
            return self._json_response({"error": "invalid JSON"}, status=400)
        session, chunked, objects = parse_subscribe_body(body)
        pushes = self.store.handle_subscribe(serial, objects)
        _LOGGER.debug(
            "%s: subscribe with %d objects, %d to send now",
            serial,
            len(objects),
            len(pushes),
        )
        wired = [p.wire() for p in pushes]
        headers = self._headers(disable_defer=contains_temperature_fields(wired))

        if not chunked:
            # Without chunked encoding the thermostat expects a full answer
            # within about seven seconds.
            self.store.mark_delivered(serial, pushes)
            return web.Response(
                text=dumps({"objects": wired}),
                content_type="application/json",
                headers=headers,
            )

        response = web.StreamResponse(status=200, headers=headers)
        response.content_type = "application/json"
        response.enable_chunked_encoding()
        # Headers go out now: the thermostat may sleep from this point on.
        await response.prepare(request)

        if pushes or self._stopping:
            if pushes:
                await self._write(response, serial, pushes)
            await self._finish(response)
            return response

        sub = self.subscriptions.add(serial, session)
        try:
            await self._hold(response, sub)
        finally:
            self.subscriptions.remove(sub)
        await self._finish(response)
        return response

    async def _hold(self, response: web.StreamResponse, sub: Subscription) -> None:
        """Keep the connection open until there is data or it is time to close."""
        try:
            first = await asyncio.wait_for(sub.queue.get(), self.hold_timeout)
        except TimeoutError:
            return  # closing the connection makes the thermostat resubscribe
        if first is CLOSE or not await self._write(response, sub.serial, first):
            return
        # More changes may follow (e.g. repeated +/- clicks): keep sending on
        # the same connection while they arrive within the batch window.
        while True:
            try:
                following = await asyncio.wait_for(sub.queue.get(), self.batch_window)
            except TimeoutError:
                return
            if following is CLOSE or not await self._write(response, sub.serial, following):
                return

    async def _handle_put(self, request: web.Request) -> web.Response:
        serial = serial_from_request(request)
        if not serial:
            return self._json_response({"error": "device serial required"}, status=400)
        body = await self._json_body(request)
        if body is None:
            return self._json_response({"error": "invalid JSON"}, status=400)
        objects = parse_put_body(body)
        receipts = self.store.handle_put(serial, objects)
        _LOGGER.debug("%s: PUT %s", serial, [o.get("object_key") for o in objects])
        return self._json_response({"objects": receipts}, protocol_headers=True)

    async def _handle_object_list(self, request: web.Request) -> web.Response:
        serial = sanitize_serial(request.match_info.get("serial", "").removeprefix("device."))
        tail = request.match_info.get("tail", "")
        if not serial and "device/" in tail:
            serial = sanitize_serial(tail.split("device/")[-1].removeprefix("device."))
        serial = serial or serial_from_request(request)
        if not serial:
            return self._json_response({"error": "device serial required"}, status=400)
        # Unknown serials get an empty list; this never creates a device.
        return self._json_response(
            {"objects": self.store.object_list(serial)}, protocol_headers=True
        )

    async def _handle_passphrase_status(self, request: web.Request) -> web.Response:
        serial = serial_from_request(request, strict=True)
        record = self.store.device(serial) if serial else None
        if record is None:
            return self._json_response({"status": "no_key", "claimed": False})
        # Thermostats are paired automatically by this server.
        return self._json_response(
            {"status": "claimed", "claimed": True, "claimedBy": record.user_key or ""}
        )

    async def _handle_ping(self, request: web.Request) -> web.Response:
        return self._json_response({"status": "ok", "timestamp": self.store.now_ms()})

    async def _handle_pro_info(self, request: web.Request) -> web.Response:
        return self._json_response(
            {
                "id": 1,
                "pro_id": request.match_info.get("code", ""),
                "dba": "Home Assistant",
                "locality": "Self-hosted thermostat",
                "rating": 5.0,
            }
        )

    async def _handle_upload(self, request: web.Request) -> web.Response:
        # Device logs are not needed; read and discard them.
        with suppress(Exception):
            await request.read()
        return self._json_response({"status": "ok"})

    async def _handle_weather(self, request: web.Request) -> web.Response:
        if self.weather_fetcher is None:
            return self._json_response({"error": "weather disabled"}, status=404)
        data = await self.weather_fetcher(request.rel_url.raw_query_string)
        if data is None:
            return self._json_response({"error": "weather unavailable"}, status=502)
        return self._json_response(data)
