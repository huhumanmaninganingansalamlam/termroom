from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from termroom.app import create_app
from termroom.config import Settings
from termroom.db import StateStore, normalize_computer_name
from termroom.ssh_backend import SSHBackendError
from termroom.workspaces import RootManager, WorkspaceManager


def _computer(store: StateStore) -> dict[str, object]:
    return store.create_computer(
        name="GPU Server",
        ssh_alias="",
        host="gpu.example.test",
        port=22,
        username="runner",
        identity_file="/tmp/key",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )


async def _login(client: httpx.AsyncClient) -> None:
    client.cookies.set("termroom_locale", "ko")
    response = await client.post(
        "/login", data={"password": "test-token"}, follow_redirects=False
    )
    assert response.status_code == 303


def test_computer_display_name_accepts_unicode_and_rejects_unsafe_values() -> None:
    assert normalize_computer_name("  연구실 GPU  ") == "연구실 GPU"
    for value in ("", "   ", "bad\nname", "bad\u2028name", "x" * 81):
        with pytest.raises(ValueError):
            normalize_computer_name(value)


def test_server_terminal_bridge_is_idempotent_distinct_and_hidden(
    tmp_path: Path,
) -> None:
    root = tmp_path / "local"
    root.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    computer = _computer(store)
    manager = WorkspaceManager(RootManager(root), store)

    project = manager.open_remote(
        str(computer["id"]), "/home/runner", "runner home project"
    )
    server_terminal = manager.open_server_terminal(
        str(computer["id"]), "/home/runner"
    )
    reopened = manager.open_server_terminal(str(computer["id"]), "/home/runner")

    assert reopened["id"] == server_terminal["id"]
    assert server_terminal["id"] != project["id"]
    assert server_terminal["workspace_kind"] == "server_terminal"
    assert server_terminal["is_server_terminal"] is True
    assert server_terminal["is_remote_run"] is False
    assert server_terminal["tmux_session"] == (
        f"termroom-server-{str(computer['id'])[:12]}"
    )
    assert server_terminal["canonical_path"] == "/home/runner"
    assert [item["id"] for item in store.list_recent_workspaces()] == [project["id"]]
    assert [
        item["id"]
        for item in store.list_workspaces_for_computer(str(computer["id"]))
    ] == [project["id"]]
    assert {
        item["id"]
        for item in store.list_registered_workspaces_for_computer(str(computer["id"]))
    } == {project["id"], server_terminal["id"]}

    store.update_computer_name(str(computer["id"]), "연구실 GPU")
    refreshed = manager.require(str(server_terminal["id"]))
    assert refreshed["display_name"] == "연구실 GPU"
    assert refreshed["connection_label"] == "연구실 GPU"


def test_concurrent_server_terminal_open_reuses_one_bridge(tmp_path: Path) -> None:
    root = tmp_path / "local"
    root.mkdir()
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    computer = _computer(store)
    manager = WorkspaceManager(RootManager(root), store)

    with ThreadPoolExecutor(max_workers=6) as executor:
        workspace_ids = list(
            executor.map(
                lambda _index: manager.open_server_terminal(
                    str(computer["id"]), "/home/runner"
                )["id"],
                range(12),
            )
        )

    assert len(set(workspace_ids)) == 1
    registered = store.list_registered_workspaces_for_computer(str(computer["id"]))
    assert [item["workspace_kind"] for item in registered] == ["server_terminal"]


@pytest.mark.asyncio
async def test_connection_settings_can_rename_computer_and_open_real_terminal_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
        default_locale="ko",
    )
    app = create_app(settings)
    computer = _computer(app.state.store)
    ensured: list[str] = []

    monkeypatch.setattr(
        app.state.ssh,
        "home_directory",
        lambda actual: "/home/runner"
        if actual["id"] == computer["id"]
        else pytest.fail("wrong computer"),
    )

    def ensure_workspace(workspace):  # type: ignore[no-untyped-def]
        ensured.append(str(workspace["id"]))
        if not app.state.store.list_terminals(str(workspace["id"])):
            app.state.store.create_terminal(str(workspace["id"]), "shell", "@1")
        return app.state.store.list_terminals(str(workspace["id"]))

    monkeypatch.setattr(app.state.ssh, "ensure_workspace", ensure_workspace)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        detail = await client.get(f"/computers/{computer['id']}")
        assert detail.status_code == 200
        assert f'action="/computers/{computer["id"]}/name"' in detail.text
        assert f'action="/computers/{computer["id"]}/server-terminal"' in detail.text
        assert "서버 터미널" in detail.text

        renamed = await client.post(
            f"/computers/{computer['id']}/name",
            data={"_csrf": settings.csrf_token, "name": "  연구실 GPU  "},
            follow_redirects=False,
        )
        assert renamed.status_code == 303
        assert renamed.headers["location"].endswith("?name_updated=1")
        assert app.state.store.get_computer(str(computer["id"]))["name"] == "연구실 GPU"

        invalid = await client.post(
            f"/computers/{computer['id']}/name",
            data={"_csrf": settings.csrf_token, "name": "bad\nname"},
            follow_redirects=False,
        )
        assert invalid.status_code == 303
        invalid_page = await client.get(invalid.headers["location"])
        assert "1~80자의 한 줄 이름" in invalid_page.text
        assert app.state.store.get_computer(str(computer["id"]))["name"] == "연구실 GPU"

        opened = await client.post(
            f"/computers/{computer['id']}/server-terminal",
            data={"_csrf": settings.csrf_token},
            follow_redirects=False,
        )
        assert opened.status_code == 303
        assert opened.headers["location"].endswith("/terminal")
        server_workspace_id = opened.headers["location"].split("/")[2]
        terminal_page = await client.get(opened.headers["location"])
        assert terminal_page.status_code == 200
        assert "SSH 서버 터미널" in terminal_page.text
        assert "연구실 GPU" in terminal_page.text
        assert f'href="/w/{server_workspace_id}/files"' not in terminal_page.text
        assert f'href="/w/{server_workspace_id}/recent"' not in terminal_page.text

        reopened = await client.post(
            f"/computers/{computer['id']}/server-terminal",
            data={"_csrf": settings.csrf_token},
            follow_redirects=False,
        )
        assert reopened.headers["location"] == opened.headers["location"]

        monkeypatch.setattr(
            app.state.ssh,
            "home_directory",
            lambda _computer: (_ for _ in ()).throw(
                SSHBackendError(
                    "home unavailable",
                    locale_key="server_terminal.home_unavailable",
                )
            ),
        )
        unavailable = await client.post(
            f"/computers/{computer['id']}/server-terminal",
            data={"_csrf": settings.csrf_token},
            follow_redirects=False,
        )
        unavailable_page = await client.get(unavailable.headers["location"])
        assert "SSH 사용자의 홈 폴더를 확인하지 못했습니다" in unavailable_page.text

    assert ensured == [server_workspace_id, server_workspace_id, server_workspace_id]
    assert app.state.store.list_workspaces_for_computer(str(computer["id"])) == []
