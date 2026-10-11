from __future__ import annotations

import concurrent.futures
import os
import pty
import select
import threading
import time
import tty

import pytest

from termroom.pty_process import write_all


def test_nonblocking_pty_backpressure_preserves_complete_paste(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master, slave = pty.openpty()
    tty.setraw(slave)
    os.set_blocking(master, False)
    os.set_blocking(slave, False)
    expected = ("\x1b[200~" + "한글 붙여넣기\r" * 1400 + "\x1b[201~NEXT").encode()
    pressure = threading.Event()
    interrupted = False
    original_write = os.write

    def observed_write(fd: int, value: bytes) -> int:
        nonlocal interrupted
        if fd == master and not interrupted:
            interrupted = True
            raise InterruptedError()
        try:
            return original_write(fd, value)
        except BlockingIOError:
            if fd == master:
                pressure.set()
            raise

    monkeypatch.setattr(os, "write", observed_write)
    received = bytearray()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(write_all, master, expected)
        try:
            assert pressure.wait(2), "Fixture must fill the actual nonblocking PTY buffer"
            assert not writer.done(), "Backpressure must wait rather than fail or truncate"
            deadline = time.monotonic() + 5
            while len(received) < len(expected) and time.monotonic() < deadline:
                ready, _, _ = select.select([slave], [], [], 0.1)
                if ready:
                    received.extend(os.read(slave, 512))
                    time.sleep(0.001)
            writer.result(timeout=2)
            assert bytes(received) == expected
        finally:
            os.close(slave)
            os.close(master)


def test_pty_writer_propagates_closed_recipient() -> None:
    master, slave = pty.openpty()
    os.close(slave)
    os.close(master)
    with pytest.raises(OSError):
        write_all(master, b"undeliverable")
