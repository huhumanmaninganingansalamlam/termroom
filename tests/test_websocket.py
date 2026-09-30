from __future__ import annotations

import contextlib
import os
import shlex
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
                assert isinstance(websocket.receive_text(), str)
                websocket.send_json({"kind": "resize", "rows": 41, "cols": 123})
                websocket.send_text("[]")
            with client.websocket_connect(
                f"/ws/terminal/{terminal['id']}",
                headers={
                    "host": "termroom.example",
                    "origin": "https://termroom.example",
                },
            ) as websocket:
                assert isinstance(websocket.receive_text(), str)
                websocket.send_json({"kind": "resize", "rows": 42, "cols": 124})
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


def test_terminal_presence_deduplicates_tabs_in_same_session(tmp_path: Path) -> None:
    app, workspace, terminal = _app(tmp_path)
    terminal_id = str(terminal["id"])
    try:
        with TestClient(app, base_url="http://testserver") as client:
            login = client.post("/login", data={"password": "correct-password"})
            assert login.status_code == 200
            headers = {"origin": "http://testserver"}

            with client.websocket_connect(f"/ws/terminal/{terminal_id}", headers=headers) as first:
                first.receive_text()
                assert client.get(f"/api/terminals/{terminal_id}/presence").json()["count"] == 1

                with client.websocket_connect(
                    f"/ws/terminal/{terminal_id}", headers=headers
                ) as second:
                    second.receive_text()
                    assert app.state.terminals.control.client_count(terminal_id) == 2
                    assert client.get(f"/api/terminals/{terminal_id}/presence").json()["count"] == 1

                assert client.get(f"/api/terminals/{terminal_id}/presence").json()["count"] == 1
    finally:
        _cleanup(app, workspace)


def test_terminal_binary_input_takes_over_but_raw_text_stays_passive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, workspace, terminal = _app(tmp_path)
    control = app.state.terminals.control
    registered: list[str] = []
    original_register = control.register

    def record_registration(terminal_id: str, *, device_id: str = "") -> str:
        client_id = original_register(terminal_id, device_id=device_id)
        registered.append(client_id)
        return client_id

    monkeypatch.setattr(control, "register", record_registration)
    terminal_id = str(terminal["id"])
    marker = "TERMROOM_RAW_TEXT_PASSIVE"
    try:
        with TestClient(app, base_url="http://testserver") as client:
            client.post("/login", data={"password": "correct-password"})
            headers = {"origin": "http://testserver"}
            with client.websocket_connect(f"/ws/terminal/{terminal_id}", headers=headers) as first:
                first.receive_text()
                with client.websocket_connect(
                    f"/ws/terminal/{terminal_id}", headers=headers
                ) as second:
                    second.receive_text()
                    assert len(registered) == 2
                    first_id, second_id = registered

                    input_revision = control.presence(terminal_id)["input_revision"]
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
                    while control.presence(terminal_id)["input_revision"] == input_revision:
                        assert time.monotonic() < deadline
                        time.sleep(0.01)
                    assert control.can_resize(terminal_id, first_id)
                    revision = control.presence(terminal_id)["input_revision"]

                    second.send_text(f"printf '%s\\n' '{marker}'\r")
                    deadline = time.monotonic() + 2
                    while marker not in app.state.terminals.capture_scrollback(workspace, terminal):
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
    monkeypatch.setattr(app.state.terminals, "_wait_browser_view_size", lambda *a, **kw: True)
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
                    time.sleep(0.05)
                    # Completed bootstrap is passive. Input promotes its role,
                    # but the already-applied identical dimensions are a no-op.
                    assert applied_sizes.count((37, 111)) == 1

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
                    assert grid_roles == [True, False, True, True]
    finally:
        _cleanup(app, workspace)


def test_terminal_one_shot_bootstrap_passive_resize_input_and_reconnect(tmp_path: Path) -> None:
    app, workspace, terminal = _app(tmp_path)
    manager = app.state.terminals
    terminal_id = str(terminal["id"])
    ready = tmp_path / "ready"
    effect = tmp_path / "effect"
    command = (
        f"printf ready > {shlex.quote(str(ready))}; "
        f"IFS= read -r -n 1 value; printf '%s' \"$value\" > {shlex.quote(str(effect))}"
    )
    manager._run_tmux(
        "send-keys",
        "-t",
        str(terminal["tmux_window"]),
        "-l",
        f"/bin/bash --noprofile --norc -c {shlex.quote(command)}",
    )
    manager._run_tmux("send-keys", "-t", str(terminal["tmux_window"]), "Enter")

    def wait_for(predicate) -> None:  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("Terminal state did not settle")

    def grid() -> str:
        return manager._run_tmux(
            "display-message",
            "-p",
            "-t",
            terminal["tmux_window"],
            "#{window_width}x#{window_height}",
        ).stdout.strip()

    try:
        wait_for(ready.exists)
        with TestClient(app, base_url="http://testserver") as client:
            client.post("/login", data={"password": "correct-password"})
            headers = {"origin": "http://testserver"}
            with client.websocket_connect(f"/ws/terminal/{terminal_id}", headers=headers) as ws:
                ws.receive_text()
                owner = manager.control._clients[terminal_id][0]
                # tmux reserves one status row: viewport 124x23 -> pane 124x22.
                ws.send_json({"kind": "resize", "rows": 23, "cols": 124})
                wait_for(
                    lambda: (
                        grid() == "124x22" and not manager.control.can_resize(terminal_id, owner)
                    )
                )
                assert manager.control.presence(terminal_id)["input_revision"] == 0
                ws.send_json({"kind": "resize", "rows": 18, "cols": 124})
                ws.send_json({"kind": "resize", "rows": 18, "cols": 124})
                time.sleep(0.1)
                assert grid() == "124x22"
                assert (
                    "ignore-size"
                    in manager._run_tmux(
                        "list-clients", "-t", f"termroom-view-{owner}", "-F", "#{client_flags}"
                    ).stdout
                )
                ws.send_json(
                    {"kind": "input", "data": "z", "rows": 18, "cols": 124, "user_input": True}
                )
                wait_for(lambda: effect.exists() and grid() == "124x17")
                assert effect.read_text() == "z"
                assert manager.control.presence(terminal_id)["input_revision"] == 1
                assert manager.control.can_resize(terminal_id, owner)
            with client.websocket_connect(f"/ws/terminal/{terminal_id}", headers=headers) as ws:
                ws.receive_text()
                ws.send_json({"kind": "resize", "rows": 30, "cols": 90})
                time.sleep(0.1)
                assert grid() == "124x17"
                assert effect.read_text() == "z"
    finally:
        _cleanup(app, workspace)


@pytest.mark.parametrize("failure", ["enable", "demote"])
def test_terminal_bootstrap_role_failure_retry_or_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    app, workspace, terminal = _app(tmp_path)
    manager = app.state.terminals
    terminal_id = str(terminal["id"])
    original = manager._set_browser_view_grid_resize
    attempts: list[bool] = []

    def role(view: str, *, enabled: bool) -> bool:
        attempts.append(enabled)
        if failure == "enable" and len(attempts) == 1:
            return False
        if failure == "demote" and not enabled:
            return False
        return original(view, enabled=enabled)

    monkeypatch.setattr(manager, "_set_browser_view_grid_resize", role)
    try:
        with TestClient(app, base_url="http://testserver") as client:
            client.post("/login", data={"password": "correct-password"})
            headers = {"origin": "http://testserver"}
            expected_error = (
                pytest.raises(WebSocketDisconnect)
                if failure == "demote"
                else contextlib.nullcontext()
            )
            with (
                expected_error,
                client.websocket_connect(f"/ws/terminal/{terminal_id}", headers=headers) as ws,
            ):
                ws.receive_text()
                ws.send_json({"kind": "resize", "rows": 23, "cols": 124})
                deadline = time.monotonic() + 3
                while not attempts and time.monotonic() < deadline:
                    time.sleep(0.01)
                if failure == "enable":
                    assert manager.control.can_resize(
                        terminal_id, manager.control._clients[terminal_id][0]
                    )
                    ws.send_json({"kind": "resize", "rows": 23, "cols": 124})
                deadline = time.monotonic() + 3
                while False not in attempts and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert False in attempts
                if failure == "demote":
                    while True:
                        ws.receive_text()
            with client.websocket_connect(f"/ws/terminal/{terminal_id}", headers=headers) as ws:
                ws.receive_text()
                assert not manager.control.can_resize(
                    terminal_id, manager.control._clients[terminal_id][0]
                )
                ws.send_json({"kind": "resize", "rows": 18, "cols": 124})
                time.sleep(0.1)
                size = manager._run_tmux(
                    "display-message",
                    "-p",
                    "-t",
                    terminal["tmux_window"],
                    "#{window_width}x#{window_height}",
                ).stdout.strip()
                assert size == "124x22"
    finally:
        _cleanup(app, workspace)


@pytest.mark.parametrize("binary", [False, True])
def test_terminal_input_waits_for_grid_promotion_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, binary: bool
) -> None:
    app, workspace, terminal = _app(tmp_path)
    manager = app.state.terminals
    terminal_id = str(terminal["id"])
    effect = tmp_path / "effect"
    ready = tmp_path / "ready"
    command = "bash -c " + shlex.quote(
        f"printf ready > {shlex.quote(str(ready))}; "
        f'read -r -n1 value; printf %s "$value" > {shlex.quote(str(effect))}'
    )
    manager._run_tmux("send-keys", "-t", terminal["tmux_window"], command, "Enter")
    original_role = manager._set_browser_view_grid_resize
    original_write = os.write
    failed = False
    writes: list[bytes] = []

    def grid() -> str:
        return manager._run_tmux(
            "display-message",
            "-p",
            "-t",
            terminal["tmux_window"],
            "#{window_width}x#{window_height}",
        ).stdout.strip()

    def role(view: str, *, enabled: bool) -> bool:
        nonlocal failed
        if enabled and not failed and manager.control.presence(terminal_id)["input_revision"]:
            failed = True
            return False
        return original_role(view, enabled=enabled)

    def write(fd: int, data: bytes) -> int:
        if data == b"z":
            assert grid() == "124x17"
            writes.append(data)
        return original_write(fd, data)

    monkeypatch.setattr(manager, "_set_browser_view_grid_resize", role)
    monkeypatch.setattr(os, "write", write)
    try:
        with TestClient(app, base_url="http://testserver") as client:
            client.post("/login", data={"password": "correct-password"})
            with client.websocket_connect(
                f"/ws/terminal/{terminal_id}", headers={"origin": "http://testserver"}
            ) as ws:
                ws.receive_text()
                ws.send_json({"kind": "resize", "rows": 23, "cols": 124})
                deadline = time.monotonic() + 3
                while (grid() != "124x22" or not ready.exists()) and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert grid() == "124x22" and ready.exists()
                ws.send_json({"kind": "resize", "rows": 18, "cols": 124})
                payload = {
                    "kind": "input",
                    "data": "z",
                    "rows": 18,
                    "cols": 124,
                    "user_input": True,
                }
                if binary:
                    ws.send_bytes(b"z")
                else:
                    ws.send_json(payload)
                deadline = time.monotonic() + 3
                while not failed and time.monotonic() < deadline:
                    time.sleep(0.01)
                time.sleep(0.1)
                assert failed and not effect.exists() and not writes
                assert grid() == "124x22"
                assert manager.control.presence(terminal_id)["input_revision"] == 1
                if binary:
                    ws.send_bytes(b"z")
                else:
                    ws.send_json(payload)
                deadline = time.monotonic() + 3
                while not effect.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert effect.read_text() == "z" and writes == [b"z"]
                assert manager.control.presence(terminal_id)["input_revision"] == 2
                ws.send_json({"kind": "resize", "rows": 18, "cols": 124})
                time.sleep(0.1)
                assert grid() == "124x17" and writes == [b"z"]
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
