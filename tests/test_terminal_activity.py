from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from termroom.db import StateStore
from termroom.node_agent import NodeAgentError, NodeRuntime
from termroom.node_core import NodeCore
from termroom.remote_access import RemoteAccess
from termroom.ssh_backend import SSHBackend
from termroom.terminal_control import TerminalControl
from termroom.terminals import TerminalManager
from termroom.workspaces import RootManager, WorkspaceManager

_PROVIDER_EPOCH_SECONDS = 1_700_000_000


def _provider_seconds(offset: int) -> int:
    return _PROVIDER_EPOCH_SECONDS + offset


def _revision(offset: int) -> int:
    return _provider_seconds(offset)


def _workspace(store: StateStore, root: Path, name: str) -> dict[str, object]:
    (root / name).mkdir(parents=True)
    return WorkspaceManager(RootManager(root), store).open(name)


def _record(
    window: str,
    name: str,
    activity_at: int,
    *,
    role: str = "shell",
    managed_run_id: str | None = None,
) -> dict[str, str | int | None]:
    return {
        "tmux_window": window,
        "name": name,
        "role": role,
        "managed_run_id": managed_run_id,
        "activity_at": (
            _provider_seconds(activity_at)
            if 0 <= activity_at < _PROVIDER_EPOCH_SECONDS
            else activity_at
        ),
    }


def test_first_terminal_activity_observation_baselines_without_unread(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = _workspace(store, root, "project")
    workspace_id = str(workspace["id"])
    terminal = store.reconcile_terminals(
        workspace_id,
        [_record("@1", "shell", 100)],
    )[0]

    first = store.terminal_activity_summary("browser-a")

    assert first == {
        "terminals": [
            {
                "terminal_id": terminal["id"],
                "workspace_id": workspace_id,
                "activity_at": _revision(100),
                "acknowledged_activity_at": _revision(100),
                "unread": False,
            }
        ],
        "workspaces": [
            {
                "workspace_id": workspace_id,
                "terminal_count": 1,
                "unread_terminal_count": 0,
                "unread_count": 0,
                "latest_unread_terminal_id": None,
            }
        ],
        "unread_count": 0,
        "latest_unread_terminal_id": None,
    }

    store.reconcile_terminals(workspace_id, [_record("@1", "shell", 101)])
    second = store.terminal_activity_summary("browser-a")

    assert second["unread_count"] == 1
    assert second["latest_unread_terminal_id"] == terminal["id"]
    assert second["terminals"][0] == {
        "terminal_id": terminal["id"],
        "workspace_id": workspace_id,
        "activity_at": _revision(101),
        "acknowledged_activity_at": _revision(100),
        "unread": True,
    }


def test_terminal_activity_is_per_device_shell_only_and_filterable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    first_workspace = _workspace(store, root, "first")
    second_workspace = _workspace(store, root, "second")
    first_workspace_id = str(first_workspace["id"])
    second_workspace_id = str(second_workspace["id"])
    managed_run_id = "run-managed"
    first_shell, managed = store.reconcile_terminals(
        first_workspace_id,
        [
            _record("@1", "shell", 10),
            _record(
                "@2",
                "Run",
                999,
                role="file_run",
                managed_run_id=managed_run_id,
            ),
        ],
    )
    second_shell = store.reconcile_terminals(
        second_workspace_id,
        [_record("@3", "logs", 20)],
    )[0]

    assert store.terminal_activity_summary("browser-a")["unread_count"] == 0
    store.reconcile_terminals(
        first_workspace_id,
        [
            _record("@1", "shell", 11),
            _record(
                "@2",
                "Run",
                1000,
                role="file_run",
                managed_run_id=managed_run_id,
            ),
        ],
    )
    store.reconcile_terminals(
        second_workspace_id,
        [_record("@3", "logs", 21)],
    )

    first_device = store.terminal_activity_summary("browser-a")
    new_device = store.terminal_activity_summary("browser-b")
    first_workspace_view = store.terminal_activity_summary(
        "browser-a", workspace_id=first_workspace_id
    )
    terminal_view = store.terminal_activity_summary(
        "browser-a", terminal_id=str(second_shell["id"])
    )

    assert first_device["unread_count"] == 2
    assert {item["terminal_id"] for item in first_device["terminals"]} == {
        first_shell["id"],
        second_shell["id"],
    }
    assert managed["id"] not in {
        item["terminal_id"] for item in first_device["terminals"]
    }
    assert new_device["unread_count"] == 0
    assert first_workspace_view["unread_count"] == 1
    assert [item["terminal_id"] for item in first_workspace_view["terminals"]] == [
        first_shell["id"]
    ]
    assert terminal_view["terminals"] == [
        {
            "terminal_id": second_shell["id"],
            "workspace_id": second_workspace_id,
            "activity_at": _revision(21),
            "acknowledged_activity_at": _revision(20),
            "unread": True,
        }
    ]


def test_terminal_activity_revisions_never_move_backwards_or_include_unknown_data(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = _workspace(store, root, "project")
    workspace_id = str(workspace["id"])
    terminal = store.reconcile_terminals(
        workspace_id,
        [_record("@1", "shell", 50)],
    )[0]

    store.reconcile_terminals(workspace_id, [_record("@1", "shell", 40)])
    assert store.get_terminal(str(terminal["id"]))["activity_at"] == _revision(50)  # type: ignore[index]
    store.reconcile_terminals(
        workspace_id,
        [
            {
                "tmux_window": "@1",
                "name": "shell",
                "role": "shell",
                "managed_run_id": None,
            }
        ],
    )
    assert store.get_terminal(str(terminal["id"]))["activity_at"] == _revision(50)  # type: ignore[index]

    unobserved = store.create_terminal(workspace_id, "new", "@2")
    view = store.terminal_activity_summary("browser-a")
    assert unobserved["id"] not in {
        item["terminal_id"] for item in view["terminals"]
    }

    with pytest.raises(ValueError, match="invalid Terminal activity revision"):
        store.reconcile_terminals(workspace_id, [_record("@1", "shell", -1)])


def test_terminal_activity_acknowledges_exact_provider_revision_monotonically(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = _workspace(store, root, "project")
    workspace_id = str(workspace["id"])
    terminal = store.reconcile_terminals(
        workspace_id,
        [_record("@1", "shell", 10)],
    )[0]
    terminal_id = str(terminal["id"])
    store.terminal_activity_summary("browser-a")

    store.observe_terminal_activity_batch(
        {workspace_id: [_record("@1", "shell", 20)]}
    )
    exact = store.acknowledge_terminal_activity(
        terminal_id,
        "browser-a",
        observed_activity_at=_revision(20),
    )
    assert exact["acknowledged_activity_at"] == _revision(20)
    assert exact["unread"] is False

    store.observe_terminal_activity_batch(
        {workspace_id: [_record("@1", "shell", 30)]}
    )
    stale = store.acknowledge_terminal_activity(
        terminal_id,
        "browser-a",
        observed_activity_at=_revision(20),
    )
    assert stale["activity_at"] == _revision(30)
    assert stale["acknowledged_activity_at"] == _revision(20)
    assert stale["unread"] is True

    with pytest.raises(ValueError, match="newer than the server cache"):
        store.acknowledge_terminal_activity(
            terminal_id,
            "browser-a",
            observed_activity_at=_revision(31),
        )


def test_node_terminal_activity_is_one_bounded_batch_without_running_tmux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = NodeRuntime([tmp_path])
    calls: list[tuple[tuple[str, ...], bool]] = []

    def fake_tmux(
        *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, check))
        return subprocess.CompletedProcess(
            ["tmux", *args],
            0,
            (
                f"termroom-first|@1|{_provider_seconds(100)}|shell|shell|\n"
                f"termroom-first|@2|{_provider_seconds(101)}|ignored|shell|\n"
                f"termroom-second|@3|{_provider_seconds(200)}|logs|shell|\n"
                f"termroom-unrequested|@9|{_provider_seconds(999)}|ignored|shell|\n"
            ),
            "",
        )

    monkeypatch.setattr(runtime, "_tmux", fake_tmux)
    result = runtime._handle_sync(
        "terminal.activity",
        {
            "workspaces": [
                {"tmux_session": "termroom-first", "windows": ["@1"]},
                {"tmux_session": "termroom-second", "windows": ["@3"]},
            ]
        },
    )

    assert len(calls) == 1
    assert calls[0][0][:2] == ("list-windows", "-a")
    assert calls[0][1] is False
    assert [entry["tmux_session"] for entry in result["workspaces"]] == [
        "termroom-first",
        "termroom-second",
    ]
    assert [entry["terminals"][0]["tmux_window"] for entry in result["workspaces"]] == [
        "@1",
        "@3",
    ]


@pytest.mark.parametrize(
    "workspaces",
    (
        [{"tmux_session": "not-termroom", "windows": ["@1"]}],
        [{"tmux_session": "termroom-first", "windows": ["1"]}],
        [
            {"tmux_session": "termroom-first", "windows": ["@1"]},
            {"tmux_session": "termroom-first", "windows": ["@2"]},
        ],
    ),
)
def test_node_terminal_activity_rejects_unbounded_or_ambiguous_targets(
    tmp_path: Path,
    workspaces: list[dict[str, object]],
) -> None:
    runtime = NodeRuntime([tmp_path])

    with pytest.raises(NodeAgentError) as exc_info:
        runtime._handle_sync("terminal.activity", {"workspaces": workspaces})

    assert exc_info.value.code == "terminal_activity_invalid"


def test_ssh_terminal_activity_refresh_batches_workspaces_per_computer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    computer = store.create_computer(
        name="SSH QA",
        ssh_alias="",
        host="ssh.example.test",
        port=22,
        username="runner",
        identity_file="/tmp/unused-key",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    manager = WorkspaceManager(RootManager(root), store)
    first = manager.open_remote(str(computer["id"]), "/srv/first")
    second = manager.open_remote(str(computer["id"]), "/srv/second")
    first_terminal = store.create_terminal(str(first["id"]), "shell", "@1")
    second_terminal = store.create_terminal(str(second["id"]), "logs", "@2")
    backend = SSHBackend(store, tmp_path / "state")
    calls: list[tuple[str, str]] = []

    def fake_exec(actual_computer: dict[str, object], command: str) -> str:
        calls.append((str(actual_computer["id"]), command))
        return (
            f"{first['tmux_session']}|@1|{_provider_seconds(100)}|shell|shell|\n"
            f"{second['tmux_session']}|@2|{_provider_seconds(200)}|logs|shell|\n"
        )

    monkeypatch.setattr(backend, "_exec", fake_exec)

    refreshed = backend.refresh_activity(computer, [first, second])

    assert len(calls) == 1
    assert calls[0][0] == computer["id"]
    assert str(first["tmux_session"]) in calls[0][1]
    assert str(second["tmux_session"]) in calls[0][1]
    assert refreshed[str(first["id"])][0]["id"] == first_terminal["id"]
    assert refreshed[str(first["id"])][0]["activity_at"] == _revision(100)
    assert refreshed[str(second["id"])][0]["id"] == second_terminal["id"]
    assert refreshed[str(second["id"])][0]["activity_at"] == _revision(200)


@pytest.mark.asyncio
async def test_remote_access_batches_node_terminal_activity_per_computer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    first = _workspace(store, root, "first")
    second = _workspace(store, root, "second")
    first_terminal = store.create_terminal(str(first["id"]), "shell", "@1")
    managed = store.create_terminal(
        str(first["id"]),
        "Run",
        "@9",
        role="file_run",
        managed_run_id="managed-run",
    )
    second_terminal = store.create_terminal(str(second["id"]), "logs", "@2")
    node = {"id": "node-computer", "connection_method": "node"}
    first = {**first, "backend_kind": "remote", "computer": node}
    second = {**second, "backend_kind": "remote", "computer": node}
    remote = RemoteAccess(
        store,
        SSHBackend(store, tmp_path / "state"),
        NodeCore(store),
        TerminalControl(),
    )
    calls: list[tuple[str, str, dict[str, object]]] = []

    async def fake_node_request(
        computer: dict[str, object], operation: str, payload: dict[str, object]
    ) -> dict[str, object]:
        calls.append((str(computer["id"]), operation, payload))
        return {
            "workspaces": [
                {
                    "tmux_session": first["tmux_session"],
                    "terminals": [
                        {
                            "tmux_window": "@1",
                            "activity_at": _provider_seconds(100),
                            "name": "shell",
                            "role": "shell",
                            "managed_run_id": None,
                        }
                    ],
                },
                {
                    "tmux_session": second["tmux_session"],
                    "terminals": [
                        {
                            "tmux_window": "@2",
                            "activity_at": _provider_seconds(200),
                            "name": "logs",
                            "role": "shell",
                            "managed_run_id": None,
                        }
                    ],
                },
            ]
        }

    monkeypatch.setattr(remote, "_node_request", fake_node_request)

    refreshed = await remote.refresh_terminal_activity([first, second])

    assert len(calls) == 1
    assert calls[0][0:2] == ("node-computer", "terminal.activity")
    requested = calls[0][2]["workspaces"]
    assert isinstance(requested, list)
    assert requested == [
        {"tmux_session": first["tmux_session"], "windows": ["@1"]},
        {"tmux_session": second["tmux_session"], "windows": ["@2"]},
    ]
    assert refreshed[str(first["id"])][0]["id"] == first_terminal["id"]
    assert refreshed[str(first["id"])][0]["activity_at"] == _revision(100)
    assert refreshed[str(second["id"])][0]["id"] == second_terminal["id"]
    assert refreshed[str(second["id"])][0]["activity_at"] == _revision(200)
    assert store.get_terminal(str(managed["id"])) is not None


def test_terminal_activity_revision_advances_after_store_restart(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    database = tmp_path / "state.sqlite3"
    store = StateStore(database)
    store.initialize()
    workspace = _workspace(store, root, "project")
    workspace_id = str(workspace["id"])
    terminal = store.reconcile_terminals(
        workspace_id,
        [_record("@1", "shell", 0)],
    )[0]
    terminal_id = str(terminal["id"])
    baseline = store.terminal_activity_summary("browser-a")
    assert baseline["unread_count"] == 0

    restarted = StateStore(database)
    restarted.initialize()
    restarted.observe_terminal_activity_batch(
        {
            workspace_id: [
                {"tmux_window": "@1", "activity_at": _provider_seconds(1)}
            ]
        }
    )

    summary = restarted.terminal_activity_summary(
        "browser-a", terminal_id=terminal_id
    )
    assert summary["unread_count"] == 1
    assert summary["terminals"][0]["activity_at"] > baseline["terminals"][0][
        "activity_at"
    ]


def test_touch_terminal_output_updates_timestamp_without_changing_provider_revision(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = _workspace(store, root, "project")
    workspace_id = str(workspace["id"])
    provider_revision = _provider_seconds(100)
    terminal = store.reconcile_terminals(
        workspace_id,
        [_record("@1", "shell", provider_revision)],
    )[0]

    before = store.get_terminal(str(terminal["id"]))
    assert before is not None
    assert before["last_output_at"] is None

    store.touch_terminal_output(str(terminal["id"]))

    after = store.get_terminal(str(terminal["id"]))
    assert after is not None
    assert after["last_output_at"] is not None
    assert after["activity_at"] == before["activity_at"] == _revision(100)


@pytest.mark.asyncio
async def test_local_attach_redraw_is_plain_text_and_does_not_change_activity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = _workspace(store, root, "project")
    workspace_id = str(workspace["id"])
    terminal = store.reconcile_terminals(
        workspace_id,
        [_record("@1", "shell", 100)],
    )[0]
    terminal_id = str(terminal["id"])
    manager = TerminalManager(store)
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"\x1b[2Jprompt")
    os.close(write_fd)
    wait_forever = asyncio.Event()

    class BrowserSocket:
        def __init__(self) -> None:
            self.text: list[str] = []

        async def send_text(self, value: str) -> None:
            self.text.append(value)

        async def send_bytes(self, value: bytes) -> None:
            raise AssertionError(f"Terminal output must be text, got {value!r}")

        async def receive(self) -> dict[str, object]:
            await wait_forever.wait()
            return {"type": "websocket.disconnect", "code": 1000}

    browser = BrowserSocket()
    monkeypatch.setattr(manager, "ensure_workspace", lambda _workspace: [terminal])
    monkeypatch.setattr(
        manager,
        "_run_tmux",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""),
    )
    monkeypatch.setattr(
        manager, "_spawn_tmux_client", lambda _workspace: (2_147_483_647, read_fd)
    )
    monkeypatch.setattr(manager, "_wait_for_pid", lambda *args, **kwargs: True)

    before = store.get_terminal(terminal_id)
    assert before is not None
    await manager.bridge(browser, workspace, terminal, device_id="browser-a")  # type: ignore[arg-type]
    after = store.get_terminal(terminal_id)

    assert browser.text == ["\x1b[2Jprompt"]
    assert after is not None
    assert after["activity_at"] == before["activity_at"] == _revision(100)


def test_provider_revision_change_is_the_only_unread_source(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = _workspace(store, root, "project")
    workspace_id = str(workspace["id"])
    terminal = store.reconcile_terminals(
        workspace_id,
        [_record("@1", "shell", 100)],
    )[0]
    terminal_id = str(terminal["id"])

    assert store.terminal_activity_summary("browser-a")["unread_count"] == 0
    store.touch_terminal_output(terminal_id)
    store.observe_terminal_activity_batch(
        {workspace_id: [_record("@1", "shell", 100)]}
    )
    assert store.terminal_activity_summary("browser-a")["unread_count"] == 0

    store.observe_terminal_activity_batch(
        {workspace_id: [_record("@1", "shell", 101)]}
    )
    changed = store.terminal_activity_summary("browser-a", terminal_id=terminal_id)
    assert changed["unread_count"] == 1
    assert changed["terminals"][0]["activity_at"] == _revision(101)


def test_terminal_activity_summary_prunes_expired_device_reads(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    workspace = _workspace(store, root, "project")
    terminal = store.reconcile_terminals(
        str(workspace["id"]),
        [_record("@1", "shell", 100)],
    )[0]
    store.terminal_activity_summary("expired-browser")
    with store.connect() as db:
        db.execute(
            """
            UPDATE terminal_activity_reads
            SET updated_at = '2000-01-01T00:00:00+00:00'
            WHERE terminal_id = ? AND device_id = 'expired-browser'
            """,
            (str(terminal["id"]),),
        )

    # A different current device triggers the bounded maintenance path without
    # immediately recreating the expired device's row.
    store_for_cleanup = StateStore(store.path)
    store_for_cleanup.terminal_activity_summary("current-browser")

    with store.connect() as db:
        expired = db.execute(
            """
            SELECT 1 FROM terminal_activity_reads
            WHERE terminal_id = ? AND device_id = 'expired-browser'
            """,
            (str(terminal["id"]),),
        ).fetchone()
    assert expired is None
