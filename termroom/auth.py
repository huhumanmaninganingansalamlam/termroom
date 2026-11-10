from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
import unicodedata
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from fastapi import Request, WebSocket

from termroom.config import Settings
from termroom.db import StateStore
from termroom.security import secure_compare

SESSION_COOKIE = "termroom_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
SESSION_CLOCK_SKEW_SECONDS = 5 * 60

CONTROL_TOKEN_PATTERN = re.compile(r"^trc1_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})$")
CONTROL_CAPABILITIES = frozenset({"read", "operate"})
MAX_CONTROL_WORKSPACES = 32


class AuthRateLimited(RuntimeError):
    pass


def _session_now() -> int:
    return int(time.time())


class AuthManager:
    """Password login with stateless, per-browser signed sessions."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        material = f"termroom-session\0{settings.login_password}".encode()
        self._session_key = hmac.new(
            settings.access_token.encode(), material, hashlib.sha256
        ).digest()

    def login(self, supplied: str, *, remote_key: str) -> str | None:
        if not self._allow_attempt(remote_key):
            raise AuthRateLimited
        if not secure_compare(supplied, self.settings.login_password):
            return None
        self._attempts.pop(remote_key or "unknown", None)
        issued_at = _session_now()
        nonce = secrets.token_urlsafe(24)
        payload = f"{issued_at}.{nonce}"
        signature = hmac.new(self._session_key, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def request_session(self, request: Request) -> dict[str, Any] | None:
        return self._decode_session(request.cookies.get(SESSION_COOKIE, ""))

    def websocket_session(self, websocket: WebSocket) -> dict[str, Any] | None:
        return self._decode_session(websocket.cookies.get(SESSION_COOKIE, ""))

    def set_session_cookie(self, response: Any, token: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=self.settings.secure_cookie,
            samesite="strict",
            max_age=SESSION_MAX_AGE_SECONDS,
        )

    def clear_session_cookie(self, response: Any) -> None:
        response.delete_cookie(SESSION_COOKIE)

    def _decode_session(self, token: str) -> dict[str, Any] | None:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        issued_raw, nonce, signature = parts
        if not issued_raw or not nonce or not signature:
            return None
        try:
            issued_at = int(issued_raw)
        except ValueError:
            return None
        now = _session_now()
        if issued_at > now + SESSION_CLOCK_SKEW_SECONDS:
            return None
        if now - issued_at > SESSION_MAX_AGE_SECONDS:
            return None
        payload = f"{issued_at}.{nonce}"
        expected = hmac.new(self._session_key, payload.encode(), hashlib.sha256).hexdigest()
        if not secure_compare(signature, expected):
            return None
        return {
            "id": hashlib.sha256(nonce.encode()).hexdigest()[:32],
            "name": "browser",
            "issued_at": issued_at,
            "expires_at": issued_at + SESSION_MAX_AGE_SECONDS,
            "expires_in": max(0, issued_at + SESSION_MAX_AGE_SECONDS - now),
        }

    def _allow_attempt(self, key: str) -> bool:
        now = time.monotonic()
        attempts = self._attempts[key or "unknown"]
        while attempts and now - attempts[0] > 60:
            attempts.popleft()
        if len(attempts) >= 10:
            return False
        attempts.append(now)
        return True


@dataclass(frozen=True, slots=True)
class ControlClient:
    """A revocable, exact-Workspace-scoped non-browser client identity."""

    id: str
    name: str
    capabilities: frozenset[str]
    workspace_ids: frozenset[str]

    def can_access(self, workspace_id: str, capability: str = "read") -> bool:
        return workspace_id in self.workspace_ids and capability in self.capabilities


class ClientCredentialManager:
    """Issue high-entropy bearer secrets; persist only their SHA-256 verifiers."""

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def issue(
        self, *, name: str, capabilities: list[str], workspace_ids: list[str]
    ) -> dict[str, Any]:
        safe_name = name.strip() if isinstance(name, str) else ""
        if (
            not 1 <= len(safe_name) <= 80
            or any(unicodedata.category(char) in {"Cc", "Zl", "Zp"} for char in safe_name)
        ):
            raise ValueError("Client name must be one line of 1 to 80 characters")
        if (
            not isinstance(capabilities, list)
            or not capabilities
            or any(not isinstance(value, str) for value in capabilities)
            or len(set(capabilities)) != len(capabilities)
            or not set(capabilities) <= CONTROL_CAPABILITIES
            or "read" not in capabilities
        ):
            raise ValueError("Capabilities must contain read and optionally operate")
        if (
            not isinstance(workspace_ids, list)
            or not 1 <= len(workspace_ids) <= MAX_CONTROL_WORKSPACES
            or any(not isinstance(value, str) or not value for value in workspace_ids)
            or len(set(workspace_ids)) != len(workspace_ids)
        ):
            raise ValueError("Select 1 to 32 distinct Workspaces")
        for workspace_id in workspace_ids:
            workspace = self.store.get_workspace(workspace_id)
            if workspace is None or workspace.get("workspace_kind") != "workspace":
                raise ValueError("A selected Workspace is no longer available")

        client_id = uuid.uuid4().hex
        token = f"trc1_{client_id}.{secrets.token_urlsafe(32)}"
        verifier = hashlib.sha256(token.encode("ascii")).hexdigest()
        row = self.store.create_client_credential(
            client_id=client_id,
            name=safe_name,
            token_hash=verifier,
            capabilities=sorted(capabilities),
            workspace_ids=workspace_ids,
        )
        return {**self._public(row), "token": token}

    def authenticate(self, authorization: str) -> ControlClient | None:
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer":
            return None
        match = CONTROL_TOKEN_PATTERN.fullmatch(token)
        if match is None:
            return None
        row = self.store.get_client_credential(match.group(1))
        verifier = hashlib.sha256(token.encode("ascii")).hexdigest()
        expected = str(row["token_hash"]) if row is not None else "0" * 64
        if not hmac.compare_digest(expected, verifier):
            return None
        if row is None or row["revoked_at"] is not None:
            return None
        self.store.touch_client_credential(str(row["id"]))
        return ControlClient(
            id=str(row["id"]),
            name=str(row["name"]),
            capabilities=frozenset(row["capabilities"]),
            workspace_ids=frozenset(row["workspace_ids"]),
        )

    def list_public(self) -> list[dict[str, Any]]:
        return [self._public(row) for row in self.store.list_client_credentials()]

    def revoke(self, client_id: str) -> dict[str, Any] | None:
        row = self.store.revoke_client_credential(client_id)
        return self._public(row) if row is not None else None

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        return {
            key: row[key]
            for key in (
                "id",
                "name",
                "capabilities",
                "workspace_ids",
                "created_at",
                "last_used_at",
                "revoked_at",
            )
        }
