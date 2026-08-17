from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

from termroom.run_sources import (
    GitCloneInvocation,
    LocalWorkspaceSnapshotSource,
    SourceFileChangedError,
    SourceValidationError,
    WorkspaceEntry,
    WorkspaceManifest,
    build_public_git_clone_invocation,
    build_workspace_manifest,
    iter_stable_local_file_chunks,
    materialize_workspace_snapshot,
    normalize_source_relative_path,
    scan_local_workspace,
    validate_contained_symlink_target,
    validate_cwd_rel,
    validate_public_https_git_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example/project.git",
        "https://gitlab.com/group/nested/project",
        "https://git.example.test:8443/scm/team/project",
        "https://127.0.0.1/public/repository.git",
    ],
)
def test_public_git_url_accepts_anonymous_https_hosts_and_nested_paths(url: str) -> None:
    assert validate_public_https_git_url(url) == url


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("", "git_url_required"),
        ("git@github.com:owner/repo.git", "git_url_scheme"),
        ("ssh://git@github.com/owner/repo.git", "git_url_scheme"),
        ("http://github.com/owner/repo.git", "git_url_scheme"),
        ("file:///tmp/repo", "git_url_scheme"),
        ("ext::sh -c anything", "git_url_whitespace"),
        ("/tmp/repo", "git_url_scheme"),
        ("https://user@github.com/owner/repo.git", "git_url_userinfo"),
        ("https://user:token@github.com/owner/repo.git", "git_url_userinfo"),
        ("https://github.com/owner/repo.git?ref=main", "git_url_query"),
        ("https://github.com/owner/repo.git?", "git_url_query"),
        ("https://github.com/owner/repo.git#readme", "git_url_fragment"),
        ("https://github.com/owner/repo.git ", "git_url_whitespace"),
        ("https://github.com/owner/\nrepo.git", "git_url_whitespace"),
        ("https://github.com\\owner/repo.git", "git_url_invalid"),
        ("https://github.com", "git_url_path"),
        ("https://github.com/", "git_url_path"),
        ("https://github.com/../repo", "git_url_path"),
        ("https://github.com:99999/owner/repo", "git_url_invalid"),
    ],
)
def test_public_git_url_rejects_credentials_non_https_and_ambiguous_data(
    url: str,
    code: str,
) -> None:
    with pytest.raises(SourceValidationError) as failed:
        validate_public_https_git_url(url)
    assert failed.value.code == code


def test_git_clone_invocation_replaces_target_git_and_credential_environment() -> None:
    invocation = build_public_git_clone_invocation(
        "https://git.example.test/group/repository",
        git_path="/usr/bin/git",
        askpass_path="/var/lib/termroom/bin/deny-askpass",
        empty_home="/var/lib/termroom/runs/run-id/.termroom/git-home",
        destination="/var/lib/termroom/runs/run-id/work.tmp",
    )

    assert isinstance(invocation, GitCloneInvocation)
    assert invocation.argv[0] == "/usr/bin/git"
    assert invocation.argv[-3:] == (
        "--",
        "https://git.example.test/group/repository",
        "/var/lib/termroom/runs/run-id/work.tmp",
    )
    assert invocation.argv[invocation.argv.index("clone") + 1 : -3] == (
        "--depth=1",
        "--single-branch",
        "--no-tags",
        "--no-recurse-submodules",
    )
    command_config = {
        invocation.argv[index + 1]
        for index, value in enumerate(invocation.argv[:-1])
        if value == "-c"
    }
    assert {
        "credential.helper=",
        "protocol.allow=never",
        "protocol.https.allow=always",
        "http.followRedirects=false",
        "http.extraHeader=",
        "http.sslCert=",
        "http.sslKey=",
    } <= command_config

    assert dict(invocation.env) == {
        "GIT_ASKPASS": "/var/lib/termroom/bin/deny-askpass",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/var/lib/termroom/runs/run-id/.termroom/git-home",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "SSH_ASKPASS": "/var/lib/termroom/bin/deny-askpass",
        "XDG_CONFIG_HOME": "/var/lib/termroom/runs/run-id/.termroom/git-home/.config",
    }
    for inherited_name in (
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NETRC",
    ):
        assert inherited_name not in invocation.env
    env_argv = invocation.as_env_i_argv()
    assert env_argv[:2] == ("/usr/bin/env", "-i")
    assert env_argv[-len(invocation.argv) :] == invocation.argv
    with pytest.raises(TypeError):
        invocation.env["HOME"] = "/home/ssh-user"  # type: ignore[index]


@pytest.mark.parametrize("field_value", ["git", "../git", "/", "/usr/bin/../bin/git"])
def test_git_clone_invocation_requires_explicit_absolute_normalized_paths(
    field_value: str,
) -> None:
    with pytest.raises(SourceValidationError) as failed:
        build_public_git_clone_invocation(
            "https://example.test/repository",
            git_path=field_value,
            askpass_path="/opt/termroom/askpass",
            empty_home="/tmp/git-home",
            destination="/tmp/work.tmp",
        )
    assert failed.value.code == "git_clone_path"


def test_source_relative_path_and_contained_symlink_primitives() -> None:
    assert normalize_source_relative_path("한글 project/src/main.py") == "한글 project/src/main.py"
    assert (
        normalize_source_relative_path(
            "nested/.termroom/config.json",
            allow_metadata=True,
        )
        == "nested/.termroom/config.json"
    )
    assert validate_cwd_rel(".") == "."
    assert validate_cwd_rel("wrapper") == "wrapper"
    assert validate_contained_symlink_target("src/current.py", "../main.py") == "../main.py"
    assert validate_contained_symlink_target("root-link", ".") == "."

    for value in (
        "",
        ".",
        "/absolute",
        "C:/drive",
        "a//b",
        "a/./b",
        "a/../b",
        "a\\b",
        "a/.termroom/marker",
        "a\x00b",
    ):
        with pytest.raises(SourceValidationError):
            normalize_source_relative_path(value)
    with pytest.raises(SourceValidationError) as outside:
        validate_contained_symlink_target("link", "../outside")
    assert outside.value.code == "source_symlink_outside"


def test_workspace_manifest_is_canonical_bounded_and_rejects_conflicts() -> None:
    manifest = build_workspace_manifest(
        [
            WorkspaceEntry("bin/tool", "file", size=8, mtime_ns=11, executable=True),
            WorkspaceEntry("bin", "directory", size=99, executable=True),
            WorkspaceEntry("current", "symlink", link_target="bin/tool"),
        ]
    )
    assert manifest.total_bytes == 8
    assert [entry.relative_path for entry in manifest.entries] == ["bin", "current", "bin/tool"]
    assert manifest.entries[0].size == 0
    assert manifest.entries[0].executable is False

    with pytest.raises(SourceValidationError) as duplicate:
        build_workspace_manifest(
            [WorkspaceEntry("same", "file"), WorkspaceEntry("same", "directory")]
        )
    assert duplicate.value.code == "source_path_duplicate"
    with pytest.raises(SourceValidationError) as conflict:
        build_workspace_manifest(
            [WorkspaceEntry("parent", "file"), WorkspaceEntry("parent/child", "file")]
        )
    assert conflict.value.code == "source_path_conflict"
    with pytest.raises(SourceValidationError) as outside_link:
        build_workspace_manifest(
            [WorkspaceEntry("nested/link", "symlink", link_target="../../outside")]
        )
    assert outside_link.value.code == "source_symlink_outside"


def test_local_workspace_scan_applies_mandatory_and_reviewable_excludes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state = workspace / ".private-state"
    (workspace / "src").mkdir(parents=True)
    (workspace / ".git" / "objects").mkdir(parents=True)
    (workspace / ".uv-cache" / "wheels").mkdir(parents=True)
    (workspace / "nested" / ".termroom").mkdir(parents=True)
    state.mkdir()
    (workspace / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    tool = workspace / "src" / "tool.sh"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o4755)
    (workspace / ".git" / "config").write_text("public-config", encoding="utf-8")
    (workspace / ".git" / "objects" / "secret").write_text("object", encoding="utf-8")
    (workspace / ".uv-cache" / "wheels" / "cached").symlink_to(
        tmp_path / "outside-cache"
    )
    (workspace / ".env").write_text("SECRET=1", encoding="utf-8")
    (state / "termroom.sqlite3").write_text("private", encoding="utf-8")
    (workspace / "nested" / ".termroom" / "config.json").write_text(
        "private metadata",
        encoding="utf-8",
    )
    (workspace / "src" / "current.py").symlink_to("main.py")

    manifest = scan_local_workspace(
        workspace,
        mandatory_excludes=(state,),
        explicitly_included=(".git/config",),
    )
    entries = {entry.relative_path: entry for entry in manifest.entries}
    assert "src/main.py" in entries
    assert "src/current.py" in entries
    assert entries["src/current.py"].link_target == "main.py"
    assert entries["src/tool.sh"].executable is True
    assert stat.S_ISUID & tool.stat().st_mode
    assert ".git/config" in entries
    assert ".git/objects" not in entries
    assert ".uv-cache" not in entries
    assert ".env" not in entries
    assert ".private-state" not in entries
    assert "nested/.termroom" not in entries
    assert manifest.excluded_prefixes == (".private-state",)

    source = LocalWorkspaceSnapshotSource(
        workspace,
        mandatory_excludes=(state,),
        explicitly_included=(".git/config",),
    )
    assert source.scan() == manifest


def test_local_workspace_scan_rejects_outside_symlinks_and_special_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "outside").symlink_to("../outside")
    with pytest.raises(SourceValidationError) as outside:
        scan_local_workspace(workspace)
    assert outside.value.code == "source_symlink_outside"

    (workspace / "outside").unlink()
    fifo = workspace / "events.fifo"
    os.mkfifo(fifo)
    with pytest.raises(SourceValidationError) as special:
        scan_local_workspace(workspace)
    assert special.value.code == "source_special_file"


def test_stable_local_reader_refuses_file_changed_after_manifest(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_file = workspace / "input.txt"
    source_file.write_bytes(b"before")
    entry = scan_local_workspace(workspace).entries[0]
    source_file.write_bytes(b"after-change")

    with pytest.raises(SourceFileChangedError):
        b"".join(iter_stable_local_file_chunks(workspace, entry, chunk_size=2))


class _ChangedLocalSource:
    def __init__(self, root: Path, stale_manifest: WorkspaceManifest) -> None:
        self.root = root
        self.stale_manifest = stale_manifest

    def scan(self) -> WorkspaceManifest:
        return self.stale_manifest

    def iter_file_chunks(
        self,
        entry: WorkspaceEntry,
        *,
        chunk_size: int,
    ) -> Iterator[bytes]:
        return iter_stable_local_file_chunks(self.root, entry, chunk_size=chunk_size)


def test_workspace_materializer_retries_a_file_changed_after_scan(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source_file = workspace / "input.txt"
    source_file.write_bytes(b"old")
    stale = scan_local_workspace(workspace)
    source_file.write_bytes(b"new content")
    source = _ChangedLocalSource(workspace, stale)
    sink = _MemorySnapshotSink()

    final_manifest = materialize_workspace_snapshot(source, sink, chunk_size=2)

    assert sink.files == {"input.txt": b"new content"}
    assert final_manifest.total_bytes == len(b"new content")
    assert final_manifest.entries[0].mtime_ns == source_file.stat().st_mtime_ns


class _RetryingSource:
    def __init__(self) -> None:
        self.attempts = 0
        self.manifest = WorkspaceManifest(
            entries=(WorkspaceEntry("input.txt", "file", size=2, mtime_ns=1),),
            total_bytes=2,
            excluded_prefixes=(".private-state",),
        )

    def scan(self) -> WorkspaceManifest:
        return self.manifest

    def iter_file_chunks(
        self,
        entry: WorkspaceEntry,
        *,
        chunk_size: int,
    ) -> Iterator[bytes]:
        del entry, chunk_size
        self.attempts += 1
        if self.attempts == 1:
            yield b"x"
            raise SourceFileChangedError("input.txt")
        yield b"ok"


class _MemorySnapshotSink:
    def __init__(self) -> None:
        self.directories: list[str] = []
        self.files: dict[str, bytes] = {}
        self.links: dict[str, str] = {}

    def make_directory(self, relative_path: str, *, executable: bool) -> None:
        assert executable is False
        self.directories.append(relative_path)

    def write_file(
        self,
        relative_path: str,
        chunks: Iterable[bytes],
        *,
        executable: bool,
        expected_size: int,
    ) -> None:
        del executable
        content = b"".join(chunks)
        assert len(content) == expected_size
        self.files[relative_path] = content

    def make_symlink(self, relative_path: str, link_target: str) -> None:
        self.links[relative_path] = link_target


def test_workspace_materializer_retries_one_unstable_file_from_the_beginning() -> None:
    source = _RetryingSource()
    sink = _MemorySnapshotSink()

    manifest = materialize_workspace_snapshot(source, sink, chunk_size=1)

    assert manifest.entries[0].digest == hashlib.sha256(b"ok").hexdigest()
    assert manifest.total_bytes == source.manifest.total_bytes
    assert manifest.excluded_prefixes == (".private-state",)
    assert source.attempts == 2
    assert sink.files == {"input.txt": b"ok"}
