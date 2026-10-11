from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pty
import select
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import tty
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from termroom.app import create_app
from termroom.config import Settings
from termroom.db import StateStore
from termroom.node_agent import TerminalAgentStream
from termroom.ssh_backend import SSHBackend
from termroom.terminals import TerminalError, TerminalManager

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")


def _app(tmp_path: Path):  # type: ignore[no-untyped-def]
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="internal-secret",
        login_password="correct-password",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open(".")
    terminal = app.state.terminals.ensure_workspace(workspace)[0]
    return app, workspace, terminal


def _cleanup(app, workspace) -> None:  # type: ignore[no-untyped-def]
    subprocess.run(
        ["tmux", "kill-session", "-t", str(workspace["tmux_session"])],
        check=False,
        capture_output=True,
    )


class _BridgeWebSocket:
    def __init__(self, *messages: dict[str, object]) -> None:
        self.messages = list(messages)

    async def receive(self) -> dict[str, object]:
        return self.messages.pop(0)

    async def send_text(self, _value: str) -> None:
        return None

    async def send_bytes(self, _value: bytes) -> None:
        return None

    async def close(self, *, code: int, reason: str) -> None:
        raise AssertionError((code, reason))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_kind", ["local", "ssh", "node"])
async def test_long_paste_short_pty_writes_preserve_tail_and_next_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend_kind: str
) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    root = store.ensure_root(tmp_path)
    workspace = store.create_workspace(str(root["id"]), ".", "fixture", "session")
    terminal = store.create_terminal(str(workspace["id"]), "fixture", "@7")
    master_fd, slave_fd = pty.openpty()
    tty.setraw(slave_fd)
    os.set_blocking(slave_fd, False)
    text = "한글" * 3300 + "종료"
    assert len(text.encode()) == 19806
    paste = "\x1b[200~" + text + "\x1b[201~"
    confirmation = "\x1b[200~confirm\x1b[201~"
    expected = (paste + "\r" + confirmation + "\r\tlegacy").encode() + b"binary"
    received = bytearray()
    original_write = os.write

    def short_write(fd: int, data: bytes) -> int:
        # A real PTY accepts the prefix; its reported count is the contract.
        return original_write(fd, data[:1024] if fd == master_fd else data)

    def drain() -> None:
        while True:
            try:
                chunk = os.read(slave_fd, 65536)
            except BlockingIOError:
                return
            if not chunk:
                loop.remove_reader(slave_fd)
                return
            received.extend(chunk)

    monkeypatch.setattr(os, "write", short_write)
    loop = asyncio.get_running_loop()
    loop.add_reader(slave_fd, drain)
    browser = _BridgeWebSocket(
        *({"type": "websocket.receive", "text": json.dumps(payload)} for payload in (
            {"kind": "command", "data": text, "paste_data": paste},
            {"kind": "command", "data": "confirm", "paste_data": confirmation},
            {"kind": "input", "data": "\t"},
        )),
        {"type": "websocket.receive", "text": "legacy"},
        {"type": "websocket.receive", "bytes": b"binary"},
        _terminal_disconnect(),
    )
    try:
        if backend_kind == "node":
            async def send(_message: object) -> None:
                pass

            stream = TerminalAgentStream("d" * 32, 999999, master_fd, send, {})
            for chunk in (paste.encode() + b"\r", confirmation.encode() + b"\r",
                          b"\t", b"legacy", b"binary"):
                await asyncio.wait_for(stream.feed(chunk), 2)
        else:
            if backend_kind == "local":
                backend = TerminalManager(store)
                monkeypatch.setattr(backend, "_setup_browser_terminal",
                                    lambda *_args: (999999, master_fd))
                monkeypatch.setattr(backend, "_release_browser_terminal", lambda *_args: None)
                monkeypatch.setattr(backend, "_finish_browser_terminal", lambda *_args: None)
            else:
                backend = SSHBackend(store, tmp_path)
                monkeypatch.setattr(backend, "ensure_workspace", lambda *_args: [])
                monkeypatch.setattr(backend, "_spawn_ssh_tmux_client",
                                    lambda *_args: (999999, master_fd))
                monkeypatch.setattr(backend, "_wait_for_pid", lambda *_args: True)
                monkeypatch.setattr(os, "killpg", lambda *_args: None)
            monkeypatch.setattr(backend, "current_pane_mode", lambda *_args: {"pane": "%1"})
            await asyncio.wait_for(backend.bridge(browser, workspace, terminal), 3)
            assert sorted(store.list_commands(str(workspace["id"]))) == sorted([text, "confirm"])
        drain()
        print(f"{backend_kind}: expected={len(expected)} received={len(received)}")
        assert bytes(received) == expected, "PTY lost paste tail/terminator or joined next input"
    finally:
        loop.remove_reader(slave_fd)
        for fd in (master_fd, slave_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


@pytest.mark.asyncio
async def test_node_cancelled_input_retains_fd_until_write_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original_write = os.write
    data = b"\x1b[200~owned-input\x1b[201~\r"

    def blocked_write(fd: int, value: bytes) -> int:
        if fd == write_fd:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3), "owned input writer was not released"
        return original_write(fd, value)

    async def send(_message: object) -> None:
        pass

    monkeypatch.setattr(os, "write", blocked_write)
    monkeypatch.setattr(os, "killpg", lambda *_args: None)
    monkeypatch.setattr("termroom.node_agent._wait_for_pid", lambda *_args: True)
    stream = TerminalAgentStream("d" * 32, 999999, write_fd, send, {})
    feeding = asyncio.create_task(stream.feed(data))
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        feeding.cancel()
        await asyncio.sleep(0)
        closing = asyncio.create_task(stream.close())
        await asyncio.sleep(0)
        assert not feeding.done(), "cancellation must not release a live writer"
        assert not closing.done()
        os.fstat(write_fd)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(feeding, 1)
        await asyncio.wait_for(closing, 1)
        assert os.read(read_fd, 65536) == data
        with pytest.raises(OSError):
            os.fstat(write_fd)
    finally:
        release.set()
        await asyncio.gather(feeding, *([closing] if closing else []), return_exceptions=True)
        for fd in (read_fd, write_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


def _receive_terminal_text(websocket) -> str:  # type: ignore[no-untyped-def]
    """Existing text contracts ignore the separately typed Local mode controls."""
    while True:
        message = websocket.receive()
        if message["type"] in {"websocket.disconnect", "websocket.close"}:
            raise WebSocketDisconnect(message.get("code", 1000))
        if message.get("text") is not None:
            return message["text"]
        assert json.loads(message["bytes"])["kind"] == "pane_mode"


async def _assert_bridge_barrier_keeps_loop_live(
    app,
    bridge,
    second_terminal_progress: Callable[[], Awaitable[None]],
    entered: threading.Event,
    release: threading.Event,
) -> tuple[list[str], dict[str, bool]]:  # type: ignore[no-untyped-def]
    loop = asyncio.get_running_loop()
    scheduled = asyncio.Event()
    ready: set[str] = set()
    ready_event = asyncio.Event()
    progress = {
        name: threading.Event() for name in ("heartbeat", "health", "navigation", "second_terminal")
    }
    trace: list[str] = []
    trace_lock = threading.Lock()

    def record(event: str) -> None:
        with trace_lock:
            trace.append(event)

    async def probe(name: str) -> None:
        ready.add(name)
        if len(ready) == len(progress):
            ready_event.set()
        await scheduled.wait()
        if name == "health":
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.get("/health")
            assert response.status_code == 200 and response.text == "ok"
        elif name == "navigation":
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                response = await client.get("/")
            assert response.status_code in {200, 401}
        elif name == "second_terminal":
            await second_terminal_progress()
        else:
            await asyncio.sleep(0)
        progress[name].set()
        record(f"probe_completed:{name}")

    probes = [asyncio.create_task(probe(name)) for name in progress]
    await ready_event.wait()
    bridge_task = asyncio.create_task(bridge())
    bridge_done = threading.Event()
    bridge_task.add_done_callback(lambda _task: bridge_done.set())
    before_release: dict[str, bool] = {}

    def watchdog() -> None:
        try:
            if not entered.wait(2):
                record("barrier_not_entered")
                return
            record("barrier_entered")
            record("heartbeat_scheduled")
            record("health_scheduled")
            record("navigation_scheduled")
            record("second_terminal_progress_scheduled")
            loop.call_soon_threadsafe(scheduled.set)
            deadline = time.monotonic() + 0.4
            while time.monotonic() < deadline and not all(
                event.is_set() for event in progress.values()
            ):
                time.sleep(0.005)
            before_release.update({name: event.is_set() for name, event in progress.items()})
            before_release["bridge_done"] = bridge_done.is_set()
            record("progress_before_release")
        finally:
            record("watchdog_release")
            release.set()

    watchdog_thread = threading.Thread(target=watchdog, daemon=True)
    watchdog_thread.start()
    try:
        await asyncio.wait_for(bridge_task, timeout=6)
    finally:
        release.set()
        if bridge_task.done() and bridge_task.exception() is None:
            await asyncio.wait_for(asyncio.gather(*probes), timeout=2)
        else:
            for task in probes:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*probes, return_exceptions=True)
        await asyncio.to_thread(watchdog_thread.join, 2)
    record("bridge_complete")
    assert not watchdog_thread.is_alive()
    assert bridge_done.is_set()
    assert all(event.is_set() for event in progress.values())
    assert trace.index("barrier_entered") < trace.index("heartbeat_scheduled")
    assert trace.index("heartbeat_scheduled") < trace.index("health_scheduled")
    assert trace.index("health_scheduled") < trace.index("navigation_scheduled")
    assert trace.index("navigation_scheduled") < trace.index("second_terminal_progress_scheduled")
    assert trace.index("second_terminal_progress_scheduled") < trace.index(
        "progress_before_release"
    )
    assert trace.index("progress_before_release") < trace.index("watchdog_release")
    assert max(trace.index(f"probe_completed:{name}") for name in progress) < trace.index(
        "bridge_complete"
    )
    return trace, before_release


def _terminal_disconnect() -> dict[str, object]:
    return {"type": "websocket.disconnect", "code": 1000}


@pytest.mark.asyncio
async def test_two_idle_local_pty_readers_leave_shared_executor_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real private PTYs, not a replacement reader/output pipeline."""
    app, workspace, terminal = _app(tmp_path)
    manager = app.state.terminals
    fifo = tmp_path / "inert-output.fifo"
    os.mkfifo(fifo)
    producer = (
        "import os,sys\n"
        "f=os.open(sys.argv[1],os.O_RDWR)\n"
        "with os.fdopen(f,'rb',buffering=0) as stream:\n"
        " for line in iter(stream.readline,b''):\n"
        "  os.write(1,line)\n"
    )
    manager._run_tmux(
        "respawn-pane",
        "-k",
        "-t",
        str(terminal["tmux_window"]),
        shlex.join([sys.executable, "-u", "-c", producer, str(fifo)]),
    )
    manager._run_tmux("set-option", "-t", str(workspace["tmux_session"]), "status", "off")
    output_fd = os.open(fifo, os.O_RDWR)
    loop = asyncio.get_running_loop()
    previous_executor = loop._default_executor
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="owned-idle-pty")
    loop.set_default_executor(executor)
    owned: dict[int, tuple[int, str]] = {}
    idle = [asyncio.Event(), asyncio.Event()]
    reader_threads: dict[int, int] = {}
    registrations: set[int] = set()
    queues: list[asyncio.Queue[bytes]] = []
    original_read, original_spawn = os.read, manager._spawn_tmux_client
    original_add, original_remove = loop.add_reader, loop.remove_reader
    original_queue = asyncio.Queue

    class Socket(_BridgeWebSocket):
        def __init__(self) -> None:
            super().__init__()
            self.incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            self.controls: list[dict[str, object]] = []
            self.text: list[str] = []
            self.delivered = asyncio.Event()

        async def receive(self) -> dict[str, object]:
            return await self.incoming.get()

        async def send_bytes(self, value: bytes) -> None:
            self.controls.append(json.loads(value))

        async def send_text(self, value: str) -> None:
            assert self.controls, "initial mode control precedes output"
            self.text.append(value)
            self.delivered.set()

    sockets = [Socket(), Socket()]
    bridges: list[asyncio.Task[None]] = []

    def spawn(_workspace, view):  # type: ignore[no-untyped-def]
        pid, fd = original_spawn(_workspace, view)
        owned[fd] = (pid, view)
        return pid, fd

    def read(fd: int, length: int) -> bytes:
        if fd in owned and not select.select([fd], [], [], 0)[0]:
            reader_threads[fd] = threading.get_ident()
            loop.call_soon_threadsafe(idle[list(owned).index(fd)].set)
        return original_read(fd, length)

    def add_reader(fd, callback, *args):  # type: ignore[no-untyped-def]
        result = original_add(fd, callback, *args)
        if fd in owned:
            registrations.add(fd)
            if not select.select([fd], [], [], 0)[0]:
                idle[list(owned).index(fd)].set()
        return result

    def remove_reader(fd):  # type: ignore[no-untyped-def]
        registrations.discard(fd)
        return original_remove(fd)

    def queue(*args, **kwargs):  # type: ignore[no-untyped-def]
        value = original_queue(*args, **kwargs)
        if value.maxsize == 16:
            queues.append(value)
        return value

    monkeypatch.setattr(manager, "_spawn_tmux_client", spawn)
    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(loop, "add_reader", add_reader)
    monkeypatch.setattr(loop, "remove_reader", remove_reader)
    monkeypatch.setattr(asyncio, "Queue", queue)
    jobs: list[asyncio.Task[object]] = []
    observed: dict[str, object] = {}
    readers: set[asyncio.Task[object]] = set()
    try:
        for index, socket in enumerate(sockets):
            bridges.append(asyncio.create_task(manager.bridge(socket, workspace, terminal)))
            await asyncio.wait_for(idle[index].wait(), 3)
        counts = [len(s.text) for s in sockets]
        readers = {
            task
            for task in asyncio.all_tasks()
            if task.get_coro().__qualname__.endswith("output_to_browser.<locals>.read_output")
        }
        assert len(readers) == 2
        heartbeat = asyncio.Event()
        loop.call_soon(heartbeat.set)
        jobs = [
            asyncio.create_task(asyncio.to_thread(lambda: "sentinel")),
            asyncio.create_task(asyncio.to_thread(manager.current_pane_mode, workspace, terminal)),
        ]
        await asyncio.wait_for(heartbeat.wait(), 0.5)
        started = time.monotonic()
        _, pending = await asyncio.wait(jobs, timeout=0.3)
        observed = {
            "workers": 2,
            "idle_bridges": 2,
            "heartbeat": heartbeat.is_set(),
            "sentinel": jobs[0].done(),
            "pane_query": jobs[1].done(),
            "no_new_output": counts == [len(s.text) for s in sockets],
            "blocking_reader_threads": len(reader_threads),
            "progress_elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        }
        print("IDLE_PTY_PROGRESS " + json.dumps(observed, sort_keys=True), flush=True)
        if pending:
            return  # cleanup first; the finally assertion retains the actual RED
        assert observed["no_new_output"]
        assert jobs[0].result() == "sentinel"
        canonical = jobs[1].result()
        first, second = "IDLE_PTY_MARKER_ONE", "IDLE_PTY_MARKER_TWO"

        async def emit(marker: str, recipients: list[Socket]) -> None:
            for socket in recipients:
                socket.delivered.clear()
            os.write(output_fd, (marker + "\r\n").encode())
            for socket in recipients:
                while marker not in "".join(socket.text):
                    await asyncio.wait_for(socket.delivered.wait(), 2)
                    socket.delivered.clear()
                assert "".join(socket.text).count(marker) == 1
            assert all(q.empty() for q in queues)

        await emit(first, sockets)
        assert len(registrations) == 2
        await sockets[0].incoming.put(_terminal_disconnect())
        await asyncio.wait_for(bridges[0], 3)
        fd_a = next(iter(owned))
        pid_a, view_a = owned[fd_a]
        assert fd_a not in registrations
        with pytest.raises(OSError):
            os.fstat(fd_a)
        with pytest.raises(ProcessLookupError):
            os.kill(pid_a, 0)
        assert not manager.session_exists(view_a)
        assert not bridges[1].done()
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "after-A"), 0.5) == "after-A"
        await emit(second, [sockets[1]])
        assert "".join(sockets[1].text).index(first) < "".join(sockets[1].text).index(second)
        assert second not in "".join(sockets[0].text)
        assert manager.current_pane_mode(workspace, terminal) == canonical
        observed.update(marker_one_each=1, marker_two_remaining=1, partial_disconnect=True)
        print(
            "IDLE_PTY_DELIVERY "
            + json.dumps(
                {
                    "marker_one_counts": ["".join(s.text).count(first) for s in sockets],
                    "marker_two_counts": ["".join(s.text).count(second) for s in sockets],
                    "queue_sizes": [q.qsize() for q in queues],
                    "canonical": canonical,
                    "controls": [s.controls for s in sockets],
                    "partial_disconnect": True,
                },
                sort_keys=True,
            ),
            flush=True,
        )
    finally:
        # RED must release actual blocked reads without needing executor capacity.
        if not observed.get("sentinel"):
            for pid, _view in owned.values():
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGTERM)
        for socket in sockets:
            socket.incoming.put_nowait(_terminal_disconnect())
        await asyncio.wait_for(asyncio.gather(*bridges, return_exceptions=True), 6)
        await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), 2)
        if previous_executor is None:
            loop._default_executor = None
        else:
            loop.set_default_executor(previous_executor)
        executor.shutdown(wait=True)
        os.close(output_fd)
        for fd, (pid, view) in owned.items():
            assert fd not in registrations
            with pytest.raises(KeyError):
                loop._selector.get_key(fd)
            with pytest.raises(OSError):
                os.fstat(fd)
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            assert not manager.session_exists(view)
        assert manager.session_exists(str(workspace["tmux_session"]))
        assert manager.control.client_count(terminal["id"]) == 0
        assert all(task.done() for task in bridges)
        assert all(task.done() for task in readers)
        assert not any(thread.is_alive() for thread in executor._threads)
        print(
            "IDLE_PTY_CLEANUP "
            + json.dumps(
                {
                    "owned": owned,
                    "registrations": len(registrations),
                    "bridges_done": True,
                    "executor_threads_alive": 0,
                    "canonical_preserved": True,
                    "reader_tasks_done": len(readers),
                    "queue_sizes": [q.qsize() for q in queues],
                },
                sort_keys=True,
            ),
            flush=True,
        )
        _cleanup(app, workspace)
        assert observed.get("sentinel") and observed.get("pane_query"), observed


@pytest.mark.asyncio
async def test_pane_controls_are_ordered_binary_and_do_not_count_as_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, workspace, terminal = _app(tmp_path)
    manager = app.state.terminals
    read_fd, write_fd = os.pipe()
    finished = asyncio.Event()
    trace: list[tuple[str, object]] = []
    touched: list[str] = []
    alternate = True
    main_thread = threading.get_ident()

    def mode(_workspace, _terminal):  # type: ignore[no-untyped-def]
        assert threading.get_ident() != main_thread
        return {
            "session": workspace["tmux_session"],
            "window": "@0",
            "pane": "%0",
            "pane_pid": 123,
            "alternate": alternate,
            "mouse_tracking": False,
        }

    class Socket(_BridgeWebSocket):
        async def receive(self) -> dict[str, object]:
            await finished.wait()
            return _terminal_disconnect()

        async def send_bytes(self, value: bytes) -> None:
            trace.append(("control", json.loads(value)))

        async def send_text(self, value: str) -> None:
            nonlocal alternate
            trace.append(("output", value))
            if value == "first":
                alternate = False
                os.write(write_fd, b"second")
            elif value == "second":
                finished.set()

    monkeypatch.setattr(manager, "current_pane_mode", mode)
    monkeypatch.setattr(manager, "_setup_browser_terminal", lambda *_args: (0, read_fd))
    monkeypatch.setattr(manager, "_release_browser_terminal", lambda *_args: None)

    # Existing release happens before collecting output tasks.
    monkeypatch.setattr(manager, "_release_browser_terminal", lambda *_args: os.close(write_fd))
    monkeypatch.setattr(manager, "_finish_browser_terminal", lambda *_args: os.close(read_fd))
    monkeypatch.setattr(
        "termroom.terminals.touch_terminal_output_if_present",
        lambda _store, ident: touched.append(ident),
    )
    try:
        os.write(write_fd, b"first")
        await asyncio.wait_for(manager.bridge(Socket(), workspace, terminal), timeout=3)
        assert [kind for kind, _value in trace] == ["control", "output", "control", "output"]
        controls = [value for kind, value in trace if kind == "control"]
        assert [value["alternate"] for value in controls] == [True, False]
        assert [value["revision"] for value in controls] == [1, 2]
        assert controls[0]["generation"] == controls[1]["generation"]
        assert touched == [terminal["id"], terminal["id"]]
        assert manager.control.client_count(terminal["id"]) == 0
    finally:
        for fd in (read_fd, write_fd):
            with contextlib.suppress(OSError):
                os.close(fd)
        _cleanup(app, workspace)


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
async def test_terminal_setup_tmux_barrier_does_not_stall_shared_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, workspace, terminal = _app(tmp_path)
    manager = app.state.terminals
    second_terminal = manager.create_terminal(workspace, "second")
    entered, release = threading.Event(), threading.Event()
    original = manager._prepare_browser_view
    original_spawn = manager._spawn_tmux_client
    owned_view = ""
    owned_process: tuple[int, int] | None = None

    def blocked_setup(*args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        nonlocal owned_view
        if str(args[1]["id"]) != str(terminal["id"]):
            original(*args, **kwargs)
            return
        owned_view = args[2]
        assert not manager.session_exists(owned_view)
        assert manager.session_exists(str(workspace["tmux_session"]))
        entered.set()
        if not release.wait(4):
            raise AssertionError("setup barrier was not released")
        original(*args, **kwargs)

    async def second_terminal_progress() -> None:
        await manager.bridge(_BridgeWebSocket(_terminal_disconnect()), workspace, second_terminal)

    def record_spawn(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal owned_process
        owned_process = original_spawn(*args, **kwargs)
        return owned_process

    monkeypatch.setattr(manager, "_prepare_browser_view", blocked_setup)
    monkeypatch.setattr(manager, "_spawn_tmux_client", record_spawn)
    try:
        trace, before_release = await _assert_bridge_barrier_keeps_loop_live(
            app,
            lambda: manager.bridge(_BridgeWebSocket(_terminal_disconnect()), workspace, terminal),
            second_terminal_progress,
            entered,
            release,
        )
        assert before_release == {
            "heartbeat": True,
            "health": True,
            "navigation": True,
            "second_terminal": True,
            "bridge_done": False,
        }, trace
        assert owned_view and owned_process
        assert manager._run_tmux("has-session", "-t", owned_view, check=False).returncode != 0
        with pytest.raises(ProcessLookupError):
            os.kill(owned_process[0], 0)
        with pytest.raises(OSError):
            os.fstat(owned_process[1])
        assert (
            manager._run_tmux(
                "has-session", "-t", str(workspace["tmux_session"]), check=False
            ).returncode
            == 0
        )
    finally:
        _cleanup(app, workspace)


@pytest.mark.asyncio
async def test_terminal_setup_cancellation_cleans_owned_view_process_and_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, workspace, terminal = _app(tmp_path)
    manager = app.state.terminals
    entered, release = threading.Event(), threading.Event()
    original_prepare = manager._prepare_browser_view
    original_spawn = manager._spawn_tmux_client
    view_session = ""
    process: tuple[int, int] | None = None

    def blocked_setup(*args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        nonlocal view_session
        view_session = args[2]
        entered.set()
        if not release.wait(4):
            raise AssertionError("setup cancellation barrier was not released")
        original_prepare(*args, **kwargs)

    def record_spawn(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal process
        process = original_spawn(*args, **kwargs)
        return process

    monkeypatch.setattr(manager, "_prepare_browser_view", blocked_setup)
    monkeypatch.setattr(manager, "_spawn_tmux_client", record_spawn)
    bridge_task = asyncio.create_task(
        manager.bridge(_BridgeWebSocket(_terminal_disconnect()), workspace, terminal)
    )
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        bridge_task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(bridge_task, timeout=6)
        assert view_session and process
        assert manager._run_tmux("has-session", "-t", view_session, check=False).returncode != 0
        with pytest.raises(ProcessLookupError):
            os.kill(process[0], 0)
        with pytest.raises(OSError):
            os.fstat(process[1])
        assert (
            manager._run_tmux(
                "has-session", "-t", str(workspace["tmux_session"]), check=False
            ).returncode
            == 0
        )
    finally:
        release.set()
        if not bridge_task.done():
            bridge_task.cancel()
            await asyncio.gather(bridge_task, return_exceptions=True)
        _cleanup(app, workspace)


@pytest.mark.asyncio
async def test_terminal_pty_readiness_failure_removes_only_owned_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, workspace, terminal = _app(tmp_path)
    manager = app.state.terminals
    original_prepare = manager._prepare_browser_view
    view_session = ""

    def capture_view(*args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        nonlocal view_session
        view_session = args[2]
        original_prepare(*args, **kwargs)

    def readiness_timeout(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("PTY child did not become ready")

    monkeypatch.setattr(manager, "_prepare_browser_view", capture_view)
    monkeypatch.setattr(manager, "_spawn_tmux_client", readiness_timeout)
    try:
        with pytest.raises(TerminalError, match="PTY child did not become ready"):
            await manager.bridge(_BridgeWebSocket(_terminal_disconnect()), workspace, terminal)
        assert view_session
        assert manager._run_tmux("has-session", "-t", view_session, check=False).returncode != 0
        assert (
            manager._run_tmux(
                "has-session", "-t", str(workspace["tmux_session"]), check=False
            ).returncode
            == 0
        )
    finally:
        _cleanup(app, workspace)


@pytest.mark.asyncio
async def test_terminal_tmux_setup_timeout_removes_partial_view_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, workspace, terminal = _app(tmp_path)
    manager = app.state.terminals
    original = manager._run_tmux
    timed_out_view = ""

    def timeout_selection(*args: str, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal timed_out_view
        if args[:1] == ("select-window",):
            assert kwargs.get("timeout") == 5.0
            timed_out_view = args[1].split(":", 1)[0]
            raise TerminalError("tmux command timed out after 5 seconds")
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, "_run_tmux", timeout_selection)
    try:
        with pytest.raises(TerminalError, match="tmux command timed out"):
            await manager.bridge(_BridgeWebSocket(_terminal_disconnect()), workspace, terminal)
        assert timed_out_view
        assert manager._run_tmux("has-session", "-t", timed_out_view, check=False).returncode != 0
        assert (
            manager._run_tmux(
                "has-session", "-t", str(workspace["tmux_session"]), check=False
            ).returncode
            == 0
        )
    finally:
        _cleanup(app, workspace)


@pytest.mark.asyncio
async def test_terminal_cleanup_retries_timed_out_exact_view_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, workspace, terminal = _app(tmp_path)
    manager = app.state.terminals
    disconnected = threading.Event()
    original = manager._run_tmux
    view_session = ""
    timed_out = False

    def timeout_once(*args: str, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal timed_out, view_session
        if (
            disconnected.is_set()
            and len(args) >= 3
            and args[:2] == ("kill-session", "-t")
            and args[2].startswith("termroom-view-")
            and not timed_out
        ):
            timed_out = True
            view_session = args[2]
            raise TerminalError("tmux command timed out after 5 seconds")
        return original(*args, **kwargs)

    class DisconnectWebSocket(_BridgeWebSocket):
        async def receive(self) -> dict[str, object]:
            disconnected.set()
            return await super().receive()

    monkeypatch.setattr(manager, "_run_tmux", timeout_once)
    try:
        await manager.bridge(DisconnectWebSocket(_terminal_disconnect()), workspace, terminal)
        assert timed_out and view_session
        assert manager._run_tmux("has-session", "-t", view_session, check=False).returncode != 0
        assert (
            manager._run_tmux(
                "has-session", "-t", str(workspace["tmux_session"]), check=False
            ).returncode
            == 0
        )
    finally:
        _cleanup(app, workspace)


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
async def test_terminal_cleanup_tmux_barrier_does_not_stall_shared_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, workspace, terminal = _app(tmp_path)
    manager = app.state.terminals
    second_terminal = manager.create_terminal(workspace, "second")
    entered, release = threading.Event(), threading.Event()
    disconnected = threading.Event()
    original = manager._run_tmux
    view_session = ""

    def blocked_tmux(*args: str, **kwargs):  # type: ignore[no-untyped-def]
        if (
            disconnected.is_set()
            and len(args) >= 3
            and args[:2] == ("kill-session", "-t")
            and args[2].startswith("termroom-view-")
        ):
            nonlocal view_session
            if not view_session or args[2] == view_session:
                view_session = args[2]
                entered.set()
                if not release.wait(4):
                    raise AssertionError("cleanup barrier was not released")
        return original(*args, **kwargs)

    class DisconnectWebSocket(_BridgeWebSocket):
        async def receive(self) -> dict[str, object]:
            disconnected.set()
            return await super().receive()

    async def second_terminal_progress() -> None:
        await manager.bridge(_BridgeWebSocket(_terminal_disconnect()), workspace, second_terminal)

    monkeypatch.setattr(manager, "_run_tmux", blocked_tmux)
    try:
        trace, before_release = await _assert_bridge_barrier_keeps_loop_live(
            app,
            lambda: manager.bridge(
                DisconnectWebSocket(_terminal_disconnect()), workspace, terminal
            ),
            second_terminal_progress,
            entered,
            release,
        )
        assert before_release == {
            "heartbeat": True,
            "health": True,
            "navigation": True,
            "second_terminal": True,
            "bridge_done": False,
        }, trace
        assert view_session
        assert manager._run_tmux("has-session", "-t", view_session, check=False).returncode != 0
        assert (
            manager._run_tmux(
                "has-session", "-t", str(workspace["tmux_session"]), check=False
            ).returncode
            == 0
        )
    finally:
        release.set()
        _cleanup(app, workspace)


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
async def test_terminal_blocked_pty_write_does_not_stall_shared_loop_or_duplicate_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, workspace, terminal = _app(tmp_path)
    manager = app.state.terminals
    second_terminal = manager.create_terminal(workspace, "second")
    entered, release = threading.Event(), threading.Event()
    original_write = os.write
    target_fd: int | None = None
    writes: list[bytes] = []
    marker = "TERMROOM_BLOCKED_WRITE_ONCE"
    command = "echo TERMROOM_BLOCKED_''WRITE_ONCE\r"

    def record_write(fd: int, data: bytes) -> int:
        if fd == target_fd:
            writes.append(data)
            entered.set()
            if not release.wait(4):
                raise AssertionError("PTY write barrier was not released")
        return original_write(fd, data)

    original_spawn = manager._spawn_tmux_client

    def record_spawn(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal target_fd
        process_pid, master_fd = original_spawn(*args, **kwargs)
        if target_fd is None:
            target_fd = master_fd
        return process_pid, master_fd

    async def second_terminal_progress() -> None:
        await manager.bridge(_BridgeWebSocket(_terminal_disconnect()), workspace, second_terminal)

    monkeypatch.setattr(manager, "_spawn_tmux_client", record_spawn)
    monkeypatch.setattr(os, "write", record_write)
    try:
        trace, before_release = await _assert_bridge_barrier_keeps_loop_live(
            app,
            lambda: manager.bridge(
                _BridgeWebSocket(
                    {
                        "type": "websocket.receive",
                        "text": json.dumps(
                            {
                                "kind": "input",
                                "data": command,
                                "rows": 24,
                                "cols": 80,
                                "user_input": True,
                            }
                        ),
                    },
                    _terminal_disconnect(),
                ),
                workspace,
                terminal,
            ),
            second_terminal_progress,
            entered,
            release,
        )
        assert before_release == {
            "heartbeat": True,
            "health": True,
            "navigation": True,
            "second_terminal": True,
            "bridge_done": False,
        }, trace
        assert writes == [command.encode()]
        deadline = time.monotonic() + 2
        while (
            marker not in manager.capture_scrollback(workspace, terminal)
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.02)
        scrollback = manager.capture_scrollback(workspace, terminal)
        assert scrollback.count(marker) == 1
        assert writes == [command.encode()]
        assert (
            manager._run_tmux(
                "has-session", "-t", str(workspace["tmux_session"]), check=False
            ).returncode
            == 0
        )
    finally:
        release.set()
        _cleanup(app, workspace)


def test_terminal_websocket_requires_authentication_and_same_origin(tmp_path: Path) -> None:
    app, workspace, terminal = _app(tmp_path)
    try:
        with TestClient(app, base_url="http://testserver") as client:
            with (
                client.websocket_connect(
                    f"/ws/terminal/{terminal['id']}",
                    headers={"origin": "http://testserver"},
                ) as websocket,
                pytest.raises(WebSocketDisconnect) as unauthenticated,
            ):
                _receive_terminal_text(websocket)
            assert unauthenticated.value.code == 4401

            login = client.post("/login", data={"password": "correct-password"})
            assert login.status_code == 200
            with (
                client.websocket_connect(
                    f"/ws/terminal/{terminal['id']}",
                    headers={"origin": "https://evil.example"},
                ) as websocket,
                pytest.raises(WebSocketDisconnect) as wrong_origin,
            ):
                _receive_terminal_text(websocket)
            assert wrong_origin.value.code == 4403

            with client.websocket_connect(
                f"/ws/terminal/{terminal['id']}",
                headers={"origin": "http://testserver"},
            ) as websocket:
                assert isinstance(_receive_terminal_text(websocket), str)
                websocket.send_json({"kind": "resize", "rows": 41, "cols": 123})
                websocket.send_text("[]")
            with client.websocket_connect(
                f"/ws/terminal/{terminal['id']}",
                headers={
                    "host": "termroom.example",
                    "origin": "https://termroom.example",
                },
            ) as websocket:
                assert isinstance(_receive_terminal_text(websocket), str)
                websocket.send_json({"kind": "resize", "rows": 42, "cols": 124})
    finally:
        _cleanup(app, workspace)


def test_terminal_websocket_writes_ascii_and_unicode_input_to_real_tmux(tmp_path: Path) -> None:
    app, workspace, terminal = _app(tmp_path)
    marker = "TERMROOM_INPUT_한글"
    try:
        with TestClient(app, base_url="http://testserver") as client:
            login = client.post("/login", data={"password": "correct-password"})
            assert login.status_code == 200
            with client.websocket_connect(
                f"/ws/terminal/{terminal['id']}",
                headers={"origin": "http://testserver"},
            ) as websocket:
                assert isinstance(_receive_terminal_text(websocket), str)
                websocket.send_json(
                    {
                        "kind": "input",
                        "data": f"printf '%s\\n' '{marker}'\r",
                        "rows": 24,
                        "cols": 80,
                        "user_input": True,
                    }
                )

                deadline = time.monotonic() + 3
                scrollback = ""
                while time.monotonic() < deadline:
                    scrollback = app.state.terminals.capture_scrollback(workspace, terminal)
                    if marker in scrollback:
                        break
                    time.sleep(0.05)

                assert marker in scrollback
    finally:
        _cleanup(app, workspace)


def test_terminal_presence_deduplicates_tabs_in_same_session(tmp_path: Path) -> None:
    app, workspace, terminal = _app(tmp_path)
    terminal_id = str(terminal["id"])
    try:
        with TestClient(app, base_url="http://testserver") as client:
            login = client.post("/login", data={"password": "correct-password"})
            assert login.status_code == 200
            headers = {"origin": "http://testserver"}

            with client.websocket_connect(f"/ws/terminal/{terminal_id}", headers=headers) as first:
                _receive_terminal_text(first)
                assert client.get(f"/api/terminals/{terminal_id}/presence").json()["count"] == 1

                with client.websocket_connect(
                    f"/ws/terminal/{terminal_id}", headers=headers
                ) as second:
                    _receive_terminal_text(second)
                    assert app.state.terminals.control.client_count(terminal_id) == 2
                    assert client.get(f"/api/terminals/{terminal_id}/presence").json()["count"] == 1

                assert client.get(f"/api/terminals/{terminal_id}/presence").json()["count"] == 1
    finally:
        _cleanup(app, workspace)


def test_terminal_binary_input_takes_over_but_raw_text_stays_passive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, workspace, terminal = _app(tmp_path)
    control = app.state.terminals.control
    registered: list[str] = []
    original_register = control.register

    def record_registration(terminal_id: str, *, device_id: str = "") -> str:
        client_id = original_register(terminal_id, device_id=device_id)
        registered.append(client_id)
        return client_id

    monkeypatch.setattr(control, "register", record_registration)
    terminal_id = str(terminal["id"])
    marker = "TERMROOM_RAW_TEXT_PASSIVE"
    try:
        with TestClient(app, base_url="http://testserver") as client:
            client.post("/login", data={"password": "correct-password"})
            headers = {"origin": "http://testserver"}
            with client.websocket_connect(f"/ws/terminal/{terminal_id}", headers=headers) as first:
                _receive_terminal_text(first)
                with client.websocket_connect(
                    f"/ws/terminal/{terminal_id}", headers=headers
                ) as second:
                    _receive_terminal_text(second)
                    assert len(registered) == 2
                    first_id, second_id = registered

                    input_revision = control.presence(terminal_id)["input_revision"]
                    first.send_json(
                        {
                            "kind": "input",
                            "data": "",
                            "rows": 24,
                            "cols": 80,
                            "user_input": True,
                        }
                    )
                    deadline = time.monotonic() + 2
                    while control.presence(terminal_id)["input_revision"] == input_revision:
                        assert time.monotonic() < deadline
                        time.sleep(0.01)
                    assert control.can_resize(terminal_id, first_id)
                    revision = control.presence(terminal_id)["input_revision"]

                    second.send_text(f"printf '%s\\n' '{marker}'\r")
                    deadline = time.monotonic() + 2
                    while marker not in app.state.terminals.capture_scrollback(workspace, terminal):
                        assert time.monotonic() < deadline
                        time.sleep(0.01)
                    assert control.can_resize(terminal_id, first_id)
                    assert not control.can_resize(terminal_id, second_id)
                    assert control.presence(terminal_id)["input_revision"] == revision

                    second.send_bytes(b"")
                    deadline = time.monotonic() + 2
                    while not control.can_resize(terminal_id, second_id):
                        assert time.monotonic() < deadline
                        time.sleep(0.01)
                    assert not control.can_resize(terminal_id, first_id)
                    assert control.presence(terminal_id)["input_revision"] == revision + 1
    finally:
        _cleanup(app, workspace)


def test_terminal_passive_view_resizes_pty_without_controlling_shared_grid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, workspace, terminal = _app(tmp_path)
    applied_sizes: list[tuple[int, int]] = []
    grid_roles: list[bool] = []

    def record_size(_fd: int, *, rows: int, cols: int) -> None:
        applied_sizes.append((rows, cols))

    def record_grid_role(
        _view_session: str, *, enabled: bool, tmux_timeout: float | None = None
    ) -> bool:
        grid_roles.append(enabled)
        return True

    monkeypatch.setattr(app.state.terminals, "_set_window_size", record_size)
    monkeypatch.setattr(app.state.terminals, "_wait_browser_view_size", lambda *a, **kw: True)
    monkeypatch.setattr(
        app.state.terminals,
        "_set_browser_view_grid_resize",
        record_grid_role,
    )
    try:
        with TestClient(app, base_url="http://testserver") as client:
            client.post("/login", data={"password": "correct-password"})
            headers = {"origin": "http://testserver"}
            with client.websocket_connect(
                f"/ws/terminal/{terminal['id']}", headers=headers
            ) as first:
                _receive_terminal_text(first)
                with client.websocket_connect(
                    f"/ws/terminal/{terminal['id']}", headers=headers
                ) as second:
                    _receive_terminal_text(second)
                    second.send_json({"kind": "resize", "rows": 22, "cols": 66})
                    deadline = time.monotonic() + 2
                    while (22, 66) not in applied_sizes and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert (22, 66) in applied_sizes

                    before_same_size_input = list(applied_sizes)
                    second.send_json(
                        {
                            "kind": "input",
                            "data": "",
                            "rows": 22,
                            "cols": 66,
                            "user_input": False,
                        }
                    )
                    time.sleep(0.05)
                    assert applied_sizes == before_same_size_input

                    before_legacy_input = list(applied_sizes)
                    second.send_json({"kind": "input", "data": ""})
                    time.sleep(0.05)
                    assert applied_sizes == before_legacy_input

                    first.send_json({"kind": "resize", "rows": 37, "cols": 111})
                    deadline = time.monotonic() + 2
                    while (37, 111) not in applied_sizes and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert applied_sizes.count((37, 111)) == 1

                    first.send_json(
                        {
                            "kind": "input",
                            "data": "",
                            "rows": 37,
                            "cols": 111,
                            "user_input": True,
                        }
                    )
                    time.sleep(0.05)
                    # Completed bootstrap is passive. Input promotes its role,
                    # but the already-applied identical dimensions are a no-op.
                    assert applied_sizes.count((37, 111)) == 1

                    second.send_json({"kind": "resize", "rows": 30, "cols": 90})
                    deadline = time.monotonic() + 2
                    while (30, 90) not in applied_sizes and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert (30, 90) in applied_sizes
                    second.send_json(
                        {
                            "kind": "input",
                            "data": "",
                            "rows": 30,
                            "cols": 90,
                            "user_input": True,
                        }
                    )
                    deadline = time.monotonic() + 2
                    while applied_sizes.count((30, 90)) < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert applied_sizes.count((30, 90)) == 2
                    # Promotion now demotes the previous tmux peer atomically inside
                    # the True transition, so the passive bridge does not need a
                    # later, separate False transition of its own.
                    assert grid_roles == [True, False, True, True]
    finally:
        _cleanup(app, workspace)


def test_terminal_one_shot_bootstrap_passive_resize_input_and_reconnect(tmp_path: Path) -> None:
    app, workspace, terminal = _app(tmp_path)
    manager = app.state.terminals
    terminal_id = str(terminal["id"])
    ready = tmp_path / "ready"
    effect = tmp_path / "effect"
    command = (
        f"printf ready > {shlex.quote(str(ready))}; "
        f"IFS= read -r -n 1 value; printf '%s' \"$value\" > {shlex.quote(str(effect))}"
    )
    manager._run_tmux(
        "send-keys",
        "-t",
        str(terminal["tmux_window"]),
        "-l",
        f"/bin/bash --noprofile --norc -c {shlex.quote(command)}",
    )
    manager._run_tmux("send-keys", "-t", str(terminal["tmux_window"]), "Enter")

    def wait_for(predicate) -> None:  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("Terminal state did not settle")

    def grid() -> str:
        return manager._run_tmux(
            "display-message",
            "-p",
            "-t",
            terminal["tmux_window"],
            "#{window_width}x#{window_height}",
        ).stdout.strip()

    try:
        wait_for(ready.exists)
        with TestClient(app, base_url="http://testserver") as client:
            client.post("/login", data={"password": "correct-password"})
            headers = {"origin": "http://testserver"}
            with client.websocket_connect(f"/ws/terminal/{terminal_id}", headers=headers) as ws:
                _receive_terminal_text(ws)
                owner = manager.control._clients[terminal_id][0]
                # tmux reserves one status row: viewport 124x23 -> pane 124x22.
                ws.send_json({"kind": "resize", "rows": 23, "cols": 124})
                wait_for(
                    lambda: (
                        grid() == "124x22" and not manager.control.can_resize(terminal_id, owner)
                    )
                )
                assert manager.control.presence(terminal_id)["input_revision"] == 0
                ws.send_json({"kind": "resize", "rows": 18, "cols": 124})
                ws.send_json({"kind": "resize", "rows": 18, "cols": 124})
                time.sleep(0.1)
                assert grid() == "124x22"
                assert (
                    "ignore-size"
                    in manager._run_tmux(
                        "list-clients", "-t", f"termroom-view-{owner}", "-F", "#{client_flags}"
                    ).stdout
                )
                ws.send_json(
                    {"kind": "input", "data": "z", "rows": 18, "cols": 124, "user_input": True}
                )
                wait_for(lambda: effect.exists() and grid() == "124x17")
                assert effect.read_text() == "z"
                assert manager.control.presence(terminal_id)["input_revision"] == 1
                assert manager.control.can_resize(terminal_id, owner)
            with client.websocket_connect(f"/ws/terminal/{terminal_id}", headers=headers) as ws:
                _receive_terminal_text(ws)
                ws.send_json({"kind": "resize", "rows": 30, "cols": 90})
                time.sleep(0.1)
                assert grid() == "124x17"
                assert effect.read_text() == "z"
    finally:
        _cleanup(app, workspace)


@pytest.mark.parametrize("failure", ["enable", "demote"])
def test_terminal_bootstrap_role_failure_retry_or_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    app, workspace, terminal = _app(tmp_path)
    manager = app.state.terminals
    terminal_id = str(terminal["id"])
    original = manager._set_browser_view_grid_resize
    attempts: list[bool] = []

    def role(view: str, *, enabled: bool, tmux_timeout: float | None = None) -> bool:
        attempts.append(enabled)
        if failure == "enable" and len(attempts) == 1:
            return False
        if failure == "demote" and not enabled:
            return False
        return original(view, enabled=enabled, tmux_timeout=tmux_timeout)

    monkeypatch.setattr(manager, "_set_browser_view_grid_resize", role)
    try:
        with TestClient(app, base_url="http://testserver") as client:
            client.post("/login", data={"password": "correct-password"})
            headers = {"origin": "http://testserver"}
            expected_error = (
                pytest.raises(WebSocketDisconnect)
                if failure == "demote"
                else contextlib.nullcontext()
            )
            with (
                expected_error,
                client.websocket_connect(f"/ws/terminal/{terminal_id}", headers=headers) as ws,
            ):
                _receive_terminal_text(ws)
                ws.send_json({"kind": "resize", "rows": 23, "cols": 124})
                deadline = time.monotonic() + 3
                while not attempts and time.monotonic() < deadline:
                    time.sleep(0.01)
                if failure == "enable":
                    assert manager.control.can_resize(
                        terminal_id, manager.control._clients[terminal_id][0]
                    )
                    ws.send_json({"kind": "resize", "rows": 23, "cols": 124})
                deadline = time.monotonic() + 3
                while False not in attempts and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert False in attempts
                if failure == "demote":
                    while True:
                        _receive_terminal_text(ws)
            with client.websocket_connect(f"/ws/terminal/{terminal_id}", headers=headers) as ws:
                _receive_terminal_text(ws)
                assert not manager.control.can_resize(
                    terminal_id, manager.control._clients[terminal_id][0]
                )
                ws.send_json({"kind": "resize", "rows": 18, "cols": 124})
                time.sleep(0.1)
                size = manager._run_tmux(
                    "display-message",
                    "-p",
                    "-t",
                    terminal["tmux_window"],
                    "#{window_width}x#{window_height}",
                ).stdout.strip()
                assert size == "124x22"
    finally:
        _cleanup(app, workspace)


@pytest.mark.parametrize("binary", [False, True])
def test_terminal_input_waits_for_grid_promotion_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, binary: bool
) -> None:
    app, workspace, terminal = _app(tmp_path)
    manager = app.state.terminals
    terminal_id = str(terminal["id"])
    effect = tmp_path / "effect"
    ready = tmp_path / "ready"
    command = "bash -c " + shlex.quote(
        f"printf ready > {shlex.quote(str(ready))}; "
        f'read -r -n1 value; printf %s "$value" > {shlex.quote(str(effect))}'
    )
    manager._run_tmux("send-keys", "-t", terminal["tmux_window"], command, "Enter")
    original_role = manager._set_browser_view_grid_resize
    original_write = os.write
    failed = False
    writes: list[bytes] = []

    def grid() -> str:
        return manager._run_tmux(
            "display-message",
            "-p",
            "-t",
            terminal["tmux_window"],
            "#{window_width}x#{window_height}",
        ).stdout.strip()

    def role(view: str, *, enabled: bool, tmux_timeout: float | None = None) -> bool:
        nonlocal failed
        if enabled and not failed and manager.control.presence(terminal_id)["input_revision"]:
            failed = True
            return False
        return original_role(view, enabled=enabled, tmux_timeout=tmux_timeout)

    def write(fd: int, data: bytes) -> int:
        if data == b"z":
            assert grid() == "124x17"
            writes.append(data)
        return original_write(fd, data)

    monkeypatch.setattr(manager, "_set_browser_view_grid_resize", role)
    monkeypatch.setattr(os, "write", write)
    try:
        with TestClient(app, base_url="http://testserver") as client:
            client.post("/login", data={"password": "correct-password"})
            with client.websocket_connect(
                f"/ws/terminal/{terminal_id}", headers={"origin": "http://testserver"}
            ) as ws:
                _receive_terminal_text(ws)
                ws.send_json({"kind": "resize", "rows": 23, "cols": 124})
                deadline = time.monotonic() + 3
                while (grid() != "124x22" or not ready.exists()) and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert grid() == "124x22" and ready.exists()
                ws.send_json({"kind": "resize", "rows": 18, "cols": 124})
                payload = {
                    "kind": "input",
                    "data": "z",
                    "rows": 18,
                    "cols": 124,
                    "user_input": True,
                }
                if binary:
                    ws.send_bytes(b"z")
                else:
                    ws.send_json(payload)
                deadline = time.monotonic() + 3
                while not failed and time.monotonic() < deadline:
                    time.sleep(0.01)
                time.sleep(0.1)
                assert failed and not effect.exists() and not writes
                assert grid() == "124x22"
                assert manager.control.presence(terminal_id)["input_revision"] == 1
                if binary:
                    ws.send_bytes(b"z")
                else:
                    ws.send_json(payload)
                deadline = time.monotonic() + 3
                while not effect.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert effect.read_text() == "z" and writes == [b"z"]
                assert manager.control.presence(terminal_id)["input_revision"] == 2
                ws.send_json({"kind": "resize", "rows": 18, "cols": 124})
                time.sleep(0.1)
                assert grid() == "124x17" and writes == [b"z"]
    finally:
        _cleanup(app, workspace)


def test_terminal_websocket_closes_when_signed_session_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("termroom.auth.SESSION_MAX_AGE_SECONDS", 1)
    issued_at = 1_800_000_000
    monkeypatch.setattr("termroom.auth._session_now", lambda: issued_at)
    app, workspace, terminal = _app(tmp_path)
    try:
        with TestClient(app, base_url="http://testserver") as client:
            client.post("/login", data={"password": "correct-password"})
            monkeypatch.setattr("termroom.auth._session_now", lambda: issued_at + 1)
            with client.websocket_connect(
                f"/ws/terminal/{terminal['id']}",
                headers={"origin": "http://testserver"},
            ) as websocket:
                closed = False
                for _ in range(20):
                    try:
                        _receive_terminal_text(websocket)
                    except WebSocketDisconnect as exc:
                        assert exc.code == 4401
                        closed = True
                        break
                assert closed
    finally:
        _cleanup(app, workspace)
