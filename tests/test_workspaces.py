from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from termroom.db import StateStore, workspace_tmux_session_name
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
    assert first["tmux_session"] == f"tr-project-{first['id'][:4]}"
    assert first["path"] == project


@pytest.mark.parametrize(
    ("display_name", "expected"),
    [
        ("My API", "tr-my-api-a1b2"),
        ("한글 프로젝트", "tr-한글-프로젝트-a1b2"),
        ("project.with:unsafe / separators", "tr-project-with-uns-a1b2"),
        ("💻", "tr-workspace-a1b2"),
    ],
)
def test_workspace_tmux_session_name_is_short_and_readable(
    display_name: str,
    expected: str,
) -> None:
    assert workspace_tmux_session_name(display_name, "a1b2" + "0" * 28) == expected


def test_workspace_tmux_session_retries_a_four_character_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "one" / "project").mkdir(parents=True)
    (tmp_path / "two" / "project").mkdir(parents=True)
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    manager = WorkspaceManager(RootManager(tmp_path / "one"), store)
    store.ensure_root(tmp_path / "two")
    ids = iter(
        (
            uuid.UUID("aaaa0000-0000-0000-0000-000000000001"),
            uuid.UUID("aaaa0000-0000-0000-0000-000000000002"),
            uuid.UUID("bbbb0000-0000-0000-0000-000000000003"),
        )
    )
    monkeypatch.setattr("termroom.db.uuid.uuid4", lambda: next(ids))

    first = manager.open_local(tmp_path / "one", "project")
    second = manager.open_local(tmp_path / "two", "project")

    assert first["tmux_session"] == "tr-project-aaaa"
    assert second["tmux_session"] == "tr-project-bbbb"


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


def test_workspace_location_counts_excludes_internal_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_root = tmp_path / "local"
    local_root.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    local = WorkspaceManager(RootManager(local_root), store)
    local_workspace = local.open(".")
    computer = store.create_computer(
        name="Build server",
        ssh_alias="",
        host="build.example",
        port=22,
        username="dev",
        identity_file="/tmp/key",
        auth_kind="key",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    first_remote = local.open_remote(str(computer["id"]), "/srv/one", "one")
    second_remote = local.open_remote(str(computer["id"]), "/srv/two", "two")
    virtual_root = store.ensure_root_value(f"ssh://{computer['id']}")
    store.create_workspace(
        str(virtual_root["id"]),
        ".termroom-server-terminal",
        "Build server",
        backend_kind="remote",
        computer_id=str(computer["id"]),
        canonical_path="/home/dev",
        workspace_kind="server_terminal",
    )

    root_counts, computer_counts = store.workspace_location_counts()

    assert root_counts == {str(local_workspace["root_id"]): 1}
    assert computer_counts == {str(computer["id"]): 2}

    monkeypatch.setattr(
        local,
        "require",
        lambda *_args: pytest.fail("Persistent Workspace lists must batch hydration"),
    )
    listed = {str(item["id"]): item for item in local.list_all()}
    assert set(listed) == {
        str(first_remote["id"]),
        str(second_remote["id"]),
        str(local_workspace["id"]),
    }
    assert listed[str(first_remote["id"])]["computer"]["id"] == computer["id"]
    assert all(item["remote_run"] is None for item in listed.values())


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
