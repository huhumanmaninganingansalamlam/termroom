from __future__ import annotations

import codecs
import difflib
import fnmatch
import heapq
import mimetypes
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from termroom.security import (
    PathBoundaryError,
    file_digest,
    is_within,
    resolve_inside,
    resolve_no_symlink_inside,
)


class FileConflictError(RuntimeError):
    pass


class UnsupportedFileError(ValueError):
    pass


class DirectoryListingLimitError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    path: Path
    relative_path: str
    content: str
    digest: str
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class RunnableFile:
    relative_path: str
    digest: str
    executable: bool
    has_shebang: bool


@dataclass(frozen=True, slots=True)
class FileEntry:
    name: str
    relative_path: str
    is_dir: bool
    size: int
    mtime_ns: int = 0


@dataclass(frozen=True, slots=True)
class RecentFiles:
    entries: list[FileEntry]
    scanned_files: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class TextPreview:
    relative_path: str
    content: str
    size: int
    mtime_ns: int
    mode: str
    truncated: bool
    offset: int = 0
    bytes_read: int = 0


def editor_newline_style(content: str) -> str:
    """Return the newline convention an HTML textarea must restore on save."""

    without_crlf = content.replace("\r\n", "")
    return "crlf" if "\r\n" in content and "\n" not in without_crlf else "lf"


def normalize_editor_newlines(content: str, style: str) -> str:
    """Undo the browser's form newline normalization at the editor boundary."""

    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", "\r\n") if style == "crlf" else normalized


def decode_utf8_preview(
    raw: bytes,
    *,
    allow_partial_start: bool,
    final: bool,
) -> str:
    """Decode a bounded UTF-8 chunk without misclassifying boundary splits.

    A range/tail read may begin on one or two UTF-8 continuation bytes. A
    head/range read may also end before the final bytes of a multibyte code
    point. Only those boundary fragments are tolerated; invalid bytes inside
    the chunk still raise UnicodeDecodeError.
    """

    skip = 0
    if allow_partial_start:
        while skip < min(3, len(raw)) and 0x80 <= raw[skip] <= 0xBF:
            skip += 1
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    return decoder.decode(raw[skip:], final=final)


DEFAULT_FILE_BROWSER_NOISE = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".cache",
        ".tox",
        ".termroom-state",
    }
)

DEFAULT_RECENT_EXCLUDES = DEFAULT_FILE_BROWSER_NOISE | frozenset(
    {
        ".env",
        ".env.local",
        ".env.development",
        ".env.production",
        ".env.test",
    }
)

RECENT_IGNORE_FILE = ".termroomignore"
MAX_RECENT_IGNORE_BYTES = 64 * 1024


def parse_recent_ignore_patterns(raw: str) -> tuple[str, ...]:
    patterns: list[str] = []
    for line in raw.splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        value = value.removeprefix("./").rstrip("/")
        if value:
            patterns.append(value)
    return tuple(patterns[:200])


def recent_path_ignored(relative_path: str, patterns: tuple[str, ...]) -> bool:
    path = relative_path.removeprefix("./")
    name = Path(path).name
    return any(
        fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(name, pattern)
        for pattern in patterns
    )


class FileService:
    def __init__(self, max_edit_bytes: int = 1024 * 1024) -> None:
        self.max_edit_bytes = max_edit_bytes

    def list_dir(
        self,
        workspace_path: Path,
        relative_path: str = ".",
        *,
        max_entries: int | None = None,
        max_metadata_bytes: int | None = None,
    ) -> tuple[Path, list[FileEntry]]:
        if max_entries is not None and (type(max_entries) is not int or max_entries < 0):
            raise ValueError("Directory entry limit is invalid")
        if max_metadata_bytes is not None and (
            type(max_metadata_bytes) is not int or max_metadata_bytes < 1
        ):
            raise ValueError("Directory metadata limit is invalid")
        directory = self._resolve_existing(workspace_path, relative_path)
        if not directory.is_dir():
            raise NotADirectoryError(directory)
        entries: list[FileEntry] = []
        scanned = 0
        metadata_bytes = 0
        with os.scandir(directory) as children:
            for child in children:
                scanned += 1
                if max_entries is not None and scanned > max_entries:
                    raise DirectoryListingLimitError(
                        "Directory contains too many entries"
                    )
                try:
                    if child.is_symlink():
                        continue
                    info = child.stat(follow_symlinks=False)
                    target = Path(child.path)
                    relative = target.relative_to(workspace_path).as_posix()
                    metadata_bytes += len(relative.encode("utf-8")) + 128
                    if (
                        max_metadata_bytes is not None
                        and metadata_bytes > max_metadata_bytes
                    ):
                        raise DirectoryListingLimitError(
                            "Directory metadata exceeds the safe response limit"
                        )
                    entries.append(
                        FileEntry(
                            name=child.name,
                            relative_path=relative,
                            is_dir=child.is_dir(follow_symlinks=False),
                            size=info.st_size,
                            mtime_ns=info.st_mtime_ns,
                        )
                    )
                except DirectoryListingLimitError:
                    raise
                except OSError:
                    continue
        entries.sort(key=lambda item: (not item.is_dir, item.name.casefold()))
        return directory, entries

    def read_text(self, workspace_path: Path, relative_path: str) -> FileSnapshot:
        target = self._resolve_existing(workspace_path, relative_path)
        if not target.is_file():
            raise UnsupportedFileError("Only regular files can be edited")
        stat = target.stat()
        if stat.st_size > self.max_edit_bytes:
            raise UnsupportedFileError("File exceeds the editable size limit")
        raw = target.read_bytes()
        if b"\x00" in raw:
            raise UnsupportedFileError("Binary files cannot be edited")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UnsupportedFileError("Only UTF-8 text files can be edited") from exc
        return FileSnapshot(
            path=target,
            relative_path=target.relative_to(workspace_path).as_posix(),
            content=content,
            digest=file_digest(raw),
            mtime_ns=stat.st_mtime_ns,
        )

    def inspect_runnable(
        self,
        workspace_path: Path,
        relative_path: str,
        *,
        expected_digest: str | None = None,
    ) -> RunnableFile:
        target = resolve_no_symlink_inside(workspace_path, relative_path)
        info = target.stat(follow_symlinks=False)
        if not target.is_file():
            raise UnsupportedFileError("Only regular files can be executed")
        if info.st_size > self.max_edit_bytes:
            raise UnsupportedFileError("File exceeds the editable size limit")
        with target.open("rb") as handle:
            raw = handle.read(self.max_edit_bytes + 1)
        if len(raw) > self.max_edit_bytes:
            raise UnsupportedFileError("File exceeds the editable size limit")
        if b"\x00" in raw:
            raise UnsupportedFileError("Binary files cannot be executed")
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UnsupportedFileError("Only UTF-8 text files can be executed") from exc
        digest = file_digest(raw)
        if expected_digest is not None and digest != expected_digest:
            raise FileConflictError("The file changed before execution")
        return RunnableFile(
            relative_path=target.relative_to(workspace_path.resolve(strict=True)).as_posix(),
            digest=digest,
            executable=bool(info.st_mode & 0o111),
            has_shebang=raw.startswith(b"#!"),
        )

    def stat(self, workspace_path: Path, relative_path: str) -> FileEntry:
        target = self._resolve_existing(workspace_path, relative_path)
        stat = target.stat()
        return FileEntry(
            name=target.name,
            relative_path=target.relative_to(workspace_path).as_posix(),
            is_dir=target.is_dir(),
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )

    def resolve_regular_file(self, workspace_path: Path, relative_path: str) -> Path:
        target = self._resolve_existing(workspace_path, relative_path)
        if not target.is_file():
            raise UnsupportedFileError("Only regular files can be opened")
        return target

    @staticmethod
    def _resolve_existing(workspace_path: Path, relative_path: str) -> Path:
        if relative_path in {"", "."}:
            return workspace_path.resolve(strict=True)
        try:
            return resolve_no_symlink_inside(workspace_path, relative_path)
        except PathBoundaryError:
            raise
        except OSError as exc:
            if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
                raise
            raise UnsupportedFileError("Symbolic links are not exposed") from exc

    def content_type(self, relative_path: str) -> str:
        basename = Path(relative_path).name.casefold()
        if basename in {
            ".env",
            ".gitignore",
            ".dockerignore",
            "dockerfile",
            "makefile",
            "procfile",
            "license",
        }:
            return "text/plain"
        suffix = Path(relative_path).suffix.casefold()
        if suffix == ".json":
            return "application/json"
        if suffix == ".csv":
            return "text/csv"
        if suffix in {
            ".txt",
            ".log",
            ".md",
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".jsx",
            ".css",
            ".html",
            ".htm",
            ".toml",
            ".yaml",
            ".yml",
            ".ini",
            ".cfg",
            ".conf",
            ".env",
            ".sql",
            ".sh",
            ".zsh",
            ".fish",
            ".xml",
        }:
            return "text/plain"
        guessed, _ = mimetypes.guess_type(relative_path)
        return guessed or "application/octet-stream"

    def read_text_preview(
        self,
        workspace_path: Path,
        relative_path: str,
        *,
        mode: str = "head",
        offset: int = 0,
        max_bytes: int = 256 * 1024,
    ) -> TextPreview:
        target = self.resolve_regular_file(workspace_path, relative_path)
        stat = target.stat()
        limit = max(4096, min(max_bytes, 1024 * 1024))
        if mode not in {"head", "tail", "range"}:
            raise ValueError("Preview mode must be head, tail, or range")

        if mode == "tail":
            start = max(0, stat.st_size - limit)
        elif mode == "range":
            start = max(0, min(int(offset), stat.st_size))
        else:
            start = 0

        with target.open("rb") as handle:
            if start:
                handle.seek(start)
            raw = handle.read(limit)
        if b"\x00" in raw:
            raise UnsupportedFileError("Binary files cannot be shown as text")
        try:
            content = decode_utf8_preview(
                raw,
                allow_partial_start=start > 0,
                final=start + len(raw) >= stat.st_size,
            )
        except UnicodeDecodeError as exc:
            raise UnsupportedFileError("Only UTF-8 text can be previewed") from exc

        if start:
            # A byte range can begin in the middle of a line. Dropping that
            # partial line produces a much less confusing log view.
            _, separator, remainder = content.partition("\n")
            if separator:
                content = remainder
        return TextPreview(
            relative_path=target.relative_to(workspace_path).as_posix(),
            content=content,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            mode=mode,
            truncated=start > 0 or start + len(raw) < stat.st_size,
            offset=start,
            bytes_read=len(raw),
        )

    def recent_files(
        self,
        workspace_path: Path,
        *,
        limit: int = 50,
        max_files: int = 10_000,
        max_seconds: float = 1.5,
        excludes: frozenset[str] = DEFAULT_RECENT_EXCLUDES,
    ) -> RecentFiles:
        root = workspace_path.resolve(strict=True)
        patterns = self._recent_ignore_patterns(root)
        wanted = max(1, min(limit, 200))
        scan_limit = max(wanted, min(max_files, 100_000))
        deadline = monotonic() + max(0.05, min(max_seconds, 10.0))
        heap: list[tuple[int, str, FileEntry]] = []
        scanned = 0
        truncated = False
        pending = [root]

        while pending:
            if scanned >= scan_limit or monotonic() >= deadline:
                truncated = True
                break
            directory = pending.pop()
            try:
                with os.scandir(directory) as iterator:
                    for item in iterator:
                        if scanned >= scan_limit or monotonic() >= deadline:
                            truncated = True
                            break
                        if item.name in excludes or item.name == RECENT_IGNORE_FILE:
                            continue
                        try:
                            if item.is_symlink():
                                continue
                            relative = Path(item.path).relative_to(root).as_posix()
                            if recent_path_ignored(relative, patterns):
                                continue
                            if item.is_dir(follow_symlinks=False):
                                if item.name.startswith("."):
                                    continue
                                pending.append(Path(item.path))
                                continue
                            if not item.is_file(follow_symlinks=False):
                                continue
                            stat = item.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        scanned += 1
                        entry = FileEntry(
                            name=item.name,
                            relative_path=relative,
                            is_dir=False,
                            size=stat.st_size,
                            mtime_ns=stat.st_mtime_ns,
                        )
                        key = (stat.st_mtime_ns, relative, entry)
                        if len(heap) < wanted:
                            heapq.heappush(heap, key)
                        elif key[:2] > heap[0][:2]:
                            heapq.heapreplace(heap, key)
            except OSError:
                continue

        entries = [item[2] for item in sorted(heap, key=lambda item: item[:2], reverse=True)]
        return RecentFiles(entries=entries, scanned_files=scanned, truncated=truncated)

    @staticmethod
    def _recent_ignore_patterns(root: Path) -> tuple[str, ...]:
        ignore_file = root / RECENT_IGNORE_FILE
        try:
            if ignore_file.is_symlink() or not ignore_file.is_file():
                return ()
            if ignore_file.stat().st_size > MAX_RECENT_IGNORE_BYTES:
                return ()
            raw = ignore_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ()
        return parse_recent_ignore_patterns(raw)

    def write_text(
        self,
        workspace_path: Path,
        relative_path: str,
        content: str,
        *,
        expected_digest: str,
        expected_mtime_ns: int,
    ) -> FileSnapshot:
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_edit_bytes:
            raise UnsupportedFileError("Content exceeds the editable size limit")

        target = self._resolve_existing(workspace_path, relative_path)
        if not target.is_file():
            raise UnsupportedFileError("Only regular files can be saved")

        def require_expected_source() -> os.stat_result:
            checked = self._resolve_existing(workspace_path, relative_path)
            if checked != target or not checked.is_file():
                raise FileConflictError("The file changed after it was opened")
            before = checked.stat()
            current = checked.read_bytes()
            after = checked.stat()
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or after.st_mtime_ns != expected_mtime_ns
                or file_digest(current) != expected_digest
            ):
                raise FileConflictError("The file changed after it was opened")
            return after

        current_stat = require_expected_source()

        parent = target.parent.resolve(strict=True)
        boundary = workspace_path.resolve(strict=True)
        if not is_within(parent, boundary):
            raise PathBoundaryError("Save destination escapes the workspace")

        mode = current_stat.st_mode & 0o777
        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=parent, prefix=f".{target.name}.", delete=False
            ) as temporary:
                temp_path = temporary.name
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temp_path, mode)
            require_expected_source()
            os.replace(temp_path, target)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
        return self.read_text(workspace_path, relative_path)

    def write_new_text(
        self,
        workspace_path: Path,
        relative_path: str,
        content: str,
    ) -> FileSnapshot:
        """Create one UTF-8 file without ever replacing an existing path."""

        encoded = content.encode("utf-8")
        if len(encoded) > self.max_edit_bytes:
            raise UnsupportedFileError("Content exceeds the editable size limit")
        raw = Path(relative_path)
        if (
            raw.is_absolute()
            or not raw.parts
            or any(part in {"", ".", ".."} for part in raw.parts)
        ):
            raise PathBoundaryError("Path must be a normalized Workspace-relative path")
        parent_relative = raw.parent.as_posix()
        parent = (
            workspace_path.resolve(strict=True)
            if parent_relative == "."
            else self._resolve_existing(workspace_path, parent_relative)
        )
        if not parent.is_dir():
            raise NotADirectoryError(parent)
        target = parent / raw.name
        boundary = workspace_path.resolve(strict=True)
        if not is_within(parent, boundary):
            raise PathBoundaryError("Save destination escapes the workspace")
        if target.exists() or target.is_symlink():
            raise FileExistsError(target)

        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=parent, prefix=f".{target.name}.", delete=False
            ) as temporary:
                temp_path = temporary.name
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temp_path, 0o644)
            try:
                os.link(temp_path, target, follow_symlinks=False)
            except FileExistsError:
                raise FileExistsError(target) from None
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
        return self.read_text(workspace_path, relative_path)

    def create(self, workspace_path: Path, parent: str, name: str, *, directory: bool) -> Path:
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("Invalid name")
        resolve_inside(workspace_path, parent)
        target = resolve_inside(workspace_path, Path(parent) / name, must_exist=False)
        if target.exists():
            raise FileExistsError(target)
        if directory:
            target.mkdir(mode=0o755)
        else:
            target.touch(mode=0o644)
        return target

    def upload_target(self, workspace_path: Path, parent: str, filename: str) -> Path:
        name = Path(filename).name
        if not name or name in {".", ".."} or name != filename or "/" in name or "\\" in name:
            raise ValueError("Invalid upload filename")
        directory = resolve_inside(workspace_path, parent)
        if not directory.is_dir():
            raise NotADirectoryError(directory)
        target = resolve_inside(workspace_path, Path(parent) / name, must_exist=False)
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise UnsupportedFileError("Upload target is not a regular file")
        return target

    def rename(self, workspace_path: Path, relative_path: str, new_name: str) -> Path:
        if not new_name or new_name in {".", ".."} or "/" in new_name or "\\" in new_name:
            raise ValueError("Invalid name")
        source = resolve_inside(workspace_path, relative_path)
        if source.is_symlink():
            raise UnsupportedFileError("Symbolic links cannot be renamed")
        destination = resolve_inside(
            workspace_path, source.relative_to(workspace_path).parent / new_name, must_exist=False
        )
        if destination.exists():
            raise FileExistsError(destination)
        source.rename(destination)
        return destination

    def delete(self, workspace_path: Path, relative_path: str) -> None:
        target = resolve_inside(workspace_path, relative_path)
        if target == workspace_path.resolve(strict=True):
            raise PathBoundaryError("The workspace root cannot be deleted")
        if target.is_symlink():
            raise UnsupportedFileError("Symbolic links cannot be deleted")
        if target.is_dir():
            target.rmdir()
        else:
            target.unlink()

    def unified_diff(self, snapshot: FileSnapshot, proposed_content: str) -> str:
        return "".join(
            difflib.unified_diff(
                snapshot.content.splitlines(keepends=True),
                proposed_content.splitlines(keepends=True),
                fromfile=f"a/{snapshot.relative_path}",
                tofile=f"b/{snapshot.relative_path}",
            )
        )
