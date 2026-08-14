from __future__ import annotations

import asyncio
import base64
import gc
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from termroom.file_runs import RUNNER_REGISTRY_VERSION
from termroom.node_agent import (
    NodeAgent,
    NodeAgentError,
    NodeConfig,
    NodeRuntime,
    normalize_allowed_roots,
)
from termroom.node_core import NODE_STREAM_QUEUE_DEPTH, NodeCoreError
from termroom.node_protocol import (
    NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW,
    NODE_REMOTE_RUN_SOURCE_VERSION,
    NODE_WORKSPACE_USAGE_VERSION,
    generate_private_key,
)
from termroom.remote_access import _cancel_bridge_tasks, _settle_bridge_tasks
from termroom.run_sources import SourceFileChangedError, SourceValidationError
from termroom.security import PathBoundaryError


def _payload(workspace: Path, **values: Any) -> dict[str, Any]:
    return {
        "workspace_path": str(workspace),
        "tmux_session": f"termroom-node-test-{uuid.uuid4().hex[:12]}",
        **values,
    }


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


@pytest.mark.asyncio
async def test_node_allowed_roots_and_file_operations_are_bounded(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    workspace = allowed / "project"
    outside = tmp_path / "outside"
    workspace.mkdir(parents=True)
    outside.mkdir()
    (workspace / "hello.txt").write_text("before\n", encoding="utf-8")
    (allowed / "escape").symlink_to(outside, target_is_directory=True)

    runtime = NodeRuntime([allowed])
    roots = runtime._handle_sync("workspace.roots", {})
    assert roots == {"roots": [{"path": str(allowed), "name": "allowed"}]}
    assert runtime._handle_sync("workspace.validate", {"path": str(workspace)})[
        "path"
    ] == str(workspace)
    with pytest.raises(PathBoundaryError):
        runtime._handle_sync("workspace.validate", {"path": str(outside)})
    with pytest.raises(PathBoundaryError):
        runtime._handle_sync("workspace.validate", {"path": str(allowed / "escape")})

    listed = runtime._handle_sync(
        "files.list", {"workspace_path": str(workspace), "path": "."}
    )
    assert [entry["name"] for entry in listed["entries"]] == ["hello.txt"]
    recent = runtime._handle_sync(
        "files.recent", {"workspace_path": str(workspace), "limit": 5}
    )
    assert [entry["relative_path"] for entry in recent["entries"]] == ["hello.txt"]
    assert recent["scanned_files"] == 1
    assert recent["truncated"] is False
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
    assert b"".join(
        base64.b64decode(message["data"])
        for message in sent
        if message["type"] == "stream.data"
    ) == b"before\n"
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
        await manifest_stream.control(
            "credit", {"count": NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW + 1}
        )
    assert invalid_credit.value.code == "stream_control_invalid"
    await manifest_stream.control("credit", {"count": opened.value["frame_count"]})
    with pytest.raises(NodeAgentError) as duplicate_credit:
        await manifest_stream.control("credit", {"count": 1})
    assert duplicate_credit.value.code == "stream_control_invalid"
    await opened.start()
    encoded_manifest = b"".join(
        base64.b64decode(message["data"])
        for message in sent
        if message["type"] == "stream.data"
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
    current = runtime._handle_sync(
        "remote_run_source.stat", {**base, "path": "src/run.sh"}
    )["entry"]
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
        base64.b64decode(message["data"])
        for message in sent
        if message["type"] == "stream.data"
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
    messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
        maxsize=NODE_STREAM_QUEUE_DEPTH
    )
    peak_data_depth = 0
    overflowed = False

    async def send(message: Any) -> None:
        nonlocal overflowed, peak_data_depth
        try:
            messages.put_nowait(dict(message))
        except asyncio.QueueFull as exc:
            overflowed = True
            raise NodeCoreError(
                "Node stream exceeded its buffer", code="stream_overflow"
            ) from exc
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
    batch_remaining = min(
        NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW, remaining_to_grant
    )
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
                batch_remaining = min(
                    NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW, remaining_to_grant
                )
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
    assert b"".join(
        base64.b64decode(message["data"])
        for message in sent
        if message["type"] == "stream.data"
    ) == source

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


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_node_workspace_reconnect_reuses_tmux_and_terminal_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    runtime = NodeRuntime([tmp_path])
    payload = _payload(workspace)
    session = str(payload["tmux_session"])
    try:
        first = runtime._handle_sync("workspace.ensure", payload)["terminals"]
        second = runtime._handle_sync("workspace.ensure", payload)["terminals"]
        assert first == second
        assert len(first) == 1

        created = runtime._handle_sync(
            "terminal.create", {**payload, "name": "한글 shell"}
        )["terminal"]
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
        assert terminals == first
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", session], check=False, capture_output=True
        )


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
        subprocess.run(
            ["tmux", "kill-session", "-t", session], check=False, capture_output=True
        )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_node_file_run_revalidates_registry_and_replays_without_reexecution(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "allowed" / "project"
    workspace.mkdir(parents=True)
    script_name = "한글 ;$(touch NODE_FILE_RUN_PWNED).py"
    script = workspace / script_name
    script.write_text(
        "from pathlib import Path\n"
        "path = Path('count.txt')\n"
        "count = int(path.read_text() or '0') if path.exists() else 0\n"
        "path.write_text(str(count + 1))\n"
        "print('NODE_FILE_RUN_OK')\n",
        encoding="utf-8",
    )
    runtime = NodeRuntime(
        [tmp_path / "allowed"],
        file_run_root=tmp_path / "node-state" / "file-runs",
    )
    base = _payload(
        workspace,
        workspace_id="workspace-node-file-run",
        runner_registry_version=RUNNER_REGISTRY_VERSION,
    )
    session = str(base["tmux_session"])
    try:
        inspected = runtime._handle_sync(
            "file_run.inspect", {**base, "path": script_name}
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
            "path": script_name,
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

        with pytest.raises(NodeAgentError) as conflict:
            runtime._handle_sync(
                "file_run.start",
                {**start_payload, "path": "different.py"},
            )
        assert conflict.value.code == "idempotency_conflict"

        metadata = tmp_path / "node-state" / "file-runs"
        assert metadata.is_dir()
        assert not any(path.name == "request.json" for path in workspace.rglob("*"))
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", session], check=False, capture_output=True
        )


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
        "import signal, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
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
        inspected = runtime._handle_sync(
            "file_run.inspect", {**base, "path": "interactive.py"}
        )
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
        _wait_for_node_file_run(
            runtime, {**base, "run_id": run_id}, states={"running"}
        )
        runtime._tmux("send-keys", "-t", window, "hello-node", "Enter")
        _wait_for_node_file_run(
            runtime, {**base, "run_id": run_id}, states={"finished"}
        )
        output = runtime._handle_sync(
            "terminal.scrollback",
            {**base, "tmux_window": window, "lines": 200},
        )["output"]
        assert "received:hello-node" in output

        inspected = runtime._handle_sync(
            "file_run.inspect", {**base, "path": "stubborn.py"}
        )
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
        _wait_for_node_file_run(
            runtime, {**base, "run_id": stubborn_id}, states={"running"}
        )
        assert runtime._handle_sync(
            "file_run.interrupt", {**base, "run_id": stubborn_id}
        )["sent"]
        time.sleep(0.2)
        assert runtime._handle_sync(
            "file_run.observe", {**base, "run_id": stubborn_id}
        )["observation"]["state"] == "running"
        assert runtime._handle_sync(
            "file_run.kill", {**base, "run_id": stubborn_id}
        )["sent"]
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
        subprocess.run(
            ["tmux", "kill-session", "-t", session], check=False, capture_output=True
        )


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
        base64.b64decode(message["data"])
        for message in sent
        if message["type"] == "stream.data"
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

    async def disconnect(_message: Any) -> None:
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
