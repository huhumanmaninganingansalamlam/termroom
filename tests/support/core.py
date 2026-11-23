"""Concrete isolated HTTP Core lifecycle; never touches a running user Core."""
from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
import uvicorn


def free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@dataclass
class IsolatedCore:
    """A real Uvicorn Core bound only to loopback, with deterministic shutdown."""

    app: Any
    server: uvicorn.Server
    thread: threading.Thread
    base_url: str

    def close(self) -> None:
        self.server.should_exit = True
        if self.thread.is_alive():
            self.thread.join(timeout=8)
        if self.thread.is_alive():
            raise AssertionError(f"Core at {self.base_url} did not shut down")


def start_isolated_core(app: Any, *, timeout: float = 10) -> IsolatedCore:
    port = free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port,
        log_level="error", access_log=False,
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    core = IsolatedCore(app=app, server=server, thread=thread, base_url=base_url)
    try:
        thread.start()
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base_url}/health", timeout=0.3).status_code == 200:
                    return core
            except httpx.HTTPError as exc:
                last_error = exc
            if not thread.is_alive():
                break
            time.sleep(0.05)
        raise AssertionError(
            f"Isolated Core at {base_url} failed readiness; last error={last_error}"
        )
    except BaseException:
        core.close()
        raise
