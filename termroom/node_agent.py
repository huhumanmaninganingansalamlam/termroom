from __future__ import annotations

import array
import asyncio
import base64
import contextlib
import ctypes
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import stat as stat_module
import struct
import subprocess
import sys
import termios
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from termroom.file_runs import RUNNER_REGISTRY_VERSION, resolve_runner
from termroom.files import (
    DEFAULT_FILE_SEARCH_MAX_ENTRIES,
    DEFAULT_FILE_SEARCH_MAX_MATCHES,
    DEFAULT_FILE_SEARCH_MAX_SECONDS,
    DirectoryListingLimitError,
    FileConflictError,
    FileService,
    RunnableFile,
    UnsupportedFileError,
)
from termroom.node_protocol import (
    MAX_NODE_MESSAGE_BYTES,
    MAX_NODE_STREAM_CHUNK_BYTES,
    NODE_CAPABILITIES,
    NODE_PROTOCOL_VERSION,
    NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW,
    NODE_REMOTE_RUN_SOURCE_VERSION,
    NODE_REQUEST_DEFAULT_BUDGET_MS,
    NODE_REQUIRED_CAPABILITIES,
    NODE_WORKSPACE_COMMAND_VERSION,
    NODE_WORKSPACE_USAGE_VERSION,
    NodeProtocolError,
    control_websocket_url,
    decode_message,
    encode_message,
    generate_private_key,
    load_private_key,
    normalize_core_url,
    private_key_pem,
    public_key_fingerprint,
    public_key_text,
    sign_challenge,
    validate_node_id,
    validate_protocol_version,
    validate_request_budget_ms,
    validate_request_id,
    validate_request_operation,
)
from termroom.node_remote_runs import (
    NodeRemoteRunError,
    NodeRemoteRunMetadataStream,
    NodeRemoteRunRuntime,
    NodeRemoteRunUploadStream,
)
from termroom.node_service import NodeServiceError, write_node_runtime_status
from termroom.pty_process import spawn_pty_process
from termroom.run_sources import (
    SourceFileChangedError,
    SourceValidationError,
    WorkspaceEntry,
    WorkspaceManifest,
    is_default_workspace_excluded,
    iter_stable_local_file_chunks,
    normalize_explicit_include_paths,
    normalize_source_relative_path,
    scan_local_workspace,
)
from termroom.security import (
    PathBoundaryError,
    is_within,
    resolve_inside,
    resolve_no_symlink_inside,
)
from termroom.terminals import (
    FILE_RUN_WRAPPER_SCRIPT,
    TERMINAL_EDITOR_WRAPPER,
    TMUX_BROWSER_SIZE_FORMAT,
    TMUX_BROWSER_VIEW_PREFIX,
    TMUX_MANAGED_RUN_OPTION,
    TMUX_TERMINAL_EDITOR_DIGEST_OPTION,
    TMUX_TERMINAL_EDITOR_RECORD_FORMAT,
    TMUX_TERMINAL_RECORD_FORMAT,
    TMUX_TERMINAL_ROLE_OPTION,
    TMUX_WORKSPACE_COMMAND_RECORD_FORMAT,
    WORKSPACE_COMMAND_READY_POLL_SECONDS,
    WORKSPACE_COMMAND_READY_TIMEOUT_SECONDS,
    WORKSPACE_COMMAND_WRAPPER_ARGV,
    clear_tmux_workspace_command_identity,
    file_run_completion_grace_active,
    file_run_completion_was_stopped,
    file_run_dead_pane_fallback,
    freeze_tmux_window_size,
    normalize_terminal_editor_path,
    normalize_terminal_name,
    normalize_workspace_command,
    parse_tmux_terminal_editor_records,
    parse_tmux_terminal_records,
    parse_tmux_workspace_command_records,
    set_tmux_browser_view_grid_resize,
    terminal_editor_digest,
    tmux_browser_view_session,
    validate_workspace_command_launch,
    validate_workspace_command_slot,
    wait_tmux_browser_grid_size,
    workspace_command_digest,
    workspace_command_history_name,
    workspace_command_record_is_ready,
)
from termroom.workspace_usage import (
    WorkspaceUsageCollectionError,
    raw_workspace_usage_payload,
    read_system_process_output,
    workspace_usage_from_outputs,
)
from termroom.workspaces import ProjectPathExists, validate_project_name

NODE_CONFIG_FILE = "node.json"
NODE_PRIVATE_KEY_FILE = "node-key.pem"
NODE_HEARTBEAT_SECONDS = 10.0
NODE_RECONNECT_MAX_SECONDS = 30.0
NODE_RECONNECT_JITTER_PER_MILLE = 200
NODE_FILE_LIST_MAX_ENTRIES = 10_000
NODE_FILE_LIST_MAX_METADATA_BYTES = 768 * 1024


def node_reconnect_delay(base_seconds: float) -> float:
    """Return bounded ±20% jitter so many Nodes do not reconnect in lockstep."""

    base = max(0.25, min(float(base_seconds), NODE_RECONNECT_MAX_SECONDS))
    offset = secrets.randbelow(NODE_RECONNECT_JITTER_PER_MILLE * 2 + 1)
    factor = 1 + (offset - NODE_RECONNECT_JITTER_PER_MILLE) / 1000
    return max(0.25, min(base * factor, NODE_RECONNECT_MAX_SECONDS))


NODE_MAX_CONCURRENT_REQUESTS = 8
NODE_LEGACY_SESSION_PATTERN = re.compile(r"^termroom-[A-Za-z0-9_-]{1,112}$")
NODE_WINDOW_PATTERN = re.compile(r"^@[0-9]+$")
NODE_WORKSPACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
FILE_RUN_DIGEST_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_TMUX_DESCRIPTOR_LAUNCHER = r"""import array, json, os, socket, sys
endpoint, config_json = sys.argv[1:]
channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    channel.connect("\0" + endpoint)
    config = json.loads(config_json)
    count = len(config["expected"])
    payload, ancillary, flags, _ = channel.recvmsg(
        1, socket.CMSG_SPACE(count * array.array("i").itemsize),
        getattr(socket, "MSG_CMSG_CLOEXEC", 0),
    )
    received = array.array("i")
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            received.frombytes(data[: len(data) - len(data) % received.itemsize])
    if payload != b"F" or flags & getattr(socket, "MSG_CTRUNC", 0) or len(received) != count:
        raise RuntimeError("invalid descriptor handoff")
    for descriptor, expected in zip(received, config["expected"], strict=True):
        info = os.fstat(descriptor)
        observed = [info.st_dev, info.st_ino, info.st_mode & 0o170000, info.st_uid]
        if info.st_mode & 0o170000 == 0o100000:
            observed.append(info.st_nlink)
        if observed != expected:
            raise RuntimeError("descriptor identity changed")
    os.fchdir(received[config["cwd"]])
    inherited = set(config["inherit"])
    for index, descriptor in enumerate(received):
        os.set_inheritable(descriptor, index in inherited)
    def resolve(value):
        for index, descriptor in enumerate(received):
            value = value.replace("{fd:" + str(index) + "}", "/proc/self/fd/" + str(descriptor))
        return value
    command = [resolve(value) for value in config["command"]]
    environment = os.environ.copy()
    environment.update({key: resolve(value) for key, value in config["environment"].items()})
    channel.sendall(b"R")
    channel.close()
    if command:
        os.execvpe(command[0], command, environment)
    shell = environment.get("SHELL") or "/bin/sh"
    os.execlp(shell, shell)
except BaseException:
    try:
        channel.sendall(b"E")
    except OSError:
        pass
    raise
"""


def node_session_is_valid(session: str) -> bool:
    """Accept legacy sessions and the canonical short Workspace session form."""

    value = str(session)
    if NODE_LEGACY_SESSION_PATTERN.fullmatch(value):
        return True
    if not value.startswith("tr-"):
        return False
    try:
        slug, suffix = value[3:].rsplit("-", 1)
    except ValueError:
        return False
    return (
        1 <= len(slug) <= 16
        and slug == slug.strip("-")
        and "--" not in slug
        and all(character.isalnum() or character == "-" for character in slug)
        and len(suffix) == 4
        and all(character in "0123456789abcdef" for character in suffix)
    )


class NodeAgentError(RuntimeError):
    def __init__(self, message: str, *, code: str = "node_agent_error") -> None:
        super().__init__(message)
        self.code = code


class NodePermanentError(NodeAgentError):
    """A local identity or protocol failure that reconnecting cannot repair."""


def _make_sealed_memfd(content: bytes, name: str, *, mode: int = 0o400) -> int:
    flags = getattr(os, "MFD_CLOEXEC", 1) | getattr(os, "MFD_ALLOW_SEALING", 2)
    create_memfd = getattr(os, "memfd_create", None)
    if create_memfd is None:
        try:
            create_memfd = ctypes.CDLL(None, use_errno=True).memfd_create
        except (AttributeError, OSError) as exc:
            raise NodeAgentError(
                "Immutable File Run execution descriptors are unavailable",
                code="capability_unsupported",
            ) from exc
        create_memfd.argtypes = (ctypes.c_char_p, ctypes.c_uint)
        create_memfd.restype = ctypes.c_int
        descriptor = create_memfd(name.encode(), flags)
        if descriptor < 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
    else:
        descriptor = create_memfd(name, flags)
    try:
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write to immutable execution descriptor")
            remaining = remaining[written:]
        os.fchmod(descriptor, mode)
        os.lseek(descriptor, 0, os.SEEK_SET)
        seals = (
            getattr(fcntl, "F_SEAL_WRITE", 0x0008)
            | getattr(fcntl, "F_SEAL_GROW", 0x0004)
            | getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
            | getattr(fcntl, "F_SEAL_SEAL", 0x0001)
        )
        fcntl.fcntl(descriptor, getattr(fcntl, "F_ADD_SEALS", 1033), seals)
        if fcntl.fcntl(descriptor, getattr(fcntl, "F_GET_SEALS", 1034)) & seals != seals:
            raise OSError("execution descriptor could not be sealed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@dataclass(frozen=True, slots=True)
class NodeConfig:
    core_url: str
    node_id: str
    name: str
    allowed_roots: tuple[Path, ...]
    state_dir: Path | None = None
    run_root: Path | None = None
    ca_file: Path | None = None
    state_dir_identity: tuple[int, int] | None = field(default=None, compare=False, repr=False)


@dataclass(slots=True)
class OperationResult:
    value: dict[str, Any]
    start: Callable[[], Awaitable[None]] | None = None


_PRIVATE_STATE_UNSPECIFIED = object()


@dataclass(frozen=True, slots=True)
class _WorkspaceSourceContext:
    workspace_id: str
    workspace_root: Path
    source_root: Path
    source_path: str
    explicitly_included: frozenset[str]


class AgentStream(Protocol):
    async def feed(self, chunk: bytes) -> None: ...

    async def control(self, kind: str, values: Mapping[str, Any]) -> None: ...

    async def close(self) -> dict[str, Any] | None: ...


def _open_directory_components(
    path: Path,
    *,
    create: bool,
    mode: int | None,
) -> tuple[int, Path]:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("no-follow directory handles are unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    path = Path(os.path.abspath(os.fspath(path.expanduser())))
    parts = path.parts
    if len(parts) < 2:
        raise OSError("private directory cannot be the filesystem root")
    descriptor = os.open(path.anchor, flags & ~os.O_NOFOLLOW)
    try:
        for index, component in enumerate(parts[1:], start=1):
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, 0o700, dir_fd=descriptor)
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            if index == len(parts) - 1:
                if os.fstat(descriptor).st_uid != os.geteuid():
                    raise OSError("private directory is not owned by the Node user")
                if mode is not None:
                    os.fchmod(descriptor, mode)
        return descriptor, path
    except BaseException:
        os.close(descriptor)
        raise


def _open_private_directory(path: Path, *, create: bool) -> tuple[int, Path]:
    return _open_directory_components(path, create=create, mode=0o700)


def _open_directory_at(
    directory_fd: int,
    components: Sequence[str],
    *,
    create: bool,
    mode: int | None,
    require_owner: bool = False,
) -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("no-follow directory handles are unavailable")
    descriptor = os.dup(directory_fd)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        for index, component in enumerate(components):
            if not component or component in {".", ".."} or "/" in component:
                raise OSError("invalid private directory component")
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, 0o700, dir_fd=descriptor)
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            if require_owner and os.fstat(descriptor).st_uid != os.geteuid():
                raise OSError("private directory is not owned by the Node user")
            if index == len(components) - 1 and mode is not None:
                os.fchmod(descriptor, mode)
        if not components:
            if require_owner and os.fstat(descriptor).st_uid != os.geteuid():
                raise OSError("private directory is not owned by the Node user")
            if mode is not None:
                os.fchmod(descriptor, mode)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _assert_directory_path_matches(path: Path, descriptor: int) -> None:
    current, _ = _open_directory_components(path, create=False, mode=None)
    try:
        expected = os.fstat(descriptor)
        observed = os.fstat(current)
        if (observed.st_dev, observed.st_ino) != (expected.st_dev, expected.st_ino):
            raise OSError("private directory was replaced")
    finally:
        os.close(current)


def _open_private_state_file(directory_fd: int, name: str, flags: int) -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise OSError("no-follow file handles are unavailable")
    return os.open(name, flags | os.O_NOFOLLOW, dir_fd=directory_fd)


def _private_state_leaf_state(
    directory_fd: int,
    name: str,
    *,
    code: str,
    label: str,
    mode: int,
) -> tuple[int, ...] | None:
    try:
        descriptor = _open_private_state_file(directory_fd, name, os.O_RDONLY)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise NodeAgentError(label, code=code) from exc
    try:
        info = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)

        def signature(value: os.stat_result) -> tuple[int, ...]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_nlink,
                value.st_uid,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        if (
            not stat_module.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat_module.S_IMODE(info.st_mode) != mode
            or signature(info) != signature(current)
        ):
            raise NodeAgentError(label, code=code)
        return signature(info)
    except OSError as exc:
        raise NodeAgentError(label, code=code) from exc
    finally:
        os.close(descriptor)


def _validate_private_state_leaf(
    directory_fd: int, name: str, *, code: str, label: str
) -> tuple[int, ...] | None:
    return _private_state_leaf_state(directory_fd, name, code=code, label=label, mode=0o600)


def _read_private_state_file(
    directory_fd: int,
    name: str,
    *,
    code: str,
    label: str,
    limit: int = 1024 * 1024,
) -> bytes:
    try:
        descriptor = _open_private_state_file(directory_fd, name, os.O_RDONLY)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise NodeAgentError(label, code=code) from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat_module.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or stat_module.S_IMODE(info.st_mode) != 0o600
        ):
            raise NodeAgentError(label, code=code)
        content = bytearray()
        while len(content) <= limit:
            chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > limit:
            raise NodeAgentError(label, code=code)
        final = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)

        def signature(value: os.stat_result) -> tuple[int, ...]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_nlink,
                value.st_uid,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        if (
            signature(info) != signature(final)
            or signature(final) != signature(current)
            or not stat_module.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or current.st_uid != os.geteuid()
            or stat_module.S_IMODE(current.st_mode) != 0o600
        ):
            raise NodeAgentError(label, code=code)
        return bytes(content)
    except OSError as exc:
        raise NodeAgentError(label, code=code) from exc
    finally:
        os.close(descriptor)


def ensure_node_identity(state_dir: Path):  # type: ignore[no-untyped-def]
    try:
        directory_fd, _resolved = _open_private_directory(state_dir, create=True)
    except OSError as exc:
        raise NodeAgentError(
            "Node identity state path is invalid", code="identity_invalid"
        ) from exc
    try:
        private_key = _ensure_node_identity_at(directory_fd)
        try:
            _assert_directory_path_matches(state_dir, directory_fd)
        except (OSError, RuntimeError) as exc:
            raise NodeAgentError(
                "Node identity state path changed during access", code="identity_invalid"
            ) from exc
        return private_key
    finally:
        os.close(directory_fd)


def _ensure_node_identity_at(directory_fd: int):  # type: ignore[no-untyped-def]
    try:
        content = _read_private_state_file(
            directory_fd,
            NODE_PRIVATE_KEY_FILE,
            code="identity_invalid",
            label="Node identity file is invalid",
        )
    except FileNotFoundError:
        private_key = generate_private_key()
        try:
            _atomic_private_write(
                Path(NODE_PRIVATE_KEY_FILE),
                private_key_pem(private_key),
                mode=0o600,
                directory_fd=directory_fd,
                expected_state=None,
                error_code="identity_invalid",
            )
        except OSError as exc:
            raise NodeAgentError(
                "Node identity could not be saved", code="identity_invalid"
            ) from exc
        return private_key
    return load_private_key(content)


def load_node_identity(state_dir: Path):  # type: ignore[no-untyped-def]
    try:
        directory_fd, _resolved = _open_private_directory(state_dir, create=False)
    except FileNotFoundError as exc:
        raise NodeAgentError(
            "Node identity is missing. Pair this Node again after revoking the old identity.",
            code="identity_missing",
        ) from exc
    except (OSError, RuntimeError) as exc:
        raise NodeAgentError(
            "Node identity state path is invalid", code="identity_invalid"
        ) from exc
    try:
        try:
            content = _read_private_state_file(
                directory_fd,
                NODE_PRIVATE_KEY_FILE,
                code="identity_invalid",
                label="Node identity file is invalid",
            )
        except FileNotFoundError as exc:
            raise NodeAgentError(
                "Node identity is missing. Pair this Node again after revoking the old identity.",
                code="identity_missing",
            ) from exc
        private_key = load_private_key(content)
        try:
            _assert_directory_path_matches(state_dir, directory_fd)
        except (OSError, RuntimeError) as exc:
            raise NodeAgentError(
                "Node identity state path changed during load", code="identity_invalid"
            ) from exc
        return private_key
    finally:
        os.close(directory_fd)


def load_node_config(state_dir: Path) -> NodeConfig:
    try:
        directory_fd, resolved_state_dir = _open_private_directory(state_dir, create=False)
    except FileNotFoundError as exc:
        raise NodeAgentError(
            "Node is not paired. Run `termroom node pair` first.", code="not_paired"
        ) from exc
    except (OSError, RuntimeError) as exc:
        raise NodeAgentError(
            "Node configuration state path is invalid", code="config_invalid"
        ) from exc
    try:
        state_info = os.fstat(directory_fd)
        state_dir_identity = (state_info.st_dev, state_info.st_ino)
        try:
            content = _read_private_state_file(
                directory_fd,
                NODE_CONFIG_FILE,
                code="config_invalid",
                label="Node configuration is invalid",
            )
        except FileNotFoundError as exc:
            raise NodeAgentError(
                "Node is not paired. Run `termroom node pair` first.",
                code="not_paired",
            ) from exc
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NodeAgentError("Node configuration is invalid", code="config_invalid") from exc
        try:
            _assert_directory_path_matches(state_dir, directory_fd)
        except (OSError, RuntimeError) as exc:
            raise NodeAgentError(
                "Node configuration state path changed during load", code="config_invalid"
            ) from exc
    finally:
        os.close(directory_fd)

    if not isinstance(value, dict):
        raise NodeAgentError("Node configuration is invalid", code="config_invalid")
    roots = normalize_allowed_roots(value.get("allowed_roots", []))
    run_root = normalize_run_root(value.get("run_root"), state_dir=resolved_state_dir)
    core_url = normalize_core_url(str(value.get("core_url") or ""))
    return NodeConfig(
        core_url=core_url,
        node_id=validate_node_id(str(value.get("node_id") or "")),
        name=str(value.get("name") or socket.gethostname())[:120],
        allowed_roots=roots,
        state_dir=resolved_state_dir,
        run_root=run_root,
        ca_file=normalize_ca_file(value.get("ca_file"), core_url=core_url),
        state_dir_identity=state_dir_identity,
    )


def save_node_config(
    state_dir: Path,
    config: NodeConfig,
    *,
    directory_fd: int | None = None,
) -> None:
    owns_directory_fd = directory_fd is None
    if directory_fd is None:
        try:
            directory_fd, _resolved_state_dir = _open_private_directory(state_dir, create=True)
        except (OSError, RuntimeError) as exc:
            raise NodeAgentError(
                "Node configuration state path is invalid", code="config_invalid"
            ) from exc
    try:
        core_url = normalize_core_url(config.core_url)
        payload = json.dumps(
            {
                "core_url": core_url,
                "node_id": validate_node_id(config.node_id),
                "name": config.name,
                "allowed_roots": [str(path) for path in config.allowed_roots],
                "run_root": str(
                    normalize_run_root(
                        config.run_root,
                        state_dir=config.state_dir or state_dir,
                    )
                ),
                "ca_file": (
                    str(normalize_ca_file(config.ca_file, core_url=core_url))
                    if config.ca_file
                    else None
                ),
                "protocol_version": NODE_PROTOCOL_VERSION,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        try:
            expected_state = _validate_private_state_leaf(
                directory_fd,
                NODE_CONFIG_FILE,
                code="config_invalid",
                label="Node configuration path is not a private regular file",
            )
            _atomic_private_write(
                Path(NODE_CONFIG_FILE),
                payload,
                mode=0o600,
                directory_fd=directory_fd,
                expected_state=expected_state,
                error_code="config_invalid",
            )
            _assert_directory_path_matches(state_dir, directory_fd)
        except OSError as exc:
            raise NodeAgentError(
                "Node configuration could not be saved", code="config_invalid"
            ) from exc
    finally:
        if owns_directory_fd:
            os.close(directory_fd)


def normalize_allowed_roots(values: object) -> tuple[Path, ...]:
    if not isinstance(values, (list, tuple)) or not values:
        raise NodeAgentError("At least one allowed root is required", code="roots_required")
    roots: list[Path] = []
    for value in values:
        raw = Path(str(value)).expanduser()
        try:
            info = raw.lstat()
            resolved = raw.resolve(strict=True)
        except OSError as exc:
            raise NodeAgentError(
                f"Allowed root is unavailable: {raw}", code="root_invalid"
            ) from exc
        if stat_module.S_ISLNK(info.st_mode) or not resolved.is_dir():
            raise NodeAgentError(
                f"Allowed root must be a real directory: {raw}", code="root_invalid"
            )
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def normalize_run_root(value: object, *, state_dir: Path) -> Path:
    if value is None or str(value) == "":
        return Path(os.path.abspath(os.fspath(state_dir.expanduser()))) / "runs"
    raw = Path(str(value)).expanduser()
    if not raw.is_absolute():
        raise NodeAgentError("Node Remote Run root must be absolute", code="config_invalid")
    return Path(os.path.abspath(os.fspath(raw)))


def normalize_ca_file(value: object, *, core_url: str) -> Path | None:
    if value is None or str(value) == "":
        return None
    raw = Path(str(value)).expanduser()
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise NodeAgentError("Node CA file is unavailable", code="ca_file_invalid") from exc
    if not resolved.is_file():
        raise NodeAgentError("Node CA file must resolve to a file", code="ca_file_invalid")
    if not core_url.startswith("https://"):
        raise NodeAgentError(
            "Node CA file requires an HTTPS Core URL", code="ca_file_requires_https"
        )
    return resolved


def _node_ssl_context(ca_file: Path | None) -> ssl.SSLContext | None:
    if ca_file is None:
        return None
    try:
        return ssl.create_default_context(cafile=str(ca_file))
    except (OSError, ssl.SSLError) as exc:
        raise NodeAgentError(
            "Node CA file is not a valid certificate bundle", code="ca_file_invalid"
        ) from exc


def pair_node(
    *,
    state_dir: Path,
    core_url: str,
    code: str,
    allowed_roots: Sequence[str | Path],
    name: str | None = None,
    run_root: str | Path | None = None,
    ca_file: str | Path | None = None,
    timeout_seconds: float = 600.0,
) -> NodeConfig:
    normalized_core = normalize_core_url(core_url)
    roots = normalize_allowed_roots(list(allowed_roots))
    trusted_ca = normalize_ca_file(ca_file, core_url=normalized_core)
    ssl_context = _node_ssl_context(trusted_ca)
    try:
        directory_fd, resolved_state_dir = _open_private_directory(state_dir, create=True)
    except (OSError, RuntimeError) as exc:
        raise NodeAgentError(
            "Node identity state path is invalid", code="identity_invalid"
        ) from exc
    try:
        private_key = _ensure_node_identity_at(directory_fd)
        public_key = public_key_text(private_key.public_key())
        state_info = os.fstat(directory_fd)
        state_identity = (state_info.st_dev, state_info.st_ino)
        polling_secret = secrets.token_urlsafe(32)
        enrollment = _json_post(
            normalized_core,
            "/api/node/enroll",
            {
                "code": code,
                "name": (name or socket.gethostname())[:120],
                "public_key": public_key,
                "fingerprint": public_key_fingerprint(public_key),
                "protocol_version": NODE_PROTOCOL_VERSION,
                "polling_secret": polling_secret,
            },
            ssl_context=ssl_context,
        )
        enrollment_id = str(enrollment.get("enrollment_id") or "")
        if not enrollment_id:
            raise NodeAgentError("Core returned an invalid enrollment", code="enrollment_invalid")
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while time.monotonic() < deadline:
            status = _json_post(
                normalized_core,
                "/api/node/enroll/status",
                {"enrollment_id": enrollment_id, "polling_secret": polling_secret},
                ssl_context=ssl_context,
            )
            decision = str(status.get("status") or "")
            if decision == "approved":
                try:
                    _assert_directory_path_matches(resolved_state_dir, directory_fd)
                except (OSError, RuntimeError) as exc:
                    raise NodeAgentError(
                        "Node identity state path changed during pairing",
                        code="config_invalid",
                    ) from exc
                config = NodeConfig(
                    core_url=normalized_core,
                    node_id=validate_node_id(str(status.get("node_id") or "")),
                    name=(name or socket.gethostname())[:120],
                    allowed_roots=roots,
                    state_dir=resolved_state_dir,
                    run_root=normalize_run_root(run_root, state_dir=resolved_state_dir),
                    ca_file=trusted_ca,
                    state_dir_identity=state_identity,
                )
                save_node_config(resolved_state_dir, config, directory_fd=directory_fd)
                try:
                    _assert_directory_path_matches(resolved_state_dir, directory_fd)
                except (OSError, RuntimeError) as exc:
                    raise NodeAgentError(
                        "Node identity state path changed during config save",
                        code="config_invalid",
                    ) from exc
                return config
            if decision == "rejected":
                raise NodeAgentError("Node pairing was rejected", code="pairing_rejected")
            time.sleep(1.0)
        raise NodeAgentError("Node pairing approval timed out", code="pairing_timeout")
    finally:
        os.close(directory_fd)


class NodeRuntime:
    def __init__(
        self,
        allowed_roots: Sequence[Path],
        *,
        max_edit_bytes: int = 1024 * 1024,
        file_run_root: Path | None = None,
        remote_run_root: Path | None = None,
        private_state_root: Path | None = None,
        private_state_identity: tuple[int, int] | None = None,
    ) -> None:
        self.allowed_roots = normalize_allowed_roots(list(allowed_roots))
        self._allowed_root_ids: dict[Path, tuple[int, int]] = {}
        for root in self.allowed_roots:
            descriptor, _ = _open_directory_components(root, create=False, mode=None)
            try:
                info = os.fstat(descriptor)
                self._allowed_root_ids[root] = (info.st_dev, info.st_ino)
            finally:
                os.close(descriptor)
        self._workspace_dir_ids: dict[Path, tuple[int, int]] = {}
        self._workspace_dir_fds: dict[Path, int] = {}
        self._workspace_dir_ids_lock = threading.Lock()
        self.files = FileService(max_edit_bytes=max_edit_bytes)
        self.streams: dict[str, AgentStream] = {}
        private_state = self._prepare_source_private_root(
            private_state_root, expected_identity=private_state_identity
        )
        self.private_state_fd = private_state[0] if private_state else None
        self.private_state_root = private_state[1] if private_state else None
        self.source_private_boundaries = (
            (self.private_state_root,) if self.private_state_root is not None else ()
        )
        self.files_private_boundaries = self.source_private_boundaries
        self.file_run_root_fd, self.file_run_root = self._open_file_run_root(
            file_run_root,
            private_state_fd=self.private_state_fd,
            private_state_root=self.private_state_root,
        )
        self._file_run_workspace_ids: dict[str, tuple[int, int]] = {}
        self._file_run_metadata_ids: dict[tuple[str, str], tuple[int, int]] = {}
        self._file_run_execution_fds: dict[tuple[str, str], tuple[int, int]] = {}
        self.remote_runs = (
            NodeRemoteRunRuntime(
                remote_run_root,
                parent_fd=self.private_state_fd,
                parent_path=self.private_state_root,
                descriptor_handoff=self._tmux_with_descriptors,
            )
            if remote_run_root is not None
            else None
        )
        private_boundaries = [
            boundary
            for boundary in (
                self.file_run_root,
                self.remote_runs.run_root if self.remote_runs is not None else None,
            )
            if boundary is not None
        ]
        self.source_private_boundaries = tuple(
            dict.fromkeys((*self.source_private_boundaries, *private_boundaries))
        )
        self.files_private_boundaries = self.source_private_boundaries
        self._file_run_locks: dict[str, threading.RLock] = {}
        self._file_run_locks_guard = threading.Lock()
        self._workspace_command_locks: dict[str, threading.RLock] = {}
        self._workspace_command_locks_guard = threading.Lock()
        self._browser_grid_resize_lock = threading.RLock()
        self._fresh_grid_windows: set[str] = set()
        self._fresh_grid_windows_lock = threading.Lock()
        self._closed_terminal_streams: dict[str, TerminalAgentStream] = {}

    async def handle(
        self,
        operation: str,
        payload: Mapping[str, Any],
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> OperationResult:
        operation = validate_request_operation(operation)
        if operation == "terminal.attach":
            return await self._terminal_attach(payload, send)
        if operation == "terminal.resize":
            stream_id = validate_request_id(str(payload.get("stream_id") or ""))
            stream = self.streams.get(stream_id) or self._closed_terminal_streams.get(stream_id)
            if not isinstance(stream, TerminalAgentStream):
                raise NodeAgentError("Terminal stream is unavailable", code="terminal_stream_stale")
            result = await stream.resize(payload)
            if stream.closed:
                self._closed_terminal_streams[stream_id] = stream
                if len(self._closed_terminal_streams) > 64:
                    self._closed_terminal_streams.pop(next(iter(self._closed_terminal_streams)))
            return OperationResult(result)
        if operation == "files.read_text.open":
            return await self._read_text_open(payload, send)
        if operation == "files.write_text.open":
            return await self._write_text_open(payload)
        if operation == "files.download.open":
            return await self._download_open(payload, send)
        if operation == "files.upload.open":
            return await self._upload_open(payload)
        if operation == "remote_run_source.manifest.open":
            return await self._remote_run_source_manifest_open(payload, send)
        if operation == "remote_run_source.file.open":
            return await self._remote_run_source_file_open(payload, send)
        if operation == "remote_run.snapshot.file.open":
            runtime = self._remote_run_runtime()
            stream = runtime.snapshot_file_open(payload, self.streams)
            self.streams[stream.stream_id] = stream
            return OperationResult({"stream_id": stream.stream_id})
        if operation == "remote_run.metadata.open":
            runtime = self._remote_run_runtime()
            stream = runtime.metadata_open(payload, self.streams)
            self.streams[stream.stream_id] = stream
            return OperationResult({"stream_id": stream.stream_id})
        return OperationResult(await asyncio.to_thread(self._handle_sync, operation, payload))

    def _handle_sync(self, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if operation == "workspace.roots":
            return {
                "roots": [
                    {"path": str(root), "name": root.name or str(root)}
                    for root in self.allowed_roots
                ]
            }
        if operation == "workspace.browse":
            return self._browse(payload)
        if operation == "workspace.create_project":
            return self._create_project(payload)
        if operation == "workspace.validate":
            path = self._workspace_path(payload)
            return {"path": str(path), "name": path.name or str(path)}
        if operation == "workspace.ensure":
            return {"terminals": self._ensure_workspace(payload)}
        if operation == "workspace.usage":
            return self._workspace_usage(payload)
        if operation == "workspace.command.run":
            return self._run_workspace_command(payload)
        if operation == "terminal.create":
            return {"terminal": self._create_terminal(payload)}
        if operation == "terminal.editor.open":
            return self._open_terminal_editor(payload)
        if operation == "terminal.rename":
            return {"terminal": self._rename_terminal(payload)}
        if operation == "terminal.close":
            return {"terminals": self._close_terminal(payload)}
        if operation == "terminal.activity":
            return {"workspaces": self._terminal_activity(payload)}
        if operation == "terminal.scrollback":
            return {"output": self._capture_scrollback(payload)}
        if operation == "files.list":
            root = self._files_workspace_path(payload)
            relative_path = str(payload.get("path") or ".")
            self._require_files_path(root, relative_path)
            raw_max_entries = payload.get("max_entries")
            if raw_max_entries is None:
                max_entries = NODE_FILE_LIST_MAX_ENTRIES
            elif type(raw_max_entries) is not int or not (
                0 <= raw_max_entries <= NODE_FILE_LIST_MAX_ENTRIES
            ):
                raise NodeAgentError("Directory entry limit is invalid", code="request_invalid")
            else:
                max_entries = raw_max_entries
            raw_metadata_bytes = payload.get("max_metadata_bytes")
            if raw_metadata_bytes is None:
                max_metadata_bytes = NODE_FILE_LIST_MAX_METADATA_BYTES
            elif type(raw_metadata_bytes) is not int or not (
                1 <= raw_metadata_bytes <= NODE_FILE_LIST_MAX_METADATA_BYTES
            ):
                raise NodeAgentError("Directory metadata limit is invalid", code="request_invalid")
            else:
                max_metadata_bytes = raw_metadata_bytes
            directory, entries = self.files.list_dir(
                root,
                relative_path,
                max_entries=max_entries,
                max_metadata_bytes=max_metadata_bytes,
                excluded_paths=self._private_paths_relative_to(root),
            )
            return {
                "directory": self._relative(root, directory),
                "entries": [asdict(entry) for entry in entries],
            }
        if operation == "files.search":
            root = self._files_workspace_path(payload)
            relative_path = str(payload.get("path") or ".")
            self._require_files_path(root, relative_path)
            raw_query = payload.get("query")
            if not isinstance(raw_query, str) or not raw_query.strip() or len(raw_query) > 256:
                raise NodeAgentError("File search query is invalid", code="request_invalid")
            raw_include_noise = payload.get("include_noise", False)
            if not isinstance(raw_include_noise, bool):
                raise NodeAgentError("File search visibility is invalid", code="request_invalid")
            search = self.files.search_files(
                root,
                relative_path,
                raw_query,
                include_noise=raw_include_noise,
                max_matches=DEFAULT_FILE_SEARCH_MAX_MATCHES,
                max_entries=DEFAULT_FILE_SEARCH_MAX_ENTRIES,
                max_seconds=DEFAULT_FILE_SEARCH_MAX_SECONDS,
                excluded_paths=self._private_paths_relative_to(root),
            )
            return {
                "entries": [asdict(entry) for entry in search.entries],
                "scanned_entries": search.scanned_entries,
                "skipped_noise": search.skipped_noise,
                "truncated": search.truncated,
            }
        if operation == "files.recent":
            root = self._files_workspace_path(payload)
            recent = self.files.recent_files(
                root,
                limit=int(payload.get("limit") or 50),
                excluded_paths=self._private_paths_relative_to(root),
            )
            return {
                "entries": [asdict(entry) for entry in recent.entries],
                "scanned_files": recent.scanned_files,
                "truncated": recent.truncated,
            }
        if operation == "files.stat":
            root = self._files_workspace_path(payload)
            relative_path = str(payload.get("path") or "")
            self._require_files_path(root, relative_path)
            return {"entry": asdict(self.files.stat(root, relative_path))}
        if operation == "files.read_preview":
            root = self._files_workspace_path(payload)
            relative_path = str(payload.get("path") or "")
            self._require_files_path(root, relative_path)
            preview = self.files.read_text_preview(
                root,
                relative_path,
                mode=str(payload.get("mode") or "head"),
                offset=int(payload.get("offset") or 0),
                max_bytes=int(payload.get("max_bytes") or 256 * 1024),
            )
            return {"preview": asdict(preview)}
        if operation == "files.create":
            root = self._files_workspace_path(payload)
            parent = str(payload.get("parent") or ".")
            name = str(payload.get("name") or "")
            self._require_files_path(
                root, Path(parent) / name, destructive=True, allow_missing=True
            )
            self.files.create(
                root,
                parent,
                name,
                directory=payload.get("directory") is True,
            )
            return {}
        if operation == "files.rename":
            root = self._files_workspace_path(payload)
            relative_path = str(payload.get("path") or "")
            source = self._require_files_path(root, relative_path, destructive=True)
            new_name = str(payload.get("new_name") or "")
            destination = source.relative_to(root).parent / new_name
            self._require_files_path(root, destination, destructive=True, allow_missing=True)
            self.files.rename(
                root,
                relative_path,
                new_name,
            )
            return {}
        if operation == "files.delete":
            root = self._files_workspace_path(payload)
            relative_path = str(payload.get("path") or "")
            self._require_files_path(root, relative_path, destructive=True)
            self.files.delete(root, relative_path)
            return {}
        if operation == "file_run.inspect":
            return self._inspect_runnable(payload)
        if operation == "file_run.start":
            return self._start_file_run(payload)
        if operation == "file_run.observe":
            return self._observe_file_run(payload)
        if operation == "file_run.interrupt":
            return {"sent": self._control_file_run(payload, force=False)}
        if operation == "file_run.kill":
            return {"sent": self._control_file_run(payload, force=True)}
        if operation == "remote_run_source.stat":
            return {
                "remote_run_source_version": NODE_REMOTE_RUN_SOURCE_VERSION,
                "entry": self._workspace_source_entry_payload(
                    self._remote_run_source_stat_entry(payload)
                ),
            }
        if operation == "remote_run.preflight":
            return self._remote_run_runtime().preflight(payload)
        if operation == "remote_run.create":
            return self._remote_run_runtime().create(payload)
        if operation == "remote_run.snapshot.begin":
            return self._remote_run_runtime().snapshot_begin(payload)
        if operation == "remote_run.snapshot.mkdir":
            return self._remote_run_runtime().snapshot_directory(payload)
        if operation == "remote_run.snapshot.symlink":
            return self._remote_run_runtime().snapshot_symlink(payload)
        if operation == "remote_run.snapshot.commit":
            return self._remote_run_runtime().snapshot_commit(payload)
        if operation == "remote_run.metadata.write":
            return self._remote_run_runtime().write_metadata(payload)
        if operation == "remote_run.start":
            return self._remote_run_runtime().start(payload)
        if operation == "remote_run.git.start":
            return self._remote_run_runtime().start_git(payload)
        if operation == "remote_run.observe":
            return self._remote_run_runtime().observe(payload)
        if operation == "remote_run.poll":
            return self._remote_run_runtime().poll(payload)
        if operation == "remote_run.interrupt":
            return self._remote_run_runtime().interrupt(payload)
        if operation == "remote_run.kill":
            return self._remote_run_runtime().kill(payload)
        if operation == "remote_run.exists":
            return self._remote_run_runtime().exists(payload)
        if operation == "remote_run.ensure_shell":
            return self._remote_run_runtime().ensure_shell(payload)
        if operation == "remote_run.delete":
            return self._remote_run_runtime().delete(payload)
        raise NodeAgentError("Node operation is unsupported", code="operation_unsupported")

    def _workspace_path(self, payload: Mapping[str, Any]) -> Path:
        if payload.get("remote_run_id") is not None:
            return self._remote_run_runtime().validate_workspace(payload)
        raw_value = str(payload.get("workspace_path") or payload.get("path") or "")
        if not raw_value or "\x00" in raw_value:
            raise PathBoundaryError("Workspace path is required")
        raw = Path(raw_value)
        if not raw.is_absolute() or os.path.normpath(raw_value) != raw_value:
            raise PathBoundaryError("Workspace path must be canonical and absolute")
        resolved = raw.resolve(strict=True)
        if resolved != raw or not resolved.is_dir():
            raise PathBoundaryError("Workspace path must be a real directory")
        if not any(is_within(resolved, root) for root in self.allowed_roots):
            raise PathBoundaryError("Workspace path is outside the Node allowed roots")
        try:
            descriptor = self._open_workspace_directory(resolved)
        except OSError as exc:
            raise PathBoundaryError("Workspace or allowed root changed") from exc
        try:
            info = os.fstat(descriptor)
            identity = (info.st_dev, info.st_ino)
            with self._workspace_dir_ids_lock:
                previous = self._workspace_dir_ids.get(resolved)
                if previous is not None and previous != identity:
                    raise PathBoundaryError("Workspace directory was replaced")
                if previous is None:
                    self._workspace_dir_ids[resolved] = identity
                    self._workspace_dir_fds[resolved] = descriptor
                    descriptor = -1
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        self._require_persistent_workspace_path(resolved)
        return resolved

    def _open_workspace_directory(self, path: Path) -> int:
        for root in self.allowed_roots:
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            root_fd, _ = _open_directory_components(root, create=False, mode=None)
            try:
                root_info = os.fstat(root_fd)
                if (root_info.st_dev, root_info.st_ino) != self._allowed_root_ids[root]:
                    raise OSError("Node allowed root was replaced")
                descriptor = _open_directory_at(root_fd, relative.parts, create=False, mode=None)
            finally:
                os.close(root_fd)
            try:
                _assert_directory_path_matches(path, descriptor)
            except BaseException:
                os.close(descriptor)
                raise
            return descriptor
        raise OSError("Workspace is outside Node allowed roots")

    @contextlib.contextmanager
    def _workspace_tmux_cwd(self, path: Path, *, remote_run_id: object = None) -> Iterator[int]:
        if remote_run_id is not None:
            descriptor = os.dup(self._remote_run_runtime().workspace_fd(str(remote_run_id), path))
            try:
                yield descriptor
            finally:
                os.close(descriptor)
            return
        try:
            descriptor = self._open_workspace_directory(path)
        except OSError as exc:
            raise PathBoundaryError("Workspace directory changed before tmux launch") from exc
        launch_descriptor = -1
        try:
            info = os.fstat(descriptor)
            identity = (info.st_dev, info.st_ino)
            with self._workspace_dir_ids_lock:
                if self._workspace_dir_ids.get(path) != identity:
                    raise PathBoundaryError("Workspace directory was replaced")
                pinned_descriptor = self._workspace_dir_fds[path]
                pinned = os.fstat(pinned_descriptor)
                if (pinned.st_dev, pinned.st_ino) != identity:
                    raise PathBoundaryError("Workspace directory was replaced")
                launch_descriptor = os.dup(pinned_descriptor)
            yield launch_descriptor
        finally:
            os.close(descriptor)
            if launch_descriptor >= 0:
                os.close(launch_descriptor)

    def __del__(self) -> None:
        for descriptor in getattr(self, "_workspace_dir_fds", {}).values():
            with contextlib.suppress(OSError):
                os.close(descriptor)
        for descriptors in getattr(self, "_file_run_execution_fds", {}).values():
            for descriptor in descriptors:
                with contextlib.suppress(OSError):
                    os.close(descriptor)

    def _files_workspace_path(self, payload: Mapping[str, Any]) -> Path:
        return self._workspace_path(payload)

    def _require_persistent_workspace_path(self, path: Path) -> None:
        if any(is_within(path, boundary) for boundary in self.source_private_boundaries):
            raise PathBoundaryError("Workspace path overlaps Node private state")

    def _private_paths_relative_to(self, root: Path) -> frozenset[str]:
        return frozenset(
            boundary.relative_to(root).as_posix()
            for boundary in self.files_private_boundaries
            if is_within(boundary, root)
        )

    def _require_files_path(
        self,
        root: Path,
        relative_path: str | Path,
        *,
        destructive: bool = False,
        allow_missing: bool = False,
        allow_missing_parents: bool = False,
    ) -> Path:
        try:
            target = resolve_inside(root, relative_path, must_exist=not allow_missing)
        except FileNotFoundError:
            if not allow_missing_parents:
                raise
            raw = Path(relative_path)
            if raw.is_absolute():
                raise PathBoundaryError("Absolute paths are not allowed") from None
            target = (root / raw).resolve(strict=False)
            if not is_within(target, root):
                raise PathBoundaryError("Path escapes the allowed boundary") from None
        if allow_missing:
            target = target.resolve(strict=False)
        protected_descendants = tuple(
            boundary for boundary in self.files_private_boundaries if is_within(boundary, root)
        )
        if any(
            is_within(target, boundary) or (destructive and is_within(boundary, target))
            for boundary in protected_descendants
        ):
            raise PathBoundaryError("Path overlaps Node private state")
        return target

    @staticmethod
    def _prepare_source_private_root(
        value: Path | None,
        *,
        expected_identity: tuple[int, int] | None = None,
    ) -> tuple[int, Path] | None:
        if value is None:
            return None
        try:
            descriptor, candidate = _open_directory_components(
                value,
                create=expected_identity is None,
                mode=None,
            )
            info = os.fstat(descriptor)
            if (
                expected_identity is not None
                and (
                    info.st_dev,
                    info.st_ino,
                )
                != expected_identity
            ):
                raise OSError("Node private state directory was replaced")
            _assert_directory_path_matches(candidate, descriptor)
            os.fchmod(descriptor, 0o700)
            _assert_directory_path_matches(candidate, descriptor)
            return descriptor, candidate
        except (OSError, RuntimeError) as exc:
            if "descriptor" in locals():
                os.close(descriptor)
            raise NodeAgentError(
                "Node private state is unavailable", code="node_state_invalid"
            ) from exc

    @staticmethod
    def _open_file_run_root(
        value: Path | None,
        *,
        private_state_fd: int | None,
        private_state_root: Path | None,
    ) -> tuple[int | None, Path | None]:
        if value is None:
            return None, None
        candidate = Path(os.path.abspath(os.fspath(value.expanduser())))
        try:
            relative = candidate.relative_to(private_state_root) if private_state_root else None
        except ValueError:
            relative = None
        descriptor = -1
        try:
            if relative is not None and private_state_fd is not None:
                descriptor = _open_directory_at(
                    private_state_fd,
                    relative.parts,
                    create=True,
                    mode=0o700,
                    require_owner=True,
                )
            else:
                descriptor, candidate = _open_private_directory(candidate, create=True)
            _assert_directory_path_matches(candidate, descriptor)
            return descriptor, candidate
        except (OSError, RuntimeError) as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise NodeAgentError(
                "Node File Run state path is not a stable real directory",
                code="file_run_state_invalid",
            ) from exc

    @staticmethod
    def _require_remote_run_source_version(payload: Mapping[str, Any]) -> None:
        value = payload.get("remote_run_source_version")
        if isinstance(value, bool) or value != NODE_REMOTE_RUN_SOURCE_VERSION:
            raise NodeAgentError(
                "Node Remote Run Source version is incompatible; update Termroom",
                code="remote_run_source_version_incompatible",
            )

    def _remote_run_source_context(self, payload: Mapping[str, Any]) -> _WorkspaceSourceContext:
        self._require_remote_run_source_version(payload)
        if payload.get("remote_run_id") not in {None, ""}:
            raise NodeAgentError(
                "A transient Remote Run Workspace cannot be a Source",
                code="source_workspace_transient",
            )
        workspace_id = self._file_run_workspace_id(payload.get("workspace_id"))
        workspace_root = self._workspace_path({"workspace_path": payload.get("workspace_path")})
        source_path = normalize_source_relative_path(
            str(payload.get("source_path") or "."), allow_root=True
        )
        if source_path == ".":
            source_root = workspace_root
        else:
            source_root = resolve_no_symlink_inside(workspace_root, source_path)
        try:
            info = source_root.lstat()
        except OSError as exc:
            raise SourceValidationError(
                "The Workspace Source does not exist",
                code="source_root_missing",
                path=source_path,
            ) from exc
        if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISDIR(info.st_mode):
            raise SourceValidationError(
                "The Workspace Source must be a real directory",
                code="source_root_type",
                path=source_path,
            )
        raw_includes = payload.get("explicitly_included") or []
        if (
            not isinstance(raw_includes, list)
            or len(raw_includes) > 10_000
            or any(not isinstance(value, str) for value in raw_includes)
        ):
            raise SourceValidationError("Explicit Source paths are invalid", code="source_options")
        return _WorkspaceSourceContext(
            workspace_id=workspace_id,
            workspace_root=workspace_root,
            source_root=source_root,
            source_path=source_path,
            explicitly_included=normalize_explicit_include_paths(raw_includes),
        )

    @staticmethod
    def _workspace_source_related(path: str, explicitly_included: frozenset[str]) -> bool:
        return any(
            path == include or path.startswith(include + "/") or include.startswith(path + "/")
            for include in explicitly_included
        )

    def _workspace_source_file(
        self, context: _WorkspaceSourceContext, relative_path: object
    ) -> tuple[Path, WorkspaceEntry]:
        relative = normalize_source_relative_path(str(relative_path or ""))
        if is_default_workspace_excluded(relative) and not self._workspace_source_related(
            relative, context.explicitly_included
        ):
            raise SourceValidationError(
                "The requested file is excluded from the Workspace Source",
                code="source_excluded",
                path=relative,
            )
        try:
            target = resolve_no_symlink_inside(context.source_root, relative)
            info = target.lstat()
        except SourceValidationError:
            raise
        except (OSError, PathBoundaryError) as exc:
            raise SourceValidationError(
                f"Cannot inspect Workspace file: {relative}",
                code="source_read_failed",
                path=relative,
            ) from exc
        if any(is_within(target, boundary) for boundary in self.source_private_boundaries):
            raise SourceValidationError(
                "The requested file is inside Termroom's private state boundary",
                code="source_private_boundary",
                path=relative,
            )
        if not stat_module.S_ISREG(info.st_mode):
            raise SourceValidationError(
                "Workspace path is no longer a regular file",
                code="source_entry_type",
                path=relative,
            )
        return target, WorkspaceEntry(
            relative,
            "file",
            size=info.st_size,
            mtime_ns=info.st_mtime_ns,
            executable=bool(info.st_mode & 0o111),
        )

    def _remote_run_source_stat_entry(self, payload: Mapping[str, Any]) -> WorkspaceEntry:
        context = self._remote_run_source_context(payload)
        _target, entry = self._workspace_source_file(context, payload.get("path"))
        return entry

    async def _remote_run_source_manifest_open(
        self,
        payload: Mapping[str, Any],
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> OperationResult:
        context = self._remote_run_source_context(payload)
        manifest = await asyncio.to_thread(
            scan_local_workspace,
            context.source_root,
            mandatory_excludes=self.source_private_boundaries,
            explicitly_included=context.explicitly_included,
        )
        stream_id = validate_request_id(str(payload.get("stream_id") or ""))
        frame_count = sum(
            (len(self._workspace_source_entry_bytes(entry)) + MAX_NODE_STREAM_CHUNK_BYTES - 1)
            // MAX_NODE_STREAM_CHUNK_BYTES
            for entry in manifest.entries
        )
        stream = WorkspaceManifestAgentStream(
            stream_id,
            manifest,
            frame_count,
            send,
            self.streams,
        )
        self.streams[stream_id] = stream
        return OperationResult(
            {
                "stream_id": stream_id,
                "remote_run_source_version": NODE_REMOTE_RUN_SOURCE_VERSION,
                "stream_window": NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW,
                "frame_count": frame_count,
                "entry_count": len(manifest.entries),
                "total_bytes": manifest.total_bytes,
            },
            start=stream.start,
        )

    async def _remote_run_source_file_open(
        self,
        payload: Mapping[str, Any],
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> OperationResult:
        context = self._remote_run_source_context(payload)
        _target, current = self._workspace_source_file(context, payload.get("path"))
        try:
            expected_size = int(payload.get("expected_size"))
            expected_mtime_ns = int(payload.get("expected_mtime_ns"))
        except (TypeError, ValueError) as exc:
            raise SourceValidationError(
                "Workspace Source file metadata is invalid",
                code="source_entry_metadata",
                path=current.relative_path,
            ) from exc
        if expected_size < 0 or expected_mtime_ns < 0:
            raise SourceValidationError(
                "Workspace Source file metadata is invalid",
                code="source_entry_metadata",
                path=current.relative_path,
            )
        if current.size != expected_size or current.mtime_ns != expected_mtime_ns:
            raise SourceFileChangedError(
                current.relative_path,
                current_size=current.size,
                current_mtime_ns=current.mtime_ns,
            )
        entry = WorkspaceEntry(
            current.relative_path,
            "file",
            size=expected_size,
            mtime_ns=expected_mtime_ns,
            executable=payload.get("executable") is True,
        )
        stream_id = validate_request_id(str(payload.get("stream_id") or ""))
        stream = WorkspaceFileAgentStream(
            stream_id,
            context.source_root,
            entry,
            send,
            self.streams,
        )
        self.streams[stream_id] = stream
        return OperationResult(
            {
                "stream_id": stream_id,
                "remote_run_source_version": NODE_REMOTE_RUN_SOURCE_VERSION,
                "stream_window": NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW,
                "frame_count": (entry.size + MAX_NODE_STREAM_CHUNK_BYTES - 1)
                // MAX_NODE_STREAM_CHUNK_BYTES,
                "size": entry.size,
                "mtime_ns": entry.mtime_ns,
            },
            start=stream.start,
        )

    @staticmethod
    def _workspace_source_entry_payload(entry: WorkspaceEntry) -> dict[str, Any]:
        return {
            "path": entry.relative_path,
            "kind": entry.kind,
            "size": entry.size,
            "mtime_ns": entry.mtime_ns,
            "executable": entry.executable,
            **({"link_target": entry.link_target} if entry.link_target is not None else {}),
        }

    @staticmethod
    def _workspace_source_entry_bytes(entry: WorkspaceEntry) -> bytes:
        return (
            json.dumps(
                NodeRuntime._workspace_source_entry_payload(entry),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    def _remote_run_runtime(self) -> NodeRemoteRunRuntime:
        if self.remote_runs is None:
            raise NodeAgentError("Node Remote Run is unavailable", code="capability_unsupported")
        return self.remote_runs

    def _browse(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        requested = payload.get("path")
        path = self.allowed_roots[0] if not requested else self._workspace_path({"path": requested})
        show_hidden = payload.get("show_hidden") is True
        entries: list[dict[str, Any]] = []
        hidden_count = 0
        for child in sorted(path.iterdir(), key=lambda item: item.name.casefold()):
            try:
                if child.name.startswith(".") and not show_hidden:
                    if child.is_dir() and not child.is_symlink():
                        hidden_count += 1
                    continue
                if child.is_dir() and not child.is_symlink():
                    entries.append({"name": child.name, "path": str(child.resolve(strict=True))})
            except OSError:
                continue
        allowed_root = next(root for root in self.allowed_roots if is_within(path, root))
        parent = str(path.parent) if path != allowed_root else None
        return {
            "current": str(path),
            "parent": parent,
            "entries": entries,
            "hidden_count": hidden_count,
            "show_hidden": show_hidden,
        }

    def _create_project(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        parent = self._workspace_path({"path": payload.get("parent")})
        safe_name = validate_project_name(str(payload.get("name") or ""))
        target = parent / safe_name
        self._require_persistent_workspace_path(target)
        try:
            info = target.lstat()
        except FileNotFoundError:
            pass
        else:
            raise ProjectPathExists(target, is_directory=stat_module.S_ISDIR(info.st_mode))
        target.mkdir(mode=0o755)
        return {"path": str(target.resolve(strict=True))}

    def _ensure_workspace(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        root = self._workspace_path(payload)
        session = self._session(payload)
        session_missing = bool(self._tmux("has-session", "-t", session, check=False).returncode)
        created_session = False
        if session_missing and not self._recover_workspace_from_browser_view(session):
            with self._workspace_tmux_cwd(
                root, remote_run_id=payload.get("remote_run_id")
            ) as cwd_fd:
                self._tmux_with_descriptors(
                    ("new-session", "-d", "-s", session, "-c", "/", "-n", "shell"),
                    (cwd_fd,),
                    rollback=lambda _result: self._tmux("kill-session", "-t", session, check=False),
                )
            created_session = True
        if created_session:
            self._tmux("set-window-option", "-t", session, "window-size", "latest", check=False)
        terminals = self._list_terminals(session)
        if created_session:
            self._mark_fresh_grid_windows(terminals)
        return terminals

    def _browser_views_for_group(self, session: str) -> list[tuple[str, bool]]:
        listed = self._tmux(
            "list-sessions",
            "-F",
            "#{session_name}\t#{session_group}\t#{session_attached}",
            check=False,
        )
        if listed.returncode:
            return []
        views: list[tuple[str, bool]] = []
        for line in listed.stdout.splitlines():
            parts = line.split("\t", 2)
            if (
                len(parts) == 3
                and parts[0].startswith(TMUX_BROWSER_VIEW_PREFIX)
                and parts[1] == session
                and parts[2].isdigit()
            ):
                views.append((parts[0], int(parts[2]) > 0))
        return views

    def _recover_workspace_from_browser_view(self, session: str) -> bool:
        views = self._browser_views_for_group(session)
        for view_session, _attached in views:
            restored = self._tmux(
                "new-session",
                "-d",
                "-s",
                session,
                "-t",
                view_session,
                check=False,
            )
            if restored.returncode:
                continue
            for stale_view, attached in views:
                if not attached:
                    self._tmux("kill-session", "-t", stale_view, check=False)
            return True
        for view_session, _attached in views:
            self._tmux("kill-session", "-t", view_session, check=False)
        return False

    def _mark_fresh_grid_windows(self, terminals: Iterable[Mapping[str, Any]]) -> None:
        with self._fresh_grid_windows_lock:
            self._fresh_grid_windows.update(str(terminal["tmux_window"]) for terminal in terminals)

    def _fresh_grid_window(self, window: str) -> bool:
        with self._fresh_grid_windows_lock:
            return window in self._fresh_grid_windows

    def _complete_fresh_grid_window(self, window: str) -> None:
        with self._fresh_grid_windows_lock:
            self._fresh_grid_windows.discard(window)

    def _workspace_command_lock(self, session: str) -> threading.RLock:
        with self._workspace_command_locks_guard:
            return self._workspace_command_locks.setdefault(session, threading.RLock())

    def _run_workspace_command(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        session = self._session(payload)
        with self._workspace_command_lock(session):
            return self._run_workspace_command_locked(payload)

    def _run_workspace_command_locked(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        root = self._workspace_path(payload)
        session = self._session(payload)
        version = payload.get("workspace_command_version")
        if type(version) is not int or version != NODE_WORKSPACE_COMMAND_VERSION:
            raise NodeAgentError(
                "Workspace command version is incompatible; update Termroom Node",
                code="workspace_command_version_incompatible",
            )
        try:
            safe_slot = validate_workspace_command_slot(payload.get("slot"))
            safe_command = normalize_workspace_command(payload.get("command"))
            safe_launch = validate_workspace_command_launch(payload.get("launch_id"))
        except ValueError as exc:
            raise NodeAgentError(str(exc), code="workspace_command_invalid") from exc
        digest = workspace_command_digest(safe_command)
        terminals = self._ensure_workspace(payload)
        listed = self._tmux(
            "list-windows",
            "-t",
            session,
            "-F",
            TMUX_WORKSPACE_COMMAND_RECORD_FORMAT,
        )
        try:
            records = parse_tmux_workspace_command_records(listed.stdout)
        except ValueError as exc:
            raise NodeAgentError(str(exc), code="workspace_command_invalid") from exc
        existing = next((item for item in records if item["slot"] == safe_slot), None)
        window = ""
        created_window = False
        if existing is not None:
            terminal = next(
                (item for item in terminals if item["tmux_window"] == existing["tmux_window"]),
                None,
            )
            if terminal is None:
                raise NodeAgentError(
                    "Workspace command Terminal is missing",
                    code="workspace_command_invalid",
                )
            if existing["launch_id"] == safe_launch:
                if existing["digest"] != digest:
                    raise NodeAgentError(
                        "Workspace command launch identity was reused for another command",
                        code="idempotency_conflict",
                    )
                return {"terminal": terminal, "terminals": terminals}
            if existing["digest"] != digest:
                if existing["dead"]:
                    self._tmux("kill-window", "-t", str(existing["tmux_window"]))
                elif not clear_tmux_workspace_command_identity(
                    self._tmux, str(existing["tmux_window"])
                ):
                    raise NodeAgentError(
                        "Previous Workspace command Terminal could not be detached",
                        code="workspace_command_invalid",
                    )
                else:
                    self._tmux(
                        "rename-window",
                        "-t",
                        str(existing["tmux_window"]),
                        workspace_command_history_name(safe_slot, str(existing["launch_id"])),
                        check=False,
                    )
                existing = None
            elif existing["state"] in {"running", "settling"}:
                return {"terminal": terminal, "terminals": terminals}
            elif existing["dead"]:
                self._tmux("kill-window", "-t", str(existing["tmux_window"]))
            else:
                window = str(existing["tmux_window"])
                with self._workspace_tmux_cwd(
                    root, remote_run_id=payload.get("remote_run_id")
                ) as cwd_fd:
                    respawned = self._tmux_with_descriptors(
                        (
                            "respawn-pane",
                            "-k",
                            "-c",
                            "/",
                            "-e",
                            f"TERMROOM_WORKSPACE_COMMAND={safe_command}",
                            "-e",
                            f"TERMROOM_WORKSPACE_COMMAND_SLOT={safe_slot}",
                            "-e",
                            f"TERMROOM_WORKSPACE_COMMAND_LAUNCH={safe_launch}",
                            "-e",
                            f"TERMROOM_WORKSPACE_COMMAND_DIGEST={digest}",
                            "-t",
                            window,
                        ),
                        (cwd_fd,),
                        WORKSPACE_COMMAND_WRAPPER_ARGV,
                        rollback=lambda _result: self._tmux(
                            "kill-window", "-t", window, check=False
                        ),
                        check=False,
                    )
                if respawned.returncode:
                    raise NodeAgentError(
                        respawned.stderr.strip() or "Workspace command Terminal could not restart",
                        code="workspace_command_invalid",
                    )

        if not window:
            with self._workspace_tmux_cwd(
                root, remote_run_id=payload.get("remote_run_id")
            ) as cwd_fd:
                created = self._tmux_with_descriptors(
                    (
                        "new-window",
                        "-d",
                        "-P",
                        "-F",
                        "#{window_id}",
                        "-e",
                        f"TERMROOM_WORKSPACE_COMMAND={safe_command}",
                        "-e",
                        f"TERMROOM_WORKSPACE_COMMAND_SLOT={safe_slot}",
                        "-e",
                        f"TERMROOM_WORKSPACE_COMMAND_LAUNCH={safe_launch}",
                        "-e",
                        f"TERMROOM_WORKSPACE_COMMAND_DIGEST={digest}",
                        "-t",
                        session,
                        "-n",
                        f"run-{safe_slot + 1}",
                        "-c",
                        "/",
                    ),
                    (cwd_fd,),
                    WORKSPACE_COMMAND_WRAPPER_ARGV,
                    rollback=lambda result: self._tmux(
                        "kill-window", "-t", result.stdout.strip(), check=False
                    ),
                )
            window = created.stdout.strip()
            created_window = True
        try:
            deadline = time.monotonic() + WORKSPACE_COMMAND_READY_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                ready = self._tmux(
                    "display-message",
                    "-p",
                    "-t",
                    window,
                    TMUX_WORKSPACE_COMMAND_RECORD_FORMAT,
                    check=False,
                )
                if ready.returncode == 0 and workspace_command_record_is_ready(
                    ready.stdout,
                    window=window,
                    slot=safe_slot,
                    launch_id=safe_launch,
                    digest=digest,
                ):
                    break
                time.sleep(WORKSPACE_COMMAND_READY_POLL_SECONDS)
            else:
                raise NodeAgentError(
                    "Workspace command Terminal did not finish starting",
                    code="workspace_command_invalid",
                )
        except Exception:
            if created_window:
                self._tmux("kill-window", "-t", window, check=False)
            raise
        terminals = self._list_terminals(session)
        terminal = next((item for item in terminals if item["tmux_window"] == window), None)
        if terminal is None:
            raise NodeAgentError(
                "Workspace command Terminal disappeared while starting",
                code="workspace_command_invalid",
            )
        return {"terminal": terminal, "terminals": terminals}

    def _workspace_usage(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        version = payload.get("workspace_usage_version")
        if isinstance(version, bool) or version != NODE_WORKSPACE_USAGE_VERSION:
            raise NodeAgentError(
                "Node Workspace activity version is incompatible; update Termroom",
                code="workspace_usage_version_incompatible",
            )
        if payload.get("remote_run_id") not in {None, ""}:
            raise NodeAgentError(
                "Transient Run Workspaces do not expose Workspace activity",
                code="capability_unsupported",
            )
        self._workspace_path(payload)
        session = self._session(payload)
        panes = self._tmux(
            "list-panes",
            "-s",
            "-t",
            session,
            "-F",
            "#{pane_pid}",
            check=False,
        )
        if panes.returncode:
            raise NodeAgentError(
                "Workspace tmux session is not available",
                code="refresh_incomplete",
            )
        try:
            usage = workspace_usage_from_outputs(panes.stdout, read_system_process_output())
        except WorkspaceUsageCollectionError as exc:
            raise NodeAgentError(str(exc), code=exc.code) from exc
        return {
            "workspace_usage_version": NODE_WORKSPACE_USAGE_VERSION,
            "usage": raw_workspace_usage_payload(usage),
        }

    def _create_terminal(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        root = self._workspace_path(payload)
        session = self._session(payload)
        self._ensure_workspace(payload)
        safe_name = normalize_terminal_name(str(payload.get("name") or "shell"))
        with self._workspace_tmux_cwd(root, remote_run_id=payload.get("remote_run_id")) as cwd_fd:
            result = self._tmux_with_descriptors(
                (
                    "new-window",
                    "-d",
                    "-P",
                    "-F",
                    "#{window_id}",
                    "-t",
                    session,
                    "-n",
                    safe_name,
                    "-c",
                    "/",
                ),
                (cwd_fd,),
                rollback=lambda created: self._tmux(
                    "kill-window", "-t", created.stdout.strip(), check=False
                ),
            )
        window = result.stdout.strip()
        terminal = next(
            item for item in self._list_terminals(session) if item["tmux_window"] == window
        )
        self._mark_fresh_grid_windows((terminal,))
        return terminal

    def _open_terminal_editor(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        root = self._files_workspace_path(payload)
        session = self._session(payload)
        try:
            normalized = normalize_terminal_editor_path(payload.get("path"))
        except ValueError as exc:
            raise NodeAgentError(str(exc), code="terminal_editor_invalid") from exc
        target = (root / normalized).resolve(strict=True)
        self._require_files_path(root, normalized)
        if not is_within(target, root) or not target.is_file():
            raise NodeAgentError(
                "Terminal editor target is not a Workspace file",
                code="terminal_editor_invalid",
            )
        if not any(shutil.which(candidate) for candidate in ("nvim", "vim", "vi")):
            raise NodeAgentError(
                "Install Neovim or Vim on this Node to edit files",
                code="terminal_editor_unavailable",
            )
        digest = terminal_editor_digest(normalized)

        with self._workspace_command_lock(session):
            terminals = self._ensure_workspace(payload)
            listed = self._tmux(
                "list-windows",
                "-t",
                session,
                "-F",
                TMUX_TERMINAL_EDITOR_RECORD_FORMAT,
            )
            try:
                records = parse_tmux_terminal_editor_records(listed.stdout)
            except ValueError as exc:
                raise NodeAgentError(str(exc), code="terminal_editor_invalid") from exc
            existing = next((item for item in records if item["digest"] == digest), None)
            if existing is not None and not existing["dead"]:
                terminal = next(
                    (item for item in terminals if item["tmux_window"] == existing["tmux_window"]),
                    None,
                )
                if terminal is None:
                    raise NodeAgentError("Vim Terminal is missing", code="terminal_editor_invalid")
                return {"terminal": terminal, "terminals": terminals}
            if existing is not None:
                self._tmux("kill-window", "-t", str(existing["tmux_window"]))

            with self._workspace_tmux_cwd(
                root, remote_run_id=payload.get("remote_run_id")
            ) as cwd_fd:
                created = self._tmux_with_descriptors(
                    (
                        "new-window",
                        "-d",
                        "-P",
                        "-F",
                        "#{window_id}",
                        "-e",
                        f"TERMROOM_TERMINAL_EDITOR_FILE={normalized}",
                        "-e",
                        f"TERMROOM_TERMINAL_EDITOR_DIGEST={digest}",
                        "-t",
                        session,
                        "-n",
                        normalize_terminal_name(f"vim-{Path(normalized).name}"),
                        "-c",
                        "/",
                    ),
                    (cwd_fd,),
                    ("/bin/sh", "-c", TERMINAL_EDITOR_WRAPPER),
                    rollback=lambda result: self._tmux(
                        "kill-window", "-t", result.stdout.strip(), check=False
                    ),
                )
            window = created.stdout.strip()
            try:
                deadline = time.monotonic() + WORKSPACE_COMMAND_READY_TIMEOUT_SECONDS
                while time.monotonic() < deadline:
                    ready = self._tmux(
                        "display-message",
                        "-p",
                        "-t",
                        window,
                        f"#{{{TMUX_TERMINAL_EDITOR_DIGEST_OPTION}}}",
                        check=False,
                    )
                    if ready.returncode == 0 and ready.stdout.strip() == digest:
                        break
                    time.sleep(WORKSPACE_COMMAND_READY_POLL_SECONDS)
                else:
                    raise NodeAgentError(
                        "Vim Terminal did not finish starting",
                        code="terminal_editor_start_failed",
                    )
            except Exception:
                self._tmux("kill-window", "-t", window, check=False)
                raise
            terminals = self._ensure_workspace(payload)
            terminal = next((item for item in terminals if item["tmux_window"] == window), None)
            if terminal is None:
                raise NodeAgentError(
                    "Vim Terminal disappeared while starting",
                    code="terminal_editor_start_failed",
                )
            return {"terminal": terminal, "terminals": terminals}

    def _rename_terminal(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._workspace_path(payload)
        session = self._session(payload)
        window = self._window(payload, session)
        terminal = next(
            item for item in self._list_terminals(session) if item["tmux_window"] == window
        )
        if terminal.get("role") != "shell":
            raise NodeAgentError("Managed Terminals cannot be renamed", code="terminal_managed")
        safe_name = normalize_terminal_name(str(payload.get("name") or "shell"))
        self._tmux("rename-window", "-t", window, safe_name)
        return next(item for item in self._list_terminals(session) if item["tmux_window"] == window)

    def _close_terminal(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        self._workspace_path(payload)
        session = self._session(payload)
        window = self._window(payload, session)
        terminals = self._list_terminals(session)
        terminal = next(item for item in terminals if item["tmux_window"] == window)
        if terminal.get("role") != "shell":
            raise NodeAgentError("Managed Terminals cannot be closed", code="terminal_managed")
        if len(terminals) <= 1:
            raise NodeAgentError("The last Terminal cannot be closed", code="terminal_last")
        self._tmux("kill-window", "-t", window)
        return self._list_terminals(session)

    def _capture_scrollback(self, payload: Mapping[str, Any]) -> str:
        self._workspace_path(payload)
        session = self._session(payload)
        window = self._window(payload, session)
        lines = max(100, min(int(payload.get("lines") or 2000), 10_000))
        history_only = payload.get("history_only", False)
        if type(history_only) is not bool:
            raise NodeAgentError(
                "Terminal scrollback history mode is invalid",
                code="request_invalid",
            )
        ansi = payload.get("ansi", False)
        if type(ansi) is not bool:
            raise NodeAgentError(
                "Terminal scrollback ANSI mode is invalid",
                code="request_invalid",
            )
        args = ["capture-pane", "-p"]
        if ansi:
            args.append("-e")
        args.extend(("-J", "-S", f"-{lines}"))
        if history_only:
            args.extend(("-E", "-1"))
        args.extend(("-t", window))
        result = self._tmux(*args, check=not ansi)
        if ansi and result.returncode:
            result = self._tmux(*(item for item in args if item != "-e"))
        return result.stdout

    def _terminal_activity(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Read activity for an explicit, bounded set of tmux sessions."""

        raw_workspaces = payload.get("workspaces")
        if not isinstance(raw_workspaces, list) or len(raw_workspaces) > 100:
            raise NodeAgentError(
                "Terminal activity request is invalid", code="terminal_activity_invalid"
            )
        requested: dict[str, set[str]] = {}
        for raw in raw_workspaces:
            if not isinstance(raw, Mapping):
                raise NodeAgentError(
                    "Terminal activity request is invalid",
                    code="terminal_activity_invalid",
                )
            session = str(raw.get("tmux_session") or "")
            if not node_session_is_valid(session) or session in requested:
                raise NodeAgentError(
                    "Terminal activity request is invalid",
                    code="terminal_activity_invalid",
                )
            raw_windows = raw.get("windows")
            if not isinstance(raw_windows, list) or len(raw_windows) > 100:
                raise NodeAgentError(
                    "Terminal activity request is invalid",
                    code="terminal_activity_invalid",
                )
            windows = {str(window) for window in raw_windows}
            if len(windows) != len(raw_windows) or any(
                not NODE_WINDOW_PATTERN.fullmatch(window) for window in windows
            ):
                raise NodeAgentError(
                    "Terminal activity request is invalid",
                    code="terminal_activity_invalid",
                )
            requested[session] = windows
        if not requested:
            return []
        result = self._tmux("list-windows", "-a", "-F", TMUX_TERMINAL_RECORD_FORMAT, check=False)
        if result.returncode:
            raise NodeAgentError(
                "Terminal activity is unavailable", code="terminal_activity_unavailable"
            )
        grouped = {session: [] for session in requested}
        for record in parse_tmux_terminal_records(result.stdout):
            session = str(record.get("tmux_session") or "")
            if session in requested and str(record["tmux_window"]) in requested[session]:
                grouped[session].append(record)
        return [
            {"tmux_session": session, "terminals": terminals}
            for session, terminals in grouped.items()
        ]

    async def _terminal_attach(
        self,
        payload: Mapping[str, Any],
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> OperationResult:
        root = self._workspace_path(payload)
        session = self._session(payload)
        self._ensure_workspace(payload)
        window = self._window(payload, session)
        bootstrap_grid = self._fresh_grid_window(window)
        stream_id = validate_request_id(str(payload.get("stream_id") or ""))
        rows = max(4, min(int(payload.get("rows") or 24), 500))
        cols = max(20, min(int(payload.get("cols") or 80), 1000))
        view_session = tmux_browser_view_session(stream_id)
        self._tmux("kill-session", "-t", view_session, check=False)
        self._tmux("new-session", "-d", "-s", view_session, "-t", session)
        try:
            self._tmux("select-window", "-t", f"{view_session}:{window}")
        except Exception:
            self._tmux("kill-session", "-t", view_session, check=False)
            raise
        environment = os.environ.copy()
        for key in tuple(environment):
            if key.startswith("TERMROOM_") or key in {"TMUX", "TMUX_PANE"}:
                environment.pop(key, None)
        environment["TERM"] = "xterm-256color"
        command = ["tmux"]
        test_socket = environment.get("PYTEST_TMUX_SOCKET", "")
        if test_socket:
            command.extend(("-S", test_socket))
        try:
            process_pid, master_fd = await asyncio.to_thread(
                spawn_pty_process,
                [
                    *command,
                    "attach-session",
                    "-f",
                    "ignore-size",
                    "-t",
                    view_session,
                ],
                cwd=str(root),
                environment=environment,
                rows=rows,
                cols=cols,
            )
        except Exception:
            self._tmux("kill-session", "-t", view_session, check=False)
            raise

        def cleanup_view() -> bool:
            self._tmux("kill-session", "-t", view_session, check=False)
            return self._tmux("has-session", "-t", view_session, check=False).returncode != 0

        def freeze_view_grid() -> bool:
            flags = self._tmux(
                "list-clients", "-t", view_session, "-F", "#{client_flags}", check=False
            ).stdout
            if "ignore-size" in flags.strip().split(","):
                return True
            return freeze_tmux_window_size(self._tmux, window)

        stream = TerminalAgentStream(
            stream_id,
            process_pid,
            master_fd,
            send,
            self.streams,
            set_grid_resize=lambda enabled: self._set_browser_view_grid_resize(
                view_session, enabled=enabled
            ),
            grid_resize_applied=(
                (lambda: self._complete_fresh_grid_window(window)) if bootstrap_grid else None
            ),
            cleanup=cleanup_view,
            freeze_grid=freeze_view_grid,
            wait_grid_resize=lambda rows, cols: wait_tmux_browser_grid_size(
                lambda: (
                    self._tmux(
                        "list-clients", "-t", view_session, "-F", TMUX_BROWSER_SIZE_FORMAT
                    ).stdout
                ),
                rows=rows,
                cols=cols,
            ),
        )
        self.streams[stream_id] = stream
        return OperationResult(
            {"stream_id": stream_id, "bootstrap_grid": bootstrap_grid},
            start=stream.start,
        )

    def _set_browser_view_grid_resize(self, view_session: str, *, enabled: bool) -> bool:
        with self._browser_grid_resize_lock:
            return set_tmux_browser_view_grid_resize(
                self._tmux,
                view_session,
                enabled=enabled,
            )

    async def _read_text_open(
        self,
        payload: Mapping[str, Any],
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> OperationResult:
        root = self._files_workspace_path(payload)
        relative_path = str(payload.get("path") or "")
        self._require_files_path(root, relative_path)
        max_bytes = min(
            self.files.max_edit_bytes,
            max(1, int(payload.get("max_bytes") or 1)),
        )
        snapshot = await asyncio.to_thread(self.files.read_text, root, relative_path)
        content = snapshot.content.encode("utf-8")
        if len(content) > max_bytes:
            raise UnsupportedFileError("File exceeds the editable size limit")
        stream_id = validate_request_id(str(payload.get("stream_id") or ""))
        stream = TextDownloadAgentStream(stream_id, content, send, self.streams)
        self.streams[stream_id] = stream
        return OperationResult(
            {
                "stream_id": stream_id,
                "size": len(content),
                "snapshot": self._snapshot(snapshot, include_content=False),
            },
            start=stream.start,
        )

    async def _write_text_open(self, payload: Mapping[str, Any]) -> OperationResult:
        root = self._files_workspace_path(payload)
        relative_path = str(payload.get("path") or "")
        self._require_files_path(root, relative_path, destructive=True)
        expected_digest = str(payload.get("expected_digest") or "")
        expected_mtime_ns = int(payload.get("expected_mtime_ns") or 0)
        max_bytes = min(
            self.files.max_edit_bytes,
            max(1, int(payload.get("max_bytes") or 1)),
        )
        current = await asyncio.to_thread(self.files.read_text, root, relative_path)
        if current.digest != expected_digest or current.mtime_ns != expected_mtime_ns:
            raise FileConflictError("The file changed after it was opened")
        stream_id = validate_request_id(str(payload.get("stream_id") or ""))
        stream = TextUploadAgentStream(
            stream_id,
            self.files,
            root,
            relative_path,
            expected_digest=expected_digest,
            expected_mtime_ns=expected_mtime_ns,
            max_bytes=max_bytes,
            registry=self.streams,
        )
        self.streams[stream_id] = stream
        return OperationResult({"stream_id": stream_id})

    async def _download_open(
        self,
        payload: Mapping[str, Any],
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> OperationResult:
        root = self._files_workspace_path(payload)
        relative_path = str(payload.get("path") or "")
        self._require_files_path(root, relative_path)
        target = self.files.resolve_regular_file(root, relative_path)
        parent_fd, _ = _open_directory_components(target.parent, create=False, mode=None)
        file_fd = -1
        try:
            file_fd = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            info = os.fstat(file_fd)
            current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat_module.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (
                current.st_dev,
                current.st_ino,
            ):
                raise NodeAgentError("Download path changed", code="path_changed")
            offset = max(0, min(int(payload.get("offset") or 0), info.st_size))
            length_value = payload.get("length")
            length = None if length_value is None else max(0, int(length_value))
            stream_id = validate_request_id(str(payload.get("stream_id") or ""))
            stream = DownloadAgentStream(
                stream_id, file_fd, parent_fd, target.name, offset, length, send, self.streams
            )
            file_fd = parent_fd = -1
            self.streams[stream_id] = stream
            return OperationResult(
                {"stream_id": stream_id, "size": info.st_size}, start=stream.start
            )
        finally:
            if file_fd >= 0:
                os.close(file_fd)
            if parent_fd >= 0:
                os.close(parent_fd)

    async def _upload_open(self, payload: Mapping[str, Any]) -> OperationResult:
        root = self._files_workspace_path(payload)
        parent = str(payload.get("parent") or ".")
        filename = str(payload.get("filename") or "")
        overwrite = payload.get("overwrite") is True
        max_bytes = max(1, int(payload.get("max_bytes") or 1))
        self._require_files_path(
            root, Path(parent) / filename, destructive=True, allow_missing=True
        )
        target = self.files.upload_target(root, parent, filename)
        parent_fd, _ = _open_directory_components(target.parent, create=False, mode=None)
        try:
            try:
                existing = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and not stat_module.S_ISREG(existing.st_mode):
                raise UnsupportedFileError("Upload target is not a regular file")
            if existing is not None and not overwrite:
                raise FileExistsError(filename)
            stream_id = validate_request_id(str(payload.get("stream_id") or ""))
            stream = UploadAgentStream(
                stream_id,
                parent_fd,
                target.name,
                existing=existing,
                overwrite=overwrite,
                max_bytes=max_bytes,
                registry=self.streams,
            )
            parent_fd = -1
            self.streams[stream_id] = stream
            return OperationResult({"stream_id": stream_id})
        finally:
            if parent_fd >= 0:
                os.close(parent_fd)

    @staticmethod
    def _prepare_file_run_root(value: Path | None) -> Path | None:
        if value is None:
            return None
        candidate = value.expanduser()
        if not candidate.is_absolute():
            candidate = candidate.absolute()
        try:
            descriptor, resolved = _open_private_directory(candidate, create=True)
        except (OSError, RuntimeError) as exc:
            raise NodeAgentError(
                "Node File Run state path is not a stable real directory",
                code="file_run_state_invalid",
            ) from exc
        os.close(descriptor)
        return resolved

    @staticmethod
    def _runner_registry_version(payload: Mapping[str, Any]) -> int:
        value = payload.get("runner_registry_version")
        if isinstance(value, bool):
            raise NodeAgentError(
                "File Run Runner Registry version is invalid",
                code="runner_registry_incompatible",
            )
        try:
            version = int(value)
        except (TypeError, ValueError) as exc:
            raise NodeAgentError(
                "File Run Runner Registry version is invalid",
                code="runner_registry_incompatible",
            ) from exc
        if version != RUNNER_REGISTRY_VERSION:
            raise NodeAgentError(
                "File Run Runner Registry is incompatible; update Termroom",
                code="runner_registry_incompatible",
            )
        return version

    @staticmethod
    def _file_run_id(value: object) -> str:
        run_id = str(value or "")
        try:
            parsed = uuid.UUID(run_id)
        except (ValueError, AttributeError) as exc:
            raise NodeAgentError("File Run identity is invalid", code="file_run_invalid") from exc
        if parsed.version != 4 or str(parsed) != run_id:
            raise NodeAgentError("File Run identity is invalid", code="file_run_invalid")
        return run_id

    @staticmethod
    def _file_run_workspace_id(value: object) -> str:
        workspace_id = str(value or "")
        if not NODE_WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
            raise NodeAgentError("File Run Workspace identity is invalid", code="file_run_invalid")
        return workspace_id

    @staticmethod
    def _file_run_digest(value: object) -> str:
        digest = str(value or "")
        if not FILE_RUN_DIGEST_PATTERN.fullmatch(digest):
            raise NodeAgentError("File Run source digest is invalid", code="file_run_invalid")
        return digest

    def _file_run_lock(self, workspace_id: str) -> threading.RLock:
        with self._file_run_locks_guard:
            return self._file_run_locks.setdefault(workspace_id, threading.RLock())

    def _assert_file_run_root(self) -> None:
        root = self.file_run_root
        root_fd = self.file_run_root_fd
        if root is None or root_fd is None:
            raise NodeAgentError(
                "Node File Run state is unavailable", code="capability_unsupported"
            )
        current = -1
        try:
            current, _ = _open_directory_components(root, create=False, mode=None)
            expected = os.fstat(root_fd)
            actual = os.fstat(current)
            if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
                raise OSError("Node File Run state directory was replaced")
        except (OSError, RuntimeError) as exc:
            raise NodeAgentError(
                "Node File Run state path is invalid", code="file_run_state_invalid"
            ) from exc
        finally:
            if current >= 0:
                os.close(current)

    def _file_run_metadata_fd(self, workspace_id: str, run_id: str, *, create: bool) -> int | None:
        self._assert_file_run_root()
        root_fd = self.file_run_root_fd
        assert root_fd is not None
        workspace_id = self._file_run_workspace_id(workspace_id)
        run_id = self._file_run_id(run_id)
        workspace_fd = -1
        metadata_fd = -1
        try:
            workspace_fd = _open_directory_at(
                root_fd, (workspace_id,), create=create, mode=None, require_owner=True
            )
        except FileNotFoundError as exc:
            if workspace_id in self._file_run_workspace_ids:
                raise NodeAgentError(
                    "Node File Run workspace directory was replaced",
                    code="file_run_state_invalid",
                ) from exc
            return None
        except OSError as exc:
            raise NodeAgentError(
                "Node File Run workspace path is invalid", code="file_run_state_invalid"
            ) from exc
        try:
            workspace_info = os.fstat(workspace_fd)
            workspace_identity = (workspace_info.st_dev, workspace_info.st_ino)
            expected_workspace = self._file_run_workspace_ids.get(workspace_id)
            if expected_workspace is not None and expected_workspace != workspace_identity:
                raise NodeAgentError(
                    "Node File Run workspace directory was replaced",
                    code="file_run_state_invalid",
                )
            self._file_run_workspace_ids.setdefault(workspace_id, workspace_identity)
            if create:
                os.fchmod(workspace_fd, 0o700)

            key = (workspace_id, run_id)
            try:
                metadata_fd = _open_directory_at(
                    workspace_fd, (run_id,), create=create, mode=None, require_owner=True
                )
            except FileNotFoundError as exc:
                if key in self._file_run_metadata_ids:
                    raise NodeAgentError(
                        "Node File Run metadata directory was replaced",
                        code="file_run_state_invalid",
                    ) from exc
                return None
            except OSError as exc:
                raise NodeAgentError(
                    "Node File Run metadata path is invalid", code="file_run_state_invalid"
                ) from exc

            metadata_info = os.fstat(metadata_fd)
            metadata_identity = (metadata_info.st_dev, metadata_info.st_ino)
            expected_metadata = self._file_run_metadata_ids.get(key)
            if expected_metadata is not None and expected_metadata != metadata_identity:
                raise NodeAgentError(
                    "Node File Run metadata directory was replaced",
                    code="file_run_state_invalid",
                )
            self._file_run_metadata_ids.setdefault(key, metadata_identity)
            if create:
                os.fchmod(metadata_fd, 0o700)
            result = metadata_fd
            metadata_fd = -1
            return result
        finally:
            os.close(workspace_fd)
            if metadata_fd >= 0:
                os.close(metadata_fd)

    def _read_file_run_leaf(
        self, directory_fd: int, name: str, *, limit: int = 16 * 1024
    ) -> tuple[bytes, float]:
        try:
            descriptor = _open_private_state_file(directory_fd, name, os.O_RDONLY)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise NodeAgentError(
                "Node File Run metadata file is invalid", code="file_run_state_invalid"
            ) from exc
        try:
            info = os.fstat(descriptor)
            if (
                not stat_module.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or info.st_size > limit
            ):
                raise NodeAgentError(
                    "Node File Run metadata file is invalid", code="file_run_state_invalid"
                )
            content = bytearray()
            while len(content) <= limit:
                chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
            final = os.fstat(descriptor)
            if (
                len(content) > limit
                or final.st_nlink != 1
                or (final.st_dev, final.st_ino) != (info.st_dev, info.st_ino)
            ):
                raise NodeAgentError(
                    "Node File Run metadata file changed while reading",
                    code="file_run_state_invalid",
                )
            return bytes(content), info.st_mtime
        finally:
            os.close(descriptor)

    def _file_run_leaf_exists(self, directory_fd: int, name: str) -> bool:
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if (
            not stat_module.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
        ):
            raise NodeAgentError(
                "Node File Run metadata file is invalid", code="file_run_state_invalid"
            )
        return True

    def _file_run_dispatch_time(self, metadata_dir: Path, run_id: str) -> float | None:
        directory_fd = self._file_run_metadata_fd(metadata_dir.parent.name, run_id, create=False)
        if directory_fd is None:
            return None
        try:
            _content, modified_at = self._read_file_run_leaf(directory_fd, "request-id", limit=4096)
            return modified_at
        except (FileNotFoundError, OSError, NodeAgentError):
            return None
        finally:
            os.close(directory_fd)

    def _file_run_metadata_dir(
        self,
        workspace_id: str,
        run_id: str,
        *,
        create: bool,
    ) -> Path:
        root = self.file_run_root
        if root is None:
            raise NodeAgentError(
                "Node File Run state is unavailable", code="capability_unsupported"
            )
        workspace_dir = root / self._file_run_workspace_id(workspace_id)
        metadata_dir = workspace_dir / self._file_run_id(run_id)
        descriptor = self._file_run_metadata_fd(workspace_id, run_id, create=create)
        if descriptor is not None:
            os.close(descriptor)
        return metadata_dir

    def _snapshot_file_run_source(
        self,
        root: Path,
        relative_path: str,
        expected_digest: str,
        *,
        remote_run_id: object = None,
    ) -> tuple[RunnableFile, int]:
        components = relative_path.split("/")
        if (
            not relative_path
            or relative_path.startswith("/")
            or any(part in {"", ".", ".."} for part in components)
        ):
            raise UnsupportedFileError("File Run path must be Workspace-relative")
        with self._workspace_tmux_cwd(root, remote_run_id=remote_run_id) as workspace_fd:
            parent_fd = (
                _open_directory_at(workspace_fd, components[:-1], create=False, mode=None)
                if len(components) > 1
                else os.dup(workspace_fd)
            )
            source_fd = -1
            try:
                source_fd = os.open(
                    components[-1],
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_fd,
                )
                before = os.fstat(source_fd)
                if not stat_module.S_ISREG(before.st_mode):
                    raise UnsupportedFileError("Only regular files can be executed")
                if before.st_size > self.files.max_edit_bytes:
                    raise UnsupportedFileError("File exceeds the editable size limit")
                content = bytearray()
                while len(content) <= self.files.max_edit_bytes:
                    chunk = os.read(
                        source_fd,
                        min(64 * 1024, self.files.max_edit_bytes + 1 - len(content)),
                    )
                    if not chunk:
                        break
                    content.extend(chunk)
                if len(content) > self.files.max_edit_bytes or b"\x00" in content:
                    raise UnsupportedFileError("File cannot be executed")
                try:
                    bytes(content).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise UnsupportedFileError("Only UTF-8 text files can be executed") from exc
                after = os.fstat(source_fd)
                current = os.stat(components[-1], dir_fd=parent_fd, follow_symlinks=False)

                def signature(info: os.stat_result) -> tuple[int, ...]:
                    return (
                        info.st_dev,
                        info.st_ino,
                        info.st_mode,
                        info.st_nlink,
                        info.st_uid,
                        info.st_size,
                        info.st_mtime_ns,
                        info.st_ctime_ns,
                    )

                if signature(before) != signature(after) or signature(after) != signature(current):
                    raise FileConflictError("The file changed before execution")
                digest = hashlib.sha256(content).hexdigest()
                if digest != expected_digest:
                    raise FileConflictError("The file changed before execution")
                runnable = RunnableFile(
                    relative_path=relative_path,
                    digest=digest,
                    executable=bool(before.st_mode & 0o111),
                    has_shebang=content.startswith(b"#!"),
                )
                try:
                    snapshot_fd = _make_sealed_memfd(
                        bytes(content), "termroom-file-run-source", mode=0o400
                    )
                except OSError as exc:
                    raise NodeAgentError(
                        "Immutable File Run source descriptors are unavailable",
                        code="capability_unsupported",
                    ) from exc
                return runnable, snapshot_fd
            finally:
                if source_fd >= 0:
                    os.close(source_fd)
                os.close(parent_fd)

    def _write_file_run_metadata(
        self,
        metadata_dir: Path,
        run_id: str,
        request_record: Mapping[str, Any],
    ) -> tuple[Path, int, int]:
        workspace_id = metadata_dir.parent.name
        metadata_dir = self._file_run_metadata_dir(workspace_id, run_id, create=True)
        directory_fd = self._file_run_metadata_fd(workspace_id, run_id, create=True)
        assert directory_fd is not None
        runner_fd = -1
        try:
            try:
                content, _ = self._read_file_run_leaf(directory_fd, "request-id", limit=4096)
            except FileNotFoundError:
                content = None
            if content is not None and content.decode("utf-8").strip() != run_id:
                raise NodeAgentError(
                    "Node File Run metadata identity does not match",
                    code="file_run_state_invalid",
                )
            _atomic_private_write(
                Path("request-id"),
                (run_id + "\n").encode("utf-8"),
                mode=0o600,
                directory_fd=directory_fd,
            )
            _atomic_private_write(
                Path("request.json"),
                json.dumps(dict(request_record), ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                ),
                mode=0o600,
                directory_fd=directory_fd,
            )
            _atomic_private_write(
                Path("runner.sh"),
                FILE_RUN_WRAPPER_SCRIPT.encode("utf-8"),
                mode=0o700,
                directory_fd=directory_fd,
            )
            runner_fd = _open_private_state_file(directory_fd, "runner.sh", os.O_RDONLY)
            info = os.fstat(runner_fd)
            if (
                not stat_module.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or stat_module.S_IMODE(info.st_mode) != 0o700
            ):
                raise NodeAgentError(
                    "Node File Run runner is invalid", code="file_run_state_invalid"
                )
            return metadata_dir, directory_fd, runner_fd
        except BaseException:
            if runner_fd >= 0:
                os.close(runner_fd)
            os.close(directory_fd)
            raise

    @staticmethod
    def _assert_file_run_execution_handles(
        metadata_dir: Path, metadata_fd: int, runner_fd: int
    ) -> None:
        try:
            _assert_directory_path_matches(metadata_dir, metadata_fd)
            current = os.stat("runner.sh", dir_fd=metadata_fd, follow_symlinks=False)
        except OSError as exc:
            raise NodeAgentError(
                "Node File Run metadata path changed", code="file_run_state_invalid"
            ) from exc
        opened = os.fstat(runner_fd)
        if (
            not stat_module.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or current.st_uid != os.geteuid()
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise NodeAgentError(
                "Node File Run runner changed before execution",
                code="file_run_state_invalid",
            )

    def _release_file_run_execution(self, workspace_id: str, run_id: str) -> None:
        descriptors = self._file_run_execution_fds.pop((workspace_id, run_id), None)
        if descriptors is not None:
            for descriptor in descriptors:
                with contextlib.suppress(OSError):
                    os.close(descriptor)

    def _read_file_run_record(self, path: Path, run_id: str) -> dict[str, Any] | None:
        directory_fd = -1
        try:
            relative = path.parent.relative_to(self.file_run_root) if self.file_run_root else None
            if relative is None or len(relative.parts) != 2:
                return None
            directory_fd = self._file_run_metadata_fd(relative.parts[0], run_id, create=False)
            if directory_fd is None:
                return None
            content, _ = self._read_file_run_leaf(directory_fd, path.name)
            value = json.loads(content.decode("utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        finally:
            if directory_fd is not None and directory_fd >= 0:
                os.close(directory_fd)
        if not isinstance(value, dict) or value.get("run_id") != run_id:
            return None
        return value

    @staticmethod
    def _runnable_payload(runnable: RunnableFile) -> dict[str, Any]:
        return {
            "relative_path": runnable.relative_path,
            "digest": runnable.digest,
            "executable": runnable.executable,
            "has_shebang": runnable.has_shebang,
        }

    def _inspect_runnable(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        version = self._runner_registry_version(payload)
        root = self._files_workspace_path(payload)
        relative_path = str(payload.get("path") or "")
        self._require_files_path(root, relative_path)
        expected_value = payload.get("expected_digest")
        expected_digest = None if expected_value is None else self._file_run_digest(expected_value)
        runnable = self.files.inspect_runnable(
            root,
            relative_path,
            expected_digest=expected_digest,
        )
        runner = resolve_runner(runnable)
        return {
            "runner_registry_version": version,
            "runnable": self._runnable_payload(runnable),
            "runner": None if runner is None else {"id": runner.id, "version": runner.version},
        }

    def _start_file_run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._runner_registry_version(payload)
        root = self._files_workspace_path(payload)
        relative_path = str(payload.get("path") or "")
        self._require_files_path(
            root,
            relative_path,
            allow_missing=True,
            allow_missing_parents=True,
        )
        session = self._session(payload)
        workspace_id = self._file_run_workspace_id(payload.get("workspace_id"))
        run_id = self._file_run_id(payload.get("run_id"))
        expected_digest = self._file_run_digest(payload.get("expected_digest"))
        expected_runner_id = str(payload.get("runner_id") or "")
        expected_runner_version = payload.get("runner_version")
        if isinstance(expected_runner_version, bool):
            raise NodeAgentError("File Run Runner version is invalid", code="runner_mismatch")
        try:
            runner_version = int(expected_runner_version)
        except (TypeError, ValueError) as exc:
            raise NodeAgentError(
                "File Run Runner version is invalid", code="runner_mismatch"
            ) from exc
        request_record = {
            "run_id": run_id,
            "relative_path": relative_path,
            "source_digest": expected_digest,
            "runner_id": expected_runner_id,
            "runner_version": runner_version,
            "runner_registry_version": RUNNER_REGISTRY_VERSION,
        }

        with self._file_run_lock(workspace_id):
            metadata_dir = self._file_run_metadata_dir(workspace_id, run_id, create=False)
            existing_metadata_fd = self._file_run_metadata_fd(workspace_id, run_id, create=False)
            if existing_metadata_fd is not None:
                os.close(existing_metadata_fd)
                stored_request = self._read_file_run_record(metadata_dir / "request.json", run_id)
                if stored_request is None:
                    raise NodeAgentError(
                        "File Run was already handled but its request metadata is unavailable",
                        code="start_status_unknown",
                    )
                if stored_request != request_record:
                    raise NodeAgentError(
                        "File Run identity was reused with a different request",
                        code="idempotency_conflict",
                    )
                observation, windows = self._file_run_observation(session, metadata_dir, run_id)
                terminal = next(
                    (
                        item
                        for item in windows or []
                        if item.get("role") == "file_run" and item.get("managed_run_id") == run_id
                    ),
                    None,
                )
                if terminal is None:
                    raise NodeAgentError(
                        "File Run was already handled but its managed Terminal is unavailable",
                        code="start_status_unknown",
                    )
                return {
                    "terminal": terminal,
                    "terminals": windows,
                    "observation": observation,
                    "replayed": True,
                }

            runnable, source_fd = self._snapshot_file_run_source(
                root,
                relative_path,
                expected_digest,
                remote_run_id=payload.get("remote_run_id"),
            )
            runner = resolve_runner(runnable)
            if (
                runner is None
                or runner.id != expected_runner_id
                or runner.version != runner_version
            ):
                os.close(source_fd)
                raise NodeAgentError(
                    "File Run Runner changed before execution",
                    code="runner_mismatch",
                )

            metadata_dir = self._file_run_metadata_dir(workspace_id, run_id, create=True)
            try:
                metadata_dir, metadata_fd, runner_fd = self._write_file_run_metadata(
                    metadata_dir, run_id, request_record
                )
            except BaseException:
                os.close(source_fd)
                raise
            try:
                self._ensure_workspace(payload)
                self._assert_file_run_execution_handles(metadata_dir, metadata_fd, runner_fd)
                windows = self._list_terminals(session)
                terminal = next((item for item in windows if item.get("role") == "file_run"), None)
                created = terminal is None
                if terminal is None:
                    result = self._tmux(
                        "new-window",
                        "-d",
                        "-P",
                        "-F",
                        "#{window_id}",
                        "-t",
                        session,
                        "-n",
                        "Run",
                        "-c",
                        "/",
                    )
                    terminal = {
                        "tmux_window": result.stdout.strip(),
                        "role": "shell",
                        "managed_run_id": None,
                    }
                else:
                    pane = self._file_run_pane(str(terminal["tmux_window"]))
                    if pane is not None and not pane["dead"]:
                        raise NodeAgentError(
                            "The managed File Run Terminal is still active",
                            code="file_run_slot_occupied",
                        )
            except BaseException:
                os.close(source_fd)
                os.close(runner_fd)
                os.close(metadata_fd)
                raise

            tmux_window = str(terminal["tmux_window"])
            previous_role = str(terminal.get("role") or "shell")
            previous_run_id = str(terminal.get("managed_run_id") or "") or None
            runner_exec_fd = -1
            try:
                self._tmux("set-window-option", "-t", tmux_window, "remain-on-exit", "on")
                self._tmux(
                    "set-window-option",
                    "-t",
                    tmux_window,
                    TMUX_TERMINAL_ROLE_OPTION,
                    "file_run",
                )
                self._tmux(
                    "set-window-option",
                    "-t",
                    tmux_window,
                    TMUX_MANAGED_RUN_OPTION,
                    run_id,
                )
                with self._workspace_tmux_cwd(
                    root, remote_run_id=payload.get("remote_run_id")
                ) as cwd_fd:
                    self._assert_file_run_execution_handles(metadata_dir, metadata_fd, runner_fd)
                    runner_exec_fd = _make_sealed_memfd(
                        FILE_RUN_WRAPPER_SCRIPT.encode("utf-8"),
                        "termroom-file-runner",
                    )
                    try:
                        result = self._tmux_with_descriptors(
                            ("respawn-pane", "-k", "-c", "/", "-t", tmux_window),
                            (cwd_fd, metadata_fd, runner_exec_fd, source_fd),
                            (
                                "/bin/sh",
                                "{fd:2}",
                                "{fd:1}",
                                run_id,
                                runner.id,
                                runner.runtime_error_code,
                                *runner.argv[:-1],
                                "{fd:3}",
                            ),
                            inherit=(1, 2, 3),
                            check=False,
                        )
                    finally:
                        os.close(runner_exec_fd)
                        runner_exec_fd = -1
                os.close(source_fd)
                if result.returncode:
                    raise NodeAgentError(
                        result.stderr.strip() or "File Run could not start",
                        code="tmux_failed",
                    )
            except (NodeAgentError, OSError, subprocess.SubprocessError):
                if runner_exec_fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(runner_exec_fd)
                with contextlib.suppress(OSError):
                    os.close(source_fd)
                os.close(runner_fd)
                os.close(metadata_fd)
                self._rollback_file_run_slot(
                    session,
                    tmux_window,
                    run_id=run_id,
                    created=created,
                    previous_role=previous_role,
                    previous_run_id=previous_run_id,
                )
                raise
            self._file_run_execution_fds[(workspace_id, run_id)] = (
                metadata_fd,
                runner_fd,
            )

            windows = self._list_terminals(session)
            terminal = next(
                (
                    item
                    for item in windows
                    if item.get("role") == "file_run" and item.get("managed_run_id") == run_id
                ),
                None,
            )
            if terminal is None:
                self._release_file_run_execution(workspace_id, run_id)
                raise NodeAgentError(
                    "Managed File Run Terminal is missing", code="managed_terminal_missing"
                )
            observation, _ = self._file_run_observation(session, metadata_dir, run_id)
            if observation["state"] in {"finished", "failed", "stopped", "lost"}:
                self._release_file_run_execution(workspace_id, run_id)
            return {
                "terminal": terminal,
                "terminals": windows,
                "observation": observation,
                "replayed": False,
            }

    def _observe_file_run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._runner_registry_version(payload)
        self._workspace_path(payload)
        session = self._session(payload)
        workspace_id = self._file_run_workspace_id(payload.get("workspace_id"))
        run_id = self._file_run_id(payload.get("run_id"))
        with self._file_run_lock(workspace_id):
            metadata_dir = self._file_run_metadata_dir(workspace_id, run_id, create=False)
            observation, windows = self._file_run_observation(session, metadata_dir, run_id)
            if observation["state"] in {"finished", "failed", "stopped", "lost"}:
                self._release_file_run_execution(workspace_id, run_id)
            result: dict[str, Any] = {"observation": observation}
            if windows is not None:
                result["terminals"] = windows
            return result

    def _file_run_observation(
        self, session: str, metadata_dir: Path, run_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
        completion = self._read_file_run_record(metadata_dir / "completion.json", run_id)
        if completion is not None and isinstance(completion.get("exit_code"), int):
            windows = self._list_terminals(session) if self._session_exists(session) else None
            return (
                {
                    "state": "stopped"
                    if file_run_completion_was_stopped(completion)
                    else "finished",
                    "started_at": completion.get("started_at"),
                    "ended_at": completion.get("ended_at"),
                    "exit_code": int(completion["exit_code"]),
                },
                windows,
            )
        prepare = self._read_file_run_record(metadata_dir / "prepare.json", run_id)
        if prepare is not None and prepare.get("state") == "failed":
            windows = self._list_terminals(session) if self._session_exists(session) else None
            return (
                {
                    "state": "failed",
                    "ended_at": prepare.get("ended_at"),
                    "error_code": prepare.get("error_code"),
                },
                windows,
            )
        if not self._session_exists(session):
            return (
                {"state": "lost", "error_code": "managed_terminal_missing"},
                None,
            )
        windows = self._list_terminals(session)
        slot = next((item for item in windows if item.get("role") == "file_run"), None)
        if slot is None or slot.get("managed_run_id") != run_id:
            return (
                {"state": "lost", "error_code": "managed_terminal_missing"},
                windows,
            )
        pane = self._file_run_pane(str(slot["tmux_window"]))
        state = self._read_file_run_record(metadata_dir / "state.json", run_id)
        if pane is not None and not pane["dead"]:
            return (
                {
                    "state": "running"
                    if state is not None and state.get("state") == "running"
                    else "preparing",
                    "started_at": state.get("started_at") if state else None,
                },
                windows,
            )
        metadata_fd = self._file_run_metadata_fd(metadata_dir.parent.name, run_id, create=False)
        force_stopped = False
        if metadata_fd is not None:
            try:
                force_stopped = self._file_run_leaf_exists(metadata_fd, "force-stopped")
            finally:
                os.close(metadata_fd)
        if force_stopped:
            return (
                {
                    "state": "stopped",
                    "started_at": state.get("started_at") if state else None,
                    "exit_code": pane.get("exit_code") if pane else None,
                    "error_code": "forced",
                },
                windows,
            )
        dispatch_at = self._file_run_dispatch_time(metadata_dir, run_id)
        if file_run_completion_grace_active(pane, dispatch_at=dispatch_at):
            return (
                {
                    "state": "running" if state else "preparing",
                    "started_at": state.get("started_at") if state else None,
                },
                windows,
            )
        fallback = file_run_dead_pane_fallback(state, pane)
        if fallback is not None:
            return fallback, windows
        return (
            {
                "state": "lost",
                "started_at": state.get("started_at") if state else None,
                "error_code": "completion_missing",
            },
            windows,
        )

    def _control_file_run(self, payload: Mapping[str, Any], *, force: bool) -> bool:
        self._runner_registry_version(payload)
        self._workspace_path(payload)
        session = self._session(payload)
        workspace_id = self._file_run_workspace_id(payload.get("workspace_id"))
        run_id = self._file_run_id(payload.get("run_id"))
        with self._file_run_lock(workspace_id):
            metadata_fd = self._file_run_metadata_fd(workspace_id, run_id, create=False)
            if metadata_fd is None:
                return False
            with contextlib.ExitStack() as cleanup:
                cleanup.callback(os.close, metadata_fd)
                try:
                    stored_bytes, _ = self._read_file_run_leaf(
                        metadata_fd, "request-id", limit=4096
                    )
                    stored_id = stored_bytes.decode("utf-8").strip()
                except (FileNotFoundError, OSError, UnicodeDecodeError, NodeAgentError):
                    return False
                if stored_id != run_id:
                    return False
                if not self._session_exists(session):
                    return False
                terminal = next(
                    (
                        item
                        for item in self._list_terminals(session)
                        if item.get("role") == "file_run" and item.get("managed_run_id") == run_id
                    ),
                    None,
                )
                if terminal is None:
                    return False
                pane = self._file_run_pane(str(terminal["tmux_window"]))
                if pane is None or pane["dead"]:
                    return False
                _atomic_private_write(
                    Path("stop-requested-at"),
                    (str(time.time()) + "\n").encode("utf-8"),
                    mode=0o600,
                    directory_fd=metadata_fd,
                )
                if not force:
                    result = self._tmux(
                        "send-keys",
                        "-t",
                        str(terminal["tmux_window"]),
                        "C-c",
                        check=False,
                    )
                    return result.returncode == 0
                pane_pid = pane.get("pane_pid")
                if not isinstance(pane_pid, int):
                    raise NodeAgentError(
                        "Managed File Run process identity is unavailable",
                        code="managed_terminal_missing",
                    )
                try:
                    os.killpg(pane_pid, signal.SIGKILL)
                except ProcessLookupError:
                    return False
                _atomic_private_write(
                    Path("force-stopped"),
                    (str(time.time()) + "\n").encode("utf-8"),
                    mode=0o600,
                    directory_fd=metadata_fd,
                )
                return True

    def _rollback_file_run_slot(
        self,
        session: str,
        tmux_window: str,
        *,
        run_id: str,
        created: bool,
        previous_role: str,
        previous_run_id: str | None,
    ) -> None:
        with contextlib.suppress(NodeAgentError, OSError, subprocess.SubprocessError):
            current = next(
                (
                    item
                    for item in self._list_terminals(session)
                    if item.get("tmux_window") == tmux_window
                ),
                None,
            )
            if (
                current is None
                or current.get("role") != "file_run"
                or current.get("managed_run_id") != run_id
            ):
                return
            if created:
                self._tmux("kill-window", "-t", tmux_window, check=False)
                return
            if previous_role == "shell":
                self._tmux(
                    "set-window-option",
                    "-u",
                    "-t",
                    tmux_window,
                    TMUX_TERMINAL_ROLE_OPTION,
                    check=False,
                )
                self._tmux(
                    "set-window-option",
                    "-u",
                    "-t",
                    tmux_window,
                    TMUX_MANAGED_RUN_OPTION,
                    check=False,
                )
                return
            self._tmux(
                "set-window-option",
                "-t",
                tmux_window,
                TMUX_TERMINAL_ROLE_OPTION,
                previous_role,
                check=False,
            )
            if previous_run_id:
                self._tmux(
                    "set-window-option",
                    "-t",
                    tmux_window,
                    TMUX_MANAGED_RUN_OPTION,
                    previous_run_id,
                    check=False,
                )
            else:
                self._tmux(
                    "set-window-option",
                    "-u",
                    "-t",
                    tmux_window,
                    TMUX_MANAGED_RUN_OPTION,
                    check=False,
                )

    def _session_exists(self, session: str) -> bool:
        return self._tmux("has-session", "-t", session, check=False).returncode == 0

    def _file_run_pane(self, tmux_window: str) -> dict[str, Any] | None:
        result = self._tmux(
            "list-panes",
            "-t",
            tmux_window,
            "-F",
            "#{pane_id}\t#{pane_dead}\t#{pane_dead_status}\t#{pane_pid}\t#{pane_dead_time}",
            check=False,
        )
        if result.returncode:
            return None
        line = next((value for value in result.stdout.splitlines() if value.strip()), "")
        if not line:
            return None
        parts = line.split("\t", 4)
        parts.extend([""] * (5 - len(parts)))
        pane_id, dead, dead_status, pane_pid, dead_time = parts
        return {
            "pane_id": pane_id,
            "dead": dead == "1",
            "exit_code": int(dead_status) if dead_status.lstrip("-").isdigit() else None,
            "pane_pid": int(pane_pid) if pane_pid.isdigit() else None,
            "dead_at": int(dead_time) if dead_time.isdigit() else None,
        }

    def _session(self, payload: Mapping[str, Any]) -> str:
        session = str(payload.get("tmux_session") or "")
        if not node_session_is_valid(session):
            raise NodeAgentError("Workspace session identity is invalid", code="session_invalid")
        return session

    def _window(self, payload: Mapping[str, Any], session: str) -> str:
        window = str(payload.get("tmux_window") or "")
        if not NODE_WINDOW_PATTERN.fullmatch(window):
            raise NodeAgentError("Terminal identity is invalid", code="terminal_invalid")
        if not any(item["tmux_window"] == window for item in self._list_terminals(session)):
            raise NodeAgentError(
                "Terminal does not belong to this Workspace", code="terminal_invalid"
            )
        return window

    def _list_terminals(self, session: str) -> list[dict[str, Any]]:
        result = self._tmux("list-windows", "-t", session, "-F", TMUX_TERMINAL_RECORD_FORMAT)
        return [dict(item) for item in parse_tmux_terminal_records(result.stdout)]

    def _tmux_with_descriptors(
        self,
        tmux_args: Sequence[str],
        descriptors: Sequence[int],
        command: Sequence[str] = (),
        *,
        cwd_index: int = 0,
        inherit: Sequence[int] = (),
        environment: Mapping[str, str] | None = None,
        rollback: Callable[[subprocess.CompletedProcess[str]], None] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if (
            not descriptors
            or not 0 <= cwd_index < len(descriptors)
            or any(not 0 <= index < len(descriptors) for index in inherit)
            or not hasattr(socket, "SCM_RIGHTS")
            or not hasattr(socket, "SO_PEERCRED")
        ):
            raise NodeAgentError(
                "tmux descriptor handoff is unavailable", code="capability_unsupported"
            )
        expected: list[list[int]] = []
        for descriptor in descriptors:
            info = os.fstat(descriptor)
            value = [info.st_dev, info.st_ino, stat_module.S_IFMT(info.st_mode), info.st_uid]
            if stat_module.S_ISREG(info.st_mode):
                value.append(info.st_nlink)
            expected.append(value)
        endpoint = "termroom-" + uuid.uuid4().hex
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection: socket.socket | None = None
        listener.settimeout(10.0)
        try:
            listener.bind("\0" + endpoint)
            listener.listen(1)
            config = json.dumps(
                {
                    "expected": expected,
                    "cwd": cwd_index,
                    "inherit": list(inherit),
                    "command": list(command),
                    "environment": dict(environment or {}),
                },
                separators=(",", ":"),
            )
            result = self._tmux(
                *tmux_args,
                sys.executable,
                "-c",
                _TMUX_DESCRIPTOR_LAUNCHER,
                endpoint,
                config,
                check=False,
            )
            if result.returncode:
                if check:
                    raise NodeAgentError(
                        result.stderr.strip() or "tmux operation failed", code="tmux_failed"
                    )
                return result
            try:
                connection, _ = listener.accept()
                connection.settimeout(5.0)
                credentials = connection.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
                )
                _pid, uid, _gid = struct.unpack("3i", credentials)
                if uid != os.geteuid():
                    raise OSError("tmux pane launcher has an unexpected owner")
                descriptor_array = array.array("i", descriptors)
                sent = connection.sendmsg(
                    [b"F"],
                    [
                        (
                            socket.SOL_SOCKET,
                            socket.SCM_RIGHTS,
                            descriptor_array.tobytes(),
                        )
                    ],
                )
                if sent != 1 or connection.recv(1) != b"R":
                    raise OSError("tmux pane launcher rejected descriptor handoff")
            except (OSError, TimeoutError) as exc:
                if rollback is not None:
                    with contextlib.suppress(Exception):
                        rollback(result)
                raise NodeAgentError(
                    "tmux pane did not acquire its verified descriptors",
                    code="tmux_handoff_failed",
                ) from exc
            return result
        finally:
            if connection is not None:
                connection.close()
            listener.close()

    @staticmethod
    def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        for key in tuple(environment):
            if key.startswith("TERMROOM_"):
                environment.pop(key, None)
        command = ["tmux"]
        test_socket = environment.get("PYTEST_TMUX_SOCKET", "")
        if test_socket:
            command.extend(("-S", test_socket))
        result = subprocess.run(
            [*command, *args],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        if check and result.returncode:
            raise NodeAgentError(
                result.stderr.strip() or "tmux operation failed", code="tmux_failed"
            )
        return result

    @staticmethod
    def _relative(root: Path, path: Path) -> str:
        value = path.relative_to(root)
        return "." if value == Path(".") else value.as_posix()

    @staticmethod
    def _snapshot(snapshot: Any, *, include_content: bool = True) -> dict[str, Any]:
        result = {
            "relative_path": snapshot.relative_path,
            "digest": snapshot.digest,
            "mtime_ns": snapshot.mtime_ns,
        }
        if include_content:
            result["content"] = snapshot.content
        return result

    async def close_streams(self) -> None:
        streams = list(self.streams.values())
        self.streams.clear()
        for stream in streams:
            if isinstance(
                stream,
                (
                    UploadAgentStream,
                    TextUploadAgentStream,
                    NodeRemoteRunUploadStream,
                    NodeRemoteRunMetadataStream,
                ),
            ):
                await stream.abort()
            else:
                await stream.close()


def _next_iterator_chunk(iterator: Iterator[bytes]) -> bytes | None:
    try:
        return next(iterator)
    except StopIteration:
        return None


class _WorkspaceSourceFlowControl:
    """A fixed credit window for Node-to-Core Workspace Source frames."""

    def __init__(self) -> None:
        self._credits = 0
        self._closed = False
        self._condition = asyncio.Condition()

    async def claim(self) -> bool:
        async with self._condition:
            await self._condition.wait_for(lambda: self._closed or self._credits > 0)
            if self._closed:
                return False
            self._credits -= 1
            return True

    async def grant(self, kind: str, values: Mapping[str, Any]) -> None:
        count = values.get("count")
        if (
            kind != "credit"
            or type(count) is not int
            or not 1 <= count <= NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW
        ):
            raise NodeAgentError(
                "Workspace Source stream control is invalid", code="stream_control_invalid"
            )
        async with self._condition:
            if self._closed:
                return
            if self._credits != 0:
                raise NodeAgentError(
                    "Workspace Source stream credit is invalid",
                    code="stream_control_invalid",
                )
            self._credits = count
            self._condition.notify_all()

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()


class WorkspaceManifestAgentStream:
    def __init__(
        self,
        stream_id: str,
        manifest: WorkspaceManifest,
        frame_count: int,
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
        registry: dict[str, AgentStream],
    ) -> None:
        self.stream_id = stream_id
        self.manifest = manifest
        self.frame_count = frame_count
        self.send = send
        self.registry = registry
        self.closed = False
        self.flow = _WorkspaceSourceFlowControl()

    async def start(self) -> None:
        try:

            def chunks() -> Iterator[bytes]:
                for entry in self.manifest.entries:
                    encoded = NodeRuntime._workspace_source_entry_bytes(entry)
                    for offset in range(0, len(encoded), MAX_NODE_STREAM_CHUNK_BYTES):
                        yield encoded[offset : offset + MAX_NODE_STREAM_CHUNK_BYTES]

            iterator = chunks()
            for _index in range(self.frame_count):
                if self.closed or not await self.flow.claim():
                    return
                try:
                    chunk = next(iterator)
                except StopIteration as exc:
                    raise NodeAgentError(
                        "Workspace manifest stream ended before its declared frames",
                        code="source_manifest",
                    ) from exc
                await _send_stream_data(self.send, self.stream_id, chunk)
            try:
                next(iterator)
            except StopIteration:
                pass
            else:
                raise NodeAgentError(
                    "Workspace manifest stream exceeded its declared frames",
                    code="source_manifest",
                )
            if not self.closed:
                await self.send({"type": "stream.close", "stream_id": self.stream_id})
        except Exception as exc:
            if not self.closed:
                with contextlib.suppress(Exception):
                    await self.send(
                        {
                            "type": "stream.error",
                            "stream_id": self.stream_id,
                            "code": _error_code(exc),
                            "error": str(exc)[:500] or "Workspace manifest stream failed",
                        }
                    )
        finally:
            await self.close()

    async def feed(self, chunk: bytes) -> None:
        raise NodeAgentError("Workspace manifest stream is read-only", code="stream_direction")

    async def control(self, kind: str, values: Mapping[str, Any]) -> None:
        await self.flow.grant(kind, values)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.flow.close()
        self.manifest = WorkspaceManifest((), 0)
        self.registry.pop(self.stream_id, None)


class WorkspaceFileAgentStream:
    def __init__(
        self,
        stream_id: str,
        source_root: Path,
        entry: WorkspaceEntry,
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
        registry: dict[str, AgentStream],
    ) -> None:
        self.stream_id = stream_id
        self.source_root = source_root
        self.entry = entry
        self.frame_count = (
            entry.size + MAX_NODE_STREAM_CHUNK_BYTES - 1
        ) // MAX_NODE_STREAM_CHUNK_BYTES
        self.send = send
        self.registry = registry
        self.closed = False
        self.iterator: Iterator[bytes] | None = None
        self.flow = _WorkspaceSourceFlowControl()

    async def start(self) -> None:
        try:
            self.iterator = iter_stable_local_file_chunks(
                self.source_root,
                self.entry,
                chunk_size=MAX_NODE_STREAM_CHUNK_BYTES,
            )
            for _index in range(self.frame_count):
                if self.closed or not await self.flow.claim():
                    return
                chunk = await asyncio.to_thread(_next_iterator_chunk, self.iterator)
                if chunk is None:
                    raise NodeAgentError(
                        "Workspace file stream ended before its declared frames",
                        code="source_file_changed",
                    )
                await _send_stream_data(self.send, self.stream_id, chunk)
            extra = await asyncio.to_thread(_next_iterator_chunk, self.iterator)
            if extra is not None:
                raise NodeAgentError(
                    "Workspace file stream exceeded its declared frames",
                    code="source_file_changed",
                )
            if not self.closed:
                await self.send({"type": "stream.close", "stream_id": self.stream_id})
        except Exception as exc:
            if not self.closed:
                with contextlib.suppress(Exception):
                    await self.send(
                        {
                            "type": "stream.error",
                            "stream_id": self.stream_id,
                            "code": _error_code(exc),
                            "error": str(exc)[:500] or "Workspace file stream failed",
                        }
                    )
        finally:
            await self.close()

    async def feed(self, chunk: bytes) -> None:
        raise NodeAgentError("Workspace file stream is read-only", code="stream_direction")

    async def control(self, kind: str, values: Mapping[str, Any]) -> None:
        await self.flow.grant(kind, values)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.flow.close()
        iterator = self.iterator
        self.iterator = None
        if iterator is not None:
            close = getattr(iterator, "close", None)
            if close is not None:
                await asyncio.to_thread(close)
        self.registry.pop(self.stream_id, None)


class TerminalAgentStream:
    def __init__(
        self,
        stream_id: str,
        process_pid: int,
        master_fd: int,
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
        registry: dict[str, AgentStream],
        set_grid_resize: Callable[[bool], bool] | None = None,
        grid_resize_applied: Callable[[], object] | None = None,
        cleanup: Callable[[], object] | None = None,
        wait_grid_resize: Callable[[int, int], bool] | None = None,
        freeze_grid: Callable[[], bool] | None = None,
    ) -> None:
        self.stream_id = stream_id
        self.process_pid = process_pid
        self.master_fd = master_fd
        self.send = send
        self.registry = registry
        self.set_grid_resize = set_grid_resize
        self.grid_resize_applied = grid_resize_applied
        self.cleanup = cleanup
        self.wait_grid_resize = wait_grid_resize
        self.freeze_grid = freeze_grid
        self.grid_active = False
        self.last_viewport: tuple[int, int] | None = None
        self.closed = False
        self.resize_lock = asyncio.Lock()
        self.resize_revision = 0
        self.resize_payload: dict[str, Any] | None = None
        self.resize_result: dict[str, Any] | None = None
        self.cleanup_confirmed = False

    async def resize(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        values = {
            key: payload.get(key)
            for key in ("stream_id", "revision", "rows", "cols", "affects_grid", "bootstrap")
        }
        revision, rows, cols = values["revision"], values["rows"], values["cols"]
        if (
            values["stream_id"] != self.stream_id
            or type(revision) is not int
            or not 1 <= revision <= 2**31 - 1
            or type(rows) is not int
            or not 4 <= rows <= 500
            or type(cols) is not int
            or not 20 <= cols <= 1000
            or type(values["affects_grid"]) is not bool
            or type(values["bootstrap"]) is not bool
            or (values["bootstrap"] and not values["affects_grid"])
        ):
            raise NodeAgentError("Terminal resize is invalid", code="terminal_resize_invalid")
        async with self.resize_lock:
            if revision == self.resize_revision and values == self.resize_payload:
                assert self.resize_result is not None
                return dict(self.resize_result)
            if revision <= self.resize_revision or self.closed:
                raise NodeAgentError("Terminal resize is stale", code="terminal_resize_stale")
            if values["bootstrap"] and self.grid_resize_applied is None:
                raise NodeAgentError("Terminal bootstrap is stale", code="terminal_resize_stale")
            self.resize_revision = revision
            self.resize_payload = values
            outcome = {
                **values,
                "ok": False,
                "shared_applied": False,
                "viewport_applied": False,
                "passive_restored": False,
                "bootstrap_consumed": False,
                "grid_active": self.grid_active,
                "retryable": True,
                "cleanup_confirmed": False,
            }
            self.resize_result = outcome
            affects_grid = values["affects_grid"]
            try:
                if affects_grid != self.grid_active or affects_grid:
                    if self.set_grid_resize is not None and not await asyncio.to_thread(
                        self.set_grid_resize, affects_grid
                    ):
                        if self.grid_active and not affects_grid:
                            await self.close()
                            outcome.update(
                                retryable=False,
                                cleanup_confirmed=self.cleanup_confirmed,
                                grid_active=self.grid_active and not self.cleanup_confirmed,
                            )
                        return dict(outcome)
                    self.grid_active = affects_grid
                viewport = (rows, cols)
                if viewport != self.last_viewport:
                    winsize = struct.pack("HHHH", rows, cols, 0, 0)
                    await asyncio.to_thread(
                        fcntl.ioctl, self.master_fd, termios.TIOCSWINSZ, winsize
                    )
                    os.killpg(self.process_pid, signal.SIGWINCH)
                    self.last_viewport = viewport
                outcome["viewport_applied"] = True
                if (
                    affects_grid
                    and self.wait_grid_resize is not None
                    and not await asyncio.to_thread(self.wait_grid_resize, rows, cols)
                ):
                    raise NodeAgentError(
                        "Terminal grid resize was not applied", code="terminal_resize_failed"
                    )
                outcome["shared_applied"] = affects_grid
                if affects_grid and self.grid_resize_applied is not None:
                    await asyncio.to_thread(self.grid_resize_applied)
                    self.grid_resize_applied = None
                    outcome["bootstrap_consumed"] = True
                if values["bootstrap"]:
                    if self.freeze_grid is not None and not await asyncio.to_thread(
                        self.freeze_grid
                    ):
                        raise NodeAgentError(
                            "Terminal grid freeze failed", code="terminal_resize_failed"
                        )
                    if self.set_grid_resize is not None and not await asyncio.to_thread(
                        self.set_grid_resize, False
                    ):
                        await self.close()
                        outcome.update(
                            retryable=False,
                            cleanup_confirmed=self.cleanup_confirmed,
                            grid_active=self.grid_active and not self.cleanup_confirmed,
                        )
                        return dict(outcome)
                    self.grid_active = False
                outcome.update(ok=True, passive_restored=not self.grid_active, retryable=False)
            except (OSError, NodeAgentError):
                if outcome["shared_applied"]:
                    await self.close()
                    outcome.update(retryable=False, cleanup_confirmed=self.cleanup_confirmed)
                elif self.grid_active:
                    if self.set_grid_resize is not None and await asyncio.to_thread(
                        self.set_grid_resize, False
                    ):
                        self.grid_active = False
                    else:
                        await self.close()
                        outcome.update(retryable=False, cleanup_confirmed=self.cleanup_confirmed)
            finally:
                outcome["grid_active"] = self.grid_active and not self.cleanup_confirmed
            return dict(outcome)

    async def start(self) -> None:
        try:
            while True:
                try:
                    chunk = await asyncio.to_thread(
                        os.read, self.master_fd, MAX_NODE_STREAM_CHUNK_BYTES
                    )
                except OSError:
                    break
                if not chunk:
                    break
                await _send_stream_data(self.send, self.stream_id, chunk)
        finally:
            if not self.closed:
                with contextlib.suppress(Exception):
                    await self.send({"type": "stream.close", "stream_id": self.stream_id})
            await self.close()

    async def feed(self, chunk: bytes) -> None:
        if not self.closed and chunk:
            await asyncio.to_thread(os.write, self.master_fd, chunk)

    async def control(self, kind: str, values: Mapping[str, Any]) -> None:
        if kind == "resize":
            raise NodeAgentError(
                "Terminal resize requires an acknowledged request",
                code="terminal_resize_unacknowledged",
            )

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.registry.pop(self.stream_id, None)
        if self.grid_active and self.freeze_grid is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.freeze_grid)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process_pid, signal.SIGTERM)
        if not await asyncio.to_thread(_wait_for_pid, self.process_pid, 1.0):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process_pid, signal.SIGKILL)
            await asyncio.to_thread(_wait_for_pid, self.process_pid, 1.0)
        with contextlib.suppress(OSError):
            os.close(self.master_fd)
        if self.cleanup is not None:
            try:
                result = await asyncio.to_thread(self.cleanup)
                self.cleanup_confirmed = result is True
            except Exception:
                self.cleanup_confirmed = False


class TextDownloadAgentStream:
    def __init__(
        self,
        stream_id: str,
        content: bytes,
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
        registry: dict[str, AgentStream],
    ) -> None:
        self.stream_id = stream_id
        self.content = content
        self.send = send
        self.registry = registry
        self.closed = False

    async def start(self) -> None:
        try:
            for offset in range(0, len(self.content), MAX_NODE_STREAM_CHUNK_BYTES):
                if self.closed:
                    break
                await _send_stream_data(
                    self.send,
                    self.stream_id,
                    self.content[offset : offset + MAX_NODE_STREAM_CHUNK_BYTES],
                )
            if not self.closed:
                await self.send({"type": "stream.close", "stream_id": self.stream_id})
        except Exception as exc:
            if not self.closed:
                with contextlib.suppress(Exception):
                    await self.send(
                        {
                            "type": "stream.error",
                            "stream_id": self.stream_id,
                            "code": "download_failed",
                            "error": str(exc)[:500],
                        }
                    )
        finally:
            await self.close()

    async def feed(self, chunk: bytes) -> None:
        raise NodeAgentError("Text download stream is read-only", code="stream_direction")

    async def control(self, kind: str, values: Mapping[str, Any]) -> None:
        return

    async def close(self) -> None:
        self.closed = True
        self.content = b""
        self.registry.pop(self.stream_id, None)


class DownloadAgentStream:
    def __init__(
        self,
        stream_id: str,
        file_fd: int,
        parent_fd: int,
        name: str,
        offset: int,
        length: int | None,
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
        registry: dict[str, AgentStream],
    ) -> None:
        self.stream_id = stream_id
        self.file_fd = file_fd
        self.parent_fd = parent_fd
        self.name = name
        self.offset = offset
        self.length = length
        self.send = send
        self.registry = registry
        self.closed = False

    async def start(self) -> None:
        try:
            opened = os.fstat(self.file_fd)
            current = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
            if not stat_module.S_ISREG(current.st_mode) or (opened.st_dev, opened.st_ino) != (
                current.st_dev,
                current.st_ino,
            ):
                raise NodeAgentError("Download path changed", code="path_changed")
            descriptor = self.file_fd
            self.file_fd = -1
            with os.fdopen(descriptor, "rb") as handle:
                handle.seek(self.offset)
                remaining = self.length
                while not self.closed:
                    if remaining is not None and remaining <= 0:
                        break
                    size = (
                        MAX_NODE_STREAM_CHUNK_BYTES
                        if remaining is None
                        else min(MAX_NODE_STREAM_CHUNK_BYTES, remaining)
                    )
                    chunk = await asyncio.to_thread(handle.read, size)
                    if not chunk:
                        break
                    if remaining is not None:
                        remaining -= len(chunk)
                    await _send_stream_data(self.send, self.stream_id, chunk)
            if not self.closed:
                await self.send({"type": "stream.close", "stream_id": self.stream_id})
        except Exception as exc:
            if not self.closed:
                with contextlib.suppress(Exception):
                    await self.send(
                        {
                            "type": "stream.error",
                            "stream_id": self.stream_id,
                            "code": exc.code
                            if isinstance(exc, NodeAgentError)
                            else "download_failed",
                            "error": str(exc)[:500],
                        }
                    )
        finally:
            await self.close()

    async def feed(self, chunk: bytes) -> None:
        raise NodeAgentError("Download stream is read-only", code="stream_direction")

    async def control(self, kind: str, values: Mapping[str, Any]) -> None:
        return

    async def close(self) -> None:
        self.closed = True
        self.registry.pop(self.stream_id, None)
        for name in ("file_fd", "parent_fd"):
            descriptor = getattr(self, name)
            if descriptor >= 0:
                os.close(descriptor)
                setattr(self, name, -1)


class UploadAgentStream:
    def __init__(
        self,
        stream_id: str,
        parent_fd: int,
        target_name: str,
        *,
        existing: os.stat_result | None,
        overwrite: bool,
        max_bytes: int,
        registry: dict[str, AgentStream],
    ) -> None:
        self.stream_id = stream_id
        self.parent_fd = parent_fd
        self.target_name = target_name
        self.existing_identity = (
            (existing.st_dev, existing.st_ino) if existing is not None else None
        )
        self.existing_mode = (
            stat_module.S_IMODE(existing.st_mode) if existing is not None else 0o644
        )
        self.overwrite = overwrite
        self.max_bytes = max_bytes
        self.registry = registry
        self.total = 0
        self.closed = False
        self.temporary_name = f".{target_name}.termroom-{uuid.uuid4().hex}"
        temporary_fd = os.open(
            self.temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        self._temporary = os.fdopen(temporary_fd, "wb")

    async def feed(self, chunk: bytes) -> None:
        if self.closed:
            raise NodeAgentError("Upload stream is closed", code="stream_closed")
        self.total += len(chunk)
        if self.total > self.max_bytes:
            await self.abort()
            raise NodeAgentError(
                "Upload exceeds the configured size limit", code="upload_too_large"
            )
        await asyncio.to_thread(self._temporary.write, chunk)

    async def control(self, kind: str, values: Mapping[str, Any]) -> None:
        return

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.registry.pop(self.stream_id, None)
        try:
            await asyncio.to_thread(self._temporary.flush)
            await asyncio.to_thread(os.fsync, self._temporary.fileno())
            await asyncio.to_thread(self._temporary.close)
            try:
                current = os.stat(
                    self.target_name,
                    dir_fd=self.parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                current = None
            identity = (current.st_dev, current.st_ino) if current is not None else None
            if identity != self.existing_identity:
                if not self.overwrite and current is not None:
                    raise FileExistsError(self.target_name)
                raise NodeAgentError("Upload path changed", code="path_changed")
            temporary_fd = os.open(
                self.temporary_name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=self.parent_fd,
            )
            try:
                os.fchmod(temporary_fd, self.existing_mode)
            finally:
                os.close(temporary_fd)
            if self.overwrite:
                os.replace(
                    self.temporary_name,
                    self.target_name,
                    src_dir_fd=self.parent_fd,
                    dst_dir_fd=self.parent_fd,
                )
            else:
                os.link(
                    self.temporary_name,
                    self.target_name,
                    src_dir_fd=self.parent_fd,
                    dst_dir_fd=self.parent_fd,
                    follow_symlinks=False,
                )
                os.unlink(self.temporary_name, dir_fd=self.parent_fd)
            os.fsync(self.parent_fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.temporary_name, dir_fd=self.parent_fd)
            os.close(self.parent_fd)
            self.parent_fd = -1

    async def abort(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.registry.pop(self.stream_id, None)
        await asyncio.to_thread(self._temporary.close)
        try:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.temporary_name, dir_fd=self.parent_fd)
        finally:
            os.close(self.parent_fd)
            self.parent_fd = -1


class TextUploadAgentStream:
    def __init__(
        self,
        stream_id: str,
        files: FileService,
        workspace_path: Path,
        relative_path: str,
        *,
        expected_digest: str,
        expected_mtime_ns: int,
        max_bytes: int,
        registry: dict[str, AgentStream],
    ) -> None:
        self.stream_id = stream_id
        self.files = files
        self.workspace_path = workspace_path
        self.relative_path = relative_path
        self.expected_digest = expected_digest
        self.expected_mtime_ns = expected_mtime_ns
        self.max_bytes = max_bytes
        self.registry = registry
        self.content = bytearray()
        self.closed = False

    async def feed(self, chunk: bytes) -> None:
        if self.closed:
            raise NodeAgentError("Text upload stream is closed", code="stream_closed")
        if len(self.content) + len(chunk) > self.max_bytes:
            await self.abort()
            raise UnsupportedFileError("Content exceeds the editable size limit")
        self.content.extend(chunk)

    async def control(self, kind: str, values: Mapping[str, Any]) -> None:
        return

    async def close(self) -> dict[str, Any] | None:
        if self.closed:
            return None
        self.closed = True
        self.registry.pop(self.stream_id, None)
        raw = bytes(self.content)
        self.content.clear()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UnsupportedFileError("Only UTF-8 text files can be saved") from exc
        snapshot = await asyncio.to_thread(
            self.files.write_text,
            self.workspace_path,
            self.relative_path,
            content,
            expected_digest=self.expected_digest,
            expected_mtime_ns=self.expected_mtime_ns,
        )
        return {
            "snapshot": {
                "relative_path": snapshot.relative_path,
                "digest": snapshot.digest,
                "mtime_ns": snapshot.mtime_ns,
            }
        }

    async def abort(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.registry.pop(self.stream_id, None)
        self.content.clear()


class NodeAgent:
    def __init__(self, config: NodeConfig, private_key: Any) -> None:
        self.config = config
        self.private_key = private_key
        state_dir = config.state_dir
        if state_dir is None:
            state_dir = Path.home() / ".local" / "state" / "termroom" / "node"
        self.runtime = NodeRuntime(
            config.allowed_roots,
            file_run_root=state_dir / "file-runs",
            remote_run_root=config.run_root or (state_dir / "runs"),
            private_state_root=state_dir,
            private_state_identity=config.state_dir_identity,
        )
        self._send_lock = asyncio.Lock()
        self._socket: ClientConnection | None = None
        self._request_tasks: set[asyncio.Task[None]] = set()
        self._request_limit = asyncio.Semaphore(NODE_MAX_CONCURRENT_REQUESTS)
        self._ssl_context = _node_ssl_context(config.ca_file)

    async def run_forever(self) -> None:
        delay = 1.0
        while True:
            self._record_status("connecting")
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except (
                OSError,
                ConnectionClosed,
                TimeoutError,
                NodeProtocolError,
                NodeAgentError,
            ) as exc:
                await self.runtime.close_streams()
                permanent = _permanent_connection_error(exc)
                if permanent is not None:
                    code, message = permanent
                    self._record_status("error", error_code=code)
                    raise NodePermanentError(message, code=code) from exc
                self._record_status("disconnected", error_code=_error_code(exc))
            else:
                delay = 1.0
                self._record_status("disconnected", error_code="node_offline")
            await asyncio.sleep(node_reconnect_delay(delay))
            delay = min(NODE_RECONNECT_MAX_SECONDS, delay * 2)

    async def run_once(self) -> None:
        url = control_websocket_url(self.config.core_url, self.config.node_id)
        ssl_options = {"ssl": self._ssl_context} if self._ssl_context is not None else {}
        async with connect(
            url,
            **ssl_options,
            max_size=MAX_NODE_MESSAGE_BYTES,
            open_timeout=10,
            ping_interval=20,
            ping_timeout=20,
        ) as websocket:
            self._socket = websocket
            try:
                await self._authenticate(websocket)
                self._record_status("connected")
                heartbeat = asyncio.create_task(self._heartbeat())
                try:
                    async for raw in websocket:
                        message = decode_message(raw)
                        await self._dispatch(message)
                finally:
                    heartbeat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat
            finally:
                self._socket = None
                await self._cancel_request_tasks()
                await self.runtime.close_streams()

    def _record_status(self, state: str, *, error_code: str | None = None) -> None:
        state_dir = self.config.state_dir
        if state_dir is None:
            return
        try:
            write_node_runtime_status(
                state_dir,
                self.config.node_id,
                state,
                error_code=error_code,
            )
        except (OSError, ValueError, NodeServiceError) as exc:
            raise NodePermanentError(
                "Node runtime status cannot be written", code="runtime_status_invalid"
            ) from exc

    def _start_request_task(self, awaitable: Awaitable[None]) -> None:
        task = asyncio.create_task(awaitable)
        self._request_tasks.add(task)
        task.add_done_callback(self._request_task_done)

    def _request_task_done(self, task: asyncio.Task[None]) -> None:
        self._request_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is None or isinstance(error, (ConnectionClosed, NodeAgentError)):
            return
        asyncio.get_running_loop().call_exception_handler(
            {
                "message": "Node background task failed",
                "exception": error,
                "task": task,
            }
        )

    async def _cancel_request_tasks(self) -> None:
        tasks = tuple(self._request_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._request_tasks.clear()

    async def _authenticate(self, websocket: ClientConnection) -> None:
        challenge = decode_message(await asyncio.wait_for(websocket.recv(), 10.0))
        if challenge.get("type") != "auth.challenge":
            raise NodeProtocolError("Core did not send a challenge", code="auth_invalid")
        nonce = str(challenge.get("nonce") or "")
        offered_capabilities = challenge.get("capabilities")
        if offered_capabilities is None:
            capabilities = NODE_REQUIRED_CAPABILITIES
        elif not isinstance(offered_capabilities, list) or any(
            not isinstance(item, str) for item in offered_capabilities
        ):
            raise NodeProtocolError("Core capabilities are invalid", code="capabilities_invalid")
        else:
            capabilities = NODE_CAPABILITIES.intersection(offered_capabilities)
        await websocket.send(
            encode_message(
                {
                    "type": "auth.response",
                    "node_id": self.config.node_id,
                    "signature": sign_challenge(self.private_key, self.config.node_id, nonce),
                    "protocol_version": NODE_PROTOCOL_VERSION,
                    "capabilities": sorted(capabilities),
                }
            )
        )
        approved = decode_message(await asyncio.wait_for(websocket.recv(), 10.0))
        if approved.get("type") != "auth.ok" or approved.get("node_id") != self.config.node_id:
            raise NodeProtocolError("Core rejected Node authentication", code="auth_rejected")

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(NODE_HEARTBEAT_SECONDS)
            await self._send({"type": "heartbeat"})

    async def _dispatch(self, message: Mapping[str, Any]) -> None:
        kind = message.get("type")
        if kind == "heartbeat.ack":
            return
        if kind == "request":
            self._start_request_task(
                self._handle_request(message, received_at=asyncio.get_running_loop().time())
            )
            return
        if kind in {"stream.data", "stream.control", "stream.close", "stream.abort"}:
            stream_id = validate_request_id(str(message.get("stream_id") or ""))
            stream = self.runtime.streams.get(stream_id)
            if stream is None:
                raise NodeProtocolError("Unknown Core stream", code="stream_unknown")
            if kind == "stream.data":
                value = message.get("data")
                if not isinstance(value, str):
                    raise NodeProtocolError("Core stream data is invalid", code="stream_invalid")
                try:
                    chunk = base64.b64decode(value.encode("ascii"), validate=True)
                except (UnicodeEncodeError, ValueError) as exc:
                    raise NodeProtocolError(
                        "Core stream data is invalid", code="stream_invalid"
                    ) from exc
                if len(chunk) > MAX_NODE_STREAM_CHUNK_BYTES:
                    raise NodeProtocolError(
                        "Core stream chunk is too large", code="stream_too_large"
                    )
                await stream.feed(chunk)
            elif kind == "stream.control":
                await stream.control(str(message.get("kind") or ""), message)
            elif kind == "stream.close":
                try:
                    result = await stream.close()
                    if isinstance(
                        stream,
                        (
                            UploadAgentStream,
                            TextUploadAgentStream,
                            NodeRemoteRunUploadStream,
                            NodeRemoteRunMetadataStream,
                        ),
                    ):
                        message: dict[str, Any] = {
                            "type": "stream.close",
                            "stream_id": stream_id,
                        }
                        if result is not None:
                            message["result"] = result
                        await self._send(message)
                except Exception as exc:
                    await self._send(
                        {
                            "type": "stream.error",
                            "stream_id": stream_id,
                            "code": _error_code(exc),
                            "error": str(exc)[:500] or "Node stream failed",
                        }
                    )
            elif isinstance(
                stream,
                (
                    UploadAgentStream,
                    TextUploadAgentStream,
                    NodeRemoteRunUploadStream,
                    NodeRemoteRunMetadataStream,
                ),
            ):
                await stream.abort()
            else:
                await stream.close()
            return
        raise NodeProtocolError("Unexpected Core message", code="message_unexpected")

    async def _handle_request(
        self,
        message: Mapping[str, Any],
        *,
        received_at: float | None = None,
    ) -> None:
        request_id = validate_request_id(str(message.get("id") or ""))
        try:
            has_version = "protocol_version" in message
            has_budget = "budget_ms" in message
            if has_version != has_budget:
                raise NodeAgentError("Node request envelope is invalid", code="request_invalid")
            if has_version:
                validate_protocol_version(message.get("protocol_version"))
                budget_seconds = validate_request_budget_ms(message.get("budget_ms"))
            else:
                budget_seconds = NODE_REQUEST_DEFAULT_BUDGET_MS / 1000
            operation = validate_request_operation(message.get("operation"))
            payload = message.get("payload")
            if not isinstance(payload, dict):
                raise NodeAgentError("Request payload is invalid", code="request_invalid")
            loop = asyncio.get_running_loop()
            admission_deadline = (
                received_at if received_at is not None else loop.time()
            ) + budget_seconds
            remaining = admission_deadline - loop.time()
            if remaining <= 0:
                raise NodeAgentError("Node request budget expired", code="deadline_exceeded")
            try:
                async with asyncio.timeout(remaining):
                    await self._request_limit.acquire()
            except TimeoutError as exc:
                raise NodeAgentError(
                    "Node request budget expired", code="deadline_exceeded"
                ) from exc
            try:
                if loop.time() >= admission_deadline:
                    raise NodeAgentError("Node request budget expired", code="deadline_exceeded")
                result = await self.runtime.handle(operation, payload, self._send)
                await self._send(
                    {
                        "type": "response",
                        "id": request_id,
                        "ok": True,
                        "result": result.value,
                    }
                )
                if (
                    operation == "terminal.resize"
                    and result.value.get("ok") is False
                    and result.value.get("retryable") is False
                    and result.value.get("grid_active") is True
                    and result.value.get("cleanup_confirmed") is False
                    and self._socket is not None
                ):
                    await self._socket.close(code=1011, reason="Terminal view cleanup failed")
                if result.start is not None:
                    self._start_request_task(result.start())
            finally:
                self._request_limit.release()
        except Exception as exc:
            await self._send_error(request_id, exc)

    async def _send_error(self, request_id: str, error: BaseException) -> None:
        await self._send(
            {
                "type": "response",
                "id": request_id,
                "ok": False,
                "code": _error_code(error),
                "error": str(error)[:500] or "Node operation failed",
            }
        )

    async def _send(self, message: Mapping[str, Any]) -> None:
        websocket = self._socket
        if websocket is None:
            raise NodeAgentError("Node control connection is closed", code="node_offline")
        async with self._send_lock:
            await websocket.send(encode_message(message))


async def _send_stream_data(
    send: Callable[[Mapping[str, Any]], Awaitable[None]], stream_id: str, chunk: bytes
) -> None:
    if len(chunk) > MAX_NODE_STREAM_CHUNK_BYTES:
        raise NodeAgentError("Node stream chunk is too large", code="stream_too_large")
    await send(
        {
            "type": "stream.data",
            "stream_id": stream_id,
            "data": base64.b64encode(chunk).decode("ascii"),
        }
    )


def _permanent_connection_error(error: BaseException) -> tuple[str, str] | None:
    if isinstance(error, NodePermanentError):
        return error.code, str(error)
    if isinstance(error, ConnectionClosed):
        received = getattr(error, "rcvd", None)
        close_code = getattr(received, "code", None)
        if close_code is None:
            close_code = getattr(error, "code", None)
        if close_code == 4001:
            return (
                "identity_in_use",
                "Another Termroom Node process connected with this identity.",
            )
        if close_code == 4403:
            return "identity_revoked", "This Termroom Node identity was revoked in Core."
        if close_code == 4406:
            return (
                "version_incompatible",
                "Core requires a different Node protocol. Update Termroom on Core and Node.",
            )
        if close_code == 4401:
            return (
                "identity_rejected",
                "Core rejected this Termroom Node identity. Review or pair a new Node.",
            )
    if isinstance(error, NodeProtocolError) and error.code in {
        "auth_invalid",
        "auth_rejected",
        "capabilities_invalid",
        "capabilities_missing",
        "version_incompatible",
    }:
        return error.code, str(error)
    return None


def _error_code(error: BaseException) -> str:
    if isinstance(error, NodeAgentError):
        return error.code
    if isinstance(error, NodeProtocolError):
        return error.code
    if isinstance(error, (NodeRemoteRunError, SourceValidationError)):
        return error.code
    if isinstance(error, FileConflictError):
        return "file_conflict"
    if isinstance(error, UnsupportedFileError):
        return "file_unsupported"
    if isinstance(error, DirectoryListingLimitError):
        return "directory_listing_limit"
    if isinstance(error, PathBoundaryError):
        return "path_outside"
    if isinstance(error, FileExistsError):
        return "already_exists"
    if isinstance(error, FileNotFoundError):
        return "not_found"
    if isinstance(error, PermissionError):
        return "permission_denied"
    return "operation_failed"


def _json_post(
    core_url: str,
    path: str,
    payload: Mapping[str, Any],
    *,
    ssl_context: ssl.SSLContext | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{core_url}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15, context=ssl_context) as response:
            raw = response.read(MAX_NODE_MESSAGE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        try:
            value = json.loads(detail)
            message = str(value.get("error") or value.get("detail") or detail)
        except json.JSONDecodeError:
            message = detail
        raise NodeAgentError(message or "Core rejected the request", code="core_rejected") from exc
    except urllib.error.URLError as exc:
        raise NodeAgentError(
            f"Could not reach Termroom Core: {exc.reason}", code="core_offline"
        ) from exc
    if len(raw) > MAX_NODE_MESSAGE_BYTES:
        raise NodeAgentError("Core response is too large", code="response_too_large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NodeAgentError("Core returned invalid JSON", code="response_invalid") from exc
    if not isinstance(value, dict):
        raise NodeAgentError("Core returned an invalid response", code="response_invalid")
    if value.get("ok") is False:
        raise NodeAgentError(
            str(value.get("error") or "Core rejected the request"),
            code=str(value.get("code") or "core_rejected"),
        )
    return value


def _atomic_private_write(
    path: Path,
    content: bytes,
    *,
    mode: int,
    directory_fd: int | None = None,
    expected_state: object = _PRIVATE_STATE_UNSPECIFIED,
    error_code: str = "file_run_state_invalid",
) -> None:
    owns_directory_fd = directory_fd is None
    if directory_fd is None:
        directory_fd, _resolved_parent = _open_private_directory(path.parent, create=True)
    temporary = f".{path.name}.{secrets.token_hex(6)}.tmp"
    fd = -1
    try:
        if expected_state is _PRIVATE_STATE_UNSPECIFIED:
            expected_state = _private_state_leaf_state(
                directory_fd,
                path.name,
                code=error_code,
                label="Private state file changed during write",
                mode=mode,
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temporary, flags, mode, dir_fd=directory_fd)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
        current_state = _private_state_leaf_state(
            directory_fd,
            path.name,
            code=error_code,
            label="Private state file changed during write",
            mode=mode,
        )
        if current_state != expected_state:
            raise NodeAgentError("Private state file changed during write", code=error_code)
        if expected_state is None:
            try:
                os.link(
                    temporary,
                    path.name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise NodeAgentError(
                    "Private state file appeared during write", code=error_code
                ) from exc
            os.unlink(temporary, dir_fd=directory_fd)
        else:
            os.replace(
                temporary,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        published_state = _private_state_leaf_state(
            directory_fd,
            path.name,
            code=error_code,
            label="Private state file changed during write",
            mode=mode,
        )
        written_info = os.fstat(fd)
        if published_state is None or published_state[:2] != (
            written_info.st_dev,
            written_info.st_ino,
        ):
            raise NodeAgentError("Private state file changed during publication", code=error_code)
        os.fsync(directory_fd)
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)
        if owns_directory_fd:
            os.close(directory_fd)


def _wait_for_pid(process_pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            finished_pid, _ = os.waitpid(process_pid, os.WNOHANG)
        except ChildProcessError:
            return True
        if finished_pid == process_pid:
            return True
        time.sleep(0.02)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process_pid, signal.SIGKILL)
    with contextlib.suppress(ChildProcessError):
        os.waitpid(process_pid, 0)
    return False
