from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from termroom.app import create_app
from termroom.config import Settings

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")


def _app(tmp_path: Path):  # type: ignore[no-untyped-def]
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="internal-secret",
        login_password="correct-password",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open(".")
    terminal = app.state.terminals.ensure_workspace(workspace)[0]
    return app, workspace, terminal


def _cleanup(app, workspace) -> None:  # type: ignore[no-untyped-def]
    subprocess.run(
        ["tmux", "kill-session", "-t", str(workspace["tmux_session"])],
        check=False,
        capture_output=True,
    )


def test_terminal_websocket_requires_authentication_and_same_origin(tmp_path: Path) -> None:
    app, workspace, terminal = _app(tmp_path)
    try:
        with TestClient(app, base_url="http://testserver") as client:
            with client.websocket_connect(
                f"/ws/terminal/{terminal['id']}",
                headers={"origin": "http://testserver"},
            ) as websocket, pytest.raises(WebSocketDisconnect) as unauthenticated:
                websocket.receive_text()
            assert unauthenticated.value.code == 4401

            login = client.post("/login", data={"password": "correct-password"})
            assert login.status_code == 200
            with client.websocket_connect(
                f"/ws/terminal/{terminal['id']}",
                headers={"origin": "https://evil.example"},
            ) as websocket, pytest.raises(WebSocketDisconnect) as wrong_origin:
                websocket.receive_text()
            assert wrong_origin.value.code == 4403

            with client.websocket_connect(
                f"/ws/terminal/{terminal['id']}",
                headers={"origin": "http://testserver"},
            ) as websocket:
                websocket.send_json({"kind": "resize", "rows": 41, "cols": 123})
                websocket.send_text("[]")
                websocket.send_json({"kind": "claim"})
    finally:
        _cleanup(app, workspace)


def test_terminal_websocket_closes_when_signed_session_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("termroom.auth.SESSION_MAX_AGE_SECONDS", 1)
    app, workspace, terminal = _app(tmp_path)
    try:
        with TestClient(app, base_url="http://testserver") as client:
            client.post("/login", data={"password": "correct-password"})
            with client.websocket_connect(
                f"/ws/terminal/{terminal['id']}",
                headers={"origin": "http://testserver"},
            ) as websocket:
                time.sleep(1.2)
                closed = False
                for _ in range(20):
                    try:
                        websocket.receive_text()
                    except WebSocketDisconnect as exc:
                        assert exc.code == 4401
                        closed = True
                        break
                assert closed
    finally:
        _cleanup(app, workspace)
