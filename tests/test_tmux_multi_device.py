from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest

from termroom.db import StateStore
from termroom.node_agent import NodeRuntime
from termroom.terminals import TerminalManager, tmux_browser_view_session
from termroom.workspaces import RootManager, WorkspaceManager


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
        manager._run_tmux(
            "kill-session", "-t", str(workspace["tmux_session"]), check=False
        )


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
async def test_node_terminal_attach_uses_an_independent_grouped_view(
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
    second = runtime._handle_sync(
        "terminal.create", {**workspace_payload, "name": "logs"}
    )["terminal"]
    stream_id = uuid.uuid4().hex
    sent: list[dict[str, Any]] = []

    async def send(message: Any) -> None:
        sent.append(dict(message))

    try:
        result = await runtime.handle(
            "terminal.attach",
            {
                **workspace_payload,
                "tmux_window": second["tmux_window"],
                "stream_id": stream_id,
                "rows": 24,
                "cols": 80,
            },
            send,
        )
        assert result.value["stream_id"] == stream_id
        view_session = tmux_browser_view_session(stream_id)
        canonical_window = runtime._tmux(
            "display-message", "-p", "-t", session, "#{window_id}"
        ).stdout.strip()
        view_window = runtime._tmux(
            "display-message", "-p", "-t", view_session, "#{window_id}"
        ).stdout.strip()
        assert canonical_window == first["tmux_window"]
        assert view_window == second["tmux_window"]

        await runtime.streams[stream_id].close()
        assert runtime._tmux(
            "has-session", "-t", view_session, check=False
        ).returncode != 0
    finally:
        for stream in tuple(runtime.streams.values()):
            await stream.close()
        runtime._tmux("kill-session", "-t", session, check=False)
