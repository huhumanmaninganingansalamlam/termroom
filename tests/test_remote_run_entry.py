from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest

from termroom.app import create_app
from termroom.config import Settings


async def _login(client: httpx.AsyncClient) -> None:
    client.cookies.set("termroom_locale", "ko")
    response = await client.post(
        "/login",
        data={"password": "test-token"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _computer(app, *, name: str, host: str) -> dict[str, object]:  # type: ignore[no-untyped-def]
    return app.state.store.create_computer(
        name=name,
        ssh_alias="",
        host=host,
        port=22,
        username="runner",
        identity_file="",
        host_key_type="ssh-ed25519",
        host_key_data="AAAATESTKEY",
        host_fingerprint=f"SHA256:{name}",
    )


def _remote_run_values(computer_id: str, *, source_label: str) -> dict[str, object]:
    run_id = str(uuid.uuid4())
    return {
        "id": run_id,
        "source_kind": "git",
        "source_workspace_id": None,
        "source_path": None,
        "source_label": source_label,
        "source_url": f"https://example.test/{source_label}.git",
        "source_options_json": '{"policy":1}',
        "source_revision": None,
        "source_size": None,
        "target_computer_id": computer_id,
        "command": "python inference.py\necho done",
        "run_base": "/home/runner/.cache/termroom/runs",
        "workspace_id": None,
        "state": "running",
        "phase": None,
        "created_at": "2026-08-09T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_workspace_files_prefill_remote_run_source_and_target(tmp_path: Path) -> None:
    root = tmp_path / "root"
    source_root = root / "training"
    source_folder = source_root / "models"
    source_folder.mkdir(parents=True)
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
        default_locale="ko",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open_local(root, "training")
    computer = _computer(app, name="GPU QA", host="gpu.example.test")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        files = await client.get(f"/w/{workspace['id']}/files?path=models")
        assert files.status_code == 200
        assert (
            f"/remote-runs/new?source_workspace_id={workspace['id']}&source_path=models"
        ) in files.text
        assert "다른 Remote에서 실행" in files.text

        form = await client.get(
            "/remote-runs/new",
            params={
                "source_workspace_id": workspace["id"],
                "source_path": "models",
                "target_computer_id": computer["id"],
            },
        )

    assert form.status_code == 200
    assert '<input type="hidden" name="source_kind" value="workspace">' in form.text
    assert (
        f'<input type="hidden" name="source_workspace_id" value="{workspace["id"]}">' in form.text
    )
    assert '<input type="hidden" name="source_path" value="models">' in form.text
    assert str(source_folder) in form.text
    assert f'<option value="{computer["id"]}" selected>' in form.text
    assert "GPU QA · runner@gpu.example.test" in form.text


@pytest.mark.asyncio
async def test_remote_open_page_has_git_archive_entry_and_only_its_reconnectable_runs(
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
    gpu = _computer(app, name="GPU QA", host="gpu.example.test")
    other = _computer(app, name="Render QA", host="render.example.test")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        empty = await client.get(f"/open/{gpu['id']}")
        assert empty.status_code == 200
        assert f"/remote-runs/new?target_computer_id={gpu['id']}&source_kind=git" in empty.text
        assert (
            f"/remote-runs/new?target_computer_id={gpu['id']}&source_kind=archive"
            in empty.text
        )
        assert "이 Remote의 실행 기록" not in empty.text

        gpu_values = _remote_run_values(str(gpu["id"]), source_label="vision/model")
        other_values = _remote_run_values(str(other["id"]), source_label="render/scene")
        app.state.store.create_remote_run(gpu_values)
        app.state.store.create_remote_run(other_values)

        populated = await client.get(f"/open/{gpu['id']}")
        git_form = await client.get(
            "/remote-runs/new",
            params={"target_computer_id": gpu["id"], "source_kind": "git"},
        )

    assert populated.status_code == 200
    assert "이 Remote의 실행 기록" in populated.text
    assert f'href="/remote-runs/{gpu_values["id"]}"' in populated.text
    assert "vision/model" in populated.text
    assert "python inference.py" in populated.text
    assert f"/remote-runs/{other_values['id']}" not in populated.text
    assert 'name="source_kind" value="git" checked' in git_form.text
    assert f'<option value="{gpu["id"]}" selected>' in git_form.text


@pytest.mark.asyncio
async def test_failed_workspace_remote_run_links_source_and_restores_retry_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    source_folder = root / "training" / "models"
    source_folder.mkdir(parents=True)
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
        default_locale="ko",
    )
    app = create_app(settings)
    source_workspace = app.state.workspaces.open_local(root, "training")
    computer = _computer(app, name="GPU QA", host="gpu.example.test")
    command = "python train.py --resume\necho retry"
    run_id = str(uuid.uuid4())
    run, created = app.state.store.create_remote_run(
        {
            "id": run_id,
            "source_kind": "workspace",
            "source_workspace_id": str(source_workspace["id"]),
            "source_path": "models",
            "source_label": "training/models",
            "source_url": None,
            "source_options_json": '{"policy":1}',
            "source_revision": None,
            "source_size": None,
            "target_computer_id": str(computer["id"]),
            "command": command,
            "run_base": "/home/runner/.cache/termroom/runs",
            "workspace_id": None,
            "state": "running",
            "phase": None,
            "created_at": "2026-08-13T00:00:00+00:00",
        }
    )
    assert created is True
    assert app.state.store.transition_remote_run(
        run_id,
        expected_states={"running"},
        state="finished",
        exit_code=7,
        ended_at="2026-08-13T00:00:07+00:00",
    )
    run = app.state.store.get_remote_run(run_id)
    assert run is not None
    run_workspace = app.state.workspaces.open_remote_run(
        run,
        f"termroom-run-{run_id}",
        f"/home/runner/.cache/termroom/runs/{run_id}/work",
    )
    terminal = app.state.store.create_terminal(
        str(run_workspace["id"]),
        "run",
        "@run",
        role="remote_run",
        managed_run_id=run_id,
    )
    monkeypatch.setattr(app.state.ssh, "ensure_workspace", lambda _workspace: [terminal])

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _login(client)
        run_page = await client.get(f"/w/{run_workspace['id']}/terminal")
        source = await client.get(f"/remote-runs/{run_id}/source", follow_redirects=False)
        retry = await client.get("/remote-runs/new", params={"retry_run_id": run_id})

    assert run_page.status_code == 200
    assert f'href="/remote-runs/{run_id}/source"' in run_page.text
    assert f'href="/remote-runs/new?retry_run_id={run_id}"' in run_page.text
    assert "원본 열기" in run_page.text
    assert "같은 설정으로 다시 실행" in run_page.text

    assert source.status_code == 302
    assert source.headers["location"] == (
        f"/w/{source_workspace['id']}/files?path=models"
    )

    assert retry.status_code == 200
    assert list(retry.url.params.keys()) == ["retry_run_id"]
    assert retry.url.params["retry_run_id"] == run_id
    assert command not in str(retry.url)
    assert (
        f'<input type="hidden" name="source_workspace_id" value="{source_workspace["id"]}">'
        in retry.text
    )
    assert '<input type="hidden" name="source_path" value="models">' in retry.text
    assert f'<option value="{computer["id"]}" selected>' in retry.text
    assert command in retry.text
    assert "이전 실행 설정을 복원했습니다" in retry.text
