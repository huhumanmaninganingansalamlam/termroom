from __future__ import annotations

import io
import stat
import struct
import zipfile
from collections.abc import Iterable

import pytest

from termroom.archive_safety import (
    ArchiveLimits,
    ArchiveSafetyError,
    ZipManifest,
    iter_verified_zip_member_chunks,
    materialize_zip_archive,
    normalize_zip_member_path,
    validate_zip_archive,
    validate_zip_filename,
)


def _archive(entries: list[tuple[zipfile.ZipInfo | str, bytes]]) -> io.BytesIO:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    output.seek(0)
    return output


def _unix_info(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = mode << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def test_zip_validation_fully_inflates_crc_checks_and_selects_wrapper_cwd() -> None:
    source = _archive(
        [
            (_unix_info("project/", stat.S_IFDIR | 0o755), b""),
            (_unix_info("project/run.sh", stat.S_IFREG | 0o755), b"#!/bin/sh\necho ok\n"),
            ("project/input.txt", b"input"),
        ]
    )

    manifest = validate_zip_archive(source, original_filename="snapshot.ZIP")

    assert manifest.cwd_rel == "project"
    assert manifest.total_bytes == len(b"#!/bin/sh\necho ok\ninput")
    assert manifest.archive_size == len(source.getvalue())
    by_path = {entry.relative_path: entry for entry in manifest.entries}
    assert by_path["project"].kind == "directory"
    assert by_path["project/run.sh"].executable is True
    assert b"".join(iter_verified_zip_member_chunks(source, by_path["project/run.sh"])) == (
        b"#!/bin/sh\necho ok\n"
    )
    assert source.tell() == 0


def test_zip_cwd_is_root_for_multiple_roots_or_one_top_level_file() -> None:
    multiple = validate_zip_archive(
        _archive([("first/a.txt", b"a"), ("second/b.txt", b"b")])
    )
    single_file = validate_zip_archive(_archive([("run.py", b"pass\n")]))
    implicit_wrapper = validate_zip_archive(_archive([("wrapper/run.py", b"pass\n")]))

    assert multiple.cwd_rel == "."
    assert single_file.cwd_rel == "."
    assert implicit_wrapper.cwd_rel == "wrapper"


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("../escape.txt", "zip_path_traversal"),
        ("parent/../../escape.txt", "zip_path_traversal"),
        ("/absolute.txt", "zip_path_absolute"),
        ("C:/drive.txt", "zip_path_absolute"),
        ("folder\\file.txt", "zip_path_backslash"),
        (".termroom/marker", "zip_path_metadata"),
        ("folder/.termroom/marker", "zip_path_metadata"),
    ],
)
def test_zip_rejects_traversal_absolute_backslash_and_metadata_paths(
    name: str,
    code: str,
) -> None:
    with pytest.raises(ArchiveSafetyError) as failed:
        validate_zip_archive(_archive([(name, b"unsafe")]))
    assert failed.value.code == code


def test_zip_path_normalization_detects_control_and_nul_before_library_truncation() -> None:
    for name in ("bad\x00name", "bad\nname", "bad\x7fname"):
        with pytest.raises(ArchiveSafetyError) as failed:
            normalize_zip_member_path(name)
        assert failed.value.code == "zip_path_control"
    assert normalize_zip_member_path("./folder//file.txt") == "folder/file.txt"


def test_zip_rejects_exact_and_normalized_duplicates_and_prefix_conflicts() -> None:
    with pytest.warns(UserWarning):
        duplicate = _archive([("same.txt", b"a"), ("same.txt", b"b")])
    with pytest.raises(ArchiveSafetyError) as exact:
        validate_zip_archive(duplicate)
    assert exact.value.code == "zip_duplicate_path"

    normalized = _archive([("folder/./file.txt", b"a"), ("folder/file.txt", b"b")])
    with pytest.raises(ArchiveSafetyError) as normalized_failure:
        validate_zip_archive(normalized)
    assert normalized_failure.value.code == "zip_duplicate_path"

    for entries in (
        [("parent", b"file"), ("parent/child", b"child")],
        [("parent/child", b"child"), ("parent", b"file")],
    ):
        with pytest.raises(ArchiveSafetyError) as conflict:
            validate_zip_archive(_archive(entries))
        assert conflict.value.code == "zip_path_conflict"


@pytest.mark.parametrize("mode", [stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600])
def test_zip_rejects_symlink_and_special_unix_entries(mode: int) -> None:
    source = _archive([(_unix_info("special", mode), b"target")])
    with pytest.raises(ArchiveSafetyError) as failed:
        validate_zip_archive(source)
    assert failed.value.code == "zip_special_entry"


def test_zip_rejects_legacy_link_extra_metadata() -> None:
    info = _unix_info("link-like", stat.S_IFREG | 0o644)
    payload = b"\x00" * 13
    info.extra = struct.pack("<HH", 0x000D, len(payload)) + payload
    with pytest.raises(ArchiveSafetyError) as failed:
        validate_zip_archive(_archive([(info, b"target")]))
    assert failed.value.code == "zip_special_entry"


def _mark_encrypted(raw: bytes) -> bytes:
    changed = bytearray(raw)
    local = changed.find(b"PK\x03\x04")
    central = changed.find(b"PK\x01\x02")
    assert local >= 0 and central >= 0
    local_flags = int.from_bytes(changed[local + 6 : local + 8], "little") | 1
    central_flags = int.from_bytes(changed[central + 8 : central + 10], "little") | 1
    changed[local + 6 : local + 8] = local_flags.to_bytes(2, "little")
    changed[central + 8 : central + 10] = central_flags.to_bytes(2, "little")
    return bytes(changed)


def test_zip_rejects_encrypted_member_before_prompting_for_a_password() -> None:
    source = _archive([("secret.txt", b"secret")])
    encrypted = io.BytesIO(_mark_encrypted(source.getvalue()))
    with pytest.raises(ArchiveSafetyError) as failed:
        validate_zip_archive(encrypted)
    assert failed.value.code == "zip_encrypted"


@pytest.mark.parametrize(
    ("limits", "entries", "code"),
    [
        (ArchiveLimits(max_entries=1), [("a", b"a"), ("b", b"b")], "zip_entry_limit"),
        (
            ArchiveLimits(max_single_file_bytes=3),
            [("large", b"1234")],
            "zip_single_file_limit",
        ),
        (
            ArchiveLimits(max_total_bytes=3),
            [("a", b"12"), ("b", b"34")],
            "zip_total_limit",
        ),
        (ArchiveLimits(max_path_bytes=3), [("long", b"a")], "zip_path_limit"),
    ],
)
def test_zip_enforces_entry_single_total_and_path_limits(
    limits: ArchiveLimits,
    entries: list[tuple[str, bytes]],
    code: str,
) -> None:
    with pytest.raises(ArchiveSafetyError) as failed:
        validate_zip_archive(_archive(entries), limits=limits)
    assert failed.value.code == code


def test_zip_enforces_compressed_upload_limit_and_magic_and_extension() -> None:
    source = _archive([("file.txt", b"content")])
    with pytest.raises(ArchiveSafetyError) as too_large:
        validate_zip_archive(
            source,
            limits=ArchiveLimits(max_upload_bytes=len(source.getvalue()) - 1),
        )
    assert too_large.value.code == "zip_upload_limit"

    with pytest.raises(ArchiveSafetyError) as magic:
        validate_zip_archive(io.BytesIO(b"not a zip file"))
    assert magic.value.code == "zip_magic"
    with pytest.raises(ArchiveSafetyError) as extension:
        validate_zip_archive(source, original_filename="snapshot.tar.gz")
    assert extension.value.code == "zip_extension"
    assert validate_zip_filename("private-code.zip") == "private-code.zip"


def _corrupt_stored_member(raw: bytes) -> bytes:
    changed = bytearray(raw)
    local = changed.find(b"PK\x03\x04")
    assert local >= 0
    filename_length = int.from_bytes(changed[local + 26 : local + 28], "little")
    extra_length = int.from_bytes(changed[local + 28 : local + 30], "little")
    content_offset = local + 30 + filename_length + extra_length
    changed[content_offset] ^= 0x01
    return bytes(changed)


def test_zip_actual_member_bytes_must_pass_crc_verification() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("result.txt", b"verified bytes")
    corrupted = io.BytesIO(_corrupt_stored_member(output.getvalue()))

    with pytest.raises(ArchiveSafetyError) as failed:
        validate_zip_archive(corrupted)
    assert failed.value.code == "zip_crc"
    assert failed.value.member == "result.txt"


class _MemoryArchiveSink:
    def __init__(self) -> None:
        self.directories: list[str] = []
        self.files: dict[str, bytes] = {}

    def make_directory(self, relative_path: str) -> None:
        self.directories.append(relative_path)

    def write_file(
        self,
        relative_path: str,
        chunks: Iterable[bytes],
        *,
        executable: bool,
        expected_size: int,
    ) -> None:
        content = b"".join(chunks)
        assert len(content) == expected_size
        if relative_path.endswith("run.sh"):
            assert executable is True
        self.files[relative_path] = content


def test_zip_materializer_creates_implicit_directories_and_streams_verified_files(
) -> None:
    source = _archive(
        [
            (_unix_info("wrapper/bin/run.sh", stat.S_IFREG | 0o755), b"#!/bin/sh\n"),
            ("wrapper/data/input.txt", b"input"),
        ]
    )
    manifest: ZipManifest = validate_zip_archive(source)
    sink = _MemoryArchiveSink()

    materialize_zip_archive(source, manifest, sink)

    assert sink.directories == ["wrapper", "wrapper/bin", "wrapper/data"]
    assert sink.files == {
        "wrapper/bin/run.sh": b"#!/bin/sh\n",
        "wrapper/data/input.txt": b"input",
    }


def test_empty_zip_is_valid_and_runs_from_archive_root() -> None:
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w"):
        pass
    source.seek(0)

    manifest = validate_zip_archive(source)

    assert manifest.entries == ()
    assert manifest.total_bytes == 0
    assert manifest.cwd_rel == "."
