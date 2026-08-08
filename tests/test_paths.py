from __future__ import annotations

from pathlib import Path

import pytest

from termroom.security import PathBoundaryError, resolve_inside


def test_resolve_inside_accepts_child(tmp_path: Path) -> None:
    child = tmp_path / "project"
    child.mkdir()
    assert resolve_inside(tmp_path, "project") == child


def test_resolve_inside_rejects_parent_escape(tmp_path: Path) -> None:
    with pytest.raises(PathBoundaryError):
        resolve_inside(tmp_path, "../outside", must_exist=False)


def test_resolve_inside_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    link = tmp_path / "escape"
    link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(PathBoundaryError):
            resolve_inside(tmp_path, "escape")
    finally:
        link.unlink()
        outside.rmdir()
