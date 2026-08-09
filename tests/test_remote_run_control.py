from __future__ import annotations

import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from termroom.app import create_app
from termroom.config import Settings
from termroom.ssh_backend import RemoteRunLayoutError, SSHBackendError


async def _login(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/login",
        data={"password": "test-token"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _running_remote_run(tmp_path: Path):  # type: ignore[no-untyped-def]
    root = tmp_path / "root"
    root.mkdir()
    app = create_app(
        Settings.create(
            root,
            state_dir=tmp_path / "state",
            access_token="test-token",
        )
    )
    computer = app.state.store.create_computer(
        name="Offline GPU",
        ssh_alias="",
        host="gpu.example.test",
        port=22,
        username="runner",
        identity_file="",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    run_id = str(uuid.uuid4())
    app.state.store.create_remote_run(
        {
            "id": run_id,
            "source_kind": "git",
            "source_workspace_id": None,
            "source_path": None,
            "source_label": "example/project",
            "source_url": "https://example.test/project.git",
            "source_options_json": "{}",
            "source_revision": None,
            "source_size": None,
            "target_computer_id": str(computer["id"]),
            "command": "sleep 300",
            "run_base": "/srv/termroom-runs",
            "workspace_id": None,
            "state": "running",
            "phase": None,
            "created_at": "2026-08-09T00:00:00+00:00",
        }
    )
    return app, run_id


def _finish_remote_run(app: object, run_id: str) -> None:
    app.state.remote_runs._apply_remote_status(  # type: ignore[attr-defined]
        app.state.remote_runs.get(run_id),  # type: ignore[attr-defined]
        {
            "state": "finished",
            "exit_code": 0,
            "started_at": "2026-08-09T01:00:00Z",
            "ended_at": "2026-08-09T01:00:05Z",
        },
    )


def _fail_bridge_once_then_attach_workspace(
    app: object,
    monkeypatch: pytest.MonkeyPatch,
) -> list[int]:
    attempts = [0]

    def attach(run: dict[str, object]) -> dict[str, object]:
        attempts[0] += 1
        if attempts[0] == 1:
            raise SSHBackendError("temporary bridge failure")
        run_id = str(run["id"])
        return app.state.workspaces.open_remote_run(  # type: ignore[attr-defined,no-any-return]
            run,
            f"termroom-run-{run_id}",
            f"{str(run['run_base']).rstrip('/')}/{run_id}/work",
        )

    monkeypatch.setattr(app.state.remote_runs, "_ensure_workspace_bridge", attach)  # type: ignore[attr-defined]
    return attempts


def test_stop_keeps_run_nonterminal_when_ssh_interrupt_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, run_id = _running_remote_run(tmp_path)

    def fail_interrupt(*_args: object, **_kwargs: object) -> None:
        raise SSHBackendError("target is offline")

    monkeypatch.setattr(app.state.ssh, "interrupt_remote_run", fail_interrupt)

    with pytest.raises(SSHBackendError, match="offline"):
        app.state.remote_runs.stop(run_id)

    stored = app.state.store.get_remote_run(run_id)
    assert stored is not None
    assert stored["state"] == "running"
    assert stored["stop_requested_at"] is not None


def test_kill_keeps_run_nonterminal_when_ssh_session_kill_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, run_id = _running_remote_run(tmp_path)

    def fail_kill(*_args: object, **_kwargs: object) -> None:
        raise SSHBackendError("target is offline")

    monkeypatch.setattr(app.state.ssh, "kill_remote_run", fail_kill)

    with pytest.raises(SSHBackendError, match="offline"):
        app.state.remote_runs.kill(run_id)

    stored = app.state.store.get_remote_run(run_id)
    assert stored is not None
    assert stored["state"] == "running"
    assert stored["stop_requested_at"] is not None


def test_remote_terminal_expiry_starts_at_remote_end_time(tmp_path: Path) -> None:
    app, run_id = _running_remote_run(tmp_path)

    app.state.remote_runs._apply_remote_status(
        app.state.remote_runs.get(run_id),
        {
            "state": "finished",
            "exit_code": 7,
            "started_at": "2026-08-09T01:00:00Z",
            "ended_at": "2026-08-09T01:02:03Z",
        },
    )

    stored = app.state.store.get_remote_run(run_id)
    assert stored is not None
    assert stored["ended_at"] == "2026-08-09T01:02:03Z"
    assert stored["expires_at"] == "2026-08-10T01:02:03+00:00"


def test_force_stop_preserves_completion_that_won_the_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, run_id = _running_remote_run(tmp_path)
    monkeypatch.setattr(
        app.state.ssh,
        "kill_remote_run",
        lambda *_args, **_kwargs: {"killed": False, "completed": True},
    )
    monkeypatch.setattr(
        app.state.ssh,
        "reconcile_remote_run",
        lambda *_args, **_kwargs: {
            "state": "finished",
            "exit_code": 0,
            "started_at": "2026-08-09T01:00:00Z",
            "ended_at": "2026-08-09T01:00:05Z",
        },
    )

    result = app.state.remote_runs.kill(run_id)

    assert result["state"] == "finished"
    assert result["exit_code"] == 0
    assert result["error_code"] is None
    assert result["expires_at"] == "2026-08-10T01:00:05+00:00"


@pytest.mark.parametrize(
    ("manager_method", "backend_method", "backend_result"),
    (
        (
            "stop",
            "interrupt_remote_run",
            {
                "sent": False,
                "completed": False,
                "layout_missing": True,
                "tmux_exists": False,
                "tmux_running": False,
            },
        ),
        (
            "kill",
            "kill_remote_run",
            {
                "killed": False,
                "completed": False,
                "layout_missing": True,
                "tmux_exists": False,
                "tmux_running": False,
            },
        ),
    ),
)
def test_missing_layout_and_missing_managed_session_becomes_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manager_method: str,
    backend_method: str,
    backend_result: dict[str, bool],
) -> None:
    app, run_id = _running_remote_run(tmp_path)
    monkeypatch.setattr(
        app.state.ssh,
        backend_method,
        lambda *_args, **_kwargs: backend_result,
    )

    getattr(app.state.remote_runs, manager_method)(run_id)

    stored = app.state.store.get_remote_run(run_id)
    assert stored is not None
    assert stored["state"] == "lost"
    assert stored["error_code"] == "layout_missing"
    assert stored["expires_at"] is not None


def test_missing_layout_can_interrupt_the_owned_remote_tmux_pane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, run_id = _running_remote_run(tmp_path)
    monkeypatch.setattr(
        app.state.ssh,
        "interrupt_remote_run",
        lambda *_args, **_kwargs: {
            "sent": True,
            "completed": False,
            "layout_missing": True,
            "tmux_exists": True,
            "tmux_running": True,
        },
    )

    result = app.state.remote_runs.stop(run_id)

    assert result["stopped"] is False
    assert result["needs_kill"] is True
    assert app.state.store.get_remote_run(run_id)["state"] == "running"


@pytest.mark.asyncio
async def test_stop_route_exposes_force_stop_on_the_wait_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, run_id = _running_remote_run(tmp_path)
    monkeypatch.setattr(
        app.state.ssh,
        "interrupt_remote_run",
        lambda *_args, **_kwargs: {
            "sent": True,
            "completed": False,
            "layout_missing": False,
        },
    )
    monkeypatch.setattr(
        app.state.remote_runs,
        "poll",
        lambda current_run_id, **_kwargs: app.state.remote_runs.get(current_run_id),
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        await _login(client)
        response = await client.post(
            f"/remote-runs/{run_id}/stop",
            data={"_csrf": app.state.settings.csrf_token},
            follow_redirects=True,
        )

    stored = app.state.store.get_remote_run(run_id)
    assert response.status_code == 200
    assert stored is not None
    assert stored["stop_requested_at"] is not None
    assert f'action="/remote-runs/{run_id}/kill"' in response.text
    assert 'data-run-force-stop' in response.text
    assert f'data-stop-requested-at="{stored["stop_requested_at"]}"' in response.text


def test_poll_marks_missing_layout_without_session_lost_not_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, run_id = _running_remote_run(tmp_path)
    monkeypatch.setattr(
        app.state.ssh,
        "poll_remote_run",
        lambda *_args, **_kwargs: {
            "state": "layout_missing",
            "layout_missing": True,
            "tmux_exists": False,
            "tmux_running": False,
            "record_errors": [],
            "log": {
                "chunk_b64": "",
                "start_offset": 0,
                "next_offset": 0,
                "eof": True,
            },
        },
    )

    result = app.state.remote_runs.poll(run_id)

    assert result["connection"] == "online"
    assert result["state"] == "lost"
    assert result["error_code"] == "layout_missing"


def test_damaged_layout_is_not_misreported_as_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, run_id = _running_remote_run(tmp_path)
    monkeypatch.setattr(
        app.state.ssh,
        "poll_remote_run",
        lambda *_args, **_kwargs: {
            "state": "layout_error",
            "layout_error": "marker_mismatch",
            "tmux_exists": True,
            "tmux_running": True,
            "record_errors": ["marker_mismatch"],
            "log": {
                "chunk_b64": "",
                "start_offset": 0,
                "next_offset": 0,
                "eof": True,
            },
        },
    )

    result = app.state.remote_runs.poll(run_id)

    assert result["connection"] == "online"
    assert result["state"] == "running"
    assert result["layout_error"] == "marker_mismatch"


@pytest.mark.parametrize(
    ("manager_method", "backend_method"),
    (("stop", "interrupt_remote_run"), ("kill", "kill_remote_run")),
)
def test_partial_layout_cancels_a_live_preparation_without_user_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manager_method: str,
    backend_method: str,
) -> None:
    app, run_id = _running_remote_run(tmp_path)
    assert app.state.store.transition_remote_run(
        run_id,
        expected_states={"running"},
        state="preparing",
        phase="copying",
    )

    handle = SimpleNamespace(
        task=SimpleNamespace(done=lambda: False),
        cancel=threading.Event(),
    )
    app.state.remote_runs._preparations[run_id] = handle

    def incomplete(*_args: object, **_kwargs: object) -> None:
        raise RemoteRunLayoutError("Remote Run layout is incomplete")

    monkeypatch.setattr(app.state.ssh, backend_method, incomplete)

    result = getattr(app.state.remote_runs, manager_method)(run_id)

    if manager_method == "kill":
        assert result["state"] == "preparing"
    else:
        assert result["cancellation_pending"] is True
    assert app.state.remote_runs._preparations[run_id].cancel.is_set()


def test_preparing_run_without_a_live_task_can_stop_before_layout_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, run_id = _running_remote_run(tmp_path)
    assert app.state.store.transition_remote_run(
        run_id,
        expected_states={"running"},
        state="preparing",
        phase="copying",
    )
    monkeypatch.setattr(
        app.state.ssh,
        "interrupt_remote_run",
        lambda *_args, **_kwargs: {
            "sent": False,
            "completed": False,
            "layout_missing": True,
            "tmux_exists": False,
        },
    )

    result = app.state.remote_runs.stop(run_id)

    assert result["stopped"] is True
    stored = app.state.store.get_remote_run(run_id)
    assert stored is not None
    assert stored["state"] == "stopped"
    assert stored["error_code"] == "cancelled"


@pytest.mark.asyncio
async def test_terminal_run_detail_retries_workspace_bridge_after_transient_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, run_id = _running_remote_run(tmp_path)
    _finish_remote_run(app, run_id)
    attempts = _fail_bridge_once_then_attach_workspace(app, monkeypatch)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        await _login(client)
        first = await client.get(f"/remote-runs/{run_id}", follow_redirects=False)

        stored = app.state.store.get_remote_run(run_id)
        assert first.status_code == 200
        assert stored is not None
        assert stored["state"] == "finished"
        assert stored["workspace_id"] is None

        second = await client.get(f"/remote-runs/{run_id}", follow_redirects=False)

    attached = app.state.store.get_remote_run(run_id)
    assert second.status_code == 303
    assert attached is not None
    assert attached["workspace_id"] is not None
    assert second.headers["location"] == f"/w/{attached['workspace_id']}/terminal"
    assert attempts == [2]


@pytest.mark.asyncio
async def test_terminal_run_status_retries_workspace_bridge_after_transient_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, run_id = _running_remote_run(tmp_path)
    _finish_remote_run(app, run_id)
    attempts = _fail_bridge_once_then_attach_workspace(app, monkeypatch)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        await _login(client)
        first = await client.get(f"/api/remote-runs/{run_id}/status")

        stored = app.state.store.get_remote_run(run_id)
        assert first.status_code == 502
        assert stored is not None
        assert stored["state"] == "finished"
        assert stored["workspace_id"] is None

        second = await client.get(f"/api/remote-runs/{run_id}/status")

    attached = app.state.store.get_remote_run(run_id)
    assert second.status_code == 200
    assert attached is not None
    assert attached["workspace_id"] is not None
    assert second.json()["workspace_url"] == f"/w/{attached['workspace_id']}/terminal"
    assert attempts == [2]
