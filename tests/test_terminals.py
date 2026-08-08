from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from termroom.db import StateStore
from termroom.terminals import (
    TerminalManager,
    TerminalOutputDecoder,
    normalize_terminal_name,
    terminal_size,
)
from termroom.workspaces import RootManager, WorkspaceManager


def test_terminal_names_preserve_unicode_and_sanitize_shell_unsafe_characters() -> None:
    assert normalize_terminal_name("크롤러 로그") == "크롤러-로그"
    assert normalize_terminal_name("  빌드_1.2  ") == "빌드_1.2"
    assert normalize_terminal_name("***") == "shell"


def test_terminal_resize_payload_is_clamped_and_invalid_values_are_ignored() -> None:
    assert terminal_size({"rows": -100, "cols": 999999}) == (4, 1000)
    assert terminal_size({"rows": "41", "cols": "123"}) == (41, 123)
    assert terminal_size({"rows": "not-a-number", "cols": 80}) is None


def test_terminal_output_decoder_preserves_multibyte_characters_across_chunks() -> None:
    encoded = "한글🙂".encode()
    decoder = TerminalOutputDecoder()

    pieces = [
        decoder.feed(encoded[:1]),
        decoder.feed(encoded[1:4]),
        decoder.feed(encoded[4:7]),
        decoder.feed(encoded[7:9]),
        decoder.feed(encoded[9:]),
        decoder.feed(b"", final=True),
    ]

    assert "".join(pieces) == "한글🙂"


def test_tmux_commands_do_not_inherit_termroom_or_legacy_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setenv("TERMROOM_PASSWORD", "do-not-leak")
    monkeypatch.setattr(subprocess, "run", fake_run)

    TerminalManager._run_tmux("display-message", "ok")

    environment = captured["env"]
    assert isinstance(environment, dict)
    assert "TERMROOM_PASSWORD" not in environment


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_tmux_session_survives_detached_commands(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = WorkspaceManager(RootManager(tmp_path), store).open("project")
    manager = TerminalManager(store)

    try:
        terminal = manager.ensure_workspace(workspace)[0]
        subprocess.run(
            [
                "tmux",
                "send-keys",
                "-t",
                terminal["tmux_window"],
                "printf 'TERMROOM_TMUX_TEST\\n'",
                "Enter",
            ],
            check=True,
        )
        time.sleep(0.2)

        assert manager.session_exists(workspace["tmux_session"])
        assert workspace["tmux_session"] in manager.existing_sessions()
        assert "TERMROOM_TMUX_TEST" in manager.capture_scrollback(workspace, terminal)
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", workspace["tmux_session"]],
            check=False,
            capture_output=True,
        )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_tmux_client_tracks_pty_resize(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = WorkspaceManager(RootManager(tmp_path), store).open("project")
    manager = TerminalManager(store)
    process_pid = -1
    master_fd = -1

    try:
        manager.ensure_workspace(workspace)
        process_pid, master_fd = manager._spawn_tmux_client(workspace)
        manager._set_window_size(master_fd, rows=40, cols=120)
        os.killpg(process_pid, signal.SIGWINCH)

        deadline = time.monotonic() + 2
        client_size = ""
        while time.monotonic() < deadline:
            result = subprocess.run(
                [
                    "tmux",
                    "list-clients",
                    "-t",
                    workspace["tmux_session"],
                    "-F",
                    "#{client_width}x#{client_height}",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            client_size = result.stdout.strip()
            if client_size == "120x40":
                break
            time.sleep(0.05)

        assert client_size == "120x40"
    finally:
        if process_pid > 0:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process_pid, signal.SIGTERM)
            manager._wait_for_pid(process_pid, 1)
        if master_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(master_fd)
        subprocess.run(
            ["tmux", "kill-session", "-t", workspace["tmux_session"]],
            check=False,
            capture_output=True,
        )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_terminal_can_be_renamed_and_closed_without_losing_workspace(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = WorkspaceManager(RootManager(tmp_path), store).open("project")
    manager = TerminalManager(store)

    try:
        first = manager.ensure_workspace(workspace)[0]
        renamed = manager.rename_terminal(workspace, first, "worker one")
        assert renamed["name"] == "worker-one"
        assert manager._list_tmux_windows(workspace["tmux_session"])[0][1] == "worker-one"

        second = manager.create_terminal(workspace, "logs")
        remaining = manager.close_terminal(workspace, second)
        assert [item["id"] for item in remaining] == [renamed["id"]]

        replacement = manager.close_terminal(workspace, renamed)
        assert len(replacement) == 1
        assert replacement[0]["id"] != renamed["id"]
        assert manager.session_exists(workspace["tmux_session"])
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", workspace["tmux_session"]],
            check=False,
            capture_output=True,
        )
