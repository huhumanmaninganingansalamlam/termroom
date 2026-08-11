from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from termroom.node_agent import NodeRuntime
from termroom.node_protocol import (
    NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW,
    NODE_REMOTE_RUN_VERSION,
)
from termroom.node_remote_runs import (
    NodeRemoteRunClient,
    NodeRemoteRunError,
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
    await _operation(runtime, "remote_run.delete", payload)


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
    temporary = aborted_stream.temporary  # type: ignore[attr-defined]
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
