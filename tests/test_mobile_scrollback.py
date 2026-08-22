from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from termroom.app import create_app
from termroom.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_base_loads_mobile_scrollback_assets() -> None:
    template = (ROOT / "termroom/templates/base.html").read_text(encoding="utf-8")
    terminal_template = (ROOT / "termroom/templates/terminal.html").read_text(
        encoding="utf-8"
    )

    assert "mobile_scrollback.css') }}?v=11" in template
    assert "mobile_scrollback.js') }}?v=23\" defer" in template
    assert "__termroomTerminalOutputHookInstalled" not in template
    assert "terminal.js') }}?v=51" in terminal_template


@pytest.mark.asyncio
async def test_mobile_scrollback_assets_are_served(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        page = await client.get("/")
        stylesheet = await client.get("/static/mobile_scrollback.css?v=11")
        script = await client.get("/static/mobile_scrollback.js?v=23")

    assert page.status_code == 401
    assert "/static/mobile_scrollback.css?v=11" in page.text
    assert "/static/mobile_scrollback.js?v=23" in page.text
    assert stylesheet.status_code == 200
    assert "overflow-y: auto" in stylesheet.text
    assert "scrollbar-gutter: stable" in stylesheet.text
    assert script.status_code == 200
    assert 'terminalHost.addEventListener(\n    "touchstart"' in script.text
    assert 'terminalHost.addEventListener(\n    "wheel"' in script.text
    assert "mobile-scrollback-trigger" not in script.text
    assert "terminal-history-layer" not in script.text
    assert "terminal-scroll-surface" in script.text


def test_mobile_scrollback_uses_existing_capture_page_without_touching_pty() -> None:
    script = (ROOT / "termroom/static/mobile_scrollback.js").read_text(encoding="utf-8")

    assert 'document.querySelector(\'.terminal-output-action[href*="/scrollback"]\')' in script
    assert 'url.searchParams.set("history_only", "1")' in script
    assert "fetch(historyOnlyUrl()" in script
    assert 'const headers = { Accept: "text/plain" };' in script
    assert "DOMParser" not in script
    assert "historyBeforeLiveViewport" not in script
    assert "bestMatchedRows" not in script
    assert 'terminalHost.querySelectorAll(".xterm-rows > div")' not in script
    assert 'liveButton.textContent = "LIVE ↓";' in script
    assert "surface.scrollTop = maxScrollTop();" in script
    assert 'behavior: "smooth"' not in script
    assert "const historyBoundarySignature = (rows) =>" in script
    assert "if (nextSignature === boundarySignature) return;" in script
    assert "historicalText !== renderedHistoryText" in script
    assert "interactionRevision !== userScrollRevision" in script
    assert "historyChangeRevision !== requestedChangeRevision" in script
    assert "HISTORY_REFRESH_MAX_WAIT_MS = 3000" in script
    assert "lastHistoryChangeAt + HISTORY_REFRESH_DEBOUNCE_MS" in script
    assert "historyDirtySince + HISTORY_REFRESH_MAX_WAIT_MS" in script
    assert "const bindParsedTerminalOutputRefresh = () =>" in script
    assert "terminalPrototype.write = function termroomScrollbackWrite" in script
    assert "isFullViewportRedraw" in script
    assert 'CURSOR_SHOW_SEQUENCE = "\\x1b[?25h"' in script
    assert "const redrawState = new WeakMap();" in script
    assert "state.fullViewport = true" in script
    assert "state.fullViewport = false" in script
    assert "!suppressHistoryRefresh" in script
    assert "terminalWriteMayAdvanceHistory" not in script
    assert "stripTerminalControls" not in script
    assert "availableRows" not in script
    assert 'headers["If-None-Match"] = historyEtag' in script
    assert "response.status === 304" in script
    assert "forceNextHistoryRefresh" in script
    assert "const responseEtag = response.headers.get" in script
    assert script.index("if (!stickToBottom && !liveFollowing && !atLiveBottom())") < script.index(
        "historyEtag = responseEtag;"
    )
    assert 'addEventListener("termroom:terminal-activity-changed"' not in script
    assert "new Proxy(NativeWebSocket" not in script
    assert "document.hidden || mouseTrackingActive()" in script
    assert "if (!stickToBottom && !liveFollowing && !atLiveBottom())" in script
    assert "if (!historyDirty || !liveFollowing) return;" in script
    assert "if (!away && !wasFollowing && historyDirty)" in script
    assert "const bindLiveInputReturn = () =>" in script
    assert 'textarea.addEventListener(\n      "keydown"' in script
    assert 'document.querySelector("#command-form")?.addEventListener' in script
    assert "const bindLiveOutputRefresh = () =>" in script
    assert "new MutationObserver(scheduleHistoryRefresh).observe" not in script
    assert "new MutationObserver(() =>" in script
    assert "const afterLayout = (callback) =>" in script
    assert "if (document.hidden)" in script
    assert "void loadHistory({ stickToBottom: true, force: true });" in script
    assert "event.touches.length !== 1" in script
    assert "touch.identifier" in script
    assert 'terminalHost.querySelector(".xterm.enable-mouse-events")' in script
    assert '"wheel",' in script
    assert "{ capture: true, passive: true }" in script
    assert "event.stopPropagation();" in script
    assert "TOUCH_HOLD_LIMIT_MS = 450" in script
    assert "touchGesture.startedAtBottom && dy < TOUCH_DISTANCE_PX" in script
    assert "ResizeObserver" in script
    assert "event.preventDefault();" not in script
    assert "socket.send(" not in script
    assert "term.input" not in script


def test_mobile_scrollback_is_native_touch_scrollable() -> None:
    stylesheet = (ROOT / "termroom/static/mobile_scrollback.css").read_text(
        encoding="utf-8"
    )

    assert ".terminal-scroll-surface {" in stylesheet
    assert "overflow-y: auto;" in stylesheet
    assert "overflow-anchor: none;" in stylesheet
    assert "scrollbar-color: auto;" in stylesheet
    assert "scrollbar-gutter: stable;" in stylesheet
    assert "scrollbar-width: auto;" in stylesheet
    assert ".terminal-scroll-surface::-webkit-scrollbar" in stylesheet
    assert "width: revert;" in stylesheet
    assert "overscroll-behavior: contain;" in stylesheet
    assert "white-space: pre-wrap;" in stylesheet
    assert "mobile-scrollback-trigger" not in stylesheet
    assert ".terminal-scroll-surface > .terminal-host" in stylesheet
    assert ".terminal-scroll-live" in stylesheet
    assert "terminal-history-layer" not in stylesheet
