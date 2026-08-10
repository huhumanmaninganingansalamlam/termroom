from __future__ import annotations

import contextlib
import os
import re
import stat
import unicodedata
import zipfile
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

DEFAULT_ARCHIVE_CHUNK_SIZE = 1024 * 1024
_ZIP_MAGIC = {b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"}
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")

ZipEntryKind = Literal["file", "directory"]
ArchiveInput = str | os.PathLike[str] | BinaryIO


class ArchiveSafetyError(ValueError):
    """A ZIP cannot be materialized without violating the Source contract."""

    def __init__(self, message: str, *, code: str, member: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.member = member


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    max_upload_bytes: int = 2 * 1024**3
    max_entries: int = 100_000
    max_single_file_bytes: int = 2 * 1024**3
    max_total_bytes: int = 8 * 1024**3
    max_path_bytes: int = 4096
    chunk_size: int = DEFAULT_ARCHIVE_CHUNK_SIZE

    def __post_init__(self) -> None:
        if (
            self.max_upload_bytes <= 0
            or self.max_entries <= 0
            or self.max_single_file_bytes <= 0
            or self.max_total_bytes <= 0
            or self.max_path_bytes <= 0
            or self.chunk_size <= 0
        ):
            raise ValueError("Archive limits must all be positive")


DEFAULT_ARCHIVE_LIMITS = ArchiveLimits()


@dataclass(frozen=True, slots=True)
class ZipEntry:
    relative_path: str
    kind: ZipEntryKind
    size: int
    compressed_size: int
    crc32: int
    executable: bool
    archive_name: str


@dataclass(frozen=True, slots=True)
class ZipManifest:
    entries: tuple[ZipEntry, ...]
    total_bytes: int
    cwd_rel: str
    archive_size: int


def validate_zip_filename(filename: str) -> str:
    if not filename or any(unicodedata.category(character) == "Cc" for character in filename):
        raise ArchiveSafetyError("Invalid ZIP filename", code="zip_filename")
    if not filename.casefold().endswith(".zip"):
        raise ArchiveSafetyError("Only .zip archives are supported", code="zip_extension")
    return filename


def normalize_zip_member_path(raw_name: str) -> str:
    """Normalize one ZIP name as a strict, relative POSIX path."""

    if not raw_name:
        raise ArchiveSafetyError("ZIP entry has an empty path", code="zip_path")
    if "\x00" in raw_name or any(
        unicodedata.category(character) == "Cc" for character in raw_name
    ):
        raise ArchiveSafetyError(
            "ZIP entry path contains a control character",
            code="zip_path_control",
            member=raw_name,
        )
    if "\\" in raw_name:
        raise ArchiveSafetyError(
            "ZIP entry paths must use forward slashes",
            code="zip_path_backslash",
            member=raw_name,
        )
    if raw_name.startswith("/") or _WINDOWS_DRIVE.match(raw_name):
        raise ArchiveSafetyError(
            "ZIP entry path must be relative",
            code="zip_path_absolute",
            member=raw_name,
        )

    parts: list[str] = []
    for part in raw_name.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise ArchiveSafetyError(
                "ZIP entry path cannot traverse parent directories",
                code="zip_path_traversal",
                member=raw_name,
            )
        parts.append(part)
    if not parts:
        raise ArchiveSafetyError("ZIP entry has an empty path", code="zip_path", member=raw_name)
    if any(part == ".termroom" for part in parts):
        raise ArchiveSafetyError(
            "ZIP archives cannot provide Termroom metadata",
            code="zip_path_metadata",
            member=raw_name,
        )
    normalized = "/".join(parts)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ArchiveSafetyError(
            "ZIP entry path is not valid UTF-8",
            code="zip_path_encoding",
            member=raw_name,
        ) from exc
    return normalized


@contextlib.contextmanager
def _open_archive_input(source: ArchiveInput) -> Iterator[tuple[BinaryIO, int]]:
    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
        try:
            handle = path.open("rb")
        except OSError as exc:
            raise ArchiveSafetyError("Cannot open ZIP archive", code="zip_open") from exc
        try:
            size = os.fstat(handle.fileno()).st_size
            yield handle, size
        finally:
            handle.close()
        return

    handle = source
    if not hasattr(handle, "read") or not hasattr(handle, "seek"):
        raise TypeError("ZIP source must be a path or seekable binary file")
    try:
        original_position = handle.tell()
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(0)
    except (AttributeError, OSError) as exc:
        raise ArchiveSafetyError(
            "ZIP source must be seekable",
            code="zip_not_seekable",
        ) from exc
    try:
        yield handle, size
    finally:
        with contextlib.suppress(OSError):
            handle.seek(original_position)


def _check_magic(handle: BinaryIO) -> None:
    handle.seek(0)
    magic = handle.read(4)
    handle.seek(0)
    if magic not in _ZIP_MAGIC:
        raise ArchiveSafetyError("File does not have ZIP magic", code="zip_magic")


def _iter_extra_fields(extra: bytes, *, member: str) -> Iterator[tuple[int, bytes]]:
    offset = 0
    while offset < len(extra):
        if len(extra) - offset < 4:
            raise ArchiveSafetyError(
                "ZIP entry has malformed extra metadata",
                code="zip_extra",
                member=member,
            )
        header_id = int.from_bytes(extra[offset : offset + 2], "little")
        length = int.from_bytes(extra[offset + 2 : offset + 4], "little")
        offset += 4
        end = offset + length
        if end > len(extra):
            raise ArchiveSafetyError(
                "ZIP entry has malformed extra metadata",
                code="zip_extra",
                member=member,
            )
        yield header_id, extra[offset:end]
        offset = end


def _reject_link_extra_fields(info: zipfile.ZipInfo, *, member: str) -> None:
    for header_id, payload in _iter_extra_fields(info.extra, member=member):
        # Info-ZIP ASi Unix: CRC32 followed by a POSIX mode. Some writers put
        # symlink metadata here even when external_attr is misleading.
        if header_id == 0x756E:
            if len(payload) < 6:
                raise ArchiveSafetyError(
                    "ZIP entry has malformed Unix metadata",
                    code="zip_extra",
                    member=member,
                )
            unix_mode = int.from_bytes(payload[4:6], "little")
            file_type = stat.S_IFMT(unix_mode)
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ArchiveSafetyError(
                    "ZIP archive contains a link or special file",
                    code="zip_special_entry",
                    member=member,
                )
        # Legacy PKWARE Unix metadata appends link data after the fixed
        # timestamps/uid/gid fields. We do not attempt to restore it.
        if header_id == 0x000D and len(payload) > 12:
            raise ArchiveSafetyError(
                "ZIP archive contains unsupported link metadata",
                code="zip_special_entry",
                member=member,
            )


def _entry_from_info(info: zipfile.ZipInfo, limits: ArchiveLimits) -> ZipEntry:
    # ZipInfo.filename is truncated at NUL by the standard library;
    # orig_filename preserves it for this validation.
    raw_name = info.orig_filename
    try:
        raw_bytes = raw_name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ArchiveSafetyError(
            "ZIP entry path is not valid UTF-8",
            code="zip_path_encoding",
            member=raw_name,
        ) from exc
    if len(raw_bytes) > limits.max_path_bytes:
        raise ArchiveSafetyError(
            "ZIP entry path exceeds the configured limit",
            code="zip_path_limit",
            member=raw_name,
        )
    path = normalize_zip_member_path(raw_name)
    if len(path.encode("utf-8")) > limits.max_path_bytes:
        raise ArchiveSafetyError(
            "ZIP entry path exceeds the configured limit",
            code="zip_path_limit",
            member=raw_name,
        )
    if info.flag_bits & 0x1:
        raise ArchiveSafetyError(
            "Encrypted ZIP archives are not supported",
            code="zip_encrypted",
            member=path,
        )

    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    slash_directory = raw_name.endswith("/")
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ArchiveSafetyError(
            "ZIP archive contains a link or special file",
            code="zip_special_entry",
            member=path,
        )
    if (file_type == stat.S_IFDIR) != slash_directory and file_type != 0:
        raise ArchiveSafetyError(
            "ZIP entry path and file type disagree",
            code="zip_type_mismatch",
            member=path,
        )
    _reject_link_extra_fields(info, member=path)

    kind: ZipEntryKind = "directory" if slash_directory else "file"
    if kind == "directory" and info.file_size != 0:
        raise ArchiveSafetyError(
            "ZIP directory entry contains file data",
            code="zip_type_mismatch",
            member=path,
        )
    if kind == "file" and info.file_size > limits.max_single_file_bytes:
        raise ArchiveSafetyError(
            "ZIP entry exceeds the single-file limit",
            code="zip_single_file_limit",
            member=path,
        )
    if info.file_size < 0 or info.compress_size < 0:
        raise ArchiveSafetyError(
            "ZIP entry has invalid sizes",
            code="zip_invalid",
            member=path,
        )

    return ZipEntry(
        relative_path=path,
        kind=kind,
        size=info.file_size if kind == "file" else 0,
        compressed_size=info.compress_size,
        crc32=info.CRC & 0xFFFFFFFF,
        executable=kind == "file" and bool(unix_mode & 0o111),
        archive_name=raw_name,
    )


def _validate_structure(
    infos: list[zipfile.ZipInfo],
    limits: ArchiveLimits,
) -> tuple[list[tuple[zipfile.ZipInfo, ZipEntry]], int]:
    if len(infos) > limits.max_entries:
        raise ArchiveSafetyError(
            "ZIP archive has too many entries",
            code="zip_entry_limit",
        )

    by_path: dict[str, ZipEntry] = {}
    pairs: list[tuple[zipfile.ZipInfo, ZipEntry]] = []
    declared_total = 0
    for info in infos:
        entry = _entry_from_info(info, limits)
        path = entry.relative_path
        if path in by_path:
            raise ArchiveSafetyError(
                f"ZIP archive contains a duplicate path: {path}",
                code="zip_duplicate_path",
                member=path,
            )

        parts = path.split("/")
        for index in range(1, len(parts)):
            ancestor_path = "/".join(parts[:index])
            ancestor = by_path.get(ancestor_path)
            if ancestor is not None and ancestor.kind != "directory":
                raise ArchiveSafetyError(
                    f"ZIP path is nested below a file: {ancestor_path}",
                    code="zip_path_conflict",
                    member=path,
                )
        if entry.kind != "directory" and any(
            candidate.startswith(path + "/") for candidate in by_path
        ):
            raise ArchiveSafetyError(
                f"ZIP file path conflicts with a directory: {path}",
                code="zip_path_conflict",
                member=path,
            )

        by_path[path] = entry
        pairs.append((info, entry))
        declared_total += entry.size
        if declared_total > limits.max_total_bytes:
            raise ArchiveSafetyError(
                "ZIP archive exceeds the total extracted-size limit",
                code="zip_total_limit",
                member=path,
            )
    return pairs, declared_total


def _zip_read_error(exc: BaseException, *, member: str) -> ArchiveSafetyError:
    if isinstance(exc, zipfile.BadZipFile) and "CRC" in str(exc).upper():
        return ArchiveSafetyError(
            f"ZIP entry failed CRC verification: {member}",
            code="zip_crc",
            member=member,
        )
    return ArchiveSafetyError(
        f"Cannot safely read ZIP entry: {member}",
        code="zip_invalid",
        member=member,
    )


def _iter_info_chunks(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    entry: ZipEntry,
    limits: ArchiveLimits,
    *,
    total_before: int = 0,
) -> Iterator[bytes]:
    actual_size = 0
    crc = 0
    try:
        with archive.open(info, "r") as member:
            while True:
                chunk = member.read(limits.chunk_size)
                if not chunk:
                    break
                actual_size += len(chunk)
                if actual_size > limits.max_single_file_bytes:
                    raise ArchiveSafetyError(
                        "ZIP entry exceeds the actual single-file limit",
                        code="zip_single_file_limit",
                        member=entry.relative_path,
                    )
                if total_before + actual_size > limits.max_total_bytes:
                    raise ArchiveSafetyError(
                        "ZIP archive exceeds the actual total extracted-size limit",
                        code="zip_total_limit",
                        member=entry.relative_path,
                    )
                crc = zlib.crc32(chunk, crc)
                yield chunk
    except ArchiveSafetyError:
        raise
    except (EOFError, NotImplementedError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
        raise _zip_read_error(exc, member=entry.relative_path) from exc

    if actual_size != entry.size:
        raise ArchiveSafetyError(
            f"ZIP entry size does not match its data: {entry.relative_path}",
            code="zip_size_mismatch",
            member=entry.relative_path,
        )
    if crc & 0xFFFFFFFF != entry.crc32:
        raise ArchiveSafetyError(
            f"ZIP entry failed CRC verification: {entry.relative_path}",
            code="zip_crc",
            member=entry.relative_path,
        )


def _cwd_rel(entries: Iterable[ZipEntry]) -> str:
    values = tuple(entries)
    if not values:
        return "."
    top_levels = {entry.relative_path.split("/", 1)[0] for entry in values}
    if len(top_levels) != 1:
        return "."
    top = next(iter(top_levels))
    top_is_directory = any(
        entry.relative_path == top and entry.kind == "directory"
        or entry.relative_path.startswith(top + "/")
        for entry in values
    )
    return top if top_is_directory else "."


def validate_zip_archive(
    source: ArchiveInput,
    *,
    limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
    original_filename: str | None = None,
) -> ZipManifest:
    """Structurally validate, fully inflate, and CRC-check a ZIP spool."""

    if original_filename is not None:
        validate_zip_filename(original_filename)
    with _open_archive_input(source) as (handle, archive_size):
        if archive_size > limits.max_upload_bytes:
            raise ArchiveSafetyError(
                "ZIP upload exceeds the configured limit",
                code="zip_upload_limit",
            )
        _check_magic(handle)
        try:
            with zipfile.ZipFile(handle, "r") as archive:
                pairs, _declared_total = _validate_structure(archive.infolist(), limits)
                actual_total = 0
                for info, entry in pairs:
                    if entry.kind == "directory":
                        continue
                    for chunk in _iter_info_chunks(
                        archive,
                        info,
                        entry,
                        limits,
                        total_before=actual_total,
                    ):
                        actual_total += len(chunk)
        except ArchiveSafetyError:
            raise
        except (EOFError, NotImplementedError, RuntimeError, zipfile.BadZipFile) as exc:
            raise _zip_read_error(exc, member="archive") from exc

    entries = tuple(
        sorted(
            (entry for _info, entry in pairs),
            key=lambda item: (
                item.relative_path.count("/"),
                item.kind != "directory",
                item.relative_path,
            ),
        )
    )
    return ZipManifest(
        entries=entries,
        total_bytes=actual_total,
        cwd_rel=_cwd_rel(entries),
        archive_size=archive_size,
    )


def iter_verified_zip_member_chunks(
    source: ArchiveInput,
    entry: ZipEntry,
    *,
    limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> Iterator[bytes]:
    """Re-open and stream one validated member, checking actual size and CRC.

    The caller must exhaust or close the iterator. A staging destination must
    only be committed after normal iterator completion.
    """

    with _open_archive_input(source) as (handle, archive_size):
        if archive_size > limits.max_upload_bytes:
            raise ArchiveSafetyError(
                "ZIP upload exceeds the configured limit",
                code="zip_upload_limit",
            )
        _check_magic(handle)
        try:
            with zipfile.ZipFile(handle, "r") as archive:
                matches = [
                    info
                    for info in archive.infolist()
                    if info.orig_filename == entry.archive_name
                ]
                if len(matches) != 1:
                    raise ArchiveSafetyError(
                        "Validated ZIP member is missing or duplicated",
                        code="zip_member_changed",
                        member=entry.relative_path,
                    )
                current = _entry_from_info(matches[0], limits)
                if current != entry:
                    raise ArchiveSafetyError(
                        "ZIP member changed after validation",
                        code="zip_member_changed",
                        member=entry.relative_path,
                    )
                yield from _iter_info_chunks(archive, matches[0], entry, limits)
        except ArchiveSafetyError:
            raise
        except (EOFError, NotImplementedError, RuntimeError, zipfile.BadZipFile) as exc:
            raise _zip_read_error(exc, member=entry.relative_path) from exc


class ArchiveSink(Protocol):
    def make_directory(self, relative_path: str) -> None: ...

    def write_file(
        self,
        relative_path: str,
        chunks: Iterable[bytes],
        *,
        executable: bool,
        expected_size: int,
    ) -> None: ...


def _validate_materialization_manifest(
    manifest: ZipManifest,
    limits: ArchiveLimits,
) -> None:
    if manifest.archive_size < 0 or manifest.archive_size > limits.max_upload_bytes:
        raise ArchiveSafetyError("Invalid ZIP manifest upload size", code="zip_manifest")
    if len(manifest.entries) > limits.max_entries:
        raise ArchiveSafetyError("Invalid ZIP manifest entry count", code="zip_manifest")
    by_path: dict[str, ZipEntry] = {}
    total = 0
    for entry in manifest.entries:
        path = normalize_zip_member_path(entry.relative_path)
        if path != entry.relative_path or entry.kind not in {"file", "directory"}:
            raise ArchiveSafetyError(
                "Invalid ZIP materialization manifest",
                code="zip_manifest",
                member=entry.relative_path,
            )
        if entry.archive_name == "" or entry.size < 0 or entry.compressed_size < 0:
            raise ArchiveSafetyError(
                "Invalid ZIP materialization metadata",
                code="zip_manifest",
                member=path,
            )
        if entry.kind == "directory" and (entry.size != 0 or entry.executable):
            raise ArchiveSafetyError(
                "Invalid ZIP directory metadata",
                code="zip_manifest",
                member=path,
            )
        if entry.kind == "file" and entry.size > limits.max_single_file_bytes:
            raise ArchiveSafetyError(
                "ZIP manifest exceeds the single-file limit",
                code="zip_single_file_limit",
                member=path,
            )
        if path in by_path:
            raise ArchiveSafetyError(
                "ZIP manifest contains a duplicate path",
                code="zip_duplicate_path",
                member=path,
            )
        for index in range(1, len(path.split("/"))):
            ancestor_path = "/".join(path.split("/")[:index])
            ancestor = by_path.get(ancestor_path)
            if ancestor is not None and ancestor.kind != "directory":
                raise ArchiveSafetyError(
                    "ZIP manifest contains a path conflict",
                    code="zip_path_conflict",
                    member=path,
                )
        if entry.kind != "directory" and any(
            candidate.startswith(path + "/") for candidate in by_path
        ):
            raise ArchiveSafetyError(
                "ZIP manifest contains a path conflict",
                code="zip_path_conflict",
                member=path,
            )
        by_path[path] = entry
        total += entry.size
        if total > limits.max_total_bytes:
            raise ArchiveSafetyError(
                "ZIP manifest exceeds the total-size limit",
                code="zip_total_limit",
                member=path,
            )
    if total != manifest.total_bytes or _cwd_rel(manifest.entries) != manifest.cwd_rel:
        raise ArchiveSafetyError(
            "Invalid ZIP manifest totals or working directory",
            code="zip_manifest",
        )


def _required_directories(entries: Iterable[ZipEntry]) -> tuple[str, ...]:
    directories: set[str] = set()
    for entry in entries:
        parts = entry.relative_path.split("/")
        parent_limit = len(parts) if entry.kind == "directory" else len(parts) - 1
        for index in range(1, parent_limit + 1):
            directories.add("/".join(parts[:index]))
    return tuple(sorted(directories, key=lambda path: (path.count("/"), path)))


def materialize_zip_archive(
    source: ArchiveInput,
    manifest: ZipManifest,
    sink: ArchiveSink,
    *,
    limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> None:
    """Materialize a validated archive member-by-member into staging."""

    _validate_materialization_manifest(manifest, limits)
    for directory in _required_directories(manifest.entries):
        sink.make_directory(directory)

    # Keep one ZipFile open for the whole extraction. Reopening it and scanning
    # every central-directory entry once per member turns a large, otherwise
    # valid archive into O(n²) work.
    with _open_archive_input(source) as (handle, archive_size):
        if archive_size > limits.max_upload_bytes:
            raise ArchiveSafetyError(
                "ZIP upload exceeds the configured limit",
                code="zip_upload_limit",
            )
        if archive_size != manifest.archive_size:
            raise ArchiveSafetyError(
                "ZIP archive changed after validation",
                code="zip_member_changed",
            )
        _check_magic(handle)
        try:
            with zipfile.ZipFile(handle, "r") as archive:
                pairs, _declared_total = _validate_structure(archive.infolist(), limits)
                current_entries = tuple(
                    sorted(
                        (entry for _info, entry in pairs),
                        key=lambda item: (
                            item.relative_path.count("/"),
                            item.kind != "directory",
                            item.relative_path,
                        ),
                    )
                )
                if current_entries != manifest.entries:
                    raise ArchiveSafetyError(
                        "ZIP archive changed after validation",
                        code="zip_member_changed",
                    )
                infos = {entry.archive_name: info for info, entry in pairs}
                actual_total = 0
                for entry in manifest.entries:
                    if entry.kind == "directory":
                        continue
                    chunks = _iter_info_chunks(
                        archive,
                        infos[entry.archive_name],
                        entry,
                        limits,
                        total_before=actual_total,
                    )
                    sink.write_file(
                        entry.relative_path,
                        chunks,
                        executable=entry.executable,
                        expected_size=entry.size,
                    )
                    actual_total += entry.size
        except ArchiveSafetyError:
            raise
        except (EOFError, NotImplementedError, RuntimeError, zipfile.BadZipFile) as exc:
            raise _zip_read_error(exc, member="archive") from exc
