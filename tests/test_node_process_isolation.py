"""Real Node fixture smoke and cleanup: no process/session leakage on exit."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.support.node import connected_real_node


@pytest.mark.skipif(
    subprocess.run(["sh", "-c", "command -v tmux"], capture_output=True).returncode != 0,
    reason="Real tmux needed",
)
def test_isolated_node_pair_has_real_core_workspace_and_owned_cleanup(
    tmp_path: Path,
) -> None:
    with connected_real_node(tmp_path) as target:
        assert target.core is not None and target.core.poll() is None
        assert target.node is not None and target.node.poll() is None
        assert target.available()
        assert target.client.get(f"/w/{target.workspace_id}/terminal").status_code == 200
        assert target.store.get_workspace(target.workspace_id) is not None
        assert target.store.get_computer(target.node_id)["connection_method"] == "node"
        assert target.managed_root.is_dir()
        owned_sessions = set(target.sessions)
        core_pid, node_pid = target.core.pid, target.node.pid
    # Never leave the fixture's real child processes or tmux sessions behind.
    for pid in (core_pid, node_pid):
        assert subprocess.run(
            ["sh", "-c", f"kill -0 {pid}"], capture_output=True,
        ).returncode != 0
    for session in owned_sessions:
        assert subprocess.run(
            ["tmux", "has-session", "-t", session], capture_output=True,
        ).returncode != 0
