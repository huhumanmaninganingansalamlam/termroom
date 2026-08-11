from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from termroom.workspace_usage import (
    ProcessRecord,
    RawWorkspaceUsage,
    WorkspaceUsageOffline,
    WorkspaceUsageService,
    WorkspaceUsageStale,
    WorkspaceUsageUnavailable,
    aggregate_workspace_usage,
    parse_pane_pids,
    parse_process_records,
    parse_raw_workspace_usage,
    raw_workspace_usage_payload,
    split_remote_workspace_usage_output,
)


def test_workspace_usage_recursively_aggregates_and_deduplicates_pane_processes() -> None:
    records = {
        100: ProcessRecord(100, 1, 1.0, 10),
        101: ProcessRecord(101, 100, 2.0, 20),
        102: ProcessRecord(102, 101, 3.0, 30),
        200: ProcessRecord(200, 1, 4.0, 40),
        300: ProcessRecord(300, 1, 90.0, 900),
    }

    usage = aggregate_workspace_usage([100, 101, 200, 200], records)

    assert usage == RawWorkspaceUsage(
        cpu_percent=10.0,
        memory_bytes=100 * 1024,
        process_count=4,
    )


def test_workspace_usage_parsers_reject_ambiguous_or_unbounded_snapshots() -> None:
    assert parse_pane_pids("42\n42\n") == {42}
    assert parse_process_records("42 1 0.5 128\n") == {42: ProcessRecord(42, 1, 0.5, 128)}
    with pytest.raises(WorkspaceUsageUnavailable) as exc_info:
        parse_pane_pids("not-a-pid\n")
    assert exc_info.value.code == "pane_snapshot_invalid"
    with pytest.raises(WorkspaceUsageUnavailable) as exc_info:
        parse_process_records("42 1 nan 128\n")
    assert exc_info.value.code == "process_snapshot_invalid"
    with pytest.raises(WorkspaceUsageUnavailable) as exc_info:
        parse_process_records("42 1 0.5 128\n42 1 0.5 128\n")
    assert exc_info.value.code == "process_snapshot_invalid"


def test_remote_workspace_usage_envelope_and_typed_payload_fail_closed() -> None:
    panes, processes = split_remote_workspace_usage_output(
        "__TERMROOM_WORKSPACE_USAGE_PANES_V1__\n"
        "42\n"
        "__TERMROOM_WORKSPACE_USAGE_PROCESSES_V1__\n"
        "42 1 0.5 128\n"
    )
    assert panes == "42\n"
    assert processes == "42 1 0.5 128\n"

    raw = RawWorkspaceUsage(12.5, 1024, 2)
    assert parse_raw_workspace_usage(raw_workspace_usage_payload(raw)) == raw
    for invalid in (
        {"cpu_percent": float("nan"), "memory_bytes": 1, "process_count": 1},
        {"cpu_percent": 1, "memory_bytes": True, "process_count": 1},
        {"cpu_percent": 1, "memory_bytes": 1, "process_count": -1},
    ):
        with pytest.raises(WorkspaceUsageUnavailable) as exc_info:
            parse_raw_workspace_usage(invalid)
        assert exc_info.value.code == "response_invalid"


async def test_workspace_usage_cache_deduplicates_concurrent_observers() -> None:
    service = WorkspaceUsageService(fresh_seconds=60)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def collect() -> RawWorkspaceUsage:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return RawWorkspaceUsage(3.0, 4096, 2)

    first = asyncio.create_task(service.observe("workspace", collect))
    await started.wait()
    second = asyncio.create_task(service.observe("workspace", collect))
    await asyncio.sleep(0)
    release.set()

    first_view, second_view = await asyncio.gather(first, second)

    assert calls == 1
    assert first_view == second_view
    assert first_view.state == "fresh"
    assert first_view.sample is not None
    payload = first_view.payload()
    assert payload["sample"] == {
        "cpu_percent": 3.0,
        "memory_bytes": 4096,
        "process_count": 2,
        "observed_at": first_view.sample.observed_at.isoformat().replace("+00:00", "Z"),
    }


async def test_workspace_usage_cache_never_presents_old_values_as_current() -> None:
    monotonic_now = 0.0
    wall_now = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)

    def monotonic() -> float:
        return monotonic_now

    def utc_now() -> datetime:
        return wall_now

    service = WorkspaceUsageService(
        fresh_seconds=5,
        retry_seconds=5,
        monotonic=monotonic,
        utc_now=utc_now,
    )

    async def initial() -> RawWorkspaceUsage:
        return RawWorkspaceUsage(8.0, 8192, 3)

    fresh = await service.observe("workspace", initial)
    assert fresh.state == "fresh"
    assert fresh.sample is not None

    monotonic_now = 6
    wall_now += timedelta(seconds=6)

    async def interrupted() -> RawWorkspaceUsage:
        raise WorkspaceUsageStale()

    stale = await service.observe("workspace", interrupted)
    assert stale.state == "stale"
    assert stale.sample is None
    assert stale.last_observed_at == fresh.sample.observed_at
    assert stale.payload(now=wall_now)["sample"] is None
    assert stale.payload(now=wall_now)["age_seconds"] == 6

    offline = await service.record_failure("workspace", WorkspaceUsageOffline())
    assert offline.state == "offline"
    assert offline.sample is None
    assert offline.last_observed_at == fresh.sample.observed_at

    async def reconnected() -> RawWorkspaceUsage:
        return RawWorkspaceUsage(2.0, 2048, 1)

    recovered = await service.observe("workspace", reconnected)
    assert recovered.state == "fresh"
    assert recovered.sample is not None
    assert recovered.sample.process_count == 1


async def test_workspace_usage_stale_without_a_prior_sample_is_unavailable() -> None:
    service = WorkspaceUsageService()

    async def interrupted() -> RawWorkspaceUsage:
        raise WorkspaceUsageStale()

    view = await service.observe("workspace", interrupted)

    assert view.state == "unavailable"
    assert view.sample is None
    assert view.last_observed_at is None


async def test_workspace_usage_cache_evicts_old_workspaces_at_its_bound() -> None:
    now = 0.0
    calls: list[str] = []
    service = WorkspaceUsageService(
        fresh_seconds=60,
        max_entries=2,
        monotonic=lambda: now,
    )

    async def observe(workspace_id: str) -> None:
        async def collect() -> RawWorkspaceUsage:
            calls.append(workspace_id)
            return RawWorkspaceUsage(1.0, 1024, 1)

        await service.observe(workspace_id, collect)

    await observe("one")
    await observe("two")
    await observe("three")
    await observe("one")

    assert calls == ["one", "two", "three", "one"]
