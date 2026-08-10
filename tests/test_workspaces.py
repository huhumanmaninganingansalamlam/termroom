from __future__ import annotations

from pathlib import Path

import pytest

from termroom.db import StateStore
from termroom.security import PathBoundaryError
from termroom.workspaces import (
    ProjectCreatedButWorkspaceFailed,
    ProjectNameError,
    ProjectPathExists,
    RootManager,
    WorkspaceManager,
    validate_project_name,
)


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


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("project", "project"),
        ("project-01_test.py", "project-01_test.py"),
        ("한글 프로젝트", "한글 프로젝트"),
        ("hello world", "hello world"),
    ],
)
def test_project_name_accepts_one_normal_unicode_folder(name: str, expected: str) -> None:
    assert validate_project_name(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        " parent",
        "child ",
        "parent/child",
        "parent\\child",
        "bad\x00name",
        "bad\nname",
        "bad\x7fname",
        "bad\x85name",
    ],
)
def test_project_name_rejects_unsafe_or_ambiguous_names(name: str) -> None:
    with pytest.raises(ProjectNameError):
        validate_project_name(name)


def test_project_name_enforces_filesystem_limit_in_utf8_bytes() -> None:
    assert validate_project_name("가" * 85, max_bytes=255) == "가" * 85
    with pytest.raises(ProjectNameError):
        validate_project_name("가" * 86, max_bytes=255)


def test_create_local_project_registers_workspace_and_distinguishes_conflicts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    parent = root / "workspace"
    parent.mkdir(parents=True)
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    manager = WorkspaceManager(RootManager(root), store)

    workspace, created = manager.create_local_project(root, "workspace", "한글 project")

    assert created == parent / "한글 project"
    assert created.is_dir()
    assert workspace["path"] == created
    assert workspace["display_name"] == "한글 project"

    with pytest.raises(ProjectPathExists) as folder_conflict:
        manager.create_local_project(root, "workspace", "한글 project")
    assert folder_conflict.value.is_directory is True

    (parent / "taken.txt").write_text("file", encoding="utf-8")
    with pytest.raises(ProjectPathExists) as file_conflict:
        manager.create_local_project(root, "workspace", "taken.txt")
    assert file_conflict.value.is_directory is False

    (parent / "target").mkdir()
    (parent / "linked").symlink_to(parent / "target", target_is_directory=True)
    with pytest.raises(ProjectPathExists) as symlink_conflict:
        manager.create_local_project(root, "workspace", "linked")
    assert symlink_conflict.value.is_directory is False


def test_create_local_project_cannot_escape_selected_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    manager = WorkspaceManager(RootManager(root), store)

    with pytest.raises(PathBoundaryError):
        manager.create_local_project(root, "../", "escape")
    assert not (tmp_path / "escape").exists()


def test_create_local_project_keeps_folder_if_workspace_registration_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    manager = WorkspaceManager(RootManager(root), store)

    def fail_open(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(manager, "open_local", fail_open)

    with pytest.raises(ProjectCreatedButWorkspaceFailed) as failed:
        manager.create_local_project(root, ".", "keep-me")

    assert failed.value.path == str(root / "keep-me")
    assert isinstance(failed.value.cause, RuntimeError)
    assert (root / "keep-me").is_dir()
