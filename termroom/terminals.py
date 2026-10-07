from __future__ import annotations

import asyncio
import codecs
import contextlib
import contextvars
import fcntl
import hashlib
import json
import os
import shutil
import signal
import stat as stat_module
import struct
import subprocess
import termios
import threading
import time
import uuid
from collections.abc import Callable
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


TMUX_BROWSER_SIZE_FORMAT = (
    "#{client_width}|#{client_height}|#{window_width}|#{window_height}|#{status}"
)


def wait_tmux_browser_grid_size(read_size: Callable[[], str], *, rows: int, cols: int) -> bool:
    """Wait for tmux to process this browser PTY's resize before demoting it."""
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        fields = read_size().strip().split("|")
        if len(fields) == 5:
            status = {"off": 0, "on": 1}.get(fields[4])
            try:
                status = int(fields[4]) if status is None else status
                actual = tuple(int(value) for value in fields[:4])
            except ValueError:
                pass
            else:
                if actual == (cols, rows, cols, rows - status):
                    return True
        time.sleep(0.01)
    return False


def freeze_tmux_window_size(run_tmux: Any, window: str) -> bool:
    size = run_tmux(
        "display-message",
        "-p",
        "-t",
        window,
        "#{window_width} #{window_height}",
        check=False,
    )
    dimensions = size.stdout.strip().split()
    if size.returncode or len(dimensions) != 2 or not all(s.isdigit() for s in dimensions):
        return False
    # Explicit dimensions make window-size manual, without changing the grid.
    return (
        run_tmux(
            "resize-window",
            "-t",
            window,
            "-x",
            dimensions[0],
            "-y",
            dimensions[1],
            check=False,
        ).returncode
        == 0
    )


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
TMUX_WORKSPACE_COMMAND_STATE_OPTION = "@termroom_workspace_command_state"
TMUX_WORKSPACE_COMMAND_OPTIONS = (
    TMUX_WORKSPACE_COMMAND_SLOT_OPTION,
    TMUX_WORKSPACE_COMMAND_LAUNCH_OPTION,
    TMUX_WORKSPACE_COMMAND_DIGEST_OPTION,
    TMUX_WORKSPACE_COMMAND_STATE_OPTION,
)
TMUX_TERMINAL_EDITOR_DIGEST_OPTION = "@termroom_terminal_editor_digest"
WORKSPACE_COMMAND_READY_TIMEOUT_SECONDS = 2.0
WORKSPACE_COMMAND_READY_POLL_SECONDS = 0.01
FILE_RUN_COMPLETION_GRACE_SECONDS = 2.0
TERMINAL_TMUX_TIMEOUT_SECONDS = 5.0
PANE_MODE_REFRESH_INTERVAL_SECONDS = 0.1
_LOCAL_BRIDGE_TMUX_TIMEOUT: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "local_bridge_tmux_timeout", default=None
)
TMUX_WORKSPACE_COMMAND_RECORD_FORMAT = (
    "#{window_id}|#{pane_dead}|"
    f"#{{{TMUX_WORKSPACE_COMMAND_SLOT_OPTION}}}|"
    f"#{{{TMUX_WORKSPACE_COMMAND_LAUNCH_OPTION}}}|"
    f"#{{{TMUX_WORKSPACE_COMMAND_DIGEST_OPTION}}}|"
    f"#{{{TMUX_WORKSPACE_COMMAND_STATE_OPTION}}}"
)
TMUX_TERMINAL_EDITOR_RECORD_FORMAT = (
    f"#{{window_id}}|#{{pane_dead}}|#{{{TMUX_TERMINAL_EDITOR_DIGEST_OPTION}}}"
)
TMUX_TERMINAL_RECORD_FORMAT = (
    "#{session_name}|#{window_id}|#{window_activity}|#{window_name}|"
    f"#{{{TMUX_TERMINAL_ROLE_OPTION}}}|#{{{TMUX_MANAGED_RUN_OPTION}}}"
)

WORKSPACE_COMMAND_WRAPPER = r"""set -eu
pane=${TMUX_PANE:?}
command=${TERMROOM_WORKSPACE_COMMAND:?}
slot=${TERMROOM_WORKSPACE_COMMAND_SLOT:?}
launch=${TERMROOM_WORKSPACE_COMMAND_LAUNCH:?}
digest=${TERMROOM_WORKSPACE_COMMAND_DIGEST:?}
tmux_bin=$(type -P tmux)
case "$tmux_bin" in /*) ;; *) tmux_bin="$PWD/$tmux_bin" ;; esac
printf -v tmux_literal %q "$tmux_bin"
"$tmux_bin" set-window-option -t "$pane" remain-on-exit off
"$tmux_bin" set-window-option -t "$pane" @termroom_workspace_command_state running
"$tmux_bin" set-window-option -t "$pane" @termroom_workspace_command_digest "$digest"
"$tmux_bin" set-window-option -t "$pane" @termroom_workspace_command_launch "$launch"
"$tmux_bin" set-window-option -t "$pane" @termroom_workspace_command_slot "$slot"
unset TERMROOM_WORKSPACE_COMMAND TERMROOM_WORKSPACE_COMMAND_SLOT \
    TERMROOM_WORKSPACE_COMMAND_LAUNCH TERMROOM_WORKSPACE_COMMAND_DIGEST
set +e
/bin/bash --noprofile --norc -c "$command"
status=$?
set -e
"$tmux_bin" set-window-option -t "$pane" @termroom_workspace_command_state settling
if test "$status" -eq 0; then
    printf "\n✓\n"
else
    printf "\n✕ %s\n" "$status"
fi
shell=${SHELL:-/bin/bash}
if test ! -x "$shell"; then
    shell=/bin/bash
fi
shell_name=${shell##*/}
if test "$shell_name" = bash; then
    # pane_current_command becomes "bash" before .bashrc finishes. Source the
    # user rc normally, then publish readiness at the first Bash prompt.
    unset TERMROOM_WORKSPACE_SHELL_READY
    exec "$shell" --rcfile <(
        if test -r "${HOME:-}/.bashrc"; then
            printf "source %q\\n" "$HOME/.bashrc"
        fi
        printf "TERMROOM_WORKSPACE_READY_LAUNCH=%s\\n" "$launch"
        cat <<TERMROOM_BASH_RC
__termroom_workspace_shell_ready() {
    if test -z "\${TERMROOM_WORKSPACE_SHELL_READY:-}"; then
        TERMROOM_WORKSPACE_SHELL_READY=1
        if test "\$($tmux_literal show-window-option -v -t "\$TMUX_PANE" \
            @termroom_workspace_command_launch 2>/dev/null || true)" \
            = "\$TERMROOM_WORKSPACE_READY_LAUNCH"; then
            $tmux_literal set-window-option -t "\$TMUX_PANE" \
                @termroom_workspace_command_state shell >/dev/null 2>&1 || true
        fi
        unset TERMROOM_WORKSPACE_READY_LAUNCH
    fi
}
if [[ "\$(declare -p PROMPT_COMMAND 2>/dev/null)" == "declare -a"* ]]; then
    PROMPT_COMMAND+=(__termroom_workspace_shell_ready)
else
    PROMPT_COMMAND="\${PROMPT_COMMAND:+\$PROMPT_COMMAND; }__termroom_workspace_shell_ready"
fi
TERMROOM_BASH_RC
    )
elif test "$shell_name" = zsh; then
    startup_dir=$(mktemp -d "${TMPDIR:-/tmp}/termroom-zsh.XXXXXXXX") \
        || exec "$shell"
    user_zdotdir=${ZDOTDIR:-${HOME:-}}
    printf -v startup_dir_literal %q "$startup_dir"
    printf -v user_zdotdir_literal %q "$user_zdotdir"
    umask 077
    cat > "$startup_dir/.zshenv" <<TERMROOM_ZSH_ENV
ZDOTDIR=$user_zdotdir_literal
if [[ -r "\$ZDOTDIR/.zshenv" ]]; then source "\$ZDOTDIR/.zshenv"; fi
TERMROOM_WORKSPACE_USER_ZDOTDIR=\${ZDOTDIR:-$user_zdotdir_literal}
TERMROOM_WORKSPACE_INIT_DIR=$startup_dir_literal
export TERMROOM_WORKSPACE_USER_ZDOTDIR TERMROOM_WORKSPACE_INIT_DIR
ZDOTDIR=$startup_dir_literal
export ZDOTDIR
TERMROOM_ZSH_ENV
    cat > "$startup_dir/.zshrc" <<TERMROOM_ZSH_RC
ZDOTDIR=\${TERMROOM_WORKSPACE_USER_ZDOTDIR:-$user_zdotdir_literal}
if [[ -r "\$ZDOTDIR/.zshrc" ]]; then source "\$ZDOTDIR/.zshrc"; fi
__termroom_workspace_cleanup() {
    /bin/rm -f $startup_dir_literal/.zshenv $startup_dir_literal/.zshrc \\
        $startup_dir_literal/.zcompdump $startup_dir_literal/.zcompdump.zwc
    /bin/rmdir $startup_dir_literal 2>/dev/null || true
}
__termroom_workspace_shell_ready() {
    precmd_functions=(\${precmd_functions:#__termroom_workspace_shell_ready})
    __termroom_workspace_cleanup
    if [[ "\$($tmux_literal show-window-option -v -t "\$TMUX_PANE" \
        @termroom_workspace_command_launch 2>/dev/null || true)" == "$launch" ]]; then
        $tmux_literal set-window-option -t "\$TMUX_PANE" \
            @termroom_workspace_command_state shell >/dev/null 2>&1 || true
    fi
}
precmd_functions+=(__termroom_workspace_shell_ready)
zshexit_functions+=(__termroom_workspace_cleanup)
TERMROOM_ZSH_RC
    ZDOTDIR=$startup_dir
    export ZDOTDIR
    exec "$shell"
elif test "$shell_name" = sh || test "$shell_name" = dash; then
    startup_file=$(mktemp "${TMPDIR:-/tmp}/termroom-sh.XXXXXXXX") \
        || exec "$shell"
    user_env=${ENV:-}
    printf -v startup_file_literal %q "$startup_file"
    printf -v user_env_literal %q "$user_env"
    umask 077
    cat > "$startup_file" <<TERMROOM_SH_ENV
if test -r $user_env_literal; then . $user_env_literal; fi
if test -n $user_env_literal; then ENV=$user_env_literal; export ENV; else unset ENV; fi
/bin/rm -f $startup_file_literal
if test "\$($tmux_literal show-window-option -v -t "\$TMUX_PANE" \\
    @termroom_workspace_command_launch 2>/dev/null || true)" = "$launch"; then
    $tmux_literal set-window-option -t "\$TMUX_PANE" \\
        @termroom_workspace_command_state shell >/dev/null 2>&1 || true
fi
TERMROOM_SH_ENV
    ENV=$startup_file
    export ENV
    exec "$shell"
else
    printf -v pane_literal %q "$pane"
    printf -v shell_name_literal %q "$shell_name"
    printf -v launch_literal %q "$launch"
    readiness_command="while test \"\$($tmux_literal display-message -p -t $pane_literal \
        \"##{pane_dead}\")\" = 0 && test \"\$($tmux_literal display-message -p -t \
        $pane_literal \"##{pane_current_command}\")\" != $shell_name_literal; do
        sleep 0.01
    done
    if test \"\$($tmux_literal display-message -p -t $pane_literal \
        \"##{pane_dead}\")\" = 0 && test \"\$($tmux_literal \
        show-window-option -v -t $pane_literal \
        @termroom_workspace_command_launch 2>/dev/null || true)\" = $launch_literal; then
        $tmux_literal set-window-option -t $pane_literal \
            @termroom_workspace_command_state shell
    fi"
    "$tmux_bin" run-shell -b "$readiness_command" >/dev/null 2>&1 || true
    exec "$shell"
fi
"""
WORKSPACE_COMMAND_WRAPPER_ARGV = (
    "/bin/bash",
    "--noprofile",
    "--norc",
    "-p",
    "-c",
    WORKSPACE_COMMAND_WRAPPER,
)

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
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("tmux exposed an invalid Terminal editor digest")
        records.append({"tmux_window": window, "dead": dead == "1", "digest": digest})
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
        parts = line.split("|", 5)
        if (
            len(parts) != 6
            or not parts[0].startswith("@")
            or not parts[0][1:].isdigit()
            or parts[1] not in {"0", "1"}
        ):
            raise ValueError("tmux exposed an invalid Workspace command record")
        window, dead, slot_raw, launch_id, digest, state_raw = parts
        if not slot_raw:
            continue
        if not slot_raw.isdigit():
            raise ValueError("tmux exposed an invalid Workspace command slot")
        slot = validate_workspace_command_slot(int(slot_raw))
        validate_workspace_command_launch(launch_id)
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("tmux exposed an invalid Workspace command digest")
        if state_raw == "":
            # Old state-less command windows are not compatible managed shortcuts.
            continue
        if state_raw not in {"running", "settling", "shell"}:
            raise ValueError("tmux exposed an invalid Workspace command state")
        dead_pane = dead == "1"
        records.append(
            {
                "tmux_window": window,
                "dead": dead_pane,
                "slot": slot,
                "launch_id": launch_id,
                "digest": digest,
                "state": "dead" if dead_pane else state_raw,
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
        and record["state"] in {"running", "settling", "shell"}
        for record in parse_tmux_workspace_command_records(output)
    )


def clear_tmux_workspace_command_identity(run_tmux: Any, window: str) -> bool:
    """Detach an existing terminal from one Workspace command shortcut slot."""

    for option in TMUX_WORKSPACE_COMMAND_OPTIONS:
        result = run_tmux(
            "set-window-option",
            "-u",
            "-t",
            window,
            option,
            check=False,
        )
        if result.returncode:
            return False
    return True


def workspace_command_history_name(slot: int, launch_id: str) -> str:
    safe_slot = validate_workspace_command_slot(slot)
    safe_launch = validate_workspace_command_launch(launch_id)
    return f"run-{safe_slot + 1}-{safe_launch[:4]}"


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
            if result.returncode:
                return False
            if enabled:
                return (
                    run_tmux(
                        "set-window-option", "-t", window_id, "window-size", "latest", check=False
                    ).returncode
                    == 0
                )
            return True
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


def file_run_dispatch_timestamp(request_id: Path) -> float | None:
    """Return the trusted local dispatch timestamp for one File Run request."""

    try:
        info = request_id.lstat()
    except OSError:
        return None
    if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISREG(info.st_mode):
        return None
    return float(info.st_mtime)


def file_run_completion_grace_active(
    pane: dict[str, Any] | None,
    *,
    dispatch_at: float | int | None = None,
    now: float | None = None,
) -> bool:
    """Keep a just-dispatched dead pane provisional while records settle.

    A reusable pane's dead timestamp belongs to the tmux lifecycle and is not
    by itself a reliable timestamp for the newly assigned run. The request-id
    file is written immediately before respawn, so the newest trustworthy
    timestamp defines the bounded completion-record grace period without
    guessing from the program name or exit code.
    """

    timestamps: list[float] = []
    dead_at = pane.get("dead_at") if pane is not None else None
    for value in (dead_at, dispatch_at):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        timestamps.append(float(value))
    if not timestamps:
        return False
    current = time.time() if now is None else float(now)
    age = current - max(timestamps)
    return 0 <= age < FILE_RUN_COMPLETION_GRACE_SECONDS


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


async def _await_owned_task(task: asyncio.Task[Any]) -> tuple[Any, bool]:
    """Wait for a blocking operation to finish before releasing what it owns."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


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
        self._terminal_resize_locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _run_tmux(
        *args: str,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if timeout is None:
            timeout = _LOCAL_BRIDGE_TMUX_TIMEOUT.get()
        environment = os.environ.copy()
        environment.pop("TERMROOM_PASSWORD", None)
        command = ["tmux"]
        test_socket = environment.get("PYTEST_TMUX_SOCKET", "")
        if test_socket:
            command.extend(("-S", test_socket))
        try:
            return subprocess.run(
                [*command, *args],
                check=check,
                capture_output=True,
                text=True,
                env=environment,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise TerminalError(f"tmux command timed out after {timeout:g} seconds") from exc

    def _tmux_runner(
        self, timeout: float | None
    ) -> Callable[..., subprocess.CompletedProcess[str]]:
        if timeout is None:
            return self._run_tmux
        return lambda *args, **kwargs: self._run_tmux(*args, timeout=timeout, **kwargs)

    def session_exists(self, session_name: str, *, tmux_timeout: float | None = None) -> bool:
        result = self._tmux_runner(tmux_timeout)("has-session", "-t", session_name, check=False)
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

    def _browser_views_for_group(
        self, session_name: str, *, tmux_timeout: float | None = None
    ) -> list[tuple[str, bool]]:
        listed = self._tmux_runner(tmux_timeout)(
            "list-sessions",
            "-F",
            "#{session_name}\t#{session_group}\t#{session_attached}",
            check=False,
        )
        if listed.returncode:
            return []
        views: list[tuple[str, bool]] = []
        for line in listed.stdout.splitlines():
            parts = line.split("\t", 2)
            if (
                len(parts) == 3
                and parts[0].startswith(TMUX_BROWSER_VIEW_PREFIX)
                and parts[1] == session_name
                and parts[2].isdigit()
            ):
                views.append((parts[0], int(parts[2]) > 0))
        return views

    def _recover_workspace_from_browser_view(
        self, session_name: str, *, tmux_timeout: float | None = None
    ) -> bool:
        """Restore a missing canonical session from a surviving linked browser view."""

        run_tmux = self._tmux_runner(tmux_timeout)
        views = self._browser_views_for_group(session_name, tmux_timeout=tmux_timeout)
        for view_session, _attached in views:
            restored = run_tmux(
                "new-session",
                "-d",
                "-s",
                session_name,
                "-t",
                view_session,
                check=False,
            )
            if restored.returncode:
                continue
            for stale_view, attached in views:
                if not attached:
                    run_tmux("kill-session", "-t", stale_view, check=False)
            return True
        for view_session, _attached in views:
            run_tmux("kill-session", "-t", view_session, check=False)
        return False

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

    def ensure_workspace(
        self,
        workspace: dict[str, Any],
        *,
        tmux_timeout: float | None = None,
    ) -> list[dict[str, Any]]:
        run_tmux = self._tmux_runner(tmux_timeout)
        session = workspace["tmux_session"]
        workspace_path = Path(workspace["path"])
        # A Core login secret must never become shell environment. If Termroom
        # is using an already-running tmux server, remove it before a new pane
        # is created.
        run_tmux("set-environment", "-g", "-u", "TERMROOM_PASSWORD", check=False)
        created_session = not self.session_exists(session, tmux_timeout=tmux_timeout)
        if created_session:
            recovered_session = self._recover_workspace_from_browser_view(
                str(session), tmux_timeout=tmux_timeout
            )
            if recovered_session:
                created_session = False
            else:
                run_tmux(
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
        if created_session:
            run_tmux("set-window-option", "-t", session, "window-size", "latest", check=False)

        terminals = self.store.reconcile_terminals(
            str(workspace["id"]),
            self._list_tmux_window_records(session, tmux_timeout=tmux_timeout),
        )
        if created_session:
            for terminal in terminals:
                self.control.mark_grid_fresh(str(terminal["id"]))
        return terminals

    def _list_tmux_window_records(
        self, session_name: str, *, tmux_timeout: float | None = None
    ) -> list[dict[str, str | int | None]]:
        result = self._tmux_runner(tmux_timeout)(
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
        dispatch_at = file_run_dispatch_timestamp(metadata_dir / "request-id")
        if file_run_completion_grace_active(pane, dispatch_at=dispatch_at):
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
        terminal = self.store.create_terminal(workspace["id"], safe_name, result.stdout.strip())
        self.control.mark_grid_fresh(str(terminal["id"]))
        return terminal

    def open_terminal_editor(self, workspace: dict[str, Any], relative_path: str) -> dict[str, Any]:
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
                    (item for item in terminals if item["tmux_window"] == existing["tmux_window"]),
                    None,
                )
                if terminal is None:
                    raise TerminalError("Vim Terminal is missing")
                return terminal
            if existing is not None:
                self._run_tmux("kill-window", "-t", str(existing["tmux_window"]), check=False)

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
            terminal = next((item for item in terminals if item["tmux_window"] == window), None)
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
        window = ""
        created_window = False
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
            if existing["digest"] != digest:
                if existing["dead"]:
                    self._run_tmux("kill-window", "-t", str(existing["tmux_window"]))
                elif not clear_tmux_workspace_command_identity(
                    self._run_tmux, str(existing["tmux_window"])
                ):
                    raise TerminalError("Previous Workspace command Terminal could not be detached")
                else:
                    self._run_tmux(
                        "rename-window",
                        "-t",
                        str(existing["tmux_window"]),
                        workspace_command_history_name(safe_slot, str(existing["launch_id"])),
                        check=False,
                    )
                existing = None
            elif existing["state"] in {"running", "settling"}:
                return terminal
            elif existing["dead"]:
                self._run_tmux("kill-window", "-t", str(existing["tmux_window"]))
            else:
                window = str(existing["tmux_window"])
                respawned = self._run_tmux(
                    "respawn-pane",
                    "-k",
                    "-c",
                    str(workspace["path"]),
                    "-e",
                    f"TERMROOM_WORKSPACE_COMMAND={safe_command}",
                    "-e",
                    f"TERMROOM_WORKSPACE_COMMAND_SLOT={safe_slot}",
                    "-e",
                    f"TERMROOM_WORKSPACE_COMMAND_LAUNCH={safe_launch}",
                    "-e",
                    f"TERMROOM_WORKSPACE_COMMAND_DIGEST={digest}",
                    "-t",
                    window,
                    *WORKSPACE_COMMAND_WRAPPER_ARGV,
                    check=False,
                )
                if respawned.returncode:
                    raise TerminalError(
                        respawned.stderr.strip() or "Workspace command Terminal could not restart"
                    )

        if not window:
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
                *WORKSPACE_COMMAND_WRAPPER_ARGV,
            )
            window = created.stdout.strip()
            created_window = True
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
            if created_window:
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
        ansi: bool = False,
    ) -> str:
        self.ensure_workspace(workspace)
        args = [
            "capture-pane",
            "-p",
        ]
        if ansi:
            args.append("-e")
        args.extend(
            (
                "-J",
                "-S",
                f"-{max(100, min(lines, 10000))}",
            )
        )
        if history_only:
            args.extend(("-E", "-1"))
        args.extend(("-t", terminal["tmux_window"]))
        result = self._run_tmux(*args, check=not ansi)
        if ansi and result.returncode:
            # tmux has supported capture-pane -e for years, but keep older
            # installations usable by retrying the exact capture as plain text.
            result = self._run_tmux(*(item for item in args if item != "-e"))
        return result.stdout

    async def bridge(
        self,
        websocket: WebSocket,
        workspace: dict[str, Any],
        terminal: dict[str, Any],
        *,
        device_id: str = "",
    ) -> None:
        terminal_id = str(terminal["id"])
        client_id = self.control.register(terminal_id, device_id=device_id)
        view_session = tmux_browser_view_session(client_id)
        resize_lock = self._terminal_resize_locks.setdefault(terminal_id, asyncio.Lock())
        process_pid: int | None = None
        master_fd: int | None = None
        last_viewport: tuple[int, int] | None = None

        async def output_to_browser() -> None:
            decoder = TerminalOutputDecoder()
            # Bounded backpressure and output-triggered coalescing: no idle polling
            # and at most ten mode queries/second, regardless of PTY chunk size.
            chunks: asyncio.Queue[bytes] = asyncio.Queue(maxsize=16)

            async def read_output() -> None:
                loop = asyncio.get_running_loop()
                while master_fd is not None:
                    readable = loop.create_future()

                    def ready(waiter: asyncio.Future[None] = readable) -> None:
                        if not waiter.done():
                            waiter.set_result(None)

                    try:
                        # One reader owns this PTY: only read after readiness,
                        # leaving shared executor capacity free while idle.
                        loop.add_reader(master_fd, ready)
                        await readable
                        chunk = os.read(master_fd, 65536)
                    except OSError:
                        chunk = b""
                    finally:
                        loop.remove_reader(master_fd)
                    await chunks.put(chunk)
                    if not chunk:
                        return

            reader = asyncio.create_task(read_output())
            last_refresh = asyncio.get_running_loop().time()
            try:
                while True:
                    batch = [await chunks.get()]
                    loop = asyncio.get_running_loop()
                    delay = last_refresh + PANE_MODE_REFRESH_INTERVAL_SECONDS - loop.time()
                    if delay > 0:
                        ready = loop.create_future()
                        timer = loop.call_later(delay, ready.set_result, None)
                        try:
                            await ready
                        finally:
                            timer.cancel()
                    while not chunks.empty() and len(batch) < 16 and batch[-1]:
                        batch.append(chunks.get_nowait())
                    await send_pane_mode()
                    last_refresh = loop.time()
                    decoded = decoder.feed(b"".join(batch), final=not batch[-1])
                    if decoded:
                        await asyncio.to_thread(
                            touch_terminal_output_if_present, self.store, terminal_id
                        )
                        await websocket.send_text(decoded)
                    if not batch[-1]:
                        return
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

        mode_revision = 0
        previous_mode: dict[str, Any] | None = None

        async def send_pane_mode() -> None:
            nonlocal mode_revision, previous_mode
            query = asyncio.create_task(
                asyncio.to_thread(self.current_pane_mode, workspace, terminal)
            )
            mode, cancelled = await _await_owned_task(query)
            if cancelled:
                raise asyncio.CancelledError
            if mode == previous_mode:
                return
            previous_mode = mode
            mode_revision += 1
            # Binary control frames cannot collide with application output (text).
            # A single output task orders state before the associated output batch.
            await websocket.send_bytes(
                json.dumps(
                    {
                        "kind": "pane_mode",
                        "terminal_id": terminal_id,
                        "generation": client_id,
                        "revision": mode_revision,
                        **mode,
                    }
                ).encode("utf-8")
            )

        async def apply_browser_resize(payload: dict[str, Any]) -> bool:
            nonlocal last_viewport
            if "rows" not in payload or "cols" not in payload:
                return True
            size = terminal_size(payload)
            if size is None or master_fd is None or process_pid is None:
                return False
            rows, cols = size
            plan = self.control.begin_resize(terminal_id, client_id, rows=rows, cols=cols)
            current = True

            async def sync_grid_role(*, enabled: bool) -> bool:
                return await asyncio.to_thread(
                    self._sync_browser_grid_role,
                    terminal_id,
                    client_id,
                    view_session,
                    enabled=enabled,
                    tmux_timeout=TERMINAL_TMUX_TIMEOUT_SECONDS,
                )

            try:
                changed = await sync_grid_role(enabled=plan is not None)
                if not changed:
                    return False
                if plan is not None and not self.control.resize_plan_current(plan):
                    if not await sync_grid_role(enabled=False):
                        raise TerminalError("Terminal grid demotion failed")
                    self.control.abort_resize(plan)
                    plan = None
                    current = False
                viewport = (rows, cols)
                if viewport != last_viewport or (plan is not None and plan.apply):
                    self._set_window_size(master_fd, rows=rows, cols=cols)
                    os.killpg(process_pid, signal.SIGWINCH)
                    last_viewport = viewport
                if plan is not None:
                    if plan.apply and not await asyncio.to_thread(
                        self._wait_browser_view_size,
                        view_session,
                        rows=rows,
                        cols=cols,
                        tmux_timeout=TERMINAL_TMUX_TIMEOUT_SECONDS,
                    ):
                        raise TerminalError("Terminal grid resize was not applied")
                    self.control.resize_applied(plan)
                    if plan.bootstrap and not await asyncio.to_thread(
                        freeze_tmux_window_size,
                        self._tmux_runner(TERMINAL_TMUX_TIMEOUT_SECONDS),
                        str(terminal["tmux_window"]),
                    ):
                        raise TerminalError("Terminal bootstrap grid freeze failed")
                    if plan.bootstrap and not await sync_grid_role(enabled=False):
                        raise TerminalError("Terminal bootstrap demotion failed")
                    if not self.control.commit_resize(plan):
                        if not await sync_grid_role(enabled=False):
                            raise TerminalError("Stale Terminal grid demotion failed")
                        return False
                return current
            finally:
                if plan is not None:
                    self.control.abort_resize(plan)

        async def resize_browser_view(payload: dict[str, Any]) -> bool:
            async with resize_lock:
                return await apply_browser_resize(payload)

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
                    if last_viewport is not None and not await resize_browser_view(
                        {"rows": last_viewport[0], "cols": last_viewport[1]}
                    ):
                        continue
                    await write_to_pty(payload_bytes)
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
                    await write_to_pty(raw.encode())
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
                    paste = str(payload.get("paste_data", command))
                    await write_to_pty(paste.encode() + b"\r")
                elif kind == "input":
                    if master_fd is None:
                        return
                    if terminal_input_claims_grid(payload):
                        self.control.mark_input(terminal_id, client_id, device_id)
                    if not await resize_browser_view(payload):
                        continue
                    await write_to_pty(str(payload.get("data", "")).encode())

        async def write_to_pty(data: bytes) -> None:
            if master_fd is None:
                return
            write_task = asyncio.create_task(asyncio.to_thread(os.write, master_fd, data))
            _, cancelled = await _await_owned_task(write_task)
            if cancelled:
                raise asyncio.CancelledError

        output_task: asyncio.Task[None] | None = None
        input_task: asyncio.Task[None] | None = None
        try:
            setup_task = asyncio.create_task(
                asyncio.to_thread(
                    self._setup_browser_terminal,
                    workspace,
                    terminal,
                    view_session,
                )
            )
            (process_pid, master_fd), cancelled = await _await_owned_task(setup_task)
            if cancelled:
                raise asyncio.CancelledError
            await send_pane_mode()
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
            if self.control.client_count(terminal_id) == 0:
                self._terminal_resize_locks.pop(terminal_id, None)
            for task in (output_task, input_task):
                if task is not None and not task.done():
                    task.cancel()
            cleanup_error = await asyncio.to_thread(
                self._release_browser_terminal,
                terminal_id,
                client_id,
                str(terminal["tmux_window"]),
                process_pid,
            )
            tasks = [task for task in (output_task, input_task) if task is not None]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.to_thread(self._finish_browser_terminal, master_fd, view_session)
            except Exception as exc:
                cleanup_error = cleanup_error or exc
            if cleanup_error is not None:
                if isinstance(cleanup_error, TerminalError):
                    raise cleanup_error
                raise TerminalError("Local Terminal cleanup failed") from cleanup_error

    def current_pane_mode(
        self, workspace: dict[str, Any], terminal: dict[str, Any]
    ) -> dict[str, Any]:
        """Query the selected canonical pane, never the connection's tmux screen."""
        result = self._tmux_runner(TERMINAL_TMUX_TIMEOUT_SECONDS)(
            "display-message",
            "-p",
            "-t",
            f"{workspace['tmux_session']}:{terminal['tmux_window']}",
            "#{session_name}|#{window_id}|#{pane_id}|#{pane_pid}|#{alternate_on}|"
            "#{mouse_any_flag}|#{mouse_standard_flag}|#{mouse_button_flag}|"
            "#{mouse_all_flag}|#{mouse_sgr_flag}",
        )
        fields = result.stdout.strip().split("|")
        if (
            len(fields) != 10
            or fields[0] != str(workspace["tmux_session"])
            or not fields[1].startswith("@")
            or not fields[2].startswith("%")
            or not fields[3].isdigit()
            or any(flag not in {"0", "1"} for flag in fields[4:])
        ):
            raise TerminalError("Current Terminal pane mode is unavailable")
        return {
            "session": fields[0],
            "window": fields[1],
            "pane": fields[2],
            "pane_pid": int(fields[3]),
            "alternate": fields[4] == "1",
            "mouse_tracking": any(flag == "1" for flag in fields[5:9]),
            "mouse_flags": [int(flag) for flag in fields[5:]],
        }

    def _setup_browser_terminal(
        self,
        workspace: dict[str, Any],
        terminal: dict[str, Any],
        view_session: str,
    ) -> tuple[int, int]:
        try:
            token = _LOCAL_BRIDGE_TMUX_TIMEOUT.set(TERMINAL_TMUX_TIMEOUT_SECONDS)
            try:
                self.ensure_workspace(workspace)
            finally:
                _LOCAL_BRIDGE_TMUX_TIMEOUT.reset(token)
            self.store.touch_terminal(terminal["id"])
            self._prepare_browser_view(
                workspace,
                terminal,
                view_session,
                tmux_timeout=TERMINAL_TMUX_TIMEOUT_SECONDS,
            )
            return self._spawn_tmux_client(workspace, view_session)
        except TerminalError:
            raise
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise TerminalError(f"Local Terminal setup failed: {exc}") from exc

    def _release_browser_terminal(
        self,
        terminal_id: str,
        client_id: str,
        window: str,
        process_pid: int | None,
    ) -> Exception | None:
        error: Exception | None = None
        try:
            self._forget_browser_grid_owner(
                terminal_id,
                client_id,
                window=window,
                tmux_timeout=TERMINAL_TMUX_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            error = exc
        if process_pid is not None:
            try:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process_pid, signal.SIGTERM)
                if not self._wait_for_pid(process_pid, 1.0):
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process_pid, signal.SIGKILL)
                    if not self._wait_for_pid(process_pid, 1.0):
                        raise TerminalError("Terminal PTY client did not exit after SIGKILL")
            except Exception as exc:
                error = error or exc
        return error

    def _finish_browser_terminal(self, master_fd: int | None, view_session: str) -> None:
        if master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(master_fd)
        for attempt in range(2):
            try:
                self._run_tmux(
                    "kill-session",
                    "-t",
                    view_session,
                    check=False,
                    timeout=TERMINAL_TMUX_TIMEOUT_SECONDS,
                )
                return
            except TerminalError:
                if attempt:
                    raise TerminalError("Browser Terminal cleanup timed out after retry") from None

    def _prepare_browser_view(
        self,
        workspace: dict[str, Any],
        terminal: dict[str, Any],
        view_session: str,
        *,
        tmux_timeout: float | None = None,
    ) -> None:
        run_tmux = self._tmux_runner(tmux_timeout)
        run_tmux("kill-session", "-t", view_session, check=False)
        created = run_tmux(
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
        try:
            # Mouse policy belongs to this disposable browser client, not the
            # original Workspace session or the user's global tmux defaults.
            run_tmux("set-option", "-t", view_session, "mouse", "on")
            selected = run_tmux(
                "select-window",
                "-t",
                f"{view_session}:{terminal['tmux_window']}",
                check=False,
            )
            if selected.returncode:
                raise TerminalError(
                    selected.stderr.strip() or "Browser Terminal window could not be selected"
                )
        except Exception:
            run_tmux("kill-session", "-t", view_session, check=False)
            raise

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

    def _set_browser_view_grid_resize(
        self,
        view_session: str,
        *,
        enabled: bool,
        tmux_timeout: float | None = None,
    ) -> bool:
        """Choose whether one browser client may affect shared tmux dimensions."""

        return set_tmux_browser_view_grid_resize(
            self._tmux_runner(tmux_timeout),
            view_session,
            enabled=enabled,
        )

    def _wait_browser_view_size(
        self,
        view_session: str,
        *,
        rows: int,
        cols: int,
        tmux_timeout: float | None = None,
    ) -> bool:
        run_tmux = self._tmux_runner(tmux_timeout)
        return wait_tmux_browser_grid_size(
            lambda: (
                run_tmux("list-clients", "-t", view_session, "-F", TMUX_BROWSER_SIZE_FORMAT).stdout
            ),
            rows=rows,
            cols=cols,
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
        tmux_timeout: float | None = None,
    ) -> bool:
        def set_grid_role(*, enabled: bool) -> bool:
            if tmux_timeout is None:
                return self._set_browser_view_grid_resize(view_session, enabled=enabled)
            return self._set_browser_view_grid_resize(
                view_session,
                enabled=enabled,
                tmux_timeout=tmux_timeout,
            )

        with self._browser_grid_lock(terminal_id):
            current = self._browser_grid_owners.get(terminal_id)
            if not enabled:
                if current != client_id:
                    return True
                if not set_grid_role(enabled=False):
                    return False
                if self._browser_grid_owners.get(terminal_id) == client_id:
                    self._browser_grid_owners.pop(terminal_id, None)
                return True
            if current == client_id and self.control.can_resize(terminal_id, client_id):
                return True
            if not self.control.can_resize(terminal_id, client_id):
                changed = set_grid_role(enabled=False)
                if changed and self._browser_grid_owners.get(terminal_id) == client_id:
                    self._browser_grid_owners.pop(terminal_id, None)
                return changed
            if not set_grid_role(enabled=True):
                return False
            self._browser_grid_owners[terminal_id] = client_id
            if not self.control.can_resize(terminal_id, client_id):
                if not set_grid_role(enabled=False):
                    return False
                if self._browser_grid_owners.get(terminal_id) == client_id:
                    self._browser_grid_owners.pop(terminal_id, None)
            return True

    def _forget_browser_grid_owner(
        self,
        terminal_id: str,
        client_id: str,
        *,
        window: str = "",
        tmux_timeout: float | None = None,
    ) -> None:
        with self._browser_grid_lock(terminal_id):
            if self._browser_grid_owners.get(terminal_id) == client_id:
                if window:
                    freeze_tmux_window_size(self._tmux_runner(tmux_timeout), window)
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
