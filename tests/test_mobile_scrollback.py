from __future__ import annotations

import subprocess
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

    assert "mobile_scrollback.css') }}?v=15" in template
    assert "terminal_selection.js') }}?v=1\" defer" in template
    assert "mobile_scrollback.js') }}?v=34\" defer" in template
    assert "__termroomTerminalOutputHookInstalled" not in template
    assert "terminal.js') }}?v=57" in terminal_template


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
        stylesheet = await client.get("/static/mobile_scrollback.css?v=15")
        ownership = await client.get("/static/terminal_selection.js?v=1")
        script = await client.get("/static/mobile_scrollback.js?v=34")

    assert page.status_code == 401
    assert "/static/mobile_scrollback.css?v=15" in page.text
    assert "/static/terminal_selection.js?v=1" in page.text
    assert "/static/mobile_scrollback.js?v=34" in page.text
    assert ownership.status_code == 200
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
    assert 'url.searchParams.set("ansi", "1")' in script
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
    assert "let terminalRevision = 0;" in script
    assert "let urgentRefreshQueued = false;" in script
    assert "requestedTerminalRevision !== terminalRevision" in script
    assert "urgentRefreshQueued = true;" in script
    assert 'window.addEventListener("termroom:terminal-switched"' in script
    assert 'historyEtag = "";' in script
    assert 'history.textContent = "";' in script
    assert "forceNextHistoryRefresh" in script
    assert "const responseEtag = response.headers.get" in script
    stable_read_guard = script.index(
        "nativeCopySelectionActive\n"
        "        || (!stickToBottom && !liveFollowing && !atLiveBottom())"
    )
    assert stable_read_guard < script.index("historyEtag = responseEtag;")
    assert 'addEventListener("termroom:terminal-activity-changed"' not in script
    assert "new Proxy(NativeWebSocket" not in script
    assert "document.hidden || mouseTrackingActive()" in script
    assert "|| (!stickToBottom && !liveFollowing && !atLiveBottom())" in script
    assert "if (!historyDirty || !liveFollowing) return;" in script
    assert "if (!away && !wasFollowing && historyDirty)" in script
    assert 'if (away) {\n      document.querySelector(".xterm-helper-textarea")?.blur();' in script
    assert "const bindLiveInputReturn = () =>" in script
    assert 'textarea.addEventListener(\n      "keydown"' in script
    assert 'document.querySelector("#command-form")?.addEventListener' in script
    assert "const bindLiveOutputRefresh = () =>" in script
    assert "const rowStyle = window.getComputedStyle(row);" in script
    assert "history.style.fontSize = rowStyle.fontSize || xtermStyle.fontSize;" in script
    assert "new MutationObserver(scheduleHistoryRefresh).observe" not in script
    assert "new MutationObserver(() =>" in script
    assert "const afterLayout = (callback) =>" in script
    assert "if (document.hidden)" in script
    assert script.count("void loadHistory({ stickToBottom: true, force: true });") == 2
    assert "event.touches.length !== 1" in script
    assert "touch.identifier" in script
    assert 'terminalHost.querySelector(".xterm.enable-mouse-events")' in script
    assert '"wheel",' in script
    assert "{ capture: true, passive: true }" in script
    assert "event.stopPropagation();" in script
    assert "TOUCH_HOLD_LIMIT_MS = 450" in script
    assert "touchGesture.startedAtBottom && dy < TOUCH_DISTANCE_PX" in script
    assert "ResizeObserver" in script
    assert 'const NATIVE_COPY_SELECTION_CLASS = "terminal-scroll-native-selection";' in script
    assert "const parseAnsiHistory = (value) =>" in script
    assert "const applyAnsiHistorySgr = (state, rawParameters) =>" in script
    assert "HISTORY_ANSI_MAX_SEGMENTS = 20_000" in script
    assert "HISTORY_HANGUL_MAX_RUNS = 20_000" in script
    assert 'const TERMINAL_CELL_WIDTH_PROPERTY = "--termroom-terminal-cell-width";' in script
    assert (
        'const HISTORY_HANGUL_SPACING_PROPERTY = "--termroom-history-hangul-spacing";'
        in script
    )
    assert "HISTORY_CELL_WIDTH_CACHE_LIMIT = 4096" in script
    assert "HISTORY_HANGUL_CANDIDATE_PATTERN" in script
    for unicode_range in (
        "\\u1100-\\u11ff",
        "\\u3131-\\u318e",
        "\\ua960-\\ua97f",
        "\\uac00-\\ud7a3",
        "\\ud7b0-\\ud7ff",
        "\\uffa0-\\uffdc",
    ):
        assert unicode_range in script
    assert 'new Intl.Segmenter(undefined, { granularity: "grapheme" })' in script
    assert "function* manualHistoryGraphemes(value)" in script
    assert "function* historyGraphemes(value)" in script
    assert "yield* manualHistoryGraphemes(value);" in script
    assert "const historyHangulType = (codePoint) =>" in script
    assert "const historyCellWidth = (value) =>" in script
    assert "terminalHost.termroomStringCellWidth" in script
    assert "fallbackHistoryCellWidth(value)" in script
    assert "const appendHistoryText = (parent, text, budget) =>" in script
    assert 'wide.className = "terminal-scroll-history-hangul";' in script
    assert 'wide.className = "terminal-scroll-history-cell-cluster";' in script
    assert 'wide.style.setProperty("--termroom-history-cell-count", String(cellWidth));' in script
    assert 'probe.textContent = "한";' in script
    assert "cellWidth * 2 - naturalHangulWidth" in script
    assert 'window.addEventListener("termroom:terminal-metrics", syncHistoryMetrics);' in script
    assert 'history.dataset.rendering = rendered.styled ? "ansi" : "plain";' in script
    assert "history.replaceChildren(rendered.fragment);" in script
    assert "document.createElement(\"span\")" in script
    assert "Object.assign(span.style, segment.style);" in script
    assert "innerHTML" not in script
    assert "const selectionBelongsToSurface = () =>" in script
    assert "surface.contains(range.startContainer)" in script
    assert "surface.contains(range.endContainer)" in script
    assert 'type: "enter-reading"' in script
    assert 'type: "pointer-finished"' in script
    assert 'type: "outside-pointer"' in script
    assert 'terminalHost.addEventListener(\n    "mousedown"' in script
    assert "event.stopImmediatePropagation();" in script
    assert "terminalHost.contains(range.endContainer)" in script
    assert 'document.addEventListener(\n    "copy"' in script
    assert 'event.clipboardData.setData("text/plain", copyText);' in script
    assert 'replace(/^\\n\\n/, "\\n")' in script
    touch_move = script[script.index('terminalHost.addEventListener(\n    "touchmove"') :]
    touch_move = touch_move[: touch_move.index("const clearTouchGesture")]
    assert "event.preventDefault();" not in touch_move
    assert "socket.send(" not in script
    assert "term.input" not in script


def test_terminal_selection_ownership_state_machine() -> None:
    script = ROOT / "termroom/static/terminal_selection.js"
    probe = """
const assert = require("node:assert/strict");
const ownership = require(process.argv[1]);
const mouseEdges = [];
const syncMouse = ownership.createMouseTrackingEdge((active) => mouseEdges.push(active));
assert.equal(syncMouse(false), true);
assert.equal(syncMouse(false), false);
assert.equal(syncMouse(true), true);
assert.equal(syncMouse(true), false);
assert.equal(syncMouse(false), true);
assert.deepEqual(mouseEdges, [false, true, false]);
let mode = ownership.LIVE_XTERM;
mode = ownership.transition(mode, {type: "enter-reading", mouseTracking: false});
assert.equal(mode, ownership.READING_NATIVE);
mode = ownership.transition(mode, {type: "pointer-finished"});
assert.equal(mode, ownership.READING_NATIVE);
mode = ownership.transition(mode, {type: "wheel"});
assert.equal(mode, ownership.READING_NATIVE);
mode = ownership.transition(mode, {
  type: "surface-pointer", primaryMouse: false, away: true, mouseTracking: false,
});
assert.equal(mode, ownership.READING_NATIVE);
mode = ownership.transition(mode, {type: "return-live", hasSurfaceSelection: true});
assert.equal(mode, ownership.READING_NATIVE);
mode = ownership.transition(mode, {type: "return-live", hasSurfaceSelection: false});
assert.equal(mode, ownership.LIVE_XTERM);
mode = ownership.transition(mode, {type: "enter-reading", mouseTracking: false});
mode = ownership.transition(mode, {type: "terminal-input"});
assert.equal(mode, ownership.LIVE_XTERM);
mode = ownership.transition(mode, {type: "enter-reading", mouseTracking: false});
mode = ownership.transition(mode, {type: "mouse-tracking", active: true, away: true});
assert.equal(mode, ownership.TUI_MOUSE);
mode = ownership.transition(mode, {type: "terminal-input", mouseTracking: true});
assert.equal(mode, ownership.TUI_MOUSE);
mode = ownership.transition(mode, {
  type: "return-live", hasSurfaceSelection: false, mouseTracking: true,
});
assert.equal(mode, ownership.TUI_MOUSE);
mode = ownership.transition(mode, {type: "pointer-finished", mouseTracking: true});
assert.equal(mode, ownership.TUI_MOUSE);
mode = ownership.transition(mode, {type: "wheel", mouseTracking: true});
assert.equal(mode, ownership.TUI_MOUSE);
mode = ownership.transition(mode, {type: "mouse-tracking", active: false, away: true});
assert.equal(mode, ownership.READING_NATIVE);
mode = ownership.transition(ownership.TUI_MOUSE, {
  type: "mouse-tracking", active: false, away: false,
});
assert.equal(mode, ownership.LIVE_XTERM);
mode = ownership.transition(mode, {type: "outside-pointer"});
assert.equal(mode, ownership.LIVE_XTERM);
"""
    result = subprocess.run(
        ["node", "-e", probe, str(script)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


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
    assert ".terminal-scroll-history-hangul {" in stylesheet
    assert "letter-spacing: var(--termroom-history-hangul-spacing, 0px);" in stylesheet
    assert ".terminal-scroll-history-cell-cluster {" in stylesheet
    assert "var(--termroom-history-cell-count, 2)" in stylesheet
    assert "vertical-align: baseline;" in stylesheet
    assert "overflow: visible;" in stylesheet
    assert ".terminal-scroll-native-selection .xterm" in stylesheet
    assert ".terminal-scroll-native-selection .xterm .xterm-rows > div" in stylesheet
    assert "user-select: text;" in stylesheet
    assert "mobile-scrollback-trigger" not in stylesheet
    assert ".terminal-scroll-surface > .terminal-host" in stylesheet
    assert ".terminal-scroll-live" in stylesheet
    assert "terminal-history-layer" not in stylesheet
