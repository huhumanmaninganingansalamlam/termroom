from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from termroom.db import StateStore
from termroom.node_agent import NodeRuntime
from termroom.terminals import (
    TerminalManager,
    set_tmux_browser_view_grid_resize,
    terminal_input_claims_grid,
    tmux_browser_view_session,
)
from termroom.workspaces import RootManager, WorkspaceManager


def _wait_for_window_size(manager: TerminalManager, target: str, expected: str) -> None:
    deadline = time.monotonic() + 2
    current = ""
    while time.monotonic() < deadline:
        current = manager._run_tmux(
            "display-message",
            "-p",
            "-t",
            target,
            "#{window_width}x#{window_height}",
        ).stdout.strip()
        if current == expected:
            return
        time.sleep(0.02)
    raise AssertionError(f"expected tmux window {expected}, got {current}")


def _wait_for_client_size(manager: TerminalManager, view_session: str, expected: str) -> None:
    deadline = time.monotonic() + 2
    current = ""
    while time.monotonic() < deadline:
        current = manager._run_tmux(
            "list-clients",
            "-t",
            view_session,
            "-F",
            "#{client_width}x#{client_height}",
        ).stdout.strip()
        if current == expected:
            return
        time.sleep(0.02)
    raise AssertionError(f"expected tmux client {expected}, got {current}")


def _client_flags(manager: TerminalManager, view_session: str) -> set[str]:
    value = manager._run_tmux(
        "list-clients",
        "-t",
        view_session,
        "-F",
        "#{client_flags}",
    ).stdout.strip()
    return set(value.split(","))


def test_only_explicit_real_user_terminal_data_claims_grid() -> None:
    assert not terminal_input_claims_grid({})
    assert terminal_input_claims_grid({"user_input": True})
    assert not terminal_input_claims_grid({"user_input": "true"})
    assert not terminal_input_claims_grid({"user_input": "false"})
    assert not terminal_input_claims_grid({"user_input": False})


def test_local_grid_promotion_allows_peer_that_disappears_during_demotion() -> None:
    listings = 0
    calls: list[tuple[str, ...]] = []

    def run_tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        nonlocal listings
        calls.append(args)
        if args[0] == "list-clients":
            listings += 1
            output = (
                "peer\ttermroom-view-peer\t@1\n"
                "target\ttermroom-view-target\t@1\n"
                if listings == 1
                else "target\ttermroom-view-target\t@1\n"
            )
            return subprocess.CompletedProcess(["tmux", *args], 0, output, "")
        if args[:3] == ("refresh-client", "-t", "peer"):
            return subprocess.CompletedProcess(["tmux", *args], 1, "", "gone")
        if args[:3] == ("refresh-client", "-t", "target"):
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        raise AssertionError(args)

    assert set_tmux_browser_view_grid_resize(
        run_tmux,
        "termroom-view-target",
        enabled=True,
    )
    assert calls == [
        ("list-clients", "-F", "#{client_name}\t#{session_name}\t#{window_id}"),
        ("refresh-client", "-t", "peer", "-f", "ignore-size"),
        ("list-clients", "-F", "#{client_name}\t#{session_name}\t#{window_id}"),
        ("refresh-client", "-t", "target", "-f", "!ignore-size"),
    ]


def test_local_grid_owner_is_forgotten_only_after_demotion_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    manager = TerminalManager(store)
    terminal_id = "terminal"
    client_id = manager.control.register(terminal_id)
    manager.control.mark_input(terminal_id, client_id)
    manager._browser_grid_owners[terminal_id] = client_id
    outcomes = iter((False, True))

    monkeypatch.setattr(
        manager,
        "_set_browser_view_grid_resize",
        lambda _view_session, *, enabled: next(outcomes) if not enabled else True,
    )

    assert not manager._sync_browser_grid_role(
        terminal_id,
        client_id,
        tmux_browser_view_session(client_id),
        enabled=False,
    )
    assert manager._browser_grid_owners[terminal_id] == client_id

    assert manager._sync_browser_grid_role(
        terminal_id,
        client_id,
        tmux_browser_view_session(client_id),
        enabled=False,
    )
    assert terminal_id not in manager._browser_grid_owners


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_local_workspace_recovers_missing_canonical_session_from_browser_view(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = WorkspaceManager(RootManager(tmp_path), store).open("project")
    manager = TerminalManager(store)
    stale_view = tmux_browser_view_session(uuid.uuid4().hex)

    try:
        original = manager.ensure_workspace(workspace)[0]
        terminal_id = str(original["id"])
        previous_owner = manager.control.register(terminal_id)
        manager.control.mark_input(terminal_id, previous_owner)
        manager.control.unregister(terminal_id, previous_owner)
        manager._prepare_browser_view(workspace, original, stale_view)
        original_window = str(original["tmux_window"])
        manager._run_tmux(
            "kill-session", "-t", str(workspace["tmux_session"]), check=False
        )
        assert manager._run_tmux(
            "has-session", "-t", stale_view, check=False
        ).returncode == 0

        terminal = manager.ensure_workspace(workspace)[0]
        assert terminal["tmux_window"] == original_window
        assert manager._run_tmux(
            "has-session", "-t", stale_view, check=False
        ).returncode != 0
        passive = manager.control.register(terminal_id)
        assert not manager.control.can_resize(terminal_id, passive)
        manager.control.unregister(terminal_id, passive)
    finally:
        manager._run_tmux("kill-session", "-t", stale_view, check=False)
        manager._run_tmux(
            "kill-session", "-t", str(workspace["tmux_session"]), check=False
        )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_new_local_tmux_session_bootstraps_the_first_browser_grid(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = WorkspaceManager(RootManager(tmp_path), store).open("project")
    manager = TerminalManager(store)
    active_view = ""
    process: tuple[int, int] | None = None

    try:
        terminal = manager.ensure_workspace(workspace)[0]

        terminal_id = str(terminal["id"])
        first = manager.control.register(terminal_id)
        observer = manager.control.register(terminal_id)
        active_view = tmux_browser_view_session(first)
        manager._prepare_browser_view(workspace, terminal, active_view)
        process = manager._spawn_tmux_client(workspace, active_view)

        assert manager.control.resize_plan(
            terminal_id, first, rows=33, cols=162
        ) == (True, True)
        assert not manager.control.can_resize(terminal_id, observer)
        assert manager._sync_browser_grid_role(
            terminal_id,
            first,
            active_view,
            enabled=True,
        )
        manager._set_window_size(process[1], rows=33, cols=162)
        os.killpg(process[0], signal.SIGWINCH)
        _wait_for_window_size(manager, str(terminal["tmux_window"]), "162x32")

        manager.control.unregister(terminal_id, first)
        manager.control.unregister(terminal_id, observer)

        restarted_manager = TerminalManager(store)
        existing = restarted_manager.ensure_workspace(workspace)[0]
        passive = restarted_manager.control.register(str(existing["id"]))
        assert not restarted_manager.control.can_resize(str(existing["id"]), passive)
    finally:
        if process is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process[0], signal.SIGTERM)
            manager._wait_for_pid(process[0], 1.0)
            with contextlib.suppress(OSError):
                os.close(process[1])
        if active_view:
            manager._run_tmux("kill-session", "-t", active_view, check=False)
        manager._run_tmux(
            "kill-session", "-t", str(workspace["tmux_session"]), check=False
        )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_local_browser_views_share_windows_without_sharing_current_selection(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = WorkspaceManager(RootManager(tmp_path), store).open("project")
    manager = TerminalManager(store)
    first = manager.ensure_workspace(workspace)[0]
    second = manager.create_terminal(workspace, "logs")
    first_view = tmux_browser_view_session(uuid.uuid4().hex)
    second_view = tmux_browser_view_session(uuid.uuid4().hex)

    try:
        manager._prepare_browser_view(workspace, first, first_view)
        manager._prepare_browser_view(workspace, second, second_view)

        canonical_window = manager._run_tmux(
            "display-message",
            "-p",
            "-t",
            str(workspace["tmux_session"]),
            "#{window_id}",
        ).stdout.strip()
        first_window = manager._run_tmux(
            "display-message", "-p", "-t", first_view, "#{window_id}"
        ).stdout.strip()
        second_window = manager._run_tmux(
            "display-message", "-p", "-t", second_view, "#{window_id}"
        ).stdout.strip()

        assert canonical_window == first["tmux_window"]
        assert first_window == first["tmux_window"]
        assert second_window == second["tmux_window"]
        assert {
            row.split("|", 1)[0]
            for row in manager._run_tmux(
                "list-windows", "-t", first_view, "-F", "#{window_id}|#{window_name}"
            ).stdout.splitlines()
        } == {first["tmux_window"], second["tmux_window"]}
    finally:
        manager._run_tmux("kill-session", "-t", first_view, check=False)
        manager._run_tmux("kill-session", "-t", second_view, check=False)
        manager._run_tmux("kill-session", "-t", str(workspace["tmux_session"]), check=False)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_local_passive_attach_does_not_replace_input_owner_grid(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = WorkspaceManager(RootManager(tmp_path), store).open("project")
    manager = TerminalManager(store)
    terminal = manager.ensure_workspace(workspace)[0]
    active_view = tmux_browser_view_session(uuid.uuid4().hex)
    passive_view = tmux_browser_view_session(uuid.uuid4().hex)
    processes: list[tuple[int, int]] = []

    try:
        manager._prepare_browser_view(workspace, terminal, active_view)
        active_process = manager._spawn_tmux_client(workspace, active_view)
        processes.append(active_process)
        assert manager._set_browser_view_grid_resize(active_view, enabled=True)
        manager._set_window_size(active_process[1], rows=37, cols=111)
        os.killpg(active_process[0], signal.SIGWINCH)
        _wait_for_window_size(manager, str(terminal["tmux_window"]), "111x36")

        manager._prepare_browser_view(workspace, terminal, passive_view)
        passive_process = manager._spawn_tmux_client(workspace, passive_view)
        processes.append(passive_process)
        manager._set_window_size(passive_process[1], rows=28, cols=51)
        os.killpg(passive_process[0], signal.SIGWINCH)
        _wait_for_client_size(manager, passive_view, "51x28")
        _wait_for_window_size(manager, str(terminal["tmux_window"]), "111x36")

        assert manager._set_browser_view_grid_resize(passive_view, enabled=True)
        assert "ignore-size" in _client_flags(manager, active_view)
        assert "ignore-size" not in _client_flags(manager, passive_view)
        manager._set_window_size(passive_process[1], rows=28, cols=51)
        os.killpg(passive_process[0], signal.SIGWINCH)
        _wait_for_window_size(manager, str(terminal["tmux_window"]), "51x27")

        assert manager._set_browser_view_grid_resize(active_view, enabled=False)
        manager._set_window_size(active_process[1], rows=29, cols=77)
        os.killpg(active_process[0], signal.SIGWINCH)
        _wait_for_client_size(manager, active_view, "77x29")
        _wait_for_window_size(manager, str(terminal["tmux_window"]), "51x27")
    finally:
        for process_pid, master_fd in reversed(processes):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process_pid, signal.SIGTERM)
            manager._wait_for_pid(process_pid, 1.0)
            with contextlib.suppress(OSError):
                os.close(master_fd)
        manager._run_tmux("kill-session", "-t", active_view, check=False)
        manager._run_tmux("kill-session", "-t", passive_view, check=False)
        manager._run_tmux("kill-session", "-t", str(workspace["tmux_session"]), check=False)


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
async def test_node_passive_attach_preserves_grid_and_grouped_window_selection(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runtime = NodeRuntime([tmp_path])
    session = "termroom-node-view-test"
    workspace_payload = {
        "workspace_path": str(project),
        "tmux_session": session,
    }
    first = runtime._handle_sync("workspace.ensure", workspace_payload)["terminals"][0]
    second = runtime._handle_sync("terminal.create", {**workspace_payload, "name": "logs"})[
        "terminal"
    ]
    active_stream_id = uuid.uuid4().hex
    passive_stream_id = uuid.uuid4().hex
    sent: list[dict[str, Any]] = []

    async def send(message: Any) -> None:
        sent.append(dict(message))

    try:
        active_result = await runtime.handle(
            "terminal.attach",
            {
                **workspace_payload,
                "tmux_window": second["tmux_window"],
                "stream_id": active_stream_id,
                "rows": 24,
                "cols": 80,
            },
            send,
        )
        assert active_result.value["stream_id"] == active_stream_id
        assert active_result.value["bootstrap_grid"] is True
        await runtime.streams[active_stream_id].control(
            "resize", {"rows": 37, "cols": 111, "affects_grid": True}
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            size = runtime._tmux(
                "display-message",
                "-p",
                "-t",
                str(second["tmux_window"]),
                "#{window_width}x#{window_height}",
            ).stdout.strip()
            if size == "111x36":
                break
            await asyncio.sleep(0.02)
        assert size == "111x36"

        passive_result = await runtime.handle(
            "terminal.attach",
            {
                **workspace_payload,
                "tmux_window": second["tmux_window"],
                "stream_id": passive_stream_id,
                "rows": 24,
                "cols": 80,
            },
            send,
        )
        assert passive_result.value["stream_id"] == passive_stream_id
        assert passive_result.value["bootstrap_grid"] is False
        await asyncio.sleep(0.2)
        assert (
            runtime._tmux(
                "display-message",
                "-p",
                "-t",
                str(second["tmux_window"]),
                "#{window_width}x#{window_height}",
            ).stdout.strip()
            == "111x36"
        )

        view_session = tmux_browser_view_session(passive_stream_id)
        await runtime.streams[passive_stream_id].control(
            "resize", {"rows": 28, "cols": 51}
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            client_size = runtime._tmux(
                "list-clients",
                "-t",
                view_session,
                "-F",
                "#{client_width}x#{client_height}",
            ).stdout.strip()
            if client_size == "51x28":
                break
            await asyncio.sleep(0.02)
        assert client_size == "51x28"
        assert (
            runtime._tmux(
                "display-message",
                "-p",
                "-t",
                str(second["tmux_window"]),
                "#{window_width}x#{window_height}",
            ).stdout.strip()
            == "111x36"
        )

        canonical_window = runtime._tmux(
            "display-message", "-p", "-t", session, "#{window_id}"
        ).stdout.strip()
        view_window = runtime._tmux(
            "display-message", "-p", "-t", view_session, "#{window_id}"
        ).stdout.strip()
        assert canonical_window == first["tmux_window"]
        assert view_window == second["tmux_window"]

        await runtime.streams[passive_stream_id].control(
            "resize", {"rows": 28, "cols": 51, "affects_grid": True}
        )
        active_view_session = tmux_browser_view_session(active_stream_id)
        assert "ignore-size" in set(
            runtime._tmux(
                "list-clients",
                "-t",
                active_view_session,
                "-F",
                "#{client_flags}",
            ).stdout.strip().split(",")
        )
        assert "ignore-size" not in set(
            runtime._tmux(
                "list-clients",
                "-t",
                view_session,
                "-F",
                "#{client_flags}",
            ).stdout.strip().split(",")
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            size = runtime._tmux(
                "display-message",
                "-p",
                "-t",
                str(second["tmux_window"]),
                "#{window_width}x#{window_height}",
            ).stdout.strip()
            if size == "51x27":
                break
            await asyncio.sleep(0.02)
        assert size == "51x27"

        await runtime.streams[active_stream_id].control(
            "resize", {"rows": 29, "cols": 77, "affects_grid": True}
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            client_size = runtime._tmux(
                "list-clients",
                "-t",
                active_view_session,
                "-F",
                "#{client_width}x#{client_height}",
            ).stdout.strip()
            if client_size == "77x29":
                break
            await asyncio.sleep(0.02)
        assert client_size == "77x29"
        assert "ignore-size" not in set(
            runtime._tmux(
                "list-clients",
                "-t",
                active_view_session,
                "-F",
                "#{client_flags}",
            ).stdout.strip().split(",")
        )
        assert "ignore-size" in set(
            runtime._tmux(
                "list-clients",
                "-t",
                view_session,
                "-F",
                "#{client_flags}",
            ).stdout.strip().split(",")
        )
        assert (
            runtime._tmux(
                "display-message",
                "-p",
                "-t",
                str(second["tmux_window"]),
                "#{window_width}x#{window_height}",
            ).stdout.strip()
            == "77x28"
        )

        await runtime.streams[passive_stream_id].close()
        assert runtime._tmux("has-session", "-t", view_session, check=False).returncode != 0
    finally:
        for stream in tuple(runtime.streams.values()):
            await stream.close()
        runtime._tmux("kill-session", "-t", session, check=False)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_node_recovers_missing_canonical_session_from_browser_view(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runtime = NodeRuntime([tmp_path])
    session = "termroom-node-recover-test"
    payload = {"workspace_path": str(project), "tmux_session": session}
    stale_view = tmux_browser_view_session(uuid.uuid4().hex)

    try:
        original = runtime._handle_sync("workspace.ensure", payload)["terminals"][0]
        original_window = str(original["tmux_window"])
        runtime._complete_fresh_grid_window(original_window)
        runtime._tmux("new-session", "-d", "-s", stale_view, "-t", session)
        runtime._tmux("kill-session", "-t", session)

        recovered = runtime._handle_sync("workspace.ensure", payload)["terminals"][0]

        assert recovered["tmux_window"] == original_window
        assert not runtime._fresh_grid_window(original_window)
        assert runtime._tmux(
            "has-session", "-t", stale_view, check=False
        ).returncode != 0
    finally:
        runtime._tmux("kill-session", "-t", stale_view, check=False)
        runtime._tmux("kill-session", "-t", session, check=False)
