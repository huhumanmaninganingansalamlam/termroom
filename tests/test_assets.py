from __future__ import annotations

import hashlib
import re

from termroom.assets import (
    ASSETS,
    TERMINAL_FONT_ASSETS,
    TERMINAL_FONT_SOURCE_ARCHIVE_SHA256,
    TERMINAL_FONT_SOURCE_ARCHIVE_URL,
    TERMINAL_FONT_SOURCE_TTF,
    TERMINAL_FONT_SOURCE_TTF_SHA256,
    TERMINAL_FONT_VERSION,
    VENDOR_DIR,
    XTERM_VERSION,
    XTERM_VERSION_FILE,
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
    assert set(ASSETS) == {"xterm.js", "xterm.css"}
    assert XTERM_VERSION_FILE.read_text(encoding="utf-8").strip() == XTERM_VERSION
    assert (VENDOR_DIR / "xterm.js").stat().st_size > 400_000
    assert (VENDOR_DIR / "xterm.css").stat().st_size > 5_000
    assert all(
        f"@xterm/xterm@{XTERM_VERSION}" in str(details["url"]) for details in ASSETS.values()
    )
    for filename, details in ASSETS.items():
        digest = hashlib.sha256((VENDOR_DIR / filename).read_bytes()).hexdigest()
        assert digest == details["sha256"]


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

    faces = re.findall(r"@font-face\s*\{(.*?)\}", stylesheet, flags=re.DOTALL)
    assert len(faces) == 4
    assert stylesheet.count('font-family: "Termroom D2Koding Nerd Mono"') == 4
    assert stylesheet.count("font-weight: 400") == 4
    assert stylesheet.count("font-style: normal") == 4
    assert "font-weight: 700" not in stylesheet
    assert "font-style: italic" not in stylesheet
    assert stylesheet.count("font-display: block") == 1
    assert stylesheet.count("font-display: swap") == 3
    assert "font-variant-ligatures: none" in stylesheet
    assert 'font-feature-settings: "liga" 0, "calt" 0' in stylesheet
    assert "font-synthesis: weight style" in stylesheet

    expected_points = _range_points(_unicode_ranges(expected_ranges))
    actual_points: set[int] = set()
    face_points: dict[str, set[int]] = {}
    for key, details in TERMINAL_FONT_ASSETS.items():
        filename = str(details["filename"])
        face = next(candidate for candidate in faces if filename in candidate)
        declaration = re.search(r"unicode-range:\s*([^;]+);", face)
        assert declaration is not None
        points = _range_points(_unicode_ranges(declaration.group(1)))
        assert points == _range_points(_unicode_ranges(str(details["unicode_range"])))
        assert actual_points.isdisjoint(points)
        actual_points.update(points)
        face_points[key] = points
        assert f'url("vendor/{filename}?v=3.5.0.1")' in face

    assert actual_points == expected_points
    assert {0x004D, 0x0301, 0x2500, 0x2800, 0xAC00} <= face_points["core_hangul"]
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
    assert str(TERMINAL_FONT_ASSETS["core_hangul"]["filename"]) in terminal_template
    for key in ("cjk", "nerd_bmp", "nerd_supp"):
        assert str(TERMINAL_FONT_ASSETS[key]["filename"]) not in terminal_template
    terminal_script = (VENDOR_DIR.parent / "terminal.js").read_text(encoding="utf-8")
    assert 'BUNDLED_TERMINAL_FONT_PROBE = "M\\uD55C"' in terminal_script
    assert "\\uE0B0" not in terminal_script
    assert "\\uF013" not in terminal_script
    assert "\\u{F0001}" not in terminal_script
    assert 'addEventListener?.("loadingdone"' in terminal_script
    assert "clearTextureAtlas?.()" in terminal_script
    assert "bundledTerminalFontLoad.completed.then" in terminal_script
    assert "bundledTerminalFontLoaded = true" in terminal_script
    assert "term.options.fontFamily = terminalFontFamily(true)" in terminal_script
    assert "BUNDLED_TERMINAL_FONT_LOAD_TIMEOUT_MS = 400" in terminal_script
    assert "scheduleResize(true)" in terminal_script
    assert "const onUserInput = term._core?.coreService?.onUserInput;" in terminal_script
    assert "const userInput = hasUserInputSignal && nextTerminalDataIsUserInput;" in terminal_script
    assert "user_input: userInput" in terminal_script
    assert "term.onBinary((data) =>" in terminal_script
    assert "socket.send(Uint8Array.from(data" in terminal_script
    assert 'kind: "command"' in terminal_script
    assert "rows: term.rows" in terminal_script
    assert "cols: term.cols" in terminal_script


def test_template_static_asset_versions_are_consistent() -> None:
    versions: dict[str, set[str]] = {}
    templates_dir = VENDOR_DIR.parents[1] / "templates"
    pattern = re.compile(r"url_for\('static', path='([^']+)'\) }}\?v=([0-9.]+)")
    for template in templates_dir.glob("*.html"):
        for asset, version in pattern.findall(template.read_text(encoding="utf-8")):
            versions.setdefault(asset, set()).add(version)

    assert all(len(asset_versions) == 1 for asset_versions in versions.values())
    assert versions["app.css"] == {"41"}
    assert versions["app.js"] == {"61"}
    assert versions["remote_run.js"] == {"11"}
    assert versions["terminal-font.css"] == {"2"}
    assert versions["terminal.js"] == {"45"}


def test_global_header_layers_settings_menu_above_transformed_page_actions() -> None:
    stylesheet = (VENDOR_DIR.parent / "app.css").read_text(encoding="utf-8")
    header_rule = stylesheet.split(".app-header {", 1)[1].split("}", 1)[0]

    assert "position: relative;" in header_rule
    assert "z-index: 60;" in header_rule
    assert "backdrop-filter: blur(14px);" in header_rule


def test_remote_run_result_zip_is_the_primary_completed_run_action() -> None:
    templates_dir = VENDOR_DIR.parents[1] / "templates"
    wait_template = (templates_dir / "remote_run_wait.html").read_text(encoding="utf-8")
    workspace_template = (templates_dir / "workspace_base.html").read_text(
        encoding="utf-8"
    )
    collect_template = (templates_dir / "remote_run_collect.html").read_text(
        encoding="utf-8"
    )

    result_link = 'class="primary-button" href="/remote-runs/{{'
    assert result_link in wait_template
    assert result_link in workspace_template
    assert 'class="secondary-button" href="/remote-runs/{{ run.id }}/result.zip"' in (
        collect_template
    )
    assert 'class="primary-button" type="submit"' in collect_template


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
        'delete link.dataset.workspaceOpening',
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
    workspace_template = (templates_dir / "workspace_base.html").read_text(
        encoding="utf-8"
    )
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


def test_workspace_background_refresh_is_user_driven() -> None:
    templates_dir = VENDOR_DIR.parents[1] / "templates"
    workspace_template = (templates_dir / "workspace_base.html").read_text(
        encoding="utf-8"
    )
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
    assert "setTimeout" not in terminal_script
    assert 'document.addEventListener("visibilitychange"' in terminal_script
    assert 'window.addEventListener("focus"' in terminal_script
    assert '"termroom:terminal-activity-changed"' in terminal_script

    usage_start = app_script.index("  const workspaceUsageViews =")
    usage_end = app_script.index(
        '  document.querySelectorAll("[data-file-run]")', usage_start
    )
    usage_script = app_script[usage_start:usage_end]
    assert 'view.addEventListener("toggle"' in usage_script
    assert "workspaceUsageViews.some((view) => view.open" in usage_script
    assert "usageRefreshInFlight || document.hidden" in usage_script
    assert "pollWorkspaceUsage();" in usage_script
