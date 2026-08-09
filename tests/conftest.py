from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolate_test_tmux(tmp_path_factory: pytest.TempPathFactory):
    """Keep integration tests off the user's real tmux server.

    Test commands can be launched from a shell that is itself inside tmux. In
    that case the inherited ``TMUX`` variable points every plain ``tmux``
    command at the user's live server. Use a private socket directory for the
    whole test session instead and tear that server down when pytest exits.
    """

    old_tmux = os.environ.pop("TMUX", None)
    old_tmux_tmpdir = os.environ.get("TMUX_TMPDIR")
    socket_root = Path(tmp_path_factory.mktemp("termroom-test-tmux"))
    socket_root.chmod(0o700)
    os.environ["TMUX_TMPDIR"] = str(socket_root)
    try:
        yield
    finally:
        if shutil.which("tmux"):
            subprocess.run(
                ["tmux", "kill-server"],
                check=False,
                capture_output=True,
                env=os.environ.copy(),
            )
        if old_tmux_tmpdir is None:
            os.environ.pop("TMUX_TMPDIR", None)
        else:
            os.environ["TMUX_TMPDIR"] = old_tmux_tmpdir
        if old_tmux is not None:
            os.environ["TMUX"] = old_tmux
