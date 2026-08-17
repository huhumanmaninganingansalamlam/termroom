from __future__ import annotations

from pathlib import Path

import pytest

from termroom.files import FileConflictError, FileService, UnsupportedFileError
from termroom.security import PathBoundaryError


def test_atomic_text_save(tmp_path: Path) -> None:
    target = tmp_path / "config.txt"
    target.write_text("before\n", encoding="utf-8")
    service = FileService()
    snapshot = service.read_text(tmp_path, "config.txt")

    saved = service.write_text(
        tmp_path,
        "config.txt",
        "after\n",
        expected_digest=snapshot.digest,
        expected_mtime_ns=snapshot.mtime_ns,
    )

    assert target.read_text(encoding="utf-8") == "after\n"
    assert saved.content == "after\n"


def test_external_change_causes_conflict(tmp_path: Path) -> None:
    target = tmp_path / "config.txt"
    target.write_text("before\n", encoding="utf-8")
    service = FileService()
    snapshot = service.read_text(tmp_path, "config.txt")
    target.write_text("external\n", encoding="utf-8")

    with pytest.raises(FileConflictError):
        service.write_text(
            tmp_path,
            "config.txt",
            "mine\n",
            expected_digest=snapshot.digest,
            expected_mtime_ns=snapshot.mtime_ns,
        )


def test_change_during_atomic_save_causes_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "config.txt"
    target.write_text("before\n", encoding="utf-8")
    service = FileService()
    snapshot = service.read_text(tmp_path, "config.txt")
    real_chmod = __import__("os").chmod
    raced = False

    def chmod_with_external_edit(path: str, mode: int) -> None:
        nonlocal raced
        real_chmod(path, mode)
        if not raced and Path(path).name.startswith(".config.txt."):
            raced = True
            target.write_text("external\n", encoding="utf-8")

    monkeypatch.setattr("termroom.files.os.chmod", chmod_with_external_edit)

    with pytest.raises(FileConflictError):
        service.write_text(
            tmp_path,
            "config.txt",
            "mine\n",
            expected_digest=snapshot.digest,
            expected_mtime_ns=snapshot.mtime_ns,
        )

    assert raced is True
    assert target.read_text(encoding="utf-8") == "external\n"


def test_file_operations_reject_symlink_components(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "config.txt").write_text("safe\n", encoding="utf-8")
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)
    service = FileService()

    for operation in (
        lambda: service.stat(tmp_path, "linked/config.txt"),
        lambda: service.read_text(tmp_path, "linked/config.txt"),
        lambda: service.write_new_text(tmp_path, "linked/new.txt", "new\n"),
    ):
        with pytest.raises(PathBoundaryError):
            operation()

    assert not (real / "new.txt").exists()


def test_binary_file_is_not_editable(tmp_path: Path) -> None:
    (tmp_path / "binary.dat").write_bytes(b"abc\x00def")
    with pytest.raises(UnsupportedFileError):
        FileService().read_text(tmp_path, "binary.dat")


def test_delete_only_empty_directory(tmp_path: Path) -> None:
    service = FileService()
    directory = tmp_path / "folder"
    directory.mkdir()
    (directory / "child.txt").write_text("x", encoding="utf-8")

    with pytest.raises(OSError):
        service.delete(tmp_path, "folder")


def test_recent_files_skips_dependency_directories(tmp_path: Path) -> None:
    service = FileService()
    (tmp_path / "result.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    hidden_dependency = tmp_path / ".venv"
    hidden_dependency.mkdir()
    (hidden_dependency / "noise.txt").write_text("ignore", encoding="utf-8")
    state_dir = tmp_path / ".termroom-state"
    state_dir.mkdir()
    (state_dir / "access-token").write_text("secret", encoding="utf-8")
    qa_dir = tmp_path / ".qa-browser"
    qa_dir.mkdir()
    (qa_dir / "cache.bin").write_bytes(b"cache")
    (tmp_path / ".env").write_text("EXAMPLE=1\n", encoding="utf-8")

    recent = service.recent_files(tmp_path)

    paths = {entry.relative_path for entry in recent.entries}
    assert paths == {"result.csv"}
    assert recent.scanned_files == 1


def test_large_text_preview_supports_tail(tmp_path: Path) -> None:
    target = tmp_path / "large.log"
    target.write_text("start\n" + ("middle\n" * 2000) + "finish\n", encoding="utf-8")
    service = FileService()

    preview = service.read_text_preview(tmp_path, "large.log", mode="tail", max_bytes=4096)

    assert preview.truncated is True
    assert "finish" in preview.content
    assert "start" not in preview.content


def test_large_text_preview_supports_middle_ranges(tmp_path: Path) -> None:
    target = tmp_path / "large.log"
    target.write_text("".join(f"line-{index:05d}\n" for index in range(3000)), encoding="utf-8")
    service = FileService()

    preview = service.read_text_preview(
        tmp_path,
        "large.log",
        mode="range",
        offset=12_000,
        max_bytes=4096,
    )

    assert preview.offset == 12_000
    assert preview.bytes_read <= 4096
    assert preview.truncated is True
    assert "line-" in preview.content


def test_large_utf8_preview_tolerates_only_multibyte_boundary_splits(tmp_path: Path) -> None:
    target = tmp_path / "korean.log"
    target.write_bytes(b"a" * 4095 + "한".encode() + "\nnext 한글\n".encode())
    service = FileService()

    head = service.read_text_preview(
        tmp_path,
        "korean.log",
        mode="head",
        max_bytes=4096,
    )
    assert head.content == "a" * 4095
    assert head.truncated is True

    middle = service.read_text_preview(
        tmp_path,
        "korean.log",
        mode="range",
        offset=4096,
        max_bytes=4096,
    )
    assert "next 한글" in middle.content

    invalid = tmp_path / "invalid.log"
    invalid.write_bytes(b"valid\n\xffbroken\n")
    with pytest.raises(UnsupportedFileError):
        service.read_text_preview(tmp_path, "invalid.log", mode="head", max_bytes=4096)


def test_common_source_and_log_files_have_text_content_types() -> None:
    service = FileService()

    assert service.content_type("crawler.log") == "text/plain"
    assert service.content_type("script.py") == "text/plain"
    assert service.content_type("result.csv") == "text/csv"
    assert service.content_type("data.json") == "application/json"
    assert service.content_type(".env") == "text/plain"
    assert service.content_type("Dockerfile") == "text/plain"


def test_recent_files_respect_termroomignore(tmp_path: Path) -> None:
    service = FileService()
    (tmp_path / "keep.log").write_text("keep", encoding="utf-8")
    (tmp_path / "ignore.tmp").write_text("ignore", encoding="utf-8")
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "result.log").write_text("ignore", encoding="utf-8")
    (tmp_path / ".termroomignore").write_text(
        "# recent-only exclusions\n*.tmp\ngenerated/*\n",
        encoding="utf-8",
    )

    paths = [entry.relative_path for entry in service.recent_files(tmp_path).entries]

    assert paths == ["keep.log"]
