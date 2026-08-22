from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from fastapi import WebSocket, WebSocketDisconnect
from starlette.datastructures import UploadFile

from termroom.db import StateStore
from termroom.files import (
    DEFAULT_FILE_SEARCH_MAX_ENTRIES,
    DEFAULT_FILE_SEARCH_MAX_MATCHES,
    DEFAULT_FILE_SEARCH_MAX_SECONDS,
    DirectoryListingLimitError,
    FileConflictError,
    FileEntry,
    FileSearch,
    FileService,
    FileSnapshot,
    RecentFiles,
    RunnableFile,
    TextPreview,
    UnsupportedFileError,
    file_browser_entry_is_noise,
)
from termroom.node_core import NodeCore, NodeCoreError
from termroom.node_protocol import NODE_WORKSPACE_USAGE_VERSION
from termroom.security import PathBoundaryError, file_digest
from termroom.ssh_backend import SSHBackend
from termroom.terminal_control import TerminalControl
from termroom.terminals import (
    MAX_TERMINAL_MESSAGE_BYTES,
    TerminalOutputDecoder,
    terminal_input_claims_grid,
    terminal_size,
    touch_terminal_output_if_present,
)
from termroom.workspace_usage import (
    RawWorkspaceUsage,
    WorkspaceUsageOffline,
    WorkspaceUsageStale,
    WorkspaceUsageUnavailable,
    parse_raw_workspace_usage,
)


class RemoteAccessError(RuntimeError):
    def __init__(self, message: str, *, code: str = "remote_error") -> None:
        super().__init__(message)
        self.code = code


async def _settle_bridge_tasks(
    done: set[asyncio.Task[None]], pending: set[asyncio.Task[None]]
) -> None:
    try:
        for task in done:
            with contextlib.suppress(WebSocketDisconnect, asyncio.CancelledError):
                await task
    finally:
        await _cancel_bridge_tasks(pending)


async def _cancel_bridge_tasks(tasks: Iterable[asyncio.Task[None]]) -> None:
    tracked = tuple(tasks)
    for task in tracked:
        if not task.done():
            task.cancel()
    if tracked:
        await asyncio.gather(*tracked, return_exceptions=True)


class RemoteAccess:
    """Explicit SSH-or-Node dispatch for the shared Remote product surface."""

    TERMINAL_ACTIVITY_MAX_CONCURRENT_COMPUTERS = 4

    def __init__(
        self,
        store: StateStore,
        ssh: SSHBackend,
        nodes: NodeCore,
        control: TerminalControl,
    ) -> None:
        self.store = store
        self.ssh = ssh
        self.nodes = nodes
        self.control = control
        self._terminal_activity_concurrency = asyncio.Semaphore(
            self.TERMINAL_ACTIVITY_MAX_CONCURRENT_COMPUTERS
        )
        self._node_terminal_resize_locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def is_node(value: Mapping[str, Any]) -> bool:
        computer = value.get("computer") if "computer" in value else value
        return isinstance(computer, Mapping) and computer.get("connection_method") == "node"

    def status(self, computer: Mapping[str, Any]) -> dict[str, Any]:
        if not self.is_node(computer):
            return {"online": None, "capabilities": ()}
        status = self.nodes.status(str(computer["id"]))
        return {"online": status.online, "capabilities": status.capabilities}

    def supports_capability(self, value: Mapping[str, Any], capability: str) -> bool:
        if not self.is_node(value):
            return True
        computer = value.get("computer") if "computer" in value else value
        if not isinstance(computer, Mapping):
            return False
        return capability in self.nodes.status(str(computer["id"])).capabilities

    async def list_browse_directories(
        self,
        computer: dict[str, Any],
        path: str | None,
        *,
        show_hidden: bool,
    ) -> dict[str, Any]:
        if not self.is_node(computer):
            return await asyncio.to_thread(
                self.ssh.list_browse_directories,
                computer,
                path,
                show_hidden=show_hidden,
            )
        result = await self._node_request(
            computer,
            "workspace.browse",
            {"path": path, "show_hidden": show_hidden},
        )
        current = self._absolute_path(result.get("current"))
        parent_value = result.get("parent")
        parent = self._absolute_path(parent_value) if parent_value is not None else None
        entries_value = result.get("entries")
        if not isinstance(entries_value, list) or len(entries_value) > 10_000:
            raise RemoteAccessError("Node returned an invalid folder list")
        entries: list[dict[str, str]] = []
        for raw in entries_value:
            if not isinstance(raw, dict):
                raise RemoteAccessError("Node returned an invalid folder list")
            entries.append(
                {
                    "name": str(raw.get("name") or "")[:255],
                    "path": self._absolute_path(raw.get("path")),
                }
            )
        return {
            "current": current,
            "parent": parent,
            "entries": entries,
            "hidden_count": max(0, int(result.get("hidden_count") or 0)),
            "show_hidden": result.get("show_hidden") is True,
        }

    async def create_project_directory(
        self, computer: dict[str, Any], parent: str, name: str
    ) -> str:
        if not self.is_node(computer):
            return await asyncio.to_thread(
                self.ssh.create_project_directory, computer, parent, name
            )
        result = await self._node_request(
            computer,
            "workspace.create_project",
            {"parent": parent, "name": name},
        )
        return self._absolute_path(result.get("path"))

    async def validate_workspace_path(self, computer: dict[str, Any], path: str) -> str:
        if not self.is_node(computer):
            return await asyncio.to_thread(self.ssh.validate_workspace_path, computer, path)
        result = await self._node_request(computer, "workspace.validate", {"path": path})
        return self._absolute_path(result.get("path"))

    async def ensure_workspace(self, workspace: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.is_node(workspace):
            return await asyncio.to_thread(self.ssh.ensure_workspace, workspace)
        result = await self._workspace_request(workspace, "workspace.ensure", {})
        return self._reconcile_terminals(workspace, result.get("terminals"))

    async def refresh_terminal_activity(
        self, workspaces: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """Refresh Remote activity once per computer for explicit workspaces."""

        grouped: dict[str, list[dict[str, Any]]] = {}
        for workspace in workspaces:
            computer = workspace.get("computer")
            if not isinstance(computer, Mapping):
                continue
            grouped.setdefault(str(computer["id"]), []).append(workspace)
        targets = self.store.terminal_activity_targets(
            str(workspace["id"])
            for computer_workspaces in grouped.values()
            for workspace in computer_workspaces
        )

        async def refresh_computer(
            computer_workspaces: list[dict[str, Any]],
        ) -> dict[str, list[dict[str, Any]]]:
            async with self._terminal_activity_concurrency:
                computer = computer_workspaces[0]["computer"]
                try:
                    if not self.is_node(computer):
                        return await asyncio.to_thread(
                            self.ssh.refresh_activity, computer, computer_workspaces
                        )
                    request_workspaces: list[dict[str, Any]] = []
                    by_session: dict[str, dict[str, Any]] = {}
                    requested_windows: dict[str, set[str]] = {}
                    for workspace in computer_workspaces:
                        session = str(workspace["tmux_session"])
                        windows = targets.get(str(workspace["id"]), [])
                        if not windows:
                            continue
                        if session in by_session:
                            raise RemoteAccessError("Node Terminal activity batch is ambiguous")
                        by_session[session] = workspace
                        requested_windows[session] = set(windows)
                        request_workspaces.append(
                            {
                                "tmux_session": session,
                                "windows": windows,
                            }
                        )
                    if not request_workspaces:
                        return {}
                    result = await self._node_request(
                        computer,
                        "terminal.activity",
                        {"workspaces": request_workspaces},
                    )
                    raw_workspaces = result.get("workspaces")
                    if not isinstance(raw_workspaces, list) or len(raw_workspaces) != len(
                        request_workspaces
                    ):
                        raise RemoteAccessError("Node returned invalid Terminal activity")
                    parsed: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
                    for raw in raw_workspaces:
                        if not isinstance(raw, Mapping):
                            raise RemoteAccessError("Node returned invalid Terminal activity")
                        session = str(raw.get("tmux_session") or "")
                        workspace = by_session.get(session)
                        terminals = raw.get("terminals")
                        if (
                            workspace is None
                            or session in parsed
                            or not isinstance(terminals, list)
                            or len(terminals) > len(requested_windows[session])
                        ):
                            raise RemoteAccessError("Node returned invalid Terminal activity")
                        records = [self._terminal_record(item) for item in terminals]
                        returned_windows = [str(item["tmux_window"]) for item in records]
                        if len(set(returned_windows)) != len(returned_windows) or not set(
                            returned_windows
                        ).issubset(requested_windows[session]):
                            raise RemoteAccessError("Node returned invalid Terminal activity")
                        parsed[session] = (workspace, records)
                    if set(parsed) != set(by_session):
                        raise RemoteAccessError("Node returned incomplete Terminal activity")
                    return self.store.observe_terminal_activity_batch(
                        {str(workspace["id"]): records for workspace, records in parsed.values()}
                    )
                except (OSError, RuntimeError, ValueError):
                    return {}

        batches = await asyncio.gather(*(refresh_computer(items) for items in grouped.values()))
        refreshed: dict[str, list[dict[str, Any]]] = {}
        for batch in batches:
            refreshed.update(batch)
        return refreshed

    async def workspace_usage(self, workspace: dict[str, Any]) -> RawWorkspaceUsage:
        if not self.is_node(workspace):
            return await asyncio.to_thread(self.ssh.workspace_usage, workspace)
        computer = workspace.get("computer")
        if not isinstance(computer, Mapping):
            raise WorkspaceUsageUnavailable("Remote Workspace computer is missing")
        if computer.get("node_revoked_at") is not None:
            raise WorkspaceUsageUnavailable("Node connection has been revoked", code="node_revoked")
        status = self.nodes.status(str(computer["id"]))
        if not status.online:
            raise WorkspaceUsageOffline()
        if "workspace_usage" not in status.capabilities:
            raise WorkspaceUsageUnavailable(
                "This Node does not support Workspace activity",
                code="capability_unsupported",
            )
        try:
            result = await self._workspace_request(
                workspace,
                "workspace.usage",
                {"workspace_usage_version": NODE_WORKSPACE_USAGE_VERSION},
            )
        except RemoteAccessError as exc:
            if exc.code == "node_offline":
                raise WorkspaceUsageOffline() from exc
            if exc.code == "refresh_incomplete":
                raise WorkspaceUsageStale() from exc
            raise WorkspaceUsageUnavailable(
                "Node Workspace activity is unavailable", code=exc.code
            ) from exc
        version = result.get("workspace_usage_version")
        if isinstance(version, bool) or version != NODE_WORKSPACE_USAGE_VERSION:
            raise WorkspaceUsageUnavailable(
                "Node Workspace activity version is incompatible",
                code="workspace_usage_version_incompatible",
            )
        return parse_raw_workspace_usage(result.get("usage"))

    async def create_terminal(self, workspace: dict[str, Any], name: str) -> dict[str, Any]:
        if not self.is_node(workspace):
            return await asyncio.to_thread(self.ssh.create_terminal, workspace, name)
        result = await self._workspace_request(workspace, "terminal.create", {"name": name})
        terminal_record = self._terminal_record(result.get("terminal"))
        terminals = await self.ensure_workspace(workspace)
        terminal = next(
            (item for item in terminals if item["tmux_window"] == terminal_record["tmux_window"]),
            None,
        )
        if terminal is None:
            raise RemoteAccessError("Node Terminal disappeared while creating it")
        return terminal

    async def open_terminal_editor(
        self, workspace: dict[str, Any], relative_path: str
    ) -> dict[str, Any]:
        if not self.is_node(workspace):
            return await asyncio.to_thread(
                self.ssh.open_terminal_editor, workspace, relative_path
            )
        if not self.supports_capability(workspace, "terminal_editor"):
            raise RemoteAccessError(
                "This Node does not support Vim file editing; update Termroom Node",
                code="capability_unsupported",
            )
        result = await self._workspace_request(
            workspace,
            "terminal.editor.open",
            {"path": relative_path},
        )
        expected = self._terminal_record(result.get("terminal"))
        terminals = self._reconcile_terminals(workspace, result.get("terminals"))
        terminal = next(
            (
                item
                for item in terminals
                if item["tmux_window"] == expected["tmux_window"]
            ),
            None,
        )
        if terminal is None:
            raise RemoteAccessError("Node Vim Terminal disappeared while starting")
        return terminal

    async def run_workspace_command(
        self,
        workspace: dict[str, Any],
        *,
        slot: int,
        command: str,
        launch_id: str,
    ) -> dict[str, Any]:
        if not self.is_node(workspace):
            return await asyncio.to_thread(
                self.ssh.run_workspace_command,
                workspace,
                slot=slot,
                command=command,
                launch_id=launch_id,
            )
        if not self.supports_capability(workspace, "workspace_command"):
            raise RemoteAccessError(
                "This Node does not support Workspace commands; update Termroom Node",
                code="capability_unsupported",
            )
        result = await self._workspace_request(
            workspace,
            "workspace.command.run",
            {"slot": slot, "command": command, "launch_id": launch_id},
        )
        expected = self._terminal_record(result.get("terminal"))
        terminals = self._reconcile_terminals(workspace, result.get("terminals"))
        terminal = next(
            (item for item in terminals if item["tmux_window"] == expected["tmux_window"]),
            None,
        )
        if terminal is None:
            raise RemoteAccessError("Node Workspace command Terminal disappeared while starting")
        return terminal

    async def rename_terminal(
        self,
        workspace: dict[str, Any],
        terminal: dict[str, Any],
        name: str,
    ) -> dict[str, Any]:
        if not self.is_node(workspace):
            return await asyncio.to_thread(self.ssh.rename_terminal, workspace, terminal, name)
        await self._workspace_request(
            workspace,
            "terminal.rename",
            {"tmux_window": terminal["tmux_window"], "name": name},
        )
        terminals = await self.ensure_workspace(workspace)
        updated = next((item for item in terminals if item["id"] == terminal["id"]), None)
        if updated is None:
            raise RemoteAccessError("Node Terminal disappeared while renaming it")
        return updated

    async def close_terminal(
        self, workspace: dict[str, Any], terminal: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if not self.is_node(workspace):
            return await asyncio.to_thread(self.ssh.close_terminal, workspace, terminal)
        result = await self._workspace_request(
            workspace,
            "terminal.close",
            {"tmux_window": terminal["tmux_window"]},
        )
        return self._reconcile_terminals(workspace, result.get("terminals"))

    async def capture_scrollback(
        self,
        workspace: dict[str, Any],
        terminal: dict[str, Any],
        lines: int,
        *,
        history_only: bool = False,
    ) -> str:
        if not self.is_node(workspace):
            return await asyncio.to_thread(
                self.ssh.capture_scrollback,
                workspace,
                terminal,
                lines,
                history_only=history_only,
            )
        result = await self._workspace_request(
            workspace,
            "terminal.scrollback",
            {
                "tmux_window": terminal["tmux_window"],
                "lines": lines,
                "history_only": history_only,
            },
        )
        output = result.get("output")
        if not isinstance(output, str):
            raise RemoteAccessError("Node returned invalid Terminal scrollback")
        return output

    async def list_dir(
        self,
        workspace: dict[str, Any],
        relative_path: str,
        *,
        max_entries: int | None = None,
        max_metadata_bytes: int | None = None,
    ) -> tuple[str, list[FileEntry]]:
        if not self.is_node(workspace):
            return await asyncio.to_thread(
                self.ssh.list_dir,
                workspace,
                relative_path,
                max_entries=max_entries,
                max_metadata_bytes=max_metadata_bytes,
            )
        payload: dict[str, Any] = {"path": relative_path}
        if max_entries is not None:
            payload["max_entries"] = max_entries
        if max_metadata_bytes is not None:
            payload["max_metadata_bytes"] = max_metadata_bytes
        result = await self._workspace_request(workspace, "files.list", payload)
        directory = str(result.get("directory") or ".")
        entries = self._file_entries(result.get("entries"))
        return directory, entries

    async def search_files(
        self,
        workspace: dict[str, Any],
        relative_path: str,
        query: str,
        *,
        include_noise: bool = False,
    ) -> FileSearch:
        if not self.is_node(workspace):
            return await asyncio.to_thread(
                self.ssh.search_files,
                workspace,
                relative_path,
                query,
                include_noise=include_noise,
            )
        try:
            result = await self._workspace_request(
                workspace,
                "files.search",
                {
                    "path": relative_path,
                    "query": query,
                    "include_noise": include_noise,
                },
            )
        except RemoteAccessError as exc:
            if exc.code != "operation_unsupported":
                raise
            return await self._search_files_via_listing(
                workspace,
                relative_path,
                query,
                include_noise=include_noise,
            )

        scanned_entries = result.get("scanned_entries")
        skipped_noise = result.get("skipped_noise")
        truncated = result.get("truncated")
        if (
            isinstance(scanned_entries, bool)
            or not isinstance(scanned_entries, int)
            or not 0 <= scanned_entries <= DEFAULT_FILE_SEARCH_MAX_ENTRIES
            or isinstance(skipped_noise, bool)
            or not isinstance(skipped_noise, int)
            or not 0 <= skipped_noise <= scanned_entries
            or not isinstance(truncated, bool)
        ):
            raise RemoteAccessError("Node returned an invalid file search result")
        entries = self._file_entries(result.get("entries"))
        if len(entries) > DEFAULT_FILE_SEARCH_MAX_MATCHES:
            raise RemoteAccessError("Node returned too many file search results")
        return FileSearch(
            entries=entries,
            scanned_entries=scanned_entries,
            skipped_noise=skipped_noise,
            truncated=truncated,
        )

    async def _search_files_via_listing(
        self,
        workspace: dict[str, Any],
        relative_path: str,
        query: str,
        *,
        include_noise: bool,
    ) -> FileSearch:
        needle = str(query).strip().casefold()
        if not needle:
            return FileSearch(entries=[], scanned_entries=0, skipped_noise=0, truncated=False)
        deadline = time.monotonic() + DEFAULT_FILE_SEARCH_MAX_SECONDS
        pending = [relative_path]
        matches: list[FileEntry] = []
        scanned = 0
        skipped_noise = 0
        truncated = False
        stop = False

        while pending and not stop:
            if scanned >= DEFAULT_FILE_SEARCH_MAX_ENTRIES or time.monotonic() >= deadline:
                truncated = True
                break
            directory = pending.pop()
            remaining = DEFAULT_FILE_SEARCH_MAX_ENTRIES - scanned
            try:
                _, children = await self.list_dir(
                    workspace,
                    directory,
                    max_entries=min(remaining, 10_000),
                    max_metadata_bytes=768 * 1024,
                )
            except DirectoryListingLimitError:
                truncated = True
                continue
            for entry in children:
                if scanned >= DEFAULT_FILE_SEARCH_MAX_ENTRIES or time.monotonic() >= deadline:
                    truncated = True
                    stop = True
                    break
                scanned += 1
                if file_browser_entry_is_noise(entry) and not include_noise:
                    skipped_noise += 1
                    continue
                if needle in entry.name.casefold():
                    if len(matches) >= DEFAULT_FILE_SEARCH_MAX_MATCHES:
                        truncated = True
                        stop = True
                        break
                    matches.append(entry)
                if entry.is_dir:
                    pending.append(entry.relative_path)

        matches.sort(key=lambda item: (not item.is_dir, item.relative_path.casefold()))
        return FileSearch(
            entries=matches,
            scanned_entries=scanned,
            skipped_noise=skipped_noise,
            truncated=truncated,
        )

    async def stat(self, workspace: dict[str, Any], relative_path: str) -> FileEntry:
        if not self.is_node(workspace):
            return await asyncio.to_thread(self.ssh.stat, workspace, relative_path)
        result = await self._workspace_request(workspace, "files.stat", {"path": relative_path})
        entries = self._file_entries([result.get("entry")])
        return entries[0]

    async def read_text(
        self, workspace: dict[str, Any], relative_path: str, max_bytes: int
    ) -> FileSnapshot:
        if not self.is_node(workspace):
            return await asyncio.to_thread(self.ssh.read_text, workspace, relative_path, max_bytes)
        stream = None
        try:
            connection = self.nodes.connection(str(workspace["computer"]["id"]))
            result, stream = await connection.open_stream(
                "files.read_text.open",
                {
                    **self._workspace_payload(workspace),
                    "path": relative_path,
                    "max_bytes": max_bytes,
                },
            )
            raw = bytearray()
            async for chunk in stream:
                if len(raw) + len(chunk) > max_bytes:
                    raise RemoteAccessError("Node returned an oversized file snapshot")
                raw.extend(chunk)
            if result.get("size") != len(raw):
                raise RemoteAccessError("Node returned an invalid file snapshot size")
            if b"\x00" in raw:
                raise RemoteAccessError("Node returned binary editor content")
            try:
                content = bytes(raw).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RemoteAccessError("Node returned invalid UTF-8 editor content") from exc
            snapshot = result.get("snapshot")
            if not isinstance(snapshot, dict):
                raise RemoteAccessError("Node returned an invalid file snapshot")
            return self._file_snapshot({**snapshot, "content": content}, max_bytes=max_bytes)
        except NodeCoreError as exc:
            self._raise_node_error(exc)
        finally:
            if stream is not None:
                await stream.close()

    async def write_text(
        self,
        workspace: dict[str, Any],
        relative_path: str,
        content: str,
        *,
        expected_digest: str,
        expected_mtime_ns: int,
        max_bytes: int,
    ) -> FileSnapshot:
        if not self.is_node(workspace):
            return await asyncio.to_thread(
                self.ssh.write_text,
                workspace,
                relative_path,
                content,
                expected_digest=expected_digest,
                expected_mtime_ns=expected_mtime_ns,
                max_bytes=max_bytes,
            )
        if len(content.encode("utf-8")) > max_bytes:
            raise RemoteAccessError("Content exceeds the editable size limit")
        stream = None
        try:
            connection = self.nodes.connection(str(workspace["computer"]["id"]))
            _, stream = await connection.open_stream(
                "files.write_text.open",
                {
                    **self._workspace_payload(workspace),
                    "path": relative_path,
                    "expected_digest": expected_digest,
                    "expected_mtime_ns": expected_mtime_ns,
                    "max_bytes": max_bytes,
                },
            )
            await stream.send(content.encode("utf-8"))
            result = await stream.finish()
            snapshot = result.get("snapshot")
            if not isinstance(snapshot, dict):
                raise RemoteAccessError("Node returned an invalid saved file snapshot")
            return self._file_snapshot({**snapshot, "content": content}, max_bytes=max_bytes)
        except NodeCoreError as exc:
            if stream is not None:
                await stream.abort()
            self._raise_node_error(exc)
        except BaseException:
            if stream is not None:
                await stream.abort()
            raise

    async def read_text_preview(
        self,
        workspace: dict[str, Any],
        relative_path: str,
        *,
        mode: str,
        offset: int,
        max_bytes: int,
    ) -> TextPreview:
        if not self.is_node(workspace):
            return await asyncio.to_thread(
                self.ssh.read_text_preview,
                workspace,
                relative_path,
                mode=mode,
                offset=offset,
                max_bytes=max_bytes,
            )
        result = await self._workspace_request(
            workspace,
            "files.read_preview",
            {
                "path": relative_path,
                "mode": mode,
                "offset": offset,
                "max_bytes": max_bytes,
            },
        )
        value = result.get("preview")
        if not isinstance(value, dict) or not isinstance(value.get("content"), str):
            raise RemoteAccessError("Node returned an invalid file preview")
        return TextPreview(
            relative_path=str(value.get("relative_path") or ""),
            content=value["content"],
            size=int(value.get("size") or 0),
            mtime_ns=int(value.get("mtime_ns") or 0),
            mode=str(value.get("mode") or "head"),
            truncated=value.get("truncated") is True,
            offset=int(value.get("offset") or 0),
            bytes_read=int(value.get("bytes_read") or 0),
        )

    async def inspect_runnable(
        self,
        workspace: dict[str, Any],
        relative_path: str,
        *,
        expected_digest: str | None,
        max_bytes: int,
        runner_registry_version: int,
    ) -> RunnableFile:
        result = await self._node_file_run_request(
            workspace,
            "file_run.inspect",
            {
                "path": relative_path,
                "expected_digest": expected_digest,
                "max_bytes": max_bytes,
                "runner_registry_version": runner_registry_version,
            },
        )
        if result.get("runner_registry_version") != runner_registry_version:
            raise RemoteAccessError(
                "Node File Run Runner Registry is incompatible",
                code="runner_registry_incompatible",
            )
        value = result.get("runnable")
        if not isinstance(value, dict):
            raise RemoteAccessError("Node returned an invalid runnable file")
        path = self._relative_path(value.get("relative_path"))
        digest = self._file_digest(value.get("digest"))
        executable = value.get("executable")
        has_shebang = value.get("has_shebang")
        if not isinstance(executable, bool) or not isinstance(has_shebang, bool):
            raise RemoteAccessError("Node returned an invalid runnable file")
        runner = result.get("runner")
        if runner is not None and (
            not isinstance(runner, dict)
            or not isinstance(runner.get("id"), str)
            or runner.get("version") != runner_registry_version
        ):
            raise RemoteAccessError("Node returned an invalid File Runner")
        return RunnableFile(
            relative_path=path,
            digest=digest,
            executable=executable,
            has_shebang=has_shebang,
        )

    async def start_file_run(
        self,
        workspace: dict[str, Any],
        *,
        run_id: str,
        relative_path: str,
        expected_digest: str,
        runner_id: str,
        runner_version: int,
        runner_registry_version: int,
    ) -> dict[str, Any]:
        result = await self._node_file_run_request(
            workspace,
            "file_run.start",
            {
                "workspace_id": str(workspace["id"]),
                "run_id": run_id,
                "path": relative_path,
                "expected_digest": expected_digest,
                "runner_id": runner_id,
                "runner_version": runner_version,
                "runner_registry_version": runner_registry_version,
            },
        )
        expected = self._terminal_record(result.get("terminal"))
        terminals = self._reconcile_terminals(workspace, result.get("terminals"))
        terminal = next(
            (
                item
                for item in terminals
                if item["tmux_window"] == expected["tmux_window"]
                and item.get("role") == "file_run"
                and item.get("managed_run_id") == run_id
            ),
            None,
        )
        if terminal is None:
            raise RemoteAccessError("Node managed File Run Terminal is missing")
        return terminal

    async def inspect_file_run(
        self,
        workspace: dict[str, Any],
        *,
        run_id: str,
        runner_registry_version: int,
    ) -> dict[str, Any]:
        result = await self._node_file_run_request(
            workspace,
            "file_run.observe",
            {
                "workspace_id": str(workspace["id"]),
                "run_id": run_id,
                "runner_registry_version": runner_registry_version,
            },
        )
        if "terminals" in result:
            self._reconcile_terminals(workspace, result["terminals"])
        return self._file_run_observation(result.get("observation"))

    async def interrupt_file_run(
        self,
        workspace: dict[str, Any],
        *,
        run_id: str,
        runner_registry_version: int,
    ) -> bool:
        return await self._control_file_run(
            workspace,
            "file_run.interrupt",
            run_id=run_id,
            runner_registry_version=runner_registry_version,
        )

    async def kill_file_run(
        self,
        workspace: dict[str, Any],
        *,
        run_id: str,
        runner_registry_version: int,
    ) -> bool:
        return await self._control_file_run(
            workspace,
            "file_run.kill",
            run_id=run_id,
            runner_registry_version=runner_registry_version,
        )

    async def _control_file_run(
        self,
        workspace: dict[str, Any],
        operation: str,
        *,
        run_id: str,
        runner_registry_version: int,
    ) -> bool:
        result = await self._node_file_run_request(
            workspace,
            operation,
            {
                "workspace_id": str(workspace["id"]),
                "run_id": run_id,
                "runner_registry_version": runner_registry_version,
            },
        )
        sent = result.get("sent")
        if not isinstance(sent, bool):
            raise RemoteAccessError("Node returned an invalid File Run control result")
        return sent

    async def recent_files(self, workspace: dict[str, Any], *, limit: int = 50) -> RecentFiles:
        if self.is_node(workspace):
            if not self.supports_capability(workspace, "recent"):
                raise RemoteAccessError(
                    "Recent is not available for this Node",
                    code="capability_unsupported",
                )
            result = await self._workspace_request(
                workspace,
                "files.recent",
                {"limit": limit},
            )
            scanned_files = result.get("scanned_files")
            if isinstance(scanned_files, bool) or not isinstance(scanned_files, int):
                raise RemoteAccessError("Node returned an invalid Recent scan count")
            if scanned_files < 0 or scanned_files > 100_000:
                raise RemoteAccessError("Node returned an invalid Recent scan count")
            truncated = result.get("truncated")
            if not isinstance(truncated, bool):
                raise RemoteAccessError("Node returned an invalid Recent scan state")
            return RecentFiles(
                entries=self._file_entries(result.get("entries")),
                scanned_files=scanned_files,
                truncated=truncated,
            )
        if limit == 50:
            return await asyncio.to_thread(self.ssh.recent_files, workspace)
        return await asyncio.to_thread(self.ssh.recent_files, workspace, limit=limit)

    async def create(
        self,
        workspace: dict[str, Any],
        parent: str,
        name: str,
        *,
        directory: bool,
    ) -> None:
        if not self.is_node(workspace):
            await asyncio.to_thread(self.ssh.create, workspace, parent, name, directory=directory)
            return
        await self._workspace_request(
            workspace,
            "files.create",
            {"parent": parent, "name": name, "directory": directory},
        )

    async def rename(self, workspace: dict[str, Any], relative_path: str, new_name: str) -> None:
        if not self.is_node(workspace):
            await asyncio.to_thread(self.ssh.rename, workspace, relative_path, new_name)
            return
        await self._workspace_request(
            workspace,
            "files.rename",
            {"path": relative_path, "new_name": new_name},
        )

    async def delete(self, workspace: dict[str, Any], relative_path: str) -> None:
        if not self.is_node(workspace):
            await asyncio.to_thread(self.ssh.delete, workspace, relative_path)
            return
        await self._workspace_request(workspace, "files.delete", {"path": relative_path})

    async def upload(
        self,
        workspace: dict[str, Any],
        parent: str,
        upload: UploadFile,
        *,
        overwrite: bool,
        max_bytes: int,
    ) -> None:
        if not self.is_node(workspace):
            await self.ssh.upload(
                workspace,
                parent,
                upload,
                overwrite=overwrite,
                max_bytes=max_bytes,
            )
            return

        async def chunks() -> AsyncIterator[bytes]:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    return
                yield chunk

        await self.upload_stream(
            workspace,
            parent,
            upload.filename or "",
            chunks(),
            overwrite=overwrite,
            max_bytes=max_bytes,
        )

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
        if not self.is_node(workspace):
            await self.ssh.upload_stream(
                workspace,
                parent,
                filename,
                chunks,
                overwrite=overwrite,
                max_bytes=max_bytes,
            )
            return
        stream = None
        try:
            connection = self.nodes.connection(str(workspace["computer"]["id"]))
            _, stream = await connection.open_stream(
                "files.upload.open",
                {
                    **self._workspace_payload(workspace),
                    "parent": parent,
                    "filename": filename,
                    "overwrite": overwrite,
                    "max_bytes": max_bytes,
                },
            )
            total = 0
            async for chunk in chunks:
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("Upload exceeds the configured size limit")
                await stream.send(chunk)
            await stream.finish()
        except NodeCoreError as exc:
            if stream is not None:
                await stream.abort()
            self._raise_node_error(exc)
        except BaseException:
            if stream is not None:
                await stream.abort()
            raise

    async def download_stream(
        self,
        workspace: dict[str, Any],
        relative_path: str,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> AsyncIterator[bytes]:
        if not self.is_node(workspace):
            iterator = self.ssh.download_iter(
                workspace, relative_path, offset=offset, length=length
            )
            while True:
                chunk = await asyncio.to_thread(next, iterator, None)
                if chunk is None:
                    return
                yield chunk
            return
        stream = None
        try:
            connection = self.nodes.connection(str(workspace["computer"]["id"]))
            _, stream = await connection.open_stream(
                "files.download.open",
                {
                    **self._workspace_payload(workspace),
                    "path": relative_path,
                    "offset": offset,
                    "length": length,
                },
            )
            async for chunk in stream:
                yield chunk
        except NodeCoreError as exc:
            self._raise_node_error(exc)
        finally:
            if stream is not None:
                await stream.close()

    async def bridge(
        self,
        websocket: WebSocket,
        workspace: dict[str, Any],
        terminal: dict[str, Any],
        *,
        device_id: str,
    ) -> None:
        if not self.is_node(workspace):
            await self.ssh.bridge(websocket, workspace, terminal, device_id=device_id)
            return
        await self.ensure_workspace(workspace)
        self.store.touch_terminal(str(terminal["id"]))
        terminal_id = str(terminal["id"])
        client_id = self.control.register(terminal_id, device_id=device_id)
        resize_lock = self._node_terminal_resize_locks.setdefault(
            terminal_id,
            asyncio.Lock(),
        )
        stream = None
        output_task: asyncio.Task[None] | None = None
        input_task: asyncio.Task[None] | None = None
        try:
            connection = self.nodes.connection(str(workspace["computer"]["id"]))
            _, stream = await connection.open_stream(
                "terminal.attach",
                {
                    **self._workspace_payload(workspace),
                    "tmux_window": terminal["tmux_window"],
                    "rows": 24,
                    "cols": 80,
                },
            )
            grid_active = False
            last_viewport: tuple[int, int] | None = None

            async def output_to_browser() -> None:
                decoder = TerminalOutputDecoder()
                async for chunk in stream:
                    decoded = decoder.feed(chunk)
                    if decoded:
                        await asyncio.to_thread(
                            touch_terminal_output_if_present, self.store, terminal_id
                        )
                        await websocket.send_text(decoded)
                tail = decoder.feed(b"", final=True)
                if tail:
                    await asyncio.to_thread(
                        touch_terminal_output_if_present, self.store, terminal_id
                    )
                    await websocket.send_text(tail)

            async def resize_browser_view(payload: dict[str, Any]) -> None:
                nonlocal grid_active, last_viewport
                if "rows" not in payload or "cols" not in payload:
                    return
                size = terminal_size(payload)
                if size is None:
                    return
                rows, cols = size
                async with resize_lock:
                    controls_grid, grid_resize = self.control.resize_plan(
                        terminal_id, client_id, rows=rows, cols=cols
                    )
                    viewport = (rows, cols)
                    if (
                        controls_grid == grid_active
                        and viewport == last_viewport
                        and not grid_resize
                    ):
                        return
                    await stream.control(
                        "resize",
                        rows=rows,
                        cols=cols,
                        affects_grid=controls_grid,
                    )
                    grid_active = controls_grid
                    last_viewport = viewport
                    if controls_grid and not self.control.can_resize(
                        terminal_id,
                        client_id,
                    ):
                        await stream.control(
                            "resize",
                            rows=rows,
                            cols=cols,
                            affects_grid=False,
                        )
                        grid_active = False

            async def browser_to_input() -> None:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        raise WebSocketDisconnect(message.get("code", 1000))
                    if message.get("bytes") is not None:
                        value = bytes(message["bytes"])
                        if len(value) > MAX_TERMINAL_MESSAGE_BYTES:
                            await websocket.close(code=1009, reason="Terminal input is too large")
                            return
                        self.control.mark_input(terminal_id, client_id, device_id)
                        if last_viewport is not None:
                            await resize_browser_view(
                                {"rows": last_viewport[0], "cols": last_viewport[1]}
                            )
                        await stream.send(value)
                        continue
                    raw = message.get("text") or ""
                    if len(raw.encode("utf-8")) > MAX_TERMINAL_MESSAGE_BYTES:
                        await websocket.close(code=1009, reason="Terminal input is too large")
                        return
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        await stream.send(raw.encode())
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
                    elif kind in {"input", "command"}:
                        if kind == "command" or terminal_input_claims_grid(payload):
                            self.control.mark_input(terminal_id, client_id, device_id)
                        await resize_browser_view(payload)
                        data = str(payload.get("data") or "")
                        if kind == "command":
                            self.store.add_command(str(workspace["id"]), terminal_id, data)
                            data += "\r"
                        await stream.send(data.encode())

            output_task = asyncio.create_task(output_to_browser())
            input_task = asyncio.create_task(browser_to_input())
            done, pending = await asyncio.wait(
                {output_task, input_task}, return_when=asyncio.FIRST_COMPLETED
            )
            await _settle_bridge_tasks(done, pending)
        except NodeCoreError as exc:
            self._raise_node_error(exc)
        finally:
            await _cancel_bridge_tasks(
                task for task in (output_task, input_task) if task is not None
            )
            self.control.unregister(terminal_id, client_id)
            if self.control.client_count(terminal_id) == 0:
                self._node_terminal_resize_locks.pop(terminal_id, None)
            if stream is not None:
                await stream.close()

    def content_type(self, relative_path: str) -> str:
        return FileService().content_type(relative_path)

    async def _node_request(
        self,
        computer: Mapping[str, Any],
        operation: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if computer.get("node_revoked_at") is not None:
            raise RemoteAccessError("Node connection has been revoked", code="node_revoked")
        try:
            return await self.nodes.connection(str(computer["id"])).request(operation, payload)
        except NodeCoreError as exc:
            self._raise_node_error(exc)

    async def _node_file_run_request(
        self,
        workspace: Mapping[str, Any],
        operation: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not self.is_node(workspace):
            raise RemoteAccessError(
                "File Run Node operation requires a Node Remote",
                code="capability_unsupported",
            )
        if not self.supports_capability(workspace, "file_run"):
            raise RemoteAccessError(
                "This Node does not support current-file execution",
                code="capability_unsupported",
            )
        return await self._workspace_request(workspace, operation, payload)

    @staticmethod
    def _raise_node_error(exc: NodeCoreError) -> NoReturn:
        message = str(exc)
        if exc.code == "file_conflict":
            raise FileConflictError(message) from exc
        if exc.code == "file_unsupported":
            raise UnsupportedFileError(message) from exc
        if exc.code == "path_outside":
            raise PathBoundaryError(message) from exc
        if exc.code == "not_found":
            raise FileNotFoundError(message) from exc
        if exc.code == "already_exists":
            raise FileExistsError(message) from exc
        if exc.code == "permission_denied":
            raise PermissionError(message) from exc
        if exc.code == "directory_listing_limit":
            raise DirectoryListingLimitError(message) from exc
        raise RemoteAccessError(message, code=exc.code) from exc

    async def _workspace_request(
        self,
        workspace: Mapping[str, Any],
        operation: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        computer = workspace.get("computer")
        if not isinstance(computer, Mapping):
            raise RemoteAccessError("Remote Workspace computer is missing")
        return await self._node_request(
            computer, operation, {**self._workspace_payload(workspace), **payload}
        )

    @staticmethod
    def _workspace_payload(workspace: Mapping[str, Any]) -> dict[str, str]:
        payload = {
            "workspace_path": str(workspace.get("path") or ""),
            "tmux_session": str(workspace.get("tmux_session") or ""),
        }
        remote_run_id = str(workspace.get("remote_run_id") or "")
        if remote_run_id:
            payload["remote_run_id"] = remote_run_id
        return payload

    def _reconcile_terminals(
        self, workspace: Mapping[str, Any], value: object
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not value:
            raise RemoteAccessError("Node returned an invalid Terminal list")
        records = [self._terminal_record(item) for item in value]
        return self.store.reconcile_terminals(str(workspace["id"]), records)

    @staticmethod
    def _terminal_record(value: object) -> dict[str, str | int | None]:
        if not isinstance(value, dict):
            raise RemoteAccessError("Node returned an invalid Terminal")
        window = str(value.get("tmux_window") or "")
        name = str(value.get("name") or "shell")[:255]
        role = str(value.get("role") or "shell")
        managed = str(value.get("managed_run_id") or "") or None
        raw_activity_at = value.get("activity_at")
        if raw_activity_at is not None and (
            isinstance(raw_activity_at, bool)
            or not isinstance(raw_activity_at, int)
            or raw_activity_at < 0
        ):
            raise RemoteAccessError("Node returned invalid Terminal activity")
        if not window.startswith("@") or not window[1:].isdigit():
            raise RemoteAccessError("Node returned an invalid Terminal identity")
        if role == "shell":
            if managed is not None:
                raise RemoteAccessError("Node returned an invalid Terminal identity")
        elif role in {"file_run", "remote_run"}:
            try:
                parsed = uuid.UUID(str(managed))
            except (AttributeError, ValueError) as exc:
                raise RemoteAccessError(
                    "Node returned an invalid managed Terminal identity"
                ) from exc
            if parsed.version != 4 or str(parsed) != managed:
                raise RemoteAccessError("Node returned an invalid managed Terminal identity")
        else:
            raise RemoteAccessError("Node returned an invalid Terminal role")
        return {
            "tmux_window": window,
            "name": name,
            "role": role,
            "managed_run_id": managed,
            "activity_at": raw_activity_at,
        }

    @staticmethod
    def _relative_path(value: object) -> str:
        path = str(value or "")
        relative = PurePosixPath(path)
        if (
            not path
            or relative.is_absolute()
            or relative.as_posix() != path
            or any(part in {"", ".", ".."} for part in relative.parts)
            or "\\" in path
        ):
            raise RemoteAccessError("Node returned an invalid relative path")
        return path

    @staticmethod
    def _file_digest(value: object) -> str:
        digest = str(value or "")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise RemoteAccessError("Node returned an invalid file digest")
        return digest

    @staticmethod
    def _file_run_observation(value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise RemoteAccessError("Node returned an invalid File Run observation")
        state = str(value.get("state") or "")
        if state not in {
            "preparing",
            "running",
            "finished",
            "stopped",
            "failed",
            "lost",
        }:
            raise RemoteAccessError("Node returned an invalid File Run state")
        result: dict[str, Any] = {"state": state}
        for field in ("started_at", "ended_at"):
            item = value.get(field)
            if item is not None:
                if not isinstance(item, str) or not item or len(item) > 80:
                    raise RemoteAccessError("Node returned an invalid File Run timestamp")
                result[field] = item
        exit_code = value.get("exit_code")
        if exit_code is not None:
            if isinstance(exit_code, bool) or not isinstance(exit_code, int):
                raise RemoteAccessError("Node returned an invalid File Run exit code")
            result["exit_code"] = exit_code
        for field in ("error_code", "error_detail"):
            item = value.get(field)
            if item is not None:
                if not isinstance(item, str) or len(item) > 500:
                    raise RemoteAccessError("Node returned an invalid File Run error")
                result[field] = item
        return result

    @staticmethod
    def _file_entries(value: object) -> list[FileEntry]:
        if not isinstance(value, list) or len(value) > 100_000:
            raise RemoteAccessError("Node returned an invalid file list")
        entries: list[FileEntry] = []
        for raw in value:
            if not isinstance(raw, dict):
                raise RemoteAccessError("Node returned an invalid file entry")
            relative_path = str(raw.get("relative_path") or "")
            if not relative_path or PurePosixPath(relative_path).is_absolute():
                raise RemoteAccessError("Node returned an invalid file path")
            entries.append(
                FileEntry(
                    name=str(raw.get("name") or "")[:255],
                    relative_path=relative_path,
                    is_dir=raw.get("is_dir") is True,
                    size=max(0, int(raw.get("size") or 0)),
                    mtime_ns=max(0, int(raw.get("mtime_ns") or 0)),
                )
            )
        return entries

    @staticmethod
    def _file_snapshot(value: object, *, max_bytes: int) -> FileSnapshot:
        if not isinstance(value, dict) or not isinstance(value.get("content"), str):
            raise RemoteAccessError("Node returned an invalid file snapshot")
        content = value["content"]
        encoded = content.encode("utf-8")
        if len(encoded) > max_bytes:
            raise RemoteAccessError("Node returned an oversized file snapshot")
        relative_path = str(value.get("relative_path") or "")
        if not relative_path or PurePosixPath(relative_path).is_absolute():
            raise RemoteAccessError("Node returned an invalid file path")
        digest = str(value.get("digest") or "")
        if digest != file_digest(encoded):
            raise RemoteAccessError("Node returned an invalid file digest")
        mtime_ns = int(value.get("mtime_ns") or 0)
        if mtime_ns < 0:
            raise RemoteAccessError("Node returned an invalid file timestamp")
        return FileSnapshot(
            path=Path(relative_path),
            relative_path=relative_path,
            content=content,
            digest=digest,
            mtime_ns=mtime_ns,
        )

    @staticmethod
    def _absolute_path(value: object) -> str:
        path = str(value or "")
        normalized = PurePosixPath(path).as_posix()
        if not path.startswith("/") or normalized != path or "\\" in path:
            raise RemoteAccessError("Node returned an invalid absolute path")
        return path
