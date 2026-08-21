from __future__ import annotations

import asyncio
import contextlib
import errno
import io
import json
import os
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from starlette.datastructures import UploadFile

from termroom.app import create_app
from termroom.config import Settings
from termroom.db import StateStore
from termroom.file_runs import FILE_RUN_TERMINAL_STATES, FileRunManager
from termroom.files import FileService, UnsupportedFileError
from termroom.remote_runs import RemoteRunManager
from termroom.ssh_backend import SSHBackend, SSHBackendError, SSHHostKeyChanged
from termroom.terminals import TerminalManager, tmux_browser_view_session
from termroom.workspaces import ProjectPathExists, RootManager, WorkspaceManager


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextlib.contextmanager
def _test_sshd(tmp_path: Path, *, log_level: str = "ERROR") -> Iterator[dict[str, object]]:
    qa = tmp_path / "sshd"
    qa.mkdir()
    remote_tmux_root = qa / "tmux"
    remote_tmux_root.mkdir(mode=0o700)
    host_key = qa / "host_key"
    client_key = qa / "client_key"
    authorized_keys = qa / "authorized_keys"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)],
        check=True,
    )
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(client_key)],
        check=True,
    )
    authorized_keys.write_bytes(client_key.with_suffix(".pub").read_bytes())
    authorized_keys.chmod(0o600)
    port = _free_port()
    username = os.environ.get("USER") or subprocess.check_output(["id", "-un"], text=True).strip()
    config = qa / "sshd_config"
    config.write_text(
        "\n".join(
            [
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
                f"LogLevel {log_level}",
                "Subsystem sftp internal-sftp",
                "",
            ]
        ),
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
                    raise RuntimeError((qa / "sshd.log").read_text(encoding="utf-8")) from exc
                time.sleep(0.05)
        else:
            raise RuntimeError("test sshd did not start")
        yield {
            "port": port,
            "username": username,
            "client_key": client_key,
            "authorized_keys": authorized_keys,
            "log_path": qa / "sshd.log",
        }
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        log.close()


pytestmark = pytest.mark.skipif(
    not all(shutil.which(command) for command in ("ssh", "ssh-keygen", "tmux"))
    or not Path("/usr/sbin/sshd").is_file(),
    reason="OpenSSH server/client and tmux are required",
)


def test_termroom_managed_key_survives_backend_restart(tmp_path: Path) -> None:
    state_dir = tmp_path / "config"
    state_dir.mkdir()
    store = StateStore(state_dir / "termroom.sqlite3")
    store.initialize()

    first = SSHBackend(store, state_dir).ensure_managed_key()
    second = SSHBackend(store, state_dir).ensure_managed_key()

    assert first == second
    private_key = Path(first["private_key"])
    assert private_key.stat().st_mode & 0o077 == 0
    assert private_key.with_suffix(".pub").stat().st_mode & 0o022 == 0


def test_resolve_target_uses_openssh_effective_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = StateStore(state_dir / "termroom.sqlite3")
    store.initialize()
    backend = SSHBackend(store, state_dir)
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                "hostname 10.0.0.8\n"
                "user ubuntu\n"
                "port 2222\n"
                "identitiesonly yes\n"
                "identityfile ~/.ssh/id_gpu\n"
                "identityfile ~/.ssh/id_fallback\n"
                "identityagent /tmp/agent.sock\n"
                "proxyjump bastion-a,bastion-b\n"
            ),
            stderr="",
        )

    monkeypatch.setattr("termroom.ssh_backend.subprocess.run", fake_run)
    target = backend.resolve_target("gpu")

    assert seen == [["ssh", "-G", "-T", "--", "gpu"]]
    assert target["host"] == "10.0.0.8"
    assert target["username"] == "ubuntu"
    assert target["port"] == 2222
    assert target["identity_files"] == (
        str(Path("~/.ssh/id_gpu").expanduser()),
        str(Path("~/.ssh/id_fallback").expanduser()),
    )
    assert target["identities_only"] is True
    assert target["identity_agent"] == "/tmp/agent.sock"
    assert target["proxyjump"] == "bastion-a,bastion-b"


def test_proxy_command_host_key_probe_verifies_final_target(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = StateStore(state_dir / "termroom.sqlite3")
    store.initialize()
    backend = SSHBackend(store, state_dir)

    with _test_sshd(tmp_path) as server:
        direct = backend.probe_host_key("127.0.0.1", int(server["port"]))
        proxied = backend.probe_target_host_key(
            {
                "ssh_alias": "proxied",
                "host": "127.0.0.1",
                "port": int(server["port"]),
                "username": str(server["username"]),
                "proxycommand": "nc %h %p",
                "proxyjump": "",
            }
        )

    assert proxied == direct


def test_ssh_backend_uses_alias_specific_agent_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = StateStore(state_dir / "termroom.sqlite3")
    store.initialize()

    with _test_sshd(tmp_path) as server:
        agent_socket = Path("/tmp") / f"termroom-agent-{uuid.uuid4().hex[:12]}"
        agent = subprocess.Popen(
            ["ssh-agent", "-D", "-a", str(agent_socket)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not agent_socket.exists():
                if agent.poll() is not None:
                    raise RuntimeError("test ssh-agent did not start")
                time.sleep(0.02)
            if not agent_socket.exists():
                raise RuntimeError("test ssh-agent socket was not created")
            agent_env = os.environ.copy()
            agent_env["SSH_AUTH_SOCK"] = str(agent_socket)
            subprocess.run(
                ["ssh-add", str(server["client_key"])],
                check=True,
                capture_output=True,
                text=True,
                env=agent_env,
            )

            backend = SSHBackend(store, state_dir)
            probe = backend.probe_host_key("127.0.0.1", int(server["port"]))
            computer = store.create_computer(
                name="Agent QA",
                ssh_alias="agent-qa",
                host="127.0.0.1",
                port=int(server["port"]),
                username=str(server["username"]),
                identity_file="",
                host_key_type=probe["host_key_type"],
                host_key_data=probe["host_key_data"],
                host_fingerprint=probe["host_fingerprint"],
            )
            monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "wrong-agent"))
            monkeypatch.setattr(
                backend,
                "resolve_target",
                lambda _alias: {
                    "identity_files": (str(server["client_key"]) + ".pub",),
                    "identity_agent": str(agent_socket),
                    "identity_agent_disabled": False,
                    "identities_only": True,
                    "proxycommand": "",
                    "proxyjump": "",
                },
            )

            assert backend.test_connection(computer)["tmux"].startswith("tmux ")
        finally:
            agent.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                agent.wait(timeout=2)
            if agent.poll() is None:
                agent.kill()
                agent.wait(timeout=2)
            with contextlib.suppress(FileNotFoundError):
                agent_socket.unlink()


def test_ssh_backend_reuses_idle_authenticated_transport(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = StateStore(state_dir / "termroom.sqlite3")
    store.initialize()

    with _test_sshd(tmp_path, log_level="INFO") as server:
        backend = SSHBackend(store, state_dir, reuse_connections=True)
        probe = backend.probe_host_key("127.0.0.1", int(server["port"]))
        computer = store.create_computer(
            name="Reuse QA",
            ssh_alias="",
            host="127.0.0.1",
            port=int(server["port"]),
            username=str(server["username"]),
            identity_file=str(server["client_key"]),
            host_key_type=probe["host_key_type"],
            host_key_data=probe["host_key_data"],
            host_fingerprint=probe["host_fingerprint"],
        )
        backend.remember_host_key(computer)

        assert backend.test_connection(computer)["tmux"].startswith("tmux ")
        assert backend.home_directory(computer).startswith("/")
        assert backend.list_browse_directories(computer, str(tmp_path))["current"] == str(tmp_path)

        log_path = Path(server["log_path"])
        auth_marker = "Accepted " + "publickey for"
        deadline = time.monotonic() + 2
        accepted = 0
        while time.monotonic() < deadline:
            accepted = log_path.read_text(encoding="utf-8", errors="replace").count(auth_marker)
            if accepted >= 1:
                break
            time.sleep(0.02)
        assert accepted == 1

        backend.close_connections(str(computer["id"]))
        assert backend.test_connection(computer)["tmux"].startswith("tmux ")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            accepted = log_path.read_text(encoding="utf-8", errors="replace").count(auth_marker)
            if accepted >= 2:
                break
            time.sleep(0.02)
        assert accepted == 2
        backend.close()


def test_password_ssh_attach_uses_encrypted_askpass_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "config"
    state_dir.mkdir()
    store = StateStore(state_dir / "termroom.sqlite3")
    store.initialize()
    backend = SSHBackend(store, state_dir)
    computer = store.create_computer(
        name="Password QA",
        ssh_alias="prod-alias",
        host="example.invalid",
        port=22,
        username="user",
        identity_file="",
        auth_kind="password",
        host_key_type="ssh-ed25519",
        host_key_data="unused",
        host_fingerprint="SHA256:unused",
    )
    backend.save_password(str(computer["id"]), "stored-password")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ssh = fake_bin / "ssh"
    args_log = tmp_path / "ssh-args"
    fake_ssh.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(args_log))}\n"
        "value=\"$($SSH_ASKPASS 'Password:')\"\n"
        "if [ \"$value\" != 'stored-password' ]; then exit 91; fi\n"
        "printf 'ASKPASS_OK\\n'\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    workspace = {
        "id": "workspace",
        "computer": computer,
        "tmux_session": "termroom-test",
        "remote_path": "/tmp",
    }
    terminal = {"tmux_window": "@1"}
    process_pid, master_fd = backend._spawn_ssh_tmux_client(
        workspace,
        terminal,
        tmux_browser_view_session(uuid.uuid4().hex),
    )
    try:
        deadline = time.monotonic() + 2
        output = b""
        while time.monotonic() < deadline:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
            if b"ASKPASS_OK" in output:
                break
        assert b"ASKPASS_OK" in output
        assert b"stored-password" not in output
        args = args_log.read_text(encoding="utf-8").splitlines()
        assert "HostName=example.invalid" in args
        assert "22" in args
        assert "user" in args
        assert args[-2] == "prod-alias"
    finally:
        backend._wait_for_pid(process_pid, 1)
        os.close(master_fd)

    helper = state_dir / "ssh" / "askpass"
    assert helper.is_file()
    assert helper.stat().st_mode & 0o077 == 0
    assert sys.executable in helper.read_text(encoding="utf-8")


def test_ssh_grid_owner_retries_promotion_without_losing_previous_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = StateStore(state_dir / "termroom.sqlite3")
    store.initialize()
    backend = SSHBackend(store, state_dir)
    terminal_id = "terminal"
    previous = backend.control.register(terminal_id)
    candidate = backend.control.register(terminal_id)
    backend.control.mark_input(terminal_id, previous)
    backend._browser_grid_owners[terminal_id] = previous
    backend.control.mark_input(terminal_id, candidate)
    outcomes = iter((False, True))
    attempts: list[bool] = []

    def set_grid_role(
        _workspace: dict[str, object], _view_session: str, *, enabled: bool
    ) -> bool:
        attempts.append(enabled)
        return next(outcomes)

    monkeypatch.setattr(backend, "_set_ssh_browser_view_grid_resize", set_grid_role)
    workspace: dict[str, object] = {}

    assert not backend._sync_ssh_browser_grid_role(
        terminal_id,
        candidate,
        workspace,  # type: ignore[arg-type]
        tmux_browser_view_session(candidate),
        enabled=True,
    )
    assert backend._browser_grid_owners[terminal_id] == previous

    assert backend._sync_ssh_browser_grid_role(
        terminal_id,
        candidate,
        workspace,  # type: ignore[arg-type]
        tmux_browser_view_session(candidate),
        enabled=True,
    )
    assert backend._browser_grid_owners[terminal_id] == candidate
    assert attempts == [True, True]


def test_ssh_grid_owner_is_forgotten_only_after_demotion_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = StateStore(state_dir / "termroom.sqlite3")
    store.initialize()
    backend = SSHBackend(store, state_dir)
    terminal_id = "terminal"
    client_id = backend.control.register(terminal_id)
    backend.control.mark_input(terminal_id, client_id)
    backend._browser_grid_owners[terminal_id] = client_id
    outcomes = iter((False, True))

    def set_grid_role(
        _workspace: dict[str, object], _view_session: str, *, enabled: bool
    ) -> bool:
        assert not enabled
        return next(outcomes)

    monkeypatch.setattr(backend, "_set_ssh_browser_view_grid_resize", set_grid_role)
    workspace: dict[str, object] = {}

    assert not backend._sync_ssh_browser_grid_role(
        terminal_id,
        client_id,
        workspace,  # type: ignore[arg-type]
        tmux_browser_view_session(client_id),
        enabled=False,
    )
    assert backend._browser_grid_owners[terminal_id] == client_id

    assert backend._sync_ssh_browser_grid_role(
        terminal_id,
        client_id,
        workspace,  # type: ignore[arg-type]
        tmux_browser_view_session(client_id),
        enabled=False,
    )
    assert terminal_id not in backend._browser_grid_owners


def test_ssh_grid_promotion_allows_peer_that_disconnected_during_demotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = StateStore(state_dir / "termroom.sqlite3")
    store.initialize()
    backend = SSHBackend(store, state_dir)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "tmux.log"
    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$TERMROOM_TEST_TMUX_LOG\"\n"
        "if [ \"$1\" = 'list-clients' ]; then\n"
        "  if [ \"$2\" = '-t' ]; then printf 'target|@1\\n';\n"
        "  else printf 'peer|termroom-view-peer|@1\\ntarget|termroom-view-target|@1\\n'; fi\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$1\" = 'refresh-client' ] && [ \"$3\" = 'peer' ]; then exit 1; fi\n"
        "if [ \"$1\" = 'display-message' ] && [ \"$4\" = 'peer' ]; then exit 1; fi\n"
        "if [ \"$1\" = 'refresh-client' ] && [ \"$3\" = 'target' ]; then exit 0; fi\n"
        "exit 92\n",
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)
    monkeypatch.setenv("TERMROOM_TEST_TMUX_LOG", str(log_path))
    observed: list[tuple[str, int, str]] = []

    def run_locally(_computer: dict[str, object], command: str) -> str:
        command = command.replace("tmux ", f"{shlex.quote(str(fake_tmux))} ")
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            check=False,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "TERMROOM_TEST_TMUX_LOG": str(log_path),
            },
        )
        observed.append((command, result.returncode, result.stderr))
        if result.returncode:
            raise SSHBackendError(result.stderr or "fake tmux command failed")
        return result.stdout

    monkeypatch.setattr(backend, "_exec", run_locally)
    workspace = {"computer": {"id": "computer"}}

    promoted = backend._set_ssh_browser_view_grid_resize(
        workspace,  # type: ignore[arg-type]
        tmux_browser_view_session(uuid.uuid4().hex),
        enabled=True,
    )
    assert promoted, observed
    calls = log_path.read_text(encoding="utf-8").splitlines()
    assert "refresh-client -t peer -f ignore-size" in calls
    assert "display-message -p -c peer #{window_id}" in calls
    assert "refresh-client -t target -f !ignore-size" in calls


@pytest.mark.asyncio
async def test_ssh_bridge_cleans_control_registration_when_tmux_spawn_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = StateStore(state_dir / "termroom.sqlite3")
    store.initialize()
    backend = SSHBackend(store, state_dir)
    terminal_id = "terminal"
    monkeypatch.setattr(backend, "ensure_workspace", lambda _workspace: [])

    def fail_spawn(
        _workspace: dict[str, object],
        _terminal: dict[str, object],
        view_session: str,
    ) -> tuple[int, int]:
        client_id = view_session.removeprefix("termroom-view-")
        backend._browser_grid_owners[terminal_id] = client_id
        raise SSHBackendError("tmux spawn failed")

    monkeypatch.setattr(backend, "_spawn_ssh_tmux_client", fail_spawn)

    with pytest.raises(SSHBackendError, match="tmux spawn failed"):
        await backend.bridge(  # type: ignore[arg-type]
            object(),
            {"id": "workspace"},
            {"id": terminal_id},
            device_id="device",
        )

    assert backend.control.client_count(terminal_id) == 0
    assert terminal_id not in backend._browser_grid_owners


@pytest.mark.asyncio
async def test_password_setup_failure_never_echoes_or_stores_password(tmp_path: Path) -> None:
    local_root = tmp_path / "local"
    local_root.mkdir()
    state_dir = tmp_path / "state"

    with _test_sshd(tmp_path) as server:
        settings = Settings.create(
            local_root,
            state_dir=state_dir,
            access_token="internal-secret",
            login_password="termroom-password",
        )
        app = create_app(settings)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post(
                "/login", data={"password": "termroom-password"}, follow_redirects=False
            )
            assert login.status_code == 303
            probe = await client.post(
                "/computers/probe",
                data={
                    "_csrf": settings.csrf_token,
                    "target": "127.0.0.1",
                    "username": str(server["username"]),
                    "port": str(server["port"]),
                    "auth_mode": "password",
                },
            )
            assert probe.status_code == 200
            host_key = probe.json()
            secret = "super-secret-password"
            failed = await client.post(
                "/computers",
                data={
                    "_csrf": settings.csrf_token,
                    "target": "127.0.0.1",
                    "username": str(server["username"]),
                    "port": str(server["port"]),
                    "auth_mode": "password",
                    "password": secret,
                    "host_key_type": host_key["host_key_type"],
                    "host_key_data": host_key["host_key_data"],
                    "host_fingerprint": host_key["host_fingerprint"],
                    "confirm_fingerprint": "1",
                },
            )
            assert failed.status_code == 400
            assert "SSH password authentication failed." in failed.text
            assert secret not in failed.text
            assert app.state.store.list_computers() == []
            with app.state.store.connect() as db:
                columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(computers)")}
                assert "password" not in columns


@pytest.mark.asyncio
async def test_password_setup_route_persists_encrypted_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_root = tmp_path / "local"
    local_root.mkdir()
    state_dir = tmp_path / "config"
    settings = Settings.create(
        local_root,
        state_dir=state_dir,
        access_token="internal-secret",
        login_password="termroom-password",
    )
    app = create_app(settings)
    host_key = {
        "host_key_type": "ssh-ed25519",
        "host_key_data": "AAAATEST",
        "host_fingerprint": "SHA256:test",
    }
    monkeypatch.setattr(app.state.ssh, "probe_target_host_key", lambda target: host_key)
    seen_passwords: list[str] = []

    def fake_password_test(computer: dict[str, object], password: str) -> dict[str, str]:
        assert computer["host"] == "example.test"
        seen_passwords.append(password)
        return {"shell": "/bin/sh", "tmux": "tmux 3.5"}

    monkeypatch.setattr(app.state.ssh, "test_password_connection", fake_password_test)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    secret = "remote-password-123"
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/login", data={"password": "termroom-password"}, follow_redirects=False
        )
        assert login.status_code == 303
        created = await client.post(
            "/computers",
            data={
                "_csrf": settings.csrf_token,
                "target": "example.test",
                "username": "deploy",
                "port": "22",
                "auth_mode": "password",
                "password": secret,
                "host_key_type": host_key["host_key_type"],
                "host_key_data": host_key["host_key_data"],
                "host_fingerprint": host_key["host_fingerprint"],
                "confirm_fingerprint": "1",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303

    computers = app.state.store.list_computers()
    assert len(computers) == 1
    computer = computers[0]
    assert computer["auth_kind"] == "password"
    assert computer["ssh_alias"] == "example.test"
    assert computer["identity_file"] == ""
    assert seen_passwords == [secret]
    encrypted = state_dir / "credentials" / str(computer["id"])
    assert encrypted.is_file()
    assert secret.encode() not in encrypted.read_bytes()

    reopened = SSHBackend(app.state.store, state_dir)
    assert reopened._stored_password(computer) == secret


@pytest.mark.asyncio
async def test_public_key_setup_route_uses_persistent_termroom_key(tmp_path: Path) -> None:
    local_root = tmp_path / "local"
    local_root.mkdir()
    state_dir = tmp_path / "state"

    with _test_sshd(tmp_path) as server:
        settings = Settings.create(
            local_root,
            state_dir=state_dir,
            access_token="internal-secret",
            login_password="termroom-password",
        )
        app = create_app(settings)
        managed = app.state.ssh.ensure_managed_key()
        authorized_keys = Path(server["authorized_keys"])
        with authorized_keys.open("a", encoding="utf-8") as handle:
            handle.write(managed["public_key"] + "\n")

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post(
                "/login", data={"password": "termroom-password"}, follow_redirects=False
            )
            assert login.status_code == 303

            probe = await client.post(
                "/computers/probe",
                data={
                    "_csrf": settings.csrf_token,
                    "target": "127.0.0.1",
                    "username": str(server["username"]),
                    "port": str(server["port"]),
                    "auth_mode": "key",
                },
            )
            assert probe.status_code == 200
            host_key = probe.json()
            assert host_key["ok"] is True

            created = await client.post(
                "/computers",
                data={
                    "_csrf": settings.csrf_token,
                    "target": "127.0.0.1",
                    "name": "Managed QA",
                    "username": str(server["username"]),
                    "port": str(server["port"]),
                    "auth_mode": "key",
                    "host_key_type": host_key["host_key_type"],
                    "host_key_data": host_key["host_key_data"],
                    "host_fingerprint": host_key["host_fingerprint"],
                    "confirm_fingerprint": "1",
                },
                follow_redirects=False,
            )
            assert created.status_code == 303
            assert created.headers["location"].startswith("/open/")
            picker = await client.get(created.headers["location"])
            assert picker.status_code == 200
            assert "Managed QA" in picker.text
            assert "SSH authentication and tmux are working." in picker.text
            computers = app.state.store.list_computers()
            assert len(computers) == 1
            assert computers[0]["auth_kind"] == "key"
            assert computers[0]["identity_file"] == managed["private_key"]
            detail = await client.get(f"/computers/{computers[0]['id']}")
            assert detail.status_code == 200
            assert "Termroom public key" in detail.text


@pytest.mark.asyncio
async def test_ssh_backend_remote_tmux_sftp_and_resize(tmp_path: Path) -> None:
    project = tmp_path / "remote-project"
    project.mkdir()
    (project / "readme.txt").write_text("hello remote\n", encoding="utf-8")
    (project / "large.log").write_text(
        "".join(f"remote-line-{index:05d}\n" for index in range(4000)),
        encoding="utf-8",
    )
    (project / "ignored.tmp").write_text("ignore", encoding="utf-8")
    (project / ".termroomignore").write_text("*.tmp\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("do not expose", encoding="utf-8")
    (project / "escape").symlink_to(outside, target_is_directory=True)
    local_root = tmp_path / "local"
    local_root.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    with _test_sshd(tmp_path) as server:
        store = StateStore(state_dir / "termroom.sqlite3")
        store.initialize()
        backend = SSHBackend(store, state_dir)
        probe = backend.probe_host_key("127.0.0.1", int(server["port"]))
        computer = store.create_computer(
            name="Loopback QA",
            ssh_alias="",
            host="127.0.0.1",
            port=int(server["port"]),
            username=str(server["username"]),
            identity_file=str(server["client_key"]),
            host_key_type=probe["host_key_type"],
            host_key_data=probe["host_key_data"],
            host_fingerprint=probe["host_fingerprint"],
        )
        backend.remember_host_key(computer)
        assert backend.test_connection(computer)["tmux"].startswith("tmux ")
        picker = backend.list_browse_directories(computer, str(tmp_path))
        assert picker["current"] == str(tmp_path)
        assert "remote-project" in {item["name"] for item in picker["entries"]}
        project_picker = backend.list_browse_directories(computer, str(project))
        assert "escape" not in {item["name"] for item in project_picker["entries"]}
        canonical = backend.validate_workspace_path(computer, str(project))
        manager = WorkspaceManager(RootManager(local_root), store)
        workspace = manager.open_remote(computer["id"], canonical, "remote-qa")
        terminal = backend.ensure_workspace(workspace)[0]
        remote_home = backend.home_directory(computer)
        server_workspace = manager.open_server_terminal(computer["id"], remote_home)
        server_terminal = backend.ensure_workspace(server_workspace)[0]

        try:
            terminal = backend.rename_terminal(workspace, terminal, "worker one")
            assert terminal["name"] == "worker-one"
            extra_terminal = backend.create_terminal(workspace, "logs")
            remaining_terminals = backend.close_terminal(workspace, extra_terminal)
            assert [item["id"] for item in remaining_terminals] == [terminal["id"]]

            backend._exec(
                computer,
                "tmux send-keys -t "
                f"{shlex.quote(str(terminal['tmux_window']))} {shlex.quote('sleep 30')} Enter",
            )
            deadline = time.monotonic() + 2
            while True:
                workspace_usage = backend.workspace_usage(workspace)
                if workspace_usage.process_count >= 2 or time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            assert workspace_usage.process_count >= 2
            assert workspace_usage.memory_bytes > 0
            assert workspace_usage.cpu_percent >= 0

            assert backend.read_text(workspace, "readme.txt", 1024).content == "hello remote\n"
            remote_range = backend.read_text_preview(
                workspace,
                "large.log",
                mode="range",
                offset=16_000,
                max_bytes=4096,
            )
            assert remote_range.offset == 16_000
            assert remote_range.bytes_read <= 4096
            assert "remote-line-" in remote_range.content
            with pytest.raises((SSHBackendError, UnsupportedFileError)):
                backend.read_text(workspace, "escape/secret.txt", 1024)
            backend.create(workspace, ".", "empty.txt", directory=False)
            assert backend.stat(workspace, "empty.txt").size == 0
            backend.create(workspace, ".", "not-empty", directory=True)
            backend.create(workspace, "not-empty", "child.txt", directory=False)
            with pytest.raises(OSError) as nonempty_error:
                backend.delete(workspace, "not-empty")
            assert nonempty_error.value.errno == errno.ENOTEMPTY
            backend.delete(workspace, "not-empty/child.txt")
            backend.delete(workspace, "not-empty")
            backend.create(workspace, ".", "empty-no-read", directory=True)
            unreadable_empty = project / "empty-no-read"
            unreadable_empty.chmod(0)
            try:
                backend.delete(workspace, "empty-no-read")
            finally:
                if unreadable_empty.exists():
                    unreadable_empty.chmod(0o700)
            assert not unreadable_empty.exists()

            upload = UploadFile(file=io.BytesIO(b"a,b\n1,2\n"), filename="result.csv")
            await backend.upload(
                workspace,
                ".",
                upload,
                overwrite=False,
                max_bytes=1024 * 1024,
            )
            await upload.close()
            assert b"".join(backend.download_iter(workspace, "result.csv")) == b"a,b\n1,2\n"

            async def streamed_chunks():  # type: ignore[no-untyped-def]
                yield b"streamed "
                yield b"over ssh\n"

            await backend.upload_stream(
                workspace,
                ".",
                "streamed.txt",
                streamed_chunks(),
                overwrite=False,
                max_bytes=1024 * 1024,
            )
            assert b"".join(backend.download_iter(workspace, "streamed.txt")) == (
                b"streamed over ssh\n"
            )

            async def racing_chunks():  # type: ignore[no-untyped-def]
                yield b"first chunk\n"
                (project / "raced.txt").write_text("created elsewhere\n", encoding="utf-8")
                yield b"second chunk\n"

            with pytest.raises(FileExistsError):
                await backend.upload_stream(
                    workspace,
                    ".",
                    "raced.txt",
                    racing_chunks(),
                    overwrite=False,
                    max_bytes=1024 * 1024,
                )
            assert (project / "raced.txt").read_text(encoding="utf-8") == "created elsewhere\n"
            unreadable = project / "unreadable"
            unreadable.mkdir()
            (unreadable / "private.txt").write_text("private\n", encoding="utf-8")
            unreadable.chmod(0)
            try:
                recent_paths = [
                    entry.relative_path for entry in backend.recent_files(workspace).entries
                ]
            finally:
                unreadable.chmod(0o700)
            assert "result.csv" in recent_paths
            assert "ignored.tmp" not in recent_paths
            assert "unreadable/private.txt" not in recent_paths

            browser_terminal = backend.create_terminal(workspace, "browser-view")
            backend._exec(
                computer,
                "tmux send-keys -t "
                f"{shlex.quote(str(browser_terminal['tmux_window']))} "
                + shlex.quote(
                    "printf 'SSH_HISTORY_OLD\\n'; "
                    "seq -f 'SSH_HISTORY_%02g' 1 80; "
                    "printf 'SSH_HISTORY_LIVE\\n'"
                )
                + " Enter",
            )
            deadline = time.monotonic() + 2
            full_scrollback = ""
            while time.monotonic() < deadline:
                full_scrollback = backend.capture_scrollback(workspace, browser_terminal)
                if "SSH_HISTORY_LIVE" in full_scrollback:
                    break
                time.sleep(0.05)
            history_scrollback = backend.capture_scrollback(
                workspace,
                browser_terminal,
                history_only=True,
            )
            assert "SSH_HISTORY_OLD" in history_scrollback
            assert "SSH_HISTORY_LIVE" in full_scrollback
            assert "SSH_HISTORY_LIVE" not in history_scrollback

            view_session = tmux_browser_view_session(uuid.uuid4().hex)
            process_pid, master_fd = backend._spawn_ssh_tmux_client(
                workspace,
                browser_terminal,
                view_session,
            )
            try:
                time.sleep(0.4)
                assert backend._exec(
                    computer,
                    f"tmux display-message -p -t {shlex.quote(str(workspace['tmux_session']))} "
                    "'#{window_id}'",
                ).strip() == str(terminal["tmux_window"])
                assert backend._exec(
                    computer,
                    f"tmux display-message -p -t {shlex.quote(view_session)} '#{{window_id}}'",
                ).strip() == str(browser_terminal["tmux_window"])
                assert backend._set_ssh_browser_view_grid_resize(
                    workspace, view_session, enabled=True
                )
                backend._set_window_size(master_fd, rows=41, cols=123)
                os.killpg(process_pid, signal.SIGWINCH)
                deadline = time.monotonic() + 2
                sizes: list[str] = []
                while time.monotonic() < deadline:
                    output = backend._exec(
                        computer,
                        f"tmux list-clients -t {shlex.quote(view_session)} "
                        "-F '#{client_width}x#{client_height}'",
                    )
                    sizes = output.strip().splitlines()
                    if "123x41" in sizes:
                        break
                    time.sleep(0.05)
                assert "123x41" in sizes

                passive_view = tmux_browser_view_session(uuid.uuid4().hex)
                passive_pid, passive_fd = backend._spawn_ssh_tmux_client(
                    workspace,
                    browser_terminal,
                    passive_view,
                )
                try:
                    backend._set_window_size(passive_fd, rows=28, cols=51)
                    os.killpg(passive_pid, signal.SIGWINCH)
                    deadline = time.monotonic() + 2
                    passive_sizes: list[str] = []
                    while time.monotonic() < deadline:
                        try:
                            output = backend._exec(
                                computer,
                                f"tmux list-clients -t {shlex.quote(passive_view)} "
                                "-F '#{client_width}x#{client_height}'",
                            )
                        except SSHBackendError:
                            time.sleep(0.05)
                            continue
                        passive_sizes = output.strip().splitlines()
                        if "51x28" in passive_sizes:
                            break
                        time.sleep(0.05)
                    assert "51x28" in passive_sizes
                    assert (
                        backend._exec(
                            computer,
                            "tmux display-message -p -t "
                            f"{shlex.quote(str(browser_terminal['tmux_window']))} "
                            "'#{window_width}x#{window_height}'",
                        ).strip()
                        == "123x40"
                    )

                    assert backend._set_ssh_browser_view_grid_resize(
                        workspace, passive_view, enabled=True
                    )
                    backend._set_window_size(passive_fd, rows=28, cols=51)
                    os.killpg(passive_pid, signal.SIGWINCH)
                    deadline = time.monotonic() + 2
                    window_size = ""
                    while time.monotonic() < deadline:
                        window_size = backend._exec(
                            computer,
                            "tmux display-message -p -t "
                            f"{shlex.quote(str(browser_terminal['tmux_window']))} "
                            "'#{window_width}x#{window_height}'",
                        ).strip()
                        if window_size == "51x27":
                            break
                        time.sleep(0.05)
                    assert window_size == "51x27"

                    assert backend._set_ssh_browser_view_grid_resize(
                        workspace, view_session, enabled=False
                    )
                    backend._set_window_size(master_fd, rows=29, cols=77)
                    os.killpg(process_pid, signal.SIGWINCH)
                    deadline = time.monotonic() + 2
                    active_sizes: list[str] = []
                    while time.monotonic() < deadline:
                        output = backend._exec(
                            computer,
                            f"tmux list-clients -t {shlex.quote(view_session)} "
                            "-F '#{client_width}x#{client_height}'",
                        )
                        active_sizes = output.strip().splitlines()
                        if "77x29" in active_sizes:
                            break
                        time.sleep(0.05)
                    assert "77x29" in active_sizes
                    assert (
                        backend._exec(
                            computer,
                            "tmux display-message -p -t "
                            f"{shlex.quote(str(browser_terminal['tmux_window']))} "
                            "'#{window_width}x#{window_height}'",
                        ).strip()
                        == "51x27"
                    )
                finally:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(passive_pid, signal.SIGTERM)
                    backend._wait_for_pid(passive_pid, 1)
                    os.close(passive_fd)
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process_pid, signal.SIGTERM)
                backend._wait_for_pid(process_pid, 1)
                os.close(master_fd)
                backend.close_terminal(workspace, browser_terminal)

            applied_input_sizes: list[tuple[int, int]] = []
            original_set_window_size = backend._set_window_size
            original_set_grid_resize = backend._set_ssh_browser_view_grid_resize
            grid_resize_attempts: list[bool] = []
            terminal_id = str(terminal["id"])
            competing_client = backend.control.register(terminal_id)
            backend.control.mark_input(terminal_id, competing_client)

            def record_input_size(fd: int, *, rows: int, cols: int) -> None:
                applied_input_sizes.append((rows, cols))
                original_set_window_size(fd, rows=rows, cols=cols)

            def retry_grid_resize(
                resize_workspace: dict[str, object],
                resize_view: str,
                *,
                enabled: bool,
            ) -> bool:
                grid_resize_attempts.append(enabled)
                if len(grid_resize_attempts) == 1:
                    return False
                changed = original_set_grid_resize(
                    resize_workspace,  # type: ignore[arg-type]
                    resize_view,
                    enabled=enabled,
                )
                if len(grid_resize_attempts) == 2:
                    backend.control.mark_input(terminal_id, competing_client)
                return changed

            class InputWebSocket:
                def __init__(self) -> None:
                    self.messages = [
                        {
                            "type": "websocket.receive",
                            "text": json.dumps(
                                {
                                    "kind": "input",
                                    "data": "",
                                    "rows": 37,
                                    "cols": 111,
                                    "user_input": True,
                                }
                            ),
                        },
                        {
                            "type": "websocket.receive",
                            "text": json.dumps(
                                {
                                    "kind": "input",
                                    "data": "",
                                    "rows": 37,
                                    "cols": 111,
                                    "user_input": True,
                                }
                            ),
                        },
                        {
                            "type": "websocket.receive",
                            "text": json.dumps({"kind": "input", "data": ""}),
                        },
                        {"type": "websocket.disconnect", "code": 1000},
                    ]

                async def receive(self) -> dict[str, object]:
                    return self.messages.pop(0)

                async def send_text(self, _value: str) -> None:
                    return None

                async def close(self, *, code: int, reason: str) -> None:
                    raise AssertionError((code, reason))

            backend._set_window_size = record_input_size  # type: ignore[method-assign]
            backend._set_ssh_browser_view_grid_resize = retry_grid_resize  # type: ignore[method-assign]
            try:
                await backend.bridge(
                    InputWebSocket(),  # type: ignore[arg-type]
                    workspace,
                    terminal,
                    device_id="ssh-device",
                )
            finally:
                backend._set_window_size = original_set_window_size  # type: ignore[method-assign]
                backend._set_ssh_browser_view_grid_resize = original_set_grid_resize  # type: ignore[method-assign]
                backend.control.unregister(terminal_id, competing_client)
            assert grid_resize_attempts == [True, True, False]
            assert applied_input_sizes == [(37, 111)]

            settings = Settings.create(
                local_root,
                state_dir=state_dir,
                access_token="test-token",
            )
            app = create_app(settings)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                login = await client.post(
                    "/login",
                    data={"password": "test-token"},
                    follow_redirects=False,
                )
                assert login.status_code == 303
                home = await client.get("/")
                assert home.status_code == 200
                assert "Loopback QA" in home.text

                files_page = await client.get(f"/w/{workspace['id']}/files")
                assert files_page.status_code == 200
                assert "result.csv" in files_page.text

                view = await client.get(f"/w/{workspace['id']}/view/result.csv")
                assert view.status_code == 200
                assert "CSV preview" in view.text

                download = await client.get(f"/w/{workspace['id']}/download/result.csv")
                assert download.status_code == 200
                assert download.content == b"a,b\n1,2\n"

                ranged = await client.get(
                    f"/w/{workspace['id']}/download/result.csv",
                    headers={"Range": "bytes=2-5"},
                )
                assert ranged.status_code == 206
                assert ranged.headers["accept-ranges"] == "bytes"
                assert ranged.headers["content-range"] == "bytes 2-5/8"
                assert ranged.headers["content-length"] == "4"
                assert ranged.content == b"b\n1,"

                suffix = await client.get(
                    f"/w/{workspace['id']}/download/result.csv",
                    headers={"Range": "bytes=-2"},
                )
                assert suffix.status_code == 206
                assert suffix.content == b"2\n"

                invalid_range = await client.get(
                    f"/w/{workspace['id']}/download/result.csv",
                    headers={"Range": "bytes=999-1000"},
                )
                assert invalid_range.status_code == 416
                assert invalid_range.headers["content-range"] == "bytes */8"

                recent = await client.get(f"/w/{workspace['id']}/recent")
                assert recent.status_code == 200
                assert "result.csv" in recent.text

                terminal_page = await client.get(f"/w/{workspace['id']}/terminal")
                assert terminal_page.status_code == 200
                assert "Loopback QA" in terminal_page.text
                assert f"/api/workspaces/{workspace['id']}/usage" in terminal_page.text

                usage_response = await client.get(f"/api/workspaces/{workspace['id']}/usage")
                assert usage_response.status_code == 200
                assert usage_response.json()["state"] == "fresh"
                assert usage_response.json()["sample"]["process_count"] >= 2

                server_open = await client.post(
                    f"/computers/{computer['id']}/server-terminal",
                    data={"_csrf": settings.csrf_token},
                    follow_redirects=False,
                )
                assert server_open.status_code == 303
                assert server_open.headers["location"] == (f"/w/{server_workspace['id']}/terminal")
                server_page = await client.get(server_open.headers["location"])
                assert server_page.status_code == 200
                assert "SSH server terminal" in server_page.text
                assert remote_home in server_page.text
                assert server_terminal["id"] in server_page.text
                assert f"/w/{server_workspace['id']}/files" not in server_page.text
                assert f"/api/workspaces/{server_workspace['id']}/usage" not in server_page.text

                picker_page = await client.get(f"/open/{computer['id']}")
                assert picker_page.status_code == 200
                assert f"/w/{server_workspace['id']}" not in picker_page.text
        finally:
            with contextlib.suppress(Exception):
                backend._exec(computer, f"tmux kill-session -t {workspace['tmux_session']}")
            with contextlib.suppress(Exception):
                backend._exec(
                    computer,
                    f"tmux kill-session -t {server_workspace['tmux_session']}",
                )


def test_ssh_file_run_end_to_end_recovers_reuses_and_stops_exact_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "remote-file-run"
    project.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')\n", encoding="utf-8")
    (project / "escape.py").symlink_to(outside)
    local_root = tmp_path / "local"
    local_root.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    with _test_sshd(tmp_path) as server:
        store = StateStore(state_dir / "termroom.sqlite3")
        store.initialize()
        backend = SSHBackend(store, state_dir)
        probe = backend.probe_host_key("127.0.0.1", int(server["port"]))
        computer = store.create_computer(
            name="File Run QA",
            ssh_alias="",
            host="127.0.0.1",
            port=int(server["port"]),
            username=str(server["username"]),
            identity_file=str(server["client_key"]),
            host_key_type=probe["host_key_type"],
            host_key_data=probe["host_key_data"],
            host_fingerprint=probe["host_fingerprint"],
        )
        backend.remember_host_key(computer)
        workspaces = WorkspaceManager(RootManager(local_root), store)
        workspace = workspaces.open_remote(
            str(computer["id"]),
            backend.validate_workspace_path(computer, str(project)),
            "file-run-qa",
        )
        manager = FileRunManager(
            store,
            workspaces,
            FileService(),
            TerminalManager(store),
            backend,
            state_dir=state_dir,
            max_edit_bytes=1024 * 1024,
        )
        remote_home = backend.home_directory(computer)
        metadata_workspace = Path(remote_home) / ".termroom-file-runs" / str(workspace["id"])

        def wait_for(run_id: str, *, terminal: bool) -> dict[str, object]:
            deadline = time.monotonic() + 8
            latest: dict[str, object] = {}
            while time.monotonic() < deadline:
                latest = manager.reconcile(run_id)
                if bool(latest.get("state") in FILE_RUN_TERMINAL_STATES) == terminal:
                    return latest
                time.sleep(0.05)
            raise AssertionError(f"SSH File Run did not settle: {latest}")

        try:
            (project / "ask.py").write_text(
                "value = input('remote value: ')\nprint('seen:' + value)\n",
                encoding="utf-8",
            )
            digest = backend.inspect_runnable(workspace, "ask.py", max_bytes=1024 * 1024).digest
            interactive = manager.start(
                workspace,
                "ask.py",
                expected_digest=digest,
                idempotency_key=str(uuid.uuid4()),
            )
            wait_for(str(interactive["id"]), terminal=False)
            terminal = store.get_managed_terminal(str(workspace["id"]), "file_run")
            assert terminal is not None
            first_terminal_id = str(terminal["id"])
            backend._exec(
                computer,
                "tmux send-keys -t "
                f"{shlex.quote(str(terminal['tmux_window']))} "
                f"{shlex.quote('SSH 한글 value')} Enter",
            )
            completed = wait_for(str(interactive["id"]), terminal=True)
            assert completed["state"] == "finished"
            assert completed["exit_code"] == 0
            assert "seen:SSH 한글 value" in backend.capture_scrollback(workspace, terminal)

            special_path = "한글 $(touch PWNED); value.py"
            (project / special_path).write_text("print('safe remote path')\n", encoding="utf-8")
            special_digest = backend.inspect_runnable(
                workspace, special_path, max_bytes=1024 * 1024
            ).digest
            special = manager.start(
                workspace,
                special_path,
                expected_digest=special_digest,
                idempotency_key=str(uuid.uuid4()),
            )
            special_done = wait_for(str(special["id"]), terminal=True)
            assert special_done["state"] == "finished"
            assert special_done["exit_code"] == 0
            assert not (project / "PWNED").exists()
            reused = store.get_managed_terminal(str(workspace["id"]), "file_run")
            assert reused is not None
            assert reused["id"] == first_terminal_id

            missing_interpreter = project / "missing-interpreter"
            missing_interpreter.write_text(
                "#!/termroom-interpreter-that-does-not-exist\nprintf 'never\\n'\n",
                encoding="utf-8",
            )
            missing_interpreter.chmod(0o700)
            missing_digest = backend.inspect_runnable(
                workspace, missing_interpreter.name, max_bytes=1024 * 1024
            ).digest
            missing_run = manager.start(
                workspace,
                missing_interpreter.name,
                expected_digest=missing_digest,
                idempotency_key=str(uuid.uuid4()),
            )
            missing_failed = wait_for(str(missing_run["id"]), terminal=True)
            assert missing_failed["state"] == "failed"
            assert missing_failed["error_code"] == "direct_runner_failed"
            assert missing_failed["exit_code"] is None

            (project / "restart.py").write_text(
                "import time\ntime.sleep(2)\nprint('reconciled remotely')\n",
                encoding="utf-8",
            )
            restart_digest = backend.inspect_runnable(
                workspace, "restart.py", max_bytes=1024 * 1024
            ).digest
            restarting = manager.start(
                workspace,
                "restart.py",
                expected_digest=restart_digest,
                idempotency_key=str(uuid.uuid4()),
            )
            wait_for(str(restarting["id"]), terminal=False)
            restarted_backend = SSHBackend(store, state_dir)
            restarted = FileRunManager(
                store,
                workspaces,
                FileService(),
                TerminalManager(store),
                restarted_backend,
                state_dir=state_dir,
                max_edit_bytes=1024 * 1024,
            )
            time.sleep(2.2)
            recovered = restarted.reconcile(str(restarting["id"]))
            assert recovered["state"] == "finished"
            assert recovered["exit_code"] == 0

            (project / "wait.py").write_text(
                "import signal, time\n"
                "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
                "while True: time.sleep(0.1)\n",
                encoding="utf-8",
            )
            wait_digest = backend.inspect_runnable(
                workspace, "wait.py", max_bytes=1024 * 1024
            ).digest
            waiting = manager.start(
                workspace,
                "wait.py",
                expected_digest=wait_digest,
                idempotency_key=str(uuid.uuid4()),
            )
            wait_for(str(waiting["id"]), terminal=False)
            waiting_terminal = store.get_managed_terminal(str(workspace["id"]), "file_run")
            assert waiting_terminal is not None
            waiting_window = str(waiting_terminal["tmux_window"])
            original_windows = backend._remote_file_run_windows

            def drift_identity(client, selected_workspace):  # type: ignore[no-untyped-def]
                windows = original_windows(client, selected_workspace)
                backend._exec_client(
                    client,
                    "tmux set-window-option -t "
                    f"{shlex.quote(waiting_window)} @termroom_managed_run_id other-run",
                )
                return windows

            monkeypatch.setattr(backend, "_remote_file_run_windows", drift_identity)
            assert backend.interrupt_file_run(workspace, run_id=str(waiting["id"])) is False
            monkeypatch.setattr(backend, "_remote_file_run_windows", original_windows)
            backend._exec(
                computer,
                "tmux set-window-option -t "
                f"{shlex.quote(waiting_window)} @termroom_managed_run_id "
                f"{shlex.quote(str(waiting['id']))}",
            )
            pane_dead = backend._exec(
                computer,
                f"tmux display-message -p -t {shlex.quote(waiting_window)} '#{{pane_dead}}'",
            ).strip()
            assert pane_dead == "0"
            interrupted = manager.stop(str(waiting["id"]))
            assert interrupted["needs_force"] is True
            killed = manager.kill(str(waiting["id"]))
            assert killed["state"] == "stopped"
            assert backend.session_exists(workspace)

            with pytest.raises(UnsupportedFileError):
                manager.start(
                    workspace,
                    "escape.py",
                    expected_digest="0" * 64,
                    idempotency_key=str(uuid.uuid4()),
                )
            assert store.get_active_file_run(str(workspace["id"])) is None
        finally:
            with contextlib.suppress(Exception):
                backend._exec(
                    computer,
                    f"tmux kill-session -t {shlex.quote(str(workspace['tmux_session']))}",
                )
            shutil.rmtree(metadata_workspace, ignore_errors=True)


def test_ssh_backend_rejects_changed_host_key(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    with _test_sshd(tmp_path) as server:
        store = StateStore(state_dir / "state.sqlite3")
        store.initialize()
        backend = SSHBackend(store, state_dir)
        probe = backend.probe_host_key("127.0.0.1", int(server["port"]))
        wrong_key_data = probe["host_key_data"][:-1] + (
            "A" if probe["host_key_data"][-1] != "A" else "B"
        )
        computer = store.create_computer(
            name="Wrong key",
            ssh_alias="",
            host="127.0.0.1",
            port=int(server["port"]),
            username=str(server["username"]),
            identity_file=str(server["client_key"]),
            host_key_type=probe["host_key_type"],
            host_key_data=wrong_key_data,
            host_fingerprint=probe["host_fingerprint"],
        )

        with pytest.raises(SSHHostKeyChanged):
            backend.test_connection(computer)


@pytest.mark.asyncio
async def test_ssh_new_project_end_to_end(tmp_path: Path) -> None:
    local_root = tmp_path / "local"
    local_root.mkdir()
    remote_parent = tmp_path / "remote-parent"
    remote_parent.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    with _test_sshd(tmp_path) as server:
        store = StateStore(state_dir / "termroom.sqlite3")
        store.initialize()
        backend = SSHBackend(store, state_dir)
        probe = backend.probe_host_key("127.0.0.1", int(server["port"]))
        computer = store.create_computer(
            name="GPU QA",
            ssh_alias="",
            host="127.0.0.1",
            port=int(server["port"]),
            username=str(server["username"]),
            identity_file=str(server["client_key"]),
            host_key_type=probe["host_key_type"],
            host_key_data=probe["host_key_data"],
            host_fingerprint=probe["host_fingerprint"],
        )
        backend.remember_host_key(computer)

        canonical = backend.create_project_directory(
            computer, str(remote_parent), "한글 remote project"
        )
        created = Path(canonical)
        assert created.is_dir()
        assert created.name == "한글 remote project"

        with pytest.raises(ProjectPathExists) as folder_conflict:
            backend.create_project_directory(computer, str(remote_parent), created.name)
        assert folder_conflict.value.is_directory is True

        (remote_parent / "taken.txt").write_text("file", encoding="utf-8")
        with pytest.raises(ProjectPathExists) as file_conflict:
            backend.create_project_directory(computer, str(remote_parent), "taken.txt")
        assert file_conflict.value.is_directory is False

        workspaces = WorkspaceManager(RootManager(local_root), store)
        workspace = workspaces.open_remote(str(computer["id"]), canonical)
        terminals = backend.ensure_workspace(workspace)
        assert terminals
        try:
            assert backend.session_exists(workspace)
        finally:
            with contextlib.suppress(Exception):
                backend._exec(
                    computer,
                    f"tmux kill-session -t {workspace['tmux_session']}",
                )


@pytest.mark.asyncio
async def test_remote_model_upgrade_preserves_live_ssh_tmux_and_files(
    tmp_path: Path,
) -> None:
    project = tmp_path / "upgrade-project"
    project.mkdir()
    (project / "upgrade.txt").write_text("preserved through upgrade\n", encoding="utf-8")
    local_root = tmp_path / "local"
    local_root.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    with _test_sshd(tmp_path) as server:
        settings = Settings.create(
            local_root,
            state_dir=state_dir,
            access_token="test-token",
            default_locale="ko",
        )
        store = StateStore(state_dir / "termroom.sqlite3")
        store.initialize()
        backend = SSHBackend(store, state_dir)
        probe = backend.probe_host_key("127.0.0.1", int(server["port"]))
        computer = store.create_computer(
            name="Upgrade QA",
            ssh_alias="",
            host="127.0.0.1",
            port=int(server["port"]),
            username=str(server["username"]),
            identity_file=str(server["client_key"]),
            host_key_type=probe["host_key_type"],
            host_key_data=probe["host_key_data"],
            host_fingerprint=probe["host_fingerprint"],
        )
        backend.remember_host_key(computer)
        manager = WorkspaceManager(RootManager(local_root), store)
        workspace = manager.open_remote(str(computer["id"]), str(project), "Upgrade project")
        terminal = backend.ensure_workspace(workspace)[0]
        before_panes = backend._exec(
            computer,
            f"tmux list-panes -t {workspace['tmux_session']} -F '#{{pane_id}}:#{{pane_pid}}'",
        ).strip()

        with sqlite3.connect(store.path) as legacy:
            legacy.execute("ALTER TABLE computers RENAME COLUMN connection_method TO kind")
            legacy.execute(
                "ALTER TABLE remote_runs RENAME COLUMN archive_format TO legacy_archive_format"
            )
            legacy.execute(
                "UPDATE workspaces SET backend_kind = 'ssh' WHERE backend_kind = 'remote'"
            )

        app = create_app(settings)
        migrated_workspace = app.state.workspaces.require(str(workspace["id"]))
        migrated_computer = app.state.store.get_computer(str(computer["id"]))
        assert migrated_workspace["backend_kind"] == "remote"
        assert migrated_workspace["tmux_session"] == workspace["tmux_session"]
        assert migrated_computer is not None
        assert migrated_computer["connection_method"] == "ssh"
        assert app.state.ssh.ensure_workspace(migrated_workspace)[0]["id"] == terminal["id"]
        after_panes = app.state.ssh._exec(
            migrated_computer,
            f"tmux list-panes -t {workspace['tmux_session']} -F '#{{pane_id}}:#{{pane_pid}}'",
        ).strip()
        assert after_panes == before_panes

        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post(
                "/login",
                data={"password": "test-token"},
                follow_redirects=False,
            )
            assert login.status_code == 303
            files_page = await client.get(f"/w/{workspace['id']}/files")
            terminal_page = await client.get(f"/w/{workspace['id']}/terminal")
        assert files_page.status_code == 200
        assert "upgrade.txt" in files_page.text
        assert terminal_page.status_code == 200
        assert "원격 작업공간" in terminal_page.text
        assert (
            app.state.ssh.read_text(migrated_workspace, "upgrade.txt", 1024).content
            == "preserved through upgrade\n"
        )

        with contextlib.suppress(Exception):
            app.state.ssh._exec(
                migrated_computer,
                f"tmux kill-session -t {workspace['tmux_session']}",
            )


@pytest.mark.asyncio
async def test_remote_run_end_to_end_uses_real_ssh_sftp_tmux_and_workspace(
    tmp_path: Path,
) -> None:
    local_root = tmp_path / "local"
    source_project = local_root / "source-project"
    source_project.mkdir(parents=True)
    (source_project / "input.txt").write_text("source-data\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    remote_run_base = tmp_path / "remote-runs"

    with _test_sshd(tmp_path) as server:
        store = StateStore(state_dir / "termroom.sqlite3")
        store.initialize()
        backend = SSHBackend(store, state_dir)
        probe = backend.probe_host_key("127.0.0.1", int(server["port"]))
        computer = store.create_computer(
            name="Remote Run QA",
            ssh_alias="",
            host="127.0.0.1",
            port=int(server["port"]),
            username=str(server["username"]),
            identity_file=str(server["client_key"]),
            host_key_type=probe["host_key_type"],
            host_key_data=probe["host_key_data"],
            host_fingerprint=probe["host_fingerprint"],
        )
        store.update_computer_run_base(str(computer["id"]), str(remote_run_base))
        computer = store.get_computer(str(computer["id"]))
        assert computer is not None
        backend.remember_host_key(computer)

        workspaces = WorkspaceManager(RootManager(local_root), store)
        source = workspaces.open("source-project")
        manager = RemoteRunManager(
            store,
            workspaces,
            backend,
            state_dir=state_dir,
            max_archive_bytes=64 * 1024 * 1024,
        )
        run_id = str(uuid.uuid4())
        run, created = await manager.create(
            {
                "id": run_id,
                "source_kind": "workspace",
                "source_workspace_id": str(source["id"]),
                "source_path": ".",
                "target_computer_id": str(computer["id"]),
                "command": (
                    'test "$(cat input.txt)" = source-data\n'
                    "false\n"
                    "printf 'continued-after-false\\n' > result.txt"
                ),
            }
        )
        assert created is True
        assert run["state"] == "preparing"

        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                run = await asyncio.to_thread(manager.poll, run_id, offset=0)
                if run["state"] in {"finished", "stopped", "failed", "lost"}:
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("Remote Run did not finish through the real SSH/tmux path")

            assert run["state"] == "finished"
            assert run["exit_code"] == 0
            assert run["workspace_id"]
            workspace = workspaces.require(str(run["workspace_id"]))
            assert workspace["is_remote_run"] is True
            assert backend.read_text(workspace, "result.txt", 1024).content == (
                "continued-after-false\n"
            )
            assert [row["id"] for row in store.list_recent_workspaces()] == [source["id"]]

            deleted = manager.request_delete(run_id)
            assert deleted["deleted"] is True
            assert store.get_remote_run(run_id) is None
            assert not remote_run_base.joinpath(run_id).exists()
        finally:
            with contextlib.suppress(Exception):
                manager.kill(run_id)
            with contextlib.suppress(Exception):
                terminal_run = store.get_remote_run(run_id)
                if terminal_run and terminal_run.get("workspace_id"):
                    store.delete_remote_run_workspace(run_id)
            await manager.shutdown()


@pytest.mark.asyncio
async def test_archive_remote_run_end_to_end_uses_real_ssh_and_one_event(
    tmp_path: Path,
) -> None:
    local_root = tmp_path / "local"
    local_root.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    remote_run_base = tmp_path / "archive-runs"

    with _test_sshd(tmp_path) as server:
        store = StateStore(state_dir / "termroom.sqlite3")
        store.initialize()
        backend = SSHBackend(store, state_dir)
        probe = backend.probe_host_key("127.0.0.1", int(server["port"]))
        computer = store.create_computer(
            name="Archive QA",
            ssh_alias="",
            host="127.0.0.1",
            port=int(server["port"]),
            username=str(server["username"]),
            identity_file=str(server["client_key"]),
            host_key_type=probe["host_key_type"],
            host_key_data=probe["host_key_data"],
            host_fingerprint=probe["host_fingerprint"],
        )
        store.update_computer_run_base(str(computer["id"]), str(remote_run_base))
        computer = store.get_computer(str(computer["id"]))
        assert computer is not None
        backend.remember_host_key(computer)
        workspaces = WorkspaceManager(RootManager(local_root), store)
        manager = RemoteRunManager(
            store,
            workspaces,
            backend,
            state_dir=state_dir,
            max_archive_bytes=64 * 1024 * 1024,
        )
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr("project/input.txt", "archive-source\n")
        archive_bytes = archive.getvalue()
        run_id = str(uuid.uuid4())
        run, created = await manager.create(
            {
                "id": run_id,
                "source_kind": "archive",
                "archive_format": "zip",
                "archive_name": "source.zip",
                "target_computer_id": str(computer["id"]),
                "command": (
                    'test "$(cat input.txt)" = archive-source\n'
                    "printf 'archive-result\\n' > result.txt"
                ),
            }
        )
        assert created is True
        assert run["source_kind"] == "archive"
        assert run["archive_format"] == "zip"

        async def archive_chunks():  # type: ignore[no-untyped-def]
            yield archive_bytes[:17]
            yield archive_bytes[17:]

        try:
            await manager.upload_archive(
                run_id,
                "source.zip",
                archive_chunks(),
                content_length=len(archive_bytes),
            )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                run = await asyncio.to_thread(manager.poll, run_id, offset=0)
                if run["state"] in {"finished", "stopped", "failed", "lost"}:
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("Archive Remote Run did not finish through real SSH/tmux")

            assert run["state"] == "finished"
            assert run["exit_code"] == 0
            workspace = workspaces.require(str(run["workspace_id"]))
            assert backend.read_text(workspace, "project/result.txt", 1024).content == (
                "archive-result\n"
            )
            matching_events = [
                event for event in store.list_activity_events() if event["subject_id"] == run_id
            ]
            assert len(matching_events) == 1
            assert matching_events[0]["kind"] == "remote_run.completed"

            deleted = manager.request_delete(run_id)
            assert deleted["deleted"] is True
            assert not remote_run_base.joinpath(run_id).exists()
            retained = store.get_activity_event(str(matching_events[0]["id"]))
            assert retained is not None
            assert retained["subject_exists"] == 0
        finally:
            with contextlib.suppress(Exception):
                manager.kill(run_id)
            await manager.shutdown()


@pytest.mark.asyncio
async def test_remote_run_observer_reconciles_real_ssh_completion_after_restart(
    tmp_path: Path,
) -> None:
    local_root = tmp_path / "local"
    source_project = local_root / "source-project"
    source_project.mkdir(parents=True)
    (source_project / "input.txt").write_text("observer-source\n", encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    remote_run_base = tmp_path / "remote-runs"

    with _test_sshd(tmp_path) as server:
        store = StateStore(state_dir / "termroom.sqlite3")
        store.initialize()
        backend = SSHBackend(store, state_dir)
        probe = backend.probe_host_key("127.0.0.1", int(server["port"]))
        computer = store.create_computer(
            name="Observer QA",
            ssh_alias="",
            host="127.0.0.1",
            port=int(server["port"]),
            username=str(server["username"]),
            identity_file=str(server["client_key"]),
            host_key_type=probe["host_key_type"],
            host_key_data=probe["host_key_data"],
            host_fingerprint=probe["host_fingerprint"],
        )
        store.update_computer_run_base(str(computer["id"]), str(remote_run_base))
        computer = store.get_computer(str(computer["id"]))
        assert computer is not None
        backend.remember_host_key(computer)

        workspaces = WorkspaceManager(RootManager(local_root), store)
        source = workspaces.open("source-project")
        first_manager = RemoteRunManager(
            store,
            workspaces,
            backend,
            state_dir=state_dir,
            max_archive_bytes=64 * 1024 * 1024,
        )
        await first_manager.startup()
        run_id = str(uuid.uuid4())
        second_manager: RemoteRunManager | None = None
        try:
            run, created = await first_manager.create(
                {
                    "id": run_id,
                    "source_kind": "workspace",
                    "source_workspace_id": str(source["id"]),
                    "source_path": ".",
                    "target_computer_id": str(computer["id"]),
                    "command": (
                        "sleep 3\n"
                        'test "$(cat input.txt)" = observer-source\n'
                        "printf 'observed-after-restart\\n' > observed.txt"
                    ),
                }
            )
            assert created is True
            assert run["state"] == "preparing"

            running_deadline = time.monotonic() + 10
            while time.monotonic() < running_deadline:
                stored = store.get_remote_run(run_id)
                assert stored is not None
                if stored["state"] == "running":
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("Remote Run did not reach running before Core restart")

            await first_manager.shutdown()
            second_manager = RemoteRunManager(
                store,
                workspaces,
                backend,
                state_dir=state_dir,
                max_archive_bytes=64 * 1024 * 1024,
            )
            await second_manager.startup()

            completion_deadline = time.monotonic() + 15
            while time.monotonic() < completion_deadline:
                matching_events = [
                    event for event in store.list_activity_events() if event["subject_id"] == run_id
                ]
                if matching_events:
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("Background observer did not record the real SSH completion")

            stored = store.get_remote_run(run_id)
            assert stored is not None
            assert stored["state"] == "finished"
            assert stored["exit_code"] == 0
            assert len(matching_events) == 1
            assert matching_events[0]["kind"] == "remote_run.completed"
            assert (
                remote_run_base.joinpath(run_id, "work", "observed.txt").read_text(encoding="utf-8")
                == "observed-after-restart\n"
            )

            deleted = second_manager.request_delete(run_id)
            assert deleted["deleted"] is True
            retained_event = store.get_activity_event(str(matching_events[0]["id"]))
            assert retained_event is not None
            assert retained_event["subject_exists"] == 0
        finally:
            if second_manager is None:
                await first_manager.shutdown()
            else:
                await second_manager.shutdown()


def test_ssh_backend_rejects_overly_permissive_private_key(tmp_path: Path) -> None:
    key = tmp_path / "id_test"
    key.write_text("not a real key", encoding="utf-8")
    key.chmod(0o644)

    with pytest.raises(SSHBackendError, match="permissions are too open"):
        SSHBackend.validate_identity_file(str(key))
