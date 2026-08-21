from __future__ import annotations

import asyncio
import codecs
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from termroom.db import MAX_WORKSPACE_COMMANDS, StateStore, normalize_workspace_commands
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
TMUX_BROWSER_VIEW_PREFIX = "termroom-view-"
TMUX_WORKSPACE_COMMAND_SLOT_OPTION = "@termroom_workspace_command_slot"
TMUX_WORKSPACE_COMMAND_LAUNCH_OPTION = "@termroom_workspace_command_launch"
TMUX_WORKSPACE_COMMAND_DIGEST_OPTION = "@termroom_workspace_command_digest"
TMUX_TERMINAL_EDITOR_DIGEST_OPTION = "@termroom_terminal_editor_digest"
WORKSPACE_COMMAND_READY_TIMEOUT_SECONDS = 2.0
WORKSPACE_COMMAND_READY_POLL_SECONDS = 0.01
TMUX_WORKSPACE_COMMAND_RECORD_FORMAT = (
    "#{window_id}|#{pane_dead}|"
    f"#{{{TMUX_WORKSPACE_COMMAND_SLOT_OPTION}}}|"
    f"#{{{TMUX_WORKSPACE_COMMAND_LAUNCH_OPTION}}}|"
    f"#{{{TMUX_WORKSPACE_COMMAND_DIGEST_OPTION}}}"
)
TMUX_TERMINAL_EDITOR_RECORD_FORMAT = (
    "#{window_id}|#{pane_dead}|"
    f"#{{{TMUX_TERMINAL_EDITOR_DIGEST_OPTION}}}"
)
TMUX_TERMINAL_RECORD_FORMAT = (
    "#{session_name}|#{window_id}|#{window_activity}|#{window_name}|"
    f"#{{{TMUX_TERMINAL_ROLE_OPTION}}}|#{{{TMUX_MANAGED_RUN_OPTION}}}"
)

WORKSPACE_COMMAND_WRAPPER = r"""/bin/bash --noprofile --norc -c '
set -eu
pane=${TMUX_PANE:?}
command=${TERMROOM_WORKSPACE_COMMAND:?}
slot=${TERMROOM_WORKSPACE_COMMAND_SLOT:?}
launch=${TERMROOM_WORKSPACE_COMMAND_LAUNCH:?}
digest=${TERMROOM_WORKSPACE_COMMAND_DIGEST:?}
tmux set-window-option -t "$pane" remain-on-exit on
tmux set-window-option -t "$pane" @termroom_workspace_command_digest "$digest"
tmux set-window-option -t "$pane" @termroom_workspace_command_launch "$launch"
tmux set-window-option -t "$pane" @termroom_workspace_command_slot "$slot"
unset TERMROOM_WORKSPACE_COMMAND TERMROOM_WORKSPACE_COMMAND_SLOT \
    TERMROOM_WORKSPACE_COMMAND_LAUNCH TERMROOM_WORKSPACE_COMMAND_DIGEST
eval -- "$command"
'
"""

TERMINAL_EDITOR_WRAPPER = r"""/bin/sh -c '
set -eu
pane=${TMUX_PANE:?}
file=${TERMROOM_TERMINAL_EDITOR_FILE:?}
digest=${TERMROOM_TERMINAL_EDITOR_DIGEST:?}
tmux set-window-option -t "$pane" @termroom_terminal_editor_digest "$digest"
unset TERMROOM_TERMINAL_EDITOR_FILE TERMROOM_TERMINAL_EDITOR_DIGEST
for editor in nvim vim vi; do
    if command -v "$editor" >/dev/null 2>&1; then
        exec "$editor" "$file"
    fi
done
printf "Termroom: install Neovim or Vim to edit this file.\n" >&2
exit 127
'
"""


def normalize_terminal_editor_path(value: object) -> str:
    raw = str(value)
    path = PurePosixPath(raw)
    if (
        not raw
        or "\x00" in raw
        or path.is_absolute()
        or path == PurePosixPath(".")
        or any(part == ".." for part in path.parts)
    ):
        raise ValueError("Terminal editor file path is invalid")
    return path.as_posix()


def terminal_editor_digest(relative_path: object) -> str:
    normalized = normalize_terminal_editor_path(relative_path)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def parse_tmux_terminal_editor_records(output: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in output.splitlines():
        if not line:
            continue
        parts = line.split("|", 2)
        if (
            len(parts) != 3
            or not parts[0].startswith("@")
            or not parts[0][1:].isdigit()
            or parts[1] not in {"0", "1"}
        ):
            raise ValueError("tmux exposed an invalid Terminal editor record")
        window, dead, digest = parts
        if not digest:
            continue
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError("tmux exposed an invalid Terminal editor digest")
        records.append(
            {"tmux_window": window, "dead": dead == "1", "digest": digest}
        )
    return records


def normalize_workspace_command(value: object) -> str:
    commands = normalize_workspace_commands((value,))
    if len(commands) != 1:
        raise ValueError("Workspace command cannot be empty")
    return commands[0]


def validate_workspace_command_slot(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Workspace command slot is invalid")
    if value < 0 or value >= MAX_WORKSPACE_COMMANDS:
        raise ValueError("Workspace command slot is invalid")
    return value


def validate_workspace_command_launch(value: object) -> str:
    launch_id = str(value)
    try:
        parsed = uuid.UUID(hex=launch_id)
    except (AttributeError, ValueError) as exc:
        raise ValueError("Workspace command launch identity is invalid") from exc
    if parsed.hex != launch_id:
        raise ValueError("Workspace command launch identity is invalid")
    return launch_id


def workspace_command_digest(command: str) -> str:
    return hashlib.sha256(normalize_workspace_command(command).encode("utf-8")).hexdigest()


def parse_tmux_workspace_command_records(output: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in output.splitlines():
        if not line:
            continue
        parts = line.split("|", 4)
        if (
            len(parts) != 5
            or not parts[0].startswith("@")
            or not parts[0][1:].isdigit()
            or parts[1] not in {"0", "1"}
        ):
            raise ValueError("tmux exposed an invalid Workspace command record")
        window, dead, slot_raw, launch_id, digest = parts
        if not slot_raw:
            continue
        if not slot_raw.isdigit():
            raise ValueError("tmux exposed an invalid Workspace command slot")
        slot = validate_workspace_command_slot(int(slot_raw))
        validate_workspace_command_launch(launch_id)
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("tmux exposed an invalid Workspace command digest")
        records.append(
            {
                "tmux_window": window,
                "dead": dead == "1",
                "slot": slot,
                "launch_id": launch_id,
                "digest": digest,
            }
        )
    slots = [record["slot"] for record in records]
    if len(slots) != len(set(slots)):
        raise ValueError("tmux exposed duplicate Workspace command slots")
    return records


def workspace_command_record_is_ready(
    output: str,
    *,
    window: str,
    slot: int,
    launch_id: str,
    digest: str,
) -> bool:
    return any(
        record["tmux_window"] == window
        and record["slot"] == slot
        and record["launch_id"] == launch_id
        and record["digest"] == digest
        for record in parse_tmux_workspace_command_records(output)
    )


def tmux_browser_view_session(client_id: str) -> str:
    """Return the internal tmux session used by one browser terminal view."""

    try:
        parsed = uuid.UUID(hex=str(client_id))
    except (ValueError, AttributeError) as exc:
        raise TerminalError("Browser Terminal view identity is invalid") from exc
    if parsed.hex != client_id:
        raise TerminalError("Browser Terminal view identity is invalid")
    return f"{TMUX_BROWSER_VIEW_PREFIX}{client_id}"


def set_tmux_browser_view_grid_resize(
    run_tmux: Any,
    view_session: str,
    *,
    enabled: bool,
) -> bool:
    """Make one browser view the only size-affecting client for its window."""

    def listed_clients() -> tuple[bool, list[tuple[str, str, str]]]:
        listed = run_tmux(
            "list-clients",
            "-F",
            "#{client_name}\t#{session_name}\t#{window_id}",
            check=False,
        )
        clients: list[tuple[str, str, str]] = []
        for line in listed.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3 and all(parts):
                clients.append((parts[0], parts[1], parts[2]))
        return listed.returncode == 0, clients

    deadline = time.monotonic() + 1.0
    while True:
        _listed, clients = listed_clients()
        target = next((item for item in clients if item[1] == view_session), None)
        if target is not None:
            client_name, _, window_id = target
            if enabled:
                for peer_name, peer_session, peer_window in clients:
                    if (
                        peer_name == client_name
                        or peer_window != window_id
                        or not peer_session.startswith(TMUX_BROWSER_VIEW_PREFIX)
                    ):
                        continue
                    demoted = run_tmux(
                        "refresh-client",
                        "-t",
                        peer_name,
                        "-f",
                        "ignore-size",
                        check=False,
                    )
                    if demoted.returncode:
                        rechecked, current_clients = listed_clients()
                        if not rechecked or any(
                            current_name == peer_name
                            and current_session.startswith(TMUX_BROWSER_VIEW_PREFIX)
                            and current_window == window_id
                            for current_name, current_session, current_window in current_clients
                        ):
                            return False
            result = run_tmux(
                "refresh-client",
                "-t",
                client_name,
                "-f",
                "!ignore-size" if enabled else "ignore-size",
                check=False,
            )
            return result.returncode == 0
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def parse_tmux_terminal_records(output: str) -> list[dict[str, str | int | None]]:
    """Decode the printable tmux record format shared by Local and SSH."""

    records: list[dict[str, str | int | None]] = []
    for line in output.splitlines():
        if not line:
            continue
        first, separator, remainder = line.partition("|")
        if first.startswith("@"):
            # Compatibility for persisted fixtures and older Node peers. Live
            # providers use the version with session and activity fields.
            session_name: str | None = None
            window_id = first
            activity_at: int | None = None
            window_separator = activity_separator = separator
        else:
            session_name = first
            window_id, window_separator, remainder = remainder.partition("|")
            activity_raw, activity_separator, remainder = remainder.partition("|")
            if not activity_raw.isdigit():
                raise ValueError("tmux exposed an invalid Terminal record")
            activity_at = int(activity_raw)
        identity = remainder.rsplit("|", 2)
        if (
            not separator
            or not window_separator
            or not activity_separator
            or session_name == ""
            or not window_id.startswith("@")
            or len(identity) != 3
        ):
            raise ValueError("tmux exposed an invalid Terminal record")
        name, role, managed_run_id = identity
        records.append(
            {
                "tmux_window": window_id,
                "tmux_session": session_name,
                "activity_at": activity_at,
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


def file_run_dead_pane_fallback(
    state: dict[str, Any] | None,
    pane: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Recover a confirmed program exit after runtime preparation succeeded.

    A valid running record proves the wrapper reached the program. If its atomic
    completion record is still unavailable after the dead-pane grace period,
    tmux's exit status remains authoritative for every program exit code. Stop
    requests are handled before this fallback by the force-stopped marker.
    """
    exit_code = pane.get("exit_code") if pane is not None else None
    if (
        state is None
        or state.get("state") != "running"
        or pane is None
        or pane.get("dead") is not True
        or isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
    ):
        return None
    return {
        "state": "finished",
        "started_at": state.get("started_at"),
        "ended_at": None,
        "exit_code": exit_code,
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


def terminal_input_claims_grid(payload: dict[str, Any]) -> bool:
    """Let only an explicit real-user signal claim the shared terminal grid."""

    return payload.get("user_input") is True


def touch_terminal_output_if_present(store: StateStore, terminal_id: str) -> bool:
    """Ignore the expected attach-exit race after reconciliation removes a window."""

    try:
        store.touch_terminal_output(terminal_id)
    except KeyError:
        return False
    return True


class TerminalManager:
    def __init__(self, store: StateStore, control: TerminalControl | None = None) -> None:
        self.store = store
        self.control = control or TerminalControl()
        self._workspace_command_locks: dict[str, threading.RLock] = {}
        self._workspace_command_locks_guard = threading.Lock()
        self._browser_grid_locks: dict[str, threading.RLock] = {}
        self._browser_grid_locks_guard = threading.Lock()
        self._browser_grid_owners: dict[str, str] = {}

    @staticmethod
    def _run_tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("TERMROOM_PASSWORD", None)
        command = ["tmux"]
        test_socket = environment.get("PYTEST_TMUX_SOCKET", "")
        if test_socket:
            command.extend(("-S", test_socket))
        return subprocess.run(
            [*command, *args],
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
        self._run_tmux("set-environment", "-g", "-u", "TERMROOM_PASSWORD", check=False)
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

    def _list_tmux_window_records(self, session_name: str) -> list[dict[str, str | int | None]]:
        result = self._run_tmux(
            "list-windows",
            "-t",
            session_name,
            "-F",
            TMUX_TERMINAL_RECORD_FORMAT,
        )
        records = parse_tmux_terminal_records(result.stdout)
        for record in records:
            if record["tmux_session"] is None:
                record["tmux_session"] = session_name
        return records

    def refresh_activity(self, workspaces: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """Refresh requested Local workspaces with one tmux server query."""

        requested = {
            str(workspace["tmux_session"]): workspace
            for workspace in workspaces
            if workspace.get("backend_kind", "local") == "local"
        }
        if not requested:
            return {}
        result = self._run_tmux(
            "list-windows", "-a", "-F", TMUX_TERMINAL_RECORD_FORMAT, check=False
        )
        if result.returncode:
            raise TerminalError(result.stderr.strip() or "Terminal activity refresh failed")
        grouped: dict[str, list[dict[str, Any]]] = {
            str(workspace["id"]): [] for workspace in requested.values()
        }
        for record in parse_tmux_terminal_records(result.stdout):
            workspace = requested.get(str(record["tmux_session"]))
            if workspace is None:
                continue
            grouped[str(workspace["id"])].append(record)
        return self.store.observe_terminal_activity_batch(
            {workspace_id: records for workspace_id, records in grouped.items() if records}
        )

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
        terminal = next((item for item in terminals if item["tmux_window"] == tmux_window), None)
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
            "#{pane_id}\t#{pane_dead}\t#{pane_dead_status}\t#{pane_pid}\t#{pane_dead_time}",
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
                    for item in self._list_tmux_window_records(str(workspace["tmux_session"]))
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
                result = self._run_tmux("kill-window", "-t", tmux_window, check=False)
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
        terminal = next((item for item in terminals if item.get("role") == "file_run"), None)
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
            self._run_tmux("set-window-option", "-t", tmux_window, "remain-on-exit", "on")
            self._run_tmux(
                "set-window-option",
                "-t",
                tmux_window,
                "remain-on-exit-format",
                "",
                check=False,
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
                raise TerminalError(result.stderr.strip() or "File Run could not start")
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
                "state": "stopped" if file_run_completion_was_stopped(completion) else "finished",
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
        fallback = file_run_dead_pane_fallback(state, pane)
        if fallback is not None:
            return fallback
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
                if item.get("role") == "file_run" and item.get("managed_run_id") == run_id
            ),
            None,
        )
        if terminal is None:
            return False
        pane = self._file_run_pane(str(terminal["tmux_window"]))
        if pane is None or pane["dead"]:
            return False
        (metadata_dir / "stop-requested-at").write_text(str(time.time()) + "\n", encoding="utf-8")
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
                if item.get("role") == "file_run" and item.get("managed_run_id") == run_id
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
        (metadata_dir / "stop-requested-at").write_text(str(time.time()) + "\n", encoding="utf-8")
        try:
            os.killpg(pane_pid, signal.SIGKILL)
        except ProcessLookupError:
            return False
        (metadata_dir / "force-stopped").write_text(str(time.time()) + "\n", encoding="utf-8")
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

    def open_terminal_editor(
        self, workspace: dict[str, Any], relative_path: str
    ) -> dict[str, Any]:
        """Open one file in a persistent tmux-hosted Vim-compatible editor."""

        normalized = normalize_terminal_editor_path(relative_path)
        root = Path(workspace["path"]).resolve(strict=True)
        target = (root / normalized).resolve(strict=True)
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise TerminalError("Terminal editor file is outside the Workspace") from exc
        if not target.is_file():
            raise TerminalError("Terminal editor target is not a regular file")
        if not any(shutil.which(candidate) for candidate in ("nvim", "vim", "vi")):
            raise TerminalError("Install Neovim or Vim to edit files in the Terminal")

        with self._workspace_command_lock(str(workspace["id"])):
            terminals = self.ensure_workspace(workspace)
            session = str(workspace["tmux_session"])
            digest = terminal_editor_digest(normalized)
            listed = self._run_tmux(
                "list-windows", "-t", session, "-F", TMUX_TERMINAL_EDITOR_RECORD_FORMAT
            )
            try:
                records = parse_tmux_terminal_editor_records(listed.stdout)
            except ValueError as exc:
                raise TerminalError(str(exc)) from exc
            existing = next((item for item in records if item["digest"] == digest), None)
            if existing is not None and not existing["dead"]:
                terminal = next(
                    (
                        item
                        for item in terminals
                        if item["tmux_window"] == existing["tmux_window"]
                    ),
                    None,
                )
                if terminal is None:
                    raise TerminalError("Vim Terminal is missing")
                return terminal
            if existing is not None:
                self._run_tmux(
                    "kill-window", "-t", str(existing["tmux_window"]), check=False
                )

            created = self._run_tmux(
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{window_id}",
                "-e",
                f"TERMROOM_TERMINAL_EDITOR_FILE={target}",
                "-e",
                f"TERMROOM_TERMINAL_EDITOR_DIGEST={digest}",
                "-t",
                session,
                "-n",
                normalize_terminal_name(f"vim-{PurePosixPath(normalized).name}"),
                "-c",
                str(root),
                TERMINAL_EDITOR_WRAPPER,
            )
            window = created.stdout.strip()
            try:
                deadline = time.monotonic() + WORKSPACE_COMMAND_READY_TIMEOUT_SECONDS
                while time.monotonic() < deadline:
                    ready = self._run_tmux(
                        "display-message",
                        "-p",
                        "-t",
                        window,
                        f"#{{{TMUX_TERMINAL_EDITOR_DIGEST_OPTION}}}",
                        check=False,
                    )
                    if ready.returncode == 0 and ready.stdout.strip() == digest:
                        break
                    time.sleep(WORKSPACE_COMMAND_READY_POLL_SECONDS)
                else:
                    raise TerminalError("Vim Terminal did not finish starting")
            except Exception:
                self._run_tmux("kill-window", "-t", window, check=False)
                raise
            terminals = self.ensure_workspace(workspace)
            terminal = next(
                (item for item in terminals if item["tmux_window"] == window), None
            )
            if terminal is None:
                raise TerminalError("Vim Terminal disappeared while starting")
            return terminal

    def _workspace_command_lock(self, workspace_id: str) -> threading.RLock:
        with self._workspace_command_locks_guard:
            return self._workspace_command_locks.setdefault(workspace_id, threading.RLock())

    def run_workspace_command(
        self,
        workspace: dict[str, Any],
        *,
        slot: int,
        command: str,
        launch_id: str,
    ) -> dict[str, Any]:
        """Open or reuse one explicit Workspace-root command window."""

        with self._workspace_command_lock(str(workspace["id"])):
            return self._run_workspace_command_locked(
                workspace,
                slot=slot,
                command=command,
                launch_id=launch_id,
            )

    def _run_workspace_command_locked(
        self,
        workspace: dict[str, Any],
        *,
        slot: int,
        command: str,
        launch_id: str,
    ) -> dict[str, Any]:
        safe_slot = validate_workspace_command_slot(slot)
        safe_command = normalize_workspace_command(command)
        safe_launch = validate_workspace_command_launch(launch_id)
        digest = workspace_command_digest(safe_command)
        terminal_list = self.ensure_workspace(workspace)
        session = str(workspace["tmux_session"])
        result = self._run_tmux(
            "list-windows",
            "-t",
            session,
            "-F",
            TMUX_WORKSPACE_COMMAND_RECORD_FORMAT,
        )
        try:
            records = parse_tmux_workspace_command_records(result.stdout)
        except ValueError as exc:
            raise TerminalError(str(exc)) from exc
        existing = next((item for item in records if item["slot"] == safe_slot), None)
        if existing is not None:
            terminal = next(
                (item for item in terminal_list if item["tmux_window"] == existing["tmux_window"]),
                None,
            )
            if terminal is None:
                raise TerminalError("Workspace command Terminal is missing")
            if existing["launch_id"] == safe_launch:
                if existing["digest"] != digest:
                    raise TerminalError(
                        "Workspace command launch identity was reused for another command"
                    )
                return terminal
            if not existing["dead"]:
                return terminal
            self._run_tmux("kill-window", "-t", str(existing["tmux_window"]))

        created = self._run_tmux(
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{window_id}",
            "-e",
            f"TERMROOM_WORKSPACE_COMMAND={safe_command}",
            "-e",
            f"TERMROOM_WORKSPACE_COMMAND_SLOT={safe_slot}",
            "-e",
            f"TERMROOM_WORKSPACE_COMMAND_LAUNCH={safe_launch}",
            "-e",
            f"TERMROOM_WORKSPACE_COMMAND_DIGEST={digest}",
            "-t",
            session,
            "-n",
            f"run-{safe_slot + 1}",
            "-c",
            str(workspace["path"]),
            WORKSPACE_COMMAND_WRAPPER,
        )
        window = created.stdout.strip()
        try:
            deadline = time.monotonic() + WORKSPACE_COMMAND_READY_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                ready = self._run_tmux(
                    "display-message",
                    "-p",
                    "-t",
                    window,
                    TMUX_WORKSPACE_COMMAND_RECORD_FORMAT,
                    check=False,
                )
                if ready.returncode == 0 and workspace_command_record_is_ready(
                    ready.stdout,
                    window=window,
                    slot=safe_slot,
                    launch_id=safe_launch,
                    digest=digest,
                ):
                    break
                time.sleep(WORKSPACE_COMMAND_READY_POLL_SECONDS)
            else:
                raise TerminalError("Workspace command Terminal did not finish starting")
        except Exception:
            self._run_tmux("kill-window", "-t", window, check=False)
            raise
        terminal_list = self.ensure_workspace(workspace)
        terminal = next((item for item in terminal_list if item["tmux_window"] == window), None)
        if terminal is None:
            raise TerminalError("Workspace command Terminal disappeared while starting")
        return terminal

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
        self,
        workspace: dict[str, Any],
        terminal: dict[str, Any],
        lines: int = 2000,
        *,
        history_only: bool = False,
    ) -> str:
        self.ensure_workspace(workspace)
        args = [
            "capture-pane",
            "-p",
            "-J",
            "-S",
            f"-{max(100, min(lines, 10000))}",
        ]
        if history_only:
            args.extend(("-E", "-1"))
        args.extend(("-t", terminal["tmux_window"]))
        result = self._run_tmux(*args)
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
        terminal_id = str(terminal["id"])
        client_id = self.control.register(terminal_id)
        view_session = tmux_browser_view_session(client_id)
        process_pid: int | None = None
        master_fd: int | None = None
        last_viewport: tuple[int, int] | None = None

        async def output_to_browser() -> None:
            decoder = TerminalOutputDecoder()
            while True:
                try:
                    if master_fd is None:
                        return
                    chunk = await asyncio.to_thread(os.read, master_fd, 65536)
                except OSError:
                    tail = decoder.feed(b"", final=True)
                    if tail:
                        await asyncio.to_thread(
                            touch_terminal_output_if_present, self.store, terminal_id
                        )
                        await websocket.send_text(tail)
                    return
                if not chunk:
                    tail = decoder.feed(b"", final=True)
                    if tail:
                        await asyncio.to_thread(
                            touch_terminal_output_if_present, self.store, terminal_id
                        )
                        await websocket.send_text(tail)
                    return
                decoded = decoder.feed(chunk)
                if decoded:
                    await asyncio.to_thread(
                        touch_terminal_output_if_present, self.store, terminal_id
                    )
                    await websocket.send_text(decoded)

        async def resize_browser_view(payload: dict[str, Any]) -> None:
            nonlocal last_viewport
            if "rows" not in payload or "cols" not in payload:
                return
            size = terminal_size(payload)
            if size is None or master_fd is None or process_pid is None:
                return
            rows, cols = size
            controls_grid, grid_resize = self.control.resize_plan(
                terminal_id, client_id, rows=rows, cols=cols
            )
            role_changed = await asyncio.to_thread(
                self._browser_grid_role_changed,
                terminal_id,
                client_id,
                enabled=controls_grid,
            )
            if role_changed:
                changed = await asyncio.to_thread(
                    self._sync_browser_grid_role,
                    terminal_id,
                    client_id,
                    view_session,
                    enabled=controls_grid,
                )
                if not changed:
                    return
            if controls_grid and not self.control.can_resize(terminal_id, client_id):
                changed = await asyncio.to_thread(
                    self._sync_browser_grid_role,
                    terminal_id,
                    client_id,
                    view_session,
                    enabled=False,
                )
                if not changed:
                    return
                controls_grid = False
                grid_resize = False
            viewport = (rows, cols)
            if viewport == last_viewport and not grid_resize:
                return
            self._set_window_size(master_fd, rows=rows, cols=cols)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process_pid, signal.SIGWINCH)
            last_viewport = viewport

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
                    if master_fd is None:
                        return
                    self.control.mark_input(terminal_id, client_id, device_id)
                    if last_viewport is not None:
                        await resize_browser_view(
                            {"rows": last_viewport[0], "cols": last_viewport[1]}
                        )
                    os.write(master_fd, payload_bytes)
                    continue
                raw = message.get("text") or ""
                if len(raw.encode("utf-8")) > MAX_TERMINAL_MESSAGE_BYTES:
                    await websocket.close(code=1009, reason="Terminal input is too large")
                    return
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    if master_fd is None:
                        return
                    os.write(master_fd, raw.encode())
                    continue

                if not isinstance(payload, dict):
                    continue
                kind = payload.get("kind")
                if kind == "activity_ack":
                    revision = payload.get("activity_at")
                    if isinstance(revision, bool) or not isinstance(revision, int):
                        continue
                    try:
                        await asyncio.to_thread(
                            self.store.acknowledge_terminal_activity,
                            terminal_id,
                            device_id,
                            revision,
                        )
                    except (KeyError, ValueError):
                        continue
                elif kind == "resize":
                    await resize_browser_view(payload)
                elif kind == "command":
                    if master_fd is None:
                        return
                    self.control.mark_input(terminal_id, client_id, device_id)
                    await resize_browser_view(payload)
                    command = str(payload.get("data", ""))
                    await asyncio.to_thread(
                        self.store.add_command, workspace["id"], terminal["id"], command
                    )
                    os.write(master_fd, command.encode() + b"\r")
                elif kind == "input":
                    if master_fd is None:
                        return
                    if terminal_input_claims_grid(payload):
                        self.control.mark_input(terminal_id, client_id, device_id)
                    await resize_browser_view(payload)
                    os.write(master_fd, str(payload.get("data", "")).encode())

        output_task: asyncio.Task[None] | None = None
        input_task: asyncio.Task[None] | None = None
        try:
            self._prepare_browser_view(workspace, terminal, view_session)
            # The helper process creates a real controlling terminal after a
            # posix_spawn. This preserves resize semantics without calling forkpty
            # from the multi-threaded web server.
            process_pid, master_fd = self._spawn_tmux_client(workspace, view_session)
            output_task = asyncio.create_task(output_to_browser())
            input_task = asyncio.create_task(browser_to_input())
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
            self._forget_browser_grid_owner(terminal_id, client_id)
            for task in (output_task, input_task):
                if task is not None and not task.done():
                    task.cancel()
            if process_pid is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process_pid, signal.SIGTERM)
                exited = await asyncio.to_thread(self._wait_for_pid, process_pid, 1.0)
                if not exited:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process_pid, signal.SIGKILL)
                    await asyncio.to_thread(self._wait_for_pid, process_pid, 1.0)
            if master_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(master_fd)
            self._run_tmux("kill-session", "-t", view_session, check=False)

    def _prepare_browser_view(
        self,
        workspace: dict[str, Any],
        terminal: dict[str, Any],
        view_session: str,
    ) -> None:
        self._run_tmux("kill-session", "-t", view_session, check=False)
        created = self._run_tmux(
            "new-session",
            "-d",
            "-s",
            view_session,
            "-t",
            str(workspace["tmux_session"]),
            check=False,
        )
        if created.returncode:
            raise TerminalError(created.stderr.strip() or "Browser Terminal view could not start")
        selected = self._run_tmux(
            "select-window",
            "-t",
            f"{view_session}:{terminal['tmux_window']}",
            check=False,
        )
        if selected.returncode:
            self._run_tmux("kill-session", "-t", view_session, check=False)
            raise TerminalError(
                selected.stderr.strip() or "Browser Terminal window could not be selected"
            )

    def _spawn_tmux_client(
        self, workspace: dict[str, Any], view_session: str | None = None
    ) -> tuple[int, int]:
        environment = os.environ.copy()
        environment.pop("TMUX", None)
        environment.pop("TERMROOM_PASSWORD", None)
        command = ["tmux"]
        test_socket = environment.get("PYTEST_TMUX_SOCKET", "")
        if test_socket:
            command.extend(("-S", test_socket))
        environment["TERM"] = "xterm-256color"
        target_session = view_session or str(workspace["tmux_session"])
        process_pid, master_fd = spawn_pty_process(
            [
                *command,
                "attach-session",
                "-f",
                "ignore-size",
                "-t",
                target_session,
            ],
            cwd=str(workspace["path"]),
            environment=environment,
        )
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process_pid, signal.SIGWINCH)
        return process_pid, master_fd

    def _set_browser_view_grid_resize(self, view_session: str, *, enabled: bool) -> bool:
        """Choose whether one browser client may affect shared tmux dimensions."""

        return set_tmux_browser_view_grid_resize(
            self._run_tmux,
            view_session,
            enabled=enabled,
        )

    def _browser_grid_lock(self, terminal_id: str) -> threading.RLock:
        with self._browser_grid_locks_guard:
            return self._browser_grid_locks.setdefault(terminal_id, threading.RLock())

    def _browser_grid_role_changed(
        self,
        terminal_id: str,
        client_id: str,
        *,
        enabled: bool,
    ) -> bool:
        with self._browser_grid_lock(terminal_id):
            return (self._browser_grid_owners.get(terminal_id) == client_id) != enabled

    def _sync_browser_grid_role(
        self,
        terminal_id: str,
        client_id: str,
        view_session: str,
        *,
        enabled: bool,
    ) -> bool:
        with self._browser_grid_lock(terminal_id):
            current = self._browser_grid_owners.get(terminal_id)
            if not enabled:
                if current != client_id:
                    return True
                if not self._set_browser_view_grid_resize(view_session, enabled=False):
                    return False
                if self._browser_grid_owners.get(terminal_id) == client_id:
                    self._browser_grid_owners.pop(terminal_id, None)
                return True
            if current == client_id and self.control.can_resize(terminal_id, client_id):
                return True
            if not self.control.can_resize(terminal_id, client_id):
                changed = self._set_browser_view_grid_resize(view_session, enabled=False)
                if changed and self._browser_grid_owners.get(terminal_id) == client_id:
                    self._browser_grid_owners.pop(terminal_id, None)
                return changed
            if not self._set_browser_view_grid_resize(view_session, enabled=True):
                return False
            self._browser_grid_owners[terminal_id] = client_id
            if not self.control.can_resize(terminal_id, client_id):
                if not self._set_browser_view_grid_resize(view_session, enabled=False):
                    return False
                if self._browser_grid_owners.get(terminal_id) == client_id:
                    self._browser_grid_owners.pop(terminal_id, None)
            return True

    def _forget_browser_grid_owner(self, terminal_id: str, client_id: str) -> None:
        with self._browser_grid_lock(terminal_id):
            if self._browser_grid_owners.get(terminal_id) == client_id:
                self._browser_grid_owners.pop(terminal_id, None)

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
