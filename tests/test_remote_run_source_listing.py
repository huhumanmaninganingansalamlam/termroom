from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from termroom.app import create_app
from termroom.config import Settings
from termroom.node_protocol import (
    NODE_PROTOCOL_VERSION,
    generate_pairing_code,
    generate_private_key,
    pairing_code_digest,
    public_key_fingerprint,
    public_key_text,
    secret_digest,
)


def _node_computer(app, name: str, capabilities: tuple[str, ...]):  # type: ignore[no-untyped-def]
    code = generate_pairing_code()
    app.state.store.create_node_pairing_code(
        code_hash=pairing_code_digest(code),
        expires_at="2999-01-01T00:00:00+00:00",
    )
    public_key = public_key_text(generate_private_key().public_key())
    enrollment = app.state.store.submit_node_enrollment(
        code_hash=pairing_code_digest(code),
        name=name,
        public_key=public_key,
        fingerprint=public_key_fingerprint(public_key),
        protocol_version=NODE_PROTOCOL_VERSION,
        polling_secret_hash=secret_digest(f"{name}-polling"),
    )
    assert enrollment is not None
    computer = app.state.store.approve_node_enrollment(str(enrollment["id"]))
    app.state.store.update_node_connection(
        str(computer["id"]),
        protocol_version=NODE_PROTOCOL_VERSION,
        capabilities=capabilities,
    )
    return app.state.store.get_computer(str(computer["id"]))


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


@pytest.mark.asyncio
async def test_remote_run_source_lists_only_capable_node_workspaces_and_gates_files_cta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
        default_locale="ko",
    )
    app = create_app(settings)
    capable = _node_computer(
        app,
        "Node Source",
        ("files", "remote_run_source", "terminal", "workspace"),
    )
    target_only = _node_computer(
        app,
        "Target Only",
        ("files", "remote_run", "terminal", "workspace"),
    )
    assert capable is not None
    assert target_only is not None
    capable_workspace = app.state.workspaces.open_remote(
        str(capable["id"]), "/srv/capable", "Capable Workspace"
    )
    target_only_workspace = app.state.workspaces.open_remote(
        str(target_only["id"]), "/srv/target-only", "Target-only Workspace"
    )

    async def empty_list(_workspace: object, _path: str):  # type: ignore[no-untyped-def]
        return ".", []

    monkeypatch.setattr(app.state.remote, "list_dir", empty_list)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/login",
            data={"password": "test-token"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        form = await client.get("/remote-runs/new")
        capable_files = await client.get(f"/w/{capable_workspace['id']}/files")
        target_only_files = await client.get(f"/w/{target_only_workspace['id']}/files")

    assert form.status_code == 200
    assert "Capable Workspace" in form.text
    assert "Target-only Workspace" not in form.text
    assert capable_files.status_code == 200
    assert f"source_workspace_id={capable_workspace['id']}" in capable_files.text
    assert "다른 Remote에서 실행" in capable_files.text
    assert target_only_files.status_code == 200
    assert "다른 Remote에서 실행" not in target_only_files.text
