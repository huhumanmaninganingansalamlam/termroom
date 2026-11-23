"""Strictly isolated OpenSSH process with test-owned keys and tmux root."""
from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from tests.support.core import free_loopback_port


@dataclass(frozen=True)
class IsolatedSshd:
    port: int
    username: str
    client_key: Path


@contextlib.contextmanager
def start_isolated_sshd(tmp_path: Path) -> Iterator[IsolatedSshd]:
    qa = tmp_path / "sshd"
    qa.mkdir()
    remote_tmux_root = qa / "tmux"
    remote_tmux_root.mkdir(mode=0o700)
    host_key = qa / "host_key"
    client_key = qa / "client_key"
    authorized_keys = qa / "authorized_keys"
    for key in (host_key, client_key):
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
            check=True,
        )
    authorized_keys.write_bytes(client_key.with_suffix(".pub").read_bytes())
    authorized_keys.chmod(0o600)
    port = free_loopback_port()
    username = os.environ.get("USER") or subprocess.check_output(
        ["id", "-un"], text=True,
    ).strip()
    config = qa / "sshd_config"
    config.write_text(
        "\n".join([
            f"Port {port}",
            "ListenAddress 127.0.0.1",
            f"HostKey {host_key}",
            f"PidFile {qa / 'sshd.pid'}",
            f"AuthorizedKeysFile {authorized_keys}",
            "StrictModes no",
            "PasswordAuthentication no",
            "KbdInteractiveAuthentication no",
            "PubkeyAuthentication yes",
            "UsePAM no",
            "PermitRootLogin no",
            f"AllowUsers {username}",
            f"SetEnv TMUX_TMPDIR={remote_tmux_root}",
            "LogLevel ERROR",
            "Subsystem sftp internal-sftp",
            "",
        ]),
        encoding="utf-8",
    )
    log = (qa / "sshd.log").open("wb")
    process = subprocess.Popen(
        ["/usr/sbin/sshd", "-D", "-e", "-f", str(config)],
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError as exc:
                if process.poll() is not None:
                    raise AssertionError(
                        f"sshd {port} exited: {log.name}="
                        f"{(qa / 'sshd.log').read_text(encoding='utf-8')}"
                    ) from exc
                time.sleep(0.05)
        else:
            raise AssertionError(f"Private sshd {port} failed to listen")
        yield IsolatedSshd(port=port, username=username, client_key=client_key)
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        log.close()
