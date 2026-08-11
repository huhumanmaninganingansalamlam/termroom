from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from termroom.archive_safety import (
    ArchiveLimits,
    ArchiveSafetyError,
    ZipManifest,
    materialize_zip_archive,
    validate_zip_archive,
    validate_zip_filename,
)
from termroom.db import StateStore, utc_now
from termroom.files import FileEntry, FileService, TextPreview
from termroom.node_remote_runs import NodeRemoteRunClient, NodeRemoteRunError
from termroom.run_sources import (
    LocalWorkspaceSnapshotSource,
    WorkspaceManifest,
    build_public_git_clone_invocation,
    materialize_workspace_snapshot,
    normalize_explicit_include_paths,
    normalize_source_relative_path,
    validate_public_https_git_url,
)
from termroom.security import resolve_inside
from termroom.ssh_backend import (
    RemoteRunLayoutError,
    SSHBackend,
    SSHBackendError,
    SSHCommandStatusUnknown,
)
from termroom.workspaces import WorkspaceManager

REMOTE_RUN_RETENTION = timedelta(hours=24)
WAITING_UPLOAD_TTL = timedelta(hours=1)
MAX_REMOTE_RUN_COMMAND_BYTES = 256 * 1024
TERMINAL_STATES = frozenset({"finished", "stopped", "failed", "lost"})
NONTERMINAL_STATES = frozenset({"preparing", "running"})
REMOTE_RUN_OBSERVER_INTERVAL = 2.0
REMOTE_RUN_OBSERVER_MAX_BACKOFF = 30.0


class RemoteRunError(RuntimeError):
    def __init__(self, message: str, *, code: str = "remote_run_error") -> None:
        super().__init__(message)
        self.code = code


class RemoteRunConflict(RemoteRunError):
    pass


@dataclass(slots=True)
class _CancellableSink:
    sink: Any
    cancel: threading.Event

    def _check(self) -> None:
        if self.cancel.is_set():
            raise RemoteRunError("Remote Run preparation was cancelled", code="cancelled")

    def make_directory(self, *args: Any, **kwargs: Any) -> Any:
        self._check()
        return self.sink.make_directory(*args, **kwargs)

    def make_symlink(self, *args: Any, **kwargs: Any) -> Any:
        self._check()
        return self.sink.make_symlink(*args, **kwargs)

    def write_file(self, path: str, chunks: Any, **kwargs: Any) -> Any:
        self._check()

        def checked_chunks() -> Iterator[bytes]:
            for chunk in chunks:
                self._check()
                yield chunk
            self._check()

        return self.sink.write_file(path, checked_chunks(), **kwargs)


@dataclass(slots=True)
class _PreparationHandle:
    task: asyncio.Task[None]
    cancel: threading.Event


@dataclass(slots=True)
class _ScannedSource:
    source: Any
    manifest: WorkspaceManifest

    def scan(self) -> WorkspaceManifest:
        return self.manifest

    def iter_file_chunks(self, entry: Any, *, chunk_size: int) -> Iterator[bytes]:
        return self.source.iter_file_chunks(entry, chunk_size=chunk_size)


def validate_remote_run_id(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Remote Run id must be a canonical UUIDv4")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError("Remote Run id must be a canonical UUIDv4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("Remote Run id must be a canonical lowercase UUIDv4")
    return value


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _terminal_expiry(now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    return (current + REMOTE_RUN_RETENTION).isoformat(timespec="seconds")


def _terminal_expiry_from(ended_at: str) -> str:
    try:
        ended = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
    except ValueError:
        return _terminal_expiry()
    if ended.tzinfo is None:
        return _terminal_expiry()
    return _terminal_expiry(ended.astimezone(UTC))


def _waiting_upload_expiry(now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    return (current + WAITING_UPLOAD_TTL).isoformat(timespec="seconds")


class RemoteRunManager:
    """One Remote Run lifecycle with explicit SSH or Node target dispatch."""

    def __init__(
        self,
        store: StateStore,
        workspaces: WorkspaceManager,
        ssh: SSHBackend,
        node_runs: NodeRemoteRunClient | None = None,
        *,
        state_dir: Path,
        max_archive_bytes: int,
    ) -> None:
        self.store = store
        self.workspaces = workspaces
        self.ssh = ssh
        self.node_runs = node_runs
        self.max_archive_bytes = max_archive_bytes
        self.archive_limits = ArchiveLimits(max_upload_bytes=max_archive_bytes)
        self.spool_root = (state_dir / "remote-run-spool").resolve()
        self.spool_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.spool_root.chmod(0o700)
        self.files = FileService()
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._preparations: dict[str, _PreparationHandle] = {}
        self._startup_reconcile_task: asyncio.Task[None] | None = None
        self._observer_task: asyncio.Task[None] | None = None
        self._observer_wakeup = asyncio.Event()
        self._cleanup_task: asyncio.Task[int] | None = None

    def lock_for(self, run_id: str) -> threading.RLock:
        safe_id = validate_remote_run_id(run_id)
        with self._locks_guard:
            return self._locks.setdefault(safe_id, threading.RLock())

    def get(self, run_id: str) -> dict[str, Any]:
        safe_id = validate_remote_run_id(run_id)
        row = self.store.get_remote_run(safe_id)
        if not row:
            raise KeyError(f"Unknown Remote Run: {safe_id}")
        row["target"] = self.store.get_computer(str(row["target_computer_id"]))
        if not row["target"]:
            raise RemoteRunError("Remote Run target registration is missing", code="target_missing")
        return row

    def list_recent(self, limit: int = 8) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in self.store.list_recent_remote_runs(limit):
            try:
                result.append(self.get(str(row["id"])))
            except (KeyError, RemoteRunError, ValueError):
                continue
        return result

    async def create(self, payload: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        self._bind_node_loop()
        normalized = self._normalize_create_payload(payload)
        existing = self.store.get_remote_run(str(normalized["id"]))
        if existing:
            self._assert_idempotent(existing, normalized)
            return self.get(str(existing["id"])), False

        computer = self._require_computer(str(normalized["target_computer_id"]))
        backend = self._target_backend(computer)
        preflight = await asyncio.to_thread(
            backend.preflight_remote_run_target,
            computer,
            run_base_dir=str(computer.get("run_base_dir") or "") or None,
            require_git=normalized["source_kind"] == "git",
        )
        phase = "waiting_upload" if normalized["source_kind"] == "archive" else (
            "cloning" if normalized["source_kind"] == "git" else "scanning"
        )
        values = {
            **normalized,
            "run_base": str(preflight["run_base"]),
            "state": "preparing",
            "phase": phase,
            "created_at": utc_now(),
            "expires_at": (
                _waiting_upload_expiry()
                if normalized["source_kind"] == "archive"
                else None
            ),
        }
        row, created = self.store.create_remote_run(values)
        if not created:
            self._assert_idempotent(row, normalized)
            return self.get(str(row["id"])), False
        if normalized["source_kind"] != "archive":
            self.schedule(str(row["id"]))
        return self.get(str(row["id"])), True

    def _normalize_create_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = validate_remote_run_id(str(payload.get("id", "")))
        requested_source_kind = str(payload.get("source_kind", "")).strip()
        source_kind = "archive" if requested_source_kind == "zip" else requested_source_kind
        if source_kind not in {"workspace", "git", "archive"}:
            raise RemoteRunError(
                "Remote Run Source must be Workspace, Git, or Archive",
                code="source_kind",
            )
        archive_format: str | None = None
        if source_kind == "archive":
            archive_format = str(payload.get("archive_format") or "zip").strip().lower()
            if archive_format != "zip":
                raise RemoteRunError(
                    "Only ZIP Archives are supported",
                    code="archive_format",
                )
        target_id = str(payload.get("target_computer_id", "")).strip()
        self._require_computer(target_id)
        command = str(payload.get("command", "")).rstrip()
        if not command.strip():
            raise RemoteRunError(
                "Remote Run command cannot be empty",
                code="command_required",
            )
        if len(command.encode("utf-8")) > MAX_REMOTE_RUN_COMMAND_BYTES:
            raise RemoteRunError(
                "Remote Run command is too large",
                code="command_required",
            )

        options_value = payload.get("source_options") or {}
        if not isinstance(options_value, Mapping):
            raise RemoteRunError(
                "Remote Run Source options must be an object",
                code="source_options",
            )
        explicit_values = options_value.get("explicitly_included") or []
        if not isinstance(explicit_values, list):
            raise RemoteRunError(
                "Explicit Source paths must be a list",
                code="source_options",
            )
        explicit_paths = sorted(normalize_explicit_include_paths(map(str, explicit_values)))
        source_options = {"policy": 1, "explicitly_included": explicit_paths}

        workspace_id: str | None = None
        source_path: str | None = None
        source_url: str | None = None
        source_label: str
        if source_kind == "workspace":
            workspace_id = str(payload.get("source_workspace_id", "")).strip()
            if not workspace_id:
                raise RemoteRunError(
                    "Choose a Workspace Source",
                    code="workspace_required",
                )
            try:
                workspace = self.workspaces.require(workspace_id)
                if workspace.get("transient") or workspace.get("is_remote_run"):
                    raise RemoteRunError(
                        "A transient Remote Run Workspace cannot be a Source",
                        code="workspace_required",
                    )
                source_computer = workspace.get("computer") or {}
                source_method = source_computer.get("connection_method")
                if workspace.get("backend_kind") == "remote" and source_method == "node":
                    if (
                        self.node_runs is None
                        or not self.node_runs.supports_remote_run_source(source_computer)
                    ):
                        raise RemoteRunError(
                            "This Remote does not support Workspace Sources",
                            code="capability_unsupported",
                        )
                elif workspace.get("backend_kind") == "remote" and source_method != "ssh":
                    raise RemoteRunError(
                        "This Workspace connection method is not supported yet",
                        code="workspace_required",
                    )
                source_path = normalize_source_relative_path(
                    str(payload.get("source_path", ".")) or ".", allow_root=True
                )
            except (KeyError, ValueError) as exc:
                raise RemoteRunError(
                    "Choose a valid Workspace Source",
                    code="workspace_required",
                ) from exc
            source_label = str(workspace["display_name"])
            if source_path != ".":
                source_label = f"{source_label}/{source_path}"
        elif source_kind == "git":
            source_url = validate_public_https_git_url(str(payload.get("source_url", "")))
            parsed = urlsplit(source_url)
            source_label = f"{parsed.hostname}{parsed.path}".removesuffix(".git")
        else:
            archive_name = validate_zip_filename(str(payload.get("archive_name", "")))
            source_label = Path(archive_name).name
            source_options["archive_name"] = source_label

        return {
            "id": run_id,
            "source_kind": source_kind,
            "archive_format": archive_format,
            "source_workspace_id": workspace_id,
            "source_path": source_path,
            "source_label": source_label,
            "source_url": source_url,
            "source_options_json": _canonical_json(source_options),
            "source_revision": None,
            "source_size": None,
            "target_computer_id": target_id,
            "command": command,
        }

    @staticmethod
    def _assert_idempotent(existing: Mapping[str, Any], requested: Mapping[str, Any]) -> None:
        immutable = (
            "source_kind",
            "archive_format",
            "source_workspace_id",
            "source_path",
            "source_label",
            "source_url",
            "source_options_json",
            "target_computer_id",
            "command",
        )
        if any(existing.get(key) != requested.get(key) for key in immutable):
            raise RemoteRunConflict(
                "Remote Run id was already used with different settings",
                code="idempotency_conflict",
            )

    def schedule(self, run_id: str) -> bool:
        self._bind_node_loop()
        safe_id = validate_remote_run_id(run_id)
        current = self._preparations.get(safe_id)
        if current and not current.task.done():
            return False
        cancel = threading.Event()
        task = asyncio.create_task(self._prepare(safe_id, cancel))
        self._preparations[safe_id] = _PreparationHandle(task=task, cancel=cancel)
        task.add_done_callback(lambda completed, value=safe_id: self._task_done(value, completed))
        self._observer_wakeup.set()
        return True

    def _task_done(self, run_id: str, task: asyncio.Task[None]) -> None:
        current = self._preparations.get(run_id)
        if current and current.task is task:
            self._preparations.pop(run_id, None)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()

    async def _prepare(self, run_id: str, cancel: threading.Event) -> None:
        try:
            await asyncio.to_thread(self._prepare_sync, run_id, cancel)
        except Exception as exc:
            await asyncio.to_thread(self._record_preparation_failure, run_id, exc, cancel)

    def _prepare_sync(self, run_id: str, cancel: threading.Event) -> None:
        run = self.get(run_id)
        if run["state"] != "preparing":
            return
        target = self._require_run_target(run)
        if run["source_kind"] == "git":
            self._prepare_git(run, target, cancel)
        elif run["source_kind"] == "workspace":
            self._prepare_workspace(run, target, cancel)
        else:
            self._prepare_zip(run, target, cancel)

    def _prepare_workspace(
        self,
        run: dict[str, Any],
        target: dict[str, Any],
        cancel: threading.Event,
    ) -> None:
        self._check_cancel(cancel)
        workspace = self.workspaces.require(str(run["source_workspace_id"]))
        source_path = str(run.get("source_path") or ".")
        options = self._source_options(run)
        includes = tuple(map(str, options.get("explicitly_included") or ()))
        backend = self._target_backend(target)
        layout = backend.create_remote_run_layout(
            target,
            str(run["id"]),
            run_base_dir=str(run["run_base"]),
            command=str(run["command"]),
            cwd_rel=".",
        )

        if (
            workspace.get("backend_kind") == "remote"
            and (workspace.get("computer") or {}).get("connection_method") == "node"
        ):
            if self.node_runs is None:
                raise RemoteRunError(
                    "Node Workspace Sources are unavailable",
                    code="capability_unsupported",
                )
            with self.node_runs.remote_workspace_snapshot_source(
                workspace,
                source_path,
                explicitly_included=includes,
            ) as source:
                self._materialize_workspace(run, target, source, cancel)
        elif workspace.get("backend_kind") == "remote":
            exclusions = self._same_target_exclusions(workspace, source_path, run)
            with self.ssh.remote_workspace_snapshot_source(
                workspace,
                source_path,
                exclusions=exclusions,
                explicitly_included=includes,
            ) as source:
                self._materialize_workspace(run, target, source, cancel)
        else:
            root = Path(workspace["path"]).resolve(strict=True)
            source_root = root if source_path == "." else resolve_inside(root, source_path)
            source = LocalWorkspaceSnapshotSource(
                source_root,
                mandatory_excludes=(self.ssh.state_dir,),
                explicitly_included=includes,
            )
            self._materialize_workspace(run, target, source, cancel)

        self._check_cancel(cancel)
        backend.commit_remote_run_snapshot(target, str(run["run_base"]), str(run["id"]))
        self._write_source_metadata(
            run,
            target,
            cwd_rel=".",
            extra={"workspace_id": run["source_workspace_id"], "path": source_path},
        )
        self._start_materialized_run(run, target, layout, cancel)

    def _materialize_workspace(
        self,
        run: dict[str, Any],
        target: dict[str, Any],
        source: Any,
        cancel: threading.Event,
    ) -> WorkspaceManifest:
        self._check_cancel(cancel)
        manifest = source.scan()
        self._ensure_target_capacity(target, run, manifest.total_bytes)
        if not self.store.transition_remote_run(
            str(run["id"]),
            expected_states={"preparing"},
            state="preparing",
            phase="copying",
            source_size=manifest.total_bytes,
        ):
            self._check_cancel(cancel)
            raise RemoteRunError("Remote Run preparation was superseded", code="state_conflict")
        self._check_cancel(cancel)
        backend = self._target_backend(target)
        with backend.remote_run_snapshot_sink(
            target,
            str(run["run_base"]),
            str(run["id"]),
        ) as sink:
            manifest = materialize_workspace_snapshot(
                _ScannedSource(source, manifest), _CancellableSink(sink, cancel)
            )
        self._write_manifest(run, target, manifest)
        return manifest

    def _prepare_git(
        self,
        run: dict[str, Any],
        target: dict[str, Any],
        cancel: threading.Event,
    ) -> None:
        self._check_cancel(cancel)
        backend = self._target_backend(target)
        preflight = backend.preflight_remote_run_target(
            target,
            run_base_dir=str(run["run_base"]),
            require_git=True,
        )
        layout = backend.create_remote_run_layout(
            target,
            str(run["id"]),
            run_base_dir=str(run["run_base"]),
            command=str(run["command"]),
            cwd_rel=".",
        )
        if self._is_node_target(target):
            parameters = backend.remote_run_git_clone_parameters(
                target, str(run["run_base"]), str(run["id"])
            )
        else:
            metadata = str(layout["metadata"])
            parameters = {
                "git_path": str(preflight["tools"]["git"]),
                "askpass_path": f"{metadata}/git-askpass",
                "empty_home": f"{metadata}/git-home",
                "destination": str(layout["work_staging"]),
            }
        invocation = build_public_git_clone_invocation(
            str(run["source_url"]),
            git_path=str(parameters["git_path"]),
            askpass_path=str(parameters["askpass_path"]),
            empty_home=str(parameters["empty_home"]),
            destination=str(parameters["destination"]),
        )
        self._write_source_metadata(run, target, cwd_rel=".")
        self._check_cancel(cancel)
        if not self.store.transition_remote_run(
            str(run["id"]),
            expected_states={"preparing"},
            state="preparing",
            phase="starting",
            ended_at=None,
            expires_at=None,
            error_code="start_status_unknown",
            error_detail=None,
        ):
            self._check_cancel(cancel)
            raise RemoteRunError("Remote Run preparation was superseded", code="state_conflict")
        started = self._start_with_reconciliation(
            run,
            target,
            lambda: backend.start_remote_git_run(
                target,
                str(run["run_base"]),
                str(run["id"]),
                invocation,
            ),
        )
        if not started:
            return
        self.store.transition_remote_run(
            str(run["id"]),
            expected_states={"preparing"},
            state="preparing",
            phase="cloning",
            error_code=None,
            error_detail=None,
        )

    def _prepare_zip(
        self,
        run: dict[str, Any],
        target: dict[str, Any],
        cancel: threading.Event,
    ) -> None:
        self._check_cancel(cancel)
        archive_path, sidecar_path, _part_path = self._spool_paths(str(run["id"]))
        if not archive_path.is_file() or not sidecar_path.is_file():
            raise RemoteRunError("Verified ZIP spool is missing", code="zip_spool_missing")
        options = self._source_options(run)
        manifest = validate_zip_archive(
            archive_path,
            limits=self.archive_limits,
            original_filename=str(options.get("archive_name") or run["source_label"]),
        )
        self._ensure_target_capacity(target, run, manifest.total_bytes)
        self.store.transition_remote_run(
            str(run["id"]),
            expected_states={"preparing"},
            state="preparing",
            phase="copying",
            source_size=manifest.total_bytes,
        )
        backend = self._target_backend(target)
        layout = backend.create_remote_run_layout(
            target,
            str(run["id"]),
            run_base_dir=str(run["run_base"]),
            command=str(run["command"]),
            cwd_rel=manifest.cwd_rel,
        )
        self._check_cancel(cancel)
        with backend.remote_run_snapshot_sink(
            target,
            str(run["run_base"]),
            str(run["id"]),
        ) as sink:
            materialize_zip_archive(
                archive_path,
                manifest,
                _CancellableSink(sink, cancel),
                limits=self.archive_limits,
            )
        self._check_cancel(cancel)
        backend.commit_remote_run_snapshot(target, str(run["run_base"]), str(run["id"]))
        self._write_zip_manifest(run, target, manifest)
        self._write_source_metadata(run, target, cwd_rel=manifest.cwd_rel)
        self._start_materialized_run(run, target, layout, cancel)

    def _start_materialized_run(
        self,
        run: dict[str, Any],
        target: dict[str, Any],
        layout: Mapping[str, str],
        cancel: threading.Event,
    ) -> None:
        del layout
        self._check_cancel(cancel)
        if not self.store.transition_remote_run(
            str(run["id"]),
            expected_states={"preparing"},
            state="preparing",
            phase="starting",
            ended_at=None,
            expires_at=None,
            error_code="start_status_unknown",
            error_detail=None,
        ):
            self._check_cancel(cancel)
            raise RemoteRunError("Remote Run preparation was superseded", code="state_conflict")
        self._start_with_reconciliation(
            run,
            target,
            lambda: self._target_backend(target).start_remote_run(
                target, str(run["run_base"]), str(run["id"])
            ),
        )

    def _start_with_reconciliation(
        self,
        run: Mapping[str, Any],
        target: dict[str, Any],
        starter: Callable[[], Any],
    ) -> bool:
        """Resolve the ambiguous boundary where SSH can lose only the start ACK."""

        try:
            starter()
        except (SSHCommandStatusUnknown, NodeRemoteRunError) as exc:
            self._reconcile_start_exception(run, target, exc)
            return False
        self.store.transition_remote_run(
            str(run["id"]),
            expected_states={"preparing"},
            error_code=None,
            error_detail=None,
        )
        return True

    def _reconcile_start_exception(
        self,
        run: Mapping[str, Any],
        target: dict[str, Any],
        exc: BaseException,
    ) -> None:
        run_id = str(run["id"])
        try:
            remote = self._target_backend(target).reconcile_remote_run(
                target,
                str(run["run_base"]),
                run_id,
            )
        except Exception:
            remote = None

        current = self.get(run_id)
        if remote is not None:
            self._apply_remote_status(current, remote)
            current = self.get(run_id)
            if current["state"] != "preparing" or remote.get("phase") == "cloning":
                return

        self.store.transition_remote_run(
            run_id,
            expected_states={"preparing"},
            state="preparing",
            phase=current.get("phase") or "starting",
            ended_at=None,
            expires_at=None,
            error_code="start_status_unknown",
            error_detail=str(exc)[:1000],
        )

    def _write_source_metadata(
        self,
        run: Mapping[str, Any],
        target: dict[str, Any],
        *,
        cwd_rel: str,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        value = {
            "kind": str(run["source_kind"]),
            "label": str(run["source_label"]),
            "cwd_rel": cwd_rel,
            **dict(extra or {}),
        }
        if run.get("source_url"):
            value["url"] = str(run["source_url"])
        self._target_backend(target).write_remote_run_json(
            target,
            str(run["run_base"]),
            str(run["id"]),
            "source.json",
            value,
        )

    def _write_manifest(
        self,
        run: Mapping[str, Any],
        target: dict[str, Any],
        manifest: WorkspaceManifest,
    ) -> None:
        entries = [
            {
                "path": entry.relative_path,
                "kind": entry.kind,
                "size": entry.size,
                "mtime_ns": entry.mtime_ns,
                "executable": entry.executable,
                **({"link_target": entry.link_target} if entry.link_target else {}),
            }
            for entry in manifest.entries
        ]
        self._target_backend(target).write_remote_run_json(
            target,
            str(run["run_base"]),
            str(run["id"]),
            "source-manifest.json",
            entries,
        )

    def _write_zip_manifest(
        self,
        run: Mapping[str, Any],
        target: dict[str, Any],
        manifest: ZipManifest,
    ) -> None:
        entries = [
            {
                "path": entry.relative_path,
                "kind": entry.kind,
                "size": entry.size,
                "executable": entry.executable,
            }
            for entry in manifest.entries
        ]
        self._target_backend(target).write_remote_run_json(
            target,
            str(run["run_base"]),
            str(run["id"]),
            "source-manifest.json",
            entries,
        )

    def _ensure_target_capacity(
        self, target: dict[str, Any], run: Mapping[str, Any], required: int
    ) -> None:
        preflight = self._target_backend(target).preflight_remote_run_target(
            target,
            run_base_dir=str(run["run_base"]),
        )
        available = preflight.get("available_bytes")
        if isinstance(available, int) and required > available:
            raise RemoteRunError(
                "The Remote Run Source is larger than the target's available space",
                code="target_disk_space",
            )

    @staticmethod
    def _check_cancel(cancel: threading.Event) -> None:
        if cancel.is_set():
            raise RemoteRunError("Remote Run preparation was cancelled", code="cancelled")

    def _record_preparation_failure(
        self,
        run_id: str,
        exc: BaseException,
        cancel: threading.Event,
    ) -> None:
        current = self.store.get_remote_run(run_id)
        if not current or current["state"] != "preparing":
            return
        stopped = cancel.is_set() or (
            isinstance(exc, RemoteRunError) and exc.code == "cancelled"
        )
        now = utc_now()
        code = "cancelled" if stopped else getattr(exc, "code", "prepare_failed")
        self.store.transition_remote_run(
            run_id,
            expected_states={"preparing"},
            state="stopped" if stopped else "failed",
            phase=None,
            ended_at=now,
            expires_at=_terminal_expiry(),
            error_code=str(code)[:80],
            error_detail=None if stopped else str(exc)[:1000],
        )

    def _source_options(self, run: Mapping[str, Any]) -> dict[str, Any]:
        try:
            value = json.loads(str(run.get("source_options_json") or "{}"))
        except json.JSONDecodeError as exc:
            raise RemoteRunError(
                "Remote Run Source options are invalid", code="source_options"
            ) from exc
        if not isinstance(value, dict):
            raise RemoteRunError("Remote Run Source options are invalid", code="source_options")
        return value

    def _same_target_exclusions(
        self,
        workspace: Mapping[str, Any],
        source_path: str,
        run: Mapping[str, Any],
    ) -> tuple[str, ...]:
        if str(workspace.get("computer_id") or "") != str(run["target_computer_id"]):
            return ()
        workspace_root = PurePosixPath(str(workspace["remote_path"]))
        source_root = workspace_root if source_path == "." else workspace_root / source_path
        run_base = PurePosixPath(str(run["run_base"]))
        try:
            relative = run_base.relative_to(source_root)
        except ValueError:
            return ()
        if not relative.parts:
            raise RemoteRunError(
                "The target Remote Run directory cannot be the Workspace Source",
                code="source_contains_run_base",
            )
        return (relative.as_posix(),)

    async def upload_archive(
        self,
        run_id: str,
        filename: str,
        chunks: AsyncIterator[bytes],
        *,
        content_length: int | None = None,
    ) -> dict[str, Any]:
        safe_id = validate_remote_run_id(run_id)
        run = self.get(safe_id)
        if run["source_kind"] != "archive" or run.get("archive_format") != "zip":
            raise RemoteRunError(
                "This Remote Run does not use a ZIP Archive",
                code="source_kind",
            )
        options = self._source_options(run)
        expected_name = str(options.get("archive_name") or "")
        safe_name = Path(validate_zip_filename(filename)).name
        if safe_name != expected_name:
            raise RemoteRunConflict(
                "ZIP filename differs from the immutable Remote Run Source",
                code="idempotency_conflict",
            )
        if content_length is not None and content_length > self.max_archive_bytes:
            raise ArchiveSafetyError(
                "ZIP upload exceeds the configured limit", code="zip_upload_limit"
            )
        if not self.store.transition_remote_run(
            safe_id,
            expected_states={"preparing"},
            expected_phase="waiting_upload",
            state="preparing",
            phase="uploading",
        ):
            current = self.get(safe_id)
            if current["phase"] in {"checking", "copying", "starting"}:
                return current
            raise RemoteRunConflict(
                "ZIP upload is already active or finished", code="upload_conflict"
            )

        archive_path, sidecar_path, part_path = self._spool_paths(safe_id)
        total = 0
        try:
            with part_path.open("xb") as handle:
                part_path.chmod(0o600)
                async for chunk in chunks:
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.max_archive_bytes:
                        raise ArchiveSafetyError(
                            "ZIP upload exceeds the configured limit",
                            code="zip_upload_limit",
                        )
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(part_path, archive_path)
            uploaded_at = datetime.now(UTC)
            sidecar_temp = sidecar_path.with_suffix(".json.part")
            sidecar_temp.write_text(
                _canonical_json(
                    {
                        "run_id": safe_id,
                        "filename": safe_name,
                        "uploaded_at": uploaded_at.isoformat(timespec="seconds"),
                        "absolute_expires_at": (
                            uploaded_at + REMOTE_RUN_RETENTION
                        ).isoformat(timespec="seconds"),
                        "size": total,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            sidecar_temp.chmod(0o600)
            os.replace(sidecar_temp, sidecar_path)
            if not self.store.transition_remote_run(
                safe_id,
                expected_states={"preparing"},
                expected_phase="uploading",
                state="preparing",
                phase="checking",
                expires_at=None,
                source_size=total,
            ):
                raise RemoteRunConflict(
                    "ZIP upload completion was superseded", code="state_conflict"
                )
            self._schedule_spool_expiry(safe_id, uploaded_at + REMOTE_RUN_RETENTION)
            self.schedule(safe_id)
            return self.get(safe_id)
        except Exception:
            part_path.unlink(missing_ok=True)
            self.store.transition_remote_run(
                safe_id,
                expected_states={"preparing"},
                expected_phase="uploading",
                state="preparing",
                phase="waiting_upload",
                expires_at=_waiting_upload_expiry(),
                source_size=None,
            )
            raise

    def poll(
        self,
        run_id: str,
        *,
        stream: str = "command",
        offset: int | None = None,
        limit: int = 256 * 1024,
    ) -> dict[str, Any]:
        safe_id = validate_remote_run_id(run_id)
        if stream not in {"prepare", "command"}:
            raise ValueError("Remote Run log stream must be prepare or command")
        with self.lock_for(safe_id):
            run = self.get(safe_id)
            target = self._require_run_target(run)
            backend = self._target_backend(target)
            try:
                remote = backend.poll_remote_run(
                    target,
                    str(run["run_base"]),
                    safe_id,
                    stream=stream,
                    offset=offset,
                    limit=limit,
                )
            except SSHBackendError:
                return {
                    **run,
                    "connection": "offline",
                    "cleanup_pending": self._cleanup_pending(run),
                    "log": {
                        "chunk_b64": "",
                        "start_offset": offset or 0,
                        "next_offset": offset or 0,
                        "eof": False,
                    },
                }
            self._apply_remote_observation(run, remote)
            updated = self.get(safe_id)
            if (
                updated["state"] in {"running", "finished", "stopped", "lost"}
                and not remote.get("layout_missing")
                and not remote.get("layout_error")
            ):
                try:
                    self._ensure_workspace_bridge(updated)
                except (FileNotFoundError, OSError, RemoteRunError, SSHBackendError):
                    if updated["state"] in {"running", "finished"}:
                        raise
                updated = self.get(safe_id)
            if updated["state"] == "running" and updated["source_kind"] == "archive":
                self._remove_spool(safe_id)
            return {
                **updated,
                "connection": "online",
                "cleanup_pending": self._cleanup_pending(updated),
                **{key: value for key, value in remote.items() if key not in {"state"}},
                "state": updated["state"],
            }

    def reconcile(self, run_id: str) -> dict[str, Any]:
        """Observe lifecycle metadata without reading any Remote Run log bytes."""

        safe_id = validate_remote_run_id(run_id)
        with self.lock_for(safe_id):
            run = self.get(safe_id)
            if run["state"] in TERMINAL_STATES:
                return {
                    **run,
                    "connection": "online",
                    "cleanup_pending": self._cleanup_pending(run),
                }
            target = self._require_run_target(run)
            backend = self._target_backend(target)
            try:
                remote = backend.reconcile_remote_run(
                    target,
                    str(run["run_base"]),
                    safe_id,
                )
            except SSHBackendError:
                return {
                    **run,
                    "connection": "offline",
                    "cleanup_pending": self._cleanup_pending(run),
                }
            self._apply_remote_observation(run, remote)
            updated = self.get(safe_id)
            if updated["state"] == "running" and updated["source_kind"] == "archive":
                self._remove_spool(safe_id)
            return {
                **updated,
                "connection": "online",
                "cleanup_pending": self._cleanup_pending(updated),
                **{key: value for key, value in remote.items() if key != "state"},
                "state": updated["state"],
            }

    def _apply_remote_observation(
        self,
        run: Mapping[str, Any],
        remote: Mapping[str, Any],
    ) -> None:
        if remote.get("layout_missing"):
            if run["state"] == "running" and not remote.get("tmux_running"):
                self._apply_remote_status(
                    run,
                    {
                        "state": "lost",
                        "started_at": run.get("started_at"),
                        "ended_at": utc_now(),
                        "error_code": "layout_missing",
                        "error_detail": "Remote Run files and managed tmux pane are missing",
                    },
                )
            return
        if not remote.get("layout_error"):
            self._apply_remote_status(run, remote)

    def _apply_remote_status(
        self, run: Mapping[str, Any], remote: Mapping[str, Any]
    ) -> None:
        state = str(remote.get("state") or "")
        if state not in NONTERMINAL_STATES | TERMINAL_STATES:
            return
        current_state = str(run["state"])
        if current_state in TERMINAL_STATES:
            return
        if state == "preparing":
            phase = remote.get("phase")
            if phase == "cloning":
                self.store.transition_remote_run(
                    str(run["id"]),
                    expected_states={"preparing"},
                    state="preparing",
                    phase="cloning",
                    error_code=None,
                    error_detail=None,
                )
            else:
                self.store.transition_remote_run(
                    str(run["id"]),
                    expected_states={"preparing"},
                    state="preparing",
                    phase=str(phase) if phase else run.get("phase"),
                )
            return
        if state == "running":
            self.store.transition_remote_run(
                str(run["id"]),
                expected_states={"preparing", "running"},
                state="running",
                phase=None,
                started_at=remote.get("started_at") or run.get("started_at") or utc_now(),
                error_code=None,
                error_detail=None,
                source_revision=remote.get("source_revision") or run.get("source_revision"),
            )
            return

        ended_at = str(remote.get("ended_at") or utc_now())
        expires_at = _terminal_expiry_from(ended_at)
        self.store.transition_remote_run(
            str(run["id"]),
            expected_states={"preparing", "running"},
            state=state,
            phase=None,
            started_at=remote.get("started_at") or run.get("started_at"),
            ended_at=ended_at,
            expires_at=expires_at,
            exit_code=remote.get("exit_code"),
            error_code=remote.get("error_code"),
            error_detail=remote.get("error_detail"),
            source_revision=remote.get("source_revision") or run.get("source_revision"),
        )

    def stop(self, run_id: str) -> dict[str, Any]:
        safe_id = validate_remote_run_id(run_id)
        run = self.get(safe_id)
        if run["state"] in TERMINAL_STATES:
            return {"stopped": True, "needs_kill": False, "run": run}
        now = utc_now()
        self.store.transition_remote_run(
            safe_id,
            expected_states={"preparing", "running"},
            stop_requested_at=now,
        )
        handle = self._preparations.get(safe_id)
        live_handle = bool(handle and not handle.task.done())
        if live_handle and handle:
            handle.cancel.set()
        if run["state"] == "preparing" and run.get("phase") == "waiting_upload":
            self.store.transition_remote_run(
                safe_id,
                expected_states={"preparing"},
                expected_phase="waiting_upload",
                state="stopped",
                phase=None,
                ended_at=now,
                expires_at=_terminal_expiry(),
                error_code="cancelled",
            )
            return {"stopped": True, "needs_kill": False, "run": self.get(safe_id)}
        target = self._require_run_target(run)
        backend = self._target_backend(target)
        try:
            result = backend.interrupt_remote_run(
                target, str(run["run_base"]), safe_id
            )
        except RemoteRunLayoutError as exc:
            if live_handle and run["state"] == "preparing":
                return {
                    "stopped": False,
                    "needs_kill": False,
                    "cancellation_pending": True,
                    "run": self.get(safe_id),
                }
            raise RemoteRunConflict(
                "Remote Run files cannot be verified safely",
                code="layout_unverifiable",
            ) from exc
        if result.get("completed"):
            remote = backend.reconcile_remote_run(
                target, str(run["run_base"]), safe_id
            )
            self._apply_remote_status(self.get(safe_id), remote)
            updated = self.get(safe_id)
            return {
                "stopped": updated["state"] in TERMINAL_STATES,
                "needs_kill": False,
                "run": updated,
            }
        if result.get("layout_missing"):
            if result.get("sent"):
                return {
                    "stopped": False,
                    "needs_kill": True,
                    "run": self.get(safe_id),
                }
            if run["state"] == "running" and not result.get("tmux_running"):
                self._apply_remote_status(
                    self.get(safe_id),
                    {
                        "state": "lost",
                        "started_at": run.get("started_at"),
                        "ended_at": now,
                        "error_code": "layout_missing",
                        "error_detail": "Remote Run files and managed tmux pane are missing",
                    },
                )
                return {
                    "stopped": True,
                    "needs_kill": False,
                    "run": self.get(safe_id),
                }
            if run["state"] != "preparing" or result.get("tmux_exists"):
                raise RemoteRunConflict(
                    "Remote Run files are missing while its session cannot be verified",
                    code="layout_unverifiable",
                )
            if live_handle:
                return {
                    "stopped": False,
                    "needs_kill": False,
                    "cancellation_pending": True,
                    "run": self.get(safe_id),
                }
            self.store.transition_remote_run(
                safe_id,
                expected_states={"preparing", "running"},
                state="stopped",
                phase=None,
                ended_at=now,
                expires_at=_terminal_expiry(),
                error_code="cancelled",
            )
            return {"stopped": True, "needs_kill": False, "run": self.get(safe_id)}
        sent = bool(result.get("sent"))
        if sent:
            return {"stopped": False, "needs_kill": True, "run": self.get(safe_id)}
        if live_handle:
            return {
                "stopped": False,
                "needs_kill": False,
                "cancellation_pending": True,
                "run": self.get(safe_id),
            }
        remote = backend.reconcile_remote_run(
            target, str(run["run_base"]), safe_id
        )
        self._apply_remote_status(self.get(safe_id), remote)
        updated = self.get(safe_id)
        return {
            "stopped": updated["state"] in TERMINAL_STATES,
            "needs_kill": False,
            "run": updated,
        }

    def kill(self, run_id: str) -> dict[str, Any]:
        safe_id = validate_remote_run_id(run_id)
        run = self.get(safe_id)
        if run["state"] in TERMINAL_STATES:
            return run
        handle = self._preparations.get(safe_id)
        live_handle = bool(handle and not handle.task.done())
        if live_handle and handle:
            handle.cancel.set()
        now = utc_now()
        self.store.transition_remote_run(
            safe_id,
            expected_states={"preparing", "running"},
            stop_requested_at=run.get("stop_requested_at") or now,
        )
        target = self._require_run_target(run)
        backend = self._target_backend(target)
        try:
            result = backend.kill_remote_run(target, str(run["run_base"]), safe_id)
        except RemoteRunLayoutError as exc:
            if live_handle and run["state"] == "preparing":
                return self.get(safe_id)
            raise RemoteRunConflict(
                "Remote Run files cannot be verified safely",
                code="layout_unverifiable",
            ) from exc
        if result.get("completed"):
            remote = backend.reconcile_remote_run(
                target, str(run["run_base"]), safe_id
            )
            self._apply_remote_status(self.get(safe_id), remote)
            return self.get(safe_id)
        if result.get("layout_missing"):
            if result.get("killed"):
                self.store.transition_remote_run(
                    safe_id,
                    expected_states={"preparing", "running"},
                    state="stopped",
                    phase=None,
                    ended_at=now,
                    expires_at=_terminal_expiry(),
                    error_code="killed",
                )
                return self.get(safe_id)
            if run["state"] == "running" and not result.get("tmux_running"):
                self._apply_remote_status(
                    self.get(safe_id),
                    {
                        "state": "lost",
                        "started_at": run.get("started_at"),
                        "ended_at": now,
                        "error_code": "layout_missing",
                        "error_detail": "Remote Run files and managed tmux pane are missing",
                    },
                )
                return self.get(safe_id)
            if run["state"] != "preparing" or result.get("tmux_exists"):
                raise RemoteRunConflict(
                    "Remote Run files are missing while its session cannot be verified",
                    code="layout_unverifiable",
                )
            if live_handle:
                return self.get(safe_id)
        self.store.transition_remote_run(
            safe_id,
            expected_states={"preparing", "running"},
            state="stopped",
            phase=None,
            ended_at=now,
            expires_at=_terminal_expiry(),
            error_code="cancelled" if result.get("layout_missing") else "killed",
        )
        return self.get(safe_id)

    def request_delete(self, run_id: str) -> dict[str, Any]:
        safe_id = validate_remote_run_id(run_id)
        with self.lock_for(safe_id):
            run = self.get(safe_id)
            if run["state"] not in TERMINAL_STATES:
                raise RemoteRunConflict(
                    "Stop the Remote Run before deleting it",
                    code="run_active",
                )
            now = utc_now()
            if not self.store.expire_remote_run_now(safe_id, now):
                raise RemoteRunConflict("Remote Run is not deletable", code="state_conflict")
            run = self.get(safe_id)
            target = self._require_run_target(run)
            backend = self._target_backend(target)
            try:
                backend.delete_remote_run_root(
                    target, str(run["run_base"]), safe_id
                )
            except (OSError, SSHBackendError):
                return {**run, "cleanup_pending": True, "deleted": False}
            self._remove_spool(safe_id)
            self.store.delete_remote_run_workspace(safe_id)
            self.store.delete_remote_run(safe_id)
            return {"id": safe_id, "deleted": True, "cleanup_pending": False}

    def forget(self, run_id: str) -> None:
        safe_id = validate_remote_run_id(run_id)
        with self.lock_for(safe_id):
            run = self.get(safe_id)
            if run["state"] not in TERMINAL_STATES or not self._cleanup_pending(run):
                raise RemoteRunConflict(
                    "Only a terminal cleanup-pending Run can be forgotten",
                    code="forget_not_allowed",
                )
            self._remove_spool(safe_id)
            self.store.delete_remote_run_workspace(safe_id)
            self.store.delete_remote_run(safe_id)

    @staticmethod
    def _cleanup_pending(run: Mapping[str, Any]) -> bool:
        expires = run.get("expires_at")
        return bool(
            run.get("state") in TERMINAL_STATES
            and expires
            and str(expires) <= utc_now()
        )

    async def preflight(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._bind_node_loop()
        normalized = self._normalize_create_payload(payload)
        target = self._require_computer(str(normalized["target_computer_id"]))
        backend = self._target_backend(target)
        result = await asyncio.to_thread(
            backend.preflight_remote_run_target,
            target,
            run_base_dir=str(target.get("run_base_dir") or "") or None,
            require_git=normalized["source_kind"] == "git",
        )
        return {
            "ok": True,
            "run_base": result["run_base"],
            "warnings": result.get("warnings") or [],
        }

    def list_files(
        self, run_id: str, relative_path: str = "."
    ) -> tuple[str, list[FileEntry]]:
        safe_id = validate_remote_run_id(run_id)
        with self.lock_for(safe_id):
            run = self.get(safe_id)
            return self.ssh.list_dir(self._run_workspace(run), relative_path)

    def stat_file(self, run_id: str, relative_path: str) -> FileEntry:
        safe_id = validate_remote_run_id(run_id)
        with self.lock_for(safe_id):
            run = self.get(safe_id)
            return self.ssh.stat(self._run_workspace(run), relative_path)

    def read_preview(
        self,
        run_id: str,
        relative_path: str,
        *,
        mode: str,
        offset: int,
        max_bytes: int,
    ) -> TextPreview:
        safe_id = validate_remote_run_id(run_id)
        with self.lock_for(safe_id):
            run = self.get(safe_id)
            return self.ssh.read_text_preview(
                self._run_workspace(run),
                relative_path,
                mode=mode,
                offset=offset,
                max_bytes=max_bytes,
            )

    def download_iter(self, run_id: str, relative_path: str) -> Iterator[bytes]:
        safe_id = validate_remote_run_id(run_id)

        def generate() -> Iterator[bytes]:
            with self.lock_for(safe_id):
                run = self.get(safe_id)
                iterator = self.ssh.download_iter(self._run_workspace(run), relative_path)
                try:
                    yield from iterator
                finally:
                    close = getattr(iterator, "close", None)
                    if close:
                        close()

        return generate()

    def log_iter(self, run_id: str, stream: str) -> Iterator[bytes]:
        safe_id = validate_remote_run_id(run_id)
        if stream not in {"prepare", "command"}:
            raise ValueError("Remote Run log stream must be prepare or command")

        def generate() -> Iterator[bytes]:
            with self.lock_for(safe_id):
                run = self.get(safe_id)
                target = self._require_run_target(run)
                backend = self._target_backend(target)
                offset = 0
                while True:
                    chunk = backend.read_remote_run_log(
                        target,
                        str(run["run_base"]),
                        safe_id,
                        stream=stream,
                        offset=offset,
                        limit=256 * 1024,
                    )
                    raw = base64.b64decode(str(chunk.get("chunk_b64") or ""))
                    if raw:
                        yield raw
                    next_offset = int(chunk.get("next_offset") or offset)
                    if next_offset <= offset or bool(chunk.get("eof")):
                        break
                    offset = next_offset

        return generate()

    def content_type(self, relative_path: str) -> str:
        return self.files.content_type(relative_path)

    def cleanup_expired(self, *, computer_id: str | None = None) -> int:
        removed = 0
        now = utc_now()
        for row in self.store.list_abandoned_remote_run_uploads(now):
            if computer_id and str(row["target_computer_id"]) != computer_id:
                continue
            run_id = str(row["id"])
            if not self.store.transition_remote_run(
                run_id,
                expected_states={"preparing"},
                expected_phase="waiting_upload",
                state="failed",
                phase=None,
                ended_at=now,
                expires_at=now,
                error_code="upload_expired",
                error_detail="ZIP upload was not completed",
            ):
                continue
            self._remove_spool(run_id)
            self.store.delete_remote_run(run_id)
            removed += 1
        for row in self.store.list_expired_remote_runs(now):
            if computer_id and str(row["target_computer_id"]) != computer_id:
                continue
            try:
                result = self.request_delete(str(row["id"]))
            except (KeyError, OSError, RemoteRunError, SSHBackendError):
                continue
            if result.get("deleted"):
                removed += 1
        return removed

    def schedule_cleanup(self) -> bool:
        """Run best-effort SSH cleanup without delaying an HTTP response."""

        current = self._cleanup_task
        if current and not current.done():
            return False
        task = asyncio.create_task(asyncio.to_thread(self.cleanup_expired))
        self._cleanup_task = task
        task.add_done_callback(self._cleanup_done)
        return True

    def _cleanup_done(self, task: asyncio.Task[int]) -> None:
        if self._cleanup_task is task:
            self._cleanup_task = None
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()

    async def startup(self) -> None:
        self._bind_node_loop()
        await asyncio.to_thread(self.cleanup_spools)
        if not self._startup_reconcile_task or self._startup_reconcile_task.done():
            self._startup_reconcile_task = asyncio.create_task(self._reconcile_startup())
        if not self._observer_task or self._observer_task.done():
            self._observer_task = asyncio.create_task(self._observe_active_runs())
        self._observer_wakeup.set()

    async def _observe_active_runs(self) -> None:
        loop = asyncio.get_running_loop()
        next_attempt: dict[str, float] = {}
        failures: dict[str, int] = {}
        while True:
            self._observer_wakeup.clear()
            rows = await asyncio.to_thread(self.store.list_active_remote_runs)
            active_ids = {str(row["id"]) for row in rows}
            for run_id in set(next_attempt) - active_ids:
                next_attempt.pop(run_id, None)
                failures.pop(run_id, None)

            now = loop.time()
            for row in rows:
                run_id = str(row["id"])
                if next_attempt.get(run_id, 0.0) > now:
                    continue
                try:
                    result = await asyncio.to_thread(self.reconcile, run_id)
                except (KeyError, OSError, RemoteRunError, SSHBackendError, ValueError):
                    connection = "offline"
                else:
                    connection = str(result.get("connection") or "online")
                    if result.get("state") in TERMINAL_STATES:
                        next_attempt.pop(run_id, None)
                        failures.pop(run_id, None)
                        continue
                if connection == "offline":
                    failure_count = failures.get(run_id, 0) + 1
                    failures[run_id] = failure_count
                    delay = min(
                        REMOTE_RUN_OBSERVER_MAX_BACKOFF,
                        REMOTE_RUN_OBSERVER_INTERVAL
                        * (2 ** min(failure_count - 1, 4)),
                    )
                else:
                    failures.pop(run_id, None)
                    delay = REMOTE_RUN_OBSERVER_INTERVAL
                next_attempt[run_id] = loop.time() + delay

            if self._observer_wakeup.is_set():
                continue
            if not rows:
                await self._observer_wakeup.wait()
                continue
            due = [next_attempt.get(str(row["id"]), loop.time()) for row in rows]
            timeout = max(0.0, min(due) - loop.time())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._observer_wakeup.wait(), timeout=timeout)

    async def _reconcile_startup(self) -> None:
        rows = self.store.list_recent_remote_runs(10_000)
        for row in rows:
            run_id = str(row["id"])
            if row["state"] == "running":
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.poll, run_id, offset=0, limit=1)
            elif row["state"] == "preparing":
                if row["phase"] == "waiting_upload":
                    continue
                try:
                    result = await asyncio.to_thread(self.poll, run_id, offset=0, limit=1)
                except (OSError, RemoteRunError, SSHBackendError):
                    try:
                        target = self._require_run_target(row)
                        backend = self._target_backend(target)
                        layout_exists = await asyncio.to_thread(
                            backend.remote_run_layout_exists,
                            target,
                            str(row["run_base"]),
                            run_id,
                        )
                    except (OSError, RemoteRunError, SSHBackendError):
                        continue
                    if layout_exists:
                        continue
                    result = {
                        "state": "preparing",
                        "connection": "online",
                        "tmux_exists": False,
                        "record_errors": [],
                    }
                if result.get("state") != "preparing":
                    continue
                if result.get("connection") != "online":
                    try:
                        target = self._require_run_target(row)
                        backend = self._target_backend(target)
                        layout_exists = await asyncio.to_thread(
                            backend.remote_run_layout_exists,
                            target,
                            str(row["run_base"]),
                            run_id,
                        )
                    except (OSError, RemoteRunError, SSHBackendError):
                        continue
                    if layout_exists:
                        continue
                    result = {
                        **result,
                        "connection": "online",
                        "tmux_exists": False,
                        "record_errors": [],
                    }
                if result.get("tmux_exists") or result.get("record_errors"):
                    continue
                current = self.store.get_remote_run(run_id)
                if current and current.get("error_code") == "start_status_unknown":
                    continue
                self._record_preparation_failure(
                    run_id,
                    RemoteRunError(
                        "Core restarted before Source preparation finished",
                        code="core_restarted",
                    ),
                    threading.Event(),
                )
        await asyncio.to_thread(self.cleanup_expired)
        self._observer_wakeup.set()

    async def shutdown(self) -> None:
        handles = list(self._preparations.values())
        for handle in handles:
            handle.cancel.set()
        if handles:
            await asyncio.gather(
                *(handle.task for handle in handles),
                return_exceptions=True,
            )
        maintenance = [
            task
            for task in (
                self._startup_reconcile_task,
                self._observer_task,
                self._cleanup_task,
            )
            if task and not task.done()
        ]
        for task in maintenance:
            task.cancel()
        if maintenance:
            await asyncio.gather(*maintenance, return_exceptions=True)

    def retry_preparation(self, run_id: str) -> dict[str, Any]:
        safe_id = validate_remote_run_id(run_id)
        run = self.get(safe_id)
        if run["source_kind"] != "archive" or run["state"] != "failed":
            raise RemoteRunConflict(
                "Only a failed ZIP preparation can be retried",
                code="retry_not_allowed",
            )
        archive_path, sidecar_path, _part_path = self._spool_paths(safe_id)
        if not archive_path.is_file() or not sidecar_path.is_file():
            raise RemoteRunError("Verified ZIP spool has expired", code="zip_spool_expired")
        target = self._require_run_target(run)
        with contextlib.suppress(FileNotFoundError):
            self._target_backend(target).delete_remote_run_root(
                target, str(run["run_base"]), safe_id
            )
        if not self.store.transition_remote_run(
            safe_id,
            expected_states={"failed"},
            state="preparing",
            phase="checking",
            started_at=None,
            stop_requested_at=None,
            ended_at=None,
            expires_at=None,
            exit_code=None,
            error_code=None,
            error_detail=None,
        ):
            raise RemoteRunConflict("Remote Run retry was superseded", code="state_conflict")
        self.schedule(safe_id)
        return self.get(safe_id)

    def cleanup_spools(self) -> int:
        removed = 0
        now = datetime.now(UTC)
        for part in self.spool_root.glob("*.part"):
            with contextlib.suppress(OSError):
                modified = datetime.fromtimestamp(part.stat().st_mtime, tz=UTC)
                if now - modified > WAITING_UPLOAD_TTL:
                    part.unlink()
                    removed += 1
        for sidecar in self.spool_root.glob("*.json"):
            run_id = sidecar.stem
            with contextlib.suppress(ValueError):
                validate_remote_run_id(run_id)
                archive, metadata, _part = self._spool_paths(run_id)
                try:
                    value = json.loads(metadata.read_text(encoding="utf-8"))
                    expires = datetime.fromisoformat(str(value["absolute_expires_at"]))
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=UTC)
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    archive.unlink(missing_ok=True)
                    metadata.unlink(missing_ok=True)
                    removed += 1
                    continue
                if not archive.is_file() or expires <= now:
                    archive.unlink(missing_ok=True)
                    metadata.unlink(missing_ok=True)
                    removed += 1
        for archive in self.spool_root.glob("*.zip"):
            sidecar = archive.with_suffix(".json")
            if not sidecar.is_file():
                archive.unlink(missing_ok=True)
                removed += 1
        return removed

    def _spool_paths(self, run_id: str) -> tuple[Path, Path, Path]:
        safe_id = validate_remote_run_id(run_id)
        return (
            self.spool_root / f"{safe_id}.zip",
            self.spool_root / f"{safe_id}.json",
            self.spool_root / f"{safe_id}.part",
        )

    def _schedule_spool_expiry(self, run_id: str, expires_at: datetime) -> None:
        delay = max(0.0, (expires_at - datetime.now(UTC)).total_seconds())
        loop = asyncio.get_running_loop()
        loop.call_later(delay, self._remove_spool, run_id)

    def _remove_spool(self, run_id: str) -> None:
        archive, sidecar, part = self._spool_paths(run_id)
        archive.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        part.unlink(missing_ok=True)

    def _run_workspace(self, run: Mapping[str, Any]) -> dict[str, Any]:
        target = self._require_run_target(run)
        root = f"{str(run['run_base']).rstrip('/')}/{run['id']}/work"
        return {
            "backend_kind": "remote",
            "computer_id": str(target["id"]),
            "computer": target,
            "remote_path": root,
            "canonical_path": root,
        }

    def ensure_workspace_bridge(self, run_id: str) -> dict[str, Any]:
        """Retry attaching a completed remote work tree to its Workspace."""

        safe_id = validate_remote_run_id(run_id)
        with self.lock_for(safe_id):
            run = self.get(safe_id)
            if run.get("workspace_id"):
                return run
            if run["state"] not in {"finished", "stopped", "lost"}:
                return run
            self._ensure_workspace_bridge(run)
            return self.get(safe_id)

    def _ensure_workspace_bridge(self, run: Mapping[str, Any]) -> dict[str, Any]:
        """Attach a real Workspace shell once the remote work tree is committed."""

        workspace_id = run.get("workspace_id")
        if workspace_id:
            return self.workspaces.require(str(workspace_id))
        target = self._require_run_target(run)
        run_id = str(run["id"])
        backend = self._target_backend(target)
        shell = backend.ensure_remote_run_workspace_shell(
            target,
            str(run["run_base"]),
            run_id,
            allow_create_session=str(run["state"]) in TERMINAL_STATES,
        )
        workspace = self.workspaces.open_remote_run(
            run,
            str(shell["session_name"]),
            str(shell["work_path"]),
        )
        if self._is_node_target(target):
            terminals = shell.get("terminals")
            if not isinstance(terminals, list) or not terminals:
                raise RemoteRunError(
                    "Node Remote Run Terminal list is invalid",
                    code="terminal_invalid",
                )
            self.store.reconcile_terminals(str(workspace["id"]), terminals)
        else:
            self.ssh.ensure_workspace(workspace)
        return workspace

    def _require_computer(self, computer_id: str) -> dict[str, Any]:
        if not computer_id:
            raise RemoteRunError("Choose a Remote", code="target_required")
        computer = self.store.get_computer(computer_id)
        if not computer:
            raise RemoteRunError(
                f"Unknown Remote: {computer_id}",
                code="target_required",
            )
        method = computer.get("connection_method")
        if method == "node":
            if (
                computer.get("node_revoked_at") is not None
                or self.node_runs is None
                or "remote_run" not in self.node_runs.nodes.status(computer_id).capabilities
            ):
                raise RemoteRunError(
                    "This Remote does not support Remote Run",
                    code="capability_unsupported",
                )
        elif method != "ssh":
            raise RemoteRunError(
                "This Remote connection method is not supported yet",
                code="target_required",
            )
        return computer

    def _require_run_target(self, run: Mapping[str, Any]) -> dict[str, Any]:
        target = run.get("target")
        return target if isinstance(target, dict) else self._require_computer(
            str(run["target_computer_id"])
        )

    @staticmethod
    def _is_node_target(target: Mapping[str, Any]) -> bool:
        return target.get("connection_method") == "node"

    def _target_backend(self, target: Mapping[str, Any]) -> Any:
        if not self._is_node_target(target):
            return self.ssh
        if self.node_runs is None:
            raise RemoteRunError(
                "Node Remote Run is unavailable", code="capability_unsupported"
            )
        return self.node_runs

    def _bind_node_loop(self) -> None:
        if self.node_runs is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.node_runs.bind_loop(loop)
