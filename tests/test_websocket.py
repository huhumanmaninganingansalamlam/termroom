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
            with (
                client.websocket_connect(
                    f"/ws/terminal/{terminal['id']}",
                    headers={"origin": "http://testserver"},
                ) as websocket,
                pytest.raises(WebSocketDisconnect) as unauthenticated,
            ):
                websocket.receive_text()
            assert unauthenticated.value.code == 4401

            login = client.post("/login", data={"password": "correct-password"})
            assert login.status_code == 200
            with (
                client.websocket_connect(
                    f"/ws/terminal/{terminal['id']}",
                    headers={"origin": "https://evil.example"},
                ) as websocket,
                pytest.raises(WebSocketDisconnect) as wrong_origin,
            ):
                websocket.receive_text()
            assert wrong_origin.value.code == 4403

            with client.websocket_connect(
                f"/ws/terminal/{terminal['id']}",
                headers={"origin": "http://testserver"},
            ) as websocket:
                websocket.send_json({"kind": "resize", "rows": 41, "cols": 123})
                websocket.send_text("[]")
    finally:
        _cleanup(app, workspace)


def test_terminal_websocket_writes_ascii_and_unicode_input_to_real_tmux(tmp_path: Path) -> None:
    app, workspace, terminal = _app(tmp_path)
    marker = "TERMROOM_INPUT_한글"
    try:
        with TestClient(app, base_url="http://testserver") as client:
            login = client.post("/login", data={"password": "correct-password"})
            assert login.status_code == 200
            with client.websocket_connect(
                f"/ws/terminal/{terminal['id']}",
                headers={"origin": "http://testserver"},
            ) as websocket:
                assert isinstance(websocket.receive_text(), str)
                websocket.send_json(
                    {
                        "kind": "input",
                        "data": f"printf '%s\\n' '{marker}'\r",
                        "rows": 24,
                        "cols": 80,
                        "user_input": True,
                    }
                )

                deadline = time.monotonic() + 3
                scrollback = ""
                while time.monotonic() < deadline:
                    scrollback = app.state.terminals.capture_scrollback(workspace, terminal)
                    if marker in scrollback:
                        break
                    time.sleep(0.05)

                assert marker in scrollback
    finally:
        _cleanup(app, workspace)


def test_terminal_binary_input_takes_over_but_raw_text_stays_passive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, workspace, terminal = _app(tmp_path)
    control = app.state.terminals.control
    registered: list[str] = []
    original_register = control.register

    def record_registration(terminal_id: str) -> str:
        client_id = original_register(terminal_id)
        registered.append(client_id)
        return client_id

    monkeypatch.setattr(control, "register", record_registration)
    terminal_id = str(terminal["id"])
    marker = "TERMROOM_RAW_TEXT_PASSIVE"
    try:
        with TestClient(app, base_url="http://testserver") as client:
            client.post("/login", data={"password": "correct-password"})
            headers = {"origin": "http://testserver"}
            with client.websocket_connect(
                f"/ws/terminal/{terminal_id}", headers=headers
            ) as first:
                first.receive_text()
                with client.websocket_connect(
                    f"/ws/terminal/{terminal_id}", headers=headers
                ) as second:
                    second.receive_text()
                    assert len(registered) == 2
                    first_id, second_id = registered

                    first.send_json(
                        {
                            "kind": "input",
                            "data": "",
                            "rows": 24,
                            "cols": 80,
                            "user_input": True,
                        }
                    )
                    deadline = time.monotonic() + 2
                    while not control.can_resize(terminal_id, first_id):
                        assert time.monotonic() < deadline
                        time.sleep(0.01)
                    revision = control.presence(terminal_id)["input_revision"]

                    second.send_text(f"printf '%s\\n' '{marker}'\r")
                    deadline = time.monotonic() + 2
                    while marker not in app.state.terminals.capture_scrollback(
                        workspace, terminal
                    ):
                        assert time.monotonic() < deadline
                        time.sleep(0.01)
                    assert control.can_resize(terminal_id, first_id)
                    assert not control.can_resize(terminal_id, second_id)
                    assert control.presence(terminal_id)["input_revision"] == revision

                    second.send_bytes(b"")
                    deadline = time.monotonic() + 2
                    while not control.can_resize(terminal_id, second_id):
                        assert time.monotonic() < deadline
                        time.sleep(0.01)
                    assert not control.can_resize(terminal_id, first_id)
                    assert control.presence(terminal_id)["input_revision"] == revision + 1
    finally:
        _cleanup(app, workspace)


def test_terminal_passive_view_resizes_pty_without_controlling_shared_grid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, workspace, terminal = _app(tmp_path)
    applied_sizes: list[tuple[int, int]] = []
    grid_roles: list[bool] = []

    def record_size(_fd: int, *, rows: int, cols: int) -> None:
        applied_sizes.append((rows, cols))

    def record_grid_role(_view_session: str, *, enabled: bool) -> bool:
        grid_roles.append(enabled)
        return True

    monkeypatch.setattr(app.state.terminals, "_set_window_size", record_size)
    monkeypatch.setattr(
        app.state.terminals,
        "_set_browser_view_grid_resize",
        record_grid_role,
    )
    try:
        with TestClient(app, base_url="http://testserver") as client:
            client.post("/login", data={"password": "correct-password"})
            headers = {"origin": "http://testserver"}
            with client.websocket_connect(
                f"/ws/terminal/{terminal['id']}", headers=headers
            ) as first:
                first.receive_text()
                with client.websocket_connect(
                    f"/ws/terminal/{terminal['id']}", headers=headers
                ) as second:
                    second.receive_text()
                    second.send_json({"kind": "resize", "rows": 22, "cols": 66})
                    deadline = time.monotonic() + 2
                    while (22, 66) not in applied_sizes and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert (22, 66) in applied_sizes

                    before_same_size_input = list(applied_sizes)
                    second.send_json(
                        {
                            "kind": "input",
                            "data": "",
                            "rows": 22,
                            "cols": 66,
                            "user_input": False,
                        }
                    )
                    time.sleep(0.05)
                    assert applied_sizes == before_same_size_input

                    before_legacy_input = list(applied_sizes)
                    second.send_json({"kind": "input", "data": ""})
                    time.sleep(0.05)
                    assert applied_sizes == before_legacy_input

                    first.send_json({"kind": "resize", "rows": 37, "cols": 111})
                    deadline = time.monotonic() + 2
                    while (37, 111) not in applied_sizes and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert applied_sizes.count((37, 111)) == 1

                    first.send_json(
                        {
                            "kind": "input",
                            "data": "",
                            "rows": 37,
                            "cols": 111,
                            "user_input": True,
                        }
                    )
                    deadline = time.monotonic() + 2
                    while (37, 111) not in applied_sizes and time.monotonic() < deadline:
                        time.sleep(0.01)
                    while applied_sizes.count((37, 111)) < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert applied_sizes.count((37, 111)) == 2

                    second.send_json({"kind": "resize", "rows": 30, "cols": 90})
                    deadline = time.monotonic() + 2
                    while (30, 90) not in applied_sizes and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert (30, 90) in applied_sizes
                    second.send_json(
                        {
                            "kind": "input",
                            "data": "",
                            "rows": 30,
                            "cols": 90,
                            "user_input": True,
                        }
                    )
                    deadline = time.monotonic() + 2
                    while applied_sizes.count((30, 90)) < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    assert applied_sizes.count((30, 90)) == 2
                    # Promotion now demotes the previous tmux peer atomically inside
                    # the True transition, so the passive bridge does not need a
                    # later, separate False transition of its own.
                    assert grid_roles == [True, True]
    finally:
        _cleanup(app, workspace)


def test_terminal_websocket_closes_when_signed_session_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("termroom.auth.SESSION_MAX_AGE_SECONDS", 1)
    issued_at = 1_800_000_000
    monkeypatch.setattr("termroom.auth._session_now", lambda: issued_at)
    app, workspace, terminal = _app(tmp_path)
    try:
        with TestClient(app, base_url="http://testserver") as client:
            client.post("/login", data={"password": "correct-password"})
            monkeypatch.setattr("termroom.auth._session_now", lambda: issued_at + 1)
            with client.websocket_connect(
                f"/ws/terminal/{terminal['id']}",
                headers={"origin": "http://testserver"},
            ) as websocket:
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
