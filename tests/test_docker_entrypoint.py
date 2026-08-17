from __future__ import annotations

import os
import subprocess
from pathlib import Path

ENTRYPOINT = Path(__file__).parents[1] / "docker" / "termroom-entrypoint.sh"


def _fake_entrypoint_environment(tmp_path: Path, *, mode: str) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    commands = {
        "id": "#!/bin/sh\nprintf '1000\\n'\n",
        "mkdir": "#!/bin/sh\nexit 0\n",
        "chown": "#!/bin/sh\nexit 0\n",
        "chmod": "#!/bin/sh\nexit 0\n",
        "gosu": "#!/bin/sh\nshift\nprintf '%s\\n' \"$*\"\n",
    }
    for name, source in commands.items():
        path = fake_bin / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "PUID": "1000",
        "PGID": "1000",
        "TERMROOM_MODE": mode,
    }


def test_docker_entrypoint_selects_core_or_node_from_environment(tmp_path: Path) -> None:
    core = subprocess.run(
        ["/bin/sh", str(ENTRYPOINT), "/workspaces", "--foreground"],
        env=_fake_entrypoint_environment(tmp_path / "core", mode="core"),
        capture_output=True,
        text=True,
        check=False,
    )
    node_environment = _fake_entrypoint_environment(tmp_path / "node", mode="node")
    node_environment["TERMROOM_NODE_CONFIG_DIR"] = "/config/custom-node"
    node = subprocess.run(
        ["/bin/sh", str(ENTRYPOINT), "/workspaces", "--foreground"],
        env=node_environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert core.returncode == 0
    assert core.stdout.strip() == "termroom /workspaces --foreground"
    assert node.returncode == 0
    assert node.stdout.strip() == "termroom node --config-dir /config/custom-node"


def test_docker_entrypoint_preserves_explicit_node_pair_command(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "/bin/sh",
            str(ENTRYPOINT),
            "node",
            "--config-dir",
            "/config/node",
            "pair",
            "--core",
            "https://core.example",
            "--code",
            "ONE-TIME-CODE",
            "--allow-root",
            "/workspace",
        ],
        env=_fake_entrypoint_environment(tmp_path, mode="node"),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == (
        "termroom node --config-dir /config/node pair --core https://core.example "
        "--code ONE-TIME-CODE --allow-root /workspace"
    )


def test_docker_entrypoint_rejects_unknown_mode(tmp_path: Path) -> None:
    result = subprocess.run(
        ["/bin/sh", str(ENTRYPOINT), "/workspaces"],
        env=_fake_entrypoint_environment(tmp_path, mode="worker"),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr.strip() == "TERMROOM_MODE must be either core or node."
