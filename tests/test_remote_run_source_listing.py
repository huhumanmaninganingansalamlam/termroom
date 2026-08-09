from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from termroom.app import create_app
from termroom.config import Settings


@pytest.mark.asyncio
async def test_remote_run_source_lists_every_persistent_workspace(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
        default_locale="ko",
    )
    app = create_app(settings)
    app.state.store.create_computer(
        name="GPU QA",
        ssh_alias="",
        host="gpu.example.test",
        port=22,
        username="runner",
        identity_file="",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    for index in range(25):
        folder = root / f"project-{index:02d}"
        folder.mkdir()
        app.state.workspaces.open_local(root, folder.name)

    assert len(app.state.workspaces.list_recent()) == 20
    assert len(app.state.workspaces.list_all()) == 25

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/login",
            data={"password": "test-token"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        page = await client.get("/remote-runs/new")

    assert page.status_code == 200
    for index in range(25):
        assert f"project-{index:02d}" in page.text
