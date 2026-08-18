from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
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


def test_source_workspace_delete_is_blocked_while_remote_run_is_linked(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    root_row = store.ensure_root(root)
    workspace = store.create_workspace(
        str(root_row["id"]), ".", "Source Workspace"
    )
    computer = _computer(store)
    values = _remote_run_values(str(computer["id"]))
    values.update(
        source_kind="workspace",
        source_workspace_id=str(workspace["id"]),
        source_path=".",
        source_url=None,
        source_label="Source Workspace",
    )
    store.create_remote_run(values)

    with pytest.raises(RuntimeError, match="Remote Runs"):
        store.delete_workspace(str(workspace["id"]))

    assert store.get_workspace(str(workspace["id"])) is not None
    assert store.get_remote_run(str(values["id"]))["source_workspace_id"] == workspace["id"]  # type: ignore[index]

    store.delete_remote_run(str(values["id"]))
    store.delete_workspace(str(workspace["id"]))
    assert store.get_workspace(str(workspace["id"])) is None


def test_waiting_zip_upload_expiry_is_persisted_and_queryable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    computer = _computer(store)
    values = _remote_run_values(str(computer["id"]))
    values.update(
        source_kind="archive",
        archive_format="zip",
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


@pytest.mark.parametrize(
    ("state", "exit_code", "kind"),
    [
        ("finished", 0, "remote_run.completed"),
        ("finished", 7, "remote_run.failed"),
        ("failed", None, "remote_run.failed"),
        ("stopped", None, "remote_run.stopped"),
        ("lost", None, "remote_run.attention"),
    ],
)
def test_terminal_transition_atomically_creates_one_safe_event(
    tmp_path: Path,
    state: str,
    exit_code: int | None,
    kind: str,
) -> None:
    store = _store(tmp_path)
    computer = _computer(store)
    values = _remote_run_values(str(computer["id"]))
    values["command"] = "TOKEN=do-not-copy python /private/project/main.py"
    store.create_remote_run(values)

    with ThreadPoolExecutor(max_workers=8) as executor:
        transitions = list(
            executor.map(
                lambda _index: store.transition_remote_run(
                    str(values["id"]),
                    expected_states={"preparing"},
                    state=state,
                    phase=None,
                    ended_at=utc_now(),
                    exit_code=exit_code,
                    error_detail="private output must not be copied",
                ),
                range(8),
            )
        )

    assert transitions.count(True) == 1
    events = store.list_activity_events()
    assert len(events) == 1
    event = events[0]
    assert event["kind"] == kind
    assert event["subject_id"] == values["id"]
    assert event["primary_label"] == values["source_label"]
    assert event["secondary_label"] == computer["name"]
    assert event["exit_code"] == exit_code
    serialized = repr(event)
    assert "TOKEN=" not in serialized
    assert "/private/project" not in serialized
    assert "private output" not in serialized


def test_event_revision_preserves_a_later_retry_outcome(tmp_path: Path) -> None:
    store = _store(tmp_path)
    computer = _computer(store)
    values = _remote_run_values(str(computer["id"]))
    store.create_remote_run(values)

    assert store.transition_remote_run(
        str(values["id"]),
        expected_states={"preparing"},
        state="failed",
        phase=None,
        ended_at=utc_now(),
        error_code="prepare_failed",
    )
    assert store.transition_remote_run(
        str(values["id"]),
        expected_states={"failed"},
        state="preparing",
        phase="checking",
        ended_at=None,
        error_code=None,
    )
    assert store.transition_remote_run(
        str(values["id"]),
        expected_states={"preparing"},
        state="finished",
        phase=None,
        ended_at=utc_now(),
        exit_code=0,
    )

    events = list(reversed(store.list_activity_events()))
    assert [event["kind"] for event in events] == [
        "remote_run.failed",
        "remote_run.completed",
    ]
    assert events[0]["subject_revision"] < events[1]["subject_revision"]


def test_event_insert_failure_rolls_back_the_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    computer = _computer(store)
    values = _remote_run_values(str(computer["id"]))
    store.create_remote_run(values)

    def fail_event(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("event write failed")

    monkeypatch.setattr(store, "_insert_remote_run_event", fail_event)

    with pytest.raises(sqlite3.OperationalError, match="event write failed"):
        store.transition_remote_run(
            str(values["id"]),
            expected_states={"preparing"},
            state="finished",
            phase=None,
            ended_at=utc_now(),
            exit_code=0,
        )

    run = store.get_remote_run(str(values["id"]))
    assert run is not None
    assert run["state"] == "preparing"
    assert store.list_activity_events() == []


def test_deleted_subject_keeps_activity_and_device_claim_is_exactly_once(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    computer = _computer(store)
    first = _remote_run_values(str(computer["id"]))
    store.create_remote_run(first)
    assert store.transition_remote_run(
        str(first["id"]),
        expected_states={"preparing"},
        state="finished",
        phase=None,
        ended_at=utc_now(),
        exit_code=0,
    )

    device_id = "b28003fe-56b0-4e52-a41c-591a1259ad02"
    assert store.claim_event_notifications(device_id) == []

    second = {**first, "id": "fcd204bb-ae46-4f1a-b620-abfa09200c03"}
    store.create_remote_run(second)
    assert store.transition_remote_run(
        str(second["id"]),
        expected_states={"preparing"},
        state="finished",
        phase=None,
        ended_at=utc_now(),
        exit_code=7,
    )
    claimed = store.claim_event_notifications(device_id)
    assert [event["subject_id"] for event in claimed] == [second["id"]]
    assert store.claim_event_notifications(device_id) == []

    event_id = str(claimed[0]["id"])
    store.delete_remote_run(str(second["id"]))
    retained = store.get_activity_event(event_id)
    assert retained is not None
    assert retained["subject_exists"] == 0
    assert retained["primary_label"] == second["source_label"]
    assert store.count_unread_events() == 2
    assert store.mark_event_read(event_id)["read_at"] is not None  # type: ignore[index]
    assert store.mark_all_events_read() == 1
    assert store.count_unread_events() == 0


def test_initialize_migrates_existing_terminal_run_without_new_notification(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    store = StateStore(database)
    store.initialize()
    computer = _computer(store)
    values = _remote_run_values(str(computer["id"]))
    store.create_remote_run(values)
    assert store.transition_remote_run(
        str(values["id"]),
        expected_states={"preparing"},
        state="finished",
        phase=None,
        ended_at=utc_now(),
        exit_code=0,
    )
    with store.connect() as db:
        db.execute("DROP TABLE event_notification_claims")
        db.execute("DROP TABLE notification_devices")
        db.execute("DROP TABLE events")
        db.execute("ALTER TABLE remote_runs DROP COLUMN lifecycle_revision")

    migrated = StateStore(database)
    migrated.initialize()

    run = migrated.get_remote_run(str(values["id"]))
    assert run is not None
    assert run["id"] == values["id"]
    assert run["state"] == "finished"
    assert run["lifecycle_revision"] == 0
    events = migrated.list_activity_events()
    assert len(events) == 1
    assert events[0]["kind"] == "remote_run.completed"
    assert events[0]["read_at"] is not None
    assert events[0]["notify"] == 0
