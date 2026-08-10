from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_theme_is_resolved_before_the_stylesheet_loads() -> None:
    template = (ROOT / "termroom/templates/base.html").read_text(encoding="utf-8")

    theme_script = template.index('const storageKey = "termroom.theme"')
    stylesheet = template.index("path='app.css'")

    assert theme_script < stylesheet
    assert 'meta name="color-scheme" content="dark light"' in template
    assert "prefers-color-scheme: light" in template
    assert "data-theme-toggle" in template


def test_theme_changes_are_persisted_and_announced() -> None:
    javascript = (ROOT / "termroom/static/app.js").read_text(encoding="utf-8")

    assert 'const THEME_STORAGE_KEY = "termroom.theme"' in javascript
    assert "window.localStorage.setItem(THEME_STORAGE_KEY" in javascript
    assert 'new CustomEvent("themechange"' in javascript
    assert 'window.addEventListener("storage"' in javascript


def test_terminal_theme_uses_live_css_tokens() -> None:
    javascript = (ROOT / "termroom/static/terminal.js").read_text(encoding="utf-8")

    assert 'color("--terminal-bg"' in javascript
    assert 'window.addEventListener("themechange"' in javascript
    assert "term.options.theme = terminalTheme()" in javascript


def test_ui_font_stacks_cover_platform_and_korean_fallbacks() -> None:
    stylesheet = (ROOT / "termroom/static/app.css").read_text(encoding="utf-8")
    terminal_script = (ROOT / "termroom/static/terminal.js").read_text(encoding="utf-8")

    assert '"Apple SD Gothic Neo"' in stylesheet
    assert '"Noto Sans KR"' in stylesheet
    assert '"Malgun Gothic"' in stylesheet
    assert '"Noto Sans Mono CJK KR"' in stylesheet
    assert "font-family: var(--font-ui)" in stylesheet
    assert 'font-family: "SFMono-Regular", Consolas, monospace' not in stylesheet
    assert '.getPropertyValue("--font-mono")' in terminal_script
    assert "fontFamily: terminalFontFamily(bundledTerminalFontLoaded)" in terminal_script
    assert 'fontFamily: \'"SFMono-Regular", Consolas' not in terminal_script


def test_terminal_page_preloads_the_single_bundled_terminal_face_only() -> None:
    base_template = (ROOT / "termroom/templates/base.html").read_text(encoding="utf-8")
    template = (ROOT / "termroom/templates/terminal.html").read_text(encoding="utf-8")

    assert "d2koding-ligature-nerd-font-mono-3.5.0.woff2" not in base_template
    assert "terminal-font.css" not in base_template
    assert "path='vendor/d2koding-ligature-nerd-font-mono-3.5.0.woff2'" in template
    assert 'as="font"' in template
    assert 'type="font/woff2"' in template
    assert "crossorigin" in template
    assert "path='terminal-font.css'" in template


def test_terminal_waits_for_the_bundled_face_before_first_open() -> None:
    javascript = (ROOT / "termroom/static/terminal.js").read_text(encoding="utf-8")

    load_call = javascript.index("await loadBundledTerminalFont()")
    constructor = javascript.index("new window.Terminal")
    first_open = javascript.index("term.open(host)")
    connect = javascript.rindex("connect();")

    assert load_call < constructor < first_open < connect
    assert ".load(BUNDLED_TERMINAL_FONT_DESCRIPTOR, BUNDLED_TERMINAL_FONT_PROBE)" in javascript
    assert "Promise.race([loadFace, timeout])" in javascript
    assert (
        'host.dataset.terminalFont = bundledTerminalFontLoaded ? "bundled" : "system-fallback"'
        in javascript
    )
    assert "terminalFontFamily(bundledTerminalFontLoaded)" in javascript
    assert "BUNDLED_TERMINAL_FONT_DESCRIPTORS" not in javascript
    assert "document.fonts?.ready" not in javascript


def test_terminal_font_failure_keeps_the_existing_system_stack() -> None:
    javascript = (ROOT / "termroom/static/terminal.js").read_text(encoding="utf-8")

    assert 'if (typeof document.fonts?.load !== "function") return false' in javascript
    assert ".catch(() => false)" in javascript
    assert "BUNDLED_TERMINAL_FONT_LOAD_TIMEOUT_MS = 5000" in javascript
    assert "return includeBundledFont" in javascript
    assert "? `${BUNDLED_TERMINAL_FONT_FAMILY}, ${systemFamily}`" in javascript
    assert ": systemFamily" in javascript
    assert '"Noto Sans Mono CJK KR"' in javascript
    assert "D2Coding" in javascript
