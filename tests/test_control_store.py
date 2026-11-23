"""Existing v0.4.23 credential/receipt SQLite data opens without migration loss."""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from termroom.control_store import CONTROL_SCHEMA_SQL, ControlStore
from termroom.db import StateStore

ROOT = Path(__file__).resolve().parents[1]


def _control_schema(db: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [
        (str(row[0]), str(row[1]), str(row[2]))
        for row in db.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name IN ('client_credentials', 'client_action_receipts', "
            "'idx_client_actions_time') ORDER BY name"
        )
    ]


def test_v0423_client_schema_and_records_survive_state_initialization(
    tmp_path: Path,
) -> None:
    file = tmp_path / "existing-v0423.sqlite3"
    old_schema = (ROOT / "tests/control_schema_v0423.sql").read_text()
    assert CONTROL_SCHEMA_SQL.strip() == old_schema.strip()
    assert not any(
        hasattr(StateStore, name)
        for name in (
            "_client_credential_row",
            "create_client_credential", "get_client_credential",
            "list_client_credentials", "touch_client_credential",
            "revoke_client_credential", "claim_client_action",
            "get_client_action", "update_client_action",
        )
    ), "Credential/action SQL must not reappear in StateStore"
    created = datetime.now(UTC).isoformat(timespec="seconds")
    future = (datetime.now(UTC) + timedelta(days=20)).isoformat(timespec="seconds")
    past = (datetime.now(UTC) - timedelta(minutes=2)).isoformat(timespec="seconds")
    verifier = hashlib.sha256(b"unusable-test-token").hexdigest()
    with sqlite3.connect(file) as legacy:
        legacy.executescript(old_schema)
        legacy.execute(
            "INSERT INTO client_credentials "
            "(id,name,token_hash,capabilities_json,workspace_ids_json,"
            "created_at,last_used_at,revoked_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "legacy-client", "기존 클라이언트", verifier,
                '["read","operate"]', '["workspace-1"]',
                created, None, None,
            ),
        )
        for action_id, status, phase in (
            ("legacy-accepted", "accepted", "pty_delivered"),
            ("legacy-observed", "observed", "process_finished"),
        ):
            legacy.execute(
                "INSERT INTO client_action_receipts "
                "(client_id,action_id,action,resource_kind,resource_id,"
                "payload_sha256,status,phase,instance_id,created_at,updated_at,expires_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-client", action_id, "terminal.input", "terminal",
                    "terminal-1", hashlib.sha256(action_id.encode()).hexdigest(),
                    status, phase, "legacy-instance", past, past, future,
                ),
            )
        legacy.commit()
        original_schema = _control_schema(legacy)
        assert legacy.execute(
            "SELECT id FROM client_credentials WHERE id = 'legacy-client'"
        ).fetchone() == ("legacy-client",)

    state = StateStore(file)
    state.initialize()  # exercise real current on-disk schema upgrade
    control = ControlStore(state)
    with state.connect() as db:
        assert _control_schema(db) == original_schema
    credential = control.get_credential("legacy-client")
    assert credential is not None
    assert credential["name"] == "기존 클라이언트"
    assert credential["token_hash"] == verifier
    assert credential["capabilities"] == ["read", "operate"]
    assert credential["workspace_ids"] == ["workspace-1"]
    accepted = control.get_action(
        "legacy-client", "legacy-accepted", instance_id="legacy-instance"
    )
    assert accepted is not None
    assert (accepted["status"], accepted["phase"]) == ("accepted", "pty_delivered")
    completed = control.get_action(
        "legacy-client", "legacy-observed", instance_id="new-core"
    )
    assert completed is not None
    assert (completed["status"], completed["phase"]) == (
        "observed", "process_finished",
    )
    # Only the previously accepted, unfinished operation becomes unknown
    # when a new Core instance sees it, never an inferred completion.
    unknown = control.get_action(
        "legacy-client", "legacy-accepted", instance_id="new-core"
    )
    assert unknown is not None
    assert (unknown["status"], unknown["phase"]) == ("unknown", "pty_delivered")
    with pytest.raises(RuntimeError, match="no longer owned"):
        control.advance_action(
            client_id="legacy-client", action_id="legacy-accepted",
            instance_id="new-core", status="observed", phase="process_finished",
        )
    revoked = control.revoke_credential("legacy-client")
    assert revoked is not None
    assert revoked["revoked_at"]
    assert control.get_credential("legacy-client") == revoked
    assert "token" not in credential
    assert control.get_action(
        "legacy-client", "legacy-observed", instance_id="new-core"
    )["status"] == "observed"


def test_control_store_uses_existing_sqlite_transaction_and_foreign_keys(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite3")
    state.initialize()
    control = ControlStore(state)
    with pytest.raises(sqlite3.IntegrityError):
        control.claim_action(
            client_id="nonexistent-client", action_id="action-000001",
            action="terminal.input", resource_kind="terminal",
            resource_id="terminal-1",
            payload_sha256=hashlib.sha256(b"a").hexdigest(),
            instance_id="instance-1",
        )
    # A failed insert does not leave a phantom action; foreign keys and
    # commit semantics remain owned by the established StateStore.connect.
    assert control.get_action(
        "nonexistent-client", "action-000001", instance_id="instance-1"
    ) is None
