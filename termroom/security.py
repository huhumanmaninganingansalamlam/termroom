from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
from pathlib import Path


class PathBoundaryError(ValueError):
    pass


def is_within(path: Path, boundary: Path) -> bool:
    try:
        path.relative_to(boundary)
    except ValueError:
        return False
    return True


def resolve_inside(
    boundary: Path,
    relative_path: str | Path = ".",
    *,
    must_exist: bool = True,
) -> Path:
    boundary = boundary.resolve(strict=True)
    raw = Path(relative_path)
    if raw.is_absolute():
        raise PathBoundaryError("Absolute paths are not allowed")

    candidate = boundary / raw
    if must_exist:
        resolved = candidate.resolve(strict=True)
    else:
        parent = candidate.parent.resolve(strict=True)
        resolved = parent / candidate.name

    if not is_within(resolved, boundary):
        raise PathBoundaryError("Path escapes the allowed boundary")
    return resolved


def file_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def secure_compare(left: str | None, right: str) -> bool:
    return bool(left) and hmac.compare_digest(left, right)


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(PermissionError):
        os.chmod(path, 0o700)
