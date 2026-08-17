from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import shutil
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from termroom.security import ensure_private_directory

NODE_SERVICE_UNIT_NAME = "termroom-node.service"
NODE_SERVICE_UNIT_MARKER = "# Managed by Termroom."
NODE_PROCESS_LOCK_FILE = "node-process.lock"
NODE_RUNTIME_STATUS_FILE = "node-runtime.json"
NODE_PERMANENT_EXIT_STATUS = 78
NODE_RUNTIME_STATES = frozenset(
    {"starting", "connecting", "connected", "disconnected", "error", "stopped"}
)


class NodeServiceError(RuntimeError):
    def __init__(self, message: str, *, code: str = "node_service_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class NodeServiceStatus:
    installed: bool
    enabled: bool
    active: bool
    service_state: str
    core_state: str
    linger: str
    last_error_code: str | None = None


class NodeProcessLock:
    def __init__(self, state_dir: Path, node_id: str) -> None:
        self.state_dir = state_dir.resolve(strict=False)
        self.node_id = node_id
        self._fd: int | None = None

    def __enter__(self) -> NodeProcessLock:
        ensure_private_directory(self.state_dir)
        path = self.state_dir / NODE_PROCESS_LOCK_FILE
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise NodeServiceError(
                "Node process lock is unavailable", code="process_lock_invalid"
            ) from exc
        try:
            _validate_private_lock_fd(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise NodeServiceError(
                    "Termroom Node is already running for this configuration.",
                    code="already_running",
                ) from exc
            metadata = json.dumps(
                {
                    "node_id": self.node_id,
                    "pid": os.getpid(),
                    "pid_start_ticks": _pid_start_ticks(os.getpid()),
                    "started_at": _utc_now(),
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            os.ftruncate(fd, 0)
            os.write(fd, metadata)
            os.fsync(fd)
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, *_args: object) -> None:
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def node_process_is_running(state_dir: Path) -> bool:
    path = state_dir.resolve(strict=False) / NODE_PROCESS_LOCK_FILE
    flags = os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise NodeServiceError(
            "Node process lock is unavailable", code="process_lock_invalid"
        ) from exc
    try:
        _validate_private_lock_fd(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def write_node_runtime_status(
    state_dir: Path,
    node_id: str,
    state: str,
    *,
    error_code: str | None = None,
) -> None:
    if state not in NODE_RUNTIME_STATES:
        raise ValueError(f"Unsupported Node runtime state: {state}")
    payload = {
        "node_id": node_id,
        "pid": os.getpid(),
        "pid_start_ticks": _pid_start_ticks(os.getpid()),
        "state": state,
        "error_code": error_code,
        "updated_at": _utc_now(),
    }
    _atomic_private_write(
        state_dir.resolve(strict=False) / NODE_RUNTIME_STATUS_FILE,
        (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8"),
    )


def read_node_runtime_status(state_dir: Path) -> dict[str, Any] | None:
    path = state_dir.resolve(strict=False) / NODE_RUNTIME_STATUS_FILE
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise NodeServiceError(
                "Node runtime status path is invalid", code="runtime_status_invalid"
            )
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NodeServiceError(
            "Node runtime status is invalid", code="runtime_status_invalid"
        ) from exc
    if not isinstance(value, dict) or value.get("state") not in NODE_RUNTIME_STATES:
        raise NodeServiceError("Node runtime status is invalid", code="runtime_status_invalid")
    value["process_alive"] = _process_identity_matches(value)
    return value


class NodeServiceManager:
    def __init__(
        self,
        state_dir: Path,
        *,
        unit_dir: Path | None = None,
        systemctl: str | Path | None = None,
        loginctl: str | Path | None = None,
    ) -> None:
        self.state_dir = state_dir.resolve(strict=False)
        self.unit_dir = (unit_dir or _default_user_unit_dir()).resolve(strict=False)
        self.unit_path = self.unit_dir / NODE_SERVICE_UNIT_NAME
        self.systemctl = _resolve_command(systemctl, "systemctl")
        self.loginctl = _resolve_command(loginctl, "loginctl", required=False)

    def install(self, command: Sequence[str]) -> NodeServiceStatus:
        self._ensure_manager()
        before = self._show()
        previous = self._owned_unit_content(optional=True)
        self._reject_foreign_fragment(before, previous)
        was_enabled = before.get("UnitFileState") == "enabled"
        was_active = before.get("ActiveState") == "active"
        if not was_active and node_process_is_running(self.state_dir):
            raise NodeServiceError(
                "Stop the foreground Termroom Node before installing the service.",
                code="already_running",
            )
        content = render_node_service_unit(command)
        if previous == content and was_enabled and was_active:
            return self.status()

        wrote_unit = previous != content
        if wrote_unit:
            _atomic_unit_write(self.unit_path, content)
        try:
            self._systemctl("daemon-reload")
            if not was_enabled:
                self._systemctl("enable", NODE_SERVICE_UNIT_NAME)
            if was_active:
                if wrote_unit:
                    self._systemctl("restart", NODE_SERVICE_UNIT_NAME)
            else:
                self._systemctl("start", NODE_SERVICE_UNIT_NAME)
            current = self.status()
            if not current.enabled or not current.active:
                raise NodeServiceError(
                    "Termroom Node service did not become enabled and active",
                    code="service_start_failed",
                )
            return current
        except Exception as exc:
            self._rollback(previous, was_enabled=was_enabled, was_active=was_active)
            if isinstance(exc, NodeServiceError):
                raise
            raise NodeServiceError(
                "Termroom Node service installation failed", code="service_install_failed"
            ) from exc

    def uninstall(self) -> NodeServiceStatus:
        self._ensure_manager()
        before = self._show()
        previous = self._owned_unit_content(optional=True)
        self._reject_foreign_fragment(before, previous)
        if previous is None:
            return self.status()
        was_enabled = before.get("UnitFileState") == "enabled"
        was_active = before.get("ActiveState") == "active"
        try:
            if before.get("ActiveState") == "failed":
                self._systemctl("reset-failed", NODE_SERVICE_UNIT_NAME)
            if was_active:
                self._systemctl("stop", NODE_SERVICE_UNIT_NAME)
            if was_enabled:
                self._systemctl("disable", NODE_SERVICE_UNIT_NAME)
            self.unit_path.unlink()
            self._systemctl("daemon-reload")
        except Exception as exc:
            self._rollback(previous, was_enabled=was_enabled, was_active=was_active)
            if isinstance(exc, NodeServiceError):
                raise
            raise NodeServiceError(
                "Termroom Node service removal failed", code="service_uninstall_failed"
            ) from exc
        return self.status()

    def status(self) -> NodeServiceStatus:
        self._ensure_manager()
        properties = self._show()
        content = self._owned_unit_content(optional=True)
        self._reject_foreign_fragment(properties, content)
        installed = content is not None
        enabled = installed and properties.get("UnitFileState") == "enabled"
        active = installed and properties.get("ActiveState") == "active"
        sub_state = str(properties.get("SubState") or "dead")
        service_state = f"{properties.get('ActiveState') or 'inactive'}/{sub_state}"
        runtime = read_node_runtime_status(self.state_dir) if installed else None
        core_state = "stopped"
        last_error_code: str | None = None
        if runtime is not None:
            runtime_state = str(runtime["state"])
            last_error_code = str(runtime.get("error_code")) if runtime.get("error_code") else None
            if active and runtime.get("process_alive"):
                core_state = runtime_state
            elif runtime_state == "error":
                core_state = "error"
        return NodeServiceStatus(
            installed=installed,
            enabled=enabled,
            active=active,
            service_state=service_state,
            core_state=core_state,
            linger=self.linger_state(),
            last_error_code=last_error_code,
        )

    def linger_state(self) -> str:
        if self.loginctl is None:
            return "unknown"
        result = subprocess.run(
            [
                str(self.loginctl),
                "show-user",
                str(os.getuid()),
                "--property=Linger",
                "--value",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return "unknown"
        value = result.stdout.strip().casefold()
        return "enabled" if value == "yes" else "disabled" if value == "no" else "unknown"

    def _ensure_manager(self) -> None:
        self._systemctl("show-environment")

    def _show(self) -> dict[str, str]:
        result = self._systemctl(
            "show",
            NODE_SERVICE_UNIT_NAME,
            "--property=LoadState",
            "--property=FragmentPath",
            "--property=UnitFileState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=Result",
            "--property=ExecMainStatus",
            "--no-pager",
        )
        properties: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                properties[key] = value
        return properties

    def _systemctl(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(self.systemctl), "--user", *arguments],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            message = result.stderr.strip() or "systemd user manager rejected the request"
            raise NodeServiceError(message[:500], code="systemd_unavailable")
        return result

    def _owned_unit_content(self, *, optional: bool) -> str | None:
        try:
            info = self.unit_path.lstat()
        except FileNotFoundError:
            if optional:
                return None
            raise
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise NodeServiceError(
                "Termroom Node unit path is not a regular file", code="unit_conflict"
            )
        content = self.unit_path.read_text(encoding="utf-8")
        if not content.startswith(f"{NODE_SERVICE_UNIT_MARKER}\n"):
            raise NodeServiceError(
                "An unmanaged systemd unit already uses the Termroom Node name.",
                code="unit_conflict",
            )
        return content

    def _reject_foreign_fragment(
        self, properties: dict[str, str], local_content: str | None
    ) -> None:
        fragment = str(properties.get("FragmentPath") or "")
        if local_content is None and fragment:
            try:
                same_path = Path(fragment).resolve(strict=False) == self.unit_path
            except OSError:
                same_path = False
            if not same_path:
                raise NodeServiceError(
                    "Another systemd unit already uses the Termroom Node name.",
                    code="unit_conflict",
                )

    def _rollback(self, previous: str | None, *, was_enabled: bool, was_active: bool) -> None:
        with contextlib.suppress(NodeServiceError):
            self._systemctl("stop", NODE_SERVICE_UNIT_NAME)
        with contextlib.suppress(NodeServiceError):
            self._systemctl("disable", NODE_SERVICE_UNIT_NAME)
        try:
            if previous is None:
                self.unit_path.unlink(missing_ok=True)
            else:
                _atomic_unit_write(self.unit_path, previous)
        except OSError:
            return
        with contextlib.suppress(NodeServiceError):
            self._systemctl("daemon-reload")
        if was_enabled:
            with contextlib.suppress(NodeServiceError):
                self._systemctl("enable", NODE_SERVICE_UNIT_NAME)
        if was_active:
            with contextlib.suppress(NodeServiceError):
                self._systemctl("start", NODE_SERVICE_UNIT_NAME)


def render_node_service_unit(command: Sequence[str]) -> str:
    values = [str(value) for value in command]
    if not values:
        raise NodeServiceError("Node service command is empty", code="service_command_invalid")
    executable = Path(values[0])
    if not executable.is_absolute():
        raise NodeServiceError(
            "Node service executable must be an absolute executable file",
            code="service_command_invalid",
        )
    executable = Path(os.path.abspath(executable))
    try:
        executable_info = executable.stat()
    except OSError as exc:
        raise NodeServiceError(
            "Node service executable is unavailable", code="service_command_invalid"
        ) from exc
    if not stat.S_ISREG(executable_info.st_mode) or not os.access(executable, os.X_OK):
        raise NodeServiceError(
            "Node service executable must be an absolute executable file",
            code="service_command_invalid",
        )
    values[0] = str(executable)
    exec_start = " ".join(_systemd_quote(value) for value in values)
    return (
        f"{NODE_SERVICE_UNIT_MARKER}\n"
        "[Unit]\n"
        "Description=Termroom Node\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n"
        "StartLimitIntervalSec=60s\n"
        "StartLimitBurst=5\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=5s\n"
        f"RestartPreventExitStatus={NODE_PERMANENT_EXIT_STATUS}\n"
        "KillMode=process\n"
        "TimeoutStopSec=15s\n"
        "UMask=0077\n"
        "UnsetEnvironment=TERMROOM_PASSWORD TERMROOM_SSH_CREDENTIAL_ID\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _default_user_unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    config_home = Path(base).expanduser() if base else Path.home() / ".config"
    return config_home / "systemd" / "user"


def _resolve_command(value: str | Path | None, name: str, *, required: bool = True) -> Path | None:
    candidate = str(value) if value is not None else shutil.which(name)
    if not candidate:
        if required:
            raise NodeServiceError(
                f"{name} is required for the Node user service",
                code="systemd_unavailable",
            )
        return None
    try:
        path = Path(candidate).resolve(strict=True)
    except OSError as exc:
        if required:
            raise NodeServiceError(f"{name} is unavailable", code="systemd_unavailable") from exc
        return None
    if not path.is_file() or not os.access(path, os.X_OK):
        if required:
            raise NodeServiceError(f"{name} is unavailable", code="systemd_unavailable")
        return None
    return path


def _systemd_quote(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise NodeServiceError(
            "Node service argument contains an unsupported character",
            code="service_command_invalid",
        )
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$").replace("%", "%%")
    return f'"{escaped}"'


def _validate_private_lock_fd(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise NodeServiceError(
            "Node process lock is not a regular file", code="process_lock_invalid"
        )
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise NodeServiceError(
            "Node process lock belongs to another user", code="process_lock_invalid"
        )
    os.fchmod(fd, 0o600)


def _atomic_private_write(path: Path, content: bytes) -> None:
    ensure_private_directory(path.parent)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(6)}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_unit_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(6)}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _process_identity_matches(value: dict[str, Any]) -> bool:
    try:
        pid = int(value.get("pid", 0))
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    expected = str(value.get("pid_start_ticks") or "")
    actual = _pid_start_ticks(pid)
    return bool(actual) and (not expected or actual == expected)


def _pid_start_ticks(pid: int) -> str:
    try:
        parts = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    except OSError:
        return ""
    return parts[21] if len(parts) > 21 else ""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
