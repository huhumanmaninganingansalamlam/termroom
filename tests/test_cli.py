from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from termroom import cli
from termroom.config import Settings
from termroom.db import StateStore
from termroom.terminals import TerminalManager
from termroom.workspaces import RootManager, WorkspaceManager


def test_cli_reports_missing_tmux_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(cli.shutil, "which", lambda command: None)
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exc:
        cli._require_tmux(parser)

    assert exc.value.code == 2
    assert "tmux is required for persistent Termroom terminals" in capsys.readouterr().err


def test_termroom_dot_reuses_running_core_and_registers_new_root(
    tmp_path: Path, capsys
) -> None:  # type: ignore[no-untyped-def]
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    state_dir = tmp_path / "state"
    settings = Settings.create(
        first,
        state_dir=state_dir,
        access_token="admin-token",
    )
    store = StateStore(settings.database_path)
    store.initialize()
    first_manager = WorkspaceManager(RootManager(first), store)
    first_manager.open(".")
    cli._write_core_metadata(settings, "existing-workspace")

    created_session: str | None = None
    try:
        cli.main([str(second), "--state-dir", str(state_dir), "--no-open"])
        manager_from_first_core = WorkspaceManager(RootManager(first), store)
        matches = [item for item in manager_from_first_core.list_recent() if item["path"] == second]
        assert len(matches) == 1
        created_session = str(matches[0]["tmux_session"])
        assert "Termroom Core is already running" in capsys.readouterr().out
    finally:
        if created_session:
            with contextlib.suppress(Exception):
                TerminalManager(store)._run_tmux("kill-session", "-t", created_session)
        metadata = cli._read_core_metadata(state_dir)
        if metadata and int(metadata.get("pid", -1)) == os.getpid():
            with contextlib.suppress(FileNotFoundError):
                (state_dir / "core.json").unlink()


def test_termroom_dot_starts_background_core_and_stop_core(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    dotenv = project / ".env"
    dotenv.write_text("TERMROOM_PASSWORD=test-password\n", encoding="utf-8")
    dotenv.chmod(0o600)
    state_dir = tmp_path / "state"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])

    command = [
        sys.executable,
        "-m",
        "termroom.cli",
        str(project),
        "--state-dir",
        str(state_dir),
        "--port",
        str(port),
        "--no-open",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=12, check=False)
    assert result.returncode == 0, result.stderr
    assert "started in the background" in result.stdout

    deadline = time.monotonic() + 5
    health = ""
    try:
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(  # noqa: S310 - loopback test server only
                    f"http://127.0.0.1:{port}/health", timeout=0.5
                ) as response:
                    health = response.read().decode()
                    break
            except OSError:
                time.sleep(0.05)
        assert health == "ok"
    finally:
        stop = subprocess.run(
            [
                sys.executable,
                "-m",
                "termroom.cli",
                "stop",
                "--core",
                "--state-dir",
                str(state_dir),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert stop.returncode == 0, stop.stderr


def test_termroom_dot_restarts_core_when_runtime_fingerprint_is_stale(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    dotenv = state_dir / ".env"
    dotenv.write_text("TERMROOM_PASSWORD=test-password\n", encoding="utf-8")
    dotenv.chmod(0o600)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])

    base_command = [
        sys.executable,
        "-m",
        "termroom.cli",
        str(project),
        "--state-dir",
        str(state_dir),
        "--port",
        str(port),
        "--no-open",
    ]
    first = subprocess.run(base_command, capture_output=True, text=True, timeout=12, check=False)
    assert first.returncode == 0, first.stderr
    metadata = cli._read_core_metadata(state_dir)
    assert metadata
    old_pid = int(metadata["pid"])
    metadata["runtime_fingerprint"] = "stale-runtime"
    (state_dir / "core.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )

    try:
        restarted = subprocess.run(
            base_command, capture_output=True, text=True, timeout=12, check=False
        )
        assert restarted.returncode == 0, restarted.stderr
        assert "using older code; restarting it" in restarted.stdout
        new_metadata = cli._read_core_metadata(state_dir)
        assert new_metadata
        assert int(new_metadata["pid"]) != old_pid
        assert cli._core_runtime_matches(new_metadata)
    finally:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "termroom.cli",
                "stop",
                "--core",
                "--state-dir",
                str(state_dir),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )


def test_termroom_dot_restarts_core_when_default_locale_changes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    dotenv = state_dir / ".env"
    dotenv.write_text(
        "TERMROOM_PASSWORD=test-password\nTERMROOM_LOCALE=en\n", encoding="utf-8"
    )
    dotenv.chmod(0o600)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])

    command = [
        sys.executable,
        "-m",
        "termroom.cli",
        str(project),
        "--state-dir",
        str(state_dir),
        "--port",
        str(port),
        "--no-open",
    ]
    first = subprocess.run(command, capture_output=True, text=True, timeout=12, check=False)
    assert first.returncode == 0, first.stderr
    first_metadata = cli._read_core_metadata(state_dir)
    assert first_metadata
    old_pid = int(first_metadata["pid"])
    assert first_metadata["default_locale"] == "en"

    dotenv.write_text(
        "TERMROOM_PASSWORD=test-password\nTERMROOM_LOCALE=ko\n", encoding="utf-8"
    )
    dotenv.chmod(0o600)

    try:
        restarted = subprocess.run(command, capture_output=True, text=True, timeout=12, check=False)
        assert restarted.returncode == 0, restarted.stderr
        assert "using updated settings; restarting it" in restarted.stdout
        new_metadata = cli._read_core_metadata(state_dir)
        assert new_metadata
        assert int(new_metadata["pid"]) != old_pid
        assert new_metadata["default_locale"] == "ko"
    finally:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "termroom.cli",
                "stop",
                "--core",
                "--state-dir",
                str(state_dir),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
