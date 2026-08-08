from __future__ import annotations

from pathlib import Path

from termroom.db import StateStore
from termroom.workspaces import RootManager, WorkspaceManager


def test_open_workspace_is_stable(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    manager = WorkspaceManager(RootManager(tmp_path), store)

    first = manager.open("project")
    second = manager.open("project")

    assert first["id"] == second["id"]
    assert first["tmux_session"].startswith("termroom-")
    assert first["path"] == project


def test_workspace_manager_can_reopen_local_workspace_from_another_root(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()

    first_manager = WorkspaceManager(RootManager(first_root), store)
    second_manager = WorkspaceManager(RootManager(second_root), store)
    remote_from_first_root = second_manager.open(".")

    reopened = first_manager.require(remote_from_first_root["id"])

    assert reopened["path"] == second_root
    assert reopened["canonical_path"] == str(second_root)
    assert reopened["connection_label"] == ""


def test_workspace_manager_can_open_multiple_local_roots(tmp_path: Path) -> None:
    first_root = tmp_path / "disk-a"
    second_root = tmp_path / "disk-b"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / "alpha").mkdir()
    (second_root / "beta").mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    manager = WorkspaceManager(RootManager(first_root), store)

    first = manager.open_local(first_root, "alpha")
    second = manager.open_local(second_root, "beta")

    assert first["path"] == first_root / "alpha"
    assert second["path"] == second_root / "beta"
    roots = {Path(item["path"]) for item in store.list_local_roots()}
    assert first_root in roots
    assert second_root in roots
