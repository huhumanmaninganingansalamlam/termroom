"""Direct tests for the concrete real-environment lifecycle helpers."""
from __future__ import annotations

import json
from collections import deque

import httpx
import pytest
from fastapi import FastAPI

from tests.support.chrome import ChromeCdpSession
from tests.support.core import start_isolated_core


class FakeCdpSocket:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = deque(responses)
        self.sent: list[dict[str, object]] = []

    def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def recv(self, *, timeout: float) -> str:
        if not self.responses:
            raise TimeoutError("CDP receive exhausted")
        return json.dumps(self.responses.popleft())


def test_chrome_cdp_sequences_real_events_and_response_ids() -> None:
    events: list[dict[str, object]] = []
    fake = FakeCdpSocket([
        {"method": "Network.webSocketFrameSent", "params": {"frame": 1}},
        {"id": 17, "result": {"unrelated": True}},
        {"id": 1, "result": {"ok": True}},
        {"id": 2, "result": {
            "result": {"value": "한글\nsecond"},
        }},
    ])
    session = ChromeCdpSession(fake, stage="private-A", on_event=events.append)
    assert session.cdp("Page.enable") == {"ok": True}
    assert session.evaluate("'한글\\nsecond'") == "한글\nsecond"
    assert fake.sent == [
        {"id": 1, "method": "Page.enable", "params": {}},
        {"id": 2, "method": "Runtime.evaluate", "params": {
            "expression": "'한글\\nsecond'", "returnByValue": True,
            "awaitPromise": False,
        }},
    ]
    assert [event["method"] for event in events] == ["Network.webSocketFrameSent"]


def test_chrome_cdp_stage_rich_errors_and_no_silent_success() -> None:
    denied = ChromeCdpSession(FakeCdpSocket([
        {"id": 1, "error": {"code": -32000, "message": "Denied"}},
    ]), stage="private-SSH")
    with pytest.raises(AssertionError, match="Chrome private-SSH CDP Runtime.evaluate"):
        denied.evaluate("document.title")
    thrown = ChromeCdpSession(FakeCdpSocket([
        {"id": 1, "result": {"exceptionDetails": {"text": "boom"}}},
    ]), stage="copy-TUI")
    with pytest.raises(AssertionError, match="copy-TUI.*boom"):
        thrown.evaluate("throw new Error('boom')")
    timed_out = ChromeCdpSession(FakeCdpSocket([]), stage="broken-page")
    with pytest.raises(AssertionError, match="broken-page.*response timeout"):
        timed_out.cdp("Page.navigate")


def test_isolated_core_binds_only_loopback_and_shuts_down() -> None:
    app = FastAPI()

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    instance = start_isolated_core(app)
    assert instance.base_url.startswith("http://127.0.0.1:")
    try:
        response = httpx.get(instance.base_url + "/health", timeout=3)
        assert response.json() == {"ok": True}
    finally:
        instance.close()
    assert not instance.thread.is_alive()
