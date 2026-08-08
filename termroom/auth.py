from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import Request, WebSocket

from termroom.config import Settings
from termroom.security import secure_compare

SESSION_COOKIE = "termroom_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
SESSION_CLOCK_SKEW_SECONDS = 5 * 60


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
