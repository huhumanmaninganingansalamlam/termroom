from __future__ import annotations

import hashlib
import shutil
import urllib.request
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
VENDOR_DIR = PACKAGE_ROOT / "static" / "vendor"
XTERM_VERSION = "6.0.0"
XTERM_UNICODE11_VERSION = "0.8.0"
XTERM_VERSION_FILE = VENDOR_DIR / "xterm.version"
TERMINAL_FONT_VERSION = "3.5.0"
TERMINAL_FONT_ASSETS = {
    "core_hangul": {
        "filename": (
            f"d2koding-ligature-nerd-font-mono-{TERMINAL_FONT_VERSION}-core-hangul.woff2"
        ),
        "sha256": "b9fae6a182cc440dcf69c7a8b3a8b2ad60284fa2ed67e8e41c2f26bead83ede9",
        "size": 567_584,
        "unicode_range": "U+0000-4DFF,U+AC00-DFFF,U+FB00-FFFF",
    },
    "cjk": {
        "filename": f"d2koding-ligature-nerd-font-mono-{TERMINAL_FONT_VERSION}-cjk.woff2",
        "sha256": "0d1b8923dc714312107b8282b1e2f1d79645e48f26e0a12d38dac886028c9783",
        "size": 801_848,
        "unicode_range": "U+4E00-ABFF,U+F900-FAFF",
    },
    "nerd_bmp": {
        "filename": (
            f"d2koding-ligature-nerd-font-mono-{TERMINAL_FONT_VERSION}-nerd-bmp.woff2"
        ),
        "sha256": "64c9508468b380ac4e31f65ce5fc548e14939670ae1833870596f276661122dd",
        "size": 497_412,
        "unicode_range": (
            "U+E000-E00A,U+E0A0-E0A3,U+E0B0-E0C8,U+E0CA,U+E0CC-E0D2,"
            "U+E0D4,U+E0D6-E0D7,U+E200-E2A9,U+E300-E3E3,U+E5FA-E6BB,"
            "U+E700-E8EF,U+EA60-EA88,U+EA8A-EA8C,U+EA8F-EAC7,U+EAC9,"
            "U+EACC-EB09,U+EB0B-EB4E,U+EB50-EC5E,U+EC60-EC84,U+ED00-EFCF,"
            "U+F000-F385,U+F400-F533"
        ),
    },
    "nerd_supp": {
        "filename": (
            f"d2koding-ligature-nerd-font-mono-{TERMINAL_FONT_VERSION}-nerd-supp.woff2"
        ),
        "sha256": "25fa39d5040346d76385e04aa5977b15b1bc72b3af27bff429f27e341fc03f0d",
        "size": 397_908,
        "unicode_range": "U+F0001-F1AF0",
    },
}
TERMINAL_FONT_SOURCE_ARCHIVE_URL = (
    "https://github.com/ryanoasis/nerd-fonts/releases/download/"
    f"v{TERMINAL_FONT_VERSION}/D2Coding.tar.xz"
)
TERMINAL_FONT_SOURCE_ARCHIVE_SHA256 = (
    "c1d4e7cbee20b9e55d2481762bbb8413124fda224cee26863b805fe2f863aaec"
)
TERMINAL_FONT_SOURCE_TTF = "D2KodingLigatureNerdFontMono-Regular.ttf"
TERMINAL_FONT_SOURCE_TTF_SHA256 = "be8964904705f43a1e5a62339629d9e20eb37316008dda4de5b5681547ea2996"

ASSETS = {
    "xterm.js": {
        "url": f"https://cdn.jsdelivr.net/npm/@xterm/xterm@{XTERM_VERSION}/lib/xterm.js",
        "sha256": "14903579ff54664cd72f8e8699e6961a6272c21863ec1c3b118cdc8af5d4a972",
        "candidates": [
            Path.cwd() / "node_modules/@xterm/xterm/lib/xterm.js",
        ],
    },
    "xterm.css": {
        "url": f"https://cdn.jsdelivr.net/npm/@xterm/xterm@{XTERM_VERSION}/css/xterm.css",
        "sha256": "854a7c0fb70e8b1a083c16797ab827299fb18744f5ad34f227b48337e33293c6",
        "candidates": [
            Path.cwd() / "node_modules/@xterm/xterm/css/xterm.css",
        ],
    },
    "addon-unicode11.js": {
        "url": (
            "https://cdn.jsdelivr.net/npm/@xterm/addon-unicode11@"
            f"{XTERM_UNICODE11_VERSION}/lib/addon-unicode11.js"
        ),
        "sha256": "b0c3be540a9984713aea996966c24ed1a639d11f60d44986b22661e3a8a148d0",
        "candidates": [
            Path.cwd() / "node_modules/@xterm/addon-unicode11/lib/addon-unicode11.js",
        ],
    },
}


def ensure_xterm_assets() -> list[Path]:
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    current_version = ""
    if XTERM_VERSION_FILE.is_file():
        current_version = XTERM_VERSION_FILE.read_text(encoding="utf-8").strip()
    current_assets = all(
        _asset_matches(VENDOR_DIR / filename, str(details["sha256"]))
        for filename, details in ASSETS.items()
    )
    if current_version == XTERM_VERSION and current_assets:
        return [VENDOR_DIR / filename for filename in ASSETS]

    installed: list[Path] = []
    for filename, details in ASSETS.items():
        destination = VENDOR_DIR / filename
        copied = False
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        for candidate in details["candidates"]:
            if candidate.exists():
                shutil.copyfile(candidate, temporary)
                copied = True
                break

        if not copied:
            request = urllib.request.Request(
                details["url"],
                headers={"User-Agent": "Termroom/0.1 asset bootstrap"},
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                temporary.write_bytes(response.read())
        if not _asset_matches(temporary, str(details["sha256"])):
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"xterm asset checksum mismatch: {filename}")
        temporary.replace(destination)
        installed.append(destination)
    XTERM_VERSION_FILE.write_text(XTERM_VERSION + "\n", encoding="utf-8")
    return installed


def _asset_matches(path: Path, expected_sha256: str) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest == expected_sha256


def main() -> None:
    for path in ensure_xterm_assets():
        print(path)


if __name__ == "__main__":
    main()
