from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

from termroom.app import create_app
from termroom.config import Settings
from termroom.i18n import localize_exception
from termroom.ssh_backend import SSHBackendError

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / "termroom" / "locales"


def _locale(name: str) -> dict[str, str]:
    value = json.loads((LOCALES / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return {str(key): str(text) for key, text in value.items()}


def test_korean_and_english_locale_keys_match() -> None:
    assert set(_locale("ko")) == set(_locale("en"))


def test_workspace_kind_labels_are_backend_specific() -> None:
    assert _locale("en")["workspace.kind.local"] == "Local Workspace"
    assert _locale("en")["workspace.kind.ssh"] == "SSH Workspace"
    assert _locale("ko")["workspace.kind.local"] == "로컬 작업공간"
    assert _locale("ko")["workspace.kind.ssh"] == "SSH 작업공간"


def test_ui_strings_live_in_locale_files() -> None:
    hangul = re.compile(r"[가-힣]")
    sources = (
        list((ROOT / "termroom").glob("*.py"))
        + list((ROOT / "termroom" / "templates").glob("*.html"))
        + [
            ROOT / "termroom" / "static" / "app.js",
            ROOT / "termroom" / "static" / "terminal.js",
        ]
    )
    offenders = [str(path.relative_to(ROOT)) for path in sources if hangul.search(path.read_text())]
    assert offenders == []


def test_literal_translation_keys_exist() -> None:
    known = set(_locale("ko"))
    used: set[str] = set()
    for path in (ROOT / "termroom" / "templates").glob("*.html"):
        used.update(re.findall(r"\bt\(['\"]([^'\"]+)['\"]", path.read_text()))
    for path in (
        ROOT / "termroom" / "static" / "app.js",
        ROOT / "termroom" / "static" / "terminal.js",
    ):
        used.update(re.findall(r"\btr\(['\"]([^'\"]+)['\"]", path.read_text()))
    assert used - known == set()


def test_missing_remote_git_error_is_localized() -> None:
    error = SSHBackendError("git is not installed on the remote computer")

    assert "Git is not installed" in localize_exception("en", error)
    assert "Git이 설치되어 있지 않습니다" in localize_exception("ko", error)


@pytest.mark.asyncio
async def test_english_is_default_until_locale_cookie_switches_ui(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "config",
        access_token="internal-secret",
        login_password="termroom-password",
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open(".")
    app.state.terminals.ensure_workspace(workspace)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Accept-Language": "ko-KR,ko;q=0.9"},
    ) as client:
        login = await client.get("/")
        assert login.status_code == 401
        assert '<html lang="en">' in login.text
        assert "Enter with your password" in login.text
        assert "Local only" in login.text

        signed_in = await client.post(
            "/login",
            data={"password": "termroom-password"},
            follow_redirects=True,
        )
        assert signed_in.status_code == 200
        assert "Where do you want to continue?" in signed_in.text
        assert "Session alive" in signed_in.text

        switched = await client.get("/locale/ko?next=/", follow_redirects=True)
        assert switched.status_code == 200
        assert client.cookies.get("termroom_locale") == "ko"
        assert '<html lang="ko">' in switched.text
        assert "어디서 이어서 작업할까요?" in switched.text


@pytest.mark.asyncio
async def test_configured_korean_is_initial_ui_until_cookie_overrides_it(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "config",
        access_token="internal-secret",
        login_password="termroom-password",
        default_locale="ko",
    )
    transport = httpx.ASGITransport(app=create_app(settings), raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        initial = await client.get("/")
        assert initial.status_code == 401
        assert '<html lang="ko">' in initial.text
        assert "비밀번호로 들어가기" in initial.text

        switched = await client.get("/locale/en?next=/", follow_redirects=True)
        assert client.cookies.get("termroom_locale") == "en"
        assert '<html lang="en">' in switched.text


@pytest.mark.asyncio
async def test_locale_redirect_rejects_backslash_external_target(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "config",
        access_token="internal-secret",
        login_password="termroom-password",
    )
    transport = httpx.ASGITransport(app=create_app(settings), raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/locale/en",
            params={"next": r"/\evil.example/path"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/"
