from __future__ import annotations

import re
import uuid
from pathlib import Path

import httpx
import pytest

from termroom.app import create_app
from termroom.config import Settings
from termroom.security import file_digest


async def _login(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/login",
        data={"password": "test-token"},
        follow_redirects=False,
    )
    assert response.status_code == 303


@pytest.mark.asyncio
async def test_remote_run_recovery_uses_compact_connection_state_without_layout_jump(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(root, state_dir=tmp_path / "state", access_token="test-token")
    app = create_app(settings)
    computer = app.state.store.create_computer(
        name="GPU",
        ssh_alias="",
        host="gpu.example.test",
        port=22,
        username="runner",
        identity_file="/tmp/key",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    run_id = str(uuid.uuid4())
    _run, created = app.state.store.create_remote_run(
        {
            "id": run_id,
            "source_kind": "git",
            "source_workspace_id": None,
            "source_path": None,
            "source_label": "model",
            "source_url": "https://example.test/model.git",
            "source_options_json": '{"policy":1}',
            "source_revision": None,
            "source_size": None,
            "target_computer_id": str(computer["id"]),
            "command": "python main.py",
            "run_base": "/home/runner/.cache/termroom/runs",
            "workspace_id": None,
            "state": "running",
            "phase": None,
            "created_at": "2026-08-16T00:00:00+00:00",
        }
    )
    assert created is True
    connection = "offline"

    def poll(*_args: object, **_kwargs: object) -> dict[str, object]:
        run = dict(app.state.remote_runs.get(run_id))
        run["connection"] = connection
        return run

    monkeypatch.setattr(app.state.remote_runs, "poll", poll)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        offline = await client.get(f"/remote-runs/{run_id}")
        connection = "online"
        online = await client.get(f"/remote-runs/{run_id}")

    assert offline.status_code == 200
    assert 'class="state-chip remote-run-connection-state"' in offline.text
    assert "data-run-connection>" in offline.text
    assert "Rechecking connection…" in offline.text
    assert 'class="connection-notice error"' not in offline.text
    assert "Lost connection to GPU" not in offline.text
    assert online.status_code == 200
    assert "data-run-connection hidden>" in online.text


@pytest.mark.asyncio
async def test_nonzero_file_run_is_presented_as_failed_without_changing_lifecycle_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    source = b"raise SystemExit(7)\n"
    (project / "main.py").write_bytes(source)
    settings = Settings.create(root, state_dir=tmp_path / "state", access_token="test-token")
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    _status, run = app.state.store.claim_file_run(
        {
            "id": str(uuid.uuid4()),
            "workspace_id": str(workspace["id"]),
            "idempotency_key": str(uuid.uuid4()),
            "relative_path": "main.py",
            "source_digest": file_digest(source),
            "runner_id": "python3",
            "runner_version": 1,
            "argv": ("python3", "--", "./main.py"),
        }
    )
    assert app.state.store.transition_file_run(
        str(run["id"]),
        expected_states={"preparing"},
        state="finished",
        exit_code=7,
    )

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        editor = await client.get(f"/w/{workspace['id']}/edit/main.py")
        status = await client.get(f"/api/file-runs/{run['id']}/status")
        (project / "main.py").write_text("print('fixed')\n", encoding="utf-8")
        changed_editor = await client.get(f"/w/{workspace['id']}/edit/main.py")

    assert editor.status_code == 200
    assert "Failed" in editor.text
    assert "Exit 7" in editor.text
    assert "data-file-run-stale" not in editor.text
    assert changed_editor.status_code == 200
    assert "data-file-run-stale" in changed_editor.text
    assert "Failed" in changed_editor.text
    assert "Exit 7" in changed_editor.text
    status_wrapper = re.search(
        r"<span[^>]*data-file-run-connection(?:\s|>)[^>]*>", editor.text
    )
    assert status_wrapper is not None
    assert 'role="status"' in status_wrapper.group(0)
    assert 'aria-live="polite"' in status_wrapper.group(0)
    assert 'aria-atomic="true"' in status_wrapper.group(0)
    assert " hidden" not in status_wrapper.group(0)
    connection_chip = re.search(
        r"<small[^>]*data-file-run-connection-chip[^>]*>", editor.text
    )
    assert connection_chip is not None
    assert 'aria-hidden="true"' in connection_chip.group(0)
    assert " hidden" in connection_chip.group(0)
    assert "The Remote connection is unavailable" in editor.text
    assert (
        '<span class="sr-only" data-file-run-connection-announcer></span>'
        in editor.text
    )
    assert status.json()["state"] == "finished"
    assert status.json()["display_state"] == "failed"
    assert status.json()["state_label"] == "Failed"


@pytest.mark.asyncio
async def test_nonzero_remote_run_is_presented_as_failed_on_home_and_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(root, state_dir=tmp_path / "state", access_token="test-token")
    app = create_app(settings)
    computer = app.state.store.create_computer(
        name="GPU",
        ssh_alias="",
        host="gpu.example.test",
        port=22,
        username="runner",
        identity_file="/tmp/key",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    run_id = str(uuid.uuid4())
    run, created = app.state.store.create_remote_run(
        {
            "id": run_id,
            "source_kind": "git",
            "source_workspace_id": None,
            "source_path": None,
            "source_label": "model",
            "source_url": "https://example.test/model.git",
            "source_options_json": '{"policy":1}',
            "source_revision": None,
            "source_size": None,
            "target_computer_id": str(computer["id"]),
            "command": "python main.py",
            "run_base": "/home/runner/.cache/termroom/runs",
            "workspace_id": None,
            "state": "running",
            "phase": None,
            "created_at": "2026-08-13T00:00:00+00:00",
        }
    )
    assert created is True
    assert app.state.store.transition_remote_run(
        run_id,
        expected_states={"running"},
        state="finished",
        exit_code=7,
        ended_at="2026-08-13T00:00:07+00:00",
    )
    run = app.state.store.get_remote_run(run_id)
    assert run is not None
    workspace = app.state.workspaces.open_remote_run(
        run,
        f"termroom-run-{run_id}",
        f"/home/runner/.cache/termroom/runs/{run_id}/work",
    )
    terminal = app.state.store.create_terminal(
        str(workspace["id"]),
        "run",
        "@run",
        role="remote_run",
        managed_run_id=run_id,
    )
    monkeypatch.setattr(app.state.ssh, "ensure_workspace", lambda _workspace: [terminal])

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        home = await client.get("/")
        terminal_page = await client.get(f"/w/{workspace['id']}/terminal")
        status = await client.get(f"/api/remote-runs/{run_id}/status")

    assert home.status_code == 200
    assert terminal_page.status_code == 200
    assert "Could not run" in home.text
    assert "Could not run" in terminal_page.text
    assert "Exit code 7" in terminal_page.text
    status_wrapper = re.search(
        r"<span[^>]*data-run-workspace-connection[^>]*>", terminal_page.text
    )
    assert status_wrapper is not None
    assert 'role="status"' in status_wrapper.group(0)
    assert 'aria-live="polite"' in status_wrapper.group(0)
    assert 'aria-atomic="true"' in status_wrapper.group(0)
    assert " hidden" not in status_wrapper.group(0)
    connection_chip = re.search(
        r"<span[^>]*data-run-workspace-connection-chip[^>]*>", terminal_page.text
    )
    assert connection_chip is not None
    assert 'aria-hidden="true"' in connection_chip.group(0)
    assert " hidden" in connection_chip.group(0)
    assert "Rechecking connection…" in terminal_page.text
    assert (
        '<span class="sr-only" data-run-workspace-connection-announcer></span>'
        in terminal_page.text
    )
    assert status.json()["state"] == "finished"
    assert status.json()["display_state"] == "failed"
    assert status.json()["state_label"] == "Could not run"
