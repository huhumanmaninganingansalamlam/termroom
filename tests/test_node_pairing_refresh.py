from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from termroom.app import create_app
from termroom.config import Settings


def test_fingerprint_check_keeps_unconsumed_pairing_code(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
        login_password="test-password",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        assert client.post("/login", data={"password": "test-password"}).status_code == 200
        created = client.post(
            "/computers/node/pair", data={"_csrf": settings.csrf_token}
        )
        pairing = re.search(r'name="pairing_id" value="([a-f0-9]{32})"', created.text)
        code = re.search(r'class="node-pairing-code">([^<]+)<', created.text)
        assert pairing is not None
        assert code is not None

        checked = client.post(
            "/computers/node/pair/check",
            data={
                "_csrf": settings.csrf_token,
                "pairing_id": pairing.group(1),
                "code": code.group(1),
            },
        )

    assert checked.status_code == 200
    assert f'class="node-pairing-code">{code.group(1)}<' in checked.text
    assert f'name="pairing_id" value="{pairing.group(1)}"' in checked.text
    assert f'name="code" value="{code.group(1)}"' in checked.text


def test_fingerprint_check_rejects_a_code_from_another_pairing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
        login_password="test-password",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        client.post("/login", data={"password": "test-password"})
        first = client.post("/computers/node/pair", data={"_csrf": settings.csrf_token})
        second = client.post("/computers/node/pair", data={"_csrf": settings.csrf_token})
        pairing = re.search(r'name="pairing_id" value="([a-f0-9]{32})"', first.text)
        other_code = re.search(r'class="node-pairing-code">([^<]+)<', second.text)
        assert pairing is not None
        assert other_code is not None

        checked = client.post(
            "/computers/node/pair/check",
            data={
                "_csrf": settings.csrf_token,
                "pairing_id": pairing.group(1),
                "code": other_code.group(1),
            },
        )

    assert checked.status_code == 409
