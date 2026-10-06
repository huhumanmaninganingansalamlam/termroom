from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import httpx
import pytest

from termroom.app import create_app
from termroom.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def _asset_hash(filename: str) -> str:
    return hashlib.sha256((ROOT / "termroom/static" / filename).read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("history", "live", "overlap"),
    [
        ("alpha\nmarker", ["marker", "beta"], 1),
        ("marker\nmarker", ["marker", "beta"], 1),
        ("alpha\nmarker", ["marker", "marker", "beta"], 1),
        ("alpha\nmarker", ["other", "beta"], 0),
        ("alpha\none\ntwo", ["one", "two", "beta"], 2),
        ("marker\nother", ["marker", "beta"], 0),
        ("alpha\n\nmarker", ["", "marker", "beta"], 2),
        ("alpha\r\nmarker\u00a0", ["marker   ", "beta"], 1),
        ("alpha\n한글界e\u0301", ["한글界e\u0301", "beta"], 1),
        ("\n", ["", ""], 0),
        ("alpha\n" + "x" * 2049, ["x" * 2049], 0),
        ("alpha\nmarker", ["marker"] * 7, 0),
        ("\n".join(["x" * 2048] * 6), ["x" * 2048] * 6, 0),
    ],
)
def test_history_live_boundary_behavior(history: str, live: list[str], overlap: int) -> None:
    import json

    script = (ROOT / "termroom/static/mobile_scrollback.js").read_text()
    helper = script[
        script.index("  const boundaryRowText =") : script.index("  const liveBoundarySnapshot =")
    ]
    probe = f"""
const assert = require('node:assert/strict');
const HISTORY_BOUNDARY_ROW_COUNT = 6;
const HISTORY_BOUNDARY_MAX_CHARS = 8192;
const HISTORY_BOUNDARY_MAX_ROW_CHARS = 2048;
const normalizeText = value => String(value).replace(/\\r\\n?/g, '\\n').replace(/\\u00a0/g, ' ');
{helper}
assert.equal(
  historyBoundaryOverlap(normalizeText({json.dumps(history)}), {json.dumps(live)}), {overlap}
);
"""
    result = subprocess.run(["node", "-e", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("live_revisions", [0, 1000])
def test_history_boundary_styles_revisions_and_304(live_revisions: int) -> None:
    script = (ROOT / "termroom/static/mobile_scrollback.js").read_text()
    constants = script[script.index("  const SCROLL_BOTTOM") : script.index("  const surface =")]
    parser = script[
        script.index("  const normalizeText =") : script.index("  const applySelectionOwnership =")
    ]
    parser = parser.replace(
        "const parseAnsiHistory = (value) => {",
        "const parseAnsiHistory = (value) => { parserBuildCalls++;",
        1,
    )
    boundary = script[
        script.index("  const boundaryRowText =") : script.index("  const isFullViewportRedraw =")
    ]
    loader = script[
        script.index("  const loadHistory =") : script.index("  const scheduleHistoryRefresh =")
    ]
    probe = (
        r"""
const assert = require('node:assert/strict');
class Node {
  constructor(type = 1, text = '') {
    this.nodeType = type; this.data = text; this.children = []; this.style = {setProperty(){}};
  }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  get lastChild() { return this.children.at(-1); }
  get textContent() {
    return this.nodeType === 3 ? this.data : this.children.map(n => n.textContent).join('');
  }
  set textContent(text) {
    this.data = text; this.children = [];
    if (this.nodeType !== 3 && text) this.append(new Node(3, text));
  }
  remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
}
let parserBuildCalls = 0, fragmentBuildCalls = 0;
const document = {
  createDocumentFragment: () => { fragmentBuildCalls++; return new Node(); },
  createElement: () => new Node(),
  createTextNode: text => new Node(3, text)
};
const historyGraphemeSegmenter = new Intl.Segmenter(undefined, {granularity: 'grapheme'});
const historyCellWidthCache = new Map();
let live = ['marker', 'beta'];
const terminalHost = {
  dataset: {terminalId: 'run'},
  querySelector: () => ({children: live.map(textContent => ({textContent}))})
};
let terminalRevision = 0, switchGeneration = 0, renderedHistoryOverlap = -1;
let renderedHistoryText = '';
let renderedHistoryPlainText = '';
let refreshQueued = false, urgentRefreshQueued = false, forceNextHistoryRefresh = false;
let loading = false, historyChangeRevision = 0, historyEtag = '', historyDirty = true;
let historyDirtySince = 0, lastHistoryChangeAt = 0, nativeCopySelectionActive = false;
let liveFollowing = true, userScrollRevision = 0, historyRenderRevision = 0;
let commits = 0;
const history = {dataset: {}, replaceChildren(fragment) { commits++; this.fragment = fragment; }};
const syncHistoryMetrics = () => {};
const surface = {dataset: {}, scrollTop: 0};
const atLiveBottom = () => true, maxScrollTop = () => 0;
const afterLayout = () => {}, syncLiveHeight = () => {}, updateScrollState = () => {};
const clearHistoryDirty = () => { historyDirty = false; };
const scheduleHistoryRefresh = () => {};
const historyOnlyUrl = () => '/history';
"""
        + constants
        + parser
        + boundary
        + loader
        + r"""
(async () => {
  const text = '\x1b[31malpha 한글界e\u0301\x1b[0m\nmarker';
  assert.equal(commitHistoryBoundary(text, liveBoundarySnapshot()), true);
  assert.equal(history.fragment.textContent, 'alpha 한글界e\u0301');
  assert.equal(history.dataset.rendering, 'ansi');
  assert.equal(history.fragment.children[0].style.color, 'var(--terminal-red)');
  const stale = liveBoundarySnapshot();
  terminalRevision++;
  assert.equal(commitHistoryBoundary('wrong', stale), false);
  assert.equal(commits, 1);
  const switched = liveBoundarySnapshot();
  switchGeneration++;
  terminalHost.dataset.terminalId = 'shell';
  assert.equal(commitHistoryBoundary('wrong', switched), false);
  assert.equal(commits, 1);
  live = ['other'];
  globalThis.fetch = async () => ({status: 304, headers: {get: () => 'same'}});
  await loadHistory();
  assert.equal(history.fragment.textContent, 'alpha 한글界e\u0301\nmarker');
  assert.equal(commits, 2);
  globalThis.fetch = async () => {
    terminalRevision++;
    return {status: 200, ok: true, headers: {get: () => 'new'}, text: async () => 'wrong'};
  };
  await loadHistory();
  assert.equal(commits, 2);
  globalThis.fetch = async () => ({
    status: 200, ok: true, headers: {get: () => 'new'},
    text: async () => {switchGeneration++; return 'wrong';}
  });
  await loadHistory();
  assert.equal(commits, 2);
  assert.equal(historyEtag, 'same');
  const large = ('\x1b[31m' + 'x'.repeat(100) + '\x1b[0m\n').repeat(2000) + 'boundary';
  live = ['other'];
  assert.equal(commitHistoryBoundary(large, liveBoundarySnapshot()), true);
  const parsed = parserBuildCalls, built = fragmentBuildCalls, committed = commits;
  for (let index = 0; index < LIVE_REVISIONS; index++) {
    terminalRevision++;
    live = ['revision-' + index];
    assert.equal(commitHistoryBoundary(large, liveBoundarySnapshot()), false);
  }
  assert.equal(parserBuildCalls, parsed);
  assert.equal(fragmentBuildCalls, built);
  assert.equal(commits, committed);
  live = ['boundary', 'next'];
  assert.equal(commitHistoryBoundary(large, liveBoundarySnapshot()), true);
  assert.equal(parserBuildCalls, parsed + 1);
  assert.equal(fragmentBuildCalls, built + 1);
  live = ['other'];
  assert.equal(commitHistoryBoundary(large, liveBoundarySnapshot()), true);
  assert.equal(parserBuildCalls, parsed + 2);
  assert.equal(history.fragment.textContent.endsWith('boundary'), true);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    )
    probe = probe.replace("LIVE_REVISIONS", str(live_revisions))
    result = subprocess.run(["node", "-e", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_base_loads_mobile_scrollback_assets() -> None:
    template = (ROOT / "termroom/templates/base.html").read_text(encoding="utf-8")
    terminal_template = (ROOT / "termroom/templates/terminal.html").read_text(encoding="utf-8")

    assert (
        "mobile_scrollback.css') }}?v={{ static_asset_version('mobile_scrollback.css') }}"
        in template
    )
    assert (
        "terminal_selection.js') }}?v={{ static_asset_version('terminal_selection.js') }}\" defer"
        in template
    )
    assert (
        "mobile_scrollback.js') }}?v={{ static_asset_version('mobile_scrollback.js') }}\" defer"
        in template
    )
    assert "__termroomTerminalOutputHookInstalled" not in template
    assert "terminal.js') }}?v={{ static_asset_version('terminal.js') }}" in terminal_template


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
        stylesheet_version = _asset_hash("mobile_scrollback.css")
        ownership_version = _asset_hash("terminal_selection.js")
        script_version = _asset_hash("mobile_scrollback.js")
        stylesheet = await client.get(f"/static/mobile_scrollback.css?v={stylesheet_version}")
        ownership = await client.get(f"/static/terminal_selection.js?v={ownership_version}")
        script = await client.get(f"/static/mobile_scrollback.js?v={script_version}")

    assert page.status_code == 401
    assert f"/static/mobile_scrollback.css?v={stylesheet_version}" in page.text
    assert f"/static/terminal_selection.js?v={ownership_version}" in page.text
    assert f"/static/mobile_scrollback.js?v={script_version}" in page.text
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


def test_parsed_write_history_state_rejects_old_terminal_and_connection_epochs() -> None:
    script = (ROOT / "termroom/static/mobile_scrollback.js").read_text()
    binding = script[script.index("  const bindParsedTerminalOutputRefresh ="):
                     script.index("  const mouseTrackingActive =")]
    probe = r"""
const assert=require('node:assert/strict');
const queue=[];
class Terminal {write(data,callback){queue.push(callback);}}
const window={Terminal};
const terminalHost={dataset:{terminalId:'A'},termroomConnectionEpoch:0,
 contains:e=>e===term.element};
const term=new Terminal();term.element={};
let switchGeneration=0,terminalRevision=0,dirty=0,refresh=0,completed=0;
const document={hidden:false},liveFollowing=true,mouseTrackingActive=()=>false;
const isFullViewportRedraw=text=>text==='REDRAW',CURSOR_SHOW_SEQUENCE='SHOW';
const markHistoryDirty=()=>dirty++,scheduleHistoryRefresh=()=>refresh++;
BINDING
bindParsedTerminalOutputRefresh();
term.write('old-A',()=>completed++);
terminalHost.termroomConnectionEpoch++;
queue.shift()();
assert.deepEqual([terminalRevision,dirty,refresh,completed],[0,0,0,1]);
term.write('current-A',()=>completed++);queue.shift()();
assert.deepEqual([terminalRevision,dirty,refresh,completed],[1,1,1,2]);
term.write('REDRAW',()=>completed++);
terminalHost.dataset.terminalId='B';terminalHost.termroomConnectionEpoch++;switchGeneration++;
queue.shift()();
assert.deepEqual([terminalRevision,dirty,refresh,completed],[1,1,1,3]);
term.write('current-B',()=>completed++);queue.shift()();
assert.deepEqual([terminalRevision,dirty,refresh,completed],[2,2,2,4],
  'old full-viewport suppression must not carry into a new connection');
term.write('',()=>completed++);queue.shift()();
assert.deepEqual([terminalRevision,dirty,refresh,completed],[2,2,2,5],
  'drain fences complete without dirtying history');
""".replace("BINDING", binding)
    result = subprocess.run(["node", "-e", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_mobile_scrollback_uses_existing_capture_page_without_touching_pty() -> None:
    script = (ROOT / "termroom/static/mobile_scrollback.js").read_text(encoding="utf-8")

    assert "document.querySelector('.terminal-output-action[href*=\"/scrollback\"]')" in script
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
    assert 'const HISTORY_HANGUL_SPACING_PROPERTY = "--termroom-history-hangul-spacing";' in script
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
    assert 'document.createElement("span")' in script
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


def test_layout_scroll_does_not_turn_live_input_into_reading_mode() -> None:
    script = (ROOT / "termroom/static/mobile_scrollback.js").read_text(encoding="utf-8")
    start = script.index("  const updateScrollState = () => {")
    end = script.index("\n\n  const scrollToLive", start)
    update_scroll_state = script[start:end].replace(
        "  const updateScrollState = () => {", "globalThis.updateScrollState = () => {", 1
    )
    input_start = script.index("    const returnToLive = () => {")
    input_end = script.index("\n    textarea.addEventListener(", input_start)
    return_to_live = script[input_start:input_end].replace(
        "    const returnToLive = () => {", "globalThis.returnToLive = () => {", 1
    )
    composer_start = script.index('    "click",\n    (event) => {')
    composer_end = script.index("\n    { capture: true },", composer_start)
    composer_click = (
        script[composer_start:composer_end]
        .replace(
            '    "click",\n    (event) => {',
            "globalThis.handleComposerClick = (event) => {",
            1,
        )
        .replace("\n    },", "\n};", 1)
    )
    probe = f"""
const assert = require("node:assert/strict");
globalThis.Element = Object;
let liveFollowing = true;
let userScrollIntentPending = false;
let historyDirty = false;
let atBottom = false;
let returnedToLive = 0;
let blurred = 0;
let enteredReading = 0;
const atLiveBottom = () => atBottom;
const scrollToLive = () => {{
  returnedToLive += 1;
  liveFollowing = true;
  atBottom = true;
  updateScrollState();
}};
const scheduleHistoryRefresh = () => {{}};
const mouseTrackingActive = () => false;
const selectionBelongsToSurface = () => false;
const applySelectionOwnership = (event) => {{
  if (event.type === "enter-reading") enteredReading += 1;
}};
const liveButton = {{ hidden: true }};
const document = {{
  body: {{ classList: {{ toggle() {{}} }} }},
  querySelector: () => ({{ blur() {{ blurred += 1; }} }}),
}};
{update_scroll_state}
const helperTarget = {{
  closest: (selector) => selector.includes("[data-terminal-action]") ? helperTarget : null,
}};
const pasteTarget = {{
  closest: (selector) => selector.includes("#paste-terminal") ? pasteTarget : null,
}};
{composer_click}
updateScrollState();
assert.equal(returnedToLive, 1);
assert.equal(blurred, 0);
assert.equal(enteredReading, 0);
assert.equal(liveFollowing, true);
atBottom = false;
userScrollIntentPending = true;
updateScrollState();
assert.equal(returnedToLive, 1);
assert.equal(blurred, 1);
assert.equal(enteredReading, 1);
assert.equal(liveFollowing, false);
assert.equal(liveButton.hidden, false);
atBottom = true;
liveFollowing = true;
userScrollIntentPending = true;
handleComposerClick({{ target: helperTarget }});
assert.equal(userScrollIntentPending, false);
atBottom = false;
updateScrollState();
assert.equal(returnedToLive, 2);
assert.equal(blurred, 1);
assert.equal(enteredReading, 1);
assert.equal(liveFollowing, true);
assert.equal(liveButton.hidden, true);
atBottom = true;
liveFollowing = true;
userScrollIntentPending = true;
handleComposerClick({{ target: pasteTarget }});
assert.equal(userScrollIntentPending, false);
atBottom = false;
updateScrollState();
assert.equal(returnedToLive, 3);
assert.equal(blurred, 1);
assert.equal(enteredReading, 1);
assert.equal(liveFollowing, true);
assert.equal(liveButton.hidden, true);
atBottom = true;
liveFollowing = true;
userScrollIntentPending = true;
{return_to_live}
returnToLive();
assert.equal(userScrollIntentPending, false);
atBottom = false;
updateScrollState();
assert.equal(returnedToLive, 4);
assert.equal(liveFollowing, true);
"""
    result = subprocess.run(["node", "-e", probe], check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_wheel_ownership_uses_buffer_and_mouse_modes() -> None:
    script = (ROOT / "termroom/static/mobile_scrollback.js").read_text()
    helpers = script[
        script.index("  const mouseTrackingActive =") : script.index(
            "  const bindMouseTrackingOwnership ="
        )
    ]
    host_start = script.index('  terminalHost.addEventListener(\n    "wheel",')
    host_listener = script[host_start : script.index("\n\n", host_start)]
    surface_start = script.index('  surface.addEventListener(\n    "wheel",')
    surface_listener = script[surface_start : script.index("\n\n", surface_start)]
    probe = (
        r"""
const assert = require('node:assert/strict');
class Node {}
let mouse = false;
const callbacks = {};
const terminalHost = {
  dataset: {paneModeCapable:'true'},
  termroomActiveBufferType: 'alternate', // tmux's outer screen is not pane authority
  termroomPaneMode: {alternate:false,mouse_tracking:false},
  querySelector: () => mouse ? {} : null,
  contains: () => true,
  addEventListener: (_, callback) => callbacks.host = callback,
};
const surface = {addEventListener: (_, callback) => callbacks.surface = callback};
let historyIntents = 0;
const applySelectionOwnership = () => {};
const noteUserScrollIntent = () => historyIntents++;
"""
        + helpers
        + host_listener
        + surface_listener
        + r"""
for (const [buffer, tracking, nativeHistory] of [
  ['normal', false, true], ['alternate', false, false],
  ['alternate', true, false], ['normal', true, false],
]) {
  terminalHost.termroomPaneMode = {alternate:buffer==='alternate',mouse_tracking:tracking};
  mouse = tracking;
  historyIntents = 0;
  let stopped = 0;
  const event = {target:new Node(), deltaY:-180, stopPropagation:()=>stopped++};
  callbacks.surface(event);
  callbacks.host(event);
  assert.equal(stopped, Number(nativeHistory), `${buffer}/mouse=${tracking}`);
  assert.equal(historyIntents, Number(nativeHistory));
}
mouse = false;
for (const buffer of ['normal', 'alternate']) {
  terminalHost.termroomPaneMode = {alternate:buffer==='alternate',mouse_tracking:false};
  for (const modifier of ['ctrlKey', 'metaKey', 'altKey', 'shiftKey']) {
    let stopped = 0;
    historyIntents = 0;
    const event = {target:new Node(),deltaY:-180,[modifier]:true,stopPropagation:()=>stopped++};
    callbacks.surface(event);
    callbacks.host(event);
    assert.equal(stopped, 0);
    assert.equal(historyIntents, 0);
  }
}
terminalHost.termroomPaneMode = null;
let pendingStopped = 0, prevented = 0;
let bubbled = 0;
mouse = false;
callbacks.surface({target:new Node(),deltaY:-180,stopPropagation:()=>bubbled++});
callbacks.host({stopImmediatePropagation:()=>pendingStopped++,stopPropagation:()=>bubbled++,preventDefault:()=>prevented++});
assert.equal(historyIntents, 1);
assert.equal(pendingStopped, 0);
assert.equal(prevented, 0);
mouse = true;
callbacks.host({stopImmediatePropagation:()=>pendingStopped++,stopPropagation:()=>bubbled++,preventDefault:()=>prevented++});
assert.equal(pendingStopped, 0);
assert.equal(prevented, 0);
callbacks.host({ctrlKey:true,stopImmediatePropagation:()=>pendingStopped++,stopPropagation:()=>bubbled++,preventDefault:()=>prevented++});
assert.equal(pendingStopped, 0);
assert.equal(prevented, 0);
terminalHost.dataset.paneModeCapable = 'false';
for (const tracking of [false,true]) {
  mouse = tracking;
  let stopped = 0;
  callbacks.host({stopPropagation:()=>stopped++});
  assert.equal(stopped, Number(!tracking));
}
"""
    )
    result = subprocess.run(["node", "-e", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


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
    stylesheet = (ROOT / "termroom/static/mobile_scrollback.css").read_text(encoding="utf-8")

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
