from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from termroom.db import StateStore
from termroom.remote_access import RemoteAccess
from termroom.ssh_backend import SSHBackend
from termroom.terminal_control import TerminalControl
from termroom.terminals import TerminalManager, write_terminal_input


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend_kind", "race", "input_kind"),
    [
        (backend, race, kind)
        for backend, race in [
            ("local", "promotion"),
            ("local", "commit"),
            ("local", "retryable"),
            ("ssh", "promotion"),
            ("ssh", "commit"),
            ("ssh", "retryable"),
            ("node", "commit"),
            ("node", "retryable"),
        ]
        for kind in ("input", "binary")
    ]
    + [("node", "retryable", "command")],
)
async def test_valid_paste_survives_resize_ownership_change_or_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend_kind: str,
    race: str,
    input_kind: str,
) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    root = store.ensure_root(tmp_path)
    workspace = store.create_workspace(root["id"], "project", "project")
    terminal = store.create_terminal(workspace["id"], "shell", "@1")
    terminal_id = str(terminal["id"])
    control = TerminalControl()
    peer = control.register(terminal_id, device_id="other-browser")
    control.mark_grid_existing(terminal_id)
    writer, recipient = socket.socketpair()
    fd = writer.detach()
    recipient.setblocking(False)
    complete = asyncio.Event()
    paste = "\x1b[200~" + ("한글 붙여넣기\r" * 1400)[:11000] + "\x1b[201~z"
    expected = (paste + ("\r" if input_kind == "command" else "")).encode()
    received = bytearray()
    injected = False

    async def receive_bytes() -> None:
        loop = asyncio.get_running_loop()
        while len(received) < len(expected):
            received.extend(await loop.sock_recv(recipient, 65536))
        complete.set()

    receiver = asyncio.create_task(receive_bytes())

    class Browser:
        def __init__(self) -> None:
            payload = {
                "kind": "command" if input_kind == "command" else "input",
                "data": paste,
                "user_input": True,
                "rows": 18,
                "cols": 131,
            }
            self.messages = [
                {"type": "websocket.receive", "text": json.dumps({"kind": "control_claim"})},
                {
                    "type": "websocket.receive",
                    "text": json.dumps({"kind": "resize", "rows": 18, "cols": 131}),
                },
                {"type": "websocket.receive", "bytes": expected}
                if input_kind == "binary"
                else {"type": "websocket.receive", "text": json.dumps(payload)},
            ]

        async def receive(self) -> dict[str, object]:
            if self.messages:
                return self.messages.pop(0)
            await asyncio.wait_for(complete.wait(), 2)
            assert control.can_resize(terminal_id, peer) is (race != "retryable")
            return {"type": "websocket.disconnect", "code": 1000}

        async def send_bytes(self, _data: bytes) -> None:
            pass

        async def send_text(self, _data: str) -> None:
            pass

        async def close(self, *, code: int, reason: str) -> None:
            raise AssertionError((code, reason))

    def grid_role(*_args: object, enabled: bool, **_kwargs: object) -> bool:
        nonlocal injected
        if enabled and not injected and race in {"promotion", "retryable"}:
            injected = True
            if race == "retryable":
                return False
            control.mark_input(terminal_id, peer, "other-browser")
        return True

    def view_size(*_args: object, **_kwargs: object) -> bool:
        nonlocal injected
        if race == "commit" and not injected:
            injected = True
            control.mark_input(terminal_id, peer, "other-browser")
        return True

    mode = {
        "session": workspace["tmux_session"],
        "window": "@1",
        "pane": "%1",
        "pane_pid": 123,
        "alternate": False,
        "mouse_tracking": False,
        "mouse_flags": [0] * 5,
    }
    monkeypatch.setattr(os, "killpg", lambda *_args: None)
    if backend_kind in {"local", "ssh"}:
        if backend_kind == "local":
            backend = TerminalManager(store, control=control)
            monkeypatch.setattr(backend, "_setup_browser_terminal", lambda *_args: (999999, fd))
            monkeypatch.setattr(backend, "_set_browser_view_grid_resize", grid_role)
            monkeypatch.setattr(backend, "_wait_browser_view_size", view_size)
            monkeypatch.setattr(
                backend, "_forget_browser_grid_owner", lambda *_args, **_kwargs: None
            )
            monkeypatch.setattr(
                backend, "_finish_browser_terminal", lambda master_fd, _view: os.close(master_fd)
            )
        else:
            backend = SSHBackend(store, tmp_path, control=control)
            monkeypatch.setattr(backend, "ensure_workspace", lambda *_args: [])
            monkeypatch.setattr(backend, "_spawn_ssh_tmux_client", lambda *_args: (999999, fd))
            monkeypatch.setattr(backend, "_set_ssh_browser_view_grid_resize", grid_role)
            monkeypatch.setattr(backend, "_wait_ssh_browser_view_size", view_size)
            monkeypatch.setattr(
                backend, "_forget_ssh_browser_grid_owner", lambda *_args, **_kwargs: None
            )
        monkeypatch.setattr(backend, "_set_window_size", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(backend, "_wait_for_pid", lambda *_args: True)
        monkeypatch.setattr(backend, "current_pane_mode", lambda *_args: mode)
    else:

        class Stream:
            stream_id = "a" * 32

            async def receive_event(self) -> None:
                await asyncio.Event().wait()

            async def send(self, value: bytes) -> None:
                await write_terminal_input(fd, value)

            async def close(self) -> None:
                os.close(fd)

        class Connection:
            async def open_stream(self, *_args: object) -> tuple[dict[str, bool], Stream]:
                return {"bootstrap_grid": False}, Stream()

            async def request(
                self, _operation: str, payload: dict[str, object]
            ) -> dict[str, object]:
                nonlocal injected
                result = {
                    **payload,
                    "ok": True,
                    "shared_applied": payload["affects_grid"],
                    "viewport_applied": True,
                    "passive_restored": not payload["affects_grid"],
                    "bootstrap_consumed": False,
                    "grid_active": payload["affects_grid"],
                    "retryable": False,
                    "cleanup_confirmed": True,
                }
                if payload["affects_grid"] and not injected:
                    injected = True
                    if race == "retryable":
                        result.update(
                            ok=False,
                            shared_applied=False,
                            viewport_applied=False,
                            grid_active=False,
                            retryable=True,
                        )
                    else:
                        control.mark_input(terminal_id, peer, "other-browser")
                return result

        workspace["computer"] = {"id": "fixture-node", "connection_method": "node"}
        workspace["canonical_path"] = str(tmp_path)
        backend = RemoteAccess(
            store, object(), SimpleNamespace(connection=lambda _id: Connection()), control
        )  # type: ignore[arg-type]

        async def ensure_workspace(_workspace: object) -> list[object]:
            return []

        monkeypatch.setattr(backend, "ensure_workspace", ensure_workspace)

    try:
        await asyncio.wait_for(
            backend.bridge(Browser(), workspace, terminal, device_id="main-browser"), 4
        )  # type: ignore[arg-type]
        assert injected
        assert bytes(received) == expected
    finally:
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        recipient.close()
        with contextlib.suppress(OSError):
            os.close(fd)
        control.unregister(terminal_id, peer)
