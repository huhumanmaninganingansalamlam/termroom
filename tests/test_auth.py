from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from termroom.app import create_app
from termroom.auth import SESSION_MAX_AGE_SECONDS
from termroom.config import Settings, default_config_dir


@pytest.mark.asyncio
async def test_password_login_creates_stateless_browser_session(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="internal-secret",
        login_password="correct-password",
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        wrong = await client.post(
            "/login", data={"password": "wrong-password"}, follow_redirects=False
        )
        assert wrong.status_code == 401
        assert not client.cookies.get("termroom_session")

        login = await client.post(
            "/login", data={"password": "correct-password"}, follow_redirects=False
        )
        assert login.status_code == 303
        token = client.cookies.get("termroom_session")
        assert token
        assert "." in token

        home = await client.get("/")
        assert home.status_code == 200
        assert "Device connections" not in home.text
        assert "Sign out" in home.text


@pytest.mark.asyncio
async def test_each_browser_gets_a_different_signed_session(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="internal-secret",
        login_password="correct-password",
    )
    transport = httpx.ASGITransport(app=create_app(settings), raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as first:
        await first.post("/login", data={"password": "correct-password"})
        first_token = first.cookies.get("termroom_session")
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as second:
        await second.post("/login", data={"password": "correct-password"})
        second_token = second.cookies.get("termroom_session")

    assert first_token
    assert second_token
    assert first_token != second_token


@pytest.mark.asyncio
async def test_successful_login_does_not_consume_failure_rate_limit(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="internal-secret",
        login_password="correct-password",
    )
    transport = httpx.ASGITransport(app=create_app(settings), raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for _ in range(12):
            response = await client.post(
                "/login",
                data={"password": "correct-password"},
                follow_redirects=False,
            )
            assert response.status_code == 303


@pytest.mark.asyncio
async def test_login_rate_limit_has_distinct_user_feedback(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="internal-secret",
        login_password="correct-password",
    )
    transport = httpx.ASGITransport(app=create_app(settings), raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        for _ in range(10):
            response = await client.post(
                "/login",
                data={"password": "wrong-password"},
                follow_redirects=False,
            )
            assert response.status_code == 401

        limited = await client.post(
            "/login",
            data={"password": "correct-password"},
            follow_redirects=False,
        )
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "60"
        assert "Too many login attempts" in limited.text


@pytest.mark.asyncio
async def test_logout_clears_session_cookie(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="internal-secret",
        login_password="correct-password",
    )
    transport = httpx.ASGITransport(app=create_app(settings), raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await client.post("/login", data={"password": "correct-password"})
        response = await client.post(
            "/logout",
            data={"_csrf": settings.csrf_token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert not client.cookies.get("termroom_session")


@pytest.mark.asyncio
async def test_password_change_invalidates_existing_browser_sessions(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state = tmp_path / "state"
    first_settings = Settings.create(
        root,
        state_dir=state,
        access_token="internal-secret",
        login_password="first-password",
    )
    first_transport = httpx.ASGITransport(
        app=create_app(first_settings), raise_app_exceptions=False
    )

    async with httpx.AsyncClient(
        transport=first_transport, base_url="http://testserver"
    ) as client:
        await client.post("/login", data={"password": "first-password"})
        old_token = client.cookies.get("termroom_session")

    second_settings = Settings.create(
        root,
        state_dir=state,
        access_token="internal-secret",
        login_password="second-password",
    )
    second_transport = httpx.ASGITransport(
        app=create_app(second_settings), raise_app_exceptions=False
    )
    async with httpx.AsyncClient(
        transport=second_transport, base_url="http://testserver"
    ) as client:
        client.cookies.set("termroom_session", old_token)
        response = await client.get("/")
        assert response.status_code == 401


def test_settings_loads_termroom_password_from_project_dotenv(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    dotenv = root / ".env"
    dotenv.write_text("TERMROOM_PASSWORD='dotenv-password'\n", encoding="utf-8")
    dotenv.chmod(0o600)

    settings = Settings.create(root, state_dir=tmp_path / "state")

    assert settings.login_password == "dotenv-password"


def test_settings_prefers_config_dotenv_over_project_dotenv(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    project_dotenv = root / ".env"
    project_dotenv.write_text("TERMROOM_PASSWORD=project-password\n", encoding="utf-8")
    project_dotenv.chmod(0o600)
    state = tmp_path / "state"
    state.mkdir()
    config_dotenv = state / ".env"
    config_dotenv.write_text("TERMROOM_PASSWORD=config-password\n", encoding="utf-8")
    config_dotenv.chmod(0o600)

    settings = Settings.create(root, state_dir=state)

    assert settings.login_password == "config-password"


def test_settings_environment_password_overrides_config_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    config_dotenv = state / ".env"
    config_dotenv.write_text("TERMROOM_PASSWORD=config-password\n", encoding="utf-8")
    config_dotenv.chmod(0o600)
    monkeypatch.setenv("TERMROOM_PASSWORD", "environment-password")

    settings = Settings.create(root, state_dir=state)

    assert settings.login_password == "environment-password"


def test_settings_loads_default_locale_from_config_dotenv(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / ".env").write_text("TERMROOM_LOCALE=ko\n", encoding="utf-8")

    settings = Settings.create(
        root,
        state_dir=state,
        access_token="internal-secret",
        login_password="password",
    )

    assert settings.default_locale == "ko"


def test_settings_environment_locale_overrides_config_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / ".env").write_text("TERMROOM_LOCALE=ko\n", encoding="utf-8")
    monkeypatch.setenv("TERMROOM_LOCALE", "en")

    settings = Settings.create(
        root,
        state_dir=state,
        access_token="internal-secret",
        login_password="password",
    )

    assert settings.default_locale == "en"


def test_settings_rejects_unknown_default_locale(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(ValueError, match="TERMROOM_LOCALE"):
        Settings.create(
            root,
            state_dir=tmp_path / "state",
            access_token="internal-secret",
            login_password="password",
            default_locale="fr",
        )


def test_settings_rejects_world_readable_config_password_file(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    config_dotenv = state / ".env"
    config_dotenv.write_text("TERMROOM_PASSWORD=config-password\n", encoding="utf-8")
    config_dotenv.chmod(0o644)

    with pytest.raises(ValueError, match="chmod 600"):
        Settings.create(root, state_dir=state)


def test_default_config_dir_uses_termroom_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.delenv("TERMROOM_CONFIG_DIR", raising=False)
    assert default_config_dir() == config_home / "termroom"


def test_settings_database_path_is_termroom_database(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state = tmp_path / "state"
    settings = Settings.create(root, state_dir=state, access_token="internal-secret")
    assert settings.database_path == state / "termroom.sqlite3"


def test_settings_accepts_short_operator_password_by_default(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="internal-secret",
        login_password="1234",
    )

    assert settings.login_password == "1234"


def test_settings_respects_optional_minimum_password_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("TERMROOM_MIN_PASSWORD_LENGTH", "6")

    with pytest.raises(ValueError, match="TERMROOM_MIN_PASSWORD_LENGTH"):
        Settings.create(
            root,
            state_dir=tmp_path / "state-short",
            access_token="internal-secret",
            login_password="1234",
        )

    settings = Settings.create(
        root,
        state_dir=tmp_path / "state-ok",
        access_token="internal-secret",
        login_password="123456",
    )
    assert settings.login_password == "123456"


def test_settings_rejects_world_readable_termroom_password_file(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    dotenv = root / ".env"
    dotenv.write_text("TERMROOM_PASSWORD=dotenv-password\n", encoding="utf-8")
    dotenv.chmod(0o644)

    with pytest.raises(ValueError, match="chmod 600"):
        Settings.create(root, state_dir=tmp_path / "state")


@pytest.mark.asyncio
async def test_signed_browser_session_expires_server_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="internal-secret",
        login_password="correct-password",
    )
    app = create_app(settings)
    issued_at = 1_800_000_000
    monkeypatch.setattr("termroom.auth._session_now", lambda: issued_at)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/login",
            data={"password": "correct-password"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        assert (await client.get("/")).status_code == 200

        monkeypatch.setattr(
            "termroom.auth._session_now",
            lambda: issued_at + SESSION_MAX_AGE_SECONDS + 1,
        )
        assert (await client.get("/")).status_code == 401
