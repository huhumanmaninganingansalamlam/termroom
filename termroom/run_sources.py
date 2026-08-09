from __future__ import annotations

import contextlib
import os
import stat
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal, Protocol
from urllib.parse import urlsplit

DEFAULT_TRANSFER_CHUNK_SIZE = 1024 * 1024

DEFAULT_EXCLUDED_DIRECTORY_NAMES = frozenset(
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
        ".tox",
        ".cache",
        ".uv-cache",
    }
)
DEFAULT_EXCLUDED_FILE_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.development",
        ".env.production",
        ".env.test",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
)
PRIVATE_KEY_SUFFIXES = (".key", ".p12", ".pfx", ".pem")

WorkspaceEntryKind = Literal["file", "directory", "symlink"]


class SourceValidationError(ValueError):
    """A stable, user-correctable Source validation failure."""

    def __init__(self, message: str, *, code: str, path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


class SourceFileChangedError(SourceValidationError):
    def __init__(
        self,
        path: str,
        *,
        current_size: int | None = None,
        current_mtime_ns: int | None = None,
    ) -> None:
        super().__init__(
            f"Source file changed while it was being copied: {path}",
            code="source_file_changed",
            path=path,
        )
        self.current_size = current_size
        self.current_mtime_ns = current_mtime_ns


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _require_metadata_line(value: str, *, field: str) -> str:
    if not value or _contains_control(value):
        raise SourceValidationError(
            f"{field} must be one non-empty line",
            code="invalid_metadata",
        )
    return value


def validate_public_https_git_url(value: str) -> str:
    """Validate the v1 anonymous public Git URL contract.

    This validates the transport shape only. Whether the repository is public
    is established by running the isolated, non-interactive clone invocation.
    """

    if not isinstance(value, str) or not value:
        raise SourceValidationError("A Git repository URL is required", code="git_url_required")
    if value != value.strip() or any(character.isspace() for character in value):
        raise SourceValidationError(
            "Git repository URLs cannot contain whitespace",
            code="git_url_whitespace",
        )
    if _contains_control(value):
        raise SourceValidationError(
            "Git repository URLs cannot contain control characters",
            code="git_url_control",
        )
    if "\\" in value:
        raise SourceValidationError(
            "Git repository URLs must use URL path separators",
            code="git_url_invalid",
        )
    # urlsplit cannot distinguish an absent query from a trailing empty '?'.
    if "?" in value:
        raise SourceValidationError(
            "Git repository URLs cannot contain a query",
            code="git_url_query",
        )
    if "#" in value:
        raise SourceValidationError(
            "Git repository URLs cannot contain a fragment",
            code="git_url_fragment",
        )

    try:
        parsed = urlsplit(value)
        # Accessing port performs urllib's range and syntax validation.
        _port = parsed.port
    except ValueError as exc:
        raise SourceValidationError("Invalid Git repository URL", code="git_url_invalid") from exc

    if parsed.scheme.lower() != "https":
        raise SourceValidationError(
            "Only anonymous HTTPS Git repositories are supported",
            code="git_url_scheme",
        )
    if not parsed.netloc or not parsed.hostname:
        raise SourceValidationError(
            "The Git repository URL must include a host",
            code="git_url_host",
        )
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise SourceValidationError(
            "Git repository URLs cannot include credentials",
            code="git_url_userinfo",
        )

    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts or any(part in {".", ".."} for part in path_parts):
        raise SourceValidationError(
            "The Git repository URL must include an unambiguous repository path",
            code="git_url_path",
        )
    return value


def _validate_absolute_remote_path(value: str, *, field: str) -> str:
    _require_metadata_line(value, field=field)
    if "\\" in value or not value.startswith("/"):
        raise SourceValidationError(
            f"{field} must be an absolute POSIX path",
            code="git_clone_path",
        )
    normalized = PurePosixPath(value).as_posix()
    if (
        normalized != value
        or normalized == "/"
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise SourceValidationError(
            f"{field} must be a normalized non-root absolute POSIX path",
            code="git_clone_path",
        )
    return normalized


def _validate_fixed_path(value: str) -> str:
    _require_metadata_line(value, field="PATH")
    components = value.split(":")
    if not components or any(
        not component.startswith("/")
        or PurePosixPath(component).as_posix() != component
        or any(part in {".", ".."} for part in component.split("/"))
        for component in components
    ):
        raise SourceValidationError(
            "The Git clone PATH must contain absolute normalized directories",
            code="git_clone_path",
        )
    return value


@dataclass(frozen=True, slots=True)
class GitCloneInvocation:
    """An argv plus a from-scratch environment for an anonymous clone."""

    argv: tuple[str, ...]
    env: Mapping[str, str]

    def as_env_i_argv(self, *, env_path: str = "/usr/bin/env") -> tuple[str, ...]:
        """Return an argv suitable for a remote exec primitive using ``env -i``."""

        safe_env_path = _validate_absolute_remote_path(env_path, field="env executable")
        assignments = tuple(f"{key}={value}" for key, value in sorted(self.env.items()))
        return (safe_env_path, "-i", *assignments, *self.argv)


def build_public_git_clone_invocation(
    url: str,
    *,
    git_path: str,
    askpass_path: str,
    empty_home: str,
    destination: str,
    fixed_path: str = "/usr/bin:/bin",
) -> GitCloneInvocation:
    """Build the exact non-interactive shallow clone argv and clean environment.

    Callers must pass this environment as a replacement environment (or use
    :meth:`GitCloneInvocation.as_env_i_argv`), never merge it with the target
    user's environment.
    """

    safe_url = validate_public_https_git_url(url)
    safe_git = _validate_absolute_remote_path(git_path, field="git executable")
    safe_askpass = _validate_absolute_remote_path(askpass_path, field="askpass executable")
    safe_home = _validate_absolute_remote_path(empty_home, field="empty Git HOME")
    safe_destination = _validate_absolute_remote_path(destination, field="clone destination")
    safe_path = _validate_fixed_path(fixed_path)

    argv = (
        safe_git,
        "-c",
        "credential.helper=",
        "-c",
        "credential.useHttpPath=false",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        "-c",
        "http.followRedirects=false",
        "-c",
        "http.extraHeader=",
        "-c",
        "http.cookieFile=",
        "-c",
        "http.sslCert=",
        "-c",
        "http.sslKey=",
        "-c",
        "submodule.recurse=false",
        "clone",
        "--depth=1",
        "--single-branch",
        "--no-tags",
        "--no-recurse-submodules",
        "--",
        safe_url,
        safe_destination,
    )
    env = MappingProxyType(
        {
            "GIT_ASKPASS": safe_askpass,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": safe_home,
            "LC_ALL": "C",
            "PATH": safe_path,
            "SSH_ASKPASS": safe_askpass,
            "XDG_CONFIG_HOME": f"{safe_home}/.config",
        }
    )
    return GitCloneInvocation(argv=argv, env=env)


def normalize_source_relative_path(value: str, *, allow_root: bool = False) -> str:
    """Return a strict POSIX relative path for a Source manifest."""

    if not isinstance(value, str) or not value:
        raise SourceValidationError("A relative Source path is required", code="source_path")
    if _contains_control(value) or "\\" in value:
        raise SourceValidationError(
            "Source paths cannot contain control characters or backslashes",
            code="source_path",
            path=value,
        )
    if value == "." and allow_root:
        return value
    if value.startswith("/") or (len(value) >= 2 and value[0].isalpha() and value[1] == ":"):
        raise SourceValidationError(
            "Source paths must be relative",
            code="source_path_absolute",
            path=value,
        )
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise SourceValidationError(
            "Source paths must already be normalized and cannot traverse parents",
            code="source_path_traversal",
            path=value,
        )
    if any(part == ".termroom" for part in parts):
        raise SourceValidationError(
            "The .termroom metadata directory cannot be a Source path",
            code="source_path_metadata",
            path=value,
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SourceValidationError(
            "Source paths must be valid UTF-8",
            code="source_path_encoding",
            path=value,
        ) from exc
    return value


def validate_cwd_rel(value: str) -> str:
    return normalize_source_relative_path(value, allow_root=True)


def validate_contained_symlink_target(link_path: str, target: str) -> str:
    """Validate a relative symlink's target lexically against the Source root."""

    safe_link = normalize_source_relative_path(link_path)
    if not target or _contains_control(target) or "\\" in target or target.startswith("/"):
        raise SourceValidationError(
            "Only contained relative symbolic links can be copied",
            code="source_symlink_outside",
            path=safe_link,
        )

    stack = list(PurePosixPath(safe_link).parent.parts)
    for part in target.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not stack:
                raise SourceValidationError(
                    "Symbolic link target escapes the Source root",
                    code="source_symlink_outside",
                    path=safe_link,
                )
            stack.pop()
            continue
        if part == ".termroom":
            raise SourceValidationError(
                "Symbolic links into .termroom metadata are not copied",
                code="source_symlink_metadata",
                path=safe_link,
            )
        stack.append(part)
    if not stack:
        # A link to the Source root is contained and remains relative.
        return target
    normalize_source_relative_path("/".join(stack))
    return target


@dataclass(frozen=True, slots=True)
class WorkspaceEntry:
    relative_path: str
    kind: WorkspaceEntryKind
    size: int = 0
    mtime_ns: int = 0
    executable: bool = False
    link_target: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceManifest:
    entries: tuple[WorkspaceEntry, ...]
    total_bytes: int


def build_workspace_manifest(entries: Iterable[WorkspaceEntry]) -> WorkspaceManifest:
    """Validate and canonicalize one authoritative Workspace tree listing."""

    by_path: dict[str, WorkspaceEntry] = {}
    for entry in entries:
        path = normalize_source_relative_path(entry.relative_path)
        if entry.kind not in {"file", "directory", "symlink"}:
            raise SourceValidationError(
                f"Unsupported Source entry type: {entry.kind}",
                code="source_entry_type",
                path=path,
            )
        if path in by_path:
            raise SourceValidationError(
                f"Duplicate Source path: {path}",
                code="source_path_duplicate",
                path=path,
            )
        if entry.size < 0 or entry.mtime_ns < 0:
            raise SourceValidationError(
                f"Invalid Source metadata: {path}",
                code="source_entry_metadata",
                path=path,
            )

        if entry.kind == "symlink":
            if entry.link_target is None:
                raise SourceValidationError(
                    f"Symbolic link target is missing: {path}",
                    code="source_symlink_target",
                    path=path,
                )
            validate_contained_symlink_target(path, entry.link_target)
        elif entry.link_target is not None:
            raise SourceValidationError(
                f"Only symbolic links can have link targets: {path}",
                code="source_entry_metadata",
                path=path,
            )

        canonical = replace(
            entry,
            relative_path=path,
            size=entry.size if entry.kind == "file" else 0,
            executable=entry.executable if entry.kind == "file" else False,
        )
        by_path[path] = canonical

    for path, entry in by_path.items():
        parts = path.split("/")
        for index in range(1, len(parts)):
            ancestor_path = "/".join(parts[:index])
            ancestor = by_path.get(ancestor_path)
            if ancestor is not None and ancestor.kind != "directory":
                raise SourceValidationError(
                    f"Source path is nested below a non-directory: {ancestor_path}",
                    code="source_path_conflict",
                    path=path,
                )
        if entry.kind != "directory" and any(
            candidate.startswith(path + "/") for candidate in by_path
        ):
            raise SourceValidationError(
                f"Source path conflicts with a child path: {path}",
                code="source_path_conflict",
                path=path,
            )

    ordered = tuple(
        sorted(
            by_path.values(),
            key=lambda item: (
                item.relative_path.count("/"),
                item.kind != "directory",
                item.relative_path,
            ),
        )
    )
    return WorkspaceManifest(
        entries=ordered,
        total_bytes=sum(entry.size for entry in ordered if entry.kind == "file"),
    )


def normalize_explicit_include_paths(values: Iterable[str]) -> frozenset[str]:
    return frozenset(normalize_source_relative_path(value) for value in values)


def is_default_workspace_excluded(relative_path: str) -> bool:
    path = normalize_source_relative_path(relative_path)
    parts = path.split("/")
    name = parts[-1]
    return (
        any(part in DEFAULT_EXCLUDED_DIRECTORY_NAMES for part in parts)
        or name in DEFAULT_EXCLUDED_FILE_NAMES
        or name.startswith(".env.")
        or name.casefold().endswith(PRIVATE_KEY_SUFFIXES)
    )


def _is_related_to_explicit_include(path: str, includes: frozenset[str]) -> bool:
    return any(
        path == include
        or path.startswith(include + "/")
        or include.startswith(path + "/")
        for include in includes
    )


def scan_local_workspace(
    root: Path,
    *,
    mandatory_excludes: Iterable[Path] = (),
    explicitly_included: Iterable[str] = (),
) -> WorkspaceManifest:
    """Perform the one authoritative, no-follow Local Workspace tree scan."""

    source_argument = Path(root).expanduser()
    if source_argument.is_symlink():
        raise SourceValidationError(
            "A Workspace Source root cannot be a symbolic link",
            code="source_root_symlink",
            path=str(source_argument),
        )
    try:
        source_root = source_argument.resolve(strict=True)
    except OSError as exc:
        raise SourceValidationError(
            "The Workspace Source does not exist",
            code="source_root_missing",
            path=str(source_argument),
        ) from exc
    if not source_root.is_dir():
        raise SourceValidationError(
            "The Workspace Source must be a directory",
            code="source_root_type",
            path=str(source_root),
        )

    mandatory = tuple(Path(path).expanduser().resolve(strict=False) for path in mandatory_excludes)
    for boundary in mandatory:
        if source_root == boundary or source_root.is_relative_to(boundary):
            raise SourceValidationError(
                "The selected Workspace is inside Termroom's private state boundary",
                code="source_private_boundary",
                path=str(source_root),
            )
    mandatory_relative = tuple(
        boundary.relative_to(source_root).as_posix()
        for boundary in mandatory
        if boundary.is_relative_to(source_root)
    )

    includes = normalize_explicit_include_paths(explicitly_included)
    collected: list[WorkspaceEntry] = []

    def is_mandatory_excluded(relative_path: str) -> bool:
        if ".termroom" in relative_path.split("/"):
            return True
        return any(
            relative_path == boundary or relative_path.startswith(boundary + "/")
            for boundary in mandatory_relative
        )

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)

    def walk(directory_fd: int, prefix: str = "") -> None:
        try:
            with os.scandir(directory_fd) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise SourceValidationError(
                f"Cannot scan Workspace directory: {prefix or '.'}",
                code="source_scan_failed",
                path=prefix or ".",
            ) from exc

        for child in children:
            raw_relative = f"{prefix}/{child.name}" if prefix else child.name
            relative = normalize_source_relative_path(raw_relative)
            if is_mandatory_excluded(relative):
                continue
            if is_default_workspace_excluded(relative) and not _is_related_to_explicit_include(
                relative, includes
            ):
                continue

            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise SourceValidationError(
                    f"Cannot inspect Workspace path: {relative}",
                    code="source_scan_failed",
                    path=relative,
                ) from exc

            if stat.S_ISLNK(metadata.st_mode):
                try:
                    link_target = os.readlink(child.name, dir_fd=directory_fd)
                except OSError as exc:
                    raise SourceValidationError(
                        f"Cannot read symbolic link: {relative}",
                        code="source_scan_failed",
                        path=relative,
                    ) from exc
                validate_contained_symlink_target(relative, link_target)
                collected.append(
                    WorkspaceEntry(
                        relative,
                        "symlink",
                        mtime_ns=metadata.st_mtime_ns,
                        link_target=link_target,
                    )
                )
            elif stat.S_ISDIR(metadata.st_mode):
                collected.append(
                    WorkspaceEntry(relative, "directory", mtime_ns=metadata.st_mtime_ns)
                )
                try:
                    child_fd = os.open(child.name, directory_flags, dir_fd=directory_fd)
                except OSError as exc:
                    raise SourceValidationError(
                        f"Cannot open Workspace directory without following links: {relative}",
                        code="source_scan_failed",
                        path=relative,
                    ) from exc
                try:
                    walk(child_fd, relative)
                finally:
                    with contextlib.suppress(OSError):
                        os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                collected.append(
                    WorkspaceEntry(
                        relative,
                        "file",
                        size=metadata.st_size,
                        mtime_ns=metadata.st_mtime_ns,
                        executable=bool(metadata.st_mode & 0o111),
                    )
                )
            else:
                raise SourceValidationError(
                    f"Unsupported special file in Workspace Source: {relative}",
                    code="source_special_file",
                    path=relative,
                )

    try:
        root_fd = os.open(source_root, directory_flags)
    except OSError as exc:
        raise SourceValidationError(
            "Cannot open the Workspace Source without following links",
            code="source_scan_failed",
            path=str(source_root),
        ) from exc
    try:
        walk(root_fd)
    finally:
        with contextlib.suppress(OSError):
            os.close(root_fd)
    return build_workspace_manifest(collected)


def iter_stable_local_file_chunks(
    root: Path,
    entry: WorkspaceEntry,
    *,
    chunk_size: int = DEFAULT_TRANSFER_CHUNK_SIZE,
) -> Iterator[bytes]:
    """Read one manifest file through no-follow descriptors and detect changes."""

    if entry.kind != "file":
        raise SourceValidationError(
            "Only regular manifest files can be read",
            code="source_entry_type",
            path=entry.relative_path,
        )
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    relative = normalize_source_relative_path(entry.relative_path)
    parts = relative.split("/")
    source_root = Path(root).expanduser().resolve(strict=True)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)

    descriptors: list[int] = []
    try:
        current_fd = os.open(source_root, directory_flags)
        descriptors.append(current_fd)
        for part in parts[:-1]:
            current_fd = os.open(part, directory_flags, dir_fd=current_fd)
            descriptors.append(current_fd)
        file_fd = os.open(parts[-1], file_flags, dir_fd=current_fd)
        descriptors.append(file_fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise SourceValidationError(
                "Workspace path is no longer a regular file",
                code="source_entry_type",
                path=relative,
            )
        if before.st_size != entry.size or before.st_mtime_ns != entry.mtime_ns:
            raise SourceFileChangedError(
                relative,
                current_size=before.st_size,
                current_mtime_ns=before.st_mtime_ns,
            )

        total = 0
        while True:
            chunk = os.read(file_fd, chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if total > before.st_size:
                current = os.fstat(file_fd)
                raise SourceFileChangedError(
                    relative,
                    current_size=current.st_size,
                    current_mtime_ns=current.st_mtime_ns,
                )
            yield chunk

        after = os.fstat(file_fd)
        if (
            total != before.st_size
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            raise SourceFileChangedError(
                relative,
                current_size=after.st_size,
                current_mtime_ns=after.st_mtime_ns,
            )
    except SourceValidationError:
        raise
    except OSError as exc:
        raise SourceValidationError(
            f"Cannot read Workspace file: {relative}",
            code="source_read_failed",
            path=relative,
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            with contextlib.suppress(OSError):
                os.close(descriptor)


class SnapshotSource(Protocol):
    def scan(self) -> WorkspaceManifest: ...

    def iter_file_chunks(
        self,
        entry: WorkspaceEntry,
        *,
        chunk_size: int,
    ) -> Iterator[bytes]: ...


class SnapshotSink(Protocol):
    def make_directory(self, relative_path: str, *, executable: bool) -> None: ...

    def write_file(
        self,
        relative_path: str,
        chunks: Iterable[bytes],
        *,
        executable: bool,
        expected_size: int,
    ) -> None: ...

    def make_symlink(self, relative_path: str, link_target: str) -> None: ...


@dataclass(slots=True)
class LocalWorkspaceSnapshotSource:
    root: Path
    mandatory_excludes: tuple[Path, ...] = ()
    explicitly_included: tuple[str, ...] = ()

    def scan(self) -> WorkspaceManifest:
        return scan_local_workspace(
            self.root,
            mandatory_excludes=self.mandatory_excludes,
            explicitly_included=self.explicitly_included,
        )

    def iter_file_chunks(
        self,
        entry: WorkspaceEntry,
        *,
        chunk_size: int = DEFAULT_TRANSFER_CHUNK_SIZE,
    ) -> Iterator[bytes]:
        return iter_stable_local_file_chunks(self.root, entry, chunk_size=chunk_size)


def _require_exact_chunks(
    chunks: Iterable[bytes],
    *,
    expected_size: int,
    path: str,
) -> Iterator[bytes]:
    total = 0
    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise TypeError("Snapshot chunks must be bytes")
        total += len(chunk)
        if total > expected_size:
            raise SourceFileChangedError(path)
        if chunk:
            yield chunk
    if total != expected_size:
        raise SourceFileChangedError(path)


def materialize_workspace_snapshot(
    source: SnapshotSource,
    sink: SnapshotSink,
    *,
    chunk_size: int = DEFAULT_TRANSFER_CHUNK_SIZE,
) -> WorkspaceManifest:
    """Drive a bounded Source-to-staging transfer through narrow interfaces.

    ``SnapshotSink.write_file`` must leave no committed destination file when
    its chunk iterator raises. A file that changes is retried once from the
    beginning, matching the Source preparation contract.
    """

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    scanned_manifest = source.scan()
    manifest = build_workspace_manifest(scanned_manifest.entries)
    if manifest.total_bytes != scanned_manifest.total_bytes:
        raise SourceValidationError(
            "Workspace manifest total does not match its entries",
            code="source_manifest_total",
        )
    final_entries = list(manifest.entries)
    for entry_index, entry in enumerate(final_entries):
        if entry.kind == "directory":
            sink.make_directory(entry.relative_path, executable=False)
        elif entry.kind == "symlink":
            assert entry.link_target is not None
            sink.make_symlink(entry.relative_path, entry.link_target)
        else:
            current_entry = entry
            for attempt in range(2):
                chunks = _require_exact_chunks(
                    source.iter_file_chunks(current_entry, chunk_size=chunk_size),
                    expected_size=current_entry.size,
                    path=current_entry.relative_path,
                )
                try:
                    sink.write_file(
                        current_entry.relative_path,
                        chunks,
                        executable=current_entry.executable,
                        expected_size=current_entry.size,
                    )
                except SourceFileChangedError as exc:
                    if attempt:
                        raise
                    if exc.current_size is not None and exc.current_mtime_ns is not None:
                        current_entry = replace(
                            current_entry,
                            size=exc.current_size,
                            mtime_ns=exc.current_mtime_ns,
                        )
                        final_entries[entry_index] = current_entry
                    continue
                break
    return build_workspace_manifest(final_entries)
