from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_mobile_terminal_keeps_direct_xterm_as_primary_input() -> None:
    template = (ROOT / "termroom/templates/terminal.html").read_text(encoding="utf-8")
    script = (ROOT / "termroom/static/terminal.js").read_text(encoding="utf-8")

    assert 'id="open-command-editor"' in template
    assert 'id="close-command-editor"' in template
    assert 'class="terminal-compose-pane" hidden' in template
    assert 'data-terminal-input-mode="direct"' not in template
    assert 'data-terminal-input-mode="compose"' not in template
    assert "term.textarea" in script
    assert "setComposerOpen" in script
    assert 'addEventListener("compositionstart"' in script
    assert 'addEventListener("compositionend"' in script
    assert "composerComposing" in script


def test_mobile_terminal_uses_xterm_for_paste_and_helper_input() -> None:
    script = (ROOT / "termroom/static/terminal.js").read_text(encoding="utf-8")

    assert "term.paste(text)" in script
    assert "term.input(value, true)" in script
    assert "term.modes.applicationCursorKeysMode" in script
    assert 'application ? "\\u001bOA" : "\\u001b[A"' in script


def test_mobile_terminal_has_visual_viewport_keyboard_focus_mode() -> None:
    script = (ROOT / "termroom/static/terminal.js").read_text(encoding="utf-8")
    styles = (ROOT / "termroom/static/app.css").read_text(encoding="utf-8")

    assert "window.visualViewport" in script
    assert 'classList.toggle("terminal-keyboard-open"' in script
    assert ".workspace-page.terminal-keyboard-open .workspace-header" in styles
    assert ".terminal-composer.command-editor-open" in styles


def test_terminal_focus_is_not_stolen_by_closed_popovers_or_narrow_desktop_layouts() -> None:
    app_script = (ROOT / "termroom/static/app.js").read_text(encoding="utf-8")
    terminal_script = (ROOT / "termroom/static/terminal.js").read_text(encoding="utf-8")
    styles = (ROOT / "termroom/static/app.css").read_text(encoding="utf-8")

    assert "if (!details.open) return false" in app_script
    assert "restoreFocus = false" in app_script
    assert "if (restoreFocus)" in app_script
    assert "popoverDetails.filter((details) => details.open)" in app_script
    assert 'window.matchMedia("(pointer: coarse)")' in terminal_script
    assert "if (!coarsePrimaryPointer.matches) term.focus()" in terminal_script
    assert "if (!mobileInput.matches) term.focus()" not in terminal_script

    helper = re.search(
        r"\.terminal-host \.xterm-helper-textarea\s*\{(?P<body>.*?)\}",
        styles,
        re.S,
    )
    assert helper
    assert "min-height: 0" in helper.group("body")


def test_more_keys_popover_has_an_explicit_close_and_returns_focus_to_terminal() -> None:
    template = (ROOT / "termroom/templates/terminal.html").read_text(encoding="utf-8")
    script = (ROOT / "termroom/static/terminal.js").read_text(encoding="utf-8")

    assert 'id="close-more-keys"' in template
    assert 'class="more-keys-close"' in template
    assert "data-close-popover" in template
    assert 'const moreKeys = document.querySelector("details.more-keys")' in script
    assert 'button.closest(".more-keys-panel")' in script
    assert "moreKeys.open = false" in script
    assert 'document.querySelector("#close-more-keys")' in script


def test_mobile_terminal_has_short_landscape_and_safe_area_rules() -> None:
    template = (ROOT / "termroom/templates/terminal.html").read_text(encoding="utf-8")
    styles = (ROOT / "termroom/static/app.css").read_text(encoding="utf-8")

    assert "workspace-page terminal-page" in template
    assert "(orientation: landscape)" in styles
    assert ".terminal-page .bottom-nav" in styles
    assert "safe-area-inset-left" in styles
    assert "safe-area-inset-right" in styles


def test_workspace_shell_scrolls_long_content_and_terminal_owns_remaining_height() -> None:
    styles = (ROOT / "termroom/static/app.css").read_text(encoding="utf-8")

    desktop_workspace = re.search(
        r"@media \(min-width: 1024px\).*?\.workspace-content\s*\{(?P<body>.*?)\}",
        styles,
        re.S,
    )
    assert desktop_workspace
    assert "overflow: auto" in desktop_workspace.group("body")

    terminal_host = re.search(r"\.terminal-host\s*\{(?P<body>.*?)\}", styles, re.S)
    assert terminal_host
    assert "flex: 1 1 0" in terminal_host.group("body")
    assert "overflow: hidden" in terminal_host.group("body")
    assert ".terminal-key-rail" in styles
    assert ".terminal-top-actions" in styles


def test_workspace_shell_contains_long_labels_and_exposes_full_values() -> None:
    styles = (ROOT / "termroom/static/app.css").read_text(encoding="utf-8")
    template = (ROOT / "termroom/templates/workspace_base.html").read_text(encoding="utf-8")
    base_template = (ROOT / "termroom/templates/base.html").read_text(encoding="utf-8")

    text_owner = re.search(
        r"\.workspace-title,\s*\.workspace-sidebar-copy\s*\{(?P<body>.*?)\}",
        styles,
        re.S,
    )
    assert text_owner
    assert "min-width: 0" in text_owner.group("body")

    clipping = re.search(
        r"\.workspace-title strong,\s*"
        r"\.workspace-title small,\s*"
        r"\.workspace-sidebar-copy strong,\s*"
        r"\.workspace-sidebar-copy small\s*\{(?P<body>.*?)\}",
        styles,
        re.S,
    )
    assert clipping
    for declaration in (
        "display: block",
        "overflow: hidden",
        "text-overflow: ellipsis",
        "white-space: nowrap",
    ):
        assert declaration in clipping.group("body")

    assert 'title="{{ workspace.display_name }}"' in template
    assert 'title="{{ workspace.canonical_path }}"' in template
    assert 'class="workspace-sidebar-copy"' in template
    assert 'title="{{ workspace_sidebar_status }}"' in template
    assert 'title="{{ workspace_sidebar_detail }}"' in template
    assert "path='app.css') }}?v=20" in base_template


def test_dynamic_headings_and_notices_wrap_long_unbroken_values() -> None:
    styles = (ROOT / "termroom/static/app.css").read_text(encoding="utf-8")

    for selector in (
        r"\.open-page-heading > div",
        r"\.notice",
        r"\.computer-detail-heading > div",
        r"\.connection-notice",
    ):
        rule = re.search(rf"{selector}\s*\{{(?P<body>.*?)\}}", styles, re.S)
        assert rule
        assert "overflow-wrap: anywhere" in rule.group("body")


def test_computer_picker_contains_long_labels_and_exposes_full_values() -> None:
    styles = (ROOT / "termroom/static/app.css").read_text(encoding="utf-8")
    template = (ROOT / "termroom/templates/workspace_open.html").read_text(encoding="utf-8")

    text_owner = re.search(
        r"\.open-computer-main\s*\{(?P<body>.*?)\}",
        styles,
        re.S,
    )
    assert text_owner
    assert "min-width: 0" in text_owner.group("body")
    assert "overflow: hidden" in text_owner.group("body")

    clipping = re.search(
        r"\.open-computer-main strong,\s*"
        r"\.open-computer-main small\s*\{(?P<body>.*?)\}",
        styles,
        re.S,
    )
    assert clipping
    for declaration in (
        "display: block",
        "min-width: 0",
        "overflow: hidden",
        "text-overflow: ellipsis",
        "white-space: nowrap",
    ):
        assert declaration in clipping.group("body")

    assert 'title="{{ computer.name }}"' in template
    assert 'title="{{ computer.username }}@{{ computer.host }}' in template


def test_terminal_status_updates_label_without_replacing_status_structure() -> None:
    template = (ROOT / "termroom/templates/terminal.html").read_text(encoding="utf-8")
    script = (ROOT / "termroom/static/terminal.js").read_text(encoding="utf-8")

    assert 'class="terminal-status-dot"' in template
    assert 'class="terminal-status-label"' in template
    assert 'querySelector(".terminal-status-label")' in script
    assert "setStatusMessage" in script
    assert "status.textContent = message" not in script


def test_terminal_font_size_is_user_adjustable_and_browser_persistent() -> None:
    template = (ROOT / "termroom/templates/terminal.html").read_text(encoding="utf-8")
    script = (ROOT / "termroom/static/terminal.js").read_text(encoding="utf-8")

    assert 'id="terminal-font-decrease"' in template
    assert 'id="terminal-font-increase"' in template
    assert 'id="terminal-font-reset"' in template
    assert 'id="terminal-font-size-value"' in template
    assert '"termroom.terminal.font-size"' in script
    assert "term.options.fontSize = size" in script
    assert "window.localStorage.setItem" in script
    assert "scheduleResize(true)" in script


def test_file_upload_ui_uses_stream_progress_and_cancel() -> None:
    template = (ROOT / "termroom/templates/files.html").read_text(encoding="utf-8")
    script = (ROOT / "termroom/static/app.js").read_text(encoding="utf-8")

    assert 'data-stream-url="/w/{{ workspace.id }}/files/upload-stream"' in template
    assert 'id="upload-progress-panel"' in template
    assert "new XMLHttpRequest()" in script
    assert 'xhr.upload.addEventListener("progress"' in script
    assert "activeUploadRequest.abort()" in script
