from __future__ import annotations

import hashlib
import shutil
import urllib.request
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
VENDOR_DIR = PACKAGE_ROOT / "static" / "vendor"
XTERM_VERSION = "6.0.0"
XTERM_VERSION_FILE = VENDOR_DIR / "xterm.version"

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
