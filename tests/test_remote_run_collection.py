from __future__ import annotations

import asyncio
import json
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from termroom.app import create_app
from termroom.config import Settings
from termroom.files import FileService
from termroom.remote_runs import RemoteRunManager
from termroom.run_results import (
    MAX_COLLECTION_DISPLAY_ITEMS,
    CollectionPlan,
    CollectionPlanItem,
    CollectionReport,
    CollectionReportItem,
    RemoteRunResultCollector,
    ResultCollectionConflict,
    ResultCollectionError,
)
from termroom.run_sources import WorkspaceEntry, build_workspace_manifest
from termroom.security import file_digest

RUN_ID = "11111111-1111-4111-8111-111111111111"
SECOND_RUN_ID = "22222222-2222-4222-8222-222222222222"


@dataclass
class FakeRemoteRuns:
    run: dict[str, Any]
    baseline: Any

    def get(self, run_id: str) -> dict[str, Any]:
        assert run_id == RUN_ID
        return dict(self.run)

    def ensure_workspace_bridge(self, run_id: str) -> dict[str, Any]:
        assert run_id == RUN_ID
        return dict(self.run)

    def collection_manifest(self, run_id: str) -> Any:
        assert run_id == RUN_ID
        return self.baseline


@dataclass
class MultipleFakeRemoteRuns:
    runs: dict[str, dict[str, Any]]
    baselines: dict[str, Any]

    def get(self, run_id: str) -> dict[str, Any]:
        return dict(self.runs[run_id])

    def ensure_workspace_bridge(self, run_id: str) -> dict[str, Any]:
        return dict(self.runs[run_id])

    def collection_manifest(self, run_id: str) -> Any:
        return self.baselines[run_id]


@dataclass
class FakeWorkspaces:
    values: dict[str, dict[str, Any]]

    def require(self, workspace_id: str) -> dict[str, Any]:
        if workspace_id not in self.values:
            raise KeyError(workspace_id)
        return dict(self.values[workspace_id])


class LocalTreeAccess:
    def __init__(self, files: FileService) -> None:
        self.files = files
        self.remote_writes: list[str] = []
        self.remote_uploads: list[tuple[str, str, str, bool]] = []
        self.upload_race_content: bytes | None = None

    @staticmethod
    def _root(workspace: dict[str, Any]) -> Path:
        return Path(workspace["path"])

    async def list_dir(
        self,
        workspace: dict[str, Any],
        relative_path: str,
        *,
        max_entries: int | None = None,
        max_metadata_bytes: int | None = None,
    ) -> tuple[str, list[Any]]:
        directory, entries = self.files.list_dir(
            self._root(workspace),
            relative_path,
            max_entries=max_entries,
            max_metadata_bytes=max_metadata_bytes,
        )
        return str(directory), entries

    async def stat(self, workspace: dict[str, Any], relative_path: str) -> Any:
        return self.files.stat(self._root(workspace), relative_path)

    async def download_stream(
        self, workspace: dict[str, Any], relative_path: str
    ) -> Any:
        target = self.files.resolve_regular_file(self._root(workspace), relative_path)
        with target.open("rb") as handle:
            while chunk := handle.read(7):
                yield chunk

    async def read_text(
        self, workspace: dict[str, Any], relative_path: str, max_bytes: int
    ) -> Any:
        assert max_bytes == self.files.max_edit_bytes
        return self.files.read_text(self._root(workspace), relative_path)

    async def write_text(
        self,
        workspace: dict[str, Any],
        relative_path: str,
        content: str,
        *,
        expected_digest: str,
        expected_mtime_ns: int,
        max_bytes: int,
    ) -> Any:
        assert max_bytes == self.files.max_edit_bytes
        self.remote_writes.append(relative_path)
        return self.files.write_text(
            self._root(workspace),
            relative_path,
            content,
            expected_digest=expected_digest,
            expected_mtime_ns=expected_mtime_ns,
        )

    async def upload_stream(
        self,
        workspace: dict[str, Any],
        parent: str,
        filename: str,
        chunks: Any,
        *,
        overwrite: bool,
        max_bytes: int,
    ) -> Any:
        assert overwrite is False
        raw = bytearray()
        async for chunk in chunks:
            raw.extend(chunk)
            if len(raw) > max_bytes:
                raise ValueError("upload too large")
        content = bytes(raw).decode("utf-8")
        relative_path = filename if parent == "." else f"{parent}/{filename}"
        connection_method = str(workspace["computer"]["connection_method"])
        self.remote_uploads.append(
            (connection_method, parent, filename, overwrite)
        )
        if self.upload_race_content is not None:
            target = self._root(workspace) / Path(relative_path)
            target.write_bytes(self.upload_race_content)
            self.upload_race_content = None
        return self.files.write_new_text(
            self._root(workspace), relative_path, content
        )


class ChangingTreeAccess(LocalTreeAccess):
    async def download_stream(
        self, workspace: dict[str, Any], relative_path: str
    ) -> Any:
        target = self.files.resolve_regular_file(self._root(workspace), relative_path)
        original = target.read_bytes()
        yield original
        changed = b"z" * len(original)
        target.write_bytes(changed)
        current = target.stat().st_mtime_ns
        os.utime(target, ns=(current + 1_000_000_000, current + 1_000_000_000))


def _baseline(values: dict[str, bytes]) -> Any:
    return build_workspace_manifest(
        WorkspaceEntry(
            path,
            "file",
            size=len(content),
            mtime_ns=1,
            digest=file_digest(content),
        )
        for path, content in values.items()
    )


def _collector(
    tmp_path: Path,
    *,
    source: Path,
    result: Path,
    baseline: Any,
    access: LocalTreeAccess | None = None,
    source_backend: str = "local",
    source_connection_method: str = "ssh",
    max_edit_bytes: int = 64,
    max_archive_bytes: int = 1024 * 1024,
) -> tuple[RemoteRunResultCollector, LocalTreeAccess]:
    files = access.files if access is not None else FileService(max_edit_bytes)
    remote_access = access or LocalTreeAccess(files)
    run = {
        "id": RUN_ID,
        "state": "finished",
        "workspace_id": "result-workspace",
        "source_kind": "workspace",
        "source_workspace_id": "source-workspace",
        "source_path": ".",
    }
    workspaces = FakeWorkspaces(
        {
            "result-workspace": {
                "id": "result-workspace",
                "backend_kind": "remote",
                "path": result,
                "transient": True,
                "is_remote_run": True,
            },
            "source-workspace": {
                "id": "source-workspace",
                "backend_kind": source_backend,
                "path": source,
                "transient": False,
                "is_remote_run": False,
                "computer": {"connection_method": source_connection_method},
            },
        }
    )
    collector = RemoteRunResultCollector(
        FakeRemoteRuns(run, baseline),  # type: ignore[arg-type]
        workspaces,  # type: ignore[arg-type]
        remote_access,  # type: ignore[arg-type]
        files,
        state_dir=tmp_path / "state",
        max_archive_bytes=max_archive_bytes,
    )
    return collector, remote_access


def test_collection_manifest_round_trips_excluded_prefixes_and_reads_v1(
    tmp_path: Path,
) -> None:
    collection_root = tmp_path / "collection"
    collection_root.mkdir()
    manager = object.__new__(RemoteRunManager)
    manager.collection_root = collection_root
    digest = file_digest(b"before\n")
    entries = [
        {
            "path": "main.py",
            "kind": "file",
            "size": 7,
            "mtime_ns": 1,
            "executable": False,
            "digest": digest,
        }
    ]

    manager._write_collection_manifest(
        RUN_ID,
        entries,
        excluded_prefixes=(".config/termroom",),
    )

    loaded = manager.collection_manifest(RUN_ID)
    assert loaded is not None
    assert loaded.excluded_prefixes == (".config/termroom",)
    assert loaded.entries[0].digest == digest

    legacy_path = manager._collection_manifest_path(SECOND_RUN_ID)
    legacy_path.write_text(
        json.dumps({"version": 1, "entries": entries}),
        encoding="utf-8",
    )
    legacy = manager.collection_manifest(SECOND_RUN_ID)
    assert legacy is not None
    assert legacy.excluded_prefixes == ()


@pytest.mark.asyncio
async def test_result_archive_contains_every_safe_regular_file_and_caller_cleans_it(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    result = tmp_path / "result"
    source.mkdir()
    (result / "nested").mkdir(parents=True)
    (result / "nested" / "message.txt").write_text("hello\n", encoding="utf-8")
    (result / "binary.bin").write_bytes(b"\x00\xff\x01")
    (result / "link").symlink_to("nested/message.txt")
    if hasattr(os, "mkfifo"):
        os.mkfifo(result / "pipe")

    collector, _access = _collector(
        tmp_path,
        source=source,
        result=result,
        baseline=_baseline({}),
    )

    archive_path = await collector.create_archive(RUN_ID)

    assert archive_path.parent.name == "remote-run-result-downloads"
    assert archive_path.stat().st_mode & 0o777 == 0o600
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["binary.bin", "nested/message.txt"]
        assert archive.read("binary.bin") == b"\x00\xff\x01"
        assert archive.read("nested/message.txt") == b"hello\n"
    assert not list(archive_path.parent.glob(".file-*"))

    archive_path.unlink()
    assert not archive_path.exists()


@pytest.mark.asyncio
async def test_private_result_paths_are_zip_only_and_never_applied(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    result = tmp_path / "result"
    source.mkdir()
    private_state = result / ".config" / "termroom"
    metadata = result / "nested" / ".termroom"
    private_state.mkdir(parents=True)
    metadata.mkdir(parents=True)
    (private_state / "new-file").write_text("private state\n", encoding="utf-8")
    (metadata / "config.json").write_text("metadata\n", encoding="utf-8")
    baseline = build_workspace_manifest(
        (),
        excluded_prefixes=(".config/termroom",),
    )
    collector, _access = _collector(
        tmp_path,
        source=source,
        result=result,
        baseline=baseline,
    )

    plan = await collector.review(RUN_ID)

    assert {
        item.path: (item.status, item.reason) for item in plan.items
    } == {
        ".config/termroom/new-file": ("skipped", "excluded_new_path"),
        "nested/.termroom/config.json": ("skipped", "excluded_new_path"),
    }
    report = await collector.apply(RUN_ID, plan.revision)
    assert {item.outcome for item in report.items} == {"skipped"}
    assert not (source / ".config").exists()
    assert not (source / "nested").exists()

    archive_path = await collector.create_archive(RUN_ID)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            assert archive.namelist() == [
                ".config/termroom/new-file",
                "nested/.termroom/config.json",
            ]
    finally:
        archive_path.unlink()


@pytest.mark.asyncio
async def test_review_and_apply_are_text_only_conflict_safe_and_never_delete(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    result = tmp_path / "result"
    source.mkdir()
    result.mkdir()
    originals = {
        "same.txt": b"same\n",
        "modify.txt": b"before\n",
        "conflict.txt": b"before\n",
        "already.txt": b"before\n",
        "delete.txt": b"keep me\n",
    }
    for path, content in originals.items():
        (source / path).write_bytes(content)
    (source / "conflict.txt").write_text("user edit\n", encoding="utf-8")
    (source / "already.txt").write_text("remote result\n", encoding="utf-8")

    (result / "same.txt").write_bytes(originals["same.txt"])
    (result / "modify.txt").write_text("remote edit\n", encoding="utf-8")
    (result / "conflict.txt").write_text("remote edit\n", encoding="utf-8")
    (result / "already.txt").write_text("remote result\n", encoding="utf-8")
    (result / "added.txt").write_text("new result\n", encoding="utf-8")
    (result / ".env").write_text("SECRET=remote\n", encoding="utf-8")
    (result / "binary.bin").write_bytes(b"abc\x00def")
    (result / "large.txt").write_bytes(b"x" * 65)

    collector, _access = _collector(
        tmp_path,
        source=source,
        result=result,
        baseline=_baseline(originals),
        max_edit_bytes=64,
    )

    plan = await collector.review(RUN_ID)
    by_path = {item.path: item for item in plan.items}
    assert (by_path["modify.txt"].status, by_path["modify.txt"].change) == (
        "ready",
        "modified",
    )
    assert (by_path["added.txt"].status, by_path["added.txt"].change) == (
        "ready",
        "added",
    )
    assert by_path["same.txt"].status == "already_result"
    assert by_path["already.txt"].status == "already_result"
    assert by_path["conflict.txt"].reason == "source_changed_since_run"
    assert by_path["delete.txt"].reason == "deletion_not_applied"
    assert by_path[".env"].reason == "excluded_new_path"
    assert by_path["binary.bin"].reason == "binary_result"
    assert by_path["large.txt"].reason == "file_too_large"
    serialized = json_without_content(plan.as_dict())
    assert "remote edit" not in serialized
    assert "SECRET=remote" not in serialized

    report = await collector.apply(RUN_ID, plan.revision)
    outcomes = {item.path: item.outcome for item in report.items}
    assert outcomes["modify.txt"] == "applied"
    assert outcomes["added.txt"] == "applied"
    assert outcomes["same.txt"] == "already_result"
    assert outcomes["conflict.txt"] == "conflict"
    assert outcomes["delete.txt"] == "skipped"
    assert (source / "modify.txt").read_text(encoding="utf-8") == "remote edit\n"
    assert (source / "added.txt").read_text(encoding="utf-8") == "new result\n"
    assert (source / "conflict.txt").read_text(encoding="utf-8") == "user edit\n"
    assert (source / "delete.txt").read_text(encoding="utf-8") == "keep me\n"
    assert not (source / ".env").exists()
    assert not (source / "binary.bin").exists()
    assert "remote edit" not in json_without_content(report.as_dict())


def json_without_content(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_collection_payloads_hide_unchanged_rows_and_bound_changed_rows() -> None:
    changed_count = MAX_COLLECTION_DISPLAY_ITEMS + 3
    plan = CollectionPlan(
        RUN_ID,
        "source-workspace",
        ".",
        "revision",
        tuple(
            CollectionPlanItem(
                f"changed-{index:04d}.txt",
                "modified",
                "ready",
                "source_unchanged_since_run",
            )
            for index in range(changed_count)
        )
        + (
            CollectionPlanItem(
                "same-a.txt",
                "unchanged",
                "already_result",
                "source_matches_result",
            ),
            CollectionPlanItem(
                "same-b.txt",
                "unchanged",
                "already_result",
                "result_matches_baseline",
            ),
        ),
    )

    payload = plan.as_dict()

    assert len(payload["items"]) == MAX_COLLECTION_DISPLAY_ITEMS
    assert all(item["change"] != "unchanged" for item in payload["items"])
    assert payload["total_items"] == changed_count + 2
    assert payload["changed_items"] == changed_count
    assert payload["unchanged_items"] == 2
    assert payload["shown_items"] == MAX_COLLECTION_DISPLAY_ITEMS
    assert payload["items_truncated"] is True
    assert payload["omitted_items"] == 3
    assert payload["apply_allowed"] is False
    assert payload["summary"] == {
        "ready": changed_count,
        "already_result": 2,
        "conflict": 0,
        "skipped": 0,
    }

    report = CollectionReport(
        RUN_ID,
        plan.revision,
        tuple(
            CollectionReportItem(
                f"changed-{index:04d}.txt",
                "modified",
                "applied",
                "applied",
            )
            for index in range(changed_count)
        )
        + (
            CollectionReportItem(
                "same-a.txt",
                "unchanged",
                "already_result",
                "source_matches_result",
            ),
            CollectionReportItem(
                "same-b.txt",
                "unchanged",
                "already_result",
                "result_matches_baseline",
            ),
        ),
    )
    report_payload = report.as_dict()
    assert len(report_payload["items"]) == MAX_COLLECTION_DISPLAY_ITEMS
    assert report_payload["total_items"] == changed_count + 2
    assert report_payload["changed_items"] == changed_count
    assert report_payload["unchanged_items"] == 2
    assert report_payload["items_truncated"] is True
    assert report_payload["omitted_items"] == 3
    assert report_payload["summary"]["applied"] == changed_count
    assert report_payload["summary"]["already_result"] == 2


@pytest.mark.asyncio
async def test_apply_rejects_more_changed_rows_than_can_be_reviewed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    result = tmp_path / "result"
    source.mkdir()
    result.mkdir()
    for index in range(MAX_COLLECTION_DISPLAY_ITEMS + 1):
        (result / f"new-{index:04d}.txt").write_text("x", encoding="utf-8")
    collector, _access = _collector(
        tmp_path,
        source=source,
        result=result,
        baseline=_baseline({}),
    )
    plan = await collector.review(RUN_ID)
    assert plan.as_dict()["apply_allowed"] is False

    with pytest.raises(ResultCollectionError) as failed:
        await collector.apply(RUN_ID, plan.revision)

    assert failed.value.code == "collection_too_many_changes"
    assert list(source.iterdir()) == []


@pytest.mark.asyncio
async def test_apply_recalculates_revision_before_any_mutation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    result = tmp_path / "result"
    source.mkdir()
    result.mkdir()
    original = {"a.txt": b"before a\n", "b.txt": b"before b\n"}
    for path, content in original.items():
        (source / path).write_bytes(content)
        (result / path).write_text(f"result {path}\n", encoding="utf-8")

    collector, _access = _collector(
        tmp_path,
        source=source,
        result=result,
        baseline=_baseline(original),
    )
    plan = await collector.review(RUN_ID)
    (source / "b.txt").write_text("new local work\n", encoding="utf-8")

    with pytest.raises(ResultCollectionConflict) as failed:
        await collector.apply(RUN_ID, plan.revision)
    assert failed.value.code == "plan_revision_mismatch"
    assert (source / "a.txt").read_bytes() == original["a.txt"]
    assert (source / "b.txt").read_text(encoding="utf-8") == "new local work\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("source_backend", ["local", "remote"])
async def test_apply_rejects_a_source_path_replaced_by_a_symlink(
    tmp_path: Path, source_backend: str
) -> None:
    source = tmp_path / "source"
    result = tmp_path / "result"
    source.mkdir()
    result.mkdir()
    original = b"before\n"
    source_file = source / "config.txt"
    source_file.write_bytes(original)
    original_mtime = source_file.stat().st_mtime_ns
    (result / "config.txt").write_text("remote result\n", encoding="utf-8")
    collector, _access = _collector(
        tmp_path,
        source=source,
        result=result,
        baseline=_baseline({"config.txt": original}),
        source_backend=source_backend,
    )
    plan = await collector.review(RUN_ID)
    moved = source / "moved.txt"
    source_file.rename(moved)
    source_file.symlink_to(moved.name)
    os.utime(moved, ns=(original_mtime, original_mtime))

    with pytest.raises(ResultCollectionConflict) as failed:
        await collector.apply(RUN_ID, plan.revision)

    assert failed.value.code == "plan_revision_mismatch"
    assert source_file.is_symlink()
    assert moved.read_bytes() == original


@pytest.mark.asyncio
async def test_different_runs_for_one_source_serialize_their_apply(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    first_result = tmp_path / "result-a"
    second_result = tmp_path / "result-b"
    source.mkdir()
    first_result.mkdir()
    second_result.mkdir()
    original = b"before\n"
    (source / "shared.txt").write_bytes(original)
    (first_result / "shared.txt").write_text("result a\n", encoding="utf-8")
    (second_result / "shared.txt").write_text("result b\n", encoding="utf-8")
    baseline = _baseline({"shared.txt": original})
    runs = {
        RUN_ID: {
            "id": RUN_ID,
            "state": "finished",
            "workspace_id": "result-a",
            "source_kind": "workspace",
            "source_workspace_id": "source-workspace",
            "source_path": ".",
        },
        SECOND_RUN_ID: {
            "id": SECOND_RUN_ID,
            "state": "finished",
            "workspace_id": "result-b",
            "source_kind": "workspace",
            "source_workspace_id": "source-workspace",
            "source_path": ".",
        },
    }
    workspaces = FakeWorkspaces(
        {
            "source-workspace": {
                "id": "source-workspace",
                "backend_kind": "local",
                "path": source,
                "transient": False,
                "is_remote_run": False,
            },
            "result-a": {
                "id": "result-a",
                "backend_kind": "remote",
                "path": first_result,
                "transient": True,
                "is_remote_run": True,
            },
            "result-b": {
                "id": "result-b",
                "backend_kind": "remote",
                "path": second_result,
                "transient": True,
                "is_remote_run": True,
            },
        }
    )
    files = FileService(64)
    access = LocalTreeAccess(files)
    collector = RemoteRunResultCollector(
        MultipleFakeRemoteRuns(
            runs,
            {RUN_ID: baseline, SECOND_RUN_ID: baseline},
        ),  # type: ignore[arg-type]
        workspaces,  # type: ignore[arg-type]
        access,  # type: ignore[arg-type]
        files,
        state_dir=tmp_path / "state",
        max_archive_bytes=1024 * 1024,
    )
    first_plan, second_plan = await asyncio.gather(
        collector.review(RUN_ID),
        collector.review(SECOND_RUN_ID),
    )

    outcomes = await asyncio.gather(
        collector.apply(RUN_ID, first_plan.revision),
        collector.apply(SECOND_RUN_ID, second_plan.revision),
        return_exceptions=True,
    )

    reports = [value for value in outcomes if isinstance(value, CollectionReport)]
    conflicts = [
        value for value in outcomes if isinstance(value, ResultCollectionConflict)
    ]
    assert len(reports) == 1
    assert reports[0].items[0].outcome == "applied"
    assert len(conflicts) == 1
    assert conflicts[0].code == "plan_revision_mismatch"
    assert (source / "shared.txt").read_text(encoding="utf-8") in {
        "result a\n",
        "result b\n",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("connection_method", ["ssh", "node"])
async def test_remote_source_additions_use_no_clobber_upload(
    tmp_path: Path,
    connection_method: str,
) -> None:
    source = tmp_path / "source"
    result = tmp_path / "result"
    (source / "nested").mkdir(parents=True)
    (result / "nested").mkdir(parents=True)
    (result / "nested" / "added.txt").write_text(
        "remote result\n", encoding="utf-8"
    )

    collector, access = _collector(
        tmp_path,
        source=source,
        result=result,
        baseline=_baseline({}),
        source_backend="remote",
        source_connection_method=connection_method,
    )
    plan = await collector.review(RUN_ID)
    assert len(plan.items) == 1
    assert plan.items[0].status == "ready"
    assert plan.items[0].reason == "new_remote_text_file"

    report = await collector.apply(RUN_ID, plan.revision)
    assert report.items[0].outcome == "applied"
    assert access.remote_writes == []
    assert access.remote_uploads == [
        (connection_method, "nested", "added.txt", False)
    ]
    assert (source / "nested" / "added.txt").read_text(encoding="utf-8") == (
        "remote result\n"
    )

    repeated = await collector.review(RUN_ID)
    assert repeated.items[0].status == "already_result"


@pytest.mark.asyncio
async def test_remote_existing_text_uses_conditional_remote_write_and_is_idempotent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    result = tmp_path / "result"
    source.mkdir()
    result.mkdir()
    original = {"existing.txt": b"before\n"}
    (source / "existing.txt").write_bytes(original["existing.txt"])
    (result / "existing.txt").write_text("after\n", encoding="utf-8")

    collector, access = _collector(
        tmp_path,
        source=source,
        result=result,
        baseline=_baseline(original),
        source_backend="remote",
    )
    plan = await collector.review(RUN_ID)
    assert plan.items[0].status == "ready"

    report = await collector.apply(RUN_ID, plan.revision)
    assert report.items[0].outcome == "applied"
    assert access.remote_writes == ["existing.txt"]
    assert (source / "existing.txt").read_text(encoding="utf-8") == "after\n"

    repeated = await collector.review(RUN_ID)
    assert repeated.items[0].status == "already_result"


@pytest.mark.asyncio
async def test_remote_addition_never_clobbers_a_path_created_during_upload(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    result = tmp_path / "result"
    source.mkdir()
    result.mkdir()
    (result / "added.txt").write_text("remote result\n", encoding="utf-8")

    collector, access = _collector(
        tmp_path,
        source=source,
        result=result,
        baseline=_baseline({}),
        source_backend="remote",
        source_connection_method="node",
    )
    plan = await collector.review(RUN_ID)
    access.upload_race_content = b"concurrent source\n"

    report = await collector.apply(RUN_ID, plan.revision)

    assert report.items[0].outcome == "conflict"
    assert (source / "added.txt").read_bytes() == b"concurrent source\n"
    assert access.remote_uploads == [("node", ".", "added.txt", False)]


@pytest.mark.asyncio
@pytest.mark.parametrize("source_backend", ["local", "remote"])
async def test_addition_requires_an_existing_real_parent(
    tmp_path: Path,
    source_backend: str,
) -> None:
    source = tmp_path / "source"
    result = tmp_path / "result"
    source.mkdir()
    (result / "new-directory").mkdir(parents=True)
    (result / "new-directory" / "added.txt").write_text("new\n", encoding="utf-8")

    collector, _access = _collector(
        tmp_path,
        source=source,
        result=result,
        baseline=_baseline({}),
        source_backend=source_backend,
    )
    plan = await collector.review(RUN_ID)

    assert len(plan.items) == 1
    assert plan.items[0].status == "conflict"
    assert plan.items[0].reason == "source_parent_missing"
    report = await collector.apply(RUN_ID, plan.revision)
    assert report.items[0].outcome == "conflict"
    assert not (source / "new-directory").exists()


@pytest.mark.asyncio
async def test_archive_discards_partial_output_when_result_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    result = tmp_path / "result"
    source.mkdir()
    result.mkdir()
    (result / "changing.txt").write_text("original\n", encoding="utf-8")
    files = FileService()
    access = ChangingTreeAccess(files)
    collector, _access = _collector(
        tmp_path,
        source=source,
        result=result,
        baseline=_baseline({}),
        access=access,
    )

    with pytest.raises(ResultCollectionConflict) as failed:
        await collector.create_archive(RUN_ID)
    assert failed.value.code == "result_changed"
    assert not list(collector.download_root.iterdir())


@pytest.mark.asyncio
async def test_collection_rejects_nonterminal_runs_and_oversized_results(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    result = tmp_path / "result"
    source.mkdir()
    result.mkdir()
    (result / "large.bin").write_bytes(b"12345")
    collector, _access = _collector(
        tmp_path,
        source=source,
        result=result,
        baseline=_baseline({}),
        max_archive_bytes=4,
    )

    with pytest.raises(ResultCollectionError) as oversized:
        await collector.create_archive(RUN_ID)
    assert oversized.value.code == "result_too_large"

    collector.remote_runs.run["state"] = "running"  # type: ignore[attr-defined]
    with pytest.raises(ResultCollectionError) as active:
        await collector.create_archive(RUN_ID)
    assert active.value.code == "result_not_ready"


@pytest.mark.asyncio
async def test_collection_stops_listing_at_the_remaining_entry_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    result = tmp_path / "result"
    source.mkdir()
    result.mkdir()
    for index in range(11):
        (result / f"entry-{index:02d}.txt").write_text("x", encoding="utf-8")
    collector, _access = _collector(
        tmp_path,
        source=source,
        result=result,
        baseline=_baseline({}),
    )
    monkeypatch.setattr("termroom.run_results.MAX_RESULT_ENTRIES", 10)

    with pytest.raises(ResultCollectionError) as failed:
        await collector.review(RUN_ID)

    assert failed.value.code == "result_too_many_entries"


@pytest.mark.asyncio
async def test_result_routes_download_review_recheck_and_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    run = {
        "id": RUN_ID,
        "state": "finished",
        "source_kind": "workspace",
        "source_workspace_id": "source-workspace",
        "source_label": "project",
    }
    plan = CollectionPlan(
        RUN_ID,
        "source-workspace",
        ".",
        "review-revision",
        (
            CollectionPlanItem(
                "main.py",
                "modified",
                "ready",
                "source_unchanged_since_run",
            ),
            CollectionPlanItem(
                "model.bin",
                "added",
                "skipped",
                "binary_result",
            ),
        ),
    )
    report = CollectionReport(
        RUN_ID,
        plan.revision,
        (
            CollectionReportItem("main.py", "modified", "applied", "applied"),
            CollectionReportItem(
                "model.bin", "added", "skipped", "binary_result"
            ),
        ),
    )
    archive_path = tmp_path / "result.zip"
    archive_path.write_bytes(b"safe-result-zip")
    applied_revisions: list[str] = []

    async def create_archive(run_id: str) -> Path:
        assert run_id == RUN_ID
        return archive_path

    async def review(run_id: str) -> CollectionPlan:
        assert run_id == RUN_ID
        return plan

    async def apply(run_id: str, revision: str) -> CollectionReport:
        assert run_id == RUN_ID
        applied_revisions.append(revision)
        if revision == "too-many":
            raise ResultCollectionError(
                "review is intentionally bounded",
                code="collection_too_many_changes",
            )
        if revision != plan.revision:
            raise ResultCollectionConflict(
                "review changed", code="plan_revision_mismatch"
            )
        return report

    monkeypatch.setattr(app.state.remote_runs, "get", lambda run_id: dict(run))
    monkeypatch.setattr(app.state.run_results, "create_archive", create_archive)
    monkeypatch.setattr(app.state.run_results, "review", review)
    monkeypatch.setattr(app.state.run_results, "apply", apply)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        login = await client.post(
            "/login",
            data={"password": "test-token"},
            follow_redirects=False,
        )
        assert login.status_code == 303

        downloaded = await client.get(f"/remote-runs/{RUN_ID}/result.zip")
        assert downloaded.status_code == 200
        assert downloaded.content == b"safe-result-zip"
        assert downloaded.headers["content-type"] == "application/zip"
        assert "result.zip" in downloaded.headers["content-disposition"]
        assert not archive_path.exists()

        reviewed = await client.get(f"/remote-runs/{RUN_ID}/collect")
        assert reviewed.status_code == 200
        assert "Bring changes to original" in reviewed.text
        assert "main.py" in reviewed.text
        assert "model.bin" in reviewed.text
        assert "Recover this file from the result ZIP." in reviewed.text

        too_many = await client.post(
            f"/remote-runs/{RUN_ID}/collect",
            data={"_csrf": settings.csrf_token, "revision": "too-many"},
        )
        assert too_many.status_code == 409
        assert "This result has more than 500 changes" in too_many.text

        stale = await client.post(
            f"/remote-runs/{RUN_ID}/collect",
            data={"_csrf": settings.csrf_token, "revision": "stale"},
        )
        assert stale.status_code == 409
        assert "The original or result changed after review" in stale.text
        assert "review-revision" in stale.text

        applied = await client.post(
            f"/remote-runs/{RUN_ID}/collect",
            data={"_csrf": settings.csrf_token, "revision": plan.revision},
            follow_redirects=False,
        )
        assert applied.status_code == 303
        assert applied.headers["location"] == (
            f"/remote-runs/{RUN_ID}/collect?collected=1&applied=1&conflict=0"
            "&already_result=0&skipped=1&failed=0"
        )

        completed = await client.get(applied.headers["location"])
        assert completed.status_code == 200
        assert "Finished: 1 applied, 0 conflicts, 0 already present, 1 ZIP only" in (
            completed.text
        )
        assert "Bring changes to original" in completed.text
        assert "main.py" in completed.text

        refreshed = await client.get(applied.headers["location"])
        assert refreshed.status_code == 200
        assert "Finished: 1 applied" in refreshed.text

    assert applied_revisions == ["too-many", "stale", plan.revision]
