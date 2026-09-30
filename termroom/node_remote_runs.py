from __future__ import annotations

import asyncio
import base64
import contextlib
import ctypes
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from termroom.node_core import NodeCore, NodeCoreError, NodeStream
from termroom.node_protocol import (
    MAX_NODE_MESSAGE_BYTES,
    MAX_NODE_STREAM_CHUNK_BYTES,
    NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW,
    NODE_REMOTE_RUN_SOURCE_VERSION,
    NODE_REMOTE_RUN_VERSION,
    validate_request_id,
)
from termroom.run_sources import (
    SourceFileChangedError,
    SourceValidationError,
    WorkspaceEntry,
    WorkspaceManifest,
    build_public_git_clone_invocation,
    build_workspace_manifest,
    normalize_explicit_include_paths,
    normalize_source_relative_path,
    validate_contained_symlink_target,
    validate_cwd_rel,
    validate_public_https_git_url,
)
from termroom.security import is_within
from termroom.ssh_backend import (
    REMOTE_GIT_BOOTSTRAP_SCRIPT,
    REMOTE_RUN_INITIAL_TAIL,
    REMOTE_RUN_LOG_PIPE_SCRIPT,
    REMOTE_RUN_LOG_READ_LIMIT,
    REMOTE_RUN_SESSION_PREFIX,
    REMOTE_RUNNER_SCRIPT,
    SSHBackend,
    SSHBackendError,
)
from termroom.terminals import (
    TMUX_MANAGED_RUN_OPTION,
    TMUX_TERMINAL_RECORD_FORMAT,
    TMUX_TERMINAL_ROLE_OPTION,
    parse_tmux_terminal_records,
)


class NodeRemoteRunError(SSHBackendError):
    """A typed Node target failure that fits the existing Remote Run boundary."""

    def __init__(self, message: str, *, code: str = "node_remote_run_error") -> None:
        super().__init__(message)
        self.code = code


def _make_sealed_memfd(content: bytes, name: str, *, mode: int = 0o400) -> int:
    flags = getattr(os, "MFD_CLOEXEC", 1) | getattr(os, "MFD_ALLOW_SEALING", 2)
    create_memfd = getattr(os, "memfd_create", None)
    if create_memfd is None:
        try:
            create_memfd = ctypes.CDLL(None, use_errno=True).memfd_create
        except (AttributeError, OSError) as exc:
            raise NodeRemoteRunError(
                "Immutable Remote Run execution descriptors are unavailable",
                code="capability_unsupported",
            ) from exc
        create_memfd.argtypes = (ctypes.c_char_p, ctypes.c_uint)
        create_memfd.restype = ctypes.c_int
        descriptor = create_memfd(name.encode(), flags)
        if descriptor < 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
    else:
        descriptor = create_memfd(name, flags)
    try:
        remaining = memoryview(content)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write to immutable execution descriptor")
            remaining = remaining[written:]
        os.fchmod(descriptor, mode)
        os.lseek(descriptor, 0, os.SEEK_SET)
        seals = (
            getattr(fcntl, "F_SEAL_WRITE", 0x0008)
            | getattr(fcntl, "F_SEAL_GROW", 0x0004)
            | getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
            | getattr(fcntl, "F_SEAL_SEAL", 0x0001)
        )
        fcntl.fcntl(descriptor, getattr(fcntl, "F_ADD_SEALS", 1033), seals)
        if fcntl.fcntl(descriptor, getattr(fcntl, "F_GET_SEALS", 1034)) & seals != seals:
            raise OSError("execution descriptor could not be sealed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _replace_script_once(script: str, source: str, replacement: str) -> str:
    if script.count(source) != 1:
        raise NodeRemoteRunError(
            "Node Remote Run launcher script is incompatible", code="runner_invalid"
        )
    return script.replace(source, replacement, 1)


def _node_remote_run_script() -> str:
    script = _replace_script_once(
        REMOTE_RUNNER_SCRIPT,
        'script_path=${BASH_SOURCE[0]}\n'
        'meta_dir=${script_path%/*}\n'
        'meta_dir=$(CDPATH= cd -- "$meta_dir" && pwd -P) || exit 120\n'
        'run_root=$(CDPATH= cd -- "$meta_dir/.." && pwd -P) || exit 120',
        'meta_dir=${TERMROOM_REMOTE_RUN_META_DIR:?}\n'
        'run_root=${TERMROOM_REMOTE_RUN_ROOT_DIR:?}\n'
        'command_path=${TERMROOM_REMOTE_RUN_COMMAND:?}\n'
        'work_root=${TERMROOM_REMOTE_RUN_WORK_DIR:?}\n'
        'work_dir=${TERMROOM_REMOTE_RUN_CWD_DIR:?}\n'
        'output_path=${TERMROOM_REMOTE_RUN_OUTPUT_FILE:?}',
    )
    script = _replace_script_once(
        script,
        'IFS= read -r cwd_rel < "$meta_dir/cwd" || prepare_failed cwd_invalid',
        'cwd_rel=${TERMROOM_REMOTE_RUN_CWD_REL:?}',
    )
    script = _replace_script_once(
        script,
        'work_root=$(CDPATH= cd -- "$run_root/work" && pwd -P) || prepare_failed work_missing\n'
        'work_dir=$(CDPATH= cd -- "$work_root/$cwd_rel" && pwd -P) || prepare_failed cwd_missing\n'
        'case "$work_dir/" in\n'
        '    "$work_root/"*) ;;\n'
        '    *) prepare_failed cwd_outside ;;\n'
        'esac',
        'test -d "$work_dir" || prepare_failed cwd_missing',
    )
    script = _replace_script_once(
        script,
        'test -f "$meta_dir/command.sh" || prepare_failed command_missing',
        'test -f "$command_path" || prepare_failed command_missing',
    )
    script = script.replace('"$meta_dir/command.sh"', '"$command_path"')
    script = _replace_script_once(
        script,
        ': > "$meta_dir/output.log" || exit 120\n'
        'chmod 0600 "$meta_dir/output.log" || exit 120',
        ':',
    )
    script = script.replace('"$meta_dir/output.log"', '"$output_path"')
    script = _replace_script_once(
        script,
        r'''started_at=$(utc_now)
printf '{"phase":"running","started_at":"%s"}\n' "$started_at" \
    | atomic_record "$meta_dir/state.json" || exit 120''',
        r'''cd -- "$work_dir" || prepare_failed cwd_missing
if test "${TERMROOM_REMOTE_RUN_PIPE:-}" != true || \
    test -z "${TMUX_PANE:-}" || ! command -v tmux >/dev/null 2>&1; then
    prepare_failed output_pipe_unavailable
fi
case "$meta_dir" in
    /proc/self/fd/*) metadata_fd=${meta_dir##*/} ;;
    *) prepare_failed output_pipe_invalid ;;
esac
case "$output_path" in
    /proc/self/fd/*) output_fd=${output_path##*/} ;;
    *) prepare_failed output_pipe_invalid ;;
esac
case "$metadata_fd" in
    ''|*[!0-9]*) prepare_failed output_pipe_invalid ;;
esac
case "$output_fd" in
    ''|*[!0-9]*) prepare_failed output_pipe_invalid ;;
esac
pipe_pid=$BASHPID
metadata_path="/proc/$pipe_pid/fd/$metadata_fd"
output_file="/proc/$pipe_pid/fd/$output_fd"
channel_path=${TERMROOM_REMOTE_RUN_LOG_CHANNEL:?}
channel_write=${TERMROOM_REMOTE_RUN_LOG_CHANNEL_WRITE:?}
case "$channel_path" in
    /proc/self/fd/*) channel_read_fd=${channel_path##*/} ;;
    *) prepare_failed output_pipe_invalid ;;
esac
case "$channel_read_fd" in
    ''|*[!0-9]*) prepare_failed output_pipe_invalid ;;
esac
case "$channel_write" in
    /proc/self/fd/*) channel_fd=${channel_write##*/} ;;
    *) prepare_failed output_pipe_invalid ;;
esac
case "$channel_fd" in
    ''|*[!0-9]*) prepare_failed output_pipe_invalid ;;
esac
printf -v pipe_command \
    '%q -I -c %q %q %q %q' \
    "$TERMROOM_NODE_PYTHON" "$TERMROOM_REMOTE_RUN_LOG_HELPER_CODE" \
    "$metadata_path" "$output_file" "/proc/$pipe_pid/fd/$channel_fd"
if ! tmux pipe-pane -o -t "$TMUX_PANE" "$pipe_command"; then
    prepare_failed output_pipe_unavailable
fi
if ! IFS= read -r -t 5 pipe_ready < "$channel_path" || test "$pipe_ready" != ready; then
    tmux pipe-pane -t "$TMUX_PANE" || true
    prepare_failed output_pipe_unavailable
fi
started_at=$(utc_now)
printf '{"phase":"running","started_at":"%s"}\n' "$started_at" \
    | atomic_record "$meta_dir/state.json" || exit 120''',
    )
    script = _replace_script_once(
        script,
        r'''cd -- "$work_dir" || prepare_failed cwd_missing

status=0
pipe_active=false
if test -n "${TMUX_PANE:-}" && command -v tmux >/dev/null 2>&1; then
    printf -v pipe_command '/bin/bash --noprofile --norc %q' "$meta_dir/log-pipe.sh"
    if tmux pipe-pane -o -t "$TMUX_PANE" "$pipe_command"; then
        pipe_active=true
    fi
fi''',
        'status=0',
    )
    script = _replace_script_once(script, 'rm -f -- "$meta_dir/output-seal.json"', ':')
    drain_start = script.index('if test "$pipe_active" = true; then')
    drain_end = script.index('\nstop_requested=false', drain_start)
    script = script[:drain_start] + r'''(
    exec {channel_fd}>&- {channel_read_fd}<&-
    unset TERMROOM_REMOTE_RUN_LOG_CHANNEL TERMROOM_REMOTE_RUN_LOG_CHANNEL_WRITE
    exec /bin/bash --noprofile --norc -- "$command_path"
) </dev/null 2>&1 || status=$?
tmux pipe-pane -t "$TMUX_PANE" || true
log_incomplete=true
log_size=$(wc -c < "$output_path")
log_size=${log_size//[[:space:]]/}
if IFS= read -r -t 2 pipe_receipt < "$channel_path"; then
    if admitted_size=$("$TERMROOM_NODE_PYTHON" -I -c \
        "$TERMROOM_REMOTE_RUN_LOG_VERIFY_CODE" "$meta_dir" "$output_path" "$pipe_receipt"); then
        log_size=$admitted_size
        log_incomplete=false
    fi
fi
''' + script[drain_end:]
    return script


def _node_git_bootstrap_script() -> str:
    script = _replace_script_once(
        REMOTE_GIT_BOOTSTRAP_SCRIPT,
        'script_path=${BASH_SOURCE[0]}\n'
        'meta_dir=${script_path%/*}\n'
        'meta_dir=$(CDPATH= cd -- "$meta_dir" && pwd -P) || exit 120\n'
        'run_root=$(CDPATH= cd -- "$meta_dir/.." && pwd -P) || exit 120',
        'meta_dir=${TERMROOM_REMOTE_RUN_META_DIR:?}\n'
        'run_root=${TERMROOM_REMOTE_RUN_ROOT_DIR:?}\n'
        'staging_dir=${TERMROOM_REMOTE_RUN_STAGING_DIR:?}\n'
        'git_argv_path=${TERMROOM_REMOTE_RUN_GIT_ARGV:?}\n'
        'git_path_file=${TERMROOM_REMOTE_RUN_GIT_PATH_FILE:?}\n'
        'askpass_path=${TERMROOM_REMOTE_RUN_ASKPASS:?}\n'
        'git_home=${TERMROOM_REMOTE_RUN_GIT_HOME:?}\n'
        'prepare_log=${TERMROOM_REMOTE_RUN_PREPARE_LOG:?}',
    )
    script = _replace_script_once(
        script,
        ': > "$meta_dir/prepare.log" || exit 120\n'
        'chmod 0600 "$meta_dir/prepare.log" || exit 120\n'
        'exec >>"$meta_dir/prepare.log" 2>&1',
        'exec >>"$prepare_log" 2>&1',
    )
    script = _replace_script_once(
        script,
        'done < "$meta_dir/git-argv"',
        'done < "$git_argv_path"',
    )
    script = _replace_script_once(
        script,
        '    clone_argv+=("$value")',
        '    case "$value" in\n'
        '        /__termroom_node_remote_run_work_staging_fd__) value=$staging_dir ;;\n'
        '        /__termroom_node_remote_run_askpass_fd__) value=$askpass_path ;;\n'
        '        /__termroom_node_remote_run_git_home_fd__) value=$git_home ;;\n'
        '    esac\n'
        '    clone_argv+=("$value")',
    )
    script = _replace_script_once(script, 'rm -rf -- "$run_root/work.tmp"', ':')
    script = _replace_script_once(
        script,
        'IFS= read -r git_path < "$meta_dir/git-path" || {',
        'IFS= read -r git_path < "$git_path_file" || {',
    )
    script = _replace_script_once(
        script,
        'revision=$("$git_path" -C "$run_root/work.tmp" rev-parse --verify HEAD 2>/dev/null) || {',
        'revision=$("$git_path" -C "$staging_dir" rev-parse --verify HEAD 2>/dev/null) || {',
    )
    script = _replace_script_once(
        script,
        'rmdir -- "$run_root/work" || { prepare_result failed work_already_committed; exit 120; }\n'
        'mv -- "$run_root/work.tmp" "$run_root/work" || {\n'
        '    mkdir -m 0700 -- "$run_root/work" 2>/dev/null || true\n'
        '    prepare_result failed work_commit_failed\n'
        '    exit 120\n'
        '}',
        ':',
    )
    script = _replace_script_once(
        script,
        'exec /bin/bash --noprofile --norc "$meta_dir/runner.sh"',
        'exec "$TERMROOM_NODE_PYTHON" -c "$TERMROOM_REMOTE_RUN_GIT_FINISH_CODE" '
        '"$run_root" "$staging_dir" "$TERMROOM_REMOTE_RUN_WORK_DIR"',
    )
    return script


_NODE_REMOTE_RUN_GIT_FINISH_CODE = r'''import os, sys
run_path, staging_path, work_path = sys.argv[1:4]
run_fd, staging_fd, work_fd = (
    int(value.rsplit("/", 1)[1]) for value in (run_path, staging_path, work_path)
)
def identity(info):
    return info.st_dev, info.st_ino
if identity(os.stat("work.tmp", dir_fd=run_fd, follow_symlinks=False)) != identity(
    os.fstat(staging_fd)
):
    raise RuntimeError("Remote Run staging directory was replaced")
if identity(os.stat("work", dir_fd=run_fd, follow_symlinks=False)) != identity(os.fstat(work_fd)):
    raise RuntimeError("Remote Run work directory was replaced")
relative = os.environ["TERMROOM_REMOTE_RUN_CWD_REL"]
parts = () if relative == "." else tuple(relative.split("/"))
cwd_fd = os.dup(staging_fd)
try:
    for part in parts:
        if not part or part in {".", ".."} or "/" in part:
            raise RuntimeError("Remote Run working directory is invalid")
        next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cwd_fd)
        os.close(cwd_fd)
        cwd_fd = next_fd
    os.rename("work.tmp", "work", src_dir_fd=run_fd, dst_dir_fd=run_fd)
    if identity(os.stat("work", dir_fd=run_fd, follow_symlinks=False)) != identity(
        os.fstat(staging_fd)
    ):
        raise RuntimeError("Remote Run work promotion failed")
    os.fchdir(cwd_fd)
    os.set_inheritable(cwd_fd, True)
    environment = os.environ.copy()
    environment["TERMROOM_REMOTE_RUN_WORK_DIR"] = staging_path
    environment["TERMROOM_REMOTE_RUN_CWD_DIR"] = "/proc/self/fd/" + str(cwd_fd)
    script = environment["TERMROOM_REMOTE_RUN_RUNNER_SCRIPT"]
    os.execvpe(
        "/bin/bash",
        ["/bin/bash", "--noprofile", "--norc", "-c", script, "termroom-node-remote-run"],
        environment,
    )
finally:
    os.close(cwd_fd)
'''


def _validate_run_id(value: object) -> str:
    return SSHBackend.validate_remote_run_id(str(value or ""))


def _normalize_command(value: object) -> str:
    command = str(value or "")
    if not command.strip():
        raise NodeRemoteRunError("Remote Run command cannot be empty", code="command_required")
    if "\x00" in command:
        raise NodeRemoteRunError("Remote Run command cannot contain NUL", code="command_invalid")
    body = command.rstrip("\n") + "\n"
    if len(body.encode("utf-8")) > 256 * 1024:
        raise NodeRemoteRunError("Remote Run command is too large", code="command_invalid")
    return body


def _open_directory_at(
    directory_fd: int,
    components: Iterable[str],
    *,
    create: bool,
    mode: int | None,
    require_owner: bool = True,
) -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("no-follow directory handles are unavailable")
    descriptor = os.dup(directory_fd)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    values = tuple(components)
    try:
        for index, component in enumerate(values):
            if not component or component in {".", ".."} or "/" in component:
                raise OSError("invalid Remote Run directory component")
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, 0o700, dir_fd=descriptor)
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            if require_owner and os.fstat(descriptor).st_uid != os.geteuid():
                raise OSError("Remote Run directory is not owned by the Node user")
            if index == len(values) - 1 and mode is not None:
                os.fchmod(descriptor, mode)
        if not values:
            if require_owner and os.fstat(descriptor).st_uid != os.geteuid():
                raise OSError("Remote Run directory is not owned by the Node user")
            if mode is not None:
                os.fchmod(descriptor, mode)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _create_directory_handle_at(
    parent_fd: int, name: str, *, mode: int = 0o700
) -> int:
    if not name or name in {".", ".."} or "/" in name:
        raise OSError("invalid Remote Run directory name")
    os.mkdir(name, mode, dir_fd=parent_fd)
    created: os.stat_result | None = None
    descriptor = -1
    try:
        created = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(created.st_mode):
            raise OSError("new Remote Run entry is not a directory")
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        opened = os.fstat(descriptor)
        if (
            (created.st_dev, created.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_uid != os.geteuid()
        ):
            raise OSError("new Remote Run directory was replaced before opening")
        os.fchmod(descriptor, mode)
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != mode:
            raise OSError("new Remote Run directory is invalid")
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise OSError("new Remote Run directory was replaced")
        return descriptor
    except BaseException:
        if created is not None:
            with contextlib.suppress(OSError):
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (created.st_dev, created.st_ino) == (
                    current.st_dev,
                    current.st_ino,
                ):
                    os.rmdir(name, dir_fd=parent_fd)
        if descriptor >= 0:
            os.close(descriptor)
        raise


def _open_directory_components(
    value: Path,
    *,
    create: bool,
    parent_fd: int | None = None,
    parent_path: Path | None = None,
    mode: int | None = 0o700,
) -> tuple[int, Path]:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("no-follow directory handles are unavailable")
    path = Path(os.path.abspath(os.fspath(value.expanduser())))
    if not path.is_absolute() or len(path.parts) < 2:
        raise OSError("Remote Run root must be a non-root absolute directory")
    if parent_fd is not None and parent_path is not None:
        try:
            relative = path.relative_to(parent_path)
        except ValueError:
            pass
        else:
            return (
                _open_directory_at(
                    parent_fd,
                    relative.parts,
                    create=create,
                    mode=mode,
                    require_owner=True,
                ),
                path,
            )

    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        for index, component in enumerate(path.parts[1:]):
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, 0o700, dir_fd=descriptor)
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            if index == len(path.parts) - 2:
                if os.fstat(descriptor).st_uid != os.geteuid():
                    raise OSError("Remote Run root is not owned by the Node user")
                if mode is not None:
                    os.fchmod(descriptor, mode)
        return descriptor, path
    except BaseException:
        os.close(descriptor)
        raise


_PRIVATE_WRITE_UNSPECIFIED = object()


def _private_leaf_state_at(
    directory_fd: int, name: str, *, mode: int
) -> tuple[int, ...] | None:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != mode
    ):
        raise NodeRemoteRunError(
            "Remote Run metadata is invalid", code="metadata_invalid"
        )
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _atomic_private_write_at(
    directory_fd: int,
    name: str,
    content: bytes,
    *,
    mode: int = 0o600,
    expected_state: object = _PRIVATE_WRITE_UNSPECIFIED,
) -> tuple[int, ...]:
    temporary = f".{name}.tmp-{uuid.uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        if expected_state is _PRIVATE_WRITE_UNSPECIFIED:
            expected_state = _private_leaf_state_at(directory_fd, name, mode=mode)
        descriptor = os.open(temporary, flags, mode, dir_fd=directory_fd)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.fchmod(descriptor, mode)
        current_state = _private_leaf_state_at(directory_fd, name, mode=mode)
        if current_state != expected_state:
            raise NodeRemoteRunError(
                "Remote Run metadata changed during write", code="metadata_invalid"
            )
        if expected_state is None:
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise NodeRemoteRunError(
                    "Remote Run metadata appeared during write",
                    code="metadata_invalid",
                ) from exc
            os.unlink(temporary, dir_fd=directory_fd)
        else:
            os.replace(
                temporary,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        published_state = _private_leaf_state_at(directory_fd, name, mode=mode)
        written_info = os.fstat(descriptor)
        if published_state is None or published_state[:2] != (
            written_info.st_dev,
            written_info.st_ino,
        ):
            raise NodeRemoteRunError(
                "Remote Run metadata changed during publication",
                code="metadata_invalid",
            )
        os.fsync(directory_fd)
        return published_state
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)


def _read_regular_at(directory_fd: int, name: str, limit: int) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
        ):
            raise NodeRemoteRunError("Remote Run metadata is invalid", code="metadata_invalid")
        if info.st_size > limit:
            raise NodeRemoteRunError(
                "Remote Run metadata is too large", code="metadata_invalid"
            )
        chunks = bytearray()
        while len(chunks) <= limit:
            chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        if len(chunks) > limit:
            raise NodeRemoteRunError(
                "Remote Run metadata is too large", code="metadata_invalid"
            )
        final = os.fstat(descriptor)
        try:
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise NodeRemoteRunError(
                "Remote Run metadata changed while reading", code="metadata_invalid"
            ) from exc

        def signature(value: os.stat_result) -> tuple[int, ...]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_nlink,
                value.st_uid,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        if (
            signature(info) != signature(final)
            or signature(final) != signature(current)
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or current.st_uid != os.geteuid()
        ):
            raise NodeRemoteRunError(
                "Remote Run metadata changed while reading", code="metadata_invalid"
            )
        return bytes(chunks)
    finally:
        os.close(descriptor)


def _sealed_regular_at(
    directory_fd: int,
    name: str,
    limit: int,
    *,
    mode: int = 0o400,
) -> int:
    content = _read_regular_at(directory_fd, name, limit)
    try:
        return _make_sealed_memfd(content, f"termroom-{name}", mode=mode)
    except OSError as exc:
        raise NodeRemoteRunError(
            "Immutable Remote Run execution descriptors are unavailable",
            code="capability_unsupported",
        ) from exc


def _open_owned_regular_at(
    directory_fd: int, name: str, *, flags: int = os.O_RDONLY
) -> int:
    descriptor = os.open(
        name, flags | os.O_NOFOLLOW, dir_fd=directory_fd
    )
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
    ):
        os.close(descriptor)
        raise NodeRemoteRunError("Remote Run metadata is invalid", code="metadata_invalid")
    return descriptor


def _node_remote_run_output_state(metadata_fd: int, output_fd: int) -> tuple[int, ...]:
    state = _private_leaf_state_at(metadata_fd, "output.log", mode=0o600)
    info = os.fstat(output_fd)
    if state is None or state != (
        info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid,
        info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    ):
        raise NodeRemoteRunError("Remote Run output changed", code="metadata_invalid")
    return state


def _node_remote_run_log_helper(metadata_path: str, output_path: str, channel_path: str) -> int:
    descriptors: list[int] = []
    channel_fd = -1
    try:
        channel_fd = os.open(channel_path, os.O_WRONLY | os.O_NONBLOCK)
        descriptors.append(channel_fd)
        metadata_fd = os.open(metadata_path, os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(metadata_fd)
        info = os.fstat(metadata_fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise NodeRemoteRunError("Remote Run metadata is invalid", code="metadata_invalid")
        output_fd = os.open(output_path, os.O_WRONLY | os.O_APPEND)
        descriptors.append(output_fd)
        _node_remote_run_output_state(metadata_fd, output_fd)
        if _private_leaf_state_at(metadata_fd, "output-seal.json", mode=0o600) is not None:
            raise NodeRemoteRunError("Remote Run seal already exists", code="metadata_invalid")
        os.write(channel_fd, b"ready\n")
        while chunk := os.read(0, 64 * 1024):
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(output_fd, remaining)
                if written <= 0:
                    raise OSError("short write to Remote Run output")
                remaining = remaining[written:]
        os.fsync(output_fd)
        output_state = _node_remote_run_output_state(metadata_fd, output_fd)
        content = json.dumps({
            "size": output_state[5],
            "sealed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }).encode() + b"\n"
        seal_state = _atomic_private_write_at(
            metadata_fd, "output-seal.json", content, expected_state=None,
        )
        os.write(channel_fd, b"sealed " + json.dumps(seal_state).encode() + b"\n")
        return 0
    except (OSError, NodeRemoteRunError):
        if channel_fd >= 0:
            with contextlib.suppress(OSError):
                os.write(channel_fd, b"failed\n")
        return 120
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _node_remote_run_log_size(metadata_path: str, output_path: str, receipt: str) -> int:
    if not receipt.startswith("sealed "):
        raise NodeRemoteRunError("Remote Run output did not drain", code="metadata_invalid")
    expected = tuple(json.loads(receipt.removeprefix("sealed ")))
    metadata_fd = os.open(metadata_path, os.O_RDONLY | os.O_DIRECTORY)
    seal_fd = output_fd = -1
    try:
        # NONBLOCK prevents a raced FIFO from hanging admission; never read a
        # leaf until its no-follow descriptor matches the helper's receipt.
        seal_fd = os.open(
            "output-seal.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=metadata_fd,
        )
        info = os.fstat(seal_fd)
        if (
            _private_leaf_state_at(metadata_fd, "output-seal.json", mode=0o600) != expected
            or expected[:2] != (info.st_dev, info.st_ino)
            or not stat.S_ISREG(info.st_mode)
            or not 0 < info.st_size <= 4096
        ):
            raise NodeRemoteRunError("Remote Run seal changed", code="metadata_invalid")
        seal = json.loads(os.read(seal_fd, 4097))
        output_fd = os.open(output_path, os.O_WRONLY | os.O_APPEND)
        output_state = _node_remote_run_output_state(metadata_fd, output_fd)
        if (
            set(seal) != {"size", "sealed_at"}
            or type(seal["size"]) is not int
            or seal["size"] != output_state[5]
            or not isinstance(seal["sealed_at"], str)
            or not seal["sealed_at"]
            or _private_leaf_state_at(metadata_fd, "output-seal.json", mode=0o600) != expected
        ):
            raise NodeRemoteRunError("Remote Run seal is incomplete", code="metadata_invalid")
        return seal["size"]
    finally:
        for descriptor in (seal_fd, output_fd, metadata_fd):
            if descriptor >= 0:
                os.close(descriptor)


_NODE_REMOTE_RUN_LOG_HELPER_CODE = (
    "import sys; from termroom.node_remote_runs import _node_remote_run_log_helper; "
    "sys.exit(_node_remote_run_log_helper(*sys.argv[1:]))"
)
_NODE_REMOTE_RUN_LOG_VERIFY_CODE = '''import sys
from termroom.node_remote_runs import _node_remote_run_log_size
try:
    print(_node_remote_run_log_size(*sys.argv[1:]))
except Exception:
    sys.exit(120)
'''


def _remove_directory_contents(directory_fd: int) -> None:
    for entry in os.listdir(directory_fd):
        info = os.stat(entry, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child_fd = os.open(
                entry,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                _remove_directory_contents(child_fd)
                expected = os.fstat(child_fd)
                current = os.stat(entry, dir_fd=directory_fd, follow_symlinks=False)
                if (expected.st_dev, expected.st_ino) != (current.st_dev, current.st_ino):
                    raise OSError("Remote Run child directory was replaced")
                os.rmdir(entry, dir_fd=directory_fd)
            finally:
                os.close(child_fd)
        else:
            os.unlink(entry, dir_fd=directory_fd)


def _copy_snapshot_tree(source_fd: int, destination_fd: int, parent: str = "") -> None:
    def signature(info: os.stat_result) -> tuple[int, ...]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_uid,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    for name in os.listdir(source_fd):
        entry = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        relative = f"{parent}/{name}" if parent else name
        normalize_source_relative_path(relative)
        if stat.S_ISDIR(entry.st_mode):
            source_child = _open_directory_at(
                source_fd, (name,), create=False, mode=None, require_owner=False
            )
            try:
                opened = os.fstat(source_child)
                if (
                    signature(entry) != signature(opened)
                    or opened.st_uid != os.geteuid()
                    or stat.S_IMODE(opened.st_mode) != 0o700
                ):
                    raise NodeRemoteRunError(
                        "Remote Run snapshot directory changed", code="layout_invalid"
                    )
                os.mkdir(name, 0o700, dir_fd=destination_fd)
                destination_child = _open_directory_at(
                    destination_fd, (name,), create=False, mode=None
                )
                try:
                    _copy_snapshot_tree(source_child, destination_child, relative)
                finally:
                    os.close(destination_child)
                current = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
                if signature(opened) != signature(current):
                    raise NodeRemoteRunError(
                        "Remote Run snapshot directory changed", code="layout_invalid"
                    )
            finally:
                os.close(source_child)
        elif stat.S_ISREG(entry.st_mode):
            source_file = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_fd
            )
            destination_file = -1
            try:
                before = os.fstat(source_file)
                mode = 0o700 if before.st_mode & 0o111 else 0o600
                if (
                    signature(entry) != signature(before)
                    or before.st_uid != os.geteuid()
                    or before.st_nlink != 1
                    or stat.S_IMODE(before.st_mode) != mode
                ):
                    raise NodeRemoteRunError(
                        "Remote Run snapshot file changed", code="layout_invalid"
                    )
                destination_file = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    mode,
                    dir_fd=destination_fd,
                )
                while True:
                    chunk = os.read(source_file, 1024 * 1024)
                    if not chunk:
                        break
                    view = memoryview(chunk)
                    while view:
                        written = os.write(destination_file, view)
                        if written <= 0:
                            raise OSError("short write while copying Remote Run snapshot")
                        view = view[written:]
                after = os.fstat(source_file)
                current = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
                if signature(before) != signature(after) or signature(after) != signature(current):
                    raise NodeRemoteRunError(
                        "Remote Run snapshot file changed", code="layout_invalid"
                    )
                os.fchmod(destination_file, mode)
            finally:
                os.close(source_file)
                if destination_file >= 0:
                    os.close(destination_file)
        elif stat.S_ISLNK(entry.st_mode):
            target = os.readlink(name, dir_fd=source_fd)
            current = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            if signature(entry) != signature(current):
                raise NodeRemoteRunError(
                    "Remote Run snapshot symlink changed", code="layout_invalid"
                )
            try:
                validate_contained_symlink_target(relative, target)
            except SourceValidationError as exc:
                raise NodeRemoteRunError(
                    "Remote Run snapshot symlink is invalid", code="layout_invalid"
                ) from exc
            os.symlink(target, name, dir_fd=destination_fd)
        else:
            raise NodeRemoteRunError(
                "Remote Run snapshot contains an unsupported file", code="layout_invalid"
            )


def _remove_tree_at(
    parent_fd: int,
    name: str,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    directory_fd = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(directory_fd)
        if expected_identity is not None and (
            opened.st_dev,
            opened.st_ino,
        ) != expected_identity:
            raise OSError("Remote Run cleanup directory was replaced")
        _remove_directory_contents(directory_fd)
        expected = os.fstat(directory_fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (expected.st_dev, expected.st_ino) != (current.st_dev, current.st_ino):
            raise OSError("Remote Run cleanup directory was replaced")
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)


class NodeRemoteRunUploadStream:
    def __init__(
        self,
        stream_id: str,
        parent_fd: int,
        target_name: str,
        *,
        expected_size: int,
        executable: bool,
        registry: dict[str, Any],
    ) -> None:
        self.stream_id = stream_id
        self.parent_fd = os.dup(parent_fd)
        self.target_name = target_name
        self.expected_size = expected_size
        self.executable = executable
        self.registry = registry
        self.temporary_name = f".{target_name}.upload-{stream_id}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            self.descriptor = os.open(
                self.temporary_name, flags, 0o600, dir_fd=self.parent_fd
            )
        except BaseException:
            os.close(self.parent_fd)
            self.parent_fd = -1
            raise
        self.total = 0
        self.closed = False

    async def feed(self, chunk: bytes) -> None:
        if self.closed:
            raise NodeRemoteRunError("Remote Run Source stream is closed", code="stream_closed")
        self.total += len(chunk)
        if self.total > self.expected_size:
            await self.abort()
            raise NodeRemoteRunError(
                "Remote Run Source file exceeds its declared size",
                code="source_file_changed",
            )
        view = memoryview(chunk)
        while view:
            written = os.write(self.descriptor, view)
            view = view[written:]

    async def control(self, kind: str, values: Mapping[str, Any]) -> None:
        del kind, values

    async def close(self) -> dict[str, Any]:
        if self.closed:
            raise NodeRemoteRunError("Remote Run Source stream is closed", code="stream_closed")
        self.closed = True
        self.registry.pop(self.stream_id, None)
        try:
            if self.total != self.expected_size:
                raise NodeRemoteRunError(
                    "Remote Run Source file size changed during transfer",
                    code="source_file_changed",
                )
            os.fchmod(self.descriptor, 0o700 if self.executable else 0o600)
            os.fsync(self.descriptor)
            os.close(self.descriptor)
            self.descriptor = -1
            try:
                os.stat(self.target_name, dir_fd=self.parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise NodeRemoteRunError(
                    "Remote Run Source path already exists",
                    code="source_path_conflict",
                )
            os.replace(
                self.temporary_name,
                self.target_name,
                src_dir_fd=self.parent_fd,
                dst_dir_fd=self.parent_fd,
            )
            return {"size": self.total}
        except BaseException:
            if self.descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(self.descriptor)
                self.descriptor = -1
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.temporary_name, dir_fd=self.parent_fd)
            raise
        finally:
            if self.closed and self.parent_fd >= 0:
                os.close(self.parent_fd)
                self.parent_fd = -1

    async def abort(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.registry.pop(self.stream_id, None)
        if self.descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(self.descriptor)
            self.descriptor = -1
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.temporary_name, dir_fd=self.parent_fd)
        if self.parent_fd >= 0:
            os.close(self.parent_fd)
            self.parent_fd = -1


class NodeRemoteRunMetadataStream:
    def __init__(
        self,
        stream_id: str,
        parent_fd: int,
        target_name: str,
        *,
        expected_size: int,
        commit: Callable[[bytes], None],
        registry: dict[str, Any],
    ) -> None:
        self.stream_id = stream_id
        self.parent_fd = os.dup(parent_fd)
        self.target_name = target_name
        self.expected_size = expected_size
        self.commit = commit
        self.registry = registry
        self.temporary_name = f".{target_name}.upload-{stream_id}"
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            self.descriptor = os.open(
                self.temporary_name, flags, 0o600, dir_fd=self.parent_fd
            )
        except BaseException:
            os.close(self.parent_fd)
            self.parent_fd = -1
            raise
        self.total = 0
        self.closed = False

    async def feed(self, chunk: bytes) -> None:
        if self.closed:
            raise NodeRemoteRunError("Remote Run metadata stream is closed", code="stream_closed")
        self.total += len(chunk)
        if self.total > self.expected_size:
            await self.abort()
            raise NodeRemoteRunError(
                "Remote Run metadata exceeds its declared size", code="metadata_invalid"
            )
        view = memoryview(chunk)
        while view:
            written = os.write(self.descriptor, view)
            view = view[written:]

    async def control(self, kind: str, values: Mapping[str, Any]) -> None:
        del kind, values

    async def close(self) -> dict[str, Any]:
        if self.closed:
            raise NodeRemoteRunError("Remote Run metadata stream is closed", code="stream_closed")
        self.closed = True
        self.registry.pop(self.stream_id, None)
        try:
            if self.total != self.expected_size:
                raise NodeRemoteRunError(
                    "Remote Run metadata size changed during transfer",
                    code="metadata_invalid",
                )
            info = os.fstat(self.descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or info.st_size != self.expected_size
            ):
                raise NodeRemoteRunError(
                    "Remote Run metadata file is invalid", code="metadata_invalid"
                )
            os.fsync(self.descriptor)
            os.lseek(self.descriptor, 0, os.SEEK_SET)
            content = bytearray()
            while len(content) <= self.expected_size:
                chunk = os.read(
                    self.descriptor,
                    min(64 * 1024, self.expected_size + 1 - len(content)),
                )
                if not chunk:
                    break
                content.extend(chunk)
            if len(content) != self.expected_size:
                raise NodeRemoteRunError(
                    "Remote Run metadata size changed during transfer",
                    code="metadata_invalid",
                )
            self.commit(bytes(content))
            os.close(self.descriptor)
            self.descriptor = -1
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.temporary_name, dir_fd=self.parent_fd)
            return {"size": self.total}
        except BaseException:
            if self.descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(self.descriptor)
                self.descriptor = -1
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.temporary_name, dir_fd=self.parent_fd)
            raise
        finally:
            if self.parent_fd >= 0:
                os.close(self.parent_fd)
                self.parent_fd = -1

    async def abort(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.registry.pop(self.stream_id, None)
        if self.descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(self.descriptor)
            self.descriptor = -1
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.temporary_name, dir_fd=self.parent_fd)
        if self.parent_fd >= 0:
            os.close(self.parent_fd)
            self.parent_fd = -1


class NodeRemoteRunRuntime:
    """Node-local implementation of the fixed Remote Run operations."""

    def __init__(
        self,
        run_root: Path,
        *,
        parent_fd: int | None = None,
        parent_path: Path | None = None,
        descriptor_handoff: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        try:
            proc_fd = os.open("/proc/self/fd", os.O_RDONLY | os.O_DIRECTORY)
        except OSError as exc:
            raise NodeRemoteRunError(
                "Node Remote Run directory handles are unavailable", code="run_root_invalid"
            ) from exc
        else:
            os.close(proc_fd)
        self._root_fd, self.run_root = self._prepare_run_root(
            run_root, parent_fd=parent_fd, parent_path=parent_path
        )
        self._root_path = Path(f"/proc/self/fd/{self._root_fd}")
        if not self._root_path.is_dir():
            os.close(self._root_fd)
            raise NodeRemoteRunError(
                "Node Remote Run directory handles are unavailable", code="run_root_invalid"
            )
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._run_dir_fds: dict[str, int] = {}
        self._metadata_dir_fds: dict[str, int] = {}
        self._work_dir_fds: dict[str, int] = {}
        self._work_staging_dir_fds: dict[str, int] = {}
        self._work_identity_states: dict[tuple[int, int], tuple[int, ...] | None] = {}
        self._invalid_run_ids: set[str] = set()
        self._descriptor_handoff = descriptor_handoff

    @staticmethod
    def _prepare_run_root(
        value: Path,
        *,
        parent_fd: int | None = None,
        parent_path: Path | None = None,
    ) -> tuple[int, Path]:
        candidate = value.expanduser()
        if not candidate.is_absolute():
            raise NodeRemoteRunError(
                "Node Remote Run root must be absolute", code="run_root_invalid"
            )
        descriptor = -1
        try:
            descriptor, candidate = _open_directory_components(
                candidate,
                create=True,
                parent_fd=parent_fd,
                parent_path=parent_path,
            )
            current, _ = _open_directory_components(candidate, create=False, mode=None)
            try:
                expected = os.fstat(descriptor)
                actual = os.fstat(current)
                if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
                    raise OSError("Remote Run root was replaced during initialization")
            finally:
                os.close(current)
        except (OSError, RuntimeError) as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise NodeRemoteRunError(
                "Node Remote Run root is unavailable", code="run_root_invalid"
            ) from exc
        return descriptor, candidate

    def _assert_root_path_matches(self) -> None:
        current = -1
        try:
            current, _ = _open_directory_components(
                self.run_root, create=False, mode=None
            )
            expected = os.fstat(self._root_fd)
            actual = os.fstat(current)
            if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
                raise NodeRemoteRunError(
                    "Node Remote Run root was replaced", code="run_root_invalid"
                )
        except OSError as exc:
            raise NodeRemoteRunError(
                "Node Remote Run root is unavailable", code="run_root_invalid"
            ) from exc
        finally:
            if current >= 0:
                os.close(current)

    def lock_for(self, run_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(_validate_run_id(run_id), threading.RLock())

    def preflight(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._require_version(payload)
        self._assert_root_path_matches()
        bash = Path("/bin/bash")
        tmux = shutil.which("tmux")
        git = shutil.which("git") if payload.get("require_git") is True else None
        if not bash.is_file() or not os.access(bash, os.X_OK):
            raise NodeRemoteRunError("Node does not provide /bin/bash", code="bash_missing")
        if not tmux:
            raise NodeRemoteRunError("Node does not provide tmux", code="tmux_missing")
        if payload.get("require_git") is True and not git:
            raise NodeRemoteRunError("Node does not provide Git", code="git_missing")
        probe = f".termroom-probe-{uuid.uuid4()}"
        renamed = probe + ".renamed"
        probe_created = False
        renamed_created = False
        try:
            os.mkdir(probe, 0o700, dir_fd=self._root_fd)
            probe_created = True
            os.rename(probe, renamed, src_dir_fd=self._root_fd, dst_dir_fd=self._root_fd)
            probe_created = False
            renamed_created = True
            os.rmdir(renamed, dir_fd=self._root_fd)
            renamed_created = False
        except OSError as exc:
            if probe_created:
                with contextlib.suppress(OSError):
                    os.rmdir(probe, dir_fd=self._root_fd)
            if renamed_created:
                with contextlib.suppress(OSError):
                    os.rmdir(renamed, dir_fd=self._root_fd)
            raise NodeRemoteRunError(
                "Node Remote Run root is not writable", code="run_root_unwritable"
            ) from exc
        usage = shutil.disk_usage(self._root_path)
        tools = {"bash": str(bash), "tmux": str(Path(tmux).resolve())}
        if git:
            tools["git"] = str(Path(git).resolve())
        return {
            "remote_run_version": NODE_REMOTE_RUN_VERSION,
            "run_base": str(self.run_root),
            "tools": tools,
            "available_bytes": usage.free,
            "warnings": [],
        }

    def create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        command = _normalize_command(payload.get("command"))
        cwd_rel = validate_cwd_rel(str(payload.get("cwd_rel") or "."))
        with self.lock_for(run_id):
            if run_id in self._invalid_run_ids:
                raise NodeRemoteRunError(
                    "Remote Run directory was replaced", code="layout_invalid"
                )
            self._finish_interrupted_delete(run_id)
            paths = self._paths(run_id)
            if self._path_exists(paths["root"]):
                self._assert_layout(run_id)
                if self._read_regular(paths["command"], 256 * 1024) != command.encode():
                    raise NodeRemoteRunError(
                        "Remote Run id already belongs to a different command",
                        code="idempotency_conflict",
                    )
                if self._read_regular(paths["cwd"], 4096).decode().strip() != cwd_rel:
                    raise NodeRemoteRunError(
                        "Remote Run id already belongs to a different working directory",
                        code="idempotency_conflict",
                    )
                return self._layout_payload(run_id)

            creating = self._paths(run_id, leaf=f".termroom-creating-{run_id}")
            if self._path_exists(creating["root"]):
                self._remove_owned_tree(creating["root"], run_id, allow_missing_marker=True)
            creating_run_fd = metadata_fd = work_fd = -1
            publication_started = False
            try:
                creating_run_fd = _create_directory_handle_at(
                    self._root_fd, creating["root"].name
                )
                metadata_fd = _create_directory_handle_at(
                    creating_run_fd, ".termroom"
                )
                _atomic_private_write_at(
                    metadata_fd, "marker", (run_id + "\n").encode(), mode=0o600,
                    expected_state=None,
                )
                work_fd = _create_directory_handle_at(
                    creating_run_fd, "work"
                )
                _atomic_private_write_at(
                    metadata_fd, "cwd", (cwd_rel + "\n").encode(), mode=0o600,
                    expected_state=None,
                )
                _atomic_private_write_at(
                    metadata_fd, "runner.sh", REMOTE_RUNNER_SCRIPT.encode(), mode=0o700,
                    expected_state=None,
                )
                _atomic_private_write_at(
                    metadata_fd,
                    "log-pipe.sh",
                    REMOTE_RUN_LOG_PIPE_SCRIPT.encode(),
                    mode=0o700,
                    expected_state=None,
                )
                _atomic_private_write_at(
                    metadata_fd, "command.sh", command.encode(), mode=0o600,
                    expected_state=None,
                )
                self._write_work_identity(
                    metadata_fd, work_fd, "stable", expected_state=None
                )
                publication_started = True
                self._replace(creating["root"], paths["root"])
                published_fd = -1
                try:
                    published_fd = _open_directory_at(
                        self._root_fd, (run_id,), create=False, mode=None
                    )
                    expected = os.fstat(creating_run_fd)
                    observed = os.fstat(published_fd)
                except OSError as exc:
                    self._invalid_run_ids.add(run_id)
                    raise NodeRemoteRunError(
                        "Remote Run directory changed during publication",
                        code="layout_invalid",
                    ) from exc
                finally:
                    if published_fd >= 0:
                        os.close(published_fd)
                if (expected.st_dev, expected.st_ino) != (
                    observed.st_dev,
                    observed.st_ino,
                ):
                    self._invalid_run_ids.add(run_id)
                    raise NodeRemoteRunError(
                        "Remote Run directory changed during publication",
                        code="layout_invalid",
                    )
                self._run_dir_fds[run_id] = creating_run_fd
                creating_run_fd = -1
                self._metadata_dir_fds[run_id] = metadata_fd
                metadata_fd = -1
                self._work_dir_fds[run_id] = work_fd
                work_fd = -1
                self._assert_layout(run_id)
            except BaseException as exc:
                if publication_started or isinstance(exc, OSError):
                    self._invalid_run_ids.add(run_id)
                self._forget_layout_handles(run_id)
                if self._path_exists(creating["root"]):
                    self._remove_owned_tree(
                        creating["root"], run_id, allow_missing_marker=True
                    )
                if isinstance(exc, OSError):
                    raise NodeRemoteRunError(
                        "Remote Run directory initialization failed",
                        code="layout_invalid",
                    ) from exc
                raise
            finally:
                for descriptor in (creating_run_fd, metadata_fd, work_fd):
                    if descriptor >= 0:
                        os.close(descriptor)
            return self._layout_payload(run_id)

    def snapshot_begin(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        with self.lock_for(run_id):
            paths = self._assert_layout(run_id)
            self._assert_not_started(run_id, paths)
            staging = paths["work_staging"]
            if self._path_exists(staging):
                self._remove_staging(staging, run_id)
            try:
                staging_fd = _create_directory_handle_at(
                    self._run_dir_fds[run_id], "work.tmp"
                )
            except OSError as exc:
                self._invalid_run_ids.add(run_id)
                raise NodeRemoteRunError(
                    "Remote Run staging directory was replaced",
                    code="layout_invalid",
                ) from exc
            self._work_staging_dir_fds[run_id] = staging_fd
            try:
                self._assert_layout(run_id)
            except BaseException:
                self._invalid_run_ids.add(run_id)
                self._work_staging_dir_fds.pop(run_id, None)
                os.close(staging_fd)
                raise
            return {"ready": True}

    def snapshot_directory(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        relative = normalize_source_relative_path(str(payload.get("path") or ""))
        with self.lock_for(run_id):
            self._assert_not_started(run_id, self._assert_layout(run_id))
            target = self._staging_target(run_id, relative)
            self._create_directory(target)
            return {"path": relative}

    def snapshot_symlink(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        relative = normalize_source_relative_path(str(payload.get("path") or ""))
        link_target = validate_contained_symlink_target(
            relative, str(payload.get("link_target") or "")
        )
        with self.lock_for(run_id):
            self._assert_not_started(run_id, self._assert_layout(run_id))
            target = self._staging_target(run_id, relative)
            parent_fd, name = self._open_parent(target)
            try:
                os.symlink(link_target, name, dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
            return {"path": relative}

    def snapshot_file_open(
        self,
        payload: Mapping[str, Any],
        registry: dict[str, Any],
    ) -> NodeRemoteRunUploadStream:
        run_id = self._identity(payload)
        relative = normalize_source_relative_path(str(payload.get("path") or ""))
        stream_id = validate_request_id(str(payload.get("stream_id") or ""))
        try:
            expected_size = int(payload.get("expected_size"))
        except (TypeError, ValueError) as exc:
            raise NodeRemoteRunError(
                "Remote Run Source size is invalid", code="source_entry_metadata"
            ) from exc
        if expected_size < 0:
            raise NodeRemoteRunError(
                "Remote Run Source size is invalid", code="source_entry_metadata"
            )
        with self.lock_for(run_id):
            self._assert_not_started(run_id, self._assert_layout(run_id))
            target = self._staging_target(run_id, relative)
            parent_fd, name = self._open_parent(target)
            try:
                return NodeRemoteRunUploadStream(
                    stream_id,
                    parent_fd,
                    name,
                    expected_size=expected_size,
                    executable=payload.get("executable") is True,
                    registry=registry,
                )
            finally:
                os.close(parent_fd)

    def snapshot_commit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        with self.lock_for(run_id):
            paths = self._assert_layout(run_id)
            self._assert_not_started(run_id, paths)
            run_fd, metadata_fd = self._layout_handles(run_id)
            staging_fd = self._work_staging_dir_fds.get(run_id)
            if staging_fd is None:
                staging_fd = self._open_directory(paths["work_staging"])
                self._work_staging_dir_fds[run_id] = staging_fd
                self._assert_layout(run_id)
            work_fd = self._work_dir_fds[run_id]
            has_entries = bool(os.listdir(work_fd))
            if has_entries:
                raise NodeRemoteRunError(
                    "Remote Run work directory is already committed",
                    code="work_already_committed",
                )
            staging_info = os.fstat(staging_fd)
            staging_entry = os.stat(
                "work.tmp", dir_fd=run_fd, follow_symlinks=False
            )
            if (
                not stat.S_ISDIR(staging_entry.st_mode)
                or (staging_info.st_dev, staging_info.st_ino)
                != (staging_entry.st_dev, staging_entry.st_ino)
            ):
                raise NodeRemoteRunError(
                    "Remote Run staging directory was replaced", code="layout_invalid"
                )
            try:
                self._write_work_identity(
                    metadata_fd,
                    self._work_dir_fds[run_id],
                    "snapshot-pending",
                    staging_fd=staging_fd,
                )
                _copy_snapshot_tree(staging_fd, self._work_dir_fds[run_id])
                self._assert_layout(run_id)
                self._remove_staging(paths["work_staging"], run_id)
                self._write_work_identity(
                    metadata_fd, self._work_dir_fds[run_id], "stable"
                )
            except BaseException:
                with contextlib.suppress(OSError):
                    _remove_directory_contents(self._work_dir_fds[run_id])
                    self._write_work_identity(
                        metadata_fd, self._work_dir_fds[run_id], "stable"
                    )
                raise
            return {"work_path": str(self._display_path(paths["work"]))}

    def write_metadata(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        name = str(payload.get("name") or "")
        value = payload.get("value")
        encoded = self._encode_metadata(name, value)
        with self.lock_for(run_id):
            self._commit_metadata(run_id, name, encoded)
            return {"written": True}

    def metadata_open(
        self,
        payload: Mapping[str, Any],
        registry: dict[str, Any],
    ) -> NodeRemoteRunMetadataStream:
        run_id = self._identity(payload)
        name = self._metadata_name(payload.get("name"))
        stream_id = validate_request_id(str(payload.get("stream_id") or ""))
        try:
            expected_size = int(payload.get("expected_size"))
        except (TypeError, ValueError) as exc:
            raise NodeRemoteRunError(
                "Remote Run metadata size is invalid", code="metadata_invalid"
            ) from exc
        if expected_size < 0 or expected_size > 8 * 1024 * 1024:
            raise NodeRemoteRunError(
                "Remote Run metadata size is invalid", code="metadata_invalid"
            )
        with self.lock_for(run_id):
            paths = self._assert_layout(run_id)
            self._assert_not_started(run_id, paths)
            target = paths["metadata"] / name
            parent_fd, target_name = self._open_parent(target)
            try:
                return NodeRemoteRunMetadataStream(
                    stream_id,
                    parent_fd,
                    target_name,
                    expected_size=expected_size,
                    commit=lambda content: self._commit_metadata(run_id, name, content),
                    registry=registry,
                )
            finally:
                os.close(parent_fd)

    def start(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        with self.lock_for(run_id):
            paths = self._assert_layout(run_id)
            existing = self._existing_start(run_id, paths)
            if existing is not None:
                return existing
            self._start_tmux(run_id, mode="run")
            return {
                "state": "preparing",
                "session_name": self._session_name(run_id),
                "run_root": str(self._display_path(paths["root"])),
            }

    def start_git(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        url = validate_public_https_git_url(str(payload.get("url") or ""))
        with self.lock_for(run_id):
            paths = self._assert_layout(run_id)
            existing = self._existing_start(run_id, paths)
            if existing is not None:
                stored_url = self._read_regular(paths["git_url"], 64 * 1024).decode().strip()
                if stored_url != url:
                    raise NodeRemoteRunError(
                        "Remote Run id already belongs to a different Git Source",
                        code="idempotency_conflict",
                    )
                return existing
            git = shutil.which("git")
            if not git:
                raise NodeRemoteRunError("Node does not provide Git", code="git_missing")
            invocation = build_public_git_clone_invocation(
                url,
                git_path=str(Path(git).resolve()),
                askpass_path="/__termroom_node_remote_run_askpass_fd__",
                empty_home="/__termroom_node_remote_run_git_home_fd__",
                destination="/__termroom_node_remote_run_work_staging_fd__",
            )
            argv = invocation.as_env_i_argv()
            encoded_argv = b"\x00".join(item.encode() for item in argv) + b"\x00"
            self._create_directory(paths["git_home"])
            self._write_private(
                paths["git_askpass"], b"#!/bin/sh\nexit 1\n", mode=0o700,
                expected_state=None,
            )
            self._write_private(paths["git_argv"], encoded_argv, expected_state=None)
            self._write_private(
                paths["git_url"], (url + "\n").encode(), expected_state=None
            )
            self._write_private(
                paths["git_path"], (str(Path(git).resolve()) + "\n").encode(),
                expected_state=None,
            )
            self._write_private(
                paths["git_bootstrap"], REMOTE_GIT_BOOTSTRAP_SCRIPT.encode(),
                mode=0o700, expected_state=None,
            )
            staging_fd = self._create_git_work_staging(run_id)
            self._work_staging_dir_fds[run_id] = staging_fd
            try:
                self._set_work_identity_state(
                    run_id, "git-pending", staging_fd=staging_fd
                )
                self._start_tmux(run_id, mode="git")
            except BaseException:
                self._discard_git_work_staging(run_id)
                raise
            return {
                "state": "preparing",
                "phase": "cloning",
                "session_name": self._session_name(run_id),
                "run_root": str(self._display_path(paths["root"])),
                "source_url": url,
            }

    def observe(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        with self.lock_for(run_id):
            try:
                paths = self._assert_layout(run_id)
            except FileNotFoundError:
                return self._unavailable_layout(run_id, missing=True)
            except NodeRemoteRunError as exc:
                return self._unavailable_layout(run_id, error=exc.code)
            return self._reconcile(run_id, paths)

    def poll(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = self.observe(payload)
        stream = str(payload.get("stream") or "command")
        offset_value = payload.get("offset")
        offset = None if offset_value is None else int(offset_value)
        limit = int(payload.get("limit") or REMOTE_RUN_LOG_READ_LIMIT)
        run_id = _validate_run_id(payload.get("run_id"))
        if result.get("layout_missing") or result.get("layout_error"):
            result["log"] = self._empty_log(stream, offset)
            return result
        with self.lock_for(run_id):
            paths = self._assert_layout(run_id)
            result["log"] = self._read_log(paths, stream=stream, offset=offset, limit=limit)
            return result

    def interrupt(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        with self.lock_for(run_id):
            try:
                paths = self._assert_layout(run_id)
            except FileNotFoundError:
                status = self._tmux_status(run_id)
                return {
                    "sent": False,
                    "completed": False,
                    "layout_missing": True,
                    "tmux_exists": status["exists"],
                    "tmux_running": status["running"],
                }
            completion = self._read_json_record(paths["completion"])
            if self._valid_completion(completion):
                return {"sent": False, "completed": True}
            self._publish_stop(paths)
            sent = False
            if self._owned_run_window(run_id):
                result = self._tmux(
                    "send-keys", "-t", f"{self._session_name(run_id)}:run.0", "C-c", check=False
                )
                sent = result.returncode == 0
            return {"sent": sent, "completed": False}

    def kill(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        with self.lock_for(run_id):
            try:
                paths = self._assert_layout(run_id)
            except FileNotFoundError:
                status = self._tmux_status(run_id)
                killed = status["exists"] and self._kill_session(run_id)
                return {
                    "killed": killed,
                    "completed": False,
                    "layout_missing": True,
                    "tmux_exists": status["exists"],
                    "tmux_running": status["running"],
                }
            completion = self._read_json_record(paths["completion"])
            if self._valid_completion(completion):
                return {"killed": False, "completed": True}
            self._publish_stop(paths)
            killed = self._kill_session(run_id)
            completion = self._read_json_record(paths["completion"])
            if self._valid_completion(completion):
                return {"killed": False, "completed": True}
            return {"killed": killed, "completed": False}

    def exists(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        with self.lock_for(run_id):
            try:
                self._assert_layout(run_id)
            except FileNotFoundError:
                return {"exists": False}
            return {"exists": True}

    def ensure_shell(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        allow_create = payload.get("allow_create_session") is True
        with self.lock_for(run_id):
            paths = self._assert_layout(run_id)
            work = self._real_directory(paths["work"], "Remote Run work")
            session = self._session_name(run_id)
            status = self._tmux_status(run_id)
            created_session = False
            if not status["exists"]:
                if not allow_create:
                    raise NodeRemoteRunError(
                        "Remote Run tmux session is missing; "
                        "terminal state is required to recreate it",
                        code="session_missing",
                    )
                work_fd = os.dup(self._work_dir_fds[run_id])
                try:
                    self._handoff_tmux(
                        ("new-session", "-d", "-s", session, "-c", "/", "-n", "shell"),
                        (work_fd,),
                        rollback=lambda _result: self._tmux(
                            "kill-session", "-t", session, check=False
                        ),
                    )
                finally:
                    os.close(work_fd)
                self._tmux(
                    "set-option", "-t", session, "@termroom_remote_run_id", run_id
                )
                created_session = True
            elif not self._session_owned(run_id):
                records = self._terminal_records(session)
                legacy_owned = any(
                    item.get("role") == "remote_run"
                    and item.get("managed_run_id") == run_id
                    for item in records
                )
                if not legacy_owned:
                    raise NodeRemoteRunError(
                        "Remote Run session identity is already in use",
                        code="session_identity_conflict",
                    )
                self._tmux(
                    "set-option", "-t", session, "@termroom_remote_run_id", run_id
                )
            records = self._terminal_records(session)
            shell = next((item for item in records if item.get("role") == "shell"), None)
            if shell is None:
                work_fd = os.dup(self._work_dir_fds[run_id])
                try:
                    result = self._handoff_tmux(
                        (
                            "new-window", "-d", "-P", "-F", "#{window_id}",
                            "-t", session, "-c", "/", "-n", "shell",
                        ),
                        (work_fd,),
                        rollback=lambda created: self._tmux(
                            "kill-window", "-t", created.stdout.strip(), check=False
                        ),
                    )
                finally:
                    os.close(work_fd)
                window = result.stdout.strip()
                records = self._terminal_records(session)
                shell = next(item for item in records if item["tmux_window"] == window)
            return {
                "session_name": session,
                "work_path": str(self._display_path(work)),
                "shell_window": shell,
                "terminals": records,
                "created_session": created_session,
            }

    def delete(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        run_id = self._identity(payload)
        with self.lock_for(run_id):
            paths = self._paths(run_id)
            quarantine = self._root_path / f".termroom-deleting-{run_id}"
            root_exists = self._path_exists(paths["root"])
            quarantine_exists = self._path_exists(quarantine)
            if root_exists and quarantine_exists:
                raise NodeRemoteRunError(
                    "Multiple Remote Run roots exist; refusing automatic deletion",
                    code="cleanup_ambiguous",
                )
            self._kill_session(run_id)
            if not root_exists and not quarantine_exists:
                return {"deleted": True, "already_missing": True}
            source = paths["root"] if root_exists else quarantine
            self._assert_marked_tree(source, run_id)
            if root_exists:
                self._replace(source, quarantine)
                source = quarantine
                self._assert_marked_tree(source, run_id)
            self._remove_owned_tree(source, run_id)
            return {"deleted": True, "already_missing": False}

    def validate_workspace(self, payload: Mapping[str, Any]) -> Path:
        run_id = _validate_run_id(payload.get("remote_run_id"))
        supplied = str(payload.get("workspace_path") or "")
        with self.lock_for(run_id):
            paths = self._assert_layout(run_id)
            work = self._real_directory(paths["work"], "Remote Run work")
            display_work = self._display_path(work)
            if supplied != str(display_work):
                raise NodeRemoteRunError(
                    "Remote Run Workspace path does not match its managed Run",
                    code="path_outside",
                )
            return display_work

    def workspace_fd(self, run_id: str, workspace_path: Path) -> int:
        with self.lock_for(run_id):
            paths = self._assert_layout(run_id)
            expected = self._display_path(paths["work"])
            if workspace_path != expected:
                raise NodeRemoteRunError(
                    "Remote Run Workspace path does not match its managed Run",
                    code="path_outside",
                )
            return self._work_dir_fds[run_id]

    @staticmethod
    def _require_version(payload: Mapping[str, Any]) -> None:
        value = payload.get("remote_run_version")
        if isinstance(value, bool) or value != NODE_REMOTE_RUN_VERSION:
            raise NodeRemoteRunError(
                "Node Remote Run version is incompatible; update Termroom",
                code="remote_run_version_incompatible",
            )

    def _identity(self, payload: Mapping[str, Any]) -> str:
        self._require_version(payload)
        run_id = _validate_run_id(payload.get("run_id"))
        if str(payload.get("run_base") or "") != str(self.run_root):
            raise NodeRemoteRunError(
                "Remote Run root does not match Node local policy",
                code="run_root_mismatch",
            )
        self._assert_root_path_matches()
        return run_id

    def _paths(self, run_id: str, *, leaf: str | None = None) -> dict[str, Path]:
        run_id = _validate_run_id(run_id)
        safe_leaf = leaf or run_id
        if safe_leaf not in {
            run_id,
            f".termroom-creating-{run_id}",
            f".termroom-deleting-{run_id}",
        }:
            raise NodeRemoteRunError("Remote Run internal path is invalid", code="path_invalid")
        root = self._root_path / safe_leaf
        metadata = root / ".termroom"
        return {
            "root": root,
            "work": root / "work",
            "work_staging": root / "work.tmp",
            "metadata": metadata,
            "marker": metadata / "marker",
            "cwd": metadata / "cwd",
            "command": metadata / "command.sh",
            "runner": metadata / "runner.sh",
            "log_pipe": metadata / "log-pipe.sh",
            "state": metadata / "state.json",
            "stop": metadata / "stop-requested-at",
            "prepare_result": metadata / "prepare-result.json",
            "prepare_log": metadata / "prepare.log",
            "output": metadata / "output.log",
            "completion": metadata / "completion.json",
            "git_url": metadata / "git-url",
            "git_path": metadata / "git-path",
            "git_revision": metadata / "git-revision",
            "git_argv": metadata / "git-argv",
            "git_askpass": metadata / "git-askpass",
            "git_home": metadata / "git-home",
            "git_bootstrap": metadata / "git-bootstrap.sh",
        }

    def _assert_layout(self, run_id: str) -> dict[str, Path]:
        if run_id in self._invalid_run_ids:
            raise NodeRemoteRunError(
                "Remote Run directory was replaced", code="layout_invalid"
            )
        self._assert_root_path_matches()
        paths = self._paths(run_id)
        self._layout_handles(run_id)
        staging_fd = self._work_staging_dir_fds.get(run_id)
        if staging_fd is not None:
            try:
                current = _open_directory_at(
                    self._run_dir_fds[run_id], ("work.tmp",), create=False, mode=None
                )
            except OSError as exc:
                raise NodeRemoteRunError(
                    "Remote Run staging directory is unavailable", code="layout_invalid"
                ) from exc
            try:
                expected = os.fstat(staging_fd)
                actual = os.fstat(current)
                if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
                    raise NodeRemoteRunError(
                        "Remote Run staging directory was replaced", code="layout_invalid"
                    )
            finally:
                os.close(current)
        return paths

    def _assert_marked_tree(self, root: Path, run_id: str) -> None:
        try:
            relative = root.relative_to(self._root_path)
            if len(relative.parts) != 1:
                raise OSError("Remote Run root is not a direct child")
            root_fd = _open_directory_at(
                self._root_fd, relative.parts, create=False, mode=None
            )
        except (OSError, ValueError) as exc:
            raise NodeRemoteRunError(
                "Remote Run layout is incomplete", code="layout_incomplete"
            ) from exc
        metadata_fd = -1
        try:
            metadata_fd = _open_directory_at(
                root_fd, (".termroom",), create=False, mode=None
            )
            if _read_regular_at(metadata_fd, "marker", 128).decode().strip() != run_id:
                raise NodeRemoteRunError(
                    "Remote Run marker does not match", code="marker_mismatch"
                )
        except OSError as exc:
            raise NodeRemoteRunError(
                "Remote Run layout is incomplete", code="layout_incomplete"
            ) from exc
        finally:
            if metadata_fd >= 0:
                os.close(metadata_fd)
            os.close(root_fd)

    def _layout_payload(self, run_id: str) -> dict[str, Any]:
        paths = self._paths(run_id)
        return {
            **{key: str(self._display_path(value)) for key, value in paths.items()},
            "run_base": str(self.run_root),
            "session_name": self._session_name(run_id),
        }

    def _display_path(self, path: Path) -> Path:
        return self.run_root / path.relative_to(self._root_path)

    def _open_parent(self, path: Path) -> tuple[int, str]:
        relative = path.relative_to(self._root_path)
        parts = relative.parts
        if not parts:
            raise NodeRemoteRunError("Remote Run path is invalid", code="path_invalid")
        run_id = parts[0]
        run_fd = self._run_dir_fds.get(run_id)
        metadata_fd = self._metadata_dir_fds.get(run_id)
        if metadata_fd is not None and len(parts) >= 3 and parts[1] == ".termroom":
            base_fd = os.dup(metadata_fd)
            components = parts[2:-1]
        elif run_fd is not None and len(parts) >= 2:
            base_fd = os.dup(run_fd)
            components = parts[1:-1]
        else:
            base_fd = os.dup(self._root_fd)
            components = parts[:-1]
        try:
            if components:
                nested = _open_directory_at(
                    base_fd, components, create=False, mode=None
                )
                os.close(base_fd)
                base_fd = nested
            return base_fd, parts[-1]
        except BaseException:
            os.close(base_fd)
            raise

    def _create_directory(self, path: Path, *, mode: int = 0o700) -> None:
        parent_fd, name = self._open_parent(path)
        try:
            directory_fd = _open_directory_at(
                parent_fd, (name,), create=True, mode=mode
            )
            os.close(directory_fd)
        finally:
            os.close(parent_fd)

    def _private_state_for_path(self, path: Path, *, mode: int) -> tuple[int, ...] | None:
        parent_fd, name = self._open_parent(path)
        try:
            return _private_leaf_state_at(parent_fd, name, mode=mode)
        finally:
            os.close(parent_fd)

    def _write_private(
        self,
        path: Path,
        content: bytes,
        *,
        expected_state: object,
        mode: int = 0o600,
    ) -> None:
        parent_fd, name = self._open_parent(path)
        try:
            _atomic_private_write_at(
                parent_fd, name, content, mode=mode, expected_state=expected_state
            )
        finally:
            os.close(parent_fd)

    def _layout_handles(self, run_id: str) -> tuple[int, int]:
        run_fd = self._run_dir_fds.get(run_id)
        try:
            current_run_fd = _open_directory_at(
                self._root_fd, (run_id,), create=False, mode=None
            )
        except FileNotFoundError as exc:
            if run_fd is not None:
                raise NodeRemoteRunError(
                    "Remote Run directory was replaced", code="layout_invalid"
                ) from exc
            raise
        except OSError as exc:
            raise NodeRemoteRunError(
                "Remote Run directory is invalid", code="layout_invalid"
            ) from exc
        if run_fd is None:
            run_fd = current_run_fd
            self._run_dir_fds[run_id] = run_fd
        else:
            expected = os.fstat(run_fd)
            current = os.fstat(current_run_fd)
            os.close(current_run_fd)
            if (expected.st_dev, expected.st_ino) != (current.st_dev, current.st_ino):
                raise NodeRemoteRunError(
                    "Remote Run directory was replaced", code="layout_invalid"
                )
        metadata_fd = self._metadata_dir_fds.get(run_id)
        try:
            current_metadata_fd = _open_directory_at(
                run_fd, (".termroom",), create=False, mode=None
            )
        except OSError as exc:
            raise NodeRemoteRunError(
                "Remote Run metadata directory is invalid", code="layout_invalid"
            ) from exc
        if metadata_fd is None:
            metadata_fd = current_metadata_fd
            self._metadata_dir_fds[run_id] = metadata_fd
        else:
            expected = os.fstat(metadata_fd)
            current = os.fstat(current_metadata_fd)
            os.close(current_metadata_fd)
            if (expected.st_dev, expected.st_ino) != (current.st_dev, current.st_ino):
                raise NodeRemoteRunError(
                    "Remote Run metadata directory was replaced", code="layout_invalid"
                )
        if _read_regular_at(metadata_fd, "marker", 128).decode().strip() != run_id:
            raise NodeRemoteRunError(
                "Remote Run marker does not match", code="marker_mismatch"
            )
        try:
            current_work_fd = _open_directory_at(
                run_fd, ("work",), create=False, mode=None
            )
        except OSError as exc:
            raise NodeRemoteRunError(
                "Remote Run work directory is invalid", code="layout_invalid"
            ) from exc
        work_fd = self._work_dir_fds.get(run_id)
        current_info = os.fstat(current_work_fd)
        current_identity = (current_info.st_dev, current_info.st_ino)
        metadata_info = os.fstat(metadata_fd)
        identity_key = (metadata_info.st_dev, metadata_info.st_ino)
        try:
            identity_state = _private_leaf_state_at(
                metadata_fd, "work.identity", mode=0o600
            )
            identity_record = _read_regular_at(metadata_fd, "work.identity", 128)
            if _private_leaf_state_at(
                metadata_fd, "work.identity", mode=0o600
            ) != identity_state:
                raise NodeRemoteRunError(
                    "Remote Run work identity changed during validation",
                    code="layout_invalid",
                )
        except FileNotFoundError:
            identity_record = None
        except (OSError, NodeRemoteRunError) as exc:
            os.close(current_work_fd)
            raise NodeRemoteRunError(
                "Remote Run work identity is invalid", code="layout_invalid"
            ) from exc
        if identity_record is None:
            os.close(current_work_fd)
            raise NodeRemoteRunError(
                "Remote Run work identity is missing", code="layout_invalid"
            )
        else:
            self._work_identity_states[identity_key] = identity_state
            try:
                state, recorded_identity, staging_identity = self._parse_work_identity(
                    identity_record
                )
            except NodeRemoteRunError:
                os.close(current_work_fd)
                raise
            authorized_change = False
            if (
                state == "git-pending"
                and run_id not in self._work_staging_dir_fds
                and current_identity != staging_identity
            ):
                os.close(current_work_fd)
                raise NodeRemoteRunError(
                    "Remote Run work directory was replaced", code="layout_invalid"
                )
            if state == "snapshot-pending":
                staging_fd = self._work_staging_dir_fds.get(run_id)
                if staging_fd is None:
                    os.close(current_work_fd)
                    raise NodeRemoteRunError(
                        "Remote Run snapshot recovery is unavailable",
                        code="layout_invalid",
                    )
                staged = os.fstat(staging_fd)
                if (staged.st_dev, staged.st_ino) != staging_identity:
                    os.close(current_work_fd)
                    raise NodeRemoteRunError(
                        "Remote Run snapshot staging was replaced",
                        code="layout_invalid",
                    )
            if current_identity != recorded_identity:
                if state != "git-pending" or current_identity != staging_identity:
                    os.close(current_work_fd)
                    raise NodeRemoteRunError(
                        "Remote Run work directory was replaced", code="layout_invalid"
                    )
                staging_fd = self._work_staging_dir_fds.get(run_id)
                if staging_fd is not None:
                    staged = os.fstat(staging_fd)
                    if (staged.st_dev, staged.st_ino) != staging_identity:
                        os.close(current_work_fd)
                        raise NodeRemoteRunError(
                            "Remote Run work does not match its staged identity",
                            code="layout_invalid",
                        )
                revision = None
                with contextlib.suppress(OSError, UnicodeDecodeError):
                    revision = _read_regular_at(
                        metadata_fd, "git-revision", 128
                    ).decode().strip()
                if not revision or not re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
                    os.close(current_work_fd)
                    raise NodeRemoteRunError(
                        "Remote Run work directory was replaced", code="layout_invalid"
                    )
                self._write_work_identity(
                    metadata_fd,
                    current_work_fd,
                    "stable",
                    expected_state=identity_state,
                )
                authorized_change = True
        if work_fd is not None:
            expected = os.fstat(work_fd)
            if (expected.st_dev, expected.st_ino) != current_identity:
                if not authorized_change:
                    os.close(current_work_fd)
                    raise NodeRemoteRunError(
                        "Remote Run work directory was replaced", code="layout_invalid"
                    )
                staging_fd = self._work_staging_dir_fds.get(run_id)
                if staging_fd is not None:
                    staged = os.fstat(staging_fd)
                    if (staged.st_dev, staged.st_ino) != current_identity:
                        os.close(current_work_fd)
                        raise NodeRemoteRunError(
                            "Remote Run work does not match its staged identity",
                            code="layout_invalid",
                        )
                    self._work_staging_dir_fds.pop(run_id)
                    os.close(work_fd)
                    os.close(current_work_fd)
                    self._work_dir_fds[run_id] = staging_fd
                else:
                    os.close(work_fd)
                    self._work_dir_fds[run_id] = current_work_fd
            else:
                os.close(current_work_fd)
        else:
            staging_fd = self._work_staging_dir_fds.get(run_id)
            if authorized_change and staging_fd is not None:
                staged = os.fstat(staging_fd)
                if (staged.st_dev, staged.st_ino) != current_identity:
                    os.close(current_work_fd)
                    raise NodeRemoteRunError(
                        "Remote Run work does not match its staged identity",
                        code="layout_invalid",
                    )
                self._work_staging_dir_fds.pop(run_id)
                os.close(current_work_fd)
                self._work_dir_fds[run_id] = staging_fd
            else:
                self._work_dir_fds[run_id] = current_work_fd
        return run_fd, metadata_fd

    @staticmethod
    def _parse_work_identity(
        value: bytes,
    ) -> tuple[str, tuple[int, int], tuple[int, int] | None]:
        try:
            parts = value.decode("ascii").strip().split(":")
            state = parts[0]
            if state == "stable" and len(parts) == 3:
                _, device, inode = parts
                return state, (int(device), int(inode)), None
            if state in {"git-pending", "snapshot-pending"} and len(parts) == 5:
                _, device, inode, staging_device, staging_inode = parts
                return (
                    state,
                    (int(device), int(inode)),
                    (int(staging_device), int(staging_inode)),
                )
            raise ValueError
        except (UnicodeDecodeError, ValueError) as exc:
            raise NodeRemoteRunError(
                "Remote Run work identity is invalid", code="layout_invalid"
            ) from exc

    def _write_work_identity(
        self,
        metadata_fd: int,
        work_fd: int,
        state: str,
        *,
        staging_fd: int | None = None,
        expected_state: object = _PRIVATE_WRITE_UNSPECIFIED,
    ) -> None:
        metadata_info = os.fstat(metadata_fd)
        identity_key = (metadata_info.st_dev, metadata_info.st_ino)
        if expected_state is _PRIVATE_WRITE_UNSPECIFIED:
            expected_state = self._work_identity_states.get(
                identity_key, _PRIVATE_WRITE_UNSPECIFIED
            )
        if expected_state is _PRIVATE_WRITE_UNSPECIFIED:
            expected_state = _private_leaf_state_at(metadata_fd, "work.identity", mode=0o600)
        info = os.fstat(work_fd)
        if state in {"git-pending", "snapshot-pending"}:
            if staging_fd is None:
                raise NodeRemoteRunError(
                    "Remote Run work staging identity is unavailable",
                    code="layout_invalid",
                )
            staging_info = os.fstat(staging_fd)
            value = (
                f"{state}:{info.st_dev}:{info.st_ino}:"
                f"{staging_info.st_dev}:{staging_info.st_ino}\n"
            )
        elif state == "stable" and staging_fd is None:
            value = f"stable:{info.st_dev}:{info.st_ino}\n"
        else:
            raise NodeRemoteRunError(
                "Remote Run work identity state is invalid", code="layout_invalid"
            )
        self._work_identity_states[identity_key] = _atomic_private_write_at(
            metadata_fd,
            "work.identity",
            value.encode("ascii"),
            mode=0o600,
            expected_state=expected_state,
        )

    def _set_work_identity_state(
        self, run_id: str, state: str, *, staging_fd: int | None = None
    ) -> None:
        self._layout_handles(run_id)
        self._write_work_identity(
            self._metadata_dir_fds[run_id],
            self._work_dir_fds[run_id],
            state,
            staging_fd=staging_fd,
        )

    def _create_git_work_staging(self, run_id: str) -> int:
        run_fd = self._run_dir_fds[run_id]
        try:
            return _create_directory_handle_at(run_fd, "work.tmp")
        except FileExistsError as exc:
            self._invalid_run_ids.add(run_id)
            raise NodeRemoteRunError(
                "Remote Run staging directory already exists", code="layout_invalid"
            ) from exc
        except OSError as exc:
            self._invalid_run_ids.add(run_id)
            raise NodeRemoteRunError(
                "Remote Run staging directory was replaced", code="layout_invalid"
            ) from exc

    def _discard_git_work_staging(self, run_id: str) -> None:
        descriptor = self._work_staging_dir_fds.pop(run_id, None)
        if descriptor is None:
            return
        try:
            run_fd = self._run_dir_fds[run_id]
            info = os.fstat(descriptor)
            current = os.stat("work.tmp", dir_fd=run_fd, follow_symlinks=False)
            if (
                (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino)
                and not os.listdir(descriptor)
            ):
                os.rmdir("work.tmp", dir_fd=run_fd)
            self._write_work_identity(
                self._metadata_dir_fds[run_id], self._work_dir_fds[run_id], "stable"
            )
        except (FileNotFoundError, OSError):
            pass
        finally:
            os.close(descriptor)

    def _refresh_work_directory(
        self, run_id: str, run_fd: int, metadata_fd: int
    ) -> None:
        current = _open_directory_at(run_fd, ("work",), create=False, mode=None)
        try:
            self._write_work_identity(metadata_fd, current, "stable")
        except BaseException:
            os.close(current)
            raise
        previous = self._work_dir_fds.get(run_id)
        self._work_dir_fds[run_id] = current
        if previous is not None:
            os.close(previous)

    def _relative_parts(self, path: Path) -> tuple[str, ...]:
        try:
            relative = path.relative_to(self._root_path)
        except ValueError as exc:
            raise NodeRemoteRunError("Remote Run path is invalid", code="path_invalid") from exc
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise NodeRemoteRunError("Remote Run path is invalid", code="path_invalid")
        return relative.parts

    def _open_directory(self, path: Path) -> int:
        return _open_directory_at(
            self._root_fd, self._relative_parts(path), create=False, mode=None
        )

    def _path_exists(self, path: Path) -> bool:
        parent_fd = -1
        try:
            parent_fd, name = self._open_parent(path)
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False
        finally:
            if parent_fd >= 0:
                os.close(parent_fd)

    def _replace(self, source: Path, destination: Path) -> None:
        source_parent, source_name = self._open_parent(source)
        try:
            destination_parent, destination_name = self._open_parent(destination)
            try:
                os.replace(
                    source_name,
                    destination_name,
                    src_dir_fd=source_parent,
                    dst_dir_fd=destination_parent,
                )
            finally:
                os.close(destination_parent)
        finally:
            os.close(source_parent)

    def _forget_layout_handles(self, run_id: str) -> None:
        metadata_fd = self._metadata_dir_fds.get(run_id)
        if metadata_fd is not None:
            info = os.fstat(metadata_fd)
            self._work_identity_states.pop((info.st_dev, info.st_ino), None)
        for handles in (
            self._metadata_dir_fds,
            self._run_dir_fds,
            self._work_dir_fds,
            self._work_staging_dir_fds,
        ):
            descriptor = handles.pop(run_id, None)
            if descriptor is not None:
                os.close(descriptor)

    def _staging_target(self, run_id: str, relative: str) -> Path:
        paths = self._assert_layout(run_id)
        staging = self._real_directory(paths["work_staging"], "Source staging")
        target = staging.joinpath(*relative.split("/"))
        if target.parent != staging:
            parent = target.parent
            if not is_within(parent, staging):
                raise NodeRemoteRunError("Source path escapes staging", code="path_outside")
            self._real_directory(parent, "Source parent")
        if self._path_exists(target):
            raise NodeRemoteRunError(
                "Remote Run Source path already exists", code="source_path_conflict"
            )
        return target

    def _real_directory(self, path: Path, label: str) -> Path:
        try:
            try:
                relative = path.relative_to(self._root_path)
            except ValueError:
                relative = path.relative_to(self.run_root)
            run_fd = self._run_dir_fds.get(relative.parts[0]) if relative.parts else None
            if run_fd is not None:
                descriptor = _open_directory_at(
                    run_fd, relative.parts[1:], create=False, mode=None
                )
            else:
                descriptor = _open_directory_at(
                    self._root_fd, relative.parts, create=False, mode=None
                )
            os.close(descriptor)
        except (OSError, ValueError) as exc:
            raise NodeRemoteRunError(f"{label} is unavailable", code="path_invalid") from exc
        return path

    def _read_regular(self, path: Path, limit: int) -> bytes:
        parent_fd = -1
        try:
            parent_fd, name = self._open_parent(path)
            return _read_regular_at(parent_fd, name, limit)
        except NodeRemoteRunError:
            raise
        except OSError as exc:
            raise NodeRemoteRunError(
                "Remote Run metadata is unavailable", code="metadata_invalid"
            ) from exc
        finally:
            if parent_fd >= 0:
                os.close(parent_fd)

    @staticmethod
    def _validate_source_manifest(value: dict[str, Any] | list[Any]) -> None:
        if not isinstance(value, list):
            raise NodeRemoteRunError("Source manifest is invalid", code="source_manifest")
        entries: list[WorkspaceEntry] = []
        for raw in value:
            if not isinstance(raw, dict):
                raise NodeRemoteRunError("Source manifest is invalid", code="source_manifest")
            entries.append(
                WorkspaceEntry(
                    relative_path=str(raw.get("path") or ""),
                    kind=str(raw.get("kind") or ""),  # type: ignore[arg-type]
                    size=int(raw.get("size") or 0),
                    mtime_ns=int(raw.get("mtime_ns") or 0),
                    executable=raw.get("executable") is True,
                    link_target=(
                        str(raw["link_target"]) if raw.get("link_target") is not None else None
                    ),
                )
            )
        build_workspace_manifest(entries)

    @staticmethod
    def _metadata_name(value: object) -> str:
        name = str(value or "")
        if name not in {"source.json", "source-manifest.json", "inputs.json"}:
            raise NodeRemoteRunError(
                "Remote Run metadata name is unsupported", code="metadata_invalid"
            )
        return name

    def _encode_metadata(self, name: str, value: object) -> bytes:
        name = self._metadata_name(name)
        if not isinstance(value, (dict, list)):
            raise NodeRemoteRunError(
                "Remote Run metadata is invalid", code="metadata_invalid"
            )
        if name == "source-manifest.json":
            try:
                self._validate_source_manifest(value)
            except (TypeError, ValueError) as exc:
                raise NodeRemoteRunError(
                    "Source manifest is invalid", code="source_manifest"
                ) from exc
        try:
            encoded = (
                json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode()
        except (TypeError, ValueError) as exc:
            raise NodeRemoteRunError(
                "Remote Run metadata is invalid", code="metadata_invalid"
            ) from exc
        if len(encoded) > 8 * 1024 * 1024:
            raise NodeRemoteRunError(
                "Remote Run metadata is too large", code="metadata_invalid"
            )
        return encoded

    def _commit_metadata(self, run_id: str, name: str, content: bytes) -> None:
        name = self._metadata_name(name)
        try:
            decoded = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NodeRemoteRunError(
                "Remote Run metadata is invalid", code="metadata_invalid"
            ) from exc
        canonical = self._encode_metadata(name, decoded)
        if canonical != content:
            raise NodeRemoteRunError(
                "Remote Run metadata is not canonical", code="metadata_invalid"
            )
        with self.lock_for(run_id):
            paths = self._assert_layout(run_id)
            self._assert_not_started(run_id, paths)
            metadata_path = paths["metadata"] / name
            expected_state = self._private_state_for_path(metadata_path, mode=0o600)
            self._write_private(
                metadata_path, content, expected_state=expected_state
            )

    def _assert_not_started(
        self, run_id: str, paths: Mapping[str, Path]
    ) -> None:
        if self._stop_requested(paths) or any(
            self._path_exists(paths[name])
            for name in ("state", "prepare_result", "completion")
        ) or self._tmux_status(run_id)["exists"]:
            raise NodeRemoteRunError(
                "Remote Run Source cannot change after execution starts",
                code="run_already_started",
            )

    def _existing_start(
        self, run_id: str, paths: Mapping[str, Path]
    ) -> dict[str, Any] | None:
        lifecycle = self._stop_requested(paths) or any(
            self._path_exists(paths[name])
            for name in ("state", "prepare_result", "completion")
        )
        status = self._tmux_status(run_id)
        if not lifecycle and not status["exists"]:
            return None
        if status["exists"] and not self._session_owned(run_id):
            raise NodeRemoteRunError(
                "Remote Run session identity is already in use",
                code="session_identity_conflict",
            )
        observation = self._reconcile(run_id, dict(paths))
        return {
            **observation,
            "session_name": self._session_name(run_id),
            "run_root": str(self._display_path(paths["root"])),
            "replayed": True,
        }

    def _handoff_tmux(
        self,
        tmux_args: tuple[str, ...],
        descriptors: tuple[int, ...],
        command: tuple[str, ...] = (),
        *,
        inherit: tuple[int, ...] = (),
        environment: Mapping[str, str] | None = None,
        rollback: Callable[[subprocess.CompletedProcess[str]], None] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if self._descriptor_handoff is None:
            raise NodeRemoteRunError(
                "Node tmux descriptor handoff is unavailable", code="capability_unsupported"
            )
        try:
            return self._descriptor_handoff(
                tmux_args,
                descriptors,
                command,
                inherit=inherit,
                environment=environment,
                rollback=rollback,
                check=check,
            )
        except NodeRemoteRunError:
            raise
        except Exception as exc:
            raise NodeRemoteRunError(
                str(exc), code=str(getattr(exc, "code", "tmux_handoff_failed"))
            ) from exc

    def _start_tmux(self, run_id: str, *, mode: str) -> None:
        paths = self._assert_layout(run_id)
        if mode not in {"run", "git"}:
            raise NodeRemoteRunError("Remote Run mode is invalid", code="path_invalid")
        if self._tmux("has-session", "-t", self._session_name(run_id), check=False).returncode == 0:
            raise NodeRemoteRunError("Remote Run session already exists", code="run_exists")

        run_fd, metadata_fd = self._layout_handles(run_id)
        work_fd = self._work_dir_fds[run_id]
        cwd_rel = _read_regular_at(metadata_fd, "cwd", 4096).decode("utf-8").strip()
        try:
            cwd_rel = validate_cwd_rel(cwd_rel)
        except (SourceValidationError, UnicodeDecodeError) as exc:
            raise NodeRemoteRunError(
                "Remote Run working directory is invalid", code="path_invalid"
            ) from exc

        launch_fds: list[int] = []
        try:
            if mode == "run":
                cwd_fd = _open_directory_at(
                    work_fd,
                    () if cwd_rel == "." else cwd_rel.split("/"),
                    create=False,
                    mode=None,
                )
            else:
                cwd_fd = os.dup(run_fd)
            launch_fds.append(cwd_fd)
            launch_fds.extend((os.dup(run_fd), os.dup(metadata_fd), os.dup(work_fd)))
            command_fd = _sealed_regular_at(
                metadata_fd, "command.sh", 256 * 1024
            )
            launch_fds.append(command_fd)
            if _private_leaf_state_at(metadata_fd, "output-seal.json", mode=0o600) is not None:
                raise NodeRemoteRunError(
                    "Remote Run seal already exists", code="metadata_invalid"
                )
            self._write_private(paths["output"], b"", expected_state=None)
            output_fd = _open_owned_regular_at(
                metadata_fd, "output.log", flags=os.O_WRONLY | os.O_APPEND
            )
            launch_fds.append(output_fd)

            environment = {
                "TERMROOM_REMOTE_RUN_META_DIR": "{fd:2}",
                "TERMROOM_REMOTE_RUN_ROOT_DIR": "{fd:1}",
                "TERMROOM_REMOTE_RUN_COMMAND": "{fd:4}",
                "TERMROOM_REMOTE_RUN_WORK_DIR": "{fd:3}",
                "TERMROOM_REMOTE_RUN_CWD_DIR": "{fd:0}",
                "TERMROOM_REMOTE_RUN_OUTPUT_FILE": "{fd:5}",
                "TERMROOM_REMOTE_RUN_CWD_REL": cwd_rel,
                "TERMROOM_REMOTE_RUN_PIPE": "true",
                "TERMROOM_NODE_PYTHON": sys.executable,
                "TERMROOM_REMOTE_RUN_LOG_HELPER_CODE": _NODE_REMOTE_RUN_LOG_HELPER_CODE,
                "TERMROOM_REMOTE_RUN_LOG_VERIFY_CODE": _NODE_REMOTE_RUN_LOG_VERIFY_CODE,
            }
            command = (
                "/bin/bash", "--noprofile", "--norc", "-c",
                _node_remote_run_script(), "termroom-node-remote-run", run_id,
            )
            if mode == "git":
                staging_fd = self._work_staging_dir_fds.get(run_id)
                if staging_fd is None:
                    raise NodeRemoteRunError(
                        "Remote Run staging identity is unavailable", code="layout_invalid"
                    )
                git_argv_fd = _sealed_regular_at(
                    metadata_fd, "git-argv", 256 * 1024
                )
                git_path_fd = _sealed_regular_at(metadata_fd, "git-path", 4096)
                askpass_fd = _sealed_regular_at(
                    metadata_fd, "git-askpass", 4096, mode=0o700
                )
                git_home_fd = _open_directory_at(
                    metadata_fd, ("git-home",), create=False, mode=None
                )
                self._write_private(paths["prepare_log"], b"", expected_state=None)
                prepare_log_fd = _open_owned_regular_at(
                    metadata_fd,
                    "prepare.log",
                    flags=os.O_WRONLY | os.O_APPEND,
                )
                launch_fds.extend(
                    (os.dup(staging_fd), git_argv_fd, git_path_fd, askpass_fd,
                     git_home_fd, prepare_log_fd)
                )
                environment.update(
                    {
                        "TERMROOM_REMOTE_RUN_STAGING_DIR": "{fd:6}",
                        "TERMROOM_REMOTE_RUN_GIT_ARGV": "{fd:7}",
                        "TERMROOM_REMOTE_RUN_GIT_PATH_FILE": "{fd:8}",
                        "TERMROOM_REMOTE_RUN_ASKPASS": "{fd:9}",
                        "TERMROOM_REMOTE_RUN_GIT_HOME": "{fd:10}",
                        "TERMROOM_REMOTE_RUN_PREPARE_LOG": "{fd:11}",
                        "TERMROOM_NODE_PYTHON": sys.executable,
                        "TERMROOM_REMOTE_RUN_GIT_FINISH_CODE": (
                            _NODE_REMOTE_RUN_GIT_FINISH_CODE
                        ),
                        "TERMROOM_REMOTE_RUN_RUNNER_SCRIPT": _node_remote_run_script(),
                    }
                )
                command = (
                    "/bin/bash", "--noprofile", "--norc", "-c",
                    _node_git_bootstrap_script(), "termroom-node-remote-git", run_id,
                )

            # Append after Git descriptors to preserve their existing handoff indices.
            channel_index = len(launch_fds)
            launch_fds.extend(os.pipe())
            environment.update({
                "TERMROOM_REMOTE_RUN_LOG_CHANNEL": f"{{fd:{channel_index}}}",
                "TERMROOM_REMOTE_RUN_LOG_CHANNEL_WRITE": f"{{fd:{channel_index + 1}}}",
            })
            session = self._session_name(run_id)
            self._tmux(
                "new-session", "-d", "-s", session, "-c", "/", "-n", "run",
                "/bin/sleep 2147483647",
            )
            target = f"{session}:run"
            try:
                self._tmux("set-option", "-t", session, "@termroom_remote_run_id", run_id)
                self._tmux("set-window-option", "-t", target, "remain-on-exit", "on")
                self._tmux(
                    "set-window-option", "-t", target, "remain-on-exit-format", "", check=False
                )
                self._tmux(
                    "set-window-option", "-t", target, "window-size", "latest", check=False
                )
                self._tmux(
                    "set-window-option", "-t", target, TMUX_TERMINAL_ROLE_OPTION, "remote_run"
                )
                self._tmux(
                    "set-window-option", "-t", target, TMUX_MANAGED_RUN_OPTION, run_id
                )
                result = self._handoff_tmux(
                    ("respawn-pane", "-k", "-c", "/", "-t", f"{session}:run.0"),
                    tuple(launch_fds),
                    command,
                    inherit=tuple(range(len(launch_fds))),
                    environment=environment,
                    rollback=lambda _result: self._tmux(
                        "kill-session", "-t", session, check=False
                    ),
                    check=False,
                )
                if result.returncode:
                    raise NodeRemoteRunError(
                        result.stderr.strip() or "Remote Run could not start",
                        code="tmux_failed",
                    )
            except BaseException:
                self._tmux("kill-session", "-t", session, check=False)
                raise
        finally:
            for descriptor in launch_fds:
                with contextlib.suppress(OSError):
                    os.close(descriptor)

    def _reconcile(self, run_id: str, paths: dict[str, Path]) -> dict[str, Any]:
        tmux = self._tmux_status(run_id)
        completion_exists = self._path_exists(paths["completion"])
        completion = self._read_json_record(paths["completion"])
        prepare_exists = self._path_exists(paths["prepare_result"])
        prepare = self._read_json_record(paths["prepare_result"])
        state_exists = self._path_exists(paths["state"])
        state_record = self._read_json_record(paths["state"])
        completion_valid = self._valid_completion(completion)
        prepare_valid = self._valid_prepare(prepare)
        state_valid = self._valid_state(state_record)
        stop_requested = self._stop_requested(paths)
        errors = [
            name
            for name, exists, valid in (
                ("completion.json", completion_exists, completion_valid),
                ("prepare-result.json", prepare_exists, prepare_valid),
                ("state.json", state_exists, state_valid),
            )
            if exists and not valid
        ]
        result: dict[str, Any] = {
            "state": "preparing",
            "phase": state_record.get("phase") if state_valid else None,
            "exit_code": None,
            "started_at": state_record.get("started_at") if state_valid else None,
            "ended_at": None,
            "stop_requested": stop_requested,
            "tmux_exists": tmux["exists"],
            "run_pane_exists": tmux["run_pane_exists"],
            "tmux_running": tmux["running"],
            "record_errors": errors,
        }
        live_phase = state_record.get("phase") if state_valid else None
        if not completion_valid and not prepare_valid and not tmux["running"] and live_phase:
            tmux = self._tmux_status(run_id)
            result.update(
                tmux_exists=tmux["exists"],
                run_pane_exists=tmux["run_pane_exists"],
                tmux_running=tmux["running"],
            )
            if not tmux["running"]:
                completion = self._read_json_record(paths["completion"])
                prepare = self._read_json_record(paths["prepare_result"])
                state_record = self._read_json_record(paths["state"])
                completion_valid = self._valid_completion(completion)
                prepare_valid = self._valid_prepare(prepare)
                state_valid = self._valid_state(state_record)
                stop_requested = self._stop_requested(paths)
                result.update(
                    phase=state_record.get("phase") if state_valid else None,
                    started_at=state_record.get("started_at") if state_valid else None,
                    stop_requested=stop_requested,
                )
                result["record_errors"] = [
                    name
                    for name, path, valid in (
                        ("completion.json", paths["completion"], completion_valid),
                        ("prepare-result.json", paths["prepare_result"], prepare_valid),
                        ("state.json", paths["state"], state_valid),
                    )
                    if self._path_exists(path) and not valid
                ]
        if completion_valid:
            result.update(
                state="stopped" if completion["stop_requested"] else "finished",
                phase=None,
                exit_code=completion["exit_code"],
                started_at=completion["started_at"],
                ended_at=completion["ended_at"],
                stop_requested=completion["stop_requested"],
                log_incomplete=bool(completion.get("log_incomplete", False)),
            )
        elif prepare_valid:
            result.update(
                state=prepare["state"],
                phase=None,
                ended_at=prepare["ended_at"],
                error_code=prepare.get("error_code"),
            )
        elif tmux["running"]:
            if state_valid and state_record.get("phase") == "running":
                result["state"] = "running"
        elif stop_requested:
            result.update(state="stopped", phase=None)
        elif not result["record_errors"] and state_valid:
            if state_record.get("phase") == "running":
                result.update(state="lost", phase=None)
            elif state_record.get("phase") == "cloning":
                result.update(state="failed", phase=None, error_code="git_session_lost")
        revision = self._read_optional_line(paths["git_revision"])
        if (
            revision
            and len(revision) == 40
            and all(c in "0123456789abcdefABCDEF" for c in revision)
        ):
            result["source_revision"] = revision.lower()
        return result

    def _tmux_status(self, run_id: str) -> dict[str, Any]:
        session = self._session_name(run_id)
        if self._tmux("has-session", "-t", session, check=False).returncode:
            return {
                "exists": False,
                "run_pane_exists": False,
                "running": False,
                "pane_exit_code": None,
            }
        result = self._tmux(
            "list-panes", "-t", f"{session}:run.0", "-F", "#{pane_dead}|#{pane_dead_status}",
            check=False,
        )
        if result.returncode or not result.stdout.strip():
            return {
                "exists": True,
                "run_pane_exists": False,
                "running": False,
                "pane_exit_code": None,
            }
        dead, separator, exit_value = result.stdout.splitlines()[0].partition("|")
        if not separator or dead not in {"0", "1"}:
            raise NodeRemoteRunError("Node tmux status is invalid", code="tmux_invalid")
        return {
            "exists": True,
            "run_pane_exists": True,
            "running": dead == "0",
            "pane_exit_code": int(exit_value) if exit_value.lstrip("-").isdigit() else None,
        }

    def _owned_run_window(self, run_id: str) -> bool:
        if not self._session_owned(run_id):
            return False
        session = self._session_name(run_id)
        records = self._terminal_records(session, check=False)
        return any(
            item.get("name") == "run"
            and item.get("role") == "remote_run"
            and item.get("managed_run_id") == run_id
            for item in records
        )

    def _session_owned(self, run_id: str) -> bool:
        session = self._session_name(run_id)
        result = self._tmux(
            "show-options",
            "-v",
            "-t",
            session,
            "@termroom_remote_run_id",
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == run_id

    def _terminal_records(self, session: str, *, check: bool = True) -> list[dict[str, Any]]:
        result = self._tmux(
            "list-windows", "-t", session, "-F", TMUX_TERMINAL_RECORD_FORMAT, check=check
        )
        if result.returncode:
            return []
        return [dict(item) for item in parse_tmux_terminal_records(result.stdout)]

    def _read_json_record(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(self._read_regular(path, 1024 * 1024).decode("utf-8"))
            return dict(value) if isinstance(value, dict) else {}
        except (OSError, NodeRemoteRunError, UnicodeDecodeError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _valid_completion(value: Mapping[str, Any]) -> bool:
        return (
            isinstance(value.get("exit_code"), int)
            and not isinstance(value.get("exit_code"), bool)
            and isinstance(value.get("stop_requested"), bool)
            and isinstance(value.get("started_at"), str)
            and bool(value.get("started_at"))
            and isinstance(value.get("ended_at"), str)
            and bool(value.get("ended_at"))
        )

    @staticmethod
    def _valid_prepare(value: Mapping[str, Any]) -> bool:
        return (
            value.get("state") in {"failed", "stopped"}
            and isinstance(value.get("ended_at"), str)
            and bool(value.get("ended_at"))
        )

    @staticmethod
    def _valid_state(value: Mapping[str, Any]) -> bool:
        return (
            value.get("phase") in {"cloning", "running"}
            and isinstance(value.get("started_at"), str)
            and bool(value.get("started_at"))
        )

    def _read_log(
        self,
        paths: Mapping[str, Path],
        *,
        stream: str,
        offset: int | None,
        limit: int,
    ) -> dict[str, Any]:
        if stream not in {"prepare", "command"}:
            raise NodeRemoteRunError("Remote Run log stream is invalid", code="log_invalid")
        if limit <= 0:
            raise NodeRemoteRunError("Remote Run log limit is invalid", code="log_invalid")
        limit = min(limit, REMOTE_RUN_LOG_READ_LIMIT)
        path = paths["prepare_log"] if stream == "prepare" else paths["output"]
        parent_fd = -1
        descriptor = -1
        try:
            parent_fd, name = self._open_parent(path)
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except FileNotFoundError:
            if parent_fd >= 0:
                os.close(parent_fd)
            return self._empty_log(stream, offset)
        except OSError as exc:
            if parent_fd >= 0:
                os.close(parent_fd)
            raise NodeRemoteRunError("Remote Run log is invalid", code="log_invalid") from exc
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
            ):
                raise NodeRemoteRunError("Remote Run log is invalid", code="log_invalid")
            size = info.st_size
            start = max(0, size - REMOTE_RUN_INITIAL_TAIL) if offset is None else offset
            if start < 0 or start > size:
                raise NodeRemoteRunError("Remote Run log offset is invalid", code="log_invalid")
            if offset is None and start:
                os.lseek(descriptor, start, os.SEEK_SET)
                prefix = os.read(descriptor, 4)
                skipped = 0
                while skipped < len(prefix) and prefix[skipped] & 0xC0 == 0x80:
                    skipped += 1
                start += skipped
            os.lseek(descriptor, start, os.SEEK_SET)
            raw = os.read(descriptor, min(limit, size - start))
        finally:
            os.close(descriptor)
            os.close(parent_fd)
        next_offset = start + len(raw)
        return {
            "stream": stream,
            "chunk_b64": base64.b64encode(raw).decode("ascii"),
            "start_offset": start,
            "next_offset": next_offset,
            "size": size,
            "eof": next_offset >= size,
        }

    @staticmethod
    def _empty_log(stream: str, offset: int | None) -> dict[str, Any]:
        if stream not in {"prepare", "command"}:
            raise NodeRemoteRunError("Remote Run log stream is invalid", code="log_invalid")
        start = max(0, offset or 0)
        return {
            "stream": stream,
            "chunk_b64": "",
            "start_offset": start,
            "next_offset": start,
            "size": start,
            "eof": True,
        }

    def _unavailable_layout(
        self, run_id: str, *, missing: bool = False, error: str | None = None
    ) -> dict[str, Any]:
        status = self._tmux_status(run_id)
        return {
            "state": "layout_missing" if missing else "layout_error",
            "phase": None,
            "exit_code": None,
            "started_at": None,
            "ended_at": None,
            "stop_requested": False,
            "tmux_exists": status["exists"],
            "tmux_running": status["running"],
            "run_pane_exists": status["run_pane_exists"],
            "record_errors": [error] if error else [],
            "layout_missing": missing,
            "layout_error": error,
        }

    def _stop_requested(self, paths: Mapping[str, Path]) -> bool:
        return self._private_state_for_path(paths["stop"], mode=0o600) is not None

    def _publish_stop(self, paths: Mapping[str, Path]) -> None:
        if self._stop_requested(paths):
            return
        from datetime import UTC, datetime

        value = datetime.now(UTC).isoformat(timespec="seconds") + "\n"
        self._write_private(paths["stop"], value.encode(), expected_state=None)

    def _finish_interrupted_delete(self, run_id: str) -> None:
        quarantine = self._root_path / f".termroom-deleting-{run_id}"
        if self._path_exists(quarantine):
            self._assert_marked_tree(quarantine, run_id)
            self._remove_owned_tree(quarantine, run_id)

    def _remove_staging(self, path: Path, run_id: str) -> None:
        expected = self._paths(run_id)["work_staging"]
        if path != expected or path.parent != self._paths(run_id)["root"]:
            raise NodeRemoteRunError("Source staging path is invalid", code="path_invalid")
        parent_fd, name = self._open_parent(path)
        try:
            staging_fd = self._work_staging_dir_fds.get(run_id)
            staging_identity = None
            if staging_fd is not None:
                staging_info = os.fstat(staging_fd)
                staging_identity = (staging_info.st_dev, staging_info.st_ino)
            _remove_tree_at(
                parent_fd, name, expected_identity=staging_identity
            )
            staging_fd = self._work_staging_dir_fds.pop(run_id, None)
            if staging_fd is not None:
                os.close(staging_fd)
        except OSError as exc:
            raise NodeRemoteRunError("Source staging is invalid", code="path_invalid") from exc
        finally:
            os.close(parent_fd)

    def _remove_owned_tree(
        self, path: Path, run_id: str, *, allow_missing_marker: bool = False
    ) -> None:
        allowed = {
            self._root_path / run_id,
            self._root_path / f".termroom-creating-{run_id}",
            self._root_path / f".termroom-deleting-{run_id}",
        }
        if path not in allowed or path.parent != self._root_path:
            raise NodeRemoteRunError("Remote Run cleanup path is invalid", code="cleanup_invalid")
        parent_fd, name = self._open_parent(path)
        root_fd = -1
        try:
            root_fd = _open_directory_at(parent_fd, (name,), create=False, mode=None)
            try:
                metadata_fd = _open_directory_at(
                    root_fd, (".termroom",), create=False, mode=None
                )
            except FileNotFoundError:
                if not allow_missing_marker:
                    raise
            else:
                try:
                    marker = _read_regular_at(metadata_fd, "marker", 128).decode().strip()
                    if marker != run_id:
                        raise NodeRemoteRunError(
                            "Remote Run marker does not match", code="marker_mismatch"
                        )
                finally:
                    os.close(metadata_fd)
            _remove_directory_contents(root_fd)
            expected = os.fstat(root_fd)
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (expected.st_dev, expected.st_ino) != (current.st_dev, current.st_ino):
                raise NodeRemoteRunError(
                    "Remote Run cleanup root was replaced", code="cleanup_invalid"
                )
            os.rmdir(name, dir_fd=parent_fd)
            self._forget_layout_handles(run_id)
        except OSError as exc:
            raise NodeRemoteRunError(
                "Remote Run cleanup root is invalid", code="cleanup_invalid"
            ) from exc
        finally:
            if root_fd >= 0:
                os.close(root_fd)
            os.close(parent_fd)

    def _kill_session(self, run_id: str) -> bool:
        session = self._session_name(run_id)
        exists = self._tmux("has-session", "-t", session, check=False).returncode == 0
        if exists and self._session_owned(run_id):
            self._tmux("kill-session", "-t", session, check=False)
            return True
        return False

    @staticmethod
    def _session_name(run_id: str) -> str:
        return REMOTE_RUN_SESSION_PREFIX + _validate_run_id(run_id)

    def _read_optional_line(self, path: Path) -> str | None:
        try:
            value = self._read_regular(path, 4096).decode("utf-8").strip()
            return value or None
        except (OSError, NodeRemoteRunError, UnicodeDecodeError):
            return None

    @staticmethod
    def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        for key in tuple(environment):
            if key.startswith("TERMROOM_"):
                environment.pop(key, None)
        command = ["tmux"]
        test_socket = environment.get("PYTEST_TMUX_SOCKET", "")
        if test_socket:
            command.extend(("-S", test_socket))
        result = subprocess.run(
            [*command, *args],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        if check and result.returncode:
            raise NodeRemoteRunError(
                result.stderr.strip() or "tmux operation failed", code="tmux_failed"
            )
        return result


class NodeRemoteRunSnapshotSink:
    def __init__(
        self,
        client: NodeRemoteRunClient,
        computer: Mapping[str, Any],
        run_base: str,
        run_id: str,
    ) -> None:
        self.client = client
        self.computer = computer
        self.run_base = run_base
        self.run_id = run_id

    def make_directory(self, relative_path: str, *, executable: bool) -> None:
        del executable
        self.client._request(
            self.computer,
            "remote_run.snapshot.mkdir",
            self.client._payload(self.run_base, self.run_id, path=relative_path),
        )

    def make_symlink(self, relative_path: str, link_target: str) -> None:
        self.client._request(
            self.computer,
            "remote_run.snapshot.symlink",
            self.client._payload(
                self.run_base, self.run_id, path=relative_path, link_target=link_target
            ),
        )

    def write_file(
        self,
        relative_path: str,
        chunks: Iterable[bytes],
        *,
        executable: bool,
        expected_size: int,
    ) -> None:
        stream: NodeStream | None = None
        try:
            _result, stream = self.client._open_stream(
                self.computer,
                "remote_run.snapshot.file.open",
                self.client._payload(
                    self.run_base,
                    self.run_id,
                    path=relative_path,
                    executable=executable,
                    expected_size=expected_size,
                ),
            )
            for chunk in chunks:
                self.client._submit(stream.send(chunk))
            result = self.client._submit(stream.finish())
            if int(result.get("size", -1)) != expected_size:
                raise NodeRemoteRunError(
                    "Node returned an invalid Source file size", code="source_file_changed"
                )
        except BaseException:
            if stream is not None:
                with contextlib.suppress(Exception):
                    self.client._submit(stream.abort())
            raise


class _WorkspaceSourceReceiveWindow:
    """Grant only the exact Node-to-Core Source frames that remain."""

    def __init__(
        self,
        client: NodeRemoteRunClient,
        stream: NodeStream,
        frame_count: int,
    ) -> None:
        self.client = client
        self.stream = stream
        self.frame_count = frame_count
        self.received = 0
        self._ungranted = frame_count
        self._batch_remaining = 0
        self._grant_next_batch()

    def _grant_next_batch(self) -> None:
        count = min(NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW, self._ungranted)
        if count == 0:
            return
        self.client._submit(self.stream.control("credit", count=count))
        self._ungranted -= count
        self._batch_remaining = count

    def receive(self) -> bytes | None:
        chunk = self.client._submit(self.stream.receive())
        if chunk is None:
            if self.received != self.frame_count:
                raise NodeRemoteRunError(
                    "Node Remote Run Source stream ended at the wrong frame",
                    code="source_stream_invalid",
                )
            return None
        if self._batch_remaining == 0 or self.received >= self.frame_count:
            raise NodeRemoteRunError(
                "Node Remote Run Source stream exceeded its declared frames",
                code="source_stream_invalid",
            )
        self.received += 1
        self._batch_remaining -= 1
        if self._batch_remaining == 0:
            self._grant_next_batch()
        return chunk


class NodeWorkspaceSnapshotSource:
    """A bounded Node-to-Core ``SnapshotSource`` for one persistent Workspace."""

    def __init__(
        self,
        client: NodeRemoteRunClient,
        workspace: Mapping[str, Any],
        source_path: str,
        *,
        explicitly_included: Iterable[str],
    ) -> None:
        computer = workspace.get("computer")
        if not isinstance(computer, Mapping) or computer.get("connection_method") != "node":
            raise NodeRemoteRunError(
                "Node Workspace Source computer is invalid", code="workspace_required"
            )
        if workspace.get("transient") or workspace.get("remote_run_id"):
            raise NodeRemoteRunError(
                "A transient Remote Run Workspace cannot be a Source",
                code="source_workspace_transient",
            )
        self.client = client
        self.computer = computer
        self.payload = {
            "remote_run_source_version": NODE_REMOTE_RUN_SOURCE_VERSION,
            "workspace_id": str(workspace.get("id") or ""),
            "workspace_path": str(
                workspace.get("canonical_path") or workspace.get("path") or ""
            ),
            "source_path": normalize_source_relative_path(
                source_path or ".", allow_root=True
            ),
            "explicitly_included": sorted(
                normalize_explicit_include_paths(explicitly_included)
            ),
        }

    @staticmethod
    def _entry(value: object) -> WorkspaceEntry:
        if not isinstance(value, dict):
            raise SourceValidationError(
                "Node Workspace manifest entry is invalid", code="source_manifest"
            )
        try:
            size = int(value.get("size"))
            mtime_ns = int(value.get("mtime_ns"))
        except (TypeError, ValueError) as exc:
            raise SourceValidationError(
                "Node Workspace manifest entry is invalid", code="source_manifest"
            ) from exc
        return WorkspaceEntry(
            relative_path=str(value.get("path") or ""),
            kind=str(value.get("kind") or ""),  # type: ignore[arg-type]
            size=size,
            mtime_ns=mtime_ns,
            executable=value.get("executable") is True,
            link_target=(
                str(value["link_target"])
                if value.get("link_target") is not None
                else None
            ),
        )

    @staticmethod
    def _result_integer(result: Mapping[str, Any], name: str) -> int:
        value = result.get(name)
        if isinstance(value, bool):
            raise SourceValidationError(
                "Node Workspace manifest metadata is invalid", code="source_manifest"
            )
        try:
            normalized = int(value)
        except (TypeError, ValueError) as exc:
            raise SourceValidationError(
                "Node Workspace manifest metadata is invalid", code="source_manifest"
            ) from exc
        if normalized < 0:
            raise SourceValidationError(
                "Node Workspace manifest metadata is invalid", code="source_manifest"
            )
        return normalized

    @staticmethod
    def _require_stream_contract(result: Mapping[str, Any]) -> None:
        if (
            type(result.get("remote_run_source_version")) is not int
            or result["remote_run_source_version"] != NODE_REMOTE_RUN_SOURCE_VERSION
            or type(result.get("stream_window")) is not int
            or result["stream_window"] != NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW
        ):
            raise NodeRemoteRunError(
                "Node Remote Run Source stream is incompatible; update Termroom",
                code="remote_run_source_version_incompatible",
            )

    @staticmethod
    def _stream_frame_count(result: Mapping[str, Any]) -> int:
        frame_count = result.get("frame_count")
        if type(frame_count) is not int or frame_count < 0:
            raise NodeRemoteRunError(
                "Node Remote Run Source frame count is invalid",
                code="source_stream_invalid",
            )
        return frame_count

    def _receive_window(
        self, stream: NodeStream, frame_count: int
    ) -> _WorkspaceSourceReceiveWindow:
        return _WorkspaceSourceReceiveWindow(
            self.client,
            stream,
            frame_count,
        )

    def scan(self) -> WorkspaceManifest:
        stream: NodeStream | None = None
        try:
            result, stream = self.client._open_stream(
                self.computer,
                "remote_run_source.manifest.open",
                self.payload,
            )
            self._require_stream_contract(result)
            frame_count = self._stream_frame_count(result)
            expected_count = self._result_integer(result, "entry_count")
            expected_total = self._result_integer(result, "total_bytes")
            receiver = self._receive_window(stream, frame_count)
            entries: list[WorkspaceEntry] = []
            buffered = bytearray()
            while True:
                chunk = receiver.receive()
                if chunk is None:
                    break
                buffered.extend(chunk)
                while True:
                    newline = buffered.find(b"\n")
                    if newline < 0:
                        break
                    line = bytes(buffered[:newline])
                    del buffered[: newline + 1]
                    if not line:
                        raise SourceValidationError(
                            "Node Workspace manifest stream is invalid",
                            code="source_manifest",
                        )
                    try:
                        value = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise SourceValidationError(
                            "Node Workspace manifest stream is invalid",
                            code="source_manifest",
                        ) from exc
                    entries.append(self._entry(value))
                if len(buffered) > MAX_NODE_MESSAGE_BYTES:
                    raise SourceValidationError(
                        "Node Workspace manifest entry is too large",
                        code="source_manifest",
                    )
            if buffered:
                raise SourceValidationError(
                    "Node Workspace manifest stream ended mid-entry",
                    code="source_manifest",
                )
            manifest = build_workspace_manifest(entries)
            if (
                len(manifest.entries) != expected_count
                or manifest.total_bytes != expected_total
            ):
                raise SourceValidationError(
                    "Node Workspace manifest metadata does not match its entries",
                    code="source_manifest_total",
                )
            return manifest
        finally:
            if stream is not None:
                with contextlib.suppress(Exception):
                    self.client._submit(stream.abort())

    def _current_entry(self, relative_path: str) -> WorkspaceEntry | None:
        try:
            result = self.client._request(
                self.computer,
                "remote_run_source.stat",
                {**self.payload, "path": relative_path},
            )
            if (
                result.get("remote_run_source_version")
                != NODE_REMOTE_RUN_SOURCE_VERSION
            ):
                return None
            entry = self._entry(result.get("entry"))
            return entry if entry.kind == "file" else None
        except (NodeRemoteRunError, SourceValidationError):
            return None

    def _changed(self, relative_path: str, cause: BaseException | None = None) -> None:
        current = self._current_entry(relative_path)
        error = SourceFileChangedError(
            relative_path,
            current_size=current.size if current is not None else None,
            current_mtime_ns=current.mtime_ns if current is not None else None,
        )
        if cause is None:
            raise error
        raise error from cause

    def iter_file_chunks(
        self,
        entry: WorkspaceEntry,
        *,
        chunk_size: int,
    ) -> Iterator[bytes]:
        if entry.kind != "file":
            raise SourceValidationError(
                "Only regular manifest files can be read",
                code="source_entry_type",
                path=entry.relative_path,
            )
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        stream: NodeStream | None = None
        total = 0
        try:
            result, stream = self.client._open_stream(
                self.computer,
                "remote_run_source.file.open",
                {
                    **self.payload,
                    "path": entry.relative_path,
                    "expected_size": entry.size,
                    "expected_mtime_ns": entry.mtime_ns,
                    "executable": entry.executable,
                },
            )
            self._require_stream_contract(result)
            frame_count = self._stream_frame_count(result)
            expected_frames = (
                entry.size + MAX_NODE_STREAM_CHUNK_BYTES - 1
            ) // MAX_NODE_STREAM_CHUNK_BYTES
            if frame_count != expected_frames:
                raise NodeRemoteRunError(
                    "Node Remote Run Source file stream is invalid",
                    code="source_stream_invalid",
                )
            if (
                self._result_integer(result, "size") != entry.size
                or self._result_integer(result, "mtime_ns") != entry.mtime_ns
            ):
                self._changed(entry.relative_path)
            receiver = self._receive_window(stream, frame_count)
            while True:
                chunk = receiver.receive()
                if chunk is None:
                    break
                total += len(chunk)
                if total > entry.size:
                    self._changed(entry.relative_path)
                for offset in range(0, len(chunk), chunk_size):
                    value = chunk[offset : offset + chunk_size]
                    if value:
                        yield value
            if total != entry.size:
                self._changed(entry.relative_path)
        except NodeRemoteRunError as exc:
            if exc.code == "source_file_changed":
                self._changed(entry.relative_path, exc)
            raise
        finally:
            if stream is not None:
                with contextlib.suppress(Exception):
                    self.client._submit(stream.abort())


class NodeRemoteRunClient:
    """Synchronous Core-side facade used only from RemoteRunManager worker threads."""

    def __init__(self, nodes: NodeCore) -> None:
        self.nodes = nodes
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def supports_remote_run_source(self, computer: Mapping[str, Any]) -> bool:
        if computer.get("connection_method") != "node":
            return False
        try:
            status = self.nodes.status(str(computer["id"]))
        except (KeyError, NodeCoreError):
            return False
        return (
            computer.get("node_revoked_at") is None
            and "remote_run_source" in status.capabilities
        )

    @contextlib.contextmanager
    def remote_workspace_snapshot_source(
        self,
        workspace: Mapping[str, Any],
        relative_path: str = ".",
        *,
        explicitly_included: Iterable[str] = (),
    ) -> Iterator[NodeWorkspaceSnapshotSource]:
        computer = workspace.get("computer")
        if not isinstance(computer, Mapping) or not self.supports_remote_run_source(
            computer
        ):
            raise NodeRemoteRunError(
                "This Remote does not support Workspace Sources",
                code="capability_unsupported",
            )
        yield NodeWorkspaceSnapshotSource(
            self,
            workspace,
            relative_path,
            explicitly_included=explicitly_included,
        )

    def _submit(self, awaitable: Any, *, timeout: float = 45.0) -> Any:
        loop = self._loop
        if loop is None or not loop.is_running():
            close = getattr(awaitable, "close", None)
            if close:
                close()
            raise NodeRemoteRunError("Node control loop is unavailable", code="node_offline")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            close = getattr(awaitable, "close", None)
            if close:
                close()
            raise RuntimeError("Node Remote Run client must run outside the Core event loop")
        future = asyncio.run_coroutine_threadsafe(awaitable, loop)
        try:
            return future.result(timeout=timeout)
        except NodeCoreError as exc:
            raise NodeRemoteRunError(str(exc), code=exc.code) from exc
        except TimeoutError as exc:
            future.cancel()
            raise NodeRemoteRunError("Node did not answer in time", code="node_offline") from exc

    def _request(
        self,
        computer: Mapping[str, Any],
        operation: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            connection = self.nodes.connection(str(computer["id"]))
        except (KeyError, NodeCoreError) as exc:
            code = getattr(exc, "code", "node_offline")
            raise NodeRemoteRunError(str(exc) or "Node is offline", code=code) from exc
        result = self._submit(connection.request(operation, payload))
        if not isinstance(result, dict):
            raise NodeRemoteRunError("Node returned an invalid response", code="response_invalid")
        return result

    def _open_stream(
        self,
        computer: Mapping[str, Any],
        operation: str,
        payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], NodeStream]:
        try:
            connection = self.nodes.connection(str(computer["id"]))
        except (KeyError, NodeCoreError) as exc:
            code = getattr(exc, "code", "node_offline")
            raise NodeRemoteRunError(str(exc) or "Node is offline", code=code) from exc
        return self._submit(connection.open_stream(operation, payload))

    @staticmethod
    def _payload(run_base: str, run_id: str, **values: Any) -> dict[str, Any]:
        return {
            "remote_run_version": NODE_REMOTE_RUN_VERSION,
            "run_base": run_base,
            "run_id": _validate_run_id(run_id),
            **values,
        }

    def preflight_remote_run_target(
        self,
        computer: Mapping[str, Any],
        *,
        run_base_dir: str | None = None,
        require_git: bool = False,
    ) -> dict[str, Any]:
        del run_base_dir
        return self._request(
            computer,
            "remote_run.preflight",
            {
                "remote_run_version": NODE_REMOTE_RUN_VERSION,
                "require_git": require_git,
            },
        )

    def create_remote_run_layout(
        self,
        computer: Mapping[str, Any],
        run_id: str,
        *,
        run_base_dir: str | None = None,
        command: str | None = None,
        cwd_rel: str = ".",
    ) -> dict[str, str]:
        if not run_base_dir:
            raise NodeRemoteRunError("Node Remote Run root is missing", code="run_root_invalid")
        result = self._request(
            computer,
            "remote_run.create",
            self._payload(run_base_dir, run_id, command=command, cwd_rel=cwd_rel),
        )
        return {key: str(value) for key, value in result.items() if isinstance(value, str)}

    @contextlib.contextmanager
    def remote_run_snapshot_sink(
        self,
        computer: Mapping[str, Any],
        run_base: str,
        run_id: str,
        *,
        reset_staging: bool = True,
    ):
        if reset_staging:
            self._request(
                computer,
                "remote_run.snapshot.begin",
                self._payload(run_base, run_id),
            )
        yield NodeRemoteRunSnapshotSink(self, computer, run_base, run_id)

    def commit_remote_run_snapshot(
        self, computer: Mapping[str, Any], run_base: str, run_id: str
    ) -> str:
        result = self._request(
            computer,
            "remote_run.snapshot.commit",
            self._payload(run_base, run_id),
        )
        return str(result.get("work_path") or "")

    def write_remote_run_json(
        self,
        computer: Mapping[str, Any],
        run_base: str,
        run_id: str,
        name: str,
        value: dict[str, Any] | list[Any],
    ) -> str:
        encoded = (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
        if len(encoded) <= 512 * 1024:
            self._request(
                computer,
                "remote_run.metadata.write",
                self._payload(run_base, run_id, name=name, value=value),
            )
        else:
            stream: NodeStream | None = None
            try:
                _result, stream = self._open_stream(
                    computer,
                    "remote_run.metadata.open",
                    self._payload(
                        run_base,
                        run_id,
                        name=name,
                        expected_size=len(encoded),
                    ),
                )
                self._submit(stream.send(encoded))
                result = self._submit(stream.finish())
                if int(result.get("size", -1)) != len(encoded):
                    raise NodeRemoteRunError(
                        "Node returned an invalid metadata size",
                        code="metadata_invalid",
                    )
            except BaseException:
                if stream is not None:
                    with contextlib.suppress(Exception):
                        self._submit(stream.abort())
                raise
        return f"{run_base.rstrip('/')}/{run_id}/.termroom/{name}"

    def start_remote_run(
        self, computer: Mapping[str, Any], run_base: str, run_id: str
    ) -> dict[str, Any]:
        return self._request(
            computer, "remote_run.start", self._payload(run_base, run_id)
        )

    def start_remote_git_run(
        self,
        computer: Mapping[str, Any],
        run_base: str,
        run_id: str,
        invocation: Any,
    ) -> dict[str, Any]:
        argv = tuple(getattr(invocation, "argv", ()))
        if len(argv) < 2:
            raise NodeRemoteRunError("Git Source invocation is invalid", code="git_url_invalid")
        url = validate_public_https_git_url(str(argv[-2]))
        return self._request(
            computer,
            "remote_run.git.start",
            self._payload(run_base, run_id, url=url),
        )

    def remote_run_git_clone_parameters(
        self, computer: Mapping[str, Any], run_base: str, run_id: str
    ) -> dict[str, str]:
        preflight = self.preflight_remote_run_target(computer, require_git=True)
        root = f"{run_base.rstrip('/')}/{run_id}"
        metadata = f"{root}/.termroom"
        return {
            "git_path": str(preflight["tools"]["git"]),
            "askpass_path": f"{metadata}/git-askpass",
            "empty_home": f"{metadata}/git-home",
            "destination": f"{root}/work.tmp",
        }

    def reconcile_remote_run(
        self, computer: Mapping[str, Any], run_base: str, run_id: str
    ) -> dict[str, Any]:
        return self._request(
            computer, "remote_run.observe", self._payload(run_base, run_id)
        )

    def poll_remote_run(
        self,
        computer: Mapping[str, Any],
        run_base: str,
        run_id: str,
        *,
        stream: str = "command",
        offset: int | None = None,
        limit: int = REMOTE_RUN_LOG_READ_LIMIT,
    ) -> dict[str, Any]:
        return self._request(
            computer,
            "remote_run.poll",
            self._payload(
                run_base, run_id, stream=stream, offset=offset, limit=limit
            ),
        )

    def read_remote_run_log(
        self,
        computer: Mapping[str, Any],
        run_base: str,
        run_id: str,
        *,
        stream: str = "command",
        offset: int | None = None,
        limit: int = REMOTE_RUN_LOG_READ_LIMIT,
    ) -> dict[str, Any]:
        result = self.poll_remote_run(
            computer,
            run_base,
            run_id,
            stream=stream,
            offset=offset,
            limit=limit,
        )
        log = result.get("log")
        if not isinstance(log, dict):
            raise NodeRemoteRunError("Node returned an invalid Remote Run log", code="log_invalid")
        return dict(log)

    def interrupt_remote_run(
        self, computer: Mapping[str, Any], run_base: str, run_id: str
    ) -> dict[str, Any]:
        return self._request(
            computer, "remote_run.interrupt", self._payload(run_base, run_id)
        )

    def kill_remote_run(
        self, computer: Mapping[str, Any], run_base: str, run_id: str
    ) -> dict[str, Any]:
        return self._request(
            computer, "remote_run.kill", self._payload(run_base, run_id)
        )

    def remote_run_layout_exists(
        self, computer: Mapping[str, Any], run_base: str, run_id: str
    ) -> bool:
        result = self._request(
            computer, "remote_run.exists", self._payload(run_base, run_id)
        )
        return result.get("exists") is True

    def ensure_remote_run_workspace_shell(
        self,
        computer: Mapping[str, Any],
        run_base: str,
        run_id: str,
        *,
        allow_create_session: bool = False,
    ) -> dict[str, Any]:
        return self._request(
            computer,
            "remote_run.ensure_shell",
            self._payload(
                run_base, run_id, allow_create_session=allow_create_session
            ),
        )

    def delete_remote_run_root(
        self, computer: Mapping[str, Any], run_base: str, run_id: str
    ) -> dict[str, Any]:
        return self._request(
            computer, "remote_run.delete", self._payload(run_base, run_id)
        )
