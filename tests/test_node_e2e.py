from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import uuid
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from websockets.sync.client import connect as websocket_connect

from termroom.db import StateStore
from termroom.node_service import NODE_SERVICE_UNIT_NAME, NodeServiceManager
from termroom.ssh_backend import SSHBackend


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
    username = os.environ.get("USER") or subprocess.check_output(
        ["id", "-un"], text=True
    ).strip()
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
        yield {"port": port, "username": username, "client_key": client_key}
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        log.close()


def _wait_until(
    predicate: Callable[[], Any],
    *,
    timeout: float = 15.0,
    description: str,
) -> Any:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, httpx.HTTPError) as exc:
            last_error = exc
        time.sleep(0.1)
    detail = f": {last_error}" if last_error else ""
    raise AssertionError(f"Timed out waiting for {description}{detail}")


def _start_process(command: list[str], environment: dict[str, str], log_path: Path):
    log_handle = log_path.open("ab", buffering=0)
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        start_new_session=True,
    )
    return process, log_handle


def _stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _login(base_url: str, password: str) -> tuple[httpx.Client, str]:
    client = httpx.Client(base_url=base_url, timeout=10)
    response = client.post("/login", data={"password": password})
    assert response.status_code == 303
    setup = client.get("/computers/new")
    assert setup.status_code == 200
    match = re.search(r'name="_csrf" value="([a-f0-9]{64})"', setup.text)
    assert match is not None
    return client, match.group(1)


def _node_status_is(client: httpx.Client, node_id: str, status: str) -> bool:
    response = client.get(f"/computers/{node_id}")
    return (
        response.status_code == 200
        and f"<p><strong>{status}</strong></p>" in response.text
    )


def _file_run_status_is(
    client: httpx.Client, run_id: str, state: str
) -> dict[str, Any] | None:
    response = client.get(f"/api/file-runs/{run_id}/status")
    if response.status_code != 200:
        return None
    payload = response.json()
    if payload.get("ok") is True and payload.get("state") == state:
        return payload
    return None


def _remote_run_status_is(
    client: httpx.Client,
    run_id: str,
    state: str,
    *,
    connection: str | None = None,
) -> dict[str, Any] | None:
    response = client.get(f"/api/remote-runs/{run_id}/status")
    if response.status_code != 200:
        return None
    payload = response.json()
    if payload.get("ok") is not True or payload.get("state") != state:
        return None
    if connection is not None and payload.get("connection") != connection:
        return None
    return payload


def _workspace_usage_is(
    client: httpx.Client,
    workspace_id: str,
    state: str,
    *,
    minimum_processes: int = 0,
) -> dict[str, Any] | None:
    response = client.get(f"/api/workspaces/{workspace_id}/usage")
    if response.status_code != 200:
        return None
    payload = response.json()
    if payload.get("ok") is not True or payload.get("state") != state:
        return None
    if minimum_processes:
        sample = payload.get("sample")
        if not isinstance(sample, dict) or sample.get("process_count", 0) < minimum_processes:
            return None
    return payload


def _create_remote_run(
    client: httpx.Client,
    csrf: str,
    *,
    source_kind: str,
    target_id: str,
    command: str,
    **source: Any,
) -> str:
    run_id = str(uuid.uuid4())
    response = client.post(
        "/api/remote-runs",
        json={
            "id": run_id,
            "source_kind": source_kind,
            "target_computer_id": target_id,
            "command": command,
            **source,
        },
        headers={"X-Termroom-CSRF": csrf},
    )
    assert response.status_code == 202, response.text
    assert response.json()["run_id"] == run_id
    return run_id


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def _start_file_run(
    client: httpx.Client,
    csrf: str,
    workspace_id: str,
    relative_path: str,
    content: str,
) -> tuple[str, str]:
    editor = client.get(f"/w/{workspace_id}/edit/{relative_path}")
    assert editor.status_code == 200
    assert 'value="save_and_run"' in editor.text
    digest = re.search(r'name="digest" value="([a-f0-9]{64})"', editor.text)
    mtime = re.search(r'name="mtime_ns" value="([0-9]+)"', editor.text)
    key = re.search(
        r'name="file_run_idempotency_key" value="([a-f0-9-]{36})"',
        editor.text,
    )
    assert digest is not None and mtime is not None and key is not None
    started = client.post(
        f"/w/{workspace_id}/edit/{relative_path}",
        data={
            "_csrf": csrf,
            "digest": digest.group(1),
            "mtime_ns": mtime.group(1),
            "content": content,
            "intent": "save_and_run",
            "file_run_idempotency_key": key.group(1),
        },
    )
    assert started.status_code == 303, started.text
    destination = urlsplit(started.headers["location"])
    terminal_values = parse_qs(destination.query).get("terminal")
    assert terminal_values and len(terminal_values) == 1
    terminal_id = terminal_values[0]
    assert re.fullmatch(r"[a-f0-9]{32}", terminal_id)
    terminal_page = client.get(started.headers["location"])
    assert terminal_page.status_code == 200
    run_match = re.search(
        r'data-status-url="/api/file-runs/([a-f0-9-]{36})/status"',
        terminal_page.text,
    )
    assert run_match is not None
    return run_match.group(1), terminal_id


def _core_command(root: Path, state_dir: Path, port: int) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "termroom.cli",
        str(root),
        "--foreground",
        "--no-open",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--state-dir",
        str(state_dir),
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        command.append("--allow-root")
    return command


def _node_prefix(state_dir: Path) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "termroom.cli",
        "node",
        "--state-dir",
        str(state_dir),
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        command.append("--allow-root-user")
    return command


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is required")
def test_real_node_pairing_and_control_connection_use_custom_ca(tmp_path: Path) -> None:
    core_root = tmp_path / "core-root"
    core_state = tmp_path / "core-state"
    node_root = tmp_path / "node-root"
    node_state = tmp_path / "node-state"
    certificates = tmp_path / "certificates"
    for path in (core_root, node_root, certificates):
        path.mkdir()

    ca_key = certificates / "ca.key"
    ca_file = certificates / "ca.pem"
    server_key = certificates / "server.key"
    server_request = certificates / "server.csr"
    server_certificate = certificates / "server.pem"
    server_extensions = certificates / "server.ext"
    server_extensions.write_text(
        "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
        "extendedKeyUsage=serverAuth\n"
        "keyUsage=digitalSignature,keyEncipherment\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-days",
            "1",
            "-nodes",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_file),
            "-subj",
            "/CN=Termroom E2E CA",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "req",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-keyout",
            str(server_key),
            "-out",
            str(server_request),
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(server_request),
            "-CA",
            str(ca_file),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(server_certificate),
            "-days",
            "1",
            "-sha256",
            "-extfile",
            str(server_extensions),
        ],
        check=True,
        capture_output=True,
    )

    port = _free_port()
    base_url = f"https://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment["TERMROOM_PASSWORD"] = "custom-ca-e2e-password"
    core_script = (
        "import sys, uvicorn; "
        "from termroom.app import create_app; "
        "from termroom.config import Settings; "
        "root, state, port, certificate, key = sys.argv[1:]; "
        "settings = Settings.create(root, host='127.0.0.1', port=int(port), "
        "state_dir=state, secure_cookie=True); "
        "uvicorn.run(create_app(settings), host='127.0.0.1', port=int(port), "
        "ssl_certfile=certificate, ssl_keyfile=key, log_level='info')"
    )
    core: subprocess.Popen[bytes] | None = None
    pair: subprocess.Popen[bytes] | None = None
    node: subprocess.Popen[bytes] | None = None
    handles: list[Any] = []
    trusted_context = ssl.create_default_context(cafile=str(ca_file))
    client = httpx.Client(base_url=base_url, timeout=10, verify=trusted_context)

    try:
        core, handle = _start_process(
            [
                sys.executable,
                "-c",
                core_script,
                str(core_root),
                str(core_state),
                str(port),
                str(server_certificate),
                str(server_key),
            ],
            environment,
            tmp_path / "tls-core.log",
        )
        handles.append(handle)
        _wait_until(
            lambda: client.get("/health").status_code == 200,
            description="TLS Core startup",
        )
        login = client.post("/login", data={"password": "custom-ca-e2e-password"})
        assert login.status_code == 303
        setup = client.get("/computers/new")
        csrf_match = re.search(r'name="_csrf" value="([a-f0-9]{64})"', setup.text)
        assert csrf_match is not None
        csrf = csrf_match.group(1)

        created = client.post("/computers/node/pair", data={"_csrf": csrf})
        assert created.status_code == 201
        code_match = re.search(r'class="node-pairing-code">([^<]+)<', created.text)
        pairing_match = re.search(
            r'name="pairing_id" value="([a-f0-9]{32})"', created.text
        )
        assert code_match is not None and pairing_match is not None
        pair_command = [
            *_node_prefix(node_state),
            "pair",
            "--core",
            base_url,
            "--code",
            code_match.group(1),
            "--allow-root",
            str(node_root),
            "--name",
            "Custom CA E2E Node",
            "--timeout",
            "20",
        ]

        untrusted = subprocess.run(
            pair_command,
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert untrusted.returncode == 2
        assert "certificate verify failed" in (untrusted.stdout + untrusted.stderr).lower()
        assert not (node_state / "node.json").exists()

        pair, handle = _start_process(
            [*pair_command, "--ca-file", str(ca_file)],
            environment,
            tmp_path / "tls-pair.log",
        )
        handles.append(handle)
        review_url = f"/computers/node/pair?pairing_id={pairing_match.group(1)}"
        review = _wait_until(
            lambda: (
                response
                if (response := client.get(review_url)).status_code == 200
                and "SHA256:" in response.text
                else None
            ),
            description="custom-CA Node fingerprint submission",
        )
        enrollment_match = re.search(
            r'/computers/node/pair/([a-f0-9]{32})/approve', review.text
        )
        assert enrollment_match is not None
        approved = client.post(
            f"/computers/node/pair/{enrollment_match.group(1)}/approve",
            data={"_csrf": csrf},
        )
        assert approved.status_code == 303
        node_id = approved.headers["location"].split("/")[2].split("?")[0]
        assert pair.wait(timeout=10) == 0
        stored = json.loads((node_state / "node.json").read_text(encoding="utf-8"))
        assert stored["ca_file"] == str(ca_file.resolve())

        node, handle = _start_process(
            _node_prefix(node_state), environment, tmp_path / "tls-node.log"
        )
        handles.append(handle)
        _wait_until(
            lambda: _node_status_is(client, node_id, "Online"),
            description="custom-CA Node WSS control connection",
        )
        assert node.poll() is None
    finally:
        client.close()
        _stop_process(node)
        _stop_process(pair)
        _stop_process(core)
        for handle in handles:
            handle.close()


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_real_node_process_pair_workspace_terminal_files_and_recovery(tmp_path: Path) -> None:
    core_root = tmp_path / "core-root"
    node_root = tmp_path / "node-root"
    core_state = tmp_path / "core-state"
    node_state = tmp_path / "node-state"
    editor_limit = 1024 * 1024
    core_root.mkdir()
    node_root.mkdir()
    source_root = core_root / "source-project"
    source_root.mkdir()
    (source_root / "source.txt").write_text("workspace-source\n", encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        "if test \"${1:-}\" = '-C'; then\n"
        "  printf 'abcdef0123456789abcdef0123456789abcdef01\\n'\n"
        "  exit 0\n"
        "fi\n"
        "last=''\n"
        "for value in \"$@\"; do last=$value; done\n"
        "mkdir -p -- \"$last\"\n"
        "printf 'git-source\\n' > \"$last/source.txt\"\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o700)
    (node_root / "hello.txt").write_text("before\n", encoding="utf-8")
    (node_root / "large.txt").write_bytes(b"x" * editor_limit)
    (node_root / "interactive.py").write_text(
        "from pathlib import Path\n"
        "value = input('node-value: ')\n"
        "Path('interactive-result.txt').write_text(value)\n"
        "print('NODE_FILE_RUN:' + value)\n",
        encoding="utf-8",
    )
    (node_root / "fail.py").write_text(
        "raise SystemExit(7)\n", encoding="utf-8"
    )
    (node_root / "stubborn.py").write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "while True: time.sleep(0.1)\n",
        encoding="utf-8",
    )
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment["TERMROOM_PASSWORD"] = "node-e2e-password"
    environment["TERMROOM_LOCALE"] = "en"
    environment["PATH"] = f"{fake_bin}:{environment.get('PATH', '')}"
    core: subprocess.Popen[bytes] | None = None
    node: subprocess.Popen[bytes] | None = None
    pair: subprocess.Popen[bytes] | None = None
    handles: list[Any] = []
    client: httpx.Client | None = None
    session_name = ""

    try:
        core, handle = _start_process(
            _core_command(core_root, core_state, port),
            environment,
            tmp_path / "core.log",
        )
        handles.append(handle)
        _wait_until(
            lambda: httpx.get(f"{base_url}/health", timeout=1).status_code == 200,
            description="Core startup",
        )
        client, csrf = _login(base_url, "node-e2e-password")

        created = client.post(
            "/computers/node/pair", data={"_csrf": csrf}
        )
        assert created.status_code == 201
        code_match = re.search(r'class="node-pairing-code">([^<]+)<', created.text)
        pairing_match = re.search(r'name="pairing_id" value="([a-f0-9]{32})"', created.text)
        assert code_match is not None and pairing_match is not None

        pair_command = [
            *_node_prefix(node_state),
            "pair",
            "--core",
            base_url,
            "--code",
            code_match.group(1),
            "--allow-root",
            str(node_root),
            "--name",
            "E2E Node",
            "--timeout",
            "20",
        ]
        pair, handle = _start_process(
            pair_command, environment, tmp_path / "pair.log"
        )
        handles.append(handle)

        review_url = f"/computers/node/pair?pairing_id={pairing_match.group(1)}"
        review = _wait_until(
            lambda: (
                response
                if (response := client.get(review_url)).status_code == 200
                and "SHA256:" in response.text
                else None
            ),
            description="Node fingerprint submission",
        )
        enrollment_match = re.search(
            r'/computers/node/pair/([a-f0-9]{32})/approve', review.text
        )
        assert enrollment_match is not None
        approved = client.post(
            f"/computers/node/pair/{enrollment_match.group(1)}/approve",
            data={"_csrf": csrf},
        )
        assert approved.status_code == 303
        node_id = approved.headers["location"].split("/")[2].split("?")[0]
        assert pair.wait(timeout=10) == 0

        node, handle = _start_process(
            _node_prefix(node_state), environment, tmp_path / "node.log"
        )
        handles.append(handle)
        _wait_until(
            lambda: _node_status_is(client, node_id, "Online"),
            description="Node control connection",
        )

        store = StateStore(core_state / "termroom.sqlite3")
        target_picker = client.get("/remote-runs/new")
        assert target_picker.status_code == 200
        assert f'<option value="{node_id}"' in target_picker.text
        assert ">E2E Node</option>" in target_picker.text

        local_root = next(
            item
            for item in store.list_local_roots()
            if Path(str(item["path"])) == core_root
        )
        opened_source = client.post(
            "/api/workspaces",
            data={
                "_csrf": csrf,
                "root_id": local_root["id"],
                "path": "source-project",
            },
        )
        assert opened_source.status_code == 303
        source_workspace_id = opened_source.headers["location"].split("/")[2]

        workspace_run_id = _create_remote_run(
            client,
            csrf,
            source_kind="workspace",
            target_id=node_id,
            command="cat source.txt > workspace-result.txt",
            source_workspace_id=source_workspace_id,
            source_path=".",
        )
        workspace_run = _wait_until(
            lambda: _remote_run_status_is(client, workspace_run_id, "finished"),
            description="Node Workspace Source Remote Run",
        )
        assert workspace_run["exit_code"] == 0
        assert workspace_run["workspace_id"]
        workspace_files = client.get(
            f"/w/{workspace_run['workspace_id']}/files"
        )
        assert workspace_files.status_code == 200
        assert "workspace-result.txt" in workspace_files.text

        archive_run_id = _create_remote_run(
            client,
            csrf,
            source_kind="archive",
            target_id=node_id,
            command="cat archive.txt > archive-result.txt",
            archive_name="node-source.zip",
            archive_format="zip",
        )
        archive_upload = client.post(
            f"/api/remote-runs/{archive_run_id}/archive",
            params={"filename": "node-source.zip"},
            content=_zip_bytes({"archive.txt": b"archive-source\n"}),
            headers={"X-Termroom-CSRF": csrf},
        )
        assert archive_upload.status_code == 202, archive_upload.text
        archive_run = _wait_until(
            lambda: _remote_run_status_is(client, archive_run_id, "finished"),
            description="Node Archive Source Remote Run",
        )
        assert archive_run["exit_code"] == 0

        git_run_id = _create_remote_run(
            client,
            csrf,
            source_kind="git",
            target_id=node_id,
            command="cat source.txt > git-result.txt",
            source_url="https://example.test/public.git",
        )
        git_run = _wait_until(
            lambda: _remote_run_status_is(client, git_run_id, "finished"),
            description="Node public Git Source Remote Run",
        )
        assert git_run["exit_code"] == 0
        stored_git_run = store.get_remote_run(git_run_id)
        assert stored_git_run is not None
        assert stored_git_run["source_revision"] == (
            "abcdef0123456789abcdef0123456789abcdef01"
        )

        node_config = json.loads((node_state / "node.json").read_text(encoding="utf-8"))
        managed_root = Path(node_config["run_root"])
        assert (
            managed_root / workspace_run_id / "work" / "workspace-result.txt"
        ).read_text(encoding="utf-8") == "workspace-source\n"
        assert (
            managed_root / archive_run_id / "work" / "archive-result.txt"
        ).read_text(encoding="utf-8") == "archive-source\n"
        assert (
            managed_root / git_run_id / "work" / "git-result.txt"
        ).read_text(encoding="utf-8") == "git-source\n"

        for completed_run_id in (workspace_run_id, archive_run_id, git_run_id):
            deleted = client.post(
                f"/remote-runs/{completed_run_id}/delete",
                data={"_csrf": csrf},
            )
            assert deleted.status_code == 303
            assert store.get_remote_run(completed_run_id) is None
            assert not (managed_root / completed_run_id).exists()

        opened = client.post(
            f"/computers/{node_id}/workspaces",
            data={
                "_csrf": csrf,
                "path": str(node_root),
                "display_name": "Node Project",
            },
        )
        assert opened.status_code == 303
        workspace_id = opened.headers["location"].split("/")[2]
        terminal_page = client.get(f"/w/{workspace_id}/terminal")
        assert terminal_page.status_code == 200
        terminal_match = re.search(r'data-terminal-id="([a-f0-9]{32})"', terminal_page.text)
        assert terminal_match is not None
        terminal_id = terminal_match.group(1)

        files_page = client.get(f"/w/{workspace_id}/files")
        assert files_page.status_code == 200
        assert "hello.txt" in files_page.text
        recent_page = client.get(f"/w/{workspace_id}/recent")
        assert recent_page.status_code == 200
        assert "hello.txt" in recent_page.text
        assert "shell" in recent_page.text
        assert "Run this location on another Remote" in files_page.text
        editor = client.get(f"/w/{workspace_id}/edit/hello.txt")
        digest = re.search(r'name="digest" value="([a-f0-9]{64})"', editor.text)
        mtime = re.search(r'name="mtime_ns" value="([0-9]+)"', editor.text)
        assert digest is not None and mtime is not None
        assert 'value="save_and_run"' not in editor.text
        saved = client.post(
            f"/w/{workspace_id}/edit/hello.txt",
            data={
                "_csrf": csrf,
                "digest": digest.group(1),
                "mtime_ns": mtime.group(1),
                "content": "Node 한글 saved\n",
                "intent": "save",
            },
        )
        assert saved.status_code == 303
        assert (node_root / "hello.txt").read_text(encoding="utf-8") == "Node 한글 saved\n"

        large_editor = client.get(f"/w/{workspace_id}/edit/large.txt")
        assert large_editor.status_code == 200
        large_digest = re.search(
            r'name="digest" value="([a-f0-9]{64})"', large_editor.text
        )
        large_mtime = re.search(
            r'name="mtime_ns" value="([0-9]+)"', large_editor.text
        )
        assert large_digest is not None and large_mtime is not None
        large_saved = client.post(
            f"/w/{workspace_id}/edit/large.txt",
            data={
                "_csrf": csrf,
                "digest": large_digest.group(1),
                "mtime_ns": large_mtime.group(1),
                "content": "y" * editor_limit,
                "intent": "save",
            },
        )
        assert large_saved.status_code == 303
        assert (node_root / "large.txt").read_bytes() == b"y" * editor_limit

        node_source_run_id = _create_remote_run(
            client,
            csrf,
            source_kind="workspace",
            target_id=node_id,
            command="cat hello.txt > node-source-result.txt; "
            "wc -c < large.txt > large-size.txt",
            source_workspace_id=workspace_id,
            source_path=".",
        )
        node_source_run = _wait_until(
            lambda: _remote_run_status_is(client, node_source_run_id, "finished"),
            description="same-Node Workspace Source Remote Run",
        )
        assert node_source_run["exit_code"] == 0
        assert (
            managed_root / node_source_run_id / "work" / "node-source-result.txt"
        ).read_text(encoding="utf-8") == "Node 한글 saved\n"
        assert int(
            (managed_root / node_source_run_id / "work" / "large-size.txt")
            .read_text(encoding="utf-8")
            .strip()
        ) == editor_limit
        assert (node_root / "hello.txt").read_text(encoding="utf-8") == "Node 한글 saved\n"
        deleted_node_source = client.post(
            f"/remote-runs/{node_source_run_id}/delete",
            data={"_csrf": csrf},
        )
        assert deleted_node_source.status_code == 303
        assert not (managed_root / node_source_run_id).exists()

        interactive_content = (node_root / "interactive.py").read_text(encoding="utf-8")
        interactive_run_id, run_terminal_id = _start_file_run(
            client,
            csrf,
            workspace_id,
            "interactive.py",
            interactive_content,
        )
        _wait_until(
            lambda: _file_run_status_is(client, interactive_run_id, "running"),
            description="Node interactive File Run",
        )
        initial_usage = _wait_until(
            lambda: _workspace_usage_is(
                client, workspace_id, "fresh", minimum_processes=2
            ),
            description="Node Workspace activity",
        )
        assert initial_usage["estimated"] is True
        assert initial_usage["sample"]["memory_bytes"] > 0
        assert initial_usage["sample"]["cpu_percent"] >= 0
        assert initial_usage["last_observed_at"]

        session_cookie = client.cookies.get("termroom_session")
        assert session_cookie
        with websocket_connect(
            f"ws://127.0.0.1:{port}/ws/terminal/{terminal_id}",
            origin=base_url,
            additional_headers={"Cookie": f"termroom_session={session_cookie}"},
            open_timeout=5,
        ) as websocket:
            websocket.send(json.dumps({"kind": "claim"}))
            websocket.send(
                json.dumps({"kind": "input", "data": "printf 'NODE_E2E_한글\\n'\r"})
            )
            output = ""
            deadline = time.monotonic() + 8
            while "NODE_E2E_한글" not in output and time.monotonic() < deadline:
                output += str(websocket.recv(timeout=2))
            assert "NODE_E2E_한글" in output

        store = StateStore(core_state / "termroom.sqlite3")
        workspace = store.get_workspace(workspace_id)
        assert workspace is not None
        session_name = str(workspace["tmux_session"])
        assert len(store.list_computers()) == 1
        assert len(store.list_workspaces_for_computer(node_id)) == 1
        assert len(store.list_terminals(workspace_id)) == 2

        _wait_until(
            lambda: _node_status_is(client, node_id, "Online"),
            description="Node connection after Terminal stream close",
        )

        recovery_run_id = _create_remote_run(
            client,
            csrf,
            source_kind="workspace",
            target_id=node_id,
            command="printf 'once\\n' >> executions.txt; sleep 60",
            source_workspace_id=source_workspace_id,
            source_path=".",
        )
        recovery_run = _wait_until(
            lambda: _remote_run_status_is(
                client, recovery_run_id, "running", connection="online"
            ),
            description="active Node Remote Run before Core restart",
        )
        assert recovery_run["workspace_id"]
        execution_record = managed_root / recovery_run_id / "work" / "executions.txt"
        _wait_until(
            lambda: execution_record.exists(),
            description="Node Remote Run first command side effect",
        )
        assert execution_record.read_text(encoding="utf-8") == "once\n"

        _stop_process(core)
        core = None
        if client is not None:
            client.close()
            client = None
        core, handle = _start_process(
            _core_command(core_root, core_state, port),
            environment,
            tmp_path / "core-restart.log",
        )
        handles.append(handle)
        _wait_until(
            lambda: httpx.get(f"{base_url}/health", timeout=1).status_code == 200,
            description="Core restart",
        )
        client, csrf = _login(base_url, "node-e2e-password")
        _wait_until(
            lambda: _node_status_is(client, node_id, "Online"),
            timeout=20,
            description="Node reconnect after Core restart",
        )
        restored = client.get(
            f"/w/{workspace_id}/terminal/{terminal_id}/scrollback"
        )
        assert restored.status_code == 200
        assert "NODE_E2E_한글" in restored.text
        restored_run = _wait_until(
            lambda: _file_run_status_is(client, interactive_run_id, "running"),
            description="File Run reconciliation after Core restart",
        )
        assert restored_run["connection"] == "online"
        restored_remote_run = _wait_until(
            lambda: _remote_run_status_is(
                client, recovery_run_id, "running", connection="online"
            ),
            description="Remote Run reconciliation after Core restart",
        )
        assert restored_remote_run["workspace_id"] == recovery_run["workspace_id"]
        assert execution_record.read_text(encoding="utf-8") == "once\n"
        run_scrollback = client.get(
            f"/w/{workspace_id}/terminal/{run_terminal_id}/scrollback"
        )
        assert run_scrollback.status_code == 200
        assert "node-value:" in run_scrollback.text
        usage_before_disconnect = _wait_until(
            lambda: _workspace_usage_is(
                client, workspace_id, "fresh", minimum_processes=2
            ),
            description="Workspace activity after Core restart",
        )

        session_cookie = client.cookies.get("termroom_session")
        assert session_cookie
        with websocket_connect(
            f"ws://127.0.0.1:{port}/ws/terminal/{run_terminal_id}",
            origin=base_url,
            additional_headers={"Cookie": f"termroom_session={session_cookie}"},
            open_timeout=5,
        ) as websocket:
            websocket.send(json.dumps({"kind": "claim"}))
            websocket.send(json.dumps({"kind": "input", "data": "after-restart\r"}))
            output = ""
            deadline = time.monotonic() + 8
            while "NODE_FILE_RUN:after-restart" not in output and time.monotonic() < deadline:
                output += str(websocket.recv(timeout=2))
            assert "NODE_FILE_RUN:after-restart" in output
        completed = _wait_until(
            lambda: _file_run_status_is(client, interactive_run_id, "finished"),
            description="interactive File Run completion",
        )
        assert completed["exit_code"] == 0
        assert (
            node_root / "interactive-result.txt"
        ).read_text(encoding="utf-8") == "after-restart"

        failed_run_id, reused_run_terminal_id = _start_file_run(
            client,
            csrf,
            workspace_id,
            "fail.py",
            (node_root / "fail.py").read_text(encoding="utf-8"),
        )
        assert reused_run_terminal_id == run_terminal_id
        failed = _wait_until(
            lambda: _file_run_status_is(client, failed_run_id, "finished"),
            description="non-zero Node File Run",
        )
        assert failed["exit_code"] == 7

        stubborn_run_id, reused_run_terminal_id = _start_file_run(
            client,
            csrf,
            workspace_id,
            "stubborn.py",
            (node_root / "stubborn.py").read_text(encoding="utf-8"),
        )
        assert reused_run_terminal_id == run_terminal_id
        _wait_until(
            lambda: _file_run_status_is(client, stubborn_run_id, "running"),
            description="stubborn Node File Run",
        )

        _stop_process(node)
        node = None
        _wait_until(
            lambda: _node_status_is(client, node_id, "Offline"),
            description="Node disconnect",
        )
        offline_usage = _wait_until(
            lambda: _workspace_usage_is(client, workspace_id, "offline"),
            description="offline Workspace activity",
        )
        assert offline_usage["sample"] is None
        assert offline_usage["last_observed_at"] == usage_before_disconnect[
            "last_observed_at"
        ]
        assert offline_usage["age_seconds"] >= 0
        offline_run = _wait_until(
            lambda: _file_run_status_is(client, stubborn_run_id, "running"),
            description="active File Run preserved while Node is offline",
        )
        assert offline_run["connection"] == "offline"
        offline_remote_run = _wait_until(
            lambda: _remote_run_status_is(
                client, recovery_run_id, "running", connection="offline"
            ),
            description="active Remote Run preserved while Node is offline",
        )
        assert offline_remote_run["workspace_id"] == recovery_run["workspace_id"]
        node, handle = _start_process(
            _node_prefix(node_state), environment, tmp_path / "node-restart.log"
        )
        handles.append(handle)
        _wait_until(
            lambda: _node_status_is(client, node_id, "Online"),
            timeout=20,
            description="Node process reconnect",
        )
        reconnected_usage = _wait_until(
            lambda: _workspace_usage_is(
                client, workspace_id, "fresh", minimum_processes=2
            ),
            description="Workspace activity after Node reconnect",
        )
        assert reconnected_usage["sample"] is not None
        reconnected_run = _wait_until(
            lambda: _file_run_status_is(client, stubborn_run_id, "running"),
            description="active File Run after Node reconnect",
        )
        assert reconnected_run["connection"] == "online"
        reconnected_remote_run = _wait_until(
            lambda: _remote_run_status_is(
                client, recovery_run_id, "running", connection="online"
            ),
            description="active Remote Run after Node reconnect",
        )
        assert reconnected_remote_run["workspace_id"] == recovery_run["workspace_id"]
        assert execution_record.read_text(encoding="utf-8") == "once\n"
        stopped_remote = client.post(
            f"/remote-runs/{recovery_run_id}/stop",
            data={"_csrf": csrf},
        )
        assert stopped_remote.status_code == 303
        stopped_remote_run = _wait_until(
            lambda: _remote_run_status_is(client, recovery_run_id, "stopped"),
            description="Node Remote Run interrupt",
        )
        assert stopped_remote_run["workspace_id"] == recovery_run["workspace_id"]
        assert execution_record.read_text(encoding="utf-8") == "once\n"
        interrupted = client.post(
            f"/file-runs/{stubborn_run_id}/stop",
            data={"_csrf": csrf, "return_to": "terminal"},
        )
        assert interrupted.status_code == 303
        killed = client.post(
            f"/file-runs/{stubborn_run_id}/kill",
            data={"_csrf": csrf, "return_to": "terminal"},
        )
        assert killed.status_code == 303
        _wait_until(
            lambda: _file_run_status_is(client, stubborn_run_id, "stopped"),
            description="forced Node File Run stop",
        )
        events = store.list_activity_events(limit=100)
        matching_events = [
            event
            for event in events
            if event.get("subject_type") == "file_run"
            and event.get("subject_id")
            in {interactive_run_id, failed_run_id, stubborn_run_id}
        ]
        assert [event["kind"] for event in reversed(matching_events)] == [
            "file_run.completed",
            "file_run.failed",
            "file_run.stopped",
        ]
        assert len({event["subject_id"] for event in matching_events}) == 3
        remote_events = [
            event
            for event in store.list_activity_events(limit=100)
            if event.get("subject_type") == "remote_run"
            and event.get("subject_id") == recovery_run_id
        ]
        assert [event["kind"] for event in remote_events] == ["remote_run.stopped"]
        deleted_recovery = client.post(
            f"/remote-runs/{recovery_run_id}/delete",
            data={"_csrf": csrf},
        )
        assert deleted_recovery.status_code == 303
        assert store.get_remote_run(recovery_run_id) is None
        assert not (managed_root / recovery_run_id).exists()
        assert len(store.list_computers()) == 1
        assert len(store.list_workspaces_for_computer(node_id)) == 1
        assert len(store.list_terminals(workspace_id)) == 2

        revoked = client.post(
            f"/computers/{node_id}/revoke",
            data={"_csrf": csrf},
        )
        assert revoked.status_code == 303
        _wait_until(
            lambda: _node_status_is(client, node_id, "Revoked"),
            description="Node revocation",
        )
        time.sleep(1.5)
        assert not _node_status_is(client, node_id, "Online")
        computer = store.get_computer(node_id)
        assert computer is not None
        assert computer["connection_method"] == "node"
        assert computer["node_revoked_at"]
        assert not computer["host"]
        assert not computer["username"]
        assert len(store.list_computers()) == 1
        assert len(store.list_workspaces_for_computer(node_id)) == 1
        assert len(store.list_terminals(workspace_id)) == 2

        token = (core_state / "access-token").read_text(encoding="utf-8").strip()
        assert hashlib.sha256(f"termroom-csrf:{token}".encode()).hexdigest() == csrf
    finally:
        if client is not None:
            client.close()
        _stop_process(pair)
        _stop_process(node)
        _stop_process(core)
        if session_name:
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                check=False,
                capture_output=True,
            )
        for handle in handles:
            handle.close()


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_real_node_workspace_source_streams_to_a_separate_node(tmp_path: Path) -> None:
    core_root = tmp_path / "core-root"
    core_state = tmp_path / "core-state"
    source_root = tmp_path / "source-node-root"
    target_root = tmp_path / "target-node-root"
    source_state = tmp_path / "source-node-state"
    target_state = tmp_path / "target-node-state"
    core_root.mkdir()
    source_root.mkdir()
    target_root.mkdir()
    large_size = 2 * 1024 * 1024 + 17
    (source_root / "payload.txt").write_text("from-source-node\n", encoding="utf-8")
    (source_root / "large.bin").write_bytes(b"z" * large_size)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment["TERMROOM_PASSWORD"] = "node-source-e2e-password"
    environment["TERMROOM_LOCALE"] = "en"
    core: subprocess.Popen[bytes] | None = None
    node_processes: list[subprocess.Popen[bytes]] = []
    pair_processes: list[subprocess.Popen[bytes]] = []
    handles: list[Any] = []
    client: httpx.Client | None = None
    workspace_session = ""

    def pair_and_start(
        csrf: str, state_dir: Path, allowed_root: Path, name: str
    ) -> str:
        assert client is not None
        created = client.post("/computers/node/pair", data={"_csrf": csrf})
        assert created.status_code == 201
        code_match = re.search(r'class="node-pairing-code">([^<]+)<', created.text)
        pairing_match = re.search(
            r'name="pairing_id" value="([a-f0-9]{32})"', created.text
        )
        assert code_match is not None and pairing_match is not None
        pair_process, pair_handle = _start_process(
            [
                *_node_prefix(state_dir),
                "pair",
                "--core",
                base_url,
                "--code",
                code_match.group(1),
                "--allow-root",
                str(allowed_root),
                "--name",
                name,
                "--timeout",
                "20",
            ],
            environment,
            tmp_path / f"{name}-pair.log",
        )
        pair_processes.append(pair_process)
        handles.append(pair_handle)
        review_url = f"/computers/node/pair?pairing_id={pairing_match.group(1)}"
        review = _wait_until(
            lambda: (
                response
                if (response := client.get(review_url)).status_code == 200
                and "SHA256:" in response.text
                else None
            ),
            description=f"{name} fingerprint submission",
        )
        enrollment_match = re.search(
            r'/computers/node/pair/([a-f0-9]{32})/approve', review.text
        )
        assert enrollment_match is not None
        approved = client.post(
            f"/computers/node/pair/{enrollment_match.group(1)}/approve",
            data={"_csrf": csrf},
        )
        assert approved.status_code == 303
        node_id = approved.headers["location"].split("/")[2].split("?")[0]
        assert pair_process.wait(timeout=10) == 0
        process, process_handle = _start_process(
            _node_prefix(state_dir),
            environment,
            tmp_path / f"{name}-node.log",
        )
        node_processes.append(process)
        handles.append(process_handle)
        _wait_until(
            lambda: _node_status_is(client, node_id, "Online"),
            description=f"{name} control connection",
        )
        return node_id

    try:
        core, handle = _start_process(
            _core_command(core_root, core_state, port),
            environment,
            tmp_path / "core.log",
        )
        handles.append(handle)
        _wait_until(
            lambda: httpx.get(f"{base_url}/health", timeout=1).status_code == 200,
            description="Core startup",
        )
        client, csrf = _login(base_url, "node-source-e2e-password")
        source_node_id = pair_and_start(
            csrf, source_state, source_root, "Source-E2E-Node"
        )
        target_node_id = pair_and_start(
            csrf, target_state, target_root, "Target-E2E-Node"
        )
        opened = client.post(
            f"/computers/{source_node_id}/workspaces",
            data={
                "_csrf": csrf,
                "path": str(source_root),
                "display_name": "Node Source Project",
            },
        )
        assert opened.status_code == 303
        workspace_id = opened.headers["location"].split("/")[2]
        store = StateStore(core_state / "termroom.sqlite3")
        workspace = store.get_workspace(workspace_id)
        assert workspace is not None
        workspace_session = str(workspace["tmux_session"])
        form = client.get(
            "/remote-runs/new",
            params={"source_workspace_id": workspace_id, "source_path": "."},
        )
        assert form.status_code == 200
        assert 'name="source_workspace_id"' in form.text
        assert "Node Source Project" in form.text

        same_node_run_id = _create_remote_run(
            client,
            csrf,
            source_kind="workspace",
            target_id=source_node_id,
            command="cat payload.txt > copied.txt; wc -c < large.bin > size.txt",
            source_workspace_id=workspace_id,
            source_path=".",
        )
        separate_node_run_id = _create_remote_run(
            client,
            csrf,
            source_kind="workspace",
            target_id=target_node_id,
            command="cat payload.txt > copied.txt; wc -c < large.bin > size.txt",
            source_workspace_id=workspace_id,
            source_path=".",
        )
        for run_id, description in (
            (same_node_run_id, "same-Node Source run"),
            (separate_node_run_id, "separate-Node Source run"),
        ):
            result = _wait_until(
                lambda value=run_id: _remote_run_status_is(client, value, "finished"),
                timeout=30,
                description=description,
            )
            assert result["exit_code"] == 0

        source_run_root = Path(
            json.loads((source_state / "node.json").read_text(encoding="utf-8"))[
                "run_root"
            ]
        )
        target_run_root = Path(
            json.loads((target_state / "node.json").read_text(encoding="utf-8"))[
                "run_root"
            ]
        )
        for run_root, run_id in (
            (source_run_root, same_node_run_id),
            (target_run_root, separate_node_run_id),
        ):
            work = run_root / run_id / "work"
            assert (work / "copied.txt").read_text(encoding="utf-8") == "from-source-node\n"
            assert int((work / "size.txt").read_text(encoding="utf-8").strip()) == large_size
            deleted = client.post(
                f"/remote-runs/{run_id}/delete", data={"_csrf": csrf}
            )
            assert deleted.status_code == 303
            assert not (run_root / run_id).exists()

        assert (source_root / "payload.txt").read_text(encoding="utf-8") == (
            "from-source-node\n"
        )
        assert (source_root / "large.bin").stat().st_size == large_size
        assert not (source_root / "copied.txt").exists()
        completed_events = [
            event
            for event in store.list_activity_events(limit=100)
            if event.get("subject_id") in {same_node_run_id, separate_node_run_id}
        ]
        assert {event["kind"] for event in completed_events} == {
            "remote_run.completed"
        }
        assert len(completed_events) == 2
    finally:
        if client is not None:
            client.close()
        for process in pair_processes:
            _stop_process(process)
        for process in node_processes:
            _stop_process(process)
        _stop_process(core)
        if workspace_session:
            subprocess.run(
                ["tmux", "kill-session", "-t", workspace_session],
                check=False,
                capture_output=True,
            )
        for handle in handles:
            handle.close()


@pytest.mark.skipif(
    not all(shutil.which(command) for command in ("ssh", "ssh-keygen", "tmux"))
    or not Path("/usr/sbin/sshd").is_file(),
    reason="OpenSSH server/client and tmux are required",
)
def test_real_node_workspace_source_streams_to_ssh_target(tmp_path: Path) -> None:
    core_root = tmp_path / "core-root"
    core_state = tmp_path / "core-state"
    node_root = tmp_path / "node-root"
    node_state = tmp_path / "node-state"
    ssh_run_root = tmp_path / "ssh-runs"
    core_root.mkdir()
    node_root.mkdir()
    payload = b"node-to-ssh\n" * 100_000
    (node_root / "input.bin").write_bytes(payload)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment["TERMROOM_PASSWORD"] = "node-ssh-e2e-password"
    environment["TERMROOM_LOCALE"] = "en"
    core: subprocess.Popen[bytes] | None = None
    node: subprocess.Popen[bytes] | None = None
    pair: subprocess.Popen[bytes] | None = None
    handles: list[Any] = []
    client: httpx.Client | None = None
    workspace_session = ""

    with _test_sshd(tmp_path) as server:
        try:
            core, handle = _start_process(
                _core_command(core_root, core_state, port),
                environment,
                tmp_path / "core.log",
            )
            handles.append(handle)
            _wait_until(
                lambda: httpx.get(f"{base_url}/health", timeout=1).status_code == 200,
                description="Core startup",
            )
            client, csrf = _login(base_url, "node-ssh-e2e-password")
            created = client.post("/computers/node/pair", data={"_csrf": csrf})
            assert created.status_code == 201
            code_match = re.search(r'class="node-pairing-code">([^<]+)<', created.text)
            pairing_match = re.search(
                r'name="pairing_id" value="([a-f0-9]{32})"', created.text
            )
            assert code_match is not None and pairing_match is not None
            pair, handle = _start_process(
                [
                    *_node_prefix(node_state),
                    "pair",
                    "--core",
                    base_url,
                    "--code",
                    code_match.group(1),
                    "--allow-root",
                    str(node_root),
                    "--name",
                    "Node-SSH-Source",
                    "--timeout",
                    "20",
                ],
                environment,
                tmp_path / "pair.log",
            )
            handles.append(handle)
            review_url = f"/computers/node/pair?pairing_id={pairing_match.group(1)}"
            review = _wait_until(
                lambda: (
                    response
                    if (response := client.get(review_url)).status_code == 200
                    and "SHA256:" in response.text
                    else None
                ),
                description="Node fingerprint submission",
            )
            enrollment_match = re.search(
                r'/computers/node/pair/([a-f0-9]{32})/approve', review.text
            )
            assert enrollment_match is not None
            approved = client.post(
                f"/computers/node/pair/{enrollment_match.group(1)}/approve",
                data={"_csrf": csrf},
            )
            assert approved.status_code == 303
            node_id = approved.headers["location"].split("/")[2].split("?")[0]
            assert pair.wait(timeout=10) == 0
            node, handle = _start_process(
                _node_prefix(node_state), environment, tmp_path / "node.log"
            )
            handles.append(handle)
            _wait_until(
                lambda: _node_status_is(client, node_id, "Online"),
                description="Node control connection",
            )

            store = StateStore(core_state / "termroom.sqlite3")
            backend = SSHBackend(store, core_state)
            host_key = backend.probe_host_key("127.0.0.1", int(server["port"]))
            ssh_computer = store.create_computer(
                name="SSH Target",
                ssh_alias="",
                host="127.0.0.1",
                port=int(server["port"]),
                username=str(server["username"]),
                identity_file=str(server["client_key"]),
                host_key_type=host_key["host_key_type"],
                host_key_data=host_key["host_key_data"],
                host_fingerprint=host_key["host_fingerprint"],
            )
            store.update_computer_run_base(str(ssh_computer["id"]), str(ssh_run_root))
            registered = store.get_computer(str(ssh_computer["id"]))
            assert registered is not None
            backend.remember_host_key(registered)

            opened = client.post(
                f"/computers/{node_id}/workspaces",
                data={
                    "_csrf": csrf,
                    "path": str(node_root),
                    "display_name": "Node SSH Source",
                },
            )
            assert opened.status_code == 303
            workspace_id = opened.headers["location"].split("/")[2]
            workspace = store.get_workspace(workspace_id)
            assert workspace is not None
            workspace_session = str(workspace["tmux_session"])
            run_id = _create_remote_run(
                client,
                csrf,
                source_kind="workspace",
                target_id=str(registered["id"]),
                command="wc -c < input.bin > byte-count.txt; "
                "cp input.bin copied.bin",
                source_workspace_id=workspace_id,
                source_path=".",
            )
            result = _wait_until(
                lambda: _remote_run_status_is(client, run_id, "finished"),
                timeout=30,
                description="Node Source to SSH Target Remote Run",
            )
            assert result["exit_code"] == 0
            target_work = ssh_run_root / run_id / "work"
            assert int(
                (target_work / "byte-count.txt").read_text(encoding="utf-8").strip()
            ) == len(payload)
            assert (target_work / "copied.bin").read_bytes() == payload
            assert (node_root / "input.bin").read_bytes() == payload
            assert not (node_root / "copied.bin").exists()
            event = next(
                item
                for item in store.list_activity_events(limit=50)
                if item.get("subject_id") == run_id
            )
            assert event["kind"] == "remote_run.completed"
            deleted = client.post(
                f"/remote-runs/{run_id}/delete", data={"_csrf": csrf}
            )
            assert deleted.status_code == 303
            assert not (ssh_run_root / run_id).exists()
        finally:
            if client is not None:
                client.close()
            _stop_process(pair)
            _stop_process(node)
            _stop_process(core)
            if workspace_session:
                subprocess.run(
                    ["tmux", "kill-session", "-t", workspace_session],
                    check=False,
                    capture_output=True,
                )
            for handle in handles:
                handle.close()


@pytest.mark.skipif(
    os.environ.get("TERMROOM_SYSTEMD_USER_E2E") != "1",
    reason="set TERMROOM_SYSTEMD_USER_E2E=1 for the real systemd user-service test",
)
def test_real_node_systemd_service_preserves_identity_tmux_and_reconnects(
    tmp_path: Path,
) -> None:
    if shutil.which("systemctl") is None or shutil.which("tmux") is None:
        pytest.skip("systemctl and tmux are required")
    manager = NodeServiceManager(tmp_path / "preflight-state")
    try:
        existing = manager.status()
    except Exception as exc:
        pytest.skip(f"systemd user manager is unavailable: {exc}")
    if existing.installed:
        pytest.skip("an existing Termroom Node user service must be preserved")

    core_root = tmp_path / "core-root"
    node_root = tmp_path / "node-root"
    core_state = tmp_path / "core-state"
    node_state = tmp_path / "node-state"
    core_root.mkdir()
    node_root.mkdir()
    (node_root / "interactive.py").write_text(
        "from pathlib import Path\n"
        "value = input('service-value: ')\n"
        "Path('service-result.txt').write_text(value)\n"
        "print('SERVICE_RUN:' + value)\n",
        encoding="utf-8",
    )
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment["TERMROOM_PASSWORD"] = "node-service-e2e-password"
    environment["TERMROOM_LOCALE"] = "en"
    core: subprocess.Popen[bytes] | None = None
    pair: subprocess.Popen[bytes] | None = None
    handles: list[Any] = []
    client: httpx.Client | None = None
    session_name = ""
    service_installed = False
    manager_tmux_tmpdir: str | None = None
    manager_environment_changed = False

    def systemctl(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["systemctl", "--user", *arguments],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def main_pid() -> int:
        result = systemctl("show", NODE_SERVICE_UNIT_NAME, "--property=MainPID", "--value")
        return int(result.stdout.strip() or "0") if result.returncode == 0 else 0

    try:
        manager_environment = systemctl("show-environment")
        assert manager_environment.returncode == 0, manager_environment.stderr
        manager_tmux_tmpdir = next(
            (
                line.removeprefix("TMUX_TMPDIR=")
                for line in manager_environment.stdout.splitlines()
                if line.startswith("TMUX_TMPDIR=")
            ),
            None,
        )
        test_tmux_tmpdir = os.environ.get("TMUX_TMPDIR")
        assert test_tmux_tmpdir
        configured_tmux = systemctl(
            "set-environment", f"TMUX_TMPDIR={test_tmux_tmpdir}"
        )
        assert configured_tmux.returncode == 0, configured_tmux.stderr
        manager_environment_changed = True

        core, handle = _start_process(
            _core_command(core_root, core_state, port),
            environment,
            tmp_path / "core.log",
        )
        handles.append(handle)
        _wait_until(
            lambda: httpx.get(f"{base_url}/health", timeout=1).status_code == 200,
            description="Core startup",
        )
        client, csrf = _login(base_url, "node-service-e2e-password")
        created = client.post("/computers/node/pair", data={"_csrf": csrf})
        assert created.status_code == 201
        code_match = re.search(r'class="node-pairing-code">([^<]+)<', created.text)
        pairing_match = re.search(
            r'name="pairing_id" value="([a-f0-9]{32})"', created.text
        )
        assert code_match is not None and pairing_match is not None
        pair, handle = _start_process(
            [
                *_node_prefix(node_state),
                "pair",
                "--core",
                base_url,
                "--code",
                code_match.group(1),
                "--allow-root",
                str(node_root),
                "--name",
                "Service E2E Node",
                "--timeout",
                "20",
            ],
            environment,
            tmp_path / "pair.log",
        )
        handles.append(handle)
        review_url = f"/computers/node/pair?pairing_id={pairing_match.group(1)}"
        review = _wait_until(
            lambda: (
                response
                if (response := client.get(review_url)).status_code == 200
                and "SHA256:" in response.text
                else None
            ),
            description="Node fingerprint submission",
        )
        enrollment_match = re.search(
            r'/computers/node/pair/([a-f0-9]{32})/approve', review.text
        )
        assert enrollment_match is not None
        approved = client.post(
            f"/computers/node/pair/{enrollment_match.group(1)}/approve",
            data={"_csrf": csrf},
        )
        assert approved.status_code == 303
        node_id = approved.headers["location"].split("/")[2].split("?")[0]
        assert pair.wait(timeout=10) == 0
        identity_before = (node_state / "node-key.pem").read_bytes()
        config_before = (node_state / "node.json").read_bytes()

        installed = subprocess.run(
            [*_node_prefix(node_state), "install-service"],
            capture_output=True,
            text=True,
            timeout=20,
            env=environment,
            check=False,
        )
        assert installed.returncode == 0, installed.stderr
        service_installed = True
        assert "Installed: yes" in installed.stdout
        _wait_until(
            lambda: _node_status_is(client, node_id, "Online"),
            timeout=20,
            description="systemd Node connection",
        )
        first_pid = _wait_until(
            lambda: main_pid() or None,
            description="systemd Node main process",
        )

        opened = client.post(
            f"/computers/{node_id}/workspaces",
            data={
                "_csrf": csrf,
                "path": str(node_root),
                "display_name": "Service Project",
            },
        )
        assert opened.status_code == 303
        workspace_id = opened.headers["location"].split("/")[2]
        terminal_page = client.get(f"/w/{workspace_id}/terminal")
        terminal_match = re.search(
            r'data-terminal-id="([a-f0-9]{32})"', terminal_page.text
        )
        assert terminal_match is not None
        terminal_id = terminal_match.group(1)
        store = StateStore(core_state / "termroom.sqlite3")
        workspace = store.get_workspace(workspace_id)
        assert workspace is not None
        session_name = str(workspace["tmux_session"])

        run_id, run_terminal_id = _start_file_run(
            client,
            csrf,
            workspace_id,
            "interactive.py",
            (node_root / "interactive.py").read_text(encoding="utf-8"),
        )
        _wait_until(
            lambda: _file_run_status_is(client, run_id, "running"),
            description="service-managed Node File Run",
        )
        assert subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
            check=False,
        ).returncode == 0

        stopped = systemctl("stop", NODE_SERVICE_UNIT_NAME)
        assert stopped.returncode == 0, stopped.stderr
        _wait_until(
            lambda: _node_status_is(client, node_id, "Offline"),
            description="Node service stop",
        )
        assert subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
            check=False,
        ).returncode == 0
        offline_run = _wait_until(
            lambda: _file_run_status_is(client, run_id, "running"),
            description="File Run preserved during service stop",
        )
        assert offline_run["connection"] == "offline"

        started = systemctl("start", NODE_SERVICE_UNIT_NAME)
        assert started.returncode == 0, started.stderr
        _wait_until(
            lambda: _node_status_is(client, node_id, "Online"),
            timeout=20,
            description="Node service start reconnect",
        )
        assert main_pid() != first_pid
        restored_run = _wait_until(
            lambda: _file_run_status_is(client, run_id, "running"),
            description="File Run after service reconnect",
        )
        assert restored_run["connection"] == "online"

        session_cookie = client.cookies.get("termroom_session")
        assert session_cookie
        with websocket_connect(
            f"ws://127.0.0.1:{port}/ws/terminal/{run_terminal_id}",
            origin=base_url,
            additional_headers={"Cookie": f"termroom_session={session_cookie}"},
            open_timeout=5,
        ) as websocket:
            websocket.send(json.dumps({"kind": "claim"}))
            websocket.send(json.dumps({"kind": "input", "data": "service-reconnect\r"}))
            output = ""
            deadline = time.monotonic() + 8
            while "SERVICE_RUN:service-reconnect" not in output and time.monotonic() < deadline:
                output += str(websocket.recv(timeout=2))
            assert "SERVICE_RUN:service-reconnect" in output
        _wait_until(
            lambda: _file_run_status_is(client, run_id, "finished"),
            description="File Run completion after service reconnect",
        )

        before_crash = main_pid()
        killed = systemctl(
            "kill", "--kill-whom=main", "--signal=KILL", NODE_SERVICE_UNIT_NAME
        )
        assert killed.returncode == 0, killed.stderr
        _wait_until(
            lambda: (pid if (pid := main_pid()) and pid != before_crash else None),
            timeout=20,
            description="bounded systemd process restart",
        )
        _wait_until(
            lambda: _node_status_is(client, node_id, "Online"),
            timeout=20,
            description="Node reconnect after process failure",
        )

        _stop_process(core)
        core = None
        client.close()
        client = None
        core, handle = _start_process(
            _core_command(core_root, core_state, port),
            environment,
            tmp_path / "core-restart.log",
        )
        handles.append(handle)
        _wait_until(
            lambda: httpx.get(f"{base_url}/health", timeout=1).status_code == 200,
            description="Core restart",
        )
        client, csrf = _login(base_url, "node-service-e2e-password")
        _wait_until(
            lambda: _node_status_is(client, node_id, "Online"),
            timeout=20,
            description="service reconnect after Core restart",
        )
        assert store.get_workspace(workspace_id) is not None
        assert len(store.list_computers()) == 1
        assert len(store.list_workspaces_for_computer(node_id)) == 1
        assert len(store.list_terminals(workspace_id)) == 2
        scrollback = client.get(f"/w/{workspace_id}/terminal/{terminal_id}/scrollback")
        assert scrollback.status_code == 200

        removed = subprocess.run(
            [*_node_prefix(node_state), "uninstall-service"],
            capture_output=True,
            text=True,
            timeout=20,
            env=environment,
            check=False,
        )
        assert removed.returncode == 0, removed.stderr
        service_installed = False
        assert (node_state / "node-key.pem").read_bytes() == identity_before
        assert (node_state / "node.json").read_bytes() == config_before

        reinstalled = subprocess.run(
            [*_node_prefix(node_state), "install-service"],
            capture_output=True,
            text=True,
            timeout=20,
            env=environment,
            check=False,
        )
        assert reinstalled.returncode == 0, reinstalled.stderr
        service_installed = True
        _wait_until(
            lambda: _node_status_is(client, node_id, "Online"),
            timeout=20,
            description="same identity after service reinstall",
        )
        assert len(store.list_computers()) == 1
        assert len(store.list_workspaces_for_computer(node_id)) == 1

        revoked = client.post(f"/computers/{node_id}/revoke", data={"_csrf": csrf})
        assert revoked.status_code == 303
        _wait_until(
            lambda: systemctl(
                "show", NODE_SERVICE_UNIT_NAME, "--property=ActiveState", "--value"
            ).stdout.strip()
            in {"failed", "inactive"},
            timeout=20,
            description="revoked service permanent stop",
        )
        restarts = systemctl(
            "show", NODE_SERVICE_UNIT_NAME, "--property=NRestarts", "--value"
        ).stdout.strip()
        time.sleep(6)
        assert systemctl(
            "show", NODE_SERVICE_UNIT_NAME, "--property=NRestarts", "--value"
        ).stdout.strip() == restarts
        status = subprocess.run(
            [*_node_prefix(node_state), "status"],
            capture_output=True,
            text=True,
            timeout=20,
            env=environment,
            check=False,
        )
        assert status.returncode == 0, status.stderr
        assert "Core:      error" in status.stdout
        assert "Last error: identity_revoked" in status.stdout
    finally:
        if service_installed or manager.unit_path.exists():
            subprocess.run(
                [*_node_prefix(node_state), "uninstall-service"],
                capture_output=True,
                text=True,
                timeout=20,
                env=environment,
                check=False,
            )
        if client is not None:
            client.close()
        _stop_process(pair)
        _stop_process(core)
        if session_name:
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                check=False,
                capture_output=True,
            )
        if manager_environment_changed:
            if manager_tmux_tmpdir is None:
                systemctl("unset-environment", "TMUX_TMPDIR")
            else:
                systemctl("set-environment", f"TMUX_TMPDIR={manager_tmux_tmpdir}")
        for handle in handles:
            handle.close()
