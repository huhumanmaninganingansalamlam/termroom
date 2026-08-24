from __future__ import annotations

import concurrent.futures
import re
import shlex
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from termroom.app import _workspace_context, create_app
from termroom.config import Settings
from termroom.db import StateStore, normalize_workspace_commands
from termroom.node_agent import NodeAgentError, NodeRuntime
from termroom.node_protocol import (
    NODE_REQUEST_OPERATIONS,
    NODE_WORKSPACE_COMMAND_CAPABILITY,
    NODE_WORKSPACE_COMMAND_VERSION,
)
from termroom.remote_access import RemoteAccess, RemoteAccessError
from termroom.security import PathBoundaryError
from termroom.ssh_backend import SSHBackend
from termroom.terminal_control import TerminalControl
from termroom.terminals import (
    TMUX_WORKSPACE_COMMAND_RECORD_FORMAT,
    WORKSPACE_COMMAND_WRAPPER,
    TerminalError,
    TerminalManager,
    parse_tmux_workspace_command_records,
    terminal_editor_digest,
    workspace_command_digest,
    workspace_command_record_is_ready,
)
from termroom.workspaces import RootManager, WorkspaceManager


def _local_workspace(tmp_path: Path) -> tuple[StateStore, dict[str, Any]]:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = WorkspaceManager(RootManager(root), store).open("project")
    return store, workspace


def _wait_until(predicate: Any, message: str, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(message)


async def _login(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/login",
        data={"password": "test-token"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_workspace_command_storage_migrates_and_replaces_atomically(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store = StateStore(database)
    store.initialize()
    with sqlite3.connect(database) as db:
        db.execute("ALTER TABLE workspaces DROP COLUMN workspace_commands_json")

    store.initialize()
    columns = {
        str(row[1])
        for row in sqlite3.connect(database).execute("PRAGMA table_info(workspaces)")
    }
    assert "workspace_commands_json" in columns

    root = tmp_path / "root"
    (root / "project").mkdir(parents=True)
    workspace = WorkspaceManager(RootManager(root), store).open("project")
    workspace_id = str(workspace["id"])
    assert store.list_workspace_commands(workspace_id) == ()

    assert store.replace_workspace_commands(
        workspace_id,
        ["  uv run pytest  ", "", "npm run build"],
    ) == ("uv run pytest", "npm run build")
    with pytest.raises(ValueError, match="one line"):
        store.replace_workspace_commands(workspace_id, ["uv run pytest\n"])
    assert store.list_workspace_commands(workspace_id) == (
        "uv run pytest",
        "npm run build",
    )
    assert store.replace_workspace_commands_if_current(
        workspace_id,
        ("uv run pytest", "npm run build"),
        ("uv run pytest -q", "npm run build"),
    ) == ("uv run pytest -q", "npm run build")
    assert (
        store.replace_workspace_commands_if_current(
            workspace_id,
            ("uv run pytest", "npm run build"),
            ("stale update",),
        )
        is None
    )
    assert store.list_workspace_commands(workspace_id) == (
        "uv run pytest -q",
        "npm run build",
    )

    internal = store.create_workspace(
        str(workspace["root_id"]),
        "internal",
        "internal",
        workspace_kind="remote_run",
    )
    with pytest.raises(ValueError, match="persistent Workspaces"):
        store.replace_workspace_commands(str(internal["id"]), ["pytest"])


@pytest.mark.parametrize(
    "commands",
    (
        ["one", "two", "three", "four"],
        ["echo\tunsafe"],
        ["echo\x00unsafe"],
        ["echo\u2028unsafe"],
        ["x" * 4097],
        [object()],
    ),
)
def test_workspace_command_validation_rejects_implicit_or_unsafe_forms(
    commands: list[object],
) -> None:
    with pytest.raises(ValueError):
        normalize_workspace_commands(commands)


def test_workspace_command_records_are_typed_and_unique() -> None:
    first_launch = uuid.uuid4().hex
    second_launch = uuid.uuid4().hex
    first_digest = workspace_command_digest("pytest")
    second_digest = workspace_command_digest("ruff check .")

    assert parse_tmux_workspace_command_records(
        f"@2|0|0|{first_launch}|{first_digest}|shell\n"
        f"@3|1|2|{second_launch}|{second_digest}|running\n"
    ) == [
        {
            "tmux_window": "@2",
            "dead": False,
            "slot": 0,
            "launch_id": first_launch,
            "digest": first_digest,
            "state": "shell",
        },
        {
            "tmux_window": "@3",
            "dead": True,
            "slot": 2,
            "launch_id": second_launch,
            "digest": second_digest,
            "state": "dead",
        },
    ]
    with pytest.raises(ValueError, match="duplicate"):
        parse_tmux_workspace_command_records(
            f"@2|0|0|{first_launch}|{first_digest}|running\n"
            f"@3|1|0|{second_launch}|{second_digest}|shell\n"
        )
    with pytest.raises(ValueError, match="slot"):
        parse_tmux_workspace_command_records(
            f"@2|0|3|{first_launch}|{first_digest}|running\n"
        )
    with pytest.raises(ValueError, match="record"):
        parse_tmux_workspace_command_records(
            f"@2|0|0|{first_launch}|{first_digest}\n"
        )
    assert (
        parse_tmux_workspace_command_records(
            f"@2|0|0|{first_launch}|{first_digest}|\n"
        )
        == []
    )
    assert parse_tmux_workspace_command_records("@4|0||||\n") == []
    assert workspace_command_record_is_ready(
        f"@2|0|0|{first_launch}|{first_digest}|shell\n",
        window="@2",
        slot=0,
        launch_id=first_launch,
        digest=first_digest,
    )
    assert not workspace_command_record_is_ready(
        "@2|0||||\n",
        window="@2",
        slot=0,
        launch_id=first_launch,
        digest=first_digest,
    )

    digest_publish = (
        'tmux set-window-option -t "$pane" '
        '@termroom_workspace_command_digest "$digest"'
    )
    launch_publish = (
        'tmux set-window-option -t "$pane" '
        '@termroom_workspace_command_launch "$launch"'
    )
    slot_publish = (
        'tmux set-window-option -t "$pane" '
        '@termroom_workspace_command_slot "$slot"'
    )
    assert WORKSPACE_COMMAND_WRAPPER.index(digest_publish) < (
        WORKSPACE_COMMAND_WRAPPER.index(launch_publish)
    )
    assert WORKSPACE_COMMAND_WRAPPER.index(launch_publish) < (
        WORKSPACE_COMMAND_WRAPPER.index(slot_publish)
    )
    assert WORKSPACE_COMMAND_WRAPPER.index(slot_publish) < (
        WORKSPACE_COMMAND_WRAPPER.index('/bin/bash --noprofile --norc -c "$command"')
    )
    assert 'remain-on-exit off' in WORKSPACE_COMMAND_WRAPPER
    assert 'remain-on-exit-format' not in WORKSPACE_COMMAND_WRAPPER
    assert 'printf "\\n✓\\n"' in WORKSPACE_COMMAND_WRAPPER
    assert 'printf "\\n✕ %s\\n" "$status"' in WORKSPACE_COMMAND_WRAPPER
    assert "@termroom_workspace_command_state running" in WORKSPACE_COMMAND_WRAPPER
    assert "@termroom_workspace_command_state settling" in WORKSPACE_COMMAND_WRAPPER
    assert "@termroom_workspace_command_state shell" in WORKSPACE_COMMAND_WRAPPER
    assert "tmux run-shell -b" in WORKSPACE_COMMAND_WRAPPER
    assert 'exec "$shell"' in WORKSPACE_COMMAND_WRAPPER
    assert WORKSPACE_COMMAND_WRAPPER.count("'") == 2


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_local_workspace_commands_run_at_root_and_reuse_managed_slots(
    tmp_path: Path,
) -> None:
    store, workspace = _local_workspace(tmp_path)
    manager = TerminalManager(store)
    root = Path(workspace["path"])
    session = str(workspace["tmux_session"])

    def records() -> list[dict[str, Any]]:
        result = manager._run_tmux(
            "list-windows",
            "-t",
            session,
            "-F",
            TMUX_WORKSPACE_COMMAND_RECORD_FORMAT,
        )
        return parse_tmux_workspace_command_records(result.stdout)

    try:
        first_launch = uuid.uuid4().hex
        first_command = "pwd > workspace-root.txt; printf 'first\\n' >> count.txt"
        first = manager.run_workspace_command(
            workspace,
            slot=0,
            command=first_command,
            launch_id=first_launch,
        )
        _wait_until(
            lambda: (root / "count.txt").is_file(),
            "Workspace command did not run",
        )
        _wait_until(
            lambda: next(item for item in records() if item["slot"] == 0)["state"]
            == "shell",
            "Workspace command did not return to its shell",
        )
        completion_output = manager._run_tmux(
            "capture-pane",
            "-p",
            "-t",
            str(first["tmux_window"]),
        ).stdout
        remain_on_exit = manager._run_tmux(
            "show-window-options",
            "-v",
            "-t",
            str(first["tmux_window"]),
            "remain-on-exit",
        ).stdout.strip()
        assert "✓" in completion_output
        assert remain_on_exit == "off"
        replay = manager.run_workspace_command(
            workspace,
            slot=0,
            command=first_command,
            launch_id=first_launch,
        )
        assert replay["tmux_window"] == first["tmux_window"]
        assert (root / "workspace-root.txt").read_text().strip() == str(root)
        assert (root / "count.txt").read_text().splitlines() == ["first"]

        with pytest.raises(TerminalError, match="reused for another command"):
            manager.run_workspace_command(
                workspace,
                slot=0,
                command="printf 'conflict\\n' >> count.txt",
                launch_id=first_launch,
            )

        second = manager.run_workspace_command(
            workspace,
            slot=0,
            command="printf 'second\\n' >> count.txt",
            launch_id=uuid.uuid4().hex,
        )
        assert second["tmux_window"] != first["tmux_window"]
        _wait_until(
            lambda: (root / "count.txt").read_text().splitlines()
            == ["first", "second"],
            "Changed command did not start in a new managed Terminal",
        )
        _wait_until(
            lambda: next(item for item in records() if item["slot"] == 0)["state"]
            == "shell",
            "Restarted Workspace command did not return to its shell",
        )
        manager._run_tmux(
            "send-keys",
            "-t",
            str(second["tmux_window"]),
            "printf 'inspect\\n' > inspect.txt",
            "Enter",
        )
        inspect_file = root / "inspect.txt"
        _wait_until(
            lambda: inspect_file.is_file() and inspect_file.read_text().strip() == "inspect",
            "Workspace command shell did not accept follow-up input",
        )
        detached_slot = manager._run_tmux(
            "show-window-options",
            "-v",
            "-t",
            str(first["tmux_window"]),
            "@termroom_workspace_command_slot",
            check=False,
        )
        assert detached_slot.returncode != 0
        detached_name = manager._run_tmux(
            "display-message",
            "-p",
            "-t",
            str(first["tmux_window"]),
            "#{window_name}",
        ).stdout.strip()
        assert detached_name == f"run-1-{first_launch[:4]}"

        active_launch = uuid.uuid4().hex
        active = manager.run_workspace_command(
            workspace,
            slot=1,
            command="printf 'active\\n' >> active.txt; sleep 30",
            launch_id=active_launch,
        )
        _wait_until(lambda: (root / "active.txt").is_file(), "Active command did not start")
        replacement = manager.run_workspace_command(
            workspace,
            slot=1,
            command="printf 'replacement\\n' >> replaced.txt",
            launch_id=uuid.uuid4().hex,
        )
        assert replacement["tmux_window"] != active["tmux_window"]
        _wait_until(
            lambda: (root / "replaced.txt").read_text().splitlines() == ["replacement"],
            "Changed active command did not start in its own Terminal",
        )

        concurrent_launch = uuid.uuid4().hex
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    manager.run_workspace_command,
                    workspace,
                    slot=2,
                    command="printf 'once\\n' >> concurrent.txt; sleep 30",
                    launch_id=concurrent_launch,
                )
                for _ in range(2)
            ]
            concurrent_results = [future.result() for future in futures]
        assert concurrent_results[0]["tmux_window"] == concurrent_results[1][
            "tmux_window"
        ]
        _wait_until(
            lambda: (root / "concurrent.txt").is_file(),
            "Concurrent command did not start",
        )
        assert (root / "concurrent.txt").read_text().splitlines() == ["once"]
    finally:
        manager._run_tmux("kill-server", check=False)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_local_workspace_command_failure_keeps_clean_output(tmp_path: Path) -> None:
    store, workspace = _local_workspace(tmp_path)
    manager = TerminalManager(store)
    session = str(workspace["tmux_session"])

    try:
        terminal = manager.run_workspace_command(
            workspace,
            slot=0,
            command="printf 'failure-output\\n'; exit 7",
            launch_id=uuid.uuid4().hex,
        )

        def finished() -> bool:
            result = manager._run_tmux(
                "display-message",
                "-p",
                "-t",
                str(terminal["tmux_window"]),
                TMUX_WORKSPACE_COMMAND_RECORD_FORMAT,
                check=False,
            )
            if result.returncode:
                return False
            records = parse_tmux_workspace_command_records(result.stdout)
            return bool(records and records[0]["state"] == "shell")

        _wait_until(finished, "Failed Workspace command did not return to its shell")
        output = manager._run_tmux(
            "capture-pane",
            "-p",
            "-t",
            str(terminal["tmux_window"]),
        ).stdout
        remain_on_exit = manager._run_tmux(
            "show-window-options",
            "-v",
            "-t",
            str(terminal["tmux_window"]),
            "remain-on-exit",
        ).stdout.strip()

        assert "failure-output" in output
        assert "✕ 7" in output
        assert "Pane is dead" not in output
        assert remain_on_exit == "off"
        pane_state = manager._run_tmux(
            "display-message",
            "-p",
            "-t",
            str(terminal["tmux_window"]),
            "#{pane_dead}|#{pane_current_command}",
        ).stdout.strip()
        assert pane_state.startswith("0|")
    finally:
        manager._run_tmux("kill-session", "-t", session, check=False)


@pytest.mark.asyncio
async def test_workspace_command_http_uses_only_server_saved_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    (root / "project").mkdir(parents=True)
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    workspace_id = str(workspace["id"])
    captured: list[dict[str, Any]] = []

    def run_workspace_command(
        current: dict[str, Any],
        *,
        slot: int,
        command: str,
        launch_id: str,
    ) -> dict[str, Any]:
        captured.append(
            {
                "workspace": current,
                "slot": slot,
                "command": command,
                "launch_id": launch_id,
                "thread_id": threading.get_ident(),
            }
        )
        return {"id": "managed-terminal"}

    monkeypatch.setattr(
        app.state.terminals,
        "run_workspace_command",
        run_workspace_command,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        first_saved = await client.post(
            f"/w/{workspace_id}/run-commands/add",
            data={"_csrf": settings.csrf_token, "command": "  uv run pytest  "},
            follow_redirects=False,
        )
        second_saved = await client.post(
            f"/w/{workspace_id}/run-commands/add",
            data={"_csrf": settings.csrf_token, "command": "ruff check ."},
            follow_redirects=False,
        )
        assert first_saved.status_code == second_saved.status_code == 303
        assert app.state.store.list_workspace_commands(workspace_id) == (
            "uv run pytest",
            "ruff check .",
        )

        page = await client.get(f"/w/{workspace_id}/files")
        assert page.status_code == 200
        assert page.text.count("data-workspace-run-menu") == 1
        nav_start = page.text.index('<nav class="bottom-nav"')
        nav_end = page.text.index("</nav>", nav_start)
        run_menu = page.text.index("workspace-run-menu-navigation")
        assert (
            nav_start
            < page.text.index(f'href="/w/{workspace_id}/recent"', nav_start)
            < run_menu
            < nav_end
        )
        assert f'action="/w/{workspace_id}/run-commands/0"' in page.text
        assert f'action="/w/{workspace_id}/run-commands/1"' in page.text
        assert f'action="/w/{workspace_id}/run-commands/0/save"' in page.text
        assert f'action="/w/{workspace_id}/run-commands/1/save"' in page.text
        assert f'action="/w/{workspace_id}/run-commands/add"' in page.text
        assert "uv run pytest" in page.text
        assert "ruff check ." in page.text
        assert page.text.count('name="command"') == 3
        cards = re.findall(
            r'<article class="workspace-run-command-card[^"]*"\s+'
            r'data-workspace-run-command-card(?P<attributes>[^>]*)>',
            page.text,
        )
        assert len(cards) == 3
        assert all("hidden" not in attributes for attributes in cards[:2])
        assert "hidden" in cards[2]
        assert page.text.count("data-workspace-run-command-add") == 1
        assert "Add command" in page.text
        assert page.text.count("data-workspace-command-delete") == 2
        assert f'action="/w/{workspace_id}/run-commands/0/delete"' in page.text
        assert f'action="/w/{workspace_id}/run-commands/1/delete"' in page.text
        assert page.text.count(
            f'name="command_digest" value="{workspace_command_digest("ruff check .")}"'
        ) == 2

        added = await client.post(
            f"/w/{workspace_id}/run-commands/add",
            data={"_csrf": settings.csrf_token, "command": "python -m build"},
            follow_redirects=False,
        )
        assert added.status_code == 303
        assert app.state.store.list_workspace_commands(workspace_id) == (
            "uv run pytest",
            "ruff check .",
            "python -m build",
        )
        over_limit = await client.post(
            f"/w/{workspace_id}/run-commands/add",
            data={"_csrf": settings.csrf_token, "command": "echo fourth"},
            follow_redirects=False,
        )
        assert over_limit.status_code == 303
        assert over_limit.headers["location"].startswith(
            f"/w/{workspace_id}/terminal?error="
        )
        app.state.store.replace_workspace_commands(
            workspace_id,
            ["uv run pytest", "ruff check ."],
        )

        removed_legacy_endpoint = await client.post(
            f"/w/{workspace_id}/run-commands",
            data={
                "_csrf": settings.csrf_token,
                "commands": ["one", "two", "three", "four"],
            },
            follow_redirects=False,
        )
        assert removed_legacy_endpoint.status_code == 404
        assert app.state.store.list_workspace_commands(workspace_id) == (
            "uv run pytest",
            "ruff check .",
        )

        saved_one = await client.post(
            f"/w/{workspace_id}/run-commands/1/save",
            data={
                "_csrf": settings.csrf_token,
                "command_digest": workspace_command_digest("ruff check ."),
                "command": "ruff check --fix .",
            },
            follow_redirects=False,
        )
        assert saved_one.status_code == 303
        assert app.state.store.list_workspace_commands(workspace_id) == (
            "uv run pytest",
            "ruff check --fix .",
        )
        stale_save = await client.post(
            f"/w/{workspace_id}/run-commands/1/save",
            data={
                "_csrf": settings.csrf_token,
                "command_digest": workspace_command_digest("ruff check ."),
                "command": "ruff check --unsafe-fixes .",
            },
            follow_redirects=False,
        )
        assert stale_save.status_code == 303
        assert stale_save.headers["location"].startswith(
            f"/w/{workspace_id}/terminal?error="
        )
        stale = await client.post(
            f"/w/{workspace_id}/run-commands/1",
            data={
                "_csrf": settings.csrf_token,
                "launch_id": uuid.uuid4().hex,
                "command_digest": workspace_command_digest("ruff check ."),
                "command": "printf attacker-controlled",
            },
            follow_redirects=False,
        )
        assert stale.status_code == 303
        assert stale.headers["location"].startswith(f"/w/{workspace_id}/terminal?error=")
        assert captured == []

        refreshed_page = await client.get(f"/w/{workspace_id}/files")
        assert refreshed_page.status_code == 200
        assert "ruff check --fix ." in refreshed_page.text
        assert refreshed_page.text.count(
            f'name="command_digest" value="{workspace_command_digest("ruff check --fix .")}"'
        ) == 2

        launch_id = uuid.uuid4().hex
        started = await client.post(
            f"/w/{workspace_id}/run-commands/1",
            data={
                "_csrf": settings.csrf_token,
                "launch_id": launch_id,
                "command_digest": workspace_command_digest("ruff check --fix ."),
                "command": "printf attacker-controlled",
            },
            follow_redirects=False,
        )
        assert started.status_code == 303
        assert "terminal=managed-terminal" in started.headers["location"]
        assert captured[0]["slot"] == 1
        assert captured[0]["command"] == "ruff check --fix ."
        assert captured[0]["launch_id"] == launch_id
        assert captured[0]["thread_id"] != threading.get_ident()

        missing_csrf = await client.post(
            f"/w/{workspace_id}/run-commands/0",
            data={"launch_id": uuid.uuid4().hex},
        )
        assert missing_csrf.status_code == 403

        computer = app.state.store.create_computer(
            name="Build server",
            ssh_alias="",
            host="build.example.test",
            port=22,
            username="runner",
            identity_file="",
            host_key_type="ssh-ed25519",
            host_key_data="AAAATESTKEY",
            host_fingerprint="SHA256:test",
        )
        virtual_root = app.state.store.ensure_root_value(f"ssh://{computer['id']}")
        internal = app.state.store.create_workspace(
            str(virtual_root["id"]),
            ".termroom-server-terminal",
            "Build server",
            backend_kind="remote",
            computer_id=str(computer["id"]),
            canonical_path="/home/runner",
            workspace_kind="server_terminal",
        )
        blocked = await client.post(
            f"/w/{internal['id']}/run-commands/add",
            data={"_csrf": settings.csrf_token, "command": "pytest"},
        )
        assert blocked.status_code == 404

    refreshed = app.state.workspaces.require(workspace_id)
    first_context = _workspace_context(
        settings,
        refreshed,
        active_tab="terminal",
        workspace_commands_supported=True,
    )
    second_context = _workspace_context(
        settings,
        refreshed,
        active_tab="terminal",
        workspace_commands_supported=True,
    )
    unsupported_context = _workspace_context(
        settings,
        refreshed,
        active_tab="terminal",
        workspace_commands_supported=False,
    )
    assert [item["slot"] for item in first_context["workspace_commands"]] == [0, 1]
    assert [item["command"] for item in first_context["workspace_commands"]] == [
        "uv run pytest",
        "ruff check --fix .",
    ]
    assert [item["command_digest"] for item in first_context["workspace_commands"]] == [
        workspace_command_digest("uv run pytest"),
        workspace_command_digest("ruff check --fix ."),
    ]
    assert {
        item["launch_id"] for item in first_context["workspace_commands"]
    }.isdisjoint(item["launch_id"] for item in second_context["workspace_commands"])
    assert first_context["workspace_commands_visible"] is True
    assert first_context["workspace_commands_supported"] is True
    assert unsupported_context["workspace_commands_visible"] is True
    assert unsupported_context["workspace_commands_supported"] is False


@pytest.mark.asyncio
async def test_workspace_command_delete_uses_only_the_saved_digest(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "project").mkdir(parents=True)
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    workspace_id = str(workspace["id"])
    app.state.store.replace_workspace_commands(
        workspace_id,
        ["uv run pytest", "ruff check ."],
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        stale = await client.post(
            f"/w/{workspace_id}/run-commands/0/delete",
            data={
                "_csrf": settings.csrf_token,
                "command_digest": workspace_command_digest("changed elsewhere"),
            },
            follow_redirects=False,
        )
        assert stale.status_code == 303
        assert stale.headers["location"].startswith(f"/w/{workspace_id}/terminal?error=")
        assert app.state.store.list_workspace_commands(workspace_id) == (
            "uv run pytest",
            "ruff check .",
        )

        deleted = await client.post(
            f"/w/{workspace_id}/run-commands/0/delete",
            data={
                "_csrf": settings.csrf_token,
                "command_digest": workspace_command_digest("uv run pytest"),
            },
            follow_redirects=False,
        )
        assert deleted.status_code == 303
        assert deleted.headers["location"] == f"/w/{workspace_id}"
        assert app.state.store.list_workspace_commands(workspace_id) == ("ruff check .",)

        missing_csrf = await client.post(
            f"/w/{workspace_id}/run-commands/0/delete",
            data={"command_digest": workspace_command_digest("ruff check .")},
        )
        assert missing_csrf.status_code == 403


@pytest.mark.asyncio
async def test_node_workspace_commands_require_the_persistent_shell_contract() -> None:
    access = RemoteAccess(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        TerminalControl(),
    )
    workspace = {
        "id": "workspace",
        "computer": {"id": "node", "connection_method": "node"},
    }
    calls: list[tuple[str, dict[str, Any]]] = []
    capability = {"enabled": False}

    access.is_node = lambda _workspace: True  # type: ignore[method-assign]
    access.supports_capability = (  # type: ignore[method-assign]
        lambda _workspace, name: capability["enabled"]
        and name == NODE_WORKSPACE_COMMAND_CAPABILITY
    )
    access._terminal_record = lambda value: dict(value)  # type: ignore[method-assign]
    access._reconcile_terminals = (  # type: ignore[method-assign]
        lambda _workspace, values: [dict(item) for item in values]
    )

    async def request(  # type: ignore[no-untyped-def]
        _workspace,
        operation,
        payload,
    ) -> dict[str, Any]:
        calls.append((str(operation), dict(payload)))
        terminal = {"id": "terminal", "tmux_window": "@7"}
        return {"terminal": terminal, "terminals": [terminal]}

    access._workspace_request = request  # type: ignore[method-assign]

    with pytest.raises(RemoteAccessError, match="update Termroom Node") as unsupported:
        await access.run_workspace_command(
            workspace,
            slot=0,
            command="pytest",
            launch_id=uuid.uuid4().hex,
        )
    assert unsupported.value.code == "capability_unsupported"
    assert calls == []

    capability["enabled"] = True
    launch_id = uuid.uuid4().hex
    terminal = await access.run_workspace_command(
        workspace,
        slot=0,
        command="pytest",
        launch_id=launch_id,
    )
    assert terminal == {"id": "terminal", "tmux_window": "@7"}
    assert calls == [
        (
            "workspace.command.run",
            {
                "workspace_command_version": NODE_WORKSPACE_COMMAND_VERSION,
                "slot": 0,
                "command": "pytest",
                "launch_id": launch_id,
            },
        )
    ]


def test_ssh_workspace_command_quotes_user_text_and_replays_one_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    backend = SSHBackend(store, tmp_path / "state")
    workspace = {
        "id": "workspace-id",
        "tmux_session": "termroom-remote-workspace",
        "remote_path": "/srv/project with spaces",
        "computer": {"id": "computer-id"},
    }
    launch_id = uuid.uuid4().hex
    command = "printf '%s' \"$(touch must-not-run-locally)\"; echo done"
    state: dict[str, Any] = {
        "window": None,
        "dead": False,
        "command_state": "running",
        "launch_id": launch_id,
        "command": command,
    }
    restart_request: dict[str, str] = {}
    executed: list[str] = []

    def terminals(_workspace: dict[str, Any]) -> list[dict[str, Any]]:
        items = [{"id": "shell", "tmux_window": "@1"}]
        if state["window"]:
            items.append({"id": "run", "tmux_window": state["window"]})
        return items

    def execute(_computer: dict[str, Any], remote_command: str) -> str:
        executed.append(remote_command)
        if remote_command.startswith("tmux list-windows"):
            if not state["window"]:
                return ""
            return (
                f"{state['window']}|{int(state['dead'])}|0|{state['launch_id']}|"
                f"{workspace_command_digest(str(state['command']))}|"
                f"{state['command_state']}\n"
            )
        if remote_command.startswith("window=$(tmux new-window"):
            state["window"] = "@9"
            return "@9\n"
        if remote_command.startswith("tmux respawn-pane -k"):
            state["launch_id"] = restart_request["launch_id"]
            state["command"] = restart_request["command"]
            state["command_state"] = "running"
            return "@9\n"
        raise AssertionError(f"Unexpected SSH command: {remote_command}")

    monkeypatch.setattr(backend, "ensure_workspace", terminals)
    monkeypatch.setattr(backend, "_exec", execute)

    first = backend.run_workspace_command(
        workspace,
        slot=0,
        command=command,
        launch_id=launch_id,
    )
    replay = backend.run_workspace_command(
        workspace,
        slot=0,
        command=command,
        launch_id=launch_id,
    )
    state["command_state"] = "shell"
    restarted_launch = uuid.uuid4().hex
    restarted_command = command
    restart_request.update(
        {"launch_id": restarted_launch, "command": restarted_command}
    )
    restarted = backend.run_workspace_command(
        workspace,
        slot=0,
        command=restarted_command,
        launch_id=restarted_launch,
    )

    assert first == replay == restarted == {"id": "run", "tmux_window": "@9"}
    starts = [item for item in executed if item.startswith("window=$(tmux new-window")]
    restarts = [item for item in executed if item.startswith("tmux respawn-pane -k")]
    assert len(starts) == 1
    assert len(restarts) == 1
    assert shlex.quote(f"TERMROOM_WORKSPACE_COMMAND={command}") in starts[0]
    assert shlex.quote(f"TERMROOM_WORKSPACE_COMMAND={restarted_command}") in restarts[0]
    assert shlex.quote("/srv/project with spaces") in starts[0]
    assert not (Path.cwd() / "must-not-run-locally").exists()


def test_ssh_terminal_editor_quotes_the_file_and_reuses_its_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    backend = SSHBackend(store, tmp_path / "state")
    workspace = {
        "id": "workspace-id",
        "tmux_session": "termroom-remote-workspace",
        "remote_path": "/srv/project with spaces",
        "computer": {"id": "computer-id"},
    }
    relative_path = "src/a $(touch must-not-run-locally).txt"
    digest = terminal_editor_digest(relative_path)
    state: dict[str, Any] = {"window": None}
    executed: list[str] = []

    def terminals(_workspace: dict[str, Any]) -> list[dict[str, Any]]:
        items = [{"id": "shell", "tmux_window": "@1"}]
        if state["window"]:
            items.append({"id": "vim", "tmux_window": state["window"]})
        return items

    def execute(_computer: dict[str, Any], remote_command: str) -> str:
        executed.append(remote_command)
        if remote_command.startswith("command -v nvim"):
            return ""
        if remote_command.startswith("tmux list-windows"):
            return f"{state['window']}|0|{digest}\n" if state["window"] else ""
        if remote_command.startswith("window=$(tmux new-window"):
            state["window"] = "@9"
            return "@9\n"
        raise AssertionError(f"Unexpected SSH command: {remote_command}")

    monkeypatch.setattr(backend, "ensure_workspace", terminals)
    monkeypatch.setattr(backend, "_exec", execute)

    first = backend.open_terminal_editor(workspace, relative_path)
    replay = backend.open_terminal_editor(workspace, relative_path)

    assert first == replay == {"id": "vim", "tmux_window": "@9"}
    starts = [item for item in executed if item.startswith("window=$(tmux new-window")]
    assert len(starts) == 1
    assert shlex.quote(
        "TERMROOM_TERMINAL_EDITOR_FILE="
        "/srv/project with spaces/src/a $(touch must-not-run-locally).txt"
    ) in starts[0]
    assert not (Path.cwd() / "must-not-run-locally").exists()


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_node_workspace_command_revalidates_boundary_and_replays(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    workspace = allowed / "project"
    outside = tmp_path / "outside"
    workspace.mkdir(parents=True)
    outside.mkdir()
    runtime = NodeRuntime([allowed])
    session = f"termroom-node-{uuid.uuid4().hex[:12]}"
    launch_id = uuid.uuid4().hex
    payload = {
        "workspace_command_version": NODE_WORKSPACE_COMMAND_VERSION,
        "workspace_path": str(workspace),
        "tmux_session": session,
        "slot": 0,
        "command": "printf 'node\\n' >> node.txt",
        "launch_id": launch_id,
    }

    try:
        first = runtime._handle_sync("workspace.command.run", payload)
        replay = runtime._handle_sync("workspace.command.run", payload)
        assert first["terminal"]["tmux_window"] == replay["terminal"]["tmux_window"]
        _wait_until(lambda: (workspace / "node.txt").is_file(), "Node command did not run")
        assert (workspace / "node.txt").read_text().splitlines() == ["node"]
        window = str(first["terminal"]["tmux_window"])

        def command_state(target_window: str) -> str:
            result = runtime._tmux(
                "display-message",
                "-p",
                "-t",
                target_window,
                TMUX_WORKSPACE_COMMAND_RECORD_FORMAT,
                check=False,
            )
            if result.returncode:
                return ""
            records = parse_tmux_workspace_command_records(result.stdout)
            return str(records[0]["state"]) if records else ""

        _wait_until(
            lambda: command_state(window) == "shell",
            "Node command shell did not remain",
        )
        restarted = runtime._handle_sync(
            "workspace.command.run",
            {
                **payload,
                "command": "printf 'node-2\\n' >> node.txt",
                "launch_id": uuid.uuid4().hex,
            },
        )
        restarted_window = str(restarted["terminal"]["tmux_window"])
        assert restarted_window != window
        _wait_until(
            lambda: (workspace / "node.txt").read_text().splitlines()
            == ["node", "node-2"],
            "Changed Node command did not start in a new Terminal",
        )
        _wait_until(
            lambda: command_state(restarted_window) == "shell",
            "Restarted Node command shell did not remain",
        )
        old_slot = runtime._tmux(
            "show-window-options",
            "-v",
            "-t",
            window,
            "@termroom_workspace_command_slot",
            check=False,
        )
        assert old_slot.returncode != 0
        old_name = runtime._tmux(
            "display-message",
            "-p",
            "-t",
            window,
            "#{window_name}",
        ).stdout.strip()
        assert old_name == f"run-1-{launch_id[:4]}"
        assert "workspace.command.run" in NODE_REQUEST_OPERATIONS

        with pytest.raises(NodeAgentError) as incompatible_version:
            runtime._handle_sync(
                "workspace.command.run",
                {**payload, "workspace_command_version": 1},
            )
        assert incompatible_version.value.code == "workspace_command_version_incompatible"

        with pytest.raises(NodeAgentError) as invalid_slot:
            runtime._handle_sync("workspace.command.run", {**payload, "slot": 3})
        assert invalid_slot.value.code == "workspace_command_invalid"
        with pytest.raises(NodeAgentError) as invalid_launch:
            runtime._handle_sync(
                "workspace.command.run",
                {**payload, "launch_id": str(uuid.uuid4())},
            )
        assert invalid_launch.value.code == "workspace_command_invalid"
        with pytest.raises(PathBoundaryError):
            runtime._handle_sync(
                "workspace.command.run",
                {**payload, "workspace_path": str(outside)},
            )
    finally:
        runtime._tmux("kill-server", check=False)
