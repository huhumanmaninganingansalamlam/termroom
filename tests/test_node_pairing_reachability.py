from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from termroom.app import create_app
from termroom.config import Settings


@pytest.mark.parametrize(
    ("base_url", "expected_hint"),
    [
        ("http://127.0.0.1:8765", "only points back to this computer"),
        ("http://termroom.example.test", "requires HTTPS"),
    ],
)
def test_node_pairing_ui_blocks_unreachable_or_insecure_browser_urls(
    tmp_path: Path,
    base_url: str,
    expected_hint: str,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        host="0.0.0.0",
        state_dir=tmp_path / "state",
        login_password="test-password",
    )
    app = create_app(settings)

    with TestClient(app, base_url=base_url) as client:
        assert client.post("/login", data={"password": "test-password"}).status_code == 200
        page = client.get("/computers/node/pair")

    assert page.status_code == 200
    assert expected_hint in page.text
    assert 'aria-disabled="true"' in page.text
    assert 'role="status"' in page.text


def test_node_pairing_https_page_exposes_a_runnable_core_url(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        host="0.0.0.0",
        state_dir=tmp_path / "state",
        login_password="test-password",
    )
    app = create_app(settings)

    with TestClient(app, base_url="https://termroom.example.test") as client:
        assert client.post("/login", data={"password": "test-password"}).status_code == 200
        page = client.get("/computers/node/pair")
        assert 'aria-disabled="true"' not in page.text
        created = client.post(
            "/computers/node/pair",
            data={"_csrf": settings.csrf_token},
        )

    assert created.status_code == 201
    assert "termroom node pair --core https://termroom.example.test" in created.text
    assert re.search(r'name="code" value="[A-Z2-7-]+"', created.text)
