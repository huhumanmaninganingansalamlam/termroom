from __future__ import annotations

import asyncio
import contextlib
import io
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from starlette.datastructures import UploadFile

from termroom.app import create_app
from termroom.config import Settings
from termroom.db import StateStore
from termroom.remote_runs import RemoteRunManager
from termroom.ssh_backend import SSHBackend, SSHBackendError, SSHHostKeyChanged
from termroom.workspaces import ProjectPathExists, RootManager, WorkspaceManager


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextlib.contextmanager
def _test_sshd(tmp_path: Path) -> Iterator[dict[str, object]]:
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
                "LogLevel ERROR",
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
                    raise RuntimeError(
                        (qa / "sshd.log").read_text(encoding="utf-8")
                    ) from exc
                time.sleep(0.05)
        else:
            raise RuntimeError("test sshd did not start")
        yield {
            "port": port,
            "username": username,
            "client_key": client_key,
            "authorized_keys": authorized_keys,
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
        ssh_alias="",
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
    fake_ssh.write_text(
        "#!/bin/sh\n"
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
    process_pid, master_fd = backend._spawn_ssh_tmux_client(workspace, terminal)
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
    finally:
        backend._wait_for_pid(process_pid, 1)
        os.close(master_fd)

    helper = state_dir / "ssh" / "askpass"
    assert helper.is_file()
    assert helper.stat().st_mode & 0o077 == 0
    assert sys.executable in helper.read_text(encoding="utf-8")


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
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
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
                columns = {
                    str(row["name"]) for row in db.execute("PRAGMA table_info(computers)")
                }
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
    monkeypatch.setattr(app.state.ssh, "probe_host_key", lambda host, port: host_key)
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
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
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
            with pytest.raises(SSHBackendError):
                backend.read_text(workspace, "escape/secret.txt", 1024)
            backend.create(workspace, ".", "empty.txt", directory=False)
            assert backend.stat(workspace, "empty.txt").size == 0

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
            recent_paths = [
                entry.relative_path for entry in backend.recent_files(workspace).entries
            ]
            assert "result.csv" in recent_paths
            assert "ignored.tmp" not in recent_paths

            process_pid, master_fd = backend._spawn_ssh_tmux_client(workspace, terminal)
            try:
                time.sleep(0.4)
                backend._set_window_size(master_fd, rows=41, cols=123)
                os.killpg(process_pid, signal.SIGWINCH)
                deadline = time.monotonic() + 2
                sizes: list[str] = []
                while time.monotonic() < deadline:
                    output = backend._exec(
                        computer,
                        f"tmux list-clients -t {workspace['tmux_session']} "
                        "-F '#{client_width}x#{client_height}'",
                    )
                    sizes = output.strip().splitlines()
                    if "123x41" in sizes:
                        break
                    time.sleep(0.05)
                assert "123x41" in sizes
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process_pid, signal.SIGTERM)
                backend._wait_for_pid(process_pid, 1)
                os.close(master_fd)

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

                server_open = await client.post(
                    f"/computers/{computer['id']}/server-terminal",
                    data={"_csrf": settings.csrf_token},
                    follow_redirects=False,
                )
                assert server_open.status_code == 303
                assert server_open.headers["location"] == (
                    f"/w/{server_workspace['id']}/terminal"
                )
                server_page = await client.get(server_open.headers["location"])
                assert server_page.status_code == 200
                assert "SSH server terminal" in server_page.text
                assert remote_home in server_page.text
                assert server_terminal["id"] in server_page.text
                assert f'/w/{server_workspace["id"]}/files' not in server_page.text

                picker_page = await client.get(f"/open/{computer['id']}")
                assert picker_page.status_code == 200
                assert f'/w/{server_workspace["id"]}' not in picker_page.text
        finally:
            with contextlib.suppress(Exception):
                backend._exec(computer, f"tmux kill-session -t {workspace['tmux_session']}")
            with contextlib.suppress(Exception):
                backend._exec(
                    computer,
                    f"tmux kill-session -t {server_workspace['tmux_session']}",
                )


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
                    "test \"$(cat input.txt)\" = source-data\n"
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


def test_ssh_backend_rejects_overly_permissive_private_key(tmp_path: Path) -> None:
    key = tmp_path / "id_test"
    key.write_text("not a real key", encoding="utf-8")
    key.chmod(0o644)

    with pytest.raises(SSHBackendError, match="permissions are too open"):
        SSHBackend.validate_identity_file(str(key))
