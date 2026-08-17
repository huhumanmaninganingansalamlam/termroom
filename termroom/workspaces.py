from __future__ import annotations

import os
import sqlite3
import stat as stat_module
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from termroom.db import StateStore
from termroom.security import resolve_inside


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    name: str
    relative_path: str


class ProjectNameError(ValueError):
    def __init__(self, message: str, *, locale_key: str) -> None:
        super().__init__(message)
        self.locale_key = locale_key
        self.locale_values: dict[str, Any] = {}


class ProjectPathExists(FileExistsError):
    def __init__(self, path: str | Path, *, is_directory: bool) -> None:
        self.path = str(path)
        self.is_directory = is_directory
        super().__init__(self.path)


class ProjectCreatedButWorkspaceFailed(RuntimeError):
    def __init__(self, path: str | Path, cause: BaseException) -> None:
        self.path = str(path)
        self.cause = cause
        super().__init__(str(cause))


def validate_project_name(value: str, *, max_bytes: int = 255) -> str:
    if value != value.strip():
        raise ProjectNameError(
            "Project folder name cannot start or end with whitespace",
            locale_key="project.error.edge_whitespace",
        )
    if not value or value in {".", ".."}:
        raise ProjectNameError(
            "Project folder name is invalid", locale_key="project.error.invalid_name"
        )
    if any(character in value for character in ("/", "\\")):
        raise ProjectNameError(
            "Project folder name must be one folder name",
            locale_key="project.error.single_name",
        )
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ProjectNameError(
            "Project folder name cannot contain control characters",
            locale_key="project.error.invalid_name",
        )
    if len(value.encode("utf-8")) > max_bytes:
        raise ProjectNameError(
            "Project folder name is too long", locale_key="project.error.name_too_long"
        )
    return value


class RootManager:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)

    def resolve(self, relative_path: str = ".", *, must_exist: bool = True) -> Path:
        return resolve_inside(self.root, relative_path, must_exist=must_exist)

    def relative(self, path: Path) -> str:
        value = path.resolve(strict=True).relative_to(self.root)
        return "." if str(value) == "." else value.as_posix()

    def list_directories(self, relative_path: str = ".") -> tuple[Path, list[DirectoryEntry]]:
        directory = self.resolve(relative_path)
        if not directory.is_dir():
            raise NotADirectoryError(directory)
        entries: list[DirectoryEntry] = []
        for child in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
            try:
                if child.is_dir() and not child.is_symlink():
                    entries.append(DirectoryEntry(child.name, self.relative(child)))
            except OSError:
                continue
        return directory, entries


class WorkspaceManager:
    def __init__(
        self,
        root_manager: RootManager,
        store: StateStore,
        *,
        allow_local_workspaces: bool = True,
    ) -> None:
        self.root_manager = root_manager
        self.store = store
        self.allow_local_workspaces = allow_local_workspaces
        self.root_record = store.ensure_root(root_manager.root)

    def open(self, relative_path: str) -> dict[str, Any]:
        return self.open_local(self.root_manager.root, relative_path)

    def open_local(self, root_path: str | Path, relative_path: str = ".") -> dict[str, Any]:
        root_manager = RootManager(Path(root_path))
        root_record = self.store.ensure_root(root_manager.root)
        directory = root_manager.resolve(relative_path)
        if not directory.is_dir():
            raise NotADirectoryError(directory)
        normalized = root_manager.relative(directory)
        existing = self.store.find_workspace(str(root_record["id"]), normalized)
        if existing:
            self.store.touch_workspace(existing["id"])
            return self.require(existing["id"])

        display_name = directory.name or str(directory)
        workspace = self.store.create_workspace(str(root_record["id"]), normalized, display_name)
        return self.require(workspace["id"])

    def create_local_project(
        self, root_path: str | Path, parent_relative: str, name: str
    ) -> tuple[dict[str, Any], Path]:
        root_manager = RootManager(Path(root_path))
        parent = root_manager.resolve(parent_relative)
        if not parent.is_dir():
            raise NotADirectoryError(parent)
        try:
            max_bytes = int(os.pathconf(parent, "PC_NAME_MAX"))
        except (OSError, ValueError):
            max_bytes = 255
        safe_name = validate_project_name(name, max_bytes=max_bytes)
        target = parent / safe_name
        try:
            existing = target.lstat()
        except FileNotFoundError:
            pass
        else:
            raise ProjectPathExists(
                target,
                is_directory=stat_module.S_ISDIR(existing.st_mode),
            )
        target.mkdir(mode=0o755)
        try:
            workspace = self.open_local(root_manager.root, root_manager.relative(target))
        except Exception as exc:
            # The directory may already contain user data by the time a later
            # Workspace registration step fails. Never roll it back here.
            raise ProjectCreatedButWorkspaceFailed(target, exc) from exc
        return workspace, target

    def require(self, workspace_id: str) -> dict[str, Any]:
        workspace = self.store.get_workspace(workspace_id)
        if not workspace:
            raise KeyError(f"Unknown workspace: {workspace_id}")
        computer = None
        if workspace.get("backend_kind", "local") == "remote":
            computer = self.store.get_computer(str(workspace.get("computer_id", "")))
            if computer is None:
                raise KeyError(f"Unknown computer for workspace: {workspace_id}")
        remote_run = self.store.get_remote_run_for_workspace(workspace_id)
        return self._hydrate_workspace(
            workspace,
            computer=computer,
            remote_run=remote_run,
        )

    def _hydrate_workspace(
        self,
        workspace: Mapping[str, Any],
        *,
        computer: Mapping[str, Any] | None,
        remote_run: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        item = dict(workspace)
        workspace_id = str(item["id"])
        if item.get("backend_kind", "local") == "remote":
            if computer is None:
                raise KeyError(f"Unknown computer for workspace: {workspace_id}")
            item["path"] = str(item["canonical_path"] or item["relative_path"])
            item["remote_path"] = item["path"]
            item["computer"] = dict(computer)
            item["connection_label"] = computer["name"]
        else:
            root_value = item.get("root_path")
            root_path = (
                Path(str(root_value)).expanduser().resolve(strict=True)
                if root_value
                else self.root_manager.root
            )
            item["path"] = resolve_inside(root_path, item["relative_path"])
            item["canonical_path"] = str(item["path"])
            item["backend_kind"] = "local"
            item["connection_label"] = ""
        workspace_kind = str(item.get("workspace_kind") or "workspace")
        item["remote_run"] = dict(remote_run) if remote_run is not None else None
        item["remote_run_id"] = str(remote_run["id"]) if remote_run else None
        item["is_remote_run"] = remote_run is not None
        item["is_server_terminal"] = workspace_kind == "server_terminal"
        item["transient"] = remote_run is not None or item["is_server_terminal"]
        if item["is_server_terminal"]:
            item["display_name"] = str(item["computer"]["name"])
        return item

    def list_recent(self) -> list[dict[str, Any]]:
        workspaces = self.store.list_recent_workspaces()
        return self._hydrate_persistent_workspaces(workspaces)

    def list_all(self) -> list[dict[str, Any]]:
        """Return every persistent Workspace as a selectable Source."""

        workspaces = self.store.list_recent_workspaces(limit=None)
        return self._hydrate_persistent_workspaces(workspaces)

    def update_display_name(
        self, workspace: Mapping[str, Any], display_name: str
    ) -> None:
        if workspace.get("transient"):
            raise ValueError("Transient Workspace names are managed by their owner")

        name = display_name.strip()
        if not name:
            path = workspace["path"]
            name = (
                PurePosixPath(str(path)).name
                if workspace.get("backend_kind") == "remote"
                else Path(path).name
            )
        if (
            not name
            or len(name) > 120
            or any(
                unicodedata.category(character) in {"Cc", "Zl", "Zp"}
                for character in name
            )
        ):
            raise ValueError(
                "Workspace display name must be a single line of 1 to 120 characters"
            )
        self.store.update_workspace_display_name(str(workspace["id"]), name)

    def _hydrate_persistent_workspaces(
        self, workspaces: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        has_remote = any(
            workspace.get("backend_kind", "local") == "remote"
            for workspace in workspaces
        )
        computer_by_id = (
            {
                str(computer["id"]): computer
                for computer in self.store.list_computers()
            }
            if has_remote
            else {}
        )
        result: list[dict[str, Any]] = []
        for workspace in workspaces:
            try:
                result.append(
                    self._hydrate_workspace(
                        workspace,
                        computer=computer_by_id.get(
                            str(workspace.get("computer_id") or "")
                        ),
                        remote_run=None,
                    )
                )
            except (KeyError, OSError):
                continue
        return result

    def open_remote(
        self, computer_id: str, canonical_path: str, display_name: str | None = None
    ) -> dict[str, Any]:
        computer = self.store.get_computer(computer_id)
        if not computer:
            raise KeyError(f"Unknown computer: {computer_id}")
        normalized = PurePosixPath(canonical_path).as_posix()
        if not normalized.startswith("/") or normalized in {"/", "."}:
            raise ValueError("Remote Workspace must use an absolute non-root path")
        existing = self.store.find_remote_workspace(computer_id, normalized)
        if existing:
            self.store.touch_workspace(str(existing["id"]))
            return self.require(str(existing["id"]))

        connection_method = str(computer.get("connection_method") or "ssh")
        virtual_root = self.store.ensure_root_value(f"{connection_method}://{computer_id}")
        name = (display_name or PurePosixPath(normalized).name or normalized).strip()
        workspace = self.store.create_workspace(
            str(virtual_root["id"]),
            normalized,
            name[:120],
            backend_kind="remote",
            computer_id=computer_id,
            canonical_path=normalized,
        )
        return self.require(str(workspace["id"]))

    def open_server_terminal(
        self, computer_id: str, canonical_home: str
    ) -> dict[str, Any]:
        """Create or reuse the hidden SSH-home bridge used by Server Terminal."""

        computer = self.store.get_computer(computer_id)
        if not computer:
            raise KeyError(f"Unknown computer: {computer_id}")
        raw_home = str(canonical_home)
        normalized_home = PurePosixPath(raw_home).as_posix()
        if (
            not raw_home.startswith("/")
            or normalized_home != raw_home
            or "\\" in raw_home
            or any(unicodedata.category(character) == "Cc" for character in raw_home)
        ):
            raise ValueError("SSH home directory must be a canonical absolute POSIX path")

        existing = self.store.find_server_terminal_workspace(computer_id)
        if existing:
            if str(existing.get("canonical_path") or "") != normalized_home:
                raise RuntimeError("Stored Server Terminal home directory has changed")
            self.store.touch_workspace(str(existing["id"]))
            return self.require(str(existing["id"]))

        virtual_root = self.store.ensure_root_value(f"ssh://{computer_id}")
        try:
            workspace = self.store.create_workspace(
                str(virtual_root["id"]),
                ".termroom-server-terminal",
                str(computer["name"]),
                tmux_session=f"termroom-server-{computer_id[:12]}",
                backend_kind="remote",
                computer_id=computer_id,
                canonical_path=normalized_home,
                workspace_kind="server_terminal",
            )
        except sqlite3.IntegrityError as exc:
            workspace = self.store.find_server_terminal_workspace(computer_id)
            if workspace is None:
                raise
            if str(workspace.get("canonical_path") or "") != normalized_home:
                raise RuntimeError(
                    "Stored Server Terminal home directory has changed"
                ) from exc
        return self.require(str(workspace["id"]))

    def open_remote_run(
        self,
        run: Mapping[str, Any],
        tmux_session: str,
        remote_work_path: str,
    ) -> dict[str, Any]:
        """Create or reopen the transient Workspace shell for a Remote Run."""

        run_id = str(run.get("id") or "")
        if not run_id:
            raise ValueError("Remote Run id is required")
        stored_run = self.store.get_remote_run(run_id)
        if not stored_run:
            raise KeyError(f"Unknown Remote Run: {run_id}")
        computer_id = str(stored_run.get("target_computer_id") or "")
        computer = self.store.get_computer(computer_id)
        if not computer:
            raise KeyError(f"Unknown computer for Remote Run: {run_id}")

        safe_session = str(tmux_session)
        expected_session = f"termroom-run-{run_id}"
        if safe_session != expected_session:
            raise ValueError("Remote Run Workspace must use the Run's tmux session")

        raw_path = str(remote_work_path)
        if (
            not raw_path.startswith("/")
            or "\\" in raw_path
            or any(unicodedata.category(character) == "Cc" for character in raw_path)
        ):
            raise ValueError("Remote Run work path must be an absolute POSIX path")
        normalized_path = PurePosixPath(raw_path).as_posix()
        if normalized_path != raw_path or any(
            part in {".", ".."} for part in raw_path.split("/")
        ):
            raise ValueError("Remote Run work path must already be canonical")
        expected_path = (
            PurePosixPath(str(stored_run["run_base"])) / run_id / "work"
        ).as_posix()
        if normalized_path != expected_path:
            raise ValueError("Remote Run work path does not match its managed Run root")

        attached_id = stored_run.get("workspace_id")
        if attached_id:
            workspace = self.store.get_workspace(str(attached_id))
            if not workspace:
                raise RuntimeError("Remote Run references a missing Workspace")
            self._assert_remote_run_workspace(
                workspace,
                computer_id=computer_id,
                tmux_session=safe_session,
                remote_work_path=normalized_path,
            )
            self.store.touch_workspace(str(workspace["id"]))
            return self.require(str(workspace["id"]))

        existing = self.store.find_remote_workspace(
            computer_id, normalized_path, workspace_kind="remote_run"
        )
        if existing:
            self._assert_remote_run_workspace(
                existing,
                computer_id=computer_id,
                tmux_session=safe_session,
                remote_work_path=normalized_path,
            )
            self.store.attach_remote_run_workspace(run_id, str(existing["id"]))
            return self.require(str(existing["id"]))

        # Recover bridge rows created by an older Core between the Workspace
        # insert and Remote Run attachment. A real user Workspace cannot pass
        # the owned tmux-session assertion below.
        legacy = self.store.find_remote_workspace(computer_id, normalized_path)
        if legacy:
            self._assert_remote_run_workspace(
                legacy,
                computer_id=computer_id,
                tmux_session=safe_session,
                remote_work_path=normalized_path,
            )
            self.store.update_workspace_kind(str(legacy["id"]), "remote_run")
            self.store.attach_remote_run_workspace(run_id, str(legacy["id"]))
            return self.require(str(legacy["id"]))

        virtual_root = self.store.ensure_root_value(f"ssh://{computer_id}")
        display_name = str(stored_run.get("source_label") or "Remote Run").strip()
        try:
            workspace = self.store.create_workspace(
                str(virtual_root["id"]),
                normalized_path,
                display_name[:120] or "Remote Run",
                tmux_session=safe_session,
                backend_kind="remote",
                computer_id=computer_id,
                canonical_path=normalized_path,
                workspace_kind="remote_run",
            )
        except sqlite3.IntegrityError:
            # Status requests can discover the same remote tmux session at the
            # same time. Reuse the row that won the insert race instead of
            # returning a 500 for an otherwise valid transient Workspace.
            workspace = self.store.find_remote_workspace(
                computer_id, normalized_path, workspace_kind="remote_run"
            )
            if workspace is None:
                raise
            self._assert_remote_run_workspace(
                workspace,
                computer_id=computer_id,
                tmux_session=safe_session,
                remote_work_path=normalized_path,
            )
        try:
            self.store.attach_remote_run_workspace(run_id, str(workspace["id"]))
        except Exception:
            self.store.delete_workspace(str(workspace["id"]))
            raise
        return self.require(str(workspace["id"]))

    def create_remote_run_workspace(
        self,
        run: Mapping[str, Any],
        tmux_session: str,
        remote_work_path: str,
    ) -> dict[str, Any]:
        return self.open_remote_run(run, tmux_session, remote_work_path)

    @staticmethod
    def _assert_remote_run_workspace(
        workspace: Mapping[str, Any],
        *,
        computer_id: str,
        tmux_session: str,
        remote_work_path: str,
    ) -> None:
        if (
            workspace.get("backend_kind") != "remote"
            or str(workspace.get("computer_id") or "") != computer_id
            or str(workspace.get("tmux_session") or "") != tmux_session
            or str(workspace.get("canonical_path") or "") != remote_work_path
        ):
            raise RuntimeError("Remote Run Workspace metadata does not match the Run")
