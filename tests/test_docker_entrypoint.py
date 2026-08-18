from __future__ import annotations

import os
import subprocess
import time
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
        "sleep": (
            "#!/bin/sh\n"
            "if [ -n \"${TERMROOM_TEST_WAIT_READY:-}\" ]; then\n"
            "  : > \"$TERMROOM_TEST_WAIT_READY\"\n"
            "fi\n"
            "exec /bin/sleep 0.05\n"
        ),
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


def _wait_for_path(path: Path, *, timeout: float = 2) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for {path}")
        time.sleep(0.01)


def test_docker_entrypoint_selects_core_or_node_from_environment(tmp_path: Path) -> None:
    core = subprocess.run(
        ["/bin/sh", str(ENTRYPOINT), "/workspaces", "--foreground"],
        env=_fake_entrypoint_environment(tmp_path / "core", mode="core"),
        capture_output=True,
        text=True,
        check=False,
    )
    node_environment = _fake_entrypoint_environment(tmp_path / "node", mode="node")
    node_config = tmp_path / "node" / "config" / "custom-node"
    node_config.mkdir(parents=True)
    (node_config / "node.json").write_text("{}", encoding="utf-8")
    node_environment["TERMROOM_NODE_CONFIG_DIR"] = str(node_config)
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
    assert node.stdout.strip() == f"termroom node --config-dir {node_config}"


def test_unpaired_docker_node_waits_for_exec_pairing_config(tmp_path: Path) -> None:
    environment = _fake_entrypoint_environment(tmp_path, mode="node")
    node_config = tmp_path / "config" / "node"
    wait_ready = tmp_path / "wait-ready"
    environment["TERMROOM_NODE_CONFIG_DIR"] = str(node_config)
    environment["TERMROOM_TEST_WAIT_READY"] = str(wait_ready)
    process = subprocess.Popen(
        ["/bin/sh", str(ENTRYPOINT), "/workspaces", "--foreground"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        _wait_for_path(wait_ready)
        assert process.poll() is None
        node_config.mkdir(parents=True)
        (node_config / "node.json").write_text("{}", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=2)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)

    assert process.returncode == 0
    assert stderr == ""
    assert "Waiting for pairing configuration" in stdout
    assert f"termroom node --config-dir {node_config}" in stdout


def test_unpaired_docker_node_wait_exits_cleanly_on_sigterm(tmp_path: Path) -> None:
    environment = _fake_entrypoint_environment(tmp_path, mode="node")
    wait_ready = tmp_path / "wait-ready"
    environment["TERMROOM_NODE_CONFIG_DIR"] = str(tmp_path / "config" / "node")
    environment["TERMROOM_TEST_WAIT_READY"] = str(wait_ready)
    process = subprocess.Popen(
        ["/bin/sh", str(ENTRYPOINT), "/workspaces", "--foreground"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        _wait_for_path(wait_ready)
        assert process.poll() is None
        process.terminate()
        stdout, stderr = process.communicate(timeout=2)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)

    assert process.returncode == 0
    assert stderr == ""
    assert "Waiting for pairing configuration" in stdout
    assert "Pairing configuration found" not in stdout


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


def test_docker_entrypoint_enables_secure_cookie_for_https_proxy(tmp_path: Path) -> None:
    environment = _fake_entrypoint_environment(tmp_path, mode="core")
    environment["TERMROOM_SECURE_COOKIE"] = "true"

    result = subprocess.run(
        ["/bin/sh", str(ENTRYPOINT), "/workspaces", "--foreground"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "termroom /workspaces --foreground --secure-cookie"


def test_docker_entrypoint_rejects_invalid_secure_cookie_setting(tmp_path: Path) -> None:
    environment = _fake_entrypoint_environment(tmp_path, mode="core")
    environment["TERMROOM_SECURE_COOKIE"] = "sometimes"

    result = subprocess.run(
        ["/bin/sh", str(ENTRYPOINT), "/workspaces"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr.strip() == "TERMROOM_SECURE_COOKIE must be true or false."


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
