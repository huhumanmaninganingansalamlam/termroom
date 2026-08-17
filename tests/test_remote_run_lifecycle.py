from __future__ import annotations

import asyncio
import threading
import uuid
from pathlib import Path

import pytest

from termroom.app import create_app
from termroom.config import Settings
from termroom.remote_runs import RemoteRunError, _CancellableSink
from termroom.ssh_backend import SSHBackendError, SSHCommandStatusUnknown


def _app_with_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    computer = app.state.store.create_computer(
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
    monkeypatch.setattr(
        app.state.ssh,
        "preflight_remote_run_target",
        lambda _computer, **_kwargs: {
            "run_base": "/home/runner/.cache/termroom/runs",
            "tools": {"bash": "/bin/bash", "tmux": "/usr/bin/tmux"},
            "available_bytes": 1024 * 1024,
            "warnings": [],
        },
    )
    return app, computer


def _store_preparing_run(
    app: object,
    computer: dict[str, object],
    *,
    source_kind: str,
    phase: str,
) -> str:
    run_id = str(uuid.uuid4())
    app.state.store.create_remote_run(  # type: ignore[attr-defined]
        {
            "id": run_id,
            "source_kind": source_kind,
            "archive_format": "zip" if source_kind == "archive" else None,
            "source_workspace_id": None,
            "source_path": "." if source_kind == "workspace" else None,
            "source_label": "source",
            "source_url": "https://example.test/source.git" if source_kind == "git" else None,
            "source_options_json": "{}",
            "source_revision": None,
            "source_size": None,
            "target_computer_id": str(computer["id"]),
            "command": "printf done",
            "run_base": "/home/runner/.cache/termroom/runs",
            "workspace_id": None,
            "state": "preparing",
            "phase": phase,
            "created_at": "2026-08-09T00:00:00+00:00",
        }
    )
    return run_id


@pytest.mark.asyncio
async def test_interrupted_archive_upload_keeps_same_run_retryable_with_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    run_id = str(uuid.uuid4())
    run, created = await app.state.remote_runs.create(
        {
            "id": run_id,
            "source_kind": "archive",
            "archive_name": "source.zip",
            "target_computer_id": str(computer["id"]),
            "command": "python main.py",
        }
    )
    assert created is True
    assert run["expires_at"] is not None

    async def interrupted_upload():  # type: ignore[no-untyped-def]
        yield b"partial"
        raise OSError("connection dropped")

    with pytest.raises(OSError, match="connection dropped"):
        await app.state.remote_runs.upload_archive(
            run_id,
            "source.zip",
            interrupted_upload(),
        )

    stored = app.state.store.get_remote_run(run_id)
    assert stored is not None
    assert stored["state"] == "preparing"
    assert stored["phase"] == "waiting_upload"
    assert stored["expires_at"] is not None
    assert not (app.state.remote_runs.spool_root / f"{run_id}.part").exists()


@pytest.mark.asyncio
async def test_abandoned_archive_upload_is_removed_without_contacting_ssh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    run_id = str(uuid.uuid4())
    await app.state.remote_runs.create(
        {
            "id": run_id,
            "source_kind": "archive",
            "archive_name": "source.zip",
            "target_computer_id": str(computer["id"]),
            "command": "python main.py",
        }
    )
    assert app.state.store.transition_remote_run(
        run_id,
        expected_states={"preparing"},
        expected_phase="waiting_upload",
        state="preparing",
        phase="waiting_upload",
        expires_at="2000-01-01T00:00:00+00:00",
    )

    monkeypatch.setattr(
        app.state.ssh,
        "delete_remote_run_root",
        lambda *_args, **_kwargs: pytest.fail("abandoned upload contacted SSH"),
    )

    assert app.state.remote_runs.cleanup_expired() == 1
    assert app.state.store.get_remote_run(run_id) is None


@pytest.mark.asyncio
async def test_waiting_archive_upload_can_be_cancelled_without_remote_run_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    run_id = str(uuid.uuid4())
    await app.state.remote_runs.create(
        {
            "id": run_id,
            "source_kind": "archive",
            "archive_name": "source.zip",
            "target_computer_id": str(computer["id"]),
            "command": "python main.py",
        }
    )
    monkeypatch.setattr(
        app.state.ssh,
        "interrupt_remote_run",
        lambda *_args, **_kwargs: pytest.fail("waiting upload contacted SSH"),
    )

    result = app.state.remote_runs.stop(run_id)

    assert result["stopped"] is True
    stored = app.state.store.get_remote_run(run_id)
    assert stored is not None
    assert stored["state"] == "stopped"
    assert stored["expires_at"] is not None


@pytest.mark.asyncio
async def test_stop_before_remote_layout_waits_for_live_preparation_to_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    manager = app.state.remote_runs
    monkeypatch.setattr(
        manager,
        "_prepare_sync",
        lambda _run_id, cancel: manager._check_cancel(cancel),
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
    run_id = str(uuid.uuid4())
    await manager.create(
        {
            "id": run_id,
            "source_kind": "git",
            "source_url": "https://example.test/source.git",
            "target_computer_id": str(computer["id"]),
            "command": "printf done",
        }
    )
    handle = manager._preparations[run_id]

    result = manager.stop(run_id)

    assert result["cancellation_pending"] is True
    pending = app.state.store.get_remote_run(run_id)
    assert pending is not None
    assert pending["state"] == "preparing"
    await handle.task
    stored = app.state.store.get_remote_run(run_id)
    assert stored is not None
    assert stored["state"] == "stopped"
    assert stored["error_code"] == "cancelled"


def test_cancellable_sink_checks_between_transfer_chunks() -> None:
    cancel = threading.Event()

    class Sink:
        def write_file(self, _path: str, chunks: object, **_kwargs: object) -> None:
            iterator = iter(chunks)  # type: ignore[arg-type]
            assert next(iterator) == b"first"
            cancel.set()
            next(iterator)

    sink = _CancellableSink(Sink(), cancel)

    with pytest.raises(RemoteRunError) as raised:
        sink.write_file("large.bin", iter((b"first", b"second")), expected_size=11)

    assert raised.value.code == "cancelled"


@pytest.mark.asyncio
async def test_startup_marks_git_preparation_failed_when_layout_has_no_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    manager = app.state.remote_runs
    run_id = _store_preparing_run(app, computer, source_kind="git", phase="cloning")
    monkeypatch.setattr(
        manager,
        "poll",
        lambda *_args, **_kwargs: {
            "state": "preparing",
            "connection": "online",
            "phase": None,
            "tmux_exists": False,
            "record_errors": [],
        },
    )

    await manager._reconcile_startup()

    stored = app.state.store.get_remote_run(run_id)
    assert stored is not None
    assert stored["state"] == "failed"
    assert stored["error_code"] == "core_restarted"
    assert stored["expires_at"] is not None


@pytest.mark.asyncio
async def test_startup_preserves_unknown_start_when_online_poll_has_no_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    manager = app.state.remote_runs
    run_id = _store_preparing_run(
        app,
        computer,
        source_kind="workspace",
        phase="starting",
    )
    assert app.state.store.transition_remote_run(
        run_id,
        expected_states={"preparing"},
        state="preparing",
        phase="starting",
        error_code="start_status_unknown",
        error_detail="start acknowledgement was lost",
    )
    monkeypatch.setattr(
        manager,
        "poll",
        lambda *_args, **_kwargs: {
            "state": "preparing",
            "connection": "online",
            "phase": None,
            "tmux_exists": False,
            "run_pane_exists": False,
            "tmux_running": False,
            "record_errors": [],
        },
    )

    await manager._reconcile_startup()

    stored = app.state.store.get_remote_run(run_id)
    assert stored is not None
    assert stored["state"] == "preparing"
    assert stored["phase"] == "starting"
    assert stored["error_code"] == "start_status_unknown"
    assert stored["error_detail"] == "start acknowledgement was lost"
    assert stored["ended_at"] is None
    assert stored["expires_at"] is None


@pytest.mark.asyncio
async def test_startup_recovers_core_crash_after_dispatch_before_unknown_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    original = app.state.remote_runs
    run_id = _store_preparing_run(
        app,
        computer,
        source_kind="workspace",
        phase="copying",
    )
    dispatch_rows: list[dict[str, object]] = []

    class SimulatedCoreCrash(BaseException):
        pass

    def dispatch_then_crash(
        _target: object,
        _run_base: str,
        value: str,
    ) -> None:
        row = app.state.store.get_remote_run(value)
        assert row is not None
        dispatch_rows.append(row)
        raise SimulatedCoreCrash

    monkeypatch.setattr(app.state.ssh, "start_remote_run", dispatch_then_crash)
    run = original.get(run_id)

    with pytest.raises(SimulatedCoreCrash):
        original._start_materialized_run(
            run,
            run["target"],
            {},
            threading.Event(),
        )

    assert len(dispatch_rows) == 1
    assert dispatch_rows[0]["phase"] == "starting"
    assert dispatch_rows[0]["error_code"] == "start_status_unknown"
    crash_row = app.state.store.get_remote_run(run_id)
    assert crash_row is not None
    assert crash_row["state"] == "preparing"
    assert crash_row["phase"] == "starting"
    assert crash_row["error_code"] == "start_status_unknown"
    assert crash_row["ended_at"] is None
    assert crash_row["expires_at"] is None

    restarted = type(original)(
        original.store,
        original.workspaces,
        original.ssh,
        state_dir=original.spool_root.parent,
        max_archive_bytes=original.max_archive_bytes,
    )
    statuses = iter(
        (
            {
                "state": "preparing",
                "phase": None,
                "tmux_exists": False,
                "run_pane_exists": False,
                "tmux_running": False,
                "record_errors": [],
                "log": {
                    "chunk_b64": "",
                    "start_offset": 0,
                    "next_offset": 0,
                    "eof": True,
                },
            },
            {
                "state": "running",
                "phase": None,
                "started_at": "2026-08-09T01:00:01Z",
                "tmux_exists": True,
                "run_pane_exists": True,
                "tmux_running": True,
                "record_errors": [],
                "log": {
                    "chunk_b64": "",
                    "start_offset": 0,
                    "next_offset": 0,
                    "eof": False,
                },
            },
        )
    )
    monkeypatch.setattr(
        app.state.ssh,
        "poll_remote_run",
        lambda *_args, **_kwargs: next(statuses),
    )
    monkeypatch.setattr(restarted, "_ensure_workspace_bridge", lambda _run: None)

    await restarted._reconcile_startup()

    uncertain = app.state.store.get_remote_run(run_id)
    assert uncertain is not None
    assert uncertain["state"] == "preparing"
    assert uncertain["phase"] == "starting"
    assert uncertain["error_code"] == "start_status_unknown"
    assert uncertain["ended_at"] is None
    assert uncertain["expires_at"] is None

    recovered = restarted.poll(run_id)

    stored = app.state.store.get_remote_run(run_id)
    assert recovered["state"] == "running"
    assert stored is not None
    assert stored["state"] == "running"
    assert stored["started_at"] == "2026-08-09T01:00:01Z"
    assert stored["error_code"] is None
    assert stored["error_detail"] is None


def test_successful_workspace_start_marks_unknown_before_dispatch_then_clears_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    manager = app.state.remote_runs
    run_id = _store_preparing_run(
        app,
        computer,
        source_kind="workspace",
        phase="copying",
    )
    dispatch_rows: list[dict[str, object]] = []

    def observe_dispatch_marker(
        _target: object,
        _run_base: str,
        value: str,
    ) -> None:
        row = app.state.store.get_remote_run(value)
        assert row is not None
        dispatch_rows.append(row)

    monkeypatch.setattr(app.state.ssh, "start_remote_run", observe_dispatch_marker)
    run = manager.get(run_id)

    manager._start_materialized_run(
        run,
        run["target"],
        {},
        threading.Event(),
    )

    assert len(dispatch_rows) == 1
    assert dispatch_rows[0]["state"] == "preparing"
    assert dispatch_rows[0]["phase"] == "starting"
    assert dispatch_rows[0]["error_code"] == "start_status_unknown"
    stored = app.state.store.get_remote_run(run_id)
    assert stored is not None
    assert stored["state"] == "preparing"
    assert stored["phase"] == "starting"
    assert stored["error_code"] is None
    assert stored["error_detail"] is None
    assert stored["ended_at"] is None
    assert stored["expires_at"] is None


@pytest.mark.asyncio
async def test_startup_distinguishes_missing_layout_from_masked_offline_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    manager = app.state.remote_runs
    run_id = _store_preparing_run(app, computer, source_kind="git", phase="cloning")
    monkeypatch.setattr(
        manager,
        "poll",
        lambda *_args, **_kwargs: {"state": "preparing", "connection": "offline"},
    )
    monkeypatch.setattr(app.state.ssh, "remote_run_layout_exists", lambda *_args: False)

    await manager._reconcile_startup()

    stored = app.state.store.get_remote_run(run_id)
    assert stored is not None
    assert stored["state"] == "failed"
    assert stored["error_code"] == "core_restarted"


@pytest.mark.asyncio
async def test_start_ack_loss_reconciles_running_instead_of_recording_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    manager = app.state.remote_runs
    run_id = _store_preparing_run(
        app,
        computer,
        source_kind="workspace",
        phase="copying",
    )
    started: list[str] = []
    reconciled: list[str] = []

    def accepted_then_ack_lost(
        _target: object,
        _run_base: str,
        value: str,
    ) -> None:
        started.append(value)
        raise SSHCommandStatusUnknown("start acknowledgement was lost")

    def reconcile_running(
        _target: object,
        _run_base: str,
        value: str,
    ) -> dict[str, object]:
        reconciled.append(value)
        return {
            "state": "running",
            "phase": None,
            "started_at": "2026-08-09T00:00:05Z",
            "tmux_exists": True,
            "tmux_running": True,
            "record_errors": [],
        }

    def start_materialized(value: str, cancel: threading.Event) -> None:
        run = manager.get(value)
        manager._start_materialized_run(run, run["target"], {}, cancel)

    monkeypatch.setattr(manager, "_prepare_sync", start_materialized)
    monkeypatch.setattr(app.state.ssh, "start_remote_run", accepted_then_ack_lost)
    monkeypatch.setattr(app.state.ssh, "reconcile_remote_run", reconcile_running)
    monkeypatch.setattr(manager, "_ensure_workspace_bridge", lambda _run: None)

    await manager._prepare(run_id, threading.Event())

    stored = app.state.store.get_remote_run(run_id)
    assert started == [run_id]
    assert reconciled == [run_id]
    assert stored is not None
    assert stored["state"] == "running"
    assert stored["phase"] is None
    assert stored["started_at"] == "2026-08-09T00:00:05Z"
    assert stored["error_code"] is None
    assert stored["expires_at"] is None


@pytest.mark.asyncio
async def test_git_start_ack_loss_reconciles_live_clone_instead_of_recording_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    manager = app.state.remote_runs
    run_id = _store_preparing_run(
        app,
        computer,
        source_kind="git",
        phase="cloning",
    )
    started: list[str] = []
    reconciled: list[str] = []
    run_base = "/home/runner/.cache/termroom/runs"

    monkeypatch.setattr(
        app.state.ssh,
        "preflight_remote_run_target",
        lambda _target, **_kwargs: {
            "run_base": run_base,
            "tools": {
                "bash": "/bin/bash",
                "git": "/usr/bin/git",
                "tmux": "/usr/bin/tmux",
            },
            "available_bytes": 1024 * 1024,
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        app.state.ssh,
        "create_remote_run_layout",
        lambda *_args, **_kwargs: {
            "metadata": f"{run_base}/{run_id}/.termroom",
            "work_staging": f"{run_base}/{run_id}/work.incoming",
        },
    )
    monkeypatch.setattr(
        app.state.ssh,
        "write_remote_run_json",
        lambda *_args, **_kwargs: None,
    )

    def accepted_then_ack_lost(
        _target: object,
        _run_base: str,
        value: str,
        _invocation: object,
    ) -> None:
        started.append(value)
        raise SSHCommandStatusUnknown("git start acknowledgement was lost")

    def reconcile_live_clone(
        _target: object,
        _run_base: str,
        value: str,
    ) -> dict[str, object]:
        reconciled.append(value)
        return {
            "state": "preparing",
            "phase": "cloning",
            "started_at": "2026-08-09T00:00:05Z",
            "tmux_exists": True,
            "tmux_running": True,
            "record_errors": [],
        }

    monkeypatch.setattr(app.state.ssh, "start_remote_git_run", accepted_then_ack_lost)
    monkeypatch.setattr(app.state.ssh, "reconcile_remote_run", reconcile_live_clone)

    await manager._prepare(run_id, threading.Event())

    stored = app.state.store.get_remote_run(run_id)
    assert started == [run_id]
    assert reconciled == [run_id]
    assert stored is not None
    assert stored["state"] == "preparing"
    assert stored["phase"] == "cloning"
    assert stored["error_code"] is None
    assert stored["ended_at"] is None
    assert stored["expires_at"] is None


@pytest.mark.asyncio
async def test_start_ack_loss_while_offline_stays_starting_and_recovers_on_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    manager = app.state.remote_runs
    run_id = _store_preparing_run(
        app,
        computer,
        source_kind="workspace",
        phase="copying",
    )

    def start_materialized(value: str, cancel: threading.Event) -> None:
        run = manager.get(value)
        manager._start_materialized_run(run, run["target"], {}, cancel)

    monkeypatch.setattr(manager, "_prepare_sync", start_materialized)
    monkeypatch.setattr(
        app.state.ssh,
        "start_remote_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SSHCommandStatusUnknown("start acknowledgement was lost")
        ),
    )
    monkeypatch.setattr(
        app.state.ssh,
        "reconcile_remote_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SSHBackendError("server is offline")
        ),
    )

    await manager._prepare(run_id, threading.Event())

    uncertain = app.state.store.get_remote_run(run_id)
    assert uncertain is not None
    assert uncertain["state"] == "preparing"
    assert uncertain["phase"] == "starting"
    assert uncertain["error_code"] == "start_status_unknown"
    assert uncertain["ended_at"] is None
    assert uncertain["expires_at"] is None

    monkeypatch.setattr(
        app.state.ssh,
        "poll_remote_run",
        lambda *_args, **_kwargs: {
            "state": "running",
            "phase": None,
            "started_at": "2026-08-09T00:00:08Z",
            "tmux_exists": True,
            "tmux_running": True,
            "record_errors": [],
            "log": {
                "chunk_b64": "",
                "start_offset": 0,
                "next_offset": 0,
                "eof": False,
            },
        },
    )
    monkeypatch.setattr(manager, "_ensure_workspace_bridge", lambda _run: None)

    result = manager.poll(run_id)

    recovered = app.state.store.get_remote_run(run_id)
    assert result["connection"] == "online"
    assert result["state"] == "running"
    assert recovered is not None
    assert recovered["state"] == "running"
    assert recovered["started_at"] == "2026-08-09T00:00:08Z"
    assert recovered["error_code"] is None
    assert recovered["error_detail"] is None


@pytest.mark.asyncio
async def test_unknown_start_without_tmux_or_records_stays_preparing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    manager = app.state.remote_runs
    run_id = _store_preparing_run(
        app,
        computer,
        source_kind="workspace",
        phase="copying",
    )
    reconciled: list[str] = []

    def start_materialized(value: str, cancel: threading.Event) -> None:
        run = manager.get(value)
        manager._start_materialized_run(run, run["target"], {}, cancel)

    def reconcile_missing_start(
        _target: object,
        _run_base: str,
        value: str,
    ) -> dict[str, object]:
        reconciled.append(value)
        return {
            "state": "preparing",
            "phase": None,
            "tmux_exists": False,
            "tmux_running": False,
            "record_errors": [],
        }

    monkeypatch.setattr(manager, "_prepare_sync", start_materialized)
    monkeypatch.setattr(
        app.state.ssh,
        "start_remote_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SSHCommandStatusUnknown("remote start completion status is unknown")
        ),
    )
    monkeypatch.setattr(
        app.state.ssh,
        "reconcile_remote_run",
        reconcile_missing_start,
    )

    await manager._prepare(run_id, threading.Event())

    uncertain = app.state.store.get_remote_run(run_id)
    assert reconciled == [run_id]
    assert uncertain is not None
    assert uncertain["state"] == "preparing"
    assert uncertain["phase"] == "starting"
    assert uncertain["error_code"] == "start_status_unknown"
    assert uncertain["ended_at"] is None
    assert uncertain["expires_at"] is None

    monkeypatch.setattr(
        app.state.ssh,
        "poll_remote_run",
        lambda *_args, **_kwargs: {
            "state": "preparing",
            "phase": None,
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

    result = manager.poll(run_id)

    stored = app.state.store.get_remote_run(run_id)
    assert result["connection"] == "online"
    assert result["state"] == "preparing"
    assert stored is not None
    assert stored["state"] == "preparing"
    assert stored["phase"] == "starting"
    assert stored["error_code"] == "start_status_unknown"
    assert stored["ended_at"] is None
    assert stored["expires_at"] is None


@pytest.mark.asyncio
async def test_definitive_start_rejection_records_failure_without_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    manager = app.state.remote_runs
    run_id = _store_preparing_run(
        app,
        computer,
        source_kind="workspace",
        phase="copying",
    )

    def start_materialized(value: str, cancel: threading.Event) -> None:
        run = manager.get(value)
        manager._start_materialized_run(run, run["target"], {}, cancel)

    monkeypatch.setattr(manager, "_prepare_sync", start_materialized)
    monkeypatch.setattr(
        app.state.ssh,
        "start_remote_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SSHBackendError("tmux is not installed on the remote computer")
        ),
    )
    monkeypatch.setattr(
        app.state.ssh,
        "reconcile_remote_run",
        lambda *_args, **_kwargs: pytest.fail("definitive rejection was reconciled"),
    )

    await manager._prepare(run_id, threading.Event())

    stored = app.state.store.get_remote_run(run_id)
    assert stored is not None
    assert stored["state"] == "failed"
    assert stored["phase"] is None
    assert stored["error_code"] == "prepare_failed"
    assert stored["ended_at"] is not None
    assert stored["expires_at"] is not None


def test_unknown_start_with_local_stop_request_stays_preparing_without_remote_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    manager = app.state.remote_runs
    run_id = _store_preparing_run(
        app,
        computer,
        source_kind="workspace",
        phase="starting",
    )
    assert app.state.store.transition_remote_run(
        run_id,
        expected_states={"preparing"},
        state="preparing",
        phase="starting",
        stop_requested_at="2026-08-09T00:00:07Z",
        error_code="start_status_unknown",
        error_detail="start acknowledgement was lost",
    )
    monkeypatch.setattr(
        app.state.ssh,
        "poll_remote_run",
        lambda *_args, **_kwargs: {
            "state": "preparing",
            "phase": None,
            "tmux_exists": False,
            "run_pane_exists": False,
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
    result = manager.poll(run_id)

    stored = app.state.store.get_remote_run(run_id)
    assert result["connection"] == "online"
    assert result["state"] == "preparing"
    assert stored is not None
    assert stored["state"] == "preparing"
    assert stored["phase"] == "starting"
    assert stored["stop_requested_at"] == "2026-08-09T00:00:07Z"
    assert stored["error_code"] == "start_status_unknown"
    assert stored["ended_at"] is None
    assert stored["expires_at"] is None


def test_unknown_start_with_unverified_tmux_session_stays_preparing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    manager = app.state.remote_runs
    run_id = _store_preparing_run(
        app,
        computer,
        source_kind="workspace",
        phase="starting",
    )
    assert app.state.store.transition_remote_run(
        run_id,
        expected_states={"preparing"},
        state="preparing",
        phase="starting",
        error_code="start_status_unknown",
        error_detail="start acknowledgement was lost",
    )
    monkeypatch.setattr(
        app.state.ssh,
        "poll_remote_run",
        lambda *_args, **_kwargs: {
            "state": "layout_missing",
            "phase": None,
            "layout_missing": True,
            "tmux_exists": True,
            "run_pane_exists": False,
            "tmux_running": False,
            "record_errors": [],
            "log": {
                "chunk_b64": "",
                "start_offset": 0,
                "next_offset": 0,
                "eof": False,
            },
        },
    )

    result = manager.poll(run_id)

    stored = app.state.store.get_remote_run(run_id)
    assert result["connection"] == "online"
    assert result["state"] == "preparing"
    assert stored is not None
    assert stored["state"] == "preparing"
    assert stored["phase"] == "starting"
    assert stored["error_code"] == "start_status_unknown"
    assert stored["ended_at"] is None
    assert stored["expires_at"] is None


@pytest.mark.asyncio
async def test_start_ack_loss_does_not_trust_tmux_without_managed_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    manager = app.state.remote_runs
    run_id = _store_preparing_run(
        app,
        computer,
        source_kind="workspace",
        phase="copying",
    )

    def start_materialized(value: str, cancel: threading.Event) -> None:
        run = manager.get(value)
        manager._start_materialized_run(run, run["target"], {}, cancel)

    monkeypatch.setattr(manager, "_prepare_sync", start_materialized)
    monkeypatch.setattr(
        app.state.ssh,
        "start_remote_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SSHCommandStatusUnknown("start acknowledgement was lost")
        ),
    )
    monkeypatch.setattr(
        app.state.ssh,
        "reconcile_remote_run",
        lambda *_args, **_kwargs: {
            "state": "preparing",
            "phase": None,
            "started_at": None,
            "tmux_exists": True,
            "run_pane_exists": True,
            "tmux_running": True,
            "record_errors": [],
        },
    )

    await manager._prepare(run_id, threading.Event())

    stored = app.state.store.get_remote_run(run_id)
    assert stored is not None
    assert stored["state"] == "preparing"
    assert stored["phase"] == "starting"
    assert stored["error_code"] == "start_status_unknown"
    assert stored["error_detail"] == "start acknowledgement was lost"
    assert stored["ended_at"] is None
    assert stored["expires_at"] is None


def test_poll_does_not_finalize_existing_clone_without_start_uncertainty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    manager = app.state.remote_runs
    run_id = _store_preparing_run(
        app,
        computer,
        source_kind="git",
        phase="cloning",
    )
    monkeypatch.setattr(
        app.state.ssh,
        "poll_remote_run",
        lambda *_args, **_kwargs: {
            "state": "preparing",
            "phase": None,
            "tmux_exists": False,
            "run_pane_exists": False,
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

    result = manager.poll(run_id)

    stored = app.state.store.get_remote_run(run_id)
    assert result["connection"] == "online"
    assert result["state"] == "preparing"
    assert stored is not None
    assert stored["state"] == "preparing"
    assert stored["phase"] == "cloning"
    assert stored["error_code"] is None
    assert stored["error_detail"] is None
    assert stored["ended_at"] is None
    assert stored["expires_at"] is None


@pytest.mark.asyncio
async def test_background_observer_records_completion_without_browser_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    manager = app.state.remote_runs
    run_id = _store_preparing_run(
        app,
        computer,
        source_kind="workspace",
        phase="starting",
    )
    assert app.state.store.transition_remote_run(
        run_id,
        expected_states={"preparing"},
        state="running",
        phase=None,
        started_at="2026-08-09T00:00:05+00:00",
    )

    async def skip_startup_sweep() -> None:
        return None

    monkeypatch.setattr(manager, "_reconcile_startup", skip_startup_sweep)
    monkeypatch.setattr(
        app.state.ssh,
        "poll_remote_run",
        lambda *_args, **_kwargs: pytest.fail("observer read Remote Run logs"),
    )
    reconcile_calls = 0

    def completed(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal reconcile_calls
        reconcile_calls += 1
        return {
            "state": "finished",
            "started_at": "2026-08-09T00:00:05+00:00",
            "ended_at": "2026-08-09T00:00:09+00:00",
            "exit_code": 0,
            "tmux_exists": True,
            "tmux_running": False,
            "record_errors": [],
        }

    monkeypatch.setattr(app.state.ssh, "reconcile_remote_run", completed)
    monkeypatch.setattr("termroom.remote_runs.REMOTE_RUN_OBSERVER_INTERVAL", 0.01)

    await manager.startup()
    try:
        for _attempt in range(100):
            if app.state.store.list_activity_events():
                break
            await asyncio.sleep(0.01)
    finally:
        await manager.shutdown()

    stored = app.state.store.get_remote_run(run_id)
    assert stored is not None
    assert stored["state"] == "finished"
    assert reconcile_calls == 1
    events = app.state.store.list_activity_events()
    assert len(events) == 1
    assert events[0]["kind"] == "remote_run.completed"
    assert events[0]["subject_id"] == run_id


@pytest.mark.asyncio
async def test_background_observer_keeps_offline_run_active_and_backs_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, computer = _app_with_target(tmp_path, monkeypatch)
    manager = app.state.remote_runs
    run_id = _store_preparing_run(
        app,
        computer,
        source_kind="workspace",
        phase="starting",
    )
    assert app.state.store.transition_remote_run(
        run_id,
        expected_states={"preparing"},
        state="running",
        phase=None,
        started_at="2026-08-09T00:00:05+00:00",
    )

    async def skip_startup_sweep() -> None:
        return None

    monkeypatch.setattr(manager, "_reconcile_startup", skip_startup_sweep)
    reconcile_calls = 0

    def offline(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal reconcile_calls
        reconcile_calls += 1
        raise SSHBackendError("offline")

    monkeypatch.setattr(app.state.ssh, "reconcile_remote_run", offline)
    monkeypatch.setattr("termroom.remote_runs.REMOTE_RUN_OBSERVER_INTERVAL", 0.01)
    monkeypatch.setattr("termroom.remote_runs.REMOTE_RUN_OBSERVER_MAX_BACKOFF", 0.04)

    await manager.startup()
    try:
        deadline = asyncio.get_running_loop().time() + 2.0
        while reconcile_calls < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert reconcile_calls >= 2
        calls_after_initial_retry = reconcile_calls
        await asyncio.sleep(0.075)
    finally:
        await manager.shutdown()

    stored = app.state.store.get_remote_run(run_id)
    assert stored is not None
    assert stored["state"] == "running"
    assert stored["ended_at"] is None
    assert app.state.store.list_activity_events() == []
    assert calls_after_initial_retry <= reconcile_calls <= calls_after_initial_retry + 3
