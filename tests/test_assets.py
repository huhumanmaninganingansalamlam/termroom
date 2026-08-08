from __future__ import annotations

import hashlib

from termroom.assets import ASSETS, VENDOR_DIR, XTERM_VERSION, XTERM_VERSION_FILE


def test_vendored_xterm_matches_declared_scoped_release() -> None:
    assert XTERM_VERSION == "6.0.0"
    assert XTERM_VERSION_FILE.read_text(encoding="utf-8").strip() == XTERM_VERSION
    assert (VENDOR_DIR / "xterm.js").stat().st_size > 400_000
    assert (VENDOR_DIR / "xterm.css").stat().st_size > 5_000
    assert all(
        f"@xterm/xterm@{XTERM_VERSION}" in str(details["url"])
        for details in ASSETS.values()
    )
    for filename, details in ASSETS.items():
        digest = hashlib.sha256((VENDOR_DIR / filename).read_bytes()).hexdigest()
        assert digest == details["sha256"]
