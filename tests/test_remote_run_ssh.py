from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from termroom.db import StateStore
from termroom.run_sources import (
    build_public_git_clone_invocation,
    materialize_workspace_snapshot,
)
from termroom.ssh_backend import (
    REMOTE_GIT_BOOTSTRAP_SCRIPT,
    REMOTE_RUN_LOG_PIPE_SCRIPT,
    REMOTE_RUNNER_SCRIPT,
    RemoteRunLayoutError,
    SSHBackend,
    SSHBackendError,
    SSHCommandStatusUnknown,
)

RUN_ID = "123e4567-e89b-42d3-a456-426614174000"


class _ExecStream:
    def __init__(self, value: bytes, status: int) -> None:
        self.value = value
        self.channel = SimpleNamespace(recv_exit_status=lambda: status)

    def read(self) -> bytes:
        return self.value

    def close(self) -> None:
        pass


class _ExecClient:
    def __init__(self, status: int, *, error: BaseException | None = None) -> None:
        self.status = status
        self.error = error

    def exec_command(self, _command: str, *, timeout: float | None = None) -> tuple[Any, Any, Any]:
        del timeout
        if self.error:
            raise self.error
        return (
            _ExecStream(b"", self.status),
            _ExecStream(b"", self.status),
            _ExecStream(b"__TERMROOM_NO_TMUX__", self.status),
        )


@pytest.mark.parametrize(
    "client",
    (
        _ExecClient(-1),
        _ExecClient(0, error=EOFError("transport closed before an exit status")),
    ),
)
def test_remote_command_transport_loss_has_distinct_unknown_status(client: object) -> None:
    with pytest.raises(SSHCommandStatusUnknown):
        SSHBackend._exec_client(client, "true")  # type: ignore[arg-type]


def test_remote_command_nonzero_status_is_a_definitive_backend_error() -> None:
    with pytest.raises(SSHBackendError, match="tmux is not installed") as raised:
        SSHBackend._exec_client(_ExecClient(45), "false")  # type: ignore[arg-type]

    assert not isinstance(raised.value, SSHCommandStatusUnknown)


class _FakeFile:
    def __init__(self, path: Path, mode: str) -> None:
        self.handle = path.open(mode)

    def __enter__(self) -> _FakeFile:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def read(self, size: int = -1) -> Any:
        return self.handle.read(size)

    def write(self, value: Any) -> int:
        return self.handle.write(value)

    def seek(self, offset: int) -> int:
        return self.handle.seek(offset)

    def close(self) -> None:
        self.handle.close()

    def stat(self) -> Any:
        return _attributes(os.fstat(self.handle.fileno()))


def _attributes(value: os.stat_result, *, filename: str = "") -> Any:
    return SimpleNamespace(
        filename=filename,
        st_mode=value.st_mode,
        st_size=value.st_size,
        st_mtime=int(value.st_mtime),
    )


class _FakeSFTP:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home" / "tester"
        self.home.mkdir(parents=True)

    def _local(self, value: str) -> Path:
        if value == ".":
            return self.home
        if value.startswith("/"):
            return self.root / value.lstrip("/")
        return self.home / value

    def _remote(self, value: Path) -> str:
        resolved = value.resolve(strict=False)
        try:
            relative = resolved.relative_to(self.root.resolve())
        except ValueError:
            return resolved.as_posix()
        return "/" + relative.as_posix()

    def normalize(self, value: str) -> str:
        return self._remote(self._local(value))

    def lstat(self, value: str) -> Any:
        return _attributes(os.lstat(self._local(value)))

    def listdir_attr(self, value: str) -> list[Any]:
        return [
            _attributes(os.lstat(child), filename=child.name)
            for child in sorted(self._local(value).iterdir(), key=lambda path: path.name)
        ]

    def mkdir(self, value: str, mode: int = 0o777) -> None:
        self._local(value).mkdir(mode=mode)

    def rmdir(self, value: str) -> None:
        self._local(value).rmdir()

    def remove(self, value: str) -> None:
        self._local(value).unlink()

    def rename(self, source: str, target: str) -> None:
        self._local(source).rename(self._local(target))

    def posix_rename(self, source: str, target: str) -> None:
        os.replace(self._local(source), self._local(target))

    def open(self, value: str, mode: str = "r") -> _FakeFile:
        return _FakeFile(self._local(value), mode)

    def chmod(self, value: str, mode: int) -> None:
        self._local(value).chmod(mode)

    def symlink(self, target: str, value: str) -> None:
        os.symlink(target, self._local(value))

    def readlink(self, value: str) -> str:
        return os.readlink(self._local(value))

    def close(self) -> None:
        pass


class _FakeTransport:
    def set_keepalive(self, _seconds: int) -> None:
        pass


class _FakeClient:
    def __init__(self, sftp: _FakeSFTP) -> None:
        self.sftp = sftp

    def get_transport(self) -> _FakeTransport:
        return _FakeTransport()

    def open_sftp(self) -> _FakeSFTP:
        return self.sftp

    def close(self) -> None:
        pass


@pytest.fixture
def remote_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SSHBackend, _FakeSFTP, dict[str, Any]]:
    state = tmp_path / "state"
    state.mkdir()
    store = StateStore(state / "termroom.sqlite3")
    store.initialize()
    backend = SSHBackend(store, state)
    sftp = _FakeSFTP(tmp_path / "remote")
    computer = {"id": "target", "run_base_dir": "/scratch/termroom-runs"}
    client = _FakeClient(sftp)
    monkeypatch.setattr(backend, "_connect", lambda _computer: client)
    return backend, sftp, computer


def test_remote_run_id_is_canonical_uuid_v4_before_remote_side_effects() -> None:
    assert SSHBackend.validate_remote_run_id(RUN_ID) == RUN_ID
    for invalid in (
        "123e4567e89b42d3a456426614174000",
        RUN_ID.upper(),
        "123e4567-e89b-12d3-a456-426614174000",
        "../run",
    ):
        with pytest.raises(ValueError, match="UUID"):
            SSHBackend.validate_remote_run_id(invalid)


def test_existing_run_base_must_not_be_replaceable_by_other_users(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
) -> None:
    backend, sftp, computer = remote_backend
    base = sftp._local(str(computer["run_base_dir"]))
    base.mkdir(parents=True, mode=0o770)
    base.chmod(0o770)

    with pytest.raises(SSHBackendError, match="writable by other users"):
        backend.create_remote_run_layout(computer, RUN_ID, command="exit 0")


def test_layout_is_canonical_direct_child_and_command_is_not_runner_metadata(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
) -> None:
    backend, sftp, computer = remote_backend
    layout = backend.create_remote_run_layout(
        computer, RUN_ID, command="printf 'private command\\n'"
    )

    assert layout["run_base"] == "/scratch/termroom-runs"
    assert layout["root"] == f"/scratch/termroom-runs/{RUN_ID}"
    assert sftp._local(layout["marker"]).read_text() == RUN_ID + "\n"
    assert sftp._local(layout["command"]).read_text() == "printf 'private command\\n'\n"
    runner = sftp._local(layout["runner"]).read_text()
    assert "/bin/bash --noprofile --norc --" in runner
    assert "private command" not in runner
    assert stat.S_IMODE(sftp._local(layout["root"]).stat().st_mode) == 0o700

    again = backend.create_remote_run_layout(
        computer, RUN_ID, command="printf 'private command\\n'"
    )
    assert again == layout
    with pytest.raises(SSHBackendError, match="different command"):
        backend.create_remote_run_layout(computer, RUN_ID, command="echo changed")


def test_layout_is_fully_built_before_uuid_root_is_published(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, sftp, computer = remote_backend
    final_root = f"/scratch/termroom-runs/{RUN_ID}"
    creating_root = f"/scratch/termroom-runs/.termroom-creating-{RUN_ID}"
    real_rename = sftp.rename
    publications: list[tuple[str, str]] = []

    def inspect_publish(source: str, target: str) -> None:
        if source == creating_root and target == final_root:
            assert not sftp._local(final_root).exists()
            assert sftp._local(f"{creating_root}/.termroom/marker").read_text() == RUN_ID + "\n"
            assert sftp._local(f"{creating_root}/.termroom/command.sh").is_file()
            assert sftp._local(f"{creating_root}/work").is_dir()
            publications.append((source, target))
        real_rename(source, target)

    monkeypatch.setattr(sftp, "rename", inspect_publish)

    layout = backend.create_remote_run_layout(computer, RUN_ID, command="printf ready")

    assert publications == [(creating_root, final_root)]
    assert sftp._local(layout["root"]).is_dir()
    assert not sftp._local(creating_root).exists()


def test_layout_failure_before_marker_leaves_no_public_or_staging_root(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, sftp, computer = remote_backend
    real_write = backend._sftp_atomic_write
    creating_root = sftp._local(
        f"/scratch/termroom-runs/.termroom-creating-{RUN_ID}"
    )

    def fail_marker(
        target_sftp: _FakeSFTP,
        destination: str,
        value: bytes,
        *,
        mode: int = 0o600,
    ) -> None:
        if destination.endswith("/.termroom/marker"):
            raise OSError("simulated marker failure")
        real_write(target_sftp, destination, value, mode=mode)

    monkeypatch.setattr(backend, "_sftp_atomic_write", fail_marker)

    def remove_pristine_creation(
        _client: object,
        command: str,
        *,
        timeout: float | None = 20,
    ) -> str:
        assert timeout == 20
        assert "rmdir -- .termroom" in command
        (creating_root / ".termroom").rmdir()
        creating_root.rmdir()
        return ""

    monkeypatch.setattr(backend, "_exec_client", remove_pristine_creation)

    with pytest.raises(OSError, match="marker failure"):
        backend.create_remote_run_layout(computer, RUN_ID, command="printf ready")

    base = sftp._local("/scratch/termroom-runs")
    assert not (base / RUN_ID).exists()
    assert not (base / f".termroom-creating-{RUN_ID}").exists()


def test_fixed_runner_preserves_normal_bash_multiline_command_semantics(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
) -> None:
    backend, sftp, computer = remote_backend
    layout = backend.create_remote_run_layout(
        computer,
        RUN_ID,
        command="printf 'before\\n'\nprintf x | false\nprintf 'after\\n'",
    )
    runner = sftp._local(layout["runner"])

    completed = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", str(runner)],
        check=False,
        capture_output=True,
    )

    assert completed.returncode == 0
    assert sftp._local(layout["output"]).read_text() == "before\nafter\n"
    completion = json.loads(sftp._local(layout["completion"]).read_text())
    assert completion["exit_code"] == 0
    assert completion["stop_requested"] is False
    assert completion["log_incomplete"] is False


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_fixed_runner_keeps_output_visible_in_tmux_while_sealing_log(tmp_path: Path) -> None:
    run_id = str(uuid.uuid4())
    session = f"termroom-run-{run_id}"
    root = tmp_path / run_id
    work = root / "work"
    metadata = root / ".termroom"
    work.mkdir(parents=True)
    metadata.mkdir()
    (metadata / "cwd").write_text(".\n")
    (metadata / "command.sh").write_text("printf 'visible output\\n'\n")
    (metadata / "runner.sh").write_text(REMOTE_RUNNER_SCRIPT)
    (metadata / "log-pipe.sh").write_text(REMOTE_RUN_LOG_PIPE_SCRIPT)
    (metadata / "runner.sh").chmod(0o700)
    (metadata / "log-pipe.sh").chmod(0o700)

    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session, "-c", str(work), "-n", "run"],
            check=True,
        )
        subprocess.run(
            ["tmux", "set-window-option", "-t", session, "remain-on-exit", "on"],
            check=True,
        )
        subprocess.run(
            [
                "tmux",
                "respawn-pane",
                "-k",
                "-t",
                f"{session}:0.0",
                "-c",
                str(work),
                "/bin/bash",
                "--noprofile",
                "--norc",
                str(metadata / "runner.sh"),
            ],
            check=True,
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not (metadata / "completion.json").is_file():
            time.sleep(0.05)
        assert (metadata / "completion.json").is_file()
        captured = subprocess.check_output(
            ["tmux", "capture-pane", "-p", "-S", "-", "-t", f"{session}:0.0"],
            text=True,
        )
        assert "visible output" in captured
        assert (metadata / "output.log").read_text() == "visible output\n"
        completion = json.loads((metadata / "completion.json").read_text())
        assert completion["exit_code"] == 0
        assert completion["log_incomplete"] is False
    finally:
        with contextlib.suppress(subprocess.CalledProcessError):
            subprocess.run(
                ["tmux", "kill-session", "-t", session],
                check=True,
                capture_output=True,
            )


def test_start_tmux_command_never_contains_the_user_command(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _sftp, computer = remote_backend
    secret = "echo SHOULD_NEVER_APPEAR_IN_TMUX"
    layout = backend.create_remote_run_layout(computer, RUN_ID, command=secret)
    executed: list[str] = []
    monkeypatch.setattr(
        backend,
        "_exec_client",
        lambda _client, command: executed.append(command) or "",
    )

    backend.start_remote_run(computer, layout["run_base"], RUN_ID)

    assert len(executed) == 1
    assert executed[0].startswith("/bin/bash --noprofile --norc -c ")
    assert secret not in executed[0]
    assert "/bin/bash --noprofile --norc" in executed[0]
    assert "runner.sh" in executed[0]
    assert "respawn-pane" in executed[0]


def test_remote_run_workspace_shell_reuses_session_and_is_idempotent(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _sftp, computer = remote_backend
    secret = "echo SOURCE_TEXT_MUST_NOT_REACH_TMUX"
    layout = backend.create_remote_run_layout(computer, RUN_ID, command=secret)
    windows = [{"id": "@1", "name": "run"}]
    executed: list[str] = []

    def fake_exec(_client: object, command: str) -> str:
        executed.append(command)
        if "list-panes" in command:
            return "0|\n"
        if "list-windows" in command:
            return "".join(f"{item['id']}|{item['name']}\n" for item in windows)
        if "new-window" in command:
            windows.append({"id": "@2", "name": "shell"})
            return "@2|shell\n"
        raise AssertionError(command)

    monkeypatch.setattr(backend, "_exec_client", fake_exec)

    first = backend.ensure_remote_run_workspace_shell(computer, layout["run_base"], RUN_ID)
    second = backend.ensure_remote_run_workspace_shell(computer, layout["run_base"], RUN_ID)

    assert first["session_name"] == f"termroom-run-{RUN_ID}"
    assert first["work_path"] == layout["work"]
    assert first["shell_window"] == {"id": "@2", "name": "shell"}
    assert second["shell_window"] == {"id": "@2", "name": "shell"}
    assert sum("new-window" in command for command in executed) == 1
    assert all(
        command.startswith("/bin/bash --noprofile --norc -c ")
        for command in executed
    )
    assert all(secret not in command for command in executed)


def test_remote_run_workspace_shell_recovery_requires_explicit_terminal_permission(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, _sftp, computer = remote_backend
    layout = backend.create_remote_run_layout(computer, RUN_ID, command="exit 0")
    executed: list[str] = []

    def fake_exec(_client: object, command: str) -> str:
        executed.append(command)
        if "list-panes" in command:
            return "missing\n"
        if "new-session" in command:
            return "@9|shell\n"
        raise AssertionError(command)

    monkeypatch.setattr(backend, "_exec_client", fake_exec)

    with pytest.raises(SSHBackendError, match="terminal state"):
        backend.ensure_remote_run_workspace_shell(computer, layout["run_base"], RUN_ID)
    assert not any("new-session" in command for command in executed)

    restored = backend.ensure_remote_run_workspace_shell(
        computer,
        layout["run_base"],
        RUN_ID,
        allow_create_session=True,
    )
    assert restored["created_session"] is True
    assert restored["shell_window"] == {"id": "@9", "name": "shell"}
    assert sum("new-session" in command for command in executed) == 1


def test_reconcile_prefers_atomic_completion_and_log_reads_use_byte_offsets(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, sftp, computer = remote_backend
    layout = backend.create_remote_run_layout(computer, RUN_ID, command="exit 7")
    sftp._local(layout["state"]).write_text(
        '{"phase":"running","started_at":"2026-08-09T01:00:00Z"}\n'
    )
    sftp._local(layout["completion"]).write_text(
        '{"exit_code":7,"stop_requested":false,'
        '"started_at":"2026-08-09T01:00:00Z",'
        '"ended_at":"2026-08-09T01:01:00Z"}\n'
    )
    payload = "첫째 줄\nsecond 😀 line\n".encode()
    sftp._local(layout["output"]).write_bytes(payload)
    monkeypatch.setattr(backend, "_exec_client", lambda _client, _command: "1|7\n")

    status = backend.reconcile_remote_run(computer, layout["run_base"], RUN_ID)
    assert status["state"] == "finished"
    assert status["exit_code"] == 7
    assert status["ended_at"] == "2026-08-09T01:01:00Z"

    first = backend.read_remote_run_log(
        computer, layout["run_base"], RUN_ID, offset=0, limit=7
    )
    second = backend.read_remote_run_log(
        computer,
        layout["run_base"],
        RUN_ID,
        offset=first["next_offset"],
        limit=len(payload),
    )
    combined = base64.b64decode(first["chunk_b64"]) + base64.b64decode(
        second["chunk_b64"]
    )
    assert combined == payload
    assert second["eof"] is True


@pytest.mark.parametrize(
    ("phase", "expected_state"),
    (("running", "running"), ("cloning", "preparing")),
)
def test_reconcile_resamples_tmux_when_active_metadata_arrives_after_stale_sample(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    expected_state: str,
) -> None:
    backend, sftp, computer = remote_backend
    layout = backend.create_remote_run_layout(computer, RUN_ID, command="sleep 300")
    samples: list[int] = []

    def racing_tmux_sample(_client: object, _run_id: str) -> dict[str, Any]:
        samples.append(len(samples) + 1)
        if len(samples) == 1:
            sftp._local(layout["state"]).write_text(
                json.dumps(
                    {
                        "phase": phase,
                        "started_at": "2026-08-09T01:00:00Z",
                    }
                )
                + "\n"
            )
            return {
                "exists": False,
                "run_pane_exists": False,
                "running": False,
                "pane_exit_code": None,
            }
        return {
            "exists": True,
            "run_pane_exists": True,
            "running": True,
            "pane_exit_code": None,
        }

    monkeypatch.setattr(backend, "_remote_tmux_status", racing_tmux_sample)

    status = backend.reconcile_remote_run(computer, layout["run_base"], RUN_ID)

    assert samples == [1, 2]
    assert status["state"] == expected_state
    assert status["phase"] == phase
    assert status["tmux_exists"] is True
    assert status["tmux_running"] is True


@pytest.mark.parametrize(
    ("state_update", "expected_state", "expected_errors"),
    (
        ("running", "lost", []),
        ("corrupt", "preparing", ["state.json"]),
        ("missing", "preparing", []),
    ),
    ids=("running", "corrupt", "missing"),
)
def test_reconcile_rereads_state_after_tmux_resample_before_terminal_inference(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    state_update: str,
    expected_state: str,
    expected_errors: list[str],
) -> None:
    backend, sftp, computer = remote_backend
    layout = backend.create_remote_run_layout(computer, RUN_ID, command="sleep 300")
    samples: list[int] = []

    def racing_tmux_sample(_client: object, _run_id: str) -> dict[str, Any]:
        samples.append(len(samples) + 1)
        state_path = sftp._local(layout["state"])
        if len(samples) == 1:
            state_path.write_text(
                json.dumps(
                    {
                        "phase": "cloning",
                        "started_at": "2026-08-09T01:00:00Z",
                    }
                )
                + "\n"
            )
        elif state_update == "running":
            state_path.write_text(
                json.dumps(
                    {
                        "phase": "running",
                        "started_at": "2026-08-09T01:00:01Z",
                    }
                )
                + "\n"
            )
        elif state_update == "corrupt":
            state_path.write_text("{invalid-json\n")
        else:
            state_path.unlink()
        return {
            "exists": False,
            "run_pane_exists": False,
            "running": False,
            "pane_exit_code": None,
        }

    monkeypatch.setattr(backend, "_remote_tmux_status", racing_tmux_sample)

    status = backend.reconcile_remote_run(computer, layout["run_base"], RUN_ID)

    assert samples == [1, 2]
    assert status["state"] == expected_state
    assert status["phase"] is None
    assert status["tmux_exists"] is False
    assert status["tmux_running"] is False
    assert status["record_errors"] == expected_errors


@pytest.mark.parametrize(
    ("phase", "record_key", "record", "expected"),
    (
        (
            "running",
            "completion",
            {
                "exit_code": 0,
                "stop_requested": False,
                "started_at": "2026-08-09T01:00:00Z",
                "ended_at": "2026-08-09T01:00:03Z",
            },
            {"state": "finished", "exit_code": 0, "error_code": None},
        ),
        (
            "cloning",
            "prepare_result",
            {
                "state": "failed",
                "ended_at": "2026-08-09T01:00:03Z",
                "error_code": "git_clone_failed",
            },
            {
                "state": "failed",
                "exit_code": None,
                "error_code": "git_clone_failed",
            },
        ),
    ),
    ids=("completion", "prepare-failure"),
)
def test_reconcile_prefers_terminal_record_written_during_tmux_resample(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    record_key: str,
    record: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    backend, sftp, computer = remote_backend
    layout = backend.create_remote_run_layout(computer, RUN_ID, command="sleep 300")
    samples: list[int] = []

    def exiting_tmux_sample(_client: object, _run_id: str) -> dict[str, Any]:
        samples.append(len(samples) + 1)
        if len(samples) == 1:
            sftp._local(layout["state"]).write_text(
                json.dumps(
                    {
                        "phase": phase,
                        "started_at": "2026-08-09T01:00:00Z",
                    }
                )
                + "\n"
            )
        else:
            sftp._local(layout[record_key]).write_text(json.dumps(record) + "\n")
        return {
            "exists": False,
            "run_pane_exists": False,
            "running": False,
            "pane_exit_code": None,
        }

    monkeypatch.setattr(backend, "_remote_tmux_status", exiting_tmux_sample)

    status = backend.reconcile_remote_run(computer, layout["run_base"], RUN_ID)

    assert samples == [1, 2]
    assert status["phase"] is None
    assert status["ended_at"] == "2026-08-09T01:00:03Z"
    for key, value in expected.items():
        assert status.get(key) == value


@pytest.mark.parametrize(
    (
        "corrupt_key",
        "phase",
        "stop_requested",
        "expected_state",
        "expected_error",
    ),
    (
        ("completion", "running", False, "preparing", "completion.json"),
        ("prepare_result", "cloning", False, "preparing", "prepare-result.json"),
        ("state", None, True, "stopped", "state.json"),
    ),
    ids=("completion", "prepare-result", "state-with-stop"),
)
def test_reconcile_uses_only_explicit_evidence_with_syntax_corrupt_metadata(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    corrupt_key: str,
    phase: str | None,
    stop_requested: bool,
    expected_state: str,
    expected_error: str,
) -> None:
    backend, sftp, computer = remote_backend
    layout = backend.create_remote_run_layout(computer, RUN_ID, command="sleep 300")
    if phase:
        sftp._local(layout["state"]).write_text(
            json.dumps(
                {
                    "phase": phase,
                    "started_at": "2026-08-09T01:00:00Z",
                }
            )
            + "\n"
        )
    sftp._local(layout[corrupt_key]).write_text("{invalid-json\n")
    if stop_requested:
        sftp._local(layout["stop"]).touch()
    monkeypatch.setattr(
        backend,
        "_remote_tmux_status",
        lambda *_args: {
            "exists": True,
            "run_pane_exists": True,
            "running": False,
            "pane_exit_code": 1,
        },
    )

    status = backend.reconcile_remote_run(computer, layout["run_base"], RUN_ID)

    assert status["state"] == expected_state
    assert status["phase"] == phase
    assert status["ended_at"] is None
    assert status["tmux_exists"] is True
    assert status["tmux_running"] is False
    assert status["stop_requested"] is stop_requested
    assert status["record_errors"] == [expected_error]


@pytest.mark.parametrize(
    ("corrupt_key", "record", "live_phase", "expected_phase", "expected_error"),
    (
        ("completion", {}, "running", "running", "completion.json"),
        ("prepare_result", {}, "cloning", "cloning", "prepare-result.json"),
        ("state", {"phase": "running"}, None, None, "state.json"),
    ),
    ids=("completion", "prepare-result", "state"),
)
def test_reconcile_does_not_terminalize_schema_corrupt_lifecycle_metadata(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    corrupt_key: str,
    record: dict[str, Any],
    live_phase: str | None,
    expected_phase: str | None,
    expected_error: str,
) -> None:
    backend, sftp, computer = remote_backend
    layout = backend.create_remote_run_layout(computer, RUN_ID, command="sleep 300")
    if live_phase:
        sftp._local(layout["state"]).write_text(
            json.dumps(
                {
                    "phase": live_phase,
                    "started_at": "2026-08-09T01:00:00Z",
                }
            )
            + "\n"
        )
    sftp._local(layout[corrupt_key]).write_text(json.dumps(record) + "\n")
    monkeypatch.setattr(
        backend,
        "_remote_tmux_status",
        lambda *_args: {
            "exists": True,
            "run_pane_exists": True,
            "running": False,
            "pane_exit_code": 1,
        },
    )

    status = backend.reconcile_remote_run(computer, layout["run_base"], RUN_ID)

    assert status["state"] == "preparing"
    assert status["phase"] == expected_phase
    assert status["ended_at"] is None
    assert status["tmux_exists"] is True
    assert status["tmux_running"] is False
    assert status["stop_requested"] is False
    assert status["record_errors"] == [expected_error]


@pytest.mark.parametrize(
    "state_payload",
    ("{invalid-json\n", json.dumps({"phase": "running"}) + "\n"),
    ids=("syntax", "schema"),
)
def test_reconcile_accepts_valid_completion_despite_corrupt_state(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    state_payload: str,
) -> None:
    backend, sftp, computer = remote_backend
    layout = backend.create_remote_run_layout(computer, RUN_ID, command="exit 0")
    sftp._local(layout["state"]).write_text(state_payload)
    sftp._local(layout["completion"]).write_text(
        json.dumps(
            {
                "exit_code": 0,
                "stop_requested": False,
                "started_at": "2026-08-09T01:00:00Z",
                "ended_at": "2026-08-09T01:00:03Z",
            }
        )
        + "\n"
    )
    monkeypatch.setattr(
        backend,
        "_remote_tmux_status",
        lambda *_args: {
            "exists": True,
            "run_pane_exists": True,
            "running": False,
            "pane_exit_code": 0,
        },
    )

    status = backend.reconcile_remote_run(computer, layout["run_base"], RUN_ID)

    assert status["state"] == "finished"
    assert status["phase"] is None
    assert status["exit_code"] == 0
    assert status["ended_at"] == "2026-08-09T01:00:03Z"
    assert status["record_errors"] == ["state.json"]


def test_interrupt_publishes_stop_intent_before_sending_ctrl_c(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, sftp, computer = remote_backend
    layout = backend.create_remote_run_layout(computer, RUN_ID, command="sleep 300")

    def inspect_stop_before_exec(_client: object, command: str) -> str:
        assert sftp._local(layout["stop"]).is_file()
        assert command.startswith("/bin/bash --noprofile --norc -c ")
        assert "send-keys" in command
        assert f"termroom-run-{RUN_ID}:run.0" in command
        return "sent\n"

    monkeypatch.setattr(backend, "_exec_client", inspect_stop_before_exec)
    result = backend.interrupt_remote_run(computer, layout["run_base"], RUN_ID)
    assert result == {"sent": True, "completed": False}


def test_control_distinguishes_an_absent_layout_from_a_damaged_marker(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, sftp, computer = remote_backend
    run_base = "/scratch/termroom-runs"
    sftp._local(run_base).mkdir(parents=True, mode=0o700)
    sftp._local(run_base).chmod(0o700)
    monkeypatch.setattr(backend, "_exec_client", lambda *_args: "missing\n")

    assert backend.remote_run_layout_exists(computer, run_base, RUN_ID) is False
    assert backend.interrupt_remote_run(computer, run_base, RUN_ID) == {
        "sent": False,
        "completed": False,
        "layout_missing": True,
        "tmux_exists": False,
        "tmux_running": False,
    }
    assert backend.kill_remote_run(computer, run_base, RUN_ID) == {
        "killed": False,
        "completed": False,
        "layout_missing": True,
        "tmux_exists": False,
        "tmux_running": False,
    }

    sftp._local(f"{run_base}/{RUN_ID}").mkdir()
    with pytest.raises(RemoteRunLayoutError, match="layout is incomplete"):
        backend.remote_run_layout_exists(computer, run_base, RUN_ID)
    with pytest.raises(RemoteRunLayoutError, match="layout is incomplete"):
        backend.interrupt_remote_run(computer, run_base, RUN_ID)


def test_poll_reports_missing_layout_as_online_remote_state(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, sftp, computer = remote_backend
    run_base = "/scratch/termroom-runs"
    sftp._local(run_base).mkdir(parents=True, mode=0o700)
    sftp._local(run_base).chmod(0o700)
    monkeypatch.setattr(backend, "_exec_client", lambda *_args: "0|\n")

    status = backend.poll_remote_run(computer, run_base, RUN_ID, offset=37)

    assert status["state"] == "layout_missing"
    assert status["layout_missing"] is True
    assert status["tmux_running"] is True
    assert status["log"]["start_offset"] == 37
    assert status["log"]["next_offset"] == 37
    assert status["log"]["eof"] is True


def test_kill_rereads_completion_after_tmux_kill_race(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, sftp, computer = remote_backend
    layout = backend.create_remote_run_layout(computer, RUN_ID, command="exit 0")

    def complete_while_killing(_client: object, command: str) -> str:
        if "kill-session" in command:
            sftp._local(layout["completion"]).write_text(
                '{"exit_code":0,"stop_requested":false,'
                '"started_at":"2026-08-09T01:00:00Z",'
                '"ended_at":"2026-08-09T01:00:01Z"}\n'
            )
        return ""

    monkeypatch.setattr(backend, "_exec_client", complete_while_killing)

    assert backend.kill_remote_run(computer, layout["run_base"], RUN_ID) == {
        "killed": False,
        "completed": True,
    }


def test_reconcile_does_not_mistake_workspace_shell_for_managed_run_pane(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, sftp, computer = remote_backend
    layout = backend.create_remote_run_layout(computer, RUN_ID, command="sleep 300")
    sftp._local(layout["state"]).write_text(
        '{"phase":"running","started_at":"2026-08-09T01:00:00Z"}\n'
    )
    commands: list[str] = []

    def missing_managed_pane(_client: object, command: str) -> str:
        commands.append(command)
        return "missing-run\n"

    monkeypatch.setattr(backend, "_exec_client", missing_managed_pane)

    status = backend.reconcile_remote_run(computer, layout["run_base"], RUN_ID)

    assert status["state"] == "lost"
    assert status["tmux_exists"] is True
    assert any(f"termroom-run-{RUN_ID}:run.0" in command for command in commands)


def test_safe_delete_checks_marker_and_does_not_follow_child_symlinks(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, sftp, computer = remote_backend
    layout = backend.create_remote_run_layout(computer, RUN_ID, command="exit 0")
    outside = sftp.root / "outside.txt"
    outside.write_text("keep")
    os.symlink(outside, sftp._local(layout["work"]) / "outside-link")
    commands: list[str] = []
    quarantine = sftp._local(
        f"/scratch/termroom-runs/.termroom-deleting-{RUN_ID}"
    )

    def fake_exec(
        _client: object,
        command: str,
        *,
        timeout: float | None = 20,
    ) -> str:
        commands.append(command)
        if "tmux kill-session" in command:
            assert timeout == 20
            assert sftp._local(layout["root"]).exists()
        else:
            assert timeout == 10 * 60
            assert command.startswith("/bin/bash --noprofile --norc -c ")
            assert "cd -- /scratch/termroom-runs" in command
            assert 'test "$(pwd -P)" = /scratch/termroom-runs' in command
            assert f"rm -rf -- .termroom-deleting-{RUN_ID}" in command
            assert quarantine.is_dir()
            shutil.rmtree(quarantine)
        return ""

    monkeypatch.setattr(backend, "_exec_client", fake_exec)

    sftp._local(layout["marker"]).write_text("different\n")
    with pytest.raises(SSHBackendError, match="marker"):
        backend.delete_remote_run_root(computer, layout["run_base"], RUN_ID)
    assert sftp._local(layout["root"]).is_dir()
    assert commands == []

    sftp._local(layout["marker"]).write_text(RUN_ID + "\n")
    result = backend.delete_remote_run_root(computer, layout["run_base"], RUN_ID)
    assert result["deleted"] is True
    assert commands[0] == backend._remote_run_bash_command(
        f"tmux kill-session -t termroom-run-{RUN_ID} 2>/dev/null || true"
    )
    assert len(commands) == 2
    assert not sftp._local(layout["root"]).exists()
    assert outside.read_text() == "keep"


def test_delete_reclaims_markerless_creation_skeleton_after_interruption(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, sftp, computer = remote_backend
    base = backend._canonical_remote_run_base(sftp, str(computer["run_base_dir"]))
    creating = backend._remote_run_creation_paths(base, RUN_ID)
    sftp.mkdir(creating["root"], mode=0o700)
    sftp.mkdir(creating["metadata"], mode=0o700)
    commands: list[str] = []

    def fake_exec(
        _client: object,
        command: str,
        *,
        timeout: float | None = 20,
    ) -> str:
        commands.append(command)
        if "tmux kill-session" not in command:
            assert timeout == 20
            assert command.startswith("/bin/bash --noprofile --norc -c ")
            assert "rmdir -- .termroom" in command
            sftp._local(creating["metadata"]).rmdir()
            sftp._local(creating["root"]).rmdir()
        return ""

    monkeypatch.setattr(backend, "_exec_client", fake_exec)

    result = backend.delete_remote_run_root(computer, base, RUN_ID)

    assert result["deleted"] is True
    assert not sftp._local(creating["root"]).exists()
    assert commands[0] == backend._remote_run_bash_command(
        f"tmux kill-session -t termroom-run-{RUN_ID} 2>/dev/null || true"
    )
    assert len(commands) == 2


def test_delete_resumes_after_metadata_was_removed_before_creation_root(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, sftp, computer = remote_backend
    base = backend._canonical_remote_run_base(sftp, str(computer["run_base_dir"]))
    creating = backend._remote_run_creation_paths(base, RUN_ID)
    sftp.mkdir(creating["root"], mode=0o700)
    commands: list[str] = []

    def fake_exec(
        _client: object,
        command: str,
        *,
        timeout: float | None = 20,
    ) -> str:
        commands.append(command)
        if "tmux kill-session" not in command:
            leaf = f".termroom-creating-{RUN_ID}"
            assert timeout == 20
            assert command.startswith("/bin/bash --noprofile --norc -c ")
            assert f'test -z "$(ls -A -- {leaf})"' in command
            assert f"rmdir -- {leaf}" in command
            sftp._local(creating["root"]).rmdir()
        return ""

    monkeypatch.setattr(backend, "_exec_client", fake_exec)

    result = backend.delete_remote_run_root(computer, base, RUN_ID)

    assert result["deleted"] is True
    assert not sftp._local(creating["root"]).exists()
    assert commands[0] == backend._remote_run_bash_command(
        f"tmux kill-session -t termroom-run-{RUN_ID} 2>/dev/null || true"
    )
    assert len(commands) == 2


def test_delete_reclaims_creation_skeleton_with_atomic_marker_temporary(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, sftp, computer = remote_backend
    base = backend._canonical_remote_run_base(sftp, str(computer["run_base_dir"]))
    creating = backend._remote_run_creation_paths(base, RUN_ID)
    sftp.mkdir(creating["root"], mode=0o700)
    sftp.mkdir(creating["metadata"], mode=0o700)
    marker_temporary = sftp._local(creating["metadata"]) / (
        f".marker.termroom-{uuid.uuid4()}.tmp"
    )
    marker_temporary.write_text(RUN_ID + "\n")
    commands: list[str] = []

    def fake_exec(
        _client: object,
        command: str,
        *,
        timeout: float | None = 20,
    ) -> str:
        commands.append(command)
        if "tmux kill-session" not in command:
            assert timeout == 20
            assert command.startswith("/bin/bash --noprofile --norc -c ")
            if marker_temporary.exists():
                temporary_name = marker_temporary.name
                assert f"test -f {temporary_name}" in command
                assert f"test ! -L {temporary_name}" in command
                assert f"rm -f -- {temporary_name}" in command
                marker_temporary.unlink()
            sftp._local(creating["metadata"]).rmdir()
            sftp._local(creating["root"]).rmdir()
        return ""

    monkeypatch.setattr(backend, "_exec_client", fake_exec)

    result = backend.delete_remote_run_root(computer, base, RUN_ID)

    assert result["deleted"] is True
    assert not sftp._local(creating["root"]).exists()
    assert commands[0] == backend._remote_run_bash_command(
        f"tmux kill-session -t termroom-run-{RUN_ID} 2>/dev/null || true"
    )
    assert len(commands) == 2


def test_reset_staging_does_not_follow_work_tmp_symlink(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, sftp, computer = remote_backend
    layout = backend.create_remote_run_layout(computer, RUN_ID, command="exit 0")
    outside = sftp.root / "outside-work"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep")
    staging = sftp._local(layout["work_staging"])
    os.symlink(outside, staging)
    commands: list[str] = []

    def fake_exec(
        _client: object,
        command: str,
        *,
        timeout: float | None = 20,
    ) -> str:
        commands.append(command)
        assert timeout == 10 * 60
        assert command.startswith("/bin/bash --noprofile --norc -c ")
        assert f"cd -- {layout['root']}" in command
        assert "rm -rf -- work.tmp" in command
        assert staging.is_symlink()
        staging.unlink()
        return ""

    monkeypatch.setattr(backend, "_exec_client", fake_exec)

    with backend.remote_run_snapshot_sink(
        computer, layout["run_base"], RUN_ID
    ):
        assert staging.is_dir()

    assert sentinel.read_text() == "keep"
    assert len(commands) == 1


def test_delete_resumes_after_quarantine_loses_its_internal_marker(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, sftp, computer = remote_backend
    layout = backend.create_remote_run_layout(computer, RUN_ID, command="exit 0")
    quarantine = f"{layout['run_base']}/.termroom-deleting-{RUN_ID}"
    deletion_marker = backend._write_remote_run_deletion_marker(
        sftp, layout["run_base"], RUN_ID
    )
    sftp.rename(layout["root"], quarantine)
    sftp.remove(f"{quarantine}/.termroom/marker")
    commands: list[str] = []

    def finish_delete(
        _client: object,
        command: str,
        *,
        timeout: float | None = 20,
    ) -> str:
        commands.append(command)
        if "tmux kill-session" not in command:
            assert timeout == 10 * 60
            assert command.startswith("/bin/bash --noprofile --norc -c ")
            assert f"cat -- .termroom-deleting-{RUN_ID}.marker" in command
            shutil.rmtree(sftp._local(quarantine))
        return ""

    monkeypatch.setattr(backend, "_exec_client", finish_delete)

    result = backend.delete_remote_run_root(computer, layout["run_base"], RUN_ID)

    assert result["deleted"] is True
    assert not sftp._local(quarantine).exists()
    assert not sftp._local(deletion_marker).exists()
    assert len(commands) == 2


def test_create_finishes_interrupted_quarantine_before_reusing_run_id(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, sftp, computer = remote_backend
    original = backend.create_remote_run_layout(computer, RUN_ID, command="printf old")
    quarantine = f"{original['run_base']}/.termroom-deleting-{RUN_ID}"
    deletion_marker = backend._write_remote_run_deletion_marker(
        sftp, original["run_base"], RUN_ID
    )
    sftp.rename(original["root"], quarantine)

    def finish_delete(
        _client: object,
        command: str,
        *,
        timeout: float | None = 20,
    ) -> str:
        if "tmux kill-session" not in command:
            assert timeout == 10 * 60
            assert command.startswith("/bin/bash --noprofile --norc -c ")
            shutil.rmtree(sftp._local(quarantine))
        return ""

    monkeypatch.setattr(backend, "_exec_client", finish_delete)

    replacement = backend.create_remote_run_layout(
        computer, RUN_ID, command="printf replacement"
    )

    assert sftp._local(replacement["root"]).is_dir()
    assert sftp._local(replacement["command"]).read_text() == "printf replacement\n"
    assert not sftp._local(quarantine).exists()
    assert not sftp._local(deletion_marker).exists()


def test_remote_workspace_source_to_remote_run_sink_uses_tree_contract(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
) -> None:
    backend, sftp, computer = remote_backend
    source_root = sftp._local("/source")
    (source_root / "pkg").mkdir(parents=True)
    source_file = source_root / "pkg" / "run.sh"
    source_file.write_text("#!/bin/sh\nprintf copied\\n")
    source_file.chmod(0o755)
    (source_root / ".git").mkdir()
    (source_root / ".git" / "config").write_text("secret")
    os.symlink("pkg/run.sh", source_root / "entrypoint")
    workspace = {
        "backend_kind": "ssh",
        "computer": computer,
        "remote_path": "/source",
        "canonical_path": "/source",
    }
    layout = backend.create_remote_run_layout(computer, RUN_ID, command="./pkg/run.sh")

    with (
        backend.remote_workspace_snapshot_source(workspace) as source,
        backend.remote_run_snapshot_sink(computer, layout["run_base"], RUN_ID) as sink,
    ):
        manifest = materialize_workspace_snapshot(source, sink, chunk_size=5)
    backend.commit_remote_run_snapshot(computer, layout["run_base"], RUN_ID)

    assert {entry.relative_path for entry in manifest.entries} == {
        "entrypoint",
        "pkg",
        "pkg/run.sh",
    }
    copied = sftp._local(layout["work"]) / "pkg" / "run.sh"
    assert copied.read_text() == source_file.read_text()
    assert copied.stat().st_mode & 0o111
    assert os.readlink(sftp._local(layout["work"]) / "entrypoint") == "pkg/run.sh"
    assert not (sftp._local(layout["work"]) / ".git").exists()


def test_remote_workspace_deep_exclusion_does_not_drop_its_parent_siblings(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
) -> None:
    backend, sftp, computer = remote_backend
    source_root = sftp._local("/source")
    (source_root / "parent" / "excluded").mkdir(parents=True)
    (source_root / "parent" / "excluded" / "secret.txt").write_text("secret")
    (source_root / "parent" / "keep.txt").write_text("keep")
    workspace = {
        "backend_kind": "ssh",
        "computer": computer,
        "remote_path": "/source",
        "canonical_path": "/source",
    }

    manifest = backend.scan_remote_workspace_source(
        workspace, exclusions=("parent/excluded",)
    )

    assert {entry.relative_path for entry in manifest.entries} == {
        "parent",
        "parent/keep.txt",
    }


def test_git_bootstrap_stores_url_as_argv_metadata_not_tmux_shell_text(
    remote_backend: tuple[SSHBackend, _FakeSFTP, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, sftp, computer = remote_backend
    layout = backend.create_remote_run_layout(computer, RUN_ID, command="printf done")
    executed: list[str] = []

    def fake_exec(_client: object, command: str) -> str:
        executed.append(command)
        if "command -v git" in command:
            return "/usr/bin/git\n"
        return ""

    monkeypatch.setattr(backend, "_exec_client", fake_exec)
    parameters = backend.remote_run_git_clone_parameters(
        computer, layout["run_base"], RUN_ID
    )
    url = "https://github.com/example/public-repo.git"
    invocation = build_public_git_clone_invocation(url, **parameters)
    backend.start_remote_git_run(computer, layout["run_base"], RUN_ID, invocation)

    tmux_command = executed[-1]
    assert all(
        command.startswith("/bin/bash --noprofile --norc -c ")
        for command in executed
    )
    assert "respawn-pane" in tmux_command
    assert url not in tmux_command
    argv_metadata = sftp._local(layout["git_argv"]).read_bytes().split(b"\x00")
    assert url.encode() in argv_metadata
    assert sftp._local(layout["git_url"]).read_text() == url + "\n"
    bootstrap = sftp._local(layout["git_bootstrap"]).read_text()
    assert '"${clone_argv[@]}"' in bootstrap
    assert url not in bootstrap


def test_git_bootstrap_publishes_running_after_commit_before_runner_exec() -> None:
    script = REMOTE_GIT_BOOTSTRAP_SCRIPT
    commit = script.index('mv -- "$run_root/work.tmp" "$run_root/work"')
    running_record = script.index(
        'printf \'{"phase":"running","started_at":"%s"}\\n\'',
        commit,
    )
    atomic_publish = script.index(
        '| atomic_record "$meta_dir/state.json"',
        running_record,
    )
    runner_exec = script.index(
        'exec /bin/bash --noprofile --norc "$meta_dir/runner.sh"',
        atomic_publish,
    )

    assert commit < running_record < atomic_publish < runner_exec
    publish_block = script[running_record:runner_exec]
    assert "prepare_result failed state_publish_failed" in publish_block
    assert "exit 120" in publish_block
