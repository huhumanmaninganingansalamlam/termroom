from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import json
import os
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from termroom import node_remote_runs
from termroom.node_agent import NodeRuntime
from termroom.node_protocol import (
    NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW,
    NODE_REMOTE_RUN_VERSION,
)
from termroom.node_remote_runs import (
    NodeRemoteRunClient,
    NodeRemoteRunError,
    NodeRemoteRunRuntime,
    NodeWorkspaceSnapshotSource,
)
from termroom.remote_runs import RemoteRunError, RemoteRunManager
from termroom.run_sources import (
    WorkspaceEntry,
    build_workspace_manifest,
    materialize_workspace_snapshot,
)


def _payload(run_root: Path, run_id: str, **values: Any) -> dict[str, Any]:
    return {
        "remote_run_version": NODE_REMOTE_RUN_VERSION,
        "run_base": str(run_root.resolve()),
        "run_id": run_id,
        **values,
    }


async def _operation(
    runtime: NodeRuntime, operation: str, payload: dict[str, Any]
) -> dict[str, Any]:
    result = await runtime.handle(operation, payload, _unused_send)
    return result.value


async def _unused_send(_message: dict[str, Any]) -> None:
    raise AssertionError("This operation must not send an unsolicited frame")


async def _wait_for_terminal(
    runtime: NodeRuntime, payload: dict[str, Any], *, timeout: float = 8.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = await _operation(runtime, "remote_run.observe", payload)
        if observed["state"] in {"finished", "stopped", "failed", "lost"}:
            return observed
        await asyncio.sleep(0.05)
    raise AssertionError("Remote Run did not reach a terminal state")


async def _write_snapshot_file(
    runtime: NodeRuntime,
    payload: dict[str, Any],
    relative_path: str,
    content: bytes,
    *,
    executable: bool = False,
) -> None:
    stream_id = uuid.uuid4().hex
    opened = await runtime.handle(
        "remote_run.snapshot.file.open",
        {
            **payload,
            "stream_id": stream_id,
            "path": relative_path,
            "expected_size": len(content),
            "executable": executable,
        },
        _unused_send,
    )
    assert opened.value == {"stream_id": stream_id}
    stream = runtime.streams[stream_id]
    await stream.feed(content[:2])
    await stream.feed(content[2:])
    assert await stream.close() == {"size": len(content)}


def _quarantined_run(tmp_path: Path) -> tuple[NodeRemoteRunRuntime, str, Path, Path]:
    root = tmp_path / "managed-runs"
    runtime = NodeRemoteRunRuntime(root)
    runtime._tmux = lambda *args, check=True: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args, 1 if args[0] == "has-session" else 0, "", ""
    )
    run_id = str(uuid.uuid4())
    payload = _payload(root, run_id)
    runtime.create({**payload, "command": "true", "cwd_rel": "."})
    quarantine = root / f".termroom-deleting-{run_id}"
    runtime._replace(
        runtime._root_path / run_id,
        runtime._root_path / quarantine.name,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    return runtime, run_id, quarantine, sentinel


def _run_layout_descriptors(runtime: NodeRemoteRunRuntime, run_id: str) -> tuple[int, ...]:
    return tuple(
        handles[run_id]
        for handles in (
            runtime._metadata_dir_fds,
            runtime._run_dir_fds,
            runtime._work_dir_fds,
            runtime._work_staging_dir_fds,
        )
        if run_id in handles
    )


def _track_cleanup_descriptors(monkeypatch: pytest.MonkeyPatch) -> set[int]:
    opened: set[int] = set()
    original_open = os.open
    original_close = os.close

    def track_open(*args: Any, **kwargs: Any) -> int:
        descriptor = original_open(*args, **kwargs)
        opened.add(descriptor)
        return descriptor

    def track_close(descriptor: int) -> None:
        original_close(descriptor)
        if descriptor in opened:
            with pytest.raises(OSError) as closed:
                os.fstat(descriptor)
            assert closed.value.errno == errno.EBADF
            opened.remove(descriptor)

    monkeypatch.setattr(node_remote_runs.os, "open", track_open)
    monkeypatch.setattr(node_remote_runs.os, "close", track_close)
    return opened


def _assert_run_layout_descriptors_closed(
    runtime: NodeRemoteRunRuntime, run_id: str, descriptors: tuple[int, ...]
) -> None:
    for handles in (
        runtime._metadata_dir_fds,
        runtime._run_dir_fds,
        runtime._work_dir_fds,
        runtime._work_staging_dir_fds,
    ):
        assert run_id not in handles
    for descriptor in descriptors:
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF


def test_remote_run_delete_rescans_one_late_quarantine_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, run_id, quarantine, sentinel = _quarantined_run(tmp_path)
    sibling_id = str(uuid.uuid4())
    runtime.create(
        {
            **_payload(runtime.run_root, sibling_id),
            "command": "true",
            "cwd_rel": ".",
        }
    )
    descriptors = _run_layout_descriptors(runtime, run_id)
    original_forget = runtime._forget_layout_handles
    checked_handles = False

    def forget_and_check(candidate_id: str) -> None:
        nonlocal checked_handles
        original_forget(candidate_id)
        if candidate_id == run_id and not checked_handles:
            checked_handles = True
            _assert_run_layout_descriptors_closed(runtime, run_id, descriptors)

    monkeypatch.setattr(runtime, "_forget_layout_handles", forget_and_check)
    cleanup_descriptors = _track_cleanup_descriptors(monkeypatch)
    original_rmdir = os.rmdir
    injected = False

    def add_late_entry(name: str | bytes, *, dir_fd: int | None = None) -> None:
        nonlocal injected
        if name == quarantine.name and dir_fd is not None and not injected:
            injected = True
            (quarantine / "late-entry").write_bytes(b"late")
        original_rmdir(name, dir_fd=dir_fd)

    monkeypatch.setattr(node_remote_runs.os, "rmdir", add_late_entry)
    deleted = runtime.delete(_payload(runtime.run_root, run_id))

    assert deleted == {"deleted": True, "already_missing": False}
    assert injected
    assert not quarantine.exists()
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert checked_handles
    assert not cleanup_descriptors
    assert (runtime.run_root / sibling_id / ".termroom" / "marker").read_text() == f"{sibling_id}\n"


def test_remote_run_delete_after_external_tree_removal_forgets_handles(
    tmp_path: Path,
) -> None:
    runtime, run_id, quarantine, sentinel = _quarantined_run(tmp_path)
    descriptors = _run_layout_descriptors(runtime, run_id)
    detached = tmp_path / "detached-quarantine"
    quarantine.rename(detached)

    assert runtime.delete(_payload(runtime.run_root, run_id)) == {
        "deleted": True,
        "already_missing": True,
    }
    _assert_run_layout_descriptors_closed(runtime, run_id, descriptors)
    assert (detached / ".termroom" / "marker").read_text() == f"{run_id}\n"
    assert sentinel.read_bytes() == b"keep"


def test_remote_run_delete_treats_listed_entry_disappearance_as_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, run_id, quarantine, sentinel = _quarantined_run(tmp_path)
    (quarantine / "vanish").write_bytes(b"gone")
    original_stat = os.stat
    original_unlink = os.unlink
    removed = False

    def remove_after_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal removed
        result = original_stat(path, *args, **kwargs)
        if (
            path == "vanish"
            and kwargs.get("dir_fd") is not None
            and not removed
        ):
            removed = True
            original_unlink(path, dir_fd=kwargs["dir_fd"])
        return result

    monkeypatch.setattr(node_remote_runs.os, "stat", remove_after_stat)
    deleted = runtime.delete(_payload(runtime.run_root, run_id))

    assert deleted == {"deleted": True, "already_missing": False}
    assert removed
    assert not quarantine.exists()
    assert sentinel.read_bytes() == b"keep"


def test_remote_run_delete_rejects_entry_replacement_before_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, run_id, quarantine, sentinel = _quarantined_run(tmp_path)
    victim = quarantine / "victim"
    victim.write_bytes(b"owned")
    original_stat = os.stat
    original_rename = os.rename
    swapped = False

    def replace_after_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal swapped
        result = original_stat(path, *args, **kwargs)
        if (
            path == "victim"
            and kwargs.get("dir_fd") is not None
            and not swapped
        ):
            swapped = True
            parent_fd = kwargs["dir_fd"]
            original_rename("victim", "saved-victim", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            replacement_fd = os.open(
                "victim", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd
            )
            try:
                os.write(replacement_fd, b"replacement")
            finally:
                os.close(replacement_fd)
        return result

    monkeypatch.setattr(node_remote_runs.os, "stat", replace_after_stat)
    with pytest.raises(NodeRemoteRunError) as invalid:
        runtime.delete(_payload(runtime.run_root, run_id))

    assert invalid.value.code == "cleanup_invalid"
    assert swapped
    assert (quarantine / "victim").read_bytes() == b"replacement"
    assert (quarantine / "saved-victim").read_bytes() == b"owned"
    assert sentinel.read_bytes() == b"keep"
    assert quarantine.exists()


def test_remote_run_delete_bounds_persistent_late_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, run_id, quarantine, sentinel = _quarantined_run(tmp_path)
    original_rmdir = os.rmdir
    attempts = 0
    descriptors = _run_layout_descriptors(runtime, run_id)
    original_forget = runtime._forget_layout_handles
    checked_handles = False

    def forget_and_check(candidate_id: str) -> None:
        nonlocal checked_handles
        original_forget(candidate_id)
        if candidate_id == run_id and not checked_handles:
            checked_handles = True
            _assert_run_layout_descriptors_closed(runtime, run_id, descriptors)

    def keep_root_nonempty(name: str | bytes, *, dir_fd: int | None = None) -> None:
        nonlocal attempts
        if name == quarantine.name and dir_fd is not None:
            attempts += 1
            (quarantine / f"late-{attempts}").write_bytes(b"late")
        original_rmdir(name, dir_fd=dir_fd)

    monkeypatch.setattr(runtime, "_forget_layout_handles", forget_and_check)
    cleanup_descriptors = _track_cleanup_descriptors(monkeypatch)
    monkeypatch.setattr(node_remote_runs.os, "rmdir", keep_root_nonempty)
    with pytest.raises(NodeRemoteRunError) as invalid:
        runtime.delete(_payload(runtime.run_root, run_id))

    assert invalid.value.code == "cleanup_invalid"
    assert isinstance(invalid.value.__cause__, OSError)
    assert invalid.value.__cause__.errno == errno.ENOTEMPTY
    assert attempts == 3
    assert checked_handles
    assert not cleanup_descriptors
    assert quarantine.exists()
    assert sentinel.read_bytes() == b"keep"
    runtime._assert_marked_tree(runtime._root_path / quarantine.name, run_id)
    monkeypatch.setattr(node_remote_runs.os, "rmdir", original_rmdir)
    assert runtime.delete(_payload(runtime.run_root, run_id)) == {
        "deleted": True,
        "already_missing": False,
    }
    assert not quarantine.exists()
    assert not cleanup_descriptors


@pytest.mark.parametrize(
    ("stage", "error_number"),
    [
        ("entry-stat", errno.EACCES),
        ("entry-open", errno.EIO),
        ("entry-unlink", errno.EACCES),
        ("child-rmdir", errno.EACCES),
        ("root-stat", errno.EIO),
        ("root-rmdir", errno.EACCES),
        ("layout-handle-close", errno.EIO),
    ],
)
def test_remote_run_delete_preserves_quarantine_and_errno_by_failure_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    error_number: int,
) -> None:
    runtime, run_id, quarantine, sentinel = _quarantined_run(tmp_path)
    if stage == "entry-open" or stage == "child-rmdir":
        (quarantine / stage).mkdir()
    elif stage.startswith("entry-"):
        (quarantine / stage).write_bytes(b"entry")

    operation = {
        "entry-stat": "stat",
        "entry-open": "open",
        "entry-unlink": "unlink",
        "child-rmdir": "rmdir",
        "root-stat": "stat",
        "root-rmdir": "rmdir",
        "layout-handle-close": "close",
    }[stage]
    original = getattr(os, operation)
    target_descriptor = runtime._metadata_dir_fds[run_id]
    calls = 0
    root_stat_calls = 0

    def fail_at_stage(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls, root_stat_calls
        target = args[0] if args else None
        should_fail = False
        if stage in {"entry-stat", "entry-open", "entry-unlink", "child-rmdir"}:
            should_fail = target == stage and kwargs.get("dir_fd") is not None
        elif stage == "root-stat":
            should_fail = target == quarantine.name and kwargs.get("dir_fd") is not None
            if should_fail:
                root_stat_calls += 1
                should_fail = root_stat_calls == 2
        elif stage == "root-rmdir":
            should_fail = target == quarantine.name and kwargs.get("dir_fd") is not None
        else:
            should_fail = target == target_descriptor
        if should_fail:
            calls += 1
            raise OSError(error_number, "injected cleanup failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(node_remote_runs.os, operation, fail_at_stage)
    with pytest.raises(NodeRemoteRunError) as invalid:
        runtime.delete(_payload(runtime.run_root, run_id))

    assert invalid.value.code == "cleanup_invalid"
    assert isinstance(invalid.value.__cause__, OSError)
    assert invalid.value.__cause__.errno == error_number
    assert calls == 1
    assert quarantine.exists()
    assert sentinel.read_bytes() == b"keep"
    runtime._assert_marked_tree(runtime._root_path / quarantine.name, run_id)
    monkeypatch.setattr(node_remote_runs.os, operation, original)
    assert runtime.delete(_payload(runtime.run_root, run_id)) == {
        "deleted": True,
        "already_missing": False,
    }


def test_remote_run_delete_rejects_invalid_and_ambiguous_cleanup_roots(
    tmp_path: Path,
) -> None:
    runtime, run_id, quarantine, sentinel = _quarantined_run(tmp_path)
    outside = tmp_path / "outside-tree"
    outside.mkdir()
    marker = outside / "marker"
    marker.write_bytes(b"preserve")

    with pytest.raises(NodeRemoteRunError) as invalid:
        runtime._remove_owned_tree(outside, run_id)
    assert invalid.value.code == "cleanup_invalid"

    (runtime.run_root / run_id).mkdir()
    with pytest.raises(NodeRemoteRunError) as ambiguous:
        runtime.delete(_payload(runtime.run_root, run_id))
    assert ambiguous.value.code == "cleanup_ambiguous"
    assert (runtime.run_root / run_id).is_dir()
    assert quarantine.is_dir()
    assert marker.read_bytes() == b"preserve"
    assert sentinel.read_bytes() == b"keep"


def test_remote_run_delete_rejects_marker_and_child_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, run_id, quarantine, sentinel = _quarantined_run(tmp_path)
    metadata_marker = quarantine / ".termroom" / "marker"
    metadata_marker.write_text("wrong-run\n")
    with pytest.raises(NodeRemoteRunError) as mismatch:
        runtime.delete(_payload(runtime.run_root, run_id))
    assert mismatch.value.code == "marker_mismatch"
    metadata_marker.write_text(f"{run_id}\n")

    child = quarantine / "child"
    child.mkdir()
    (child / "original").write_bytes(b"original")
    moved = quarantine / "child-original"
    original_open = os.open
    replaced = False

    def replace_child_before_open(path: Any, *args: Any, **kwargs: Any) -> int:
        nonlocal replaced
        if path == "child" and kwargs.get("dir_fd") is not None and not replaced:
            parent_fd = kwargs["dir_fd"]
            os.rename("child", "child-original", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.mkdir("child", 0o700, dir_fd=parent_fd)
            replacement_fd = original_open(
                "child", os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent_fd
            )
            try:
                leaf_fd = os.open(
                    "replacement",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=replacement_fd,
                )
                os.close(leaf_fd)
            finally:
                os.close(replacement_fd)
            replaced = True
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(node_remote_runs.os, "open", replace_child_before_open)
    with pytest.raises(NodeRemoteRunError) as replacement:
        runtime.delete(_payload(runtime.run_root, run_id))
    assert replacement.value.code == "cleanup_invalid"
    assert replaced
    assert (moved / "original").read_bytes() == b"original"
    assert (child / "replacement").is_file()
    assert sentinel.read_bytes() == b"keep"


def test_remote_run_delete_rejects_replaced_quarantine_root_identity(
    tmp_path: Path,
) -> None:
    runtime, run_id, quarantine, sentinel = _quarantined_run(tmp_path)
    detached = tmp_path / "detached-original-run"
    quarantine.rename(detached)
    (quarantine / ".termroom").mkdir(parents=True, mode=0o700)
    marker = quarantine / ".termroom" / "marker"
    marker.write_text(f"{run_id}\n")
    marker.chmod(0o600)
    with pytest.raises(NodeRemoteRunError) as invalid:
        runtime._remove_owned_tree(runtime._root_path / quarantine.name, run_id)

    assert invalid.value.code == "cleanup_invalid"
    assert (detached / ".termroom" / "marker").read_text() == f"{run_id}\n"
    assert (detached / "work").is_dir()
    assert (quarantine / ".termroom" / "marker").read_text() == f"{run_id}\n"
    assert sentinel.read_bytes() == b"keep"


def test_remote_run_delete_unlinks_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    runtime, run_id, quarantine, sentinel = _quarantined_run(tmp_path)
    link = quarantine / "outside-link"
    link.symlink_to(sentinel)

    deleted = runtime.delete(_payload(runtime.run_root, run_id))

    assert deleted == {"deleted": True, "already_missing": False}
    assert not link.exists()
    assert not link.is_symlink()
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640


def test_node_remote_run_root_rejects_symlink_before_touching_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    outside.chmod(0o755)
    root = tmp_path / "managed-runs"
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(NodeRemoteRunError) as invalid:
        NodeRemoteRunRuntime(root)

    assert invalid.value.code == "run_root_invalid"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]


def test_node_remote_run_root_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    outside.chmod(0o755)
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)

    with pytest.raises(NodeRemoteRunError) as invalid:
        NodeRemoteRunRuntime(alias / "managed-runs")

    assert invalid.value.code == "run_root_invalid"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]


def test_node_remote_run_root_rejects_swap_at_permission_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "managed-runs"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    outside.chmod(0o755)
    moved_root = tmp_path / "original-managed-runs"
    original_open = node_remote_runs._open_directory_components
    replaced = False

    def replace_after_open(
        path: Path,
        *,
        create: bool,
        parent_fd: int | None = None,
        parent_path: Path | None = None,
        mode: int | None = 0o700,
    ) -> tuple[int, Path]:
        nonlocal replaced
        descriptor, candidate = original_open(
            path,
            create=create,
            parent_fd=parent_fd,
            parent_path=parent_path,
            mode=mode,
        )
        if path == root and create and not replaced:
            replaced = True
            path.rename(moved_root)
            path.symlink_to(outside, target_is_directory=True)
        return descriptor, candidate

    monkeypatch.setattr(node_remote_runs, "_open_directory_components", replace_after_open)
    with pytest.raises(NodeRemoteRunError) as invalid:
        NodeRemoteRunRuntime(root)

    assert invalid.value.code == "run_root_invalid"
    assert replaced
    assert root.is_symlink()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]


def test_node_remote_run_root_rejects_wrong_owner_before_chmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "managed-runs"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    actual_euid = os.geteuid()
    monkeypatch.setattr(node_remote_runs.os, "geteuid", lambda: actual_euid + 1)

    with pytest.raises(NodeRemoteRunError) as invalid:
        NodeRemoteRunRuntime(root)

    assert invalid.value.code == "run_root_invalid"
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    assert list(root.iterdir()) == []


def test_node_remote_run_rejects_wrong_owner_descendant_before_chmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "managed-runs"
    runtime = NodeRemoteRunRuntime(root)
    run_id = str(uuid.uuid4())
    runtime.create({**_payload(root, run_id), "command": "true", "cwd_rel": "."})
    metadata = root / run_id / ".termroom"
    original_entries = sorted(path.name for path in metadata.iterdir())
    metadata.chmod(0o755)
    sentinel = metadata / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    metadata.chmod(0o755)
    run_fd = os.open(root / run_id, os.O_RDONLY | os.O_DIRECTORY)
    actual_euid = os.geteuid()
    monkeypatch.setattr(node_remote_runs.os, "geteuid", lambda: actual_euid + 1)
    try:
        with pytest.raises(OSError):
            node_remote_runs._open_directory_at(
                run_fd, (".termroom",), create=False, mode=0o700
            )
    finally:
        os.close(run_fd)

    assert stat.S_IMODE(metadata.stat().st_mode) == 0o755
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert sorted(path.name for path in metadata.iterdir()) == sorted(
        [*original_entries, "sentinel"]
    )


def test_node_remote_run_root_preserves_real_directory_behavior(tmp_path: Path) -> None:
    root = tmp_path / "managed-runs"
    runtime = NodeRemoteRunRuntime(root)
    assert runtime.run_root == root
    assert root.is_dir()
    assert not root.is_symlink()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700

    root.chmod(0o755)
    accepted = NodeRemoteRunRuntime(root)
    assert accepted.run_root == root
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


@pytest.mark.parametrize("replacement_kind", ["symlink", "real"])
def test_node_remote_run_root_rejects_replacement_after_directory_validation(
    tmp_path: Path,
    replacement_kind: str,
) -> None:
    root = tmp_path / "managed-runs"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    outside.chmod(0o755)
    moved_root = tmp_path / "original-managed-runs"
    runtime = NodeRemoteRunRuntime(root)
    root.rename(moved_root)
    if replacement_kind == "symlink":
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir(mode=0o755)
        (root / "replacement-sentinel").write_bytes(b"keep")
    with pytest.raises(NodeRemoteRunError) as invalid:
        runtime.preflight({"remote_run_version": NODE_REMOTE_RUN_VERSION})

    assert invalid.value.code == "run_root_invalid"
    assert runtime.run_root == root
    assert root.is_symlink() is (replacement_kind == "symlink")
    if replacement_kind == "real":
        assert stat.S_IMODE(root.stat().st_mode) == 0o755
        assert (root / "replacement-sentinel").read_bytes() == b"keep"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]


def test_node_remote_run_rejects_replaced_metadata_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "managed-runs"
    runtime = NodeRemoteRunRuntime(root)
    run_id = str(uuid.uuid4())
    payload = _payload(root, run_id)
    runtime.create({**payload, "command": "true", "cwd_rel": "."})
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    outside.chmod(0o755)
    metadata = root / run_id / ".termroom"
    moved = tmp_path / "original-metadata"
    metadata.rename(moved)
    metadata.symlink_to(outside, target_is_directory=True)

    with pytest.raises(NodeRemoteRunError) as invalid:
        runtime.write_metadata({**payload, "name": "inputs.json", "value": {}})

    assert invalid.value.code == "layout_invalid"
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]


def test_node_remote_run_rejects_real_metadata_replacement_without_chmod(
    tmp_path: Path,
) -> None:
    root = tmp_path / "managed-runs"
    runtime = NodeRemoteRunRuntime(root)
    run_id = str(uuid.uuid4())
    payload = _payload(root, run_id)
    runtime.create({**payload, "command": "true", "cwd_rel": "."})
    metadata = root / run_id / ".termroom"
    metadata.rename(tmp_path / "original-metadata")
    replacement = tmp_path / "replacement-metadata"
    replacement.mkdir(mode=0o755)
    sentinel = replacement / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    replacement.chmod(0o755)
    replacement.rename(metadata)

    with pytest.raises(NodeRemoteRunError) as invalid:
        runtime.write_metadata({**payload, "name": "inputs.json", "value": {}})

    assert invalid.value.code == "layout_invalid"
    assert stat.S_IMODE(metadata.stat().st_mode) == 0o755
    replacement_sentinel = metadata / "sentinel"
    assert replacement_sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(replacement_sentinel.stat().st_mode) == 0o640
    assert sorted(path.name for path in metadata.iterdir()) == ["sentinel"]


def test_remote_run_start_rejects_replaced_output_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "managed-runs"
    runtime = NodeRemoteRunRuntime(root)
    run_id = str(uuid.uuid4())
    payload = _payload(root, run_id)
    runtime.create({**payload, "command": "true", "cwd_rel": "."})
    outside = tmp_path / "outside-output"
    outside.write_bytes(b"keep")
    outside.chmod(0o640)
    output = root / run_id / ".termroom" / "output.log"
    output.symlink_to(outside)
    runtime._tmux = lambda *args, check=True: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args, 1 if args[0] == "has-session" else 0, "", ""
    )

    with pytest.raises(NodeRemoteRunError) as invalid:
        runtime.start(payload)

    assert invalid.value.code == "metadata_invalid"
    assert output.is_symlink()
    assert outside.read_bytes() == b"keep"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o640
    assert sorted(path.name for path in outside.parent.iterdir()) == [
        "managed-runs", "outside-output"
    ]
    assert not list(output.parent.glob(".output.log.tmp-*"))


@pytest.mark.parametrize("action", ["interrupt", "kill", "observe"])
def test_remote_run_rejects_substituted_stop_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    root = tmp_path / "managed-runs"
    runtime = NodeRemoteRunRuntime(root)
    run_id = str(uuid.uuid4())
    payload = _payload(root, run_id)
    runtime.create({**payload, "command": "true", "cwd_rel": "."})
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    outside.chmod(0o755)
    stop = root / run_id / ".termroom" / "stop-requested-at"
    stop.symlink_to(sentinel)
    metadata_entries = sorted(path.name for path in stop.parent.iterdir())
    outside_entries = sorted(path.name for path in outside.iterdir())
    monkeypatch.setattr(
        runtime,
        "_tmux",
        lambda *args, check=True: subprocess.CompletedProcess(args, 1, "", ""),
    )

    with pytest.raises(NodeRemoteRunError) as invalid:
        getattr(runtime, action)(payload)

    assert invalid.value.code == "metadata_invalid"
    assert stop.is_symlink()
    assert stop.resolve() == sentinel
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert sorted(path.name for path in stop.parent.iterdir()) == metadata_entries
    assert sorted(path.name for path in outside.iterdir()) == outside_entries
    assert not list(stop.parent.glob(".stop-requested-at.tmp-*"))


def test_remote_run_metadata_write_rejects_replaced_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "managed-runs"
    runtime = NodeRemoteRunRuntime(root)
    run_id = str(uuid.uuid4())
    payload = _payload(root, run_id)
    runtime.create({**payload, "command": "true", "cwd_rel": "."})
    runtime.write_metadata({**payload, "name": "inputs.json", "value": {"v": 1}})

    metadata = root / run_id / ".termroom"
    record = metadata / "inputs.json"
    record_original = record.read_bytes()
    moved = tmp_path / "original-inputs.json"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    original_write = node_remote_runs._atomic_private_write_at

    def replace_after_validation(
        directory_fd: int,
        name: str,
        content: bytes,
        *,
        mode: int = 0o600,
        expected_state: object = node_remote_runs._PRIVATE_WRITE_UNSPECIFIED,
    ) -> tuple[int, ...]:
        assert name == "inputs.json"
        assert expected_state is not None
        record.rename(moved)
        record.symlink_to(sentinel)
        return original_write(
            directory_fd,
            name,
            content,
            mode=mode,
            expected_state=expected_state,
        )

    monkeypatch.setattr(node_remote_runs, "_atomic_private_write_at", replace_after_validation)
    with pytest.raises(NodeRemoteRunError) as invalid:
        runtime.write_metadata({**payload, "name": "inputs.json", "value": {"v": 2}})

    assert invalid.value.code == "metadata_invalid"
    assert record.is_symlink()
    assert record.resolve() == sentinel
    assert moved.read_bytes() == record_original
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]
    assert not list(metadata.glob(".inputs.json.tmp-*"))


def test_remote_run_work_identity_rejects_replacement_before_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "managed-runs"
    runtime = NodeRemoteRunRuntime(root)
    run_id = str(uuid.uuid4())
    runtime.create({**_payload(root, run_id), "command": "true", "cwd_rel": "."})
    _run_fd, metadata_fd = runtime._layout_handles(run_id)
    work_fd = runtime._work_dir_fds[run_id]
    identity = root / run_id / ".termroom" / "work.identity"
    original = identity.read_bytes()
    moved = tmp_path / "original-work-identity"
    identity.rename(moved)
    identity.write_bytes(b"replacement")
    identity.chmod(0o600)

    with pytest.raises(NodeRemoteRunError) as invalid:
        runtime._write_work_identity(metadata_fd, work_fd, "stable")

    assert invalid.value.code == "metadata_invalid"
    assert identity.read_bytes() == b"replacement"
    assert stat.S_IMODE(identity.stat().st_mode) == 0o600
    assert moved.read_bytes() == original
    assert not list(identity.parent.glob(".work.identity.tmp-*"))


def test_remote_run_private_write_does_not_clobber_new_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    path = tmp_path / "record"
    raced_bytes = b"preserve competing entry"
    original_link = os.link

    def install_before_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if destination == "record":
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dst_dir_fd,
            )
            try:
                os.write(descriptor, raced_bytes)
            finally:
                os.close(descriptor)
        original_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(node_remote_runs.os, "link", install_before_link)
    try:
        with pytest.raises(NodeRemoteRunError) as invalid:
            node_remote_runs._atomic_private_write_at(
                directory_fd, "record", b"new value", expected_state=None
            )
    finally:
        os.close(directory_fd)

    assert invalid.value.code == "metadata_invalid"
    assert path.read_bytes() == raced_bytes
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(item.name for item in tmp_path.iterdir()) == ["record"]


def test_remote_run_workspace_identity_rejects_real_directory_replacement(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "projects"
    allowed.mkdir()
    root = tmp_path / "managed-runs"
    runtime = NodeRuntime([allowed], remote_run_root=root)
    assert runtime.remote_runs is not None
    run_id = str(uuid.uuid4())
    payload = _payload(root, run_id)
    runtime.remote_runs.create({**payload, "command": "true", "cwd_rel": "."})
    runtime.remote_runs.snapshot_begin(payload)
    result = runtime.remote_runs.snapshot_commit(payload)
    work = Path(result["work_path"])
    assert runtime.remote_runs.validate_workspace(
        {"remote_run_id": run_id, "workspace_path": str(work)}
    ) == work

    moved = work.with_name("work-original")
    work.rename(moved)
    work.mkdir(mode=0o755)
    sentinel = work / "replacement-sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)

    workspace_payload = {
        "remote_run_id": run_id,
        "workspace_path": str(work),
        "tmux_session": f"termroom-node-test-{run_id[:12]}",
    }
    operations = (
        lambda: runtime._workspace_path(workspace_payload),
        lambda: runtime._handle_sync("files.list", workspace_payload),
        lambda: runtime._handle_sync("workspace.ensure", workspace_payload),
    )
    for operation in operations:
        with pytest.raises(NodeRemoteRunError) as invalid:
            operation()
        assert invalid.value.code == "layout_invalid"

    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640


def test_remote_run_snapshot_promotion_rejects_replaced_staging_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "managed-runs"
    runtime = NodeRemoteRunRuntime(root)
    runtime._tmux = lambda *args, check=True: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args, 1 if args[0] == "has-session" else 0, "", ""
    )
    run_id = str(uuid.uuid4())
    payload = _payload(root, run_id)
    runtime.create({**payload, "command": "true", "cwd_rel": "."})
    runtime.snapshot_begin(payload)
    runtime.snapshot_directory({**payload, "path": "approved"})
    run_fd = runtime._run_dir_fds[run_id]
    metadata_fd = runtime._metadata_dir_fds[run_id]
    staging_fd = runtime._work_staging_dir_fds[run_id]
    old_work_fd = runtime._work_dir_fds[run_id]
    expected_staging = os.fstat(staging_fd)
    expected_work = os.fstat(old_work_fd)
    original_copy = node_remote_runs._copy_snapshot_tree
    replaced = False

    def replace_staging(
        source_directory_fd: int,
        destination_directory_fd: int,
        parent: str = "",
    ) -> None:
        nonlocal replaced
        if source_directory_fd == staging_fd and not replaced:
            os.rename(
                "work.tmp",
                "work.tmp.original",
                src_dir_fd=run_fd,
                dst_dir_fd=run_fd,
            )
            os.mkdir("work.tmp", 0o700, dir_fd=run_fd)
            replacement_fd = os.open(
                "work.tmp", os.O_RDONLY | os.O_DIRECTORY, dir_fd=run_fd
            )
            try:
                sentinel_fd = os.open(
                    "sentinel",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=replacement_fd,
                )
                os.write(sentinel_fd, b"replacement")
                os.close(sentinel_fd)
            finally:
                os.close(replacement_fd)
            replaced = True
        original_copy(source_directory_fd, destination_directory_fd, parent)

    monkeypatch.setattr(node_remote_runs, "_copy_snapshot_tree", replace_staging)
    with pytest.raises(NodeRemoteRunError) as invalid:
        runtime.snapshot_commit(payload)

    assert invalid.value.code == "layout_invalid"
    assert replaced
    moved_stage = os.stat("work.tmp.original", dir_fd=run_fd, follow_symlinks=False)
    assert (moved_stage.st_dev, moved_stage.st_ino) == (
        expected_staging.st_dev,
        expected_staging.st_ino,
    )
    current_work = os.stat("work", dir_fd=run_fd, follow_symlinks=False)
    assert (current_work.st_dev, current_work.st_ino) == (
        expected_work.st_dev,
        expected_work.st_ino,
    )
    assert os.listdir(old_work_fd) == []
    replacement_fd = os.open(
        "work.tmp", os.O_RDONLY | os.O_DIRECTORY, dir_fd=run_fd
    )
    try:
        sentinel_fd = os.open("sentinel", os.O_RDONLY, dir_fd=replacement_fd)
        try:
            assert os.read(sentinel_fd, 32) == b"replacement"
        finally:
            os.close(sentinel_fd)
    finally:
        os.close(replacement_fd)
    state, recorded_work, pending_stage = runtime._parse_work_identity(
        node_remote_runs._read_regular_at(metadata_fd, "work.identity", 128)
    )
    assert state == "stable"
    assert recorded_work == (expected_work.st_dev, expected_work.st_ino)
    assert pending_stage is None
    with pytest.raises(NodeRemoteRunError) as invalid:
        runtime._assert_layout(run_id)
    assert invalid.value.code == "layout_invalid"


@pytest.mark.parametrize("operation", ["snapshot", "git"])
def test_remote_run_staging_creation_rejects_symlink_before_first_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    root = tmp_path / "managed-runs"
    runtime = NodeRemoteRunRuntime(root)
    run_id = str(uuid.uuid4())
    payload = _payload(root, run_id)
    runtime.create({**payload, "command": "true", "cwd_rel": "."})
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    outside.chmod(0o755)
    original_open = os.open
    replaced = False

    def replace_before_open(path: Any, *args: Any, **kwargs: Any) -> int:
        nonlocal replaced
        if path == "work.tmp" and not replaced:
            parent_fd = kwargs["dir_fd"]
            os.rename(
                "work.tmp",
                "work.tmp.original",
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.symlink(outside, "work.tmp", dir_fd=parent_fd)
            replaced = True
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(node_remote_runs.os, "open", replace_before_open)
    with pytest.raises(NodeRemoteRunError) as invalid:
        if operation == "snapshot":
            runtime.snapshot_begin(payload)
        else:
            runtime._create_git_work_staging(run_id)

    assert invalid.value.code == "layout_invalid"
    assert replaced
    assert (root / run_id / "work.tmp").is_symlink()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]
    assert run_id in runtime._invalid_run_ids


def test_remote_run_git_pending_rejects_replacement_after_agent_restart(
    tmp_path: Path,
) -> None:
    root = tmp_path / "managed-runs"
    runtime = NodeRemoteRunRuntime(root)
    run_id = str(uuid.uuid4())
    payload = _payload(root, run_id)
    runtime.create({**payload, "command": "true", "cwd_rel": "."})
    run_fd, metadata_fd = runtime._layout_handles(run_id)
    staging_fd = runtime._create_git_work_staging(run_id)
    runtime._work_staging_dir_fds[run_id] = staging_fd
    runtime._set_work_identity_state(run_id, "git-pending", staging_fd=staging_fd)
    paths = runtime._paths(run_id)
    runtime._write_private(
        paths["git_revision"], b"a" * 40 + b"\n", expected_state=None
    )
    staged = os.fstat(staging_fd)
    os.rename("work.tmp", "work", src_dir_fd=run_fd, dst_dir_fd=run_fd)
    work = root / run_id / "work"
    work.rename(root / run_id / "work.original")
    work.mkdir(mode=0o700)
    sentinel = work / "replacement-sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    replacement = work.stat()
    assert (replacement.st_dev, replacement.st_ino) != (staged.st_dev, staged.st_ino)
    # Model inode reuse: the replacement now has the old pending work identity.
    replacement_fd = os.open(work, os.O_RDONLY | os.O_DIRECTORY)
    try:
        runtime._write_work_identity(
            metadata_fd, replacement_fd, "git-pending", staging_fd=staging_fd
        )
    finally:
        os.close(replacement_fd)
    runtime._forget_layout_handles(run_id)

    restarted = NodeRemoteRunRuntime(root)

    with pytest.raises(NodeRemoteRunError) as invalid:
        restarted.validate_workspace(
            {"remote_run_id": run_id, "workspace_path": str(work)}
        )
    assert invalid.value.code == "layout_invalid"
    assert restarted.observe(payload)["layout_error"] == "layout_invalid"
    with pytest.raises(NodeRemoteRunError) as invalid:
        restarted.ensure_shell(payload)
    assert invalid.value.code == "layout_invalid"

    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640


def test_remote_run_snapshot_pending_rejects_restart_even_with_git_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "managed-runs"
    runtime = NodeRemoteRunRuntime(root)
    run_id = str(uuid.uuid4())
    payload = _payload(root, run_id)
    runtime.create({**payload, "command": "true", "cwd_rel": "."})
    runtime.snapshot_begin(payload)
    run_fd = runtime._run_dir_fds[run_id]
    metadata_fd = runtime._metadata_dir_fds[run_id]
    staging_fd = runtime._work_staging_dir_fds[run_id]
    staging_sentinel = os.open(
        "sentinel",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
        dir_fd=staging_fd,
    )
    os.write(staging_sentinel, b"keep")
    os.close(staging_sentinel)

    original_write = runtime._write_work_identity

    def fail_stable_write(
        current_metadata_fd: int,
        work_fd: int,
        state: str,
        *,
        staging_fd: int | None = None,
    ) -> None:
        if state == "stable":
            raise OSError("simulated interruption before stable identity")
        original_write(
            current_metadata_fd, work_fd, state, staging_fd=staging_fd
        )

    def interrupt_copy(
        _source_fd: int, _destination_fd: int, _parent: str = ""
    ) -> None:
        raise OSError("simulated snapshot interruption")

    monkeypatch.setattr(runtime, "_write_work_identity", fail_stable_write)
    monkeypatch.setattr(node_remote_runs, "_copy_snapshot_tree", interrupt_copy)
    with pytest.raises(OSError, match="snapshot interruption"):
        runtime.snapshot_commit(payload)

    identity = node_remote_runs._read_regular_at(metadata_fd, "work.identity", 128)
    state, _work_identity, _staging_identity = runtime._parse_work_identity(identity)
    assert state == "snapshot-pending"

    os.rmdir("work", dir_fd=run_fd)
    os.rename("work.tmp", "work", src_dir_fd=run_fd, dst_dir_fd=run_fd)
    paths = runtime._paths(run_id)
    runtime._write_private(
        paths["git_revision"], b"a" * 40 + b"\n", expected_state=None
    )
    runtime._forget_layout_handles(run_id)

    restarted = NodeRemoteRunRuntime(root)
    work = root / run_id / "work"
    with pytest.raises(NodeRemoteRunError) as invalid:
        restarted.validate_workspace(
            {"remote_run_id": run_id, "workspace_path": str(work)}
        )
    assert invalid.value.code == "layout_invalid"
    assert restarted.observe(payload)["layout_error"] == "layout_invalid"
    with pytest.raises(NodeRemoteRunError) as invalid:
        restarted.ensure_shell(payload)
    assert invalid.value.code == "layout_invalid"
    assert (work / "sentinel").read_bytes() == b"keep"


def test_remote_run_missing_work_identity_rejects_replacement_after_restart(
    tmp_path: Path,
) -> None:
    root = tmp_path / "managed-runs"
    runtime = NodeRemoteRunRuntime(root)
    run_id = str(uuid.uuid4())
    payload = _payload(root, run_id)
    runtime.create({**payload, "command": "true", "cwd_rel": "."})
    run_fd, metadata_fd = runtime._layout_handles(run_id)
    staging_fd = runtime._create_git_work_staging(run_id)
    runtime._work_staging_dir_fds[run_id] = staging_fd
    runtime._set_work_identity_state(run_id, "git-pending", staging_fd=staging_fd)
    paths = runtime._paths(run_id)
    runtime._write_private(
        paths["git_revision"], b"b" * 40 + b"\n", expected_state=None
    )
    os.rename("work.tmp", "work", src_dir_fd=run_fd, dst_dir_fd=run_fd)
    runtime._layout_handles(run_id)
    os.unlink("work.identity", dir_fd=metadata_fd)

    work = root / run_id / "work"
    work.rename(root / run_id / "work.original")
    work.mkdir(mode=0o700)
    sentinel = work / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    runtime._forget_layout_handles(run_id)
    restarted = NodeRemoteRunRuntime(root)

    with pytest.raises(NodeRemoteRunError) as invalid:
        restarted.validate_workspace(
            {"remote_run_id": run_id, "workspace_path": str(work)}
        )
    assert invalid.value.code == "layout_invalid"
    assert restarted.observe(payload)["layout_error"] == "layout_invalid"
    with pytest.raises(NodeRemoteRunError) as invalid:
        restarted.ensure_shell(payload)
    assert invalid.value.code == "layout_invalid"
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640


def test_remote_run_create_rejects_work_replacement_at_publication(
    tmp_path: Path,
) -> None:
    root = tmp_path / "managed-runs"
    runtime = NodeRemoteRunRuntime(root)
    run_id = str(uuid.uuid4())
    payload = _payload(root, run_id)
    work = root / run_id / "work"
    original_work = root / run_id / "work.original"
    sentinel = work / "sentinel"
    original_replace = runtime._replace
    replaced = False

    def replace_then_swap(source: Path, destination: Path) -> None:
        nonlocal replaced
        original_replace(source, destination)
        if source.name == f".termroom-creating-{run_id}" and destination.name == run_id:
            work.rename(original_work)
            work.mkdir(mode=0o700)
            sentinel.write_bytes(b"replacement")
            replaced = True

    runtime._replace = replace_then_swap  # type: ignore[method-assign]
    with pytest.raises(NodeRemoteRunError) as invalid:
        runtime.create({**payload, "command": "true", "cwd_rel": "."})

    assert invalid.value.code == "layout_invalid"
    assert replaced
    metadata_fd = os.open(
        root / run_id / ".termroom", os.O_RDONLY | os.O_DIRECTORY
    )
    try:
        identity = node_remote_runs._read_regular_at(
            metadata_fd, "work.identity", 128
        )
    finally:
        os.close(metadata_fd)
    state, recorded_work, staging = runtime._parse_work_identity(identity)
    replacement = work.stat()
    original = original_work.stat()
    assert state == "stable"
    assert staging is None
    assert recorded_work == (original.st_dev, original.st_ino)
    assert recorded_work != (replacement.st_dev, replacement.st_ino)
    assert sentinel.read_bytes() == b"replacement"
    assert run_id not in runtime._run_dir_fds
    with pytest.raises(NodeRemoteRunError) as invalid:
        runtime.validate_workspace(
            {"remote_run_id": run_id, "workspace_path": str(work)}
        )
    assert invalid.value.code == "layout_invalid"
    with pytest.raises(NodeRemoteRunError) as invalid:
        runtime.start(payload)
    assert invalid.value.code == "layout_invalid"


def test_remote_run_create_rejects_work_symlink_before_first_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "managed-runs"
    runtime = NodeRemoteRunRuntime(root)
    run_id = str(uuid.uuid4())
    payload = _payload(root, run_id)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    outside.chmod(0o755)
    original_open = os.open
    replaced = False

    def replace_before_open(path: Any, *args: Any, **kwargs: Any) -> int:
        nonlocal replaced
        if path == "work" and not replaced:
            parent_fd = kwargs["dir_fd"]
            os.rename("work", "work.original", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.symlink(outside, "work", dir_fd=parent_fd)
            replaced = True
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(node_remote_runs.os, "open", replace_before_open)
    with pytest.raises(NodeRemoteRunError) as invalid:
        runtime.create({**payload, "command": "true", "cwd_rel": "."})

    assert invalid.value.code == "layout_invalid"
    assert replaced
    creating_root = root / f".termroom-creating-{run_id}"
    assert not creating_root.exists()
    assert not (root / run_id).exists()
    assert run_id in runtime._invalid_run_ids
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]


@pytest.mark.parametrize("boundary", ["publication-check", "final-layout-check"])
def test_remote_run_create_rejects_replaced_published_root_on_retry(
    tmp_path: Path,
    boundary: str,
) -> None:
    root = tmp_path / "managed-runs"
    runtime = NodeRemoteRunRuntime(root)
    run_id = str(uuid.uuid4())
    payload = _payload(root, run_id)
    published = root / run_id
    detached = tmp_path / "detached-original-run"
    original_replace = runtime._replace
    replaced = False

    def replace_full_root() -> None:
        nonlocal replaced
        published.rename(detached)
        shutil.copytree(detached, published)
        sentinel = published / "work" / "sentinel"
        sentinel.write_bytes(b"replacement")
        metadata_fd = os.open(
            published / ".termroom", os.O_RDONLY | os.O_DIRECTORY
        )
        work_fd = os.open(published / "work", os.O_RDONLY | os.O_DIRECTORY)
        try:
            runtime._write_work_identity(metadata_fd, work_fd, "stable")
        finally:
            os.close(metadata_fd)
            os.close(work_fd)
        replaced = True

    if boundary == "publication-check":
        def replace_before_publication_check(source: Path, destination: Path) -> None:
            original_replace(source, destination)
            if destination.name == run_id and not replaced:
                replace_full_root()

        runtime._replace = replace_before_publication_check  # type: ignore[method-assign]
    else:
        original_assert_layout = runtime._assert_layout

        def replace_before_final_layout_check(candidate_run_id: str) -> dict[str, Path]:
            if candidate_run_id == run_id and run_id in runtime._run_dir_fds and not replaced:
                replace_full_root()
            return original_assert_layout(candidate_run_id)

        runtime._assert_layout = replace_before_final_layout_check  # type: ignore[method-assign]

    with pytest.raises(NodeRemoteRunError) as invalid:
        runtime.create({**payload, "command": "true", "cwd_rel": "."})
    assert invalid.value.code == "layout_invalid"
    assert replaced
    assert run_id in runtime._invalid_run_ids

    sentinel = published / "work" / "sentinel"
    assert sentinel.read_bytes() == b"replacement"
    with pytest.raises(NodeRemoteRunError) as retry:
        runtime.create({**payload, "command": "true", "cwd_rel": "."})
    assert retry.value.code == "layout_invalid"
    assert sentinel.read_bytes() == b"replacement"


@pytest.mark.parametrize(
    ("mode", "leaf", "descriptor_index"),
    [
        ("run", "command.sh", 4),
        ("git", "git-argv", 7),
        ("git", "git-path", 8),
        ("git", "git-askpass", 9),
    ],
)
def test_remote_run_handoff_uses_sealed_metadata_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    leaf: str,
    descriptor_index: int,
) -> None:
    root = tmp_path / "managed-runs"
    run_id = str(uuid.uuid4())
    payload = _payload(root, run_id)
    sentinel = tmp_path / "attacker-ran"
    observed: list[bytes] = []

    def handoff(
        tmux_args: tuple[str, ...],
        descriptors: tuple[int, ...],
        _command: tuple[str, ...] = (),
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        path = root / run_id / ".termroom" / leaf
        original = path.read_bytes()
        before = path.stat()
        descriptor = descriptors[descriptor_index]
        seals = fcntl.fcntl(descriptor, getattr(fcntl, "F_GET_SEALS", 1034))
        required = (
            getattr(fcntl, "F_SEAL_WRITE", 0x0008)
            | getattr(fcntl, "F_SEAL_GROW", 0x0004)
            | getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
            | getattr(fcntl, "F_SEAL_SEAL", 0x0001)
        )
        assert seals & required == required
        with path.open("r+b") as handle:
            handle.seek(0)
            handle.truncate()
            handle.write(f"touch {sentinel}\n".encode())
        after = path.stat()
        assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
        os.lseek(descriptor, 0, os.SEEK_SET)
        sealed_content = os.read(descriptor, 1024 * 1024)
        os.lseek(descriptor, 0, os.SEEK_SET)
        assert sealed_content == original
        observed.append(sealed_content)
        return subprocess.CompletedProcess(tmux_args, 0, "", "")

    runtime = NodeRemoteRunRuntime(root, descriptor_handoff=handoff)
    runtime._tmux = lambda *args, check=True: subprocess.CompletedProcess(  # type: ignore[method-assign]
        args, 1 if args[0] == "has-session" else 0, "", ""
    )
    runtime.create({**payload, "command": "printf 'approved\\n'", "cwd_rel": "."})
    if mode == "git":
        fake_git = tmp_path / "fake-git"
        fake_git.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_git.chmod(0o700)
        monkeypatch.setattr(node_remote_runs.shutil, "which", lambda _name: str(fake_git))
        runtime.start_git({**payload, "url": "https://example.test/public.git"})
    else:
        runtime.start(payload)

    assert len(observed) == 1
    assert not sentinel.exists()


@pytest.mark.parametrize("mutation", ["replace", "hardlink"])
def test_remote_run_metadata_read_rejects_leaf_change_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    path = tmp_path / "record"
    path.write_bytes(b"admitted")
    path.chmod(0o600)
    moved = tmp_path / "original-record"
    alias = tmp_path / "outside-record"
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_open = os.open
    original_read = os.read
    opened: set[int] = set()
    changed = False

    def track_open(*args: Any, **kwargs: Any) -> int:
        descriptor = original_open(*args, **kwargs)
        if args[0] == "record":
            opened.add(descriptor)
        return descriptor

    def mutate_after_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        content = original_read(descriptor, size)
        if descriptor in opened and not changed:
            changed = True
            if mutation == "replace":
                path.rename(moved)
                path.write_bytes(b"replacement")
                path.chmod(0o600)
            else:
                os.link(path, alias)
        return content

    monkeypatch.setattr(node_remote_runs.os, "open", track_open)
    monkeypatch.setattr(node_remote_runs.os, "read", mutate_after_read)
    try:
        with pytest.raises(NodeRemoteRunError) as invalid:
            node_remote_runs._read_regular_at(directory_fd, "record", 1024)
    finally:
        os.close(directory_fd)

    assert invalid.value.code == "metadata_invalid"
    assert changed
    if mutation == "replace":
        assert moved.read_bytes() == b"admitted"
        assert path.read_bytes() == b"replacement"
    else:
        assert alias.read_bytes() == b"admitted"
        assert path.stat().st_nlink == 2
        assert stat.S_IMODE(alias.stat().st_mode) == 0o600


def _large_source_manifest() -> list[dict[str, Any]]:
    return [
        {
            "path": f"payload/{index:04d}-{'x' * 180}.txt",
            "kind": "file",
            "size": 1,
            "mtime_ns": 1,
            "executable": False,
        }
        for index in range(2_100)
    ]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.asyncio
async def test_node_remote_run_snapshot_lifecycle_workspace_bridge_and_cleanup(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "projects"
    allowed.mkdir()
    run_root = tmp_path / "managed-runs"
    runtime = NodeRuntime([allowed], remote_run_root=run_root)
    run_id = str(uuid.uuid4())
    payload = _payload(run_root, run_id)

    preflight = await _operation(
        runtime,
        "remote_run.preflight",
        {"remote_run_version": NODE_REMOTE_RUN_VERSION, "require_git": False},
    )
    assert preflight["run_base"] == str(run_root.resolve())
    assert preflight["remote_run_version"] == NODE_REMOTE_RUN_VERSION
    with pytest.raises(NodeRemoteRunError) as wrong_root:
        await _operation(
            runtime,
            "remote_run.create",
            {
                **payload,
                "run_base": str(tmp_path / "other"),
                "command": "exit 0",
                "cwd_rel": ".",
            },
        )
    assert wrong_root.value.code == "run_root_mismatch"

    await _operation(
        runtime,
        "remote_run.create",
        {**payload, "command": "cat nested/message.txt", "cwd_rel": "."},
    )
    await _operation(runtime, "remote_run.snapshot.begin", payload)
    await _operation(
        runtime,
        "remote_run.snapshot.mkdir",
        {**payload, "path": "nested"},
    )
    await _write_snapshot_file(runtime, payload, "nested/message.txt", b"from-node\n")
    await _operation(
        runtime,
        "remote_run.snapshot.symlink",
        {**payload, "path": "message-link", "link_target": "nested/message.txt"},
    )
    await _operation(runtime, "remote_run.snapshot.commit", payload)
    await _operation(
        runtime,
        "remote_run.metadata.write",
        {
            **payload,
            "name": "source-manifest.json",
            "value": [
                {"path": "nested", "kind": "directory", "size": 0},
                {"path": "nested/message.txt", "kind": "file", "size": 10},
                {
                    "path": "message-link",
                    "kind": "symlink",
                    "size": 0,
                    "link_target": "nested/message.txt",
                },
            ],
        },
    )

    await _operation(runtime, "remote_run.start", payload)
    replay = await _operation(runtime, "remote_run.start", payload)
    assert replay["replayed"] is True
    observed = await _wait_for_terminal(runtime, payload)
    assert observed["state"] == "finished"
    assert observed["exit_code"] == 0

    polled = await _operation(
        runtime,
        "remote_run.poll",
        {**payload, "stream": "command", "offset": 0, "limit": 1024},
    )
    assert polled["log"]["chunk_b64"]

    shell = await _operation(
        runtime,
        "remote_run.ensure_shell",
        {**payload, "allow_create_session": True},
    )
    assert shell["session_name"] == f"termroom-run-{run_id}"
    assert {item["role"] for item in shell["terminals"]} == {"remote_run", "shell"}

    listed = await _operation(
        runtime,
        "files.list",
        {
            "remote_run_id": run_id,
            "workspace_path": shell["work_path"],
            "path": ".",
        },
    )
    assert {entry["name"] for entry in listed["entries"]} == {"nested"}
    assert (run_root / run_id / "work" / "message-link").is_symlink()

    with pytest.raises(NodeRemoteRunError) as outside:
        await _operation(
            runtime,
            "files.list",
            {
                "remote_run_id": run_id,
                "workspace_path": str(allowed),
                "path": ".",
            },
        )
    assert outside.value.code == "path_outside"

    deleted = await _operation(runtime, "remote_run.delete", payload)
    assert deleted["deleted"] is True
    assert not (run_root / run_id).exists()


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.asyncio
async def test_node_remote_run_start_is_idempotent_and_interrupt_targets_owned_run(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "projects"
    allowed.mkdir()
    run_root = tmp_path / "managed-runs"
    runtime = NodeRuntime([allowed], remote_run_root=run_root)
    run_id = str(uuid.uuid4())
    payload = _payload(run_root, run_id)
    await _operation(
        runtime,
        "remote_run.create",
        {
            **payload,
            "command": "printf 'once\\n' >> count.txt; sleep 30",
            "cwd_rel": ".",
        },
    )
    await _operation(runtime, "remote_run.snapshot.begin", payload)
    await _operation(runtime, "remote_run.snapshot.commit", payload)
    await _operation(runtime, "remote_run.start", payload)
    replay = await _operation(runtime, "remote_run.start", payload)
    assert replay["replayed"] is True

    deadline = time.monotonic() + 5
    count = run_root / run_id / "work" / "count.txt"
    while not count.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert count.read_text(encoding="utf-8") == "once\n"

    interrupted = await _operation(runtime, "remote_run.interrupt", payload)
    assert interrupted == {"sent": True, "completed": False}
    observed = await _wait_for_terminal(runtime, payload)
    assert observed["state"] == "stopped"
    assert count.read_text(encoding="utf-8") == "once\n"
    deleted = await _operation(runtime, "remote_run.delete", payload)
    assert deleted == {"deleted": True, "already_missing": False}
    assert not (run_root / run_id).exists()
    assert not (run_root / f".termroom-deleting-{run_id}").exists()
    assert runtime.remote_runs is not None
    session = runtime.remote_runs._session_name(run_id)
    assert runtime.remote_runs._tmux("has-session", "-t", session, check=False).returncode != 0
    assert await _operation(runtime, "remote_run.delete", payload) == {
        "deleted": True,
        "already_missing": True,
    }


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.asyncio
async def test_node_remote_run_public_git_uses_fixed_node_side_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = tmp_path / "projects"
    allowed.mkdir()
    run_root = tmp_path / "managed-runs"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        "if test \"${1:-}\" = '-C'; then\n"
        "  printf '0123456789abcdef0123456789abcdef01234567\\n'\n"
        "  exit 0\n"
        "fi\n"
        "last=''\n"
        "for value in \"$@\"; do last=$value; done\n"
        "mkdir -p -- \"$last\"\n"
        "printf 'from-fake-git\\n' > \"$last/source.txt\"\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o700)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    runtime = NodeRuntime([allowed], remote_run_root=run_root)
    run_id = str(uuid.uuid4())
    payload = _payload(run_root, run_id)
    await _operation(
        runtime,
        "remote_run.create",
        {**payload, "command": "cat source.txt", "cwd_rel": "."},
    )
    started = await _operation(
        runtime,
        "remote_run.git.start",
        {**payload, "url": "https://example.test/public.git"},
    )
    assert started["phase"] == "cloning"
    observed = await _wait_for_terminal(runtime, payload)
    assert observed["state"] == "finished"
    assert observed["source_revision"] == "0123456789abcdef0123456789abcdef01234567"
    assert (run_root / run_id / "work" / "source.txt").read_text() == "from-fake-git\n"
    await _operation(runtime, "remote_run.delete", payload)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.asyncio
async def test_node_remote_run_refuses_an_unowned_tmux_session_collision(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "projects"
    allowed.mkdir()
    run_root = tmp_path / "managed-runs"
    runtime = NodeRuntime([allowed], remote_run_root=run_root)
    run_id = str(uuid.uuid4())
    payload = _payload(run_root, run_id)
    await _operation(
        runtime,
        "remote_run.create",
        {**payload, "command": "exit 0", "cwd_rel": "."},
    )
    await _operation(runtime, "remote_run.snapshot.begin", payload)
    await _operation(runtime, "remote_run.snapshot.commit", payload)
    session = f"termroom-run-{run_id}"
    subprocess_result = runtime._tmux(  # type: ignore[attr-defined]
        "new-session", "-d", "-s", session, "-c", str(allowed), "-n", "user-shell"
    )
    assert subprocess_result.returncode == 0
    try:
        with pytest.raises(NodeRemoteRunError) as conflict:
            await _operation(runtime, "remote_run.start", payload)
        assert conflict.value.code == "session_identity_conflict"
        assert runtime._tmux(  # type: ignore[attr-defined]
            "has-session", "-t", session, check=False
        ).returncode == 0
        await _operation(runtime, "remote_run.delete", payload)
        assert runtime._tmux(  # type: ignore[attr-defined]
            "has-session", "-t", session, check=False
        ).returncode == 0
    finally:
        runtime._tmux("kill-session", "-t", session, check=False)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_node_remote_run_streams_large_metadata_and_cleans_aborted_upload(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "projects"
    allowed.mkdir()
    run_root = tmp_path / "managed-runs"
    runtime = NodeRuntime([allowed], remote_run_root=run_root)
    run_id = str(uuid.uuid4())
    payload = _payload(run_root, run_id)
    await _operation(
        runtime,
        "remote_run.create",
        {**payload, "command": "exit 0", "cwd_rel": "."},
    )

    manifest = _large_source_manifest()
    encoded = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    assert 512 * 1024 < len(encoded) < 8 * 1024 * 1024
    stream_id = uuid.uuid4().hex
    opened = await runtime.handle(
        "remote_run.metadata.open",
        {
            **payload,
            "stream_id": stream_id,
            "name": "source-manifest.json",
            "expected_size": len(encoded),
        },
        _unused_send,
    )
    assert opened.value == {"stream_id": stream_id}
    stream = runtime.streams[stream_id]
    for offset in range(0, len(encoded), 64 * 1024):
        await stream.feed(encoded[offset : offset + 64 * 1024])
    assert await stream.close() == {"size": len(encoded)}
    assert stream_id not in runtime.streams
    assert (
        run_root / run_id / ".termroom" / "source-manifest.json"
    ).read_bytes() == encoded

    aborted_id = uuid.uuid4().hex
    aborted = await runtime.handle(
        "remote_run.metadata.open",
        {
            **payload,
            "stream_id": aborted_id,
            "name": "inputs.json",
            "expected_size": len(encoded),
        },
        _unused_send,
    )
    assert aborted.value == {"stream_id": aborted_id}
    aborted_stream = runtime.streams[aborted_id]
    temporary = run_root / run_id / ".termroom" / aborted_stream.temporary_name
    await aborted_stream.feed(encoded[:64])
    await aborted_stream.abort()
    assert aborted_id not in runtime.streams
    assert not temporary.exists()
    assert not (run_root / run_id / ".termroom" / "inputs.json").exists()
    interrupted = await _operation(runtime, "remote_run.observe", payload)
    assert interrupted["state"] == "preparing"
    assert interrupted["tmux_exists"] is False
    assert interrupted["record_errors"] == []


def test_node_remote_run_client_uses_stream_above_inline_metadata_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _large_source_manifest()
    encoded = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    assert len(encoded) > 512 * 1024
    sent = bytearray()
    finished = 0
    aborted = 0

    class RecordingStream:
        async def send(self, data: bytes) -> None:
            sent.extend(data)

        async def finish(self) -> dict[str, int]:
            nonlocal finished
            finished += 1
            return {"size": len(sent)}

        async def abort(self) -> None:
            nonlocal aborted
            aborted += 1

    stream = RecordingStream()
    client = NodeRemoteRunClient(object())  # type: ignore[arg-type]

    def refuse_inline(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise AssertionError("Large metadata must not use an inline request")

    def open_stream(
        _computer: object,
        operation: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, str], RecordingStream]:
        assert operation == "remote_run.metadata.open"
        assert payload["name"] == "source-manifest.json"
        assert payload["expected_size"] == len(encoded)
        return {"stream_id": "metadata-stream"}, stream

    monkeypatch.setattr(client, "_request", refuse_inline)
    monkeypatch.setattr(client, "_open_stream", open_stream)
    monkeypatch.setattr(client, "_submit", lambda awaitable: asyncio.run(awaitable))
    run_id = str(uuid.uuid4())
    run_base = str((tmp_path / "managed-runs").resolve())

    path = client.write_remote_run_json(
        {"id": "node-id"},
        run_base,
        run_id,
        "source-manifest.json",
        manifest,
    )

    assert path == f"{run_base}/{run_id}/.termroom/source-manifest.json"
    assert bytes(sent) == encoded
    assert finished == 1
    assert aborted == 0


def test_node_workspace_source_preserves_changed_file_metadata_for_one_retry(
    tmp_path: Path,
) -> None:
    initial_entry = {
        "path": "payload.txt",
        "kind": "file",
        "size": 3,
        "mtime_ns": 10,
        "executable": False,
    }
    current_entry = {**initial_entry, "size": 7, "mtime_ns": 20}
    manifest_line = json.dumps(initial_entry, separators=(",", ":")).encode() + b"\n"
    file_open_count = 0
    opened_streams: list[ReadStream] = []

    class ReadStream:
        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = list(chunks)
            self.closed = False
            self.credit_batches: list[int] = []

        async def receive(self) -> bytes | None:
            if self.chunks:
                return self.chunks.pop(0)
            self.closed = True
            return None

        async def control(self, kind: str, **values: Any) -> None:
            assert not self.closed
            assert kind == "credit"
            count = values.get("count")
            assert type(count) is int
            assert 1 <= count <= NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW
            self.credit_batches.append(count)

        async def abort(self) -> None:
            self.closed = True

    class SourceClient:
        stream_window = NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW

        def _open_stream(
            self,
            _computer: object,
            operation: str,
            payload: dict[str, Any],
        ) -> tuple[dict[str, Any], ReadStream]:
            nonlocal file_open_count
            if operation == "remote_run_source.manifest.open":
                assert "remote_run_id" not in payload
                stream = ReadStream([manifest_line[:5], manifest_line[5:]])
                opened_streams.append(stream)
                return (
                    {
                        "remote_run_source_version": 1,
                        "stream_window": self.stream_window,
                        "frame_count": 2,
                        "entry_count": 1,
                        "total_bytes": 3,
                    },
                    stream,
                )
            assert operation == "remote_run_source.file.open"
            file_open_count += 1
            if file_open_count == 1:
                assert payload["expected_size"] == 3
                raise NodeRemoteRunError(
                    "Source file changed", code="source_file_changed"
                )
            assert payload["expected_size"] == 7
            assert payload["expected_mtime_ns"] == 20
            stream = ReadStream([b"updated"])
            opened_streams.append(stream)
            return (
                {
                    "remote_run_source_version": 1,
                    "stream_window": self.stream_window,
                    "frame_count": 1,
                    "size": 7,
                    "mtime_ns": 20,
                },
                stream,
            )

        def _request(
            self,
            _computer: object,
            operation: str,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            assert operation == "remote_run_source.stat"
            assert payload["path"] == "payload.txt"
            return {
                "remote_run_source_version": 1,
                "entry": current_entry,
            }

        @staticmethod
        def _submit(awaitable: Any) -> Any:
            return asyncio.run(awaitable)

    class RecordingSink:
        def __init__(self) -> None:
            self.files: dict[str, bytes] = {}

        def make_directory(self, relative_path: str, *, executable: bool) -> None:
            raise AssertionError((relative_path, executable))

        def make_symlink(self, relative_path: str, link_target: str) -> None:
            raise AssertionError((relative_path, link_target))

        def write_file(
            self,
            relative_path: str,
            chunks: Any,
            *,
            executable: bool,
            expected_size: int,
        ) -> None:
            content = b"".join(chunks)
            assert len(content) == expected_size
            assert executable is False
            self.files[relative_path] = content

    client = SourceClient()
    source = NodeWorkspaceSnapshotSource(
        client,  # type: ignore[arg-type]
        {
            "id": "persistent-workspace",
            "path": str(tmp_path / "source"),
            "computer": {"id": "node-source", "connection_method": "node"},
        },
        ".",
        explicitly_included=(),
    )
    sink = RecordingSink()

    manifest = materialize_workspace_snapshot(source, sink)

    assert file_open_count == 2
    assert manifest.total_bytes == 7
    assert manifest.entries[0].size == 7
    assert manifest.entries[0].mtime_ns == 20
    assert sink.files == {"payload.txt": b"updated"}
    assert [stream.credit_batches for stream in opened_streams] == [[2], [1]]

    client.stream_window += 1
    with pytest.raises(NodeRemoteRunError) as incompatible:
        source.scan()
    assert incompatible.value.code == "remote_run_source_version_incompatible"
    assert opened_streams[-1].credit_batches == []


def test_remote_run_rejects_target_only_node_workspace_source_server_side(
    tmp_path: Path,
) -> None:
    target = {"id": "ssh-target", "connection_method": "ssh"}
    source_computer = {
        "id": "node-source",
        "connection_method": "node",
        "node_revoked_at": None,
    }
    workspace = {
        "id": "persistent-workspace",
        "backend_kind": "remote",
        "computer": source_computer,
        "display_name": "Node Source",
        "canonical_path": "/srv/source",
        "path": "/srv/source",
        "transient": False,
        "is_remote_run": False,
    }

    class Store:
        @staticmethod
        def get_computer(computer_id: str) -> dict[str, Any] | None:
            return target if computer_id == target["id"] else None

    class Workspaces:
        @staticmethod
        def require(workspace_id: str) -> dict[str, Any]:
            if workspace_id != workspace["id"]:
                raise KeyError(workspace_id)
            return workspace

    class NodeRuns:
        supported = False

        def supports_remote_run_source(self, computer: object) -> bool:
            assert computer is source_computer
            return self.supported

    node_runs = NodeRuns()
    manager = RemoteRunManager(
        Store(),  # type: ignore[arg-type]
        Workspaces(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        node_runs,  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        max_archive_bytes=1024,
    )
    payload = {
        "id": str(uuid.uuid4()),
        "source_kind": "workspace",
        "source_workspace_id": workspace["id"],
        "source_path": ".",
        "target_computer_id": target["id"],
        "command": "exit 0",
    }

    with pytest.raises(RemoteRunError) as unsupported:
        manager._normalize_create_payload(payload)  # type: ignore[attr-defined]
    assert unsupported.value.code == "capability_unsupported"

    node_runs.supported = True
    normalized = manager._normalize_create_payload(payload)  # type: ignore[attr-defined]
    assert normalized["source_workspace_id"] == workspace["id"]
    assert normalized["source_path"] == "."


def test_interrupted_node_source_never_promotes_target_staging_or_starts_command(
    tmp_path: Path,
) -> None:
    run_id = str(uuid.uuid4())
    source_computer = {"id": "node-source", "connection_method": "node"}
    target = {
        "id": "ssh-target",
        "connection_method": "ssh",
        "run_base_dir": str(tmp_path / "target-runs"),
    }
    workspace = {
        "id": "persistent-workspace",
        "backend_kind": "remote",
        "computer_id": source_computer["id"],
        "computer": source_computer,
        "display_name": "Node Source",
        "canonical_path": "/srv/source",
        "remote_path": "/srv/source",
        "path": "/srv/source",
        "transient": False,
    }
    manifest = build_workspace_manifest(
        [WorkspaceEntry("payload.bin", "file", size=8, mtime_ns=1)]
    )

    class InterruptedSource:
        closed = False

        @staticmethod
        def scan():  # type: ignore[no-untyped-def]
            return manifest

        @staticmethod
        def iter_file_chunks(
            _entry: WorkspaceEntry, *, chunk_size: int
        ):  # type: ignore[no-untyped-def]
            assert chunk_size > 0
            yield b"part"
            raise NodeRemoteRunError(
                "Node disconnected during Source transfer", code="node_offline"
            )

    source = InterruptedSource()

    class SourceClient:
        @contextlib.contextmanager
        def remote_workspace_snapshot_source(
            self, selected: object, path: str, *, explicitly_included: object
        ):  # type: ignore[no-untyped-def]
            assert selected is workspace
            assert path == "."
            assert tuple(explicitly_included) == ()  # type: ignore[arg-type]
            try:
                yield source
            finally:
                source.closed = True

    class Store:
        transitions: list[dict[str, Any]] = []

        @classmethod
        def transition_remote_run(
            cls, selected_run_id: str, **values: Any
        ) -> bool:
            assert selected_run_id == run_id
            cls.transitions.append(values)
            return True

    class Workspaces:
        @staticmethod
        def require(workspace_id: str) -> dict[str, Any]:
            assert workspace_id == workspace["id"]
            return workspace

    class Sink:
        def __init__(self, staging: Path) -> None:
            self.staging = staging

        def make_directory(self, relative_path: str, *, executable: bool) -> None:
            del relative_path, executable

        def make_symlink(self, relative_path: str, link_target: str) -> None:
            del relative_path, link_target

        def write_file(
            self,
            relative_path: str,
            chunks: Any,
            *,
            executable: bool,
            expected_size: int,
        ) -> None:
            del executable, expected_size
            target_file = self.staging / relative_path
            temporary = target_file.with_suffix(".partial")
            try:
                with temporary.open("wb") as handle:
                    for chunk in chunks:
                        handle.write(chunk)
                temporary.replace(target_file)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise

    class TargetBackend:
        committed = False
        started = False
        metadata_written = False

        @staticmethod
        def preflight_remote_run_target(
            _computer: object, *, run_base_dir: str | None = None, require_git: bool = False
        ) -> dict[str, Any]:
            del require_git
            return {
                "run_base": run_base_dir,
                "available_bytes": 1024 * 1024,
            }

        @staticmethod
        def create_remote_run_layout(
            _computer: object,
            selected_run_id: str,
            *,
            run_base_dir: str | None = None,
            command: str | None = None,
            cwd_rel: str = ".",
        ) -> dict[str, str]:
            assert selected_run_id == run_id
            assert command == "touch COMMAND_STARTED"
            assert cwd_rel == "."
            root = Path(str(run_base_dir)) / selected_run_id
            (root / "work").mkdir(parents=True)
            return {
                "root": str(root),
                "work": str(root / "work"),
                "work_staging": str(root / "work.tmp"),
            }

        @contextlib.contextmanager
        def remote_run_snapshot_sink(
            self, _computer: object, run_base: str, selected_run_id: str
        ):  # type: ignore[no-untyped-def]
            staging = Path(run_base) / selected_run_id / "work.tmp"
            staging.mkdir()
            yield Sink(staging)

        def commit_remote_run_snapshot(
            self, _computer: object, run_base: str, selected_run_id: str
        ) -> str:
            self.committed = True
            staging = Path(run_base) / selected_run_id / "work.tmp"
            work = staging.with_name("work")
            work.rmdir()
            staging.replace(work)
            return str(work)

        def write_remote_run_json(self, *_args: object, **_kwargs: object) -> str:
            self.metadata_written = True
            return "metadata"

        def start_remote_run(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
            self.started = True
            return {"state": "running"}

    target_backend = TargetBackend()
    manager = RemoteRunManager(
        Store(),  # type: ignore[arg-type]
        Workspaces(),  # type: ignore[arg-type]
        target_backend,  # type: ignore[arg-type]
        SourceClient(),  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        max_archive_bytes=1024,
    )
    run = {
        "id": run_id,
        "source_kind": "workspace",
        "source_workspace_id": workspace["id"],
        "source_path": ".",
        "source_options_json": '{"policy":1,"explicitly_included":[]}',
        "source_label": "Node Source",
        "target_computer_id": target["id"],
        "target": target,
        "run_base": target["run_base_dir"],
        "command": "touch COMMAND_STARTED",
    }

    with pytest.raises(NodeRemoteRunError) as interrupted:
        manager._prepare_workspace(run, target, asyncio.Event())  # type: ignore[arg-type,attr-defined]

    assert interrupted.value.code == "node_offline"
    target_root = Path(str(target["run_base_dir"])) / run_id
    assert source.closed is True
    assert (target_root / "work").is_dir()
    assert list((target_root / "work").iterdir()) == []
    assert (target_root / "work.tmp").is_dir()
    assert list((target_root / "work.tmp").iterdir()) == []
    assert target_backend.committed is False
    assert target_backend.metadata_written is False
    assert target_backend.started is False
    assert not (target_root / "COMMAND_STARTED").exists()
