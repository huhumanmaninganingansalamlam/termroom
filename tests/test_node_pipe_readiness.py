"""Real tmux regression for a slow but valid Node output-pipe handshake.

This fixture intentionally delays the *ready* byte, not the command output.
It used to fail closed after only five seconds even if a healthy helper
became ready a moment later on a busy Linux machine.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from termroom.node_agent import NodeRuntime
from termroom.node_protocol import NODE_REMOTE_RUN_VERSION
from termroom.ssh_backend import REMOTE_RUN_SESSION_PREFIX


@pytest.mark.skipif(shutil.which("tmux") is None, reason="Real tmux required")
def test_real_node_delayed_log_pipe_readiness_does_not_lose_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    allowed = tmp_path / "projects"
    allowed.mkdir()
    target = tmp_path / "runs"
    node = NodeRuntime([allowed], remote_run_root=target)
    runtime = node.remote_runs
    assert runtime is not None
    run_id = str(uuid.uuid4())
    payload = {
        "remote_run_version": NODE_REMOTE_RUN_VERSION,
        "run_base": str(target), "run_id": run_id,
    }
    runtime.create({
        **payload,
        "cwd_rel": ".",
        "command": "printf 'slow-ready-한글\\n'; exit 0",
    })
    runtime.snapshot_begin(payload)
    runtime.snapshot_commit(payload)
    handoff = runtime._descriptor_handoff
    assert handoff is not None
    helper_code = '''import os, sys, time
from termroom.node_remote_runs import _node_remote_run_log_helper
original_write = os.write
def deliberately_delayed_ready(fd, data):
    if data == b"ready\\n":
        time.sleep(6.25)
    return original_write(fd, data)
os.write = deliberately_delayed_ready
sys.exit(_node_remote_run_log_helper(*sys.argv[1:]))
'''

    def handoff_with_delayed_helper(*args: Any, **kwargs: Any) -> Any:
        environment = dict(kwargs["environment"])
        environment["TERMROOM_REMOTE_RUN_LOG_HELPER_CODE"] = (
            f"exec({helper_code!r})"
        )
        kwargs["environment"] = environment
        return handoff(*args, **kwargs)

    monkeypatch.setattr(runtime, "_descriptor_handoff", handoff_with_delayed_helper)
    session = f"{REMOTE_RUN_SESSION_PREFIX}{run_id}"
    try:
        started = runtime.start(payload)
        assert started["state"] in {"preparing", "running", "finished"}
        # Observe actual Node process status, not a mocked transport receipt.
        deadline = time.monotonic() + 20
        completed = runtime.observe(payload)
        while completed["state"] not in {"finished", "failed", "lost"}:
            assert time.monotonic() < deadline, (
                f"Real Node pipe never completed: {completed}"
            )
            time.sleep(0.1)
            completed = runtime.observe(payload)
        assert completed["state"] == "finished", completed
        assert completed["exit_code"] == 0
        assert completed["log_incomplete"] is False
        metadata = target / run_id / ".termroom"
        assert "slow-ready-한글" in (
            metadata / "output.log"
        ).read_text(encoding="utf-8")
        seal = json.loads((metadata / "output-seal.json").read_text())
        assert seal["size"] == (metadata / "output.log").stat().st_size
        assert runtime.start(payload)["replayed"] is True
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", session],
            capture_output=True, check=False,
        )
