"""Concrete client credential and action-receipt SQLite owner.

The schema and SQL are moved without modifications from StateStore v0.4.23.
Every mutation uses the existing StateStore.connect context and its atomic
commit/foreign-key/WAL policy; there is no nested connection or new ORM.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from termroom.db import StateStore


CONTROL_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS client_credentials (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    capabilities_json TEXT NOT NULL,
    workspace_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS client_action_receipts (
    client_id TEXT NOT NULL
        REFERENCES client_credentials(id) ON DELETE CASCADE,
    action_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'accepted'
        CHECK(status IN ('accepted', 'refused', 'observed', 'unknown')),
    phase TEXT NOT NULL DEFAULT 'accepted',
    instance_id TEXT NOT NULL,
    result_resource_id TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    PRIMARY KEY(client_id, action_id)
);
CREATE INDEX IF NOT EXISTS idx_client_actions_time
    ON client_action_receipts(client_id, created_at DESC);
"""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ControlStore:
    """Own the credential and idempotent action SQL, sharing Core's SQLite DB."""

    def __init__(self, state: StateStore) -> None:
        self.state = state

    @staticmethod
    def _credential_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["capabilities"] = json.loads(str(row["capabilities_json"]))
        result["workspace_ids"] = json.loads(str(row["workspace_ids_json"]))
        del result["capabilities_json"]
        del result["workspace_ids_json"]
        return result

    def create_credential(
        self,
        *,
        client_id: str,
        name: str,
        token_hash: str,
        capabilities: list[str],
        workspace_ids: list[str],
    ) -> dict[str, Any]:
        now = _utc_now()
        with self.state.connect() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM client_credentials WHERE revoked_at IS NULL"
            ).fetchone()[0]
            if count >= 32:
                raise ValueError("Revoke an unused client before adding another")
            db.execute(
                """
                INSERT INTO client_credentials (
                    id, name, token_hash, capabilities_json, workspace_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    client_id,
                    name,
                    token_hash,
                    json.dumps(capabilities, separators=(",", ":")),
                    json.dumps(workspace_ids, separators=(",", ":")),
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM client_credentials WHERE id = ?", (client_id,)
            ).fetchone()
            return self._credential_row(row)

    def get_credential(self, client_id: str) -> dict[str, Any] | None:
        with self.state.connect() as db:
            row = db.execute(
                "SELECT * FROM client_credentials WHERE id = ?", (client_id,)
            ).fetchone()
            return self._credential_row(row) if row else None

    def list_credentials(self) -> list[dict[str, Any]]:
        with self.state.connect() as db:
            rows = db.execute(
                "SELECT * FROM client_credentials ORDER BY created_at DESC, id DESC LIMIT 200"
            ).fetchall()
            return [self._credential_row(row) for row in rows]

    def touch_credential(self, client_id: str) -> None:
        now = datetime.now(UTC)
        with self.state.connect() as db:
            db.execute(
                """
                UPDATE client_credentials SET last_used_at = ?
                WHERE id = ? AND revoked_at IS NULL
                  AND (last_used_at IS NULL OR last_used_at < ?)
                """,
                (
                    now.isoformat(timespec="seconds"),
                    client_id,
                    (now - timedelta(minutes=1)).isoformat(timespec="seconds"),
                ),
            )

    def revoke_credential(self, client_id: str) -> dict[str, Any] | None:
        with self.state.connect() as db:
            db.execute(
                "UPDATE client_credentials SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?",
                (_utc_now(), client_id),
            )
            row = db.execute(
                "SELECT * FROM client_credentials WHERE id = ?", (client_id,)
            ).fetchone()
            return self._credential_row(row) if row else None

    def claim_action(
        self,
        *,
        client_id: str,
        action_id: str,
        action: str,
        resource_kind: str,
        resource_id: str,
        payload_sha256: str,
        instance_id: str,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically reserve one mutation identity; never replay a reused key."""

        if (
            not 8 <= len(action_id) <= 128
            or not 1 <= len(action) <= 80
            or not 1 <= len(resource_kind) <= 40
            or not 1 <= len(resource_id) <= 128
            or len(payload_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in payload_sha256)
        ):
            raise ValueError("Invalid action identity")
        now = datetime.now(UTC)
        created_at = now.isoformat(timespec="seconds")
        expires_at = (now + timedelta(days=30)).isoformat(timespec="seconds")
        with self.state.connect() as db:
            prior = db.execute(
                "SELECT 1 FROM client_action_receipts WHERE client_id = ? AND action_id = ?",
                (client_id, action_id),
            ).fetchone()
            if prior is None:
                count = db.execute(
                    "SELECT COUNT(*) FROM client_action_receipts WHERE client_id = ?",
                    (client_id,),
                ).fetchone()[0]
                if count >= 10000:
                    raise RuntimeError("Action receipt capacity reached; do not reuse old IDs")
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO client_action_receipts (
                    client_id, action_id, action, resource_kind, resource_id,
                    payload_sha256, instance_id, created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_id, action_id, action, resource_kind, resource_id,
                    payload_sha256, instance_id, created_at, created_at, expires_at,
                ),
            )
            created = cursor.rowcount == 1
            row = db.execute(
                "SELECT * FROM client_action_receipts WHERE client_id = ? AND action_id = ?",
                (client_id, action_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("Action receipt was not persisted")
            if (
                row["action"] != action
                or row["resource_kind"] != resource_kind
                or row["resource_id"] != resource_id
                or row["payload_sha256"] != payload_sha256
            ):
                raise ValueError("idempotency_conflict")
            if not created and (
                (row["instance_id"] != instance_id or row["expires_at"] <= created_at)
                and row["status"] == "accepted"
            ):
                db.execute(
                    """
                    UPDATE client_action_receipts SET status = 'unknown', updated_at = ?
                    WHERE client_id = ? AND action_id = ? AND status = 'accepted'
                    """,
                    (created_at, client_id, action_id),
                )
                row = db.execute(
                    "SELECT * FROM client_action_receipts WHERE client_id = ? AND action_id = ?",
                    (client_id, action_id),
                ).fetchone()
            return dict(row), created

    def get_action(
        self, client_id: str, action_id: str, *, instance_id: str
    ) -> dict[str, Any] | None:
        with self.state.connect() as db:
            row = db.execute(
                "SELECT * FROM client_action_receipts WHERE client_id = ? AND action_id = ?",
                (client_id, action_id),
            ).fetchone()
            if row is None:
                return None
            now = _utc_now()
            if row["status"] == "accepted" and (
                row["instance_id"] != instance_id or row["expires_at"] <= now
            ):
                db.execute(
                    """
                    UPDATE client_action_receipts SET status = 'unknown', updated_at = ?
                    WHERE client_id = ? AND action_id = ? AND status = 'accepted'
                    """,
                    (now, client_id, action_id),
                )
                row = db.execute(
                    "SELECT * FROM client_action_receipts WHERE client_id = ? AND action_id = ?",
                    (client_id, action_id),
                ).fetchone()
            return dict(row)

    def advance_action(
        self,
        *,
        client_id: str,
        action_id: str,
        instance_id: str,
        status: str,
        phase: str,
        result_resource_id: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"accepted", "refused", "observed", "unknown"}:
            raise ValueError("Invalid action status")
        if phase not in {
            "accepted", "dispatched", "pty_delivered",
            "process_started", "process_finished", "result_verified",
        }:
            raise ValueError("Invalid observation phase")
        if result_resource_id is not None and len(result_resource_id) > 128:
            raise ValueError("Result identity too long")
        if error_code is not None and len(error_code) > 80:
            raise ValueError("Error code too long")
        with self.state.connect() as db:
            previous = db.execute(
                "SELECT * FROM client_action_receipts WHERE client_id = ? AND action_id = ?",
                (client_id, action_id),
            ).fetchone()
            if (
                previous is None
                or previous["instance_id"] != instance_id
                or previous["status"] not in {"accepted", "observed"}
            ):
                raise RuntimeError("Action is no longer owned or already unknown/refused")
            phases = (
                "accepted", "dispatched", "pty_delivered",
                "process_started", "process_finished", "result_verified",
            )
            if phases.index(phase) < phases.index(previous["phase"]):
                raise ValueError("Cannot move action evidence backwards")
            if status == "observed" and phase not in {"process_finished", "result_verified"}:
                raise ValueError("A transport receipt cannot establish process completion")
            if previous["status"] == "observed" and status != "observed":
                raise ValueError("Cannot erase observed completion")
            updated = db.execute(
                """
                UPDATE client_action_receipts
                SET status = ?, phase = ?, result_resource_id = ?, error_code = ?,
                    updated_at = ?
                WHERE client_id = ? AND action_id = ? AND instance_id = ?
                  AND status IN ('accepted', 'observed')
                """,
                (
                    status, phase, result_resource_id, error_code, _utc_now(),
                    client_id, action_id, instance_id,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("Action is no longer owned or already unknown/refused")
            row = db.execute(
                "SELECT * FROM client_action_receipts WHERE client_id = ? AND action_id = ?",
                (client_id, action_id),
            ).fetchone()
            return dict(row)
