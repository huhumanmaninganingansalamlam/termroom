from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from termroom.db import StateStore
from termroom.node_agent import NodeRuntime
from termroom.terminals import (
    TerminalManager,
    freeze_tmux_window_size,
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
                "peer\ttermroom-view-peer\t@1\ntarget\ttermroom-view-target\t@1\n"
                if listings == 1
                else "target\ttermroom-view-target\t@1\n"
            )
            return subprocess.CompletedProcess(["tmux", *args], 0, output, "")
        if args[:3] == ("refresh-client", "-t", "peer"):
            return subprocess.CompletedProcess(["tmux", *args], 1, "", "gone")
        if args[:3] == ("refresh-client", "-t", "target"):
            return subprocess.CompletedProcess(["tmux", *args], 0, "", "")
        if args == ("set-window-option", "-t", "@1", "window-size", "latest"):
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
        ("set-window-option", "-t", "@1", "window-size", "latest"),
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
        manager._run_tmux("kill-session", "-t", str(workspace["tmux_session"]), check=False)
        assert manager._run_tmux("has-session", "-t", stale_view, check=False).returncode == 0

        terminal = manager.ensure_workspace(workspace)[0]
        assert terminal["tmux_window"] == original_window
        assert manager._run_tmux("has-session", "-t", stale_view, check=False).returncode != 0
        passive = manager.control.register(terminal_id)
        assert not manager.control.can_resize(terminal_id, passive)
        manager.control.unregister(terminal_id, passive)
    finally:
        manager._run_tmux("kill-session", "-t", stale_view, check=False)
        manager._run_tmux("kill-session", "-t", str(workspace["tmux_session"]), check=False)


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

        plan = manager.control.begin_resize(terminal_id, first, rows=33, cols=162)
        assert plan is not None and plan.bootstrap
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
        assert manager.control.resize_applied(plan)
        assert freeze_tmux_window_size(manager._run_tmux, str(terminal["tmux_window"]))
        assert manager._sync_browser_grid_role(terminal_id, first, active_view, enabled=False)
        assert manager.control.commit_resize(plan)
        assert not manager.control.can_resize(terminal_id, first)
        assert "ignore-size" in _client_flags(manager, active_view)
        manager._set_window_size(process[1], rows=17, cols=162)
        os.killpg(process[0], signal.SIGWINCH)
        _wait_for_client_size(manager, active_view, "162x17")
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
        manager._run_tmux("kill-session", "-t", str(workspace["tmux_session"]), check=False)


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
        original_mode = manager.current_pane_mode(workspace, first)
        assert original_mode["session"] == workspace["tmux_session"]
        assert original_mode["window"] == first["tmux_window"]
        assert manager.current_pane_mode(workspace, second)["window"] == second["tmux_window"]
        manager._run_tmux("kill-session", "-t", first_view)
        assert not manager.session_exists(first_view)
        replacement_view = tmux_browser_view_session(uuid.uuid4().hex)
        try:
            manager._prepare_browser_view(workspace, first, replacement_view)
            assert replacement_view != first_view
            assert manager.current_pane_mode(workspace, first) == original_mode
            assert manager._run_tmux(
                "display-message",
                "-p",
                "-t",
                replacement_view,
                "#{window_id}|#{pane_id}|#{pane_pid}",
            ).stdout.strip() == (
                f"{original_mode['window']}|{original_mode['pane']}|{original_mode['pane_pid']}"
            )
        finally:
            manager._run_tmux("kill-session", "-t", replacement_view, check=False)
        manager._prepare_browser_view(workspace, first, first_view)
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
        resized = await runtime.handle(
            "terminal.resize",
            {
                "stream_id": active_stream_id,
                "revision": 1,
                "rows": 37,
                "cols": 111,
                "affects_grid": True,
                "bootstrap": False,
            },
            send,
        )
        assert resized.value["ok"]
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
        resized = await runtime.handle(
            "terminal.resize",
            {
                "stream_id": passive_stream_id,
                "revision": 1,
                "rows": 28,
                "cols": 51,
                "affects_grid": False,
                "bootstrap": False,
            },
            send,
        )
        assert resized.value["ok"]
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

        resized = await runtime.handle(
            "terminal.resize",
            {
                "stream_id": passive_stream_id,
                "revision": 2,
                "rows": 28,
                "cols": 51,
                "affects_grid": True,
                "bootstrap": False,
            },
            send,
        )
        assert resized.value["ok"]
        active_view_session = tmux_browser_view_session(active_stream_id)
        assert "ignore-size" in set(
            runtime._tmux(
                "list-clients",
                "-t",
                active_view_session,
                "-F",
                "#{client_flags}",
            )
            .stdout.strip()
            .split(",")
        )
        assert "ignore-size" not in set(
            runtime._tmux(
                "list-clients",
                "-t",
                view_session,
                "-F",
                "#{client_flags}",
            )
            .stdout.strip()
            .split(",")
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

        resized = await runtime.handle(
            "terminal.resize",
            {
                "stream_id": active_stream_id,
                "revision": 2,
                "rows": 29,
                "cols": 77,
                "affects_grid": True,
                "bootstrap": False,
            },
            send,
        )
        assert resized.value["ok"]
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
            )
            .stdout.strip()
            .split(",")
        )
        assert "ignore-size" in set(
            runtime._tmux(
                "list-clients",
                "-t",
                view_session,
                "-F",
                "#{client_flags}",
            )
            .stdout.strip()
            .split(",")
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
        assert runtime._tmux("has-session", "-t", stale_view, check=False).returncode != 0
    finally:
        runtime._tmux("kill-session", "-t", stale_view, check=False)
        runtime._tmux("kill-session", "-t", session, check=False)

@pytest.mark.skipif(
    shutil.which("tmux") is None or shutil.which("google-chrome") is None,
    reason="Real tmux and Google Chrome are required",
)
def test_real_headless_browser_a_is_unchanged_by_scoped_b_c_reads(
    tmp_path: Path,
) -> None:
    """A stays live while scoped B/C clients read, edit, and run real work."""

    from websockets.sync.client import connect as websocket_connect

    from termroom.app import create_app
    from termroom.config import Settings

    project_root = tmp_path / "projects"
    project_root.mkdir()
    for name in ("a", "b", "c"):
        directory = project_root / name
        directory.mkdir()
        (directory / "qa-target.txt").write_text(f"project-{name}\n", encoding="utf-8")
        if name == "c":
            (directory / "qa-check.py").write_text("print('qa-c-run-ok')\n", encoding="utf-8")
    settings = Settings.create(
        project_root, state_dir=tmp_path / "state",
        login_password="isolated-qa-password", access_token="isolated-qa-token",
    )
    app = create_app(settings)
    workspaces = {name: app.state.workspaces.open(name) for name in ("a", "b", "c")}
    terminals = {
        name: app.state.terminals.ensure_workspace(workspace)[0]
        for name, workspace in workspaces.items()
    }
    tokens = {
        name: app.state.control_credentials.issue(
            name=f"reader-{name}", capabilities=["read"],
            workspace_ids=[workspaces[name]["id"]],
        )["token"]
        for name in ("b", "c")
    }

    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = int(port_socket.getsockname()[1])
    base_url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port,
            log_level="error", access_log=False,
        )
    )
    server_thread = threading.Thread(target=server.run, daemon=True)
    chrome: subprocess.Popen[bytes] | None = None
    try:
        server_thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base_url}/health", timeout=0.3).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("Isolated Termroom Core did not become ready")

        with httpx.Client(base_url=base_url) as login:
            response = login.post(
                "/login", data={"password": "isolated-qa-password"},
            )
            assert response.status_code == 303
            cookie = login.cookies.get("termroom_session")
            assert cookie

        chrome_profile = tmp_path / "isolated-chrome-profile"
        chrome_profile.mkdir()
        chrome = subprocess.Popen(
            [
                "google-chrome", "--headless=new", "--disable-gpu",
                "--no-first-run", "--no-default-browser-check",
                "--remote-debugging-port=0",
                "--remote-allow-origins=http://127.0.0.1",
                f"--user-data-dir={chrome_profile}", "about:blank",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        debug_port_file = chrome_profile / "DevToolsActivePort"
        deadline = time.monotonic() + 10
        while not debug_port_file.exists() and time.monotonic() < deadline:
            if chrome.poll() is not None:
                raise AssertionError("Isolated Chrome exited before readiness")
            time.sleep(0.05)
        assert debug_port_file.exists(), "Isolated Chrome debugger not ready"
        debug_port = int(debug_port_file.read_text(encoding="utf-8").splitlines()[0])
        page = httpx.put(
            f"http://127.0.0.1:{debug_port}/json/new?about:blank", timeout=5,
        ).json()
        with websocket_connect(
            page["webSocketDebuggerUrl"],
            origin="http://127.0.0.1",
            open_timeout=5,
        ) as devtools:
            sequence = 0
            protocol_frames: list[tuple[str, dict[str, Any]]] = []

            def cdp(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
                nonlocal sequence
                sequence += 1
                request_id = sequence
                devtools.send(json.dumps({
                    "id": request_id, "method": method, "params": params or {},
                }))
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    message = json.loads(devtools.recv(timeout=10))
                    if message.get("method") in (
                        "Network.webSocketFrameSent", "Network.webSocketFrameReceived",
                    ):
                        response = message.get("params", {}).get("response", {})
                        if response.get("opcode") in (1, 2):
                            try:
                                payload = response.get("payloadData", "")
                                if response["opcode"] == 2:
                                    payload = base64.b64decode(payload).decode("utf-8", "replace")
                                parsed = json.loads(payload)
                            except (TypeError, ValueError):
                                parsed = None
                            if isinstance(parsed, dict) and parsed.get("kind") in (
                                "command", "command_ack",
                            ):
                                protocol_frames.append((message["method"], parsed))
                    if message.get("id") != request_id:
                        continue
                    if "error" in message:
                        raise AssertionError(f"Chrome CDP {method}: {message['error']}")
                    return message.get("result", {})
                raise AssertionError(f"Chrome CDP {method} timed out")

            def evaluate(expression: str) -> Any:
                result = cdp("Runtime.evaluate", {
                    "expression": expression,
                    "returnByValue": True,
                })
                if "exceptionDetails" in result:
                    raise AssertionError(str(result["exceptionDetails"]))
                return result.get("result", {}).get("value")

            cdp("Page.enable")
            cdp("Runtime.enable")
            cdp("Network.enable")
            assert cdp("Network.setCookie", {
                "name": "termroom_session", "value": cookie,
                "url": f"{base_url}/", "httpOnly": True,
            })["success"]
            a_id = workspaces["a"]["id"]
            a_terminal_id = terminals["a"]["id"]
            cdp("Page.navigate", {
                "url": f"{base_url}/w/{a_id}/terminal?terminal={a_terminal_id}",
            })
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    ready = evaluate(
                        "document.readyState === 'complete' && "
                        "!!document.querySelector('#terminal .xterm-helper-textarea')"
                    )
                except AssertionError:
                    ready = False
                if ready and app.state.terminals.control.client_count(a_terminal_id) > 0:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("Real Chrome did not establish Terminal A")

            prepared = evaluate("""(() => {
              document.querySelector('#open-command-editor').click();
              const input = document.querySelector('#command-input');
              if (document.querySelector('#terminal-command-editor').hidden) {
                throw new Error('Command editor stayed closed');
              }
              input.value = 'echo unsent-private-draft-한글';
              input.dispatchEvent(new Event('input', { bubbles: true }));
              input.focus();
              input.setSelectionRange(5, 10);
              const marker = document.createElement('div');
              marker.id = 'qa-scroll-probe';
              marker.style.cssText =
                'position:fixed;left:-10000px;top:-10000px;width:40px;' +
                'height:40px;overflow:auto';
              const inside = document.createElement('div');
              inside.style.height = '500px';
              marker.appendChild(inside);
              document.body.appendChild(marker);
              marker.scrollTop = 67;
              return input.value;
            })()""")
            assert prepared == "echo unsent-private-draft-한글"
            state_expression = """(() => {
              const input = document.querySelector('#command-input');
              const viewport = document.querySelector('.xterm-viewport');
              return {
                url: location.href,
                selectedTerminal:
                  document.querySelector('.terminal-tabs a.active')?.dataset.terminalActivityTab,
                composerOpen: !document.querySelector('#terminal-command-editor').hidden,
                focusId: document.activeElement?.id,
                draft: input.value,
                selectionStart: input.selectionStart,
                selectionEnd: input.selectionEnd,
                viewportScroll: viewport ? viewport.scrollTop : null,
                probeScroll: document.querySelector('#qa-scroll-probe').scrollTop,
                pageScroll: window.scrollY
              };
            })()"""
            a_workspace = workspaces["a"]
            target = f"{a_workspace['tmux_session']}:{terminals['a']['tmux_window']}"

            def grid_and_controller() -> tuple[str, dict[str, int | str], list[dict[str, Any]]]:
                geometry = app.state.terminals._run_tmux(
                    "display-message", "-p", "-t", target,
                    "#{pane_width}|#{pane_height}|#{pane_id}",
                ).stdout.strip()
                controller = app.state.terminals.control.control_state(
                    a_terminal_id, "qa-observer",
                )
                return geometry, controller, app.state.store.list_terminals(a_id)

            # Initial attach, composer layout and output metadata can settle
            # after DOM readiness; wait for a stable A before B/C reads.
            stable_start = time.monotonic()
            stable_value = grid_and_controller()
            deadline = stable_start + 8
            while time.monotonic() < deadline:
                time.sleep(0.05)
                current = grid_and_controller()
                if current != stable_value:
                    stable_value = current
                    stable_start = time.monotonic()
                if time.monotonic() - stable_start >= 0.8:
                    break
            else:
                raise AssertionError("Browser A initial grid/control did not stabilize")
            before_geometry, before_controller, before_terminals = stable_value
            before_browser = evaluate(state_expression)
            assert before_browser["selectedTerminal"] == a_terminal_id
            assert before_browser["focusId"] == "command-input"
            assert before_browser["composerOpen"] is True
            assert before_browser["probeScroll"] == 67
            before_workspace = app.state.store.get_workspace(a_id)["last_opened_at"]

            async def observe(name: str) -> None:
                scoped_id = workspaces[name]["id"]
                terminal_id = terminals[name]["id"]
                headers = {"Authorization": f"Bearer {tokens[name]}"}
                async with httpx.AsyncClient(base_url=base_url, timeout=10) as reader:
                    paths = [
                        "/api/control/v1/workspaces",
                        f"/api/control/v1/workspaces/{scoped_id}/files",
                        f"/api/control/v1/workspaces/{scoped_id}/files/search?q=qa-target",
                        f"/api/control/v1/workspaces/{scoped_id}/files/text?path=qa-target.txt",
                        f"/api/control/v1/workspaces/{scoped_id}/terminals/{terminal_id}/snapshot?lines=10",
                        f"/api/control/v1/workspaces/{scoped_id}/file-runs",
                    ]
                    results = await asyncio.gather(*(
                        reader.get(path, headers=headers) for path in paths
                    ))
                    assert all(r.status_code == 200 for r in results), [
                        (r.status_code, r.text[:200]) for r in results
                    ]
                    assert [w["id"] for w in results[0].json()["workspaces"]] == [scoped_id]
                    assert results[3].json()["content"] == f"project-{name}\n"
                    assert results[4].json()["pane_identity_verified"] is True

            async def observe_both() -> None:
                await asyncio.gather(observe("b"), observe("c"))

            asyncio.run(observe_both())
            after_browser = evaluate(state_expression)
            assert after_browser == before_browser
            assert app.state.terminals._run_tmux(
                "display-message", "-p", "-t", target,
                "#{pane_width}|#{pane_height}|#{pane_id}",
            ).stdout.strip() == before_geometry
            assert app.state.terminals.control.control_state(
                a_terminal_id, "qa-observer",
            ) == before_controller
            assert app.state.store.get_workspace(a_id)["last_opened_at"] == before_workspace
            assert app.state.store.list_terminals(a_id) == before_terminals

            # T43: actual scoped B/C mutations while the real browser stays on A.
            # All files, browsers, tmux sessions, and tokens belong to tmp_path.
            operators = {
                name: app.state.control_credentials.issue(
                    name=f"operator-{name}", capabilities=["read", "operate"],
                    workspace_ids=[workspaces[name]["id"]],
                )
                for name in ("b", "c")
            }
            b_id, c_id = workspaces["b"]["id"], workspaces["c"]["id"]
            b_headers = {"Authorization": f"Bearer {operators['b']['token']}"}
            c_headers = {"Authorization": f"Bearer {operators['c']['token']}"}
            b_commands_url = f"/api/control/v1/workspaces/{b_id}/commands"
            c_files_url = f"/api/control/v1/workspaces/{c_id}/files/text"
            with httpx.Client(base_url=base_url, timeout=10) as agent:
                initial = agent.get(b_commands_url, headers=b_headers)
                assert initial.status_code == 200, initial.text
                created = agent.post(
                    b_commands_url, headers=b_headers,
                    json={
                        "action_id": "qa-b-command-create-0001",
                        "expected_revision": initial.json()["revision"],
                        "name": "QA B action", "command": "printf 'qa-b-command-ok\\n'",
                    },
                )
                assert created.status_code == 201, created.text
                commands = agent.get(b_commands_url, headers=b_headers)
                assert commands.status_code == 200
                b_command = next(
                    item for item in commands.json()["commands"]
                    if item["id"] == created.json()["result_resource_id"]
                )
                b_launch_url = f"{b_commands_url}/{b_command['id']}/launch"
                b_launch_payload = {
                    "action_id": "qa-b-command-launch-0001",
                    "launch_id": uuid.uuid4().hex,
                    "expected_revision": b_command["revision"],
                }
                launched = agent.post(
                    b_launch_url, headers=b_headers, json=b_launch_payload,
                )
                assert launched.status_code == 202, launched.text
                b_terminal_ids = {
                    row["id"] for row in app.state.store.list_terminals(b_id)
                }
                replayed = agent.post(
                    b_launch_url, headers=b_headers, json=b_launch_payload,
                )
                assert replayed.status_code == 202, replayed.text
                assert replayed.json()["result_resource_id"] == b_launch_payload["launch_id"]
                assert launched.json()["result_resource_id"] == b_launch_payload["launch_id"]
                assert replayed.json()["phase"] == "process_started"
                assert {
                    row["id"] for row in app.state.store.list_terminals(b_id)
                } == b_terminal_ids
                b_terminal = app.state.store.get_terminal(launched.json()["terminal_id"])
                assert b_terminal is not None
                # B's operator cannot edit C even while the browser owns A.
                denied = agent.put(
                    c_files_url, headers=b_headers,
                    json={
                        "action_id": "qa-b-cross-scope",
                        "relative_path": "qa-target.txt",
                        "content": "not-allowed",
                        "expected_digest": hashlib.sha256(b"project-c\n").hexdigest(),
                        "expected_mtime_ns": (
                            project_root / "c" / "qa-target.txt"
                        ).stat().st_mtime_ns,
                    },
                )
                assert denied.status_code == 404

                observed = agent.get(
                    c_files_url, params={"path": "qa-target.txt"}, headers=c_headers,
                )
                assert observed.status_code == 200, observed.text
                saved = agent.put(
                    c_files_url, headers=c_headers,
                    json={
                        "action_id": "qa-c-file-save-0001",
                        "relative_path": "qa-target.txt", "content": "qa-c-updated\n",
                        "expected_digest": observed.json()["digest"],
                        "expected_mtime_ns": observed.json()["mtime_ns"],
                    },
                )
                assert saved.status_code == 200, saved.text
                assert (project_root / "c" / "qa-target.txt").read_text(
                    encoding="utf-8",
                ) == "qa-c-updated\n"
                started = agent.post(
                    f"/api/control/v1/workspaces/{c_id}/file-runs",
                    headers=c_headers,
                    json={
                        "action_id": "qa-c-file-run-0001",
                        "relative_path": "qa-check.py",
                        "expected_digest": hashlib.sha256(
                            b"print('qa-c-run-ok')\n",
                        ).hexdigest(),
                        "idempotency_key": str(uuid.uuid4()),
                    },
                )
                assert started.status_code == 202, started.text
                c_run_id = started.json()["run"]["id"]

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                b_output = app.state.terminals.capture_scrollback(
                    workspaces["b"], b_terminal,
                )
                if any(line.strip() == "qa-b-command-ok" for line in b_output.splitlines()):
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("B command did not execute in real tmux")
            latest = app.state.file_runs.reconcile(c_run_id)
            deadline = time.monotonic() + 10
            while latest["state"] not in {"finished", "failed", "stopped"}:
                assert time.monotonic() < deadline, latest
                time.sleep(0.05)
                latest = app.state.file_runs.reconcile(c_run_id)
            assert latest["state"] == "finished" and latest["exit_code"] == 0, latest
            c_terminal = app.state.store.get_managed_terminal(c_id, "file_run")
            assert c_terminal is not None
            assert "qa-c-run-ok" in app.state.terminals.capture_scrollback(
                workspaces["c"], c_terminal,
            )
            with httpx.Client(
                base_url=base_url, cookies={"termroom_session": cookie}, timeout=10,
            ) as human_viewer:
                b_page = human_viewer.get(
                    f"/w/{b_id}/terminal?terminal={b_terminal['id']}",
                )
                assert b_page.status_code == 200 and "QA B action" in b_page.text
                c_view = human_viewer.get(f"/w/{c_id}/view/qa-target.txt")
                assert c_view.status_code == 200 and "qa-c-updated" in c_view.text
                assert human_viewer.get(
                    f"/w/{c_id}/terminal?terminal={c_terminal['id']}",
                ).status_code == 200
                # A human session edits C after the operator has read its version.
                # The stale non-UI CAS must preserve the human's final bytes.
                with httpx.Client(base_url=base_url, timeout=10) as stale_operator:
                    version = stale_operator.get(
                        c_files_url, params={"path": "qa-target.txt"}, headers=c_headers,
                    )
                    assert version.status_code == 200
                    human_editor = human_viewer.get(f"/w/{c_id}/edit/qa-target.txt")
                    assert human_editor.status_code == 200
                    human_saved = human_viewer.post(
                        f"/w/{c_id}/edit/qa-target.txt",
                        data={
                            "_csrf": settings.csrf_token,
                            "digest": version.json()["digest"],
                            "mtime_ns": str(version.json()["mtime_ns"]),
                            "content": "qa-c-human-wins\n",
                        },
                        follow_redirects=False,
                    )
                    assert human_saved.status_code == 303, human_saved.text
                    stale = stale_operator.put(
                        c_files_url, headers=c_headers,
                        json={
                            "action_id": "qa-c-stale-save-0001",
                            "relative_path": "qa-target.txt",
                            "content": "qa-c-stale-must-not-win\n",
                            "expected_digest": version.json()["digest"],
                            "expected_mtime_ns": version.json()["mtime_ns"],
                        },
                    )
                    assert stale.status_code == 409, stale.text
                    assert stale.json()["code"] == "file_conflict"
                    assert stale.json()["current_version"]["digest"] == hashlib.sha256(
                        b"qa-c-human-wins\n",
                    ).hexdigest()
                    assert "qa-c-stale-must-not-win" not in stale.text
                    receipt = stale_operator.get(
                        "/api/control/v1/actions/qa-c-stale-save-0001",
                        headers=c_headers,
                    )
                    assert receipt.status_code == 200
                    assert receipt.json()["status"] == "refused"
                assert (project_root / "c" / "qa-target.txt").read_text(
                    encoding="utf-8",
                ) == "qa-c-human-wins\n"
                final_view = human_viewer.get(f"/w/{c_id}/view/qa-target.txt")
                assert final_view.status_code == 200
                assert "qa-c-human-wins" in final_view.text
                # Revocation cannot stop a command already started in B's tmux.
                with httpx.Client(base_url=base_url, timeout=10) as worker:
                    commands = worker.get(b_commands_url, headers=b_headers)
                    assert commands.status_code == 200
                    created = worker.post(b_commands_url, headers=b_headers, json={
                        "action_id": "qa-b-revoke-command-create",
                        "expected_revision": commands.json()["revision"],
                        "name": "B revoke survival",
                        "command": (
                            "printf 'qa-b-running-before-revoke\\n'; "
                            "while [ ! -e qa-b-revoke-release ]; do sleep 0.1; done; "
                            "printf 'qa-b-running-after-revoke\\n'"
                        ),
                    })
                    assert created.status_code == 201, created.text
                    updated_commands = worker.get(b_commands_url, headers=b_headers)
                    survivor_command = next(
                        row for row in updated_commands.json()["commands"]
                        if row["id"] == created.json()["result_resource_id"]
                    )
                    launched = worker.post(
                        f"{b_commands_url}/{survivor_command['id']}/launch",
                        headers=b_headers,
                        json={
                            "action_id": "qa-b-revoke-command-launch",
                            "launch_id": uuid.uuid4().hex,
                            "expected_revision": survivor_command["revision"],
                        },
                    )
                    assert launched.status_code == 202, launched.text
                    survivor = app.state.store.get_terminal(launched.json()["terminal_id"])
                    assert survivor is not None
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        output = app.state.terminals.capture_scrollback(
                            workspaces["b"], survivor,
                        )
                        if "qa-b-running-before-revoke" in output:
                            break
                        time.sleep(0.05)
                    else:
                        raise AssertionError("B's managed command did not start")
                    release_flag = project_root / "b" / "qa-b-revoke-release"
                    try:
                        revoked = human_viewer.post(
                            f"/api/client-credentials/{operators['b']['id']}/revoke",
                            headers={"X-Termroom-CSRF": settings.csrf_token},
                            json={"confirm_client_id": operators["b"]["id"]},
                        )
                        assert revoked.status_code == 200, revoked.text
                        assert worker.get(
                            b_commands_url, headers=b_headers,
                        ).status_code == 401
                    finally:
                        release_flag.touch()
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        output = app.state.terminals.capture_scrollback(
                            workspaces["b"], survivor,
                        )
                        if "qa-b-running-after-revoke" in output:
                            break
                        time.sleep(0.05)
                    else:
                        raise AssertionError("Credential revocation killed B's active process")
            assert evaluate(state_expression) == before_browser
            assert app.state.terminals._run_tmux(
                "display-message", "-p", "-t", target,
                "#{pane_width}|#{pane_height}|#{pane_id}",
            ).stdout.strip() == before_geometry
            assert app.state.terminals.control.control_state(
                a_terminal_id, "qa-observer",
            ) == before_controller
            assert app.state.store.get_workspace(a_id)["last_opened_at"] == before_workspace
            # A's shell can emit output independently; its output/activity clock
            # need not freeze during long B/C runs. Its terminal identity does.
            stable_fields = (
                "id", "workspace_id", "name", "tmux_window", "role",
                "managed_run_id", "created_at", "last_opened_at",
            )
            assert [
                tuple(row[field] for field in stable_fields)
                for row in app.state.store.list_terminals(a_id)
            ] == [tuple(row[field] for field in stable_fields) for row in before_terminals]
            # T43: trace one synthetic browser command through the real local PTY.
            # The receiver is test-owned, never a user's shell/TUI prompt.
            receiver_script = tmp_path / "synthetic_receiver.py"
            receiver_output = tmp_path / "synthetic_receiver.bin"
            receiver_script.write_text(
                "import os, sys, termios, tty\n"
                "fd = sys.stdin.fileno()\n"
                "old = termios.tcgetattr(fd)\n"
                "tty.setraw(fd)\n"
                "try:\n"
                "    sys.stdout.write('\\x1b[?2004hRECEIVER_READY\\r\\n')\n"
                "    sys.stdout.flush()\n"
                "    raw = bytearray()\n"
                "    while b'\\r' not in raw and len(raw) < 65536:\n"
                "        raw.extend(os.read(fd, 4096))\n"
                "    with open(sys.argv[1], 'wb') as target: target.write(raw)\n"
                "    sys.stdout.write('\\x1b[?2004lRECEIVER_EFFECT\\r\\n')\n"
                "    sys.stdout.flush()\n"
                "finally:\n"
                "    termios.tcsetattr(fd, termios.TCSANOW, old)\n",
                encoding="utf-8",
            )
            receiver_command = (
                f"python3 -u {shlex.quote(str(receiver_script))} "
                f"{shlex.quote(str(receiver_output))}"
            )
            app.state.terminals._run_tmux(
                "send-keys", "-l", "-t", target, receiver_command,
            )
            app.state.terminals._run_tmux("send-keys", "-t", target, "Enter")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                pane = app.state.terminals._run_tmux(
                    "capture-pane", "-p", "-t", target,
                ).stdout
                if "RECEIVER_READY" in pane:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("Synthetic PTY receiver never became ready")

            marker = uuid.uuid4().hex
            payload = "qa-delivery-" + marker + "-" + "한글" * 1000
            cdp("Page.bringToFront")
            script = r"""(() => {
              window.__qaKeys = [];
              document.addEventListener('keydown', event => {
                if (event.key === 'Enter' && event.ctrlKey)
                  window.__qaKeys.push(performance.now());
              }, true);
              const input = document.querySelector('#command-input');
              input.value = SYNTHETIC_PAYLOAD;
              input.dispatchEvent(new Event('input', {bubbles:true}));
              input.focus();
              return {focus:document.hasFocus(),visible:document.visibilityState,
                      inputFocused:document.activeElement === input};
            })()""".replace("SYNTHETIC_PAYLOAD", json.dumps(payload, ensure_ascii=False))
            prepared_command = evaluate(script)
            assert prepared_command == {
                "focus": True, "visible": "visible", "inputFocused": True,
            }
            cdp("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13, "modifiers": 2,
            })
            cdp("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13, "modifiers": 2,
            })
            deadline = time.monotonic() + 8
            observed = {}
            while time.monotonic() < deadline:
                observed = evaluate("""(() => {
                  const host = document.querySelector('#terminal');
                  return {
                    keys: window.__qaKeys,
                    receipt: document.querySelector('#command-delivery')?.dataset.state,
                    draft: document.querySelector('#command-input')?.value,
                    controlsInput: host.termroomControlsInput,
                    pane: host.dataset.inputTargetPane,
                    visibility: document.visibilityState,
                    focused: document.hasFocus()
                  };
                })()""")
                sent = [
                    frame for direction, frame in protocol_frames
                    if direction == "Network.webSocketFrameSent"
                    and frame["kind"] == "command"
                ]
                acks = [
                    frame for direction, frame in protocol_frames
                    if direction == "Network.webSocketFrameReceived"
                    and frame["kind"] == "command_ack"
                ]
                if (
                    receiver_output.exists()
                    and observed["receipt"] == "accepted"
                    and len(sent) == 1
                    and len(acks) == 1
                ):
                    break
                time.sleep(0.05)
            else:
                raise AssertionError(
                    "Command/ACK/receiver not observed: "
                    f"browser={observed}, sent={len(sent)}, acks={len(acks)}"
                )

            assert len(observed["keys"]) == 1, observed
            assert observed["controlsInput"] is True
            assert observed["visibility"] == "visible"
            assert observed["focused"] is True
            assert len(sent) == len(acks) == 1
            frame, ack = sent[0], acks[0]
            assert frame["data"] == payload
            assert frame["pane"] == observed["pane"]
            assert frame["command_id"]
            assert ack["accepted"] is True
            assert ack["terminal_id"] == a_terminal_id
            assert ack["command_id"] == frame["command_id"]
            assert ack["generation"] == app.state.terminals.control.control_state(
                a_terminal_id, "qa-observer",
            )["controller_client_id"]
            assert observed["draft"] == ""
            assert app.state.store.list_commands(a_id) == [payload]
            raw = receiver_output.read_bytes()
            assert raw == frame["paste_data"].encode("utf-8") + b"\r"
            assert raw.count(b"\r") == 1
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                pane = app.state.terminals._run_tmux(
                    "capture-pane", "-p", "-t", target,
                ).stdout
                if "RECEIVER_EFFECT" in pane:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("Receiver effect not visible after matching ACK")
            print(
                f"T43 marker={marker} pane={frame['pane']} "
                f"generation={ack['generation']} command_id={frame['command_id']} "
                f"utf8_bytes={len(payload.encode('utf-8'))} "
                f"payload_sha256={hashlib.sha256(payload.encode('utf-8')).hexdigest()} "
                f"pty_sha256={hashlib.sha256(raw).hexdigest()} "
                f"browser_keydowns={len(observed['keys'])}, "
                f"command_frames={len(sent)}, matching_acks={len(acks)}, "
                f"pty_bytes={len(raw)}, ack={observed['receipt']}, "
                "receiver_effect=True"
            )
            # T39: a real browser hands its existing Terminal to a bearer client.
            # A synthetic raw-mode PTY receiver records exact bytes, never a user command.
            t39_script = tmp_path / "t39_receiver.py"
            t39_result = tmp_path / "t39_receiver.bin"
            t39_script.write_text(
                receiver_script.read_text(encoding="utf-8")
                .replace("RECEIVER_READY", "T39_RECEIVER_READY")
                .replace("RECEIVER_EFFECT", "T39_RECEIVER_EFFECT"),
                encoding="utf-8",
            )
            app.state.terminals._run_tmux(
                "send-keys", "-l", "-t", target,
                f"python3 -u {shlex.quote(str(t39_script))} {shlex.quote(str(t39_result))}",
            )
            app.state.terminals._run_tmux("send-keys", "-t", target, "Enter")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if "T39_RECEIVER_READY" in app.state.terminals._run_tmux(
                    "capture-pane", "-p", "-t", target,
                ).stdout:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("T39 synthetic PTY receiver did not start")

            # Literal human bytes, intentionally without Enter.
            initial_revision = app.state.terminal_control.presence(a_terminal_id)["input_revision"]
            cdp("Page.bringToFront")
            assert evaluate("""(() => {
              const input = document.querySelector('#terminal .xterm-helper-textarea');
              input.focus();
              return document.activeElement === input && document.hasFocus();
            })()""")
            cdp("Input.insertText", {"text": "git comm"})
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                if (
                    app.state.terminal_control.presence(a_terminal_id)["input_revision"]
                    > initial_revision
                ):
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("Real browser native partial input was not admitted")

            a_writer = app.state.control_credentials.issue(
                name="qa-t39-client", capabilities=["read", "operate"],
                workspace_ids=[a_id],
            )["token"]
            control_url = f"/api/control/v1/workspaces/{a_id}/terminals/{a_terminal_id}"
            with httpx.Client(base_url=base_url, timeout=8) as writer:
                headers_a = {"Authorization": f"Bearer {a_writer}"}
                observed_control = writer.get(control_url + "/control", headers=headers_a)
                assert observed_control.status_code == 200, observed_control.text
                pane_id = observed_control.json()["pane"]["pane"]
                current_rev = observed_control.json()["control"]["control_revision"]
                denied = writer.post(
                    control_url + "/control/claim", headers=headers_a,
                    json={
                        "action_id": "t39-browser-owns",
                        "expected_tmux_window": terminals["a"]["tmux_window"],
                        "expected_pane": pane_id, "expected_control_revision": current_rev,
                    },
                )
                assert denied.status_code == 409, denied.text
                assert denied.json()["code"] == "control_conflict"
                assert not t39_result.exists(), "Automation must not submit the partial human line"

                # Preserve an unsubmitted browser-composer draft across the handoff.
                assert evaluate("""(() => {
                  const draft = document.querySelector('#command-input');
                  draft.value = 'private-draft-한글-keep';
                  draft.dispatchEvent(new Event('input', {bubbles:true}));
                  const menu = document.querySelector(
                    '.terminal-manage-menu [data-popover-trigger]'
                  );
                  menu.click();
                  window.confirm = () => true;
                  const button = document.querySelector('#terminal-release-control');
                  if (button.disabled) throw new Error('Active browser cannot release control');
                  button.click();
                  return draft.value;
                })()""") == "private-draft-한글-keep"
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    if evaluate(
                        "document.querySelector('#terminal').termroomControlsInput === false"
                    ):
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError("Real browser did not relinquish Terminal control")

                after_release = writer.get(control_url + "/control", headers=headers_a).json()
                assert after_release["control"]["controller_kind"] == "none"
                granted = writer.post(
                    control_url + "/control/claim", headers=headers_a,
                    json={
                        "action_id": "t39-client-claim",
                        "expected_tmux_window": terminals["a"]["tmux_window"],
                        "expected_pane": pane_id,
                        "expected_control_revision": after_release["control"]["control_revision"],
                    },
                )
                assert granted.status_code == 200, granted.text
                generation = granted.json()["generation"]
                control_rev = granted.json()["control_revision"]
                input_body = {
                    "action_id": "t39-client-input", "command_id": "t39-client-command",
                    "expected_tmux_window": terminals["a"]["tmux_window"],
                    "pane": pane_id, "generation": generation,
                    "expected_control_revision": control_rev,
                    "data": "it", "append_enter": True,
                }
                receipt = writer.post(control_url + "/input", headers=headers_a, json=input_body)
                assert receipt.status_code == 202, receipt.text
                assert receipt.json()["phase"] == "pty_delivered"
                again = writer.post(control_url + "/input", headers=headers_a, json=input_body)
                assert again.status_code == 202
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not t39_result.exists():
                    time.sleep(0.05)
                assert t39_result.exists(), "External input never reached synthetic receiver"
                raw_t39 = t39_result.read_bytes()
                assert raw_t39.startswith(b"git comm"), raw_t39[:100]
                assert b"it" in raw_t39
                assert raw_t39.endswith(b"\r"), raw_t39[-100:]
                assert raw_t39.count(b"\r") == 1, raw_t39
                assert (
                    evaluate("document.querySelector('#command-input').value")
                    == "private-draft-한글-keep"
                )

                # Genuine Terminal click reclaims input; the old client generation dies.
                location = evaluate("""(() => {
                  const box = document.querySelector('#terminal').getBoundingClientRect();
                  return {x: box.left + 30, y: box.top + 30};
                })()""")
                cdp("Input.dispatchMouseEvent", {
                    "type": "mousePressed", "x": location["x"], "y": location["y"],
                    "button": "left", "clickCount": 1,
                })
                cdp("Input.dispatchMouseEvent", {
                    "type": "mouseReleased", "x": location["x"], "y": location["y"],
                    "button": "left", "clickCount": 1,
                })
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if evaluate(
                        "document.querySelector('#terminal').termroomControlsInput === true"
                    ):
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError("Genuine browser activation did not reclaim control")
                late = writer.post(
                    control_url + "/input", headers=headers_a,
                    json={**input_body, "action_id": "t39-client-lost-control",
                          "command_id": "t39-client-command-lost", "data": "SHOULD_NOT_SEND",
                          "append_enter": False},
                )
                assert late.status_code == 409
                assert late.json()["code"] == "control_conflict"
                assert (
                    evaluate("document.querySelector('#command-input').value")
                    == "private-draft-한글-keep"
                )
                print(
                    f"T39 pane={pane_id} handoff_browser_client_browser=True "
                    f"partial_bytes=8 receiver_bytes={len(raw_t39)} "
                    f"enter_count={raw_t39.count(bytes([13]))} "
                    f"client_receipt={receipt.json()['phase']} stale_refused=True"
                )
    finally:
        if chrome is not None and chrome.poll() is None:
            os.killpg(chrome.pid, signal.SIGTERM)
            try:
                chrome.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(chrome.pid, signal.SIGKILL)
                chrome.wait(timeout=5)
        server.should_exit = True
        if server_thread.is_alive():
            server_thread.join(timeout=8)
        for workspace in workspaces.values():
            app.state.terminals._run_tmux(
                "kill-session", "-t", str(workspace["tmux_session"]), check=False,
            )
