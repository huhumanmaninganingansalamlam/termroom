from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import tempfile
import zipfile
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from termroom.files import (
    DirectoryListingLimitError,
    FileConflictError,
    FileEntry,
    FileService,
    FileSnapshot,
    UnsupportedFileError,
)
from termroom.remote_access import RemoteAccess
from termroom.remote_runs import TERMINAL_STATES, RemoteRunManager
from termroom.run_sources import (
    WorkspaceEntry,
    is_default_workspace_excluded,
    normalize_source_relative_path,
)
from termroom.security import PathBoundaryError, file_digest
from termroom.workspaces import WorkspaceManager

MAX_RESULT_ENTRIES = 10_000
MAX_RESULT_DEPTH = 64
MAX_RESULT_PATH_BYTES = 4_096
MAX_RESULT_LISTING_METADATA_BYTES = 768 * 1024
MAX_COLLECTION_DISPLAY_ITEMS = 500
TRANSFER_CHUNK_BYTES = 1024 * 1024

PlanStatus = Literal["ready", "already_result", "conflict", "skipped"]
PlanChange = Literal["modified", "added", "deleted", "unchanged", "unsupported"]
ApplyOutcome = Literal["applied", "already_result", "conflict", "skipped", "failed"]

_SOURCE_UNSUPPORTED_EXCEPTIONS = (
    NotADirectoryError,
    PathBoundaryError,
    UnsupportedFileError,
)
_SOURCE_MISSING_OR_UNSUPPORTED_EXCEPTIONS = (
    FileNotFoundError,
    *_SOURCE_UNSUPPORTED_EXCEPTIONS,
)
_APPLY_CONFLICT_EXCEPTIONS = (
    FileConflictError,
    FileExistsError,
    *_SOURCE_MISSING_OR_UNSUPPORTED_EXCEPTIONS,
)


def _write_archive_entry(
    archive: zipfile.ZipFile,
    staged: Path,
    relative_path: str,
) -> None:
    info = zipfile.ZipInfo(relative_path)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (0o100644 & 0xFFFF) << 16
    with staged.open("rb") as source, archive.open(
        info, mode="w", force_zip64=True
    ) as destination:
        while chunk := source.read(TRANSFER_CHUNK_BYTES):
            destination.write(chunk)


def _flush_and_sync(handle: Any) -> None:
    handle.flush()
    os.fsync(handle.fileno())


class ResultCollectionError(RuntimeError):
    """A stable Remote Run result collection failure."""

    def __init__(self, message: str, *, code: str = "result_collection_error") -> None:
        super().__init__(message)
        self.code = code


class ResultCollectionConflict(ResultCollectionError):
    """The reviewed collection inputs no longer match the requested mutation."""


@dataclass(frozen=True, slots=True)
class CollectionPlanItem:
    path: str
    change: PlanChange
    status: PlanStatus
    reason: str
    baseline_digest: str | None = None
    result_digest: str | None = None
    current_digest: str | None = None
    current_mtime_ns: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "change": self.change,
            "status": self.status,
            "reason": self.reason,
            "baseline_digest": self.baseline_digest,
            "result_digest": self.result_digest,
            "current_digest": self.current_digest,
            "current_mtime_ns": self.current_mtime_ns,
        }


@dataclass(frozen=True, slots=True)
class CollectionPlan:
    run_id: str
    source_workspace_id: str
    source_path: str
    revision: str
    items: tuple[CollectionPlanItem, ...]

    @property
    def changed_item_count(self) -> int:
        return sum(
            item.change != "unchanged" and item.status != "already_result"
            for item in self.items
        )

    @property
    def apply_allowed(self) -> bool:
        return self.changed_item_count <= MAX_COLLECTION_DISPLAY_ITEMS

    def as_dict(self) -> dict[str, Any]:
        counts = Counter(item.status for item in self.items)
        changed = tuple(
            item
            for item in self.items
            if item.change != "unchanged" and item.status != "already_result"
        )
        shown = changed[:MAX_COLLECTION_DISPLAY_ITEMS]
        omitted = len(changed) - len(shown)
        return {
            "run_id": self.run_id,
            "source_workspace_id": self.source_workspace_id,
            "source_path": self.source_path,
            "revision": self.revision,
            "items": [item.as_dict() for item in shown],
            "total_items": len(self.items),
            "changed_items": len(changed),
            "unchanged_items": len(self.items) - len(changed),
            "shown_items": len(shown),
            "items_truncated": omitted > 0,
            "omitted_items": omitted,
            "apply_allowed": self.apply_allowed,
            "summary": {
                "ready": counts["ready"],
                "already_result": counts["already_result"],
                "conflict": counts["conflict"],
                "skipped": counts["skipped"],
            },
        }


@dataclass(frozen=True, slots=True)
class CollectionReportItem:
    path: str
    change: PlanChange
    outcome: ApplyOutcome
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "change": self.change,
            "outcome": self.outcome,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CollectionReport:
    run_id: str
    plan_revision: str
    items: tuple[CollectionReportItem, ...]

    def as_dict(self) -> dict[str, Any]:
        counts = Counter(item.outcome for item in self.items)
        changed = tuple(item for item in self.items if item.outcome != "already_result")
        shown = changed[:MAX_COLLECTION_DISPLAY_ITEMS]
        omitted = len(changed) - len(shown)
        return {
            "run_id": self.run_id,
            "plan_revision": self.plan_revision,
            "items": [item.as_dict() for item in shown],
            "total_items": len(self.items),
            "changed_items": len(changed),
            "unchanged_items": len(self.items) - len(changed),
            "shown_items": len(shown),
            "items_truncated": omitted > 0,
            "omitted_items": omitted,
            "summary": {
                "applied": counts["applied"],
                "already_result": counts["already_result"],
                "conflict": counts["conflict"],
                "skipped": counts["skipped"],
                "failed": counts["failed"],
            },
        }


@dataclass(frozen=True, slots=True)
class _ScannedTree:
    entries: tuple[FileEntry, ...]
    total_file_bytes: int

    @property
    def files(self) -> tuple[FileEntry, ...]:
        return tuple(entry for entry in self.entries if not entry.is_dir)

    @property
    def paths(self) -> frozenset[str]:
        return frozenset(entry.relative_path for entry in self.entries)


@dataclass(slots=True)
class _PlanBuild:
    plan: CollectionPlan
    run: dict[str, Any]
    source_workspace: dict[str, Any]
    result_workspace: dict[str, Any]
    result_entries: dict[str, FileEntry]
    current_snapshots: dict[str, _ExpectedSource]


@dataclass(frozen=True, slots=True)
class _ExpectedSource:
    digest: str
    mtime_ns: int


class RemoteRunResultCollector:
    """Download and explicitly collect one terminal Remote Run result tree."""

    def __init__(
        self,
        remote_runs: RemoteRunManager,
        workspaces: WorkspaceManager,
        remote_access: RemoteAccess,
        files: FileService,
        *,
        state_dir: Path,
        max_archive_bytes: int,
    ) -> None:
        if max_archive_bytes <= 0:
            raise ValueError("max_archive_bytes must be positive")
        self.remote_runs = remote_runs
        self.workspaces = workspaces
        self.remote_access = remote_access
        self.files = files
        self.max_archive_bytes = max_archive_bytes
        self.download_root = (state_dir / "remote-run-result-downloads").resolve()
        self.download_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._source_apply_locks: dict[str, asyncio.Lock] = {}
        with contextlib.suppress(PermissionError):
            self.download_root.chmod(0o700)

    async def create_archive(self, run_id: str) -> Path:
        """Build a private temporary ZIP whose returned Path the caller must unlink."""

        _run, workspace = await self._result_workspace(run_id)
        tree = await self._scan_tree(workspace)
        descriptor, raw_path = await asyncio.to_thread(
            tempfile.mkstemp,
            prefix=f"{run_id}-",
            suffix=".zip",
            dir=self.download_root,
        )
        archive_path = Path(raw_path)
        await asyncio.to_thread(os.close, descriptor)
        archive: zipfile.ZipFile | None = None
        try:
            archive = await asyncio.to_thread(
                zipfile.ZipFile,
                archive_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                allowZip64=True,
            )
            try:
                for entry in tree.files:
                    staged = await self._stage_stable_file(workspace, entry)
                    if staged is None:
                        continue
                    try:
                        await asyncio.to_thread(
                            _write_archive_entry,
                            archive,
                            staged,
                            entry.relative_path,
                        )
                    finally:
                        await asyncio.to_thread(staged.unlink, missing_ok=True)
            finally:
                await asyncio.to_thread(archive.close)
                archive = None
            await asyncio.to_thread(os.chmod, archive_path, 0o600)
            return archive_path
        except BaseException:
            if archive is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(archive.close)
            await asyncio.to_thread(archive_path.unlink, missing_ok=True)
            raise

    async def review(self, run_id: str) -> CollectionPlan:
        """Return a content-free, canonical review plan for a Workspace Source."""

        return (await self._build_plan(run_id, retain_current_snapshots=False)).plan

    async def apply(self, run_id: str, revision: str) -> CollectionReport:
        """Apply only a freshly reviewed plan through conditional text writes."""

        run = await asyncio.to_thread(self.remote_runs.get, run_id)
        source_workspace_id = str(run.get("source_workspace_id") or "")
        lock_key = source_workspace_id or f"run:{run_id}"
        lock = self._source_apply_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            return await self._apply_locked(run_id, revision)

    async def _apply_locked(self, run_id: str, revision: str) -> CollectionReport:
        built = await self._build_plan(run_id, retain_current_snapshots=True)
        if not revision or revision != built.plan.revision:
            raise ResultCollectionConflict(
                "Remote Run collection inputs changed; review the plan again",
                code="plan_revision_mismatch",
            )
        if not built.plan.apply_allowed:
            raise ResultCollectionError(
                "Remote Run result contains too many changes to review safely",
                code="collection_too_many_changes",
            )

        reports: list[CollectionReportItem] = []
        for item in built.plan.items:
            if item.status != "ready":
                outcome: ApplyOutcome = (
                    "already_result" if item.status == "already_result" else item.status
                )
                reports.append(
                    CollectionReportItem(item.path, item.change, outcome, item.reason)
                )
                continue

            source_relative = self._source_relative_path(built.run, item.path)
            try:
                content = await self._ready_result_content(built, item)
                if item.change == "modified":
                    current = built.current_snapshots[item.path]
                    await self._write_existing(
                        built.source_workspace,
                        source_relative,
                        content,
                        current,
                    )
                elif item.change == "added":
                    await self._write_new(
                        built.source_workspace,
                        source_relative,
                        content,
                    )
                else:  # pragma: no cover - guarded by plan construction
                    raise AssertionError(f"Unexpected ready change: {item.change}")
            except ResultCollectionConflict:
                reports.append(
                    CollectionReportItem(
                        item.path,
                        item.change,
                        "conflict",
                        "result_changed_during_apply",
                    )
                )
            except _APPLY_CONFLICT_EXCEPTIONS:
                reports.append(
                    CollectionReportItem(
                        item.path,
                        item.change,
                        "conflict",
                        "source_changed_during_apply",
                    )
                )
            except Exception:
                reports.append(
                    CollectionReportItem(item.path, item.change, "failed", "apply_failed")
                )
            else:
                reports.append(
                    CollectionReportItem(item.path, item.change, "applied", "applied")
                )

        return CollectionReport(run_id, built.plan.revision, tuple(reports))

    async def _ready_result_content(
        self, built: _PlanBuild, item: CollectionPlanItem
    ) -> str:
        entry = built.result_entries.get(item.path)
        if entry is None or entry.is_dir or not item.result_digest:
            raise ResultCollectionConflict(
                "Remote Run result changed while it was applied",
                code="result_changed",
            )
        raw = await self._read_stable_file(built.result_workspace, entry)
        if file_digest(raw) != item.result_digest or b"\x00" in raw:
            raise ResultCollectionConflict(
                "Remote Run result changed while it was applied",
                code="result_changed",
            )
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ResultCollectionConflict(
                "Remote Run result changed while it was applied",
                code="result_changed",
            ) from exc

    async def _result_workspace(
        self, run_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        run = await asyncio.to_thread(self.remote_runs.get, run_id)
        if str(run.get("state") or "") not in TERMINAL_STATES:
            raise ResultCollectionError(
                "Remote Run results are available after the command ends",
                code="result_not_ready",
            )
        run = await asyncio.to_thread(self.remote_runs.ensure_workspace_bridge, run_id)
        workspace_id = str(run.get("workspace_id") or "")
        if not workspace_id:
            raise ResultCollectionError(
                "Remote Run result Workspace is unavailable",
                code="result_workspace_unavailable",
            )
        try:
            workspace = await asyncio.to_thread(self.workspaces.require, workspace_id)
        except KeyError as exc:
            raise ResultCollectionError(
                "Remote Run result Workspace is unavailable",
                code="result_workspace_unavailable",
            ) from exc
        return run, workspace

    async def _scan_tree(self, workspace: dict[str, Any]) -> _ScannedTree:
        pending: list[tuple[str, int]] = [(".", 0)]
        seen: set[str] = set()
        entries: list[FileEntry] = []
        total_bytes = 0

        while pending:
            directory, depth = pending.pop()
            remaining = MAX_RESULT_ENTRIES - len(seen)
            try:
                _resolved, children = await self.remote_access.list_dir(
                    workspace,
                    directory,
                    max_entries=remaining,
                    max_metadata_bytes=MAX_RESULT_LISTING_METADATA_BYTES,
                )
            except DirectoryListingLimitError as exc:
                raise ResultCollectionError(
                    "Remote Run result contains too many entries",
                    code="result_too_many_entries",
                ) from exc
            for entry in children:
                relative = self._validate_child_entry(directory, entry)
                if relative in seen:
                    raise ResultCollectionError(
                        "Remote Run result contains a duplicate path",
                        code="result_path_duplicate",
                    )
                seen.add(relative)
                if len(seen) > MAX_RESULT_ENTRIES:
                    raise ResultCollectionError(
                        "Remote Run result contains too many entries",
                        code="result_too_many_entries",
                    )
                canonical = FileEntry(
                    name=PurePosixPath(relative).name,
                    relative_path=relative,
                    is_dir=entry.is_dir,
                    size=entry.size,
                    mtime_ns=entry.mtime_ns,
                )
                entries.append(canonical)
                if canonical.is_dir:
                    next_depth = depth + 1
                    if next_depth > MAX_RESULT_DEPTH:
                        raise ResultCollectionError(
                            "Remote Run result directory nesting is too deep",
                            code="result_too_deep",
                        )
                    pending.append((relative, next_depth))
                else:
                    total_bytes += canonical.size
                    if total_bytes > self.max_archive_bytes:
                        raise ResultCollectionError(
                            "Remote Run result exceeds the collection size limit",
                            code="result_too_large",
                        )

        entries.sort(key=lambda item: item.relative_path)
        return _ScannedTree(tuple(entries), total_bytes)

    @staticmethod
    def _validate_child_entry(directory: str, entry: FileEntry) -> str:
        if type(entry.size) is not int or entry.size < 0:
            raise ResultCollectionError(
                "Remote Run result contains invalid file metadata",
                code="result_metadata_invalid",
            )
        if type(entry.mtime_ns) is not int or entry.mtime_ns < 0:
            raise ResultCollectionError(
                "Remote Run result contains invalid file metadata",
                code="result_metadata_invalid",
            )
        try:
            relative = normalize_source_relative_path(
                entry.relative_path,
                allow_metadata=True,
            )
        except (TypeError, ValueError) as exc:
            raise ResultCollectionError(
                "Remote Run result contains an unsafe path",
                code="result_path_invalid",
            ) from exc
        if len(relative.encode("utf-8")) > MAX_RESULT_PATH_BYTES:
            raise ResultCollectionError(
                "Remote Run result path is too long",
                code="result_path_invalid",
            )
        expected_parent = PurePosixPath(relative).parent.as_posix()
        if expected_parent != directory:
            raise ResultCollectionError(
                "Remote Run result listing crossed a directory boundary",
                code="result_path_invalid",
            )
        return relative

    async def _stage_stable_file(
        self, workspace: dict[str, Any], entry: FileEntry
    ) -> Path | None:
        descriptor, raw_path = await asyncio.to_thread(
            tempfile.mkstemp,
            prefix=".file-",
            dir=self.download_root,
        )
        staged = Path(raw_path)
        output = None
        try:
            unsupported = False
            output = os.fdopen(descriptor, "wb")

            async def write_chunk(chunk: bytes) -> None:
                assert output is not None
                await asyncio.to_thread(output.write, chunk)

            try:
                await self._stream_stable_file(workspace, entry, write_chunk)
            except UnsupportedFileError:
                unsupported = True
            if not unsupported:
                await asyncio.to_thread(_flush_and_sync, output)
            await asyncio.to_thread(output.close)
            output = None
            if unsupported:
                await asyncio.to_thread(staged.unlink, missing_ok=True)
                return None
            return staged
        except BaseException:
            if output is not None:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(output.close)
            else:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(os.close, descriptor)
            await asyncio.to_thread(staged.unlink, missing_ok=True)
            raise

    async def _read_stable_file(
        self, workspace: dict[str, Any], entry: FileEntry
    ) -> bytes:
        content = bytearray()

        async def append(chunk: bytes) -> None:
            content.extend(chunk)

        await self._stream_stable_file(workspace, entry, append)
        return bytes(content)

    async def _stream_stable_file(
        self,
        workspace: dict[str, Any],
        entry: FileEntry,
        write: Callable[[bytes], Awaitable[None]],
    ) -> None:
        before = await self.remote_access.stat(workspace, entry.relative_path)
        self._require_same_file_entry(entry, before)
        total = 0
        async for chunk in self.remote_access.download_stream(
            workspace, entry.relative_path
        ):
            if not isinstance(chunk, bytes):
                raise ResultCollectionError(
                    "Remote Run result returned invalid file data",
                    code="result_read_invalid",
                )
            total += len(chunk)
            if total > entry.size:
                raise ResultCollectionConflict(
                    "Remote Run result changed while it was collected",
                    code="result_changed",
                )
            if chunk:
                await write(chunk)
        after = await self.remote_access.stat(workspace, entry.relative_path)
        self._require_same_file_entry(entry, after)
        if total != entry.size:
            raise ResultCollectionConflict(
                "Remote Run result changed while it was collected",
                code="result_changed",
            )

    @staticmethod
    def _require_same_file_entry(expected: FileEntry, current: FileEntry) -> None:
        if (
            current.is_dir
            or current.relative_path != expected.relative_path
            or current.size != expected.size
            or current.mtime_ns != expected.mtime_ns
        ):
            raise ResultCollectionConflict(
                "Remote Run result changed while it was collected",
                code="result_changed",
            )

    async def _build_plan(
        self, run_id: str, *, retain_current_snapshots: bool
    ) -> _PlanBuild:
        run, result_workspace = await self._result_workspace(run_id)
        if run.get("source_kind") != "workspace":
            raise ResultCollectionError(
                "Only Workspace Source runs can collect changes",
                code="source_collection_unsupported",
            )
        baseline = await asyncio.to_thread(
            self.remote_runs.collection_manifest,
            run_id,
        )
        if baseline is None:
            raise ResultCollectionError(
                "This Remote Run has no compatible Source baseline",
                code="collection_baseline_unavailable",
            )
        source_workspace_id = str(run.get("source_workspace_id") or "")
        try:
            source_workspace = await asyncio.to_thread(
                self.workspaces.require,
                source_workspace_id,
            )
        except KeyError as exc:
            raise ResultCollectionError(
                "The Source Workspace is no longer available",
                code="source_workspace_unavailable",
            ) from exc
        if source_workspace.get("transient") or source_workspace.get("is_remote_run"):
            raise ResultCollectionError(
                "The Source Workspace is not persistent",
                code="source_workspace_unavailable",
            )

        tree = await self._scan_tree(result_workspace)
        result_entries = {entry.relative_path: entry for entry in tree.entries}
        baseline_entries = {entry.relative_path: entry for entry in baseline.entries}
        current_snapshots: dict[str, _ExpectedSource] = {}
        items: list[CollectionPlanItem] = []

        for path, result_entry in sorted(result_entries.items()):
            baseline_entry = baseline_entries.get(path)
            if self._is_excluded_result_path(path, baseline.excluded_prefixes):
                if not result_entry.is_dir:
                    items.append(
                        self._skipped_item(path, baseline_entry, "excluded_new_path")
                    )
                continue
            if result_entry.is_dir:
                if baseline_entry is not None and baseline_entry.kind == "file":
                    items.append(
                        CollectionPlanItem(
                            path,
                            "unsupported",
                            "conflict",
                            "result_type_conflict",
                            baseline_digest=baseline_entry.digest,
                        )
                    )
                continue
            if result_entry.size > self.files.max_edit_bytes:
                items.append(
                    self._skipped_item(path, baseline_entry, "file_too_large")
                )
                continue
            try:
                raw = await self._read_stable_file(result_workspace, result_entry)
            except UnsupportedFileError:
                items.append(
                    self._skipped_item(path, baseline_entry, "unsupported_result_type")
                )
                continue
            if b"\x00" in raw:
                items.append(self._skipped_item(path, baseline_entry, "binary_result"))
                continue
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                items.append(self._skipped_item(path, baseline_entry, "non_utf8_result"))
                continue
            result_digest = file_digest(raw)

            if baseline_entry is not None and baseline_entry.kind != "file":
                items.append(
                    CollectionPlanItem(
                        path,
                        "unsupported",
                        "conflict",
                        "baseline_type_conflict",
                        result_digest=result_digest,
                    )
                )
                continue
            if baseline_entry is None and is_default_workspace_excluded(path):
                items.append(
                    CollectionPlanItem(
                        path,
                        "added",
                        "skipped",
                        "excluded_new_path",
                        result_digest=result_digest,
                    )
                )
                continue

            current = await self._read_current_source(run, source_workspace, path)
            if isinstance(current, str):
                items.append(
                    CollectionPlanItem(
                        path,
                        "modified" if baseline_entry else "added",
                        "conflict",
                        current,
                        baseline_digest=baseline_entry.digest if baseline_entry else None,
                        result_digest=result_digest,
                    )
                )
                continue
            if current is not None:
                if retain_current_snapshots:
                    current_snapshots[path] = _ExpectedSource(
                        current.digest, current.mtime_ns
                    )
                if current.digest == result_digest:
                    items.append(
                        CollectionPlanItem(
                            path,
                            "unchanged",
                            "already_result",
                            "source_matches_result",
                            baseline_digest=(
                                baseline_entry.digest if baseline_entry else None
                            ),
                            result_digest=result_digest,
                            current_digest=current.digest,
                            current_mtime_ns=current.mtime_ns,
                        )
                    )
                    continue

            if baseline_entry is None:
                if current is not None:
                    items.append(
                        CollectionPlanItem(
                            path,
                            "added",
                            "conflict",
                            "source_path_exists",
                            result_digest=result_digest,
                            current_digest=current.digest,
                            current_mtime_ns=current.mtime_ns,
                        )
                    )
                elif not await self._addition_parent_exists(
                    run, source_workspace, path
                ):
                    items.append(
                        CollectionPlanItem(
                            path,
                            "added",
                            "conflict",
                            "source_parent_missing",
                            result_digest=result_digest,
                        )
                    )
                else:
                    items.append(
                        CollectionPlanItem(
                            path,
                            "added",
                            "ready",
                            (
                                "new_local_text_file"
                                if source_workspace.get("backend_kind") == "local"
                                else "new_remote_text_file"
                            ),
                            result_digest=result_digest,
                        )
                    )
                continue

            baseline_digest = baseline_entry.digest
            if baseline_digest is None:
                raise ResultCollectionError(
                    "Remote Run Source baseline is incomplete",
                    code="collection_baseline_unavailable",
                )
            if current is None:
                items.append(
                    CollectionPlanItem(
                        path,
                        "modified",
                        "conflict",
                        "source_file_missing",
                        baseline_digest=baseline_digest,
                        result_digest=result_digest,
                    )
                )
            elif current.digest != baseline_digest:
                items.append(
                    CollectionPlanItem(
                        path,
                        "modified",
                        "conflict",
                        "source_changed_since_run",
                        baseline_digest=baseline_digest,
                        result_digest=result_digest,
                        current_digest=current.digest,
                        current_mtime_ns=current.mtime_ns,
                    )
                )
            elif result_digest == baseline_digest:
                items.append(
                    CollectionPlanItem(
                        path,
                        "unchanged",
                        "already_result",
                        "result_matches_baseline",
                        baseline_digest=baseline_digest,
                        result_digest=result_digest,
                        current_digest=current.digest,
                        current_mtime_ns=current.mtime_ns,
                    )
                )
            else:
                items.append(
                    CollectionPlanItem(
                        path,
                        "modified",
                        "ready",
                        "source_unchanged_since_run",
                        baseline_digest=baseline_digest,
                        result_digest=result_digest,
                        current_digest=current.digest,
                        current_mtime_ns=current.mtime_ns,
                    )
                )

        result_paths = tree.paths
        for entry in baseline.entries:
            if entry.kind == "file" and entry.relative_path not in result_paths:
                items.append(
                    CollectionPlanItem(
                        entry.relative_path,
                        "deleted",
                        "skipped",
                        "deletion_not_applied",
                        baseline_digest=entry.digest,
                    )
                )

        ordered = tuple(sorted(items, key=lambda item: item.path))
        source_path = str(run.get("source_path") or ".")
        revision = self._plan_revision(
            run_id,
            source_workspace_id,
            source_path,
            ordered,
        )
        plan = CollectionPlan(
            run_id,
            source_workspace_id,
            source_path,
            revision,
            ordered,
        )
        return _PlanBuild(
            plan,
            run,
            source_workspace,
            result_workspace,
            result_entries,
            current_snapshots,
        )

    @staticmethod
    def _skipped_item(
        path: str, baseline: WorkspaceEntry | None, reason: str
    ) -> CollectionPlanItem:
        return CollectionPlanItem(
            path,
            "modified" if baseline and baseline.kind == "file" else "added",
            "skipped",
            reason,
            baseline_digest=baseline.digest if baseline else None,
        )

    @staticmethod
    def _is_excluded_result_path(
        path: str,
        excluded_prefixes: tuple[str, ...],
    ) -> bool:
        if ".termroom" in path.split("/"):
            return True
        return any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in excluded_prefixes
        )

    async def _read_current_source(
        self,
        run: dict[str, Any],
        workspace: dict[str, Any],
        result_path: str,
    ) -> FileSnapshot | str | None:
        relative = self._source_relative_path(run, result_path)
        try:
            before = await self._source_stat(workspace, relative)
        except FileNotFoundError:
            return None
        except _SOURCE_UNSUPPORTED_EXCEPTIONS:
            return "source_path_unsupported"
        if before.is_dir:
            return "source_path_is_directory"
        try:
            snapshot = await self._source_read_text(workspace, relative)
            after = await self._source_stat(workspace, relative)
        except FileNotFoundError:
            return "source_changed_during_review"
        except _SOURCE_UNSUPPORTED_EXCEPTIONS:
            return "source_not_editable_text"
        if (
            before.relative_path != after.relative_path
            or before.size != after.size
            or before.mtime_ns != after.mtime_ns
            or snapshot.mtime_ns != before.mtime_ns
            or len(snapshot.content.encode("utf-8")) != before.size
        ):
            return "source_changed_during_review"
        return snapshot

    async def _addition_parent_exists(
        self,
        run: dict[str, Any],
        workspace: dict[str, Any],
        result_path: str,
    ) -> bool:
        relative = self._source_relative_path(run, result_path)
        parent = PurePosixPath(relative).parent.as_posix()
        if parent == ".":
            return True
        try:
            entry = await self._source_stat(workspace, parent)
        except _SOURCE_MISSING_OR_UNSUPPORTED_EXCEPTIONS:
            return False
        return entry.is_dir

    async def _source_stat(
        self, workspace: dict[str, Any], relative_path: str
    ) -> FileEntry:
        if workspace.get("backend_kind") == "local":
            return await asyncio.to_thread(
                self.files.stat, Path(workspace["path"]), relative_path
            )
        return await self.remote_access.stat(workspace, relative_path)

    async def _source_read_text(
        self, workspace: dict[str, Any], relative_path: str
    ) -> FileSnapshot:
        if workspace.get("backend_kind") == "local":
            return await asyncio.to_thread(
                self.files.read_text, Path(workspace["path"]), relative_path
            )
        return await self.remote_access.read_text(
            workspace,
            relative_path,
            self.files.max_edit_bytes,
        )

    async def _write_existing(
        self,
        workspace: dict[str, Any],
        relative_path: str,
        content: str,
        current: _ExpectedSource,
    ) -> None:
        if workspace.get("backend_kind") == "local":
            await asyncio.to_thread(
                self.files.write_text,
                Path(workspace["path"]),
                relative_path,
                content,
                expected_digest=current.digest,
                expected_mtime_ns=current.mtime_ns,
            )
            return
        await self.remote_access.write_text(
            workspace,
            relative_path,
            content,
            expected_digest=current.digest,
            expected_mtime_ns=current.mtime_ns,
            max_bytes=self.files.max_edit_bytes,
        )

    async def _write_new(
        self,
        workspace: dict[str, Any],
        relative_path: str,
        content: str,
    ) -> None:
        if workspace.get("backend_kind") == "local":
            await asyncio.to_thread(
                self.files.write_new_text,
                Path(workspace["path"]),
                relative_path,
                content,
            )
            return

        path = PurePosixPath(relative_path)
        parent = path.parent.as_posix()
        encoded = content.encode("utf-8")

        async def chunks() -> AsyncIterator[bytes]:
            if encoded:
                yield encoded

        await self.remote_access.upload_stream(
            workspace,
            parent,
            path.name,
            chunks(),
            overwrite=False,
            max_bytes=self.files.max_edit_bytes,
        )

    @staticmethod
    def _source_relative_path(run: dict[str, Any], result_path: str) -> str:
        source_path = str(run.get("source_path") or ".")
        if source_path == ".":
            return result_path
        return normalize_source_relative_path(f"{source_path}/{result_path}")

    @staticmethod
    def _plan_revision(
        run_id: str,
        source_workspace_id: str,
        source_path: str,
        items: tuple[CollectionPlanItem, ...],
    ) -> str:
        payload = {
            "version": 1,
            "run_id": run_id,
            "source_workspace_id": source_workspace_id,
            "source_path": source_path,
            "items": [item.as_dict() for item in items],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
