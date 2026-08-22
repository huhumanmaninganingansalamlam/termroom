from __future__ import annotations

import asyncio
import concurrent.futures
import io
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from termroom.db import StateStore
from termroom.file_runs import (
    FILE_RUN_TERMINAL_STATES,
    FileRunConflict,
    FileRunError,
    FileRunManager,
    resolve_runner,
)
from termroom.files import FileService, RunnableFile, UnsupportedFileError
from termroom.security import PathBoundaryError
from termroom.ssh_backend import SSHBackend, SSHBackendError
from termroom.terminals import (
    TerminalError,
    TerminalManager,
    file_run_completion_grace_active,
    file_run_dead_pane_fallback,
)
from termroom.workspaces import RootManager, WorkspaceManager


def _runnable(
    path: str,
    *,
    executable: bool = False,
    shebang: bool = False,
) -> RunnableFile:
    return RunnableFile(
        relative_path=path,
        digest="a" * 64,
        executable=executable,
        has_shebang=shebang,
    )


def test_runner_registry_is_deterministic_and_uses_exact_argv() -> None:
    assert resolve_runner(_runnable("scripts/main.py")).argv == (
        "python3",
        "--",
        "./scripts/main.py",
    )
    assert resolve_runner(_runnable("main.js")).argv == ("node", "--", "./main.js")
    assert resolve_runner(_runnable("main.mjs")).argv == ("node", "--", "./main.mjs")
    assert resolve_runner(_runnable("main.cjs")).argv == ("node", "--", "./main.cjs")
    assert resolve_runner(_runnable("run.sh")).argv == (
        "bash",
        "--noprofile",
        "--norc",
        "--",
        "./run.sh",
    )
    assert resolve_runner(_runnable("run.bash")).argv[-1] == "./run.bash"
    assert resolve_runner(_runnable("MAIN.PY")) is None
    assert resolve_runner(_runnable("main.ts")) is None


def test_executable_utf8_shebang_takes_precedence_over_suffix() -> None:
    runner = resolve_runner(_runnable("tool.py", executable=True, shebang=True))
    assert runner is not None
    assert runner.id == "direct"
    assert runner.argv == ("./tool.py",)

    not_executable = resolve_runner(_runnable("tool.rb", shebang=True))
    assert not_executable is None


def _store_with_workspace(tmp_path: Path) -> tuple[StateStore, dict[str, object]]:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = WorkspaceManager(RootManager(root), store).open("project")
    return store, workspace


def _claim_payload(
    workspace_id: str,
    *,
    run_id: str | None = None,
    key: str | None = None,
    path: str = "main.py",
) -> dict[str, object]:
    return {
        "id": run_id or str(uuid.uuid4()),
        "workspace_id": workspace_id,
        "idempotency_key": key or str(uuid.uuid4()),
        "relative_path": path,
        "source_digest": "b" * 64,
        "runner_id": "python3",
        "runner_version": 1,
        "argv": ("python3", "--", f"./{path}"),
    }


def test_file_run_claim_is_payload_idempotent_and_workspace_exclusive(
    tmp_path: Path,
) -> None:
    store, workspace = _store_with_workspace(tmp_path)
    payload = _claim_payload(str(workspace["id"]))
    status, first = store.claim_file_run(payload)
    assert status == "created"

    replay_status, replay = store.claim_file_run(payload)
    assert replay_status == "idempotent"
    assert replay["id"] == first["id"]

    changed = {**payload, "relative_path": "other.py"}
    with pytest.raises(ValueError, match="different payload"):
        store.claim_file_run(changed)

    occupied_status, occupied = store.claim_file_run(
        _claim_payload(str(workspace["id"]), path="other.py")
    )
    assert occupied_status == "occupied"
    assert occupied["id"] == first["id"]


def test_concurrent_file_run_claim_creates_only_one_active_row(tmp_path: Path) -> None:
    store, workspace = _store_with_workspace(tmp_path)
    workspace_id = str(workspace["id"])
    payloads = [_claim_payload(workspace_id) for _ in range(2)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(store.claim_file_run, payloads))

    assert sorted(status for status, _run in results) == ["created", "occupied"]
    assert len(store.list_active_file_runs()) == 1


def test_runnable_read_stays_bounded_if_file_grows_after_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    script = project / "main.py"
    script.write_text("x", encoding="utf-8")
    service = FileService(max_edit_bytes=4)
    original_open = Path.open

    def growing_open(path: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if path == script and args and args[0] == "rb":
            return io.BytesIO(b"12345")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", growing_open)
    with pytest.raises(UnsupportedFileError, match="editable size limit"):
        service.inspect_runnable(project, "main.py")


def test_file_run_terminal_transition_creates_one_safe_event(tmp_path: Path) -> None:
    store, workspace = _store_with_workspace(tmp_path)
    payload = _claim_payload(str(workspace["id"]), path="private/main.py")
    _status, run = store.claim_file_run(payload)
    started = datetime.now(UTC)
    ended = started + timedelta(seconds=3)
    assert store.transition_file_run(
        str(run["id"]),
        expected_states={"preparing"},
        state="finished",
        started_at=started.isoformat(timespec="seconds"),
        ended_at=ended.isoformat(timespec="seconds"),
        exit_code=7,
        error_detail="TOKEN=secret /absolute/private/path",
    )
    assert not store.transition_file_run(
        str(run["id"]),
        expected_states={"preparing"},
        state="finished",
        ended_at=ended.isoformat(timespec="seconds"),
        exit_code=7,
    )
    events = store.list_activity_events()
    assert len(events) == 1
    assert events[0]["kind"] == "file_run.failed"
    serialized = repr(events[0])
    assert "TOKEN=secret" not in serialized
    assert "/absolute/private" not in serialized


def test_history_pruning_bounds_old_events_and_file_runs_without_losing_live_ui_state(
    tmp_path: Path,
) -> None:
    store, workspace = _store_with_workspace(tmp_path)
    workspace_id = str(workspace["id"])
    now = datetime(2026, 8, 19, 12, tzinfo=UTC)
    old = now - timedelta(days=31)
    recent = now - timedelta(days=1)

    def finished_run(path: str, ended_at: datetime) -> str:
        _status, run = store.claim_file_run(
            _claim_payload(workspace_id, path=path)
        )
        assert store.transition_file_run(
            str(run["id"]),
            expected_states={"preparing"},
            state="finished",
            started_at=(ended_at - timedelta(seconds=2)).isoformat(timespec="seconds"),
            ended_at=ended_at.isoformat(timespec="seconds"),
            exit_code=0,
        )
        return str(run["id"])

    expired_id = finished_run("expired.py", old)
    pinned_id = finished_run("pinned.py", old)
    recent_id = finished_run("recent.py", recent)
    pinned_terminal = store.create_terminal(
        workspace_id,
        "Run",
        "@file-run",
        role="file_run",
        managed_run_id=pinned_id,
    )
    assert store.set_file_run_terminal(pinned_id, str(pinned_terminal["id"])) is False
    _status, active = store.claim_file_run(
        _claim_payload(workspace_id, path="active.py")
    )

    old_device_time = (now - timedelta(days=91)).isoformat(timespec="seconds")
    with store.connect() as db:
        expired_event = db.execute(
            "SELECT id FROM events WHERE subject_id = ?", (expired_id,)
        ).fetchone()
        assert expired_event is not None
        db.execute(
            """
            INSERT INTO notification_devices(
                id, start_sequence, created_at, last_seen_at
            ) VALUES ('old-device', 0, ?, ?)
            """,
            (old_device_time, old_device_time),
        )
        db.execute(
            "INSERT INTO event_reads(event_id, device_id, read_at) VALUES (?, ?, ?)",
            (expired_event["id"], "old-device", old_device_time),
        )
        db.execute(
            """
            INSERT INTO event_notification_claims(event_id, device_id, claimed_at)
            VALUES (?, 'old-device', ?)
            """,
            (expired_event["id"], old_device_time),
        )

    removed = store.prune_history(now=now)

    assert removed["events"] == 2
    assert removed["notification_devices"] == 1
    assert removed["file_runs"] == [
        {"id": expired_id, "workspace_id": workspace_id}
    ]
    assert store.get_file_run(expired_id) is None
    assert store.get_file_run(pinned_id) is not None
    assert store.get_file_run(recent_id) is not None
    assert store.get_file_run(str(active["id"])) is not None
    assert [event["subject_id"] for event in store.list_activity_events()] == [
        recent_id
    ]
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM event_reads WHERE event_id = ?",
                (expired_event["id"],),
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM event_notification_claims WHERE event_id = ?",
                (expired_event["id"],),
            ).fetchone()[0]
            == 0
        )


def _local_manager(
    tmp_path: Path,
) -> tuple[FileRunManager, dict[str, object], Path]:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = StateStore(state_dir / "termroom.sqlite3")
    store.initialize()
    workspaces = WorkspaceManager(RootManager(root), store)
    workspace = workspaces.open("project")
    files = FileService()
    terminals = TerminalManager(store)
    ssh = SSHBackend(store, state_dir)
    manager = FileRunManager(
        store,
        workspaces,
        files,
        terminals,
        ssh,
        state_dir=state_dir,
        max_edit_bytes=1024 * 1024,
    )
    return manager, workspace, project


def test_file_run_manager_removes_metadata_for_pruned_runs(tmp_path: Path) -> None:
    manager, workspace, _project = _local_manager(tmp_path)
    workspace_id = str(workspace["id"])
    _status, run = manager.store.claim_file_run(
        _claim_payload(workspace_id, path="old.py")
    )
    old = datetime.now(UTC) - timedelta(days=31)
    assert manager.store.transition_file_run(
        str(run["id"]),
        expected_states={"preparing"},
        state="finished",
        started_at=(old - timedelta(seconds=1)).isoformat(timespec="seconds"),
        ended_at=old.isoformat(timespec="seconds"),
        exit_code=0,
    )
    metadata_dir = manager.metadata_root / workspace_id / str(run["id"])
    metadata_dir.mkdir(parents=True)
    (metadata_dir / "state.json").write_text("{}", encoding="utf-8")

    removed = manager.cleanup_history(force=True)

    assert removed["file_runs"] == [
        {"id": str(run["id"]), "workspace_id": workspace_id}
    ]
    assert not metadata_dir.exists()


def _wait_terminal_run(
    manager: FileRunManager,
    run_id: str,
    *,
    terminal: bool = True,
    timeout: float = 5,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        latest = manager.reconcile(run_id)
        if bool(latest.get("state") in FILE_RUN_TERMINAL_STATES) == terminal:
            return latest
        time.sleep(0.05)
    raise AssertionError(f"File Run did not reach expected state: {latest}")


def _cleanup_workspace(workspace: dict[str, object]) -> None:
    subprocess.run(
        ["tmux", "kill-session", "-t", str(workspace["tmux_session"])],
        check=False,
        capture_output=True,
    )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_start_and_idempotent_replay_cannot_be_reconciled_lost_mid_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, workspace, project = _local_manager(tmp_path)
    script = project / "wait.py"
    script.write_text("import time\ntime.sleep(1)\n", encoding="utf-8")
    digest = manager.files.inspect_runnable(project, "wait.py").digest
    key = str(uuid.uuid4())
    entered = threading.Event()
    release = threading.Event()
    original_start = manager.terminals.start_file_run

    def delayed_start(*args: object, **kwargs: object) -> dict[str, object]:
        entered.set()
        assert release.wait(timeout=5)
        return original_start(*args, **kwargs)

    monkeypatch.setattr(manager.terminals, "start_file_run", delayed_start)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            first_future = pool.submit(
                manager.start,
                workspace,
                "wait.py",
                expected_digest=digest,
                idempotency_key=key,
            )
            assert entered.wait(timeout=5)
            active = manager.store.get_active_file_run(str(workspace["id"]))
            assert active is not None
            observer_future = pool.submit(manager.reconcile, str(active["id"]))
            replay_future = pool.submit(
                manager.start,
                workspace,
                "wait.py",
                expected_digest=digest,
                idempotency_key=key,
            )
            release.set()
            first = first_future.result(timeout=5)
            observed = observer_future.result(timeout=5)
            replay = replay_future.result(timeout=5)

        assert first["id"] == observed["id"] == replay["id"]
        assert first["state"] != "lost"
        assert observed["state"] != "lost"
        assert replay["state"] != "lost"
    finally:
        active = manager.store.get_active_file_run(str(workspace["id"]))
        if active is not None:
            manager.kill(str(active["id"]))
        _cleanup_workspace(workspace)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_failed_first_respawn_does_not_leave_a_managed_shell_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, workspace, _project = _local_manager(tmp_path)
    run_id = str(uuid.uuid4())
    original_run_tmux = manager.terminals._run_tmux

    def fail_respawn(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        if args and args[0] == "respawn-pane":
            return subprocess.CompletedProcess(["tmux", *args], 1, "", "rejected")
        return original_run_tmux(*args, check=check)

    monkeypatch.setattr(manager.terminals, "_run_tmux", fail_respawn)
    try:
        with pytest.raises(TerminalError, match="rejected"):
            manager.terminals.start_file_run(
                workspace,
                run_id=run_id,
                runner_id="python3",
                runtime_error_code="python3_missing",
                argv=("python3", "--", "./main.py"),
                metadata_dir=tmp_path / "metadata" / run_id,
            )
        terminals = manager.terminals.ensure_workspace(workspace)
        assert all(item["role"] == "shell" for item in terminals)
    finally:
        _cleanup_workspace(workspace)


def test_completion_requires_an_observed_stop_signal_to_be_stopped(
    tmp_path: Path,
) -> None:
    store, workspace = _store_with_workspace(tmp_path)
    manager = TerminalManager(store)
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    run_id = str(uuid.uuid4())
    base = {
        "run_id": run_id,
        "exit_code": 0,
        "stop_requested": True,
        "started_at": "2026-08-11T00:00:00Z",
        "ended_at": "2026-08-11T00:00:01Z",
    }
    (metadata / "completion.json").write_text(
        json.dumps({**base, "stop_signal": None}), encoding="utf-8"
    )
    natural = manager.inspect_file_run(
        workspace, run_id=run_id, metadata_dir=metadata
    )
    assert natural["state"] == "finished"

    (metadata / "completion.json").write_text(
        json.dumps({**base, "stop_signal": "INT"}), encoding="utf-8"
    )
    interrupted = manager.inspect_file_run(
        workspace, run_id=run_id, metadata_dir=metadata
    )
    assert interrupted["state"] == "stopped"


@pytest.mark.parametrize("exit_code", [0, 1, 126, 127, 130])
def test_dead_pane_fallback_requires_successful_runtime_preparation(
    exit_code: int,
) -> None:
    state = {
        "state": "running",
        "started_at": "2026-08-18T00:00:00Z",
    }

    pane = {"dead": True, "exit_code": exit_code}
    assert file_run_dead_pane_fallback(state, pane) == {
        "state": "finished",
        "started_at": "2026-08-18T00:00:00Z",
        "ended_at": None,
        "exit_code": exit_code,
    }
    assert file_run_dead_pane_fallback(None, pane) is None
    assert file_run_dead_pane_fallback(state, {"dead": False, "exit_code": exit_code}) is None
    assert file_run_dead_pane_fallback(state, {"dead": True, "exit_code": None}) is None


def test_recent_dispatch_marker_outlives_a_stale_reused_pane_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, workspace = _store_with_workspace(tmp_path)
    manager = TerminalManager(store)
    run_id = str(uuid.uuid4())
    metadata = tmp_path / "metadata" / run_id
    manager._write_file_run_metadata(metadata, run_id=run_id)
    terminal = store.create_terminal(
        str(workspace["id"]),
        "Run",
        "@1",
        role="file_run",
        managed_run_id=run_id,
    )
    records = [
        {
            "tmux_window": "@1",
            "tmux_session": str(workspace["tmux_session"]),
            "activity_at": None,
            "name": "Run",
            "role": "file_run",
            "managed_run_id": run_id,
        }
    ]
    stale_dead_pane = {
        "dead": True,
        "exit_code": 0,
        "pid": None,
        "dead_at": 1,
    }
    monkeypatch.setattr(manager, "session_exists", lambda _session: True)
    monkeypatch.setattr(manager, "_list_tmux_window_records", lambda _session: records)
    monkeypatch.setattr(manager, "_file_run_pane", lambda _window: stale_dead_pane)

    provisional = manager.inspect_file_run(
        workspace,
        run_id=run_id,
        metadata_dir=metadata,
    )

    assert provisional == {
        "state": "preparing",
        "started_at": None,
    }
    assert file_run_completion_grace_active(
        stale_dead_pane,
        dispatch_at=100,
        now=101,
    )
    assert not file_run_completion_grace_active(
        stale_dead_pane,
        dispatch_at=100,
        now=103,
    )

    request_id = metadata / "request-id"
    os.utime(request_id, (1, 1))
    expired = manager.inspect_file_run(
        workspace,
        run_id=run_id,
        metadata_dir=metadata,
    )
    assert expired["state"] == "lost"
    assert expired["error_code"] == "completion_missing"
    assert terminal["id"] == store.get_managed_terminal(
        str(workspace["id"]), "file_run"
    )["id"]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_local_file_run_preserves_argv_interactive_pty_and_exit_code(
    tmp_path: Path,
) -> None:
    manager, workspace, project = _local_manager(tmp_path)
    script = project / "ask.py"
    script.write_text(
        "value = input('value: ')\nprint('seen:' + value)\n",
        encoding="utf-8",
    )
    try:
        digest = manager.files.inspect_runnable(project, "ask.py").digest
        run = manager.start(
            workspace,
            "ask.py",
            expected_digest=digest,
            idempotency_key=str(uuid.uuid4()),
        )
        running = _wait_terminal_run(manager, str(run["id"]), terminal=False)
        assert running["state"] == "running"
        terminal = manager.store.get_managed_terminal(str(workspace["id"]), "file_run")
        assert terminal is not None
        subprocess.run(
            ["tmux", "send-keys", "-t", str(terminal["tmux_window"]), "한글 value", "Enter"],
            check=True,
        )
        finished = _wait_terminal_run(manager, str(run["id"]))
        assert finished["state"] == "finished"
        assert finished["exit_code"] == 0
        assert finished["argv"] == ["python3", "--", "./ask.py"]
        output = manager.terminals.capture_scrollback(workspace, terminal)
        assert "seen:한글 value" in output
        assert terminal["role"] == "file_run"
        assert terminal["managed_run_id"] == run["id"]
    finally:
        _cleanup_workspace(workspace)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.parametrize(
    ("relative_path", "content", "executable", "expected_runner"),
    [
        ("main.js", "console.log('node runner')\n", False, "node"),
        ("main.sh", "printf 'bash runner\\n'\n", False, "bash"),
        ("tool.custom", "#!/bin/sh\nprintf 'direct runner\\n'\n", True, "direct"),
    ],
)
def test_local_file_run_registry_executes_each_builtin_runner_and_reuses_slot(
    tmp_path: Path,
    relative_path: str,
    content: str,
    executable: bool,
    expected_runner: str,
) -> None:
    if expected_runner == "node" and shutil.which("node") is None:
        pytest.skip("Node.js is required")
    manager, workspace, project = _local_manager(tmp_path)
    script = project / relative_path
    script.write_text(content, encoding="utf-8")
    if executable:
        script.chmod(0o700)
    try:
        first_terminal_id: str | None = None
        for _attempt in range(2):
            digest = manager.files.inspect_runnable(project, relative_path).digest
            run = manager.start(
                workspace,
                relative_path,
                expected_digest=digest,
                idempotency_key=str(uuid.uuid4()),
            )
            finished = _wait_terminal_run(manager, str(run["id"]))
            assert finished["state"] == "finished", finished
            assert finished["exit_code"] == 0
            assert finished["runner_id"] == expected_runner
            terminal = manager.store.get_managed_terminal(
                str(workspace["id"]), "file_run"
            )
            assert terminal is not None
            if first_terminal_id is None:
                first_terminal_id = str(terminal["id"])
            else:
                assert terminal["id"] == first_terminal_id
    finally:
        _cleanup_workspace(workspace)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_direct_runner_distinguishes_missing_interpreter_from_program_exit_127(
    tmp_path: Path,
) -> None:
    manager, workspace, project = _local_manager(tmp_path)
    missing = project / "missing-interpreter"
    missing.write_text(
        "#!/termroom-interpreter-that-does-not-exist\nprintf 'never\\n'\n",
        encoding="utf-8",
    )
    missing.chmod(0o700)
    exits_127 = project / "exits-127"
    exits_127.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    exits_127.chmod(0o700)
    try:
        missing_digest = manager.files.inspect_runnable(
            project, missing.name
        ).digest
        missing_run = manager.start(
            workspace,
            missing.name,
            expected_digest=missing_digest,
            idempotency_key=str(uuid.uuid4()),
        )
        failed = _wait_terminal_run(manager, str(missing_run["id"]))
        assert failed["state"] == "failed"
        assert failed["error_code"] == "direct_runner_failed"
        assert failed["exit_code"] is None

        exit_digest = manager.files.inspect_runnable(project, exits_127.name).digest
        exit_run = manager.start(
            workspace,
            exits_127.name,
            expected_digest=exit_digest,
            idempotency_key=str(uuid.uuid4()),
        )
        finished = _wait_terminal_run(manager, str(exit_run["id"]))
        assert finished["state"] == "finished"
        assert finished["exit_code"] == 127
        assert finished["error_code"] is None
    finally:
        _cleanup_workspace(workspace)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_local_file_run_reports_missing_runtime_without_fallback(tmp_path: Path) -> None:
    manager, workspace, project = _local_manager(tmp_path)
    script = project / "main.js"
    script.write_text("console.log('never')\n", encoding="utf-8")
    run_id = str(uuid.uuid4())
    metadata_dir = tmp_path / "metadata" / run_id
    try:
        manager.terminals.start_file_run(
            workspace,
            run_id=run_id,
            runner_id="node",
            runtime_error_code="nodejs_missing",
            argv=("termroom-nodejs-runtime-is-missing", "--", "./main.js"),
            metadata_dir=metadata_dir,
        )
        deadline = time.monotonic() + 5
        failed: dict[str, object] = {}
        while time.monotonic() < deadline:
            failed = manager.terminals.inspect_file_run(
                workspace, run_id=run_id, metadata_dir=metadata_dir
            )
            if failed.get("state") == "failed":
                break
            time.sleep(0.05)
        assert failed["state"] == "failed"
        assert failed["error_code"] == "nodejs_missing"
    finally:
        _cleanup_workspace(workspace)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_local_file_run_interrupt_is_confirmed_by_the_managed_wrapper(
    tmp_path: Path,
) -> None:
    manager, workspace, project = _local_manager(tmp_path)
    (project / "wait.py").write_text(
        "import time\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    try:
        digest = manager.files.inspect_runnable(project, "wait.py").digest
        run = manager.start(
            workspace,
            "wait.py",
            expected_digest=digest,
            idempotency_key=str(uuid.uuid4()),
        )
        _wait_terminal_run(manager, str(run["id"]), terminal=False)
        manager.stop(str(run["id"]))
        stopped = _wait_terminal_run(manager, str(run["id"]))
        assert stopped["state"] == "stopped"
        assert stopped["stop_requested_at"] is not None
        assert stopped["exit_code"] != 0
    finally:
        _cleanup_workspace(workspace)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_local_file_run_child_does_not_receive_core_login_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TERMROOM_PASSWORD", "must-not-reach-file-run")
    manager, workspace, project = _local_manager(tmp_path)
    (project / "environment.py").write_text(
        "import os\nraise SystemExit(41 if 'TERMROOM_PASSWORD' in os.environ else 0)\n",
        encoding="utf-8",
    )
    try:
        digest = manager.files.inspect_runnable(project, "environment.py").digest
        run = manager.start(
            workspace,
            "environment.py",
            expected_digest=digest,
            idempotency_key=str(uuid.uuid4()),
        )
        finished = _wait_terminal_run(manager, str(run["id"]))
        assert finished["state"] == "finished"
        assert finished["exit_code"] == 0
    finally:
        _cleanup_workspace(workspace)


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
async def test_new_manager_observer_reconciles_completion_after_core_restart(
    tmp_path: Path,
) -> None:
    first, workspace, project = _local_manager(tmp_path)
    (project / "restart.py").write_text(
        "import time\ntime.sleep(0.4)\nprint('after restart')\n",
        encoding="utf-8",
    )
    second: FileRunManager | None = None
    try:
        digest = first.files.inspect_runnable(project, "restart.py").digest
        run = first.start(
            workspace,
            "restart.py",
            expected_digest=digest,
            idempotency_key=str(uuid.uuid4()),
        )
        assert run["state"] in {"preparing", "running"}

        second = FileRunManager(
            first.store,
            first.workspaces,
            FileService(),
            TerminalManager(first.store),
            SSHBackend(first.store, tmp_path / "state"),
            state_dir=tmp_path / "state",
            max_edit_bytes=1024 * 1024,
        )
        await second.startup()
        deadline = time.monotonic() + 5
        final: dict[str, object] = {}
        while time.monotonic() < deadline:
            current = first.store.get_file_run(str(run["id"]))
            assert current is not None
            final = current
            if current["state"] in FILE_RUN_TERMINAL_STATES:
                break
            await asyncio.sleep(0.05)

        assert final["state"] == "finished"
        assert final["exit_code"] == 0
        events = first.store.list_activity_events()
        assert [event["kind"] for event in events] == ["file_run.completed"]
        terminal = first.store.get_managed_terminal(
            str(workspace["id"]), "file_run"
        )
        assert terminal is not None
        output = second.terminals.capture_scrollback(workspace, terminal)
        assert "after restart" in output
    finally:
        if second is not None:
            await second.shutdown()
        _cleanup_workspace(workspace)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_local_file_run_special_path_is_not_shell_syntax(tmp_path: Path) -> None:
    manager, workspace, project = _local_manager(tmp_path)
    path = "한글 $(touch PWNED); value.py"
    (project / path).write_text("print('safe path')\n", encoding="utf-8")
    try:
        digest = manager.files.inspect_runnable(project, path).digest
        run = manager.start(
            workspace,
            path,
            expected_digest=digest,
            idempotency_key=str(uuid.uuid4()),
        )
        finished = _wait_terminal_run(manager, str(run["id"]))
        assert finished["state"] == "finished"
        assert finished["exit_code"] == 0
        assert not (project / "PWNED").exists()
        assert finished["argv"][-1] == f"./{path}"
    finally:
        _cleanup_workspace(workspace)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_local_nonzero_exit_is_finished_with_one_failed_event(tmp_path: Path) -> None:
    manager, workspace, project = _local_manager(tmp_path)
    (project / "fail.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
    try:
        digest = manager.files.inspect_runnable(project, "fail.py").digest
        run = manager.start(
            workspace,
            "fail.py",
            expected_digest=digest,
            idempotency_key=str(uuid.uuid4()),
        )
        finished = _wait_terminal_run(manager, str(run["id"]))
        assert finished["state"] == "finished"
        assert finished["exit_code"] == 7
        events = manager.store.list_activity_events()
        assert [event["kind"] for event in events] == ["file_run.failed"]
    finally:
        _cleanup_workspace(workspace)


def test_local_file_run_rejects_symlink_and_workspace_escape_before_claim(
    tmp_path: Path,
) -> None:
    manager, workspace, project = _local_manager(tmp_path)
    real = project / "real.py"
    real.write_text("print('real')\n", encoding="utf-8")
    (project / "link.py").symlink_to(real)
    outside = project.parent / "outside.py"
    outside.write_text("print('outside')\n", encoding="utf-8")

    with pytest.raises(PathBoundaryError):
        manager.start(
            workspace,
            "link.py",
            expected_digest="0" * 64,
            idempotency_key=str(uuid.uuid4()),
        )
    with pytest.raises(PathBoundaryError):
        manager.start(
            workspace,
            "../outside.py",
            expected_digest="0" * 64,
            idempotency_key=str(uuid.uuid4()),
        )
    assert manager.store.get_active_file_run(str(workspace["id"])) is None


def test_remote_observation_failure_keeps_active_state_without_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, workspace, _project = _local_manager(tmp_path)
    payload = _claim_payload(str(workspace["id"]))
    _status, run = manager.store.claim_file_run(payload)

    def unavailable(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise SSHBackendError("offline")

    monkeypatch.setattr(manager, "_observe_backend", unavailable)
    observed = manager.reconcile(str(run["id"]))
    assert observed["state"] == "preparing"
    assert observed["connection"] == "offline"
    assert manager.store.list_activity_events() == []


def test_node_without_file_run_capability_is_gated_before_dispatch(tmp_path: Path) -> None:
    manager, workspace, _project = _local_manager(tmp_path)

    class BaseOnlyNodeRemote:
        @staticmethod
        def is_node(_workspace: object) -> bool:
            return True

        @staticmethod
        def supports_capability(_workspace: object, capability: str) -> bool:
            assert capability == "file_run"
            return False

    manager.remote = BaseOnlyNodeRemote()  # type: ignore[assignment]
    node_workspace = {
        **workspace,
        "backend_kind": "remote",
        "computer": {"connection_method": "node"},
    }
    with pytest.raises(FileRunError) as unsupported:
        manager.runner_for_file(node_workspace, "main.py")
    assert unsupported.value.code == "workspace_not_supported"


def test_confirmed_remote_dispatch_rejection_is_failed_not_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, workspace, _project = _local_manager(tmp_path)
    remote_workspace = {
        **workspace,
        "backend_kind": "remote",
        "computer": {"connection_method": "ssh"},
    }
    runnable = _runnable("main.py")

    monkeypatch.setattr(manager, "_inspect", lambda *_args, **_kwargs: runnable)

    def rejected(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise SSHBackendError("tmux respawn rejected")

    monkeypatch.setattr(manager.ssh, "start_file_run", rejected)
    monkeypatch.setattr(
        manager,
        "_observe_backend",
        lambda *_args, **_kwargs: {
            "state": "lost",
            "error_code": "managed_terminal_missing",
        },
    )

    run = manager.start(
        remote_workspace,
        "main.py",
        expected_digest=runnable.digest,
        idempotency_key=str(uuid.uuid4()),
    )
    assert run["state"] == "failed"
    assert run["error_code"] == "start_failed"
    assert [event["kind"] for event in manager.store.list_activity_events()] == [
        "file_run.failed"
    ]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_local_file_run_stop_and_force_stop_target_only_managed_pane(
    tmp_path: Path,
) -> None:
    manager, workspace, project = _local_manager(tmp_path)
    (project / "wait.py").write_text(
        "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "while True: time.sleep(0.1)\n",
        encoding="utf-8",
    )
    try:
        shell = manager.terminals.ensure_workspace(workspace)[0]
        digest = manager.files.inspect_runnable(project, "wait.py").digest
        run = manager.start(
            workspace,
            "wait.py",
            expected_digest=digest,
            idempotency_key=str(uuid.uuid4()),
        )
        _wait_terminal_run(manager, str(run["id"]), terminal=False)
        stopped = manager.stop(str(run["id"]))
        assert stopped["needs_force"] is True
        killed = manager.kill(str(run["id"]))
        assert killed["state"] == "stopped"
        assert manager.terminals.session_exists(str(workspace["tmux_session"]))
        assert manager.store.get_terminal(str(shell["id"])) is not None
    finally:
        _cleanup_workspace(workspace)


def test_file_run_conflict_exposes_existing_active_run(tmp_path: Path) -> None:
    manager, workspace, project = _local_manager(tmp_path)
    (project / "one.py").write_text("print('one')\n", encoding="utf-8")
    payload = _claim_payload(str(workspace["id"]), path="one.py")
    _status, active = manager.store.claim_file_run(payload)
    digest = manager.files.inspect_runnable(project, "one.py").digest
    with pytest.raises(FileRunConflict) as conflict:
        manager.start(
            workspace,
            "one.py",
            expected_digest=digest,
            idempotency_key=str(uuid.uuid4()),
        )
    assert conflict.value.values["active_run"]["id"] == active["id"]
