"""Reversible TUI selection/copy with no accidental PTY input."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_probe(source: str) -> None:
    result = subprocess.run(
        ["node", "-e", source, str(ROOT / "termroom/static/terminal_selection.js")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_selection_mode_survives_tui_events_and_recovers() -> None:
    _run_probe(r"""
const assert = require("node:assert/strict");
const own = require(process.argv[1]);
let mode = own.TUI_MOUSE;
mode = own.transition(mode, {type:"start-copy", mouseTracking:true});
assert.equal(mode, own.COPY_XTERM);
for (const event of [
  {type:"surface-pointer", mouseTracking:true, primaryMouse:true, away:false},
  {type:"mouse-tracking", active:true, away:false},
  {type:"terminal-input", mouseTracking:true},
  {type:"wheel", mouseTracking:true},
  {type:"pointer-finished", mouseTracking:true},
  {type:"return-live", mouseTracking:true, hasSurfaceSelection:false},
]) {
  mode = own.transition(mode, event);
  assert.equal(mode, own.COPY_XTERM, event.type);
}
mode = own.transition(mode, {type:"stop-copy", mouseTracking:true, away:false});
assert.equal(mode, own.TUI_MOUSE);
mode = own.transition(mode, {type:"start-copy", mouseTracking:true});
mode = own.transition(mode, {type:"stop-copy", mouseTracking:false, away:true});
assert.equal(mode, own.READING_NATIVE);
mode = own.transition(mode, {type:"start-copy", mouseTracking:true});
mode = own.transition(mode, {type:"reset"});
assert.equal(mode, own.LIVE_XTERM);
""")


def test_mouse_mode_gate_tracks_tui_changes_while_copying() -> None:
    _run_probe(r"""
const assert = require("node:assert/strict");
const {createCopyMouseGate} = require(process.argv[1]);
const changes = [];
class MouseService {
  constructor() {this.protocol = "NONE";}
  get activeProtocol() {return this.protocol;}
  set activeProtocol(value) {this.protocol = value;changes.push(value);}
}
const service = new MouseService();
service.activeProtocol = "VT200";
const gate = createCopyMouseGate(service);
assert.equal(typeof gate, "function");
gate(true);
assert.equal(service.activeProtocol, "NONE");
service.activeProtocol = "DRAG";
assert.equal(service.activeProtocol, "NONE", "TUI changes must stay suppressed");
service.activeProtocol = "ANY";
assert.equal(service.activeProtocol, "NONE");
gate(false);
assert.equal(service.activeProtocol, "ANY", "restore latest TUI protocol, not stale VT200");
service.activeProtocol = "NONE";
gate(true);
service.activeProtocol = "SGR";
gate(false);
assert.equal(service.activeProtocol, "SGR");
assert.deepEqual(changes, [
  "VT200", "NONE", "NONE", "NONE", "ANY", "NONE", "NONE", "NONE", "SGR"
]);
""")


def test_visible_rows_preserve_soft_wrap_and_unicode() -> None:
    _run_probe(r"""
const assert = require("node:assert/strict");
const {visibleTerminalText} = require(process.argv[1]);
const lines = [
 {isWrapped:false, translateToString: trim => "가나다"},
 {isWrapped:true, translateToString: trim => "라마바사"},
 {isWrapped:false, translateToString: trim => "next"},
 {isWrapped:false, translateToString: trim => ""},
 {isWrapped:false, translateToString: trim => ""},
];
const term = {rows:4, buffer:{active:{
 viewportY:0, getLine:row=>lines[row]
}}};
assert.equal(visibleTerminalText(term), "가나다라마바사\nnext");
term.rows=2;
term.buffer.active.viewportY=2;
assert.equal(visibleTerminalText(term), "next");
""")


def test_copy_ui_single_mode_owner_and_localized_actions() -> None:
    source = (ROOT / "termroom/static/mobile_scrollback.js").read_text()
    copy_ui = (ROOT / "termroom/static/terminal_copy.js").read_text()
    terminal = (ROOT / "termroom/static/terminal.js").read_text()
    page = (ROOT / "termroom/templates/terminal.html").read_text()
    assert "static_asset_version('terminal_copy.js')" in page
    assert "window.TermroomTerminalCopy" in copy_ui
    assert "terminalCopyControls = window.TermroomTerminalCopy?" in source
    assert "let terminalSelectionMode =" in source
    assert "let terminalSelectionMode =" not in copy_ui
    assert "navigator.clipboard.writeText" not in source
    for marker in (
        "setTerminalSelectionMode", "syncTerminalCopyActions",
        "copyTerminalSelection", "copyVisibleTerminal",
        "termroomSetCopyMode",
    ):
        assert marker in source or marker in copy_ui or marker in terminal
    for element in ("terminal-copy-mode", "terminal-copy-selection",
                    "terminal-copy-visible", "terminal-copy-status"):
        assert f'id="{element}"' in page
    assert "term.options.disableStdin = terminalCopyModeActive ||" in terminal
    assert "navigator.clipboard.writeText" in copy_ui
    for locale in ("en", "ko"):
        messages = json.loads((ROOT / f"termroom/locales/{locale}.json").read_text())
        for key in ("terminal.copy_mode_on", "terminal.copy_mode_off",
                    "terminal.copy_mode_short_off",
                    "terminal.copy_selection", "terminal.copy_visible",
                    "terminal.copy_hint", "terminal.copy_success",
                    "terminal.copy_error"):
            assert messages.get(key), (locale, key)
