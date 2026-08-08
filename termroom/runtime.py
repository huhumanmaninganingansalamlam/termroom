from __future__ import annotations

import hashlib
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent


def _runtime_files() -> list[Path]:
    return sorted(
        (
            path
            for path in PACKAGE_ROOT.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        ),
        key=lambda path: path.relative_to(PACKAGE_ROOT).as_posix(),
    )


def runtime_stamp() -> str:
    """Cheaply identify the Termroom package files currently on disk."""

    digest = hashlib.sha256()
    for path in _runtime_files():
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode())
        digest.update(b":")
        digest.update(str(stat.st_mtime_ns).encode())
        digest.update(b"\0")
    return digest.hexdigest()


def runtime_fingerprint() -> str:
    """Content fingerprint used to decide whether a running Core is reusable."""

    digest = hashlib.sha256()
    for path in _runtime_files():
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        except OSError:
            continue
        digest.update(b"\0")
    return digest.hexdigest()
