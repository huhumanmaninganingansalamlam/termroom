from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from termroom.db import StateStore
from termroom.security import resolve_inside


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    name: str
    relative_path: str


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
    def __init__(self, root_manager: RootManager, store: StateStore) -> None:
        self.root_manager = root_manager
        self.store = store
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

    def require(self, workspace_id: str) -> dict[str, Any]:
        workspace = self.store.get_workspace(workspace_id)
        if not workspace:
            raise KeyError(f"Unknown workspace: {workspace_id}")
        if workspace.get("backend_kind", "local") == "ssh":
            computer = self.store.get_computer(str(workspace.get("computer_id", "")))
            if not computer:
                raise KeyError(f"Unknown computer for workspace: {workspace_id}")
            workspace["path"] = str(workspace["canonical_path"] or workspace["relative_path"])
            workspace["remote_path"] = workspace["path"]
            workspace["computer"] = computer
            workspace["connection_label"] = computer["name"]
        else:
            root_value = workspace.get("root_path")
            root_path = (
                Path(str(root_value)).expanduser().resolve(strict=True)
                if root_value
                else self.root_manager.root
            )
            workspace["path"] = resolve_inside(root_path, workspace["relative_path"])
            workspace["canonical_path"] = str(workspace["path"])
            workspace["backend_kind"] = "local"
            workspace["connection_label"] = ""
        return workspace

    def list_recent(self) -> list[dict[str, Any]]:
        workspaces = self.store.list_recent_workspaces()
        result: list[dict[str, Any]] = []
        for workspace in workspaces:
            try:
                result.append(self.require(str(workspace["id"])))
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

        virtual_root = self.store.ensure_root_value(f"ssh://{computer_id}")
        name = (display_name or PurePosixPath(normalized).name or normalized).strip()
        workspace = self.store.create_workspace(
            str(virtual_root["id"]),
            normalized,
            name[:120],
            backend_kind="ssh",
            computer_id=computer_id,
            canonical_path=normalized,
        )
        return self.require(str(workspace["id"]))
