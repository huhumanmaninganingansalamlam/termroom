from __future__ import annotations

from pathlib import Path

import pytest

from termroom.db import StateStore, utc_now


def _store(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    return store


def _computer(store: StateStore) -> dict[str, object]:
    return store.create_computer(
        name="GPU Server",
        ssh_alias="",
        host="gpu.example.test",
        port=22,
        username="runner",
        identity_file="",
        host_key_type="ssh-ed25519",
        host_key_data="AAAA",
        host_fingerprint="SHA256:test",
    )


def _remote_run_values(computer_id: str) -> dict[str, object]:
    return {
        "id": "7d9e6bd5-919b-4ad1-81f2-02d512fd3f00",
        "source_kind": "git",
        "source_workspace_id": None,
        "source_path": None,
        "source_label": "example/project",
        "source_url": "https://example.test/project.git",
        "source_options_json": "{}",
        "source_revision": None,
        "source_size": None,
        "target_computer_id": computer_id,
        "command": "python main.py",
        "run_base": "/srv/termroom-runs",
        "state": "preparing",
        "phase": "cloning",
        "created_at": utc_now(),
    }


def test_remote_run_insert_is_idempotent_and_transitions_use_compare_and_set(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    computer = _computer(store)
    values = _remote_run_values(str(computer["id"]))

    first, created = store.create_remote_run(values)
    duplicate, duplicate_created = store.create_remote_run(values)

    assert created is True
    assert duplicate_created is False
    assert first == duplicate
    assert store.transition_remote_run(
        str(values["id"]),
        expected_states={"preparing"},
        expected_phase="cloning",
        state="running",
        phase=None,
        started_at=utc_now(),
    )
    assert not store.transition_remote_run(
        str(values["id"]),
        expected_states={"preparing"},
        state="failed",
    )
    assert store.get_remote_run(str(values["id"]))["state"] == "running"  # type: ignore[index]


def test_terminal_remote_run_expiry_and_target_registration_are_preserved(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    computer = _computer(store)
    values = _remote_run_values(str(computer["id"]))
    store.create_remote_run(values)
    ended_at = utc_now()
    assert store.transition_remote_run(
        str(values["id"]),
        expected_states={"preparing"},
        state="failed",
        phase=None,
        ended_at=ended_at,
        expires_at=ended_at,
        error_code="prepare_failed",
    )

    assert [row["id"] for row in store.list_expired_remote_runs(ended_at)] == [values["id"]]
    with pytest.raises(RuntimeError, match="Remote Runs"):
        store.remove_computer_registration(str(computer["id"]))

    store.delete_remote_run(str(values["id"]))
    assert store.remove_computer_registration(str(computer["id"])) == []


def test_waiting_zip_upload_expiry_is_persisted_and_queryable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    computer = _computer(store)
    values = _remote_run_values(str(computer["id"]))
    values.update(
        source_kind="zip",
        source_url=None,
        source_label="source.zip",
        source_options_json='{"archive_name":"source.zip"}',
        phase="waiting_upload",
        expires_at="2026-08-09T01:00:00+00:00",
    )

    run, created = store.create_remote_run(values)

    assert created is True
    assert run["expires_at"] == "2026-08-09T01:00:00+00:00"
    assert store.list_abandoned_remote_run_uploads(
        "2026-08-09T00:59:59+00:00"
    ) == []
    assert [
        row["id"]
        for row in store.list_abandoned_remote_run_uploads(
            "2026-08-09T01:00:00+00:00"
        )
    ] == [values["id"]]


def test_computer_run_base_setting_is_optional_and_mutable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    computer = _computer(store)

    assert computer["run_base_dir"] is None
    store.update_computer_run_base(str(computer["id"]), "/scratch/termroom-runs")

    assert store.get_computer(str(computer["id"]))["run_base_dir"] == "/scratch/termroom-runs"  # type: ignore[index]
