from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import json
import os
import re
import secrets
import signal
import socket
import stat as stat_module
import struct
import subprocess
import tempfile
import termios
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from termroom.file_runs import RUNNER_REGISTRY_VERSION, resolve_runner
from termroom.files import (
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
    ensure_private_directory,
    is_within,
    resolve_no_symlink_inside,
)
from termroom.terminals import (
    FILE_RUN_WRAPPER_SCRIPT,
    TMUX_MANAGED_RUN_OPTION,
    TMUX_TERMINAL_RECORD_FORMAT,
    TMUX_TERMINAL_ROLE_OPTION,
    file_run_completion_was_stopped,
    normalize_terminal_name,
    parse_tmux_terminal_records,
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
NODE_MAX_CONCURRENT_REQUESTS = 8
NODE_SESSION_PATTERN = re.compile(r"^termroom-[A-Za-z0-9_-]{1,112}$")
NODE_WINDOW_PATTERN = re.compile(r"^@[0-9]+$")
NODE_WORKSPACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
FILE_RUN_DIGEST_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class NodeAgentError(RuntimeError):
    def __init__(self, message: str, *, code: str = "node_agent_error") -> None:
        super().__init__(message)
        self.code = code


class NodePermanentError(NodeAgentError):
    """A local identity or protocol failure that reconnecting cannot repair."""


@dataclass(frozen=True, slots=True)
class NodeConfig:
    core_url: str
    node_id: str
    name: str
    allowed_roots: tuple[Path, ...]
    state_dir: Path | None = None
    run_root: Path | None = None


@dataclass(slots=True)
class OperationResult:
    value: dict[str, Any]
    start: Callable[[], Awaitable[None]] | None = None


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


def ensure_node_identity(state_dir: Path):  # type: ignore[no-untyped-def]
    ensure_private_directory(state_dir)
    key_path = state_dir / NODE_PRIVATE_KEY_FILE
    if key_path.exists():
        if key_path.is_symlink() or not key_path.is_file():
            raise NodeAgentError(
                "Node identity path is not a regular file", code="identity_invalid"
            )
        private_key = load_private_key(key_path.read_bytes())
        with contextlib.suppress(PermissionError):
            key_path.chmod(0o600)
        return private_key
    private_key = generate_private_key()
    _atomic_private_write(key_path, private_key_pem(private_key), mode=0o600)
    return private_key


def load_node_identity(state_dir: Path):  # type: ignore[no-untyped-def]
    key_path = state_dir / NODE_PRIVATE_KEY_FILE
    try:
        info = key_path.lstat()
        if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISREG(info.st_mode):
            raise NodeAgentError(
                "Node identity path is not a regular file", code="identity_invalid"
            )
        private_key = load_private_key(key_path.read_bytes())
    except FileNotFoundError as exc:
        raise NodeAgentError(
            "Node identity is missing. Pair this Node again after revoking the old identity.",
            code="identity_missing",
        ) from exc
    except OSError as exc:
        raise NodeAgentError("Node identity is unavailable", code="identity_invalid") from exc
    with contextlib.suppress(PermissionError):
        key_path.chmod(0o600)
    return private_key


def load_node_config(state_dir: Path) -> NodeConfig:
    path = state_dir / NODE_CONFIG_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NodeAgentError(
            "Node is not paired. Run `termroom node pair` first.", code="not_paired"
        ) from exc
    if not isinstance(value, dict):
        raise NodeAgentError("Node configuration is invalid", code="config_invalid")
    roots = normalize_allowed_roots(value.get("allowed_roots", []))
    run_root = normalize_run_root(value.get("run_root"), state_dir=state_dir)
    return NodeConfig(
        core_url=normalize_core_url(str(value.get("core_url") or "")),
        node_id=validate_node_id(str(value.get("node_id") or "")),
        name=str(value.get("name") or socket.gethostname())[:120],
        allowed_roots=roots,
        state_dir=state_dir.resolve(),
        run_root=run_root,
    )


def save_node_config(state_dir: Path, config: NodeConfig) -> None:
    ensure_private_directory(state_dir)
    payload = json.dumps(
        {
            "core_url": normalize_core_url(config.core_url),
            "node_id": validate_node_id(config.node_id),
            "name": config.name,
            "allowed_roots": [str(path) for path in config.allowed_roots],
            "run_root": str(
                normalize_run_root(
                    config.run_root,
                    state_dir=config.state_dir or state_dir,
                )
            ),
            "protocol_version": NODE_PROTOCOL_VERSION,
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    _atomic_private_write(state_dir / NODE_CONFIG_FILE, payload, mode=0o600)


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
        return state_dir.resolve() / "runs"
    raw = Path(str(value)).expanduser()
    if not raw.is_absolute():
        raise NodeAgentError(
            "Node Remote Run root must be absolute", code="config_invalid"
        )
    return raw.resolve(strict=False)


def pair_node(
    *,
    state_dir: Path,
    core_url: str,
    code: str,
    allowed_roots: Sequence[str | Path],
    name: str | None = None,
    run_root: str | Path | None = None,
    timeout_seconds: float = 600.0,
) -> NodeConfig:
    normalized_core = normalize_core_url(core_url)
    roots = normalize_allowed_roots(list(allowed_roots))
    private_key = ensure_node_identity(state_dir)
    public_key = public_key_text(private_key.public_key())
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
        )
        decision = str(status.get("status") or "")
        if decision == "approved":
            config = NodeConfig(
                core_url=normalized_core,
                node_id=validate_node_id(str(status.get("node_id") or "")),
                name=(name or socket.gethostname())[:120],
                allowed_roots=roots,
                state_dir=state_dir.resolve(),
                run_root=normalize_run_root(run_root, state_dir=state_dir),
            )
            save_node_config(state_dir, config)
            return config
        if decision == "rejected":
            raise NodeAgentError("Node pairing was rejected", code="pairing_rejected")
        time.sleep(1.0)
    raise NodeAgentError("Node pairing approval timed out", code="pairing_timeout")


class NodeRuntime:
    def __init__(
        self,
        allowed_roots: Sequence[Path],
        *,
        max_edit_bytes: int = 1024 * 1024,
        file_run_root: Path | None = None,
        remote_run_root: Path | None = None,
        private_state_root: Path | None = None,
    ) -> None:
        self.allowed_roots = normalize_allowed_roots(list(allowed_roots))
        self.files = FileService(max_edit_bytes=max_edit_bytes)
        self.streams: dict[str, AgentStream] = {}
        self.file_run_root = self._prepare_file_run_root(file_run_root)
        self.remote_runs = (
            NodeRemoteRunRuntime(remote_run_root) if remote_run_root is not None else None
        )
        private_boundaries = [
            boundary
            for boundary in (
                self._prepare_source_private_root(private_state_root),
                self.file_run_root,
                self.remote_runs.run_root if self.remote_runs is not None else None,
            )
            if boundary is not None
        ]
        self.source_private_boundaries = tuple(dict.fromkeys(private_boundaries))
        self._file_run_locks: dict[str, threading.RLock] = {}
        self._file_run_locks_guard = threading.Lock()

    async def handle(
        self,
        operation: str,
        payload: Mapping[str, Any],
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> OperationResult:
        operation = validate_request_operation(operation)
        if operation == "terminal.attach":
            return await self._terminal_attach(payload, send)
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
        if operation == "terminal.create":
            return {"terminal": self._create_terminal(payload)}
        if operation == "terminal.rename":
            return {"terminal": self._rename_terminal(payload)}
        if operation == "terminal.close":
            return {"terminals": self._close_terminal(payload)}
        if operation == "terminal.scrollback":
            return {"output": self._capture_scrollback(payload)}
        if operation == "files.list":
            root = self._workspace_path(payload)
            directory, entries = self.files.list_dir(root, str(payload.get("path") or "."))
            return {
                "directory": self._relative(root, directory),
                "entries": [asdict(entry) for entry in entries],
            }
        if operation == "files.stat":
            root = self._workspace_path(payload)
            return {"entry": asdict(self.files.stat(root, str(payload.get("path") or "")))}
        if operation == "files.read_preview":
            root = self._workspace_path(payload)
            preview = self.files.read_text_preview(
                root,
                str(payload.get("path") or ""),
                mode=str(payload.get("mode") or "head"),
                offset=int(payload.get("offset") or 0),
                max_bytes=int(payload.get("max_bytes") or 256 * 1024),
            )
            return {"preview": asdict(preview)}
        if operation == "files.create":
            root = self._workspace_path(payload)
            self.files.create(
                root,
                str(payload.get("parent") or "."),
                str(payload.get("name") or ""),
                directory=payload.get("directory") is True,
            )
            return {}
        if operation == "files.rename":
            root = self._workspace_path(payload)
            self.files.rename(
                root,
                str(payload.get("path") or ""),
                str(payload.get("new_name") or ""),
            )
            return {}
        if operation == "files.delete":
            root = self._workspace_path(payload)
            self.files.delete(root, str(payload.get("path") or ""))
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
        return resolved

    @staticmethod
    def _prepare_source_private_root(value: Path | None) -> Path | None:
        if value is None:
            return None
        candidate = value.expanduser()
        try:
            info = candidate.lstat()
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise NodeAgentError(
                "Node private state is unavailable", code="node_state_invalid"
            ) from exc
        if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISDIR(info.st_mode):
            raise NodeAgentError(
                "Node private state must be a real directory", code="node_state_invalid"
            )
        return resolved

    @staticmethod
    def _require_remote_run_source_version(payload: Mapping[str, Any]) -> None:
        value = payload.get("remote_run_source_version")
        if isinstance(value, bool) or value != NODE_REMOTE_RUN_SOURCE_VERSION:
            raise NodeAgentError(
                "Node Remote Run Source version is incompatible; update Termroom",
                code="remote_run_source_version_incompatible",
            )

    def _remote_run_source_context(
        self, payload: Mapping[str, Any]
    ) -> _WorkspaceSourceContext:
        self._require_remote_run_source_version(payload)
        if payload.get("remote_run_id") not in {None, ""}:
            raise NodeAgentError(
                "A transient Remote Run Workspace cannot be a Source",
                code="source_workspace_transient",
            )
        workspace_id = self._file_run_workspace_id(payload.get("workspace_id"))
        workspace_root = self._workspace_path(
            {"workspace_path": payload.get("workspace_path")}
        )
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
            raise SourceValidationError(
                "Explicit Source paths are invalid", code="source_options"
            )
        return _WorkspaceSourceContext(
            workspace_id=workspace_id,
            workspace_root=workspace_root,
            source_root=source_root,
            source_path=source_path,
            explicitly_included=normalize_explicit_include_paths(raw_includes),
        )

    @staticmethod
    def _workspace_source_related(
        path: str, explicitly_included: frozenset[str]
    ) -> bool:
        return any(
            path == include
            or path.startswith(include + "/")
            or include.startswith(path + "/")
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

    def _remote_run_source_stat_entry(
        self, payload: Mapping[str, Any]
    ) -> WorkspaceEntry:
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
            (
                len(self._workspace_source_entry_bytes(entry))
                + MAX_NODE_STREAM_CHUNK_BYTES
                - 1
            )
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
                "frame_count": (
                    entry.size + MAX_NODE_STREAM_CHUNK_BYTES - 1
                )
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
            raise NodeAgentError(
                "Node Remote Run is unavailable", code="capability_unsupported"
            )
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
        if self._tmux("has-session", "-t", session, check=False).returncode:
            self._tmux(
                "new-session", "-d", "-s", session, "-c", str(root), "-n", "shell"
            )
        self._tmux(
            "set-window-option", "-t", session, "window-size", "latest", check=False
        )
        return self._list_terminals(session)

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
            usage = workspace_usage_from_outputs(
                panes.stdout, read_system_process_output()
            )
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
        result = self._tmux(
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
            str(root),
        )
        window = result.stdout.strip()
        return next(item for item in self._list_terminals(session) if item["tmux_window"] == window)

    def _rename_terminal(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._workspace_path(payload)
        session = self._session(payload)
        window = self._window(payload, session)
        terminal = next(
            item for item in self._list_terminals(session) if item["tmux_window"] == window
        )
        if terminal.get("role") != "shell":
            raise NodeAgentError(
                "Managed Terminals cannot be renamed", code="terminal_managed"
            )
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
            raise NodeAgentError(
                "Managed Terminals cannot be closed", code="terminal_managed"
            )
        if len(terminals) <= 1:
            raise NodeAgentError("The last Terminal cannot be closed", code="terminal_last")
        self._tmux("kill-window", "-t", window)
        return self._list_terminals(session)

    def _capture_scrollback(self, payload: Mapping[str, Any]) -> str:
        self._workspace_path(payload)
        session = self._session(payload)
        window = self._window(payload, session)
        lines = max(100, min(int(payload.get("lines") or 2000), 10_000))
        return self._tmux("capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", window).stdout

    async def _terminal_attach(
        self,
        payload: Mapping[str, Any],
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> OperationResult:
        root = self._workspace_path(payload)
        session = self._session(payload)
        self._ensure_workspace(payload)
        window = self._window(payload, session)
        stream_id = validate_request_id(str(payload.get("stream_id") or ""))
        rows = max(4, min(int(payload.get("rows") or 24), 500))
        cols = max(20, min(int(payload.get("cols") or 80), 1000))
        self._tmux("select-window", "-t", window)
        environment = os.environ.copy()
        for key in tuple(environment):
            if key.startswith("TERMROOM_") or key in {"TMUX", "TMUX_PANE"}:
                environment.pop(key, None)
        environment["TERM"] = "xterm-256color"
        process_pid, master_fd = await asyncio.to_thread(
            spawn_pty_process,
            ["tmux", "attach-session", "-t", session],
            cwd=str(root),
            environment=environment,
            rows=rows,
            cols=cols,
        )
        stream = TerminalAgentStream(stream_id, process_pid, master_fd, send, self.streams)
        self.streams[stream_id] = stream
        return OperationResult({"stream_id": stream_id}, start=stream.start)

    async def _read_text_open(
        self,
        payload: Mapping[str, Any],
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
    ) -> OperationResult:
        root = self._workspace_path(payload)
        relative_path = str(payload.get("path") or "")
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
        root = self._workspace_path(payload)
        relative_path = str(payload.get("path") or "")
        expected_digest = str(payload.get("expected_digest") or "")
        expected_mtime_ns = int(payload.get("expected_mtime_ns") or 0)
        max_bytes = min(
            self.files.max_edit_bytes,
            max(1, int(payload.get("max_bytes") or 1)),
        )
        current = await asyncio.to_thread(self.files.read_text, root, relative_path)
        if (
            current.digest != expected_digest
            or current.mtime_ns != expected_mtime_ns
        ):
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
        root = self._workspace_path(payload)
        target = self.files.resolve_regular_file(root, str(payload.get("path") or ""))
        info = target.stat()
        offset = max(0, min(int(payload.get("offset") or 0), info.st_size))
        length_value = payload.get("length")
        length = None if length_value is None else max(0, int(length_value))
        stream_id = validate_request_id(str(payload.get("stream_id") or ""))
        stream = DownloadAgentStream(
            stream_id, target, offset, length, send, self.streams
        )
        self.streams[stream_id] = stream
        return OperationResult(
            {"stream_id": stream_id, "size": info.st_size}, start=stream.start
        )

    async def _upload_open(self, payload: Mapping[str, Any]) -> OperationResult:
        root = self._workspace_path(payload)
        parent = str(payload.get("parent") or ".")
        filename = str(payload.get("filename") or "")
        overwrite = payload.get("overwrite") is True
        max_bytes = max(1, int(payload.get("max_bytes") or 1))
        target = self.files.upload_target(root, parent, filename)
        if target.exists() and not overwrite:
            raise FileExistsError(filename)
        stream_id = validate_request_id(str(payload.get("stream_id") or ""))
        stream = UploadAgentStream(
            stream_id, target, overwrite=overwrite, max_bytes=max_bytes, registry=self.streams
        )
        self.streams[stream_id] = stream
        return OperationResult({"stream_id": stream_id})

    @staticmethod
    def _prepare_file_run_root(value: Path | None) -> Path | None:
        if value is None:
            return None
        candidate = value.expanduser()
        if not candidate.is_absolute():
            candidate = candidate.absolute()
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            ensure_private_directory(candidate)
            info = candidate.lstat()
        if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISDIR(info.st_mode):
            raise NodeAgentError(
                "Node File Run state path is not a real directory",
                code="file_run_state_invalid",
            )
        resolved = candidate.resolve(strict=True)
        ensure_private_directory(resolved)
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
            raise NodeAgentError(
                "File Run Workspace identity is invalid", code="file_run_invalid"
            )
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
        metadata_dir.relative_to(root)
        for directory in (workspace_dir, metadata_dir):
            try:
                info = directory.lstat()
            except FileNotFoundError:
                if not create:
                    break
                directory.mkdir(mode=0o700)
                info = directory.lstat()
            if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISDIR(info.st_mode):
                raise NodeAgentError(
                    "Node File Run metadata path is invalid",
                    code="file_run_state_invalid",
                )
            if directory.resolve(strict=True) != directory:
                raise NodeAgentError(
                    "Node File Run metadata path is not canonical",
                    code="file_run_state_invalid",
                )
            with contextlib.suppress(PermissionError):
                directory.chmod(0o700)
        return metadata_dir

    def _write_file_run_metadata(
        self,
        metadata_dir: Path,
        run_id: str,
        request_record: Mapping[str, Any],
    ) -> Path:
        metadata_dir = self._file_run_metadata_dir(
            metadata_dir.parent.name, run_id, create=True
        )
        request_id = metadata_dir / "request-id"
        if request_id.exists():
            if request_id.is_symlink() or not request_id.is_file():
                raise NodeAgentError(
                    "Node File Run metadata identity is invalid",
                    code="file_run_state_invalid",
                )
            existing = request_id.read_text(encoding="utf-8").strip()
            if existing != run_id:
                raise NodeAgentError(
                    "Node File Run metadata identity does not match",
                    code="file_run_state_invalid",
                )
        _atomic_private_write(request_id, (run_id + "\n").encode("utf-8"), mode=0o600)
        _atomic_private_write(
            metadata_dir / "request.json",
            json.dumps(
                dict(request_record), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8"),
            mode=0o600,
        )
        wrapper = metadata_dir / "runner.sh"
        _atomic_private_write(wrapper, FILE_RUN_WRAPPER_SCRIPT.encode("utf-8"), mode=0o700)
        return wrapper

    @staticmethod
    def _read_file_run_record(path: Path, run_id: str) -> dict[str, Any] | None:
        try:
            info = path.lstat()
            if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISREG(info.st_mode):
                return None
            if info.st_size > 16 * 1024:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
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
        root = self._workspace_path(payload)
        expected_value = payload.get("expected_digest")
        expected_digest = (
            None if expected_value is None else self._file_run_digest(expected_value)
        )
        runnable = self.files.inspect_runnable(
            root,
            str(payload.get("path") or ""),
            expected_digest=expected_digest,
        )
        runner = resolve_runner(runnable)
        return {
            "runner_registry_version": version,
            "runnable": self._runnable_payload(runnable),
            "runner": None
            if runner is None
            else {"id": runner.id, "version": runner.version},
        }

    def _start_file_run(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._runner_registry_version(payload)
        root = self._workspace_path(payload)
        session = self._session(payload)
        workspace_id = self._file_run_workspace_id(payload.get("workspace_id"))
        run_id = self._file_run_id(payload.get("run_id"))
        expected_digest = self._file_run_digest(payload.get("expected_digest"))
        expected_runner_id = str(payload.get("runner_id") or "")
        expected_runner_version = payload.get("runner_version")
        relative_path = str(payload.get("path") or "")
        if isinstance(expected_runner_version, bool):
            raise NodeAgentError(
                "File Run Runner version is invalid", code="runner_mismatch"
            )
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
            metadata_dir = self._file_run_metadata_dir(
                workspace_id, run_id, create=False
            )
            if metadata_dir.exists():
                stored_request = self._read_file_run_record(
                    metadata_dir / "request.json", run_id
                )
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
                observation, windows = self._file_run_observation(
                    session, metadata_dir, run_id
                )
                terminal = next(
                    (
                        item
                        for item in windows or []
                        if item.get("role") == "file_run"
                        and item.get("managed_run_id") == run_id
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

            runnable = self.files.inspect_runnable(
                root, relative_path, expected_digest=expected_digest
            )
            runner = resolve_runner(runnable)
            if (
                runner is None
                or runner.id != expected_runner_id
                or runner.version != runner_version
            ):
                raise NodeAgentError(
                    "File Run Runner changed before execution",
                    code="runner_mismatch",
                )

            metadata_dir = self._file_run_metadata_dir(
                workspace_id, run_id, create=True
            )
            wrapper = self._write_file_run_metadata(
                metadata_dir, run_id, request_record
            )
            self._ensure_workspace(payload)
            windows = self._list_terminals(session)
            terminal = next(
                (item for item in windows if item.get("role") == "file_run"), None
            )
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
                    str(root),
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

            tmux_window = str(terminal["tmux_window"])
            previous_role = str(terminal.get("role") or "shell")
            previous_run_id = str(terminal.get("managed_run_id") or "") or None
            try:
                self._tmux(
                    "set-window-option", "-t", tmux_window, "remain-on-exit", "on"
                )
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
                result = self._tmux(
                    "respawn-pane",
                    "-k",
                    "-c",
                    str(root),
                    "-t",
                    tmux_window,
                    "/bin/sh",
                    str(wrapper),
                    str(metadata_dir),
                    run_id,
                    runner.id,
                    runner.runtime_error_code,
                    *runner.argv,
                    check=False,
                )
                if result.returncode:
                    raise NodeAgentError(
                        result.stderr.strip() or "File Run could not start",
                        code="tmux_failed",
                    )
            except (NodeAgentError, OSError, subprocess.SubprocessError):
                self._rollback_file_run_slot(
                    session,
                    tmux_window,
                    run_id=run_id,
                    created=created,
                    previous_role=previous_role,
                    previous_run_id=previous_run_id,
                )
                raise

            windows = self._list_terminals(session)
            terminal = next(
                (
                    item
                    for item in windows
                    if item.get("role") == "file_run"
                    and item.get("managed_run_id") == run_id
                ),
                None,
            )
            if terminal is None:
                raise NodeAgentError(
                    "Managed File Run Terminal is missing", code="managed_terminal_missing"
                )
            observation, _ = self._file_run_observation(session, metadata_dir, run_id)
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
            metadata_dir = self._file_run_metadata_dir(
                workspace_id, run_id, create=False
            )
            observation, windows = self._file_run_observation(
                session, metadata_dir, run_id
            )
            result: dict[str, Any] = {"observation": observation}
            if windows is not None:
                result["terminals"] = windows
            return result

    def _file_run_observation(
        self, session: str, metadata_dir: Path, run_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
        completion = self._read_file_run_record(
            metadata_dir / "completion.json", run_id
        )
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
        force_marker = metadata_dir / "force-stopped"
        if force_marker.is_file() and not force_marker.is_symlink():
            return (
                {
                    "state": "stopped",
                    "started_at": state.get("started_at") if state else None,
                    "exit_code": pane.get("exit_code") if pane else None,
                    "error_code": "forced",
                },
                windows,
            )
        dead_at = pane.get("dead_at") if pane else None
        if isinstance(dead_at, int) and time.time() - dead_at < 2:
            return (
                {
                    "state": "running" if state else "preparing",
                    "started_at": state.get("started_at") if state else None,
                },
                windows,
            )
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
            metadata_dir = self._file_run_metadata_dir(
                workspace_id, run_id, create=False
            )
            request_id = metadata_dir / "request-id"
            try:
                stored_id = request_id.read_text(encoding="utf-8").strip()
            except (FileNotFoundError, OSError, UnicodeDecodeError):
                return False
            if request_id.is_symlink() or stored_id != run_id:
                return False
            if not self._session_exists(session):
                return False
            terminal = next(
                (
                    item
                    for item in self._list_terminals(session)
                    if item.get("role") == "file_run"
                    and item.get("managed_run_id") == run_id
                ),
                None,
            )
            if terminal is None:
                return False
            pane = self._file_run_pane(str(terminal["tmux_window"]))
            if pane is None or pane["dead"]:
                return False
            _atomic_private_write(
                metadata_dir / "stop-requested-at",
                (str(time.time()) + "\n").encode("utf-8"),
                mode=0o600,
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
                metadata_dir / "force-stopped",
                (str(time.time()) + "\n").encode("utf-8"),
                mode=0o600,
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
            "#{pane_id}\t#{pane_dead}\t#{pane_dead_status}\t#{pane_pid}\t"
            "#{pane_dead_time}",
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
        if not NODE_SESSION_PATTERN.fullmatch(session):
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

    @staticmethod
    def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        for key in tuple(environment):
            if key.startswith("TERMROOM_"):
                environment.pop(key, None)
        result = subprocess.run(
            ["tmux", *args],
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
    def _snapshot(
        snapshot: Any, *, include_content: bool = True
    ) -> dict[str, Any]:
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
        raise NodeAgentError(
            "Workspace manifest stream is read-only", code="stream_direction"
        )

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
        raise NodeAgentError(
            "Workspace file stream is read-only", code="stream_direction"
        )

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
    ) -> None:
        self.stream_id = stream_id
        self.process_pid = process_pid
        self.master_fd = master_fd
        self.send = send
        self.registry = registry
        self.closed = False

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
        if self.closed or kind != "resize":
            return
        rows = max(4, min(int(values.get("rows") or 24), 500))
        cols = max(20, min(int(values.get("cols") or 80), 1000))
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        await asyncio.to_thread(fcntl.ioctl, self.master_fd, termios.TIOCSWINSZ, winsize)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process_pid, signal.SIGWINCH)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.registry.pop(self.stream_id, None)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process_pid, signal.SIGTERM)
        await asyncio.to_thread(_wait_for_pid, self.process_pid, 1.0)
        with contextlib.suppress(OSError):
            os.close(self.master_fd)


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
        path: Path,
        offset: int,
        length: int | None,
        send: Callable[[Mapping[str, Any]], Awaitable[None]],
        registry: dict[str, AgentStream],
    ) -> None:
        self.stream_id = stream_id
        self.path = path
        self.offset = offset
        self.length = length
        self.send = send
        self.registry = registry
        self.closed = False

    async def start(self) -> None:
        try:
            with self.path.open("rb") as handle:
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
                            "code": "download_failed",
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


class UploadAgentStream:
    def __init__(
        self,
        stream_id: str,
        target: Path,
        *,
        overwrite: bool,
        max_bytes: int,
        registry: dict[str, AgentStream],
    ) -> None:
        self.stream_id = stream_id
        self.target = target
        self.overwrite = overwrite
        self.max_bytes = max_bytes
        self.registry = registry
        self.total = 0
        self.closed = False
        temporary_fd, temporary_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.termroom-"
        )
        self._temporary = os.fdopen(temporary_fd, "wb")
        self.temp_path = Path(temporary_name)

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
        await asyncio.to_thread(self._temporary.flush)
        await asyncio.to_thread(os.fsync, self._temporary.fileno())
        await asyncio.to_thread(self._temporary.close)
        if self.target.exists() and not self.overwrite:
            self.temp_path.unlink(missing_ok=True)
            raise FileExistsError(self.target.name)
        mode = self.target.stat().st_mode & 0o777 if self.target.exists() else 0o644
        os.chmod(self.temp_path, mode)
        os.replace(self.temp_path, self.target)
        directory_fd = os.open(self.target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    async def abort(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.registry.pop(self.stream_id, None)
        await asyncio.to_thread(self._temporary.close)
        self.temp_path.unlink(missing_ok=True)


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
        )
        self._send_lock = asyncio.Lock()
        self._socket: ClientConnection | None = None
        self._request_tasks: set[asyncio.Task[None]] = set()
        self._request_limit = asyncio.Semaphore(NODE_MAX_CONCURRENT_REQUESTS)

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
            await asyncio.sleep(delay)
            delay = min(NODE_RECONNECT_MAX_SECONDS, delay * 2)

    async def run_once(self) -> None:
        url = control_websocket_url(self.config.core_url, self.config.node_id)
        async with connect(
            url,
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
        await websocket.send(
            encode_message(
                {
                    "type": "auth.response",
                    "node_id": self.config.node_id,
                    "signature": sign_challenge(self.private_key, self.config.node_id, nonce),
                    "protocol_version": NODE_PROTOCOL_VERSION,
                    "capabilities": sorted(NODE_CAPABILITIES),
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
            self._start_request_task(self._handle_request(message))
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

    async def _handle_request(self, message: Mapping[str, Any]) -> None:
        request_id = validate_request_id(str(message.get("id") or ""))
        operation = validate_request_operation(message.get("operation"))
        payload = message.get("payload")
        if not isinstance(payload, dict):
            await self._send_error(request_id, NodeAgentError("Request payload is invalid"))
            return
        async with self._request_limit:
            try:
                result = await self.runtime.handle(operation, payload, self._send)
                await self._send(
                    {"type": "response", "id": request_id, "ok": True, "result": result.value}
                )
                if result.start is not None:
                    self._start_request_task(result.start())
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
        close_code = getattr(error, "code", None)
        if close_code is None:
            received = getattr(error, "rcvd", None)
            close_code = getattr(received, "code", None)
        if close_code == 4001:
            return (
                "identity_in_use",
                "Another Termroom Node process connected with this identity.",
            )
        if close_code == 4403:
            return "identity_revoked", "This Termroom Node identity was revoked in Core."
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
    if isinstance(error, PathBoundaryError):
        return "path_outside"
    if isinstance(error, FileExistsError):
        return "already_exists"
    if isinstance(error, FileNotFoundError):
        return "not_found"
    if isinstance(error, PermissionError):
        return "permission_denied"
    return "operation_failed"


def _json_post(core_url: str, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{core_url}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
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


def _atomic_private_write(path: Path, content: bytes, *, mode: int) -> None:
    ensure_private_directory(path.parent)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(6)}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        temporary.unlink(missing_ok=True)


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
