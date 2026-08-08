from __future__ import annotations

import contextlib
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
from collections.abc import Mapping, Sequence


def spawn_pty_process(
    argv: Sequence[str],
    *,
    cwd: str | None = None,
    environment: Mapping[str, str] | None = None,
    rows: int = 24,
    cols: int = 80,
) -> tuple[int, int]:
    if not argv:
        raise ValueError("PTY command cannot be empty")
    master_fd, slave_fd = pty.openpty()
    try:
        _set_window_size(slave_fd, rows=rows, cols=cols)
        slave_name = os.ttyname(slave_fd)
    finally:
        os.close(slave_fd)

    child_environment = dict(os.environ if environment is None else environment)
    ready_read_fd, ready_write_fd = os.pipe()
    os.set_inheritable(ready_write_fd, True)
    helper_argv = [
        sys.executable,
        "-m",
        "termroom.pty_process",
        "--child",
        slave_name,
        cwd or "",
        str(ready_write_fd),
        *argv,
    ]
    try:
        process_pid = os.posix_spawn(sys.executable, helper_argv, child_environment)
    except BaseException:
        os.close(ready_read_fd)
        os.close(ready_write_fd)
        os.close(master_fd)
        raise
    os.close(ready_write_fd)
    try:
        readable, _, _ = select.select([ready_read_fd], [], [], 5.0)
        if not readable:
            with contextlib.suppress(ProcessLookupError):
                os.kill(process_pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(process_pid, 0)
            raise RuntimeError("PTY child did not become ready")
        readiness = os.read(ready_read_fd, 4096)
    finally:
        os.close(ready_read_fd)
    if readiness != b"ready":
        with contextlib.suppress(ChildProcessError):
            os.waitpid(process_pid, 0)
        os.close(master_fd)
        detail = readiness.removeprefix(b"error:").decode("utf-8", errors="replace")
        raise RuntimeError(detail or "PTY child failed before becoming ready")
    return process_pid, master_fd


def _child_main(arguments: list[str]) -> int:
    if len(arguments) < 5 or arguments[0] != "--child":
        return 2
    slave_name = arguments[1]
    cwd = arguments[2]
    try:
        ready_fd = int(arguments[3])
    except ValueError:
        return 2
    target_argv = arguments[4:]
    if not target_argv:
        return 2

    try:
        os.setsid()
        slave_fd = os.open(slave_name, os.O_RDWR)
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            for destination in (0, 1, 2):
                os.dup2(slave_fd, destination)
        finally:
            if slave_fd > 2:
                os.close(slave_fd)
        if cwd:
            os.chdir(cwd)
        os.write(ready_fd, b"ready")
        os.close(ready_fd)
        ready_fd = -1
        os.execvpe(target_argv[0], target_argv, os.environ)
    except BaseException as exc:
        if ready_fd >= 0:
            with contextlib.suppress(OSError):
                os.write(ready_fd, f"error:{exc}".encode("utf-8", errors="replace"))
            with contextlib.suppress(OSError):
                os.close(ready_fd)
        with contextlib.suppress(OSError):
            os.write(2, f"Termroom PTY launch failed: {exc}\r\n".encode())
        return 127
    return 127


def _set_window_size(fd: int, *, rows: int, cols: int) -> None:
    safe_rows = max(4, min(rows, 300))
    safe_cols = max(20, min(cols, 500))
    winsize = struct.pack("HHHH", safe_rows, safe_cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


if __name__ == "__main__":
    raise SystemExit(_child_main(sys.argv[1:]))
