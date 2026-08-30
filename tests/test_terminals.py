from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from termroom.db import StateStore
from termroom.terminals import (
    TMUX_TERMINAL_EDITOR_RECORD_FORMAT,
    TMUX_TERMINAL_RECORD_FORMAT,
    TerminalManager,
    TerminalOutputDecoder,
    normalize_terminal_editor_path,
    normalize_terminal_name,
    parse_tmux_terminal_editor_records,
    parse_tmux_terminal_records,
    terminal_editor_digest,
    terminal_size,
)
from termroom.workspace_usage import WorkspaceUsageStale
from termroom.workspaces import RootManager, WorkspaceManager


def test_terminal_names_preserve_unicode_and_sanitize_shell_unsafe_characters() -> None:
    assert normalize_terminal_name("크롤러 로그") == "크롤러-로그"
    assert normalize_terminal_name("  빌드_1.2  ") == "빌드_1.2"
    assert normalize_terminal_name("***") == "shell"


def test_terminal_editor_paths_and_tmux_records_are_bounded() -> None:
    assert normalize_terminal_editor_path("src/한글 file.py") == "src/한글 file.py"
    digest = terminal_editor_digest("src/한글 file.py")
    assert parse_tmux_terminal_editor_records(f"@7|0|{digest}\n@8|0|\n") == [
        {"tmux_window": "@7", "dead": False, "digest": digest}
    ]
    with pytest.raises(ValueError, match="file path is invalid"):
        normalize_terminal_editor_path("../secret")
    with pytest.raises(ValueError, match="invalid Terminal editor digest"):
        parse_tmux_terminal_editor_records("@7|0|not-a-digest\n")


@pytest.mark.skipif(
    shutil.which("tmux") is None
    or not any(shutil.which(editor) for editor in ("nvim", "vim", "vi")),
    reason="tmux and a Vim-compatible editor are required",
)
def test_terminal_editor_opens_file_once_and_reuses_live_tmux_window(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "hello world.txt"
    source.write_text("hello\n", encoding="utf-8")
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = WorkspaceManager(RootManager(tmp_path), store).open("project")
    manager = TerminalManager(store)
    try:
        first = manager.open_terminal_editor(workspace, "hello world.txt")
        second = manager.open_terminal_editor(workspace, "hello world.txt")
        assert first["id"] == second["id"]
        assert first["tmux_window"] == second["tmux_window"]
        record = manager._run_tmux(
            "display-message",
            "-p",
            "-t",
            str(first["tmux_window"]),
            TMUX_TERMINAL_EDITOR_RECORD_FORMAT,
        ).stdout.strip()
        assert record.endswith(terminal_editor_digest("hello world.txt"))
    finally:
        manager._run_tmux(
            "kill-session",
            "-t",
            str(workspace["tmux_session"]),
            check=False,
        )


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


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
async def test_local_grid_client_lookup_does_not_block_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = WorkspaceManager(RootManager(tmp_path), store).open("project")
    manager = TerminalManager(store)
    terminal = manager.ensure_workspace(workspace)[0]

    class ResizeWebSocket:
        def __init__(self) -> None:
            self.messages = [
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {
                            "kind": "input",
                            "data": "",
                            "rows": 37,
                            "cols": 111,
                            "user_input": True,
                        }
                    ),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ]

        async def receive(self) -> dict[str, object]:
            return self.messages.pop(0)

        async def send_text(self, _value: str) -> None:
            return None

        async def close(self, *, code: int, reason: str) -> None:
            raise AssertionError((code, reason))

    helper_done = threading.Event()

    def slow_missing_client(_view_session: str, *, enabled: bool) -> bool:
        assert enabled
        time.sleep(0.2)
        helper_done.set()
        return False

    monkeypatch.setattr(manager, "_set_browser_view_grid_resize", slow_missing_client)

    async def ticker() -> float:
        loop = asyncio.get_running_loop()
        ticks = [loop.time()]
        while not helper_done.is_set():
            await asyncio.sleep(0.01)
            ticks.append(loop.time())
        return max(
            (current - previous for previous, current in zip(ticks, ticks[1:], strict=False)),
            default=0,
        )

    ticker_task = asyncio.create_task(ticker())
    try:
        await manager.bridge(
            ResizeWebSocket(),  # type: ignore[arg-type]
            workspace,
            terminal,
        )
        assert helper_done.is_set()
        assert await ticker_task < 0.1
    finally:
        if not ticker_task.done():
            ticker_task.cancel()
        manager._run_tmux("kill-session", "-t", str(workspace["tmux_session"]), check=False)


def test_managed_terminal_row_survives_tmux_window_id_drift(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "project").mkdir(parents=True)
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = WorkspaceManager(RootManager(root), store).open("project")
    workspace_id = str(workspace["id"])
    shell = store.create_terminal(workspace_id, "shell", "@1")
    managed = store.create_terminal(
        workspace_id,
        "Run",
        "@2",
        role="file_run",
        managed_run_id="old-run",
    )

    reconciled = store.reconcile_terminals(
        workspace_id,
        [
            {
                "tmux_window": "@1",
                "name": "Run",
                "role": "file_run",
                "managed_run_id": "new-run",
            },
            {
                "tmux_window": "@2",
                "name": "shell",
                "role": "shell",
                "managed_run_id": None,
            },
        ],
    )

    by_role = {str(item["role"]): item for item in reconciled}
    assert by_role["file_run"]["id"] == managed["id"]
    assert by_role["file_run"]["tmux_window"] == "@1"
    assert by_role["file_run"]["managed_run_id"] == "new-run"
    assert by_role["shell"]["id"] == shell["id"]
    assert by_role["shell"]["tmux_window"] == "@2"


def test_tmux_terminal_records_preserve_printable_delimiters_in_names() -> None:
    records = parse_tmux_terminal_records(
        "termroom-project|@7|1700000123|worker|with|pipes|file_run|run-123\n"
    )

    assert records == [
        {
            "tmux_window": "@7",
            "tmux_session": "termroom-project",
            "name": "worker|with|pipes",
            "role": "file_run",
            "managed_run_id": "run-123",
            "activity_at": 1700000123,
        }
    ]


@pytest.mark.parametrize(
    "record",
    (
        "termroom-project|@7||shell|shell|",
        "termroom-project|@7|-1|shell|shell|",
        "termroom-project|@7|not-a-revision|shell|shell|",
    ),
)
def test_tmux_terminal_records_reject_invalid_activity_revisions(record: str) -> None:
    with pytest.raises(ValueError, match="invalid Terminal record"):
        parse_tmux_terminal_records(record)


def test_legacy_tmux_terminal_records_remain_compatible_without_activity() -> None:
    assert parse_tmux_terminal_records("@7|shell|shell|\n") == [
        {
            "tmux_window": "@7",
            "tmux_session": None,
            "activity_at": None,
            "name": "shell",
            "role": "shell",
            "managed_run_id": None,
        }
    ]


def test_terminal_activity_refresh_batches_local_sessions_without_running_tmux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    (root / "first").mkdir(parents=True)
    (root / "second").mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace_manager = WorkspaceManager(RootManager(root), store)
    first = workspace_manager.open("first")
    second = workspace_manager.open("second")
    first_terminal = store.create_terminal(str(first["id"]), "shell", "@1")
    second_terminal = store.create_terminal(str(second["id"]), "logs", "@2")
    manager = TerminalManager(store)
    calls: list[tuple[tuple[str, ...], bool]] = []

    def fake_tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append((args, check))
        return subprocess.CompletedProcess(
            ["tmux", *args],
            0,
            (
                f"{first['tmux_session']}|@1|100|shell|shell|\n"
                f"{second['tmux_session']}|@2|200|logs|shell|\n"
                "unrelated-session|@9|999|ignore|shell|\n"
            ),
            "",
        )

    monkeypatch.setattr(manager, "_run_tmux", fake_tmux)

    refreshed = manager.refresh_activity([first, second])

    assert calls == [(("list-windows", "-a", "-F", TMUX_TERMINAL_RECORD_FORMAT), False)]
    assert refreshed[str(first["id"])][0]["id"] == first_terminal["id"]
    assert refreshed[str(first["id"])][0]["activity_at"] == 100
    assert refreshed[str(second["id"])][0]["id"] == second_terminal["id"]
    assert refreshed[str(second["id"])][0]["activity_at"] == 200


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
        deadline = time.monotonic() + 2
        scrollback = ""
        while time.monotonic() < deadline:
            scrollback = manager.capture_scrollback(workspace, terminal)
            if "TERMROOM_TMUX_TEST" in scrollback:
                break
            time.sleep(0.05)

        assert manager.session_exists(workspace["tmux_session"])
        assert workspace["tmux_session"] in manager.existing_sessions()
        assert "TERMROOM_TMUX_TEST" in scrollback
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", workspace["tmux_session"]],
            check=False,
            capture_output=True,
        )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_styled_history_capture_preserves_tmux_sgr_attributes(tmp_path: Path) -> None:
    marker = "TERMROOM_ANSI_HISTORY"
    done = "TERMROOM_ANSI_DONE"
    project = tmp_path / "project"
    project.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = WorkspaceManager(RootManager(tmp_path), store).open("project")
    manager = TerminalManager(store)

    try:
        terminal = manager.ensure_workspace(workspace)[0]
        ready = "TERMROOM_ANSI_READY"
        subprocess.run(
            [
                "tmux",
                "send-keys",
                "-t",
                terminal["tmux_window"],
                "printf 'TERMROOM_ANSI_%s\\n' READY",
                "Enter",
            ],
            check=True,
        )
        ready_deadline = time.monotonic() + 8
        ready_scrollback = ""
        while time.monotonic() < ready_deadline:
            ready_scrollback = manager.capture_scrollback(workspace, terminal)
            if ready in ready_scrollback.splitlines():
                break
            time.sleep(0.05)
        assert ready in ready_scrollback.splitlines()

        subprocess.run(
            [
                "tmux",
                "send-keys",
                "-t",
                terminal["tmux_window"],
                (
                    "printf '\\033[1;38;5;196;48;5;25m"
                    f"{marker}"
                    "\\033[0m\\n'; seq 1 80; "
                    f"printf '{done}\\n'"
                ),
                "Enter",
            ],
            check=True,
        )
        full = ""
        styled = ""
        plain = ""
        for _ in range(5):
            full = manager.capture_scrollback(workspace, terminal)
            styled = manager.capture_scrollback(
                workspace,
                terminal,
                history_only=True,
                ansi=True,
            )
            plain = manager.capture_scrollback(
                workspace,
                terminal,
                history_only=True,
            )
            if (
                done in full.splitlines()
                and marker in styled
                and marker in plain
            ):
                break
        assert done in full.splitlines()
        assert marker in styled
        assert marker in plain
        assert "\x1b[" in styled
        assert "38;5;196" in styled
        assert "48;5;25" in styled
        assert "\x1b[" not in plain
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", workspace["tmux_session"]],
            check=False,
            capture_output=True,
        )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_history_only_scrollback_excludes_the_live_tmux_viewport(tmp_path: Path) -> None:
    old_marker = "HISTORY_ONLY_OLD"
    live_marker = "HISTORY_ONLY_LIVE"
    project = tmp_path / "project"
    project.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = WorkspaceManager(RootManager(tmp_path), store).open("project")
    manager = TerminalManager(store)

    try:
        terminal = manager.ensure_workspace(workspace)[0]
        ready = "HISTORY_ONLY_READY"
        subprocess.run(
            [
                "tmux",
                "send-keys",
                "-t",
                terminal["tmux_window"],
                "printf 'HISTORY_ONLY_%s\\n' READY",
                "Enter",
            ],
            check=True,
        )
        ready_deadline = time.monotonic() + 8
        ready_scrollback = ""
        while time.monotonic() < ready_deadline:
            ready_scrollback = manager.capture_scrollback(workspace, terminal)
            if ready in ready_scrollback.splitlines():
                break
            time.sleep(0.05)
        assert ready in ready_scrollback.splitlines()

        subprocess.run(
            [
                "tmux",
                "resize-window",
                "-t",
                terminal["tmux_window"],
                "-x",
                "80",
                "-y",
                "6",
            ],
            check=True,
        )
        subprocess.run(
            [
                "tmux",
                "send-keys",
                "-t",
                terminal["tmux_window"],
                (
                    "printf 'HISTORY_ONLY_OLD\\n'; "
                    "seq -f 'HISTORY_ONLY_%02g' 1 24; "
                    "printf 'HISTORY_ONLY_%s\\n' LIVE"
                ),
                "Enter",
            ],
            check=True,
        )
        full = ""
        history = ""
        for _ in range(5):
            full = manager.capture_scrollback(workspace, terminal)
            history = manager.capture_scrollback(
                workspace,
                terminal,
                history_only=True,
            )
            if (
                live_marker in full.splitlines()
                and old_marker in history.splitlines()
            ):
                break
        assert old_marker in history.splitlines()
        assert live_marker in full.splitlines()
        assert live_marker not in history.splitlines()
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", workspace["tmux_session"]],
            check=False,
            capture_output=True,
        )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_local_workspace_usage_tracks_tmux_descendants(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = WorkspaceManager(RootManager(tmp_path), store).open("project")
    manager = TerminalManager(store)

    try:
        terminal = manager.ensure_workspace(workspace)[0]
        subprocess.run(
            ["tmux", "send-keys", "-t", terminal["tmux_window"], "sleep 30", "Enter"],
            check=True,
        )
        deadline = time.monotonic() + 2
        while True:
            usage = manager.workspace_usage(workspace)
            if usage.process_count >= 2 or time.monotonic() >= deadline:
                break
            time.sleep(0.05)

        assert usage.process_count >= 2
        assert usage.memory_bytes > 0
        assert usage.cpu_percent >= 0

        subprocess.run(
            ["tmux", "kill-session", "-t", workspace["tmux_session"]],
            check=True,
        )
        with pytest.raises(WorkspaceUsageStale):
            manager.workspace_usage(workspace)
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
