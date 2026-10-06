"""The integration ships its own icon (Home Assistant 2026.3 and newer)."""

from __future__ import annotations

from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest

BRAND = Path(__file__).parent.parent / "custom_components" / "nest_local" / "brand"


def test_icon_files() -> None:
    from PIL import Image  # noqa: PLC0415

    for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
        with Image.open(BRAND / name) as image:
            assert image.format == "PNG"
            assert image.size == (size, size)
            assert image.mode == "RGBA"
            assert image.getpixel((0, 0))[3] == 0  # transparent corners


async def test_icon_is_served(hass: HomeAssistant, hass_client, socket_enabled: None) -> None:
    pytest.importorskip("homeassistant.components.brands")
    assert await async_setup_component(hass, "brands", {})
    client = await hass_client()
    for name in ("icon.png", "icon@2x.png", "logo.png"):
        response = await client.get(f"/api/brands/integration/nest_local/{name}")
        assert response.status == 200, name
        served = await response.read()
        expected = "icon@2x.png" if name == "icon@2x.png" else "icon.png"
        assert served == (BRAND / expected).read_bytes(), name
