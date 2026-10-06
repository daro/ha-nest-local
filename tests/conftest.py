"""Shared fixtures."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import socket

import aiohttp
import pytest

from custom_components.nest_local.protocol import BucketStore, NestServer

from .fake_nest import FakeNest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load custom_components/nest_local."""


def free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def port(socket_enabled: None) -> int:
    """A free port (real sockets are needed for these tests)."""
    return free_port()


@pytest.fixture
async def server(port: int) -> AsyncGenerator[NestServer]:
    """Protocol server without Home Assistant, with a short batch window."""
    srv = NestServer(
        BucketStore(),
        advertise_host="127.0.0.1",
        port=port,
        bind_host="127.0.0.1",
        batch_window=0.2,
    )
    await srv.start()
    yield srv
    await srv.stop()


@pytest.fixture
async def session() -> AsyncGenerator[aiohttp.ClientSession]:
    """HTTP client for the fake thermostat."""
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
def nest(server: NestServer, session: aiohttp.ClientSession) -> FakeNest:
    """A fake thermostat pointed at the protocol server."""
    return FakeNest(session, server.origin)
