from __future__ import annotations

import asyncio
import io
import shutil
import subprocess
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from termroom.app import MAX_INLINE_IMAGE_BYTES, create_app
from termroom.config import Settings
from termroom.files import FileEntry, RecentFiles
from termroom.ssh_backend import SSHBackendError

_TERMINAL_ACTIVITY_EPOCH_SECONDS = 1_700_000_000


def _terminal_activity_seconds(offset: int) -> int:
    return _TERMINAL_ACTIVITY_EPOCH_SECONDS + offset


async def _login(client: httpx.AsyncClient, password: str = "test-token") -> None:
    client.cookies.set("termroom_locale", "ko")
    response = await client.post(
        "/login",
        data={"password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.cookies.get("termroom_session")


@pytest.mark.asyncio
async def test_authentication_and_home(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
        default_locale="ko",
    )
    transport = httpx.ASGITransport(app=create_app(settings), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        unauthorized = await client.get("/", follow_redirects=False)
        assert unauthorized.status_code == 401

        await _login(client)
        authenticated = await client.get("/")
        assert authenticated.status_code == 200
        assert "Termroom" in authenticated.text
        assert "로컬 전용" in authenticated.text
        assert "작업공간 열기" in authenticated.text
        assert "SSH 컴퓨터 연결" not in authenticated.text
        assert "빠른 실행" not in authenticated.text
        assert "최근 원격 실행" not in authenticated.text
        assert authenticated.headers["cache-control"] == "no-store"
        assert authenticated.headers["x-frame-options"] == "SAMEORIGIN"
        assert authenticated.headers["referrer-policy"] == "no-referrer"


@pytest.mark.asyncio
async def test_workspace_display_name_updates_header_and_home_without_renaming_folder(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    project = root / "project-folder"
    project.mkdir(parents=True)
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
        default_locale="ko",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project-folder")
    workspace_id = str(workspace["id"])
    original_path = workspace["path"]
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        page = await client.get(f"/w/{workspace_id}/files")
        assert page.status_code == 200
        assert f'action="/w/{workspace_id}/name"' in page.text

        renamed = await client.post(
            f"/w/{workspace_id}/name",
            data={
                "_csrf": settings.csrf_token,
                "display_name": "  집중 작업  ",
            },
            follow_redirects=False,
        )
        assert renamed.status_code == 303
        assert renamed.headers["location"] == f"/w/{workspace_id}"
        assert app.state.store.get_workspace(workspace_id)["display_name"] == "집중 작업"

        renamed_page = await client.get(f"/w/{workspace_id}/files")
        home = await client.get("/")
        assert "집중 작업" in renamed_page.text
        assert "집중 작업" in home.text

        reset = await client.post(
            f"/w/{workspace_id}/name",
            data={"_csrf": settings.csrf_token, "display_name": ""},
            follow_redirects=False,
        )
        assert reset.status_code == 303
        assert app.state.store.get_workspace(workspace_id)["display_name"] == "project-folder"

        invalid = await client.post(
            f"/w/{workspace_id}/name",
            data={"_csrf": settings.csrf_token, "display_name": "bad\nname"},
        )
        assert invalid.status_code == 400
        assert "120자 이하의 한 줄 작업공간 이름" in invalid.text

    refreshed = app.state.workspaces.require(workspace_id)
    assert refreshed["path"] == original_path
    assert project.is_dir()


@pytest.mark.asyncio
async def test_terminal_activity_apis_are_per_browser_scoped_and_race_safe(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
        default_locale="ko",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    workspace_id = str(workspace["id"])
    terminal = app.state.store.create_terminal(
        workspace_id,
        "shell",
        "@1",
        activity_at=_terminal_activity_seconds(10),
    )
    managed = app.state.store.create_terminal(
        workspace_id,
        "Run",
        "@2",
        role="file_run",
        managed_run_id="run-managed",
        activity_at=_terminal_activity_seconds(999),
    )
    terminal_id = str(terminal["id"])
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as first, httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as second:
        await _login(first)
        await _login(second)

        initial = await first.get(
            "/api/terminal-activity/summary", params={"workspace_id": workspace_id}
        )
        assert initial.status_code == 200
        assert initial.json()["unread_count"] == 0
        assert [item["terminal_id"] for item in initial.json()["terminals"]] == [
            terminal_id
        ]
        assert str(managed["id"]) not in initial.text

        app.state.store.observe_terminal_activity(
            workspace_id,
            [{"tmux_window": "@1", "activity_at": _terminal_activity_seconds(20)}],
        )
        workspace_view = await first.get(
            f"/api/workspaces/{workspace_id}/terminal-activity"
        )
        assert workspace_view.status_code == 200
        assert workspace_view.json()["unread_count"] == 1
        assert workspace_view.json()["latest_unread_terminal_id"] == terminal_id

        second_baseline = await second.get(
            "/api/terminal-activity/summary", params={"workspace_id": workspace_id}
        )
        assert second_baseline.status_code == 200
        assert second_baseline.json()["unread_count"] == 0

        app.state.store.observe_terminal_activity(
            workspace_id,
            [{"tmux_window": "@1", "activity_at": _terminal_activity_seconds(30)}],
        )
        assert managed["id"] != terminal_id
        missing_workspace = await first.get(
            "/api/workspaces/missing/terminal-activity"
        )
        assert missing_workspace.status_code == 404


@pytest.mark.asyncio
async def test_home_lists_remote_runs_without_polling_and_schedules_cleanup(
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
    computer = app.state.store.create_computer(
        name="GPU QA",
        ssh_alias="",
        host="gpu.example.test",
        port=22,
        username="runner",
        identity_file="",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    run_id = str(uuid.uuid4())
    app.state.store.create_remote_run(
        {
            "id": run_id,
            "source_kind": "git",
            "source_workspace_id": None,
            "source_path": None,
            "source_label": "vision/model",
            "source_url": "https://example.test/vision/model.git",
            "source_options_json": '{"policy":1}',
            "source_revision": None,
            "source_size": None,
            "target_computer_id": str(computer["id"]),
            "command": "python inference.py\necho done",
            "run_base": "/home/runner/.cache/termroom/runs",
            "workspace_id": None,
            "state": "running",
            "phase": None,
            "created_at": "2026-08-09T00:00:00+00:00",
        }
    )
    def unexpected_poll(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("the home page must not poll Remote Runs")

    cleanup_calls = 0

    def schedule_cleanup() -> bool:
        nonlocal cleanup_calls
        cleanup_calls += 1
        return True

    monkeypatch.setattr(app.state.remote_runs, "poll", unexpected_poll)
    monkeypatch.setattr(app.state.remote_runs, "schedule_cleanup", schedule_cleanup)

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        response = await client.get("/")

    assert response.status_code == 200
    assert "최근 원격 실행" in response.text
    assert "vision/model" in response.text
    assert "GPU QA" in response.text
    assert cleanup_calls == 1
    assert f'href="/remote-runs/{run_id}"' in response.text
    run_row_start = response.text.index(f'href="/remote-runs/{run_id}"')
    run_row_end = response.text.index("</a>", run_row_start)
    assert "python inference.py" not in response.text[run_row_start:run_row_end]
    assert 'data-run-state="running"' in response.text
    assert f'data-status-url="/api/remote-runs/{run_id}/status"' in response.text


@pytest.mark.asyncio
async def test_local_project_route_creates_folder_workspace_and_terminal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    parent = root / "projects"
    parent.mkdir(parents=True)
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
        default_locale="ko",
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        response = await client.post(
            "/open/local/projects",
            data={
                "_csrf": settings.csrf_token,
                "root_id": app.state.workspaces.root_record["id"],
                "parent": "projects",
                "name": "새 프로젝트",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"].endswith("/terminal")
        assert (parent / "새 프로젝트").is_dir()
        workspace_id = response.headers["location"].split("/")[2]
        workspace = app.state.workspaces.require(workspace_id)
        assert workspace["path"] == parent / "새 프로젝트"
        assert app.state.store.list_terminals(workspace_id)

        terminal_page = await client.get(response.headers["location"])
        assert terminal_page.status_code == 200
        assert "로컬 작업공간" in terminal_page.text
        assert f'/api/workspaces/{workspace_id}/usage' in terminal_page.text
        assert "작업공간 사용량" in terminal_page.text

        usage = await client.get(f"/api/workspaces/{workspace_id}/usage")
        assert usage.status_code == 200
        assert usage.json()["estimated"] is True
        assert usage.json()["state"] == "fresh"
        assert usage.json()["sample"]["process_count"] >= 1
        assert usage.json()["last_checked_at"].endswith("Z")

        subprocess.run(
            ["tmux", "kill-session", "-t", str(workspace["tmux_session"])],
            check=False,
            capture_output=True,
        )


@pytest.mark.asyncio
async def test_local_project_route_keeps_created_folder_when_tmux_open_fails(
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

    def fail_workspace(_workspace):  # type: ignore[no-untyped-def]
        raise OSError("tmux unavailable")

    monkeypatch.setattr(app.state.terminals, "ensure_workspace", fail_workspace)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        created = await client.post(
            "/open/local/projects",
            data={
                "_csrf": settings.csrf_token,
                "root_id": app.state.workspaces.root_record["id"],
                "parent": ".",
                "name": "keep-after-error",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        assert (root / "keep-after-error").is_dir()

        notice = await client.get(created.headers["location"])
        assert notice.status_code == 200
        assert "폴더는 만들었지만 작업공간을 열지 못했습니다" in notice.text
        assert str(root / "keep-after-error") in notice.text
        assert "다시 열기" in notice.text


@pytest.mark.asyncio
async def test_remote_run_archive_api_normalizes_legacy_zip_source(
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
    computer = app.state.store.create_computer(
        name="GPU QA",
        ssh_alias="",
        host="gpu.example.test",
        port=22,
        username="runner",
        identity_file="",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )

    monkeypatch.setattr(
        app.state.ssh,
        "preflight_remote_run_target",
        lambda _computer, **_kwargs: {
            "run_base": "/home/runner/.cache/termroom/runs",
            "tools": {"bash": "/bin/bash", "tmux": "/usr/bin/tmux"},
            "available_bytes": 1024 * 1024,
            "warnings": [],
        },
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        new_page = await client.get("/remote-runs/new")
        assert new_page.status_code == 200
        assert "원격 실행" in new_page.text
        assert "GPU QA" in new_page.text
        assert "공개 Git" in new_page.text
        assert "Archive" in new_page.text
        assert "ZIP" in new_page.text

        run_id = str(uuid.uuid4())
        created = await client.post(
            "/api/remote-runs",
            json={
                "id": run_id,
                "source_kind": "zip",
                "archive_name": "private-source.zip",
                "target_computer_id": str(computer["id"]),
                "command": "python inference.py",
            },
            headers={"X-Termroom-CSRF": settings.csrf_token},
        )
        assert created.status_code == 202
        payload = created.json()
        assert payload["detail_url"] == f"/remote-runs/{run_id}"
        assert payload["archive_url"] == f"/api/remote-runs/{run_id}/archive"
        stored = app.state.store.get_remote_run(run_id)
        assert stored is not None
        assert stored["target_computer_id"] == computer["id"]
        assert stored["source_kind"] == "archive"
        assert stored["archive_format"] == "zip"
        assert stored["state"] == "preparing"
        assert stored["phase"] == "waiting_upload"

        legacy = await client.get("/runs/new")
        assert legacy.status_code == 404


@pytest.mark.asyncio
async def test_remote_run_empty_state_connects_a_remote_directly(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
        default_locale="ko",
    )
    transport = httpx.ASGITransport(
        app=create_app(settings), raise_app_exceptions=False
    )

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        await _login(client)
        response = await client.get("/remote-runs/new")

    assert response.status_code == 200
    assert 'href="/computers/new"' in response.text


@pytest.mark.asyncio
async def test_remote_run_input_errors_are_localized_in_korean(
    tmp_path: Path,
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
    computer = app.state.store.create_computer(
        name="GPU QA",
        ssh_alias="",
        host="gpu.example.test",
        port=22,
        username="runner",
        identity_file="",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    target_id = str(computer["id"])
    cases = (
        (
            {
                "source_kind": "archive",
                "archive_name": "source.zip",
                "target_computer_id": "",
                "command": "printf done",
            },
            "실행할 Remote를 선택하세요",
        ),
        (
            {
                "source_kind": "archive",
                "archive_name": "source.zip",
                "target_computer_id": target_id,
                "command": "",
            },
            "실행할 명령을 입력하세요",
        ),
        (
            {
                "source_kind": "workspace",
                "source_workspace_id": "",
                "target_computer_id": target_id,
                "command": "printf done",
            },
            "snapshot으로 보낼 Workspace 폴더를 선택하세요",
        ),
        (
            {
                "source_kind": "git",
                "source_url": "git@example.test:private/repo.git",
                "target_computer_id": target_id,
                "command": "printf done",
            },
            "공개 HTTPS clone URL을 입력하세요",
        ),
        (
            {
                "source_kind": "archive",
                "archive_format": "tar.gz",
                "archive_name": "source.tar.gz",
                "target_computer_id": target_id,
                "command": "printf done",
            },
            "현재 Archive 형식은 ZIP만 지원합니다",
        ),
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        await _login(client)
        for payload, expected in cases:
            response = await client.post(
                "/api/remote-runs",
                json={"id": str(uuid.uuid4()), **payload},
                headers={"X-Termroom-CSRF": settings.csrf_token},
            )
            assert response.status_code == 400
            message = response.json()["error"]
            assert expected in message
            assert "Remote Run" not in message
            assert "Git repository" not in message


@pytest.mark.asyncio
async def test_remote_run_preparation_error_detail_never_leaks_raw_english(
    tmp_path: Path,
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
    computer = app.state.store.create_computer(
        name="GPU QA",
        ssh_alias="",
        host="gpu.example.test",
        port=22,
        username="runner",
        identity_file="",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    run_id = str(uuid.uuid4())
    app.state.store.create_remote_run(
        {
            "id": run_id,
            "source_kind": "workspace",
            "source_workspace_id": None,
            "source_path": ".",
            "source_label": "source",
            "source_url": None,
            "source_options_json": '{"policy":1}',
            "source_revision": None,
            "source_size": None,
            "target_computer_id": str(computer["id"]),
            "command": "printf done",
            "run_base": "/home/runner/.cache/termroom/runs",
            "workspace_id": None,
            "state": "failed",
            "phase": None,
            "error_code": "prepare_failed",
            "error_detail": "Remote Run work root is invalid",
            "created_at": "2026-08-09T00:00:00+00:00",
        }
    )
    assert app.state.store.transition_remote_run(
        run_id,
        expected_states={"failed"},
        error_code="prepare_failed",
        error_detail="Remote Run work root is invalid",
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        await _login(client)
        page = await client.get(f"/remote-runs/{run_id}")
        status = await client.get(f"/api/remote-runs/{run_id}/status")

    expected = "원격 작업공간을 준비하지 못했습니다"
    assert page.status_code == 200
    assert expected in page.text
    assert "Remote Run work root is invalid" not in page.text
    assert status.status_code == 200
    assert status.json()["error_detail"] == expected + "."


@pytest.mark.asyncio
async def test_remote_run_redirects_to_existing_workspace_terminal_and_files(
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
    computer = app.state.store.create_computer(
        name="GPU QA",
        ssh_alias="",
        host="gpu.example.test",
        port=22,
        username="runner",
        identity_file="",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    run_id = str(uuid.uuid4())
    run_base = "/home/runner/.cache/termroom/runs"
    run, created = app.state.store.create_remote_run(
        {
            "id": run_id,
            "source_kind": "archive",
            "archive_format": "zip",
            "source_workspace_id": None,
            "source_path": None,
            "source_label": "source.zip",
            "source_url": None,
            "source_options_json": '{"archive_name":"source.zip","policy":1}',
            "source_revision": None,
            "source_size": 10,
            "target_computer_id": str(computer["id"]),
            "command": "python main.py",
            "run_base": run_base,
            "workspace_id": None,
            "state": "finished",
            "phase": None,
            "created_at": "2026-08-09T00:00:00+00:00",
        }
    )
    assert created is True
    workspace = app.state.workspaces.open_remote_run(
        run,
        f"termroom-run-{run_id}",
        f"{run_base}/{run_id}/work",
    )
    terminal = app.state.store.create_terminal(
        workspace["id"], "run", "@run", role="remote_run", managed_run_id=run_id
    )
    monkeypatch.setattr(
        app.state.ssh,
        "ensure_workspace",
        lambda _workspace: [terminal],
    )

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        detail = await client.get(f"/remote-runs/{run_id}", follow_redirects=False)
        assert detail.status_code == 303
        assert detail.headers["location"] == f"/w/{workspace['id']}/terminal"

        failed_action = await client.get(
            f"/remote-runs/{run_id}?error=server+offline",
            follow_redirects=False,
        )
        assert failed_action.status_code == 303
        assert failed_action.headers["location"].startswith(
            f"/w/{workspace['id']}/terminal?error="
        )

        terminal_page = await client.get(detail.headers["location"])
        assert terminal_page.status_code == 200
        assert "GPU QA" in terminal_page.text
        assert "원격 실행 작업공간" in terminal_page.text
        assert "로컬 전용" not in terminal_page.text
        assert "source.zip" in terminal_page.text
        assert f"/w/{workspace['id']}/files" in terminal_page.text
        assert "python main.py" in terminal_page.text
        assert 'value="rename"' not in terminal_page.text
        assert 'value="delete"' not in terminal_page.text

        blocked = await client.post(
            f"/w/{workspace['id']}/terminals/{terminal['id']}",
            data={
                "_csrf": settings.csrf_token,
                "action": "delete",
            },
            follow_redirects=False,
        )
        assert blocked.status_code == 303
        assert "error=" in blocked.headers["location"]
        assert app.state.store.get_terminal(str(terminal["id"])) is not None

    assert workspace["id"] not in {
        item["id"] for item in app.state.workspaces.list_recent()
    }


@pytest.mark.asyncio
async def test_home_warns_when_core_is_bound_beyond_loopback(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        host="0.0.0.0",
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    transport = httpx.ASGITransport(app=create_app(settings), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        response = await client.get("/")

        assert response.status_code == 200
        assert "네트워크 공개" in response.text


@pytest.mark.asyncio
async def test_running_core_rejects_mixed_runtime_after_package_files_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        monkeypatch.setattr("termroom.app.runtime_stamp", lambda: "new-runtime")
        response = await client.get("/")
        api_response = await client.post(
            "/api/remote-runs",
            headers={"Accept": "application/json"},
            json={},
        )

    assert response.status_code == 503
    assert response.headers["x-termroom-restart-required"] == "1"
    assert "termroom ." in response.text
    assert "The running Core is using older code" in response.text
    assert api_response.status_code == 503
    assert api_response.headers["x-termroom-restart-required"] == "1"
    assert api_response.headers["content-type"].startswith("application/json")
    assert api_response.json() == {
        "ok": False,
        "code": "restart_required",
        "error": "Termroom이 업데이트되었습니다. Core를 다시 시작한 뒤 이 페이지를 새로고침하세요.",
    }


@pytest.mark.asyncio
async def test_workspace_picker_does_not_expose_files_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "project").mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    transport = httpx.ASGITransport(app=create_app(settings), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)

        response = await client.get("/open/local?path=..")

        assert response.status_code == 403
        assert str(tmp_path.parent) not in response.text


@pytest.mark.asyncio
async def test_workspace_picker_hides_dot_directories_by_default(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "project").mkdir()
    (root / ".venv").mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    transport = httpx.ASGITransport(app=create_app(settings), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)

        default_view = await client.get("/open/local")
        hidden_view = await client.get("/open/local?hidden=1")

        assert "project" in default_view.text
        assert ".venv" not in default_view.text
        assert ".venv" in hidden_view.text


@pytest.mark.asyncio
async def test_workspace_files_hide_dot_directories_but_keep_dotfiles(tmp_path: Path) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    (project / ".github").mkdir()
    (project / ".github" / "workflow.yml").write_text("name: qa\n", encoding="utf-8")
    (project / ".env").write_text("APP_VALUE=visible-after-login\n", encoding="utf-8")
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        default_view = await client.get(f"/w/{workspace['id']}/files")
        noise_view = await client.get(f"/w/{workspace['id']}/files?noise=1")

        assert ".env" in default_view.text
        assert ".github" not in default_view.text
        assert "숨김·캐시 1개 표시" in default_view.text
        assert ".github" in noise_view.text

        env_view = await client.get(f"/w/{workspace['id']}/view/.env")
        assert env_view.status_code == 200
        assert "민감한 정보가 들어갈 수 있는 파일입니다" in env_view.text
        assert 'id="file-share"' not in env_view.text

        recent = await client.get(f"/w/{workspace['id']}/recent")
        assert ".env" not in recent.text


@pytest.mark.asyncio
async def test_file_upload_view_download_and_recent_flow(tmp_path: Path) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    header_filename = 'line\rbreak"\\.txt'
    (project / header_filename).write_bytes(b"safe header\n")
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    app.state.terminals.ensure_workspace(workspace)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        upload = await client.post(
            f"/w/{workspace['id']}/files/upload",
            data={"_csrf": settings.csrf_token, "parent": ".", "overwrite": "0"},
            files={"files": ("result.csv", b"name,value\nalpha,1\n", "text/csv")},
            follow_redirects=False,
        )
        assert upload.status_code == 303
        assert (project / "result.csv").read_bytes() == b"name,value\nalpha,1\n"

        view = await client.get(f"/w/{workspace['id']}/view/result.csv")
        assert view.status_code == 200
        assert "CSV 미리보기" in view.text
        assert "alpha" in view.text
        assert "내용 복사" in view.text
        assert 'id="file-share"' in view.text

        download = await client.get(f"/w/{workspace['id']}/download/result.csv")
        assert download.status_code == 200
        assert download.content == b"name,value\nalpha,1\n"
        assert "attachment" in download.headers["content-disposition"]

        special_download = await client.get(
            f"/w/{workspace['id']}/download/{quote(header_filename, safe='')}"
        )
        assert special_download.status_code == 200
        assert special_download.content == b"safe header\n"
        disposition = special_download.headers["content-disposition"]
        assert "\r" not in disposition
        assert "\n" not in disposition
        assert "%0D" in disposition

        recent = await client.get(f"/w/{workspace['id']}/recent")
        assert recent.status_code == 200
        assert "result.csv" in recent.text

        with (project / "result.csv").open("ab") as handle:
            handle.write(b"beta,2\n")
        growing = await client.get(f"/w/{workspace['id']}/recent")
        assert growing.status_code == 200
        assert "증가 중" in growing.text


@pytest.mark.asyncio
async def test_file_browser_can_download_selected_files_and_folders_as_zip(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    project = root / "project"
    nested = project / "output"
    nested.mkdir(parents=True)
    (project / "a.txt").write_text("alpha\n", encoding="utf-8")
    (nested / "b.txt").write_text("beta\n", encoding="utf-8")
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        browser = await client.get(f"/w/{workspace['id']}/files")
        assert browser.status_code == 200
        assert "ZIP으로 다운로드" in browser.text
        assert 'data-file-select' in browser.text

        archive = await client.post(
            f"/w/{workspace['id']}/files/archive",
            data={
                "_csrf": settings.csrf_token,
                "parent": ".",
                "paths": ["a.txt", "output"],
            },
        )

    assert archive.status_code == 200
    assert archive.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        assert set(bundle.namelist()) == {"a.txt", "output/", "output/b.txt"}
        assert bundle.read("a.txt") == b"alpha\n"
        assert bundle.read("output/b.txt") == b"beta\n"


@pytest.mark.asyncio
async def test_file_browser_searches_current_folder_and_downloads_one_folder_as_zip(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    project = root / "project"
    reports = project / "reports"
    reports.mkdir(parents=True)
    (project / "report-final.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (project / "notes.txt").write_text("notes\n", encoding="utf-8")
    (reports / "nested.txt").write_text("nested\n", encoding="utf-8")
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        search = await client.get(f"/w/{workspace['id']}/files", params={"q": "report"})
        assert search.status_code == 200
        assert "report-final.csv" in search.text
        assert "reports" in search.text
        assert "notes.txt" not in search.text
        assert 'value="report"' in search.text
        assert "data-live-file-search" in search.text
        assert 'id="file-results"' in search.text

        partial = await client.get(
            f"/w/{workspace['id']}/files",
            params={"q": "report"},
            headers={"X-Termroom-Partial": "file-results"},
        )
        assert partial.status_code == 200
        assert "report-final.csv" in partial.text
        assert "notes.txt" not in partial.text
        assert "<!doctype html>" not in partial.text

        archive = await client.get(f"/w/{workspace['id']}/archive/reports")

    assert archive.status_code == 200
    assert archive.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
        assert set(bundle.namelist()) == {"reports/", "reports/nested.txt"}
        assert bundle.read("reports/nested.txt") == b"nested\n"


@pytest.mark.asyncio
async def test_open_workspace_flow_chooses_computer_then_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    (root / "local-project").mkdir(parents=True)
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    app.state.workspaces.open("local-project")
    computer = app.state.store.create_computer(
        name="Build server",
        ssh_alias="",
        host="build.example",
        port=22,
        username="dev",
        identity_file="/tmp/key",
        auth_kind="key",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    first = app.state.workspaces.open_remote(str(computer["id"]), "/srv/api", "api")
    second = app.state.workspaces.open_remote(str(computer["id"]), "/data/jobs", "jobs")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        with monkeypatch.context() as counts_guard:
            counts_guard.setattr(
                app.state.store,
                "list_workspaces_for_root",
                lambda *_args: pytest.fail(
                    "Workspace hub must not query once per Local root"
                ),
            )
            counts_guard.setattr(
                app.state.store,
                "list_workspaces_for_computer",
                lambda *_args: pytest.fail(
                    "Workspace hub must not query once per Remote"
                ),
            )
            computers = await client.get("/open")
        assert computers.status_code == 200
        assert "어느 컴퓨터에서 열까요?" in computers.text
        assert "Build server" in computers.text
        assert "작업공간 1개" in computers.text
        assert "작업공간 2개" in computers.text

        picker = await client.get(f"/open/{computer['id']}")
        assert picker.status_code == 200
        assert f'/w/{first["id"]}' in picker.text
        assert f'/w/{second["id"]}' in picker.text
        assert "새 작업공간 추가" in picker.text
        assert "연결 설정" in picker.text


@pytest.mark.asyncio
async def test_open_workspace_keeps_computer_hub_before_first_remote(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        response = await client.get("/open", follow_redirects=False)

    assert response.status_code == 200
    assert "이 컴퓨터" in response.text
    assert "컴퓨터 연결" in response.text
    assert 'href="/open/local"' in response.text
    assert 'href="/computers/new"' in response.text


@pytest.mark.asyncio
async def test_local_workspace_picker_can_add_another_folder_location(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    other = tmp_path / "other-disk"
    project = other / "project-b"
    project.mkdir(parents=True)
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        added = await client.post(
            "/open/local/locations",
            data={"_csrf": settings.csrf_token, "path": str(other)},
            follow_redirects=False,
        )
        assert added.status_code == 303
        location_url = httpx.URL(added.headers["location"])
        root_id = location_url.params["root"]

        picker = await client.get(added.headers["location"])
        assert picker.status_code == 200
        assert "project-b" in picker.text
        assert str(other) in picker.text

        opened = await client.post(
            "/api/workspaces",
            data={
                "_csrf": settings.csrf_token,
                "root_id": root_id,
                "path": "project-b",
            },
            follow_redirects=False,
        )
        assert opened.status_code == 303
        workspace_id = opened.headers["location"].split("/")[2]
        workspace = app.state.workspaces.require(workspace_id)
        assert workspace["path"] == project


@pytest.mark.asyncio
async def test_local_location_picker_can_browse_absolute_directories(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    visible = tmp_path / "visible-location"
    visible.mkdir()
    hidden = tmp_path / ".hidden-location"
    hidden.mkdir()
    (tmp_path / "not-a-folder.txt").write_text("ignore\n", encoding="utf-8")
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        api_picker = await client.get(
            "/api/local/browse-directories",
            params={"path": str(tmp_path)},
        )
        picker = await client.get(
            "/open/local",
            params={"browse_location": "1", "location_path": str(tmp_path)},
        )
        hidden_picker = await client.get(
            "/open/local",
            params={
                "browse_location": "1",
                "location_path": str(tmp_path),
                "location_hidden": "1",
            },
        )

    assert api_picker.status_code == 200
    api_data = api_picker.json()
    assert api_data["ok"] is True
    assert api_data["current"] == str(tmp_path)
    assert "visible-location" in {entry["name"] for entry in api_data["entries"]}
    assert picker.status_code == 200
    assert "폴더 찾아보기" in picker.text
    assert "현재 폴더 열기" in picker.text
    assert "취소" in picker.text
    assert 'data-close-popover' in picker.text
    assert 'data-folder-picker-url="/api/local/browse-directories"' in picker.text
    assert "data-folder-picker-open" in picker.text
    assert "data-folder-path=" in picker.text
    assert str(tmp_path) in picker.text
    assert "visible-location" in picker.text
    assert ".hidden-location" not in picker.text
    assert "not-a-folder.txt" not in picker.text
    assert ".hidden-location" in hidden_picker.text


@pytest.mark.asyncio
async def test_remote_workspace_picker_renders_browsable_directories(
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
    computer = app.state.store.create_computer(
        name="Build server",
        ssh_alias="",
        host="build.example",
        port=22,
        username="dev",
        identity_file="/tmp/key",
        auth_kind="key",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )

    def fake_browse(  # type: ignore[no-untyped-def]
        _computer, remote_path=None, *, show_hidden=False
    ):
        assert str(_computer["id"]) == str(computer["id"])
        assert remote_path is None
        assert show_hidden is False
        return {
            "current": "/home/dev",
            "parent": "/home",
            "entries": [
                {"name": "projects", "path": "/home/dev/projects"},
                {"name": "work", "path": "/home/dev/work"},
            ],
            "hidden_count": 1,
            "show_hidden": False,
        }

    monkeypatch.setattr(app.state.ssh, "list_browse_directories", fake_browse)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        api_picker = await client.get(f"/api/computers/{computer['id']}/browse-directories")
        picker = await client.get(f"/open/{computer['id']}?browse=1")

    assert api_picker.status_code == 200
    api_data = api_picker.json()
    assert api_data["ok"] is True
    assert api_data["current"] == "/home/dev"
    assert {entry["name"] for entry in api_data["entries"]} == {"projects", "work"}
    assert picker.status_code == 200
    assert "폴더 찾아보기" in picker.text
    assert "이 위치에 새 프로젝트" in picker.text
    assert "data-folder-picker-submit" not in picker.text
    assert "data-folder-picker-default-actions" not in picker.text
    assert "data-new-project" in picker.text
    browse_url = f'/api/computers/{computer["id"]}/browse-directories'
    assert f'data-folder-picker-url="{browse_url}"' in picker.text
    assert "data-folder-picker-open" in picker.text
    assert "data-folder-path=" in picker.text
    assert "/home/dev" in picker.text
    assert "projects" in picker.text
    assert "work" in picker.text
    assert "현재 폴더 열기" in picker.text
    assert "닫기" in picker.text
    assert f'href="/open/{computer["id"]}"' in picker.text


@pytest.mark.asyncio
async def test_remote_workspace_picker_can_close_after_browse_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
        default_locale="en",
    )
    app = create_app(settings)
    computer = app.state.store.create_computer(
        name="Offline server",
        ssh_alias="",
        host="offline.example",
        port=22,
        username="dev",
        identity_file="/tmp/key",
        auth_kind="key",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )

    def fail_browse(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise SSHBackendError("SSH connection failed")

    monkeypatch.setattr(app.state.ssh, "list_browse_directories", fail_browse)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        api_response = await client.get(
            f"/api/computers/{computer['id']}/browse-directories"
        )
        response = await client.get(f"/open/{computer['id']}?browse=1")

    assert api_response.status_code == 400
    assert api_response.json() == {"ok": False, "error": "SSH connection failed"}
    assert response.status_code == 200
    assert "SSH connection failed" in response.text
    assert "닫기" in response.text
    assert f'href="/open/{computer["id"]}"' in response.text


@pytest.mark.asyncio
async def test_remote_project_route_creates_workspace_from_one_folder_name(
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
    computer = app.state.store.create_computer(
        name="GPU Server",
        ssh_alias="",
        host="gpu.example",
        port=22,
        username="dev",
        identity_file="/tmp/key",
        auth_kind="key",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    created_path = "/workspace/새 프로젝트"

    def create_project(_computer, parent, name):  # type: ignore[no-untyped-def]
        assert str(_computer["id"]) == str(computer["id"])
        assert parent == "/workspace"
        assert name == "새 프로젝트"
        return created_path

    def ensure_workspace(workspace):  # type: ignore[no-untyped-def]
        if not app.state.store.list_terminals(str(workspace["id"])):
            app.state.store.create_terminal(str(workspace["id"]), "shell", "@1")
        return app.state.store.list_terminals(str(workspace["id"]))

    monkeypatch.setattr(app.state.ssh, "create_project_directory", create_project)
    monkeypatch.setattr(app.state.ssh, "ensure_workspace", ensure_workspace)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        response = await client.post(
            f"/computers/{computer['id']}/projects",
            data={
                "_csrf": settings.csrf_token,
                "parent": "/workspace",
                "name": "새 프로젝트",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"].endswith("/terminal")
    workspace = app.state.store.find_remote_workspace(str(computer["id"]), created_path)
    assert workspace is not None
    assert workspace["display_name"] == "새 프로젝트"
    assert app.state.store.list_terminals(str(workspace["id"]))


@pytest.mark.asyncio
async def test_remote_project_route_keeps_created_folder_when_workspace_open_fails(
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
    computer = app.state.store.create_computer(
        name="GPU Server",
        ssh_alias="",
        host="gpu.example",
        port=22,
        username="dev",
        identity_file="/tmp/key",
        auth_kind="key",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    created_path = "/workspace/keep-me"
    monkeypatch.setattr(
        app.state.ssh,
        "create_project_directory",
        lambda *_args, **_kwargs: created_path,
    )
    monkeypatch.setattr(
        app.state.workspaces,
        "open_remote",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        response = await client.post(
            f"/computers/{computer['id']}/projects",
            data={
                "_csrf": settings.csrf_token,
                "parent": "/workspace",
                "name": "keep-me",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    location = response.headers["location"]
    assert "project_created=%2Fworkspace%2Fkeep-me" in location
    assert "project_existing=%2Fworkspace%2Fkeep-me" in location
    assert "project_error=" in location
    assert "database+unavailable" in location


@pytest.mark.asyncio
async def test_open_workspace_requires_explicit_local_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        response = await client.post(
            "/api/workspaces",
            data={"_csrf": settings.csrf_token, "path": "."},
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Local folder location is required"


@pytest.mark.asyncio
async def test_streaming_upload_endpoint_writes_and_protects_existing_file(tmp_path: Path) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        uploaded = await client.post(
            f"/w/{workspace['id']}/files/upload-stream",
            params={"parent": ".", "filename": "stream & #?.txt", "overwrite": "false"},
            headers={"X-Termroom-CSRF": settings.csrf_token},
            content=b"first stream\n",
        )
        assert uploaded.status_code == 200
        assert uploaded.json() == {"ok": True, "name": "stream & #?.txt"}
        assert (project / "stream & #?.txt").read_bytes() == b"first stream\n"

        conflict = await client.post(
            f"/w/{workspace['id']}/files/upload-stream",
            params={"parent": ".", "filename": "stream & #?.txt", "overwrite": "false"},
            headers={"X-Termroom-CSRF": settings.csrf_token},
            content=b"must not overwrite\n",
        )
        assert conflict.status_code == 409
        assert conflict.json()["ok"] is False
        assert (project / "stream & #?.txt").read_bytes() == b"first stream\n"

        overwritten = await client.post(
            f"/w/{workspace['id']}/files/upload-stream",
            params={"parent": ".", "filename": "stream & #?.txt", "overwrite": "true"},
            headers={"X-Termroom-CSRF": settings.csrf_token},
            content=b"replaced\n",
        )
        assert overwritten.status_code == 200
        assert (project / "stream & #?.txt").read_bytes() == b"replaced\n"

        csrf_failure = await client.post(
            f"/w/{workspace['id']}/files/upload-stream",
            params={"parent": ".", "filename": "blocked.txt"},
            content=b"blocked\n",
        )
        assert csrf_failure.status_code == 403
        assert not (project / "blocked.txt").exists()


@pytest.mark.asyncio
async def test_upload_does_not_silently_overwrite_existing_file(tmp_path: Path) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    (project / "keep.txt").write_text("old", encoding="utf-8")
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        response = await client.post(
            f"/w/{workspace['id']}/files/upload",
            data={"_csrf": settings.csrf_token, "parent": ".", "overwrite": "0"},
            files={"files": ("keep.txt", b"new", "text/plain")},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert (project / "keep.txt").read_text(encoding="utf-8") == "old"


@pytest.mark.asyncio
async def test_file_flows_preserve_special_character_paths(tmp_path: Path) -> None:
    root = tmp_path / "root"
    project = root / "project"
    special_dir = project / "결과 & logs #1?"
    special_dir.mkdir(parents=True)
    special_file = special_dir / "메모 #1?.txt"
    special_file.write_text("before\n", encoding="utf-8")
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        parent = "결과 & logs #1?"

        created = await client.post(
            f"/w/{workspace['id']}/files/create",
            data={
                "_csrf": settings.csrf_token,
                "parent": parent,
                "kind": "file",
                "name": "new & #?.txt",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        created_location = httpx.URL(created.headers["location"])
        assert created_location.params["path"] == parent
        created_page = await client.get(created.headers["location"])
        assert created_page.status_code == 200
        assert "new &amp; #?.txt" in created_page.text

        relative_file = f"{parent}/메모 #1?.txt"
        editor = await client.get(
            f"/w/{workspace['id']}/edit/{quote(relative_file, safe='/')}"
        )
        assert editor.status_code == 200
        snapshot = app.state.files.read_text(project, relative_file)
        saved = await client.post(
            f"/w/{workspace['id']}/edit/{quote(relative_file, safe='/')}",
            data={
                "_csrf": settings.csrf_token,
                "digest": snapshot.digest,
                "mtime_ns": str(snapshot.mtime_ns),
                "content": "after\n",
            },
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert "#" not in saved.headers["location"]
        saved_page = await client.get(saved.headers["location"])
        assert saved_page.status_code == 200
        assert special_file.read_text(encoding="utf-8") == "after\n"

        view = await client.get(
            f"/w/{workspace['id']}/view/{quote(relative_file, safe='/')}"
        )
        assert view.status_code == 200
        assert "path=%EA%B2%B0%EA%B3%BC" in view.text
        assert "%26" in view.text
        assert "%23" in view.text


@pytest.mark.asyncio
async def test_large_directories_are_paginated_in_browser(tmp_path: Path) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    for index in range(205):
        (project / f"file-{index:03d}.txt").write_text(str(index), encoding="utf-8")
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        first = await client.get(f"/w/{workspace['id']}/files")
        second = await client.get(f"/w/{workspace['id']}/files?page=2")

        assert first.status_code == 200
        assert "1/2 페이지 · 총 205개" in first.text
        assert "file-000.txt" in first.text
        assert "file-204.txt" not in first.text
        assert second.status_code == 200
        assert "2/2 페이지 · 총 205개" in second.text
        assert "file-204.txt" in second.text

        conflict_check = await client.post(
            f"/w/{workspace['id']}/files/upload-check",
            headers={"X-Termroom-CSRF": settings.csrf_token},
            json={"parent": ".", "names": ["file-204.txt"]},
        )
        assert conflict_check.status_code == 200
        assert conflict_check.json()["conflicts"][0]["name"] == "file-204.txt"

        duplicate_check = await client.post(
            f"/w/{workspace['id']}/files/upload-check",
            headers={"X-Termroom-CSRF": settings.csrf_token},
            json={
                "parent": ".",
                "names": ["first.txt", "second.txt", "second.txt", "first.txt"],
            },
        )
        assert duplicate_check.status_code == 400
        assert "first.txt" in duplicate_check.json()["error"]


@pytest.mark.asyncio
async def test_oversized_image_uses_download_instead_of_inline_preview(tmp_path: Path) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    huge = project / "huge.png"
    with huge.open("wb") as handle:
        handle.truncate(MAX_INLINE_IMAGE_BYTES + 1)
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        view = await client.get(f"/w/{workspace['id']}/view/huge.png")
        raw = await client.get(f"/w/{workspace['id']}/raw/huge.png")

        assert view.status_code == 200
        assert "브라우저 미리보기에는 너무 큰 파일입니다" in view.text
        assert "/raw/huge.png" not in view.text
        assert raw.status_code == 413


@pytest.mark.asyncio
async def test_editor_preserves_submitted_content_when_file_disappears(tmp_path: Path) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    target = project / "note.txt"
    target.write_text("before\n", encoding="utf-8")
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    snapshot = app.state.files.read_text(project, "note.txt")
    target.unlink()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        response = await client.post(
            f"/w/{workspace['id']}/edit/note.txt",
            data={
                "_csrf": settings.csrf_token,
                "digest": snapshot.digest,
                "mtime_ns": str(snapshot.mtime_ns),
                "content": "my unsaved content\n",
            },
        )

    assert response.status_code == 409
    assert "저장하지 못했습니다" in response.text
    assert "my unsaved content" in response.text
    assert 'data-unsaved="1"' in response.text


@pytest.mark.asyncio
async def test_editor_conflict_marks_preserved_content_as_unsaved(tmp_path: Path) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    target = project / "note.txt"
    target.write_text("before\n", encoding="utf-8")
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    snapshot = app.state.files.read_text(project, "note.txt")
    target.write_text("changed elsewhere\n", encoding="utf-8")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        response = await client.post(
            f"/w/{workspace['id']}/edit/note.txt",
            data={
                "_csrf": settings.csrf_token,
                "digest": snapshot.digest,
                "mtime_ns": str(snapshot.mtime_ns),
                "content": "my conflicted content\n",
            },
        )

    assert response.status_code == 409
    assert "외부 변경을 감지했습니다" in response.text
    assert "my conflicted content" in response.text
    assert 'data-unsaved="1"' in response.text


@pytest.mark.asyncio
async def test_save_and_run_conflict_creates_no_run_or_managed_terminal(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    target = project / "main.py"
    target.write_text("print('before')\n", encoding="utf-8")
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    snapshot = app.state.files.read_text(project, "main.py")
    target.write_text("print('changed elsewhere')\n", encoding="utf-8")
    key = str(uuid.uuid4())
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        await _login(client)
        response = await client.post(
            f"/w/{workspace['id']}/edit/main.py",
            data={
                "_csrf": settings.csrf_token,
                "digest": snapshot.digest,
                "mtime_ns": str(snapshot.mtime_ns),
                "content": "print('my change')\n",
                "intent": "save_and_run",
                "file_run_idempotency_key": key,
            },
        )

    assert response.status_code == 409
    assert app.state.store.get_file_run_by_idempotency(str(workspace["id"]), key) is None
    assert app.state.store.get_managed_terminal(str(workspace["id"]), "file_run") is None
    assert "print(&#39;my change&#39;)" in response.text


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
async def test_file_run_http_lifecycle_is_idempotent_and_csrf_protected(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    target = project / "wait.py"
    content = (
        "import signal, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "while True: time.sleep(0.1)\n"
    )
    target.write_text(content, encoding="utf-8")
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    snapshot = app.state.files.read_text(project, "wait.py")
    key = str(uuid.uuid4())
    payload = {
        "_csrf": settings.csrf_token,
        "digest": snapshot.digest,
        "mtime_ns": str(snapshot.mtime_ns),
        "content": content,
        "intent": "save_and_run",
        "file_run_idempotency_key": key,
    }
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            await _login(client)
            started = await client.post(
                f"/w/{workspace['id']}/edit/wait.py",
                data=payload,
                follow_redirects=False,
            )
            assert started.status_code == 303
            run = app.state.store.get_file_run_by_idempotency(
                str(workspace["id"]), key
            )
            assert run is not None
            run_id = str(run["id"])

            replay = await client.post(
                f"/w/{workspace['id']}/edit/wait.py",
                data=payload,
                follow_redirects=False,
            )
            assert replay.status_code == 303
            assert app.state.store.get_file_run_by_idempotency(
                str(workspace["id"]), key
            )["id"] == run_id

            conflict = await client.post(
                f"/w/{workspace['id']}/edit/wait.py",
                data={**payload, "content": "print('different')\n"},
            )
            assert conflict.status_code == 409
            assert "다른 파일 내용에 이미 사용되었습니다" in conflict.text

            status_payload: dict[str, object] = {}
            for _attempt in range(40):
                status = await client.get(f"/api/file-runs/{run_id}/status")
                assert status.status_code == 200
                status_payload = status.json()
                if status_payload.get("state") == "running":
                    break
                await asyncio.sleep(0.05)
            assert status_payload["state"] == "running"

            assert (await client.post(f"/file-runs/{run_id}/stop")).status_code == 403
            stopped = await client.post(
                f"/file-runs/{run_id}/stop",
                data={"_csrf": settings.csrf_token, "return_to": "editor"},
                follow_redirects=False,
            )
            assert stopped.status_code == 303
            after_stop = await client.get(f"/api/file-runs/{run_id}/status")
            assert after_stop.json()["needs_force"] is True

            assert (await client.post(f"/file-runs/{run_id}/kill")).status_code == 403
            killed = await client.post(
                f"/file-runs/{run_id}/kill",
                data={"_csrf": settings.csrf_token, "return_to": "editor"},
                follow_redirects=False,
            )
            assert killed.status_code == 303
            final = await client.get(f"/api/file-runs/{run_id}/status")
            assert final.json()["state"] == "stopped"

        events = app.state.store.list_activity_events()
        assert [event["kind"] for event in events] == ["file_run.stopped"]
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", str(workspace["tmux_session"])],
            check=False,
            capture_output=True,
        )


@pytest.mark.asyncio
async def test_missing_and_nonempty_paths_return_localized_errors(tmp_path: Path) -> None:
    root = tmp_path / "root"
    project = root / "project"
    nonempty = project / "not-empty"
    nonempty.mkdir(parents=True)
    (nonempty / "child.txt").write_text("keep\n", encoding="utf-8")
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        missing = await client.get(f"/w/{workspace['id']}/files?path=gone")
        assert missing.status_code == 404
        assert "파일 또는 폴더가 더 이상 존재하지 않습니다" in missing.text

        deleted = await client.post(
            f"/w/{workspace['id']}/files/delete",
            data={"_csrf": settings.csrf_token, "path": "not-empty"},
            follow_redirects=False,
        )
        assert deleted.status_code == 303
        error_page = await client.get(deleted.headers["location"])
        assert error_page.status_code == 200
        assert "폴더가 비어 있지 않아 삭제할 수 없습니다" in error_page.text


@pytest.mark.asyncio
async def test_remote_recent_keeps_last_successful_results_on_refresh_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    computer = app.state.store.create_computer(
        name="QA server",
        ssh_alias="",
        host="127.0.0.1",
        port=22,
        username="qa",
        identity_file="/tmp/key",
        auth_kind="key",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    workspace = app.state.workspaces.open_remote(
        str(computer["id"]), "/srv/project", "remote-project"
    )
    terminal = app.state.store.create_terminal(workspace["id"], "shell", "@qa")
    recent_scan = RecentFiles(
        entries=[
            FileEntry(
                name="result.csv",
                relative_path="output/result.csv",
                is_dir=False,
                size=123,
                mtime_ns=1_800_000_000_000_000_000,
            )
        ],
        scanned_files=1,
        truncated=False,
    )
    monkeypatch.setattr(app.state.ssh, "recent_files", lambda workspace: recent_scan)
    monkeypatch.setattr(
        app.state.ssh,
        "ensure_workspace",
        lambda workspace: [app.state.store.get_terminal(terminal["id"])],
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        first = await client.get(f"/w/{workspace['id']}/recent")
        assert first.status_code == 200
        assert "result.csv" in first.text

        def fail_recent(workspace: dict[str, object]) -> RecentFiles:
            raise SSHBackendError(
                "SSH connection refused",
                locale_key="ssh.backend.refused",
                locale_values={"host": "127.0.0.1", "port": 22},
            )

        def fail_terminals(workspace: dict[str, object]) -> list[dict[str, object]]:
            raise SSHBackendError(
                "SSH connection refused",
                locale_key="ssh.backend.refused",
                locale_values={"host": "127.0.0.1", "port": 22},
            )

        monkeypatch.setattr(app.state.ssh, "recent_files", fail_recent)
        monkeypatch.setattr(app.state.ssh, "ensure_workspace", fail_terminals)
        stale = await client.get(f"/w/{workspace['id']}/recent")

    assert stale.status_code == 200
    assert "result.csv" in stale.text
    assert "원격 상태를 새로 고치지 못했습니다" in stale.text
    assert "SSH 연결이 거부되었습니다" in stale.text
    assert "shell" in stale.text


@pytest.mark.asyncio
async def test_remote_terminal_page_stays_available_while_ssh_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    computer = app.state.store.create_computer(
        name="QA server",
        ssh_alias="",
        host="127.0.0.1",
        port=22,
        username="qa",
        identity_file="/tmp/key",
        auth_kind="key",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    workspace = app.state.workspaces.open_remote(
        str(computer["id"]), "/srv/project", "remote-project"
    )
    terminal = app.state.store.create_terminal(workspace["id"], "shell", "@qa")

    def fail_terminals(workspace: dict[str, object]) -> list[dict[str, object]]:
        raise SSHBackendError(
            "SSH connection refused",
            locale_key="ssh.backend.refused",
            locale_values={"host": "127.0.0.1", "port": 22},
        )

    monkeypatch.setattr(app.state.ssh, "ensure_workspace", fail_terminals)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        response = await client.get(f"/w/{workspace['id']}/terminal")

    assert response.status_code == 200
    assert "shell" in response.text
    assert str(terminal["id"]) in response.text
    assert "SSH 연결이 거부되었습니다" in response.text
    assert "QA server" in response.text
    assert "원격 작업공간" in response.text
    assert "로컬 전용" not in response.text


@pytest.mark.asyncio
async def test_persisted_ssh_error_renders_in_current_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    computer = app.state.store.create_computer(
        name="QA server",
        ssh_alias="",
        host="127.0.0.1",
        port=22,
        username="qa",
        identity_file="",
        auth_kind="password",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    app.state.ssh.save_password(str(computer["id"]), "remote-password")

    def fail_password(computer: dict[str, object], password: str):
        raise SSHBackendError(
            "SSH password authentication failed",
            locale_key="ssh.backend.password_auth",
        )

    monkeypatch.setattr(app.state.ssh, "_connect_password", fail_password)
    with pytest.raises(SSHBackendError):
        app.state.ssh.test_connection(computer)
    stored = app.state.store.get_computer(str(computer["id"]))
    assert str(stored["last_error"]).startswith("termroom-i18n:")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        korean = await client.get(f"/computers/{computer['id']}")
        assert "SSH 비밀번호 인증에 실패했습니다" in korean.text
        await client.get("/locale/en", params={"next": f"/computers/{computer['id']}"})
        english = await client.get(f"/computers/{computer['id']}")
        assert "SSH password authentication failed" in english.text


@pytest.mark.asyncio
async def test_ssh_password_update_verifies_before_replacing_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    computer = app.state.store.create_computer(
        name="QA server",
        ssh_alias="",
        host="127.0.0.1",
        port=22,
        username="qa",
        identity_file="",
        auth_kind="password",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    computer_id = str(computer["id"])
    app.state.ssh.save_password(computer_id, "old-password")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    def reject_password(computer: dict[str, object], password: str):
        raise SSHBackendError(
            "SSH password authentication failed",
            locale_key="ssh.backend.password_auth",
        )

    monkeypatch.setattr(app.state.ssh, "test_password_connection", reject_password)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        failed = await client.post(
            f"/computers/{computer_id}/password",
            data={"_csrf": settings.csrf_token, "password": "wrong-password"},
            follow_redirects=False,
        )
        assert failed.status_code == 303
        assert app.state.ssh._stored_password(computer) == "old-password"

        monkeypatch.setattr(
            app.state.ssh,
            "test_password_connection",
            lambda computer, password: {"tmux": "tmux 3", "shell": "/bin/sh"},
        )
        updated = await client.post(
            f"/computers/{computer_id}/password",
            data={"_csrf": settings.csrf_token, "password": "new-password"},
            follow_redirects=False,
        )
        assert updated.status_code == 303
        assert "password_updated=1" in updated.headers["location"]
        assert app.state.ssh._stored_password(computer) == "new-password"
        detail = await client.get(updated.headers["location"])
        assert "SSH 비밀번호를 변경했습니다" in detail.text


@pytest.mark.asyncio
async def test_terminal_management_routes_rename_and_replace_last_terminal(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    terminal = app.state.terminals.ensure_workspace(workspace)[0]
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            await _login(client)
            renamed = await client.post(
                f"/w/{workspace['id']}/terminals/{terminal['id']}",
                data={
                    "_csrf": settings.csrf_token,
                    "action": "rename",
                    "name": "worker one",
                },
                follow_redirects=False,
            )
            assert renamed.status_code == 303
            assert app.state.store.get_terminal(terminal["id"])["name"] == "worker-one"

            closed = await client.post(
                f"/w/{workspace['id']}/terminals/{terminal['id']}",
                data={"_csrf": settings.csrf_token, "action": "delete"},
                follow_redirects=False,
            )
            assert closed.status_code == 303
            assert app.state.store.get_terminal(terminal["id"]) is None
            assert len(app.state.store.list_terminals(workspace["id"])) == 1
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", workspace["tmux_session"]],
            check=False,
            capture_output=True,
        )


@pytest.mark.asyncio
async def test_command_history_can_be_cleared_from_workspace(tmp_path: Path) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    terminal = app.state.terminals.ensure_workspace(workspace)[0]
    app.state.store.add_command(workspace["id"], terminal["id"], "echo secret-ish")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _login(client)
            response = await client.post(
                f"/w/{workspace['id']}/commands/clear",
                data={"_csrf": settings.csrf_token, "terminal": terminal["id"]},
                follow_redirects=False,
            )

        assert response.status_code == 303
        assert terminal["id"] in response.headers["location"]
        assert app.state.store.list_commands(workspace["id"]) == []
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", workspace["tmux_session"]],
            check=False,
            capture_output=True,
        )


@pytest.mark.asyncio
async def test_remove_ssh_computer_cleans_termroom_registration_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    computer = app.state.store.create_computer(
        name="QA server",
        ssh_alias="",
        host="127.0.0.1",
        port=22,
        username="qa",
        identity_file="",
        auth_kind="password",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    workspace = app.state.workspaces.open_remote(
        str(computer["id"]), "/srv/project", "remote-project"
    )
    terminal = app.state.store.create_terminal(workspace["id"], "shell", "@qa")
    app.state.store.add_command(workspace["id"], terminal["id"], "echo keep-remote-running")
    app.state.ssh.save_password(str(computer["id"]), "remote-password")
    app.state.ssh.remember_host_key(computer)
    credential = settings.state_dir / "credentials" / str(computer["id"])
    assert credential.is_file()
    assert f"termroom-{computer['id']} " in app.state.ssh.known_hosts_path.read_text()

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        response = await client.post(
            f"/computers/{computer['id']}/delete",
            data={"_csrf": settings.csrf_token},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/open?computer_removed=1"
        home = await client.get(response.headers["location"])
        assert "SSH 컴퓨터 등록을 삭제했습니다" in home.text
        assert "이 컴퓨터" in home.text

    assert app.state.store.get_computer(str(computer["id"])) is None
    assert app.state.store.get_workspace(str(workspace["id"])) is None
    assert app.state.store.get_terminal(str(terminal["id"])) is None
    assert not credential.exists()
    assert f"termroom-{computer['id']} " not in app.state.ssh.known_hosts_path.read_text()


@pytest.mark.asyncio
async def test_remove_ssh_computer_keeps_credentials_when_remote_run_exists(
    tmp_path: Path,
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
    computer = app.state.store.create_computer(
        name="Busy GPU",
        ssh_alias="",
        host="127.0.0.1",
        port=22,
        username="qa",
        identity_file="",
        auth_kind="password",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    run_id = str(uuid.uuid4())
    app.state.store.create_remote_run(
        {
            "id": run_id,
            "source_kind": "archive",
            "archive_format": "zip",
            "source_workspace_id": None,
            "source_path": None,
            "source_label": "source.zip",
            "source_url": None,
            "source_options_json": '{"archive_name":"source.zip","policy":1}',
            "source_revision": None,
            "source_size": None,
            "target_computer_id": str(computer["id"]),
            "command": "python main.py",
            "run_base": "/home/qa/.cache/termroom/runs",
            "workspace_id": None,
            "state": "preparing",
            "phase": "waiting_upload",
            "created_at": "2026-08-09T00:00:00+00:00",
        }
    )
    app.state.ssh.save_password(str(computer["id"]), "keep-this-password")
    credential = settings.state_dir / "credentials" / str(computer["id"])
    assert credential.is_file()

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        response = await client.post(
            f"/computers/{computer['id']}/delete",
            data={"_csrf": settings.csrf_token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith(f"/computers/{computer['id']}?")

    assert app.state.store.get_computer(str(computer["id"])) is not None
    assert app.state.store.get_remote_run(run_id) is not None
    assert credential.is_file()


@pytest.mark.asyncio
async def test_ssh_probe_accepts_openssh_proxy_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(root, state_dir=tmp_path / "state", access_token="test-token")
    app = create_app(settings)
    target = {
        "ssh_alias": "gpu",
        "host": "10.0.0.8",
        "port": 22,
        "username": "runner",
        "identity_file": "",
        "proxycommand": "nc %h %p",
        "proxyjump": "",
    }
    monkeypatch.setattr(app.state.ssh, "resolve_target", lambda value: dict(target))
    seen: list[dict[str, object]] = []

    def probe(resolved: dict[str, object]) -> dict[str, str]:
        seen.append(dict(resolved))
        return {
            "host_key_type": "ssh-ed25519",
            "host_key_data": "AAAATESTKEY",
            "host_fingerprint": "SHA256:test",
        }

    monkeypatch.setattr(app.state.ssh, "probe_target_host_key", probe)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        response = await client.post(
            "/computers/probe",
            data={
                "_csrf": settings.csrf_token,
                "target": "gpu",
                "username": "",
                "port": "",
                "auth_mode": "existing",
            },
        )

    assert response.status_code == 200
    assert seen and seen[0]["proxycommand"] == "nc %h %p"
    assert response.json()["host"] == "10.0.0.8"


@pytest.mark.asyncio
async def test_failed_ssh_computer_registration_rolls_back_local_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    ssh = app.state.ssh
    monkeypatch.setattr(
        ssh,
        "ensure_managed_key",
        lambda: {"private_key": "/tmp/termroom-test-key", "public_key": "ssh-ed25519 AAAA test"},
    )
    monkeypatch.setattr(
        ssh,
        "resolve_target",
        lambda value: {
            "ssh_alias": value,
            "host": "example.test",
            "port": 22,
            "username": "qa",
            "identity_file": "",
            "proxycommand": "",
        },
    )
    host_key = {
        "host_key_type": "ssh-ed25519",
        "host_key_data": "AAAATESTKEY",
        "host_fingerprint": "SHA256:test",
    }
    monkeypatch.setattr(ssh, "probe_target_host_key", lambda target: host_key)
    monkeypatch.setattr(
        ssh,
        "test_connection",
        lambda computer: {"tmux": "tmux 3", "shell": "/bin/sh"},
    )

    def fail_known_hosts(computer: dict[str, object]) -> None:
        raise OSError("known_hosts is not writable")

    monkeypatch.setattr(ssh, "remember_host_key", fail_known_hosts)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        response = await client.post(
            "/computers",
            data={
                "_csrf": settings.csrf_token,
                "target": "example.test",
                "username": "qa",
                "port": "22",
                "name": "QA",
                "auth_mode": "key",
                "host_key_type": host_key["host_key_type"],
                "host_key_data": host_key["host_key_data"],
                "host_fingerprint": host_key["host_fingerprint"],
                "confirm_fingerprint": "1",
            },
        )

    assert response.status_code == 400
    assert app.state.store.list_computers() == []
    credentials = settings.state_dir / "credentials"
    assert not credentials.exists() or not list(credentials.iterdir())


@pytest.mark.asyncio
async def test_ssh_password_setup_page_does_not_require_ssh_keygen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    real_which = __import__("shutil").which

    def without_keygen(command: str):  # type: ignore[no-untyped-def]
        if command == "ssh-keygen":
            return None
        return real_which(command)

    monkeypatch.setattr("termroom.ssh_backend.shutil.which", without_keygen)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        response = await client.get("/computers/new")

    assert response.status_code == 200
    assert 'href="/open"' in response.text
    assert 'name="auth_mode" value="password" checked' in response.text
    assert 'name="auth_mode" value="key"' in response.text
    assert 'value="key"  disabled' in response.text
    assert "ssh-keygen이 필요합니다" in response.text


@pytest.mark.asyncio
async def test_failed_remote_workspace_start_rolls_back_new_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    computer = app.state.store.create_computer(
        name="QA server",
        ssh_alias="",
        host="127.0.0.1",
        port=22,
        username="qa",
        identity_file="/tmp/key",
        auth_kind="key",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint="SHA256:test",
    )
    monkeypatch.setattr(
        app.state.ssh,
        "validate_workspace_path",
        lambda computer, path: "/srv/project",
    )

    def fail_tmux(workspace: dict[str, object]) -> list[dict[str, object]]:
        raise SSHBackendError("remote tmux failed")

    monkeypatch.setattr(app.state.ssh, "ensure_workspace", fail_tmux)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        response = await client.post(
            f"/computers/{computer['id']}/workspaces",
            data={
                "_csrf": settings.csrf_token,
                "path": "/srv/project",
                "display_name": "project",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert app.state.store.list_workspaces_for_computer(str(computer["id"])) == []


@pytest.mark.asyncio
async def test_file_browser_hides_dependency_noise_but_keeps_useful_dotfiles(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    (project / ".venv").mkdir()
    (project / "__pycache__").mkdir()
    (project / ".env").write_text("EXAMPLE=1\n", encoding="utf-8")
    settings = Settings.create(
        root,
        state_dir=project / ".termroom-state",
        access_token="test-token",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        default_view = await client.get(f"/w/{workspace['id']}/files")
        noisy_view = await client.get(f"/w/{workspace['id']}/files?noise=1")

        assert ".env" in default_view.text
        assert ".venv" not in default_view.text
        assert "__pycache__" not in default_view.text
        assert ".termroom-state" not in default_view.text
        assert ".venv" in noisy_view.text
        assert "__pycache__" in noisy_view.text
        assert ".termroom-state" not in noisy_view.text


@pytest.mark.asyncio
async def test_internal_config_is_not_addressable_through_file_routes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    state = root / ".termroom-state"
    settings = Settings.create(root, state_dir=state, access_token="test-token")
    app = create_app(settings)
    workspace = app.state.workspaces.open(".")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        for path in (
            ".termroom-state",
            ".termroom-state/access-token",
            ".termroom-state/ssh/id_ed25519",
        ):
            response = await client.get(
                f"/w/{workspace['id']}/view/{quote(path, safe='/')}",
                headers={"Accept": "text/html"},
            )
            assert response.status_code == 403
            assert "내부 설정 파일은 파일 화면에서 열 수 없습니다" in response.text

        download = await client.get(
            f"/w/{workspace['id']}/download/.termroom-state/access-token"
        )
        assert download.status_code == 403

        upload = await client.post(
            f"/w/{workspace['id']}/files/upload-stream",
            params={
                "parent": ".termroom-state",
                "filename": "injected.txt",
                "overwrite": "false",
            },
            headers={"X-Termroom-CSRF": settings.csrf_token},
            content=b"blocked\n",
        )
        assert upload.status_code == 403
        assert not (state / "injected.txt").exists()


@pytest.mark.asyncio
async def test_pwa_public_contract_does_not_cache_workspace_pages(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        worker = await client.get("/sw.js")
        manifest = await client.get("/static/manifest.webmanifest")
        icons = {
            size: await client.get(f"/icons/termroom-{size}.png") for size in (192, 512)
        }
        missing_icon = await client.get("/icons/termroom-256.png")
        unauthorized_home = await client.get("/")

        assert worker.status_code == 200
        assert worker.headers["service-worker-allowed"] == "/"
        assert worker.headers["cache-control"] == "no-cache"
        assert "caches.open" not in worker.text
        assert manifest.status_code == 200
        manifest_payload = manifest.json()
        assert manifest_payload["name"] == "Termroom"
        assert manifest_payload["short_name"] == "Termroom"
        png_icons = {
            (icon["sizes"], icon["type"], icon["purpose"])
            for icon in manifest_payload["icons"]
            if icon["src"].endswith(".png")
        }
        assert ("192x192", "image/png", "any") in png_icons
        assert ("512x512", "image/png", "any maskable") in png_icons
        for size, icon in icons.items():
            assert icon.status_code == 200
            assert icon.headers["content-type"] == "image/png"
            assert icon.content.startswith(b"\x89PNG\r\n\x1a\n")
            assert int.from_bytes(icon.content[16:20], "big") == size
            assert int.from_bytes(icon.content[20:24], "big") == size
            assert icon.content[24] == 8
            assert icon.content[25] == 2
        assert missing_icon.status_code == 404
        assert unauthorized_home.status_code == 401
