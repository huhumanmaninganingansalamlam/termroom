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
from termroom.node_protocol import NODE_REQUEST_OPERATIONS
from termroom.security import PathBoundaryError
from termroom.ssh_backend import SSHBackend
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
        f"@2|0|0|{first_launch}|{first_digest}\n"
        f"@3|1|2|{second_launch}|{second_digest}\n"
    ) == [
        {
            "tmux_window": "@2",
            "dead": False,
            "slot": 0,
            "launch_id": first_launch,
            "digest": first_digest,
        },
        {
            "tmux_window": "@3",
            "dead": True,
            "slot": 2,
            "launch_id": second_launch,
            "digest": second_digest,
        },
    ]
    with pytest.raises(ValueError, match="duplicate"):
        parse_tmux_workspace_command_records(
            f"@2|0|0|{first_launch}|{first_digest}\n"
            f"@3|1|0|{second_launch}|{second_digest}\n"
        )
    with pytest.raises(ValueError, match="slot"):
        parse_tmux_workspace_command_records(
            f"@2|0|3|{first_launch}|{first_digest}\n"
        )
    assert parse_tmux_workspace_command_records("@4|0|||\n") == []
    assert workspace_command_record_is_ready(
        f"@2|0|0|{first_launch}|{first_digest}\n",
        window="@2",
        slot=0,
        launch_id=first_launch,
        digest=first_digest,
    )
    assert not workspace_command_record_is_ready(
        "@2|0|||\n",
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
        WORKSPACE_COMMAND_WRAPPER.index('eval -- "$command"')
    )


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
            lambda: next(item for item in records() if item["slot"] == 0)["dead"],
            "Workspace command pane did not finish",
        )
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
            "Dead command slot was not replaced",
        )

        active_launch = uuid.uuid4().hex
        active = manager.run_workspace_command(
            workspace,
            slot=1,
            command="printf 'active\\n' >> active.txt; sleep 30",
            launch_id=active_launch,
        )
        _wait_until(lambda: (root / "active.txt").is_file(), "Active command did not start")
        reopened = manager.run_workspace_command(
            workspace,
            slot=1,
            command="printf 'must-not-run\\n' >> replaced.txt",
            launch_id=uuid.uuid4().hex,
        )
        assert reopened["tmux_window"] == active["tmux_window"]
        assert not (root / "replaced.txt").exists()

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
        saved = await client.post(
            f"/w/{workspace_id}/run-commands",
            data={
                "_csrf": settings.csrf_token,
                "commands": ["  uv run pytest  ", "ruff check ."],
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
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
        assert "uv run pytest" in page.text
        assert "ruff check ." in page.text
        assert page.text.count('name="commands"') == 3
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
        assert page.text.count(
            f'name="command_digest" value="{workspace_command_digest("ruff check .")}"'
        ) == 1

        invalid = await client.post(
            f"/w/{workspace_id}/run-commands",
            data={
                "_csrf": settings.csrf_token,
                "commands": ["one", "two", "three", "four"],
            },
            follow_redirects=False,
        )
        assert invalid.status_code == 303
        assert invalid.headers["location"].startswith(f"/w/{workspace_id}/terminal?")
        assert app.state.store.list_workspace_commands(workspace_id) == (
            "uv run pytest",
            "ruff check .",
        )

        app.state.store.replace_workspace_commands(
            workspace_id,
            ["uv run pytest", "ruff check --fix ."],
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
        ) == 1

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
            f"/w/{internal['id']}/run-commands",
            data={"_csrf": settings.csrf_token, "commands": ["pytest"]},
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
    state: dict[str, Any] = {"window": None, "dead": False}
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
                f"{state['window']}|{int(state['dead'])}|0|{launch_id}|"
                f"{workspace_command_digest(command)}\n"
            )
        if remote_command.startswith("window=$(tmux new-window"):
            state["window"] = "@9"
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

    assert first == replay == {"id": "run", "tmux_window": "@9"}
    starts = [item for item in executed if item.startswith("window=$(tmux new-window")]
    assert len(starts) == 1
    assert shlex.quote(f"TERMROOM_WORKSPACE_COMMAND={command}") in starts[0]
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
        assert "workspace.command.run" in NODE_REQUEST_OPERATIONS

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
