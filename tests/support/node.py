"""Private real CLI Core+Node pair and lifecycle for integration tests."""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import time
import uuid
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from termroom.db import StateStore
from tests.support.core import free_loopback_port


def wait_until(predicate: Callable[[], Any], *, description: str, timeout: float = 20) -> Any:
    deadline = time.monotonic() + timeout
    latest: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, httpx.HTTPError) as exc:
            latest = exc
        time.sleep(0.1)
    raise AssertionError(
        f"Real Node {description} timed out" + (f": {latest}" if latest else "")
    )


def start_cli(
    argv: list[str], environment: dict[str, str], log_path: Path,
) -> tuple[subprocess.Popen[bytes], Any]:
    log = log_path.open("ab", buffering=0)
    process = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=log,
        stderr=subprocess.STDOUT,
        cwd=Path(__file__).resolve().parents[2], env=environment,
        start_new_session=True,
    )
    return process, log


def stop_cli(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def core_command(root: Path, state: Path, port: int) -> list[str]:
    command = [
        sys.executable, "-m", "termroom.cli", str(root),
        "--foreground", "--no-open", "--host", "127.0.0.1",
        "--port", str(port), "--state-dir", str(state),
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        command.append("--allow-root")
    return command


def node_prefix(state: Path) -> list[str]:
    command = [
        sys.executable, "-m", "termroom.cli", "node", "--state-dir", str(state),
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        command.append("--allow-root-user")
    return command


@dataclass
class ConnectedRealNode:
    """Test-owned Core, paired outbound Node, workspace and exact cleanup."""

    tmp_path: Path
    port: int = field(init=False)
    base_url: str = field(init=False)
    core_root: Path = field(init=False)
    core_state: Path = field(init=False)
    node_root: Path = field(init=False)
    archive_root: Path = field(init=False)
    node_state: Path = field(init=False)
    environment: dict[str, str] = field(init=False)
    client: httpx.Client = field(init=False)
    csrf: str = field(init=False)
    node_id: str = field(init=False)
    source_workspace_id: str = field(init=False)
    workspace_id: str = field(init=False)
    terminal_id: str = field(init=False)
    managed_root: Path = field(init=False)
    store: StateStore = field(init=False)
    core: subprocess.Popen[bytes] | None = field(default=None, init=False)
    node: subprocess.Popen[bytes] | None = field(default=None, init=False)
    pair: subprocess.Popen[bytes] | None = field(default=None, init=False)
    handles: list[Any] = field(default_factory=list, init=False)
    sessions: set[str] = field(default_factory=set, init=False)

    def _spawn(self, argv: list[str], name: str) -> subprocess.Popen[bytes]:
        process, handle = start_cli(argv, self.environment, self.tmp_path / name)
        self.handles.append(handle)
        return process

    def _login(self) -> None:
        client = httpx.Client(base_url=self.base_url, timeout=10)
        try:
            logged = client.post(
                "/login", data={"password": "isolated-node-e2e-password"},
            )
            assert logged.status_code == 303, logged.text
            setup = client.get("/computers/new")
            assert setup.status_code == 200
            csrf = re.search(r'name="_csrf" value="([a-f0-9]{64})"', setup.text)
            assert csrf
            self.client = client
            self.csrf = csrf.group(1)
        except BaseException:
            client.close()
            raise

    def core_healthy(self) -> bool:
        if self.core is None or self.core.poll() is not None:
            status = None if self.core is None else self.core.returncode
            raise AssertionError(
                f"Private test Core exited before readiness: status={status}; "
                f"log={self.tmp_path / 'core.log'}"
            )
        return httpx.get(f"{self.base_url}/health", timeout=1).status_code == 200

    def node_connected(self) -> bool:
        if self.node is None or self.node.poll() is not None:
            status = None if self.node is None else self.node.returncode
            raise AssertionError(
                f"Private test Node exited before readiness: status={status}; "
                f"log={self.tmp_path / 'node.log'}"
            )
        return self.available()

    def available(self) -> bool:
        response = self.client.get(f"/computers/{self.node_id}")
        return (
            response.status_code == 200
            and 'class="state-chip remote available"' in response.text
        )

    def start(self) -> None:
        self.port = free_loopback_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.core_root = self.tmp_path / "core-root"
        self.node_root = self.tmp_path / "node-root"
        self.core_state = self.tmp_path / "core-state"
        self.archive_root = self.node_root / "container"
        self.node_state = self.archive_root / ".local/state/termroom/node"
        self.core_root.mkdir()
        self.node_root.mkdir()
        self.archive_root.mkdir()
        (self.archive_root / "archive-visible.txt").write_text(
            "ordinary archive sibling\n", encoding="utf-8",
        )
        source = self.core_root / "source-project"
        source.mkdir()
        (source / "source.txt").write_text("workspace-source\n", encoding="utf-8")
        fake_bin = self.tmp_path / "fake-bin"
        fake_bin.mkdir()
        git = fake_bin / "git"
        git.write_text(
            "#!/bin/sh\n"
            "if test \"$1\" = '-C'; then\n"
            "  printf 'abcdef0123456789abcdef0123456789abcdef01\\n'\n"
            "  exit 0\n"
            "fi\n"
            "last=''\n"
            "for value in \"$@\"; do last=$value; done\n"
            "mkdir -p -- \"$last\"\n"
            "printf 'git-source\\n' > \"$last/source.txt\"\n",
            encoding="utf-8",
        )
        git.chmod(0o700)
        files = {
            "hello.txt": "before\n",
            "interactive.py": (
                "from pathlib import Path\n"
                "value = input('node-value: ')\n"
                "Path('interactive-result.txt').write_text(value)\n"
                "print('NODE_FILE_RUN:' + value)\n"
            ),
            "fail.py": "raise SystemExit(7)\n",
            "qa-control.py": "print('T43_NODE_API_RUN_OK')\n",
            "stubborn.py": (
                "import signal, time\n"
                "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
                "while True: time.sleep(0.1)\n"
            ),
        }
        for name, content in files.items():
            (self.node_root / name).write_text(content, encoding="utf-8")
        (self.node_root / "large.txt").write_bytes(b"x" * (1024 * 1024))
        self.environment = os.environ.copy()
        self.environment.update({
            "TERMROOM_PASSWORD": "isolated-node-e2e-password",
            "TERMROOM_LOCALE": "en",
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        })
        self.core = self._spawn(
            core_command(self.core_root, self.core_state, self.port), "core.log",
        )
        wait_until(
            self.core_healthy,
            description="Core startup",
            timeout=30,
        )
        self._login()
        created = self.client.post(
            "/computers/node/pair", data={"_csrf": self.csrf},
        )
        assert created.status_code == 201, created.text
        code = re.search(r'class="node-pairing-code">([^<]+)<', created.text)
        pairing = re.search(
            r'name="pairing_id" value="([a-f0-9]{32})"', created.text,
        )
        assert code and pairing
        self.pair = self._spawn(
            [
                *node_prefix(self.node_state), "pair", "--core", self.base_url,
                "--code", code.group(1), "--allow-root", str(self.node_root),
                "--name", "E2E Node", "--timeout", "20",
            ],
            "pair.log",
        )
        review_url = f"/computers/node/pair?pairing_id={pairing.group(1)}"
        review = wait_until(
            lambda: (
                response if (
                    (response := self.client.get(review_url)).status_code == 200
                    and "SHA256:" in response.text
                ) else None
            ),
            description="Node fingerprint submission",
        )
        enrollment = re.search(
            r'/computers/node/pair/([a-f0-9]{32})/approve', review.text,
        )
        assert enrollment
        approved = self.client.post(
            f"/computers/node/pair/{enrollment.group(1)}/approve",
            data={"_csrf": self.csrf},
        )
        assert approved.status_code == 303, approved.text
        self.node_id = approved.headers["location"].split("/")[2].split("?")[0]
        assert self.pair.wait(timeout=10) == 0
        self.node = self._spawn(node_prefix(self.node_state), "node.log")
        wait_until(
            self.node_connected, description="Node connected", timeout=25,
        )
        self.store = StateStore(self.core_state / "termroom.sqlite3")
        target = self.client.get("/remote-runs/new")
        assert target.status_code == 200
        assert f'<option value="{self.node_id}"' in target.text
        root = next(
            item for item in self.store.list_local_roots()
            if Path(str(item["path"])) == self.core_root
        )
        source = self.client.post(
            "/api/workspaces",
            data={
                "_csrf": self.csrf, "root_id": root["id"], "path": "source-project",
            },
        )
        assert source.status_code == 303, source.text
        self.source_workspace_id = source.headers["location"].split("/")[2]
        opened = self.client.post(
            f"/computers/{self.node_id}/workspaces",
            data={
                "_csrf": self.csrf, "path": str(self.node_root),
                "display_name": "Node Project",
            },
        )
        assert opened.status_code == 303, opened.text
        self.workspace_id = opened.headers["location"].split("/")[2]
        terminal = self.client.get(f"/w/{self.workspace_id}/terminal")
        assert terminal.status_code == 200, terminal.text
        match = re.search(r'data-terminal-id="([a-f0-9]{32})"', terminal.text)
        assert match
        self.terminal_id = match.group(1)
        config = json.loads(
            (self.node_state / "node.json").read_text(encoding="utf-8")
        )
        self.managed_root = Path(config["run_root"])
        for workspace_id in (self.source_workspace_id, self.workspace_id):
            record = self.store.get_workspace(workspace_id)
            assert record is not None
            self.sessions.add(str(record["tmux_session"]))

    def stop_core(self) -> None:
        stop_cli(self.core)
        self.core = None
        if hasattr(self, "client"):
            self.client.close()

    def restart_core(self) -> None:
        self.stop_core()
        self.core = self._spawn(
            core_command(self.core_root, self.core_state, self.port),
            "core-restart.log",
        )
        wait_until(
            self.core_healthy, description="Core restart", timeout=30,
        )
        self._login()
        wait_until(
            self.node_connected, description="Node reconnect after Core restart",
            timeout=25,
        )

    def stop_node(self) -> None:
        stop_cli(self.node)
        self.node = None

    def restart_node(self) -> None:
        self.stop_node()
        self.node = self._spawn(node_prefix(self.node_state), "node-restart.log")
        wait_until(
            self.node_connected, description="Node process reconnect", timeout=25,
        )

    def close(self) -> None:
        if hasattr(self, "client"):
            self.client.close()
        stop_cli(self.pair)
        stop_cli(self.node)
        stop_cli(self.core)
        for session in self.sessions:
            subprocess.run(
                ["tmux", "kill-session", "-t", session],
                check=False, capture_output=True,
            )
        for handle in self.handles:
            handle.close()


@contextlib.contextmanager
def connected_real_node(tmp_path: Path) -> Iterator[ConnectedRealNode]:
    """No existing credentials, ports, projects or Node processes are reused."""
    node = ConnectedRealNode(tmp_path)
    try:
        node.start()
        yield node
    finally:
        node.close()


# Shared Node-scenario HTTP admission and status polling; actual endpoints.
def _remote_connection_state_is(
    client: httpx.Client, node_id: str, state: str
) -> bool:
    response = client.get(f"/computers/{node_id}")
    return (
        response.status_code == 200
        and f'class="state-chip remote {state}"' in response.text
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
