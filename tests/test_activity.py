from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from termroom.app import create_app
from termroom.config import Settings
from termroom.db import utc_now


async def _login(client: httpx.AsyncClient) -> None:
    client.cookies.set("termroom_locale", "ko")
    response = await client.post(
        "/login",
        data={"password": "test-token"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _app(tmp_path: Path):  # type: ignore[no-untyped-def]
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
        name="GPU 서버",
        ssh_alias="",
        host="gpu.example.test",
        port=22,
        username="runner",
        identity_file="",
        host_key_type="ssh-ed25519",
        host_key_data="AAAA",
        host_fingerprint="SHA256:test",
    )
    return app, settings, computer


def _terminal_run(app: object, computer: dict[str, object], *, exit_code: int) -> str:
    run_id = str(uuid.uuid4())
    app.state.store.create_remote_run(  # type: ignore[attr-defined]
        {
            "id": run_id,
            "source_kind": "git",
            "source_workspace_id": None,
            "source_path": None,
            "source_label": "example/project",
            "source_url": "https://example.test/project.git",
            "source_options_json": "{}",
            "source_revision": None,
            "source_size": None,
            "target_computer_id": str(computer["id"]),
            "command": "TOKEN=secret cat /private/result.txt",
            "run_base": "/srv/private/termroom-runs",
            "state": "running",
            "phase": None,
            "created_at": utc_now(),
        }
    )
    assert app.state.store.transition_remote_run(  # type: ignore[attr-defined]
        run_id,
        expected_states={"running"},
        state="finished",
        phase=None,
        started_at=utc_now(),
        ended_at=utc_now(),
        exit_code=exit_code,
        error_detail="private output",
    )
    return run_id


def _file_run(
    app: object,
    workspace: dict[str, object],
    *,
    relative_path: str,
    duration_seconds: int,
    exit_code: int,
    terminal: bool = False,
) -> str:
    run_id = str(uuid.uuid4())
    status, _run = app.state.store.claim_file_run(  # type: ignore[attr-defined]
        {
            "id": run_id,
            "workspace_id": str(workspace["id"]),
            "idempotency_key": str(uuid.uuid4()),
            "relative_path": relative_path,
            "source_digest": "a" * 64,
            "runner_id": "python3",
            "runner_version": 1,
            "argv": ("python3", "--", f"./{relative_path}"),
        }
    )
    assert status == "created"
    if terminal:
        terminal_row = app.state.store.create_terminal(  # type: ignore[attr-defined]
            str(workspace["id"]),
            "Run",
            "@file-run",
            role="file_run",
            managed_run_id=run_id,
        )
        assert app.state.store.set_file_run_terminal(  # type: ignore[attr-defined]
            run_id, str(terminal_row["id"])
        )
    started = datetime.now(UTC)
    ended = started + timedelta(seconds=duration_seconds)
    assert app.state.store.transition_file_run(  # type: ignore[attr-defined]
        run_id,
        expected_states={"preparing"},
        state="finished",
        started_at=started.isoformat(timespec="seconds"),
        ended_at=ended.isoformat(timespec="seconds"),
        exit_code=exit_code,
    )
    return run_id


@pytest.mark.asyncio
async def test_activity_read_open_deleted_target_and_safe_notification_claim(
    tmp_path: Path,
) -> None:
    app, settings, computer = _app(tmp_path)
    first_run_id = _terminal_run(app, computer, exit_code=0)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        await _login(client)

        home = await client.get("/")
        assert home.status_code == 200
        assert 'href="/activity"' in home.text
        assert "data-activity-unread" in home.text

        page = await client.get("/activity")
        assert page.status_code == 200
        assert "원격 실행 완료" in page.text
        assert "example/project" in page.text
        assert "GPU 서버" in page.text
        assert "TOKEN=secret" not in page.text
        assert "/private/result.txt" not in page.text
        assert "private output" not in page.text

        summary = await client.get("/api/activity/summary")
        assert summary.json() == {"ok": True, "unread_count": 1}

        first_event = app.state.store.list_activity_events()[0]
        opened = await client.post(
            f"/activity/{first_event['id']}/open",
            data={"_csrf": settings.csrf_token},
            follow_redirects=False,
        )
        assert opened.status_code == 303
        assert opened.headers["location"] == f"/remote-runs/{first_run_id}"
        assert app.state.store.count_unread_events() == 0

        registered = await client.post(
            "/api/activity/notifications/claim",
            headers={"X-Termroom-CSRF": settings.csrf_token},
            json={},
        )
        assert registered.status_code == 200
        assert registered.json()["events"] == []

        second_run_id = _terminal_run(app, computer, exit_code=7)
        claims = await asyncio.gather(
            client.post(
                "/api/activity/notifications/claim",
                headers={"X-Termroom-CSRF": settings.csrf_token},
                json={},
            ),
            client.post(
                "/api/activity/notifications/claim",
                headers={"X-Termroom-CSRF": settings.csrf_token},
                json={},
            ),
        )
        payloads = [event for response in claims for event in response.json()["events"]]
        assert len(payloads) == 1
        assert payloads[0]["kind"] == "remote_run.failed"
        assert payloads[0]["url"] == f"/remote-runs/{second_run_id}"
        serialized = repr(payloads[0])
        assert "TOKEN=secret" not in serialized
        assert "/private/" not in serialized
        assert "private output" not in serialized

        second_event = app.state.store.list_activity_events()[0]
        app.state.store.delete_remote_run(second_run_id)
        deleted_page = await client.get("/activity")
        assert "대상을 열 수 없음" in deleted_page.text
        assert "example/project" in deleted_page.text

        deleted_open = await client.post(
            f"/activity/{second_event['id']}/open",
            data={"_csrf": settings.csrf_token},
            follow_redirects=False,
        )
        assert deleted_open.status_code == 303
        assert deleted_open.headers["location"] == "/activity?unavailable=1"

        read_all = await client.post(
            "/activity/read-all",
            data={"_csrf": settings.csrf_token},
            follow_redirects=False,
        )
        assert read_all.status_code == 303
        assert app.state.store.count_unread_events() == 0


@pytest.mark.asyncio
async def test_activity_mutations_require_csrf(tmp_path: Path) -> None:
    app, _settings, computer = _app(tmp_path)
    _terminal_run(app, computer, exit_code=0)
    event = app.state.store.list_activity_events()[0]
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        await _login(client)
        assert (await client.post("/activity/read-all")).status_code == 403
        assert (
            await client.post(f"/activity/{event['id']}/read")
        ).status_code == 403
        assert (
            await client.post("/api/activity/notifications/claim", json={})
        ).status_code == 403


@pytest.mark.asyncio
async def test_file_run_activity_targets_exact_outcome_and_notification_threshold(
    tmp_path: Path,
) -> None:
    app, settings, _computer = _app(tmp_path)
    project = settings.root / "project"
    project.mkdir()
    for name in ("short.py", "long.py", "failed.py"):
        (project / name).write_text("print('ok')\n", encoding="utf-8")
    workspace = app.state.workspaces.open("project")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        await _login(client)
        registered = await client.post(
            "/api/activity/notifications/claim",
            headers={"X-Termroom-CSRF": settings.csrf_token},
            json={},
        )
        assert registered.json()["events"] == []

        short_id = _file_run(
            app,
            workspace,
            relative_path="short.py",
            duration_seconds=29,
            exit_code=0,
        )
        long_id = _file_run(
            app,
            workspace,
            relative_path="long.py",
            duration_seconds=30,
            exit_code=0,
        )
        failed_id = _file_run(
            app,
            workspace,
            relative_path="failed.py",
            duration_seconds=1,
            exit_code=7,
            terminal=True,
        )

        claimed = await client.post(
            "/api/activity/notifications/claim",
            headers={"X-Termroom-CSRF": settings.csrf_token},
            json={},
        )
        notifications = claimed.json()["events"]
        assert {event["kind"] for event in notifications} == {
            "file_run.completed",
            "file_run.failed",
        }
        assert all(f"run={short_id}" not in event["url"] for event in notifications)
        assert any(f"run={long_id}" in event["url"] for event in notifications)
        assert any(f"run={failed_id}" in event["url"] for event in notifications)

        page = await client.get("/activity")
        assert page.status_code == 200
        assert "파일 실행 완료" in page.text
        assert "파일 실행 실패" in page.text
        assert "short.py" in page.text
        assert "long.py" in page.text
        assert "failed.py" in page.text

        failed_event = next(
            event
            for event in app.state.store.list_activity_events()
            if event["subject_id"] == failed_id
        )
        opened = await client.post(
            f"/activity/{failed_event['id']}/open",
            data={"_csrf": settings.csrf_token},
            follow_redirects=False,
        )
        assert opened.status_code == 303
        assert opened.headers["location"].endswith(
            f"/edit/failed.py?run={failed_id}"
        )

        terminal = app.state.store.get_managed_terminal(
            str(workspace["id"]), "file_run"
        )
        assert terminal is not None
        opened_terminal = await client.post(
            f"/activity/{failed_event['id']}/open",
            data={"_csrf": settings.csrf_token, "destination": "terminal"},
            follow_redirects=False,
        )
        assert opened_terminal.status_code == 303
        assert f"terminal={terminal['id']}" in opened_terminal.headers["location"]
