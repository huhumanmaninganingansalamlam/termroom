from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import hashlib
import heapq
import json
import os
import posixpath
import shlex
import shutil
import signal
import socket
import stat as stat_module
import struct
import subprocess
import sys
import termios
import time
import uuid
from collections.abc import AsyncIterator, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import paramiko
from fastapi import WebSocket, WebSocketDisconnect
from starlette.datastructures import UploadFile

from termroom.db import StateStore
from termroom.files import (
    DEFAULT_RECENT_EXCLUDES,
    MAX_RECENT_IGNORE_BYTES,
    RECENT_IGNORE_FILE,
    FileConflictError,
    FileEntry,
    FileSnapshot,
    RecentFiles,
    TextPreview,
    UnsupportedFileError,
    decode_utf8_preview,
    parse_recent_ignore_patterns,
    recent_path_ignored,
)
from termroom.pty_process import spawn_pty_process
from termroom.secrets import SecretStore, SecretStoreError
from termroom.security import file_digest
from termroom.terminal_control import TerminalControl
from termroom.terminals import (
    MAX_TERMINAL_MESSAGE_BYTES,
    TerminalOutputDecoder,
    normalize_terminal_name,
    terminal_size,
)
from termroom.workspaces import ProjectPathExists, validate_project_name

REMOTE_RUN_LOG_READ_LIMIT = 256 * 1024
REMOTE_RUN_INITIAL_TAIL = 64 * 1024
REMOTE_RUN_SESSION_PREFIX = "termroom-run-"
REMOTE_RUN_DELETE_TIMEOUT_SECONDS = 10 * 60

REMOTE_RUN_LOG_PIPE_SCRIPT = r"""#!/bin/bash
set -u
umask 077
script_path=${BASH_SOURCE[0]}
meta_dir=${script_path%/*}
meta_dir=$(CDPATH= cd -- "$meta_dir" && pwd -P) || exit 120
cat >> "$meta_dir/output.log"
log_size=$(wc -c < "$meta_dir/output.log")
log_size=${log_size//[[:space:]]/}
sealed_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
temporary="$meta_dir/output-seal.json.tmp.$$"
printf '{"size":%s,"sealed_at":"%s"}\n' "${log_size:-0}" "$sealed_at" \
    > "$temporary" || exit 120
chmod 0600 "$temporary" || exit 120
mv -f -- "$temporary" "$meta_dir/output-seal.json"
"""

# This script is metadata owned by Termroom, not user input.  The command entered by
# the user is stored only in command.sh.  In particular, neither the command nor the
# selected working directory is interpolated into the tmux command line.
REMOTE_RUNNER_SCRIPT = r"""#!/bin/bash
set -u
umask 077

script_path=${BASH_SOURCE[0]}
meta_dir=${script_path%/*}
meta_dir=$(CDPATH= cd -- "$meta_dir" && pwd -P) || exit 120
run_root=$(CDPATH= cd -- "$meta_dir/.." && pwd -P) || exit 120

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
    printf '{"state":"failed","error_code":"%s","ended_at":"%s"}\n' \
        "$code" "$ended_at" | atomic_record "$meta_dir/prepare-result.json"
    exit 120
}

test -f "$meta_dir/command.sh" || prepare_failed command_missing
test -f "$meta_dir/cwd" || prepare_failed cwd_missing
IFS= read -r cwd_rel < "$meta_dir/cwd" || prepare_failed cwd_invalid
case "$cwd_rel" in
    ''|/*|*$'\n'*|*$'\r'*) prepare_failed cwd_invalid ;;
esac
case "/$cwd_rel/" in
    */../*|*/./../*) prepare_failed cwd_invalid ;;
esac

work_root=$(CDPATH= cd -- "$run_root/work" && pwd -P) || prepare_failed work_missing
work_dir=$(CDPATH= cd -- "$work_root/$cwd_rel" && pwd -P) || prepare_failed cwd_missing
case "$work_dir/" in
    "$work_root/"*) ;;
    *) prepare_failed cwd_outside ;;
esac

if test -f "$meta_dir/stop-requested-at"; then
    ended_at=$(utc_now)
    printf '{"state":"stopped","error_code":"cancelled","ended_at":"%s"}\n' \
        "$ended_at" | atomic_record "$meta_dir/prepare-result.json"
    exit 130
fi

started_at=$(utc_now)
printf '{"phase":"running","started_at":"%s"}\n' "$started_at" \
    | atomic_record "$meta_dir/state.json" || exit 120

: > "$meta_dir/output.log" || exit 120
chmod 0600 "$meta_dir/output.log" || exit 120
rm -f -- "$meta_dir/output-seal.json"
cd -- "$work_dir" || prepare_failed cwd_missing

status=0
pipe_active=false
if test -n "${TMUX_PANE:-}" && command -v tmux >/dev/null 2>&1; then
    printf -v pipe_command '/bin/bash --noprofile --norc %q' "$meta_dir/log-pipe.sh"
    if tmux pipe-pane -o -t "$TMUX_PANE" "$pipe_command"; then
        pipe_active=true
    fi
fi

if test "$pipe_active" = true; then
    /bin/bash --noprofile --norc -- "$meta_dir/command.sh" \
        </dev/null 2>&1 || status=$?
    tmux pipe-pane -t "$TMUX_PANE" || true
    attempts=0
    while ! test -f "$meta_dir/output-seal.json" && test "$attempts" -lt 40; do
        sleep 0.05
        attempts=$((attempts + 1))
    done
else
    /bin/bash --noprofile --norc -- "$meta_dir/command.sh" \
        </dev/null >>"$meta_dir/output.log" 2>&1 || status=$?
    sealed_at=$(utc_now)
    log_size=$(wc -c < "$meta_dir/output.log")
    log_size=${log_size//[[:space:]]/}
    printf '{"size":%s,"sealed_at":"%s"}\n' "${log_size:-0}" "$sealed_at" \
        | atomic_record "$meta_dir/output-seal.json" || true
fi

log_size=$(wc -c < "$meta_dir/output.log")
log_size=${log_size//[[:space:]]/}
log_incomplete=true
if test -f "$meta_dir/output-seal.json"; then
    log_incomplete=false
fi

stop_requested=false
if test -f "$meta_dir/stop-requested-at"; then
    stop_requested=true
fi
ended_at=$(utc_now)
{
    printf '{"exit_code":%s,"stop_requested":%s,' "$status" "$stop_requested"
    printf '"started_at":"%s","ended_at":"%s",' "$started_at" "$ended_at"
    printf '"log_size":%s,"log_incomplete":%s}\n' "${log_size:-0}" "$log_incomplete"
} | atomic_record "$meta_dir/completion.json"
exit "$status"
"""

REMOTE_GIT_BOOTSTRAP_SCRIPT = r"""#!/bin/bash
set -u
umask 077

script_path=${BASH_SOURCE[0]}
meta_dir=${script_path%/*}
meta_dir=$(CDPATH= cd -- "$meta_dir" && pwd -P) || exit 120
run_root=$(CDPATH= cd -- "$meta_dir/.." && pwd -P) || exit 120

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

prepare_result() {
    state=$1
    code=$2
    ended_at=$(utc_now)
    printf '{"state":"%s","error_code":"%s","ended_at":"%s"}\n' \
        "$state" "$code" "$ended_at" | atomic_record "$meta_dir/prepare-result.json"
}

: > "$meta_dir/prepare.log" || exit 120
chmod 0600 "$meta_dir/prepare.log" || exit 120
exec >>"$meta_dir/prepare.log" 2>&1

started_at=$(utc_now)
printf '{"phase":"cloning","started_at":"%s"}\n' "$started_at" \
    | atomic_record "$meta_dir/state.json" || exit 120

if test -f "$meta_dir/stop-requested-at"; then
    prepare_result stopped cancelled
    exit 130
fi

clone_argv=()
while IFS= read -r -d '' value; do
    clone_argv+=("$value")
done < "$meta_dir/git-argv"
test ${#clone_argv[@]} -gt 0 || { prepare_result failed git_metadata_invalid; exit 120; }

rm -rf -- "$run_root/work.tmp"
"${clone_argv[@]}"
status=$?
if test "$status" -ne 0; then
    if test -f "$meta_dir/stop-requested-at"; then
        prepare_result stopped cancelled
    else
        prepare_result failed git_clone_failed
    fi
    exit "$status"
fi

IFS= read -r git_path < "$meta_dir/git-path" || {
    prepare_result failed git_metadata_invalid
    exit 120
}
revision=$("$git_path" -C "$run_root/work.tmp" rev-parse --verify HEAD 2>/dev/null) || {
    prepare_result failed git_empty_repository
    exit 120
}
case "$revision" in
    *[!0-9a-fA-F]*|'') prepare_result failed git_revision_invalid; exit 120 ;;
esac
printf '%s\n' "$revision" | atomic_record "$meta_dir/git-revision" || exit 120

if test -f "$meta_dir/stop-requested-at"; then
    prepare_result stopped cancelled
    exit 130
fi
rmdir -- "$run_root/work" || { prepare_result failed work_already_committed; exit 120; }
mv -- "$run_root/work.tmp" "$run_root/work" || {
    mkdir -m 0700 -- "$run_root/work" 2>/dev/null || true
    prepare_result failed work_commit_failed
    exit 120
}

started_at=$(utc_now)
printf '{"phase":"running","started_at":"%s"}\n' "$started_at" \
    | atomic_record "$meta_dir/state.json" || {
        prepare_result failed state_publish_failed
        exit 120
    }

exec /bin/bash --noprofile --norc "$meta_dir/runner.sh"
"""


class _SSHRemoteWorkspaceSnapshotSource:
    """A structural ``SnapshotSource`` backed by one already-open SFTP client."""

    def __init__(
        self,
        backend: SSHBackend,
        sftp: paramiko.SFTPClient,
        workspace: dict[str, Any],
        source_root: str,
        *,
        exclusions: Iterable[str],
        explicitly_included: Iterable[str],
    ) -> None:
        from termroom.run_sources import normalize_source_relative_path

        self.backend = backend
        self.sftp = sftp
        self.workspace = workspace
        self.source_root = source_root
        self.exclusions = frozenset(
            normalize_source_relative_path(value) for value in exclusions
        )
        self.explicitly_included = frozenset(
            normalize_source_relative_path(value) for value in explicitly_included
        )

    @staticmethod
    def _related(path: str, values: frozenset[str]) -> bool:
        return any(
            path == value
            or path.startswith(value + "/")
            or value.startswith(path + "/")
            for value in values
        )

    def scan(self) -> Any:
        from termroom.run_sources import (
            SourceValidationError,
            WorkspaceEntry,
            build_workspace_manifest,
            is_default_workspace_excluded,
            normalize_source_relative_path,
            validate_contained_symlink_target,
        )

        collected: list[Any] = []

        def walk(remote_directory: str, prefix: str = "") -> None:
            try:
                children = sorted(
                    self.sftp.listdir_attr(remote_directory), key=lambda item: item.filename
                )
            except OSError as exc:
                raise SourceValidationError(
                    f"Cannot scan remote Workspace directory: {prefix or '.'}",
                    code="source_scan_failed",
                    path=prefix or ".",
                ) from exc
            for child in children:
                raw_relative = f"{prefix}/{child.filename}" if prefix else child.filename
                if child.filename == ".termroom":
                    continue
                relative = normalize_source_relative_path(raw_relative)
                if any(
                    relative == excluded or relative.startswith(excluded + "/")
                    for excluded in self.exclusions
                ):
                    continue
                if is_default_workspace_excluded(relative) and not self._related(
                    relative, self.explicitly_included
                ):
                    continue
                remote = posixpath.join(remote_directory, child.filename)
                mode = int(child.st_mode)
                mtime_ns = int(child.st_mtime or 0) * 1_000_000_000
                if stat_module.S_ISLNK(mode):
                    try:
                        target = str(self.sftp.readlink(remote))
                    except OSError as exc:
                        raise SourceValidationError(
                            f"Cannot read remote symbolic link: {relative}",
                            code="source_scan_failed",
                            path=relative,
                        ) from exc
                    validate_contained_symlink_target(relative, target)
                    collected.append(
                        WorkspaceEntry(
                            relative,
                            "symlink",
                            mtime_ns=mtime_ns,
                            link_target=target,
                        )
                    )
                elif stat_module.S_ISDIR(mode):
                    collected.append(
                        WorkspaceEntry(relative, "directory", mtime_ns=mtime_ns)
                    )
                    walk(remote, relative)
                elif stat_module.S_ISREG(mode):
                    collected.append(
                        WorkspaceEntry(
                            relative,
                            "file",
                            size=int(child.st_size or 0),
                            mtime_ns=mtime_ns,
                            executable=bool(mode & 0o111),
                        )
                    )
                else:
                    raise SourceValidationError(
                        f"Unsupported special file in remote Workspace: {relative}",
                        code="source_special_file",
                        path=relative,
                    )

        walk(self.source_root)
        return build_workspace_manifest(collected)

    def iter_file_chunks(self, entry: Any, *, chunk_size: int) -> Iterator[bytes]:
        from termroom.run_sources import (
            SourceFileChangedError,
            SourceValidationError,
            normalize_source_relative_path,
        )

        if entry.kind != "file":
            raise SourceValidationError(
                "Only regular manifest files can be read",
                code="source_entry_type",
                path=entry.relative_path,
            )
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        relative = normalize_source_relative_path(entry.relative_path)
        remote = self.backend._resolve_real_remote_tree_path(
            self.sftp, self.source_root, relative, expected_directory=False
        )[0]
        try:
            with self.sftp.open(remote, "rb") as handle:
                before = handle.stat()
                before_mtime_ns = int(before.st_mtime or 0) * 1_000_000_000
                if (
                    not stat_module.S_ISREG(before.st_mode)
                    or int(before.st_size or 0) != entry.size
                    or before_mtime_ns != entry.mtime_ns
                ):
                    raise SourceFileChangedError(
                        relative,
                        current_size=int(before.st_size or 0),
                        current_mtime_ns=before_mtime_ns,
                    )
                total = 0
                while True:
                    chunk = handle.read(chunk_size)
                    if not chunk:
                        break
                    value = chunk.encode() if isinstance(chunk, str) else bytes(chunk)
                    total += len(value)
                    if total > entry.size:
                        current = handle.stat()
                        raise SourceFileChangedError(
                            relative,
                            current_size=int(current.st_size or 0),
                            current_mtime_ns=int(current.st_mtime or 0) * 1_000_000_000,
                        )
                    yield value
                after = handle.stat()
                if (
                    total != entry.size
                    or int(after.st_size or 0) != int(before.st_size or 0)
                    or int(after.st_mtime or 0) != int(before.st_mtime or 0)
                ):
                    raise SourceFileChangedError(
                        relative,
                        current_size=int(after.st_size or 0),
                        current_mtime_ns=int(after.st_mtime or 0) * 1_000_000_000,
                    )
        except SourceValidationError:
            raise
        except OSError as exc:
            raise SourceValidationError(
                f"Cannot read remote Workspace file: {relative}",
                code="source_read_failed",
                path=relative,
            ) from exc


class _SSHRemoteRunSnapshotSink:
    """A structural ``SnapshotSink`` writing through one target SFTP connection."""

    def __init__(
        self,
        backend: SSHBackend,
        sftp: paramiko.SFTPClient,
        staging_root: str,
    ) -> None:
        self.backend = backend
        self.sftp = sftp
        self.staging_root = staging_root

    def make_directory(self, relative_path: str, *, executable: bool = False) -> None:
        del executable
        remote = self.backend._new_remote_tree_path(
            self.sftp, self.staging_root, relative_path, create_parents=True
        )
        try:
            attr = self.sftp.lstat(remote)
        except OSError as exc:
            if not self.backend._is_missing_sftp_error(exc):
                raise
            self.sftp.mkdir(remote, mode=0o755)
            return
        if stat_module.S_ISLNK(attr.st_mode) or not stat_module.S_ISDIR(attr.st_mode):
            raise SSHBackendError(f"Snapshot directory conflicts with a file: {relative_path}")

    def write_file(
        self,
        relative_path: str,
        chunks: Iterable[bytes],
        *,
        executable: bool,
        expected_size: int,
    ) -> None:
        from termroom.run_sources import SourceFileChangedError

        remote = self.backend._new_remote_tree_path(
            self.sftp, self.staging_root, relative_path, create_parents=True
        )
        temporary = posixpath.join(
            posixpath.dirname(remote),
            f".{posixpath.basename(remote)}.termroom-{uuid.uuid4()}.part",
        )
        total = 0
        try:
            with self.sftp.open(temporary, "wb") as handle:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("Snapshot chunks must be bytes")
                    total += len(chunk)
                    if total > expected_size:
                        raise SourceFileChangedError(relative_path)
                    if chunk:
                        handle.write(chunk)
            if total != expected_size:
                raise SourceFileChangedError(relative_path)
            self.sftp.chmod(temporary, 0o755 if executable else 0o644)
            self.sftp.rename(temporary, remote)
        finally:
            with contextlib.suppress(OSError):
                self.sftp.remove(temporary)

    def make_symlink(self, relative_path: str, link_target: str) -> None:
        from termroom.run_sources import validate_contained_symlink_target

        target = validate_contained_symlink_target(relative_path, link_target)
        remote = self.backend._new_remote_tree_path(
            self.sftp, self.staging_root, relative_path, create_parents=True
        )
        self.sftp.symlink(target, remote)


class SSHBackendError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        locale_key: str | None = None,
        locale_values: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.locale_key = locale_key
        self.locale_values = locale_values or {}


class SSHCommandStatusUnknown(SSHBackendError):
    """The SSH channel closed without proving whether its command was accepted."""


class RemoteRunLayoutError(SSHBackendError):
    """The SSH connection works, but a managed Run layout is not trustworthy."""

    def __init__(self, message: str, *, code: str = "layout_incomplete") -> None:
        super().__init__(message)
        self.code = code


class SSHHostKeyChanged(SSHBackendError):
    pass


class _ExpectedHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, key_type: str, key_data: str) -> None:
        self.key_type = key_type
        self.key_data = key_data

    def missing_host_key(
        self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey
    ) -> None:
        del client, hostname
        actual = key.get_base64()
        if key.get_name() != self.key_type or actual != self.key_data:
            raise SSHHostKeyChanged("SSH host key changed; reconnect only after verifying it")


class SSHBackend:
    def __init__(
        self,
        store: StateStore,
        state_dir: Path,
        control: TerminalControl | None = None,
    ) -> None:
        self.store = store
        self.state_dir = state_dir
        self.ssh_dir = state_dir / "ssh"
        self.ssh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ssh_dir.chmod(0o700)
        self.known_hosts_path = self.ssh_dir / "known_hosts"
        self.control = control or TerminalControl()
        self.secrets = SecretStore(state_dir)
        self.secrets.initialize()

    @property
    def managed_key_path(self) -> Path:
        return self.ssh_dir / "id_ed25519"

    def ensure_managed_key(self) -> dict[str, str]:
        if shutil.which("ssh-keygen") is None:
            raise SSHBackendError(
                "ssh-keygen is required to create the Termroom SSH key",
                locale_key="ssh.backend.ssh_keygen_missing",
            )
        private_key = self.managed_key_path
        public_key = private_key.with_suffix(".pub")
        private_key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not private_key.exists() or not public_key.exists():
            try:
                subprocess.run(
                    [
                        "ssh-keygen",
                        "-q",
                        "-t",
                        "ed25519",
                        "-N",
                        "",
                        "-C",
                        "termroom",
                        "-f",
                        str(private_key),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except FileNotFoundError as exc:
                raise SSHBackendError(
                    "ssh-keygen is required to create the Termroom SSH key"
                ) from exc
            except subprocess.CalledProcessError as exc:
                raise SSHBackendError(
                    exc.stderr.strip() or "Could not create the Termroom SSH key"
                ) from exc
        private_key.chmod(0o600)
        public_key.chmod(0o644)
        value = public_key.read_text(encoding="utf-8").strip()
        if not value.startswith("ssh-ed25519 "):
            raise SSHBackendError("Generated Termroom SSH public key is invalid")
        return {
            "private_key": str(private_key.resolve()),
            "public_key": value,
        }

    def save_password(self, computer_id: str, password: str) -> None:
        try:
            self.secrets.put(computer_id, password)
        except (OSError, ValueError, SecretStoreError) as exc:
            raise SSHBackendError(
                "Could not store the SSH password securely",
                locale_key="ssh.backend.credential_save",
            ) from exc

    def delete_password(self, computer_id: str) -> None:
        self.secrets.delete(computer_id)

    def forget_host_key(self, computer_id: str) -> None:
        alias = f"termroom-{computer_id}"
        if not self.known_hosts_path.exists():
            return
        existing = self.known_hosts_path.read_text(encoding="utf-8")
        filtered = [row for row in existing.splitlines() if not row.startswith(alias + " ")]
        temporary = self.known_hosts_path.with_suffix(".tmp")
        body = "\n".join(filtered)
        temporary.write_text(body + ("\n" if body else ""), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, self.known_hosts_path)

    def _stored_password(self, computer: dict[str, Any]) -> str:
        computer_id = str(computer.get("id") or "")
        if not computer_id:
            raise SSHBackendError(
                "Stored SSH credential identifier is missing",
                locale_key="ssh.backend.credential_missing",
            )
        try:
            return self.secrets.get(computer_id)
        except (OSError, ValueError, SecretStoreError) as exc:
            raise SSHBackendError(
                "Could not read the stored SSH password",
                locale_key="ssh.backend.credential_read",
            ) from exc

    def resolve_target(self, value: str) -> dict[str, Any]:
        alias = value.strip()
        if not alias:
            raise ValueError("SSH host or config alias is required")
        config = paramiko.SSHConfig()
        for config_path in (self.ssh_dir / "config", Path.home() / ".ssh" / "config"):
            if config_path.is_file():
                with config_path.open(encoding="utf-8") as handle:
                    config.parse(handle)
        resolved = config.lookup(alias)
        host = str(resolved.get("hostname") or alias)
        port = int(resolved.get("port") or 22)
        username = str(resolved.get("user") or os.environ.get("USER") or "")
        identities = resolved.get("identityfile") or []
        if isinstance(identities, str):
            identities = [identities]
        identity_file = str(identities[0]) if identities else ""
        return {
            "ssh_alias": alias,
            "host": host,
            "port": port,
            "username": username,
            "identity_file": os.path.expanduser(identity_file) if identity_file else "",
            "proxycommand": str(resolved.get("proxycommand") or ""),
        }

    @staticmethod
    def probe_host_key(host: str, port: int, *, timeout: float = 8.0) -> dict[str, str]:
        try:
            connection = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise SSHBackend.connection_error(exc, host, port) from exc
        transport = paramiko.Transport(connection)
        try:
            transport.start_client(timeout=timeout)
            key = transport.get_remote_server_key()
            fingerprint = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii")
            return {
                "host_key_type": key.get_name(),
                "host_key_data": key.get_base64(),
                "host_fingerprint": "SHA256:" + fingerprint.rstrip("="),
            }
        finally:
            transport.close()
            connection.close()

    def remember_host_key(self, computer: dict[str, Any]) -> None:
        alias = f"termroom-{computer['id']}"
        line = f"{alias} {computer['host_key_type']} {computer['host_key_data']}\n"
        existing = ""
        if self.known_hosts_path.exists():
            existing = self.known_hosts_path.read_text(encoding="utf-8")
        filtered = [row for row in existing.splitlines() if not row.startswith(alias + " ")]
        filtered.append(line.rstrip("\n"))
        temporary = self.known_hosts_path.with_suffix(".tmp")
        temporary.write_text("\n".join(filtered) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, self.known_hosts_path)

    def test_connection(self, computer: dict[str, Any]) -> dict[str, str]:
        self._require_ssh_client()
        client = self._connect(computer)
        return self._connection_info(client)

    def test_password_connection(
        self, computer: dict[str, Any], password: str
    ) -> dict[str, str]:
        self._require_ssh_client()
        client = self._connect_password(computer, password)
        return self._connection_info(client)

    @staticmethod
    def _require_ssh_client() -> None:
        if shutil.which("ssh") is None:
            raise SSHBackendError(
                "OpenSSH client is required for Termroom SSH terminals",
                locale_key="ssh.backend.ssh_missing",
            )

    def _connection_info(self, client: paramiko.SSHClient) -> dict[str, str]:
        try:
            script = (
                "printf 'shell=%s\\n' \"${SHELL:-unknown}\"; "
                "if command -v tmux >/dev/null 2>&1; "
                "then tmux -V; else echo 'tmux=missing'; fi"
            )
            output = self._exec_client(
                client,
                self._remote_posix_command(script),
            )
        finally:
            client.close()
        lines = output.strip().splitlines()
        return {
            "shell": next((line[6:] for line in lines if line.startswith("shell=")), "unknown"),
            "tmux": next((line for line in lines if line.startswith("tmux")), "unknown"),
        }

    def validate_workspace_path(self, computer: dict[str, Any], remote_path: str) -> str:
        normalized = posixpath.normpath(remote_path)
        if not normalized.startswith("/") or normalized == "/":
            raise ValueError("Remote Workspace must be an absolute non-root path")
        command = (
            f"test -d {shlex.quote(normalized)} || "
            "{ echo '__TERMROOM_NO_DIR__' >&2; exit 44; }; "
            "command -v tmux >/dev/null 2>&1 || "
            "{ echo '__TERMROOM_NO_TMUX__' >&2; exit 45; }; "
            f"cd {shlex.quote(normalized)} && pwd -P"
        )
        canonical = self._exec(
            computer, self._remote_posix_command(command)
        ).strip()
        if not canonical.startswith("/") or canonical == "/":
            raise SSHBackendError("Remote Workspace path could not be canonicalized safely")
        return canonical

    def home_directory(self, computer: dict[str, Any]) -> str:
        """Resolve the authenticated SSH user's real home directory over SFTP."""

        client = self._connect(computer)
        try:
            sftp = client.open_sftp()
        except (OSError, paramiko.SSHException) as exc:
            client.close()
            raise SSHBackendError(
                "Could not open SFTP to resolve the SSH home directory",
                locale_key="server_terminal.home_unavailable",
            ) from exc
        try:
            canonical = posixpath.normpath(sftp.normalize("."))
            if not canonical.startswith("/"):
                raise SSHBackendError(
                    "SSH home directory could not be canonicalized safely",
                    locale_key="server_terminal.home_unavailable",
                )
            attr = sftp.lstat(canonical)
            if stat_module.S_ISLNK(attr.st_mode) or not stat_module.S_ISDIR(attr.st_mode):
                raise SSHBackendError(
                    "SSH home directory is not a real directory",
                    locale_key="server_terminal.home_unavailable",
                )
            return canonical
        except paramiko.SSHException as exc:
            raise SSHBackendError(
                f"Could not resolve the SSH home directory: {exc}",
                locale_key="server_terminal.home_unavailable",
            ) from exc
        finally:
            sftp.close()
            client.close()

    def list_browse_directories(
        self,
        computer: dict[str, Any],
        remote_path: str | None = None,
        *,
        show_hidden: bool = False,
    ) -> dict[str, Any]:
        """List real directories for the pre-Workspace SSH folder picker.

        Unlike ``list_dir`` this operates before a Workspace exists, so paths are
        absolute and intentionally not constrained to a Workspace root. Symlinks
        are skipped to keep the picker predictable and consistent with the file UI.
        """

        client = self._connect(computer)
        try:
            sftp = client.open_sftp()
        except Exception:
            client.close()
            raise
        try:
            requested = (remote_path or ".").strip() or "."
            if requested != "." and not requested.startswith("/"):
                raise ValueError("Remote folder browser path must be absolute")
            attr = sftp.lstat(requested)
            if stat_module.S_ISLNK(attr.st_mode):
                raise SSHBackendError("Symbolic links are not exposed in the folder browser")
            if not stat_module.S_ISDIR(attr.st_mode):
                raise NotADirectoryError(requested)
            canonical = sftp.normalize(requested)
            if not canonical.startswith("/"):
                raise SSHBackendError("Remote folder path could not be canonicalized safely")

            hidden_count = 0
            entries: list[dict[str, str]] = []
            for child in sftp.listdir_attr(canonical):
                if stat_module.S_ISLNK(child.st_mode) or not stat_module.S_ISDIR(child.st_mode):
                    continue
                if child.filename.startswith("."):
                    hidden_count += 1
                    if not show_hidden:
                        continue
                entries.append(
                    {
                        "name": child.filename,
                        "path": posixpath.join(canonical, child.filename),
                    }
                )
            entries.sort(key=lambda item: item["name"].casefold())
            parent = None if canonical == "/" else (posixpath.dirname(canonical.rstrip("/")) or "/")
            return {
                "current": canonical,
                "parent": parent,
                "entries": entries,
                "hidden_count": hidden_count,
                "show_hidden": show_hidden,
            }
        except paramiko.SSHException as exc:
            raise SSHBackendError(f"Could not browse remote folders: {exc}") from exc
        finally:
            sftp.close()
            client.close()

    def create_project_directory(
        self, computer: dict[str, Any], parent_path: str, name: str
    ) -> str:
        safe_name = validate_project_name(name)
        requested = posixpath.normpath(parent_path)
        if not requested.startswith("/"):
            raise ValueError("Remote project parent must be an absolute path")
        client = self._connect(computer)
        try:
            sftp = client.open_sftp()
        except Exception:
            client.close()
            raise
        try:
            parent_attr = sftp.lstat(requested)
            if stat_module.S_ISLNK(parent_attr.st_mode) or not stat_module.S_ISDIR(
                parent_attr.st_mode
            ):
                raise NotADirectoryError(requested)
            parent = sftp.normalize(requested)
            target = posixpath.join(parent, safe_name)
            try:
                existing = sftp.lstat(target)
            except FileNotFoundError:
                existing = None
            except OSError as exc:
                if getattr(exc, "errno", None) == 2:
                    existing = None
                else:
                    raise
            if existing is not None:
                raise ProjectPathExists(
                    target, is_directory=stat_module.S_ISDIR(existing.st_mode)
                )
            sftp.mkdir(target, mode=0o755)
            canonical = sftp.normalize(target)
            if not canonical.startswith("/") or canonical == "/":
                raise SSHBackendError("Remote project path could not be canonicalized safely")
            return canonical
        finally:
            sftp.close()
            client.close()

    @staticmethod
    def validate_remote_run_id(run_id: str) -> str:
        """Return a canonical lowercase UUIDv4 or reject before remote side effects."""

        if not isinstance(run_id, str) or not run_id:
            raise ValueError("Remote Run id must be a canonical UUIDv4")
        try:
            parsed = uuid.UUID(run_id)
        except (ValueError, AttributeError) as exc:
            raise ValueError("Remote Run id must be a canonical UUIDv4") from exc
        if parsed.version != 4 or str(parsed) != run_id:
            raise ValueError("Remote Run id must be a canonical lowercase UUIDv4")
        return run_id

    @classmethod
    def remote_run_session_name(cls, run_id: str) -> str:
        return REMOTE_RUN_SESSION_PREFIX + cls.validate_remote_run_id(run_id)

    @contextlib.contextmanager
    def remote_run_connection(
        self, computer: dict[str, Any]
    ) -> Iterator[tuple[paramiko.SSHClient, paramiko.SFTPClient]]:
        """Own one SSH/SFTP pair for a complete Run operation.

        Snapshot transfer and a status+log poll must not reconnect for each file or
        sub-operation.  Callers that need that lifetime can use this context directly;
        the convenience methods below use it internally.
        """

        client = self._connect(computer)
        try:
            transport = client.get_transport()
            if transport is not None:
                transport.set_keepalive(15)
            sftp = client.open_sftp()
        except Exception:
            client.close()
            raise
        try:
            yield client, sftp
        finally:
            with contextlib.suppress(Exception):
                sftp.close()
            client.close()

    @contextlib.contextmanager
    def remote_workspace_snapshot_source(
        self,
        workspace: dict[str, Any],
        relative_path: str = ".",
        *,
        exclusions: Iterable[str] = (),
        explicitly_included: Iterable[str] = (),
    ) -> Iterator[Any]:
        """Yield one SnapshotSource while retaining a single Source SSH connection."""

        client, sftp = self._sftp(workspace)
        try:
            source_root, attr = self._existing_sftp_path(sftp, workspace, relative_path)
            if not stat_module.S_ISDIR(attr.st_mode):
                raise SSHBackendError("Remote Workspace Source must be a directory")
            yield _SSHRemoteWorkspaceSnapshotSource(
                self,
                sftp,
                workspace,
                source_root,
                exclusions=exclusions,
                explicitly_included=explicitly_included,
            )
        finally:
            with contextlib.suppress(Exception):
                sftp.close()
            client.close()

    def scan_remote_workspace_source(
        self,
        workspace: dict[str, Any],
        relative_path: str = ".",
        *,
        exclusions: Iterable[str] = (),
        explicitly_included: Iterable[str] = (),
    ) -> Any:
        """Convenience authoritative scan; transfer callers should use the context API."""

        with self.remote_workspace_snapshot_source(
            workspace,
            relative_path,
            exclusions=exclusions,
            explicitly_included=explicitly_included,
        ) as source:
            return source.scan()

    @contextlib.contextmanager
    def remote_run_snapshot_sink(
        self,
        computer: dict[str, Any],
        run_base: str,
        run_id: str,
        *,
        reset_staging: bool = True,
    ) -> Iterator[Any]:
        """Yield one SnapshotSink while retaining a single Target SSH connection."""

        with self.remote_run_connection(computer) as (client, sftp):
            paths = self._assert_remote_run_root(sftp, run_base, run_id)
            staging = paths["work_staging"]
            if reset_staging:
                if self._sftp_exists(sftp, staging):
                    self._remove_remote_run_work_staging(
                        client, sftp, paths, run_id
                    )
                sftp.mkdir(staging, mode=0o700)
            else:
                attr = sftp.lstat(staging)
                if stat_module.S_ISLNK(attr.st_mode) or not stat_module.S_ISDIR(attr.st_mode):
                    raise SSHBackendError("Remote Run staging root is invalid")
            yield _SSHRemoteRunSnapshotSink(self, sftp, staging)

    def commit_remote_run_snapshot(
        self,
        computer: dict[str, Any],
        run_base: str,
        run_id: str,
    ) -> str:
        """Atomically publish a completely materialized work.tmp as work."""

        with self.remote_run_connection(computer) as (_client, sftp):
            paths = self._assert_remote_run_root(sftp, run_base, run_id)
            staging_attr = sftp.lstat(paths["work_staging"])
            if stat_module.S_ISLNK(staging_attr.st_mode) or not stat_module.S_ISDIR(
                staging_attr.st_mode
            ):
                raise SSHBackendError("Remote Run staging root is invalid")
            work_attr = sftp.lstat(paths["work"])
            if stat_module.S_ISLNK(work_attr.st_mode) or not stat_module.S_ISDIR(
                work_attr.st_mode
            ):
                raise SSHBackendError("Remote Run work root is invalid")
            if sftp.listdir_attr(paths["work"]):
                raise SSHBackendError("Remote Run work root is already committed")
            sftp.rmdir(paths["work"])
            try:
                sftp.rename(paths["work_staging"], paths["work"])
            except Exception:
                with contextlib.suppress(OSError):
                    sftp.mkdir(paths["work"], mode=0o700)
                raise
            return paths["work"]

    def preflight_remote_run_target(
        self,
        computer: dict[str, Any],
        *,
        run_base_dir: str | None = None,
        require_git: bool = False,
    ) -> dict[str, Any]:
        """Check fixed runner tools and create/rename/delete access to the Run base."""

        with self.remote_run_connection(computer) as (client, sftp):
            run_base = self._canonical_remote_run_base(
                sftp, run_base_dir or str(computer.get("run_base_dir") or "")
            )
            probe = posixpath.join(run_base, f".termroom-probe-{uuid.uuid4()}")
            renamed = probe + ".renamed"
            try:
                sftp.mkdir(probe, mode=0o700)
                sftp.rename(probe, renamed)
                sftp.rmdir(renamed)
            except OSError as exc:
                with contextlib.suppress(OSError):
                    sftp.rmdir(probe)
                with contextlib.suppress(OSError):
                    sftp.rmdir(renamed)
                raise SSHBackendError(
                    f"Remote Run base is not writable: {run_base}"
                ) from exc

            tool_check = (
                "test -x /bin/bash || { echo '__TERMROOM_NO_BASH__' >&2; exit 47; }; "
                "command -v tmux >/dev/null 2>&1 || "
                "{ echo '__TERMROOM_NO_TMUX__' >&2; exit 45; }; "
            )
            if require_git:
                tool_check += (
                    "command -v git >/dev/null 2>&1 || "
                    "{ echo '__TERMROOM_NO_GIT__' >&2; exit 48; }; "
                )
            tool_check += (
                "printf 'bash=/bin/bash\\n'; "
                "printf 'tmux=%s\\n' \"$(command -v tmux)\"; "
                + ("printf 'git=%s\\n' \"$(command -v git)\"; " if require_git else "")
            )
            tools = self._exec_remote_run_bash(client, tool_check)

            available_bytes: int | None = None
            warnings: list[str] = []
            try:
                output = self._exec_remote_run_bash(
                    client,
                    f"df -Pk -- {shlex.quote(run_base)} | tail -n 1 | awk '{{print $4}}'",
                ).strip()
                if output:
                    available_bytes = int(output.splitlines()[-1]) * 1024
            except (SSHBackendError, ValueError):
                warnings.append("disk_space_unknown")
            return {
                "run_base": run_base,
                "tools": dict(
                    line.split("=", 1) for line in tools.splitlines() if "=" in line
                ),
                "available_bytes": available_bytes,
                "warnings": warnings,
            }

    def create_remote_run_layout(
        self,
        computer: dict[str, Any],
        run_id: str,
        *,
        run_base_dir: str | None = None,
        command: str | None = None,
        cwd_rel: str = ".",
    ) -> dict[str, str]:
        """Create or idempotently claim one UUID direct child of a canonical base."""

        run_id = self.validate_remote_run_id(run_id)
        cwd_rel = self._validate_run_relative_path(cwd_rel, directory=True)
        with self.remote_run_connection(computer) as (client, sftp):
            run_base = self._canonical_remote_run_base(
                sftp, run_base_dir or str(computer.get("run_base_dir") or "")
            )
            quarantine = posixpath.join(run_base, f".termroom-deleting-{run_id}")
            deletion_marker = self._remote_run_deletion_marker(run_base, run_id)
            if self._sftp_exists(sftp, quarantine) or self._sftp_exists(
                sftp, deletion_marker
            ):
                # A previous delete may have been interrupted after quarantine or
                # after the tree itself vanished.  Finish that protocol before
                # publishing a fresh UUID root for the same id.
                self._delete_remote_run_root_connection(
                    client, sftp, run_base, run_id
                )
            paths = self._remote_run_paths(run_base, run_id)
            try:
                existing = sftp.lstat(paths["root"])
            except OSError as exc:
                if not self._is_missing_sftp_error(exc):
                    raise
            else:
                if stat_module.S_ISLNK(existing.st_mode) or not stat_module.S_ISDIR(
                    existing.st_mode
                ):
                    raise SSHBackendError("Remote Run root is not a real directory")
                self._assert_remote_run_root(sftp, run_base, run_id)
                if command is not None:
                    expected = self._normalize_remote_run_command(command).encode("utf-8")
                    actual = self._read_sftp_bytes(sftp, paths["command"], max_bytes=256 * 1024)
                    if actual != expected:
                        raise SSHBackendError(
                            "Remote Run id already belongs to a different command"
                        )
                return {
                    **paths,
                    "run_base": run_base,
                    "session_name": self.remote_run_session_name(run_id),
                }

            creating = self._remote_run_creation_paths(run_base, run_id)
            if self._sftp_exists(sftp, creating["root"]):
                self._discard_remote_run_creation(
                    client, sftp, run_base, run_id
                )

            try:
                # Build the complete layout outside the public UUID path.  The
                # marker is the first file written, so a crash leaves either a
                # provably empty skeleton or a marked Termroom-owned tree.
                sftp.mkdir(creating["root"], mode=0o700)
                sftp.mkdir(creating["metadata"], mode=0o700)
                self._sftp_atomic_write(
                    sftp, creating["marker"], (run_id + "\n").encode()
                )
                sftp.mkdir(creating["work"], mode=0o700)
                self._sftp_atomic_write(sftp, creating["cwd"], (cwd_rel + "\n").encode())
                self._sftp_atomic_write(
                    sftp,
                    creating["runner"],
                    REMOTE_RUNNER_SCRIPT.encode("utf-8"),
                    mode=0o700,
                )
                self._sftp_atomic_write(
                    sftp,
                    creating["log_pipe"],
                    REMOTE_RUN_LOG_PIPE_SCRIPT.encode("utf-8"),
                    mode=0o700,
                )
                body = "" if command is None else self._normalize_remote_run_command(command)
                self._sftp_atomic_write(sftp, creating["command"], body.encode("utf-8"))
                sftp.rename(creating["root"], paths["root"])
            except Exception as exc:
                try:
                    self._discard_remote_run_creation(
                        client, sftp, run_base, run_id
                    )
                except Exception as cleanup_exc:
                    exc.add_note(f"Remote Run staging cleanup also failed: {cleanup_exc}")
                raise

            self._assert_remote_run_root(sftp, run_base, run_id)
            return {
                **paths,
                "run_base": run_base,
                "session_name": self.remote_run_session_name(run_id),
            }

    def write_remote_run_command(
        self,
        computer: dict[str, Any],
        run_base: str,
        run_id: str,
        command: str,
    ) -> str:
        body = self._normalize_remote_run_command(command)
        with self.remote_run_connection(computer) as (_client, sftp):
            paths = self._assert_remote_run_root(sftp, run_base, run_id)
            if self._sftp_exists(sftp, paths["state"]) or self._sftp_exists(
                sftp, paths["completion"]
            ):
                raise SSHBackendError("A started Remote Run command cannot be replaced")
            self._sftp_atomic_write(sftp, paths["command"], body.encode("utf-8"))
            return paths["command"]

    def write_remote_run_text(
        self,
        computer: dict[str, Any],
        run_base: str,
        run_id: str,
        name: str,
        value: str,
    ) -> str:
        allowed = {"cwd", "git-url", "git-revision"}
        if name not in allowed:
            raise ValueError("Unsupported Remote Run text metadata")
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise ValueError("Remote Run metadata must be one line")
        if name == "cwd":
            value = self._validate_run_relative_path(value, directory=True)
        with self.remote_run_connection(computer) as (_client, sftp):
            paths = self._assert_remote_run_root(sftp, run_base, run_id)
            target = posixpath.join(paths["metadata"], name)
            self._sftp_atomic_write(sftp, target, (value + "\n").encode("utf-8"))
            return target

    def write_remote_run_json(
        self,
        computer: dict[str, Any],
        run_base: str,
        run_id: str,
        name: str,
        value: dict[str, Any] | list[Any],
    ) -> str:
        allowed = {"source.json", "source-manifest.json", "inputs.json"}
        if name not in allowed:
            raise ValueError("Unsupported Remote Run JSON metadata")
        encoded = (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        with self.remote_run_connection(computer) as (_client, sftp):
            paths = self._assert_remote_run_root(sftp, run_base, run_id)
            target = posixpath.join(paths["metadata"], name)
            self._sftp_atomic_write(sftp, target, encoded)
            return target

    def start_remote_run(
        self,
        computer: dict[str, Any],
        run_base: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Start the fixed Termroom runner without putting user strings in tmux."""

        with self.remote_run_connection(computer) as (client, sftp):
            paths = self._assert_remote_run_root(sftp, run_base, run_id)
            command_attr = sftp.lstat(paths["command"])
            runner_attr = sftp.lstat(paths["runner"])
            log_pipe_attr = sftp.lstat(paths["log_pipe"])
            if not all(
                stat_module.S_ISREG(attr.st_mode)
                for attr in (command_attr, runner_attr, log_pipe_attr)
            ):
                raise SSHBackendError("Remote Run command metadata is invalid")
            if any(
                self._sftp_exists(sftp, paths[name])
                for name in ("state", "prepare_result", "stop", "completion")
            ):
                raise SSHBackendError("Remote Run has already started")

            session = self.remote_run_session_name(run_id)
            quoted_session = shlex.quote(session)
            quoted_work = shlex.quote(paths["work"])
            quoted_runner = shlex.quote(paths["runner"])
            remote_command = (
                "test -x /bin/bash || { echo '__TERMROOM_NO_BASH__' >&2; exit 47; }; "
                "command -v tmux >/dev/null 2>&1 || "
                "{ echo '__TERMROOM_NO_TMUX__' >&2; exit 45; }; "
                f"if tmux has-session -t {quoted_session} 2>/dev/null; then "
                "echo '__TERMROOM_RUN_EXISTS__' >&2; exit 46; fi; "
                f"tmux new-session -d -s {quoted_session} -c {quoted_work} -n run; "
                f"tmux set-window-option -t {quoted_session} remain-on-exit on; "
                f"tmux set-window-option -t {quoted_session} remain-on-exit-format '' "
                ">/dev/null 2>&1 || true; "
                f"tmux set-window-option -t {quoted_session} window-size latest "
                ">/dev/null 2>&1 || true; "
                f"tmux respawn-pane -k -t {quoted_session}:0.0 -c {quoted_work} "
                f"/bin/bash --noprofile --norc {quoted_runner}"
            )
            self._exec_remote_run_bash(client, remote_command)
            return {
                "state": "preparing",
                "session_name": session,
                "run_root": paths["root"],
            }

    def ensure_remote_run_workspace_shell(
        self,
        computer: dict[str, Any],
        run_base: str,
        run_id: str,
        *,
        allow_create_session: bool = False,
    ) -> dict[str, Any]:
        """Expose committed work through an idempotent tmux shell window.

        ``allow_create_session`` must only be set by a caller that has already
        established a terminal/lost Run state.  The default protects active Runs:
        a missing runner session is never silently replaced by a shell session.
        """

        with self.remote_run_connection(computer) as (client, sftp):
            paths = self._assert_remote_run_root(sftp, run_base, run_id)
            try:
                work_attr = sftp.lstat(paths["work"])
            except OSError as exc:
                raise SSHBackendError("Remote Run work directory is unavailable") from exc
            if stat_module.S_ISLNK(work_attr.st_mode) or not stat_module.S_ISDIR(
                work_attr.st_mode
            ):
                raise SSHBackendError("Remote Run work path is not a real directory")
            if posixpath.normpath(sftp.normalize(paths["work"])) != paths["work"]:
                raise SSHBackendError("Remote Run work path is not canonical")

            session = self.remote_run_session_name(run_id)
            quoted_session = shlex.quote(session)
            quoted_work = shlex.quote(paths["work"])
            session_status = self._remote_tmux_status(client, run_id)
            created_session = False
            if not session_status["exists"]:
                if not allow_create_session:
                    raise SSHBackendError(
                        "Remote Run tmux session is missing; "
                        "terminal state is required to recreate it"
                    )
                output = self._exec_remote_run_bash(
                    client,
                    f"tmux new-session -d -s {quoted_session} -c {quoted_work} -n shell; "
                    f"tmux list-windows -t {quoted_session} "
                    "-F '#{window_id}|#{window_name}'",
                )
                windows = self._parse_remote_run_windows(output)
                shell = next(
                    (window for window in windows if window["name"] == "shell"), None
                )
                if shell is None:
                    raise SSHBackendError("Recovered Remote Run session has no shell window")
                created_session = True
                return {
                    "session_name": session,
                    "work_path": paths["work"],
                    "shell_window": shell,
                    "windows": windows,
                    "created_session": created_session,
                }

            output = self._exec_remote_run_bash(
                client,
                f"tmux list-windows -t {quoted_session} "
                "-F '#{window_id}|#{window_name}'",
            )
            windows = self._parse_remote_run_windows(output)
            shell = next((window for window in windows if window["name"] == "shell"), None)
            if shell is None:
                created = self._exec_remote_run_bash(
                    client,
                    "tmux new-window -d -P -F '#{window_id}|#{window_name}' "
                    f"-t {quoted_session} -c {quoted_work} -n shell",
                )
                new_windows = self._parse_remote_run_windows(created)
                if len(new_windows) != 1:
                    raise SSHBackendError("Remote Run shell window could not be created")
                shell = new_windows[0]
                windows.append(shell)
            return {
                "session_name": session,
                "work_path": paths["work"],
                "shell_window": shell,
                "windows": windows,
                "created_session": created_session,
            }

    @staticmethod
    def _parse_remote_run_windows(output: str) -> list[dict[str, str]]:
        windows: list[dict[str, str]] = []
        for line in output.splitlines():
            window_id, separator, name = line.partition("|")
            if not separator or not window_id.startswith("@"):
                raise SSHBackendError("Remote Run tmux window list is invalid")
            windows.append({"id": window_id, "name": name or "shell"})
        if not windows:
            raise SSHBackendError("Remote Run tmux session has no windows")
        return windows

    def remote_run_git_clone_parameters(
        self,
        computer: dict[str, Any],
        run_base: str,
        run_id: str,
    ) -> dict[str, str]:
        """Return trusted paths needed to build a ``GitCloneInvocation``."""

        with self.remote_run_connection(computer) as (client, sftp):
            paths = self._assert_remote_run_root(sftp, run_base, run_id)
            output = self._exec_remote_run_bash(
                client,
                "command -v git >/dev/null 2>&1 || "
                "{ echo '__TERMROOM_NO_GIT__' >&2; exit 48; }; command -v git",
            ).strip()
            git_path = output.splitlines()[-1] if output else ""
            if not git_path.startswith("/"):
                raise SSHBackendError("Remote Git path could not be resolved safely")
            return {
                "git_path": posixpath.normpath(git_path),
                "askpass_path": paths["git_askpass"],
                "empty_home": paths["git_home"],
                "destination": paths["work_staging"],
            }

    def start_remote_git_run(
        self,
        computer: dict[str, Any],
        run_base: str,
        run_id: str,
        invocation: Any,
    ) -> dict[str, Any]:
        """Start isolated Git clone and the command in the same remote tmux."""

        from termroom.run_sources import GitCloneInvocation, validate_public_https_git_url

        if not isinstance(invocation, GitCloneInvocation):
            raise TypeError("Remote Git preparation requires a GitCloneInvocation")
        argv = tuple(getattr(invocation, "argv", ()))
        environment = dict(getattr(invocation, "env", {}))
        if len(argv) < 3 or not all(isinstance(value, str) for value in argv):
            raise ValueError("Invalid Git clone invocation")
        with self.remote_run_connection(computer) as (client, sftp):
            paths = self._assert_remote_run_root(sftp, run_base, run_id)
            if argv[-1] != paths["work_staging"]:
                raise ValueError("Git clone destination does not match this Remote Run")
            url = validate_public_https_git_url(argv[-2])
            if environment.get("GIT_ASKPASS") != paths["git_askpass"]:
                raise ValueError("Git askpass path does not match this Remote Run")
            if environment.get("HOME") != paths["git_home"]:
                raise ValueError("Git HOME does not match this Remote Run")

            env_i_argv = tuple(invocation.as_env_i_argv())
            if not env_i_argv or not all(isinstance(value, str) for value in env_i_argv):
                raise ValueError("Invalid Git clone invocation")
            if any(
                not value or any(character in value for character in ("\x00", "\r", "\n"))
                for value in env_i_argv
            ):
                raise ValueError("Git clone argv must contain single-line values")
            encoded_argv = b"\x00".join(
                value.encode("utf-8") for value in env_i_argv
            ) + b"\x00"
            if len(encoded_argv) > 1024 * 1024:
                raise ValueError("Git clone metadata is too large")
            if any(
                self._sftp_exists(sftp, paths[name])
                for name in ("state", "prepare_result", "stop", "completion")
            ) or self._remote_tmux_status(client, run_id)["exists"]:
                raise SSHBackendError("Remote Run has already started")
            if self._sftp_exists(sftp, paths["git_argv"]):
                stored_argv = self._read_sftp_bytes(
                    sftp, paths["git_argv"], max_bytes=1024 * 1024
                )
                if stored_argv != encoded_argv:
                    raise SSHBackendError(
                        "Remote Run id already belongs to a different Git Source"
                    )

            if self._sftp_exists(sftp, paths["git_home"]):
                home_attr = sftp.lstat(paths["git_home"])
                if stat_module.S_ISLNK(home_attr.st_mode) or not stat_module.S_ISDIR(
                    home_attr.st_mode
                ):
                    raise SSHBackendError("Remote Run Git HOME is invalid")
            else:
                sftp.mkdir(paths["git_home"], mode=0o700)
            self._sftp_atomic_write(
                sftp, paths["git_askpass"], b"#!/bin/sh\nexit 1\n", mode=0o700
            )
            self._sftp_atomic_write(sftp, paths["git_argv"], encoded_argv)
            self._sftp_atomic_write(sftp, paths["git_url"], (url + "\n").encode())
            self._sftp_atomic_write(
                sftp, paths["git_path"], (argv[0] + "\n").encode("utf-8")
            )
            self._sftp_atomic_write(
                sftp,
                paths["git_bootstrap"],
                REMOTE_GIT_BOOTSTRAP_SCRIPT.encode("utf-8"),
                mode=0o700,
            )

            session = self.remote_run_session_name(run_id)
            quoted_session = shlex.quote(session)
            quoted_root = shlex.quote(paths["root"])
            quoted_bootstrap = shlex.quote(paths["git_bootstrap"])
            remote_command = (
                "test -x /bin/bash || { echo '__TERMROOM_NO_BASH__' >&2; exit 47; }; "
                "command -v tmux >/dev/null 2>&1 || "
                "{ echo '__TERMROOM_NO_TMUX__' >&2; exit 45; }; "
                f"if tmux has-session -t {quoted_session} 2>/dev/null; then "
                "echo '__TERMROOM_RUN_EXISTS__' >&2; exit 46; fi; "
                f"tmux new-session -d -s {quoted_session} -c {quoted_root} -n run; "
                f"tmux set-window-option -t {quoted_session} remain-on-exit on; "
                f"tmux set-window-option -t {quoted_session} remain-on-exit-format '' "
                ">/dev/null 2>&1 || true; "
                f"tmux set-window-option -t {quoted_session} window-size latest "
                ">/dev/null 2>&1 || true; "
                f"tmux respawn-pane -k -t {quoted_session}:0.0 -c {quoted_root} "
                f"/bin/bash --noprofile --norc {quoted_bootstrap}"
            )
            self._exec_remote_run_bash(client, remote_command)
            return {
                "state": "preparing",
                "phase": "cloning",
                "session_name": session,
                "run_root": paths["root"],
                "source_url": url,
            }

    def reconcile_remote_run(
        self,
        computer: dict[str, Any],
        run_base: str,
        run_id: str,
    ) -> dict[str, Any]:
        with self.remote_run_connection(computer) as (client, sftp):
            try:
                paths = self._remote_run_control_paths(sftp, run_base, run_id)
            except RemoteRunLayoutError as exc:
                return self._remote_run_unavailable_layout_status(
                    client,
                    run_id,
                    layout_error=exc.code,
                )
            if paths is None:
                return self._remote_run_unavailable_layout_status(
                    client,
                    run_id,
                    layout_missing=True,
                )
            return self._reconcile_remote_run_connection(client, sftp, paths, run_id)

    def poll_remote_run(
        self,
        computer: dict[str, Any],
        run_base: str,
        run_id: str,
        *,
        stream: str = "command",
        offset: int | None = None,
        limit: int = REMOTE_RUN_LOG_READ_LIMIT,
    ) -> dict[str, Any]:
        """Read state and a bounded log range over the same SSH connection."""

        with self.remote_run_connection(computer) as (client, sftp):
            try:
                paths = self._remote_run_control_paths(sftp, run_base, run_id)
            except RemoteRunLayoutError as exc:
                status = self._remote_run_unavailable_layout_status(
                    client,
                    run_id,
                    layout_error=exc.code,
                )
                return {
                    **status,
                    "log": self._empty_remote_run_log(stream, offset),
                }
            if paths is None:
                status = self._remote_run_unavailable_layout_status(
                    client,
                    run_id,
                    layout_missing=True,
                )
                return {
                    **status,
                    "log": self._empty_remote_run_log(stream, offset),
                }
            status = self._reconcile_remote_run_connection(client, sftp, paths, run_id)
            log = self._read_remote_run_log_connection(
                sftp, paths, stream=stream, offset=offset, limit=limit
            )
            return {**status, "log": log}

    def read_remote_run_log(
        self,
        computer: dict[str, Any],
        run_base: str,
        run_id: str,
        *,
        stream: str = "command",
        offset: int | None = None,
        limit: int = REMOTE_RUN_LOG_READ_LIMIT,
    ) -> dict[str, Any]:
        with self.remote_run_connection(computer) as (_client, sftp):
            paths = self._assert_remote_run_root(sftp, run_base, run_id)
            return self._read_remote_run_log_connection(
                sftp, paths, stream=stream, offset=offset, limit=limit
            )

    def interrupt_remote_run(
        self,
        computer: dict[str, Any],
        run_base: str,
        run_id: str,
    ) -> dict[str, Any]:
        with self.remote_run_connection(computer) as (client, sftp):
            paths = self._remote_run_control_paths(sftp, run_base, run_id)
            if paths is None:
                tmux = self._remote_tmux_status(client, run_id)
                sent = False
                if tmux["run_pane_exists"] and tmux["running"]:
                    session = self.remote_run_session_name(run_id)
                    pane_target = shlex.quote(f"{session}:run.0")
                    command = (
                        f"tmux has-session -t {shlex.quote(session)} 2>/dev/null "
                        "|| exit 0; "
                        f"tmux display-message -p -t {pane_target} '#{{pane_id}}' "
                        ">/dev/null 2>&1 || exit 0; "
                        f"tmux send-keys -t {pane_target} C-c; printf 'sent\\n'"
                    )
                    sent = self._exec_remote_run_bash(client, command).strip() == "sent"
                return {
                    "sent": sent,
                    "completed": False,
                    "layout_missing": True,
                    "tmux_exists": bool(tmux["exists"]),
                    "tmux_running": bool(tmux["running"]),
                }
            completion, completion_valid = self._read_sftp_json(sftp, paths["completion"])
            if completion_valid and self._valid_completion_record(completion):
                return {"sent": False, "completed": True}
            self._publish_stop_request(sftp, paths)
            session = self.remote_run_session_name(run_id)
            pane_target = shlex.quote(f"{session}:run.0")
            command = (
                f"tmux has-session -t {shlex.quote(session)} 2>/dev/null || exit 0; "
                f"tmux display-message -p -t {pane_target} '#{{pane_id}}' "
                ">/dev/null 2>&1 || exit 0; "
                f"tmux send-keys -t {pane_target} C-c; printf 'sent\\n'"
            )
            output = self._exec_remote_run_bash(client, command).strip()
            return {"sent": output == "sent", "completed": False}

    def kill_remote_run(
        self,
        computer: dict[str, Any],
        run_base: str,
        run_id: str,
    ) -> dict[str, Any]:
        with self.remote_run_connection(computer) as (client, sftp):
            paths = self._remote_run_control_paths(sftp, run_base, run_id)
            if paths is None:
                tmux = self._remote_tmux_status(client, run_id)
                killed = False
                if tmux["exists"]:
                    self._kill_remote_run_session_connection(client, run_id)
                    killed = True
                return {
                    "killed": killed,
                    "completed": False,
                    "layout_missing": True,
                    "tmux_exists": bool(tmux["exists"]),
                    "tmux_running": bool(tmux["running"]),
                }
            completion, completion_valid = self._read_sftp_json(sftp, paths["completion"])
            if completion_valid and self._valid_completion_record(completion):
                return {"killed": False, "completed": True}
            self._publish_stop_request(sftp, paths)
            session = self.remote_run_session_name(run_id)
            self._exec_remote_run_bash(
                client,
                f"tmux kill-session -t {shlex.quote(session)} 2>/dev/null || true",
            )
            completion, completion_valid = self._read_sftp_json(
                sftp, paths["completion"]
            )
            if completion_valid and self._valid_completion_record(completion):
                return {"killed": False, "completed": True}
            return {"killed": True, "completed": False}

    def remote_run_layout_exists(
        self,
        computer: dict[str, Any],
        run_base: str,
        run_id: str,
    ) -> bool:
        with self.remote_run_connection(computer) as (_client, sftp):
            return self._remote_run_control_paths(sftp, run_base, run_id) is not None

    def delete_remote_run_root(
        self,
        computer: dict[str, Any],
        run_base: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Quarantine then delete a marked Run tree without following symlinks."""

        run_id = self.validate_remote_run_id(run_id)
        with self.remote_run_connection(computer) as (client, sftp):
            base = self._assert_canonical_remote_run_base(sftp, run_base)
            return self._delete_remote_run_root_connection(
                client, sftp, base, run_id
            )

    def _delete_remote_run_root_connection(
        self,
        client: paramiko.SSHClient,
        sftp: paramiko.SFTPClient,
        run_base: str,
        run_id: str,
    ) -> dict[str, Any]:
        paths = self._remote_run_paths(run_base, run_id)
        creating = self._remote_run_creation_paths(run_base, run_id)
        quarantine = posixpath.join(run_base, f".termroom-deleting-{run_id}")
        deletion_marker = self._remote_run_deletion_marker(run_base, run_id)
        original_exists = self._sftp_exists(sftp, paths["root"])
        creating_exists = self._sftp_exists(sftp, creating["root"])
        quarantine_exists = self._sftp_exists(sftp, quarantine)
        deletion_started = self._sftp_exists(sftp, deletion_marker)
        if sum((original_exists, creating_exists, quarantine_exists)) > 1:
            raise SSHBackendError(
                "Multiple Remote Run roots exist; refusing automatic deletion"
            )

        if deletion_started:
            self._assert_remote_run_deletion_marker(
                sftp, run_base, run_id
            )

        if not original_exists and not creating_exists and not quarantine_exists:
            self._kill_remote_run_session_connection(client, run_id)
            if deletion_started:
                sftp.remove(deletion_marker)
            return {
                "deleted": True,
                "already_missing": True,
                "quarantine": quarantine,
            }

        if original_exists:
            self._assert_remote_run_root(sftp, run_base, run_id)
        elif creating_exists:
            if not self._sftp_exists(sftp, creating["marker"]):
                if deletion_started:
                    sftp.remove(deletion_marker)
                self._kill_remote_run_session_connection(client, run_id)
                self._remove_pristine_remote_run_creation(
                    client, sftp, creating
                )
                return {
                    "deleted": True,
                    "already_missing": False,
                    "quarantine": quarantine,
                }
            self._assert_remote_run_creation(sftp, run_base, run_id)
        elif deletion_started:
            self._assert_quarantined_remote_run_container(
                sftp, run_base, quarantine, run_id
            )
        else:
            self._assert_quarantined_remote_run(sftp, run_base, quarantine, run_id)

        if not deletion_started:
            self._write_remote_run_deletion_marker(sftp, run_base, run_id)

        # A completed Run can still own the pane kept for output and a Workspace
        # shell window. Stop only the fixed UUID-derived session after ownership
        # checks, before moving or deleting its current directory.
        self._kill_remote_run_session_connection(client, run_id)

        source = paths["root"] if original_exists else creating["root"]
        if original_exists or creating_exists:
            try:
                sftp.rename(source, quarantine)
            except OSError as exc:
                raise SSHBackendError("Could not quarantine the Remote Run") from exc

        self._assert_quarantined_remote_run_container(
            sftp, run_base, quarantine, run_id
        )
        self._remove_marked_remote_tree_from_base(
            client, sftp, run_base, quarantine, run_id
        )
        sftp.remove(deletion_marker)
        return {
            "deleted": True,
            "already_missing": False,
            "quarantine": quarantine,
        }

    def _kill_remote_run_session_connection(
        self, client: paramiko.SSHClient, run_id: str
    ) -> None:
        session = self.remote_run_session_name(run_id)
        self._exec_remote_run_bash(
            client,
            f"tmux kill-session -t {shlex.quote(session)} 2>/dev/null || true",
        )

    @staticmethod
    def _is_missing_sftp_error(exc: BaseException) -> bool:
        return isinstance(exc, FileNotFoundError) or getattr(exc, "errno", None) == 2

    def _canonical_remote_run_base(
        self, sftp: paramiko.SFTPClient, configured: str
    ) -> str:
        home = posixpath.normpath(sftp.normalize("."))
        if configured and configured != configured.strip():
            raise ValueError("Remote Run base cannot have surrounding whitespace")
        value = configured
        if not value:
            value = posixpath.join(home, ".cache", "termroom", "runs")
        elif value == "~" or value == "$HOME":
            value = home
        elif value.startswith("~/"):
            value = posixpath.join(home, value[2:])
        elif value.startswith("$HOME/"):
            value = posixpath.join(home, value[6:])
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise ValueError("Remote Run base must be one path")
        value = posixpath.normpath(value)
        if not value.startswith("/") or value == "/":
            raise ValueError("Remote Run base must be an absolute non-root path")

        current = "/"
        for component in PurePosixPath(value).parts[1:]:
            current = posixpath.join(current, component)
            try:
                attr = sftp.lstat(current)
            except OSError as exc:
                if not self._is_missing_sftp_error(exc):
                    raise
                sftp.mkdir(current, mode=0o700)
                attr = sftp.lstat(current)
            if stat_module.S_ISLNK(attr.st_mode):
                resolved = sftp.normalize(current)
                resolved_attr = sftp.lstat(resolved)
                if stat_module.S_ISLNK(resolved_attr.st_mode) or not stat_module.S_ISDIR(
                    resolved_attr.st_mode
                ):
                    raise SSHBackendError(f"Remote Run base component is invalid: {current}")
            elif not stat_module.S_ISDIR(attr.st_mode):
                raise SSHBackendError(f"Remote Run base component is not a directory: {current}")

        canonical = posixpath.normpath(sftp.normalize(value))
        if not canonical.startswith("/") or canonical == "/":
            raise SSHBackendError("Remote Run base could not be canonicalized safely")
        attr = sftp.lstat(canonical)
        if stat_module.S_ISLNK(attr.st_mode) or not stat_module.S_ISDIR(attr.st_mode):
            raise SSHBackendError("Canonical Remote Run base is not a real directory")
        self._assert_remote_run_base_permissions(attr)
        return canonical

    def _assert_canonical_remote_run_base(
        self, sftp: paramiko.SFTPClient, run_base: str
    ) -> str:
        stored = posixpath.normpath(run_base)
        if stored != run_base.rstrip("/") or not stored.startswith("/") or stored == "/":
            raise SSHBackendError("Stored Remote Run base is invalid")
        try:
            attr = sftp.lstat(stored)
        except OSError as exc:
            raise SSHBackendError("Stored Remote Run base is unavailable") from exc
        if stat_module.S_ISLNK(attr.st_mode) or not stat_module.S_ISDIR(attr.st_mode):
            raise SSHBackendError("Stored Remote Run base is not a real directory")
        self._assert_remote_run_base_permissions(attr)
        canonical = posixpath.normpath(sftp.normalize(stored))
        if canonical != stored:
            raise SSHBackendError("Stored Remote Run base no longer resolves to itself")
        return stored

    @staticmethod
    def _assert_remote_run_base_permissions(attr: paramiko.SFTPAttributes) -> None:
        mode = int(attr.st_mode or 0)
        if mode & 0o022:
            raise SSHBackendError(
                "Remote Run base must not be writable by other users"
            )

    @staticmethod
    def _is_remote_run_marker_temporary(name: str) -> bool:
        prefix = ".marker.termroom-"
        suffix = ".tmp"
        if not name.startswith(prefix) or not name.endswith(suffix):
            return False
        candidate = name[len(prefix) : -len(suffix)]
        try:
            parsed = uuid.UUID(candidate)
        except (ValueError, AttributeError):
            return False
        return parsed.version == 4 and str(parsed) == candidate

    @staticmethod
    def _remote_run_bash_command(script: str) -> str:
        return f"/bin/bash --noprofile --norc -c {shlex.quote(script)}"

    @staticmethod
    def _remote_posix_command(script: str) -> str:
        return f"/bin/sh -c {shlex.quote(script)}"

    def _exec_remote_run_bash(
        self,
        client: paramiko.SSHClient,
        script: str,
        *,
        timeout: float | None = 20,
    ) -> str:
        command = self._remote_run_bash_command(script)
        if timeout == 20:
            return self._exec_client(client, command)
        return self._exec_client(client, command, timeout=timeout)

    def _remote_run_paths(self, run_base: str, run_id: str) -> dict[str, str]:
        return self._remote_run_paths_at_leaf(run_base, run_id, run_id)

    def _remote_run_creation_paths(
        self, run_base: str, run_id: str
    ) -> dict[str, str]:
        run_id = self.validate_remote_run_id(run_id)
        return self._remote_run_paths_at_leaf(
            run_base, run_id, f".termroom-creating-{run_id}"
        )

    def _remote_run_deletion_marker(self, run_base: str, run_id: str) -> str:
        run_id = self.validate_remote_run_id(run_id)
        base = posixpath.normpath(run_base)
        if not base.startswith("/") or base == "/":
            raise SSHBackendError("Remote Run base is invalid")
        marker = posixpath.join(base, f".termroom-deleting-{run_id}.marker")
        if posixpath.dirname(marker) != base:
            raise SSHBackendError("Remote Run deletion marker path is invalid")
        return marker

    def _remote_run_paths_at_leaf(
        self, run_base: str, run_id: str, leaf: str
    ) -> dict[str, str]:
        run_id = self.validate_remote_run_id(run_id)
        base = posixpath.normpath(run_base)
        if not base.startswith("/") or base == "/":
            raise SSHBackendError("Remote Run base is invalid")
        allowed_leaves = {
            run_id,
            f".termroom-creating-{run_id}",
            f".termroom-deleting-{run_id}",
        }
        if leaf not in allowed_leaves:
            raise SSHBackendError("Remote Run internal path is invalid")
        root = posixpath.join(base, leaf)
        if posixpath.dirname(root) != base or posixpath.basename(root) != leaf:
            raise SSHBackendError("Remote Run root is not a direct child of its base")
        metadata = posixpath.join(root, ".termroom")
        return {
            "root": root,
            "work": posixpath.join(root, "work"),
            "work_staging": posixpath.join(root, "work.tmp"),
            "metadata": metadata,
            "marker": posixpath.join(metadata, "marker"),
            "cwd": posixpath.join(metadata, "cwd"),
            "command": posixpath.join(metadata, "command.sh"),
            "runner": posixpath.join(metadata, "runner.sh"),
            "log_pipe": posixpath.join(metadata, "log-pipe.sh"),
            "state": posixpath.join(metadata, "state.json"),
            "stop": posixpath.join(metadata, "stop-requested-at"),
            "prepare_result": posixpath.join(metadata, "prepare-result.json"),
            "prepare_log": posixpath.join(metadata, "prepare.log"),
            "output": posixpath.join(metadata, "output.log"),
            "output_seal": posixpath.join(metadata, "output-seal.json"),
            "completion": posixpath.join(metadata, "completion.json"),
            "git_url": posixpath.join(metadata, "git-url"),
            "git_path": posixpath.join(metadata, "git-path"),
            "git_revision": posixpath.join(metadata, "git-revision"),
            "git_argv": posixpath.join(metadata, "git-argv"),
            "git_askpass": posixpath.join(metadata, "git-askpass"),
            "git_home": posixpath.join(metadata, "git-home"),
            "git_bootstrap": posixpath.join(metadata, "git-bootstrap.sh"),
        }

    def _assert_remote_run_root(
        self,
        sftp: paramiko.SFTPClient,
        run_base: str,
        run_id: str,
    ) -> dict[str, str]:
        base = self._assert_canonical_remote_run_base(sftp, run_base)
        paths = self._remote_run_paths(base, run_id)
        try:
            root_attr = sftp.lstat(paths["root"])
            metadata_attr = sftp.lstat(paths["metadata"])
            marker_attr = sftp.lstat(paths["marker"])
        except OSError as exc:
            raise RemoteRunLayoutError("Remote Run layout is incomplete") from exc
        if stat_module.S_ISLNK(root_attr.st_mode) or not stat_module.S_ISDIR(root_attr.st_mode):
            raise RemoteRunLayoutError(
                "Remote Run root is not a real directory", code="root_invalid"
            )
        if posixpath.normpath(sftp.normalize(paths["root"])) != paths["root"]:
            raise RemoteRunLayoutError(
                "Remote Run root resolves outside its stored path", code="root_invalid"
            )
        if stat_module.S_ISLNK(metadata_attr.st_mode) or not stat_module.S_ISDIR(
            metadata_attr.st_mode
        ):
            raise RemoteRunLayoutError(
                "Remote Run metadata directory is invalid", code="metadata_invalid"
            )
        if stat_module.S_ISLNK(marker_attr.st_mode) or not stat_module.S_ISREG(
            marker_attr.st_mode
        ):
            raise RemoteRunLayoutError(
                "Run marker is not a regular file", code="marker_invalid"
            )
        try:
            marker = self._read_sftp_bytes(sftp, paths["marker"], max_bytes=128).decode(
                "utf-8"
            ).strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise RemoteRunLayoutError(
                "Run marker is unreadable", code="marker_invalid"
            ) from exc
        if marker != run_id:
            raise RemoteRunLayoutError(
                "Run marker does not match; refusing the operation",
                code="marker_mismatch",
            )
        return paths

    def _remote_run_control_paths(
        self,
        sftp: paramiko.SFTPClient,
        run_base: str,
        run_id: str,
    ) -> dict[str, str] | None:
        base = self._assert_canonical_remote_run_base(sftp, run_base)
        paths = self._remote_run_paths(base, run_id)
        try:
            sftp.lstat(paths["root"])
        except OSError as exc:
            if self._is_missing_sftp_error(exc):
                return None
            raise SSHBackendError("Remote Run root could not be inspected") from exc
        return self._assert_remote_run_root(sftp, base, run_id)

    @staticmethod
    def _normalize_remote_run_command(command: str) -> str:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Remote Run command cannot be empty")
        if "\x00" in command:
            raise ValueError("Remote Run command cannot contain NUL")
        body = command.rstrip("\n") + "\n"
        if len(body.encode("utf-8")) > 256 * 1024:
            raise ValueError("Remote Run command is too large")
        return body

    @staticmethod
    def _validate_run_relative_path(value: str, *, directory: bool) -> str:
        del directory
        if not isinstance(value, str) or not value:
            raise ValueError("Remote Run path is required")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("Remote Run paths cannot contain control characters")
        if "\\" in value or value.startswith("/"):
            raise ValueError("Remote Run paths must be relative POSIX paths")
        if value == ".":
            return value
        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("Remote Run paths must already be normalized")
        if ".termroom" in parts:
            raise ValueError("Remote Run metadata is not exposed as a work file")
        return "/".join(parts)

    @staticmethod
    def _sftp_exists(sftp: paramiko.SFTPClient, path: str) -> bool:
        try:
            sftp.lstat(path)
            return True
        except OSError as exc:
            if SSHBackend._is_missing_sftp_error(exc):
                return False
            raise

    @staticmethod
    def _sftp_atomic_write(
        sftp: paramiko.SFTPClient,
        destination: str,
        value: bytes,
        *,
        mode: int = 0o600,
    ) -> None:
        temporary = posixpath.join(
            posixpath.dirname(destination),
            f".{posixpath.basename(destination)}.termroom-{uuid.uuid4()}.tmp",
        )
        try:
            with sftp.open(temporary, "wb") as handle:
                handle.write(value)
            sftp.chmod(temporary, mode)
            if SSHBackend._sftp_exists(sftp, destination):
                try:
                    sftp.posix_rename(temporary, destination)
                except (AttributeError, OSError) as exc:
                    raise SSHBackendError(
                        "Remote SFTP server does not support atomic metadata replacement"
                    ) from exc
            else:
                sftp.rename(temporary, destination)
        finally:
            with contextlib.suppress(OSError):
                sftp.remove(temporary)

    @staticmethod
    def _read_sftp_bytes(
        sftp: paramiko.SFTPClient,
        path: str,
        *,
        max_bytes: int | None = None,
    ) -> bytes:
        with sftp.open(path, "rb") as handle:
            raw = handle.read() if max_bytes is None else handle.read(max_bytes + 1)
        value = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        if max_bytes is not None and len(value) > max_bytes:
            raise SSHBackendError(f"Remote metadata file is too large: {path}")
        return value

    @classmethod
    def _read_sftp_json(
        cls, sftp: paramiko.SFTPClient, path: str
    ) -> tuple[Any, bool]:
        try:
            raw = cls._read_sftp_bytes(sftp, path, max_bytes=1024 * 1024)
        except OSError as exc:
            if cls._is_missing_sftp_error(exc):
                return None, False
            raise
        try:
            return json.loads(raw.decode("utf-8")), True
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, False

    @staticmethod
    def _valid_completion_record(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and isinstance(value.get("exit_code"), int)
            and not isinstance(value.get("exit_code"), bool)
            and isinstance(value.get("stop_requested"), bool)
            and isinstance(value.get("started_at"), str)
            and bool(value.get("started_at"))
            and isinstance(value.get("ended_at"), str)
            and bool(value.get("ended_at"))
        )

    @staticmethod
    def _valid_prepare_record(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and value.get("state") in {"failed", "stopped"}
            and isinstance(value.get("ended_at"), str)
            and bool(value.get("ended_at"))
        )

    @staticmethod
    def _valid_state_record(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and value.get("phase") in {"cloning", "running"}
            and isinstance(value.get("started_at"), str)
            and bool(value.get("started_at"))
        )

    def _remote_tmux_status(
        self, client: paramiko.SSHClient, run_id: str
    ) -> dict[str, Any]:
        session = self.remote_run_session_name(run_id)
        quoted = shlex.quote(session)
        pane_target = shlex.quote(f"{session}:run.0")
        command = (
            f"if ! tmux has-session -t {quoted} 2>/dev/null; then printf 'missing\\n'; "
            f"elif ! tmux display-message -p -t {pane_target} '#{{pane_id}}' "
            ">/dev/null 2>&1; then printf 'missing-run\\n'; "
            "else tmux list-panes -t "
            f"{pane_target} -F '#{{pane_dead}}|#{{pane_dead_status}}' | head -n 1; fi"
        )
        output = self._exec_remote_run_bash(client, command).strip()
        if not output or output == "missing":
            return {
                "exists": False,
                "run_pane_exists": False,
                "running": False,
                "pane_exit_code": None,
            }
        if output == "missing-run":
            return {
                "exists": True,
                "run_pane_exists": False,
                "running": False,
                "pane_exit_code": None,
            }
        dead_text, separator, status_text = output.splitlines()[0].partition("|")
        if not separator or dead_text not in {"0", "1"}:
            raise SSHBackendError("Remote tmux status is invalid")
        exit_code: int | None = None
        if dead_text == "1" and status_text:
            with contextlib.suppress(ValueError):
                exit_code = int(status_text)
        return {
            "exists": True,
            "run_pane_exists": True,
            "running": dead_text == "0",
            "pane_exit_code": exit_code,
        }

    def _reconcile_remote_run_connection(
        self,
        client: paramiko.SSHClient,
        sftp: paramiko.SFTPClient,
        paths: dict[str, str],
        run_id: str,
    ) -> dict[str, Any]:
        # Read tmux first. If the pane exits while status is being collected, the
        # runner has already atomically written completion.json by the time the
        # metadata reads below begin. Reading metadata first can observe the
        # opposite halves of that transition and incorrectly report a cleanly
        # finished Run as lost.
        tmux = self._remote_tmux_status(client, run_id)
        completion_exists = self._sftp_exists(sftp, paths["completion"])
        completion, completion_valid = self._read_sftp_json(sftp, paths["completion"])
        prepare_exists = self._sftp_exists(sftp, paths["prepare_result"])
        prepare, prepare_valid = self._read_sftp_json(sftp, paths["prepare_result"])
        state_exists = self._sftp_exists(sftp, paths["state"])
        state, state_valid = self._read_sftp_json(sftp, paths["state"])
        stop_requested = self._sftp_exists(sftp, paths["stop"])
        completion_record_valid = completion_valid and self._valid_completion_record(
            completion
        )
        prepare_record_valid = prepare_valid and self._valid_prepare_record(prepare)
        state_record_valid = state_valid and self._valid_state_record(state)
        record_errors = [
            name
            for name, exists, valid in (
                ("completion.json", completion_exists, completion_record_valid),
                ("prepare-result.json", prepare_exists, prepare_record_valid),
                ("state.json", state_exists, state_record_valid),
            )
            if exists and not valid
        ]

        result: dict[str, Any] = {
            "state": "preparing",
            "phase": state.get("phase") if state_record_valid else None,
            "exit_code": None,
            "started_at": state.get("started_at") if state_record_valid else None,
            "ended_at": None,
            "stop_requested": stop_requested,
            "tmux_exists": tmux["exists"],
            "run_pane_exists": tmux["run_pane_exists"],
            "tmux_running": tmux["running"],
            "record_errors": record_errors,
        }
        completion_terminal = completion_record_valid
        prepare_terminal = prepare_record_valid
        live_phase = state.get("phase") if state_record_valid else None
        if (
            not completion_terminal
            and not prepare_terminal
            and not tmux["running"]
            and live_phase in {"running", "cloning"}
        ):
            # A launcher can publish live metadata between the first tmux sample
            # and the reads above. Resample before treating that active phase as
            # terminal, then reread the records written when a pane exits.
            tmux = self._remote_tmux_status(client, run_id)
            result.update(
                tmux_exists=tmux["exists"],
                run_pane_exists=tmux["run_pane_exists"],
                tmux_running=tmux["running"],
            )
            if not tmux["running"]:
                completion_exists = self._sftp_exists(sftp, paths["completion"])
                completion, completion_valid = self._read_sftp_json(
                    sftp, paths["completion"]
                )
                prepare_exists = self._sftp_exists(sftp, paths["prepare_result"])
                prepare, prepare_valid = self._read_sftp_json(
                    sftp, paths["prepare_result"]
                )
                state_exists = self._sftp_exists(sftp, paths["state"])
                state, state_valid = self._read_sftp_json(sftp, paths["state"])
                completion_record_valid = (
                    completion_valid and self._valid_completion_record(completion)
                )
                prepare_record_valid = prepare_valid and self._valid_prepare_record(
                    prepare
                )
                state_record_valid = state_valid and self._valid_state_record(state)
                stop_requested = self._sftp_exists(sftp, paths["stop"])
                result.update(
                    phase=state.get("phase") if state_record_valid else None,
                    started_at=state.get("started_at") if state_record_valid else None,
                    stop_requested=stop_requested,
                )
                result["record_errors"] = [
                    name
                    for name, exists, valid in (
                        ("completion.json", completion_exists, completion_record_valid),
                        ("prepare-result.json", prepare_exists, prepare_record_valid),
                        ("state.json", state_exists, state_record_valid),
                    )
                    if exists and not valid
                ]
                completion_terminal = completion_record_valid
                prepare_terminal = prepare_record_valid

        if completion_terminal:
            assert isinstance(completion, dict)
            result.update(
                state="stopped" if completion["stop_requested"] else "finished",
                phase=None,
                exit_code=completion["exit_code"],
                started_at=completion["started_at"],
                ended_at=completion["ended_at"],
                stop_requested=completion["stop_requested"],
                log_incomplete=bool(completion.get("log_incomplete", False)),
            )
            return result
        if prepare_terminal:
            assert isinstance(prepare, dict)
            result.update(
                state=prepare["state"],
                phase=None,
                ended_at=prepare["ended_at"],
                error_code=prepare.get("error_code"),
            )
            return result
        if tmux["running"]:
            if state_record_valid and state.get("phase") == "running":
                result["state"] = "running"
            return result
        if stop_requested:
            result.update(state="stopped", phase=None)
            return result
        if result["record_errors"]:
            # Damaged lifecycle metadata is not evidence that a Run stopped.
            # Keep the last nonterminal state until a valid terminal record or
            # live managed pane can resolve the ambiguity.
            return result
        if state_record_valid and state.get("phase") == "running":
            result.update(state="lost", phase=None)
            return result
        if state_record_valid and state.get("phase") == "cloning":
            result.update(state="failed", phase=None, error_code="git_session_lost")
            return result
        return result

    def _remote_run_unavailable_layout_status(
        self,
        client: paramiko.SSHClient,
        run_id: str,
        *,
        layout_missing: bool = False,
        layout_error: str | None = None,
    ) -> dict[str, Any]:
        tmux = self._remote_tmux_status(client, run_id)
        return {
            "state": "layout_missing" if layout_missing else "layout_error",
            "phase": None,
            "exit_code": None,
            "started_at": None,
            "ended_at": None,
            "stop_requested": False,
            "tmux_exists": bool(tmux["exists"]),
            "tmux_running": bool(tmux["running"]),
            "run_pane_exists": bool(tmux["run_pane_exists"]),
            "record_errors": [layout_error] if layout_error else [],
            "layout_missing": layout_missing,
            "layout_error": layout_error,
        }

    @staticmethod
    def _empty_remote_run_log(stream: str, offset: int | None) -> dict[str, Any]:
        if stream not in {"prepare", "command"}:
            raise ValueError("Remote Run log stream must be prepare or command")
        start = max(0, offset or 0)
        return {
            "stream": stream,
            "chunk_b64": "",
            "start_offset": start,
            "next_offset": start,
            "size": start,
            "eof": True,
        }

    def _read_remote_run_log_connection(
        self,
        sftp: paramiko.SFTPClient,
        paths: dict[str, str],
        *,
        stream: str,
        offset: int | None,
        limit: int,
    ) -> dict[str, Any]:
        if stream not in {"prepare", "command"}:
            raise ValueError("Remote Run log stream must be prepare or command")
        if limit <= 0:
            raise ValueError("Remote Run log limit must be positive")
        limit = min(limit, REMOTE_RUN_LOG_READ_LIMIT)
        path = paths["prepare_log"] if stream == "prepare" else paths["output"]
        try:
            attr = sftp.lstat(path)
        except OSError as exc:
            if self._is_missing_sftp_error(exc):
                return {
                    "stream": stream,
                    "chunk_b64": "",
                    "start_offset": 0,
                    "next_offset": 0,
                    "size": 0,
                    "eof": True,
                }
            raise
        if stat_module.S_ISLNK(attr.st_mode) or not stat_module.S_ISREG(attr.st_mode):
            raise SSHBackendError("Remote Run log is not a regular file")
        size = int(attr.st_size or 0)
        if offset is None:
            start = max(0, size - REMOTE_RUN_INITIAL_TAIL)
        else:
            if offset < 0 or offset > size:
                raise ValueError("Remote Run log offset is outside the current log")
            start = offset
        with sftp.open(path, "rb") as handle:
            if offset is None and start:
                handle.seek(start)
                prefix = handle.read(4)
                prefix_bytes = prefix.encode() if isinstance(prefix, str) else bytes(prefix)
                skipped = 0
                while skipped < len(prefix_bytes) and prefix_bytes[skipped] & 0xC0 == 0x80:
                    skipped += 1
                start += skipped
            handle.seek(start)
            raw = handle.read(min(limit, max(0, size - start)))
        chunk = raw.encode() if isinstance(raw, str) else bytes(raw)
        next_offset = start + len(chunk)
        return {
            "stream": stream,
            "chunk_b64": base64.b64encode(chunk).decode("ascii"),
            "start_offset": start,
            "next_offset": next_offset,
            "size": size,
            "eof": next_offset >= size,
        }

    def _publish_stop_request(
        self, sftp: paramiko.SFTPClient, paths: dict[str, str]
    ) -> None:
        if self._sftp_exists(sftp, paths["stop"]):
            return
        value = datetime.now(UTC).isoformat(timespec="seconds") + "\n"
        self._sftp_atomic_write(sftp, paths["stop"], value.encode("utf-8"))

    @classmethod
    def _run_id_from_session(cls, session_name: str) -> str:
        if not session_name.startswith(REMOTE_RUN_SESSION_PREFIX):
            raise SSHBackendError("Invalid Remote Run tmux session")
        run_id = session_name.removeprefix(REMOTE_RUN_SESSION_PREFIX)
        return cls.validate_remote_run_id(run_id)

    def _assert_quarantined_remote_run(
        self,
        sftp: paramiko.SFTPClient,
        run_base: str,
        quarantine: str,
        run_id: str,
    ) -> None:
        expected = posixpath.join(run_base, f".termroom-deleting-{run_id}")
        if quarantine != expected or posixpath.dirname(quarantine) != run_base:
            raise SSHBackendError("Remote Run quarantine path is invalid")
        self._assert_quarantined_remote_run_container(
            sftp, run_base, quarantine, run_id
        )
        self._assert_marked_remote_run_tree(
            sftp, run_base, quarantine, run_id, label="quarantine"
        )

    def _assert_quarantined_remote_run_container(
        self,
        sftp: paramiko.SFTPClient,
        run_base: str,
        quarantine: str,
        run_id: str,
    ) -> None:
        expected = posixpath.join(run_base, f".termroom-deleting-{run_id}")
        if quarantine != expected or posixpath.dirname(quarantine) != run_base:
            raise SSHBackendError("Remote Run quarantine path is invalid")
        try:
            attr = sftp.lstat(quarantine)
        except OSError as exc:
            raise SSHBackendError("Remote Run quarantine is unavailable") from exc
        if stat_module.S_ISLNK(attr.st_mode) or not stat_module.S_ISDIR(attr.st_mode):
            raise SSHBackendError("Remote Run quarantine is not a real directory")
        if posixpath.normpath(sftp.normalize(quarantine)) != quarantine:
            raise SSHBackendError("Remote Run quarantine resolves outside its path")

    def _write_remote_run_deletion_marker(
        self,
        sftp: paramiko.SFTPClient,
        run_base: str,
        run_id: str,
    ) -> str:
        marker = self._remote_run_deletion_marker(run_base, run_id)
        if self._sftp_exists(sftp, marker):
            self._assert_remote_run_deletion_marker(sftp, run_base, run_id)
            return marker
        self._sftp_atomic_write(sftp, marker, (run_id + "\n").encode("utf-8"))
        self._assert_remote_run_deletion_marker(sftp, run_base, run_id)
        return marker

    def _assert_remote_run_deletion_marker(
        self,
        sftp: paramiko.SFTPClient,
        run_base: str,
        run_id: str,
    ) -> None:
        marker = self._remote_run_deletion_marker(run_base, run_id)
        try:
            attr = sftp.lstat(marker)
        except OSError as exc:
            raise SSHBackendError("Remote Run deletion marker is unavailable") from exc
        if stat_module.S_ISLNK(attr.st_mode) or not stat_module.S_ISREG(attr.st_mode):
            raise SSHBackendError("Remote Run deletion marker is invalid")
        if posixpath.normpath(sftp.normalize(marker)) != marker:
            raise SSHBackendError("Remote Run deletion marker is not canonical")
        value = self._read_sftp_bytes(sftp, marker, max_bytes=128).decode(
            "utf-8", errors="strict"
        ).strip()
        if value != run_id:
            raise SSHBackendError("Remote Run deletion marker does not match")

    def _assert_remote_run_creation(
        self,
        sftp: paramiko.SFTPClient,
        run_base: str,
        run_id: str,
    ) -> None:
        creating = self._remote_run_creation_paths(run_base, run_id)["root"]
        self._assert_marked_remote_run_tree(
            sftp, run_base, creating, run_id, label="creation staging"
        )

    def _assert_marked_remote_run_tree(
        self,
        sftp: paramiko.SFTPClient,
        run_base: str,
        root: str,
        run_id: str,
        *,
        label: str,
    ) -> None:
        if posixpath.dirname(root) != run_base:
            raise SSHBackendError(f"Remote Run {label} path is invalid")
        try:
            attr = sftp.lstat(root)
            metadata_attr = sftp.lstat(posixpath.join(root, ".termroom"))
            marker_path = posixpath.join(root, ".termroom", "marker")
            marker_attr = sftp.lstat(marker_path)
        except OSError as exc:
            raise SSHBackendError(f"Remote Run {label} is incomplete") from exc
        if stat_module.S_ISLNK(attr.st_mode) or not stat_module.S_ISDIR(attr.st_mode):
            raise SSHBackendError(f"Remote Run {label} is not a real directory")
        if posixpath.normpath(sftp.normalize(root)) != root:
            raise SSHBackendError(f"Remote Run {label} resolves outside its path")
        if stat_module.S_ISLNK(metadata_attr.st_mode) or not stat_module.S_ISDIR(
            metadata_attr.st_mode
        ):
            raise SSHBackendError(f"Remote Run {label} metadata is invalid")
        if stat_module.S_ISLNK(marker_attr.st_mode) or not stat_module.S_ISREG(
            marker_attr.st_mode
        ):
            raise SSHBackendError(f"Remote Run {label} marker is invalid")
        marker = self._read_sftp_bytes(sftp, marker_path, max_bytes=128).decode(
            "utf-8", errors="strict"
        ).strip()
        if marker != run_id:
            raise SSHBackendError("Run marker does not match; refusing automatic deletion")

    def _discard_remote_run_creation(
        self,
        client: paramiko.SSHClient,
        sftp: paramiko.SFTPClient,
        run_base: str,
        run_id: str,
    ) -> None:
        creating = self._remote_run_creation_paths(run_base, run_id)
        if not self._sftp_exists(sftp, creating["root"]):
            return
        if not self._sftp_exists(sftp, creating["marker"]):
            self._remove_pristine_remote_run_creation(client, sftp, creating)
            return

        self._assert_remote_run_creation(sftp, run_base, run_id)
        self._delete_remote_run_root_connection(
            client, sftp, run_base, run_id
        )

    def _remove_pristine_remote_run_creation(
        self,
        client: paramiko.SSHClient,
        sftp: paramiko.SFTPClient,
        paths: dict[str, str],
    ) -> None:
        root_attr = sftp.lstat(paths["root"])
        if stat_module.S_ISLNK(root_attr.st_mode) or not stat_module.S_ISDIR(
            root_attr.st_mode
        ):
            raise SSHBackendError("Remote Run creation staging is invalid")
        if posixpath.normpath(sftp.normalize(paths["root"])) != paths["root"]:
            raise SSHBackendError("Remote Run creation staging is not canonical")

        base = posixpath.dirname(paths["root"])
        leaf = posixpath.basename(paths["root"])
        quoted_base = shlex.quote(base)
        quoted_leaf = shlex.quote(leaf)
        root_entries = sftp.listdir_attr(paths["root"])
        if not root_entries:
            command = (
                "set -eu; "
                f"cd -- {quoted_base}; "
                f"test \"$(pwd -P)\" = {quoted_base}; "
                f"test ! -L {quoted_leaf}; test -d {quoted_leaf}; "
                f"test -z \"$(ls -A -- {quoted_leaf})\"; "
                f"rmdir -- {quoted_leaf}"
            )
            self._exec_remote_run_bash(client, command)
            if self._sftp_exists(sftp, paths["root"]):
                raise SSHBackendError("Remote Run creation staging still exists")
            return
        if len(root_entries) != 1 or root_entries[0].filename != ".termroom":
            raise SSHBackendError(
                "Remote Run creation staging contains unexpected files"
            )

        metadata_attr = sftp.lstat(paths["metadata"])
        if stat_module.S_ISLNK(metadata_attr.st_mode) or not stat_module.S_ISDIR(
            metadata_attr.st_mode
        ):
            raise SSHBackendError("Remote Run creation metadata is invalid")
        if posixpath.normpath(sftp.normalize(paths["metadata"])) != paths["metadata"]:
            raise SSHBackendError("Remote Run creation metadata is not canonical")

        marker_temporaries: list[str] = []
        for child in sftp.listdir_attr(paths["metadata"]):
            name = str(child.filename or "")
            if (
                not self._is_remote_run_marker_temporary(name)
                or stat_module.S_ISLNK(child.st_mode)
                or not stat_module.S_ISREG(child.st_mode)
            ):
                raise SSHBackendError(
                    "Remote Run creation metadata contains unexpected files"
                )
            marker_temporaries.append(name)

        quoted_root = shlex.quote(paths["root"])
        quoted_metadata = shlex.quote(paths["metadata"])
        remove_temporaries = " ".join(
            (
                f"if test -e {shlex.quote(name)} || test -L {shlex.quote(name)}; "
                f"then test -f {shlex.quote(name)}; test ! -L {shlex.quote(name)}; "
                f"rm -f -- {shlex.quote(name)}; fi;"
            )
            for name in marker_temporaries
        )
        command = (
            "set -eu; "
            f"cd -- {quoted_base}; "
            f"test \"$(pwd -P)\" = {quoted_base}; "
            f"test ! -L {quoted_leaf}; "
            f"cd -- {quoted_leaf}; "
            f"test \"$(pwd -P)\" = {quoted_root}; "
            "test ! -L .termroom; test -d .termroom; "
            "test \"$(ls -A -- .)\" = .termroom; "
            "cd -- .termroom; "
            f"test \"$(pwd -P)\" = {quoted_metadata}; "
            f"{remove_temporaries} "
            "test -z \"$(ls -A -- .)\"; "
            "cd -- ..; "
            f"test \"$(pwd -P)\" = {quoted_root}; "
            "rmdir -- .termroom; "
            "cd -- ..; "
            f"test \"$(pwd -P)\" = {quoted_base}; "
            f"rmdir -- {quoted_leaf}"
        )
        self._exec_remote_run_bash(client, command)
        if self._sftp_exists(sftp, paths["root"]):
            raise SSHBackendError("Remote Run creation staging still exists")

    def _remove_remote_run_work_staging(
        self,
        client: paramiko.SSHClient,
        sftp: paramiko.SFTPClient,
        paths: dict[str, str],
        run_id: str,
    ) -> None:
        self._assert_marked_remote_run_tree(
            sftp,
            posixpath.dirname(paths["root"]),
            paths["root"],
            run_id,
            label="root",
        )
        quoted_root = shlex.quote(paths["root"])
        quoted_run_id = shlex.quote(run_id)
        command = (
            "set -eu; "
            f"cd -- {quoted_root}; "
            f"test \"$(pwd -P)\" = {quoted_root}; "
            f"test \"$(cat -- .termroom/marker)\" = {quoted_run_id}; "
            "rm -rf -- work.tmp; "
            "test ! -e work.tmp; test ! -L work.tmp"
        )
        self._exec_remote_run_bash(
            client,
            command,
            timeout=REMOTE_RUN_DELETE_TIMEOUT_SECONDS,
        )
        if self._sftp_exists(sftp, paths["work_staging"]):
            raise SSHBackendError("Remote Run work staging still exists")

    def _remove_marked_remote_tree_from_base(
        self,
        client: paramiko.SSHClient,
        sftp: paramiko.SFTPClient,
        run_base: str,
        root: str,
        run_id: str,
    ) -> None:
        leaf = posixpath.basename(root)
        if posixpath.dirname(root) != run_base or leaf != f".termroom-deleting-{run_id}":
            raise SSHBackendError("Remote Run deletion path is invalid")
        self._assert_remote_run_deletion_marker(sftp, run_base, run_id)
        quoted_base = shlex.quote(run_base)
        quoted_leaf = shlex.quote(leaf)
        marker_leaf = posixpath.basename(
            self._remote_run_deletion_marker(run_base, run_id)
        )
        quoted_marker = shlex.quote(marker_leaf)
        quoted_run_id = shlex.quote(run_id)
        command = (
            "set -eu; "
            f"cd -- {quoted_base}; "
            f"test \"$(pwd -P)\" = {quoted_base}; "
            f"test ! -L {quoted_leaf}; "
            f"test -d {quoted_leaf}; "
            f"test \"$(cat -- {quoted_marker})\" = {quoted_run_id}; "
            f"rm -rf -- {quoted_leaf}; "
            f"test ! -e {quoted_leaf}; test ! -L {quoted_leaf}"
        )
        self._exec_remote_run_bash(
            client,
            command,
            timeout=REMOTE_RUN_DELETE_TIMEOUT_SECONDS,
        )
        if self._sftp_exists(sftp, root):
            raise SSHBackendError("Remote Run quarantine still exists after deletion")

    def _resolve_real_remote_tree_path(
        self,
        sftp: paramiko.SFTPClient,
        root: str,
        relative_path: str,
        *,
        expected_directory: bool,
    ) -> tuple[str, paramiko.SFTPAttributes]:
        relative = self._validate_run_relative_path(
            relative_path, directory=expected_directory
        )
        root_attr = sftp.lstat(root)
        if stat_module.S_ISLNK(root_attr.st_mode) or not stat_module.S_ISDIR(
            root_attr.st_mode
        ):
            raise SSHBackendError("Remote tree root is not a real directory")
        canonical_root = posixpath.normpath(sftp.normalize(root))
        if canonical_root != posixpath.normpath(root):
            raise SSHBackendError("Remote tree root is not canonical")
        current = root
        if relative == ".":
            return current, root_attr
        components = relative.split("/")
        final_attr = root_attr
        for index, component in enumerate(components):
            current = posixpath.join(current, component)
            final_attr = sftp.lstat(current)
            if stat_module.S_ISLNK(final_attr.st_mode):
                raise UnsupportedFileError("Symbolic links are not exposed")
            if index < len(components) - 1 and not stat_module.S_ISDIR(final_attr.st_mode):
                raise NotADirectoryError("/".join(components[: index + 1]))
        canonical = posixpath.normpath(sftp.normalize(current))
        if canonical != current or (
            canonical != canonical_root
            and not canonical.startswith(canonical_root.rstrip("/") + "/")
        ):
            raise SSHBackendError("Remote path resolves outside its tree root")
        return current, final_attr

    def _new_remote_tree_path(
        self,
        sftp: paramiko.SFTPClient,
        root: str,
        relative_path: str,
        *,
        create_parents: bool,
    ) -> str:
        relative = self._validate_run_relative_path(relative_path, directory=False)
        if relative == ".":
            raise ValueError("A snapshot entry cannot replace its root")
        root_attr = sftp.lstat(root)
        if stat_module.S_ISLNK(root_attr.st_mode) or not stat_module.S_ISDIR(
            root_attr.st_mode
        ):
            raise SSHBackendError("Remote snapshot root is invalid")
        current = root
        parts = relative.split("/")
        for component in parts[:-1]:
            current = posixpath.join(current, component)
            try:
                attr = sftp.lstat(current)
            except OSError as exc:
                if not self._is_missing_sftp_error(exc) or not create_parents:
                    raise
                sftp.mkdir(current, mode=0o755)
                attr = sftp.lstat(current)
            if stat_module.S_ISLNK(attr.st_mode) or not stat_module.S_ISDIR(attr.st_mode):
                raise SSHBackendError("Snapshot parent is not a real directory")
        return posixpath.join(current, parts[-1])

    @staticmethod
    def _ensure_remote_directory(
        sftp: paramiko.SFTPClient, path: str, *, mode: int
    ) -> None:
        try:
            attr = sftp.lstat(path)
        except FileNotFoundError:
            sftp.mkdir(path, mode=mode)
            return
        except OSError as exc:
            if getattr(exc, "errno", None) == 2:
                sftp.mkdir(path, mode=mode)
                return
            raise
        if stat_module.S_ISLNK(attr.st_mode) or not stat_module.S_ISDIR(attr.st_mode):
            raise SSHBackendError(f"Remote Run path is not a directory: {path}")

    def ensure_workspace(self, workspace: dict[str, Any]) -> list[dict[str, Any]]:
        computer = self._computer(workspace)
        remote_path = self._remote_root(workspace)
        session = str(workspace["tmux_session"])
        quoted_session = shlex.quote(session)
        quoted_path = shlex.quote(remote_path)
        command = (
            f"test -d {quoted_path} || {{ echo '__TERMROOM_NO_DIR__' >&2; exit 44; }}; "
            "command -v tmux >/dev/null 2>&1 || "
            "{ echo '__TERMROOM_NO_TMUX__' >&2; exit 45; }; "
            f"tmux has-session -t {quoted_session} 2>/dev/null || "
            f"tmux new-session -d -s {quoted_session} -c {quoted_path} -n shell; "
            f"tmux set-window-option -t {quoted_session} window-size latest "
            ">/dev/null 2>&1 || true; "
            f"tmux list-windows -t {quoted_session} -F '#{{window_id}}|#{{window_name}}'"
        )
        output = self._exec(computer, self._remote_posix_command(command))
        windows: list[tuple[str, str]] = []
        for line in output.splitlines():
            window_id, separator, name = line.partition("|")
            if separator and window_id.startswith("@"):
                windows.append((window_id, name or "shell"))
        if not windows:
            raise SSHBackendError("Remote tmux session did not expose any terminal windows")
        stored = self.store.list_terminals(str(workspace["id"]))
        known = {item["tmux_window"] for item in stored}
        live = {window_id for window_id, _ in windows}
        if known != live:
            self.store.reset_terminals(str(workspace["id"]))
            for window_id, name in windows:
                self.store.create_terminal(str(workspace["id"]), name, window_id)
        return self.store.list_terminals(str(workspace["id"]))

    def session_exists(self, workspace: dict[str, Any]) -> bool:
        computer = self._computer(workspace)
        command = f"tmux has-session -t {shlex.quote(str(workspace['tmux_session']))}"
        try:
            self._exec(computer, command)
            return True
        except SSHBackendError:
            return False

    def create_terminal(self, workspace: dict[str, Any], name: str = "shell") -> dict[str, Any]:
        self.ensure_workspace(workspace)
        computer = self._computer(workspace)
        safe_name = normalize_terminal_name(name)
        command = (
            "tmux new-window -d -P -F '#{window_id}' "
            f"-t {shlex.quote(str(workspace['tmux_session']))} "
            f"-n {shlex.quote(safe_name)} -c {shlex.quote(self._remote_root(workspace))}"
        )
        window_id = self._exec(computer, command).strip()
        return self.store.create_terminal(str(workspace["id"]), safe_name, window_id)

    def rename_terminal(
        self, workspace: dict[str, Any], terminal: dict[str, Any], name: str
    ) -> dict[str, Any]:
        self.ensure_workspace(workspace)
        safe_name = normalize_terminal_name(name)
        command = (
            "tmux rename-window "
            f"-t {shlex.quote(str(terminal['tmux_window']))} {shlex.quote(safe_name)}"
        )
        self._exec(self._computer(workspace), command)
        self.store.rename_terminal(str(terminal["id"]), safe_name)
        updated = self.store.get_terminal(str(terminal["id"]))
        if not updated:
            raise SSHBackendError("Remote terminal disappeared while renaming")
        return updated

    def close_terminal(
        self, workspace: dict[str, Any], terminal: dict[str, Any]
    ) -> list[dict[str, Any]]:
        self.ensure_workspace(workspace)
        command = f"tmux kill-window -t {shlex.quote(str(terminal['tmux_window']))}"
        self._exec(self._computer(workspace), command)
        self.store.delete_terminal(str(terminal["id"]))
        return self.ensure_workspace(workspace)

    def capture_scrollback(
        self, workspace: dict[str, Any], terminal: dict[str, Any], lines: int = 2000
    ) -> str:
        self.ensure_workspace(workspace)
        command = (
            "tmux capture-pane -p -J "
            f"-S -{max(100, min(lines, 10000))} -t {shlex.quote(str(terminal['tmux_window']))}"
        )
        return self._exec(self._computer(workspace), command)

    async def bridge(
        self,
        websocket: WebSocket,
        workspace: dict[str, Any],
        terminal: dict[str, Any],
        *,
        device_id: str = "",
    ) -> None:
        await asyncio.to_thread(self.ensure_workspace, workspace)
        self.store.touch_terminal(str(terminal["id"]))
        terminal_id = str(terminal["id"])
        client_id = self.control.register(terminal_id)
        process_pid, master_fd = self._spawn_ssh_tmux_client(workspace, terminal)

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
                await asyncio.to_thread(self.store.touch_terminal_output, str(terminal["id"]))
                decoded = decoder.feed(chunk)
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
                        self.store.add_command,
                        str(workspace["id"]),
                        str(terminal["id"]),
                        command,
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

    def list_dir(
        self, workspace: dict[str, Any], relative_path: str = "."
    ) -> tuple[str, list[FileEntry]]:
        client, sftp = self._sftp(workspace)
        try:
            remote, directory_attr = self._existing_sftp_path(sftp, workspace, relative_path)
            if not stat_module.S_ISDIR(directory_attr.st_mode):
                raise NotADirectoryError(remote)
            attributes = sftp.listdir_attr(remote)
            entries: list[FileEntry] = []
            for attr in attributes:
                if stat_module.S_ISLNK(attr.st_mode):
                    continue
                is_dir = stat_module.S_ISDIR(attr.st_mode)
                child = posixpath.join(remote, attr.filename)
                entries.append(
                    FileEntry(
                        name=attr.filename,
                        relative_path=self._relative_remote(workspace, child),
                        is_dir=is_dir,
                        size=int(attr.st_size or 0),
                        mtime_ns=int(attr.st_mtime or 0) * 1_000_000_000,
                    )
                )
            entries.sort(key=lambda item: (not item.is_dir, item.name.casefold()))
            return remote, entries
        finally:
            sftp.close()
            client.close()

    def stat(self, workspace: dict[str, Any], relative_path: str) -> FileEntry:
        client, sftp = self._sftp(workspace)
        try:
            remote, attr = self._existing_sftp_path(sftp, workspace, relative_path)
            return FileEntry(
                name=PurePosixPath(remote).name,
                relative_path=self._relative_remote(workspace, remote),
                is_dir=stat_module.S_ISDIR(attr.st_mode),
                size=int(attr.st_size or 0),
                mtime_ns=int(attr.st_mtime or 0) * 1_000_000_000,
            )
        finally:
            sftp.close()
            client.close()

    def read_text(
        self, workspace: dict[str, Any], relative_path: str, max_bytes: int
    ) -> FileSnapshot:
        client, sftp = self._sftp(workspace)
        try:
            remote, attr = self._existing_sftp_path(sftp, workspace, relative_path)
            if not stat_module.S_ISREG(attr.st_mode):
                raise UnsupportedFileError("Only regular files can be edited")
            if int(attr.st_size or 0) > max_bytes:
                raise UnsupportedFileError("File exceeds the editable size limit")
            with sftp.open(remote, "rb") as handle:
                raw = handle.read()
            if b"\x00" in raw:
                raise UnsupportedFileError("Binary files cannot be edited")
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise UnsupportedFileError("Only UTF-8 text files can be edited") from exc
            return FileSnapshot(
                path=Path(remote),
                relative_path=self._relative_remote(workspace, remote),
                content=content,
                digest=file_digest(raw),
                mtime_ns=int(attr.st_mtime or 0) * 1_000_000_000,
            )
        finally:
            sftp.close()
            client.close()

    def read_text_preview(
        self,
        workspace: dict[str, Any],
        relative_path: str,
        *,
        mode: str,
        offset: int = 0,
        max_bytes: int,
    ) -> TextPreview:
        client, sftp = self._sftp(workspace)
        try:
            remote, attr = self._existing_sftp_path(sftp, workspace, relative_path)
            size = int(attr.st_size or 0)
            if not stat_module.S_ISREG(attr.st_mode):
                raise UnsupportedFileError("Only regular files can be previewed")
            limit = max(4096, min(max_bytes, 1024 * 1024))
            if mode not in {"head", "tail", "range"}:
                raise ValueError("Preview mode must be head, tail, or range")
            if mode == "tail":
                start = max(0, size - limit)
            elif mode == "range":
                start = max(0, min(int(offset), size))
            else:
                start = 0
            with sftp.open(remote, "rb") as handle:
                if start:
                    handle.seek(start)
                raw = handle.read(limit)
            if b"\x00" in raw:
                raise UnsupportedFileError("Binary files cannot be shown as text")
            try:
                content = decode_utf8_preview(
                    raw,
                    allow_partial_start=start > 0,
                    final=start + len(raw) >= size,
                )
            except UnicodeDecodeError as exc:
                raise UnsupportedFileError("Only UTF-8 text can be previewed") from exc
            if start:
                _, separator, remainder = content.partition("\n")
                if separator:
                    content = remainder
            return TextPreview(
                relative_path=self._relative_remote(workspace, remote),
                content=content,
                size=size,
                mtime_ns=int(attr.st_mtime or 0) * 1_000_000_000,
                mode=mode,
                truncated=start > 0 or start + len(raw) < size,
                offset=start,
                bytes_read=len(raw),
            )
        finally:
            sftp.close()
            client.close()

    def write_text(
        self,
        workspace: dict[str, Any],
        relative_path: str,
        content: str,
        *,
        expected_digest: str,
        expected_mtime_ns: int,
        max_bytes: int,
    ) -> FileSnapshot:
        encoded = content.encode("utf-8")
        if len(encoded) > max_bytes:
            raise UnsupportedFileError("Content exceeds the editable size limit")
        client, sftp = self._sftp(workspace)
        remote = ""
        temporary = ""
        try:
            remote, attr = self._existing_sftp_path(sftp, workspace, relative_path)
            temporary = f"{remote}.termroom-{uuid.uuid4().hex[:10]}"
            if not stat_module.S_ISREG(attr.st_mode):
                raise UnsupportedFileError("Only regular files can be saved")
            with sftp.open(remote, "rb") as handle:
                current = handle.read()
            current_mtime = int(attr.st_mtime or 0) * 1_000_000_000
            if current_mtime != expected_mtime_ns or file_digest(current) != expected_digest:
                raise FileConflictError("The file changed after it was opened")
            with sftp.open(temporary, "wb") as handle:
                handle.write(encoded)
                handle.flush()
            sftp.chmod(temporary, int(attr.st_mode) & 0o777)
            try:
                sftp.posix_rename(temporary, remote)
            except OSError as exc:
                raise SSHBackendError(
                    "Remote SFTP server does not support atomic replacement for this file"
                ) from exc
        finally:
            if temporary:
                with contextlib.suppress(OSError):
                    sftp.remove(temporary)
            sftp.close()
            client.close()
        return self.read_text(workspace, relative_path, max_bytes)

    def create(
        self, workspace: dict[str, Any], parent: str, name: str, *, directory: bool
    ) -> None:
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("Invalid name")
        client, sftp = self._sftp(workspace)
        try:
            target = self._new_sftp_path(sftp, workspace, parent, name)
            try:
                sftp.lstat(target)
            except OSError:
                pass
            else:
                raise FileExistsError(target)
            if directory:
                sftp.mkdir(target, mode=0o755)
            else:
                with sftp.open(target, "x"):
                    pass
        finally:
            sftp.close()
            client.close()

    def rename(self, workspace: dict[str, Any], relative_path: str, new_name: str) -> None:
        if not new_name or new_name in {".", ".."} or "/" in new_name or "\\" in new_name:
            raise ValueError("Invalid name")
        client, sftp = self._sftp(workspace)
        try:
            source, _ = self._existing_sftp_path(sftp, workspace, relative_path)
            parent = self._relative_remote(workspace, posixpath.dirname(source))
            target = self._new_sftp_path(sftp, workspace, parent, new_name)
            try:
                sftp.lstat(target)
            except OSError:
                pass
            else:
                raise FileExistsError(target)
            sftp.rename(source, target)
        finally:
            sftp.close()
            client.close()

    def delete(self, workspace: dict[str, Any], relative_path: str) -> None:
        client, sftp = self._sftp(workspace)
        try:
            remote, attr = self._existing_sftp_path(sftp, workspace, relative_path)
            if remote == self._remote_root(workspace):
                raise SSHBackendError("The Workspace root cannot be deleted")
            if stat_module.S_ISDIR(attr.st_mode):
                sftp.rmdir(remote)
            else:
                sftp.remove(remote)
        finally:
            sftp.close()
            client.close()

    async def upload(
        self,
        workspace: dict[str, Any],
        parent: str,
        upload: UploadFile,
        *,
        overwrite: bool,
        max_bytes: int,
    ) -> None:
        filename = upload.filename or ""
        if not filename or filename != PurePosixPath(filename).name:
            raise ValueError("Invalid upload filename")
        client, sftp = await asyncio.to_thread(self._sftp, workspace)
        target = await asyncio.to_thread(self._new_sftp_path, sftp, workspace, parent, filename)
        temporary = f"{target}.termroom-upload-{uuid.uuid4().hex[:10]}"
        total = 0
        try:
            exists = False
            try:
                attr = await asyncio.to_thread(sftp.lstat, target)
                exists = True
                if stat_module.S_ISLNK(attr.st_mode) or not stat_module.S_ISREG(attr.st_mode):
                    raise UnsupportedFileError("Upload target is not a regular file")
            except FileNotFoundError:
                attr = None
            except OSError as exc:
                if getattr(exc, "errno", None) != 2:
                    raise
                attr = None
            if exists and not overwrite:
                raise FileExistsError(filename)
            handle = await asyncio.to_thread(sftp.open, temporary, "wb")
            try:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("Upload exceeds the configured size limit")
                    await asyncio.to_thread(handle.write, chunk)
                await asyncio.to_thread(handle.flush)
            finally:
                await asyncio.to_thread(handle.close)
            if attr is not None:
                await asyncio.to_thread(sftp.chmod, temporary, int(attr.st_mode) & 0o777)
            else:
                await asyncio.to_thread(sftp.chmod, temporary, 0o644)
                if not overwrite:
                    try:
                        await asyncio.to_thread(sftp.lstat, target)
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        if getattr(exc, "errno", None) != 2:
                            raise
                    else:
                        raise FileExistsError(filename)
            try:
                await asyncio.to_thread(sftp.posix_rename, temporary, target)
            except OSError as exc:
                if exists:
                    raise SSHBackendError(
                        "Remote SFTP server cannot atomically overwrite this file"
                    ) from exc
                await asyncio.to_thread(sftp.rename, temporary, target)
        finally:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(sftp.remove, temporary)
            await asyncio.to_thread(sftp.close)
            await asyncio.to_thread(client.close)

    async def upload_stream(
        self,
        workspace: dict[str, Any],
        parent: str,
        filename: str,
        chunks: AsyncIterator[bytes],
        *,
        overwrite: bool,
        max_bytes: int,
    ) -> None:
        if not filename or filename != PurePosixPath(filename).name:
            raise ValueError("Invalid upload filename")
        client, sftp = await asyncio.to_thread(self._sftp, workspace)
        target = await asyncio.to_thread(self._new_sftp_path, sftp, workspace, parent, filename)
        temporary = f"{target}.termroom-upload-{uuid.uuid4().hex[:10]}"
        total = 0
        try:
            exists = False
            try:
                attr = await asyncio.to_thread(sftp.lstat, target)
                exists = True
                if stat_module.S_ISLNK(attr.st_mode) or not stat_module.S_ISREG(attr.st_mode):
                    raise UnsupportedFileError("Upload target is not a regular file")
            except FileNotFoundError:
                attr = None
            except OSError as exc:
                if getattr(exc, "errno", None) != 2:
                    raise
                attr = None
            if exists and not overwrite:
                raise FileExistsError(filename)

            handle = await asyncio.to_thread(sftp.open, temporary, "wb")
            try:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("Upload exceeds the configured size limit")
                    await asyncio.to_thread(handle.write, chunk)
                await asyncio.to_thread(handle.flush)
            finally:
                await asyncio.to_thread(handle.close)

            if attr is not None:
                await asyncio.to_thread(sftp.chmod, temporary, int(attr.st_mode) & 0o777)
            else:
                await asyncio.to_thread(sftp.chmod, temporary, 0o644)
                if not overwrite:
                    try:
                        await asyncio.to_thread(sftp.lstat, target)
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        if getattr(exc, "errno", None) != 2:
                            raise
                    else:
                        raise FileExistsError(filename)
            try:
                await asyncio.to_thread(sftp.posix_rename, temporary, target)
            except OSError as exc:
                if exists:
                    raise SSHBackendError(
                        "Remote SFTP server cannot atomically overwrite this file"
                    ) from exc
                await asyncio.to_thread(sftp.rename, temporary, target)
        finally:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(sftp.remove, temporary)
            await asyncio.to_thread(sftp.close)
            await asyncio.to_thread(client.close)

    def download_iter(
        self,
        workspace: dict[str, Any],
        relative_path: str,
        chunk_size: int = 1024 * 1024,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> Iterator[bytes]:
        client, sftp = self._sftp(workspace)
        try:
            remote, attr = self._existing_sftp_path(sftp, workspace, relative_path)
            if not stat_module.S_ISREG(attr.st_mode):
                raise UnsupportedFileError("Only regular files can be downloaded")
            handle = sftp.open(remote, "rb")
            try:
                if offset:
                    handle.seek(offset)
                remaining = length
                while True:
                    if remaining is not None and remaining <= 0:
                        break
                    read_size = chunk_size if remaining is None else min(chunk_size, remaining)
                    chunk = handle.read(read_size)
                    if not chunk:
                        break
                    if remaining is not None:
                        remaining -= len(chunk)
                    yield bytes(chunk)
            finally:
                handle.close()
        finally:
            sftp.close()
            client.close()

    def recent_files(self, workspace: dict[str, Any], *, limit: int = 50) -> RecentFiles:
        root = self._remote_root(workspace)
        ignore_patterns = self._recent_ignore_patterns(workspace)
        prune_names = ["-name '.*'"] + [
            f"-name {shlex.quote(name)}" for name in DEFAULT_RECENT_EXCLUDES
        ]
        prune = " -o ".join(prune_names)
        command = (
            f"cd {shlex.quote(root)} && "
            "timeout 2s find . -xdev -mindepth 1 "
            f"\\( -type d \\( {prune} \\) -prune \\) -o "
            "\\( -type f -printf '%T@\\t%s\\t%p\\n' \\)"
        )
        client = self._connect(self._computer(workspace))
        heap: list[tuple[float, str, FileEntry]] = []
        scanned = 0
        truncated = False
        try:
            stdin, stdout, stderr = client.exec_command(command, timeout=8)
            stdin.close()
            for raw_line in stdout:
                scanned += 1
                if scanned > 20_000:
                    truncated = True
                    stdout.channel.close()
                    break
                timestamp, separator, remainder = raw_line.rstrip("\n").partition("\t")
                size_text, separator2, relative = remainder.partition("\t")
                if not separator or not separator2:
                    continue
                try:
                    mtime = float(timestamp)
                    size = int(size_text)
                except ValueError:
                    continue
                relative = relative.removeprefix("./")
                if (
                    PurePosixPath(relative).name in DEFAULT_RECENT_EXCLUDES
                    or relative == RECENT_IGNORE_FILE
                    or recent_path_ignored(relative, ignore_patterns)
                ):
                    continue
                entry = FileEntry(
                    name=PurePosixPath(relative).name,
                    relative_path=relative,
                    is_dir=False,
                    size=size,
                    mtime_ns=int(mtime * 1_000_000_000),
                )
                key = (mtime, relative, entry)
                if len(heap) < limit:
                    heapq.heappush(heap, key)
                elif key[:2] > heap[0][:2]:
                    heapq.heapreplace(heap, key)
            status = stdout.channel.recv_exit_status()
            error = stderr.read().decode("utf-8", errors="replace")
            if status == 124:
                truncated = True
            elif status not in {0, 141} and "timeout" not in error.casefold():
                raise SSHBackendError(error.strip() or "Remote recent-file scan failed")
        finally:
            client.close()
        entries = [item[2] for item in sorted(heap, key=lambda item: item[:2], reverse=True)]
        return RecentFiles(entries=entries, scanned_files=scanned, truncated=truncated)

    def _recent_ignore_patterns(self, workspace: dict[str, Any]) -> tuple[str, ...]:
        client, sftp = self._sftp(workspace)
        try:
            root = self._remote_root(workspace)
            remote = posixpath.join(root, RECENT_IGNORE_FILE)
            try:
                attr = sftp.lstat(remote)
            except OSError:
                return ()
            if (
                not stat_module.S_ISREG(attr.st_mode)
                or int(attr.st_size or 0) > MAX_RECENT_IGNORE_BYTES
            ):
                return ()
            with sftp.open(remote, "rb") as handle:
                raw = handle.read(MAX_RECENT_IGNORE_BYTES)
            try:
                return parse_recent_ignore_patterns(raw.decode("utf-8"))
            except UnicodeDecodeError:
                return ()
        finally:
            sftp.close()
            client.close()

    def content_type(self, relative_path: str) -> str:
        from termroom.files import FileService

        return FileService().content_type(relative_path)

    def _connect(self, computer: dict[str, Any]) -> paramiko.SSHClient:
        if str(computer.get("auth_kind") or "key") == "password":
            try:
                client = self._connect_password(computer, self._stored_password(computer))
            except SSHBackendError as exc:
                self._record_connection(computer, error=exc)
                raise
            self._record_connection(computer)
            return client
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(
            _ExpectedHostKeyPolicy(
                str(computer["host_key_type"]), str(computer["host_key_data"])
            )
        )
        connect_kwargs: dict[str, Any] = {
            "hostname": str(computer["host"]),
            "port": int(computer["port"]),
            "username": str(computer["username"]),
            "timeout": 10,
            "banner_timeout": 10,
            "auth_timeout": 15,
            "allow_agent": True,
            "look_for_keys": True,
        }
        identity = str(computer.get("identity_file") or "")
        if identity:
            connect_kwargs["key_filename"] = self.validate_identity_file(identity)
        proxy = self._proxy_command(str(computer.get("ssh_alias") or ""))
        if proxy:
            connect_kwargs["sock"] = paramiko.ProxyCommand(proxy)
        try:
            client.connect(**connect_kwargs)
        except SSHHostKeyChanged as exc:
            self._record_connection(computer, error=exc)
            client.close()
            raise
        except paramiko.BadHostKeyException as exc:
            error = SSHHostKeyChanged(
                "SSH host key no longer matches the approved fingerprint",
                locale_key="ssh.backend.host_key_changed",
            )
            self._record_connection(computer, error=error)
            client.close()
            raise error from exc
        except (OSError, paramiko.SSHException) as exc:
            error = self.connection_error(
                exc, str(computer["host"]), int(computer["port"])
            )
            self._record_connection(computer, error=error)
            client.close()
            raise error from exc
        self._record_connection(computer)
        return client

    def _connect_password(
        self, computer: dict[str, Any], password: str
    ) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(
            _ExpectedHostKeyPolicy(
                str(computer["host_key_type"]), str(computer["host_key_data"])
            )
        )
        try:
            client.connect(
                hostname=str(computer["host"]),
                port=int(computer["port"]),
                username=str(computer["username"]),
                password=password,
                timeout=10,
                banner_timeout=10,
                auth_timeout=15,
                allow_agent=False,
                look_for_keys=False,
            )
        except paramiko.AuthenticationException as exc:
            client.close()
            raise SSHBackendError(
                "SSH password authentication failed",
                locale_key="ssh.backend.password_auth",
            ) from exc
        except (OSError, paramiko.SSHException) as exc:
            client.close()
            raise self.connection_error(
                exc, str(computer["host"]), int(computer["port"])
            ) from exc
        return client

    @staticmethod
    def connection_error(exc: BaseException, host: str, port: int) -> SSHBackendError:
        if isinstance(exc, paramiko.ssh_exception.NoValidConnectionsError):
            failures = list(exc.errors.values())
            if failures and all(isinstance(error, ConnectionRefusedError) for error in failures):
                return SSHBackendError(
                    f"SSH connection refused: {host}:{port}",
                    locale_key="ssh.backend.refused",
                    locale_values={"host": host, "port": port},
                )
            if failures and all(
                isinstance(error, (TimeoutError, socket.timeout)) for error in failures
            ):
                return SSHBackendError(
                    f"SSH connection timed out: {host}:{port}",
                    locale_key="ssh.backend.timeout",
                    locale_values={"host": host, "port": port},
                )
        if isinstance(exc, socket.gaierror):
            return SSHBackendError(
                f"Could not resolve SSH address: {host}",
                locale_key="ssh.backend.dns",
                locale_values={"host": host},
            )
        if isinstance(exc, ConnectionRefusedError):
            return SSHBackendError(
                f"SSH connection refused: {host}:{port}",
                locale_key="ssh.backend.refused",
                locale_values={"host": host, "port": port},
            )
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return SSHBackendError(
                f"SSH connection timed out: {host}:{port}",
                locale_key="ssh.backend.timeout",
                locale_values={"host": host, "port": port},
            )
        if isinstance(exc, paramiko.AuthenticationException):
            return SSHBackendError(
                "SSH authentication failed",
                locale_key="ssh.backend.auth",
            )
        return SSHBackendError(
            f"SSH connection failed: {exc}",
            locale_key="ssh.backend.connection",
            locale_values={"error": str(exc)},
        )

    def _record_connection(
        self, computer: dict[str, Any], *, error: BaseException | str | None = None
    ) -> None:
        computer_id = str(computer.get("id") or "")
        if computer_id:
            stored_error: str | None
            if isinstance(error, SSHBackendError) and error.locale_key:
                stored_error = "termroom-i18n:" + json.dumps(
                    {
                        "key": error.locale_key,
                        "values": error.locale_values,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            elif error is None:
                stored_error = None
            else:
                stored_error = str(error)
            self.store.update_computer_connection(computer_id, error=stored_error)

    @staticmethod
    def validate_identity_file(value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_file():
            raise SSHBackendError(f"SSH key file does not exist: {path}")
        mode = stat_module.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise SSHBackendError(
                f"SSH key permissions are too open ({mode:o}). Run: chmod 600 {path}"
            )
        return str(path.resolve())

    def _sftp(self, workspace: dict[str, Any]) -> tuple[paramiko.SSHClient, paramiko.SFTPClient]:
        client = self._connect(self._computer(workspace))
        try:
            return client, client.open_sftp()
        except Exception:
            client.close()
            raise

    def _exec(self, computer: dict[str, Any], command: str) -> str:
        client = self._connect(computer)
        try:
            return self._exec_client(client, command)
        finally:
            client.close()

    @staticmethod
    def _exec_client(
        client: paramiko.SSHClient,
        command: str,
        *,
        timeout: float | None = 20,
    ) -> str:
        try:
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            stdin.close()
            output = stdout.read().decode("utf-8", errors="replace")
            error = stderr.read().decode("utf-8", errors="replace")
            status = stdout.channel.recv_exit_status()
        except (EOFError, OSError, paramiko.SSHException) as exc:
            raise SSHCommandStatusUnknown(
                "SSH command completion status is unknown"
            ) from exc
        if status < 0:
            raise SSHCommandStatusUnknown("SSH command completion status is unknown")
        if status:
            if "__TERMROOM_NO_DIR__" in error:
                raise SSHBackendError("Remote Workspace directory does not exist")
            if "__TERMROOM_NO_TMUX__" in error:
                raise SSHBackendError("tmux is not installed on the remote computer")
            if "__TERMROOM_NO_BASH__" in error:
                raise SSHBackendError("/bin/bash is not installed on the remote computer")
            if "__TERMROOM_NO_GIT__" in error:
                raise SSHBackendError("git is not installed on the remote computer")
            if "__TERMROOM_RUN_EXISTS__" in error:
                raise SSHBackendError("Remote Run tmux session already exists")
            raise SSHBackendError(error.strip() or f"Remote command failed with exit {status}")
        return output

    def _computer(self, workspace: dict[str, Any]) -> dict[str, Any]:
        computer = workspace.get("computer") or self.store.get_computer(
            str(workspace.get("computer_id", ""))
        )
        if not computer:
            raise SSHBackendError("Remote computer configuration is missing")
        return computer

    @staticmethod
    def _remote_root(workspace: dict[str, Any]) -> str:
        value = str(workspace.get("remote_path") or workspace.get("canonical_path") or "")
        normalized = posixpath.normpath(value)
        server_terminal = str(workspace.get("workspace_kind") or "") == "server_terminal"
        if not normalized.startswith("/") or (normalized == "/" and not server_terminal):
            raise SSHBackendError("Remote Workspace root is invalid")
        return normalized

    def _remote_path(self, workspace: dict[str, Any], relative_path: str) -> str:
        root = PurePosixPath(self._remote_root(workspace))
        relative = PurePosixPath(relative_path or ".")
        if relative.is_absolute():
            raise SSHBackendError("Absolute paths are not allowed inside a Workspace")
        candidate = PurePosixPath(posixpath.normpath(str(root / relative)))
        if not candidate.is_relative_to(root):
            raise SSHBackendError("Path escapes the Workspace root")
        return candidate.as_posix()

    def _existing_sftp_path(
        self,
        sftp: paramiko.SFTPClient,
        workspace: dict[str, Any],
        relative_path: str,
    ) -> tuple[str, paramiko.SFTPAttributes]:
        candidate = self._remote_path(workspace, relative_path)
        attr = sftp.lstat(candidate)
        if stat_module.S_ISLNK(attr.st_mode):
            raise UnsupportedFileError("Symbolic links are not exposed")
        canonical = sftp.normalize(candidate)
        self._ensure_remote_contained(workspace, canonical)
        return canonical, attr

    def _new_sftp_path(
        self,
        sftp: paramiko.SFTPClient,
        workspace: dict[str, Any],
        parent: str,
        name: str,
    ) -> str:
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("Invalid name")
        parent_candidate = self._remote_path(workspace, parent)
        parent_attr = sftp.lstat(parent_candidate)
        if stat_module.S_ISLNK(parent_attr.st_mode) or not stat_module.S_ISDIR(
            parent_attr.st_mode
        ):
            raise UnsupportedFileError("Upload/create parent must be a real directory")
        canonical_parent = sftp.normalize(parent_candidate)
        self._ensure_remote_contained(workspace, canonical_parent)
        return posixpath.join(canonical_parent, name)

    def _ensure_remote_contained(self, workspace: dict[str, Any], canonical: str) -> None:
        root = posixpath.normpath(self._remote_root(workspace))
        value = posixpath.normpath(canonical)
        if value != root and not value.startswith(root.rstrip("/") + "/"):
            raise SSHBackendError("Remote path resolves outside the Workspace root")

    def _relative_remote(self, workspace: dict[str, Any], remote_path: str) -> str:
        root = PurePosixPath(self._remote_root(workspace))
        relative = PurePosixPath(remote_path).relative_to(root)
        value = relative.as_posix()
        return "." if value == "." else value

    def _proxy_command(self, alias: str) -> str:
        if not alias:
            return ""
        config = paramiko.SSHConfig()
        found = False
        for config_path in (self.ssh_dir / "config", Path.home() / ".ssh" / "config"):
            if not config_path.is_file():
                continue
            with config_path.open(encoding="utf-8") as handle:
                config.parse(handle)
            found = True
        if not found:
            return ""
        value = config.lookup(alias).get("proxycommand")
        return str(value or "")

    def _spawn_ssh_tmux_client(
        self, workspace: dict[str, Any], terminal: dict[str, Any]
    ) -> tuple[int, int]:
        computer = self._computer(workspace)
        self.remember_host_key(computer)
        environment = os.environ.copy()
        environment.pop("TMUX", None)
        environment.pop("TERMROOM_PASSWORD", None)
        environment["TERM"] = "xterm-256color"
        if str(computer.get("auth_kind") or "key") == "password":
            environment["SSH_ASKPASS"] = str(self._ensure_askpass_helper())
            environment["SSH_ASKPASS_REQUIRE"] = "force"
            environment["TERMROOM_CONFIG_DIR"] = str(self.state_dir)
            environment["TERMROOM_SSH_CREDENTIAL_ID"] = str(computer["id"])
            environment.setdefault("DISPLAY", "termroom")
        argv = self._ssh_argv(computer)
        remote_command = (
            "exec tmux attach-session -t "
            + shlex.quote(str(workspace["tmux_session"]))
            + " \\; select-window -t "
            + shlex.quote(str(terminal["tmux_window"]))
        )
        process_pid, master_fd = spawn_pty_process(
            [*argv, remote_command], environment=environment
        )
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process_pid, signal.SIGWINCH)
        return process_pid, master_fd

    def _ssh_argv(self, computer: dict[str, Any]) -> list[str]:
        target = str(computer.get("ssh_alias") or computer["host"])
        argv = [
            "ssh",
            "-tt",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.known_hosts_path}",
            "-o",
            f"HostKeyAlias=termroom-{computer['id']}",
            "-o",
            "ControlMaster=no",
        ]
        if str(computer.get("auth_kind") or "key") == "password":
            argv.extend(
                [
                    "-o",
                    "PreferredAuthentications=password,keyboard-interactive",
                    "-o",
                    "PubkeyAuthentication=no",
                    "-o",
                    "NumberOfPasswordPrompts=1",
                ]
            )
        else:
            argv.extend(["-o", "BatchMode=yes"])
        if not computer.get("ssh_alias"):
            argv.extend(["-p", str(computer["port"]), "-l", str(computer["username"])])
        identity = str(computer.get("identity_file") or "")
        if identity:
            argv.extend(["-i", os.path.expanduser(identity)])
        argv.append(target)
        return argv

    def _ensure_askpass_helper(self) -> Path:
        helper = self.state_dir / "ssh" / "askpass"
        helper.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        expected = (
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} -m termroom.ssh_askpass \"$@\"\n"
        )
        try:
            current = helper.read_text(encoding="utf-8")
        except OSError:
            current = ""
        if current != expected:
            temporary = helper.with_suffix(".tmp")
            temporary.write_text(expected, encoding="utf-8")
            temporary.chmod(0o700)
            os.replace(temporary, helper)
        helper.chmod(0o700)
        return helper

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
