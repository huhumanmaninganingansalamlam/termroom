"""Isolated headless Chrome and CDP transport for real Termroom tests.

This module owns *only* test Chrome profiles and the CDP protocol mechanics.
Scenario-specific assertions and browser actions stay in the calling tests.
"""
from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
from websockets.sync.client import connect as websocket_connect

ChromeEventObserver = Callable[[dict[str, Any]], None]


class ChromeCdpSession:
    """One CDP page socket; all responses and events share its sequencing."""

    def __init__(
        self, socket: Any, *, stage: str, on_event: ChromeEventObserver | None = None,
    ) -> None:
        self.socket = socket
        self.stage = stage
        self.on_event = on_event
        self._sequence = 0

    def cdp(
        self, method: str, params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._sequence += 1
        request_id = self._sequence
        self.socket.send(json.dumps({
            "id": request_id, "method": method, "params": params or {},
        }))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                response = json.loads(
                    self.socket.recv(timeout=max(0.05, deadline - time.monotonic()))
                )
            except TimeoutError:
                break
            if response.get("method") and self.on_event:
                self.on_event(response)
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise AssertionError(
                    f"Chrome {self.stage} CDP {method}: {response['error']}"
                )
            return response.get("result", {})
        raise AssertionError(f"Chrome {self.stage} CDP {method}: response timeout")

    def evaluate(self, expression: str, *, await_promise: bool = False) -> Any:
        result = self.cdp("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        if "exceptionDetails" in result:
            raise AssertionError(
                f"Chrome {self.stage} Runtime.evaluate: "
                f"{result['exceptionDetails']} in {expression[:120]}"
            )
        return result.get("result", {}).get("value")


@contextlib.contextmanager
def open_chrome_page(
    profile: Path,
    *,
    on_event: ChromeEventObserver | None = None,
) -> Iterator[ChromeCdpSession]:
    """Launch a disposable Chrome and CDP page; never attach to a user's Chrome.

    The temp directory is owned by the caller, and the child process group is
    always terminated on normal exit, failure or cancelled setup.
    """
    profile.mkdir()
    chrome = subprocess.Popen(
        [
            "google-chrome", "--headless=new", "--disable-gpu",
            "--no-first-run", "--no-default-browser-check",
            "--remote-debugging-port=0",
            "--remote-allow-origins=http://127.0.0.1",
            f"--user-data-dir={profile}", "about:blank",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        port_file = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 10
        while not port_file.exists() and time.monotonic() < deadline:
            if chrome.poll() is not None:
                raise AssertionError(
                    f"Chrome profile {profile}: exited early ({chrome.returncode})"
                )
            time.sleep(0.05)
        if not port_file.exists():
            raise AssertionError(f"Chrome profile {profile}: debugger never became ready")
        debug_port = int(port_file.read_text(encoding="utf-8").splitlines()[0])
        page = httpx.put(
            f"http://127.0.0.1:{debug_port}/json/new?about:blank", timeout=5,
        ).json()
        with websocket_connect(
            page["webSocketDebuggerUrl"],
            origin="http://127.0.0.1",
            open_timeout=5,
        ) as socket:
            yield ChromeCdpSession(
                socket, stage=str(profile.name), on_event=on_event,
            )
    finally:
        if chrome.poll() is None:
            os.killpg(chrome.pid, signal.SIGTERM)
            try:
                chrome.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(chrome.pid, signal.SIGKILL)
                chrome.wait(timeout=5)
