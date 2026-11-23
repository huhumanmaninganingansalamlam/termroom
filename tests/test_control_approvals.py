"""Instance-local scoped approvals: exact identity and fail-closed revocation."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from termroom.control_api import ControlApprovalIntents


class _EvidenceStore:
    def __init__(self) -> None:
        self.credential = {
            "id": "client-exact", "revoked_at": None,
            "capabilities": ["read", "operate"],
            "workspace_ids": ["workspace-source"],
            "name": "Test operator",
        }
        self.run = {
            "source_kind": "workspace",
            "source_workspace_id": "workspace-source",
            "target_computer_id": "node-actual",
        }
        self.grants = [{
            "backend_kind": "remote",
            "computer_id": "node-actual",
        }]

    def get_client_credential(self, client_id: str):
        return self.credential if client_id == self.credential["id"] else None

    def get_remote_run(self, run_id: str):
        return self.run if run_id == "run-actual" else None

    def terminal_activity_workspaces(self, workspace_ids):
        return self.grants if "workspace-source" in workspace_ids else []


def _remote_intent() -> dict:
    return {
        "client_id": "client-exact",
        "run_id": "run-actual",
        "workspace_id": "workspace-source",
        "target_id": "node-actual",
        "revision": "revision-1",
        "approved": False,
        "consumed": False,
        "action_id": "action-actual",
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
    }


def test_scoped_approval_is_instance_local_and_expires_closed() -> None:
    first = ControlApprovalIntents()
    second = ControlApprovalIntents()
    first.remote_apply["one"] = {**_remote_intent(), "expires_at": datetime.now(UTC)}
    first.terminal_close["two"] = {
        "expires_at": datetime.now(UTC) - timedelta(seconds=1),
    }
    first.prune_remote_apply()
    first.prune_terminal_close()
    assert first.remote_apply == {}
    assert first.terminal_close == {}
    assert second.remote_apply == second.terminal_close == {}


def test_remote_approval_rechecks_exact_grant_and_revocation() -> None:
    state = ControlApprovalIntents()
    store = _EvidenceStore()
    state.remote_apply["id-exact"] = _remote_intent()
    intent = state.require_remote_apply(
        approval_id="id-exact", run_id="run-actual", revision="revision-1",
        store=store,
    )
    assert intent is state.remote_apply["id-exact"]
    state.approve_remote_apply(
        intent, plan_hash="sha256-of-reviewed-plan", approver_session_id="human-1",
    )
    assert intent["approved"] is True
    assert intent["approver_session_id"] == "human-1"
    assert intent["plan_hash"] == "sha256-of-reviewed-plan"
    with pytest.raises(HTTPException) as done:
        state.require_remote_apply(
            approval_id="id-exact", run_id="run-actual", revision="revision-1",
            store=store,
        )
    assert done.value.status_code == 404

    state.remote_apply["new"] = _remote_intent()
    with pytest.raises(HTTPException) as revised:
        state.require_remote_apply(
            approval_id="new", run_id="run-actual", revision="revision-2",
            store=store,
        )
    assert revised.value.status_code == 409
    with pytest.raises(HTTPException) as wrong_run:
        state.require_remote_apply(
            approval_id="new", run_id="run-other", revision="revision-1",
            store=store,
        )
    assert wrong_run.value.status_code == 404

    store.credential["revoked_at"] = datetime.now(UTC).isoformat()
    with pytest.raises(HTTPException) as revoked:
        state.require_remote_apply(
            approval_id="new", run_id="run-actual", revision="revision-1",
            store=store,
        )
    assert revoked.value.status_code == 404
    store.credential["revoked_at"] = None
    store.grants = []
    with pytest.raises(HTTPException) as missing_target:
        state.require_remote_apply(
            approval_id="new", run_id="run-actual", revision="revision-1",
            store=store,
        )
    assert missing_target.value.status_code == 404


def test_close_approval_rejects_stale_pane_and_revoked_client() -> None:
    state = ControlApprovalIntents()
    store = _EvidenceStore()
    terminal = {
        "id": "terminal-actual", "tmux_window": "@23", "role": "shell",
    }
    state.terminal_close["id"] = {
        "client_id": "client-exact",
        "workspace_id": "workspace-source",
        "terminal_id": terminal["id"],
        "tmux_window": terminal["tmux_window"],
        "consumed": False, "approved": False,
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
    }
    intent, credential = state.require_terminal_close(
        approval_id="id", workspace_id="workspace-source",
        terminal=terminal, store=store,
    )
    assert credential["id"] == "client-exact"
    assert intent["approved"] is False
    state.approve_terminal_close(intent)
    assert intent["approved"] is True
    with pytest.raises(HTTPException) as changed_pane:
        state.require_terminal_close(
            approval_id="id", workspace_id="workspace-source",
            terminal={**terminal, "tmux_window": "@24"}, store=store,
        )
    assert changed_pane.value.status_code == 404
    store.credential["revoked_at"] = "2026-11-23T00:00:00Z"
    with pytest.raises(HTTPException) as revoked:
        state.require_terminal_close(
            approval_id="id", workspace_id="workspace-source",
            terminal=terminal, store=store,
        )
    assert revoked.value.status_code == 404
