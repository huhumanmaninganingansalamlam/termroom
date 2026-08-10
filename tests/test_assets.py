from __future__ import annotations

import hashlib
import re

from termroom.assets import (
    ASSETS,
    TERMINAL_FONT_FILENAME,
    TERMINAL_FONT_SHA256,
    TERMINAL_FONT_SOURCE_ARCHIVE_SHA256,
    TERMINAL_FONT_SOURCE_ARCHIVE_URL,
    TERMINAL_FONT_SOURCE_TTF,
    TERMINAL_FONT_SOURCE_TTF_SHA256,
    TERMINAL_FONT_VERSION,
    VENDOR_DIR,
    XTERM_VERSION,
    XTERM_VERSION_FILE,
)


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
    assert TERMINAL_FONT_FILENAME == "d2koding-ligature-nerd-font-mono-3.5.0.woff2"
    assert (
        TERMINAL_FONT_SHA256 == "6d491d86d652cf6886afe1a37c50877a7bbf91c9369f529049d0d27cd77131be"
    )
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

    font = VENDOR_DIR / TERMINAL_FONT_FILENAME
    assert font.stat().st_size == 2_441_744
    assert hashlib.sha256(font.read_bytes()).hexdigest() == TERMINAL_FONT_SHA256

    d2_license = (VENDOR_DIR / "d2koding-nerd-font.OFL.txt").read_text(encoding="utf-8")
    nerd_license = (VENDOR_DIR / "nerd-fonts.LICENSE").read_text(encoding="utf-8")
    notice = (VENDOR_DIR / "d2koding-nerd-font.NOTICE.md").read_text(encoding="utf-8")
    assert "Reserved Font Name D2Coding" in d2_license
    assert "SIL OPEN FONT LICENSE Version 1.1" in d2_license
    assert "Copyright (c) 2014 Ryan L McIntyre" in nerd_license
    for evidence in (
        TERMINAL_FONT_SHA256,
        TERMINAL_FONT_SOURCE_ARCHIVE_SHA256,
        TERMINAL_FONT_SOURCE_TTF_SHA256,
        "FontTools 4.63.0",
        "brotli 1.2.0",
        "recalcTimestamp=False",
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
    asset_url = f"vendor/{TERMINAL_FONT_FILENAME}?v={TERMINAL_FONT_VERSION}"
    expected_ranges = (
        "U+0000-DFFF, U+E000-E00A, U+E0A0-E0A3, U+E0B0-E0C8, U+E0CA, "
        "U+E0CC-E0D2, U+E0D4, U+E0D6-E0D7, U+E200-E2A9, U+E300-E3E3, "
        "U+E5FA-E6BB, U+E700-E8EF, U+EA60-EA88, U+EA8A-EA8C, "
        "U+EA8F-EAC7, U+EAC9, U+EACC-EB09, U+EB0B-EB4E, U+EB50-EC5E, "
        "U+EC60-EC84, U+ED00-EFCF, U+F000-F385, U+F400-F533, "
        "U+F900-FFFF, U+F0001-F1AF0"
    )

    assert stylesheet.count("@font-face {") == 1
    assert stylesheet.count('font-family: "Termroom D2Koding Nerd Mono"') == 1
    assert f'src: url("{asset_url}")' in stylesheet
    assert stylesheet.count("font-weight: 400") == 1
    assert stylesheet.count("font-style: normal") == 1
    assert "font-weight: 700" not in stylesheet
    assert "font-style: italic" not in stylesheet
    assert "font-display: block" in stylesheet
    assert "font-variant-ligatures: none" in stylesheet
    assert 'font-feature-settings: "liga" 0, "calt" 0' in stylesheet
    assert "font-synthesis: weight style" in stylesheet

    compact_stylesheet = " ".join(stylesheet.split())
    assert f"unicode-range: {expected_ranges};" in compact_stylesheet
    declaration = re.search(r"unicode-range:\s*([^;]+);", stylesheet)
    assert declaration is not None
    ranges: list[tuple[int, int]] = []
    for start, end in re.findall(r"U\+([0-9A-F]+)(?:-([0-9A-F]+))?", declaration.group(1)):
        lower = int(start, 16)
        ranges.append((lower, int(end, 16) if end else lower))

    def claimed(codepoint: int) -> bool:
        return any(start <= codepoint <= end for start, end in ranges)

    for codepoint in (
        0x004D,  # ASCII
        0x0301,  # combining mark
        0x2500,  # box drawing
        0x4E2D,  # CJK
        0xAC00,  # Hangul
        0xE0B0,  # Powerline
        0xF013,  # Font Awesome gear
        0xF0001,  # supplementary Nerd Font PUA
        0xF1AF0,
    ):
        assert claimed(codepoint)

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
        assert not claimed(codepoint)
