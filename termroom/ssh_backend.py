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
from collections.abc import AsyncIterator, Iterator
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
            output = self._exec_client(
                client,
                "printf 'shell=%s\\n' \"${SHELL:-unknown}\"; "
                "if command -v tmux >/dev/null 2>&1; then tmux -V; else echo 'tmux=missing'; fi",
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
        canonical = self._exec(computer, command).strip()
        if not canonical.startswith("/") or canonical == "/":
            raise SSHBackendError("Remote Workspace path could not be canonicalized safely")
        return canonical

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
        output = self._exec(computer, command)
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
    def _exec_client(client: paramiko.SSHClient, command: str) -> str:
        stdin, stdout, stderr = client.exec_command(command, timeout=20)
        stdin.close()
        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()
        if status:
            if "__TERMROOM_NO_DIR__" in error:
                raise SSHBackendError("Remote Workspace directory does not exist")
            if "__TERMROOM_NO_TMUX__" in error:
                raise SSHBackendError("tmux is not installed on the remote computer")
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
        if not normalized.startswith("/") or normalized == "/":
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
