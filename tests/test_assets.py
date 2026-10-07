from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from termroom.assets import (
    ASSETS,
    TERMINAL_FONT_ASSETS,
    TERMINAL_FONT_SOURCE_ARCHIVE_SHA256,
    TERMINAL_FONT_SOURCE_ARCHIVE_URL,
    TERMINAL_FONT_SOURCE_TTF,
    TERMINAL_FONT_SOURCE_TTF_SHA256,
    TERMINAL_FONT_VERSION,
    VENDOR_DIR,
    XTERM_UNICODE11_VERSION,
    XTERM_VERSION,
    XTERM_VERSION_FILE,
    static_asset_version,
)


def _unicode_ranges(value: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for start, end in re.findall(r"U\+([0-9A-F]+)(?:-([0-9A-F]+))?", value):
        lower = int(start, 16)
        ranges.append((lower, int(end, 16) if end else lower))
    return ranges


def _range_points(ranges: list[tuple[int, int]]) -> set[int]:
    return {codepoint for start, end in ranges for codepoint in range(start, end + 1)}


def test_vendored_xterm_matches_declared_scoped_release() -> None:
    assert XTERM_VERSION == "6.0.0"
    assert XTERM_UNICODE11_VERSION == "0.8.0"
    assert set(ASSETS) == {"xterm.js", "xterm.css", "addon-unicode11.js"}
    assert XTERM_VERSION_FILE.read_text(encoding="utf-8").strip() == XTERM_VERSION
    assert (VENDOR_DIR / "xterm.js").stat().st_size > 400_000
    assert (VENDOR_DIR / "xterm.css").stat().st_size > 5_000
    assert (VENDOR_DIR / "addon-unicode11.js").stat().st_size > 12_000
    for filename in ("xterm.js", "xterm.css"):
        assert f"@xterm/xterm@{XTERM_VERSION}" in str(ASSETS[filename]["url"])
    assert f"@xterm/addon-unicode11@{XTERM_UNICODE11_VERSION}" in str(
        ASSETS["addon-unicode11.js"]["url"]
    )
    for filename, details in ASSETS.items():
        digest = hashlib.sha256((VENDOR_DIR / filename).read_bytes()).hexdigest()
        assert digest == details["sha256"]


def test_unicode11_provider_reports_standard_terminal_widths() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the vendored browser addon")
    script = r"""
const { Unicode11Addon } = require(process.argv[1]);
let provider = null;
const addon = new Unicode11Addon();
addon.activate({
  unicode: {
    register(value) {
      provider = value;
      return { dispose() {} };
    },
  },
});
if (!provider || typeof provider.wcwidth !== "function") {
  throw new Error("Unicode11 provider was not registered");
}
const codepoints = [0x41, 0x0301, 0xAC00, 0x4E2D, 0x200D, 0x1F600];
process.stdout.write(JSON.stringify({
  version: provider.version,
  widths: codepoints.map((codepoint) => provider.wcwidth(codepoint)),
}));
addon.dispose();
"""
    result = subprocess.run(
        [node, "-e", script, str(VENDOR_DIR / "addon-unicode11.js")],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "version": "11",
        "widths": [1, 0, 2, 2, 0, 2],
    }


def test_vendored_terminal_font_is_reproducible_and_attributed() -> None:
    assert TERMINAL_FONT_VERSION == "3.5.0"
    expected_assets = {
        "core_hangul": (
            "d2koding-ligature-nerd-font-mono-3.5.0-core-hangul.woff2",
            567_584,
            "b9fae6a182cc440dcf69c7a8b3a8b2ad60284fa2ed67e8e41c2f26bead83ede9",
        ),
        "cjk": (
            "d2koding-ligature-nerd-font-mono-3.5.0-cjk.woff2",
            801_848,
            "0d1b8923dc714312107b8282b1e2f1d79645e48f26e0a12d38dac886028c9783",
        ),
        "nerd_bmp": (
            "d2koding-ligature-nerd-font-mono-3.5.0-nerd-bmp.woff2",
            497_412,
            "64c9508468b380ac4e31f65ce5fc548e14939670ae1833870596f276661122dd",
        ),
        "nerd_supp": (
            "d2koding-ligature-nerd-font-mono-3.5.0-nerd-supp.woff2",
            397_908,
            "25fa39d5040346d76385e04aa5977b15b1bc72b3af27bff429f27e341fc03f0d",
        ),
    }
    assert set(TERMINAL_FONT_ASSETS) == set(expected_assets)
    assert TERMINAL_FONT_SOURCE_ARCHIVE_URL.endswith("/releases/download/v3.5.0/D2Coding.tar.xz")
    assert (
        TERMINAL_FONT_SOURCE_ARCHIVE_SHA256
        == "c1d4e7cbee20b9e55d2481762bbb8413124fda224cee26863b805fe2f863aaec"
    )
    assert TERMINAL_FONT_SOURCE_TTF == "D2KodingLigatureNerdFontMono-Regular.ttf"
    assert (
        TERMINAL_FONT_SOURCE_TTF_SHA256
        == "be8964904705f43a1e5a62339629d9e20eb37316008dda4de5b5681547ea2996"
    )

    for key, (filename, size, digest) in expected_assets.items():
        details = TERMINAL_FONT_ASSETS[key]
        assert details["filename"] == filename
        assert details["size"] == size
        assert details["sha256"] == digest
        font = VENDOR_DIR / filename
        assert font.stat().st_size == size
        assert hashlib.sha256(font.read_bytes()).hexdigest() == digest
    assert sum(int(details["size"]) for details in TERMINAL_FONT_ASSETS.values()) == 2_264_752
    assert not (VENDOR_DIR / "d2koding-ligature-nerd-font-mono-3.5.0.woff2").exists()

    d2_license = (VENDOR_DIR / "d2koding-nerd-font.OFL.txt").read_text(encoding="utf-8")
    nerd_license = (VENDOR_DIR / "nerd-fonts.LICENSE").read_text(encoding="utf-8")
    notice = (VENDOR_DIR / "d2koding-nerd-font.NOTICE.md").read_text(encoding="utf-8")
    assert "Reserved Font Name D2Coding" in d2_license
    assert "SIL OPEN FONT LICENSE Version 1.1" in d2_license
    assert "Copyright (c) 2014 Ryan L McIntyre" in nerd_license
    for evidence in (
        TERMINAL_FONT_SOURCE_ARCHIVE_SHA256,
        TERMINAL_FONT_SOURCE_TTF_SHA256,
        *(str(details["sha256"]) for details in TERMINAL_FONT_ASSETS.values()),
        "FontTools 4.63.0",
        "brotli 1.2.0",
        "pyftsubset",
        "PfEd",
        "CC BY 4.0",
        "Apache 2.0",
        "OFL 1.1",
        "The Unlicense",
    ):
        assert evidence in notice

    project_config = (VENDOR_DIR.parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    for filename in (
        "d2koding-nerd-font.OFL.txt",
        "nerd-fonts.LICENSE",
        "d2koding-nerd-font.NOTICE.md",
    ):
        assert f'"termroom/static/vendor/{filename}"' in project_config


def test_terminal_font_claims_only_the_audited_character_ranges() -> None:
    stylesheet = (VENDOR_DIR.parent / "terminal-font.css").read_text(encoding="utf-8")
    expected_ranges = (
        "U+0000-DFFF, U+E000-E00A, U+E0A0-E0A3, U+E0B0-E0C8, U+E0CA, "
        "U+E0CC-E0D2, U+E0D4, U+E0D6-E0D7, U+E200-E2A9, U+E300-E3E3, "
        "U+E5FA-E6BB, U+E700-E8EF, U+EA60-EA88, U+EA8A-EA8C, "
        "U+EA8F-EAC7, U+EAC9, U+EACC-EB09, U+EB0B-EB4E, U+EB50-EC5E, "
        "U+EC60-EC84, U+ED00-EFCF, U+F000-F385, U+F400-F533, "
        "U+F900-FFFF, U+F0001-F1AF0"
    )
    korean_ranges = "U+1100-11FF, U+3130-318F, U+A960-A97F, U+AC00-D7FF, U+FFA0-FFDC"

    faces = re.findall(r"@font-face\s*\{(.*?)\}", stylesheet, flags=re.DOTALL)
    assert len(faces) == 5
    korean_face = next(
        candidate for candidate in faces if 'font-family: "Termroom Korean Terminal"' in candidate
    )
    assert stylesheet.count('font-family: "Termroom D2Koding Nerd Mono"') == 4
    assert stylesheet.count('font-family: "Termroom Korean Terminal"') == 1
    assert stylesheet.count("font-weight: 400") == 5
    assert stylesheet.count("font-style: normal") == 5
    assert "font-weight: 700" not in stylesheet
    assert "font-style: italic" not in stylesheet
    assert stylesheet.count("font-display: block") == 1
    assert stylesheet.count("font-display: swap") == 4
    assert "font-variant-ligatures: none" in stylesheet
    assert 'font-feature-settings: "liga" 0, "calt" 0' in stylesheet
    assert "font-synthesis: weight style" in stylesheet
    for local_family in (
        "Noto Sans Mono CJK KR",
        "D2Coding",
        "NanumGothicCoding",
        "Apple SD Gothic Neo",
        "Malgun Gothic",
        "Noto Sans CJK KR",
        "Noto Sans KR",
    ):
        assert f'local("{local_family}")' in korean_face

    expected_points = _range_points(_unicode_ranges(expected_ranges))
    korean_points = _range_points(_unicode_ranges(korean_ranges))
    korean_declaration = re.search(r"unicode-range:\s*([^;]+);", korean_face)
    assert korean_declaration is not None
    assert _range_points(_unicode_ranges(korean_declaration.group(1))) == korean_points
    actual_points: set[int] = set()
    face_points: dict[str, set[int]] = {}
    for key, details in TERMINAL_FONT_ASSETS.items():
        filename = str(details["filename"])
        face = next(candidate for candidate in faces if filename in candidate)
        declaration = re.search(r"unicode-range:\s*([^;]+);", face)
        assert declaration is not None
        points = _range_points(_unicode_ranges(declaration.group(1)))
        source_points = _range_points(_unicode_ranges(str(details["unicode_range"])))
        assert points == source_points - korean_points
        assert actual_points.isdisjoint(points)
        actual_points.update(points)
        face_points[key] = points
        assert f'url("vendor/{filename}?v=3.5.0.1")' in face

    assert actual_points == expected_points - korean_points
    assert actual_points.isdisjoint(korean_points)
    assert {0x004D, 0x0301, 0x2500, 0x2800} <= face_points["core_hangul"]
    assert {0x1100, 0x3131, 0xAC00, 0xD7A3, 0xFFA0} <= korean_points
    assert 0x4E2D in face_points["cjk"]
    assert {0xE0B0, 0xF013} <= face_points["nerd_bmp"]
    assert {0xF0001, 0xF1AF0} <= face_points["nerd_supp"]

    for codepoint in (
        0xE132,  # D2-only legacy/extra PUA with unsafe overhang
        0xE2DC,
        0xE3E4,
        0xF841,
        0xF0000,
        0xF1AF1,
        0x1F600,  # supplementary emoji stays on the system stack
        0x20000,  # supplementary CJK stays on the system stack
    ):
        assert codepoint not in actual_points

    terminal_template = (VENDOR_DIR.parents[1] / "templates/terminal.html").read_text(
        encoding="utf-8"
    )
    assert 'id="terminal-screen-reader-mode"' in terminal_template
    assert 'for="terminal-screen-reader-mode"' in terminal_template
    assert 'aria-describedby="terminal-screen-reader-mode-help"' in terminal_template
    assert 'id="terminal-screen-reader-mode-help"' in terminal_template
    screen_reader_input = re.search(
        r'<input id="terminal-screen-reader-mode"[^>]*>', terminal_template
    )
    assert screen_reader_input is not None
    assert 'type="checkbox"' in screen_reader_input.group(0)
    assert 'autocomplete="off"' in screen_reader_input.group(0)
    assert re.search(r"\schecked(?:\s|=|>)", screen_reader_input.group(0)) is None
    assert re.search(r"\sname=", screen_reader_input.group(0)) is None
    assert str(TERMINAL_FONT_ASSETS["core_hangul"]["filename"]) in terminal_template
    assert "data-terminal-command-clear-target" in terminal_template
    for key in ("cjk", "nerd_bmp", "nerd_supp"):
        assert str(TERMINAL_FONT_ASSETS[key]["filename"]) not in terminal_template
    terminal_script = (VENDOR_DIR.parent / "terminal.js").read_text(encoding="utf-8")
    assert "KOREAN_TERMINAL_FONT_FAMILY = '\"Termroom Korean Terminal\"'" in terminal_script
    assert 'BUNDLED_TERMINAL_FONT_PROBE = "M"' in terminal_script
    assert (
        "`${KOREAN_TERMINAL_FONT_FAMILY}, ${BUNDLED_TERMINAL_FONT_FAMILY}, "
        "${systemFamily}`" in terminal_script
    )
    assert "`${KOREAN_TERMINAL_FONT_FAMILY}, ${systemFamily}`" in terminal_script
    assert "\\uE0B0" not in terminal_script
    assert "\\uF013" not in terminal_script
    assert "\\u{F0001}" not in terminal_script
    assert 'addEventListener?.("loadingdone"' in terminal_script
    assert "clearTextureAtlas?.()" in terminal_script
    assert "bundledTerminalFontLoad.completed.then" in terminal_script
    assert "bundledTerminalFontLoaded = true" in terminal_script
    assert "term.options.fontFamily = terminalFontFamily(true)" in terminal_script
    assert "BUNDLED_TERMINAL_FONT_LOAD_TIMEOUT_MS = 400" in terminal_script
    assert "const MINIMUM_TERMINAL_CONTRAST_RATIO = 4.5;" in terminal_script
    assert "minimumContrastRatio: MINIMUM_TERMINAL_CONTRAST_RATIO" in terminal_script
    assert "screenReaderMode: false," in terminal_script
    assert (
        "const screenReaderModeToggle = document.querySelector("
        '"#terminal-screen-reader-mode");' in terminal_script
    )
    assert "screenReaderModeToggle.checked = false;" in terminal_script
    assert "term.options.screenReaderMode = false;" in terminal_script
    assert terminal_script.count("new window.Terminal(") == 1
    assert re.search(
        r'screenReaderModeToggle\?\.addEventListener\("change", \(\) => \{\s*'
        r"term\.options\.screenReaderMode = screenReaderModeToggle\.checked;\s*"
        r"\}\);",
        terminal_script,
    )
    screen_reader_query_at = terminal_script.index(
        "const screenReaderModeToggle = document.querySelector("
    )
    screen_reader_checked_reset_at = terminal_script.index(
        "screenReaderModeToggle.checked = false;"
    )
    first_font_await_at = terminal_script.index("await bundledTerminalFontLoad.initial")
    terminal_constructor_at = terminal_script.index("new window.Terminal(")
    terminal_open_at = terminal_script.index("term.open(host);")
    screen_reader_option_reset_at = terminal_script.index("term.options.screenReaderMode = false;")
    screen_reader_listener_at = terminal_script.index(
        'screenReaderModeToggle?.addEventListener("change"'
    )
    assert (
        screen_reader_query_at
        < screen_reader_checked_reset_at
        < first_font_await_at
        < terminal_constructor_at
        < terminal_open_at
        < screen_reader_option_reset_at
        < screen_reader_listener_at
    )
    assert 'TERMINAL_CELL_WIDTH_PROPERTY = "--termroom-terminal-cell-width"' in terminal_script
    assert 'TERMINAL_CELL_HEIGHT_PROPERTY = "--termroom-terminal-cell-height"' in terminal_script
    assert "const terminalStringCellWidth = (value) =>" in terminal_script
    assert 'service.getStringCellWidth(String(value || ""))' in terminal_script
    assert 'Object.defineProperty(host, "termroomStringCellWidth"' in terminal_script
    buffer_mode_property = re.search(
        r'Object\.defineProperty\(host, "termroomActiveBufferType", \{.*?\n  \}\);',
        terminal_script,
        re.S,
    )
    assert buffer_mode_property is not None
    mode_probe = (
        """
const assert = require('node:assert/strict');
const host = {};
const term = {buffer:{active:{type:'normal'}}};
"""
        + buffer_mode_property.group()
        + """
assert.equal(host.termroomActiveBufferType, 'normal');
term.buffer.active = {type:'alternate'};
assert.equal(host.termroomActiveBufferType, 'alternate');
const descriptor = Object.getOwnPropertyDescriptor(host, 'termroomActiveBufferType');
assert.equal(descriptor.set, undefined);
assert.equal(Object.keys(host).includes('termroomActiveBufferType'), false);
"""
    )
    mode_result = subprocess.run(["node", "-e", mode_probe], capture_output=True, text=True)
    assert mode_result.returncode == 0, mode_result.stderr
    assert "const publishTerminalMetrics = (cell) =>" in terminal_script
    assert 'new CustomEvent("termroom:terminal-metrics"' in terminal_script
    assert "publishTerminalMetrics(cell);" in terminal_script
    assert 'new CustomEvent("termroom:terminal-output"' in terminal_script
    assert 'new CustomEvent("termroom:terminal-activity-changed"' in terminal_script
    assert "workspace_id: host.dataset.workspaceId" in terminal_script
    assert "terminal_id: host.dataset.terminalId" in terminal_script
    assert "scheduleResize(true)" in terminal_script
    assert "const onUserInput = term._core?.coreService?.onUserInput;" in terminal_script
    assert "const userInput = hasUserInputSignal && nextTerminalDataIsUserInput;" in terminal_script
    assert "user_input: userInput" in terminal_script
    assert "term.onBinary((data) =>" in terminal_script
    assert "socket.send(Uint8Array.from(data" in terminal_script
    assert 'kind: "command"' in terminal_script
    assert "rows: term.rows" in terminal_script
    assert "cols: term.cols" in terminal_script
    assert "const Unicode11AddonClass = window.Unicode11Addon?.Unicode11Addon;" in terminal_script
    assert (
        'const unicode11Available = typeof Unicode11AddonClass === "function";' in terminal_script
    )
    assert "allowProposedApi: unicode11Available" in terminal_script
    assert 'host.dataset.terminalUnicode = "default";' in terminal_script
    assert "if (unicode11Available)" in terminal_script
    assert "term.loadAddon(unicode11Addon);" in terminal_script
    assert 'term.unicode.activeVersion = "11";' in terminal_script
    assert 'host.dataset.terminalUnicode = "11";' in terminal_script
    assert 'return "\\u001b[1;5A";' in terminal_script
    assert 'return "\\u001b[1;5B";' in terminal_script
    assert 'return "\\u001b[1;5C";' in terminal_script
    assert 'return "\\u001b[1;5D";' in terminal_script
    assert "const terminalCtrlValue = (value) =>" in terminal_script
    assert "if (/^[A-Za-z]$/.test(value))" in terminal_script
    assert 'if (value === "_") return "\\u001f";' in terminal_script
    assert "if (!action) value = terminalCtrlValue(value);" in terminal_script
    assert "let reconnectAllowed = true;" in terminal_script
    assert "reconnectAllowed = !terminalCloseMessage;" in terminal_script
    assert 'document.querySelectorAll("[data-terminal-switch]")' in terminal_script
    assert (
        'if (!targetId || !shellTerminal || targetRole !== "shell") return false;'
        in terminal_script
    )
    assert "connectionEpoch += 1;" in terminal_script
    assert "term.reset();" in terminal_script
    assert "closeMoreKeys({ focus: false });" in terminal_script
    assert "terminalCommandClearTarget.value = targetId" in terminal_script
    assert "history.pushState(" in terminal_script
    assert 'window.addEventListener("popstate"' in terminal_script
    assert 'new CustomEvent("termroom:terminal-switched"' in terminal_script
    assert "if (epoch !== connectionEpoch || nextSocket !== socket) return;" in terminal_script
    assert re.search(
        r'document\.addEventListener\("visibilitychange", \(\) => \{\s*'
        r'if \(document\.visibilityState !== "visible"\) \{\s*'
        r"cancelPresenceRequest\(\);\s*return;\s*\}\s*"
        r"if \(reconnectAllowed && socket\?\.readyState === WebSocket\.CLOSED\) \{",
        terminal_script,
    )
    assert re.search(
        r'window\.addEventListener\("focus", \(\) => \{\s*'
        r"if \(reconnectAllowed && socket\?\.readyState === WebSocket\.CLOSED\) \{\s*"
        r"connect\(\);\s*return;\s*\}\s*"
        r"scheduleActivityAcknowledge\(\);\s*\}\);",
        terminal_script,
    )


def test_template_static_asset_versions_match_file_hashes() -> None:
    hashed_assets: dict[str, set[str]] = {}
    fixed_versions: dict[str, set[str]] = {}
    templates_dir = VENDOR_DIR.parents[1] / "templates"
    pattern = re.compile(
        r"url_for\('static', path='([^']+)'\) }}\?v={{ static_asset_version\('([^']+)'\) }}"
    )
    fixed_pattern = re.compile(r"url_for\('static', path='([^']+)'\) }}\?v=([0-9.]+)")
    for template in templates_dir.glob("*.html"):
        source = template.read_text(encoding="utf-8")
        for asset, hashed_asset in pattern.findall(source):
            assert asset == hashed_asset
            hashed_assets.setdefault(asset, set()).add(hashed_asset)
        for asset, version in fixed_pattern.findall(source):
            fixed_versions.setdefault(asset, set()).add(version)

    assert all(len(assets) == 1 for assets in hashed_assets.values())
    expected_app_assets = {
        "app.css",
        "app.js",
        "mobile_scrollback.css",
        "mobile_scrollback.js",
        "remote_run.js",
        "terminal-font.css",
        "terminal.js",
        "terminal_selection.js",
    }
    assert expected_app_assets <= hashed_assets.keys()
    for asset, paths in hashed_assets.items():
        assert paths == {asset}
        path = VENDOR_DIR.parent / asset
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        assert static_asset_version(path) == expected
    assert fixed_versions["vendor/addon-unicode11.js"] == {"0.8.0"}


def test_pane_controls_reject_stale_state_and_never_parse_terminal_text() -> None:
    ROOT = Path(__file__).resolve().parents[1]
    script = (ROOT / "termroom/static/terminal.js").read_text()
    handler = script[
        script.index("  const acceptPaneMode =") : script.index(
            "  term.options.screenReaderMode", script.index("  const acceptPaneMode =")
        )
    ]
    probe = (
        """
const assert = require('node:assert/strict');
const host = new EventTarget();
host.dataset = {paneModeCapable:'true',terminalId:'t'};
let modeEvents = 0;
host.addEventListener('termroom:pane-mode', () => modeEvents++);
let paneMode = null;
let paneModeGeneration = null;
let paneModeRevision = 0;
"""
        + handler
        + """
const mode = {kind:'pane_mode',terminal_id:'t',generation:'a',revision:1,
 available:true,alternate:true,mouse_tracking:false};
acceptPaneMode(mode);
assert.equal(paneMode.alternate, true);
assert.ok(Object.isFrozen(paneMode));
acceptPaneMode({...mode,revision:2,alternate:false});
assert.equal(paneMode.alternate, false);
acceptPaneMode(mode);
acceptPaneMode({...mode,generation:'old',revision:99});
acceptPaneMode({...mode,terminal_id:'other',revision:99});
assert.equal(paneMode.revision, 2);
assert.equal(modeEvents, 2);
acceptPaneMode({kind:'pane_mode',terminal_id:'t',generation:'a',revision:3,available:false});
assert.equal(paneMode, null);
assert.equal(modeEvents, 3);
acceptPaneMode(mode);
assert.equal(paneMode, null);
paneMode = null; // each new socket resets state, socket/epoch guards reject old events
paneModeGeneration = null;
paneModeRevision = 0;
acceptPaneMode({...mode,generation:'b'});
assert.equal(paneMode.generation, 'b');
host.dataset.paneModeCapable = 'false';
paneMode = null;
paneModeGeneration = null;
paneModeRevision = 0;
acceptPaneMode({...mode,generation:'c'});
assert.equal(paneMode, null);
"""
    )
    result = subprocess.run(["node", "-e", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    binary_at = script.index("if (event.data instanceof ArrayBuffer)")
    output_at = script.index("term.write(event.data", binary_at)
    assert binary_at < output_at
    assert "epoch !== connectionEpoch || nextSocket !== socket" in script
    assert 'data-pane-mode-capable="' in (ROOT / "termroom/templates/terminal.html").read_text()


def test_recovered_socket_retires_only_its_page_connection_error() -> None:
    source = (VENDOR_DIR.parent / "terminal.js").read_text()
    mode = source[source.index("  const acceptPaneMode ="):
                  source.index("  term.options.screenReaderMode")]
    connect = source[source.index("  const connect = () => {"):
                     source.index("  let nextTerminalDataIsUserInput")]
    probe = r"""
const assert = require('node:assert/strict');
const host = new EventTarget();
host.dataset = {terminalId:'t',paneModeCapable:'true'};
let connectionError = true, unrelatedError = true;
const document = {querySelector(selector) {
  if (selector === '[data-terminal-connection-error]') {
    return connectionError ? {remove(){connectionError=false;}} : null;
  }
  if (selector === '.terminal-error-banner') {
    return {remove(){unrelatedError=false;}};
  }
}};
const window = {clearTimeout(){},setTimeout(){},dispatchEvent(){}};
const location = {protocol:'https:',host:'fixture.invalid'};
let socket=null,connectionEpoch=0,reconnectTimer,reconnectDelay=500,reconnectAllowed=true;
let paneMode=null,paneModeGeneration=null,paneModeRevision=0,isConnected=false,status;
let outputRenderSequence=0,pendingActivityAt=0,acknowledgedActivityAt=0,renderedActivityAt=0;
const term={write(_,callback){callback();},focus(){}};
const coarsePrimaryPointer={matches:true};
const tr=x=>x, setStatus=(value,connected=false)=>{status=value;isConnected=connected;};
const scheduleResize=()=>{},updatePresence=()=>{},cancelPresenceRequest=()=>{};
const scheduleActivityAcknowledge=()=>{};
const CustomEvent=class {};
class WebSocket {
  constructor(){this.listeners={};}
  addEventListener(name,callback){this.listeners[name]=callback;}
  close(){this.listeners.close({code:1006});}
  emit(name,event={}){this.listeners[name](event);}
}
MODE_HANDLER
CONNECT_HANDLER
const control = (revision,extra={}) => ({data:new TextEncoder().encode(JSON.stringify({
  kind:'pane_mode',terminal_id:'t',generation:'current',revision,
  alternate:false,mouse_tracking:false,...extra
})).buffer});
connect();const old=socket;
old.emit('open');
assert.equal(connectionError,true,'transport open alone must not dismiss an SSH failure');
connect();const current=socket;current.emit('open');
const liveStatus=status;
old.emit('close',{code:4403});
old.emit('error');
old.emit('message',control(1));
assert.equal(status,liveStatus,'old errors/close cannot replace current connection status');
assert.equal(connectionError,true,'an old connection cannot prove recovery');
current.emit('message',control(1,{available:false}));
current.emit('message',{data:'ssh: connection timed out'});
assert.equal(connectionError,true,'unavailable pane and arbitrary text are not recovery');
current.emit('message',control(2,{terminal_id:'other'}));
assert.equal(connectionError,true,'another terminal cannot clear this error');
current.emit('message',control(2));
assert.equal(connectionError,false,'a verified current pane must retire its stale timeout');
assert.equal(unrelatedError,true,'unrelated page errors must remain visible');
current.emit('close',{code:4403});
assert.equal(isConnected,false);
assert.equal(status,'terminal.status.rejected','a genuine current rejection must remain visible');
""".replace("MODE_HANDLER", mode).replace("CONNECT_HANDLER", connect)
    result = subprocess.run(["node", "-e", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_command_editor_uses_native_paste_once_and_keeps_plain_history() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (VENDOR_DIR.parent / "terminal.js").read_text()
    input_handler = script[script.index("  let pastedCommand = null;"):
                           script.index("  term.onBinary(")]
    submit = script[script.index('  commandForm?.addEventListener("submit",'):
                    script.index('  commandInput?.addEventListener("input",')]
    probe = r"""
const assert = require('node:assert/strict');
const {Terminal} = require('./termroom/static/vendor/xterm.js');
const term = new Terminal();
term._core.textarea = {value:''}; // paste clears the native textarea after dispatch
const sent = [], send = value => sent.push(value);
let nextTerminalDataIsUserInput = false;
const hasUserInputSignal = false;
const commandInput = {value:'',blur(){}}, composerComposing = false;
const updateCommandComposer = () => {}, setComposerOpen = () => {};
let submit;
const commandForm = {addEventListener(_, callback){submit = callback;}};
INPUT_HANDLER
SUBMIT_HANDLER
const text = '격리된 한국어 문장\n두 번째 줄';
(async () => {
  for (const bracketed of [true, false]) {
    await new Promise(resolve => term.write(bracketed ? '\x1b[?2004h' : '\x1b[?2004l', resolve));
    commandInput.value = text;
    submit({preventDefault(){}});
    const frame = sent.at(-1);
    assert.equal(frame.kind, 'command');
    assert.equal(frame.data, text, 'history must contain plain user text');
    const normalized = text.replaceAll('\n','\r');
    const expected = bracketed ? '\x1b[200~'+normalized+'\x1b[201~' : normalized;
    assert.equal(frame.paste_data, expected);
  }
  assert.equal(sent.length, 2, 'one frame per submit, not a duplicate input frame');
  term.input('\t');
  assert.equal(sent.at(-1).kind, 'input', 'native keys must not reuse command state');
  assert.equal(sent.at(-1).data, '\t');
  term.dispose();
})().catch(error => {console.error(error);process.exitCode=1;});
""".replace("INPUT_HANDLER", input_handler).replace("SUBMIT_HANDLER", submit)
    result = subprocess.run(["node", "-e", probe], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_terminal_switch_drains_old_writes_before_reassigning_identity() -> None:
    script = (VENDOR_DIR.parent / "terminal.js").read_text()
    cancel = script[script.index("  const cancelPresenceRequest = () => {"):
                    script.index("  const updatePresence = async () => {")]
    switch = script[script.index("  const resetTerminalActivityState ="):
                    script.index("  terminalTabs.forEach((tab) => {")]
    message = script[script.index('    nextSocket.addEventListener("message",'):
                     script.index('    nextSocket.addEventListener("close",')]
    probe = r"""
const assert = require('node:assert/strict');
const events = [], connections = [], queue = [];
const host = {dataset:{terminalId:'A',workspaceId:'w',terminalRole:'shell'}};
const tabs = ['A','B','C'].map(id => ({dataset:{terminalSwitch:id,terminalRole:'shell'},
  classList:{toggle(){}},setAttribute(){},removeAttribute(){},getAttribute(){return '/'+id;}}));
const terminalTabs = tabs;
const window = {clearTimeout(){},dispatchEvent(e){events.push(e);}};
const document = {body:{classList:{remove(){}}},querySelector(){return null;}};
const CustomEvent = function(kind, options){this.type=kind;this.detail=options.detail;};
const WebSocket = {OPEN:1,CLOSING:2};
let connectionEpoch=0, pendingTerminalId='', socket={readyState:1,close(){this.readyState=3;}};
let presenceAborted=false;
let presenceRequest={controller:{abort(){presenceAborted=true;}}};
let shellTerminal=true, reconnectTimer, otherInputTimer, reconnectAllowed=true, reconnectDelay=500;
let presenceInitialized=true,lastInputRevision=1,activityAckTimer=0,pendingActivityAt=10,
 acknowledgedActivityAt=0,renderedActivityAt=0,outputRenderSequence=0,acknowledgedRenderSequence=0;
const terminalOutputLink=null,terminalManageForm=null,terminalNameInput=null,
 terminalCommandClearTarget=null;
const history={pushState(){}},location={href:'/A'},terminalHistoryState=id=>({id});
const setComposerOpen=()=>{},closeMoreKeys=()=>{},closeTerminalPopovers=()=>{},
 setStatus=()=>{},tr=x=>x;
const term={buffer:[],write(data,callback){
 queue.push(()=>{if(data)this.buffer.push(data);callback();});},
 reset(){this.buffer=[];},clearSelection(){}};
const connect=()=>{connections.push(host.dataset.terminalId);connectionEpoch++;
  socket={readyState:1,close(){this.readyState=3;}};};
const scheduleActivityAcknowledge=()=>events.push({type:'ack-scheduled',
 terminalId:host.dataset.terminalId});
const acceptPaneMode=()=>{};
CANCEL_HANDLER
function attachMessage(nextSocket,epoch) {
  let receive;
  nextSocket.addEventListener=(_,listener)=>{receive=listener;};
MESSAGE_HANDLER
  return receive;
}
SWITCH_HANDLER
const oldSocket=socket, oldReceive=attachMessage(oldSocket,connectionEpoch);
oldReceive({data:'OLD-A'});
assert.equal(switchShellTerminal(tabs[1]),true);
assert.equal(presenceAborted,true,'switching must cancel the in-flight presence request');
assert.equal(host.dataset.terminalId,'A','identity must not change until native queue drains');
assert.equal(connections.length,0);
queue.shift()(); // old A parses while still A; stale callback is ignored
assert.equal(outputRenderSequence,0);
assert.equal(events.filter(e=>e.type==='termroom:terminal-output').length,0);
queue.shift()(); // drain fence resets before assigning B and reconnecting
assert.equal(host.dataset.terminalId,'B');
assert.deepEqual(term.buffer,[]);
assert.deepEqual(connections,['B']);
const bReceive=attachMessage(socket,connectionEpoch);
pendingActivityAt=20;
bReceive({data:'CURRENT-B'});queue.shift()();
assert.deepEqual(term.buffer,['CURRENT-B']);
assert.equal(events.filter(e=>e.type==='termroom:terminal-output').at(-1).detail.terminal_id,'B');
assert.equal(renderedActivityAt,20);
// Multiple pending clicks: only the latest target fence may reset/connect.
switchShellTerminal(tabs[2]);switchShellTerminal(tabs[1]);
queue.shift()();assert.equal(host.dataset.terminalId,'B');
queue.shift()();assert.deepEqual(connections,['B','B']);
// Same terminal ID, new socket: stale reconnect callback must not render/ack.
const staleReceive=attachMessage(socket,connectionEpoch);
staleReceive({data:'OLD-CONNECTION'});socket={readyState:1};
const count=outputRenderSequence;queue.shift()();assert.equal(outputRenderSequence,count);
assert.equal(pendingTerminalId,'');
""".replace("CANCEL_HANDLER", cancel).replace("MESSAGE_HANDLER", message).replace(
    "SWITCH_HANDLER", switch
)
    result = subprocess.run(["node", "-e", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_terminal_presence_poll_does_not_overlap_slow_requests() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise terminal presence polling")
    source = (VENDOR_DIR.parent / "terminal.js").read_text()
    cancel_start = source.index("  const cancelPresenceRequest = () => {")
    start = source.index("  const updatePresence = async () => {", cancel_start)
    end = source.index("\n\n  const send =", start)
    presence = source[start:end].replace("  const updatePresence =", "const updatePresence =")
    cancel = source[cancel_start:start].replace(
        "  const cancelPresenceRequest =", "const cancelPresenceRequest ="
    )
    probe = r"""
const assert=require('node:assert/strict');
let isConnected=true,presenceInitialized=true,lastInputRevision=4,otherInputTimer=77;
let presenceRequest=null,connectionEpoch=1,launched=0,outstanding=0,maximum=0;
let failNext=false;
const urls=[],jsonBodies=[],statusUpdates=[],cleared=[],scheduled=[];
const host={dataset:{terminalId:'same-terminal',deviceId:'fixture-device'}};
const window={
  clearTimeout(value){cleared.push(value);},
  setTimeout(_callback,delay){scheduled.push(delay);return scheduled.length;}
};
const document={visibilityState:'visible'};
const tr=value=>value;
const setStatusMessage=value=>statusUpdates.push(value);
global.fetch=(_url,options)=>{
  launched++;outstanding++;maximum=Math.max(maximum,outstanding);
  urls.push(_url);
  let active=true;
  options.signal.addEventListener(
    'abort',()=>{if(active){active=false;outstanding--;}}, {once:true}
  );
  if(failNext){
    failNext=false;active=false;outstanding--;
    return Promise.reject(new Error('fixture failure'));
  }
  return Promise.resolve({ok:true,json:()=>new Promise(resolve=>{
    jsonBodies.push(value=>{if(active){active=false;outstanding--;}resolve(value);});
  })});
};
__PRESENCE_CANCEL__
__PRESENCE_UPDATE__
(async()=>{
  const state=()=>({presenceInitialized,lastInputRevision,otherInputTimer,
    statusUpdates:[...statusUpdates],cleared:[...cleared],scheduled:[...scheduled]});
  const old=updatePresence();
  const overlapping=updatePresence();
  assert.equal(launched,1,'slow polls must be single-flight');
  await Promise.resolve();
  assert.equal(jsonBodies.length,1,'the first response body must be pending');
  const before=state();
  document.visibilityState='hidden';
  cancelPresenceRequest();
  document.visibilityState='visible';
  const reopened=updatePresence();
  assert.equal(launched,2,'same-terminal reopen must start its own request');
  jsonBodies[0]({count:2,input_revision:9,last_input_device_id:'other-device'});
  await Promise.all([old,overlapping]);
  assert.deepEqual(
    state(),before,'a successful response queued before cancellation must not update reopened state'
  );
  const duplicateAfterOld=updatePresence();
  await duplicateAfterOld;
  assert.equal(launched,2,'the old finally must not clear the reopened request guard');
  assert.equal(outstanding,1);
  await Promise.resolve();
  assert.equal(jsonBodies.length,2);
  jsonBodies[1]({count:1,input_revision:4});
  await reopened;
  assert.equal(
    statusUpdates.at(-1),'terminal.status.connected','the current response must still update status'
  );
  assert.equal(outstanding,0);
  assert.equal(maximum,1);

  failNext=true;
  const beforeFailure=launched;
  await updatePresence();
  assert.equal(launched,beforeFailure+1);
  assert.equal(presenceRequest,null,'failed requests must release the single-flight guard');
  const recovered=updatePresence();
  await Promise.resolve();
  assert.equal(jsonBodies.length,3);
  jsonBodies[2]({count:1,input_revision:4});
  await recovered;
  assert.equal(launched,beforeFailure+2,'a poll after failure must recover');

  document.visibilityState='hidden';
  const beforeHidden=launched;
  await updatePresence();
  assert.equal(launched,beforeHidden,'hidden pages must not fetch presence');
  document.visibilityState='visible';
  const visible=updatePresence();
  await Promise.resolve();
  assert.equal(jsonBodies.length,4);
  jsonBodies[3]({count:1,input_revision:4});
  await visible;
  assert.equal(launched,beforeHidden+1,'visible pages must resume polling');

  const oldTerminal=updatePresence();
  await Promise.resolve();
  assert.equal(jsonBodies.length,5);
  const beforeSwitch=state();
  host.dataset.terminalId='other-terminal';
  connectionEpoch++;
  cancelPresenceRequest();
  const switched=updatePresence();
  assert.equal(urls.at(-1),'/api/terminals/other-terminal/presence');
  jsonBodies[4]({count:2,input_revision:12,last_input_device_id:'other-device'});
  await oldTerminal;
  assert.deepEqual(state(),beforeSwitch,'old terminal/epoch success must not update current state');
  await Promise.resolve();
  assert.equal(jsonBodies.length,6);
  jsonBodies[5]({count:1,input_revision:4});
  await switched;
  assert.equal(outstanding,0);
  assert.equal(maximum,1);
  process.stdout.write('PRESENCE_ASSERTIONS_COMPLETE\n');
})().catch(error=>{console.error(error);process.exitCode=1;});
""".replace("__PRESENCE_CANCEL__", cancel).replace("__PRESENCE_UPDATE__", presence)
    result = subprocess.run(
        [node, "-e", probe], capture_output=True, text=True, timeout=3
    )
    assert result.returncode == 0, result.stderr
    assert "PRESENCE_ASSERTIONS_COMPLETE" in result.stdout, result.stdout


def test_remote_run_form_reuses_one_submission_identity_until_intent_changes() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the Remote Run form")
    script = r"""
const fs = require("fs");
const vm = require("vm");

class Target {
  constructor() { this.listeners = {}; this.hidden = false; this.disabled = false; }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  emit(type, event = {}) { for (const handler of this.listeners[type] || []) handler(event); }
  scrollIntoView() {}
}

const source = new Target();
source.value = process.argv[2];
source.checked = true;
const submit = new Target();
const error = new Target();
const archive = new Target();
archive.files = [];
const form = new Target();
form.dataset = { createUrl: "/api/remote-runs", uploadPrefix: "/api/remote-runs", csrf: "csrf" };
form.values = {
  target_computer_id: "target", command: "echo first", source_workspace_id: "source",
  source_path: ".", source_url: "https://example.test/repo.git",
};
form.querySelectorAll = (selector) => selector === "input[name='source_kind']" ? [source] : [];
form.querySelector = (selector) => ({
  "#remote-run-form-error": error,
  "button[type='submit']": submit,
  "#remote-run-upload-progress": null,
  "input[name='archive']": archive,
}[selector] || null);
form.setAttribute = () => {};
form.removeAttribute = () => {};

global.document = {
  documentElement: { lang: "en" },
  querySelector: (selector) => selector === "#remote-run-form" ? form : null,
  querySelectorAll: () => [],
};
global.FormData = class {
  constructor(value) { this.value = value; }
  get(key) { return this.value.values[key] || ""; }
};
let sequence = 0;
const requestIds = [];
const requestBodies = [];
global.window = {
  crypto: { randomUUID: () => `run-${++sequence}` },
  location: { assign: () => {} },
  TermroomI18n: {},
};
global.fetch = async (_url, options) => {
  const body = JSON.parse(options.body);
  const id = body.id;
  requestIds.push(id);
  requestBodies.push(body);
  if (requestIds.length === 1) throw new Error("response lost");
  return { ok: true, json: async () => ({ ok: true, detail_url: `/remote-runs/${id}` }) };
};
let uploadOutcomes = ["error", "load", "load"];
global.XMLHttpRequest = class {
  constructor() {
    this.listeners = {}; this.upload = new Target(); this.status = 202;
    this.response = { ok: true };
  }
  open() {}
  setRequestHeader() {}
  addEventListener(type, handler) { this.listeners[type] = handler; }
  send() { queueMicrotask(() => this.listeners[uploadOutcomes.shift()]()); }
};

vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
const submitForm = async () => {
  const event = { preventDefault() {} };
  for (const handler of form.listeners.submit || []) await handler(event);
};

(async () => {
  const first = submitForm();
  await submitForm();
  await first;
  await submitForm();

  process.stdout.write(JSON.stringify({ ids: requestIds, bodies: requestBodies }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    for source_kind in ("workspace", "git"):
        result = subprocess.run(
            [node, "-e", script, str(VENDOR_DIR.parent / "remote_run.js"), source_kind],
            check=True,
            capture_output=True,
            text=True,
        )
        observed = json.loads(result.stdout)
        assert observed["ids"] == ["run-1", "run-1"]
        assert observed["bodies"][0] == observed["bodies"][1]
        assert observed["bodies"][0]["source_kind"] == source_kind
        if source_kind == "git":
            assert observed["bodies"][0]["source_url"] == "https://example.test/repo.git"


def test_remote_run_archive_retry_checks_status_before_retransmitting() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the Remote Run form")
    script = r"""
const fs=require("fs"),vm=require("vm");
class T {
  constructor(){this.l={};this.disabled=false;this.hidden=false;this.checked=false;this.files=[];this.value=""}
  addEventListener(n,f){(this.l[n]??=[]).push(f)}
  emit(n,e={}){for(const f of this.l[n]??[])f(e)}
  scrollIntoView(){}
}

function harness(scenario) {
  const source=new T(); source.value="archive"; source.checked=true;
  const submit=new T(), error=new T(), archive=new T(), form=new T();
  const fileA={name:"same.zip",size:7,lastModified:123,bytes:"A"};
  const fileB={name:"same.zip",size:7,lastModified:123,bytes:"B"};
  archive.files=[fileA];
  form.dataset={createUrl:"/api/remote-runs",uploadPrefix:"/api/remote-runs",csrf:"x"};
  form.values={target_computer_id:"target",command:"run"};
  form.querySelectorAll=(s)=>s==="input[name='source_kind']"?[source]
    :s==="button, input, select, textarea"?[source,submit,archive]:[];
  form.querySelector=(s)=>({"#remote-run-form-error":error,"button[type='submit']":submit,"#remote-run-upload-progress":null,"input[name='archive']":archive}[s]||null);
  form.setAttribute=()=>{}; form.removeAttribute=()=>{};
  let sequence=0, statusCount=0;
  const createIds=[], uploadBodies=[], uploadUrls=[], assignments=[];
  const statusResponses = {
    "create-loss": [{state:"preparing",phase:"waiting_upload"}],
    uploading: [{state:"preparing",phase:"uploading"},{state:"preparing",phase:"uploading"}],
    accepted: [{state:"preparing",phase:"uploading"},{state:"preparing",phase:"checking"}],
    unknown: [{state:"preparing"},{state:"preparing"}],
    replacement: [{state:"preparing",phase:"waiting_upload"}],
  }[scenario] || [];
  const createOutcomes = scenario==="create-loss" || scenario==="replacement"
    ? ["lost","ok"] : ["ok","ok"];
  const uploadOutcomes = scenario==="uploading" || scenario==="accepted" || scenario==="unknown"
    ? ["error"] : ["load"];
  const context={
    console,queueMicrotask,
    document:{documentElement:{lang:"en"},querySelector:s=>s==="#remote-run-form"?form:null,querySelectorAll:()=>[]},
    FormData:class{constructor(value){this.value=value}get(k){return this.value.values[k]||""}},
    window:{crypto:{randomUUID:()=>`run-${++sequence}`},location:{assign:url=>assignments.push(url)},TermroomI18n:{}},
    fetch:async(_url,options)=>{
      if(options?.body){
        const body=JSON.parse(options.body); createIds.push(body.id);
        if(createOutcomes.shift()==="lost") throw new Error("create response lost");
        return {ok:true,json:async()=>({ok:true,detail_url:`/remote-runs/${body.id}`})};
      }
      statusCount++;
      const response=statusResponses.shift() || {state:"preparing",phase:"waiting_upload"};
      return {ok:true,json:async()=>response};
    },
    XMLHttpRequest:class{
      constructor(){this.l={};this.upload=new T();this.status=202;this.response={ok:true}}
      open(_method,url){this.url=url;uploadUrls.push(url)}
      setRequestHeader(){}
      addEventListener(n,f){this.l[n]=f}
      send(body){
        uploadBodies.push(body);
        const outcome=uploadOutcomes.shift()||"load";
        queueMicrotask(()=>this.l[outcome]());
      }
    },
  };
  vm.runInNewContext(fs.readFileSync(process.argv[1],"utf8"),context);
  const send=async()=>{for(const f of form.l.submit||[])await f({preventDefault(){}})};
  return {
    source,archive,fileA,fileB,form,send,createIds,uploadBodies,uploadUrls,assignments,
    statusCount:()=>statusCount,error,
  };
}

(async()=>{
  const scenario=process.argv[2];
  const h=harness(scenario);
  if(scenario==="replacement"){
    await h.send();
    h.archive.files=[h.fileB];
    h.form.emit("change");
    await h.send();
  } else if(scenario==="accepted"){
    await h.send();
    await h.send();
    await h.send();
  } else {
    await h.send();
    await h.send();
  }
  process.stdout.write(JSON.stringify({
    createIds:h.createIds,
    statusCalls:h.statusCount(),
    uploadCount:h.uploadBodies.length,
    uploadedA:h.uploadBodies[0]===h.fileA,
    uploadedB:h.uploadBodies[0]===h.fileB,
    assignments:h.assignments,
    uploadUrls:h.uploadUrls,
    error:h.error.textContent||"",
  }));
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    expected = {
        "create-loss": {
            "createIds": ["run-1", "run-1"],
            "statusCalls": 1,
            "uploadCount": 1,
            "uploadedA": True,
            "uploadedB": False,
            "assignments": ["/remote-runs/run-1"],
        },
        "uploading": {
            "createIds": ["run-1", "run-1"],
            "statusCalls": 2,
            "uploadCount": 1,
            "uploadedA": True,
            "uploadedB": False,
            "assignments": [],
        },
        "accepted": {
            "createIds": ["run-1", "run-1"],
            "statusCalls": 2,
            "uploadCount": 1,
            "uploadedA": True,
            "uploadedB": False,
            "assignments": ["/remote-runs/run-1"],
        },
        "unknown": {
            "createIds": ["run-1", "run-1"],
            "statusCalls": 2,
            "uploadCount": 1,
            "uploadedA": True,
            "uploadedB": False,
            "assignments": [],
        },
        "replacement": {
            "createIds": ["run-1", "run-2"],
            "statusCalls": 1,
            "uploadCount": 1,
            "uploadedA": False,
            "uploadedB": True,
            "assignments": ["/remote-runs/run-2"],
        },
    }
    for scenario, assertions in expected.items():
        result = subprocess.run(
            [node, "-e", script, str(VENDOR_DIR.parent / "remote_run.js"), scenario],
            check=True,
            capture_output=True,
            text=True,
        )
        observed = json.loads(result.stdout)
        for key, value in assertions.items():
            assert observed[key] == value, (scenario, key, observed)
        expected_upload_url = (
            "/api/remote-runs/run-2/archive?filename=same.zip"
            if scenario == "replacement"
            else "/api/remote-runs/run-1/archive?filename=same.zip"
        )
        assert observed["uploadUrls"] == [expected_upload_url]


def test_remote_run_archive_validation_and_uploading_recovery_stay_retryable() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the Remote Run form")
    script = r"""
const fs=require("fs"),vm=require("vm");
class T {
  constructor(){this.l={};this.disabled=false;this.hidden=false}
  addEventListener(n,f){(this.l[n]??=[]).push(f)}
  emit(n){for(const f of this.l[n]??[])f()}
  scrollIntoView(){}
}
const source=new T(); source.value="archive"; source.checked=true;
const submit=new T(), error=new T(), archive=new T(), form=new T(); archive.files=[];
form.dataset={createUrl:"/api/remote-runs",uploadPrefix:"/api/remote-runs",csrf:"x"};
form.values={target_computer_id:"target",command:"run"};
form.querySelectorAll=(s)=>s==="input[name='source_kind']"?[source]
  :s==="button, input, select, textarea"?[source,submit,archive]:[];
form.querySelector=(s)=>({"#remote-run-form-error":error,"button[type='submit']":submit,"#remote-run-upload-progress":null,"input[name='archive']":archive}[s]||null);
form.setAttribute=()=>{};form.removeAttribute=()=>{};
let calls=0, assigned=0;
const context={
  console,queueMicrotask,
  document:{documentElement:{lang:"en"},querySelector:s=>s==="#remote-run-form"?form:null,querySelectorAll:()=>[]},
  FormData:class{constructor(){}get(k){return form.values[k]||""}},
  window:{crypto:{randomUUID:()=>"run-1"},location:{assign:()=>assigned++},TermroomI18n:{}},
  fetch:async(_url, options)=>{
    calls++;
    if(options.body)return{ok:true,json:async()=>({ok:true,detail_url:"/remote-runs/run-1"})};
    return{ok:true,json:async()=>({ok:true,state:"preparing",phase:"uploading"})};
  },
  XMLHttpRequest:class{
    constructor(){this.l={};this.upload=new T();this.status=202;this.response={ok:true}}
    open(){} setRequestHeader(){} addEventListener(n,f){this.l[n]=f}
    send(){queueMicrotask(()=>this.l.error())}
  },
};
vm.runInNewContext(fs.readFileSync(process.argv[1],"utf8"),context);
const send=async()=>{for(const f of form.l.submit||[])await f({preventDefault(){}})};
(async()=>{
  await send();
  const empty=[calls,error.textContent];
  archive.files=[{name:"same.zip"}];
  form.emit("change");
  await send();
  process.stdout.write(JSON.stringify({empty,calls,assigned}));
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(
        [node, "-e", script, str(VENDOR_DIR.parent / "remote_run.js")],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        "empty": [0, "remote_run.error.zip_required"],
        "calls": 2,
        "assigned": 0,
    }


def test_recursive_file_search_keeps_live_controls_consistent() -> None:
    templates_dir = VENDOR_DIR.parents[1] / "templates"
    files_template = (templates_dir / "files.html").read_text(encoding="utf-8")
    results_template = (templates_dir / "_file_results.html").read_text(encoding="utf-8")
    app_script = (VENDOR_DIR.parent / "app.js").read_text(encoding="utf-8")

    assert "data-file-visibility-form" in files_template
    assert "data-file-visibility-query" in files_template
    assert '<form id="file-bulk-form"' in files_template
    assert "data-file-search-metadata" in results_template
    assert "const syncFileVisibility = () =>" in app_script
    assert "fileVisibilityQuery.value = metadata.dataset.query" in app_script
    assert "syncFileVisibility();" in app_script


def test_mobile_editor_toolbar_uses_balanced_rows() -> None:
    stylesheet = (VENDOR_DIR.parents[1] / "static/app.css").read_text(encoding="utf-8")
    assert (
        """@media (max-width: 520px) {
  .editor-toolbar {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
"""
        in stylesheet
    )


def test_mobile_terminal_more_keys_panel_is_anchored_to_the_viewport() -> None:
    stylesheet = (VENDOR_DIR.parents[1] / "static/app.css").read_text(encoding="utf-8")
    assert (
        """@media (max-width: 520px) {
  .quick-keys {
    position: relative;
  }

  .more-keys {
    position: static;
  }

  .more-keys-panel {
    right: 12px;
    left: 12px;
    width: auto;
    min-width: 0;
  }
}
"""
        in stylesheet
    )


def test_mobile_workspace_usage_popover_is_anchored_to_the_viewport() -> None:
    stylesheet = (VENDOR_DIR.parents[1] / "static/app.css").read_text(encoding="utf-8")
    assert (
        """@media (max-width: 1023px) {
  .workspace-mobile-actions .workspace-usage-popover {
    position: fixed;
    top: calc(var(--topbar-height) + env(safe-area-inset-top) + 8px);
    right: auto;
    left: 50%;
    max-height: calc(100dvh - var(--topbar-height) - env(safe-area-inset-top) - 20px);
    overflow-y: auto;
    transform: translateX(-50%);
    overscroll-behavior: contain;
  }
}"""
        in stylesheet
    )


def test_global_header_layers_settings_menu_above_transformed_page_actions() -> None:
    stylesheet = (VENDOR_DIR.parent / "app.css").read_text(encoding="utf-8")
    header_rule = stylesheet.split(".app-header {", 1)[1].split("}", 1)[0]

    assert "position: relative;" in header_rule
    assert "z-index: 60;" in header_rule
    assert "backdrop-filter: blur(14px);" in header_rule


def test_remote_run_result_zip_is_the_primary_completed_run_action() -> None:
    templates_dir = VENDOR_DIR.parents[1] / "templates"
    wait_template = (templates_dir / "remote_run_wait.html").read_text(encoding="utf-8")
    workspace_template = (templates_dir / "workspace_base.html").read_text(encoding="utf-8")
    collect_template = (templates_dir / "remote_run_collect.html").read_text(encoding="utf-8")

    result_link = 'class="primary-button" href="/remote-runs/{{'
    assert result_link in wait_template
    assert result_link in workspace_template
    assert 'class="secondary-button" href="/remote-runs/{{ run.id }}/result.zip"' in (
        collect_template
    )
    assert 'class="primary-button" type="submit"' in collect_template


def test_remote_workspace_connection_freshness_is_transition_deduped() -> None:
    templates_dir = VENDOR_DIR.parents[1] / "templates"
    workspace_template = (templates_dir / "workspace_base.html").read_text(encoding="utf-8")
    remote_run_script = (VENDOR_DIR.parent / "remote_run.js").read_text(encoding="utf-8")

    status_wrapper = re.search(r"<span[^>]*data-run-workspace-connection[^>]*>", workspace_template)
    assert status_wrapper is not None
    assert 'role="status"' in status_wrapper.group(0)
    assert 'aria-live="polite"' in status_wrapper.group(0)
    assert 'aria-atomic="true"' in status_wrapper.group(0)
    assert " hidden" not in status_wrapper.group(0)

    visual_chip = re.search(
        r"<span[^>]*data-run-workspace-connection-chip[^>]*>", workspace_template
    )
    assert visual_chip is not None
    assert 'class="state-chip remote-run-connection-state"' in visual_chip.group(0)
    assert 'aria-hidden="true"' in visual_chip.group(0)
    assert " hidden" in visual_chip.group(0)
    assert "{{ t('remote_run.connection_rechecking') }}" in workspace_template
    assert (
        '<span class="sr-only" data-run-workspace-connection-announcer></span>'
        in workspace_template
    )

    workspace_start = remote_run_script.index(
        '  const workspaceRun = document.querySelector("[data-remote-run-workspace]");'
    )
    workspace_end = remote_run_script.index(
        "  const recentRuns = [...document.querySelectorAll", workspace_start
    )
    workspace_script = remote_run_script[workspace_start:workspace_end]
    for behavior in (
        'querySelector("[data-run-workspace-connection-chip]")',
        'querySelector("[data-run-workspace-connection-announcer]")',
        "let connectionUnavailable = false;",
        "const setConnectionUnavailable = (unavailable) =>",
        "if (connectionUnavailable === unavailable) return;",
        "connectionChip.hidden = !unavailable;",
        'tr("remote_run.connection_rechecking")',
        'if (result.connection !== "online") {',
        "setConnectionUnavailable(true);",
        "setConnectionUnavailable(false);",
        "window.setInterval(poll, 1500);",
    ):
        assert behavior in workspace_script
    assert workspace_script.count("setConnectionUnavailable(true);") == 2
    assert workspace_script.index('if (result.connection !== "online") {') < (
        workspace_script.index('if (!["preparing", "running"].includes(result.state))')
    )
    assert workspace_script.index("setConnectionUnavailable(false);") < (
        workspace_script.index('if (!["preparing", "running"].includes(result.state))')
    )
    assert ".focus(" not in workspace_script


def test_file_run_connection_freshness_is_transition_deduped() -> None:
    templates_dir = VENDOR_DIR.parents[1] / "templates"
    templates = [
        (templates_dir / "editor.html").read_text(encoding="utf-8"),
        (templates_dir / "terminal.html").read_text(encoding="utf-8"),
    ]
    app_script = (VENDOR_DIR.parent / "app.js").read_text(encoding="utf-8")

    for template in templates:
        status_wrapper = re.search(r"<span[^>]*data-file-run-connection(?:\s|>)[^>]*>", template)
        assert status_wrapper is not None
        assert 'role="status"' in status_wrapper.group(0)
        assert 'aria-live="polite"' in status_wrapper.group(0)
        assert 'aria-atomic="true"' in status_wrapper.group(0)
        assert " hidden" not in status_wrapper.group(0)

        visual_chip = re.search(r"<small[^>]*data-file-run-connection-chip[^>]*>", template)
        assert visual_chip is not None
        assert 'class="file-run-error"' in visual_chip.group(0)
        assert 'aria-hidden="true"' in visual_chip.group(0)
        assert " hidden" in visual_chip.group(0)
        assert "{{ t('file_run.connection_offline') }}" in template
        assert '<span class="sr-only" data-file-run-connection-announcer></span>' in template
        assert template.index("data-file-run-state") < template.index("data-file-run-connection")

    file_run_start = app_script.index(
        '  document.querySelectorAll("[data-file-run]").forEach((panel) => {'
    )
    file_run_end = app_script.index("  const setViewportHeight =", file_run_start)
    file_run_script = app_script[file_run_start:file_run_end]
    for behavior in (
        'querySelector("[data-file-run-connection-chip]")',
        'querySelector("[data-file-run-connection-announcer]")',
        "let connectionUnavailable = false;",
        "const setConnectionUnavailable = (unavailable) =>",
        "const renderConnectionAwareResult = (result) =>",
        "if (connectionUnavailable === unavailable) return;",
        "connectionChip.hidden = !unavailable;",
        'tr("file_run.connection_offline")',
        'if (result.connection !== "online") {',
        "setConnectionUnavailable(true);",
        "setConnectionUnavailable(false);",
        "if (active) schedule(250);",
        "const normalDelay = document.hidden ? 5000 : 1000;",
        "schedule(Math.min(15000, normalDelay * (2 ** Math.min(failures, 3))));",
    ):
        assert behavior in file_run_script
    assert file_run_script.count("setConnectionUnavailable(true);") == 2
    connection_handler = file_run_script.index("const renderConnectionAwareResult = (result) =>")
    offline_check = file_run_script.index(
        'if (result.connection !== "online") {', connection_handler
    )
    render_call = file_run_script.index("render(result);", offline_check)
    assert offline_check < render_call
    assert file_run_script.index("setConnectionUnavailable(false);", offline_check) < (render_call)
    poll_start = file_run_script.index("    const poll = async () => {")
    failure_reset = file_run_script.index("failures = 0;", poll_start)
    connection_render = file_run_script.index("renderConnectionAwareResult(result);", failure_reset)
    assert failure_reset < connection_render
    catch_start = file_run_script.index("      } catch {")
    failure_increment = file_run_script.index("failures += 1;", catch_start)
    assert file_run_script.index("setConnectionUnavailable(true);", catch_start) < (
        failure_increment
    )
    assert 'result.connection === "offline"' not in file_run_script
    assert ".focus(" not in file_run_script
    assert "WebSocket" not in file_run_script
    assert "window.location" not in file_run_script


def test_mobile_file_run_terminal_freshness_preserves_action_geometry() -> None:
    stylesheet = (VENDOR_DIR.parent / "app.css").read_text(encoding="utf-8")
    mobile_900_start = stylesheet.index(
        "@media (max-width: 900px)", stylesheet.index(".file-run-terminal-bar")
    )
    mobile_760_start = stylesheet.index("@media (max-width: 760px)", mobile_900_start)
    mobile_900_styles = stylesheet[mobile_900_start:mobile_760_start]

    assert (
        """  .file-run-terminal-bar .file-run-summary {
    position: relative;
  }

  .file-run-terminal-bar [data-file-run-connection] {
    position: absolute;
    inset: 0 0 auto;
    display: flex;
    min-width: 0;
    align-items: center;
    justify-content: flex-end;
    pointer-events: none;
  }

  .file-run-terminal-bar .file-run-summary [data-file-run-connection-chip] {
    box-sizing: border-box;
    display: block;
    overflow: hidden;
    width: 70%;
    padding-inline-start: 8px;
    background: var(--bg-elevated);
    text-overflow: ellipsis;
    white-space: nowrap;
  }
"""
        in mobile_900_styles
    )


def test_remote_workspace_navigation_pending_contract_is_wired() -> None:
    templates_dir = VENDOR_DIR.parents[1] / "templates"
    home_template = (templates_dir / "home.html").read_text(encoding="utf-8")
    open_template = (templates_dir / "workspace_open.html").read_text(encoding="utf-8")
    app_script = (VENDOR_DIR.parent / "app.js").read_text(encoding="utf-8")

    assert "{% if workspace.backend_kind == 'remote' %}" in home_template
    for template in (home_template, open_template):
        assert "data-workspace-open-pending" in template
        assert "data-workspace-opening-label" in template
        assert "data-workspace-open-status-label" in template
        assert "data-workspace-open-announcer" in template
        assert 'role="status"' in template
        assert 'aria-live="polite"' in template
        assert 'aria-atomic="true"' in template

    pending_start = app_script.index("  const pendingWorkspaceLinks = [")
    pending_end = app_script.index("  const workspaceRunMenus = [", pending_start)
    pending_script = app_script[pending_start:pending_end]
    for behavior in (
        'querySelectorAll("[data-workspace-open-pending]")',
        'querySelector("[data-workspace-open-announcer]")',
        'link.setAttribute("aria-busy", "true")',
        'link.setAttribute("aria-disabled", "true")',
        'if (link.dataset.workspaceOpening === "true")',
        "delete link.dataset.workspaceOpening",
        'link.removeAttribute("aria-busy")',
        'link.removeAttribute("aria-disabled")',
        'window.addEventListener("pageshow"',
        "event.metaKey",
        "event.ctrlKey",
        "event.shiftKey",
        "event.altKey",
    ):
        assert behavior in pending_script
    assert pending_script.count("event.preventDefault()") == 1
    assert pending_script.index("modifiedClick") < pending_script.index(
        'if (link.dataset.workspaceOpening === "true")'
    )
    assert pending_script.index('link.setAttribute("aria-busy", "true")') < pending_script.index(
        "workspaceOpenAnnouncer.textContent = openingLabel"
    )
    assert "fetch(" not in pending_script
    assert "window.location" not in pending_script


def test_workspace_command_pending_contract_is_wired() -> None:
    templates_dir = VENDOR_DIR.parents[1] / "templates"
    workspace_template = (templates_dir / "workspace_base.html").read_text(encoding="utf-8")
    app_script = (VENDOR_DIR.parent / "app.js").read_text(encoding="utf-8")

    for marker in (
        "data-workspace-command-run",
        "data-workspace-command-starting-label",
        "data-workspace-command-label",
        "data-workspace-command-announcer",
        'role="status"',
        'aria-live="polite"',
        'aria-atomic="true"',
    ):
        assert marker in workspace_template

    pending_start = app_script.index("  const workspaceCommandForms = [")
    pending_end = app_script.index("  const remoteConnectionChecks = [", pending_start)
    pending_script = app_script[pending_start:pending_end]
    for behavior in (
        'querySelectorAll("[data-workspace-command-run]")',
        'form.setAttribute("aria-busy", "true")',
        'button.setAttribute("aria-busy", "true")',
        'button.setAttribute("aria-disabled", "true")',
        'if (form.dataset.workspaceCommandStarting === "true")',
        "delete form.dataset.workspaceCommandStarting",
        'form.removeAttribute("aria-busy")',
        'button.removeAttribute("aria-busy")',
        'button.removeAttribute("aria-disabled")',
        'window.addEventListener("pageshow"',
    ):
        assert behavior in pending_script
    assert pending_script.count("event.preventDefault()") == 1
    assert "fetch(" not in pending_script
    assert "window.location" not in pending_script


def test_terminal_activity_refresh_is_visible_bounded_and_output_driven() -> None:
    templates_dir = VENDOR_DIR.parents[1] / "templates"
    workspace_template = (templates_dir / "workspace_base.html").read_text(encoding="utf-8")
    app_script = (VENDOR_DIR.parent / "app.js").read_text(encoding="utf-8")

    assert (
        '<details class="workspace-sidebar-footer workspace-usage-menu '
        'workspace-sidebar-usage-menu"'
    ) in workspace_template
    assert (
        'if (!notificationSupported || Notification.permission !== "granted") return;'
    ) in app_script

    terminal_start = app_script.index("  const terminalActivitySummary =")
    terminal_end = app_script.index("  const pendingWorkspaceLinks =", terminal_start)
    terminal_script = app_script[terminal_start:terminal_end]
    assert "const TERMINAL_ACTIVITY_REFRESH_INTERVAL_MS = 15000;" in terminal_script
    assert "const TERMINAL_ACTIVITY_SIGNAL_DEBOUNCE_MS = 500;" in terminal_script
    assert "const TERMINAL_ACTIVITY_RETURN_IDLE_MS = 15000;" in terminal_script
    assert 'const TERMINAL_ACTIVITY_CHANNEL_NAME = "termroom-terminal-activity";' in (
        terminal_script
    )
    assert "const createTerminalActivityChannel = () =>" in terminal_script
    assert "new window.BroadcastChannel(TERMINAL_ACTIVITY_CHANNEL_NAME)" in terminal_script
    assert "const terminalActivityChannel = createTerminalActivityChannel();" in (terminal_script)
    assert "const terminalActivityWorkspaceNeedsRefresh = new Map(" in terminal_script
    assert "const terminalActivityUnreadByTerminal = new Map();" in terminal_script
    assert "const terminalActivityRevisionByTerminal = new Map();" in terminal_script
    assert "const terminalActivityRequestedWorkspaceIds = () =>" in terminal_script
    assert "if (!requestedWorkspaceIds.length) return null;" in terminal_script
    assert "const hasUnread = items.some((item) => Boolean(item?.unread));" in (terminal_script)
    assert "items.length > 0 && !hasUnread" in terminal_script
    assert "const rememberedUnreadTerminalIds = (workspaceId) =>" in terminal_script
    assert "const renderRememberedTerminalActivity = (workspaceId) =>" in terminal_script
    assert "terminalActivityWorkspaceNeedsRefresh.get(workspaceId) === false" in (terminal_script)
    assert "terminalActivityUnreadByTerminal.get(terminalId) === true" in terminal_script
    assert "|| !terminalActivityRequest()" in terminal_script
    assert "const firstSignalInBurst = terminalActivitySignalTimer === 0;" in (terminal_script)
    assert "scheduleTerminalActivityRefresh();" in terminal_script
    assert "scheduleTerminalActivitySignalRefresh" in terminal_script
    assert 'window.addEventListener("termroom:terminal-output"' in terminal_script
    assert 'terminalActivityChannel?.addEventListener("message"' in terminal_script
    assert "terminalActivityRefreshPending" in terminal_script
    assert "window.clearTimeout(terminalActivityRefreshTimer)" in terminal_script
    assert "window.setTimeout(async () =>" in terminal_script
    assert "setInterval" not in terminal_script
    assert 'document.addEventListener("visibilitychange"' in terminal_script
    assert 'window.addEventListener("focus"' in terminal_script
    assert 'window.addEventListener("pageshow"' in terminal_script
    assert 'window.addEventListener("online"' in terminal_script
    assert 'window.addEventListener("pointermove"' in terminal_script
    assert 'window.addEventListener("pointerdown"' in terminal_script
    assert 'window.addEventListener("keydown"' in terminal_script
    assert "const noteTerminalActivityInteraction = () =>" in terminal_script
    assert "idleFor < TERMINAL_ACTIVITY_RETURN_IDLE_MS" in terminal_script
    assert "[...terminalActivityUnreadByTerminal.values()].some(Boolean)" in terminal_script
    assert "const refreshTerminalActivityAfterExternalState = () =>" in terminal_script
    assert "markAllTerminalActivityWorkspacesForRefresh();" in terminal_script
    assert '"termroom:terminal-activity-changed"' in terminal_script
    assert 'data-workspace-id="{{ workspace.id }}"' in workspace_template

    usage_start = app_script.index("  const workspaceUsageViews =")
    usage_end = app_script.index('  document.querySelectorAll("[data-file-run]")', usage_start)
    usage_script = app_script[usage_start:usage_end]
    assert 'view.addEventListener("toggle"' in usage_script
    assert "workspaceUsageViews.some((view) => view.open" in usage_script
    assert "usageRefreshInFlight || document.hidden" in usage_script
    assert "pollWorkspaceUsage();" in usage_script
