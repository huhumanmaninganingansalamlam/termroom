"""Independently runnable real Node product workflows with private Core and tmux.

These tests use the real CLI and HTTP routes; no direct NodeRuntime mocks.
Each scenario owns its own Node, Core, database and filesystem lifetime.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.sync.client import connect as websocket_connect

from tests.support.node import (
    _create_remote_run,
    _file_run_status_is,
    _remote_connection_state_is,
    _remote_run_status_is,
    _start_file_run,
    _workspace_usage_is,
    _zip_bytes,
    connected_real_node,
    wait_until,
)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="Real tmux is required")
def test_real_node_workspace_archive_and_git_sources_clean_up(tmp_path: Path) -> None:
    """Three real Remote Run source formats execute and are deleted independently."""
    with connected_real_node(tmp_path) as env:
        client, csrf = env.client, env.csrf
        node_id, managed_root = env.node_id, env.managed_root
        workspace_id = env.source_workspace_id
        store = env.store

        workspace_run_id = _create_remote_run(
            client, csrf,
            source_kind="workspace", target_id=node_id,
            command="cat source.txt > workspace-result.txt",
            source_workspace_id=workspace_id, source_path=".",
        )
        workspace_run = wait_until(
            lambda: _remote_run_status_is(client, workspace_run_id, "finished"),
            description="Node Workspace source Remote Run",
        )
        assert workspace_run["exit_code"] == 0
        assert workspace_run["workspace_id"]
        result_workspace_page = client.get(
            f"/w/{workspace_run['workspace_id']}/files"
        )
        assert result_workspace_page.status_code == 200
        assert "workspace-result.txt" in result_workspace_page.text

        archive_run_id = _create_remote_run(
            client, csrf,
            source_kind="archive", target_id=node_id,
            command="cat archive.txt > archive-result.txt",
            archive_name="node-source.zip", archive_format="zip",
        )
        archive_upload = client.post(
            f"/api/remote-runs/{archive_run_id}/archive",
            params={"filename": "node-source.zip"},
            content=_zip_bytes({"archive.txt": b"archive-source\n"}),
            headers={"X-Termroom-CSRF": csrf},
        )
        assert archive_upload.status_code == 202, archive_upload.text
        archive_run = wait_until(
            lambda: _remote_run_status_is(client, archive_run_id, "finished"),
            description="Node Archive source Remote Run",
        )
        assert archive_run["exit_code"] == 0

        git_run_id = _create_remote_run(
            client, csrf,
            source_kind="git", target_id=node_id,
            command="cat source.txt > git-result.txt",
            source_url="https://example.test/public.git",
        )
        git_run = wait_until(
            lambda: _remote_run_status_is(client, git_run_id, "finished"),
            description="Node public HTTPS Git source Remote Run",
        )
        assert git_run["exit_code"] == 0
        stored_git = store.get_remote_run(git_run_id)
        assert stored_git is not None
        assert stored_git["source_revision"] == "abcdef0123456789abcdef0123456789abcdef01"

        for run_id, artifact, content in (
            (workspace_run_id, "workspace-result.txt", "workspace-source\n"),
            (archive_run_id, "archive-result.txt", "archive-source\n"),
            (git_run_id, "git-result.txt", "git-source\n"),
        ):
            assert (managed_root / run_id / "work" / artifact).read_text(
                encoding="utf-8"
            ) == content
            deleted = client.post(
                f"/remote-runs/{run_id}/delete", data={"_csrf": csrf},
            )
            assert deleted.status_code == 303, deleted.text
            assert store.get_remote_run(run_id) is None
            assert not (managed_root / run_id).exists()


def _control_headers(
    env: Any, name: str, capabilities: list[str],
) -> dict[str, str]:
    created = env.client.post(
        "/api/client-credentials",
        json={
            "name": name, "capabilities": capabilities,
            "workspace_ids": [env.workspace_id],
        },
        headers={"X-Termroom-CSRF": env.csrf},
    )
    assert created.status_code == 201, created.text
    return {
        "Authorization": "Bearer " + created.json()["client"]["token"],
    }


@pytest.mark.skipif(shutil.which("tmux") is None, reason="Real tmux required")
@pytest.mark.parametrize(
    "with_chrome",
    [
        pytest.param(False, id="isolated-scoped-client"),
        pytest.param(
            True, id="chrome-a-node-b-isolation",
            marks=pytest.mark.skipif(
                shutil.which("google-chrome") is None,
                reason="Real Chrome required",
            ),
        ),
    ],
)
def test_real_node_scoped_client_preserves_browser_a_and_exact_file_run(
    tmp_path: Path, with_chrome: bool,
) -> None:
    """Read-only bearer, scoped CAS mutation and real Node File Run coexist."""
    from tests.support.chrome import open_chrome_page

    with connected_real_node(tmp_path) as env:
        client, wid = env.client, env.workspace_id
        read_headers = _control_headers(env, "e2e-observer", ["read"])
        operate_headers = _control_headers(
            env, "e2e-node-operator", ["read", "operate"],
        )
        with httpx.Client(base_url=env.base_url, timeout=20) as observer:
            listing = observer.get(
                "/api/control/v1/workspaces", headers=read_headers,
            )
            assert listing.status_code == 200
            assert [w["id"] for w in listing.json()["workspaces"]] == [wid]
            registered = observer.get(
                f"/api/control/v1/workspaces/{wid}/terminals",
                headers=read_headers,
            )
            assert registered.status_code == 200
            assert env.terminal_id in {
                row["id"] for row in registered.json()["terminals"]
            }
            capture = observer.get(
                f"/api/control/v1/workspaces/{wid}/terminals/"
                f"{env.terminal_id}/snapshot?lines=5",
                headers=read_headers,
            )
            assert capture.status_code == 200
            assert capture.json()["pane_identity_verified"] is False
            assert capture.json()["pane_state"] == "unknown"
            files = observer.get(
                f"/api/control/v1/workspaces/{wid}/files",
                headers=read_headers,
            )
            assert files.status_code == 200
            assert "hello.txt" in {
                entry["name"] for entry in files.json()["entries"]
            }
            detail = observer.get(
                f"/api/control/v1/workspaces/{wid}/files/stat",
                params={"path": "hello.txt"}, headers=read_headers,
            )
            assert detail.status_code == 200
            assert detail.json()["entry"]["size"] == len("before\n")
            text_read = observer.get(
                f"/api/control/v1/workspaces/{wid}/files/text",
                params={"path": "hello.txt"}, headers=read_headers,
            )
            assert text_read.status_code == 200
            assert text_read.json()["content"] == "before\n"
            private = observer.get(
                f"/api/control/v1/workspaces/{wid}/files/text",
                params={"path": "container/.local/state/termroom/node/node-key.pem"},
                headers=read_headers,
            )
            assert private.status_code == 404

        # The actual browser uses the local A terminal, with a separate
        # outbound Node B performing all mutations under an independent token.
        with contextlib.ExitStack() as browser_stack:
            browser = None
            a_target = None
            before_a = None
            before_geometry = None
            if with_chrome:
                source_a = client.get(
                    f"/w/{env.source_workspace_id}/terminal"
                )
                assert source_a.status_code == 200
                a_match = re.search(
                    r'data-terminal-id="([a-f0-9]{32})"', source_a.text,
                )
                assert a_match
                a_terminal_id = a_match.group(1)
                a_workspace = env.store.get_workspace(env.source_workspace_id)
                a_terminal = env.store.get_terminal(a_terminal_id)
                assert a_workspace and a_terminal
                a_target = (
                    f"{a_workspace['tmux_session']}:{a_terminal['tmux_window']}"
                )
                cookie = client.cookies.get("termroom_session")
                assert cookie
                browser = browser_stack.enter_context(
                    open_chrome_page(tmp_path / "chrome-a-profile"),
                )
                for method in (
                    "Page.enable", "Page.bringToFront", "Runtime.enable",
                    "Network.enable",
                ):
                    browser.cdp(method)
                assert browser.cdp("Network.setCookie", {
                    "name": "termroom_session", "value": cookie,
                    "url": env.base_url + "/", "httpOnly": True,
                })["success"]
                browser.cdp("Page.navigate", {
                    "url": (
                        f"{env.base_url}/w/{env.source_workspace_id}"
                        f"/terminal?terminal={a_terminal_id}"
                    ),
                })
                wait_until(
                    lambda: browser.evaluate(
                        "document.readyState === 'complete' && "
                        "document.querySelector('#terminal')?.termroomControlsInput"
                        " === true"
                    ),
                    description="Chrome A actual PTY controller",
                )
                assert browser.evaluate("""(() => {
                  document.querySelector('#open-command-editor').click();
                  const input=document.querySelector('#command-input');
                  input.value='unsent Chrome A 한글 draft';
                  input.dispatchEvent(new Event('input',{bubbles:true}));
                  input.focus();input.setSelectionRange(7,13);
                  return input.value;
                })()""") == "unsent Chrome A 한글 draft"
                a_state_expr = """(() => {
                  const input=document.querySelector('#command-input');
                  return {
                    url:location.href,
                    selectedTerminal:document.querySelector(
                      '.terminal-tabs a.active')?.dataset.terminalActivityTab,
                    composerOpen:!document.querySelector(
                      '#terminal-command-editor').hidden,
                    focusId:document.activeElement?.id,
                    draft:input.value,start:input.selectionStart,
                    end:input.selectionEnd,pageScroll:window.scrollY
                  };
                })()"""
                def a_geometry() -> str:
                    assert a_target is not None
                    return subprocess.check_output(
                        [
                            "tmux", "display-message", "-p", "-t", a_target,
                            "#{pane_width}x#{pane_height}|#{pane_id}",
                        ],
                        text=True,
                    ).strip()

                before_geometry = a_geometry()
                stable_at = time.monotonic()
                deadline = stable_at + 8
                while time.monotonic() < deadline:
                    time.sleep(0.05)
                    current = a_geometry()
                    if current != before_geometry:
                        before_geometry = current
                        stable_at = time.monotonic()
                    if time.monotonic() - stable_at >= 0.8:
                        break
                else:
                    raise AssertionError("Chrome A geometry failed to settle")
                before_a = browser.evaluate(a_state_expr)
                assert before_a["selectedTerminal"] == a_terminal_id
                assert before_a["focusId"] == "command-input"

            files_url = f"/api/control/v1/workspaces/{wid}/files/text"
            qa_run_url = f"/api/control/v1/workspaces/{wid}/file-runs"
            with httpx.Client(base_url=env.base_url, timeout=20) as agent:
                observed = agent.get(
                    files_url, params={"path": "hello.txt"},
                    headers=operate_headers,
                )
                assert observed.status_code == 200, observed.text
                forbidden = agent.put(
                    files_url, headers=read_headers,
                    json={
                        "action_id": "t43-node-read-only-save",
                        "relative_path": "hello.txt", "content": "denied\n",
                        "expected_digest": observed.json()["digest"],
                        "expected_mtime_ns": observed.json()["mtime_ns"],
                    },
                )
                assert forbidden.status_code == 404
                saved = agent.put(
                    files_url, headers=operate_headers,
                    json={
                        "action_id": "t43-node-file-save-0001",
                        "relative_path": "hello.txt",
                        "content": "node-client-saved\n",
                        "expected_digest": observed.json()["digest"],
                        "expected_mtime_ns": observed.json()["mtime_ns"],
                    },
                )
                assert saved.status_code == 200, saved.text
                assert saved.json()["phase"] == "result_verified"
                assert (
                    env.node_root / "hello.txt"
                ).read_text(encoding="utf-8") == "node-client-saved\n"
                browser_view = client.get(f"/w/{wid}/view/hello.txt")
                assert browser_view.status_code == 200
                assert "node-client-saved" in browser_view.text

                payload = {
                    "action_id": "t43-node-file-run-0001",
                    "relative_path": "qa-control.py",
                    "expected_digest": hashlib.sha256(
                        (env.node_root / "qa-control.py").read_bytes()
                    ).hexdigest(),
                    "idempotency_key": str(uuid.uuid4()),
                }
                started = agent.post(
                    qa_run_url, headers=operate_headers, json=payload,
                )
                assert started.status_code in (200, 202), started.text
                run_id = started.json()["run"]["id"]
                run_terminal = started.json()["run"]["terminal_id"]
                repeated = agent.post(
                    qa_run_url, headers=operate_headers, json=payload,
                )
                assert repeated.status_code in (200, 202), repeated.text
                assert repeated.json()["result_resource_id"] == run_id
                receipt = agent.get(
                    "/api/control/v1/actions/t43-node-file-run-0001",
                    headers=operate_headers,
                )
                assert receipt.status_code == 200
                assert receipt.json()["result_resource_id"] == run_id
            completed = wait_until(
                lambda: _file_run_status_is(client, run_id, "finished"),
                description="non-UI Node File Run completion",
            )
            assert completed["exit_code"] == 0
            wait_until(
                lambda: "T43_NODE_API_RUN_OK" in client.get(
                    f"/w/{wid}/terminal/{run_terminal}/scrollback"
                ).text,
                description="Node real tmux QA run output",
            )
            if browser is not None:
                assert browser.evaluate(a_state_expr) == before_a
                assert a_geometry() == before_geometry
        assert client.get(f"/w/{wid}/files").status_code == 200
        assert client.get(f"/w/{wid}/recent").status_code == 200


@pytest.mark.skipif(shutil.which("tmux") is None, reason="Real tmux is required")
def test_real_node_private_files_and_large_editor_remain_confined(
    tmp_path: Path,
) -> None:
    """Private Node identity stays invisible; editor CAS and snapshots stay exact."""
    with connected_real_node(tmp_path) as env:
        client, csrf, wid = env.client, env.csrf, env.workspace_id
        files_page = client.get(f"/w/{wid}/files")
        assert files_page.status_code == 200
        assert "hello.txt" in files_page.text
        assert "node-key.pem" not in files_page.text
        recent = client.get(f"/w/{wid}/recent")
        assert recent.status_code == 200
        assert "hello.txt" in recent.text
        assert "node-key.pem" not in recent.text
        assert "shell" in recent.text
        private = "container/.local/state/termroom/node/node-key.pem"
        for route in (
            f"/w/{wid}/view/{private}",
            f"/w/{wid}/download/{private}",
        ):
            assert client.get(route).status_code >= 400
        folder_archive = client.get(f"/w/{wid}/archive/container")
        assert folder_archive.status_code == 200
        with zipfile.ZipFile(io.BytesIO(folder_archive.content)) as archive:
            names = archive.namelist()
            assert "container/archive-visible.txt" in names
            assert not any("node-key.pem" in item for item in names)
            assert not any("/termroom/node/" in item for item in names)
            assert archive.read("container/archive-visible.txt") == (
                b"ordinary archive sibling\n"
            )
        denied_archive = client.post(
            f"/w/{wid}/files/archive",
            data={
                "_csrf": csrf, "parent": "container/.local/state/termroom",
                "paths": "container/.local/state/termroom/node",
            },
        )
        assert denied_archive.status_code == 303
        assert "error=" in denied_archive.headers["location"]
        denied_create = client.post(
            f"/w/{wid}/files/create",
            data={
                "_csrf": csrf, "parent": "container/.local/state/termroom/node",
                "kind": "file", "name": "must-not-create.txt",
            },
        )
        assert denied_create.status_code == 303
        assert "error=" in denied_create.headers["location"]
        assert not (env.node_state / "must-not-create.txt").exists()

        for file_name, content in (
            ("hello.txt", "Node 한글 saved\n"),
            ("large.txt", "y" * (1024 * 1024)),
        ):
            editor = client.get(f"/w/{wid}/edit/{file_name}")
            assert editor.status_code == 200
            assert 'value="save_and_run"' not in editor.text
            digest = re.search(
                r'name="digest" value="([a-f0-9]{64})"', editor.text,
            )
            mtime = re.search(
                r'name="mtime_ns" value="([0-9]+)"', editor.text,
            )
            assert digest and mtime
            saved = client.post(
                f"/w/{wid}/edit/{file_name}",
                data={
                    "_csrf": csrf,
                    "digest": digest.group(1), "mtime_ns": mtime.group(1),
                    "content": content, "intent": "save",
                },
            )
            assert saved.status_code == 303, saved.text
            assert (env.node_root / file_name).read_text(
                encoding="utf-8"
            ) == content

        run_id = _create_remote_run(
            client, csrf,
            source_kind="workspace", target_id=env.node_id,
            command=(
                "cat hello.txt > node-source-result.txt; "
                "wc -c < large.txt > large-size.txt"
            ),
            source_workspace_id=wid, source_path=".",
        )
        finished = wait_until(
            lambda: _remote_run_status_is(client, run_id, "finished"),
            description="same-Node Workspace source after editor CAS",
        )
        assert finished["exit_code"] == 0
        assert (env.managed_root / run_id / "work" /
                "node-source-result.txt").read_text(
            encoding="utf-8"
        ) == "Node 한글 saved\n"
        assert int((env.managed_root / run_id / "work" /
                    "large-size.txt").read_text().strip()) == 1024 * 1024
        deleted = client.post(
            f"/remote-runs/{run_id}/delete", data={"_csrf": csrf},
        )
        assert deleted.status_code == 303
        assert not (env.managed_root / run_id).exists()


def _send_terminal_input(
    env: Any, terminal_id: str, data: str, expected_output: str,
) -> None:
    """Real authenticated WebSocket to actual Node-backed tmux pane."""
    cookie = env.client.cookies.get("termroom_session")
    assert cookie
    with websocket_connect(
        f"ws://127.0.0.1:{env.port}/ws/terminal/{terminal_id}",
        origin=env.base_url,
        additional_headers={"Cookie": f"termroom_session={cookie}"},
        open_timeout=5,
    ) as websocket:
        websocket.send(json.dumps({
            "kind": "input", "data": data, "rows": 24, "cols": 80,
            "user_input": True,
        }))
        output = ""
        deadline = time.monotonic() + 8
        while expected_output not in output and time.monotonic() < deadline:
            output += str(websocket.recv(timeout=2))
        assert expected_output in output, output


def _start_recoverable_run(env: Any) -> tuple[str, dict[str, Any], Path]:
    run_id = _create_remote_run(
        env.client, env.csrf,
        source_kind="workspace", target_id=env.node_id,
        command="printf 'once\\n' >> executions.txt; sleep 60",
        source_workspace_id=env.source_workspace_id, source_path=".",
    )
    running = wait_until(
        lambda: _remote_run_status_is(
            env.client, run_id, "running", connection="online",
        ),
        description="active Node Remote Run",
    )
    assert running["workspace_id"]
    record = env.managed_root / run_id / "work" / "executions.txt"
    wait_until(record.exists, description="Node Remote Run first side effect")
    assert record.read_text(encoding="utf-8") == "once\n"
    return run_id, running, record


def _stop_recoverable_run(
    env: Any, run_id: str, original_workspace_id: str, record: Path,
) -> None:
    stopped = env.client.post(
        f"/remote-runs/{run_id}/stop", data={"_csrf": env.csrf},
    )
    assert stopped.status_code == 303, stopped.text
    finished = wait_until(
        lambda: _remote_run_status_is(env.client, run_id, "stopped"),
        description="Node Remote Run interrupt",
    )
    assert finished["workspace_id"] == original_workspace_id
    assert record.read_text(encoding="utf-8") == "once\n"
    events = [
        event for event in env.store.list_activity_events(limit=100)
        if event.get("subject_type") == "remote_run"
        and event.get("subject_id") == run_id
    ]
    assert [event["kind"] for event in events] == ["remote_run.stopped"]
    deleted = env.client.post(
        f"/remote-runs/{run_id}/delete", data={"_csrf": env.csrf},
    )
    assert deleted.status_code == 303
    assert env.store.get_remote_run(run_id) is None
    assert not (env.managed_root / run_id).exists()


@pytest.mark.skipif(shutil.which("tmux") is None, reason="Real tmux required")
def test_real_node_core_restart_preserves_pty_runs_and_receipts(
    tmp_path: Path,
) -> None:
    """One idempotent control receipt, live File/Remote Runs, Core restart."""
    with connected_real_node(tmp_path) as env:
        wid = env.workspace_id
        operate_headers = _control_headers(
            env, "reconnect-e2e-operator", ["read", "operate"],
        )
        qa_url = f"/api/control/v1/workspaces/{wid}/file-runs"
        payload = {
            "action_id": "recovery-e2e-qa-run-0001",
            "relative_path": "qa-control.py",
            "expected_digest": hashlib.sha256(
                (env.node_root / "qa-control.py").read_bytes()
            ).hexdigest(),
            "idempotency_key": str(uuid.uuid4()),
        }
        with httpx.Client(base_url=env.base_url, timeout=20) as agent:
            started = agent.post(
                qa_url, headers=operate_headers, json=payload,
            )
            assert started.status_code in (200, 202), started.text
            qa_id = started.json()["run"]["id"]
            duplicate = agent.post(
                qa_url, headers=operate_headers, json=payload,
            )
            assert duplicate.status_code in (200, 202), duplicate.text
            assert duplicate.json()["result_resource_id"] == qa_id
            action_before = agent.get(
                "/api/control/v1/actions/recovery-e2e-qa-run-0001",
                headers=operate_headers,
            )
            assert action_before.status_code == 200
            action_before = action_before.json()
            assert action_before["result_resource_id"] == qa_id
        completed_qa = wait_until(
            lambda: _file_run_status_is(env.client, qa_id, "finished"),
            description="Node control File Run completion",
        )
        assert completed_qa["exit_code"] == 0

        interactive_id, terminal_id = _start_file_run(
            env.client, env.csrf, wid, "interactive.py",
            (env.node_root / "interactive.py").read_text(encoding="utf-8"),
        )
        wait_until(
            lambda: _file_run_status_is(env.client, interactive_id, "running"),
            description="Node interactive File Run before restart",
        )
        fresh = wait_until(
            lambda: _workspace_usage_is(
                env.client, wid, "fresh", minimum_processes=2,
            ),
            description="Node process sample before Core restart",
        )
        assert fresh["estimated"] is True
        assert fresh["sample"]["memory_bytes"] > 0
        assert fresh["sample"]["cpu_percent"] >= 0
        assert fresh["last_observed_at"]
        _send_terminal_input(
            env, env.terminal_id, "printf 'NODE_E2E_한글\\n'\r",
            "NODE_E2E_한글",
        )
        assert len(env.store.list_computers()) == 1
        assert len(env.store.list_workspaces_for_computer(env.node_id)) == 1
        assert len(env.store.list_terminals(wid)) == 2

        recovery_id, recovery_before, record = _start_recoverable_run(env)
        env.restart_core()
        restored = env.client.get(
            f"/w/{wid}/terminal/{env.terminal_id}/scrollback"
        )
        assert restored.status_code == 200
        assert "NODE_E2E_한글" in restored.text
        restored_file_run = wait_until(
            lambda: _file_run_status_is(
                env.client, interactive_id, "running",
            ),
            description="File Run reconciliation after Core restart",
        )
        assert restored_file_run["connection"] == "online"
        restored_remote = wait_until(
            lambda: _remote_run_status_is(
                env.client, recovery_id, "running", connection="online",
            ),
            description="Remote Run reconciliation after Core restart",
        )
        assert restored_remote["workspace_id"] == recovery_before["workspace_id"]
        assert record.read_text(encoding="utf-8") == "once\n"

        # A process restart cannot infer completion from a transport receipt,
        # run the same action again or destroy an observed receipt.
        with httpx.Client(base_url=env.base_url, timeout=20) as agent:
            receipt = agent.get(
                "/api/control/v1/actions/recovery-e2e-qa-run-0001",
                headers=operate_headers,
            )
            assert receipt.status_code == 200, receipt.text
            expected_status = (
                "observed" if action_before["status"] == "observed"
                else "unknown"
            )
            assert receipt.json()["status"] == expected_status
            assert receipt.json()["phase"] == action_before["phase"]
            assert receipt.json()["result_resource_id"] == qa_id
            replay = agent.post(
                qa_url, headers=operate_headers, json=payload,
            )
            assert replay.json()["status"] == expected_status
            assert replay.json()["result_resource_id"] == qa_id
            detail = agent.get(
                f"{qa_url}/{qa_id}", headers=operate_headers,
            )
            assert detail.status_code == 200, detail.text
            assert detail.json()["run"]["state"] == "finished"
            assert detail.json()["run"]["exit_code"] == 0

        output = env.client.get(
            f"/w/{wid}/terminal/{terminal_id}/scrollback",
        )
        assert output.status_code == 200
        assert "node-value:" in output.text
        _send_terminal_input(
            env, terminal_id, "after-restart\r",
            "NODE_FILE_RUN:after-restart",
        )
        done = wait_until(
            lambda: _file_run_status_is(
                env.client, interactive_id, "finished",
            ),
            description="interactive File Run completion after restart",
        )
        assert done["exit_code"] == 0
        assert (env.node_root / "interactive-result.txt").read_text(
            encoding="utf-8"
        ) == "after-restart"
        _stop_recoverable_run(
            env, recovery_id, recovery_before["workspace_id"], record,
        )
        assert any(
            event.get("kind") == "file_run.completed"
            and event.get("subject_id") == interactive_id
            for event in env.store.list_activity_events(limit=100)
        )
        # Existing per-Core CSRF secret persists across process restart.
        token = (env.core_state / "access-token").read_text().strip()
        assert hashlib.sha256(
            f"termroom-csrf:{token}".encode()
        ).hexdigest() == env.csrf


@pytest.mark.skipif(shutil.which("tmux") is None, reason="Real tmux required")
def test_real_node_disconnect_resume_stop_and_revocation(
    tmp_path: Path,
) -> None:
    """Node offline is unknown, reconnect resumes exact tmux, then stops."""
    with connected_real_node(tmp_path) as env:
        wid = env.workspace_id
        failed_id, run_terminal = _start_file_run(
            env.client, env.csrf, wid, "fail.py",
            (env.node_root / "fail.py").read_text(encoding="utf-8"),
        )
        failed = wait_until(
            lambda: _file_run_status_is(env.client, failed_id, "finished"),
            description="non-zero Node File Run",
        )
        assert failed["exit_code"] == 7
        stubborn_id, second_terminal = _start_file_run(
            env.client, env.csrf, wid, "stubborn.py",
            (env.node_root / "stubborn.py").read_text(encoding="utf-8"),
        )
        assert second_terminal == run_terminal
        wait_until(
            lambda: _file_run_status_is(env.client, stubborn_id, "running"),
            description="stubborn Node File Run",
        )
        fresh = wait_until(
            lambda: _workspace_usage_is(
                env.client, wid, "fresh", minimum_processes=2,
            ),
            description="Node usage before disconnect",
        )
        recovery_id, recovery_before, record = _start_recoverable_run(env)
        env.stop_node()
        wait_until(
            lambda: _remote_connection_state_is(
                env.client, env.node_id, "unavailable",
            ),
            description="Node disconnect",
        )
        offline = wait_until(
            lambda: _workspace_usage_is(env.client, wid, "offline"),
            description="Node offline Workspace usage",
        )
        assert offline["sample"] is None
        assert offline["last_observed_at"] == fresh["last_observed_at"]
        assert offline["age_seconds"] >= 0
        offline_file = wait_until(
            lambda: _file_run_status_is(env.client, stubborn_id, "running"),
            description="File Run preserved during Node disconnect",
        )
        assert offline_file["connection"] == "offline"
        offline_remote = wait_until(
            lambda: _remote_run_status_is(
                env.client, recovery_id, "running", connection="offline",
            ),
            description="Remote Run preserved during Node disconnect",
        )
        assert offline_remote["workspace_id"] == recovery_before["workspace_id"]
        env.restart_node()
        reconnected = wait_until(
            lambda: _workspace_usage_is(
                env.client, wid, "fresh", minimum_processes=2,
            ),
            description="Node usage after process reconnect",
        )
        assert reconnected["sample"] is not None
        file_online = wait_until(
            lambda: _file_run_status_is(env.client, stubborn_id, "running"),
            description="File Run after Node reconnect",
        )
        assert file_online["connection"] == "online"
        remote_online = wait_until(
            lambda: _remote_run_status_is(
                env.client, recovery_id, "running", connection="online",
            ),
            description="Remote Run after Node reconnect",
        )
        assert remote_online["workspace_id"] == recovery_before["workspace_id"]
        assert record.read_text(encoding="utf-8") == "once\n"
        _stop_recoverable_run(
            env, recovery_id, recovery_before["workspace_id"], record,
        )

        stopped = env.client.post(
            f"/file-runs/{stubborn_id}/stop",
            data={"_csrf": env.csrf, "return_to": "terminal"},
        )
        assert stopped.status_code == 303, stopped.text
        killed = env.client.post(
            f"/file-runs/{stubborn_id}/kill",
            data={"_csrf": env.csrf, "return_to": "terminal"},
        )
        assert killed.status_code == 303, killed.text
        wait_until(
            lambda: _file_run_status_is(env.client, stubborn_id, "stopped"),
            description="forced Node File Run stop",
        )
        events = [
            item for item in env.store.list_activity_events(limit=100)
            if item.get("subject_type") == "file_run"
            and item.get("subject_id") in {failed_id, stubborn_id}
        ]
        assert [item["kind"] for item in reversed(events)] == [
            "file_run.failed", "file_run.stopped",
        ]
        assert len({item["subject_id"] for item in events}) == 2
        assert len(env.store.list_computers()) == 1
        assert len(env.store.list_workspaces_for_computer(env.node_id)) == 1
        assert len(env.store.list_terminals(wid)) == 2

        revoked = env.client.post(
            f"/computers/{env.node_id}/revoke",
            data={"_csrf": env.csrf},
        )
        assert revoked.status_code == 303
        wait_until(
            lambda: _remote_connection_state_is(
                env.client, env.node_id, "unavailable",
            ),
            description="Node revocation",
        )
        time.sleep(1.5)
        assert not env.available(), "Revoked Node must not silently reconnect"
        computer = env.store.get_computer(env.node_id)
        assert computer is not None
        assert computer["connection_method"] == "node"
        assert computer["node_revoked_at"]
        assert not computer["host"] and not computer["username"]
        assert len(env.store.list_computers()) == 1
        assert len(env.store.list_workspaces_for_computer(env.node_id)) == 1
        assert len(env.store.list_terminals(wid)) == 2
