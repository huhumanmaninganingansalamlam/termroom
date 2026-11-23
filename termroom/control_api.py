from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import secrets
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    JSONResponse,
)

from termroom.auth import ClientCredentialManager
from termroom.config import Settings
from termroom.db import (
    FILE_RUN_TERMINAL_STATES,
    StateStore,
    normalize_quick_command_name,
    quick_commands_revision,
)
from termroom.file_runs import FileRunConflict, FileRunError, FileRunManager
from termroom.files import (
    DirectoryListingLimitError,
    FileConflictError,
    FileService,
    FileSnapshot,
    UnsupportedFileError,
)
from termroom.remote_access import (
    RemoteAccess,
    RemoteAccessError,
)
from termroom.remote_runs import (
    RemoteRunError,
    RemoteRunManager,
)
from termroom.run_results import (
    RemoteRunResultCollector,
    ResultCollectionConflict,
    ResultCollectionError,
)
from termroom.security import (
    PathBoundaryError,
)
from termroom.ssh_backend import SSHBackendError
from termroom.terminal_control import TerminalControl
from termroom.terminals import (
    TerminalError,
    TerminalInputRejected,
    TerminalManager,
    normalize_terminal_name,
    normalize_workspace_command,
    terminal_command_target_error,
    validate_workspace_command_launch,
    workspace_command_digest,
)
from termroom.workspaces import (
    WorkspaceManager,
)

# Domain-owned control routes. Only the explicitly named dependencies below
# enter this feature; the product's Files/Terminal/Run managers remain owners.

@dataclass(slots=True)
class ControlApprovalIntents:
    """Instance-local human approvals, never a persistent permission grant.

    Browser routes ask this control-owned state to validate and consume
    intents. Only human session and CSRF admission remain with the UI routes.
    """
    remote_apply: dict[str, dict[str, Any]] = field(default_factory=dict)
    terminal_close: dict[str, dict[str, Any]] = field(default_factory=dict)

    @staticmethod
    def plan_hash(plan: Any) -> str:
        return hashlib.sha256(
            json.dumps(
                plan.as_dict(), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def prune_remote_apply(self) -> None:
        now = datetime.now(UTC)
        for approval_id, intent in tuple(self.remote_apply.items()):
            if intent["expires_at"] <= now:
                del self.remote_apply[approval_id]

    def prune_terminal_close(self) -> None:
        now = datetime.now(UTC)
        for approval_id, intent in tuple(self.terminal_close.items()):
            if intent["expires_at"] <= now:
                self.terminal_close.pop(approval_id, None)

    def pending_remote_apply(
        self, *, run: Mapping[str, Any], plan: Any, store: StateStore,
    ) -> list[dict[str, str]]:
        self.prune_remote_apply()
        pending: list[dict[str, str]] = []
        for approval_id, intent in self.remote_apply.items():
            if (
                intent["run_id"] != run["id"]
                or intent["revision"] != plan.revision
                or intent["workspace_id"] != run.get("source_workspace_id")
                or intent["target_id"] != run.get("target_computer_id")
                or intent["approved"] or intent["consumed"]
            ):
                continue
            credential = store.get_client_credential(intent["client_id"])
            if credential is None or credential.get("revoked_at"):
                continue
            pending.append({
                "id": approval_id,
                "client_name": str(credential["name"]),
                "action_id": str(intent["action_id"]),
            })
        return pending

    def require_remote_apply(
        self, *, approval_id: str, run_id: str, revision: str, store: StateStore,
    ) -> dict[str, Any]:
        self.prune_remote_apply()
        intent = self.remote_apply.get(approval_id)
        if (
            intent is None or intent["run_id"] != run_id
            or intent["approved"] or intent["consumed"]
        ):
            raise HTTPException(status_code=404, detail="Approval request not available")
        if revision != intent["revision"]:
            raise HTTPException(status_code=409, detail="The reviewed plan has changed")
        run = store.get_remote_run(run_id)
        client_row = store.get_client_credential(intent["client_id"])
        if (
            run is None or client_row is None or client_row.get("revoked_at")
            or "operate" not in client_row["capabilities"]
            or run["source_kind"] != "workspace"
            or run["source_workspace_id"] != intent["workspace_id"]
            or run["target_computer_id"] != intent["target_id"]
            or intent["workspace_id"] not in client_row["workspace_ids"]
        ):
            raise HTTPException(status_code=404, detail="Approval request not available")
        granted = store.terminal_activity_workspaces(client_row["workspace_ids"])
        if not any(
            row["backend_kind"] == "remote"
            and row["computer_id"] == intent["target_id"]
            for row in granted
        ):
            raise HTTPException(status_code=404, detail="Approval request not available")
        return intent

    def approve_remote_apply(
        self, intent: dict[str, Any], *, plan_hash: str, approver_session_id: str,
    ) -> None:
        intent["plan_hash"] = plan_hash
        intent["approved"] = True
        intent["approver_session_id"] = approver_session_id

    def require_terminal_close(
        self, *, approval_id: str, workspace_id: str, terminal: Mapping[str, Any],
        store: StateStore,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self.prune_terminal_close()
        intent = self.terminal_close.get(approval_id)
        if (
            intent is None or intent["workspace_id"] != workspace_id
            or intent["terminal_id"] != terminal["id"]
            or intent["tmux_window"] != terminal["tmux_window"]
            or intent["consumed"] or terminal.get("role") != "shell"
        ):
            raise HTTPException(status_code=404, detail="resource_unavailable")
        credential = store.get_client_credential(intent["client_id"])
        if credential is None or credential.get("revoked_at"):
            raise HTTPException(status_code=404, detail="resource_unavailable")
        return intent, credential

    @staticmethod
    def approve_terminal_close(intent: dict[str, Any]) -> None:
        intent["approved"] = True


@dataclass(frozen=True, slots=True)
class ControlRouteContext:
    settings: Settings
    store: StateStore
    workspaces: WorkspaceManager
    terminals: TerminalManager
    remote: RemoteAccess
    remote_runs: RemoteRunManager
    file_runs: FileRunManager
    run_results: RemoteRunResultCollector
    files: FileService
    terminal_control: TerminalControl
    control_credentials: ClientCredentialManager
    control_instance_id: str
    approval_intents: ControlApprovalIntents
    terminal_mutation_tasks: set[asyncio.Task[JSONResponse]]
    is_remote: Callable[[Mapping[str, Any]], bool]
    local_file_call: Callable[..., Any]
    list_workspace_dir: Callable[..., Any]
    search_workspace_files: Callable[..., Any]
    stat_workspace_file: Callable[..., Any]
    read_workspace_text: Callable[..., Any]
    write_workspace_text: Callable[..., Any]
    safe_control_transport: Callable[[Request], bool]
    verified_csrf_header: Callable[..., Any]
    is_internal_state_entry: Callable[..., Any]


def register_control_routes(app: FastAPI, context: ControlRouteContext) -> None:
    # Bind domain dependencies once, preserving exact source route semantics.
    settings = context.settings
    store = context.store
    workspaces = context.workspaces
    terminals = context.terminals
    remote = context.remote
    remote_runs = context.remote_runs
    file_runs = context.file_runs
    run_results = context.run_results
    files = context.files
    terminal_control = context.terminal_control
    control_credentials = context.control_credentials
    control_instance_id = context.control_instance_id
    control_apply_intents = context.approval_intents.remote_apply
    terminal_close_intents = context.approval_intents.terminal_close
    _control_apply_intents_prune = context.approval_intents.prune_remote_apply
    _control_apply_plan_hash = context.approval_intents.plan_hash
    _terminal_close_prune = context.approval_intents.prune_terminal_close
    terminal_mutation_tasks = context.terminal_mutation_tasks
    is_remote = context.is_remote
    local_file_call = context.local_file_call
    list_workspace_dir = context.list_workspace_dir
    search_workspace_files = context.search_workspace_files
    stat_workspace_file = context.stat_workspace_file
    read_workspace_text = context.read_workspace_text
    write_workspace_text = context.write_workspace_text
    _safe_control_transport = context.safe_control_transport
    _verified_csrf_header = context.verified_csrf_header
    _is_internal_state_entry = context.is_internal_state_entry

    async def _bounded_control_json(
        request: Request, *, max_body_bytes: int = 8192,
    ) -> dict[str, Any]:
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            raise HTTPException(status_code=415, detail="Expected application/json")
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > max_body_bytes:
                raise HTTPException(status_code=413, detail="Request body exceeds size limit")
            chunks.append(chunk)
        try:
            value = json.loads(b"".join(chunks))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc
        if not isinstance(value, dict):
            raise HTTPException(status_code=400, detail="Expected a JSON object")
        return value

    @app.get("/api/client-credentials", response_class=JSONResponse)
    async def list_client_credentials(request: Request) -> dict[str, Any]:
        if not _safe_control_transport(request):
            raise HTTPException(status_code=426, detail="Use HTTPS to manage clients")
        return {
            "ok": True,
            "clients": control_credentials.list_public(),
            "workspaces": [
                {"id": workspace["id"], "name": workspace["display_name"]}
                for workspace in store.list_recent_workspaces(limit=200)
            ],
        }

    @app.post("/api/client-credentials", response_class=JSONResponse)
    async def create_client_credential(request: Request) -> JSONResponse:
        if not _safe_control_transport(request):
            raise HTTPException(status_code=426, detail="Use HTTPS to manage clients")
        _verified_csrf_header(request, settings)
        values = await _bounded_control_json(request)
        if set(values) - {"name", "capabilities", "workspace_ids"}:
            raise HTTPException(status_code=400, detail="Unsupported client configuration field")
        try:
            credential = control_credentials.issue(
                name=values.get("name", ""),
                capabilities=values.get("capabilities", ["read"]),
                workspace_ids=values.get("workspace_ids", []),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "client": credential}, status_code=201)

    @app.post("/api/client-credentials/{client_id}/revoke", response_class=JSONResponse)
    async def revoke_client_credential(request: Request, client_id: str) -> dict[str, Any]:
        if not _safe_control_transport(request):
            raise HTTPException(status_code=426, detail="Use HTTPS to manage clients")
        _verified_csrf_header(request, settings)
        values = await _bounded_control_json(request)
        if values != {"confirm_client_id": client_id}:
            raise HTTPException(
                status_code=403,
                detail="Exact client revocation confirmation required",
            )
        revoked = control_credentials.revoke(client_id)
        if revoked is None:
            raise HTTPException(status_code=404, detail="Client not found")
        return {"ok": True, "client": revoked}

    control_read_gate = asyncio.Semaphore(4)

    def _control_release_observer(task: asyncio.Task[Any]) -> None:
        # Shielded backend work may finish after the HTTP reader has timed out.
        with contextlib.suppress(asyncio.CancelledError):
            task.exception()
        control_read_gate.release()

    async def _control_bounded_read(request: Request, operation: Callable[[], Any]) -> Any:
        # The deadline includes waiting for a slot. Once admitted, retain its
        # slot until real backend work ends, even if the HTTP reader gives up.
        try:
            async with asyncio.timeout(8.0):
                await control_read_gate.acquire()
                try:
                    # A queued read cannot begin after its bearer is revoked.
                    credential = store.get_client_credential(
                        request.state.control_client.id,
                    )
                    if credential is None or credential["revoked_at"] is not None:
                        raise HTTPException(status_code=409, detail="credential_revoked")
                    observer = asyncio.create_task(operation())
                except BaseException:
                    control_read_gate.release()
                    raise
                try:
                    result = await asyncio.shield(observer)
                    # Do not return a completed snapshot to a revoked reader.
                    credential = store.get_client_credential(
                        request.state.control_client.id,
                    )
                    if credential is None or credential["revoked_at"] is not None:
                        raise HTTPException(status_code=409, detail="credential_revoked")
                    return result
                finally:
                    if observer.done():
                        control_read_gate.release()
                    else:
                        observer.add_done_callback(_control_release_observer)
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="observation_timeout") from exc

    def _control_workspace(request: Request, workspace_id: str) -> dict[str, Any]:
        client = request.state.control_client
        if not client.can_access(workspace_id):
            raise HTTPException(status_code=404, detail="resource_unavailable")
        # Authentication precedes streamed request bodies; revocation must win
        # until a Workspace-scoped action is actually admitted.
        credential = store.get_client_credential(client.id)
        if credential is None or credential["revoked_at"] is not None:
            raise HTTPException(status_code=409, detail="credential_revoked")
        try:
            workspace = workspaces.require(workspace_id)
        except (KeyError, OSError, PathBoundaryError) as exc:
            raise HTTPException(status_code=404, detail="resource_unavailable") from exc
        if workspace.get("workspace_kind") != "workspace":
            raise HTTPException(status_code=404, detail="resource_unavailable")
        if not settings.allow_local_workspaces and not is_remote(workspace):
            raise HTTPException(status_code=404, detail="resource_unavailable")
        return workspace

    @app.get("/api/control/v1/capabilities", response_class=JSONResponse)
    async def control_capabilities(request: Request) -> dict[str, Any]:
        client = request.state.control_client
        return {
            "ok": True,
            "schema_version": 1,
            "client": {
                "id": client.id,
                "capabilities": sorted(client.capabilities),
                "workspace_ids": sorted(client.workspace_ids),
            },
            "actions": [
                "workspace.list", "workspace.describe", "workspace.commands.list",
                "workspace.commands.create", "workspace.commands.update",
                "workspace.commands.delete", "workspace.commands.launch",
                "workspace.terminals.list", "workspace.terminals.describe",
                "workspace.terminals.create", "workspace.terminals.rename",
                "terminal.control.observe", "terminal.control.claim",
                "terminal.control.release", "terminal.input",
                "terminal.close_intent", "terminal.close_intent.status",
                "workspace.terminals.close",
                "terminal.snapshot", "action.status",
                "workspace.files.list", "workspace.files.stat",
                "workspace.files.search", "workspace.files.read",
                "workspace.files.write", "workspace.files.create",
                "workspace.file_runs.list", "workspace.file_runs.describe",
                "workspace.file_runs.start",
                "workspace.file_runs.stop",
                "workspace.remote_runs.list", "workspace.remote_runs.describe",
                "workspace.remote_runs.start", "workspace.remote_runs.result.review",
                "workspace.remote_runs.stop",
                "workspace.remote_runs.result.apply_intent",
                "workspace.remote_runs.result.apply_intent.status",
                "workspace.remote_runs.result.apply",
                "workspace.remote_runs.snapshot",
                "workspace.workspace_runs.list", "workspace.workspace_runs.describe",
            ],
            "limits": {
                "body_bytes": 8192, "file_write_body_bytes": 262144,
                "file_write_content_bytes": 131072, "credential_workspaces": 32,
                "terminals_per_workspace": 64, "concurrent_reads": 4,
                "observation_timeout_seconds": 8,
            },
        }

    @app.get("/api/control/v1/workspaces/{workspace_id}", response_class=JSONResponse)
    async def control_workspace_identity(request: Request, workspace_id: str) -> dict[str, Any]:
        workspace = _control_workspace(request, workspace_id)
        return {
            "ok": True,
            "schema_version": 1,
            "action": "workspace.describe",
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "workspace": {
                "id": workspace["id"],
                "display_name": workspace["display_name"],
                "backend_kind": workspace["backend_kind"],
                "computer_id": workspace.get("computer_id"),
            },
        }

    @app.get("/api/control/v1/workspaces", response_class=JSONResponse)
    async def control_workspace_list(request: Request) -> dict[str, Any]:
        client = request.state.control_client
        rows = store.terminal_activity_workspaces(sorted(client.workspace_ids))
        items = []
        collected_at = datetime.now(UTC).isoformat(timespec="seconds")
        for row in rows:
            if not settings.allow_local_workspaces and row["backend_kind"] == "local":
                continue
            connected = None
            if is_remote(row) and remote.is_node(row):
                connected = remote.status(row["computer"])["online"]
            items.append({
                "id": row["id"],
                "display_name": row["display_name"],
                "backend_kind": row["backend_kind"],
                "computer_id": row.get("computer_id"),
                "connection": {
                    "state": (
                        "connected" if connected is True
                        else "disconnected" if connected is False else "unknown"
                    ),
                    "source": "core_node_transport" if connected is not None else "not_probed",
                    "collected_at": collected_at,
                },
            })
        return {
            "ok": True, "schema_version": 1, "action": "workspace.list",
            "collected_at": collected_at, "workspaces": items,
            "limit": 32, "next_cursor": None,
        }

    def _quick_control_command(row: Mapping[str, Any], slot: int) -> dict[str, Any]:
        return {
            "id": row["id"], "name": row["name"], "command": row["command"],
            "revision": row["revision"], "legacy_slot": slot,
        }

    @app.get("/api/control/v1/workspaces/{workspace_id}/commands")
    async def control_workspace_commands(request: Request, workspace_id: str) -> dict[str, Any]:
        _control_workspace(request, workspace_id)
        commands = store.list_workspace_quick_commands(workspace_id)
        return {
            "ok": True, "schema_version": 1, "action": "workspace.commands.list",
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "workspace_id": workspace_id,
            "revision": quick_commands_revision(commands),
            "commands": [
                _quick_control_command(row, slot) for slot, row in enumerate(commands)
            ],
            "identity_kind": "stable_id",
        }

    def _quick_control_response(
        row: Mapping[str, Any], *, created: bool = False,
        extra: Mapping[str, Any] | None = None,
    ) -> JSONResponse:
        status = str(row["status"])
        error = str(row.get("error_code") or (
            "unknown_outcome" if status == "unknown" else ""
        ))
        return JSONResponse(
            {
                "ok": status in {"accepted", "observed"},
                "schema_version": 1,
                "action_id": row["action_id"],
                "action": row["action"],
                "workspace_id": row["resource_id"],
                "status": status,
                "phase": row["phase"],
                "result_resource_id": row.get("result_resource_id"),
                **({"code": error} if error else {}),
                **(extra or {}),
            },
            status_code=(
                409 if status in {"refused", "unknown"}
                else 202 if status == "accepted"
                else 201 if created else 200
            ),
        )

    def _control_action_id(value: Any) -> str:
        if (
            not isinstance(value, str)
            or not 8 <= len(value) <= 128
            or not all(c.isascii() and (c.isalnum() or c in "-._:") for c in value)
        ):
            raise HTTPException(status_code=400, detail="invalid_action_id")
        return value

    def _control_claim_mutation(
        request: Request, workspace_id: str, *,
        action: str, values: dict[str, Any], required: set[str],
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        workspace = _control_workspace(request, workspace_id)
        client = request.state.control_client
        if not client.can_access(workspace_id, "operate"):
            raise HTTPException(status_code=404, detail="resource_unavailable")
        if set(values) != required:
            raise HTTPException(status_code=400, detail="invalid_fields")
        action_id = _control_action_id(values["action_id"])
        payload_sha256 = hashlib.sha256(
            json.dumps(
                values, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        try:
            receipt, claimed = store.claim_client_action(
                client_id=client.id, action_id=action_id, action=action,
                resource_kind="workspace", resource_id=workspace_id,
                payload_sha256=payload_sha256, instance_id=control_instance_id,
            )
        except ValueError as exc:
            if str(exc) == "idempotency_conflict":
                raise HTTPException(status_code=409, detail="idempotency_conflict") from exc
            raise HTTPException(status_code=400, detail="invalid_action_id") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="action_capacity_reached") from exc
        return workspace, receipt, claimed

    def _control_record_action(
        request: Request, action_id: str, *, status: str, phase: str,
        result_resource_id: str | None = None, error_code: str | None = None,
    ) -> dict[str, Any]:
        return store.update_client_action(
            client_id=request.state.control_client.id, action_id=action_id,
            instance_id=control_instance_id, status=status, phase=phase,
            result_resource_id=result_resource_id, error_code=error_code,
        )

    def _quick_control_mutation(
        request: Request, workspace_id: str, *,
        action: str, values: dict[str, Any], required: set[str],
        operation: Callable[[], dict[str, Any] | None],
    ) -> JSONResponse:
        _, receipt, claimed = _control_claim_mutation(
            request, workspace_id, action=action, values=values, required=required,
        )
        client = request.state.control_client
        action_id = values["action_id"]
        if not claimed:
            # Never retry a write after an ambiguous timeout, restart, or duplicate.
            return _quick_control_response(receipt)
        error_code = None
        result = None
        try:
            result = operation()
        except KeyError:
            error_code = "command_not_found"
        except ValueError:
            error_code = "command_limit_or_value_invalid"
        if result is None:
            receipt = store.update_client_action(
                client_id=client.id, action_id=action_id,
                instance_id=control_instance_id, status="refused", phase="accepted",
                error_code=error_code or "revision_conflict",
            )
            return _quick_control_response(receipt)
        receipt = store.update_client_action(
            client_id=client.id, action_id=action_id,
            instance_id=control_instance_id, status="observed", phase="result_verified",
            result_resource_id=str(result["id"]),
        )
        return _quick_control_response(receipt, created=(action == "workspace.commands.create"))

    @app.post("/api/control/v1/workspaces/{workspace_id}/commands")
    async def control_quick_command_create(
        request: Request, workspace_id: str,
    ) -> JSONResponse:
        values = await _bounded_control_json(request)
        if set(values) != {"action_id", "name", "command", "expected_revision"}:
            raise HTTPException(status_code=400, detail="invalid_fields")
        if (
            not isinstance(values["expected_revision"], str)
            or len(values["expected_revision"]) != 64
        ):
            raise HTTPException(status_code=400, detail="invalid_revision")
        try:
            name = normalize_quick_command_name(values["name"])
            command = normalize_workspace_command(values["command"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_command") from exc
        return _quick_control_mutation(
            request, workspace_id, action="workspace.commands.create", values=values,
            required={"action_id", "name", "command", "expected_revision"},
            operation=lambda: store.create_workspace_quick_command(
                workspace_id, name=name, command=command,
                expected_list_revision=values["expected_revision"],
            ),
        )

    @app.patch("/api/control/v1/workspaces/{workspace_id}/commands/{command_id}")
    async def control_quick_command_update(
        request: Request, workspace_id: str, command_id: str,
    ) -> JSONResponse:
        values = await _bounded_control_json(request)
        if set(values) != {"action_id", "name", "command", "expected_revision"}:
            raise HTTPException(status_code=400, detail="invalid_fields")
        if type(values["expected_revision"]) is not int or values["expected_revision"] < 1:
            raise HTTPException(status_code=400, detail="invalid_revision")
        try:
            name = normalize_quick_command_name(values["name"])
            command = normalize_workspace_command(values["command"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_command") from exc
        # The target identity is part of the immutable action fingerprint.
        values = {**values, "command_id": command_id}
        return _quick_control_mutation(
            request, workspace_id, action="workspace.commands.update", values=values,
            required={"action_id", "name", "command", "expected_revision", "command_id"},
            operation=lambda: store.update_workspace_quick_command(
                workspace_id, command_id, expected_revision=values["expected_revision"],
                name=name, command=command,
            ),
        )

    @app.delete("/api/control/v1/workspaces/{workspace_id}/commands/{command_id}")
    async def control_quick_command_delete(
        request: Request, workspace_id: str, command_id: str,
    ) -> JSONResponse:
        values = await _bounded_control_json(request)
        if set(values) != {"action_id", "expected_revision"}:
            raise HTTPException(status_code=400, detail="invalid_fields")
        if type(values["expected_revision"]) is not int or values["expected_revision"] < 1:
            raise HTTPException(status_code=400, detail="invalid_revision")
        values = {**values, "command_id": command_id}
        return _quick_control_mutation(
            request, workspace_id, action="workspace.commands.delete", values=values,
            required={"action_id", "expected_revision", "command_id"},
            operation=lambda: store.delete_workspace_quick_command(
                workspace_id, command_id, expected_revision=values["expected_revision"],
            ),
        )

    @app.post("/api/control/v1/workspaces/{workspace_id}/commands/{command_id}/launch")
    async def control_quick_command_launch(
        request: Request, workspace_id: str, command_id: str,
    ) -> JSONResponse:
        values = await _bounded_control_json(request)
        required = {"action_id", "command_id", "expected_revision", "launch_id"}
        if (
            len(command_id) != 32
            or any(char not in "0123456789abcdef" for char in command_id)
        ):
            raise HTTPException(status_code=404, detail="resource_unavailable")
        if set(values) != required - {"command_id"}:
            raise HTTPException(status_code=400, detail="invalid_fields")
        if type(values["expected_revision"]) is not int or values["expected_revision"] < 1:
            raise HTTPException(status_code=400, detail="invalid_revision")
        try:
            launch_id = validate_workspace_command_launch(values["launch_id"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="request_invalid") from exc
        values = {**values, "command_id": command_id}
        workspace, receipt, claimed = _control_claim_mutation(
            request, workspace_id, action="workspace.commands.launch",
            values=values, required=required,
        )
        extra = {"command_id": command_id, "launch_id": launch_id}
        if not claimed:
            return _quick_control_response(receipt, extra=extra)
        records = store.list_workspace_quick_commands(workspace_id)
        selected = next(
            ((slot, record) for slot, record in enumerate(records)
             if record["id"] == command_id),
            None,
        )
        if selected is None or selected[1]["revision"] != values["expected_revision"]:
            receipt = _control_record_action(
                request, values["action_id"], status="refused", phase="accepted",
                error_code="command_not_found" if selected is None else "revision_conflict",
            )
            return _quick_control_response(receipt, extra=extra)
        slot, command_row = selected
        command = str(command_row["command"])
        digest = workspace_command_digest(command)
        try:
            admission = store.claim_workspace_command(
                workspace_id, launch_id, slot, digest,
                command_id=command_id, expected_revision=values["expected_revision"],
            )
        except ValueError:
            admission = "replayed"
        if admission != "created":
            receipt = _control_record_action(
                request, values["action_id"], status="refused", phase="accepted",
                error_code="revision_conflict" if admission == "changed"
                else "launch_identity_conflict",
            )
            return _quick_control_response(receipt, extra=extra)
        _control_record_action(
            request, values["action_id"], status="accepted", phase="dispatched",
            result_resource_id=launch_id,
        )
        try:
            if is_remote(workspace):
                terminal = await remote.run_workspace_command(
                    workspace, slot=slot, command=command, launch_id=launch_id,
                )
            else:
                terminal = await asyncio.to_thread(
                    terminals.run_workspace_command,
                    workspace, slot=slot, command=command, launch_id=launch_id,
                )
            terminal_id = str(terminal["id"])
        except Exception:
            logging.getLogger(__name__).exception("Control Quick Command launch outcome unknown")
            receipt = _control_record_action(
                request, values["action_id"], status="unknown", phase="dispatched",
                result_resource_id=launch_id, error_code="unknown_outcome",
            )
            return _quick_control_response(receipt, extra=extra)
        receipt = _control_record_action(
            request, values["action_id"], status="accepted", phase="process_started",
            result_resource_id=launch_id,
        )
        return _quick_control_response(receipt, extra={
            **extra, "terminal_id": terminal_id,
            "execution_state": "managed_terminal_started_not_process_finished",
        })

    @app.get("/api/control/v1/workspaces/{workspace_id}/terminals")
    async def control_terminal_list(request: Request, workspace_id: str) -> dict[str, Any]:
        _control_workspace(request, workspace_id)
        registered = store.list_terminals(workspace_id, limit=65)
        if len(registered) > 64:
            raise HTTPException(status_code=413, detail="resource_too_large")
        return {
            "ok": True, "schema_version": 1, "action": "workspace.terminals.list",
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "workspace_id": workspace_id, "observation": "registered_not_live_verified",
            "terminals": [
                {
                    "id": row["id"], "name": row["name"], "role": row["role"],
                    "tmux_window": row["tmux_window"],
                    "created_at": row["created_at"],
                }
                for row in registered
            ],
        }

    @app.get("/api/control/v1/workspaces/{workspace_id}/terminals/{terminal_id}")
    async def control_terminal_detail(
        request: Request, workspace_id: str, terminal_id: str,
    ) -> dict[str, Any]:
        _control_workspace(request, workspace_id)
        row = store.get_terminal(terminal_id)
        if row is None or row["workspace_id"] != workspace_id:
            raise HTTPException(status_code=404, detail="resource_unavailable")
        return {
            "ok": True, "schema_version": 1,
            "action": "workspace.terminals.describe",
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "workspace_id": workspace_id, "observation": "registered_not_live_verified",
            "terminal": {
                "id": row["id"], "name": row["name"], "role": row["role"],
                "tmux_window": row["tmux_window"], "created_at": row["created_at"],
            },
        }

    def _control_terminal_resource(
        request: Request, workspace_id: str, terminal_id: str, *,
        operate: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        workspace = _control_workspace(request, workspace_id)
        if operate and not request.state.control_client.can_access(workspace_id, "operate"):
            raise HTTPException(status_code=404, detail="resource_unavailable")
        terminal = store.get_terminal(terminal_id) if len(terminal_id) <= 128 else None
        if terminal is None or terminal["workspace_id"] != workspace_id:
            raise HTTPException(status_code=404, detail="resource_unavailable")
        return workspace, terminal

    async def _control_live_pane(
        workspace: dict[str, Any], terminal: dict[str, Any]
    ) -> dict[str, Any]:
        if is_remote(workspace):
            return await remote.current_pane_mode(workspace, terminal)
        return await asyncio.to_thread(terminals.current_pane_mode, workspace, terminal)

    def _control_expected_pane(value: Any) -> str:
        if (
            not isinstance(value, str) or not 2 <= len(value) <= 20
            or not value.startswith("%") or not value[1:].isdigit()
        ):
            raise HTTPException(status_code=400, detail="invalid_pane")
        return value

    def _control_revision(value: Any) -> int:
        if type(value) is not int or value < 0:
            raise HTTPException(status_code=400, detail="invalid_revision")
        return value

    def _terminal_action_result(
        request: Request, action_id: str, *, status: str, phase: str,
        terminal_id: str, code: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> JSONResponse:
        receipt = _control_record_action(
            request, action_id, status=status, phase=phase,
            result_resource_id=terminal_id, error_code=code,
        )
        return _quick_control_response(receipt, extra=extra)

    async def _control_owned_terminal_mutation(
        operation: Callable[[], Awaitable[JSONResponse]],
    ) -> JSONResponse:
        # shield() alone does not keep a strong reference once HTTP is cancelled.
        task = asyncio.create_task(operation())
        terminal_mutation_tasks.add(task)

        def settled(done: asyncio.Task[JSONResponse]) -> None:
            terminal_mutation_tasks.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                logging.getLogger(__name__).error(
                    "Control Terminal operation escaped receipt recording",
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(settled)
        return await asyncio.shield(task)

    @app.get("/api/control/v1/workspaces/{workspace_id}/terminals/{terminal_id}/control")
    async def control_terminal_control_state(
        request: Request, workspace_id: str, terminal_id: str,
    ) -> dict[str, Any]:
        workspace, terminal = _control_terminal_resource(request, workspace_id, terminal_id)
        try:
            pane = await _control_bounded_read(
                request, lambda: _control_live_pane(workspace, terminal),
            )
        except (TerminalError, SSHBackendError, RemoteAccessError, OSError) as exc:
            raise HTTPException(status_code=503, detail="observation_unavailable") from exc
        return {
            "ok": True, "schema_version": 1, "action": "terminal.control.observe",
            "workspace_id": workspace_id, "terminal_id": terminal_id,
            "tmux_window": terminal["tmux_window"],
            "pane": pane,
            "control": terminal_control.external_state(
                terminal_id, request.state.control_client.id,
            ),
            "resizing": "never_on_external_observation_or_input",
            "observation": "live_pane_and_in_memory_control",
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }

    @app.post("/api/control/v1/workspaces/{workspace_id}/terminals")
    async def control_terminal_create(
        request: Request, workspace_id: str,
    ) -> JSONResponse:
        values = await _bounded_control_json(request)
        workspace, receipt, claimed = _control_claim_mutation(
            request, workspace_id, action="workspace.terminals.create",
            values=values, required={"action_id", "name"},
        )
        if not claimed:
            return _quick_control_response(receipt)
        name = values["name"]
        if (
            not isinstance(name, str) or not 1 <= len(name) <= 32
            or name != normalize_terminal_name(name)
        ):
            return _terminal_action_result(
                request, values["action_id"], status="refused", phase="accepted",
                terminal_id=workspace_id, code="invalid_name",
            )
        async def owned_create() -> JSONResponse:
            _control_record_action(
                request, values["action_id"], status="accepted", phase="dispatched",
            )
            try:
                if is_remote(workspace):
                    terminal = await remote.create_terminal(workspace, name)
                else:
                    terminal = await asyncio.to_thread(terminals.create_terminal, workspace, name)
                pane = await _control_live_pane(workspace, terminal)
                if pane["window"] != terminal["tmux_window"]:
                    raise TerminalError("New Terminal identity could not be verified")
            except Exception:
                logging.getLogger(__name__).exception("Control Terminal create outcome unknown")
                return _terminal_action_result(
                    request, values["action_id"], status="unknown", phase="dispatched",
                    terminal_id=workspace_id, code="unknown_outcome",
                )
            return _terminal_action_result(
                request, values["action_id"], status="observed", phase="result_verified",
                terminal_id=str(terminal["id"]),
                extra={"terminal_id": terminal["id"], "tmux_window": terminal["tmux_window"],
                       "pane": pane["pane"]},
            )

        # Finish the admitted operation and its receipt even if HTTP disconnects.
        return await _control_owned_terminal_mutation(owned_create)

    @app.post("/api/control/v1/workspaces/{workspace_id}/terminals/{terminal_id}/rename")
    async def control_terminal_rename(
        request: Request, workspace_id: str, terminal_id: str,
    ) -> JSONResponse:
        values = await _bounded_control_json(request)
        workspace, terminal = _control_terminal_resource(
            request, workspace_id, terminal_id, operate=True,
        )
        _, receipt, claimed = _control_claim_mutation(
            request, workspace_id, action="workspace.terminals.rename",
            values={**values, "terminal_id": terminal_id},
            required={"action_id", "name", "expected_tmux_window", "terminal_id"},
        )
        if not claimed:
            return _quick_control_response(receipt, extra={"terminal_id": terminal_id})
        name = values["name"]
        if (
            not isinstance(name, str) or not 1 <= len(name) <= 32
            or name != normalize_terminal_name(name)
        ):
            return _terminal_action_result(
                request, values["action_id"], status="refused", phase="accepted",
                terminal_id=terminal_id, code="invalid_name",
            )
        async def owned_rename() -> JSONResponse:
            async with terminal_control.input_lock(terminal_id):
                if (
                    terminal.get("role") != "shell"
                    or terminal["tmux_window"] != values["expected_tmux_window"]
                ):
                    return _terminal_action_result(
                        request, values["action_id"], status="refused", phase="accepted",
                        terminal_id=terminal_id, code="terminal_identity_changed",
                    )
                # A queued rename is not admitted merely by HTTP authentication.
                credential = store.get_client_credential(request.state.control_client.id)
                if credential is None or credential["revoked_at"] is not None:
                    return _terminal_action_result(
                        request, values["action_id"], status="refused", phase="accepted",
                        terminal_id=terminal_id, code="credential_revoked",
                    )
                _control_record_action(
                    request, values["action_id"], status="accepted", phase="dispatched",
                    result_resource_id=terminal_id,
                )
                try:
                    if is_remote(workspace):
                        updated = await remote.rename_terminal(workspace, terminal, name)
                    else:
                        updated = await asyncio.to_thread(
                            terminals.rename_terminal, workspace, terminal, name,
                        )
                except Exception:
                    logging.getLogger(__name__).exception("Control Terminal rename outcome unknown")
                    return _terminal_action_result(
                        request, values["action_id"], status="unknown", phase="dispatched",
                        terminal_id=terminal_id, code="unknown_outcome",
                    )
                if (
                    updated["id"] != terminal_id
                    or updated["tmux_window"] != terminal["tmux_window"]
                ):
                    return _terminal_action_result(
                        request, values["action_id"], status="unknown", phase="dispatched",
                        terminal_id=terminal_id, code="unknown_outcome",
                    )
                return _terminal_action_result(
                    request, values["action_id"], status="observed", phase="result_verified",
                    terminal_id=terminal_id, extra={"terminal_id": terminal_id,
                        "tmux_window": updated["tmux_window"], "name": updated["name"]},
                )

        return await _control_owned_terminal_mutation(owned_rename)

    @app.post("/api/control/v1/workspaces/{workspace_id}/terminals/{terminal_id}/control/claim")
    async def control_terminal_claim(
        request: Request, workspace_id: str, terminal_id: str,
    ) -> JSONResponse:
        values = await _bounded_control_json(request)
        revision = _control_revision(values.get("expected_control_revision"))
        pane_id = _control_expected_pane(values.get("expected_pane"))
        workspace, terminal = _control_terminal_resource(
            request, workspace_id, terminal_id, operate=True,
        )
        _, receipt, claimed = _control_claim_mutation(
            request, workspace_id, action="terminal.control.claim",
            values={**values, "terminal_id": terminal_id},
            required={"action_id", "expected_tmux_window", "expected_pane",
                      "expected_control_revision", "terminal_id"},
        )
        extra = {"terminal_id": terminal_id, "tmux_window": values["expected_tmux_window"],
                 "pane": pane_id, "control_revision": revision + 1}
        if not claimed:
            return _quick_control_response(receipt, extra={
                **extra, "generation": receipt.get("result_resource_id") or "",
            })
        async with terminal_control.input_lock(terminal_id):
            if (
                terminal["tmux_window"] != values["expected_tmux_window"]
                or terminal.get("role") != "shell"
            ):
                return _terminal_action_result(
                    request, values["action_id"], status="refused", phase="accepted",
                    terminal_id=terminal_id, code="terminal_identity_changed", extra=extra,
                )
            try:
                mode = await _control_live_pane(workspace, terminal)
            except (TerminalError, SSHBackendError, RemoteAccessError, OSError):
                return _terminal_action_result(
                    request, values["action_id"], status="refused", phase="accepted",
                    terminal_id=terminal_id, code="pane_unavailable", extra=extra,
                )
            reason = ("pane_changed" if mode["window"] != terminal["tmux_window"] else
                      terminal_command_target_error({"pane": pane_id}, mode))
            if reason:
                return _terminal_action_result(
                    request, values["action_id"], status="refused", phase="accepted",
                    terminal_id=terminal_id, code=reason, extra=extra,
                )
            # Authentication preceded the pane wait; revocation must win
            # until this control claim is actually admitted.
            credential = store.get_client_credential(request.state.control_client.id)
            if credential is None or credential["revoked_at"] is not None:
                return _terminal_action_result(
                    request, values["action_id"], status="refused", phase="accepted",
                    terminal_id=terminal_id, code="credential_revoked", extra=extra,
                )
            generation, reason = terminal_control.claim_external(
                terminal_id, request.state.control_client.id, revision,
            )
            if reason:
                return _terminal_action_result(
                    request, values["action_id"], status="refused", phase="accepted",
                    terminal_id=terminal_id, code=reason, extra=extra,
                )
            assert generation is not None
            receipt = _control_record_action(
                request, values["action_id"], status="observed", phase="result_verified",
                result_resource_id=generation,
            )
            return _quick_control_response(receipt, extra={**extra, "generation": generation})

    @app.post("/api/control/v1/workspaces/{workspace_id}/terminals/{terminal_id}/control/release")
    async def control_terminal_release(
        request: Request, workspace_id: str, terminal_id: str,
    ) -> JSONResponse:
        values = await _bounded_control_json(request)
        revision = _control_revision(values.get("expected_control_revision"))
        _, terminal = _control_terminal_resource(
            request, workspace_id, terminal_id, operate=True,
        )
        _, receipt, claimed = _control_claim_mutation(
            request, workspace_id, action="terminal.control.release",
            values={**values, "terminal_id": terminal_id},
            required={"action_id", "generation", "expected_control_revision",
                      "expected_tmux_window", "terminal_id"},
        )
        if not claimed:
            return _quick_control_response(receipt, extra={"terminal_id": terminal_id})
        if not isinstance(values["generation"], str) or len(values["generation"]) != 32:
            return _terminal_action_result(
                request, values["action_id"], status="refused", phase="accepted",
                terminal_id=terminal_id, code="invalid_generation",
            )
        async with terminal_control.input_lock(terminal_id):
            # A pending release is not admitted after its bearer is revoked.
            credential = store.get_client_credential(request.state.control_client.id)
            if credential is None or credential["revoked_at"] is not None:
                return _terminal_action_result(
                    request, values["action_id"], status="refused", phase="accepted",
                    terminal_id=terminal_id, code="credential_revoked",
                )
            released = (
                terminal["tmux_window"] == values["expected_tmux_window"]
                and terminal_control.release_external(
                    terminal_id, request.state.control_client.id, values["generation"], revision,
                )
            )
            if not released:
                return _terminal_action_result(
                    request, values["action_id"], status="refused", phase="accepted",
                    terminal_id=terminal_id, code="control_conflict",
                )
            return _terminal_action_result(
                request, values["action_id"], status="observed", phase="result_verified",
                terminal_id=terminal_id,
                extra={"terminal_id": terminal_id, "control_revision": revision + 1},
            )

    @app.post("/api/control/v1/workspaces/{workspace_id}/terminals/{terminal_id}/input")
    async def control_terminal_input(
        request: Request, workspace_id: str, terminal_id: str,
    ) -> JSONResponse:
        values = await _bounded_control_json(request)
        revision = _control_revision(values.get("expected_control_revision"))
        pane_id = _control_expected_pane(values.get("pane"))
        data = values.get("data")
        if (
            not isinstance(data, str) or not 1 <= len(data.encode("utf-8")) <= 4096
            or any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in data)
            or type(values.get("append_enter")) is not bool
        ):
            raise HTTPException(status_code=400, detail="invalid_terminal_input")
        command_id = _control_action_id(values.get("command_id"))
        workspace, terminal = _control_terminal_resource(
            request, workspace_id, terminal_id, operate=True,
        )
        _, receipt, claimed = _control_claim_mutation(
            request, workspace_id, action="terminal.input",
            values={**values, "terminal_id": terminal_id},
            required={"action_id", "command_id", "generation", "pane",
                      "expected_tmux_window", "expected_control_revision",
                      "data", "append_enter", "terminal_id"},
        )
        extra = {
            "terminal_id": terminal_id, "pane": pane_id,
            "tmux_window": values["expected_tmux_window"],
            "generation": values["generation"], "command_id": command_id,
            "control_revision": revision,
            "delivery_scope": "tmux_accepted_not_receiver_executed",
            "input_bytes": len(data.encode("utf-8")),
            "append_enter": values["append_enter"],
        }
        if not claimed:
            return _quick_control_response(receipt, extra=extra)

        async def owned_delivery() -> JSONResponse:
            async with terminal_control.input_lock(terminal_id):
                if (
                    terminal["tmux_window"] != values["expected_tmux_window"]
                    or terminal.get("role") != "shell"
                ):
                    return _terminal_action_result(
                        request, values["action_id"], status="refused", phase="accepted",
                        terminal_id=terminal_id, code="terminal_identity_changed", extra=extra,
                    )
                if (
                    not isinstance(values["generation"], str)
                    or not terminal_control.can_external_input(
                        terminal_id, request.state.control_client.id,
                        values["generation"], revision,
                    )
                ):
                    return _terminal_action_result(
                        request, values["action_id"], status="refused", phase="accepted",
                        terminal_id=terminal_id, code="control_conflict", extra=extra,
                    )
                try:
                    mode = await _control_live_pane(workspace, terminal)
                except (TerminalError, SSHBackendError, RemoteAccessError, OSError):
                    return _terminal_action_result(
                        request, values["action_id"], status="refused", phase="accepted",
                        terminal_id=terminal_id, code="pane_unavailable", extra=extra,
                    )
                reason = ("pane_changed" if mode["window"] != terminal["tmux_window"] else
                          terminal_command_target_error({"pane": pane_id}, mode))
                if reason:
                    return _terminal_action_result(
                        request, values["action_id"], status="refused", phase="accepted",
                        terminal_id=terminal_id, code=reason, extra=extra,
                    )
                # An HTTP-authenticated request may have waited for this lock
                # or remote pane observation while its bearer was revoked.
                credential = store.get_client_credential(request.state.control_client.id)
                if credential is None or credential["revoked_at"] is not None:
                    return _terminal_action_result(
                        request, values["action_id"], status="refused", phase="accepted",
                        terminal_id=terminal_id, code="credential_revoked", extra=extra,
                    )
                _control_record_action(
                    request, values["action_id"], status="accepted", phase="dispatched",
                    result_resource_id=terminal_id,
                )
                try:
                    if is_remote(workspace):
                        await remote.paste_terminal_input(
                            workspace, terminal, pane_id, data, append_enter=values["append_enter"],
                        )
                    else:
                        await asyncio.to_thread(
                            terminals.paste_terminal_input, workspace, terminal,
                            pane_id, data, append_enter=values["append_enter"],
                        )
                except TerminalInputRejected as exc:
                    return _terminal_action_result(
                        request, values["action_id"], status="refused", phase="accepted",
                        terminal_id=terminal_id, code=exc.code, extra=extra,
                    )
                except RemoteAccessError as exc:
                    if exc.code in {"pane_changed", "pane_ended", "pane_in_mode"}:
                        return _terminal_action_result(
                            request, values["action_id"], status="refused", phase="accepted",
                            terminal_id=terminal_id, code=exc.code, extra=extra,
                        )
                    logging.getLogger(__name__).exception("Node Terminal delivery status unknown")
                    return _terminal_action_result(
                        request, values["action_id"], status="unknown", phase="dispatched",
                        terminal_id=terminal_id, code="unknown_outcome", extra=extra,
                    )
                except (TerminalError, SSHBackendError, OSError):
                    logging.getLogger(__name__).exception(
                        "Control Terminal delivery status unknown"
                    )
                    return _terminal_action_result(
                        request, values["action_id"], status="unknown", phase="dispatched",
                        terminal_id=terminal_id, code="unknown_outcome", extra=extra,
                    )
                return _terminal_action_result(
                    request, values["action_id"], status="accepted", phase="pty_delivered",
                    terminal_id=terminal_id, extra=extra,
                )
        # HTTP disconnect must not abandon the already admitted pane write or lock.
        return await _control_owned_terminal_mutation(owned_delivery)

    def _terminal_close_intent(
        request: Request, workspace_id: str, terminal_id: str, approval_id: str,
    ) -> dict[str, Any]:
        _control_terminal_resource(request, workspace_id, terminal_id, operate=True)
        _terminal_close_prune()
        intent = terminal_close_intents.get(approval_id)
        if (
            intent is None or intent["workspace_id"] != workspace_id
            or intent["terminal_id"] != terminal_id
            or intent["client_id"] != request.state.control_client.id
        ):
            raise HTTPException(status_code=404, detail="resource_unavailable")
        return intent

    def _terminal_close_intent_public(
        approval_id: str, intent: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "ok": True, "schema_version": 1,
            "approval_id": approval_id, "action_id": intent["action_id"],
            "terminal_id": intent["terminal_id"], "tmux_window": intent["tmux_window"],
            "state": "consumed" if intent["consumed"] else
                     "approved" if intent["approved"] else "pending",
            "expires_at": intent["expires_at"].isoformat(timespec="seconds"),
            "human_review_url": (
                f"/w/{intent['workspace_id']}/terminal/"
                f"{intent['terminal_id']}/close-review/{approval_id}"
            ),
        }

    @app.post("/api/control/v1/workspaces/{workspace_id}/terminals/{terminal_id}/close-intents")
    async def control_terminal_close_intent(
        request: Request, workspace_id: str, terminal_id: str,
    ) -> JSONResponse:
        values = await _bounded_control_json(request)
        if set(values) != {"action_id", "expected_tmux_window"}:
            raise HTTPException(status_code=400, detail="invalid_fields")
        action_id = _control_action_id(values["action_id"])
        _, terminal = _control_terminal_resource(request, workspace_id, terminal_id, operate=True)
        if (
            terminal.get("role") != "shell"
            or values["expected_tmux_window"] != terminal["tmux_window"]
        ):
            raise HTTPException(status_code=409, detail="terminal_identity_changed")
        _terminal_close_prune()
        client_id = request.state.control_client.id
        for approval_id, intent in terminal_close_intents.items():
            if intent["client_id"] == client_id and intent["action_id"] == action_id:
                if (
                    intent["workspace_id"] != workspace_id
                    or intent["terminal_id"] != terminal_id
                    or intent["tmux_window"] != values["expected_tmux_window"]
                ):
                    raise HTTPException(status_code=409, detail="idempotency_conflict")
                return JSONResponse(_terminal_close_intent_public(approval_id, intent))
        if len(terminal_close_intents) >= 64:
            raise HTTPException(status_code=503, detail="approval_capacity_reached")
        approval_id = secrets.token_urlsafe(24)
        intent = {
            "client_id": client_id, "workspace_id": workspace_id,
            "terminal_id": terminal_id, "tmux_window": terminal["tmux_window"],
            "action_id": action_id, "approved": False, "consumed": False,
            "expires_at": datetime.now(UTC) + timedelta(minutes=10),
        }
        terminal_close_intents[approval_id] = intent
        return JSONResponse(_terminal_close_intent_public(approval_id, intent), status_code=201)

    @app.get("/api/control/v1/workspaces/{workspace_id}/terminals/{terminal_id}/close-intents/{approval_id}")
    async def control_terminal_close_intent_status(
        request: Request, workspace_id: str, terminal_id: str, approval_id: str,
    ) -> dict[str, Any]:
        intent = _terminal_close_intent(request, workspace_id, terminal_id, approval_id)
        return _terminal_close_intent_public(approval_id, intent)

    @app.post("/api/control/v1/workspaces/{workspace_id}/terminals/{terminal_id}/close")
    async def control_terminal_close(
        request: Request, workspace_id: str, terminal_id: str,
    ) -> JSONResponse:
        values = await _bounded_control_json(request)
        bound = {**values, "terminal_id": terminal_id}
        required = {"action_id", "approval_id", "expected_tmux_window", "terminal_id"}
        if set(bound) != required:
            raise HTTPException(status_code=400, detail="invalid_fields")
        _control_action_id(values["action_id"])
        _control_workspace(request, workspace_id)
        if not request.state.control_client.can_access(workspace_id, "operate"):
            raise HTTPException(status_code=404, detail="resource_unavailable")
        client_id = request.state.control_client.id
        # Re-query an immutable prior receipt even if its approval has been consumed.
        prior = store.get_client_action(
            client_id, values["action_id"], instance_id=control_instance_id,
        )
        if prior is not None:
            _, receipt, claimed = _control_claim_mutation(
                request, workspace_id, action="workspace.terminals.close",
                values=bound, required=required,
            )
            if not claimed:
                return _quick_control_response(receipt, extra={"terminal_id": terminal_id})
        workspace, terminal = _control_terminal_resource(
            request, workspace_id, terminal_id, operate=True,
        )
        approval_id = values["approval_id"]
        intent = _terminal_close_intent(
            request, workspace_id, terminal_id,
            approval_id if isinstance(approval_id, str) else "",
        )
        if (
            intent["action_id"] != values["action_id"]
            or intent["tmux_window"] != values["expected_tmux_window"]
            or not intent["approved"] or intent["consumed"]
        ):
            raise HTTPException(status_code=409, detail="approval_required")
        _, receipt, claimed = _control_claim_mutation(
            request, workspace_id, action="workspace.terminals.close",
            values=bound, required=required,
        )
        if not claimed:
            return _quick_control_response(receipt, extra={"terminal_id": terminal_id})
        # Human permission is single-use, even for an unknown transport outcome.
        intent["consumed"] = True

        async def owned_close() -> JSONResponse:
            async with terminal_control.input_lock(terminal_id):
                current = store.get_terminal(terminal_id)
                if (
                    current is None or current["workspace_id"] != workspace_id
                    or current.get("role") != "shell"
                    or current["tmux_window"] != intent["tmux_window"]
                    or terminal["tmux_window"] != intent["tmux_window"]
                ):
                    return _terminal_action_result(
                        request, values["action_id"], status="refused", phase="accepted",
                        terminal_id=terminal_id, code="terminal_identity_changed",
                    )
                # Approval alone is not admission: the bearer may be revoked
                # while a close waits behind an already admitted pane write.
                credential = store.get_client_credential(request.state.control_client.id)
                if credential is None or credential["revoked_at"] is not None:
                    return _terminal_action_result(
                        request, values["action_id"], status="refused", phase="accepted",
                        terminal_id=terminal_id, code="credential_revoked",
                    )
                _control_record_action(
                    request, values["action_id"], status="accepted", phase="dispatched",
                    result_resource_id=terminal_id,
                )
                try:
                    if is_remote(workspace):
                        await remote.close_terminal(workspace, current)
                    else:
                        await asyncio.to_thread(terminals.close_terminal, workspace, current)
                except Exception:
                    logging.getLogger(__name__).exception("Control Terminal close outcome unknown")
                    return _terminal_action_result(
                        request, values["action_id"], status="unknown", phase="dispatched",
                        terminal_id=terminal_id, code="unknown_outcome",
                    )
                if store.get_terminal(terminal_id) is not None:
                    return _terminal_action_result(
                        request, values["action_id"], status="unknown", phase="dispatched",
                        terminal_id=terminal_id, code="unknown_outcome",
                    )
                terminal_control.clear_terminal(terminal_id)
                return _terminal_action_result(
                    request, values["action_id"], status="observed", phase="result_verified",
                    terminal_id=terminal_id, extra={"terminal_id": terminal_id, "closed": True},
                )
        return await _control_owned_terminal_mutation(owned_close)

    @app.get("/api/control/v1/actions/{action_id}", response_class=JSONResponse)
    async def control_action_status(request: Request, action_id: str) -> dict[str, Any]:
        client = request.state.control_client
        row = store.get_client_action(client.id, action_id, instance_id=control_instance_id)
        if (
            row is None
            or row["resource_kind"] != "workspace"
            or not client.can_access(str(row["resource_id"]))
        ):
            raise HTTPException(status_code=404, detail="action_not_found")
        return {
            "ok": True,
            "schema_version": 1,
            "action_id": row["action_id"],
            "action": row["action"],
            "resource": {"kind": row["resource_kind"], "id": row["resource_id"]},
            "status": row["status"],
            "phase": row["phase"],
            "result_resource_id": row["result_resource_id"],
            "error_code": row["error_code"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "expires_at": row["expires_at"],
        }

    def _control_relative_path(request: Request) -> str:
        value = request.query_params.get("path", ".")
        if not 1 <= len(value) <= 1024 or "\x00" in value or value.startswith("/"):
            raise HTTPException(status_code=400, detail="request_invalid")
        return value

    def _control_file_entry(entry: Any) -> dict[str, Any]:
        return {
            "name": entry.name, "relative_path": entry.relative_path,
            "is_dir": entry.is_dir, "size": entry.size, "mtime_ns": entry.mtime_ns,
        }

    @app.get("/api/control/v1/workspaces/{workspace_id}/files")
    async def control_files_list(request: Request, workspace_id: str) -> dict[str, Any]:
        workspace = _control_workspace(request, workspace_id)
        path = _control_relative_path(request)
        try:
            directory, entries = await _control_bounded_read(
                request,
                lambda: list_workspace_dir(
                    workspace, path, max_entries=257, max_metadata_bytes=131072,
                )
            )
            if len(entries) > 256:
                raise HTTPException(status_code=413, detail="resource_too_large")
            visible = [
                item for item in entries
                if not _is_internal_state_entry(settings, workspace, path, item.name)
            ]
        except DirectoryListingLimitError as exc:
            raise HTTPException(status_code=413, detail="resource_too_large") from exc
        except (FileNotFoundError, NotADirectoryError, PathBoundaryError, PermissionError) as exc:
            raise HTTPException(status_code=404, detail="resource_unavailable") from exc
        except (SSHBackendError, RemoteAccessError, OSError) as exc:
            raise HTTPException(status_code=503, detail="observation_unavailable") from exc
        return {
            "ok": True, "schema_version": 1, "action": "workspace.files.list",
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "workspace_id": workspace_id, "path": path,
            "source": "filesystem", "entries": [_control_file_entry(e) for e in visible],
            "max_entries": 256, "next_cursor": None,
        }

    @app.get("/api/control/v1/workspaces/{workspace_id}/files/stat")
    async def control_files_stat(request: Request, workspace_id: str) -> dict[str, Any]:
        workspace = _control_workspace(request, workspace_id)
        path = _control_relative_path(request)
        try:
            entry = await _control_bounded_read(
                request,
                lambda: stat_workspace_file(workspace, path)
            )
        except (FileNotFoundError, NotADirectoryError, PathBoundaryError, PermissionError) as exc:
            raise HTTPException(status_code=404, detail="resource_unavailable") from exc
        except (SSHBackendError, RemoteAccessError, OSError) as exc:
            raise HTTPException(status_code=503, detail="observation_unavailable") from exc
        return {
            "ok": True, "schema_version": 1, "action": "workspace.files.stat",
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "workspace_id": workspace_id, "source": "filesystem",
            "entry": _control_file_entry(entry),
        }

    def _control_snapshot_lines(request: Request) -> int:
        raw_lines = request.query_params.get("lines", "100")
        if len(raw_lines) > 8 or not raw_lines.isascii() or not raw_lines.isdecimal():
            raise HTTPException(status_code=400, detail="request_invalid")
        lines = int(raw_lines)
        if not 1 <= lines <= 200:
            raise HTTPException(status_code=400, detail="request_invalid")
        return lines

    def _control_snapshot_fields(
        output: str, mode: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        raw = output.encode("utf-8")
        clipped = raw[-65536:] if len(raw) > 65536 else raw
        return {
            "source": "tmux_capture_pane",
            "snapshot": clipped.decode("utf-8", errors="ignore"),
            "revision": hashlib.sha256(raw).hexdigest(),
            "revision_kind": "snapshot_content_digest",
            "continuity": "snapshot_only_no_event_cursor",
            "truncated": len(raw) > 65536, "max_bytes": 65536,
            "pane": mode["pane"] if mode else None,
            "pane_state": (
                "ended" if mode and mode["dead"] else "alive" if mode else "unknown"
            ),
            "pane_identity_verified": mode is not None,
        }

    @app.get("/api/control/v1/workspaces/{workspace_id}/terminals/{terminal_id}/snapshot")
    async def control_terminal_snapshot(
        request: Request, workspace_id: str, terminal_id: str,
    ) -> dict[str, Any]:
        workspace = _control_workspace(request, workspace_id)
        terminal = store.get_terminal(terminal_id)
        if terminal is None or terminal["workspace_id"] != workspace_id:
            raise HTTPException(status_code=404, detail="resource_unavailable")
        lines = _control_snapshot_lines(request)

        async def observe() -> tuple[str, dict[str, Any] | None]:
            if is_remote(workspace):
                return await remote.observe_scrollback(workspace, terminal, lines=lines)
            return await asyncio.to_thread(
                terminals.observe_scrollback, workspace, terminal, lines=lines,
            )

        try:
            output, mode = await _control_bounded_read(request, observe)
        except (TerminalError, SSHBackendError, RemoteAccessError, OSError) as exc:
            raise HTTPException(status_code=503, detail="observation_unavailable") from exc
        return {
            "ok": True, "schema_version": 1, "action": "terminal.snapshot",
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "workspace_id": workspace_id, "terminal_id": terminal_id,
            **_control_snapshot_fields(output, mode),
        }

    @app.get("/api/control/v1/workspaces/{workspace_id}/files/search")
    async def control_files_search(request: Request, workspace_id: str) -> dict[str, Any]:
        workspace = _control_workspace(request, workspace_id)
        path = _control_relative_path(request)
        query = request.query_params.get("q", "")
        if not 1 <= len(query) <= 128 or not query.strip():
            raise HTTPException(status_code=400, detail="request_invalid")
        try:
            found = await _control_bounded_read(
                request,
                lambda: search_workspace_files(
                    workspace, path, query, include_noise=False,
                )
            )
        except (FileNotFoundError, NotADirectoryError, PathBoundaryError, PermissionError) as exc:
            raise HTTPException(status_code=404, detail="resource_unavailable") from exc
        except (SSHBackendError, RemoteAccessError, OSError) as exc:
            raise HTTPException(status_code=503, detail="observation_unavailable") from exc
        return {
            "ok": True, "schema_version": 1, "action": "workspace.files.search",
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "workspace_id": workspace_id, "path": path,
            "query": query, "source": "filesystem_filename_search",
            "entries": [_control_file_entry(e) for e in found.entries[:100]],
            "scanned_entries": found.scanned_entries,
            "truncated": found.truncated or len(found.entries) > 100,
            "limit": 100,
        }

    @app.get("/api/control/v1/workspaces/{workspace_id}/files/text")
    async def control_files_read(request: Request, workspace_id: str) -> dict[str, Any]:
        workspace = _control_workspace(request, workspace_id)
        path = _control_relative_path(request)
        if path == ".":
            raise HTTPException(status_code=400, detail="request_invalid")

        async def observe() -> FileSnapshot:
            entry = await stat_workspace_file(workspace, path)
            if entry.is_dir or entry.size > 131072:
                raise HTTPException(status_code=413, detail="resource_too_large")
            snapshot = await read_workspace_text(workspace, path)
            if len(snapshot.content.encode("utf-8")) > 131072:
                raise HTTPException(status_code=413, detail="resource_too_large")
            return snapshot

        try:
            snapshot = await _control_bounded_read(request, observe)
        except (FileNotFoundError, NotADirectoryError, PathBoundaryError, PermissionError) as exc:
            raise HTTPException(status_code=404, detail="resource_unavailable") from exc
        except UnsupportedFileError as exc:
            raise HTTPException(status_code=415, detail="file_not_text") from exc
        except (SSHBackendError, RemoteAccessError, OSError) as exc:
            raise HTTPException(status_code=503, detail="observation_unavailable") from exc
        return {
            "ok": True, "schema_version": 1, "action": "workspace.files.read",
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "workspace_id": workspace_id, "relative_path": snapshot.relative_path,
            "content": snapshot.content, "digest": snapshot.digest,
            "mtime_ns": snapshot.mtime_ns, "encoding": "utf-8",
            "max_bytes": 131072, "truncated": False,
        }

    def _control_mutation_path(value: Any, *, allow_root: bool = False) -> str:
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 1024
            or value.startswith("/")
            or "\\" in value
            or any(ord(char) < 32 for char in value)
            or (value != "." and any(part in {"", ".", ".."} for part in value.split("/")))
            or (value == "." and not allow_root)
        ):
            raise HTTPException(status_code=400, detail="request_invalid")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise HTTPException(status_code=400, detail="request_invalid") from exc
        return value

    def _control_file_identity(relative_path: str) -> str:
        # Path identity, not an inode: clients still address files by relative_path.
        return hashlib.sha256(relative_path.encode("utf-8")).hexdigest()

    @app.put("/api/control/v1/workspaces/{workspace_id}/files/text")
    async def control_files_write(request: Request, workspace_id: str) -> JSONResponse:
        values = await _bounded_control_json(request, max_body_bytes=262144)
        required = {
            "action_id", "relative_path", "content",
            "expected_digest", "expected_mtime_ns",
        }
        if set(values) != required:
            raise HTTPException(status_code=400, detail="invalid_fields")
        relative_path = _control_mutation_path(values["relative_path"])
        content = values["content"]
        digest = values["expected_digest"]
        mtime_ns = values["expected_mtime_ns"]
        if not isinstance(content, str) or "\x00" in content:
            raise HTTPException(status_code=400, detail="request_invalid")
        try:
            content_size = len(content.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise HTTPException(status_code=400, detail="request_invalid") from exc
        if content_size > min(131072, settings.max_edit_bytes):
            raise HTTPException(status_code=413, detail="resource_too_large")
        if (
            not isinstance(digest, str) or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or type(mtime_ns) is not int or mtime_ns < 0
        ):
            raise HTTPException(status_code=400, detail="request_invalid")
        workspace, receipt, claimed = _control_claim_mutation(
            request, workspace_id, action="workspace.files.write",
            values=values, required=required,
        )
        extra = {"relative_path": relative_path}
        if not claimed:
            return _quick_control_response(receipt, extra=extra)
        _control_record_action(
            request, values["action_id"], status="accepted", phase="dispatched",
        )
        try:
            snapshot = await write_workspace_text(
                workspace, relative_path, content,
                expected_digest=digest, expected_mtime_ns=mtime_ns,
            )
        except FileConflictError:
            receipt = _control_record_action(
                request, values["action_id"], status="refused", phase="dispatched",
                error_code="file_conflict",
            )
            # Report a fresh observed version, never the submitted draft or file contents.
            try:
                current = await read_workspace_text(workspace, relative_path)
            except (OSError, RemoteAccessError, SSHBackendError, UnsupportedFileError, ValueError):
                return _quick_control_response(receipt, extra=extra)
            return _quick_control_response(receipt, extra={
                **extra, "current_version": {
                    "digest": current.digest, "mtime_ns": current.mtime_ns,
                },
            })
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError,
                PathBoundaryError, PermissionError):
            receipt = _control_record_action(
                request, values["action_id"], status="refused", phase="dispatched",
                error_code="resource_unavailable",
            )
            return _quick_control_response(receipt, extra=extra)
        except UnsupportedFileError:
            receipt = _control_record_action(
                request, values["action_id"], status="refused", phase="dispatched",
                error_code="file_not_text",
            )
            return _quick_control_response(receipt, extra=extra)
        except Exception:
            # Dispatch may have reached SSH/Node or replaced a local file.
            logging.getLogger(__name__).exception("Control file write outcome is unknown")
            receipt = _control_record_action(
                request, values["action_id"], status="unknown", phase="dispatched",
                error_code="unknown_outcome",
            )
            return _quick_control_response(receipt, extra=extra)
        receipt = _control_record_action(
            request, values["action_id"], status="observed", phase="result_verified",
            result_resource_id=_control_file_identity(relative_path),
        )
        return _quick_control_response(receipt, extra={
            **extra, "version": {
                "digest": snapshot.digest, "mtime_ns": snapshot.mtime_ns,
            },
        })

    @app.post("/api/control/v1/workspaces/{workspace_id}/files/create")
    async def control_files_create(request: Request, workspace_id: str) -> JSONResponse:
        values = await _bounded_control_json(request)
        required = {"action_id", "parent", "name"}
        if set(values) != required:
            raise HTTPException(status_code=400, detail="invalid_fields")
        parent = _control_mutation_path(values["parent"], allow_root=True)
        name = values["name"]
        if (
            not isinstance(name, str) or not 1 <= len(name) <= 255
            or name in {".", ".."} or "/" in name or "\\" in name
            or any(ord(char) < 32 for char in name)
        ):
            raise HTTPException(status_code=400, detail="request_invalid")
        relative_path = _control_mutation_path(
            name if parent == "." else f"{parent}/{name}",
        )
        workspace, receipt, claimed = _control_claim_mutation(
            request, workspace_id, action="workspace.files.create",
            values=values, required=required,
        )
        extra = {"relative_path": relative_path}
        if not claimed:
            return _quick_control_response(receipt, extra=extra)
        _control_record_action(
            request, values["action_id"], status="accepted", phase="dispatched",
        )
        try:
            if is_remote(workspace):
                await remote.create(workspace, parent, name, directory=False)
            else:
                # Exclusive creation: never touch/replace a concurrent human-created file.
                await asyncio.to_thread(
                    local_file_call, files.write_new_text, workspace, relative_path, "",
                )
        except FileExistsError:
            receipt = _control_record_action(
                request, values["action_id"], status="refused", phase="dispatched",
                error_code="file_exists",
            )
            return _quick_control_response(receipt, extra=extra)
        except (FileNotFoundError, NotADirectoryError, PathBoundaryError, PermissionError):
            receipt = _control_record_action(
                request, values["action_id"], status="refused", phase="dispatched",
                error_code="resource_unavailable",
            )
            return _quick_control_response(receipt, extra=extra)
        except Exception:
            logging.getLogger(__name__).exception("Control file create outcome is unknown")
            receipt = _control_record_action(
                request, values["action_id"], status="unknown", phase="dispatched",
                error_code="unknown_outcome",
            )
            return _quick_control_response(receipt, extra=extra)
        receipt = _control_record_action(
            request, values["action_id"], status="observed", phase="result_verified",
            result_resource_id=_control_file_identity(relative_path),
        )
        return _quick_control_response(receipt, created=True, extra=extra)

    def _control_page_before(request: Request) -> str | None:
        cursor = request.query_params.get("before")
        if cursor is not None and (
            not 1 <= len(cursor) <= 128
            or any(ord(char) < 32 for char in cursor)
        ):
            raise HTTPException(status_code=400, detail="request_invalid")
        return cursor

    def _control_page_result(
        request: Request, action: str, workspace_id: str,
        items: list[dict[str, Any]], *, key: str,
    ) -> dict[str, Any]:
        has_more = len(items) > 25
        visible = items[:25]
        return {
            "ok": True, "schema_version": 1, "action": action,
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "workspace_id": workspace_id, "items": visible, "limit": 25,
            "next_cursor": str(visible[-1][key]) if has_more else None,
            "pagination": "best_effort_keyset_not_snapshot_isolated",
        }

    def _control_file_run_public(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: row.get(key)
            for key in (
                "id", "state", "relative_path", "terminal_id",
                "created_at", "started_at", "ended_at", "exit_code",
                "error_code", "lifecycle_revision",
            )
        }

    @app.post("/api/control/v1/workspaces/{workspace_id}/file-runs")
    async def control_file_run_start(
        request: Request, workspace_id: str,
    ) -> JSONResponse:
        values = await _bounded_control_json(request)
        required = {"action_id", "relative_path", "expected_digest", "idempotency_key"}
        if set(values) != required:
            raise HTTPException(status_code=400, detail="invalid_fields")
        path = _control_mutation_path(values["relative_path"])
        digest = values["expected_digest"]
        if (
            not isinstance(digest, str) or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise HTTPException(status_code=400, detail="request_invalid")
        try:
            key = uuid.UUID(values["idempotency_key"])
            if key.version != 4 or str(key) != values["idempotency_key"]:
                raise ValueError("File Run key must be canonical UUIDv4")
        except (TypeError, AttributeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="request_invalid") from exc
        workspace, receipt, claimed = _control_claim_mutation(
            request, workspace_id, action="workspace.file_runs.start",
            values=values, required=required,
        )
        extra = {"relative_path": path}
        if not claimed:
            return _quick_control_response(receipt, extra=extra)
        _control_record_action(
            request, values["action_id"], status="accepted", phase="dispatched",
        )
        try:
            run = await asyncio.to_thread(
                file_runs.start, workspace, path,
                expected_digest=digest, idempotency_key=str(key),
            )
        except (FileRunConflict, FileRunError) as exc:
            receipt = _control_record_action(
                request, values["action_id"], status="refused", phase="dispatched",
                error_code=exc.code,
            )
            return _quick_control_response(receipt, extra=extra)
        except FileConflictError:
            receipt = _control_record_action(
                request, values["action_id"], status="refused", phase="dispatched",
                error_code="file_conflict",
            )
            return _quick_control_response(receipt, extra=extra)
        except (UnsupportedFileError, ValueError, PathBoundaryError,
                FileNotFoundError, PermissionError):
            receipt = _control_record_action(
                request, values["action_id"], status="refused", phase="dispatched",
                error_code="source_invalid",
            )
            return _quick_control_response(receipt, extra=extra)
        except Exception:
            logging.getLogger(__name__).exception("Control File Run launch outcome unknown")
            receipt = _control_record_action(
                request, values["action_id"], status="unknown", phase="dispatched",
                error_code="unknown_outcome",
            )
            return _quick_control_response(receipt, extra=extra)

        run_id = str(run["id"])
        state = str(run["state"])
        detail = {**extra, "run": _control_file_run_public(run)}
        if run.get("error_code") == "start_status_unknown":
            receipt = _control_record_action(
                request, values["action_id"], status="unknown", phase="dispatched",
                error_code="start_status_unknown", result_resource_id=run_id,
            )
        elif state == "finished":
            receipt = _control_record_action(
                request, values["action_id"], status="observed", phase="process_finished",
                result_resource_id=run_id,
            )
        elif state == "running":
            receipt = _control_record_action(
                request, values["action_id"], status="accepted", phase="process_started",
                result_resource_id=run_id,
            )
        else:
            receipt = _control_record_action(
                request, values["action_id"], status="accepted", phase="dispatched",
                result_resource_id=run_id,
            )
        return _quick_control_response(receipt, extra=detail)

    @app.get("/api/control/v1/workspaces/{workspace_id}/file-runs")
    async def control_file_runs_list(request: Request, workspace_id: str) -> dict[str, Any]:
        _control_workspace(request, workspace_id)
        cursor = _control_page_before(request)
        before = None
        if cursor is not None:
            prior = store.get_file_run(cursor)
            if prior is None or prior["workspace_id"] != workspace_id:
                raise HTTPException(status_code=410, detail="page_expired")
            before = str(prior["created_at"]), str(prior["id"])
        rows = await _control_bounded_read(
            request,
            lambda: asyncio.to_thread(
                store.list_file_runs_for_workspace, workspace_id,
                limit=26, before=before,
            )
        )
        page = _control_page_result(
            request, "workspace.file_runs.list", workspace_id,
            [_control_file_run_public(row) for row in rows], key="id",
        )
        page["freshness"] = "persisted_without_live_poll"
        return page

    @app.post("/api/control/v1/workspaces/{workspace_id}/file-runs/{run_id}/stop")
    async def control_file_run_stop(
        request: Request, workspace_id: str, run_id: str,
    ) -> JSONResponse:
        values = await _bounded_control_json(request)
        if set(values) != {"action_id", "expected_lifecycle_revision"}:
            raise HTTPException(status_code=400, detail="invalid_fields")
        revision = values["expected_lifecycle_revision"]
        if type(revision) is not int or revision < 0:
            raise HTTPException(status_code=400, detail="invalid_revision")
        _control_workspace(request, workspace_id)
        if not request.state.control_client.can_access(workspace_id, "operate"):
            raise HTTPException(status_code=404, detail="resource_unavailable")
        row = store.get_file_run(run_id) if len(run_id) <= 128 else None
        if row is None or row["workspace_id"] != workspace_id:
            raise HTTPException(status_code=404, detail="resource_unavailable")
        values = {**values, "run_id": run_id}
        _, receipt, claimed = _control_claim_mutation(
            request, workspace_id, action="workspace.file_runs.stop",
            values=values,
            required={"action_id", "expected_lifecycle_revision", "run_id"},
        )
        if not claimed:
            return _quick_control_response(receipt)
        if row["lifecycle_revision"] != revision:
            receipt = _control_record_action(
                request, values["action_id"], status="refused", phase="accepted",
                error_code="revision_conflict",
            )
            return _quick_control_response(receipt)
        _control_record_action(
            request, values["action_id"], status="accepted", phase="dispatched",
            result_resource_id=run_id,
        )
        try:
            result = await asyncio.to_thread(file_runs.stop, run_id)
            stopped_run = result["run"]
            needs_force = bool(result["needs_force"])
        except Exception:
            logging.getLogger(__name__).exception("Control File Run stop outcome unknown")
            receipt = _control_record_action(
                request, values["action_id"], status="unknown", phase="dispatched",
                result_resource_id=run_id, error_code="unknown_outcome",
            )
            return _quick_control_response(receipt)
        stop_verified = (
            not needs_force and stopped_run["state"] in FILE_RUN_TERMINAL_STATES
        )
        receipt = _control_record_action(
            request, values["action_id"],
            status="observed" if stop_verified else "accepted",
            phase="result_verified" if stop_verified else "dispatched",
            result_resource_id=run_id,
        )
        return _quick_control_response(receipt, extra={
            "run": _control_file_run_public(stopped_run),
            "needs_human_force": needs_force,
            "stop_verified": stop_verified,
        })

    @app.get("/api/control/v1/workspaces/{workspace_id}/file-runs/{run_id}")
    async def control_file_run_detail(
        request: Request, workspace_id: str, run_id: str,
    ) -> dict[str, Any]:
        _control_workspace(request, workspace_id)
        if len(run_id) > 128:
            raise HTTPException(status_code=404, detail="resource_unavailable")
        row = store.get_file_run(run_id)
        if row is None or row["workspace_id"] != workspace_id:
            raise HTTPException(status_code=404, detail="resource_unavailable")
        return {
            "ok": True, "schema_version": 1, "action": "workspace.file_runs.describe",
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "workspace_id": workspace_id, "freshness": "persisted_without_live_poll",
            "run": _control_file_run_public(row),
        }

    def _control_granted_target_ids(
        request: Request, *, operate: bool = False,
    ) -> set[str]:
        client = request.state.control_client
        granted = store.terminal_activity_workspaces(sorted(client.workspace_ids))
        return {
            str(row["computer_id"])
            for row in granted
            if row["backend_kind"] == "remote" and row.get("computer_id")
            and (not operate or client.can_access(str(row["id"]), "operate"))
        }

    def _control_remote_run_visible(
        row: Mapping[str, Any] | None, source_workspace_id: str,
        allowed_targets: set[str],
    ) -> bool:
        return bool(
            row is not None
            and row["source_kind"] == "workspace"
            and row["source_workspace_id"] == source_workspace_id
            and row["target_computer_id"] in allowed_targets
        )

    def _control_remote_run_public(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: row.get(key)
            for key in (
                "id", "state", "phase", "exit_code", "lifecycle_revision",
                "created_at", "started_at", "ended_at", "target_computer_id",
            )
        }

    @app.post("/api/control/v1/workspaces/{workspace_id}/remote-runs")
    async def control_remote_run_start(
        request: Request, workspace_id: str,
    ) -> JSONResponse:
        values = await _bounded_control_json(request)
        required = {
            "action_id", "target_computer_id", "command",
            "source_path", "source_options",
        }
        if set(values) != required:
            raise HTTPException(status_code=400, detail="invalid_fields")
        target_id = values["target_computer_id"]
        if (
            not isinstance(target_id, str) or not 1 <= len(target_id) <= 128
            or any(ord(char) < 32 for char in target_id)
        ):
            raise HTTPException(status_code=400, detail="request_invalid")
        source_path = _control_mutation_path(values["source_path"], allow_root=True)
        command = values["command"]
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise HTTPException(status_code=400, detail="invalid_command")
        try:
            command_size = len(command.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise HTTPException(status_code=400, detail="invalid_command") from exc
        if command_size > 4096:
            raise HTTPException(status_code=413, detail="resource_too_large")
        options = values["source_options"]
        if not isinstance(options, dict) or set(options) != {"explicitly_included"}:
            raise HTTPException(status_code=400, detail="invalid_fields")
        included = options["explicitly_included"]
        if (
            not isinstance(included, list) or len(included) > 32
            or any(not isinstance(item, str) for item in included)
        ):
            raise HTTPException(status_code=400, detail="request_invalid")
        for item in included:
            _control_mutation_path(item)
        _control_workspace(request, workspace_id)
        client = request.state.control_client
        if (
            not client.can_access(workspace_id, "operate")
            or target_id not in _control_granted_target_ids(request, operate=True)
        ):
            raise HTTPException(status_code=404, detail="resource_unavailable")
        _, receipt, claimed = _control_claim_mutation(
            request, workspace_id, action="workspace.remote_runs.start",
            values=values, required=required,
        )
        if not claimed:
            return _quick_control_response(receipt, extra={
                "run_id": receipt.get("result_resource_id"),
            })
        run_id = str(uuid.uuid4())
        _control_record_action(
            request, values["action_id"], status="accepted", phase="dispatched",
            result_resource_id=run_id,
        )
        try:
            run, _created = await remote_runs.create({
                "id": run_id, "source_kind": "workspace",
                "source_workspace_id": workspace_id,
                "source_path": source_path, "source_options": options,
                "target_computer_id": target_id, "command": command,
            })
        except Exception:
            # The backend may have accepted the run before its response was lost.
            logging.getLogger(__name__).exception("Control Remote Run launch outcome unknown")
            receipt = _control_record_action(
                request, values["action_id"], status="unknown", phase="dispatched",
                result_resource_id=run_id, error_code="unknown_outcome",
            )
            return _quick_control_response(receipt, extra={"run_id": run_id})
        receipt = _control_record_action(
            request, values["action_id"], status="accepted", phase="dispatched",
            result_resource_id=run_id,
        )
        return _quick_control_response(receipt, extra={
            "run_id": run_id, "run": _control_remote_run_public(run),
            "source_kind": "workspace",
        })

    @app.get("/api/control/v1/workspaces/{workspace_id}/remote-runs")
    async def control_remote_runs_list(request: Request, workspace_id: str) -> dict[str, Any]:
        _control_workspace(request, workspace_id)
        targets = _control_granted_target_ids(request)
        cursor = _control_page_before(request)
        before = None
        if cursor is not None:
            prior = store.get_remote_run(cursor)
            if not _control_remote_run_visible(prior, workspace_id, targets):
                raise HTTPException(status_code=410, detail="page_expired")
            assert prior is not None
            before = str(prior["created_at"]), str(prior["id"])
        rows = await _control_bounded_read(
            request,
            lambda: asyncio.to_thread(
                store.list_scoped_remote_runs, workspace_id, targets,
                limit=26, before=before,
            )
        )
        page = _control_page_result(
            request, "workspace.remote_runs.list", workspace_id,
            [_control_remote_run_public(row) for row in rows], key="id",
        )
        page["freshness"] = "persisted_without_live_poll"
        page["source_scope"] = "persistent_workspace_only"
        page["target_scope"] = "granted_remote_workspace_computers"
        return page

    @app.post("/api/control/v1/workspaces/{workspace_id}/remote-runs/{run_id}/stop")
    async def control_remote_run_stop(
        request: Request, workspace_id: str, run_id: str,
    ) -> JSONResponse:
        values = await _bounded_control_json(request)
        if set(values) != {"action_id", "expected_lifecycle_revision"}:
            raise HTTPException(status_code=400, detail="invalid_fields")
        revision = values["expected_lifecycle_revision"]
        if type(revision) is not int or revision < 0:
            raise HTTPException(status_code=400, detail="invalid_revision")
        _control_workspace(request, workspace_id)
        if not request.state.control_client.can_access(workspace_id, "operate"):
            raise HTTPException(status_code=404, detail="resource_unavailable")
        targets = _control_granted_target_ids(request, operate=True)
        row = store.get_remote_run(run_id) if len(run_id) <= 128 else None
        if not _control_remote_run_visible(row, workspace_id, targets):
            raise HTTPException(status_code=404, detail="resource_unavailable")
        assert row is not None
        values = {**values, "run_id": run_id}
        _, receipt, claimed = _control_claim_mutation(
            request, workspace_id, action="workspace.remote_runs.stop",
            values=values,
            required={"action_id", "expected_lifecycle_revision", "run_id"},
        )
        if not claimed:
            return _quick_control_response(receipt)
        if row["lifecycle_revision"] != revision:
            receipt = _control_record_action(
                request, values["action_id"], status="refused", phase="accepted",
                error_code="revision_conflict",
            )
            return _quick_control_response(receipt)
        _control_record_action(
            request, values["action_id"], status="accepted", phase="dispatched",
            result_resource_id=run_id,
        )
        try:
            result = await asyncio.to_thread(remote_runs.stop, run_id)
            stopped_run = result["run"]
        except Exception:
            logging.getLogger(__name__).exception("Control Remote Run stop outcome unknown")
            receipt = _control_record_action(
                request, values["action_id"], status="unknown", phase="dispatched",
                result_resource_id=run_id, error_code="unknown_outcome",
            )
            return _quick_control_response(receipt)
        stop_verified = (
            bool(result.get("stopped")) and not result.get("needs_kill")
            and not result.get("cancellation_pending")
        )
        receipt = _control_record_action(
            request, values["action_id"],
            status="observed" if stop_verified else "accepted",
            phase="result_verified" if stop_verified else "dispatched",
            result_resource_id=run_id,
        )
        return _quick_control_response(receipt, extra={
            "run": _control_remote_run_public(stopped_run),
            "stop_verified": stop_verified,
            "needs_human_kill": bool(result.get("needs_kill")),
            "cancellation_pending": bool(result.get("cancellation_pending")),
        })

    @app.get("/api/control/v1/workspaces/{workspace_id}/remote-runs/{run_id}")
    async def control_remote_run_detail(
        request: Request, workspace_id: str, run_id: str,
    ) -> dict[str, Any]:
        _control_workspace(request, workspace_id)
        if len(run_id) > 128:
            raise HTTPException(status_code=404, detail="resource_unavailable")
        targets = _control_granted_target_ids(request)
        row = store.get_remote_run(run_id)
        if not _control_remote_run_visible(row, workspace_id, targets):
            raise HTTPException(status_code=404, detail="resource_unavailable")
        assert row is not None
        report = store.remote_run_report_metadata(run_id)
        return {
            "ok": True, "schema_version": 1, "action": "workspace.remote_runs.describe",
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "workspace_id": workspace_id, "freshness": "persisted_without_live_poll",
            "run": _control_remote_run_public(row),
            "review": (
                {"state": "historical_report_not_current_approval",
                 "report_id": report["report_id"],
                 "plan_revision": report["plan_revision"],
                 "recorded_at": report["created_at"]}
                if report is not None else {"state": "no_recorded_report"}
            ),
        }

    @app.get("/api/control/v1/workspaces/{workspace_id}/remote-runs/{run_id}/result/review")
    async def control_remote_run_result_review(
        request: Request, workspace_id: str, run_id: str,
    ) -> dict[str, Any]:
        _control_workspace(request, workspace_id)
        if len(run_id) > 128:
            raise HTTPException(status_code=404, detail="resource_unavailable")
        targets = _control_granted_target_ids(request)
        row = store.get_remote_run(run_id)
        if not _control_remote_run_visible(row, workspace_id, targets):
            raise HTTPException(status_code=404, detail="resource_unavailable")
        try:
            plan = await _control_bounded_read(
                request, lambda: run_results.review(run_id),
            )
        except ResultCollectionError as exc:
            raise HTTPException(status_code=409, detail="review_not_ready") from exc
        except (OSError, RemoteRunError, RemoteAccessError, SSHBackendError) as exc:
            raise HTTPException(status_code=503, detail="observation_unavailable") from exc
        return {
            "ok": True, "schema_version": 1,
            "action": "workspace.remote_runs.result.review",
            "workspace_id": workspace_id, "run_id": run_id,
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "plan": plan.as_dict(),
            "approval": "human_browser_required_for_apply",
        }

    def _control_operable_remote_run(
        request: Request, workspace_id: str, run_id: str,
    ) -> dict[str, Any]:
        _control_workspace(request, workspace_id)
        client = request.state.control_client
        if not client.can_access(workspace_id, "operate") or len(run_id) > 128:
            raise HTTPException(status_code=404, detail="resource_unavailable")
        targets = _control_granted_target_ids(request, operate=True)
        run = store.get_remote_run(run_id)
        if not _control_remote_run_visible(run, workspace_id, targets):
            raise HTTPException(status_code=404, detail="resource_unavailable")
        assert run is not None
        return run

    @app.post("/api/control/v1/workspaces/{workspace_id}/remote-runs/{run_id}/result/apply-intents")
    async def control_remote_run_apply_intent(
        request: Request, workspace_id: str, run_id: str,
    ) -> JSONResponse:
        values = await _bounded_control_json(request)
        if set(values) != {"action_id", "revision"}:
            raise HTTPException(status_code=400, detail="invalid_fields")
        action_id = _control_action_id(values["action_id"])
        revision = values["revision"]
        if (
            not isinstance(revision, str) or len(revision) != 64
            or any(char not in "0123456789abcdef" for char in revision)
        ):
            raise HTTPException(status_code=400, detail="invalid_revision")
        run = _control_operable_remote_run(request, workspace_id, run_id)
        client = request.state.control_client
        _control_apply_intents_prune()
        for approval_id, intent in control_apply_intents.items():
            if intent["client_id"] == client.id and intent["action_id"] == action_id:
                if (
                    intent["run_id"] != run_id or intent["revision"] != revision
                    or intent["workspace_id"] != workspace_id
                    or intent["target_id"] != run["target_computer_id"]
                ):
                    raise HTTPException(status_code=409, detail="idempotency_conflict")
                return JSONResponse({
                    "ok": True, "schema_version": 1, "approval_id": approval_id,
                    "state": "consumed" if intent["consumed"] else
                    "approved" if intent["approved"] else "pending",
                    "expires_at": intent["expires_at"].isoformat(timespec="seconds"),
                    "human_review_url": f"/remote-runs/{run_id}/collect",
                })
        if len(control_apply_intents) >= 64:
            raise HTTPException(status_code=503, detail="approval_capacity_reached")
        approval_id = secrets.token_urlsafe(24)
        expires_at = datetime.now(UTC) + timedelta(minutes=10)
        control_apply_intents[approval_id] = {
            "client_id": client.id, "action_id": action_id,
            "workspace_id": workspace_id, "run_id": run_id,
            "target_id": str(run["target_computer_id"]), "revision": revision,
            "expires_at": expires_at, "approved": False, "consumed": False,
            "plan_hash": None, "approver_session_id": None,
        }
        return JSONResponse({
            "ok": True, "schema_version": 1, "approval_id": approval_id,
            "state": "pending", "expires_at": expires_at.isoformat(timespec="seconds"),
            "human_review_url": f"/remote-runs/{run_id}/collect",
        }, status_code=201)

    @app.get("/api/control/v1/workspaces/{workspace_id}/remote-runs/{run_id}/result/apply-intents/{approval_id}")
    async def control_remote_run_apply_intent_status(
        request: Request, workspace_id: str, run_id: str, approval_id: str,
    ) -> dict[str, Any]:
        _control_operable_remote_run(request, workspace_id, run_id)
        _control_apply_intents_prune()
        intent = control_apply_intents.get(approval_id)
        if (
            intent is None or intent["client_id"] != request.state.control_client.id
            or intent["workspace_id"] != workspace_id or intent["run_id"] != run_id
        ):
            raise HTTPException(status_code=404, detail="resource_unavailable")
        return {
            "ok": True, "schema_version": 1, "approval_id": approval_id,
            "state": "consumed" if intent["consumed"] else
            "approved" if intent["approved"] else "pending",
            "action_id": intent["action_id"], "revision": intent["revision"],
            "expires_at": intent["expires_at"].isoformat(timespec="seconds"),
        }

    @app.post("/api/control/v1/workspaces/{workspace_id}/remote-runs/{run_id}/result/apply")
    async def control_remote_run_result_apply(
        request: Request, workspace_id: str, run_id: str,
    ) -> JSONResponse:
        values = await _bounded_control_json(request)
        if set(values) != {"action_id", "revision", "approval_id"}:
            raise HTTPException(status_code=400, detail="invalid_fields")
        _control_action_id(values["action_id"])
        run = _control_operable_remote_run(request, workspace_id, run_id)
        approval_id = values["approval_id"]
        bound = {**values, "run_id": run_id}
        # A previously claimed action is immutable and replayable even after approval expiry.
        if store.get_client_action(
            request.state.control_client.id, values["action_id"],
            instance_id=control_instance_id,
        ) is not None:
            _, receipt, claimed = _control_claim_mutation(
                request, workspace_id, action="workspace.remote_runs.result.apply",
                values=bound, required={"action_id", "revision", "approval_id", "run_id"},
            )
            if not claimed:
                return _quick_control_response(receipt)
        _control_apply_intents_prune()
        intent = control_apply_intents.get(approval_id) if isinstance(approval_id, str) else None
        if (
            intent is None or intent["client_id"] != request.state.control_client.id
            or intent["workspace_id"] != workspace_id or intent["run_id"] != run_id
            or intent["target_id"] != run["target_computer_id"]
            or intent["action_id"] != values["action_id"]
            or intent["revision"] != values["revision"]
            or not intent["approved"] or intent["consumed"] or not intent["plan_hash"]
        ):
            raise HTTPException(status_code=409, detail="approval_required")
        _, receipt, claimed = _control_claim_mutation(
            request, workspace_id, action="workspace.remote_runs.result.apply",
            values=bound, required={"action_id", "revision", "approval_id", "run_id"},
        )
        if not claimed:
            return _quick_control_response(receipt)
        # The browser approval can authorize at most one attempt.
        intent["consumed"] = True
        try:
            current_plan = await run_results.review(run_id)
        except Exception:
            logging.getLogger(__name__).exception("Approved control apply review unavailable")
            receipt = _control_record_action(
                request, values["action_id"], status="refused", phase="accepted",
                error_code="review_not_ready",
            )
            return _quick_control_response(receipt)
        if (
            current_plan.revision != intent["revision"]
            or _control_apply_plan_hash(current_plan) != intent["plan_hash"]
            or not current_plan.apply_allowed
        ):
            receipt = _control_record_action(
                request, values["action_id"], status="refused", phase="accepted",
                error_code="plan_revision_mismatch",
            )
            return _quick_control_response(receipt)
        # Approval and review may outlive the bearer that requested the apply.
        credential = store.get_client_credential(request.state.control_client.id)
        if credential is None or credential["revoked_at"] is not None:
            receipt = _control_record_action(
                request, values["action_id"], status="refused", phase="accepted",
                error_code="credential_revoked",
            )
            return _quick_control_response(receipt)
        _control_record_action(
            request, values["action_id"], status="accepted", phase="dispatched",
            result_resource_id=run_id,
        )
        try:
            report = await run_results.apply(run_id, intent["revision"])
        except (ResultCollectionConflict, ResultCollectionError) as exc:
            receipt = _control_record_action(
                request, values["action_id"], status="refused", phase="dispatched",
                error_code=exc.code,
            )
            return _quick_control_response(receipt)
        except Exception:
            logging.getLogger(__name__).exception("Approved control apply outcome unknown")
            receipt = _control_record_action(
                request, values["action_id"], status="unknown", phase="dispatched",
                result_resource_id=run_id, error_code="unknown_outcome",
            )
            return _quick_control_response(receipt)
        report_body = report.as_dict()
        report_id: str | None = None
        try:
            report_id = await asyncio.to_thread(
                store.save_remote_run_collection_report, run_id, report_body,
            )
        except (OSError, ValueError, KeyError, sqlite3.Error):
            # Files may already have changed: do not disguise this as a failed apply.
            logging.getLogger(__name__).exception("Approved apply report was not persisted")
        receipt = _control_record_action(
            request, values["action_id"], status="observed", phase="result_verified",
            result_resource_id=report_id or run_id,
        )
        return _quick_control_response(receipt, extra={
            "run_id": run_id, "report_id": report_id,
            "report_persisted": report_id is not None, "report": report_body,
        })

    @app.get("/api/control/v1/workspaces/{workspace_id}/remote-runs/{run_id}/snapshot")
    async def control_remote_run_snapshot(
        request: Request, workspace_id: str, run_id: str,
    ) -> dict[str, Any]:
        _control_workspace(request, workspace_id)
        if len(run_id) > 128:
            raise HTTPException(status_code=404, detail="resource_unavailable")
        targets = _control_granted_target_ids(request)
        run = store.get_remote_run(run_id)
        if not _control_remote_run_visible(run, workspace_id, targets):
            raise HTTPException(status_code=404, detail="resource_unavailable")
        assert run is not None
        lines = _control_snapshot_lines(request)

        # The transient shell is an internal Run-owned bridge, not a client
        # Workspace grant. A read must neither create it nor repair its state.
        transient_id = str(run.get("workspace_id") or "")
        if not transient_id:
            raise HTTPException(status_code=404, detail="resource_unavailable")
        try:
            transient = workspaces.require(transient_id)
        except (KeyError, OSError, PathBoundaryError) as exc:
            raise HTTPException(status_code=404, detail="resource_unavailable") from exc
        if (
            transient.get("workspace_kind") != "remote_run"
            or transient.get("remote_run_id") != run_id
            or transient.get("computer_id") != run["target_computer_id"]
            or transient.get("tmux_session") != f"termroom-run-{run_id}"
        ):
            raise HTTPException(status_code=404, detail="resource_unavailable")
        terminal = store.get_managed_terminal(transient_id, "remote_run")
        if terminal is None or terminal.get("managed_run_id") != run_id:
            raise HTTPException(status_code=404, detail="resource_unavailable")

        try:
            output, mode = await _control_bounded_read(
                request,
                lambda: remote.observe_scrollback(transient, terminal, lines=lines)
            )
        except (TerminalError, SSHBackendError, RemoteAccessError, OSError) as exc:
            raise HTTPException(status_code=503, detail="observation_unavailable") from exc
        return {
            "ok": True, "schema_version": 1,
            "action": "workspace.remote_runs.snapshot",
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "workspace_id": workspace_id, "run_id": run_id,
            "terminal_id": terminal["id"],
            "freshness": "observed_without_run_reconciliation",
            **_control_snapshot_fields(output, mode),
        }

    def _control_workspace_claim_public(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "launch_id": row["launch_id"], "legacy_slot": row["slot"],
            "command_digest": row["command_digest"], "admitted_at": row["created_at"],
            "state": "admission_recorded_execution_outcome_unknown",
        }

    @app.get("/api/control/v1/workspaces/{workspace_id}/workspace-runs")
    async def control_workspace_run_claims(
        request: Request, workspace_id: str,
    ) -> dict[str, Any]:
        _control_workspace(request, workspace_id)
        cursor = _control_page_before(request)
        before = None
        if cursor is not None:
            prior = store.get_workspace_run_claim(workspace_id, cursor)
            if prior is None:
                raise HTTPException(status_code=410, detail="page_expired")
            before = str(prior["created_at"]), str(prior["launch_id"])
        rows = await _control_bounded_read(
            request,
            lambda: asyncio.to_thread(
                store.list_workspace_run_claims, workspace_id,
                limit=26, before=before,
            )
        )
        page = _control_page_result(
            request, "workspace.workspace_runs.list", workspace_id,
            [_control_workspace_claim_public(row) for row in rows], key="launch_id",
        )
        page["record_kind"] = "command_launch_admission_only"
        return page

    @app.get("/api/control/v1/workspaces/{workspace_id}/workspace-runs/{launch_id}")
    async def control_workspace_run_claim_detail(
        request: Request, workspace_id: str, launch_id: str,
    ) -> dict[str, Any]:
        _control_workspace(request, workspace_id)
        if len(launch_id) > 128:
            raise HTTPException(status_code=404, detail="resource_unavailable")
        row = store.get_workspace_run_claim(workspace_id, launch_id)
        if row is None:
            raise HTTPException(status_code=404, detail="resource_unavailable")
        return {
            "ok": True, "schema_version": 1,
            "action": "workspace.workspace_runs.describe",
            "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "workspace_id": workspace_id, "record_kind": "command_launch_admission_only",
            "run": _control_workspace_claim_public(row),
        }

    return None
