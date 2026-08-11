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
import uuid
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from termroom.db import StateStore
from termroom.pty_process import spawn_pty_process
from termroom.terminal_control import TerminalControl
from termroom.workspace_usage import (
    RawWorkspaceUsage,
    WorkspaceUsageStale,
    WorkspaceUsageUnavailable,
    read_system_process_output,
    workspace_usage_from_outputs,
)


class TerminalError(RuntimeError):
    pass


MAX_TERMINAL_MESSAGE_BYTES = 1024 * 1024
MIN_TERMINAL_ROWS = 4
MAX_TERMINAL_ROWS = 500
MIN_TERMINAL_COLS = 20
MAX_TERMINAL_COLS = 1000
TMUX_TERMINAL_ROLE_OPTION = "@termroom_terminal_role"
TMUX_MANAGED_RUN_OPTION = "@termroom_managed_run_id"
TMUX_TERMINAL_RECORD_FORMAT = (
    "#{window_id}|#{window_name}|"
    f"#{{{TMUX_TERMINAL_ROLE_OPTION}}}|#{{{TMUX_MANAGED_RUN_OPTION}}}"
)


def parse_tmux_terminal_records(output: str) -> list[dict[str, str | None]]:
    """Decode the printable tmux record format shared by Local and SSH."""

    records: list[dict[str, str | None]] = []
    for line in output.splitlines():
        if not line:
            continue
        window_id, separator, remainder = line.partition("|")
        identity = remainder.rsplit("|", 2)
        if (
            not separator
            or not window_id.startswith("@")
            or len(identity) != 3
        ):
            raise ValueError("tmux exposed an invalid Terminal record")
        name, role, managed_run_id = identity
        records.append(
            {
                "tmux_window": window_id,
                "name": name or "shell",
                "role": role or "shell",
                "managed_run_id": managed_run_id or None,
            }
        )
    return records


FILE_RUN_WRAPPER_SCRIPT = r"""#!/bin/sh
set -u
umask 077

meta_dir=$1
run_id=$2
runner_id=$3
missing_code=$4
shift 4

atomic_record() {
    destination=$1
    temporary="${destination}.tmp.$$"
    cat > "$temporary" || return 1
    chmod 0600 "$temporary" || return 1
    mv -f -- "$temporary" "$destination"
}

utc_now() {
    date -u '+%Y-%m-%dT%H:%M:%SZ'
}

prepare_failed() {
    code=$1
    ended_at=$(utc_now)
    printf '{"run_id":"%s","state":"failed","error_code":"%s","ended_at":"%s"}\n' \
        "$run_id" "$code" "$ended_at" | atomic_record "$meta_dir/prepare.json"
    exit 127
}

test -d "$meta_dir" || exit 120
test "$(cat -- "$meta_dir/request-id")" = "$run_id" || exit 120
test "$#" -gt 0 || prepare_failed runner_metadata_invalid

program=$1
if test "$runner_id" = direct; then
    IFS= read -r shebang < "$program" || prepare_failed "$missing_code"
    case "$shebang" in
        '#!'*) interpreter=${shebang#\#!} ;;
        *) prepare_failed "$missing_code" ;;
    esac
    leading_space=${interpreter%%[![:space:]]*}
    interpreter=${interpreter#"$leading_space"}
    interpreter=${interpreter%%[[:space:]]*}
    test -n "$interpreter" && test -x "$interpreter" \
        || prepare_failed "$missing_code"
elif ! command -v "$program" >/dev/null 2>&1; then
    prepare_failed "$missing_code"
fi

started_at=$(utc_now)
printf '{"run_id":"%s","state":"running","started_at":"%s"}\n' \
    "$run_id" "$started_at" | atomic_record "$meta_dir/state.json" || exit 120

status=0
stop_signal=
trap 'stop_signal=INT' INT
trap 'stop_signal=TERM' TERM
trap 'stop_signal=HUP' HUP
"$@" || status=$?
trap - INT TERM HUP
stop_requested=false
stop_signal_json=null
if test -n "$stop_signal" && test -f "$meta_dir/stop-requested-at"; then
    stop_requested=true
    stop_signal_json="\"$stop_signal\""
fi
ended_at=$(utc_now)
printf '{"run_id":"%s","exit_code":%s,"stop_requested":%s,'\
'"stop_signal":%s,"started_at":"%s","ended_at":"%s"}\n' \
    "$run_id" "$status" "$stop_requested" "$stop_signal_json" "$started_at" "$ended_at" \
    | atomic_record "$meta_dir/completion.json"
exit "$status"
"""


def file_run_completion_was_stopped(record: dict[str, Any]) -> bool:
    return bool(record.get("stop_requested")) and record.get("stop_signal") in {
        "INT",
        "TERM",
        "HUP",
    }


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

    def workspace_usage(self, workspace: dict[str, Any]) -> RawWorkspaceUsage:
        try:
            result = self._run_tmux(
                "list-panes",
                "-s",
                "-t",
                str(workspace["tmux_session"]),
                "-F",
                "#{pane_pid}",
                check=False,
            )
        except OSError as exc:
            raise WorkspaceUsageUnavailable(
                "tmux is not available", code="pane_tool_missing"
            ) from exc
        if result.returncode:
            raise WorkspaceUsageStale("Workspace tmux session is not available")
        return workspace_usage_from_outputs(result.stdout, read_system_process_output())

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

        return self.store.reconcile_terminals(
            str(workspace["id"]), self._list_tmux_window_records(session)
        )

    def _list_tmux_window_records(self, session_name: str) -> list[dict[str, str | None]]:
        result = self._run_tmux(
            "list-windows",
            "-t",
            session_name,
            "-F",
            TMUX_TERMINAL_RECORD_FORMAT,
        )
        return parse_tmux_terminal_records(result.stdout)

    def _list_tmux_windows(self, session_name: str) -> list[tuple[str, str]]:
        return [
            (str(item["tmux_window"]), str(item["name"]))
            for item in self._list_tmux_window_records(session_name)
        ]

    def set_managed_identity(
        self,
        workspace: dict[str, Any],
        tmux_window: str,
        *,
        role: str,
        managed_run_id: str,
    ) -> dict[str, Any]:
        if role not in {"file_run", "remote_run"} or not managed_run_id:
            raise TerminalError("Managed Terminal identity is invalid")
        self._run_tmux(
            "set-window-option",
            "-t",
            tmux_window,
            TMUX_TERMINAL_ROLE_OPTION,
            role,
        )
        self._run_tmux(
            "set-window-option",
            "-t",
            tmux_window,
            TMUX_MANAGED_RUN_OPTION,
            managed_run_id,
        )
        terminals = self.ensure_workspace(workspace)
        terminal = next(
            (item for item in terminals if item["tmux_window"] == tmux_window), None
        )
        if terminal is None:
            raise TerminalError("Managed Terminal disappeared while recording identity")
        return terminal

    @staticmethod
    def _write_file_run_metadata(
        metadata_dir: Path,
        *,
        run_id: str,
    ) -> Path:
        metadata_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata_dir.chmod(0o700)
        request_id = metadata_dir / "request-id"
        if request_id.exists() and request_id.read_text(encoding="utf-8").strip() != run_id:
            raise TerminalError("File Run metadata identity does not match")
        request_id.write_text(run_id + "\n", encoding="utf-8")
        request_id.chmod(0o600)
        wrapper = metadata_dir / "runner.sh"
        temporary = metadata_dir / f".runner-{uuid.uuid4().hex}.tmp"
        temporary.write_text(FILE_RUN_WRAPPER_SCRIPT, encoding="utf-8")
        temporary.chmod(0o700)
        os.replace(temporary, wrapper)
        return wrapper

    def _file_run_pane(self, tmux_window: str) -> dict[str, Any] | None:
        result = self._run_tmux(
            "list-panes",
            "-t",
            tmux_window,
            "-F",
            "#{pane_id}\t#{pane_dead}\t#{pane_dead_status}\t#{pane_pid}\t"
            "#{pane_dead_time}",
            check=False,
        )
        if result.returncode != 0:
            return None
        line = next((value for value in result.stdout.splitlines() if value.strip()), "")
        if not line:
            return None
        parts = line.split("\t", 4)
        parts.extend([""] * (5 - len(parts)))
        pane_id, dead, dead_status, pane_pid, dead_time = parts
        return {
            "pane_id": pane_id,
            "dead": dead == "1",
            "exit_code": int(dead_status) if dead_status.lstrip("-").isdigit() else None,
            "pane_pid": int(pane_pid) if pane_pid.isdigit() else None,
            "dead_at": int(dead_time) if dead_time.isdigit() else None,
        }

    def _rollback_file_run_slot(
        self,
        workspace: dict[str, Any],
        tmux_window: str,
        *,
        run_id: str,
        created: bool,
        previous_role: str,
        previous_run_id: str | None,
    ) -> None:
        with contextlib.suppress(OSError, subprocess.SubprocessError, ValueError):
            current = next(
                (
                    item
                    for item in self._list_tmux_window_records(
                        str(workspace["tmux_session"])
                    )
                    if item["tmux_window"] == tmux_window
                ),
                None,
            )
            if current is None or current.get("role") != "file_run":
                return
            current_run_id = current.get("managed_run_id")
            if current_run_id not in {run_id, previous_run_id}:
                return
            if created:
                result = self._run_tmux(
                    "kill-window", "-t", tmux_window, check=False
                )
                if result.returncode == 0:
                    self.ensure_workspace(workspace)
                    return
            if previous_role == "shell":
                self._run_tmux(
                    "set-window-option",
                    "-u",
                    "-t",
                    tmux_window,
                    TMUX_TERMINAL_ROLE_OPTION,
                    check=False,
                )
                self._run_tmux(
                    "set-window-option",
                    "-u",
                    "-t",
                    tmux_window,
                    TMUX_MANAGED_RUN_OPTION,
                    check=False,
                )
            else:
                self._run_tmux(
                    "set-window-option",
                    "-t",
                    tmux_window,
                    TMUX_TERMINAL_ROLE_OPTION,
                    previous_role,
                    check=False,
                )
                if previous_run_id:
                    self._run_tmux(
                        "set-window-option",
                        "-t",
                        tmux_window,
                        TMUX_MANAGED_RUN_OPTION,
                        previous_run_id,
                        check=False,
                    )
                else:
                    self._run_tmux(
                        "set-window-option",
                        "-u",
                        "-t",
                        tmux_window,
                        TMUX_MANAGED_RUN_OPTION,
                        check=False,
                    )
            self.ensure_workspace(workspace)

    @staticmethod
    def _read_file_run_record(path: Path, run_id: str) -> dict[str, Any] | None:
        try:
            if path.is_symlink() or path.stat().st_size > 16 * 1024:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or value.get("run_id") != run_id:
            return None
        return value

    def start_file_run(
        self,
        workspace: dict[str, Any],
        *,
        run_id: str,
        runner_id: str,
        runtime_error_code: str,
        argv: tuple[str, ...],
        metadata_dir: Path,
    ) -> dict[str, Any]:
        if not argv:
            raise TerminalError("File Run argv is empty")
        wrapper = self._write_file_run_metadata(metadata_dir, run_id=run_id)
        terminals = self.ensure_workspace(workspace)
        terminal = next(
            (item for item in terminals if item.get("role") == "file_run"), None
        )
        created = terminal is None
        if terminal is None:
            terminal = self.create_terminal(workspace, "Run")
        else:
            pane = self._file_run_pane(str(terminal["tmux_window"]))
            if pane is not None and not pane["dead"]:
                raise TerminalError("The managed File Run Terminal is still active")

        tmux_window = str(terminal["tmux_window"])
        previous_role = str(terminal.get("role") or "shell")
        previous_run_id = str(terminal.get("managed_run_id") or "") or None
        try:
            self._run_tmux(
                "set-window-option", "-t", tmux_window, "remain-on-exit", "on"
            )
            terminal = self.set_managed_identity(
                workspace,
                tmux_window,
                role="file_run",
                managed_run_id=run_id,
            )
            command = (
                "/bin/sh",
                str(wrapper),
                str(metadata_dir),
                run_id,
                runner_id,
                runtime_error_code,
                *argv,
            )
            result = self._run_tmux(
                "respawn-pane",
                "-k",
                "-c",
                str(workspace["path"]),
                "-t",
                tmux_window,
                *command,
                check=False,
            )
            if result.returncode:
                raise TerminalError(
                    result.stderr.strip() or "File Run could not start"
                )
            return terminal
        except TerminalError:
            self._rollback_file_run_slot(
                workspace,
                tmux_window,
                run_id=run_id,
                created=created,
                previous_role=previous_role,
                previous_run_id=previous_run_id,
            )
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            self._rollback_file_run_slot(
                workspace,
                tmux_window,
                run_id=run_id,
                created=created,
                previous_role=previous_role,
                previous_run_id=previous_run_id,
            )
            raise TerminalError("File Run Terminal could not be prepared") from exc

    def inspect_file_run(
        self,
        workspace: dict[str, Any],
        *,
        run_id: str,
        metadata_dir: Path,
    ) -> dict[str, Any]:
        completion = self._read_file_run_record(metadata_dir / "completion.json", run_id)
        if completion is not None and isinstance(completion.get("exit_code"), int):
            return {
                "state": "stopped"
                if file_run_completion_was_stopped(completion)
                else "finished",
                "started_at": completion.get("started_at"),
                "ended_at": completion.get("ended_at"),
                "exit_code": int(completion["exit_code"]),
            }
        prepare = self._read_file_run_record(metadata_dir / "prepare.json", run_id)
        if prepare is not None and prepare.get("state") == "failed":
            return {
                "state": "failed",
                "ended_at": prepare.get("ended_at"),
                "error_code": prepare.get("error_code"),
            }

        if not self.session_exists(str(workspace["tmux_session"])):
            return {"state": "lost", "error_code": "managed_terminal_missing"}
        windows = self._list_tmux_window_records(str(workspace["tmux_session"]))
        self.store.reconcile_terminals(str(workspace["id"]), windows)
        slot = next((item for item in windows if item["role"] == "file_run"), None)
        if slot is None or slot.get("managed_run_id") != run_id:
            return {"state": "lost", "error_code": "managed_terminal_missing"}
        pane = self._file_run_pane(str(slot["tmux_window"]))
        state = self._read_file_run_record(metadata_dir / "state.json", run_id)
        if pane is not None and not pane["dead"]:
            return {
                "state": "running" if state and state.get("state") == "running" else "preparing",
                "started_at": state.get("started_at") if state else None,
            }
        if (metadata_dir / "force-stopped").is_file():
            return {
                "state": "stopped",
                "started_at": state.get("started_at") if state else None,
                "ended_at": None,
                "exit_code": pane.get("exit_code") if pane else None,
                "error_code": "forced",
            }
        dead_at = pane.get("dead_at") if pane else None
        if isinstance(dead_at, int) and time.time() - dead_at < 2:
            return {
                "state": "running" if state else "preparing",
                "started_at": state.get("started_at") if state else None,
            }
        return {
            "state": "lost",
            "started_at": state.get("started_at") if state else None,
            "error_code": "completion_missing",
        }

    def interrupt_file_run(
        self,
        workspace: dict[str, Any],
        *,
        run_id: str,
        metadata_dir: Path,
    ) -> bool:
        terminals = self.ensure_workspace(workspace)
        terminal = next(
            (
                item
                for item in terminals
                if item.get("role") == "file_run"
                and item.get("managed_run_id") == run_id
            ),
            None,
        )
        if terminal is None:
            return False
        pane = self._file_run_pane(str(terminal["tmux_window"]))
        if pane is None or pane["dead"]:
            return False
        (metadata_dir / "stop-requested-at").write_text(
            str(time.time()) + "\n", encoding="utf-8"
        )
        result = self._run_tmux(
            "send-keys",
            "-t",
            str(terminal["tmux_window"]),
            "C-c",
            check=False,
        )
        return result.returncode == 0

    def kill_file_run(
        self,
        workspace: dict[str, Any],
        *,
        run_id: str,
        metadata_dir: Path,
    ) -> bool:
        terminals = self.ensure_workspace(workspace)
        terminal = next(
            (
                item
                for item in terminals
                if item.get("role") == "file_run"
                and item.get("managed_run_id") == run_id
            ),
            None,
        )
        if terminal is None:
            return False
        pane = self._file_run_pane(str(terminal["tmux_window"]))
        if pane is None or pane["dead"]:
            return False
        pane_pid = pane.get("pane_pid")
        if not isinstance(pane_pid, int):
            raise TerminalError("Managed File Run process identity is unavailable")
        (metadata_dir / "stop-requested-at").write_text(
            str(time.time()) + "\n", encoding="utf-8"
        )
        try:
            os.killpg(pane_pid, signal.SIGKILL)
        except ProcessLookupError:
            return False
        (metadata_dir / "force-stopped").write_text(
            str(time.time()) + "\n", encoding="utf-8"
        )
        return True

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
        if str(terminal.get("role") or "shell") != "shell":
            raise TerminalError("Managed Terminals cannot be renamed")
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
        if str(terminal.get("role") or "shell") != "shell":
            raise TerminalError("Managed Terminals cannot be closed")
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
