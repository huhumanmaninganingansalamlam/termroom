from __future__ import annotations

import json
import struct
from pathlib import Path

import httpx
import pytest

from termroom.app import PACKAGE_ROOT, create_app
from termroom.config import Settings


def test_manifest_declares_explicit_192_and_512_png_icons() -> None:
    manifest = json.loads((PACKAGE_ROOT / "static" / "manifest.webmanifest").read_text())
    assert manifest["name"] == "Termroom"
    assert manifest["short_name"] == "Termroom"
    png_icons = {
        (icon["sizes"], icon["type"], icon["purpose"])
        for icon in manifest["icons"]
        if icon["src"].endswith(".png")
    }
    assert ("192x192", "image/png", "any") in png_icons
    assert ("512x512", "image/png", "any maskable") in png_icons


@pytest.mark.asyncio
async def test_pwa_png_icons_are_public_opaque_pngs_with_expected_dimensions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="internal-secret",
        login_password="correct-password",
    )
    transport = httpx.ASGITransport(app=create_app(settings), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for size in (192, 512):
            response = await client.get(f"/icons/termroom-{size}.png")
            assert response.status_code == 200
            assert response.headers["content-type"] == "image/png"
            assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
            width, height, bit_depth, color_type = struct.unpack(
                ">IIBB", response.content[16:26]
            )
            assert (width, height) == (size, size)
            assert bit_depth == 8
            assert color_type == 2

        missing = await client.get("/icons/termroom-256.png")
        assert missing.status_code == 404
