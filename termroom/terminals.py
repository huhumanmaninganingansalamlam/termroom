from __future__ import annotations

import asyncio
import codecs
import contextlib
import fcntl
import json
import os
import signal
import struct
import subprocess
import termios
import time
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from termroom.db import StateStore
from termroom.pty_process import spawn_pty_process
from termroom.terminal_control import TerminalControl


class TerminalError(RuntimeError):
    pass


MAX_TERMINAL_MESSAGE_BYTES = 1024 * 1024
MIN_TERMINAL_ROWS = 4
MAX_TERMINAL_ROWS = 500
MIN_TERMINAL_COLS = 20
MAX_TERMINAL_COLS = 1000


class TerminalOutputDecoder:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def feed(self, chunk: bytes, *, final: bool = False) -> str:
        return self._decoder.decode(chunk, final=final)


def normalize_terminal_name(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in value.strip()
    )
    cleaned = cleaned.strip("-")[:32]
    return cleaned or "shell"


def terminal_size(payload: dict[str, Any]) -> tuple[int, int] | None:
    try:
        rows = int(payload.get("rows", 24))
        cols = int(payload.get("cols", 80))
    except (TypeError, ValueError):
        return None
    return (
        max(MIN_TERMINAL_ROWS, min(rows, MAX_TERMINAL_ROWS)),
        max(MIN_TERMINAL_COLS, min(cols, MAX_TERMINAL_COLS)),
    )


class TerminalManager:
    def __init__(self, store: StateStore, control: TerminalControl | None = None) -> None:
        self.store = store
        self.control = control or TerminalControl()

    @staticmethod
    def _run_tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("TERMROOM_PASSWORD", None)
        return subprocess.run(
            ["tmux", *args],
            check=check,
            capture_output=True,
            text=True,
            env=environment,
        )

    def session_exists(self, session_name: str) -> bool:
        result = self._run_tmux("has-session", "-t", session_name, check=False)
        return result.returncode == 0

    def existing_sessions(self) -> set[str]:
        result = self._run_tmux(
            "list-sessions",
            "-F",
            "#{session_name}",
            check=False,
        )
        if result.returncode != 0:
            return set()
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}

    def ensure_workspace(self, workspace: dict[str, Any]) -> list[dict[str, Any]]:
        session = workspace["tmux_session"]
        workspace_path = Path(workspace["path"])
        # A Core login secret must never become shell environment. If Termroom
        # is using an already-running tmux server, remove it before a new pane
        # is created.
        self._run_tmux(
            "set-environment", "-g", "-u", "TERMROOM_PASSWORD", check=False
        )
        if not self.session_exists(session):
            self._run_tmux(
                "new-session",
                "-d",
                "-s",
                session,
                "-c",
                str(workspace_path),
                "-n",
                "shell",
            )
            self.store.reset_terminals(workspace["id"])

        # The most recently resized browser client should control the tmux
        # window dimensions. Without this, a detached 80x24 session can keep
        # the visible pane artificially small on a wide browser viewport.
        self._run_tmux(
            "set-window-option",
            "-t",
            session,
            "window-size",
            "latest",
            check=False,
        )

        tmux_windows = self._list_tmux_windows(session)
        stored = self.store.list_terminals(workspace["id"])
        known_windows = {terminal["tmux_window"] for terminal in stored}
        live_windows = {window_id for window_id, _ in tmux_windows}

        if known_windows != live_windows:
            self.store.reset_terminals(workspace["id"])
            for window_id, name in tmux_windows:
                self.store.create_terminal(workspace["id"], name, window_id)
        return self.store.list_terminals(workspace["id"])

    def _list_tmux_windows(self, session_name: str) -> list[tuple[str, str]]:
        result = self._run_tmux(
            "list-windows",
            "-t",
            session_name,
            "-F",
            "#{window_id}\t#{window_name}",
        )
        windows: list[tuple[str, str]] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            window_id, _, name = line.partition("\t")
            windows.append((window_id, name or "shell"))
        return windows

    def create_terminal(self, workspace: dict[str, Any], name: str = "shell") -> dict[str, Any]:
        self.ensure_workspace(workspace)
        safe_name = normalize_terminal_name(name)
        result = self._run_tmux(
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{window_id}",
            "-t",
            workspace["tmux_session"],
            "-n",
            safe_name,
            "-c",
            str(workspace["path"]),
        )
        return self.store.create_terminal(workspace["id"], safe_name, result.stdout.strip())

    def rename_terminal(
        self, workspace: dict[str, Any], terminal: dict[str, Any], name: str
    ) -> dict[str, Any]:
        self.ensure_workspace(workspace)
        safe_name = normalize_terminal_name(name)
        result = self._run_tmux(
            "rename-window",
            "-t",
            str(terminal["tmux_window"]),
            safe_name,
            check=False,
        )
        if result.returncode:
            raise TerminalError(result.stderr.strip() or "Terminal rename failed")
        self.store.rename_terminal(str(terminal["id"]), safe_name)
        updated = self.store.get_terminal(str(terminal["id"]))
        if not updated:
            raise TerminalError("Terminal disappeared while renaming")
        return updated

    def close_terminal(
        self, workspace: dict[str, Any], terminal: dict[str, Any]
    ) -> list[dict[str, Any]]:
        self.ensure_workspace(workspace)
        result = self._run_tmux(
            "kill-window",
            "-t",
            str(terminal["tmux_window"]),
            check=False,
        )
        if result.returncode:
            raise TerminalError(result.stderr.strip() or "Terminal close failed")
        self.store.delete_terminal(str(terminal["id"]))
        return self.ensure_workspace(workspace)

    def capture_scrollback(
        self, workspace: dict[str, Any], terminal: dict[str, Any], lines: int = 2000
    ) -> str:
        self.ensure_workspace(workspace)
        result = self._run_tmux(
            "capture-pane",
            "-p",
            "-J",
            "-S",
            f"-{max(100, min(lines, 10000))}",
            "-t",
            terminal["tmux_window"],
        )
        return result.stdout

    async def bridge(
        self,
        websocket: WebSocket,
        workspace: dict[str, Any],
        terminal: dict[str, Any],
        *,
        device_id: str = "",
    ) -> None:
        self.ensure_workspace(workspace)
        self.store.touch_terminal(terminal["id"])
        self._run_tmux("select-window", "-t", terminal["tmux_window"])
        terminal_id = str(terminal["id"])
        client_id = self.control.register(terminal_id)

        # The helper process creates a real controlling terminal after a
        # posix_spawn. This preserves resize semantics without calling forkpty
        # from the multi-threaded web server.
        process_pid, master_fd = self._spawn_tmux_client(workspace)

        async def output_to_browser() -> None:
            decoder = TerminalOutputDecoder()
            while True:
                try:
                    chunk = await asyncio.to_thread(os.read, master_fd, 65536)
                except OSError:
                    tail = decoder.feed(b"", final=True)
                    if tail:
                        await websocket.send_text(tail)
                    return
                if not chunk:
                    tail = decoder.feed(b"", final=True)
                    if tail:
                        await websocket.send_text(tail)
                    return
                decoded = decoder.feed(chunk)
                await asyncio.to_thread(self.store.touch_terminal_output, terminal["id"])
                if decoded:
                    await websocket.send_text(decoded)

        async def browser_to_input() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect(message.get("code", 1000))
                if message.get("bytes") is not None:
                    payload_bytes = message["bytes"]
                    if len(payload_bytes) > MAX_TERMINAL_MESSAGE_BYTES:
                        await websocket.close(code=1009, reason="Terminal input is too large")
                        return
                    self.control.mark_input(terminal_id, client_id, device_id)
                    os.write(master_fd, payload_bytes)
                    continue
                raw = message.get("text") or ""
                if len(raw.encode("utf-8")) > MAX_TERMINAL_MESSAGE_BYTES:
                    await websocket.close(code=1009, reason="Terminal input is too large")
                    return
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    self.control.mark_input(terminal_id, client_id, device_id)
                    os.write(master_fd, raw.encode())
                    continue

                if not isinstance(payload, dict):
                    continue
                kind = payload.get("kind")
                if kind == "claim":
                    self.control.claim_view(terminal_id, client_id)
                elif kind == "resize":
                    if not self.control.can_resize(terminal_id, client_id):
                        continue
                    size = terminal_size(payload)
                    if size is None:
                        continue
                    rows, cols = size
                    self._set_window_size(master_fd, rows=rows, cols=cols)
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process_pid, signal.SIGWINCH)
                elif kind == "command":
                    self.control.mark_input(terminal_id, client_id, device_id)
                    command = str(payload.get("data", ""))
                    await asyncio.to_thread(
                        self.store.add_command, workspace["id"], terminal["id"], command
                    )
                    os.write(master_fd, command.encode() + b"\r")
                elif kind == "input":
                    self.control.mark_input(terminal_id, client_id, device_id)
                    os.write(master_fd, str(payload.get("data", "")).encode())

        output_task = asyncio.create_task(output_to_browser())
        input_task = asyncio.create_task(browser_to_input())
        try:
            done, pending = await asyncio.wait(
                {output_task, input_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:
                with contextlib.suppress(WebSocketDisconnect, asyncio.CancelledError):
                    await task
        finally:
            self.control.unregister(terminal_id, client_id)
            for task in (output_task, input_task):
                if not task.done():
                    task.cancel()
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process_pid, signal.SIGTERM)
            exited = await asyncio.to_thread(self._wait_for_pid, process_pid, 1.0)
            if not exited:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process_pid, signal.SIGKILL)
                await asyncio.to_thread(self._wait_for_pid, process_pid, 1.0)
            with contextlib.suppress(OSError):
                os.close(master_fd)

    def _spawn_tmux_client(self, workspace: dict[str, Any]) -> tuple[int, int]:
        environment = os.environ.copy()
        environment.pop("TMUX", None)
        environment.pop("TERMROOM_PASSWORD", None)
        environment["TERM"] = "xterm-256color"
        process_pid, master_fd = spawn_pty_process(
            ["tmux", "attach-session", "-t", workspace["tmux_session"]],
            cwd=str(workspace["path"]),
            environment=environment,
        )
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process_pid, signal.SIGWINCH)
        return process_pid, master_fd

    @staticmethod
    def _wait_for_pid(process_pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                finished_pid, _ = os.waitpid(process_pid, os.WNOHANG)
            except ChildProcessError:
                return True
            if finished_pid == process_pid:
                return True
            time.sleep(0.02)
        return False

    @staticmethod
    def _set_window_size(fd: int, *, rows: int, cols: int) -> None:
        safe_rows = max(4, min(rows, 300))
        safe_cols = max(20, min(cols, 500))
        winsize = struct.pack("HHHH", safe_rows, safe_cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
