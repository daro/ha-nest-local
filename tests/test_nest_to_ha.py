"""End-to-end tests for tools/nest-to-ha.sh.

The thermostat's local API is a model of the NoLongerEvil firmware CGI
(firmware/builder/deps/settings in the NoLongerEvil repository); the Home
Assistant side is the real protocol server. Everything runs on 127.0.0.1 and
the script finds the thermostat's ports through NEST_* variables.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
import contextlib
import os
from pathlib import Path
import re
import shutil

import aiohttp
from aiohttp import web
import pytest

from custom_components.nest_local.protocol import NestServer

from .conftest import free_port
from .fake_nest import SERIAL, FakeNest

SCRIPT = Path(__file__).parent.parent / "tools" / "nest-to-ha.sh"
NLE_URL = "https://backdoor.nolongerevil.com/entry"
API_KEY = "k3y/With+Slash="

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("curl") is None,
    reason="the script needs bash and curl",
)

RunScript = Callable[..., Awaitable[tuple[int, str]]]


def body_field(body: str, name: str) -> str:
    """Extract a field the way the firmware's sed does (last match wins)."""
    match = re.match(rf'.*"{name}":"([^"]*)"', body, re.DOTALL)
    return match.group(1) if match else ""


def valid_ip_port(server: str) -> bool:
    match = re.fullmatch(r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3}):(\d{1,5})", server)
    if not match:
        return False
    *octets, port = (int(part) for part in match.groups())
    return all(o <= 255 for o in octets) and 1 <= port <= 65535


class FirmwareApi:
    """/cgi-bin/api/settings of the NoLongerEvil firmware (busybox httpd CGI)."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.url = NLE_URL
        self.setup_window = False
        self.setup_used = False
        self.strict_validation = True
        self.escape_slashes = False
        self.bodies: list[str] = []
        self.nests: list[FakeNest] = []
        self.port = 0
        self._restarts: list[asyncio.Task[None]] = []
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/cgi-bin/api/settings", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self.port = free_port()
        await web.TCPSite(self._runner, "127.0.0.1", self.port).start()

    async def stop(self) -> None:
        await asyncio.gather(*self._restarts, return_exceptions=True)
        if self._runner:
            await self._runner.cleanup()

    def _reply(self, text: str, status: int = 200) -> web.Response:
        if self.escape_slashes:
            text = text.replace("/", "\\/")
        return web.Response(text=text, status=status, content_type="application/json")

    async def _handle(self, request: web.Request) -> web.Response:
        if request.method == "GET":
            return self._reply(f'{{"cloudregisterurl":"{self.url}"}}')
        body = (await request.text()).replace(" ", "")
        self.bodies.append(body)
        if body_field(body, "initialize") == SERIAL:
            return self._reply(f'{{"api_key":"{API_KEY}"}}')
        if (
            body_field(body, "setup") == "true"
            and "backdoor.nolongerevil.com" in self.url
            and self.setup_window
            and not self.setup_used
        ):
            self.setup_used = True
            return self._reply(f'{{"api_key":"{API_KEY}","device_name":"{SERIAL}"}}')
        if body_field(body, "api_key") != API_KEY:
            return self._reply('{"status":"Invalid or Missing API Key."}', 401)
        endpoint = body_field(body, "endpoint")
        if not endpoint:
            return self._reply("", 400)
        if self.strict_validation and not valid_ip_port(endpoint.split("://", 1)[-1]):
            return self._reply('{"status":"Invalid IP Address/Port."}', 400)
        self.url = f"{endpoint}/entry"
        self._restarts.append(asyncio.create_task(self._restart_nestlabs()))
        return self._reply(
            f'{{"device_name":"{SERIAL}","status":"new","cloudregisterurl":"{self.url}"}}'
        )

    async def _restart_nestlabs(self) -> None:
        """The firmware restarts its client, which connects to the new server."""
        await asyncio.sleep(0.2)
        if not self.url.startswith("http://127.0.0.1:"):
            return
        nest = FakeNest(self.session, self.url.removesuffix("/entry"))
        await nest.boot()
        self.nests.append(nest)


@pytest.fixture
async def firmware(
    socket_enabled: None, session: aiohttp.ClientSession
) -> AsyncGenerator[FirmwareApi]:
    api = FirmwareApi(session)
    await api.start()
    yield api
    await api.stop()


@pytest.fixture
def run_script(tmp_path: Path, server: NestServer, firmware: FirmwareApi) -> RunScript:
    closed_port = free_port()

    async def run(
        *args: str, stdin: str = "", env: dict[str, str] | None = None
    ) -> tuple[int, str]:
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "LC_ALL": "C.UTF-8",
            "NEST_HA_IP": "127.0.0.1",
            "NEST_HA_PORT": str(server.port),
            "NEST_API_PORT": str(firmware.port),
            "NEST_SSH_PORT": str(closed_port),
            "NEST_POLL_INTERVAL": "0.1",
            "NEST_POLL_TRIES": "100",
            **(env or {}),
        }
        process = await asyncio.create_subprocess_exec(
            "bash",
            str(SCRIPT),
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=environment,
        )
        output, _ = await asyncio.wait_for(process.communicate(stdin.encode()), 60)
        assert process.returncode is not None
        return process.returncode, output.decode()

    return run


def ha_url(server: NestServer) -> str:
    return f"http://127.0.0.1:{server.port}/entry"


async def test_switch_with_serial(
    run_script: RunScript, firmware: FirmwareApi, server: NestServer, tmp_path: Path
) -> None:
    code, out = await run_script("-y", "-s", SERIAL.lower(), "127.0.0.1")
    assert code == 0, out
    assert firmware.url == ha_url(server)
    assert f"Termostat {SERIAL} połączył się z Home Assistantem" in out
    assert server.store.device(SERIAL) is not None
    backup = (tmp_path / f"nest-backup-{SERIAL}.txt").read_text()
    assert f"serial={SERIAL}\nip=127.0.0.1\ncloudregisterurl={NLE_URL}\n" in backup


async def test_switch_uses_first_boot_window(
    run_script: RunScript, firmware: FirmwareApi, server: NestServer
) -> None:
    firmware.setup_window = True
    code, out = await run_script("-y", "127.0.0.1")
    assert code == 0, out
    assert firmware.setup_used
    assert firmware.url == ha_url(server)
    assert f"Termostat {SERIAL} połączył się" in out


async def test_switch_asks_for_serial(
    run_script: RunScript, firmware: FirmwareApi, server: NestServer
) -> None:
    stdin = f"\nWRONGSERIAL1\n{SERIAL.lower()}\nt\n"  # wake, two serials, confirm
    code, out = await run_script("127.0.0.1", stdin=stdin)
    assert code == 0, out
    assert 'nie przyjął numeru "WRONGSERIAL1"' in out
    assert firmware.url == ha_url(server)


async def test_declining_changes_nothing(
    run_script: RunScript, firmware: FirmwareApi, tmp_path: Path
) -> None:
    code, out = await run_script("-s", SERIAL, "127.0.0.1", stdin="\nn\n")
    assert code == 1
    assert "Przerwane, niczego nie zmieniłem." in out
    assert firmware.url == NLE_URL
    assert not list(tmp_path.glob("nest-backup-*"))


async def test_check_only_reads(
    run_script: RunScript, firmware: FirmwareApi, tmp_path: Path
) -> None:
    firmware.escape_slashes = True
    code, out = await run_script("--check", "-y", "127.0.0.1")
    assert code == 0, out
    assert f"cloudregisterurl = {NLE_URL}" in out
    assert "Termostat da się przepiąć przez lokalne API." in out
    assert firmware.bodies == []
    assert not list(tmp_path.glob("nest-backup-*"))


@pytest.mark.parametrize("hostname_accepted", [True, False])
async def test_rerun_and_restore(
    run_script: RunScript,
    firmware: FirmwareApi,
    server: NestServer,
    tmp_path: Path,
    hostname_accepted: bool,
) -> None:
    code, out = await run_script("-y", "-s", SERIAL, "127.0.0.1")
    assert code == 0, out
    backup = tmp_path / f"nest-backup-{SERIAL}.txt"
    saved = backup.read_text()

    # Running again finds the thermostat already switched and keeps the backup.
    code, out = await run_script("-y", "127.0.0.1")
    assert code == 0, out
    assert f"Termostat już wskazuje na {ha_url(server)}" in out
    assert backup.read_text() == saved

    # The firmware validates "IP:port"; whether a host name gets through is
    # not certain, so both outcomes must be handled.
    firmware.strict_validation = not hostname_accepted
    code, out = await run_script("--restore", "-y")
    if hostname_accepted:
        assert code == 0, out
        assert firmware.url == NLE_URL
        assert f"Termostat ustawiony na {NLE_URL}" in out
    else:
        assert code == 1
        assert "Invalid IP Address/Port." in out
        assert "client.config.before-ha" in out
        assert firmware.url == ha_url(server)


async def test_home_assistant_not_listening(run_script: RunScript, firmware: FirmwareApi) -> None:
    code, out = await run_script(
        "-y", "-s", SERIAL, "127.0.0.1", env={"NEST_HA_PORT": str(free_port())}
    )
    assert code == 1
    assert "Integracja Nest Local nie odpowiada" in out
    assert "jest zamknięty" in out
    assert firmware.bodies == []


async def test_firmware_without_api_or_ssh(run_script: RunScript) -> None:
    code, out = await run_script("-y", "127.0.0.1", env={"NEST_API_PORT": str(free_port())})
    assert code == 1
    assert "brak lokalnego API NLE" in out
    assert "starsza wersja" in out
    assert "zakładkę NLE Server" in out


# ----------------------------------------------------------------- over SSH

CLIENT_CONFIG = f"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <a key="cloudregisterurl" value="{NLE_URL}"/>
  <a key="logupload" value="1"/>
</config>
"""

# Stands in for ssh: runs the remote command on a fake thermostat file system.
FAKE_SSH = """#!/bin/sh
printf '%s\\n' "$*" >"$FAKE_NEST_ROOT/ssh-args"
for last; do :; done
sed -e "s|/tmp/client.config.new|$FAKE_NEST_ROOT/tmp/client.config.new|g" \\
    -e "s|/etc/nestlabs|$FAKE_NEST_ROOT/etc/nestlabs|g" \\
    -e "s|sleep 3|sleep 0|" |
  PATH="$FAKE_NEST_ROOT/bin:$PATH" sh -c "$last"
"""


def write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def nest_root(tmp_path: Path) -> Path:
    root = tmp_path / "nest"
    for directory in ("etc/nestlabs", "tmp", "bin", "client-bin"):
        (root / directory).mkdir(parents=True)
    write_executable(root / "bin" / "hostname", f"#!/bin/sh\necho {SERIAL}\n")
    write_executable(root / "bin" / "reboot", '#!/bin/sh\ntouch "$FAKE_NEST_ROOT/rebooted"\n')
    write_executable(root / "client-bin" / "ssh", FAKE_SSH)
    return root


@pytest.fixture
async def ssh_port(socket_enabled: None) -> AsyncGenerator[int]:
    """Something listening where the thermostat's SSH server would be."""

    async def close(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    server = await asyncio.start_server(close, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()


def ssh_env(nest_root: Path, ssh_port: int) -> dict[str, str]:
    return {
        "PATH": f"{nest_root / 'client-bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "FAKE_NEST_ROOT": str(nest_root),
        "NEST_API_PORT": str(free_port()),  # firmware from before the local API
        "NEST_SSH_PORT": str(ssh_port),
    }


async def test_switch_over_ssh(
    run_script: RunScript,
    server: NestServer,
    session: aiohttp.ClientSession,
    nest_root: Path,
    ssh_port: int,
) -> None:
    config = nest_root / "etc" / "nestlabs" / "client.config"
    config.write_text(CLIENT_CONFIG)

    async def thermostat_reboots() -> None:
        while not (nest_root / "rebooted").exists():
            await asyncio.sleep(0.05)
        await FakeNest(session, server.origin).boot()

    reboot = asyncio.create_task(thermostat_reboots())
    try:
        code, out = await run_script("-y", "127.0.0.1", env=ssh_env(nest_root, ssh_port))
        await asyncio.wait_for(reboot, 5)
    finally:
        reboot.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reboot
    assert code == 0, out
    assert config.read_text() == CLIENT_CONFIG.replace(NLE_URL, ha_url(server))
    assert (config.parent / "client.config.before-ha").read_text() == CLIENT_CONFIG
    assert not (nest_root / "tmp" / "client.config.new").exists()
    assert f"-p {ssh_port}" in (nest_root / "ssh-args").read_text()
    assert "root@127.0.0.1" in (nest_root / "ssh-args").read_text()
    backup = (nest_root.parent / f"nest-backup-{SERIAL}.txt").read_text()
    assert f"cloudregisterurl={NLE_URL}\n" in backup
    assert f"Termostat {SERIAL} połączył się" in out


async def test_ssh_change_that_does_not_apply(
    run_script: RunScript, nest_root: Path, ssh_port: int, tmp_path: Path
) -> None:
    config = nest_root / "etc" / "nestlabs" / "client.config"
    unexpected = '<config>\n  <a key="logupload" value="1"/>\n</config>\n'
    config.write_text(unexpected)
    code, out = await run_script("-y", "127.0.0.1", env=ssh_env(nest_root, ssh_port))
    assert code == 1
    assert "Zmiana przez SSH nie powiodła się." in out
    assert config.read_text() == unexpected
    await asyncio.sleep(0.2)
    assert not (nest_root / "rebooted").exists()
    assert not list(tmp_path.glob("nest-backup-*"))
