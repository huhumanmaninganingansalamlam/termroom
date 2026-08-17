from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from termroom.db import StateStore, utc_now
from termroom.workspaces import RootManager, WorkspaceManager

RUN_ID = "55a01c55-7137-4eee-8616-9ea7817b214f"
SECOND_RUN_ID = "a8b4246c-7dd9-4c85-bfdc-ec38ae36be4d"


def _store(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    return store


def _computer(store: StateStore, *, name: str = "GPU Server") -> dict[str, object]:
    return store.create_computer(
        name=name,
        ssh_alias="",
        host=f"{name.casefold().replace(' ', '-')}.example.test",
        port=22,
        username="runner",
        identity_file="",
        host_key_type="ssh-ed25519",
        host_key_data="AAAA",
        host_fingerprint=f"SHA256:{name}",
    )


def _run_values(
    computer_id: str,
    *,
    run_id: str = RUN_ID,
    source_workspace_id: str | None = None,
) -> dict[str, object]:
    return {
        "id": run_id,
        "source_kind": "workspace" if source_workspace_id else "git",
        "source_workspace_id": source_workspace_id,
        "source_path": "." if source_workspace_id else None,
        "source_label": "training/models" if source_workspace_id else "example/project",
        "source_url": None if source_workspace_id else "https://example.test/project.git",
        "source_options_json": "{}",
        "source_revision": None,
        "source_size": 128,
        "target_computer_id": computer_id,
        "command": "python main.py",
        "run_base": "/scratch/termroom-runs",
        "workspace_id": None,
        "state": "running",
        "phase": None,
        "created_at": utc_now(),
    }


def _manager(tmp_path: Path, store: StateStore) -> WorkspaceManager:
    local_root = tmp_path / "local"
    local_root.mkdir(exist_ok=True)
    return WorkspaceManager(RootManager(local_root), store)


def _work_path(run_id: str = RUN_ID) -> str:
    return f"/scratch/termroom-runs/{run_id}/work"


def test_remote_run_workspace_is_idempotent_linked_and_hidden_from_workspace_lists(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    computer = _computer(store)
    manager = _manager(tmp_path, store)
    persistent = manager.open_remote(str(computer["id"]), "/srv/persistent", "persistent")
    values = _run_values(str(computer["id"]), source_workspace_id=str(persistent["id"]))
    run, _created = store.create_remote_run(values)

    workspace = manager.open_remote_run(
        run,
        f"termroom-run-{RUN_ID}",
        _work_path(),
    )
    reopened = manager.create_remote_run_workspace(
        store.get_remote_run(RUN_ID),  # type: ignore[arg-type]
        f"termroom-run-{RUN_ID}",
        _work_path(),
    )

    assert reopened["id"] == workspace["id"]
    assert workspace["backend_kind"] == "remote"
    assert workspace["computer_id"] == computer["id"]
    assert workspace["tmux_session"] == f"termroom-run-{RUN_ID}"
    assert workspace["canonical_path"] == _work_path()
    assert workspace["remote_path"] == _work_path()
    assert workspace["remote_run_id"] == RUN_ID
    assert workspace["remote_run"]["id"] == RUN_ID
    assert workspace["is_remote_run"] is True
    assert workspace["transient"] is True
    assert store.get_remote_run(RUN_ID)["workspace_id"] == workspace["id"]  # type: ignore[index]
    assert store.get_remote_run_for_workspace(str(workspace["id"]))["id"] == RUN_ID  # type: ignore[index]
    assert store.get_workspace_for_remote_run(RUN_ID)["id"] == workspace["id"]  # type: ignore[index]

    # Being a Source Workspace does not hide the persistent Workspace. Only
    # the attached execution shell is transient.
    assert [item["id"] for item in store.list_recent_workspaces()] == [persistent["id"]]
    assert [item["id"] for item in store.list_workspaces_for_computer(str(computer["id"]))] == [
        persistent["id"]
    ]
    assert [item["id"] for item in manager.list_recent()] == [persistent["id"]]
    assert [item["id"] for item in manager.list_all()] == [persistent["id"]]
    assert persistent["remote_run"] is None
    assert persistent["is_remote_run"] is False


def test_remote_run_workspace_can_recover_create_then_attach_crash_window(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    computer = _computer(store)
    manager = _manager(tmp_path, store)
    run, _created = store.create_remote_run(_run_values(str(computer["id"])))
    virtual_root = store.ensure_root_value(f"ssh://{computer['id']}")
    orphan = store.create_workspace(
        str(virtual_root["id"]),
        _work_path(),
        "example/project",
        tmux_session=f"termroom-run-{RUN_ID}",
        backend_kind="remote",
        computer_id=str(computer["id"]),
        canonical_path=_work_path(),
    )

    recovered = manager.open_remote_run(run, f"termroom-run-{RUN_ID}", _work_path())

    assert recovered["id"] == orphan["id"]
    assert recovered["remote_run_id"] == RUN_ID
    assert store.get_remote_run(RUN_ID)["workspace_id"] == orphan["id"]  # type: ignore[index]


def test_remote_run_workspace_recovers_concurrent_create_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    computer = _computer(store)
    manager = _manager(tmp_path, store)
    run, _created = store.create_remote_run(_run_values(str(computer["id"])))
    original_create = store.create_workspace

    def competing_create(*args: object, **kwargs: object) -> dict[str, object]:
        original_create(*args, **kwargs)
        raise sqlite3.IntegrityError("simulated concurrent Workspace insert")

    monkeypatch.setattr(store, "create_workspace", competing_create)

    workspace = manager.open_remote_run(
        run,
        f"termroom-run-{RUN_ID}",
        _work_path(),
    )

    assert workspace["remote_run_id"] == RUN_ID
    assert store.get_remote_run(RUN_ID)["workspace_id"] == workspace["id"]  # type: ignore[index]


def test_remote_run_workspace_rejects_wrong_target_path_session_and_existing_workspace(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    computer = _computer(store)
    manager = _manager(tmp_path, store)
    run, _created = store.create_remote_run(_run_values(str(computer["id"])))

    with pytest.raises(ValueError, match="tmux"):
        manager.open_remote_run(run, "termroom-wrong", _work_path())
    with pytest.raises(ValueError, match="canonical"):
        manager.open_remote_run(
            run,
            f"termroom-run-{RUN_ID}",
            f"/scratch/termroom-runs/{RUN_ID}/../outside",
        )

    conflicting = manager.open_remote(str(computer["id"]), _work_path(), "opened manually")
    with pytest.raises(RuntimeError, match="metadata does not match"):
        manager.open_remote_run(run, f"termroom-run-{RUN_ID}", _work_path())
    assert store.get_remote_run(RUN_ID)["workspace_id"] is None  # type: ignore[index]
    assert store.get_workspace(str(conflicting["id"])) is not None


def test_store_attach_enforces_one_target_run_and_workspace_relationship(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    target = _computer(store)
    other_target = _computer(store, name="Other Server")
    manager = _manager(tmp_path, store)
    first, _created = store.create_remote_run(_run_values(str(target["id"])))
    second, _created = store.create_remote_run(
        _run_values(str(target["id"]), run_id=SECOND_RUN_ID)
    )
    wrong_workspace = manager.open_remote(str(other_target["id"]), "/srv/project")

    with pytest.raises(ValueError, match="target Remote"):
        store.attach_remote_run_workspace(RUN_ID, str(wrong_workspace["id"]))

    attached = manager.open_remote_run(first, f"termroom-run-{RUN_ID}", _work_path())
    with pytest.raises(RuntimeError, match="another Remote Run"):
        store.attach_remote_run_workspace(SECOND_RUN_ID, str(attached["id"]))
    assert second["workspace_id"] is None


def test_remote_run_workspace_detach_and_delete_helpers_preserve_order(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    computer = _computer(store)
    manager = _manager(tmp_path, store)
    run, _created = store.create_remote_run(_run_values(str(computer["id"])))
    workspace = manager.open_remote_run(run, f"termroom-run-{RUN_ID}", _work_path())

    with pytest.raises(RuntimeError, match="Workspace"):
        store.delete_remote_run(RUN_ID)
    assert store.detach_remote_run_workspace(RUN_ID, workspace_id="wrong") is None
    assert store.get_remote_run(RUN_ID)["workspace_id"] == workspace["id"]  # type: ignore[index]

    assert store.delete_remote_run_workspace(RUN_ID) is True
    assert store.get_workspace(str(workspace["id"])) is None
    assert store.get_remote_run(RUN_ID)["workspace_id"] is None  # type: ignore[index]
    assert store.delete_remote_run_workspace(RUN_ID) is False
    store.delete_remote_run(RUN_ID)
    assert store.get_remote_run(RUN_ID) is None


def test_deleting_transient_workspace_uses_foreign_key_set_null(tmp_path: Path) -> None:
    store = _store(tmp_path)
    computer = _computer(store)
    manager = _manager(tmp_path, store)
    run, _created = store.create_remote_run(_run_values(str(computer["id"])))
    workspace = manager.open_remote_run(run, f"termroom-run-{RUN_ID}", _work_path())

    store.delete_workspace(str(workspace["id"]))

    assert store.get_remote_run(RUN_ID)["workspace_id"] is None  # type: ignore[index]
    assert store.get_remote_run_for_workspace(str(workspace["id"])) is None


def test_initialize_migrates_workspace_link_and_preserves_unreleased_quick_run_metadata(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript(
            """
            CREATE TABLE remote_runs (
                id TEXT PRIMARY KEY,
                source_kind TEXT NOT NULL,
                source_workspace_id TEXT,
                source_path TEXT,
                source_label TEXT NOT NULL,
                source_url TEXT,
                source_options_json TEXT NOT NULL DEFAULT '{}',
                source_revision TEXT,
                source_size INTEGER,
                target_computer_id TEXT NOT NULL,
                command TEXT NOT NULL,
                run_base TEXT NOT NULL,
                state TEXT NOT NULL,
                phase TEXT,
                exit_code INTEGER,
                error_code TEXT,
                error_detail TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                stop_requested_at TEXT,
                ended_at TEXT,
                expires_at TEXT
            );
            CREATE TABLE runs (
                id TEXT PRIMARY KEY,
                command TEXT NOT NULL
            );
            INSERT INTO runs(id, command) VALUES ('obsolete', 'echo obsolete');
            """
        )

    store = StateStore(database)
    store.initialize()

    with store.connect() as db:
        remote_run_columns = {
            str(row["name"]) for row in db.execute("PRAGMA table_info(remote_runs)")
        }
        foreign_keys = list(db.execute("PRAGMA foreign_key_list(remote_runs)"))
        tables = {
            str(row["name"])
            for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        indexes = {
            str(row["name"]) for row in db.execute("PRAGMA index_list(remote_runs)")
        }

    assert "workspace_id" in remote_run_columns
    assert any(
        row["from"] == "workspace_id"
        and row["table"] == "workspaces"
        and row["on_delete"] == "SET NULL"
        for row in foreign_keys
    )
    assert "idx_remote_runs_workspace" in indexes
    assert "runs" not in tables
    assert "legacy_runs" in tables
    with store.connect() as db:
        legacy = db.execute("SELECT id, command FROM legacy_runs").fetchone()
    assert dict(legacy) == {"id": "obsolete", "command": "echo obsolete"}
    for legacy_method in (
        "create_run",
        "get_run",
        "list_recent_runs",
        "list_expired_runs",
        "mark_run_started",
        "finish_run",
        "delete_run",
    ):
        assert not hasattr(store, legacy_method)
