from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import ssl
import stat
import subprocess
import sys
import time
import uuid
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import httpx
import pytest
from websockets.sync.client import connect as websocket_connect

from termroom import node_remote_runs
from termroom.db import StateStore
from termroom.node_agent import NodeRuntime
from termroom.node_protocol import NODE_REMOTE_RUN_VERSION
from termroom.node_remote_runs import NodeRemoteRunError
from termroom.node_service import NODE_SERVICE_UNIT_NAME, NodeServiceManager
from termroom.ssh_backend import REMOTE_RUN_SESSION_PREFIX, SSHBackend
from tests.support.chrome import open_chrome_page
from tests.support.core import free_loopback_port as _free_port
from tests.support.node import (
    _create_remote_run,
    _file_run_status_is,
    _remote_connection_state_is,
    _remote_run_status_is,
    _start_file_run,
)
from tests.support.node import (
    core_command as _core_command,
)
from tests.support.node import (
    node_prefix as _node_prefix,
)
from tests.support.node import (
    start_cli as _start_process,
)
from tests.support.node import (
    stop_cli as _stop_process,
)
from tests.support.node import (
    wait_until as _wait_until,
)
from tests.support.ssh import start_isolated_sshd as _test_sshd


def _login(base_url: str, password: str) -> tuple[httpx.Client, str]:
    client = httpx.Client(base_url=base_url, timeout=10)
    response = client.post("/login", data={"password": password})
    assert response.status_code == 303
    setup = client.get("/computers/new")
    assert setup.status_code == 200
    match = re.search(r'name="_csrf" value="([a-f0-9]{64})"', setup.text)
    assert match is not None
    return client, match.group(1)




















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
            lambda: _remote_connection_state_is(client, node_id, "available"),
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


@contextlib.contextmanager
def _isolated_chrome_terminal(
    profile: Path, base_url: str, cookie: str, workspace_id: str, terminal_id: str,
) -> Iterator[Callable[[str], Any]]:
    """Open the real Terminal in one isolated Chrome page."""
    with open_chrome_page(profile) as page:
        page.cdp("Page.enable")
        # The initial about:blank tab stays isolated; foreground this page
        # so focus/draft assertions keep testing real browser semantics.
        page.cdp("Page.bringToFront")
        page.cdp("Runtime.enable")
        page.cdp("Network.enable")
        assert page.cdp("Network.setCookie", {
            "name": "termroom_session", "value": cookie,
            "url": f"{base_url}/", "httpOnly": True,
        })["success"]
        page.cdp("Page.navigate", {
            "url": f"{base_url}/w/{workspace_id}/terminal?terminal={terminal_id}",
        })
        _wait_until(
            lambda: page.evaluate(
                "document.readyState === 'complete' && "
                "!!document.querySelector('#terminal .xterm-helper-textarea') && "
                "document.querySelector('.terminal-tabs a.active')"
                f"?.dataset.terminalActivityTab === '{terminal_id}'"
            ),
            description=f"real Chrome terminal {terminal_id} in {workspace_id}",
        )
        yield page.evaluate

@pytest.mark.skipif(
    not all(shutil.which(command) for command in ("tmux", "google-chrome")),
    reason="Real Chrome and a private tmux server are required",
)
def test_real_chrome_reconnect_preserves_open_composer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Core restart must not steal focus or replay an unsent browser draft."""
    from termroom.workspaces import RootManager, WorkspaceManager

    root = tmp_path / "root"
    (root / "qa").mkdir(parents=True)
    state_dir = tmp_path / "state"
    tmux_dir = tmp_path / "tmux"
    tmux_dir.mkdir(mode=0o700)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_dir))
    environment = os.environ.copy()
    environment["TERMROOM_PASSWORD"] = "isolated-chrome-reconnect-password"
    environment["TERMROOM_LOCALE"] = "en"
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    core: subprocess.Popen[bytes] | None = None
    client: httpx.Client | None = None
    handles: list[Any] = []
    try:
        core, handle = _start_process(
            _core_command(root, state_dir, port), environment, tmp_path / "core.log",
        )
        handles.append(handle)
        _wait_until(
            lambda: httpx.get(f"{base_url}/health", timeout=1).status_code == 200,
            description="isolated Chrome Core",
        )
        client, _ = _login(base_url, "isolated-chrome-reconnect-password")
        store = StateStore(state_dir / "termroom.sqlite3")
        workspace = WorkspaceManager(RootManager(root), store).open("qa")
        page = client.get(f"/w/{workspace['id']}/terminal")
        assert page.status_code == 200
        match = re.search(r'data-terminal-id="([a-f0-9]{32})"', page.text)
        assert match is not None
        terminal_id = match.group(1)
        terminal = store.get_terminal(terminal_id)
        assert terminal is not None
        target = f"{workspace['tmux_session']}:{terminal['tmux_window']}"
        cookie = client.cookies.get("termroom_session")
        assert cookie

        with _isolated_chrome_terminal(
            tmp_path / "chrome-profile", base_url, cookie,
            workspace["id"], terminal_id,
        ) as evaluate:
            _wait_until(
                lambda: evaluate(
                    "document.querySelector('#terminal')?.termroomControlsInput === true"
                ),
                description="Chrome initially controls Terminal",
            )
            draft = "echo qa-browser-unsent-no-replay"
            assert evaluate("""(() => {
              document.querySelector('#open-command-editor').click();
              const input = document.querySelector('#command-input');
              input.value = 'echo qa-browser-unsent-no-replay';
              input.dispatchEvent(new Event('input', {bubbles: true}));
              input.focus();
              input.setSelectionRange(6, 15);
              return input.value;
            })()""") == draft
            state_expr = """(() => {
              const input = document.querySelector('#command-input');
              const host = document.querySelector('#terminal');
              return {
                terminalId: host.dataset.terminalId,
                selected: document.querySelector(
                  '.terminal-tabs a.active'
                )?.dataset.terminalActivityTab,
                composerOpen: !document.querySelector(
                  '#terminal-command-editor'
                ).hidden,
                focusId: document.activeElement?.id,
                hasFocus: document.hasFocus(),
                draft: input.value,
                start: input.selectionStart,
                end: input.selectionEnd,
                epoch: host.termroomConnectionEpoch,
                controls: host.termroomControlsInput,
                ready: host.dataset.terminalReady,
                delivery: document.querySelector('#command-delivery')
                  ?.dataset.state || 'idle'
              };
            })()"""
            before = evaluate(state_expr)
            assert before["composerOpen"] and before["focusId"] == "command-input"
            assert before["selected"] == before["terminalId"] == terminal_id
            assert before["hasFocus"] and before["controls"] is True

            _stop_process(core)
            _wait_until(
                lambda: evaluate(
                    "document.querySelector('#terminal')?.dataset.terminalReady"
                ) == "false",
                description="Chrome observes Core disconnect",
            )
            during = evaluate(state_expr)
            assert during["draft"] == draft
            assert during["composerOpen"] and during["focusId"] == "command-input"

            core, handle = _start_process(
                _core_command(root, state_dir, port), environment,
                tmp_path / "core-restarted.log",
            )
            handles.append(handle)
            _wait_until(
                lambda: httpx.get(f"{base_url}/health", timeout=1).status_code == 200,
                description="restarted isolated Core",
            )
            _wait_until(
                lambda: evaluate(
                    "document.querySelector('#terminal')?.dataset.terminalReady"
                    " === 'true' && document.querySelector('#terminal')"
                    "?.termroomControlsInput === true"
                ),
                timeout=20,
                description="Chrome reconnects and regains input control",
            )
            after = evaluate(state_expr)
            assert after["terminalId"] == after["selected"] == terminal_id
            assert after["epoch"] > before["epoch"]
            assert after["draft"] == draft
            assert (after["start"], after["end"]) == (6, 15)
            assert after["composerOpen"] and after["focusId"] == "command-input"
            assert after["hasFocus"] and after["controls"] is True
            assert after["delivery"] == "idle"
            captured = subprocess.check_output(
                ["tmux", "capture-pane", "-p", "-t", target, "-S", "-80"],
                env=environment, text=True, timeout=5,
            )
            assert "qa-browser-unsent-no-replay" not in captured
    finally:
        if client is not None:
            client.close()
        _stop_process(core)
        for handle in handles:
            handle.close()
        # Only this test's TMUX_TMPDIR is used; never touch the user's tmux.
        subprocess.run(
            ["tmux", "kill-server"], env=environment, check=False,
            capture_output=True,
        )



@pytest.mark.skipif(
    not all(shutil.which(command) for command in (
        "tmux", "google-chrome", "ssh", "ssh-keygen",
    )) or not Path("/usr/sbin/sshd").is_file(),
    reason="Real Chrome, tmux and isolated OpenSSH server are required",
)
def test_real_chrome_a_stays_unchanged_while_ssh_b_operates(tmp_path: Path) -> None:
    """Concurrent Chrome A and scoped SSH B use the same temporary Core."""
    from termroom.workspaces import RootManager, WorkspaceManager

    root = tmp_path / "core-root"
    remote_root = tmp_path / "ssh-project"
    state_dir = tmp_path / "core-state"
    (root / "a").mkdir(parents=True)
    (root / "a" / "a.txt").write_text("browser-a\n", encoding="utf-8")
    remote_root.mkdir()
    (remote_root / "qa.txt").write_text("ssh-before\n", encoding="utf-8")
    (remote_root / "qa.py").write_text(
        "print('T43_SSH_CHROME_REAL_OUTPUT')\n", encoding="utf-8",
    )
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment["TERMROOM_PASSWORD"] = "isolated-ssh-chrome-password"
    environment["TERMROOM_LOCALE"] = "en"
    core: subprocess.Popen[bytes] | None = None
    client: httpx.Client | None = None
    backend: SSHBackend | None = None
    computer: dict[str, Any] | None = None
    a_session = ""
    b_session = ""
    handles: list[Any] = []
    chrome_stack = contextlib.ExitStack()

    with _test_sshd(tmp_path) as server:
        try:
            core, handle = _start_process(
                _core_command(root, state_dir, port),
                environment,
                tmp_path / "core.log",
            )
            handles.append(handle)
            _wait_until(
                lambda: httpx.get(f"{base_url}/health", timeout=1).status_code == 200,
                description="isolated SSH+Chrome Core",
            )
            client, csrf = _login(base_url, "isolated-ssh-chrome-password")
            store = StateStore(state_dir / "termroom.sqlite3")
            manager = WorkspaceManager(RootManager(root), store)
            a_workspace = manager.open("a")
            a_session = str(a_workspace["tmux_session"])

            backend = SSHBackend(store, state_dir)
            host_key = backend.probe_host_key("127.0.0.1", int(server.port))
            computer = store.create_computer(
                name="Isolated SSH B",
                ssh_alias="",
                host="127.0.0.1",
                port=int(server.port),
                username=str(server.username),
                identity_file=str(server.client_key),
                host_key_type=host_key["host_key_type"],
                host_key_data=host_key["host_key_data"],
                host_fingerprint=host_key["host_fingerprint"],
            )
            backend.remember_host_key(computer)
            assert backend.test_connection(computer)["tmux"].startswith("tmux ")
            canonical = backend.validate_workspace_path(computer, str(remote_root))
            b_workspace = manager.open_remote(
                computer["id"], canonical, "SSH B",
            )
            b_session = str(b_workspace["tmux_session"])
            backend.ensure_workspace(b_workspace)

            a_page = client.get(f"/w/{a_workspace['id']}/terminal")
            assert a_page.status_code == 200
            match = re.search(r'data-terminal-id="([a-f0-9]{32})"', a_page.text)
            assert match is not None
            a_terminal_id = match.group(1)
            a_terminal = store.get_terminal(a_terminal_id)
            assert a_terminal is not None
            a_target = f"{a_session}:{a_terminal['tmux_window']}"
            cookie = client.cookies.get("termroom_session")
            assert cookie
            evaluate = chrome_stack.enter_context(_isolated_chrome_terminal(
                tmp_path / "chrome-a-profile", base_url, cookie,
                a_workspace["id"], a_terminal_id,
            ))
            assert evaluate("""(() => {
              document.querySelector('#open-command-editor').click();
              const input = document.querySelector('#command-input');
              if (document.querySelector('#terminal-command-editor').hidden) {
                throw new Error('Composer stayed closed');
              }
              input.value = 'unsent SSH A 한글 draft';
              input.dispatchEvent(new Event('input', { bubbles: true }));
              input.focus();
              input.setSelectionRange(7, 12);
              return input.value;
            })()""") == "unsent SSH A 한글 draft"
            state_expression = """(() => {
              const input = document.querySelector('#command-input');
              return {
                url: location.href,
                selectedTerminal: document.querySelector(
                  '.terminal-tabs a.active'
                )?.dataset.terminalActivityTab,
                composerOpen: !document.querySelector(
                  '#terminal-command-editor'
                ).hidden,
                focusId: document.activeElement?.id,
                draft: input.value,
                selectionStart: input.selectionStart,
                selectionEnd: input.selectionEnd,
                pageScroll: window.scrollY
              };
            })()"""

            def a_geometry() -> str:
                return subprocess.check_output(
                    ["tmux", "display-message", "-p", "-t", a_target,
                     "#{pane_width}x#{pane_height}|#{pane_id}"],
                    text=True,
                ).strip()

            geometry = a_geometry()
            stable_at = time.monotonic()
            deadline = stable_at + 8
            while time.monotonic() < deadline:
                time.sleep(0.05)
                current = a_geometry()
                if current != geometry:
                    geometry = current
                    stable_at = time.monotonic()
                if time.monotonic() - stable_at >= 0.8:
                    break
            else:
                raise AssertionError("Chrome A initial geometry did not stabilize")
            before_a = evaluate(state_expression)
            assert before_a["selectedTerminal"] == a_terminal_id
            assert before_a["focusId"] == "command-input"

            credential = client.post(
                "/api/client-credentials",
                json={
                    "name": "ssh-b-operator",
                    "capabilities": ["read", "operate"],
                    "workspace_ids": [b_workspace["id"]],
                },
                headers={"X-Termroom-CSRF": csrf},
            )
            assert credential.status_code == 201, credential.text
            headers = {
                "Authorization": f"Bearer {credential.json()['client']['token']}",
            }
            files_url = f"/api/control/v1/workspaces/{b_workspace['id']}/files/text"
            runs_url = f"/api/control/v1/workspaces/{b_workspace['id']}/file-runs"
            with httpx.Client(base_url=base_url, timeout=25) as b_client:
                assert b_client.get(
                    f"/api/control/v1/workspaces/{a_workspace['id']}/files",
                    headers=headers,
                ).status_code == 404
                observed = b_client.get(
                    files_url, params={"path": "qa.txt"}, headers=headers,
                )
                assert observed.status_code == 200, observed.text
                saved = b_client.put(files_url, headers=headers, json={
                    "action_id": "ssh-chrome-b-save-0001",
                    "relative_path": "qa.txt",
                    "content": "ssh-b-saved\n",
                    "expected_digest": observed.json()["digest"],
                    "expected_mtime_ns": observed.json()["mtime_ns"],
                })
                assert saved.status_code == 200, saved.text
                assert saved.json()["phase"] == "result_verified"
                assert (remote_root / "qa.txt").read_text(
                    encoding="utf-8",
                ) == "ssh-b-saved\n"
                run_payload = {
                    "action_id": "ssh-chrome-b-run-0001",
                    "relative_path": "qa.py",
                    "expected_digest": hashlib.sha256(
                        (remote_root / "qa.py").read_bytes(),
                    ).hexdigest(),
                    "idempotency_key": str(uuid.uuid4()),
                }
                started = b_client.post(
                    runs_url, headers=headers, json=run_payload,
                )
                assert started.status_code in (200, 202), started.text
                run_id = started.json()["run"]["id"]
                terminal_id = started.json()["run"]["terminal_id"]
                terminal_ids = {
                    row["id"] for row in store.list_terminals(b_workspace["id"])
                }
                replayed = b_client.post(
                    runs_url, headers=headers, json=run_payload,
                )
                assert replayed.status_code in (200, 202), replayed.text
                assert replayed.json()["result_resource_id"] == run_id
                assert {
                    row["id"] for row in store.list_terminals(b_workspace["id"])
                } == terminal_ids
            finished = _wait_until(
                lambda: _file_run_status_is(client, run_id, "finished"),
                description="actual SSH B managed Run",
            )
            assert finished["exit_code"] == 0
            managed = store.get_terminal(terminal_id)
            assert managed is not None
            _wait_until(
                lambda: "T43_SSH_CHROME_REAL_OUTPUT" in backend.capture_scrollback(
                    b_workspace, managed,
                ),
                description="real SSH B tmux output",
            )
            browser_view = client.get(
                f"/w/{b_workspace['id']}/view/qa.txt",
            )
            assert browser_view.status_code == 200
            assert "ssh-b-saved" in browser_view.text
            assert evaluate(state_expression) == before_a
            assert a_geometry() == geometry

            # The same real Chrome now performs a human file save in SSH B.
            # Its HTML editor submits the normal browser form, so a previously
            # captured non-UI CAS cannot erase the user's saved bytes.
            with httpx.Client(base_url=base_url, timeout=15) as stale_client:
                stale_version = stale_client.get(
                    files_url, params={"path": "qa.txt"}, headers=headers,
                )
                assert stale_version.status_code == 200, stale_version.text
                editor_path = f"/w/{b_workspace['id']}/edit/qa.txt"
                evaluate(f"location.assign({json.dumps(base_url + editor_path)})")

                def editor_ready() -> bool:
                    try:
                        return bool(evaluate(
                            "document.readyState === 'complete' && "
                            f"location.pathname === {json.dumps(editor_path)} && "
                            "!!document.querySelector('#editor-content')"
                        ))
                    except AssertionError:
                        return False

                _wait_until(editor_ready, description="Chrome SSH B file editor")
                assert evaluate("""(() => {
                  const form = document.querySelector('#editor-form');
                  const content = form.querySelector('#editor-content');
                  if (content.value !== 'ssh-b-saved\\n') {
                    throw new Error('Browser opened a different file version');
                  }
                  content.value = 'ssh-b-human-browser-wins\\n';
                  content.dispatchEvent(new Event('input', { bubbles: true }));
                  form.querySelector('button[name=intent][value=save]').click();
                  return true;
                })()""") is True

                def browser_saved() -> bool:
                    try:
                        return bool(evaluate(
                            "document.readyState === 'complete' && "
                            "!!document.querySelector('.editor-saved-notice')"
                        ))
                    except AssertionError:
                        return False

                _wait_until(browser_saved, description="Chrome SSH B file save")
                assert (remote_root / "qa.txt").read_text(
                    encoding="utf-8",
                ) == "ssh-b-human-browser-wins\n"
                stale = stale_client.put(files_url, headers=headers, json={
                    "action_id": "ssh-chrome-b-stale-file-save-0001",
                    "relative_path": "qa.txt",
                    "content": "ssh-b-stale-must-not-win\n",
                    "expected_digest": stale_version.json()["digest"],
                    "expected_mtime_ns": stale_version.json()["mtime_ns"],
                })
                assert stale.status_code == 409, stale.text
                assert stale.json()["code"] == "file_conflict"
                assert stale.json()["current_version"]["digest"] == hashlib.sha256(
                    b"ssh-b-human-browser-wins\n",
                ).hexdigest()
                receipt = stale_client.get(
                    "/api/control/v1/actions/ssh-chrome-b-stale-file-save-0001",
                    headers=headers,
                )
                assert receipt.status_code == 200
                assert receipt.json()["status"] == "refused"
                assert (remote_root / "qa.txt").read_text(
                    encoding="utf-8",
                ) == "ssh-b-human-browser-wins\n"
            # Browser A performs the real approval page action only after the
            # unrelated SSH B work is observed. The closing Terminal is test-owned.
            reviewer = client.post(
                "/api/client-credentials",
                json={
                    "name": "a-close-reviewer",
                    "capabilities": ["read", "operate"],
                    "workspace_ids": [a_workspace["id"]],
                },
                headers={"X-Termroom-CSRF": csrf},
            )
            assert reviewer.status_code == 201, reviewer.text
            review_headers = {
                "Authorization": f"Bearer {reviewer.json()['client']['token']}",
            }
            close_base = (
                f"/api/control/v1/workspaces/{a_workspace['id']}"
                f"/terminals/{a_terminal_id}"
            )
            action_id = "ssh-chrome-human-approved-close-0001"
            with httpx.Client(base_url=base_url, timeout=15) as operator:
                intent = operator.post(
                    close_base + "/close-intents",
                    headers=review_headers,
                    json={
                        "action_id": action_id,
                        "expected_tmux_window": a_terminal["tmux_window"],
                    },
                )
                assert intent.status_code == 201, intent.text
                approval_id = intent.json()["approval_id"]
                close_payload = {
                    "action_id": action_id,
                    "approval_id": approval_id,
                    "expected_tmux_window": a_terminal["tmux_window"],
                }
                premature = operator.post(
                    close_base + "/close",
                    headers=review_headers,
                    json=close_payload,
                )
                assert premature.status_code == 409
                assert store.get_terminal(a_terminal_id) is not None

                review_path = intent.json()["human_review_url"]
                evaluate(f"location.assign({json.dumps(base_url + review_path)})")

                def review_ready() -> bool:
                    try:
                        return bool(evaluate(
                            "document.readyState === 'complete' && "
                            f"location.pathname === {json.dumps(review_path)} && "
                            "!!document.querySelector('button[name=decision][value=approve]')"
                        ))
                    except AssertionError:
                        return False

                _wait_until(review_ready, description="Chrome A exact close review")
                # A synthetic confirmation substitutes only the native dialog;
                # the visible review button still submits the real browser form.
                assert evaluate("""(() => {
                  window.confirm = () => true;
                  const approve = document.querySelector(
                    'button[name=decision][value=approve]'
                  );
                  approve.click();
                  return true;
                })()""") is True

                def approved_page() -> bool:
                    try:
                        return bool(evaluate(
                            "document.readyState === 'complete' && "
                            "!!document.querySelector('.notice[role=status]')"
                        ))
                    except AssertionError:
                        return False

                _wait_until(approved_page, description="Chrome approval notice")
                status = operator.get(
                    close_base + f"/close-intents/{approval_id}",
                    headers=review_headers,
                )
                assert status.status_code == 200
                assert status.json()["state"] == "approved"
                closed = operator.post(
                    close_base + "/close",
                    headers=review_headers,
                    json=close_payload,
                )
                assert closed.status_code == 200, closed.text
                assert closed.json()["closed"] is True
                assert store.get_terminal(a_terminal_id) is None
                assert store.get_terminal(terminal_id) is not None
                # Reverse the same-file race: SSH B now saves after a human
                # opens the editor. The real browser must show the conflict and
                # retain its unsaved content without overwriting agent bytes.
                evaluate(f"location.assign({json.dumps(base_url + editor_path)})")
                _wait_until(editor_ready, description="Chrome stale SSH editor")
                assert evaluate(
                    "document.querySelector('#editor-content').value"
                ) == "ssh-b-human-browser-wins\n"

                with httpx.Client(base_url=base_url, timeout=15) as b_operator:
                    current_version = b_operator.get(
                        files_url, params={"path": "qa.txt"}, headers=headers,
                    )
                    assert current_version.status_code == 200
                    remote_saved = b_operator.put(
                        files_url, headers=headers, json={
                            "action_id": "ssh-chrome-b-agent-wins-0001",
                            "relative_path": "qa.txt",
                            "content": "ssh-b-agent-wins\n",
                            "expected_digest": current_version.json()["digest"],
                            "expected_mtime_ns": current_version.json()["mtime_ns"],
                        },
                    )
                    assert remote_saved.status_code == 200, remote_saved.text
                assert evaluate("""(() => {
                  const form = document.querySelector('#editor-form');
                  const content = form.querySelector('#editor-content');
                  content.value = 'ssh-b-human-stale-draft\\n';
                  content.dispatchEvent(new Event('input', { bubbles: true }));
                  form.querySelector('button[name=intent][value=save]').click();
                  return true;
                })()""") is True

                def human_conflict_visible() -> bool:
                    try:
                        return bool(evaluate(
                            "document.readyState === 'complete' && "
                            "!!document.querySelector('.conflict-notice') && "
                            "document.querySelector('#editor-content')?.dataset.unsaved === '1'"
                        ))
                    except AssertionError:
                        return False

                _wait_until(
                    human_conflict_visible,
                    description="real Chrome stale human save conflict",
                )
                assert evaluate(
                    "document.querySelector('.conflict-notice')?.getAttribute('role')"
                ) == "alert"
                assert evaluate(
                    "document.querySelector('#editor-content').value"
                ) == "ssh-b-human-stale-draft\n"
                assert (remote_root / "qa.txt").read_text(
                    encoding="utf-8",
                ) == "ssh-b-agent-wins\n"
                assert store.get_terminal(terminal_id) is not None
        finally:
            chrome_stack.close()
            if client is not None:
                client.close()
            _stop_process(core)
            if a_session:
                subprocess.run(
                    ["tmux", "kill-session", "-t", a_session],
                    capture_output=True, check=False,
                )
            if backend is not None and computer is not None and b_session:
                with contextlib.suppress(Exception):
                    backend._exec(
                        computer,
                        f"tmux kill-session -t {shlex.quote(b_session)}",
                    )
            for handle in handles:
                handle.close()




def _install_output_seal_leaf(leaf: Path, kind: str, sentinel: Path) -> None:
    if kind == "symlink":
        leaf.symlink_to(sentinel)
    elif kind == "hardlink":
        os.link(sentinel, leaf)
    elif kind == "fifo":
        os.mkfifo(leaf, 0o600)
    elif kind == "directory":
        leaf.mkdir(mode=0o700)
    else:
        leaf.write_text('{"size":0,"sealed_at":"premature"}\n', encoding="utf-8")
        leaf.chmod(0o640 if kind == "wrong-mode" else 0o600)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
@pytest.mark.parametrize("phase", ["before-start", "handoff", "running"])
@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "fifo", "directory", "wrong-mode", "competing"]
)
def test_real_node_output_seal_substitution_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, phase: str,
) -> None:
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    allowed = tmp_path / "projects"
    allowed.mkdir()
    root = tmp_path / "runs"
    node = NodeRuntime([allowed], remote_run_root=root)
    runtime = node.remote_runs
    assert runtime is not None
    run_id = str(uuid.uuid4())
    payload = {
        "remote_run_version": NODE_REMOTE_RUN_VERSION,
        "run_base": str(root), "run_id": run_id,
    }
    runtime.create({
        **payload, "cwd_rel": ".", "command": "\n".join((
            "test -t 1 && test -t 2 || exit 19",
            'test -z "${TERMROOM_REMOTE_RUN_LOG_CHANNEL+x}" || exit 20',
            'test -z "${TERMROOM_REMOTE_RUN_LOG_CHANNEL_WRITE+x}" || exit 20',
            "printf 'executed\\n' >> ../executions",
            "printf 'seal-start\\n' >&2",
            "while test ! -f ../release; do sleep 0.05; done",
            "printf 'seal-end\\n'",
        )),
    })
    runtime.snapshot_begin(payload)
    runtime.snapshot_commit(payload)
    run = root / run_id
    metadata = run / ".termroom"
    leaf = metadata / "output-seal.json"
    output = metadata / "output.log"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"outside must survive\n")
    sentinel.chmod(0o640)
    session = f"{REMOTE_RUN_SESSION_PREFIX}{run_id}"
    installed: list[os.stat_result] = []

    def install() -> None:
        _install_output_seal_leaf(leaf, kind, sentinel)
        installed.append(leaf.lstat())

    original_handoff = runtime._descriptor_handoff
    assert original_handoff is not None

    def substitute_at_handoff(*args: Any, **kwargs: Any) -> Any:
        install()
        return original_handoff(*args, **kwargs)

    def terminal_record() -> dict[str, Any] | None:
        observed = runtime.observe(payload)
        return observed if observed["state"] in {"finished", "failed", "lost"} else None

    try:
        if phase == "before-start":
            install()
            with pytest.raises(NodeRemoteRunError) as invalid:
                runtime.start(payload)
            assert invalid.value.code == "metadata_invalid"
            assert not output.exists()
        else:
            if phase == "handoff":
                monkeypatch.setattr(runtime, "_descriptor_handoff", substitute_at_handoff)
            runtime.start(payload)
            if phase == "running":
                _wait_until(
                    lambda: output.exists() and b"seal-start" in output.read_bytes(),
                    timeout=8, description="seal-test command output",
                )
                assert runtime.observe(payload)["state"] == "running"
                install()
                assert not (metadata / "completion.json").exists()
                (run / "release").touch()
            completed = _wait_until(
                terminal_record, timeout=16 if phase == "handoff" else 8,
                description="bounded seal failure",
            )
            if phase == "handoff":
                assert completed["state"] == "failed"
                assert completed["error_code"] == "output_pipe_unavailable"
            else:
                assert completed["state"] == "finished"
                assert completed["exit_code"] == 0
                assert completed["log_incomplete"] is True
                record = json.loads((metadata / "completion.json").read_bytes())
                assert record["log_incomplete"] is True
                assert record["log_size"] == output.stat().st_size
                assert output.read_bytes() == b"seal-start\r\nseal-end\r\n"
                pane = subprocess.run(
                    ["tmux", "capture-pane", "-p", "-t", f"{session}:run.0"],
                    capture_output=True, text=True, check=True, timeout=3,
                ).stdout.splitlines()
                assert pane.count("seal-start") == pane.count("seal-end") == 1
                assert (run / "executions").read_text().splitlines() == ["executed"]
                assert runtime.start(payload)["replayed"] is True
                assert (run / "executions").read_text().splitlines() == ["executed"]
        if phase != "running":
            assert not (run / "executions").exists()
        assert installed == [leaf.lstat()]
        assert sentinel.read_bytes() == b"outside must survive\n"
        assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
        assert stat.S_IMODE(outside.stat().st_mode) == 0o755
        assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]
        assert not list(metadata.glob(".output-seal.json.tmp-*"))
    finally:
        (run / "release").touch()
        subprocess.run(
            ["tmux", "kill-session", "-t", session],
            capture_output=True, check=False, timeout=3,
        )


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "fifo", "directory", "wrong-mode", "competing"]
)
def test_node_output_seal_competing_publication_preserves_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    metadata = tmp_path / "metadata"
    metadata.mkdir(mode=0o700)
    output = metadata / "output.log"
    output.write_bytes(b"")
    output.chmod(0o600)
    sentinel = tmp_path / "outside"
    sentinel.write_bytes(b"preserve outside\n")
    sentinel.chmod(0o640)
    leaf = metadata / "output-seal.json"
    metadata_fd = os.open(metadata, os.O_RDONLY | os.O_DIRECTORY)
    output_fd = os.open(output, os.O_WRONLY | os.O_APPEND)
    reader, writer = os.pipe()
    original_link = os.link
    original_read = os.read
    chunks = iter((b"complete pane bytes\r\n", b""))
    installed: list[os.stat_result] = []

    def competing_link(source: Any, destination: Any, **kwargs: Any) -> None:
        if destination == "output-seal.json":
            _install_output_seal_leaf(leaf, kind, sentinel)
            installed.append(leaf.lstat())
        original_link(source, destination, **kwargs)

    monkeypatch.setattr(node_remote_runs.os, "link", competing_link)
    monkeypatch.setattr(
        node_remote_runs.os, "read",
        lambda fd, size: next(chunks) if fd == 0 else original_read(fd, size),
    )
    try:
        assert node_remote_runs._node_remote_run_log_helper(
            f"/proc/self/fd/{metadata_fd}", f"/proc/self/fd/{output_fd}",
            f"/proc/self/fd/{writer}",
        ) == 120
        assert original_read(reader, 4096) == b"ready\nfailed\n"
        assert output.read_bytes() == b"complete pane bytes\r\n"
        assert installed == [leaf.lstat()]
        assert sentinel.read_bytes() == b"preserve outside\n"
        assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
        assert not list(metadata.glob(".output-seal.json.tmp-*"))
    finally:
        for descriptor in (metadata_fd, output_fd, reader, writer):
            os.close(descriptor)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_real_node_output_seal_premature_leaf_cannot_acknowledge_delayed_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    allowed = tmp_path / "projects"
    allowed.mkdir()
    root = tmp_path / "runs"
    node = NodeRuntime([allowed], remote_run_root=root)
    runtime = node.remote_runs
    assert runtime is not None
    run_id = str(uuid.uuid4())
    payload = {
        "remote_run_version": NODE_REMOTE_RUN_VERSION,
        "run_base": str(root), "run_id": run_id,
    }
    runtime.create({
        **payload, "cwd_rel": ".", "command": "\n".join((
            "test -t 1 && test -t 2 || exit 19",
            "printf 'executed\\n' >> ../executions",
            "printf 'delayed-start\\n'",
            "while test ! -f ../release; do sleep 0.05; done",
            "printf 'delayed-end\\n' >&2",
        )),
    })
    runtime.snapshot_begin(payload)
    runtime.snapshot_commit(payload)
    run = root / run_id
    metadata = run / ".termroom"
    output = metadata / "output.log"
    seal = metadata / "output-seal.json"
    original_handoff = runtime._descriptor_handoff
    assert original_handoff is not None

    def delay_helper(*args: Any, **kwargs: Any) -> Any:
        environment = dict(kwargs["environment"])
        delayed_code = '''import os, sys, time
from termroom.node_remote_runs import _node_remote_run_log_helper
original_write = os.write
def delayed_write(fd, data):
    result = original_write(fd, data)
    if data == b"ready\\n":
        time.sleep(4)
    return result
os.write = delayed_write
sys.exit(_node_remote_run_log_helper(*sys.argv[1:]))
'''
        environment["TERMROOM_REMOTE_RUN_LOG_HELPER_CODE"] = f"exec({delayed_code!r})"
        kwargs["environment"] = environment
        return original_handoff(*args, **kwargs)

    monkeypatch.setattr(runtime, "_descriptor_handoff", delay_helper)
    session = f"{REMOTE_RUN_SESSION_PREFIX}{run_id}"
    try:
        runtime.start(payload)
        _wait_until(lambda: (run / "executions").exists(), timeout=8,
                    description="delayed helper's command")
        assert output.read_bytes() == b""
        seal.write_text('{"size":0,"sealed_at":"premature"}\n', encoding="utf-8")
        seal.chmod(0o600)
        premature = seal.read_bytes(), seal.lstat()
        start = time.monotonic()
        (run / "release").touch()
        completed = _wait_until(
            lambda: value if (value := runtime.observe(payload))["state"] == "finished" else None,
            timeout=3.5, description="bounded incomplete drain",
        )
        assert time.monotonic() - start < 3.5
        assert completed["exit_code"] == 0
        assert completed["log_incomplete"] is True
        assert (seal.read_bytes(), seal.lstat()) == premature
        record = json.loads((metadata / "completion.json").read_bytes())
        assert record["log_incomplete"] is True
        assert record["log_size"] == 0
        _wait_until(lambda: output.read_bytes() == b"delayed-start\r\ndelayed-end\r\n",
                    timeout=6, description="late pipe bytes")
        assert (seal.read_bytes(), seal.lstat()) == premature
        assert json.loads((metadata / "completion.json").read_bytes()) == record
        assert (run / "executions").read_text().splitlines() == ["executed"]
    finally:
        (run / "release").touch()
        subprocess.run(["tmux", "kill-session", "-t", session],
                       capture_output=True, check=False, timeout=3)


@pytest.mark.parametrize("kind", [
    "symlink", "hardlink", "fifo", "directory", "wrong-mode", "competing",
    "same-content", "size-mismatch", "wrong-owner",
])
def test_node_output_seal_admission_requires_receipt_identity_and_pinned_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    metadata = tmp_path / "metadata"
    metadata.mkdir(mode=0o700)
    output = metadata / "output.log"
    output.write_bytes(b"pane output\r\n")
    output.chmod(0o600)
    sentinel = tmp_path / "outside"
    sentinel.write_bytes(b"outside is unchanged\n")
    sentinel.chmod(0o640)
    leaf = metadata / "output-seal.json"
    metadata_fd = os.open(metadata, os.O_RDONLY | os.O_DIRECTORY)
    output_fd = os.open(output, os.O_WRONLY | os.O_APPEND)
    try:
        content = json.dumps({"size": output.stat().st_size, "sealed_at": "valid"}).encode()
        state = node_remote_runs._atomic_private_write_at(
            metadata_fd, "output-seal.json", content, expected_state=None,
        )
        receipt = "sealed " + json.dumps(state)
        assert node_remote_runs._node_remote_run_log_size(
            f"/proc/self/fd/{metadata_fd}", f"/proc/self/fd/{output_fd}", receipt,
        ) == output.stat().st_size
        if kind == "size-mismatch":
            os.write(output_fd, b"late output\r\n")
        elif kind == "wrong-owner":
            euid = os.geteuid()
            monkeypatch.setattr(node_remote_runs.os, "geteuid", lambda: euid + 1)
        else:
            # Keep the original inode alive so an allocator cannot reuse it.
            leaf.rename(metadata / "original-seal")
            if kind == "same-content":
                leaf.write_bytes(content)
                leaf.chmod(0o600)
            else:
                _install_output_seal_leaf(leaf, kind, sentinel)
        before = leaf.lstat()
        with pytest.raises((OSError, NodeRemoteRunError)) as invalid:
            node_remote_runs._node_remote_run_log_size(
                f"/proc/self/fd/{metadata_fd}", f"/proc/self/fd/{output_fd}", receipt,
            )
        if isinstance(invalid.value, NodeRemoteRunError):
            assert invalid.value.code == "metadata_invalid"
        assert leaf.lstat() == before
        assert sentinel.read_bytes() == b"outside is unchanged\n"
        assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    finally:
        os.close(metadata_fd)
        os.close(output_fd)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_real_node_remote_run_result_zip_conflict_apply_survives_core_restart(
    tmp_path: Path,
) -> None:
    core_root = tmp_path / "core-root"
    core_state = tmp_path / "core-state"
    source_root = core_root / "source-project"
    node_root = tmp_path / "node-root"
    node_state = tmp_path / "node-state"
    for path in (source_root, node_root):
        path.mkdir(parents=True)
    (source_root / "eligible.txt").write_text("source eligible\n", encoding="utf-8")
    (source_root / "conflict.txt").write_text("source conflict\n", encoding="utf-8")
    (source_root / "delete-sentinel.txt").write_text(
        "keep in source\n", encoding="utf-8"
    )
    (source_root / "existing-parent").mkdir()

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment["TERMROOM_PASSWORD"] = "node-collect-e2e-password"
    environment["TERMROOM_LOCALE"] = "en"
    core: subprocess.Popen[bytes] | None = None
    node: subprocess.Popen[bytes] | None = None
    pair: subprocess.Popen[bytes] | None = None
    handles: list[Any] = []
    client: httpx.Client | None = None

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
        client, csrf = _login(base_url, "node-collect-e2e-password")

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
                "Node Collection E2E",
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
            r"/computers/node/pair/([a-f0-9]{32})/approve", review.text
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
            lambda: _remote_connection_state_is(client, node_id, "available"),
            description="Node control connection",
        )

        store = StateStore(core_state / "termroom.sqlite3")
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

        execution_marker = f"executed-{uuid.uuid4()}"
        start_output_marker = f"remote-run-start-{uuid.uuid4()}"
        completion_output_marker = f"remote-run-complete-{uuid.uuid4()}"
        command = "\n".join(
            (
                "test -t 1 && test -t 2 || exit 19",
                'test -z "${TERMROOM_REMOTE_RUN_LOG_CHANNEL+x}" || exit 20',
                'test -z "${TERMROOM_REMOTE_RUN_LOG_CHANNEL_WRITE+x}" || exit 20',
                f"printf '%s\\n' '{start_output_marker}' >&2",
                f"printf '%s\\n' '{execution_marker}' >> ../execution-marker.txt",
                "printf '%s\\n' 'remote eligible' > eligible.txt",
                "printf '%s\\n' 'remote conflict' > conflict.txt",
                "printf '%s\\n' '원격 텍스트 결과' > existing-parent/new-result.txt",
                "rm -- delete-sentinel.txt",
                "printf '\\000binary\\n' > binary.dat",
                "mkdir -- missing-parent",
                "printf '%s\\n' 'parent absent from source' > missing-parent/new-file.txt",
                "ln -s -- eligible.txt symlink-result.txt",
                "while test ! -f ../.test-release; do sleep 0.1; done",
                f"printf '%s\\n' '{completion_output_marker}'",
            )
        )
        run_id = _create_remote_run(
            client,
            csrf,
            source_kind="workspace",
            target_id=node_id,
            command=command,
            source_workspace_id=source_workspace_id,
            source_path=".",
        )
        running = _wait_until(
            lambda: _remote_run_status_is(
                client, run_id, "running", connection="online"
            ),
            description="active Workspace-source Node Remote Run",
        )
        assert running["workspace_id"]
        transient_workspace_id = str(running["workspace_id"])
        node_config = json.loads((node_state / "node.json").read_text(encoding="utf-8"))
        managed_root = Path(str(node_config["run_root"]))
        run_root = managed_root / run_id
        work_root = run_root / "work"
        private_output_path = run_root / ".termroom" / "output.log"
        private_output_seal_path = run_root / ".termroom" / "output-seal.json"
        completion_record_path = run_root / ".termroom" / "completion.json"
        marker_path = run_root / "execution-marker.txt"
        symlink_path = work_root / "symlink-result.txt"
        _wait_until(
            lambda: marker_path.exists() and symlink_path.is_symlink(),
            description="Node command side effects and symlink result",
        )
        assert marker_path.read_text(encoding="utf-8").splitlines() == [execution_marker]
        assert (work_root / "binary.dat").read_bytes() == b"\x00binary\n"
        assert node.poll() is None

        tmux_session = f"{REMOTE_RUN_SESSION_PREFIX}{run_id}"

        def owned_run_session_exists() -> bool:
            exists = subprocess.run(
                ["tmux", "has-session", "-t", tmux_session],
                capture_output=True,
                check=False,
            )
            owner = subprocess.run(
                [
                    "tmux",
                    "show-option",
                    "-v",
                    "-t",
                    tmux_session,
                    "@termroom_remote_run_id",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            return exists.returncode == 0 and owner.stdout.strip() == run_id

        def run_tmux_identity() -> tuple[str, str, str, str]:
            result = subprocess.run(
                [
                    "tmux",
                    "list-panes",
                    "-s",
                    "-t",
                    tmux_session,
                    "-F",
                    "#{session_name}|#{session_id}|#{window_name}|#{window_id}|#{pane_id}",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            rows = [line.split("|") for line in result.stdout.splitlines()]
            run_windows = [row for row in rows if len(row) == 5 and row[2] == "run"]
            assert len(run_windows) == 1 and len(run_windows[0]) == 5
            return (
                run_windows[0][0],
                run_windows[0][1],
                run_windows[0][3],
                run_windows[0][4],
            )

        assert owned_run_session_exists()
        node_pid = node.pid
        tmux_identity_before_restart = run_tmux_identity()
        assert tmux_identity_before_restart[0] == tmux_session
        run_terminal = next(
            item
            for item in store.list_terminals(transient_workspace_id)
            if item.get("role") == "remote_run"
            and item.get("managed_run_id") == run_id
        )
        assert run_terminal["tmux_window"] == tmux_identity_before_restart[2]

        def capture_run_output() -> str:
            response = client.get(
                f"/w/{transient_workspace_id}/terminal/{run_terminal['id']}/scrollback?recent=2000",
                headers={"Accept": "text/plain"},
            )
            assert response.status_code == 200, response.text
            return response.text

        output_before_restart = capture_run_output()
        assert start_output_marker in output_before_restart, repr(output_before_restart)
        assert output_before_restart.splitlines().count(start_output_marker) == 1
        assert completion_output_marker not in output_before_restart
        start_marker_bytes = start_output_marker.encode()
        completion_marker_bytes = completion_output_marker.encode()
        private_output_before_restart = _wait_until(
            lambda: (
                output
                if start_marker_bytes in (output := private_output_path.read_bytes())
                else None
            ),
            description="start output in the private Node log",
        )
        assert private_output_before_restart.count(start_marker_bytes) == 1
        assert completion_marker_bytes not in private_output_before_restart

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
        client, csrf = _login(base_url, "node-collect-e2e-password")
        store = StateStore(core_state / "termroom.sqlite3")
        _wait_until(
            lambda: _remote_connection_state_is(client, node_id, "available"),
            timeout=20,
            description="Node reconnect after Core restart",
        )
        restored = _wait_until(
            lambda: _remote_run_status_is(
                client, run_id, "running", connection="online"
            ),
            description="same Remote Run after Core restart",
        )
        assert restored["workspace_id"] == running["workspace_id"]
        assert node.poll() is None and node.pid == node_pid
        assert owned_run_session_exists()
        assert run_tmux_identity() == tmux_identity_before_restart
        output_after_reconnect = capture_run_output()
        assert output_after_reconnect == output_before_restart
        assert output_after_reconnect.splitlines().count(start_output_marker) == 1
        assert completion_output_marker not in output_after_reconnect
        assert private_output_path.read_bytes() == private_output_before_restart
        assert marker_path.read_text(encoding="utf-8").splitlines() == [execution_marker]

        (run_root / ".test-release").write_text("continue\n", encoding="utf-8")
        finished = _wait_until(
            lambda: _remote_run_status_is(client, run_id, "finished"),
            description="Remote Run completion after Core recovery",
        )
        assert finished["exit_code"] == 0
        assert marker_path.read_text(encoding="utf-8").splitlines() == [execution_marker]
        assert node.poll() is None
        completed_output = _wait_until(
            lambda: (
                output
                if completion_output_marker in (output := capture_run_output())
                else None
            ),
            description="Remote Run completion output",
        )
        assert completed_output.splitlines().count(start_output_marker) == 1
        assert completed_output.splitlines().count(completion_output_marker) == 1
        private_output = private_output_path.read_bytes()
        assert private_output.count(start_marker_bytes) == 1
        assert private_output.count(completion_marker_bytes) == 1
        output_seal = json.loads(private_output_seal_path.read_text(encoding="utf-8"))
        completion_record = json.loads(completion_record_path.read_text(encoding="utf-8"))
        assert set(output_seal) == {"size", "sealed_at"}
        assert isinstance(output_seal["sealed_at"], str) and output_seal["sealed_at"]
        assert output_seal["size"] == len(private_output)
        assert completion_record["log_size"] == len(private_output)
        assert completion_record["log_incomplete"] is False

        downloaded = client.get(f"/remote-runs/{run_id}/result.zip")
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"] == "application/zip"
        archive_digest = hashlib.sha256(downloaded.content).hexdigest()
        assert downloaded.content and re.fullmatch(r"[0-9a-f]{64}", archive_digest)
        with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
            infos = archive.infolist()
            assert infos
            for info in infos:
                posix_path = PurePosixPath(info.filename)
                windows_path = PureWindowsPath(info.filename)
                assert info.filename == posix_path.as_posix()
                assert not posix_path.is_absolute()
                assert not windows_path.drive and not windows_path.root
                assert "\\" not in info.filename
                assert all(part not in {"", ".", ".."} for part in posix_path.parts)
                assert ".termroom" not in posix_path.parts
                assert stat.S_ISREG(info.external_attr >> 16)
                assert not info.is_dir()
            result_names = {info.filename for info in infos}
            assert result_names == {
                "eligible.txt",
                "conflict.txt",
                "existing-parent/new-result.txt",
                "binary.dat",
                "missing-parent/new-file.txt",
            }
            assert archive.read("eligible.txt") == b"remote eligible\n"
            assert archive.read("conflict.txt") == b"remote conflict\n"
            assert archive.read("existing-parent/new-result.txt") == (
                "원격 텍스트 결과\n".encode()
            )
            assert b"\x00" in archive.read("binary.dat")
            assert "delete-sentinel.txt" not in result_names
            assert "symlink-result.txt" not in result_names

        def collection_rows(page: str) -> dict[str, str]:
            return {
                path: status
                for status, path in re.findall(
                    r'<article class="remote-run-collect-row ([^"]+)">\s*'
                    r'<code title="([^"]+)">',
                    page,
                )
            }

        first_review = client.get(f"/remote-runs/{run_id}/collect")
        assert first_review.status_code == 200
        assert collection_rows(first_review.text)["conflict.txt"] == "ready"
        (source_root / "conflict.txt").write_text(
            "user edit after review\n", encoding="utf-8"
        )
        reviewed = client.get(f"/remote-runs/{run_id}/collect")
        assert reviewed.status_code == 200
        review_rows = collection_rows(reviewed.text)
        assert review_rows == {
            "eligible.txt": "ready",
            "conflict.txt": "conflict",
            "existing-parent/new-result.txt": "ready",
            "binary.dat": "skipped",
            "missing-parent/new-file.txt": "conflict",
            "delete-sentinel.txt": "skipped",
        }
        revision_match = re.search(
            r'name="revision" value="([^"]+)"', reviewed.text
        )
        assert revision_match is not None
        applied = client.post(
            f"/remote-runs/{run_id}/collect",
            data={"_csrf": csrf, "revision": revision_match.group(1)},
            follow_redirects=False,
        )
        assert applied.status_code == 303
        report_url = applied.headers["location"]
        report_page = client.get(report_url)
        assert report_page.status_code == 200
        expected_report_rows = {
            "eligible.txt": "applied",
            "conflict.txt": "conflict",
            "existing-parent/new-result.txt": "applied",
            "binary.dat": "skipped",
            "missing-parent/new-file.txt": "conflict",
            "delete-sentinel.txt": "skipped",
        }
        report_rows = collection_rows(report_page.text)
        assert report_rows == expected_report_rows
        assert (source_root / "eligible.txt").read_text(encoding="utf-8") == (
            "remote eligible\n"
        )
        assert (source_root / "conflict.txt").read_text(encoding="utf-8") == (
            "user edit after review\n"
        )
        assert (
            source_root / "existing-parent" / "new-result.txt"
        ).read_text(encoding="utf-8") == "원격 텍스트 결과\n"
        assert (source_root / "delete-sentinel.txt").read_text(encoding="utf-8") == (
            "keep in source\n"
        )
        assert not (source_root / "binary.dat").exists()
        assert not (source_root / "missing-parent").exists()
        assert not (source_root / "symlink-result.txt").exists()

        def source_snapshot() -> dict[str, tuple[bytes, str]]:
            snapshot = {}
            for path in source_root.rglob("*"):
                if path.is_file():
                    content = path.read_bytes()
                    snapshot[path.relative_to(source_root).as_posix()] = (
                        content,
                        hashlib.sha256(content).hexdigest(),
                    )
            return snapshot

        source_state_after_apply = source_snapshot()
        reloaded_report = client.get(report_url)
        assert reloaded_report.status_code == 200
        assert collection_rows(reloaded_report.text) == report_rows == expected_report_rows
        assert source_snapshot() == source_state_after_apply

        run_activity_events = [
            item
            for item in store.list_activity_events(limit=200)
            if item.get("subject_id") == run_id
        ]
        terminal_events = [
            item
            for item in run_activity_events
            if item.get("kind")
            in {
                "remote_run.completed",
                "remote_run.failed",
                "remote_run.stopped",
                "remote_run.attention",
            }
        ]
        assert len(terminal_events) == 1
        assert terminal_events[0].get("kind") == "remote_run.completed"
        event = _wait_until(
            lambda: terminal_events[0],
            description="Remote Run Activity event",
        )
        activity = client.get("/activity")
        assert activity.status_code == 200
        assert "Remote Run completed" in activity.text
        opened_activity = client.post(
            f"/activity/{event['id']}/open",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        assert opened_activity.status_code == 303
        assert opened_activity.headers["location"] == f"/remote-runs/{run_id}"

        sibling = managed_root / "keep-sibling.txt"
        sibling.write_text("must survive managed deletion\n", encoding="utf-8")
        source_workspace_record = store.get_workspace(source_workspace_id)
        transient_workspace_record = store.get_workspace(transient_workspace_id)
        assert source_workspace_record is not None
        assert source_workspace_record["root_id"] == local_root["id"]
        assert source_workspace_record["root_path"] == str(core_root)
        assert source_workspace_record["relative_path"] == "source-project"
        assert (
            Path(source_workspace_record["root_path"])
            / source_workspace_record["relative_path"]
        ).resolve() == source_root.resolve()
        assert transient_workspace_record is not None
        assert transient_workspace_record["root_path"] == f"ssh://{node_id}"
        assert transient_workspace_record["relative_path"] == str(work_root)
        assert transient_workspace_record["canonical_path"] == str(work_root)
        source_workspace_ids = {
            str(item["id"]) for item in store.list_workspaces_for_root(local_root["id"])
        }
        node_workspace_ids = {
            str(item["id"])
            for item in store.list_registered_workspaces_for_computer(node_id)
        }
        remote_run_ids = {
            str(item["id"]) for item in store.list_remote_runs_for_computer(node_id)
        }
        assert source_workspace_id in source_workspace_ids
        assert transient_workspace_id in node_workspace_ids
        assert run_id in remote_run_ids
        source_state_before_delete = source_snapshot()
        assert source_state_before_delete == source_state_after_apply
        entries_before_delete = {path.name for path in managed_root.iterdir()}
        deleted = client.post(
            f"/remote-runs/{run_id}/delete", data={"_csrf": csrf}
        )
        assert deleted.status_code == 303
        assert store.get_remote_run(run_id) is None
        assert store.get_workspace(transient_workspace_id) is None
        assert store.get_workspace(source_workspace_id) == source_workspace_record
        assert {
            str(item["id"]) for item in store.list_workspaces_for_root(local_root["id"])
        } == source_workspace_ids
        assert {
            str(item["id"])
            for item in store.list_registered_workspaces_for_computer(node_id)
        } == node_workspace_ids - {transient_workspace_id}
        assert {
            str(item["id"]) for item in store.list_remote_runs_for_computer(node_id)
        } == remote_run_ids - {run_id}
        assert not run_root.exists()
        assert source_root.is_dir()
        assert source_snapshot() == source_state_before_delete
        assert (source_root / "conflict.txt").read_bytes() == b"user edit after review\n"
        assert (source_root / "delete-sentinel.txt").read_bytes() == b"keep in source\n"
        assert not (source_root / "binary.dat").exists()
        assert not (source_root / "missing-parent").exists()
        assert not (source_root / "symlink-result.txt").exists()
        assert sibling.is_file()
        assert sibling.read_text(encoding="utf-8") == "must survive managed deletion\n"
        assert {path.name for path in managed_root.iterdir()} == entries_before_delete - {
            run_id
        }
    finally:
        if client is not None:
            client.close()
        _stop_process(pair)
        _stop_process(node)
        _stop_process(core)
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
            lambda: _remote_connection_state_is(client, node_id, "available"),
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
                lambda: _remote_connection_state_is(client, node_id, "available"),
                description="Node control connection",
            )

            store = StateStore(core_state / "termroom.sqlite3")
            backend = SSHBackend(store, core_state)
            host_key = backend.probe_host_key("127.0.0.1", int(server.port))
            ssh_computer = store.create_computer(
                name="SSH Target",
                ssh_alias="",
                host="127.0.0.1",
                port=int(server.port),
                username=str(server.username),
                identity_file=str(server.client_key),
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
            lambda: _remote_connection_state_is(client, node_id, "available"),
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
            lambda: _remote_connection_state_is(client, node_id, "unavailable"),
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
            lambda: _remote_connection_state_is(client, node_id, "available"),
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
            websocket.send(
                json.dumps(
                    {
                        "kind": "input",
                        "data": "service-reconnect\r",
                        "rows": 24,
                        "cols": 80,
                        "user_input": True,
                    }
                )
            )
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
            lambda: _remote_connection_state_is(client, node_id, "available"),
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
            lambda: _remote_connection_state_is(client, node_id, "available"),
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
            lambda: _remote_connection_state_is(client, node_id, "available"),
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
