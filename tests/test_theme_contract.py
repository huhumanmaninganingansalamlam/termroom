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
    assert 'data-theme-toggle' in template


def test_theme_changes_are_persisted_and_announced() -> None:
    javascript = (ROOT / "termroom/static/app.js").read_text(encoding="utf-8")

    assert 'const THEME_STORAGE_KEY = "termroom.theme"' in javascript
    assert 'window.localStorage.setItem(THEME_STORAGE_KEY' in javascript
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
    assert "fontFamily: terminalFontFamily()" in terminal_script
    assert 'fontFamily: \'"SFMono-Regular", Consolas' not in terminal_script
