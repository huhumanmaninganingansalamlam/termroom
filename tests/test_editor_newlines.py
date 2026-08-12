from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from termroom.app import create_app
from termroom.config import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("original", "submitted", "style", "expected"),
    [
        (b"before\n", "after\r\nline\r\n", "lf", b"after\nline\n"),
        (b"before\r\n", "after\r\nline\r\n", "crlf", b"after\r\nline\r\n"),
        (b"", "after\r\nline\r\n", "lf", b"after\nline\n"),
    ],
)
async def test_editor_save_restores_original_newline_style(
    tmp_path: Path,
    original: bytes,
    submitted: str,
    style: str,
    expected: bytes,
) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    target = project / "note.txt"
    target.write_bytes(original)
    settings = Settings.create(root, state_dir=tmp_path / "state", access_token="test-token")
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    snapshot = app.state.files.read_text(project, "note.txt")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/login", data={"password": "test-token"}, follow_redirects=False
        )
        assert login.status_code == 303
        response = await client.post(
            f"/w/{workspace['id']}/edit/note.txt",
            data={
                "_csrf": settings.csrf_token,
                "digest": snapshot.digest,
                "mtime_ns": str(snapshot.mtime_ns),
                "newline": style,
                "content": submitted,
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert target.read_bytes() == expected


@pytest.mark.asyncio
async def test_editor_renders_detected_newline_style(tmp_path: Path) -> None:
    root = tmp_path / "root"
    project = root / "project"
    project.mkdir(parents=True)
    (project / "windows.txt").write_bytes(b"one\r\ntwo\r\n")
    settings = Settings.create(root, state_dir=tmp_path / "state", access_token="test-token")
    app = create_app(settings)
    workspace = app.state.workspaces.open("project")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await client.post("/login", data={"password": "test-token"})
        response = await client.get(f"/w/{workspace['id']}/edit/windows.txt")

    assert response.status_code == 200
    assert 'name="newline" value="crlf"' in response.text
