from __future__ import annotations

import asyncio
import json
import shutil
import signal
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from termroom import cli
from termroom.node_agent import (
    NodeAgent,
    NodeAgentError,
    NodeConfig,
    NodePermanentError,
    load_node_config,
    load_node_identity,
    save_node_config,
)
from termroom.node_protocol import NodeProtocolError, generate_private_key
from termroom.node_service import (
    NODE_PROCESS_LOCK_FILE,
    NODE_RUNTIME_STATUS_FILE,
    NODE_SERVICE_UNIT_MARKER,
    NodeProcessLock,
    NodeServiceError,
    NodeServiceManager,
    node_process_is_running,
    read_node_runtime_status,
    render_node_service_unit,
    write_node_runtime_status,
)


class _FakeSystemd(NodeServiceManager):
    def __init__(self, state_dir: Path, unit_dir: Path) -> None:
        super().__init__(
            state_dir,
            unit_dir=unit_dir,
            systemctl=sys.executable,
            loginctl=sys.executable,
        )
        self.enabled = False
        self.active = False
        self.failed = False
        self.calls: list[tuple[str, ...]] = []
        self.failures: dict[str, int] = {}

    def linger_state(self) -> str:
        return "disabled"

    def _systemctl(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        operation = arguments[0]
        remaining = self.failures.get(operation, 0)
        if remaining:
            self.failures[operation] = remaining - 1
            raise NodeServiceError(f"injected {operation} failure", code=f"{operation}_failed")
        if operation == "enable":
            self.enabled = True
        elif operation == "disable":
            self.enabled = False
        elif operation in {"start", "restart"}:
            self.active = True
            self.failed = False
        elif operation == "stop":
            self.active = False
        elif operation == "reset-failed":
            self.failed = False
        stdout = ""
        if operation == "show":
            installed = self.unit_path.exists()
            active_state = "active" if self.active else "failed" if self.failed else "inactive"
            stdout = "\n".join(
                [
                    f"LoadState={'loaded' if installed else 'not-found'}",
                    f"FragmentPath={self.unit_path if installed else ''}",
                    f"UnitFileState={'enabled' if self.enabled else 'disabled'}",
                    f"ActiveState={active_state}",
                    f"SubState={'running' if self.active else 'failed' if self.failed else 'dead'}",
                    "Result=success",
                    "ExecMainStatus=0",
                ]
            )
        return subprocess.CompletedProcess(
            [str(self.systemctl), "--user", *arguments], 0, stdout, ""
        )


def _command(state_dir: Path, *extra: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "termroom.cli",
        "node",
        "--state-dir",
        str(state_dir),
        *extra,
    ]


def test_node_service_unit_uses_structured_absolute_command_and_no_credentials(
    tmp_path: Path,
) -> None:
    hostile = tmp_path / 'state $HOME $(touch owned); "quoted" %i'
    content = render_node_service_unit(_command(hostile))

    assert content.startswith(f"{NODE_SERVICE_UNIT_MARKER}\n")
    assert f'ExecStart="{Path(sys.executable).absolute()}' in content
    assert " -m " not in content  # every argument is independently quoted
    assert '"-m" "termroom.cli" "node" "--state-dir"' in content
    assert "$$HOME" in content
    assert "$$(touch owned)" in content
    assert "%%i" in content
    assert "KillMode=process" in content
    assert "RestartPreventExitStatus=78" in content
    assert "TERMROOM_PASSWORD" in content
    assert "secret-value" not in content

    analyzer = shutil.which("systemd-analyze")
    if analyzer is not None:
        unit = tmp_path / "termroom-node.service"
        unit.write_text(content, encoding="utf-8")
        verified = subprocess.run(
            [analyzer, "verify", str(unit)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert verified.returncode == 0, verified.stderr


def test_node_service_command_preserves_virtualenv_executable_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(Path(sys.executable).resolve(strict=True))
    monkeypatch.setattr(cli.sys, "executable", str(executable))

    command = cli._node_service_command(tmp_path / "state")
    content = render_node_service_unit(command)

    assert command[0] == str(executable)
    assert f'ExecStart="{executable}"' in content
    assert str(executable.resolve(strict=True)) not in content


def test_node_process_lock_is_singleton_private_and_reusable(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    with NodeProcessLock(state_dir, "a" * 32):
        assert node_process_is_running(state_dir)
        with pytest.raises(NodeServiceError) as conflict, NodeProcessLock(state_dir, "a" * 32):
            pass
        assert conflict.value.code == "already_running"
        assert stat.S_IMODE((state_dir / NODE_PROCESS_LOCK_FILE).stat().st_mode) == 0o600

    assert not node_process_is_running(state_dir)
    with NodeProcessLock(state_dir, "a" * 32):
        assert node_process_is_running(state_dir)


def test_node_process_lock_rejects_symlink(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("not a lock", encoding="utf-8")
    (state_dir / NODE_PROCESS_LOCK_FILE).symlink_to(outside)

    with pytest.raises(NodeServiceError) as exc_info, NodeProcessLock(state_dir, "a" * 32):
        pass
    assert exc_info.value.code == "process_lock_invalid"


def test_node_runtime_status_is_private_and_checks_process_identity(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    write_node_runtime_status(state_dir, "a" * 32, "connected")

    result = read_node_runtime_status(state_dir)
    assert result is not None
    assert result["state"] == "connected"
    assert result["process_alive"] is True
    assert stat.S_IMODE((state_dir / NODE_RUNTIME_STATUS_FILE).stat().st_mode) == 0o600

    payload = json.loads((state_dir / NODE_RUNTIME_STATUS_FILE).read_text(encoding="utf-8"))
    payload["pid_start_ticks"] = "wrong"
    (state_dir / NODE_RUNTIME_STATUS_FILE).write_text(json.dumps(payload), encoding="utf-8")
    assert read_node_runtime_status(state_dir)["process_alive"] is False  # type: ignore[index]


def test_loading_missing_node_identity_does_not_create_a_new_key(tmp_path: Path) -> None:
    state_dir = tmp_path / "node"
    state_dir.mkdir()

    with pytest.raises(NodeAgentError) as exc_info:
        load_node_identity(state_dir)

    assert exc_info.value.code == "identity_missing"
    assert list(state_dir.iterdir()) == []


def test_node_run_root_is_local_absolute_configuration(tmp_path: Path) -> None:
    state_dir = tmp_path / "node"
    allowed = tmp_path / "projects"
    run_root = tmp_path / "scratch" / "runs"
    allowed.mkdir()
    save_node_config(
        state_dir,
        NodeConfig(
            "http://127.0.0.1:1",
            "a" * 32,
            "Node",
            (allowed,),
            state_dir,
            run_root,
        ),
    )

    loaded = load_node_config(state_dir)
    assert loaded.run_root == run_root
    payload = json.loads((state_dir / "node.json").read_text(encoding="utf-8"))
    payload["run_root"] = "relative/runs"
    (state_dir / "node.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(NodeAgentError) as invalid:
        load_node_config(state_dir)
    assert invalid.value.code == "config_invalid"


@pytest.mark.asyncio
async def test_permanent_protocol_failure_stops_reconnect_and_records_status(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    state_dir = tmp_path / "state"
    root.mkdir()
    agent = NodeAgent(
        NodeConfig("http://127.0.0.1:1", "a" * 32, "Node", (root,), state_dir),
        generate_private_key(),
    )

    async def incompatible() -> None:
        raise NodeProtocolError("Update Termroom", code="version_incompatible")

    agent.run_once = incompatible  # type: ignore[method-assign]
    with pytest.raises(NodePermanentError) as exc_info:
        await agent.run_forever()

    assert exc_info.value.code == "version_incompatible"
    status = read_node_runtime_status(state_dir)
    assert status is not None
    assert status["state"] == "error"
    assert status["error_code"] == "version_incompatible"


@pytest.mark.asyncio
async def test_node_cli_sigterm_path_cancels_agent_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    canceled = False
    captured: dict[int, Callable[[], None]] = {}

    class FakeAgent:
        async def run_forever(self) -> None:
            nonlocal canceled
            started.set()
            try:
                await asyncio.Future()
            finally:
                canceled = True

    def add_handler(kind: int, callback: Callable[[], None]) -> None:
        captured[kind] = callback

    def remove_handler(kind: int) -> bool:
        return captured.pop(kind, None) is not None

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", add_handler)
    monkeypatch.setattr(loop, "remove_signal_handler", remove_handler)
    task = asyncio.create_task(cli._run_node_agent(FakeAgent()))  # type: ignore[arg-type]
    await started.wait()
    captured[signal.SIGTERM]()
    await task

    assert canceled is True


def test_install_service_is_idempotent_and_uninstall_preserves_identity(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    unit_dir = tmp_path / "units"
    state_dir.mkdir()
    identity = state_dir / "node-key.pem"
    config = state_dir / "node.json"
    identity.write_text("private identity", encoding="utf-8")
    config.write_text("paired config", encoding="utf-8")
    manager = _FakeSystemd(state_dir, unit_dir)

    first = manager.install(_command(state_dir))
    second = manager.install(_command(state_dir))

    assert first.installed and first.enabled and first.active
    assert second.installed and second.enabled and second.active
    assert sum(call[0] == "start" for call in manager.calls) == 1
    assert sum(call[0] == "enable" for call in manager.calls) == 1
    unit_content = manager.unit_path.read_text(encoding="utf-8")
    assert str(identity) not in unit_content
    assert "private identity" not in unit_content

    removed = manager.uninstall()
    assert not removed.installed
    assert not removed.enabled
    assert not removed.active
    assert identity.read_text(encoding="utf-8") == "private identity"
    assert config.read_text(encoding="utf-8") == "paired config"


def test_uninstall_resets_a_failed_owned_unit(tmp_path: Path) -> None:
    manager = _FakeSystemd(tmp_path / "state", tmp_path / "units")
    manager.install(_command(tmp_path / "state"))
    manager.active = False
    manager.failed = True

    removed = manager.uninstall()

    assert not removed.installed
    assert manager.failed is False
    assert ("reset-failed", "termroom-node.service") in manager.calls


def test_install_failure_removes_new_partial_service_state(tmp_path: Path) -> None:
    manager = _FakeSystemd(tmp_path / "state", tmp_path / "units")
    manager.failures["start"] = 1

    with pytest.raises(NodeServiceError, match="injected start failure"):
        manager.install(_command(tmp_path / "state"))

    assert not manager.unit_path.exists()
    assert manager.enabled is False
    assert manager.active is False


def test_reinstall_failure_restores_previous_working_unit(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    manager = _FakeSystemd(state_dir, tmp_path / "units")
    manager.install(_command(state_dir, "old"))
    previous = manager.unit_path.read_text(encoding="utf-8")
    manager.failures["restart"] = 1

    with pytest.raises(NodeServiceError, match="injected restart failure"):
        manager.install(_command(state_dir, "new"))

    assert manager.unit_path.read_text(encoding="utf-8") == previous
    assert manager.enabled is True
    assert manager.active is True


def test_service_manager_rejects_foreign_unit(tmp_path: Path) -> None:
    manager = _FakeSystemd(tmp_path / "state", tmp_path / "units")
    manager.unit_path.parent.mkdir(parents=True)
    manager.unit_path.write_text("[Service]\nExecStart=/bin/false\n", encoding="utf-8")

    with pytest.raises(NodeServiceError) as exc_info:
        manager.install(_command(tmp_path / "state"))

    assert exc_info.value.code == "unit_conflict"
    assert manager.unit_path.read_text(encoding="utf-8").startswith("[Service]")


def test_service_status_distinguishes_process_and_core_connection(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    manager = _FakeSystemd(state_dir, tmp_path / "units")
    manager.install(_command(state_dir))
    write_node_runtime_status(state_dir, "a" * 32, "connected")

    status = manager.status()
    assert status.active is True
    assert status.service_state == "active/running"
    assert status.core_state == "connected"
    assert status.linger == "disabled"


def test_unavailable_systemd_and_unpaired_cli_leave_no_partial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    manager = _FakeSystemd(state_dir, tmp_path / "units")
    manager.failures["show-environment"] = 1
    with pytest.raises(NodeServiceError):
        manager.install(_command(state_dir))
    assert not manager.unit_path.exists()

    parser = cli._build_node_parser()
    args = parser.parse_args(["--state-dir", str(state_dir), "install-service"])
    monkeypatch.setattr(cli, "NodeServiceManager", lambda _state_dir: manager)
    with pytest.raises(SystemExit) as exc_info:
        cli._run_node(parser, args)
    assert exc_info.value.code == 2
    assert not (state_dir / "node-key.pem").exists()


def test_node_cli_parser_exposes_only_the_service_lifecycle_commands() -> None:
    parser = cli._build_node_parser()
    assert parser.parse_args(["install-service"]).node_command == "install-service"
    assert parser.parse_args(["status"]).node_command == "status"
    assert parser.parse_args(["uninstall-service"]).node_command == "uninstall-service"
