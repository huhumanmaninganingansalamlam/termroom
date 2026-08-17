from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from termroom.db import StateStore
from termroom.terminals import TerminalManager
from termroom.workspaces import RootManager, WorkspaceManager


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_fast_file_run_keeps_output_instead_of_dead_pane_overlay(tmp_path: Path) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = WorkspaceManager(RootManager(root), store).open("project")
    terminals = TerminalManager(store)
    run_id = str(uuid.uuid4())
    metadata_dir = tmp_path / "runs" / run_id

    try:
        terminal = terminals.start_file_run(
            workspace,
            run_id=run_id,
            runner_id="sh",
            runtime_error_code="shell_missing",
            argv=("/bin/sh", "-c", "printf FAST_FILE_RUN_OUTPUT_OK"),
            metadata_dir=metadata_dir,
        )

        deadline = time.monotonic() + 5
        state: dict[str, object] = {}
        while time.monotonic() < deadline:
            state = terminals.inspect_file_run(
                workspace,
                run_id=run_id,
                metadata_dir=metadata_dir,
            )
            if state.get("state") == "finished":
                break
            time.sleep(0.05)

        assert state.get("state") == "finished"
        option = subprocess.run(
            [
                "tmux",
                "show-window-options",
                "-v",
                "-t",
                str(terminal["tmux_window"]),
                "remain-on-exit-format",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert option.stdout == "\n"
        assert "FAST_FILE_RUN_OUTPUT_OK" in terminals.capture_scrollback(
            workspace, terminal
        )
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", str(workspace["tmux_session"])],
            check=False,
            capture_output=True,
        )
