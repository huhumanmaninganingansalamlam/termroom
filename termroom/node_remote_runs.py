from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import shutil
import stat
import subprocess
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from termroom.node_core import NodeCore, NodeCoreError, NodeStream
from termroom.node_protocol import (
    MAX_NODE_MESSAGE_BYTES,
    MAX_NODE_STREAM_CHUNK_BYTES,
    NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW,
    NODE_REMOTE_RUN_SOURCE_VERSION,
    NODE_REMOTE_RUN_VERSION,
    validate_request_id,
)
from termroom.run_sources import (
    SourceFileChangedError,
    SourceValidationError,
    WorkspaceEntry,
    WorkspaceManifest,
    build_public_git_clone_invocation,
    build_workspace_manifest,
    normalize_explicit_include_paths,
    normalize_source_relative_path,
    validate_contained_symlink_target,
    validate_cwd_rel,
    validate_public_https_git_url,
)
from termroom.security import ensure_private_directory, is_within
from termroom.ssh_backend import (
    REMOTE_GIT_BOOTSTRAP_SCRIPT,
    REMOTE_RUN_INITIAL_TAIL,
    REMOTE_RUN_LOG_PIPE_SCRIPT,
    REMOTE_RUN_LOG_READ_LIMIT,
    REMOTE_RUN_SESSION_PREFIX,
    REMOTE_RUNNER_SCRIPT,
    SSHBackend,
    SSHBackendError,
)
from termroom.terminals import (
    TMUX_MANAGED_RUN_OPTION,
    TMUX_TERMINAL_RECORD_FORMAT,
    TMUX_TERMINAL_ROLE_OPTION,
    parse_tmux_terminal_records,
)


class NodeRemoteRunError(SSHBackendError):
    """A typed Node target failure that fits the existing Remote Run boundary."""

    def __init__(self, message: str, *, code: str = "node_remote_run_error") -> None:
        super().__init__(message)
        self.code = code


def _validate_run_id(value: object) -> str:
    return SSHBackend.validate_remote_run_id(str(value or ""))


def _normalize_command(value: object) -> str:
    command = str(value or "")
    if not command.strip():
        raise NodeRemoteRunError("Remote Run command cannot be empty", code="command_required")
    if "\x00" in command:
        raise NodeRemoteRunError("Remote Run command cannot contain NUL", code="command_invalid")
    body = command.rstrip("\n") + "\n"
    if len(body.encode("utf-8")) > 256 * 1024:
        raise NodeRemoteRunError("Remote Run command is too large", code="command_invalid")
    return body


def _atomic_private_write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode, follow_symlinks=False)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


class NodeRemoteRunUploadStream:
    def __init__(
        self,
        stream_id: str,
        target: Path,
        *,
        expected_size: int,
        executable: bool,
        registry: dict[str, Any],
    ) -> None:
        self.stream_id = stream_id
        self.target = target
        self.expected_size = expected_size
        self.executable = executable
        self.registry = registry
        self.temporary = target.with_name(f".{target.name}.upload-{stream_id}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        self.descriptor = os.open(self.temporary, flags, 0o600)
        self.total = 0
        self.closed = False

    async def feed(self, chunk: bytes) -> None:
        if self.closed:
            raise NodeRemoteRunError("Remote Run Source stream is closed", code="stream_closed")
        self.total += len(chunk)
        if self.total > self.expected_size:
            await self.abort()
            raise NodeRemoteRunError(
                "Remote Run Source file exceeds its declared size",
                code="source_file_changed",
            )
        view = memoryview(chunk)
        while view:
            written = os.write(self.descriptor, view)
            view = view[written:]

    async def control(self, kind: str, values: Mapping[str, Any]) -> None:
        del kind, values

    async def close(self) -> dict[str, Any]:
        if self.closed:
            raise NodeRemoteRunError("Remote Run Source stream is closed", code="stream_closed")
        self.closed = True
        self.registry.pop(self.stream_id, None)
        try:
            if self.total != self.expected_size:
                raise NodeRemoteRunError(
                    "Remote Run Source file size changed during transfer",
                    code="source_file_changed",
                )
            os.fsync(self.descriptor)
            os.close(self.descriptor)
            self.descriptor = -1
            os.chmod(self.temporary, 0o700 if self.executable else 0o600)
            if self.target.exists() or self.target.is_symlink():
                raise NodeRemoteRunError(
                    "Remote Run Source path already exists",
                    code="source_path_conflict",
                )
            os.replace(self.temporary, self.target)
            return {"size": self.total}
        except BaseException:
            if self.descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(self.descriptor)
                self.descriptor = -1
            self.temporary.unlink(missing_ok=True)
            raise

    async def abort(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.registry.pop(self.stream_id, None)
        if self.descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(self.descriptor)
            self.descriptor = -1
        self.temporary.unlink(missing_ok=True)


class NodeRemoteRunMetadataStream:
    def __init__(
        self,
        stream_id: str,
        target: Path,
        *,
        expected_size: int,
        commit: Callable[[bytes], None],
        registry: dict[str, Any],
    ) -> None:
        self.stream_id = stream_id
        self.target = target
        self.expected_size = expected_size
        self.commit = commit
        self.registry = registry
        self.temporary = target.with_name(f".{target.name}.upload-{stream_id}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        self.descriptor = os.open(self.temporary, flags, 0o600)
        self.total = 0
        self.closed = False

    async def feed(self, chunk: bytes) -> None:
        if self.closed:
            raise NodeRemoteRunError("Remote Run metadata stream is closed", code="stream_closed")
        self.total += len(chunk)
        if self.total > self.expected_size:
            await self.abort()
            raise NodeRemoteRunError(
                "Remote Run metadata exceeds its declared size", code="metadata_invalid"
            )
        view = memoryview(chunk)
        while view:
            written = os.write(self.descriptor, view)
            view = view[written:]

    async def control(self, kind: str, values: Mapping[str, Any]) -> None:
        del kind, values

    async def close(self) -> dict[str, Any]:
        if self.closed:
            raise NodeRemoteRunError("Remote Run metadata stream is closed", code="stream_closed")
        self.closed = True
        self.registry.pop(self.stream_id, None)
        try:
            if self.total != self.expected_size:
                raise NodeRemoteRunError(
                    "Remote Run metadata size changed during transfer",
                    code="metadata_invalid",
                )
            os.fsync(self.descriptor)
            os.close(self.descriptor)
            self.descriptor = -1
            content = self.temporary.read_bytes()
            self.commit(content)
            self.temporary.unlink(missing_ok=True)
            return {"size": self.total}
        except BaseException:
            if self.descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(self.descriptor)
                self.descriptor = -1
            self.temporary.unlink(missing_ok=True)
            raise

    async def abort(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.registry.pop(self.stream_id, None)
        if self.descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(self.descriptor)
            self.descriptor = -1
        self.temporary.unlink(missing_ok=True)


class NodeRemoteRunRuntime:
    """Node-local implementation of the fixed Remote Run operations."""

    def __init__(self, run_root: Path) -> None:
        self.run_root = self._prepare_run_root(run_root)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    @staticmethod
    def _prepare_run_root(value: Path) -> Path:
        candidate = value.expanduser()
        if not candidate.is_absolute():
            raise NodeRemoteRunError(
                "Node Remote Run root must be absolute", code="run_root_invalid"
            )
        ensure_private_directory(candidate)
        try:
            info = candidate.lstat()
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise NodeRemoteRunError(
                "Node Remote Run root is unavailable", code="run_root_invalid"
            ) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise NodeRemoteRunError(
                "Node Remote Run root must be a real directory", code="run_root_invalid"
            )
        if info.st_mode & 0o022:
            raise NodeRemoteRunError(
                "Node Remote Run root must not be writable by other users",
                code="run_root_invalid",
            )
        return resolved

    def lock_for(self, run_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(_validate_run_id(run_id), threading.RLock())

    def preflight(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._require_version(payload)
        bash = Path("/bin/bash")
        tmux = shutil.which("tmux")
        git = shutil.which("git") if payload.get("require_git") is True else None
        if not bash.is_file() or not os.access(bash, os.X_OK):
            raise NodeRemoteRunError("Node does not provide /bin/bash", code="bash_missing")
        if not tmux:
            raise NodeRemoteRunError("Node does not provide tmux", code="tmux_missing")
        if payload.get("require_git") is True and not git:
            raise NodeRemoteRunError("Node does not provide Git", code="git_missing")
        probe = self.run_root / f".termroom-probe-{uuid.uuid4()}"
        renamed = probe.with_name(probe.name + ".renamed")
        try:
            probe.mkdir(mode=0o700)
            probe.rename(renamed)
            renamed.rmdir()
        except OSError as exc:
            with contextlib.suppress(OSError):
                probe.rmdir()
            with contextlib.suppress(OSError):
                renamed.rmdir()
            raise NodeRemoteRunError(
                "Node Remote Run root is not writable", code="run_root_unwritable"
            ) from exc
        usage = shutil.disk_usage(self.run_root)
        tools = {"bash": str(bash), "tmux": str(Path(tmux).resolve())}
        if git:
            tools["git"] = str(Path(git).resolve())
        return {
            "remote_run_version": NODE_REMOTE_RUN_VERSION,
            "run_base": str(self.run_root),
            "tools": tools,
            "available_bytes": usage.free,
            "warnings": [],
        }

    def create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        command = _normalize_command(payload.get("command"))
        cwd_rel = validate_cwd_rel(str(payload.get("cwd_rel") or "."))
        with self.lock_for(run_id):
            self._finish_interrupted_delete(run_id)
            paths = self._paths(run_id)
            if paths["root"].exists() or paths["root"].is_symlink():
                self._assert_layout(run_id)
                if self._read_regular(paths["command"], 256 * 1024) != command.encode():
                    raise NodeRemoteRunError(
                        "Remote Run id already belongs to a different command",
                        code="idempotency_conflict",
                    )
                if self._read_regular(paths["cwd"], 4096).decode().strip() != cwd_rel:
                    raise NodeRemoteRunError(
                        "Remote Run id already belongs to a different working directory",
                        code="idempotency_conflict",
                    )
                return self._layout_payload(run_id)

            creating = self._paths(run_id, leaf=f".termroom-creating-{run_id}")
            if creating["root"].exists() or creating["root"].is_symlink():
                self._remove_owned_tree(creating["root"], run_id, allow_missing_marker=True)
            try:
                creating["root"].mkdir(mode=0o700)
                creating["metadata"].mkdir(mode=0o700)
                _atomic_private_write(creating["marker"], (run_id + "\n").encode())
                creating["work"].mkdir(mode=0o700)
                _atomic_private_write(creating["cwd"], (cwd_rel + "\n").encode())
                _atomic_private_write(
                    creating["runner"], REMOTE_RUNNER_SCRIPT.encode(), mode=0o700
                )
                _atomic_private_write(
                    creating["log_pipe"], REMOTE_RUN_LOG_PIPE_SCRIPT.encode(), mode=0o700
                )
                _atomic_private_write(creating["command"], command.encode())
                os.replace(creating["root"], paths["root"])
            except BaseException:
                if creating["root"].exists() and not creating["root"].is_symlink():
                    self._remove_owned_tree(
                        creating["root"], run_id, allow_missing_marker=True
                    )
                raise
            self._assert_layout(run_id)
            return self._layout_payload(run_id)

    def snapshot_begin(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        with self.lock_for(run_id):
            paths = self._assert_layout(run_id)
            self._assert_not_started(run_id, paths)
            staging = paths["work_staging"]
            if staging.exists() or staging.is_symlink():
                self._remove_staging(staging, run_id)
            staging.mkdir(mode=0o700)
            return {"ready": True}

    def snapshot_directory(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        relative = normalize_source_relative_path(str(payload.get("path") or ""))
        with self.lock_for(run_id):
            self._assert_not_started(run_id, self._assert_layout(run_id))
            target = self._staging_target(run_id, relative)
            target.mkdir(mode=0o700)
            return {"path": relative}

    def snapshot_symlink(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        relative = normalize_source_relative_path(str(payload.get("path") or ""))
        link_target = validate_contained_symlink_target(
            relative, str(payload.get("link_target") or "")
        )
        with self.lock_for(run_id):
            self._assert_not_started(run_id, self._assert_layout(run_id))
            target = self._staging_target(run_id, relative)
            os.symlink(link_target, target)
            return {"path": relative}

    def snapshot_file_open(
        self,
        payload: Mapping[str, Any],
        registry: dict[str, Any],
    ) -> NodeRemoteRunUploadStream:
        run_id = self._identity(payload)
        relative = normalize_source_relative_path(str(payload.get("path") or ""))
        stream_id = validate_request_id(str(payload.get("stream_id") or ""))
        try:
            expected_size = int(payload.get("expected_size"))
        except (TypeError, ValueError) as exc:
            raise NodeRemoteRunError(
                "Remote Run Source size is invalid", code="source_entry_metadata"
            ) from exc
        if expected_size < 0:
            raise NodeRemoteRunError(
                "Remote Run Source size is invalid", code="source_entry_metadata"
            )
        with self.lock_for(run_id):
            self._assert_not_started(run_id, self._assert_layout(run_id))
            target = self._staging_target(run_id, relative)
            return NodeRemoteRunUploadStream(
                stream_id,
                target,
                expected_size=expected_size,
                executable=payload.get("executable") is True,
                registry=registry,
            )

    def snapshot_commit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        with self.lock_for(run_id):
            paths = self._assert_layout(run_id)
            self._assert_not_started(run_id, paths)
            staging = self._real_directory(paths["work_staging"], "Source staging")
            work = self._real_directory(paths["work"], "Remote Run work")
            if any(work.iterdir()):
                raise NodeRemoteRunError(
                    "Remote Run work directory is already committed",
                    code="work_already_committed",
                )
            work.rmdir()
            try:
                os.replace(staging, work)
            except BaseException:
                with contextlib.suppress(OSError):
                    work.mkdir(mode=0o700)
                raise
            return {"work_path": str(work)}

    def write_metadata(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        name = str(payload.get("name") or "")
        value = payload.get("value")
        encoded = self._encode_metadata(name, value)
        with self.lock_for(run_id):
            self._commit_metadata(run_id, name, encoded)
            return {"written": True}

    def metadata_open(
        self,
        payload: Mapping[str, Any],
        registry: dict[str, Any],
    ) -> NodeRemoteRunMetadataStream:
        run_id = self._identity(payload)
        name = self._metadata_name(payload.get("name"))
        stream_id = validate_request_id(str(payload.get("stream_id") or ""))
        try:
            expected_size = int(payload.get("expected_size"))
        except (TypeError, ValueError) as exc:
            raise NodeRemoteRunError(
                "Remote Run metadata size is invalid", code="metadata_invalid"
            ) from exc
        if expected_size < 0 or expected_size > 8 * 1024 * 1024:
            raise NodeRemoteRunError(
                "Remote Run metadata size is invalid", code="metadata_invalid"
            )
        with self.lock_for(run_id):
            paths = self._assert_layout(run_id)
            self._assert_not_started(run_id, paths)
            target = paths["metadata"] / name
            return NodeRemoteRunMetadataStream(
                stream_id,
                target,
                expected_size=expected_size,
                commit=lambda content: self._commit_metadata(run_id, name, content),
                registry=registry,
            )

    def start(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        with self.lock_for(run_id):
            paths = self._assert_layout(run_id)
            existing = self._existing_start(run_id, paths)
            if existing is not None:
                return existing
            self._start_tmux(run_id, paths["work"], paths["runner"])
            return {
                "state": "preparing",
                "session_name": self._session_name(run_id),
                "run_root": str(paths["root"]),
            }

    def start_git(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        url = validate_public_https_git_url(str(payload.get("url") or ""))
        with self.lock_for(run_id):
            paths = self._assert_layout(run_id)
            existing = self._existing_start(run_id, paths)
            if existing is not None:
                stored_url = self._read_regular(paths["git_url"], 64 * 1024).decode().strip()
                if stored_url != url:
                    raise NodeRemoteRunError(
                        "Remote Run id already belongs to a different Git Source",
                        code="idempotency_conflict",
                    )
                return existing
            git = shutil.which("git")
            if not git:
                raise NodeRemoteRunError("Node does not provide Git", code="git_missing")
            invocation = build_public_git_clone_invocation(
                url,
                git_path=str(Path(git).resolve()),
                askpass_path=str(paths["git_askpass"]),
                empty_home=str(paths["git_home"]),
                destination=str(paths["work_staging"]),
            )
            argv = invocation.as_env_i_argv()
            encoded_argv = b"\x00".join(item.encode() for item in argv) + b"\x00"
            paths["git_home"].mkdir(mode=0o700)
            _atomic_private_write(paths["git_askpass"], b"#!/bin/sh\nexit 1\n", mode=0o700)
            _atomic_private_write(paths["git_argv"], encoded_argv)
            _atomic_private_write(paths["git_url"], (url + "\n").encode())
            _atomic_private_write(paths["git_path"], (str(Path(git).resolve()) + "\n").encode())
            _atomic_private_write(
                paths["git_bootstrap"], REMOTE_GIT_BOOTSTRAP_SCRIPT.encode(), mode=0o700
            )
            self._start_tmux(run_id, paths["root"], paths["git_bootstrap"])
            return {
                "state": "preparing",
                "phase": "cloning",
                "session_name": self._session_name(run_id),
                "run_root": str(paths["root"]),
                "source_url": url,
            }

    def observe(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        with self.lock_for(run_id):
            try:
                paths = self._assert_layout(run_id)
            except FileNotFoundError:
                return self._unavailable_layout(run_id, missing=True)
            except NodeRemoteRunError as exc:
                return self._unavailable_layout(run_id, error=exc.code)
            return self._reconcile(run_id, paths)

    def poll(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = self.observe(payload)
        stream = str(payload.get("stream") or "command")
        offset_value = payload.get("offset")
        offset = None if offset_value is None else int(offset_value)
        limit = int(payload.get("limit") or REMOTE_RUN_LOG_READ_LIMIT)
        run_id = _validate_run_id(payload.get("run_id"))
        if result.get("layout_missing") or result.get("layout_error"):
            result["log"] = self._empty_log(stream, offset)
            return result
        with self.lock_for(run_id):
            paths = self._assert_layout(run_id)
            result["log"] = self._read_log(paths, stream=stream, offset=offset, limit=limit)
            return result

    def interrupt(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        with self.lock_for(run_id):
            try:
                paths = self._assert_layout(run_id)
            except FileNotFoundError:
                status = self._tmux_status(run_id)
                return {
                    "sent": False,
                    "completed": False,
                    "layout_missing": True,
                    "tmux_exists": status["exists"],
                    "tmux_running": status["running"],
                }
            completion = self._read_json_record(paths["completion"])
            if self._valid_completion(completion):
                return {"sent": False, "completed": True}
            self._publish_stop(paths)
            sent = False
            if self._owned_run_window(run_id):
                result = self._tmux(
                    "send-keys", "-t", f"{self._session_name(run_id)}:run.0", "C-c", check=False
                )
                sent = result.returncode == 0
            return {"sent": sent, "completed": False}

    def kill(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        with self.lock_for(run_id):
            try:
                paths = self._assert_layout(run_id)
            except FileNotFoundError:
                status = self._tmux_status(run_id)
                killed = status["exists"] and self._kill_session(run_id)
                return {
                    "killed": killed,
                    "completed": False,
                    "layout_missing": True,
                    "tmux_exists": status["exists"],
                    "tmux_running": status["running"],
                }
            completion = self._read_json_record(paths["completion"])
            if self._valid_completion(completion):
                return {"killed": False, "completed": True}
            self._publish_stop(paths)
            killed = self._kill_session(run_id)
            completion = self._read_json_record(paths["completion"])
            if self._valid_completion(completion):
                return {"killed": False, "completed": True}
            return {"killed": killed, "completed": False}

    def exists(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        with self.lock_for(run_id):
            try:
                self._assert_layout(run_id)
            except FileNotFoundError:
                return {"exists": False}
            return {"exists": True}

    def ensure_shell(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        allow_create = payload.get("allow_create_session") is True
        with self.lock_for(run_id):
            paths = self._assert_layout(run_id)
            work = self._real_directory(paths["work"], "Remote Run work")
            session = self._session_name(run_id)
            status = self._tmux_status(run_id)
            created_session = False
            if not status["exists"]:
                if not allow_create:
                    raise NodeRemoteRunError(
                        "Remote Run tmux session is missing; "
                        "terminal state is required to recreate it",
                        code="session_missing",
                    )
                self._tmux("new-session", "-d", "-s", session, "-c", str(work), "-n", "shell")
                self._tmux(
                    "set-option", "-t", session, "@termroom_remote_run_id", run_id
                )
                created_session = True
            elif not self._session_owned(run_id):
                records = self._terminal_records(session)
                legacy_owned = any(
                    item.get("role") == "remote_run"
                    and item.get("managed_run_id") == run_id
                    for item in records
                )
                if not legacy_owned:
                    raise NodeRemoteRunError(
                        "Remote Run session identity is already in use",
                        code="session_identity_conflict",
                    )
                self._tmux(
                    "set-option", "-t", session, "@termroom_remote_run_id", run_id
                )
            records = self._terminal_records(session)
            shell = next((item for item in records if item.get("role") == "shell"), None)
            if shell is None:
                result = self._tmux(
                    "new-window", "-d", "-P", "-F", "#{window_id}", "-t", session,
                    "-c", str(work), "-n", "shell"
                )
                window = result.stdout.strip()
                records = self._terminal_records(session)
                shell = next(item for item in records if item["tmux_window"] == window)
            return {
                "session_name": session,
                "work_path": str(work),
                "shell_window": shell,
                "terminals": records,
                "created_session": created_session,
            }

    def delete(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        with self.lock_for(run_id):
            paths = self._paths(run_id)
            quarantine = self.run_root / f".termroom-deleting-{run_id}"
            root_exists = paths["root"].exists() or paths["root"].is_symlink()
            quarantine_exists = quarantine.exists() or quarantine.is_symlink()
            if root_exists and quarantine_exists:
                raise NodeRemoteRunError(
                    "Multiple Remote Run roots exist; refusing automatic deletion",
                    code="cleanup_ambiguous",
                )
            self._kill_session(run_id)
            if not root_exists and not quarantine_exists:
                return {"deleted": True, "already_missing": True}
            source = paths["root"] if root_exists else quarantine
            self._assert_marked_tree(source, run_id)
            if root_exists:
                os.replace(source, quarantine)
                source = quarantine
                self._assert_marked_tree(source, run_id)
            self._remove_owned_tree(source, run_id)
            return {"deleted": True, "already_missing": False}

    def validate_workspace(self, payload: Mapping[str, Any]) -> Path:
        run_id = _validate_run_id(payload.get("remote_run_id"))
        supplied = str(payload.get("workspace_path") or "")
        with self.lock_for(run_id):
            paths = self._assert_layout(run_id)
            work = self._real_directory(paths["work"], "Remote Run work")
            if supplied != str(work):
                raise NodeRemoteRunError(
                    "Remote Run Workspace path does not match its managed Run",
                    code="path_outside",
                )
            return work

    @staticmethod
    def _require_version(payload: Mapping[str, Any]) -> None:
        value = payload.get("remote_run_version")
        if isinstance(value, bool) or value != NODE_REMOTE_RUN_VERSION:
            raise NodeRemoteRunError(
                "Node Remote Run version is incompatible; update Termroom",
                code="remote_run_version_incompatible",
            )

    def _identity(self, payload: Mapping[str, Any]) -> str:
        self._require_version(payload)
        run_id = _validate_run_id(payload.get("run_id"))
        if str(payload.get("run_base") or "") != str(self.run_root):
            raise NodeRemoteRunError(
                "Remote Run root does not match Node local policy",
                code="run_root_mismatch",
            )
        return run_id

    def _paths(self, run_id: str, *, leaf: str | None = None) -> dict[str, Path]:
        run_id = _validate_run_id(run_id)
        safe_leaf = leaf or run_id
        if safe_leaf not in {
            run_id,
            f".termroom-creating-{run_id}",
            f".termroom-deleting-{run_id}",
        }:
            raise NodeRemoteRunError("Remote Run internal path is invalid", code="path_invalid")
        root = self.run_root / safe_leaf
        metadata = root / ".termroom"
        return {
            "root": root,
            "work": root / "work",
            "work_staging": root / "work.tmp",
            "metadata": metadata,
            "marker": metadata / "marker",
            "cwd": metadata / "cwd",
            "command": metadata / "command.sh",
            "runner": metadata / "runner.sh",
            "log_pipe": metadata / "log-pipe.sh",
            "state": metadata / "state.json",
            "stop": metadata / "stop-requested-at",
            "prepare_result": metadata / "prepare-result.json",
            "prepare_log": metadata / "prepare.log",
            "output": metadata / "output.log",
            "completion": metadata / "completion.json",
            "git_url": metadata / "git-url",
            "git_path": metadata / "git-path",
            "git_revision": metadata / "git-revision",
            "git_argv": metadata / "git-argv",
            "git_askpass": metadata / "git-askpass",
            "git_home": metadata / "git-home",
            "git_bootstrap": metadata / "git-bootstrap.sh",
        }

    def _assert_layout(self, run_id: str) -> dict[str, Path]:
        paths = self._paths(run_id)
        if not paths["root"].exists() and not paths["root"].is_symlink():
            raise FileNotFoundError(str(paths["root"]))
        self._assert_marked_tree(paths["root"], run_id)
        return paths

    def _assert_marked_tree(self, root: Path, run_id: str) -> None:
        try:
            root_info = root.lstat()
            metadata = root / ".termroom"
            metadata_info = metadata.lstat()
            marker = metadata / "marker"
            marker_info = marker.lstat()
        except OSError as exc:
            raise NodeRemoteRunError(
                "Remote Run layout is incomplete", code="layout_incomplete"
            ) from exc
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise NodeRemoteRunError("Remote Run root is invalid", code="root_invalid")
        if root.resolve(strict=True) != root or root.parent != self.run_root:
            raise NodeRemoteRunError("Remote Run root is not canonical", code="root_invalid")
        if stat.S_ISLNK(metadata_info.st_mode) or not stat.S_ISDIR(metadata_info.st_mode):
            raise NodeRemoteRunError(
                "Remote Run metadata directory is invalid", code="metadata_invalid"
            )
        if stat.S_ISLNK(marker_info.st_mode) or not stat.S_ISREG(marker_info.st_mode):
            raise NodeRemoteRunError("Remote Run marker is invalid", code="marker_invalid")
        if self._read_regular(marker, 128).decode().strip() != run_id:
            raise NodeRemoteRunError(
                "Remote Run marker does not match", code="marker_mismatch"
            )

    def _layout_payload(self, run_id: str) -> dict[str, Any]:
        paths = self._paths(run_id)
        return {
            **{key: str(value) for key, value in paths.items()},
            "run_base": str(self.run_root),
            "session_name": self._session_name(run_id),
        }

    def _staging_target(self, run_id: str, relative: str) -> Path:
        paths = self._assert_layout(run_id)
        staging = self._real_directory(paths["work_staging"], "Source staging")
        target = staging.joinpath(*relative.split("/"))
        if target.parent != staging:
            parent = target.parent
            if not is_within(parent, staging):
                raise NodeRemoteRunError("Source path escapes staging", code="path_outside")
            self._real_directory(parent, "Source parent")
        if target.exists() or target.is_symlink():
            raise NodeRemoteRunError(
                "Remote Run Source path already exists", code="source_path_conflict"
            )
        return target

    @staticmethod
    def _real_directory(path: Path, label: str) -> Path:
        try:
            info = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise NodeRemoteRunError(f"{label} is unavailable", code="path_invalid") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or resolved != path:
            raise NodeRemoteRunError(f"{label} is invalid", code="path_invalid")
        return path

    @staticmethod
    def _read_regular(path: Path, limit: int) -> bytes:
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise NodeRemoteRunError("Remote Run metadata is invalid", code="metadata_invalid")
            if info.st_size > limit:
                raise NodeRemoteRunError(
                    "Remote Run metadata is too large", code="metadata_invalid"
                )
            return path.read_bytes()
        except NodeRemoteRunError:
            raise
        except OSError as exc:
            raise NodeRemoteRunError(
                "Remote Run metadata is unavailable", code="metadata_invalid"
            ) from exc

    @staticmethod
    def _validate_source_manifest(value: dict[str, Any] | list[Any]) -> None:
        if not isinstance(value, list):
            raise NodeRemoteRunError("Source manifest is invalid", code="source_manifest")
        entries: list[WorkspaceEntry] = []
        for raw in value:
            if not isinstance(raw, dict):
                raise NodeRemoteRunError("Source manifest is invalid", code="source_manifest")
            entries.append(
                WorkspaceEntry(
                    relative_path=str(raw.get("path") or ""),
                    kind=str(raw.get("kind") or ""),  # type: ignore[arg-type]
                    size=int(raw.get("size") or 0),
                    mtime_ns=int(raw.get("mtime_ns") or 0),
                    executable=raw.get("executable") is True,
                    link_target=(
                        str(raw["link_target"]) if raw.get("link_target") is not None else None
                    ),
                )
            )
        build_workspace_manifest(entries)

    @staticmethod
    def _metadata_name(value: object) -> str:
        name = str(value or "")
        if name not in {"source.json", "source-manifest.json", "inputs.json"}:
            raise NodeRemoteRunError(
                "Remote Run metadata name is unsupported", code="metadata_invalid"
            )
        return name

    def _encode_metadata(self, name: str, value: object) -> bytes:
        name = self._metadata_name(name)
        if not isinstance(value, (dict, list)):
            raise NodeRemoteRunError(
                "Remote Run metadata is invalid", code="metadata_invalid"
            )
        if name == "source-manifest.json":
            try:
                self._validate_source_manifest(value)
            except (TypeError, ValueError) as exc:
                raise NodeRemoteRunError(
                    "Source manifest is invalid", code="source_manifest"
                ) from exc
        try:
            encoded = (
                json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode()
        except (TypeError, ValueError) as exc:
            raise NodeRemoteRunError(
                "Remote Run metadata is invalid", code="metadata_invalid"
            ) from exc
        if len(encoded) > 8 * 1024 * 1024:
            raise NodeRemoteRunError(
                "Remote Run metadata is too large", code="metadata_invalid"
            )
        return encoded

    def _commit_metadata(self, run_id: str, name: str, content: bytes) -> None:
        name = self._metadata_name(name)
        try:
            decoded = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NodeRemoteRunError(
                "Remote Run metadata is invalid", code="metadata_invalid"
            ) from exc
        canonical = self._encode_metadata(name, decoded)
        if canonical != content:
            raise NodeRemoteRunError(
                "Remote Run metadata is not canonical", code="metadata_invalid"
            )
        with self.lock_for(run_id):
            paths = self._assert_layout(run_id)
            self._assert_not_started(run_id, paths)
            _atomic_private_write(paths["metadata"] / name, content)

    def _assert_not_started(
        self, run_id: str, paths: Mapping[str, Path]
    ) -> None:
        if any(
            paths[name].exists()
            for name in ("state", "prepare_result", "stop", "completion")
        ) or self._tmux_status(run_id)["exists"]:
            raise NodeRemoteRunError(
                "Remote Run Source cannot change after execution starts",
                code="run_already_started",
            )

    def _existing_start(
        self, run_id: str, paths: Mapping[str, Path]
    ) -> dict[str, Any] | None:
        lifecycle = any(
            paths[name].exists()
            for name in ("state", "prepare_result", "stop", "completion")
        )
        status = self._tmux_status(run_id)
        if not lifecycle and not status["exists"]:
            return None
        if status["exists"] and not self._session_owned(run_id):
            raise NodeRemoteRunError(
                "Remote Run session identity is already in use",
                code="session_identity_conflict",
            )
        observation = self._reconcile(run_id, dict(paths))
        return {
            **observation,
            "session_name": self._session_name(run_id),
            "run_root": str(paths["root"]),
            "replayed": True,
        }

    def _start_tmux(self, run_id: str, cwd: Path, script: Path) -> None:
        self._real_directory(cwd, "Remote Run working directory")
        session = self._session_name(run_id)
        if self._tmux("has-session", "-t", session, check=False).returncode == 0:
            raise NodeRemoteRunError("Remote Run session already exists", code="run_exists")
        self._tmux("new-session", "-d", "-s", session, "-c", str(cwd), "-n", "run")
        try:
            target = f"{session}:run"
            self._tmux(
                "set-option", "-t", session, "@termroom_remote_run_id", run_id
            )
            self._tmux("set-window-option", "-t", target, "remain-on-exit", "on")
            self._tmux(
                "set-window-option", "-t", target, "remain-on-exit-format", "", check=False
            )
            self._tmux("set-window-option", "-t", target, "window-size", "latest", check=False)
            self._tmux("set-window-option", "-t", target, TMUX_TERMINAL_ROLE_OPTION, "remote_run")
            self._tmux("set-window-option", "-t", target, TMUX_MANAGED_RUN_OPTION, run_id)
            self._tmux(
                "respawn-pane", "-k", "-t", f"{session}:run.0", "-c", str(cwd),
                "/bin/bash", "--noprofile", "--norc", str(script)
            )
        except BaseException:
            self._tmux("kill-session", "-t", session, check=False)
            raise

    def _reconcile(self, run_id: str, paths: dict[str, Path]) -> dict[str, Any]:
        tmux = self._tmux_status(run_id)
        completion_exists = paths["completion"].exists()
        completion = self._read_json_record(paths["completion"])
        prepare_exists = paths["prepare_result"].exists()
        prepare = self._read_json_record(paths["prepare_result"])
        state_exists = paths["state"].exists()
        state_record = self._read_json_record(paths["state"])
        completion_valid = self._valid_completion(completion)
        prepare_valid = self._valid_prepare(prepare)
        state_valid = self._valid_state(state_record)
        stop_requested = paths["stop"].exists()
        errors = [
            name
            for name, exists, valid in (
                ("completion.json", completion_exists, completion_valid),
                ("prepare-result.json", prepare_exists, prepare_valid),
                ("state.json", state_exists, state_valid),
            )
            if exists and not valid
        ]
        result: dict[str, Any] = {
            "state": "preparing",
            "phase": state_record.get("phase") if state_valid else None,
            "exit_code": None,
            "started_at": state_record.get("started_at") if state_valid else None,
            "ended_at": None,
            "stop_requested": stop_requested,
            "tmux_exists": tmux["exists"],
            "run_pane_exists": tmux["run_pane_exists"],
            "tmux_running": tmux["running"],
            "record_errors": errors,
        }
        live_phase = state_record.get("phase") if state_valid else None
        if not completion_valid and not prepare_valid and not tmux["running"] and live_phase:
            tmux = self._tmux_status(run_id)
            result.update(
                tmux_exists=tmux["exists"],
                run_pane_exists=tmux["run_pane_exists"],
                tmux_running=tmux["running"],
            )
            if not tmux["running"]:
                completion = self._read_json_record(paths["completion"])
                prepare = self._read_json_record(paths["prepare_result"])
                state_record = self._read_json_record(paths["state"])
                completion_valid = self._valid_completion(completion)
                prepare_valid = self._valid_prepare(prepare)
                state_valid = self._valid_state(state_record)
                stop_requested = paths["stop"].exists()
                result.update(
                    phase=state_record.get("phase") if state_valid else None,
                    started_at=state_record.get("started_at") if state_valid else None,
                    stop_requested=stop_requested,
                )
                result["record_errors"] = [
                    name
                    for name, path, valid in (
                        ("completion.json", paths["completion"], completion_valid),
                        ("prepare-result.json", paths["prepare_result"], prepare_valid),
                        ("state.json", paths["state"], state_valid),
                    )
                    if path.exists() and not valid
                ]
        if completion_valid:
            result.update(
                state="stopped" if completion["stop_requested"] else "finished",
                phase=None,
                exit_code=completion["exit_code"],
                started_at=completion["started_at"],
                ended_at=completion["ended_at"],
                stop_requested=completion["stop_requested"],
                log_incomplete=bool(completion.get("log_incomplete", False)),
            )
        elif prepare_valid:
            result.update(
                state=prepare["state"],
                phase=None,
                ended_at=prepare["ended_at"],
                error_code=prepare.get("error_code"),
            )
        elif tmux["running"]:
            if state_valid and state_record.get("phase") == "running":
                result["state"] = "running"
        elif stop_requested:
            result.update(state="stopped", phase=None)
        elif not result["record_errors"] and state_valid:
            if state_record.get("phase") == "running":
                result.update(state="lost", phase=None)
            elif state_record.get("phase") == "cloning":
                result.update(state="failed", phase=None, error_code="git_session_lost")
        revision = self._read_optional_line(paths["git_revision"])
        if (
            revision
            and len(revision) == 40
            and all(c in "0123456789abcdefABCDEF" for c in revision)
        ):
            result["source_revision"] = revision.lower()
        return result

    def _tmux_status(self, run_id: str) -> dict[str, Any]:
        session = self._session_name(run_id)
        if self._tmux("has-session", "-t", session, check=False).returncode:
            return {
                "exists": False,
                "run_pane_exists": False,
                "running": False,
                "pane_exit_code": None,
            }
        result = self._tmux(
            "list-panes", "-t", f"{session}:run.0", "-F", "#{pane_dead}|#{pane_dead_status}",
            check=False,
        )
        if result.returncode or not result.stdout.strip():
            return {
                "exists": True,
                "run_pane_exists": False,
                "running": False,
                "pane_exit_code": None,
            }
        dead, separator, exit_value = result.stdout.splitlines()[0].partition("|")
        if not separator or dead not in {"0", "1"}:
            raise NodeRemoteRunError("Node tmux status is invalid", code="tmux_invalid")
        return {
            "exists": True,
            "run_pane_exists": True,
            "running": dead == "0",
            "pane_exit_code": int(exit_value) if exit_value.lstrip("-").isdigit() else None,
        }

    def _owned_run_window(self, run_id: str) -> bool:
        if not self._session_owned(run_id):
            return False
        session = self._session_name(run_id)
        records = self._terminal_records(session, check=False)
        return any(
            item.get("name") == "run"
            and item.get("role") == "remote_run"
            and item.get("managed_run_id") == run_id
            for item in records
        )

    def _session_owned(self, run_id: str) -> bool:
        session = self._session_name(run_id)
        result = self._tmux(
            "show-options",
            "-v",
            "-t",
            session,
            "@termroom_remote_run_id",
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == run_id

    def _terminal_records(self, session: str, *, check: bool = True) -> list[dict[str, Any]]:
        result = self._tmux(
            "list-windows", "-t", session, "-F", TMUX_TERMINAL_RECORD_FORMAT, check=check
        )
        if result.returncode:
            return []
        return [dict(item) for item in parse_tmux_terminal_records(result.stdout)]

    @staticmethod
    def _read_json_record(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            info = path.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_size > 1024 * 1024
            ):
                return {}
            value = json.loads(path.read_text(encoding="utf-8"))
            return dict(value) if isinstance(value, dict) else {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _valid_completion(value: Mapping[str, Any]) -> bool:
        return (
            isinstance(value.get("exit_code"), int)
            and not isinstance(value.get("exit_code"), bool)
            and isinstance(value.get("stop_requested"), bool)
            and isinstance(value.get("started_at"), str)
            and bool(value.get("started_at"))
            and isinstance(value.get("ended_at"), str)
            and bool(value.get("ended_at"))
        )

    @staticmethod
    def _valid_prepare(value: Mapping[str, Any]) -> bool:
        return (
            value.get("state") in {"failed", "stopped"}
            and isinstance(value.get("ended_at"), str)
            and bool(value.get("ended_at"))
        )

    @staticmethod
    def _valid_state(value: Mapping[str, Any]) -> bool:
        return (
            value.get("phase") in {"cloning", "running"}
            and isinstance(value.get("started_at"), str)
            and bool(value.get("started_at"))
        )

    def _read_log(
        self,
        paths: Mapping[str, Path],
        *,
        stream: str,
        offset: int | None,
        limit: int,
    ) -> dict[str, Any]:
        if stream not in {"prepare", "command"}:
            raise NodeRemoteRunError("Remote Run log stream is invalid", code="log_invalid")
        if limit <= 0:
            raise NodeRemoteRunError("Remote Run log limit is invalid", code="log_invalid")
        limit = min(limit, REMOTE_RUN_LOG_READ_LIMIT)
        path = paths["prepare_log"] if stream == "prepare" else paths["output"]
        if not path.exists():
            return self._empty_log(stream, offset)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise NodeRemoteRunError("Remote Run log is invalid", code="log_invalid")
        size = info.st_size
        start = max(0, size - REMOTE_RUN_INITIAL_TAIL) if offset is None else offset
        if start < 0 or start > size:
            raise NodeRemoteRunError("Remote Run log offset is invalid", code="log_invalid")
        with path.open("rb") as handle:
            if offset is None and start:
                handle.seek(start)
                prefix = handle.read(4)
                skipped = 0
                while skipped < len(prefix) and prefix[skipped] & 0xC0 == 0x80:
                    skipped += 1
                start += skipped
            handle.seek(start)
            raw = handle.read(min(limit, size - start))
        next_offset = start + len(raw)
        return {
            "stream": stream,
            "chunk_b64": base64.b64encode(raw).decode("ascii"),
            "start_offset": start,
            "next_offset": next_offset,
            "size": size,
            "eof": next_offset >= size,
        }

    @staticmethod
    def _empty_log(stream: str, offset: int | None) -> dict[str, Any]:
        if stream not in {"prepare", "command"}:
            raise NodeRemoteRunError("Remote Run log stream is invalid", code="log_invalid")
        start = max(0, offset or 0)
        return {
            "stream": stream,
            "chunk_b64": "",
            "start_offset": start,
            "next_offset": start,
            "size": start,
            "eof": True,
        }

    def _unavailable_layout(
        self, run_id: str, *, missing: bool = False, error: str | None = None
    ) -> dict[str, Any]:
        status = self._tmux_status(run_id)
        return {
            "state": "layout_missing" if missing else "layout_error",
            "phase": None,
            "exit_code": None,
            "started_at": None,
            "ended_at": None,
            "stop_requested": False,
            "tmux_exists": status["exists"],
            "tmux_running": status["running"],
            "run_pane_exists": status["run_pane_exists"],
            "record_errors": [error] if error else [],
            "layout_missing": missing,
            "layout_error": error,
        }

    def _publish_stop(self, paths: Mapping[str, Path]) -> None:
        if paths["stop"].exists():
            return
        from datetime import UTC, datetime

        value = datetime.now(UTC).isoformat(timespec="seconds") + "\n"
        _atomic_private_write(paths["stop"], value.encode())

    def _finish_interrupted_delete(self, run_id: str) -> None:
        quarantine = self.run_root / f".termroom-deleting-{run_id}"
        if quarantine.exists() or quarantine.is_symlink():
            self._assert_marked_tree(quarantine, run_id)
            self._remove_owned_tree(quarantine, run_id)

    def _remove_staging(self, path: Path, run_id: str) -> None:
        expected = self._paths(run_id)["work_staging"]
        if path != expected or path.parent != self._paths(run_id)["root"]:
            raise NodeRemoteRunError("Source staging path is invalid", code="path_invalid")
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise NodeRemoteRunError("Source staging is invalid", code="path_invalid")
        shutil.rmtree(path)

    def _remove_owned_tree(
        self, path: Path, run_id: str, *, allow_missing_marker: bool = False
    ) -> None:
        allowed = {
            self.run_root / run_id,
            self.run_root / f".termroom-creating-{run_id}",
            self.run_root / f".termroom-deleting-{run_id}",
        }
        if path not in allowed or path.parent != self.run_root:
            raise NodeRemoteRunError("Remote Run cleanup path is invalid", code="cleanup_invalid")
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise NodeRemoteRunError("Remote Run cleanup root is invalid", code="cleanup_invalid")
        if not allow_missing_marker:
            self._assert_marked_tree(path, run_id)
        shutil.rmtree(path)

    def _kill_session(self, run_id: str) -> bool:
        session = self._session_name(run_id)
        exists = self._tmux("has-session", "-t", session, check=False).returncode == 0
        if exists and self._session_owned(run_id):
            self._tmux("kill-session", "-t", session, check=False)
            return True
        return False

    @staticmethod
    def _session_name(run_id: str) -> str:
        return REMOTE_RUN_SESSION_PREFIX + _validate_run_id(run_id)

    @staticmethod
    def _read_optional_line(path: Path) -> str | None:
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                return None
            value = path.read_text(encoding="utf-8").strip()
            return value or None
        except (OSError, UnicodeDecodeError):
            return None

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
            raise NodeRemoteRunError(
                result.stderr.strip() or "tmux operation failed", code="tmux_failed"
            )
        return result


class NodeRemoteRunSnapshotSink:
    def __init__(
        self,
        client: NodeRemoteRunClient,
        computer: Mapping[str, Any],
        run_base: str,
        run_id: str,
    ) -> None:
        self.client = client
        self.computer = computer
        self.run_base = run_base
        self.run_id = run_id

    def make_directory(self, relative_path: str, *, executable: bool) -> None:
        del executable
        self.client._request(
            self.computer,
            "remote_run.snapshot.mkdir",
            self.client._payload(self.run_base, self.run_id, path=relative_path),
        )

    def make_symlink(self, relative_path: str, link_target: str) -> None:
        self.client._request(
            self.computer,
            "remote_run.snapshot.symlink",
            self.client._payload(
                self.run_base, self.run_id, path=relative_path, link_target=link_target
            ),
        )

    def write_file(
        self,
        relative_path: str,
        chunks: Iterable[bytes],
        *,
        executable: bool,
        expected_size: int,
    ) -> None:
        stream: NodeStream | None = None
        try:
            _result, stream = self.client._open_stream(
                self.computer,
                "remote_run.snapshot.file.open",
                self.client._payload(
                    self.run_base,
                    self.run_id,
                    path=relative_path,
                    executable=executable,
                    expected_size=expected_size,
                ),
            )
            for chunk in chunks:
                self.client._submit(stream.send(chunk))
            result = self.client._submit(stream.finish())
            if int(result.get("size", -1)) != expected_size:
                raise NodeRemoteRunError(
                    "Node returned an invalid Source file size", code="source_file_changed"
                )
        except BaseException:
            if stream is not None:
                with contextlib.suppress(Exception):
                    self.client._submit(stream.abort())
            raise


class _WorkspaceSourceReceiveWindow:
    """Grant only the exact Node-to-Core Source frames that remain."""

    def __init__(
        self,
        client: NodeRemoteRunClient,
        stream: NodeStream,
        frame_count: int,
    ) -> None:
        self.client = client
        self.stream = stream
        self.frame_count = frame_count
        self.received = 0
        self._ungranted = frame_count
        self._batch_remaining = 0
        self._grant_next_batch()

    def _grant_next_batch(self) -> None:
        count = min(NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW, self._ungranted)
        if count == 0:
            return
        self.client._submit(self.stream.control("credit", count=count))
        self._ungranted -= count
        self._batch_remaining = count

    def receive(self) -> bytes | None:
        chunk = self.client._submit(self.stream.receive())
        if chunk is None:
            if self.received != self.frame_count:
                raise NodeRemoteRunError(
                    "Node Remote Run Source stream ended at the wrong frame",
                    code="source_stream_invalid",
                )
            return None
        if self._batch_remaining == 0 or self.received >= self.frame_count:
            raise NodeRemoteRunError(
                "Node Remote Run Source stream exceeded its declared frames",
                code="source_stream_invalid",
            )
        self.received += 1
        self._batch_remaining -= 1
        if self._batch_remaining == 0:
            self._grant_next_batch()
        return chunk


class NodeWorkspaceSnapshotSource:
    """A bounded Node-to-Core ``SnapshotSource`` for one persistent Workspace."""

    def __init__(
        self,
        client: NodeRemoteRunClient,
        workspace: Mapping[str, Any],
        source_path: str,
        *,
        explicitly_included: Iterable[str],
    ) -> None:
        computer = workspace.get("computer")
        if not isinstance(computer, Mapping) or computer.get("connection_method") != "node":
            raise NodeRemoteRunError(
                "Node Workspace Source computer is invalid", code="workspace_required"
            )
        if workspace.get("transient") or workspace.get("remote_run_id"):
            raise NodeRemoteRunError(
                "A transient Remote Run Workspace cannot be a Source",
                code="source_workspace_transient",
            )
        self.client = client
        self.computer = computer
        self.payload = {
            "remote_run_source_version": NODE_REMOTE_RUN_SOURCE_VERSION,
            "workspace_id": str(workspace.get("id") or ""),
            "workspace_path": str(
                workspace.get("canonical_path") or workspace.get("path") or ""
            ),
            "source_path": normalize_source_relative_path(
                source_path or ".", allow_root=True
            ),
            "explicitly_included": sorted(
                normalize_explicit_include_paths(explicitly_included)
            ),
        }

    @staticmethod
    def _entry(value: object) -> WorkspaceEntry:
        if not isinstance(value, dict):
            raise SourceValidationError(
                "Node Workspace manifest entry is invalid", code="source_manifest"
            )
        try:
            size = int(value.get("size"))
            mtime_ns = int(value.get("mtime_ns"))
        except (TypeError, ValueError) as exc:
            raise SourceValidationError(
                "Node Workspace manifest entry is invalid", code="source_manifest"
            ) from exc
        return WorkspaceEntry(
            relative_path=str(value.get("path") or ""),
            kind=str(value.get("kind") or ""),  # type: ignore[arg-type]
            size=size,
            mtime_ns=mtime_ns,
            executable=value.get("executable") is True,
            link_target=(
                str(value["link_target"])
                if value.get("link_target") is not None
                else None
            ),
        )

    @staticmethod
    def _result_integer(result: Mapping[str, Any], name: str) -> int:
        value = result.get(name)
        if isinstance(value, bool):
            raise SourceValidationError(
                "Node Workspace manifest metadata is invalid", code="source_manifest"
            )
        try:
            normalized = int(value)
        except (TypeError, ValueError) as exc:
            raise SourceValidationError(
                "Node Workspace manifest metadata is invalid", code="source_manifest"
            ) from exc
        if normalized < 0:
            raise SourceValidationError(
                "Node Workspace manifest metadata is invalid", code="source_manifest"
            )
        return normalized

    @staticmethod
    def _require_stream_contract(result: Mapping[str, Any]) -> None:
        if (
            type(result.get("remote_run_source_version")) is not int
            or result["remote_run_source_version"] != NODE_REMOTE_RUN_SOURCE_VERSION
            or type(result.get("stream_window")) is not int
            or result["stream_window"] != NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW
        ):
            raise NodeRemoteRunError(
                "Node Remote Run Source stream is incompatible; update Termroom",
                code="remote_run_source_version_incompatible",
            )

    @staticmethod
    def _stream_frame_count(result: Mapping[str, Any]) -> int:
        frame_count = result.get("frame_count")
        if type(frame_count) is not int or frame_count < 0:
            raise NodeRemoteRunError(
                "Node Remote Run Source frame count is invalid",
                code="source_stream_invalid",
            )
        return frame_count

    def _receive_window(
        self, stream: NodeStream, frame_count: int
    ) -> _WorkspaceSourceReceiveWindow:
        return _WorkspaceSourceReceiveWindow(
            self.client,
            stream,
            frame_count,
        )

    def scan(self) -> WorkspaceManifest:
        stream: NodeStream | None = None
        try:
            result, stream = self.client._open_stream(
                self.computer,
                "remote_run_source.manifest.open",
                self.payload,
            )
            self._require_stream_contract(result)
            frame_count = self._stream_frame_count(result)
            expected_count = self._result_integer(result, "entry_count")
            expected_total = self._result_integer(result, "total_bytes")
            receiver = self._receive_window(stream, frame_count)
            entries: list[WorkspaceEntry] = []
            buffered = bytearray()
            while True:
                chunk = receiver.receive()
                if chunk is None:
                    break
                buffered.extend(chunk)
                while True:
                    newline = buffered.find(b"\n")
                    if newline < 0:
                        break
                    line = bytes(buffered[:newline])
                    del buffered[: newline + 1]
                    if not line:
                        raise SourceValidationError(
                            "Node Workspace manifest stream is invalid",
                            code="source_manifest",
                        )
                    try:
                        value = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise SourceValidationError(
                            "Node Workspace manifest stream is invalid",
                            code="source_manifest",
                        ) from exc
                    entries.append(self._entry(value))
                if len(buffered) > MAX_NODE_MESSAGE_BYTES:
                    raise SourceValidationError(
                        "Node Workspace manifest entry is too large",
                        code="source_manifest",
                    )
            if buffered:
                raise SourceValidationError(
                    "Node Workspace manifest stream ended mid-entry",
                    code="source_manifest",
                )
            manifest = build_workspace_manifest(entries)
            if (
                len(manifest.entries) != expected_count
                or manifest.total_bytes != expected_total
            ):
                raise SourceValidationError(
                    "Node Workspace manifest metadata does not match its entries",
                    code="source_manifest_total",
                )
            return manifest
        finally:
            if stream is not None:
                with contextlib.suppress(Exception):
                    self.client._submit(stream.abort())

    def _current_entry(self, relative_path: str) -> WorkspaceEntry | None:
        try:
            result = self.client._request(
                self.computer,
                "remote_run_source.stat",
                {**self.payload, "path": relative_path},
            )
            if (
                result.get("remote_run_source_version")
                != NODE_REMOTE_RUN_SOURCE_VERSION
            ):
                return None
            entry = self._entry(result.get("entry"))
            return entry if entry.kind == "file" else None
        except (NodeRemoteRunError, SourceValidationError):
            return None

    def _changed(self, relative_path: str, cause: BaseException | None = None) -> None:
        current = self._current_entry(relative_path)
        error = SourceFileChangedError(
            relative_path,
            current_size=current.size if current is not None else None,
            current_mtime_ns=current.mtime_ns if current is not None else None,
        )
        if cause is None:
            raise error
        raise error from cause

    def iter_file_chunks(
        self,
        entry: WorkspaceEntry,
        *,
        chunk_size: int,
    ) -> Iterator[bytes]:
        if entry.kind != "file":
            raise SourceValidationError(
                "Only regular manifest files can be read",
                code="source_entry_type",
                path=entry.relative_path,
            )
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        stream: NodeStream | None = None
        total = 0
        try:
            result, stream = self.client._open_stream(
                self.computer,
                "remote_run_source.file.open",
                {
                    **self.payload,
                    "path": entry.relative_path,
                    "expected_size": entry.size,
                    "expected_mtime_ns": entry.mtime_ns,
                    "executable": entry.executable,
                },
            )
            self._require_stream_contract(result)
            frame_count = self._stream_frame_count(result)
            expected_frames = (
                entry.size + MAX_NODE_STREAM_CHUNK_BYTES - 1
            ) // MAX_NODE_STREAM_CHUNK_BYTES
            if frame_count != expected_frames:
                raise NodeRemoteRunError(
                    "Node Remote Run Source file stream is invalid",
                    code="source_stream_invalid",
                )
            if (
                self._result_integer(result, "size") != entry.size
                or self._result_integer(result, "mtime_ns") != entry.mtime_ns
            ):
                self._changed(entry.relative_path)
            receiver = self._receive_window(stream, frame_count)
            while True:
                chunk = receiver.receive()
                if chunk is None:
                    break
                total += len(chunk)
                if total > entry.size:
                    self._changed(entry.relative_path)
                for offset in range(0, len(chunk), chunk_size):
                    value = chunk[offset : offset + chunk_size]
                    if value:
                        yield value
            if total != entry.size:
                self._changed(entry.relative_path)
        except NodeRemoteRunError as exc:
            if exc.code == "source_file_changed":
                self._changed(entry.relative_path, exc)
            raise
        finally:
            if stream is not None:
                with contextlib.suppress(Exception):
                    self.client._submit(stream.abort())


class NodeRemoteRunClient:
    """Synchronous Core-side facade used only from RemoteRunManager worker threads."""

    def __init__(self, nodes: NodeCore) -> None:
        self.nodes = nodes
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def supports_remote_run_source(self, computer: Mapping[str, Any]) -> bool:
        if computer.get("connection_method") != "node":
            return False
        try:
            status = self.nodes.status(str(computer["id"]))
        except (KeyError, NodeCoreError):
            return False
        return (
            computer.get("node_revoked_at") is None
            and "remote_run_source" in status.capabilities
        )

    @contextlib.contextmanager
    def remote_workspace_snapshot_source(
        self,
        workspace: Mapping[str, Any],
        relative_path: str = ".",
        *,
        explicitly_included: Iterable[str] = (),
    ) -> Iterator[NodeWorkspaceSnapshotSource]:
        computer = workspace.get("computer")
        if not isinstance(computer, Mapping) or not self.supports_remote_run_source(
            computer
        ):
            raise NodeRemoteRunError(
                "This Remote does not support Workspace Sources",
                code="capability_unsupported",
            )
        yield NodeWorkspaceSnapshotSource(
            self,
            workspace,
            relative_path,
            explicitly_included=explicitly_included,
        )

    def _submit(self, awaitable: Any, *, timeout: float = 45.0) -> Any:
        loop = self._loop
        if loop is None or not loop.is_running():
            close = getattr(awaitable, "close", None)
            if close:
                close()
            raise NodeRemoteRunError("Node control loop is unavailable", code="node_offline")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            close = getattr(awaitable, "close", None)
            if close:
                close()
            raise RuntimeError("Node Remote Run client must run outside the Core event loop")
        future = asyncio.run_coroutine_threadsafe(awaitable, loop)
        try:
            return future.result(timeout=timeout)
        except NodeCoreError as exc:
            raise NodeRemoteRunError(str(exc), code=exc.code) from exc
        except TimeoutError as exc:
            future.cancel()
            raise NodeRemoteRunError("Node did not answer in time", code="node_offline") from exc

    def _request(
        self,
        computer: Mapping[str, Any],
        operation: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            connection = self.nodes.connection(str(computer["id"]))
        except (KeyError, NodeCoreError) as exc:
            code = getattr(exc, "code", "node_offline")
            raise NodeRemoteRunError(str(exc) or "Node is offline", code=code) from exc
        result = self._submit(connection.request(operation, payload))
        if not isinstance(result, dict):
            raise NodeRemoteRunError("Node returned an invalid response", code="response_invalid")
        return result

    def _open_stream(
        self,
        computer: Mapping[str, Any],
        operation: str,
        payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], NodeStream]:
        try:
            connection = self.nodes.connection(str(computer["id"]))
        except (KeyError, NodeCoreError) as exc:
            code = getattr(exc, "code", "node_offline")
            raise NodeRemoteRunError(str(exc) or "Node is offline", code=code) from exc
        return self._submit(connection.open_stream(operation, payload))

    @staticmethod
    def _payload(run_base: str, run_id: str, **values: Any) -> dict[str, Any]:
        return {
            "remote_run_version": NODE_REMOTE_RUN_VERSION,
            "run_base": run_base,
            "run_id": _validate_run_id(run_id),
            **values,
        }

    def preflight_remote_run_target(
        self,
        computer: Mapping[str, Any],
        *,
        run_base_dir: str | None = None,
        require_git: bool = False,
    ) -> dict[str, Any]:
        del run_base_dir
        return self._request(
            computer,
            "remote_run.preflight",
            {
                "remote_run_version": NODE_REMOTE_RUN_VERSION,
                "require_git": require_git,
            },
        )

    def create_remote_run_layout(
        self,
        computer: Mapping[str, Any],
        run_id: str,
        *,
        run_base_dir: str | None = None,
        command: str | None = None,
        cwd_rel: str = ".",
    ) -> dict[str, str]:
        if not run_base_dir:
            raise NodeRemoteRunError("Node Remote Run root is missing", code="run_root_invalid")
        result = self._request(
            computer,
            "remote_run.create",
            self._payload(run_base_dir, run_id, command=command, cwd_rel=cwd_rel),
        )
        return {key: str(value) for key, value in result.items() if isinstance(value, str)}

    @contextlib.contextmanager
    def remote_run_snapshot_sink(
        self,
        computer: Mapping[str, Any],
        run_base: str,
        run_id: str,
        *,
        reset_staging: bool = True,
    ):
        if reset_staging:
            self._request(
                computer,
                "remote_run.snapshot.begin",
                self._payload(run_base, run_id),
            )
        yield NodeRemoteRunSnapshotSink(self, computer, run_base, run_id)

    def commit_remote_run_snapshot(
        self, computer: Mapping[str, Any], run_base: str, run_id: str
    ) -> str:
        result = self._request(
            computer,
            "remote_run.snapshot.commit",
            self._payload(run_base, run_id),
        )
        return str(result.get("work_path") or "")

    def write_remote_run_json(
        self,
        computer: Mapping[str, Any],
        run_base: str,
        run_id: str,
        name: str,
        value: dict[str, Any] | list[Any],
    ) -> str:
        encoded = (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
        if len(encoded) <= 512 * 1024:
            self._request(
                computer,
                "remote_run.metadata.write",
                self._payload(run_base, run_id, name=name, value=value),
            )
        else:
            stream: NodeStream | None = None
            try:
                _result, stream = self._open_stream(
                    computer,
                    "remote_run.metadata.open",
                    self._payload(
                        run_base,
                        run_id,
                        name=name,
                        expected_size=len(encoded),
                    ),
                )
                self._submit(stream.send(encoded))
                result = self._submit(stream.finish())
                if int(result.get("size", -1)) != len(encoded):
                    raise NodeRemoteRunError(
                        "Node returned an invalid metadata size",
                        code="metadata_invalid",
                    )
            except BaseException:
                if stream is not None:
                    with contextlib.suppress(Exception):
                        self._submit(stream.abort())
                raise
        return f"{run_base.rstrip('/')}/{run_id}/.termroom/{name}"

    def start_remote_run(
        self, computer: Mapping[str, Any], run_base: str, run_id: str
    ) -> dict[str, Any]:
        return self._request(
            computer, "remote_run.start", self._payload(run_base, run_id)
        )

    def start_remote_git_run(
        self,
        computer: Mapping[str, Any],
        run_base: str,
        run_id: str,
        invocation: Any,
    ) -> dict[str, Any]:
        argv = tuple(getattr(invocation, "argv", ()))
        if len(argv) < 2:
            raise NodeRemoteRunError("Git Source invocation is invalid", code="git_url_invalid")
        url = validate_public_https_git_url(str(argv[-2]))
        return self._request(
            computer,
            "remote_run.git.start",
            self._payload(run_base, run_id, url=url),
        )

    def remote_run_git_clone_parameters(
        self, computer: Mapping[str, Any], run_base: str, run_id: str
    ) -> dict[str, str]:
        preflight = self.preflight_remote_run_target(computer, require_git=True)
        root = f"{run_base.rstrip('/')}/{run_id}"
        metadata = f"{root}/.termroom"
        return {
            "git_path": str(preflight["tools"]["git"]),
            "askpass_path": f"{metadata}/git-askpass",
            "empty_home": f"{metadata}/git-home",
            "destination": f"{root}/work.tmp",
        }

    def reconcile_remote_run(
        self, computer: Mapping[str, Any], run_base: str, run_id: str
    ) -> dict[str, Any]:
        return self._request(
            computer, "remote_run.observe", self._payload(run_base, run_id)
        )

    def poll_remote_run(
        self,
        computer: Mapping[str, Any],
        run_base: str,
        run_id: str,
        *,
        stream: str = "command",
        offset: int | None = None,
        limit: int = REMOTE_RUN_LOG_READ_LIMIT,
    ) -> dict[str, Any]:
        return self._request(
            computer,
            "remote_run.poll",
            self._payload(
                run_base, run_id, stream=stream, offset=offset, limit=limit
            ),
        )

    def read_remote_run_log(
        self,
        computer: Mapping[str, Any],
        run_base: str,
        run_id: str,
        *,
        stream: str = "command",
        offset: int | None = None,
        limit: int = REMOTE_RUN_LOG_READ_LIMIT,
    ) -> dict[str, Any]:
        result = self.poll_remote_run(
            computer,
            run_base,
            run_id,
            stream=stream,
            offset=offset,
            limit=limit,
        )
        log = result.get("log")
        if not isinstance(log, dict):
            raise NodeRemoteRunError("Node returned an invalid Remote Run log", code="log_invalid")
        return dict(log)

    def interrupt_remote_run(
        self, computer: Mapping[str, Any], run_base: str, run_id: str
    ) -> dict[str, Any]:
        return self._request(
            computer, "remote_run.interrupt", self._payload(run_base, run_id)
        )

    def kill_remote_run(
        self, computer: Mapping[str, Any], run_base: str, run_id: str
    ) -> dict[str, Any]:
        return self._request(
            computer, "remote_run.kill", self._payload(run_base, run_id)
        )

    def remote_run_layout_exists(
        self, computer: Mapping[str, Any], run_base: str, run_id: str
    ) -> bool:
        result = self._request(
            computer, "remote_run.exists", self._payload(run_base, run_id)
        )
        return result.get("exists") is True

    def ensure_remote_run_workspace_shell(
        self,
        computer: Mapping[str, Any],
        run_base: str,
        run_id: str,
        *,
        allow_create_session: bool = False,
    ) -> dict[str, Any]:
        return self._request(
            computer,
            "remote_run.ensure_shell",
            self._payload(
                run_base, run_id, allow_create_session=allow_create_session
            ),
        )

    def delete_remote_run_root(
        self, computer: Mapping[str, Any], run_base: str, run_id: str
    ) -> dict[str, Any]:
        return self._request(
            computer, "remote_run.delete", self._payload(run_base, run_id)
        )
