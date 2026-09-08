from __future__ import annotations

import io
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from termroom.files import FileConflictError, FileService, UnsupportedFileError
from termroom.ssh_backend import SSHBackend


@pytest.mark.parametrize("backend", ["local", "ssh"])
@pytest.mark.parametrize("operation", ["read", "save", "run"])
def test_text_operations_bound_reads_when_file_grows_after_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, operation: str
) -> None:
    target = tmp_path / "main.py"
    target.write_text("before\n", encoding="utf-8")
    limit = 16
    service = FileService(max_edit_bytes=limit)
    snapshot = service.read_text(tmp_path, target.name)
    reads: list[int] = []

    class GrowingFile(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            reads.append(size)
            assert 0 <= size <= limit + 1, "File growth must not trigger an unbounded read"
            return super().read(size)

    if backend == "local":
        original_open = Path.open

        def growing_open(path, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
            if path == target and mode == "rb":
                return GrowingFile(b"x" * 1024)
            return original_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", growing_open)
        operations = {
            "read": lambda: service.read_text(tmp_path, target.name),
            "run": lambda: service.inspect_runnable(tmp_path, target.name),
            "save": lambda: service.write_text(
                tmp_path, target.name, "mine\n",
                expected_digest=snapshot.digest, expected_mtime_ns=snapshot.mtime_ns,
            ),
        }
    else:
        ssh = object.__new__(SSHBackend)
        client = SimpleNamespace(close=lambda: None)
        sftp = SimpleNamespace(
            open=lambda *_args: GrowingFile(b"x" * 1024),
            close=lambda: None,
            remove=lambda _path: None,
        )
        attr = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_size=7, st_mtime=0)
        monkeypatch.setattr(ssh, "_sftp", lambda _workspace: (client, sftp))
        monkeypatch.setattr(ssh, "_existing_sftp_path", lambda *_args: (str(target), attr))
        monkeypatch.setattr(ssh, "_remote_root", lambda _workspace: str(tmp_path))
        monkeypatch.setattr(
            ssh, "_resolve_real_remote_tree_path", lambda *_args, **_kwargs: (str(target), attr)
        )
        operations = {
            "read": lambda: ssh.read_text({}, target.name, limit),
            "run": lambda: ssh.inspect_runnable({}, target.name, max_bytes=limit),
            "save": lambda: ssh.write_text(
                {}, target.name, "mine\n", expected_digest=snapshot.digest,
                expected_mtime_ns=0, max_bytes=limit,
            ),
        }

    expected_error = FileConflictError if operation == "save" else UnsupportedFileError
    with pytest.raises(expected_error):
        operations[operation]()
    assert reads == [limit + 1]
    assert target.read_text(encoding="utf-8") == "before\n"
