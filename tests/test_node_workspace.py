from __future__ import annotations

import asyncio
import base64
import fcntl
import gc
import hashlib
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
from starlette.websockets import WebSocketDisconnect

from termroom import node_agent
from termroom.file_runs import RUNNER_REGISTRY_VERSION
from termroom.files import DirectoryListingLimitError
from termroom.node_agent import (
    NodeAgent,
    NodeAgentError,
    NodeConfig,
    NodeRuntime,
    TerminalAgentStream,
    ensure_node_identity,
    node_session_is_valid,
    normalize_allowed_roots,
    save_node_config,
)
from termroom.node_core import NODE_STREAM_QUEUE_DEPTH, NodeCoreError
from termroom.node_protocol import (
    NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW,
    NODE_REMOTE_RUN_SOURCE_VERSION,
    NODE_REMOTE_RUN_VERSION,
    NODE_WORKSPACE_USAGE_VERSION,
    generate_private_key,
)
from termroom.node_remote_runs import NodeRemoteRunError
from termroom.remote_access import (
    RemoteAccess,
    RemoteAccessError,
    _cancel_bridge_tasks,
    _settle_bridge_tasks,
)
from termroom.run_sources import SourceFileChangedError, SourceValidationError
from termroom.security import PathBoundaryError
from termroom.terminal_control import TerminalControl


def _payload(workspace: Path, **values: Any) -> dict[str, Any]:
    return {
        "workspace_path": str(workspace),
        "tmux_session": f"termroom-node-test-{uuid.uuid4().hex[:12]}",
        **values,
    }


def _terminal_resize_ack(payload: dict[str, Any]) -> dict[str, Any]:
    active = payload["affects_grid"] and not payload["bootstrap"]
    return {
        **payload,
        "ok": True,
        "shared_applied": payload["affects_grid"],
        "viewport_applied": True,
        "passive_restored": not active,
        "bootstrap_consumed": payload["bootstrap"],
        "grid_active": active,
        "retryable": False,
        "cleanup_confirmed": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("pane_mode_control", [False, True])
async def test_node_terminal_mode_control_is_opt_in_and_precedes_first_pty_output(
    monkeypatch: pytest.MonkeyPatch, pane_mode_control: bool
) -> None:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"first output")
    os.close(write_fd)
    modes = iter(
        [
            {"session": "s", "window": "@1", "pane": "%1", "pane_pid": 1,
             "alternate": False, "mouse_tracking": False, "mouse_flags": [0] * 5},
            {"session": "s", "window": "@1", "pane": "%1", "pane_pid": 1,
             "alternate": True, "mouse_tracking": True, "mouse_flags": [1] * 5},
        ]
    )
    sent: list[dict[str, Any]] = []

    async def send(message: Any) -> None:
        sent.append(dict(message))

    monkeypatch.setattr(node_agent.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(node_agent, "_wait_for_pid", lambda *_args: True)
    stream = TerminalAgentStream(
        "d" * 32,
        -1,
        read_fd,
        send,
        {},
        current_pane_mode=lambda: next(modes),
        pane_mode_control=pane_mode_control,
    )

    await stream.start()

    if pane_mode_control:
        assert [message["type"] for message in sent] == [
            "stream.control",
            "stream.control",
            "stream.data",
            "stream.close",
        ]
        assert sent[0]["mode"]["alternate"] is False
        assert sent[1]["mode"]["alternate"] is True
        assert sent[1]["revision"] == 2
        assert base64.b64decode(sent[2]["data"]) == b"first output"
    else:
        assert [message["type"] for message in sent] == ["stream.data", "stream.close"]
        assert base64.b64decode(sent[0]["data"]) == b"first output"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "enable", "apply", "demote", "cleanup"])
async def test_node_terminal_resize_bootstrap_ack_failure_retry_and_idempotency(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    events: list[Any] = []
    failed_once = False

    def role(enabled: bool) -> bool:
        nonlocal failed_once
        events.append(("role", enabled))
        if failure == "enable" and enabled and not failed_once:
            failed_once = True
            return False
        return enabled or failure not in {"demote", "cleanup"}

    def ioctl(*_args: Any) -> None:
        nonlocal failed_once
        events.append("apply")
        if failure == "apply" and not failed_once:
            failed_once = True
            raise OSError("resize failed before apply")

    async def send(_message: Any) -> None:
        return None

    monkeypatch.setattr(node_agent.fcntl, "ioctl", ioctl)
    monkeypatch.setattr(node_agent.os, "killpg", lambda *args: events.append(("signal", args)))
    monkeypatch.setattr(node_agent, "_wait_for_pid", lambda *_args: True)
    stream = TerminalAgentStream(
        "d" * 32,
        -1,
        -1,
        send,
        {},
        set_grid_resize=role,
        grid_resize_applied=lambda: events.append("consumed"),
        cleanup=lambda: failure != "cleanup",
        wait_grid_resize=lambda *_args: True,
    )
    payload = {
        "stream_id": stream.stream_id,
        "revision": 1,
        "rows": 22,
        "cols": 124,
        "affects_grid": True,
        "bootstrap": True,
    }
    result = await stream.resize(payload)
    before_duplicate = list(events)
    assert await stream.resize(payload) == result
    assert events == before_duplicate
    with pytest.raises(NodeAgentError, match="stale"):
        await stream.resize({**payload, "cols": 125})
    if failure in {"enable", "apply"}:
        assert not result["ok"] and result["retryable"]
        assert not result["shared_applied"] and not result["bootstrap_consumed"]
        assert stream.grid_resize_applied is not None
        result = await stream.resize({**payload, "revision": 2})
        assert result["ok"]
    elif failure in {"demote", "cleanup"}:
        assert not result["ok"] and not result["retryable"]
        assert result["shared_applied"] and result["bootstrap_consumed"]
        assert not result["passive_restored"] and stream.closed
        assert result["cleanup_confirmed"] == (failure == "demote")
        assert result["grid_active"] == (failure == "cleanup")
        return
    assert result["ok"] and result["shared_applied"]
    assert result["passive_restored"] and not result["grid_active"]
    assert events.index("consumed") < len(events) - 1
    assert events[-1] == ("role", False)
    with pytest.raises(NodeAgentError) as unacknowledged:
        await stream.control("resize", payload)
    assert unacknowledged.value.code == "terminal_resize_unacknowledged"
    passive = await stream.resize(
        {**payload, "revision": 3, "rows": 17, "affects_grid": False, "bootstrap": False}
    )
    assert passive["ok"] and not passive["shared_applied"]
    active = await stream.resize({**payload, "revision": 4, "rows": 17, "bootstrap": False})
    assert active["ok"] and active["grid_active"]
    with pytest.raises(NodeAgentError) as stale:
        await stream.resize({**payload, "revision": 3})
    assert stale.value.code == "terminal_resize_stale"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("rows", 3),
        ("rows", True),
        ("cols", 1001),
        ("revision", 2**31),
        ("revision", True),
        ("affects_grid", "true"),
        ("stream_id", "wrong"),
    ],
)
async def test_node_terminal_resize_rejects_invalid_identity_without_backend_effect(
    monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    async def send(_message: Any) -> None:
        return None

    def unexpected(*_args: Any) -> bool:
        raise AssertionError("Invalid request reached backend")

    stream = TerminalAgentStream("e" * 32, -1, -1, send, {}, set_grid_resize=unexpected)
    payload = {
        "stream_id": stream.stream_id,
        "revision": 1,
        "rows": 22,
        "cols": 124,
        "affects_grid": True,
        "bootstrap": False,
        field: value,
    }
    with pytest.raises(NodeAgentError) as invalid:
        await stream.resize(payload)
    assert invalid.value.code == "terminal_resize_invalid"


def _node_runtime_with_private_state(home: Path) -> tuple[NodeRuntime, Path]:
    state_root = home / ".local" / "state" / "termroom" / "node"
    ensure_node_identity(state_root)
    config = NodeConfig(
        "http://127.0.0.1:1",
        "a" * 32,
        "private-state-test",
        (home,),
        state_dir=state_root,
        run_root=state_root / "runs",
    )
    save_node_config(state_root, config)
    return (
        NodeRuntime(
            config.allowed_roots,
            file_run_root=state_root / "file-runs",
            remote_run_root=config.run_root,
            private_state_root=state_root,
        ),
        state_root,
    )


def _wait_for_node_file_run(
    runtime: NodeRuntime,
    payload: dict[str, Any],
    *,
    states: set[str],
    timeout: float = 8.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    result: dict[str, Any] = {}
    while time.monotonic() < deadline:
        result = runtime._handle_sync("file_run.observe", payload)
        if result["observation"]["state"] in states:
            return result
        time.sleep(0.05)
    raise AssertionError(f"Node File Run did not reach {states}: {result}")


@pytest.mark.parametrize(
    "session",
    [
        "termroom-node-test-a1b2",
        "termroom-run-77458706-dc26-476c-bbc6-a06e7991c42c",
        "tr-project-a1b2",
        "tr-한글-프로젝트-32cd",
    ],
)
def test_node_accepts_supported_workspace_session_names(session: str) -> None:
    assert node_session_is_valid(session)


@pytest.mark.parametrize(
    "session",
    [
        "tr-project",
        "tr--a1b2",
        "tr-project-a1b",
        "tr-project-A1B2",
        "tr-project.with-dot-a1b2",
        "tr-project--name-a1b2",
        "tr-project-name-that-is-too-long-a1b2",
    ],
)
def test_node_rejects_invalid_workspace_session_names(session: str) -> None:
    assert not node_session_is_valid(session)


def test_node_rejects_replaced_allowed_root_directory(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    workspace = allowed / "project"
    workspace.mkdir(parents=True)
    runtime = NodeRuntime([allowed])
    moved = tmp_path / "original-allowed"
    allowed.rename(moved)
    workspace.mkdir(parents=True)

    with pytest.raises(PathBoundaryError):
        runtime._workspace_path({"workspace_path": str(workspace)})


@pytest.mark.parametrize("replacement_kind", ["symlink", "real"])
def test_node_workspace_replacement_is_rejected_before_tmux_launch(
    tmp_path: Path,
    replacement_kind: str,
) -> None:
    allowed = tmp_path / "allowed"
    workspace = allowed / "project"
    outside = tmp_path / "outside"
    workspace.mkdir(parents=True)
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    runtime = NodeRuntime([allowed])
    payload = _payload(workspace)
    runtime._workspace_path(payload)
    moved = allowed / "project-original"
    calls: list[tuple[str, ...]] = []

    def replace_before_new_session(
        *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        if arguments[0] == "has-session":
            workspace.rename(moved)
            if replacement_kind == "symlink":
                workspace.symlink_to(outside, target_is_directory=True)
            else:
                workspace.mkdir(mode=0o755)
                replacement_sentinel = workspace / "sentinel"
                replacement_sentinel.write_bytes(b"replacement")
                replacement_sentinel.chmod(0o640)
            return subprocess.CompletedProcess(arguments, 1, "", "")
        if arguments[0] == "list-sessions":
            return subprocess.CompletedProcess(arguments, 0, "", "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    runtime._tmux = replace_before_new_session  # type: ignore[method-assign]
    with pytest.raises(PathBoundaryError):
        runtime._ensure_workspace(payload)

    assert not any(arguments[0] == "new-session" for arguments in calls)
    assert sentinel.read_bytes() == b"keep"
    if replacement_kind == "real":
        replacement_sentinel = workspace / "sentinel"
        assert replacement_sentinel.read_bytes() == b"replacement"
        assert stat.S_IMODE(replacement_sentinel.stat().st_mode) == 0o640


@pytest.mark.asyncio
async def test_node_allowed_roots_and_file_operations_are_bounded(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    workspace = allowed / "project"
    outside = tmp_path / "outside"
    workspace.mkdir(parents=True)
    outside.mkdir()
    (workspace / "hello.txt").write_text("before\n", encoding="utf-8")
    nested = workspace / "src" / "deep"
    nested.mkdir(parents=True)
    (nested / "node-needle.txt").write_text("match\n", encoding="utf-8")
    dependency = workspace / "node_modules"
    dependency.mkdir()
    (dependency / "hidden-needle.txt").write_text("hidden\n", encoding="utf-8")
    (allowed / "escape").symlink_to(outside, target_is_directory=True)

    runtime = NodeRuntime([allowed])
    roots = runtime._handle_sync("workspace.roots", {})
    assert roots == {"roots": [{"path": str(allowed), "name": "allowed"}]}
    assert runtime._handle_sync("workspace.validate", {"path": str(workspace)})["path"] == str(
        workspace
    )
    with pytest.raises(PathBoundaryError):
        runtime._handle_sync("workspace.validate", {"path": str(outside)})
    with pytest.raises(PathBoundaryError):
        runtime._handle_sync("workspace.validate", {"path": str(allowed / "escape")})

    listed = runtime._handle_sync("files.list", {"workspace_path": str(workspace), "path": "."})
    assert [entry["name"] for entry in listed["entries"]] == [
        "node_modules",
        "src",
        "hello.txt",
    ]
    search = runtime._handle_sync(
        "files.search",
        {
            "workspace_path": str(workspace),
            "path": ".",
            "query": "needle",
            "include_noise": False,
        },
    )
    assert [entry["relative_path"] for entry in search["entries"]] == ["src/deep/node-needle.txt"]
    assert search["skipped_noise"] == 1
    assert search["truncated"] is False
    recent = runtime._handle_sync("files.recent", {"workspace_path": str(workspace), "limit": 5})
    assert {entry["relative_path"] for entry in recent["entries"]} == {
        "hello.txt",
        "src/deep/node-needle.txt",
    }
    assert recent["scanned_files"] == 2
    assert recent["truncated"] is False
    (workspace / "second.txt").write_text("second\n", encoding="utf-8")
    with pytest.raises(DirectoryListingLimitError):
        runtime._handle_sync(
            "files.list",
            {
                "workspace_path": str(workspace),
                "path": ".",
                "max_entries": 1,
            },
        )
    sent: list[dict[str, Any]] = []

    async def send(message: Any) -> None:
        sent.append(dict(message))

    read_id = uuid.uuid4().hex
    read = await runtime.handle(
        "files.read_text.open",
        {
            "workspace_path": str(workspace),
            "path": "hello.txt",
            "max_bytes": 1024 * 1024,
            "stream_id": read_id,
        },
        send,
    )
    assert read.start is not None
    await read.start()
    assert (
        b"".join(
            base64.b64decode(message["data"])
            for message in sent
            if message["type"] == "stream.data"
        )
        == b"before\n"
    )
    snapshot = read.value["snapshot"]

    write_id = uuid.uuid4().hex
    await runtime.handle(
        "files.write_text.open",
        {
            "workspace_path": str(workspace),
            "path": "hello.txt",
            "expected_digest": snapshot["digest"],
            "expected_mtime_ns": snapshot["mtime_ns"],
            "max_bytes": 1024 * 1024,
            "stream_id": write_id,
        },
        send,
    )
    write = runtime.streams[write_id]
    await write.feed("한글 after\n".encode())
    saved = await write.close()
    assert saved is not None
    assert saved["snapshot"]["relative_path"] == "hello.txt"
    assert (workspace / "hello.txt").read_text(encoding="utf-8") == "한글 after\n"
    runtime._handle_sync(
        "files.create",
        {
            "workspace_path": str(workspace),
            "parent": ".",
            "name": "new-dir",
            "directory": True,
        },
    )
    assert (workspace / "new-dir").is_dir()


def test_node_private_identity_cannot_be_previewed_from_allowed_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    runtime, _state_root = _node_runtime_with_private_state(home)

    with pytest.raises(PathBoundaryError):
        runtime._handle_sync(
            "files.read_preview",
            {
                "workspace_path": str(home),
                "path": ".local/state/termroom/node/node-key.pem",
            },
        )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_node_workspace_admission_rejects_private_roots_but_allows_home_and_managed_runs(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    private_state = home / "node-state"
    private_state.mkdir()
    file_run_root = home / "file-runs"
    remote_run_root = home / "remote-runs"
    runtime = NodeRuntime(
        [home],
        private_state_root=private_state,
        file_run_root=file_run_root,
        remote_run_root=remote_run_root,
    )
    boundaries = runtime.source_private_boundaries
    assert runtime._handle_sync("workspace.validate", {"path": str(home)})["path"] == str(home)

    normal_payload = _payload(home)
    session_names = [str(normal_payload["tmux_session"])]
    remote_runs = runtime.remote_runs
    assert remote_runs is not None
    managed_ids = (str(uuid.uuid4()), str(uuid.uuid4()))
    managed_layouts = [
        remote_runs.create(
            {
                "remote_run_version": NODE_REMOTE_RUN_VERSION,
                "run_base": str(remote_runs.run_root),
                "run_id": run_id,
                "command": "true",
            }
        )
        for run_id in managed_ids
    ]
    managed_path = Path(managed_layouts[0]["work"])
    other_managed_path = Path(managed_layouts[1]["work"])
    managed_payload = {
        **_payload(managed_path),
        "remote_run_id": managed_ids[0],
    }
    session_names.append(str(managed_payload["tmux_session"]))

    try:
        for index, boundary in enumerate(boundaries):
            descendant = boundary / "nested"
            descendant.mkdir()
            alias = home / f"private-alias-{index}"
            alias.symlink_to(boundary, target_is_directory=True)

            for path in (boundary, descendant, alias):
                with pytest.raises(PathBoundaryError):
                    runtime._handle_sync("workspace.validate", {"path": str(path)})
                blocked = _payload(path)
                session_names.append(str(blocked["tmux_session"]))
                with pytest.raises(PathBoundaryError):
                    runtime._handle_sync("workspace.ensure", blocked)

            with pytest.raises(PathBoundaryError):
                runtime._handle_sync(
                    "workspace.create_project",
                    {"parent": str(boundary), "name": "must-not-create"},
                )
            with pytest.raises(PathBoundaryError):
                runtime._handle_sync(
                    "workspace.create_project",
                    {"parent": str(home), "name": boundary.name},
                )
            assert not (boundary / "must-not-create").exists()

        assert runtime._handle_sync("workspace.ensure", normal_payload)["terminals"]
        created = runtime._handle_sync(
            "workspace.create_project",
            {"parent": str(home), "name": "ordinary-project"},
        )
        assert Path(str(created["path"])).is_dir()

        assert runtime._handle_sync("workspace.validate", managed_payload)["path"] == str(
            managed_path
        )
        assert runtime._handle_sync("workspace.ensure", managed_payload)["terminals"]
        for mismatched in (
            {**managed_payload, "workspace_path": str(other_managed_path)},
            {**managed_payload, "remote_run_id": managed_ids[1]},
        ):
            with pytest.raises(NodeRemoteRunError):
                runtime._handle_sync("workspace.validate", mismatched)
            with pytest.raises(NodeRemoteRunError):
                runtime._handle_sync("workspace.ensure", mismatched)
    finally:
        for session in session_names:
            subprocess.run(
                ["tmux", "kill-session", "-t", session],
                check=False,
                capture_output=True,
            )


@pytest.mark.asyncio
async def test_node_private_state_is_excluded_from_files_and_file_run(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    visible = home / "visible.txt"
    visible.write_text("ordinary sibling\n", encoding="utf-8")
    runtime, state_root = _node_runtime_with_private_state(home)
    state_sibling = state_root.parent / "ordinary-state.txt"
    state_sibling.write_text("ordinary state sibling\n", encoding="utf-8")
    private_files = (state_root / "node-key.pem", state_root / "node.json")

    def private_fingerprint(path: Path) -> tuple[str, int, int]:
        info = path.stat()
        return (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            info.st_mode & 0o777,
            info.st_mtime_ns,
        )

    before = tuple(private_fingerprint(path) for path in private_files)
    alias = home / "private-alias"
    alias.symlink_to(state_root, target_is_directory=True)
    payload = _payload(home)
    key_path = ".local/state/termroom/node/node-key.pem"
    state_parent = ".local/state/termroom"

    listed = runtime._handle_sync(
        "files.list",
        {**payload, "path": ".", "max_entries": 2, "max_metadata_bytes": 300},
    )
    siblings = runtime._handle_sync(
        "files.list", {**payload, "path": state_parent, "max_entries": 1}
    )
    assert {entry["name"] for entry in listed["entries"]} == {".local", "visible.txt"}
    assert [entry["name"] for entry in siblings["entries"]] == ["ordinary-state.txt"]
    assert all(entry["name"] != alias.name for entry in listed["entries"])
    recent = runtime._handle_sync("files.recent", {**payload, "limit": 20})
    assert "visible.txt" in {entry["name"] for entry in recent["entries"]}
    search = runtime._handle_sync(
        "files.search",
        {**payload, "path": ".", "query": "node-key", "include_noise": True},
    )
    assert search["entries"] == []

    for operation, extra in (
        ("files.list", {"path": ".local/state/termroom/node"}),
        ("files.search", {"path": ".local/state/termroom/node", "query": "node-key"}),
        ("files.stat", {"path": key_path}),
        ("files.read_preview", {"path": key_path}),
        ("files.stat", {"path": "private-alias/node-key.pem"}),
        ("files.create", {"parent": ".local/state/termroom/node", "name": "new.txt"}),
        ("files.create", {"parent": ".", "name": ".local"}),
        ("files.rename", {"path": key_path, "new_name": "renamed.pem"}),
        ("files.rename", {"path": state_parent, "new_name": "renamed-state"}),
        ("files.delete", {"path": key_path}),
        ("files.delete", {"path": state_parent}),
        ("terminal.editor.open", {"path": key_path}),
        (
            "file_run.inspect",
            {"path": key_path, "runner_registry_version": RUNNER_REGISTRY_VERSION},
        ),
        (
            "file_run.start",
            {
                "path": key_path,
                "runner_registry_version": RUNNER_REGISTRY_VERSION,
            },
        ),
    ):
        with pytest.raises(PathBoundaryError):
            runtime._handle_sync(operation, {**payload, **extra})

    assert visible.read_text(encoding="utf-8") == "ordinary sibling\n"
    assert state_sibling.read_text(encoding="utf-8") == "ordinary state sibling\n"
    assert tuple(private_fingerprint(path) for path in private_files) == before

    async def send(_message: Any) -> None:
        return None

    remote_runs = runtime.remote_runs
    assert remote_runs is not None
    run_id = str(uuid.uuid4())
    run_request = {
        "remote_run_version": NODE_REMOTE_RUN_VERSION,
        "run_base": str(remote_runs.run_root),
        "run_id": run_id,
        "command": "true",
    }
    layout = remote_runs.create(run_request)
    run_work = Path(layout["work"])
    (run_work / "ordinary.txt").write_text("managed output\n", encoding="utf-8")
    run_alias = run_work / "private-alias"
    run_alias.symlink_to(state_root, target_is_directory=True)
    managed_payload = {
        **payload,
        "workspace_path": str(run_work),
        "remote_run_id": run_id,
    }
    managed_entries = runtime._handle_sync("files.list", {**managed_payload, "path": "."})[
        "entries"
    ]
    assert [entry["name"] for entry in managed_entries] == ["ordinary.txt"]
    with pytest.raises(PathBoundaryError):
        runtime._handle_sync(
            "files.read_preview",
            {**managed_payload, "path": "private-alias/node-key.pem"},
        )

    for operation, extra in (
        ("files.read_text.open", {"path": key_path}),
        ("files.write_text.open", {"path": key_path}),
        ("files.download.open", {"path": key_path}),
        (
            "files.upload.open",
            {"parent": ".local/state/termroom/node", "filename": "upload.txt"},
        ),
    ):
        stream_id = uuid.uuid4().hex
        with pytest.raises(PathBoundaryError):
            await runtime.handle(
                operation,
                {**payload, **extra, "stream_id": stream_id},
                send,
            )
        assert stream_id not in runtime.streams


@pytest.mark.asyncio
async def test_node_workspace_source_streams_manifest_and_stable_files_with_local_policy(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    workspace = allowed / "project"
    nested = workspace / "src"
    nested.mkdir(parents=True)
    state_root = workspace / ".node-private"
    state_root.mkdir()
    run_root = workspace / "managed-runs"
    content = b"node-source\n" * 8_000
    source_file = nested / "run.sh"
    source_file.write_bytes(content)
    source_file.chmod(0o700)
    (workspace / ".env").write_text("SECRET=hidden\n", encoding="utf-8")
    (workspace / "source-link").symlink_to("src/run.sh")
    (state_root / "identity.json").write_text("private", encoding="utf-8")

    runtime = NodeRuntime(
        [allowed],
        file_run_root=state_root / "file-runs",
        remote_run_root=run_root,
        private_state_root=state_root,
    )
    sent: list[dict[str, Any]] = []

    async def send(message: Any) -> None:
        sent.append(dict(message))

    base = {
        "remote_run_source_version": NODE_REMOTE_RUN_SOURCE_VERSION,
        "workspace_id": "persistent-workspace",
        "workspace_path": str(workspace),
        "source_path": ".",
        "explicitly_included": [],
    }
    manifest_id = uuid.uuid4().hex
    opened = await runtime.handle(
        "remote_run_source.manifest.open",
        {**base, "stream_id": manifest_id},
        send,
    )
    assert opened.start is not None
    assert opened.value["stream_window"] == NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW
    assert opened.value["total_bytes"] == len(content)
    assert 0 < opened.value["frame_count"] <= NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW
    manifest_stream = runtime.streams[manifest_id]
    with pytest.raises(NodeAgentError) as invalid_credit:
        await manifest_stream.control("credit", {"count": NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW + 1})
    assert invalid_credit.value.code == "stream_control_invalid"
    await manifest_stream.control("credit", {"count": opened.value["frame_count"]})
    with pytest.raises(NodeAgentError) as duplicate_credit:
        await manifest_stream.control("credit", {"count": 1})
    assert duplicate_credit.value.code == "stream_control_invalid"
    await opened.start()
    encoded_manifest = b"".join(
        base64.b64decode(message["data"]) for message in sent if message["type"] == "stream.data"
    )
    entries = [json.loads(line) for line in encoded_manifest.splitlines()]
    by_path = {entry["path"]: entry for entry in entries}
    assert set(by_path) == {"src", "src/run.sh", "source-link"}
    assert by_path["src/run.sh"]["executable"] is True
    assert by_path["source-link"]["link_target"] == "src/run.sh"
    assert not any(path.startswith(".node-private") for path in by_path)
    assert not any(path.startswith("managed-runs") for path in by_path)

    file_id = uuid.uuid4().hex
    file_opened = await runtime.handle(
        "remote_run_source.file.open",
        {
            **base,
            "stream_id": file_id,
            "path": "src/run.sh",
            "expected_size": by_path["src/run.sh"]["size"],
            "expected_mtime_ns": by_path["src/run.sh"]["mtime_ns"],
            "executable": True,
        },
        send,
    )
    assert file_opened.start is not None
    assert file_opened.value["stream_window"] == NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW
    assert 0 < file_opened.value["frame_count"] <= NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW
    file_stream = runtime.streams[file_id]
    await file_stream.control("credit", {"count": file_opened.value["frame_count"]})
    await file_opened.start()
    file_frames = [
        base64.b64decode(message["data"])
        for message in sent
        if message.get("stream_id") == file_id and message["type"] == "stream.data"
    ]
    assert b"".join(file_frames) == content
    assert all(len(frame) <= 64 * 1024 for frame in file_frames)

    source_file.write_bytes(content + b"changed\n")
    with pytest.raises(SourceFileChangedError) as changed:
        await runtime.handle(
            "remote_run_source.file.open",
            {
                **base,
                "stream_id": uuid.uuid4().hex,
                "path": "src/run.sh",
                "expected_size": by_path["src/run.sh"]["size"],
                "expected_mtime_ns": by_path["src/run.sh"]["mtime_ns"],
                "executable": True,
            },
            send,
        )
    assert changed.value.current_size == len(content) + len(b"changed\n")
    current = runtime._handle_sync("remote_run_source.stat", {**base, "path": "src/run.sh"})[
        "entry"
    ]
    assert current["size"] == changed.value.current_size

    with pytest.raises(NodeAgentError) as transient:
        await runtime.handle(
            "remote_run_source.manifest.open",
            {**base, "stream_id": uuid.uuid4().hex, "remote_run_id": str(uuid.uuid4())},
            send,
        )
    assert transient.value.code == "source_workspace_transient"
    with pytest.raises(SourceValidationError) as private:
        await runtime.handle(
            "remote_run_source.manifest.open",
            {
                **base,
                "stream_id": uuid.uuid4().hex,
                "source_path": ".node-private",
                "explicitly_included": ["identity.json"],
            },
            send,
        )
    assert private.value.code == "source_private_boundary"


@pytest.mark.asyncio
async def test_node_workspace_source_manifest_stream_exceeds_single_message_limit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    for index in range(4_500):
        (workspace / f"{index:04d}-{'x' * 180}.txt").write_bytes(b"x")
    runtime = NodeRuntime([tmp_path])
    sent: list[dict[str, Any]] = []
    frame_count = 0
    sent_frames = 0

    async def send(message: Any) -> None:
        nonlocal sent_frames
        sent.append(dict(message))
        if message.get("type") == "stream.data":
            sent_frames += 1
            if (
                sent_frames % NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW == 0
                and sent_frames < frame_count
            ):
                stream = runtime.streams[str(message["stream_id"])]
                await stream.control(
                    "credit",
                    {
                        "count": min(
                            NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW,
                            frame_count - sent_frames,
                        )
                    },
                )

    stream_id = uuid.uuid4().hex
    opened = await runtime.handle(
        "remote_run_source.manifest.open",
        {
            "remote_run_source_version": NODE_REMOTE_RUN_SOURCE_VERSION,
            "workspace_id": "large-manifest-workspace",
            "workspace_path": str(workspace),
            "source_path": ".",
            "explicitly_included": [],
            "stream_id": stream_id,
        },
        send,
    )
    assert opened.start is not None
    frame_count = opened.value["frame_count"]
    stream = runtime.streams[stream_id]
    await stream.control(
        "credit", {"count": min(NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW, frame_count)}
    )
    await opened.start()
    frames = [
        base64.b64decode(message["data"]) for message in sent if message["type"] == "stream.data"
    ]
    assert opened.value["entry_count"] == 4_500
    assert opened.value["total_bytes"] == 4_500
    assert len(frames) == frame_count
    assert sum(map(len, frames)) > 1024 * 1024
    assert all(len(frame) <= 64 * 1024 for frame in frames)
    assert sent[-1] == {"type": "stream.close", "stream_id": stream_id}


@pytest.mark.asyncio
async def test_node_workspace_source_file_stream_backpressures_slow_core_consumer(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    content_size = 8 * 1024 * 1024 + 17
    content = (b"0123456789abcdef" * ((content_size + 15) // 16))[:content_size]
    source_file = workspace / "payload.bin"
    source_file.write_bytes(content)
    source_stat = source_file.stat()
    runtime = NodeRuntime([tmp_path])
    messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=NODE_STREAM_QUEUE_DEPTH)
    peak_data_depth = 0
    overflowed = False

    async def send(message: Any) -> None:
        nonlocal overflowed, peak_data_depth
        try:
            messages.put_nowait(dict(message))
        except asyncio.QueueFull as exc:
            overflowed = True
            raise NodeCoreError("Node stream exceeded its buffer", code="stream_overflow") from exc
        if message.get("type") == "stream.data":
            peak_data_depth = max(peak_data_depth, messages.qsize())

    stream_id = uuid.uuid4().hex
    opened = await runtime.handle(
        "remote_run_source.file.open",
        {
            "remote_run_source_version": NODE_REMOTE_RUN_SOURCE_VERSION,
            "workspace_id": "large-file-workspace",
            "workspace_path": str(workspace),
            "source_path": ".",
            "explicitly_included": [],
            "stream_id": stream_id,
            "path": "payload.bin",
            "expected_size": source_stat.st_size,
            "expected_mtime_ns": source_stat.st_mtime_ns,
            "executable": False,
        },
        send,
    )
    assert opened.start is not None
    assert opened.value["stream_window"] == NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW
    frame_count = opened.value["frame_count"]
    stream = runtime.streams[stream_id]
    remaining_to_grant = frame_count
    batch_remaining = min(NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW, remaining_to_grant)
    credit_batches = [batch_remaining]
    await stream.control("credit", {"count": batch_remaining})
    remaining_to_grant -= batch_remaining
    producer = asyncio.create_task(opened.start())
    received = bytearray()
    received_frames = 0

    while True:
        message = await asyncio.wait_for(messages.get(), timeout=5.0)
        kind = message.get("type")
        if kind == "stream.data":
            await asyncio.sleep(0.002)
            received.extend(base64.b64decode(message["data"]))
            received_frames += 1
            batch_remaining -= 1
            if batch_remaining == 0 and remaining_to_grant:
                batch_remaining = min(NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW, remaining_to_grant)
                credit_batches.append(batch_remaining)
                await stream.control("credit", {"count": batch_remaining})
                remaining_to_grant -= batch_remaining
            continue
        if kind == "stream.close":
            break
        raise AssertionError(message)

    await asyncio.wait_for(producer, timeout=5.0)
    assert not overflowed
    assert bytes(received) == content
    assert received_frames == frame_count
    assert sum(credit_batches) == frame_count
    assert peak_data_depth <= NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW
    assert stream_id not in runtime.streams


@pytest.mark.asyncio
async def test_node_text_streams_support_the_exact_editor_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    limit = 1024 * 1024
    source = b"x" * limit
    target = workspace / "large.txt"
    target.write_bytes(source)
    runtime = NodeRuntime([tmp_path], max_edit_bytes=limit)
    sent: list[dict[str, Any]] = []

    async def send(message: Any) -> None:
        sent.append(dict(message))

    read_id = uuid.uuid4().hex
    read = await runtime.handle(
        "files.read_text.open",
        {
            "workspace_path": str(workspace),
            "path": "large.txt",
            "max_bytes": limit,
            "stream_id": read_id,
        },
        send,
    )
    assert read.start is not None
    await read.start()
    assert "content" not in read.value["snapshot"]
    assert read.value["size"] == limit
    assert (
        b"".join(
            base64.b64decode(message["data"])
            for message in sent
            if message["type"] == "stream.data"
        )
        == source
    )

    write_id = uuid.uuid4().hex
    await runtime.handle(
        "files.write_text.open",
        {
            "workspace_path": str(workspace),
            "path": "large.txt",
            "expected_digest": read.value["snapshot"]["digest"],
            "expected_mtime_ns": read.value["snapshot"]["mtime_ns"],
            "max_bytes": limit,
            "stream_id": write_id,
        },
        send,
    )
    replacement = b"y" * limit
    write = runtime.streams[write_id]
    for offset in range(0, len(replacement), 64 * 1024):
        await write.feed(replacement[offset : offset + 64 * 1024])
    result = await write.close()
    assert result is not None
    assert "content" not in result["snapshot"]
    assert target.read_bytes() == replacement


def test_node_identity_rejects_symlink_allowed_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(NodeAgentError) as exc_info:
        normalize_allowed_roots([linked])
    assert exc_info.value.code == "root_invalid"


def test_node_file_run_root_rejects_symlink_replaced_during_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "file-runs"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    outside.chmod(0o755)
    moved_root = tmp_path / "original-file-runs"
    original_open = node_agent._open_directory_components
    replaced = False

    def replace_after_open(path: Path, *, create: bool, mode: int | None) -> tuple[int, Path]:
        nonlocal replaced
        descriptor, candidate = original_open(path, create=create, mode=mode)
        if path == root and create and not replaced:
            replaced = True
            path.rename(moved_root)
            path.symlink_to(outside, target_is_directory=True)
        return descriptor, candidate

    monkeypatch.setattr(node_agent, "_open_directory_components", replace_after_open)
    with pytest.raises(NodeAgentError) as invalid:
        NodeRuntime([tmp_path], file_run_root=root)

    assert invalid.value.code == "file_run_state_invalid"
    assert replaced
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]


def test_node_file_run_root_rejects_symlink_present_at_entry(tmp_path: Path) -> None:
    root = tmp_path / "file-runs"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    outside.chmod(0o755)
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(NodeAgentError) as invalid:
        NodeRuntime([tmp_path], file_run_root=root)

    assert invalid.value.code == "file_run_state_invalid"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]


def test_node_file_run_root_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    outside.chmod(0o755)
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)

    with pytest.raises(NodeAgentError) as invalid:
        NodeRuntime([tmp_path], file_run_root=alias / "file-runs")

    assert invalid.value.code == "file_run_state_invalid"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]


@pytest.mark.parametrize("replacement_kind", ["symlink", "real"])
def test_node_file_run_root_rejects_replacement_after_directory_validation(
    tmp_path: Path,
    replacement_kind: str,
) -> None:
    root = tmp_path / "file-runs"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    outside.chmod(0o755)
    runtime = NodeRuntime([tmp_path], file_run_root=root)
    moved_root = tmp_path / "original-file-runs"
    root.rename(moved_root)
    if replacement_kind == "symlink":
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir(mode=0o755)
        (root / "replacement-sentinel").write_bytes(b"keep")
    with pytest.raises(NodeAgentError) as invalid:
        runtime._file_run_metadata_dir("workspace", str(uuid.uuid4()), create=True)

    assert invalid.value.code == "file_run_state_invalid"
    assert runtime.file_run_root == root
    assert root.is_symlink() is (replacement_kind == "symlink")
    if replacement_kind == "real":
        assert stat.S_IMODE(root.stat().st_mode) == 0o755
        assert (root / "replacement-sentinel").read_bytes() == b"keep"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]


def test_node_file_run_metadata_rejects_replaced_descendant_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "file-runs"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    outside.chmod(0o755)
    runtime = NodeRuntime([tmp_path], file_run_root=root)
    run_id = str(uuid.uuid4())
    metadata = runtime._file_run_metadata_dir("workspace", run_id, create=True)
    moved = tmp_path / "original-metadata"
    metadata.rename(moved)
    metadata.symlink_to(outside, target_is_directory=True)

    with pytest.raises(NodeAgentError) as invalid:
        runtime._file_run_metadata_dir("workspace", run_id, create=True)

    assert invalid.value.code == "file_run_state_invalid"
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]


def test_node_file_run_metadata_rejects_real_directory_replacement_without_chmod(
    tmp_path: Path,
) -> None:
    root = tmp_path / "file-runs"
    runtime = NodeRuntime([tmp_path], file_run_root=root)
    run_id = str(uuid.uuid4())
    metadata = runtime._file_run_metadata_dir("workspace", run_id, create=True)
    moved = tmp_path / "original-metadata"
    metadata.rename(moved)
    replacement = tmp_path / "replacement-metadata"
    replacement.mkdir(mode=0o755)
    sentinel = replacement / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    replacement.chmod(0o755)
    replacement.rename(metadata)

    with pytest.raises(NodeAgentError) as invalid:
        runtime._file_run_metadata_dir("workspace", run_id, create=True)

    assert invalid.value.code == "file_run_state_invalid"
    assert stat.S_IMODE(metadata.stat().st_mode) == 0o755
    replacement_sentinel = metadata / "sentinel"
    assert replacement_sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(replacement_sentinel.stat().st_mode) == 0o640
    assert sorted(path.name for path in metadata.iterdir()) == ["sentinel"]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.parametrize("mutation", ["replace", "in_place"])
def test_node_file_run_runner_change_before_respawn(
    tmp_path: Path,
    mutation: str,
) -> None:
    workspace = tmp_path / "allowed"
    workspace.mkdir()
    source = workspace / "script.py"
    source.write_text("print('approved')\n", encoding="utf-8")
    outside_sentinel = tmp_path / "outside-sentinel"
    runtime = NodeRuntime([workspace], file_run_root=tmp_path / "private" / "file-runs")
    base = _payload(
        workspace,
        workspace_id="workspace-runner-replacement",
        runner_registry_version=RUNNER_REGISTRY_VERSION,
    )
    inspected = runtime._handle_sync("file_run.inspect", {**base, "path": source.name})
    run_id = str(uuid.uuid4())
    start_payload = {
        **base,
        "run_id": run_id,
        "path": source.name,
        "expected_digest": inspected["runnable"]["digest"],
        "runner_id": "python3",
        "runner_version": RUNNER_REGISTRY_VERSION,
    }
    metadata = runtime.file_run_root / str(base["workspace_id"]) / run_id
    moved_runner = tmp_path / "approved-runner"
    original_tmux = runtime._tmux
    original_handoff = runtime._tmux_with_descriptors
    calls: list[tuple[str, ...]] = []
    replaced = False
    source_changed = False

    def replace_runner_before_respawn(
        *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        nonlocal replaced
        calls.append(arguments)
        if arguments[0] == "set-window-option" and not replaced:
            replaced = True
            runner_path = metadata / "runner.sh"
            if mutation == "replace":
                runner_path.rename(moved_runner)
                runner_path.write_text(f"#!/bin/sh\ntouch {outside_sentinel}\n", encoding="utf-8")
                runner_path.chmod(0o700)
            else:
                before = runner_path.stat()
                with runner_path.open("r+b") as runner_file:
                    runner_file.write(f"#!/bin/sh\ntouch {outside_sentinel}\n".encode())
                    runner_file.truncate()
                after = runner_path.stat()
                assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
        return original_tmux(*arguments, check=check)

    def mutate_source_before_handoff(
        tmux_args: tuple[str, ...],
        descriptors: tuple[int, ...],
        command: tuple[str, ...] = (),
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal source_changed
        if mutation == "in_place" and tmux_args[0] == "respawn-pane":
            source_fd = descriptors[3]
            before = source.stat()
            with source.open("r+b") as source_file:
                source_file.seek(0)
                source_file.write(
                    (
                        "from pathlib import Path\n"
                        f"Path({str(outside_sentinel)!r}).write_text('pwned')\n"
                    ).encode()
                )
                source_file.truncate()
            after = source.stat()
            assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
            required_seals = (
                getattr(fcntl, "F_SEAL_WRITE", 0x0008)
                | getattr(fcntl, "F_SEAL_GROW", 0x0004)
                | getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
                | getattr(fcntl, "F_SEAL_SEAL", 0x0001)
            )
            assert (
                fcntl.fcntl(source_fd, getattr(fcntl, "F_GET_SEALS", 1034)) & required_seals
                == required_seals
            )
            os.lseek(source_fd, 0, os.SEEK_SET)
            assert os.read(source_fd, 1024) == b"print('approved')\n"
            source_changed = True
        return original_handoff(tmux_args, descriptors, command, **kwargs)

    runtime._tmux = replace_runner_before_respawn  # type: ignore[method-assign]
    runtime._tmux_with_descriptors = mutate_source_before_handoff  # type: ignore[method-assign]
    try:
        if mutation == "replace":
            with pytest.raises(NodeAgentError) as invalid:
                runtime._handle_sync("file_run.start", start_payload)
            assert invalid.value.code == "file_run_state_invalid"
            assert not any(arguments[0] == "respawn-pane" for arguments in calls)
        else:
            started = runtime._handle_sync("file_run.start", start_payload)
            assert any(arguments[0] == "respawn-pane" for arguments in calls)
            assert source_changed
            completed = _wait_for_node_file_run(
                runtime, {**base, "run_id": run_id}, states={"finished"}
            )
            assert completed["observation"]["exit_code"] == 0
            output = runtime._handle_sync(
                "terminal.scrollback",
                {
                    **base,
                    "tmux_window": started["terminal"]["tmux_window"],
                    "lines": 200,
                },
            )["output"]
            assert "approved" in output
        assert replaced
        assert not outside_sentinel.exists()
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", str(base["tmux_session"])],
            check=False,
            capture_output=True,
        )


def test_node_file_run_root_rejects_wrong_owner_before_chmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "file-runs"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    actual_euid = os.geteuid()
    monkeypatch.setattr(node_agent.os, "geteuid", lambda: actual_euid + 1)

    with pytest.raises(NodeAgentError) as invalid:
        NodeRuntime._prepare_file_run_root(root)

    assert invalid.value.code == "file_run_state_invalid"
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    assert list(root.iterdir()) == []


def test_node_file_run_rejects_wrong_owner_descendant_before_chmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "file-runs"
    workspace = root / "workspace"
    root.mkdir(mode=0o700)
    workspace.mkdir(mode=0o755)
    sentinel = workspace / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    workspace.chmod(0o755)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    actual_euid = os.geteuid()
    monkeypatch.setattr(node_agent.os, "geteuid", lambda: actual_euid + 1)
    try:
        with pytest.raises(OSError):
            node_agent._open_directory_at(
                root_fd,
                ("workspace",),
                create=False,
                mode=0o700,
                require_owner=True,
            )
    finally:
        os.close(root_fd)

    assert stat.S_IMODE(workspace.stat().st_mode) == 0o755
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert sorted(path.name for path in workspace.iterdir()) == ["sentinel"]


def test_node_file_run_root_under_private_state_rejects_wrong_owner_before_chmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_state_root = tmp_path / "node-state"
    file_run_root = private_state_root / "file-runs"
    private_state_root.mkdir(mode=0o700)
    file_run_root.mkdir(mode=0o755)
    sentinel = file_run_root / "sentinel"
    sentinel.write_bytes(b"keep")
    sentinel.chmod(0o640)
    file_run_root.chmod(0o755)
    private_state_fd = os.open(private_state_root, os.O_RDONLY | os.O_DIRECTORY)
    actual_euid = os.geteuid()
    try:
        with monkeypatch.context() as ownership:
            ownership.setattr(node_agent.os, "geteuid", lambda: actual_euid + 1)
            with pytest.raises(NodeAgentError) as invalid:
                NodeRuntime._open_file_run_root(
                    file_run_root,
                    private_state_fd=private_state_fd,
                    private_state_root=private_state_root,
                )
        assert invalid.value.code == "file_run_state_invalid"
    finally:
        os.close(private_state_fd)

    assert stat.S_IMODE(file_run_root.stat().st_mode) == 0o755
    assert sentinel.read_bytes() == b"keep"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    assert sorted(path.name for path in file_run_root.iterdir()) == ["sentinel"]


def test_node_file_run_root_preserves_real_directory_behavior(tmp_path: Path) -> None:
    assert NodeRuntime([tmp_path], file_run_root=None).file_run_root is None

    missing_root = tmp_path / "new-file-runs"
    runtime = NodeRuntime([tmp_path], file_run_root=missing_root)
    assert runtime.file_run_root == missing_root
    assert missing_root.is_dir()
    assert not missing_root.is_symlink()
    assert stat.S_IMODE(missing_root.stat().st_mode) == 0o700

    existing_root = tmp_path / "existing-file-runs"
    existing_root.mkdir(mode=0o755)
    existing_root.chmod(0o755)
    accepted = NodeRuntime([tmp_path], file_run_root=existing_root)
    assert accepted.file_run_root == existing_root
    assert not existing_root.is_symlink()
    assert stat.S_IMODE(existing_root.stat().st_mode) == 0o700


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_node_workspace_reconnect_reuses_tmux_and_terminal_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    runtime = NodeRuntime([tmp_path])
    payload = _payload(workspace)
    session = str(payload["tmux_session"])
    list_terminals = runtime._list_terminals
    activity_revision = 1_700_000_000

    def later_activity(session: str) -> list[dict[str, Any]]:
        nonlocal activity_revision
        records = list_terminals(session)
        activity_revision += 1
        for record in records:
            record["activity_at"] = activity_revision
        return records

    # Keep real tmux identities; only make changing observation time deterministic.
    monkeypatch.setattr(runtime, "_list_terminals", later_activity)

    def identities(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {key: value for key, value in record.items() if key != "activity_at"}
            for record in records
        ]

    try:
        first = runtime._handle_sync("workspace.ensure", payload)["terminals"]
        second = runtime._handle_sync("workspace.ensure", payload)["terminals"]
        assert identities(first) == identities(second)
        assert len(first) == 1

        created = runtime._handle_sync("terminal.create", {**payload, "name": "한글 shell"})[
            "terminal"
        ]
        renamed = runtime._handle_sync(
            "terminal.rename",
            {
                **payload,
                "tmux_window": created["tmux_window"],
                "name": "work",
            },
        )["terminal"]
        assert renamed["name"] == "work"
        terminals = runtime._handle_sync(
            "terminal.close", {**payload, "tmux_window": created["tmux_window"]}
        )["terminals"]
        assert identities(terminals) == identities(first)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], check=False, capture_output=True)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_node_scrollback_can_exclude_the_live_tmux_viewport(tmp_path: Path) -> None:
    old_marker = "NODE_HISTORY_OLD"
    live_marker = "NODE_HISTORY_LIVE"
    workspace = tmp_path / "project"
    workspace.mkdir()
    runtime = NodeRuntime([tmp_path])
    payload = _payload(workspace)
    session = str(payload["tmux_session"])
    try:
        terminal = runtime._handle_sync("workspace.ensure", payload)["terminals"][0]
        window = str(terminal["tmux_window"])
        runtime._tmux("resize-window", "-t", window, "-x", "80", "-y", "6")
        runtime._tmux(
            "send-keys",
            "-t",
            window,
            (
                "printf '\\033[31mNODE_HISTORY_OLD\\033[0m\\n'; "
                "seq -f 'NODE_HISTORY_%02g' 1 24; "
                "printf 'NODE_HISTORY_%s\\n' LIVE"
            ),
            "Enter",
        )
        deadline = time.monotonic() + 4
        full = ""
        history = ""
        while time.monotonic() < deadline:
            full = runtime._handle_sync(
                "terminal.scrollback",
                {**payload, "tmux_window": window, "lines": 200},
            )["output"]
            history = runtime._handle_sync(
                "terminal.scrollback",
                {
                    **payload,
                    "tmux_window": window,
                    "lines": 200,
                    "history_only": True,
                },
            )["output"]
            if live_marker in full.splitlines() and old_marker in history.splitlines():
                break
            time.sleep(0.05)

        assert old_marker in history.splitlines()
        assert live_marker in full.splitlines()
        assert live_marker not in history.splitlines()
        styled_history = runtime._handle_sync(
            "terminal.scrollback",
            {
                **payload,
                "tmux_window": window,
                "lines": 200,
                "history_only": True,
                "ansi": True,
            },
        )["output"]
        assert old_marker in styled_history
        assert "\x1b[31m" in styled_history
        with pytest.raises(NodeAgentError) as invalid:
            runtime._handle_sync(
                "terminal.scrollback",
                {
                    **payload,
                    "tmux_window": window,
                    "history_only": "true",
                },
            )
        assert invalid.value.code == "request_invalid"
        with pytest.raises(NodeAgentError) as invalid_ansi:
            runtime._handle_sync(
                "terminal.scrollback",
                {
                    **payload,
                    "tmux_window": window,
                    "ansi": "true",
                },
            )
        assert invalid_ansi.value.code == "request_invalid"
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], check=False, capture_output=True)


@pytest.mark.asyncio
async def test_remote_access_forwards_history_and_ansi_to_node() -> None:
    access = RemoteAccess(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        TerminalControl(),
    )
    workspace = {
        "id": "workspace",
        "computer": {"id": "node", "connection_method": "node"},
    }
    terminal = {"id": "terminal", "tmux_window": "@7"}
    calls: list[tuple[str, dict[str, Any]]] = []

    access.is_node = lambda _workspace: True  # type: ignore[method-assign]

    async def request(  # type: ignore[no-untyped-def]
        _workspace,
        operation,
        payload,
    ) -> dict[str, str]:
        calls.append((str(operation), dict(payload)))
        return {"output": "node-history"}

    access._workspace_request = request  # type: ignore[method-assign]
    output = await access.capture_scrollback(
        workspace,
        terminal,
        321,
        history_only=True,
        ansi=True,
    )

    assert output == "node-history"
    assert calls == [
        (
            "terminal.scrollback",
            {
                "tmux_window": "@7",
                "lines": 321,
                "history_only": True,
                "ansi": True,
            },
        )
    ]


@pytest.mark.asyncio
async def test_remote_access_falls_back_to_recursive_listing_for_older_nodes() -> None:
    access = RemoteAccess(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        TerminalControl(),
    )
    workspace = {
        "id": "workspace",
        "computer": {"id": "node", "connection_method": "node"},
    }
    calls: list[tuple[str, dict[str, Any]]] = []
    access.is_node = lambda _workspace: True  # type: ignore[method-assign]

    def entry(name: str, relative_path: str, *, directory: bool) -> dict[str, Any]:
        return {
            "name": name,
            "relative_path": relative_path,
            "is_dir": directory,
            "size": 0,
            "mtime_ns": 1,
        }

    listings = {
        ".": [
            entry("node_modules", "node_modules", directory=True),
            entry("src", "src", directory=True),
        ],
        "src": [entry("deep", "src/deep", directory=True)],
        "src/deep": [entry("node-needle.txt", "src/deep/node-needle.txt", directory=False)],
    }

    async def request(  # type: ignore[no-untyped-def]
        _workspace,
        operation,
        payload,
    ) -> dict[str, Any]:
        calls.append((str(operation), dict(payload)))
        if operation == "files.search":
            raise RemoteAccessError("Node operation is unsupported", code="operation_unsupported")
        assert operation == "files.list"
        relative = str(payload["path"])
        return {"directory": relative, "entries": listings[relative]}

    access._workspace_request = request  # type: ignore[method-assign]
    result = await access.search_files(workspace, ".", "needle")

    assert [entry.relative_path for entry in result.entries] == ["src/deep/node-needle.txt"]
    assert result.skipped_noise == 1
    assert result.truncated is False
    assert [payload["path"] for operation, payload in calls if operation == "files.list"] == [
        ".",
        "src",
        "src/deep",
    ]


@pytest.mark.skipif(
    shutil.which("tmux") is None
    or not any(shutil.which(editor) for editor in ("nvim", "vim", "vi")),
    reason="tmux and a Vim-compatible editor are required",
)
def test_node_terminal_editor_reuses_the_live_file_window(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "note.txt").write_text("hello\n", encoding="utf-8")
    runtime = NodeRuntime([tmp_path])
    payload = _payload(workspace)
    session = str(payload["tmux_session"])
    try:
        first = runtime._handle_sync("terminal.editor.open", {**payload, "path": "note.txt"})
        second = runtime._handle_sync("terminal.editor.open", {**payload, "path": "note.txt"})
        assert first["terminal"]["tmux_window"] == second["terminal"]["tmux_window"]
        assert first["terminal"]["name"] == "vim-note.txt"
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], check=False, capture_output=True)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_node_workspace_usage_is_versioned_fixed_and_tracks_descendants(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    runtime = NodeRuntime([tmp_path])
    payload = _payload(workspace)
    session = str(payload["tmux_session"])
    try:
        terminal = runtime._handle_sync("workspace.ensure", payload)["terminals"][0]
        subprocess.run(
            ["tmux", "send-keys", "-t", str(terminal["tmux_window"]), "sleep 30", "Enter"],
            check=True,
        )
        deadline = time.monotonic() + 2
        result: dict[str, Any] = {}
        while time.monotonic() < deadline:
            result = runtime._handle_sync(
                "workspace.usage",
                {
                    **payload,
                    "workspace_usage_version": NODE_WORKSPACE_USAGE_VERSION,
                },
            )
            if result["usage"]["process_count"] >= 2:
                break
            time.sleep(0.05)

        assert result["workspace_usage_version"] == NODE_WORKSPACE_USAGE_VERSION
        assert result["usage"]["process_count"] >= 2
        assert result["usage"]["memory_bytes"] > 0
        assert result["usage"]["cpu_percent"] >= 0
        with pytest.raises(NodeAgentError) as exc_info:
            runtime._handle_sync(
                "workspace.usage",
                {**payload, "workspace_usage_version": True},
            )
        assert exc_info.value.code == "workspace_usage_version_incompatible"
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], check=False, capture_output=True)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_node_file_run_revalidates_registry_and_replays_without_reexecution(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "allowed"
    source_parent = workspace / "project"
    source_parent.mkdir(parents=True)
    private_state = workspace / ".node-private"
    private_state.mkdir()
    script_name = "한글 ;$(touch NODE_FILE_RUN_PWNED).py"
    script = source_parent / script_name
    script.write_text(
        "from pathlib import Path\n"
        "path = Path('count.txt')\n"
        "count = int(path.read_text() or '0') if path.exists() else 0\n"
        "path.write_text(str(count + 1))\n"
        "print('NODE_FILE_RUN_OK')\n",
        encoding="utf-8",
    )
    runtime = NodeRuntime(
        [workspace],
        file_run_root=tmp_path / "node-state" / "file-runs",
        private_state_root=private_state,
    )
    base = _payload(
        workspace,
        workspace_id="workspace-node-file-run",
        runner_registry_version=RUNNER_REGISTRY_VERSION,
    )
    session = str(base["tmux_session"])
    try:
        inspected = runtime._handle_sync(
            "file_run.inspect", {**base, "path": f"project/{script_name}"}
        )
        assert inspected["runner"] == {
            "id": "python3",
            "version": RUNNER_REGISTRY_VERSION,
        }
        digest = str(inspected["runnable"]["digest"])
        run_id = str(uuid.uuid4())
        start_payload = {
            **base,
            "run_id": run_id,
            "path": f"project/{script_name}",
            "expected_digest": digest,
            "runner_id": "python3",
            "runner_version": RUNNER_REGISTRY_VERSION,
            "argv": ["sh", "-c", "touch FORGED_NODE_ARGV"],
        }
        started = runtime._handle_sync("file_run.start", start_payload)
        assert started["terminal"]["role"] == "file_run"
        assert started["terminal"]["managed_run_id"] == run_id

        completed = _wait_for_node_file_run(
            runtime,
            {**base, "run_id": run_id},
            states={"finished"},
        )
        assert completed["observation"]["exit_code"] == 0
        assert (workspace / "count.txt").read_text(encoding="utf-8") == "1"
        assert not (workspace / "NODE_FILE_RUN_PWNED").exists()
        assert not (workspace / "FORGED_NODE_ARGV").exists()
        output = runtime._handle_sync(
            "terminal.scrollback",
            {
                **base,
                "tmux_window": started["terminal"]["tmux_window"],
                "lines": 200,
            },
        )["output"]
        assert "NODE_FILE_RUN_OK" in output

        replayed = runtime._handle_sync("file_run.start", start_payload)
        assert replayed["replayed"] is True
        assert replayed["observation"]["state"] == "finished"
        assert (workspace / "count.txt").read_text(encoding="utf-8") == "1"

        shutil.rmtree(source_parent)
        replayed_after_source_removal = runtime._handle_sync("file_run.start", start_payload)
        assert replayed_after_source_removal["replayed"] is True
        assert replayed_after_source_removal["observation"]["state"] == "finished"
        assert (workspace / "count.txt").read_text(encoding="utf-8") == "1"

        with pytest.raises(PathBoundaryError):
            runtime._handle_sync(
                "file_run.start",
                {**start_payload, "path": ".node-private/secret.py"},
            )

        with pytest.raises(NodeAgentError) as conflict:
            runtime._handle_sync(
                "file_run.start",
                {**start_payload, "path": "different.py"},
            )
        assert conflict.value.code == "idempotency_conflict"

        with pytest.raises(NodeAgentError) as digest_conflict:
            runtime._handle_sync(
                "file_run.start",
                {**start_payload, "expected_digest": "0" * 64},
            )
        assert digest_conflict.value.code == "idempotency_conflict"

        with pytest.raises(NodeAgentError) as runner_conflict:
            runtime._handle_sync(
                "file_run.start",
                {**start_payload, "runner_id": "nodejs"},
            )
        assert runner_conflict.value.code == "idempotency_conflict"
        assert (workspace / "count.txt").read_text(encoding="utf-8") == "1"

        metadata = tmp_path / "node-state" / "file-runs"
        assert metadata.is_dir()
        assert not any(path.name == "request.json" for path in workspace.rglob("*"))
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], check=False, capture_output=True)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_node_file_run_keeps_interactive_pty_and_targets_only_managed_slot(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "allowed" / "project"
    workspace.mkdir(parents=True)
    interactive = workspace / "interactive.py"
    interactive.write_text(
        "value = input('value: ')\nprint('received:' + value)\n",
        encoding="utf-8",
    )
    stubborn = workspace / "stubborn.py"
    stubborn.write_text(
        "from pathlib import Path\n"
        "import signal, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "Path('stubborn-ready').write_text('ready')\n"
        "while True: time.sleep(0.1)\n",
        encoding="utf-8",
    )
    runtime = NodeRuntime(
        [tmp_path / "allowed"],
        file_run_root=tmp_path / "node-state" / "file-runs",
    )
    base = _payload(
        workspace,
        workspace_id="workspace-node-interactive",
        runner_registry_version=RUNNER_REGISTRY_VERSION,
    )
    session = str(base["tmux_session"])
    try:
        inspected = runtime._handle_sync("file_run.inspect", {**base, "path": "interactive.py"})
        run_id = str(uuid.uuid4())
        started = runtime._handle_sync(
            "file_run.start",
            {
                **base,
                "run_id": run_id,
                "path": "interactive.py",
                "expected_digest": inspected["runnable"]["digest"],
                "runner_id": "python3",
                "runner_version": RUNNER_REGISTRY_VERSION,
            },
        )
        window = str(started["terminal"]["tmux_window"])
        _wait_for_node_file_run(runtime, {**base, "run_id": run_id}, states={"running"})
        runtime._tmux("send-keys", "-t", window, "hello-node", "Enter")
        _wait_for_node_file_run(runtime, {**base, "run_id": run_id}, states={"finished"})
        output = runtime._handle_sync(
            "terminal.scrollback",
            {**base, "tmux_window": window, "lines": 200},
        )["output"]
        assert "received:hello-node" in output

        inspected = runtime._handle_sync("file_run.inspect", {**base, "path": "stubborn.py"})
        stubborn_id = str(uuid.uuid4())
        runtime._handle_sync(
            "file_run.start",
            {
                **base,
                "run_id": stubborn_id,
                "path": "stubborn.py",
                "expected_digest": inspected["runnable"]["digest"],
                "runner_id": "python3",
                "runner_version": RUNNER_REGISTRY_VERSION,
            },
        )
        _wait_for_node_file_run(runtime, {**base, "run_id": stubborn_id}, states={"running"})
        readiness = workspace / "stubborn-ready"
        deadline = time.monotonic() + 4
        while not readiness.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert readiness.read_text(encoding="utf-8") == "ready"
        assert runtime._handle_sync("file_run.interrupt", {**base, "run_id": stubborn_id})["sent"]
        time.sleep(0.2)
        assert (
            runtime._handle_sync("file_run.observe", {**base, "run_id": stubborn_id})[
                "observation"
            ]["state"]
            == "running"
        )
        assert runtime._handle_sync("file_run.kill", {**base, "run_id": stubborn_id})["sent"]
        stopped = _wait_for_node_file_run(
            runtime,
            {**base, "run_id": stubborn_id},
            states={"stopped"},
        )
        assert stopped["observation"]["error_code"] == "forced"
        terminals = runtime._handle_sync("workspace.ensure", base)["terminals"]
        assert any(item["role"] == "shell" for item in terminals)
        managed = next(item for item in terminals if item["role"] == "file_run")
        for operation in ("terminal.rename", "terminal.close"):
            with pytest.raises(NodeAgentError) as protected:
                runtime._handle_sync(
                    operation,
                    {
                        **base,
                        "tmux_window": managed["tmux_window"],
                        "name": "forged",
                    },
                )
            assert protected.value.code == "terminal_managed"
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], check=False, capture_output=True)


@pytest.mark.asyncio
async def test_node_file_streams_are_chunked_atomic_and_abortable(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    source = workspace / "source.bin"
    source.write_bytes(b"0123456789")
    runtime = NodeRuntime([tmp_path])
    sent: list[dict[str, Any]] = []

    async def send(message: Any) -> None:
        sent.append(dict(message))

    download_id = uuid.uuid4().hex
    download = await runtime.handle(
        "files.download.open",
        {
            "workspace_path": str(workspace),
            "path": "source.bin",
            "offset": 2,
            "length": 5,
            "stream_id": download_id,
        },
        send,
    )
    assert download.start is not None
    await download.start()
    content = b"".join(
        base64.b64decode(message["data"]) for message in sent if message["type"] == "stream.data"
    )
    assert content == b"23456"
    assert sent[-1] == {"type": "stream.close", "stream_id": download_id}

    upload_id = uuid.uuid4().hex
    await runtime.handle(
        "files.upload.open",
        {
            "workspace_path": str(workspace),
            "parent": ".",
            "filename": "uploaded.txt",
            "overwrite": False,
            "max_bytes": 100,
            "stream_id": upload_id,
        },
        send,
    )
    upload = runtime.streams[upload_id]
    await upload.feed("한글".encode())
    await upload.close()
    assert (workspace / "uploaded.txt").read_text(encoding="utf-8") == "한글"

    partial_id = uuid.uuid4().hex
    await runtime.handle(
        "files.upload.open",
        {
            "workspace_path": str(workspace),
            "parent": ".",
            "filename": "partial.txt",
            "overwrite": False,
            "max_bytes": 100,
            "stream_id": partial_id,
        },
        send,
    )
    await runtime.streams[partial_id].feed(b"partial")
    await runtime.close_streams()
    assert not (workspace / "partial.txt").exists()
    assert not list(workspace.glob(".partial.txt.termroom-*"))


@pytest.mark.asyncio
async def test_node_download_rejects_replaced_leaf_before_streaming(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    source = workspace / "source.txt"
    source.write_bytes(b"admitted")
    outside = tmp_path / "outside-secret"
    outside.write_bytes(b"must not leak")
    runtime = NodeRuntime([tmp_path])
    sent: list[dict[str, Any]] = []

    async def send(message: Any) -> None:
        sent.append(dict(message))

    opened = await runtime.handle(
        "files.download.open",
        {
            "workspace_path": str(workspace),
            "path": source.name,
            "stream_id": "a" * 32,
        },
        send,
    )
    source.unlink()
    source.symlink_to(outside)

    assert opened.start is not None
    await opened.start()
    assert not any(message["type"] == "stream.data" for message in sent)
    assert sent[-1]["type"] == "stream.error"
    assert sent[-1]["code"] == "path_changed"
    assert outside.read_bytes() == b"must not leak"


@pytest.mark.asyncio
async def test_node_upload_finishes_in_open_parent_after_parent_replacement(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    parent = workspace / "nested"
    workspace.mkdir()
    parent.mkdir()
    runtime = NodeRuntime([tmp_path])
    stream_id = "b" * 32

    async def send(_message: Any) -> None:
        raise AssertionError("This operation must not send an unsolicited frame")

    await runtime.handle(
        "files.upload.open",
        {
            "workspace_path": str(workspace),
            "parent": "nested",
            "filename": "uploaded.txt",
            "overwrite": False,
            "max_bytes": 100,
            "stream_id": stream_id,
        },
        send,
    )
    await runtime.streams[stream_id].feed(b"pinned parent")
    moved = workspace / "nested-original"
    parent.rename(moved)
    parent.mkdir()

    await runtime.streams[stream_id].close()

    assert (moved / "uploaded.txt").read_bytes() == b"pinned parent"
    assert not (parent / "uploaded.txt").exists()
    assert not list(parent.glob(".uploaded.txt.termroom-*"))


def test_node_operation_set_does_not_expose_arbitrary_shell(tmp_path: Path) -> None:
    runtime = NodeRuntime([tmp_path])
    with pytest.raises(Exception) as exc_info:
        runtime._handle_sync("shell.exec", {"command": "id"})
    assert "unsupported" in str(exc_info.value).casefold()


def test_node_config_contains_no_private_key_material(tmp_path: Path) -> None:
    runtime = NodeRuntime([tmp_path])
    assert json.dumps([str(path) for path in runtime.allowed_roots])


@pytest.mark.asyncio
async def test_node_agent_retrieves_expected_background_disconnects(
    tmp_path: Path,
) -> None:
    agent = NodeAgent(
        NodeConfig("http://127.0.0.1:1", "a" * 32, "test", (tmp_path,)),
        generate_private_key(),
    )
    observed: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: observed.append(context))

    async def disconnect(_message: Any, **_kwargs: Any) -> None:
        raise NodeAgentError("control connection closed", code="node_offline")

    agent._handle_request = disconnect  # type: ignore[method-assign]
    try:
        await agent._dispatch({"type": "request"})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert not agent._request_tasks
    assert observed == []


@pytest.mark.asyncio
async def test_node_terminal_bridge_retrieves_the_canceled_direction() -> None:
    observed: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: observed.append(context))

    async def exercise() -> None:
        peer_started = asyncio.Event()

        async def backend_disconnect() -> None:
            await peer_started.wait()
            raise NodeCoreError("Node disconnected")

        async def browser_disconnect() -> None:
            peer_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                raise WebSocketDisconnect(1012)

        output_task = asyncio.create_task(backend_disconnect())
        input_task = asyncio.create_task(browser_disconnect())
        done, pending = await asyncio.wait(
            {output_task, input_task}, return_when=asyncio.FIRST_COMPLETED
        )
        with pytest.raises(NodeCoreError, match="Node disconnected"):
            await _settle_bridge_tasks(done, pending)

    try:
        await exercise()
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert observed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("paste_data", "competing_input"),
    [(None, False), ("\x1b[200~pwd\x1b[201~", False), ("\x1b[200~pwd\x1b[201~", True)],
)
async def test_node_terminal_binary_and_structured_input_take_over_before_send(
    paste_data: str | None, competing_input: bool,
) -> None:
    events: list[tuple[Any, ...]] = []
    attach_payload: dict[str, Any] = {}

    class FakeStore:
        def touch_terminal(self, _terminal_id: str) -> None:
            return None

        def touch_terminal_output(self, _terminal_id: str) -> None:
            return None

        def add_command(self, _workspace_id: str, _terminal_id: str, command: str) -> None:
            events.append(("command", command))

    class FakeStream:
        def __aiter__(self) -> FakeStream:
            return self

        async def __anext__(self) -> bytes:
            await asyncio.Event().wait()
            raise StopAsyncIteration

        async def receive_event(self) -> bytes | dict[str, Any] | None:
            if not hasattr(self, "events"):
                await asyncio.Event().wait()
            return self.events.pop(0)

        async def control(self, action: str, **values: Any) -> None:
            events.append(("control", action, values))

        async def send(self, value: bytes) -> None:
            events.append(("send", value))

        async def close(self) -> None:
            return None

    stream = FakeStream()
    stream.events = [
        {
            "kind": "pane_mode",
            "revision": 1,
            "available": True,
            "mode": {
                "session": "termroom-project",
                "window": "@1",
                "pane": "%1",
                "pane_pid": 4321,
                "alternate": True,
                "mouse_tracking": False,
                "mouse_flags": [0, 0, 0, 0, 0],
            },
        },
        b"remote output",
        None,
    ]

    browser_frames: list[tuple[str, Any]] = []

    class FakeConnection:
        async def open_stream(
            self, _operation: str, _payload: dict[str, Any]
        ) -> tuple[dict[str, Any], FakeStream]:
            attach_payload.update(_payload)
            stream.stream_id = "a" * 32
            return {}, stream

        async def request(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            assert operation == "terminal.resize"
            events.append(
                (
                    "control",
                    "resize",
                    {key: payload[key] for key in ("rows", "cols", "affects_grid")},
                )
            )
            return _terminal_resize_ack(payload)

    class FakeNodes:
        def connection(self, _computer_id: str) -> FakeConnection:
            return FakeConnection()

    messages = [
        {
            "type": "websocket.receive",
            "text": json.dumps({"kind": "resize", "rows": 24, "cols": 80}),
        },
        {"type": "websocket.receive", "text": "raw-passive"},
        {"type": "websocket.receive", "bytes": b"binary-input"},
        {
            "type": "websocket.receive",
            "text": json.dumps(
                {
                    "kind": "input",
                    "data": "a",
                    "rows": 37,
                    "cols": 111,
                    "user_input": True,
                }
            ),
        },
        {
            "type": "websocket.receive",
            "text": json.dumps(
                {
                    "kind": "input",
                    "data": "b",
                    "rows": 37,
                    "cols": 111,
                    "user_input": True,
                }
            ),
        },
        {
            "type": "websocket.receive",
            "text": json.dumps({
                "kind": "command", "data": "pwd", "rows": 38, "cols": 112,
                **({"paste_data": paste_data} if paste_data is not None else {}),
            }),
        },
        {
            "type": "websocket.receive",
            "text": json.dumps({"kind": "input", "data": "legacy"}),
        },
        {"type": "websocket.disconnect", "code": 1000},
    ]

    class FakeWebSocket:
        async def receive(self) -> dict[str, Any]:
            message = messages.pop(0)
            if competing_input and '"kind": "command"' in str(message.get("text", "")):
                control.mark_input("terminal", existing_owner)
            return message

        async def send_bytes(self, value: bytes) -> None:
            browser_frames.append(("control", json.loads(value)))

        async def send_text(self, value: str) -> None:
            browser_frames.append(("text", value))

        async def close(self, *, code: int, reason: str) -> None:
            raise AssertionError((code, reason))

    control = TerminalControl()
    existing_owner = control.register("terminal")
    control.mark_input("terminal", existing_owner)
    access = RemoteAccess(
        FakeStore(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        FakeNodes(),  # type: ignore[arg-type]
        control,
    )

    async def ensure_workspace(_workspace: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    access.ensure_workspace = ensure_workspace  # type: ignore[method-assign]
    workspace = {
        "id": "workspace",
        "path": "/project",
        "tmux_session": "termroom-project",
        "computer": {"id": "node", "connection_method": "node"},
    }
    terminal = {"id": "terminal", "tmux_window": "@1"}

    await access.bridge(
        FakeWebSocket(),  # type: ignore[arg-type]
        workspace,
        terminal,
        device_id="device",
    )

    assert attach_payload["pane_mode_control"] is True
    assert events == [
        (
            "control",
            "resize",
            {"rows": 24, "cols": 80, "affects_grid": False},
        ),
        ("send", b"raw-passive"),
        (
            "control",
            "resize",
            {"rows": 24, "cols": 80, "affects_grid": True},
        ),
        ("send", b"binary-input"),
        (
            "control",
            "resize",
            {"rows": 37, "cols": 111, "affects_grid": True},
        ),
        ("send", b"a"),
        ("send", b"b"),
        (
            "control",
            "resize",
            {"rows": 38, "cols": 112, "affects_grid": True},
        ),
        ("command", "pwd"),
        ("send", (paste_data or "pwd").encode() + b"\r"),
        ("send", b"legacy"),
    ]
    assert browser_frames == [
        (
            "control",
            {
                "kind": "pane_mode",
                "terminal_id": "terminal",
                "generation": next(
                    message["generation"]
                    for kind, message in browser_frames
                    if kind == "control"
                ),
                "revision": 1,
                "available": True,
                "session": "termroom-project",
                "window": "@1",
                "pane": "%1",
                "pane_pid": 4321,
                "alternate": True,
                "mouse_tracking": False,
                "mouse_flags": [0, 0, 0, 0, 0],
            },
        ),
        ("text", "remote output"),
    ]


@pytest.mark.asyncio
async def test_node_fresh_terminal_bootstraps_grid_before_user_input() -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    class FakeStore:
        def touch_terminal(self, _terminal_id: str) -> None:
            return None

        def touch_terminal_output(self, _terminal_id: str) -> None:
            return None

    class FakeStream:
        async def receive_event(self) -> bytes | dict[str, Any] | None:
            await asyncio.Event().wait()

        async def control(self, action: str, **values: Any) -> None:
            events.append((action, values))

        async def send(self, _value: bytes) -> None:
            return None

        async def close(self) -> None:
            return None

    stream = FakeStream()

    class FakeConnection:
        async def open_stream(
            self, _operation: str, _payload: dict[str, Any]
        ) -> tuple[dict[str, Any], FakeStream]:
            stream.stream_id = "b" * 32
            return {"bootstrap_grid": True}, stream

        async def request(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            assert operation == "terminal.resize"
            assert payload["bootstrap"] is True
            events.append(
                ("resize", {key: payload[key] for key in ("rows", "cols", "affects_grid")})
            )
            return _terminal_resize_ack(payload)

    class FakeNodes:
        def connection(self, _computer_id: str) -> FakeConnection:
            return FakeConnection()

    class FakeWebSocket:
        def __init__(self) -> None:
            self.messages = [
                {
                    "type": "websocket.receive",
                    "text": json.dumps({"kind": "resize", "rows": 33, "cols": 162}),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ]

        async def receive(self) -> dict[str, Any]:
            return self.messages.pop(0)

        async def send_text(self, _value: str) -> None:
            return None

        async def close(self, *, code: int, reason: str) -> None:
            raise AssertionError((code, reason))

    control = TerminalControl()
    access = RemoteAccess(
        FakeStore(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        FakeNodes(),  # type: ignore[arg-type]
        control,
    )

    async def ensure_workspace(_workspace: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    access.ensure_workspace = ensure_workspace  # type: ignore[method-assign]
    await access.bridge(
        FakeWebSocket(),  # type: ignore[arg-type]
        {
            "id": "workspace",
            "path": "/project",
            "tmux_session": "termroom-project",
            "computer": {"id": "node", "connection_method": "node"},
        },
        {"id": "terminal", "tmux_window": "@1"},
        device_id="device",
    )

    assert events == [("resize", {"rows": 33, "cols": 162, "affects_grid": True})]
    assert control.presence("terminal")["input_revision"] == 0


@pytest.mark.asyncio
async def test_node_terminal_bridge_demotes_owner_that_changes_during_control() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    control = TerminalControl()
    competing_client = control.register("terminal")

    class FakeStore:
        def touch_terminal(self, _terminal_id: str) -> None:
            return None

        def touch_terminal_output(self, _terminal_id: str) -> None:
            return None

    class FakeStream:
        async def receive_event(self) -> bytes | dict[str, Any] | None:
            await asyncio.Event().wait()

        async def control(self, action: str, **values: Any) -> None:
            events.append((action, values))
            if len(events) == 1:
                control.mark_input("terminal", competing_client)

        async def send(self, _value: bytes) -> None:
            return None

        async def close(self) -> None:
            return None

    stream = FakeStream()

    class FakeConnection:
        async def open_stream(
            self, _operation: str, _payload: dict[str, Any]
        ) -> tuple[dict[str, Any], FakeStream]:
            stream.stream_id = "c" * 32
            return {}, stream

        async def request(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
            assert operation == "terminal.resize"
            events.append(
                ("resize", {key: payload[key] for key in ("rows", "cols", "affects_grid")})
            )
            if len(events) == 1:
                control.mark_input("terminal", competing_client)
            return _terminal_resize_ack(payload)

    class FakeNodes:
        def connection(self, _computer_id: str) -> FakeConnection:
            return FakeConnection()

    class FakeWebSocket:
        def __init__(self) -> None:
            self.messages = [
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {
                            "kind": "input",
                            "data": "",
                            "rows": 37,
                            "cols": 111,
                            "user_input": True,
                        }
                    ),
                },
                {"type": "websocket.disconnect", "code": 1000},
            ]

        async def receive(self) -> dict[str, Any]:
            return self.messages.pop(0)

        async def send_text(self, _value: str) -> None:
            return None

        async def close(self, *, code: int, reason: str) -> None:
            raise AssertionError((code, reason))

    access = RemoteAccess(
        FakeStore(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        FakeNodes(),  # type: ignore[arg-type]
        control,
    )

    async def ensure_workspace(_workspace: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    access.ensure_workspace = ensure_workspace  # type: ignore[method-assign]
    workspace = {
        "id": "workspace",
        "path": "/project",
        "tmux_session": "termroom-project",
        "computer": {"id": "node", "connection_method": "node"},
    }
    try:
        await access.bridge(
            FakeWebSocket(),  # type: ignore[arg-type]
            workspace,
            {"id": "terminal", "tmux_window": "@1"},
            device_id="device",
        )
    finally:
        control.unregister("terminal", competing_client)

    assert events == [
        (
            "resize",
            {"rows": 37, "cols": 111, "affects_grid": True},
        ),
        (
            "resize",
            {"rows": 37, "cols": 111, "affects_grid": False},
        ),
    ]


@pytest.mark.asyncio
async def test_node_terminal_bridge_retrieves_children_when_parent_is_canceled() -> None:
    observed: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: observed.append(context))

    async def exercise() -> None:
        child_started = asyncio.Event()

        async def child() -> None:
            child_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                raise WebSocketDisconnect(1012)

        child_task = asyncio.create_task(child())
        await child_started.wait()
        await _cancel_bridge_tasks((child_task,))

    try:
        await exercise()
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert observed == []
