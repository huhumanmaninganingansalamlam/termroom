from __future__ import annotations

import asyncio
import math
import os
import subprocess
import time
from collections import OrderedDict, defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

MAX_WORKSPACE_PANES = 4_096
MAX_PROCESS_ROWS = 200_000
DEFAULT_FRESH_SECONDS = 5.0
DEFAULT_RETRY_SECONDS = 5.0
DEFAULT_MAX_CACHE_ENTRIES = 256
WORKSPACE_USAGE_PANES_MARKER = "__TERMROOM_WORKSPACE_USAGE_PANES_V1__"
WORKSPACE_USAGE_PROCESSES_MARKER = "__TERMROOM_WORKSPACE_USAGE_PROCESSES_V1__"

WorkspaceUsageState = Literal["fresh", "stale", "unavailable", "offline"]


class WorkspaceUsageCollectionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        state: WorkspaceUsageState,
        code: str,
    ) -> None:
        super().__init__(message)
        self.state = state
        self.code = code


class WorkspaceUsageUnavailable(WorkspaceUsageCollectionError):
    def __init__(self, message: str, *, code: str = "measurement_unavailable") -> None:
        super().__init__(message, state="unavailable", code=code)


class WorkspaceUsageOffline(WorkspaceUsageCollectionError):
    def __init__(self, message: str = "Workspace Remote is offline") -> None:
        super().__init__(message, state="offline", code="remote_offline")


class WorkspaceUsageStale(WorkspaceUsageCollectionError):
    def __init__(self, message: str = "Workspace activity could not be refreshed") -> None:
        super().__init__(message, state="stale", code="refresh_incomplete")


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    pid: int
    parent_pid: int
    cpu_percent: float
    rss_kib: int


@dataclass(frozen=True, slots=True)
class RawWorkspaceUsage:
    cpu_percent: float
    memory_bytes: int
    process_count: int


@dataclass(frozen=True, slots=True)
class WorkspaceUsageSample:
    cpu_percent: float
    memory_bytes: int
    process_count: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class WorkspaceUsageView:
    state: WorkspaceUsageState
    sample: WorkspaceUsageSample | None
    last_observed_at: datetime | None
    last_checked_at: datetime
    reason: str | None = None

    def payload(self, *, now: datetime | None = None) -> dict[str, Any]:
        current_time = now or datetime.now(UTC)
        sample_payload: dict[str, int | float | str] | None = None
        if self.state == "fresh" and self.sample is not None:
            sample_payload = {
                "cpu_percent": round(self.sample.cpu_percent, 1),
                "memory_bytes": self.sample.memory_bytes,
                "process_count": self.sample.process_count,
                "observed_at": _iso_timestamp(self.sample.observed_at),
            }
        observed_at = self.sample.observed_at if self.sample is not None else self.last_observed_at
        age_seconds = None
        if observed_at is not None:
            age_seconds = max(0, int((current_time - observed_at).total_seconds()))
        return {
            "ok": True,
            "estimated": True,
            "state": self.state,
            "sample": sample_payload,
            "last_observed_at": _iso_timestamp(observed_at) if observed_at else None,
            "last_checked_at": _iso_timestamp(self.last_checked_at),
            "age_seconds": age_seconds,
            "reason": self.reason,
        }


@dataclass(slots=True)
class _CacheEntry:
    view: WorkspaceUsageView
    cached_at: float
    last_sample: WorkspaceUsageSample | None


class WorkspaceUsageService:
    """Bounded per-Workspace observation cache with shared in-flight refreshes."""

    def __init__(
        self,
        *,
        fresh_seconds: float = DEFAULT_FRESH_SECONDS,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
        max_entries: int = DEFAULT_MAX_CACHE_ENTRIES,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if fresh_seconds < 0 or retry_seconds < 0 or max_entries < 1:
            raise ValueError("Workspace usage cache settings are invalid")
        self.fresh_seconds = fresh_seconds
        self.retry_seconds = retry_seconds
        self.max_entries = max_entries
        self._monotonic = monotonic
        self._utc_now = utc_now
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[WorkspaceUsageView]] = {}
        self._lock = asyncio.Lock()

    async def observe(
        self,
        workspace_id: str,
        collector: Callable[[], Awaitable[RawWorkspaceUsage]],
    ) -> WorkspaceUsageView:
        if not workspace_id:
            raise ValueError("Workspace id is required")
        async with self._lock:
            entry = self._cache.get(workspace_id)
            if entry is not None:
                ttl = self.fresh_seconds if entry.view.state == "fresh" else self.retry_seconds
                if entry.view.state != "offline" and self._monotonic() - entry.cached_at <= ttl:
                    self._cache.move_to_end(workspace_id)
                    return entry.view
            task = self._inflight.get(workspace_id)
            if task is None:
                task = asyncio.create_task(self._collect(workspace_id, collector))
                self._inflight[workspace_id] = task
        return await asyncio.shield(task)

    async def record_failure(
        self,
        workspace_id: str,
        error: WorkspaceUsageCollectionError,
    ) -> WorkspaceUsageView:
        checked_at = self._utc_now()
        async with self._lock:
            previous = self._cache.get(workspace_id)
            last_sample = previous.last_sample if previous is not None else None
            view = self._failure_view(error, last_sample, checked_at=checked_at)
            self._store(workspace_id, view, last_sample=last_sample)
            return view

    async def _collect(
        self,
        workspace_id: str,
        collector: Callable[[], Awaitable[RawWorkspaceUsage]],
    ) -> WorkspaceUsageView:
        try:
            try:
                raw = await collector()
                raw = validate_raw_workspace_usage(raw)
            except WorkspaceUsageCollectionError as exc:
                checked_at = self._utc_now()
                async with self._lock:
                    previous = self._cache.get(workspace_id)
                    last_sample = previous.last_sample if previous is not None else None
                    view = self._failure_view(exc, last_sample, checked_at=checked_at)
                    self._store(workspace_id, view, last_sample=last_sample)
                    return view

            observed_at = self._utc_now()
            sample = WorkspaceUsageSample(
                cpu_percent=raw.cpu_percent,
                memory_bytes=raw.memory_bytes,
                process_count=raw.process_count,
                observed_at=observed_at,
            )
            view = WorkspaceUsageView(
                state="fresh",
                sample=sample,
                last_observed_at=sample.observed_at,
                last_checked_at=observed_at,
            )
            async with self._lock:
                self._store(workspace_id, view, last_sample=sample)
            return view
        finally:
            async with self._lock:
                if self._inflight.get(workspace_id) is asyncio.current_task():
                    self._inflight.pop(workspace_id, None)

    @staticmethod
    def _failure_view(
        error: WorkspaceUsageCollectionError,
        last_sample: WorkspaceUsageSample | None,
        *,
        checked_at: datetime,
    ) -> WorkspaceUsageView:
        state = error.state
        if state == "stale" and last_sample is None:
            state = "unavailable"
        return WorkspaceUsageView(
            state=state,
            sample=None,
            last_observed_at=(last_sample.observed_at if last_sample is not None else None),
            last_checked_at=checked_at,
            reason=error.code,
        )

    def _store(
        self,
        workspace_id: str,
        view: WorkspaceUsageView,
        *,
        last_sample: WorkspaceUsageSample | None,
    ) -> None:
        self._cache[workspace_id] = _CacheEntry(
            view=view,
            cached_at=self._monotonic(),
            last_sample=last_sample,
        )
        self._cache.move_to_end(workspace_id)
        while len(self._cache) > self.max_entries:
            self._cache.popitem(last=False)


def parse_pane_pids(output: str) -> set[int]:
    pane_pids: set[int] = set()
    for line in output.splitlines():
        value = line.strip()
        if not value:
            continue
        if not value.isdigit() or int(value) <= 0:
            raise WorkspaceUsageUnavailable(
                "tmux returned an invalid pane process", code="pane_snapshot_invalid"
            )
        pane_pids.add(int(value))
        if len(pane_pids) > MAX_WORKSPACE_PANES:
            raise WorkspaceUsageUnavailable(
                "Workspace has too many panes to measure", code="pane_snapshot_too_large"
            )
    if not pane_pids:
        raise WorkspaceUsageUnavailable(
            "Workspace has no measurable panes", code="pane_snapshot_empty"
        )
    return pane_pids


def parse_process_records(output: str) -> dict[int, ProcessRecord]:
    records: dict[int, ProcessRecord] = {}
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line.strip():
            continue
        if len(records) >= MAX_PROCESS_ROWS:
            raise WorkspaceUsageUnavailable(
                "Process snapshot is too large", code="process_snapshot_too_large"
            )
        parts = line.split()
        if len(parts) != 4:
            raise WorkspaceUsageUnavailable(
                f"Process snapshot row {line_number} is invalid",
                code="process_snapshot_invalid",
            )
        try:
            pid = int(parts[0])
            parent_pid = int(parts[1])
            cpu_percent = float(parts[2])
            rss_kib = int(parts[3])
        except ValueError as exc:
            raise WorkspaceUsageUnavailable(
                f"Process snapshot row {line_number} is invalid",
                code="process_snapshot_invalid",
            ) from exc
        if (
            pid <= 0
            or parent_pid < 0
            or not math.isfinite(cpu_percent)
            or cpu_percent < 0
            or rss_kib < 0
            or pid in records
        ):
            raise WorkspaceUsageUnavailable(
                f"Process snapshot row {line_number} is invalid",
                code="process_snapshot_invalid",
            )
        records[pid] = ProcessRecord(pid, parent_pid, cpu_percent, rss_kib)
    if not records:
        raise WorkspaceUsageUnavailable("Process snapshot is empty", code="process_snapshot_empty")
    return records


def aggregate_workspace_usage(
    pane_pids: Iterable[int], process_records: Mapping[int, ProcessRecord]
) -> RawWorkspaceUsage:
    roots = {int(pid) for pid in pane_pids if int(pid) > 0}
    if not roots:
        raise WorkspaceUsageUnavailable(
            "Workspace has no measurable panes", code="pane_snapshot_empty"
        )
    children: dict[int, set[int]] = defaultdict(set)
    for record in process_records.values():
        children[record.parent_pid].add(record.pid)

    pending = list(roots)
    selected: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in selected:
            continue
        selected.add(pid)
        pending.extend(children.get(pid, ()))

    measured = [process_records[pid] for pid in selected if pid in process_records]
    if not measured:
        raise WorkspaceUsageUnavailable(
            "Workspace processes changed during measurement",
            code="process_snapshot_incomplete",
        )
    return validate_raw_workspace_usage(
        RawWorkspaceUsage(
            cpu_percent=sum(record.cpu_percent for record in measured),
            memory_bytes=sum(record.rss_kib for record in measured) * 1024,
            process_count=len(measured),
        )
    )


def workspace_usage_from_outputs(pane_output: str, process_output: str) -> RawWorkspaceUsage:
    return aggregate_workspace_usage(
        parse_pane_pids(pane_output), parse_process_records(process_output)
    )


def split_remote_workspace_usage_output(output: str) -> tuple[str, str]:
    pane_marker = WORKSPACE_USAGE_PANES_MARKER + "\n"
    process_marker = WORKSPACE_USAGE_PROCESSES_MARKER + "\n"
    if not output.startswith(pane_marker) or output.count(process_marker) != 1:
        raise WorkspaceUsageUnavailable(
            "Remote process snapshot is invalid", code="process_snapshot_invalid"
        )
    pane_output, process_output = output[len(pane_marker) :].split(process_marker, 1)
    return pane_output, process_output


def read_system_process_output() -> str:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith("TERMROOM_"):
            environment.pop(key, None)
    environment["LC_ALL"] = "C"
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,pcpu=,rss="],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
    except FileNotFoundError as exc:
        raise WorkspaceUsageUnavailable("ps is not installed", code="process_tool_missing") from exc
    if result.returncode:
        raise WorkspaceUsageUnavailable(
            result.stderr.strip() or "ps could not read process activity",
            code="process_snapshot_failed",
        )
    return result.stdout


def raw_workspace_usage_payload(value: RawWorkspaceUsage) -> dict[str, int | float]:
    usage = validate_raw_workspace_usage(value)
    return {
        "cpu_percent": usage.cpu_percent,
        "memory_bytes": usage.memory_bytes,
        "process_count": usage.process_count,
    }


def parse_raw_workspace_usage(value: object) -> RawWorkspaceUsage:
    if not isinstance(value, Mapping):
        raise WorkspaceUsageUnavailable(
            "Workspace activity response is invalid", code="response_invalid"
        )
    cpu_percent = value.get("cpu_percent")
    memory_bytes = value.get("memory_bytes")
    process_count = value.get("process_count")
    if (
        isinstance(cpu_percent, bool)
        or not isinstance(cpu_percent, (int, float))
        or isinstance(memory_bytes, bool)
        or not isinstance(memory_bytes, int)
        or isinstance(process_count, bool)
        or not isinstance(process_count, int)
    ):
        raise WorkspaceUsageUnavailable(
            "Workspace activity response is invalid", code="response_invalid"
        )
    return validate_raw_workspace_usage(
        RawWorkspaceUsage(float(cpu_percent), memory_bytes, process_count)
    )


def validate_raw_workspace_usage(value: RawWorkspaceUsage) -> RawWorkspaceUsage:
    if (
        not isinstance(value, RawWorkspaceUsage)
        or isinstance(value.cpu_percent, bool)
        or not isinstance(value.cpu_percent, (int, float))
        or not math.isfinite(value.cpu_percent)
        or value.cpu_percent < 0
        or value.cpu_percent > 1_000_000
        or isinstance(value.memory_bytes, bool)
        or not isinstance(value.memory_bytes, int)
        or value.memory_bytes < 0
        or value.memory_bytes > 2**63 - 1
        or isinstance(value.process_count, bool)
        or not isinstance(value.process_count, int)
        or value.process_count < 0
        or value.process_count > MAX_PROCESS_ROWS
    ):
        raise WorkspaceUsageUnavailable(
            "Workspace activity response is invalid", code="response_invalid"
        )
    return value


def _iso_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
