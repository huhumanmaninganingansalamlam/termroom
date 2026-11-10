from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest

from termroom.app import create_app
from termroom.auth import SESSION_MAX_AGE_SECONDS
from termroom.config import Settings, default_config_dir


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("password", "wrong_password"),
    [
        ("correct-password", "wrong-password"),
        ("correct-password", "\uD55C\uAE00-\uC624\uC785\uB825"),
        ("\uD55C\uAE00-\uBE44\uBC00\uBC88\uD638\U0001f510", "wrong-password"),
        ("caf\u00e9-\U0001f510", "cafe\u0301-\U0001f510"),
    ],
)
async def test_password_login_creates_authenticated_browser_session(
    tmp_path: Path, password: str, wrong_password: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="internal-secret",
        login_password=password,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        wrong = await client.post(
            "/login", data={"password": wrong_password}, follow_redirects=False
        )
        assert wrong.status_code == 401
        assert not client.cookies.get("termroom_session")

        login = await client.post(
            "/login", data={"password": password}, follow_redirects=False
        )
        assert login.status_code == 303
        token = client.cookies.get("termroom_session")
        assert token

        home = await client.get("/")
        assert home.status_code == 200


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
async def test_login_rate_limit_returns_retry_contract(tmp_path: Path) -> None:
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


def test_settings_loads_local_workspace_policy_from_dotenv_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / ".env").write_text(
        "TERMROOM_ALLOW_LOCAL_WORKSPACES=false\n", encoding="utf-8"
    )

    from_dotenv = Settings.create(
        root,
        state_dir=state,
        access_token="internal-secret",
        login_password="password",
    )
    monkeypatch.setenv("TERMROOM_ALLOW_LOCAL_WORKSPACES", "true")
    from_environment = Settings.create(
        root,
        state_dir=state,
        access_token="internal-secret",
        login_password="password",
    )

    assert from_dotenv.allow_local_workspaces is False
    assert from_environment.allow_local_workspaces is True


def test_settings_rejects_invalid_local_workspace_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("TERMROOM_ALLOW_LOCAL_WORKSPACES", "sometimes")

    with pytest.raises(ValueError, match="TERMROOM_ALLOW_LOCAL_WORKSPACES"):
        Settings.create(
            root,
            state_dir=tmp_path / "state",
            access_token="internal-secret",
            login_password="password",
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


def test_settings_repairs_existing_access_token_permissions(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    token_path = state / "access-token"
    token_path.write_text("existing-token\n", encoding="utf-8")
    token_path.chmod(0o644)

    settings = Settings.create(root, state_dir=state, login_password="local-password")

    assert settings.access_token == "existing-token"
    assert token_path.stat().st_mode & 0o777 == 0o600


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


def _control_test_app(tmp_path: Path):
    root = tmp_path / "root"
    (root / "one").mkdir(parents=True)
    (root / "two").mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="internal-secret",
        login_password="correct-password",
    )
    app = create_app(settings)
    one = app.state.workspaces.open("one")
    two = app.state.workspaces.open("two")
    return settings, app, one, two


@pytest.mark.asyncio
async def test_control_client_bearer_is_scoped_revocable_and_not_a_browser_session(
    tmp_path: Path,
) -> None:
    settings, app, one, two = _control_test_app(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as browser,
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as agent,
    ):
        assert (await agent.get("/api/control/v1/capabilities")).status_code == 401
        await browser.post("/login", data={"password": "correct-password"})
        assert (await browser.get("/api/control/v1/capabilities")).status_code == 401
        denied = await browser.post(
            "/api/client-credentials",
            json={"name": "reader", "workspace_ids": [one["id"]]},
        )
        assert denied.status_code == 403

        created = await browser.post(
            "/api/client-credentials",
            json={"name": "reader", "workspace_ids": [one["id"]]},
            headers={"X-Termroom-CSRF": settings.csrf_token},
        )
        assert created.status_code == 201, created.text
        credential = created.json()["client"]
        token = credential["token"]
        assert credential["capabilities"] == ["read"]
        saved = app.state.store.get_client_credential(credential["id"])
        assert saved["token_hash"] != token
        assert len(saved["token_hash"]) == 64
        listed = await browser.get("/api/client-credentials")
        assert listed.status_code == 200
        assert token not in listed.text
        assert "token_hash" not in listed.text
        assert listed.json()["clients"][0]["last_used_at"] is None

        headers = {"Authorization": f"Bearer {token}"}
        capabilities = await agent.get("/api/control/v1/capabilities", headers=headers)
        assert capabilities.status_code == 200
        assert capabilities.json()["client"]["workspace_ids"] == [one["id"]]
        observed = await agent.get(
            f"/api/control/v1/workspaces/{one['id']}", headers=headers
        )
        assert observed.status_code == 200
        assert observed.json()["workspace"]["display_name"] == "one"
        assert "root_path" not in observed.text
        assert "path" not in observed.json()["workspace"]
        denied_scope = await agent.get(
            f"/api/control/v1/workspaces/{two['id']}", headers=headers
        )
        missing = await agent.get(
            "/api/control/v1/workspaces/does-not-exist", headers=headers
        )
        assert denied_scope.status_code == missing.status_code == 404
        assert denied_scope.json() == missing.json()
        assert app.state.store.get_client_credential(credential["id"])["last_used_at"]
        assert (
            await agent.get("/api/control/v1/capabilities", headers={"Authorization": "Bearer bad"})
        ).status_code == 401
        assert (await agent.get("/", headers=headers)).status_code == 401

        not_confirmed = await browser.post(
            f"/api/client-credentials/{credential['id']}/revoke",
            json={"confirm_client_id": "another"},
            headers={"X-Termroom-CSRF": settings.csrf_token},
        )
        assert not_confirmed.status_code == 403
        revoked = await browser.post(
            f"/api/client-credentials/{credential['id']}/revoke",
            json={"confirm_client_id": credential["id"]},
            headers={"X-Termroom-CSRF": settings.csrf_token},
        )
        assert revoked.status_code == 200
        assert revoked.json()["client"]["revoked_at"]
        assert (await agent.get("/api/control/v1/capabilities", headers=headers)).status_code == 401
        assert (await browser.get("/")).status_code == 200


@pytest.mark.asyncio
async def test_control_mutation_rechecks_revocation_after_streamed_body(
    tmp_path: Path,
) -> None:
    settings, app, one, _ = _control_test_app(tmp_path)
    issued = app.state.control_credentials.issue(
        name="operator", capabilities=["read", "operate"], workspace_ids=[one["id"]],
    )
    headers = {"Authorization": f"Bearer {issued['token']}"}
    url = f"/api/control/v1/workspaces/{one['id']}/commands"
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    body_requested = asyncio.Event()
    release_body = asyncio.Event()

    async with (
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as browser,
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as agent,
    ):
        await browser.post("/login", data={"password": "correct-password"})
        listed = await agent.get(url, headers=headers)
        assert listed.status_code == 200, listed.text
        payload = (
            '{"action_id":"body-revoke-0001","expected_revision":"'
            + listed.json()["revision"]
            + '","name":"must-not-exist","command":"printf blocked"}'
        ).encode()

        async def delayed_body():  # type: ignore[no-untyped-def]
            body_requested.set()
            await release_body.wait()
            yield payload

        pending = asyncio.create_task(agent.post(
            url,
            content=delayed_body(),
            headers={**headers, "Content-Type": "application/json"},
        ))
        try:
            await asyncio.wait_for(body_requested.wait(), 5)
            revoked = await browser.post(
                f"/api/client-credentials/{issued['id']}/revoke",
                json={"confirm_client_id": issued["id"]},
                headers={"X-Termroom-CSRF": settings.csrf_token},
            )
            assert revoked.status_code == 200, revoked.text
        finally:
            release_body.set()

        refused = await asyncio.wait_for(pending, 5)
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "credential_revoked"
        assert app.state.store.list_workspace_quick_commands(one["id"]) == ()
        assert app.state.store.get_client_action(
            issued["id"], "body-revoke-0001",
            instance_id=app.state.control_instance_id,
        ) is None


@pytest.mark.asyncio
async def test_control_client_validates_grants_transport_and_request_bounds(
    tmp_path: Path,
) -> None:
    settings, app, one, _ = _control_test_app(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    auth_headers = {"X-Termroom-CSRF": settings.csrf_token}
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver"
    ) as browser:
        await browser.post("/login", data={"password": "correct-password"})
        invalid = (
            {"name": "", "workspace_ids": [one["id"]]},
            {"name": "reader", "workspace_ids": ["unknown"]},
            {"name": "reader", "workspace_ids": [one["id"], one["id"]]},
            {"name": "reader", "workspace_ids": [one["id"]], "capabilities": ["operate"]},
            {"name": "reader", "workspace_ids": [one["id"]], "capabilities": ["read", "admin"]},
            {"name": "reader", "workspace_ids": [one["id"]], "unexpected": True},
        )
        for payload in invalid:
            response = await browser.post(
                "/api/client-credentials", json=payload, headers=auth_headers
            )
            assert response.status_code == 400, payload
        oversize = await browser.post(
            "/api/client-credentials",
            content=b"{" + b" " * 8200 + b"}",
            headers={**auth_headers, "Content-Type": "application/json"},
        )
        assert oversize.status_code == 413
        explicit = await browser.post(
            "/api/client-credentials",
            json={
                "name": "operator",
                "workspace_ids": [one["id"]],
                "capabilities": ["read", "operate"],
            },
            headers=auth_headers,
        )
        assert explicit.status_code == 201
        assert explicit.json()["client"]["capabilities"] == ["operate", "read"]

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as untrusted:
        await untrusted.post("/login", data={"password": "correct-password"})
        assert (await untrusted.get("/api/client-credentials")).status_code == 426
        assert (
            await untrusted.get(
                "/api/control/v1/capabilities",
                headers={"Authorization": f"Bearer {explicit.json()['client']['token']}"},
            )
        ).status_code == 426


@pytest.mark.asyncio
async def test_control_action_identity_preserves_unknown_after_restart(
    tmp_path: Path,
) -> None:
    _, app, one, _ = _control_test_app(tmp_path)
    issued = app.state.control_credentials.issue(
        name="runner", capabilities=["read", "operate"], workspace_ids=[one["id"]]
    )
    store = app.state.store
    kwargs = {
        "client_id": issued["id"],
        "action_id": "request-00001",
        "action": "terminal.submit",
        "resource_kind": "workspace",
        "resource_id": one["id"],
        "payload_sha256": hashlib.sha256(b"synthetic-only").hexdigest(),
    }
    first, created = store.claim_client_action(
        **kwargs, instance_id=app.state.control_instance_id
    )
    assert created and first["status"] == "accepted"
    repeated, created = store.claim_client_action(
        **kwargs, instance_id=app.state.control_instance_id
    )
    assert not created and repeated["action_id"] == first["action_id"]
    with pytest.raises(ValueError, match="idempotency_conflict"):
        store.claim_client_action(
            **{**kwargs, "payload_sha256": hashlib.sha256(b"different").hexdigest()},
            instance_id=app.state.control_instance_id,
        )
    stage = store.update_client_action(
        client_id=issued["id"], action_id=kwargs["action_id"],
        instance_id=app.state.control_instance_id,
        status="accepted", phase="pty_delivered",
    )
    assert stage["phase"] == "pty_delivered" and stage["status"] == "accepted"
    with pytest.raises(ValueError, match="backwards"):
        store.update_client_action(
            client_id=issued["id"], action_id=kwargs["action_id"],
            instance_id=app.state.control_instance_id,
            status="accepted", phase="accepted",
        )
    with pytest.raises(ValueError, match="transport receipt"):
        store.update_client_action(
            client_id=issued["id"], action_id=kwargs["action_id"],
            instance_id=app.state.control_instance_id,
            status="observed", phase="pty_delivered",
        )
    assert store.get_client_action(
        issued["id"], kwargs["action_id"], instance_id=app.state.control_instance_id
    )["status"] == "accepted"
    recovered = store.get_client_action(
        issued["id"], kwargs["action_id"], instance_id="new-core-process"
    )
    assert recovered["status"] == "unknown"
    assert recovered["phase"] == "pty_delivered"
    still_unknown, created = store.claim_client_action(
        **kwargs, instance_id="new-core-process"
    )
    assert not created and still_unknown["status"] == "unknown"
    assert store.get_client_action("another-client", kwargs["action_id"], instance_id="new") is None

    complete_kwargs = {**kwargs, "action_id": "request-00002"}
    completed, created = store.claim_client_action(
        **complete_kwargs, instance_id=app.state.control_instance_id
    )
    assert created and completed["status"] == "accepted"
    completed = store.update_client_action(
        client_id=issued["id"], action_id=complete_kwargs["action_id"],
        instance_id=app.state.control_instance_id,
        status="observed", phase="process_finished",
    )
    assert completed["status"] == "observed"
    with pytest.raises(ValueError, match="erase observed completion"):
        store.update_client_action(
            client_id=issued["id"], action_id=complete_kwargs["action_id"],
            instance_id=app.state.control_instance_id,
            status="accepted", phase="process_finished",
        )

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver"
    ) as agent:
        response = await agent.get(
            f"/api/control/v1/actions/{kwargs['action_id']}",
            headers={"Authorization": f"Bearer {issued['token']}"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "unknown"
        assert response.json()["phase"] == "pty_delivered"
        assert kwargs["payload_sha256"] not in response.text


@pytest.mark.asyncio
async def test_control_workspace_identity_uses_browser_workspace_owner_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, app, one, _ = _control_test_app(tmp_path)
    issued = app.state.control_credentials.issue(
        name="reader", capabilities=["read"], workspace_ids=[one["id"]]
    )
    store = app.state.store
    before = store.get_workspace(one["id"])["last_opened_at"]
    calls: list[str] = []
    original = app.state.workspaces.require

    def observed_require(workspace_id: str):
        calls.append(workspace_id)
        return original(workspace_id)

    monkeypatch.setattr(app.state.workspaces, "require", observed_require)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as browser,
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as agent,
    ):
        await browser.post("/login", data={"password": "correct-password"})
        browser_view = await browser.get(f"/w/{one['id']}", follow_redirects=False)
        api_view = await agent.get(
            f"/api/control/v1/workspaces/{one['id']}",
            headers={"Authorization": f"Bearer {issued['token']}"},
        )
        assert browser_view.status_code == 303
        assert api_view.status_code == 200
    assert calls == [one["id"], one["id"]]
    assert store.get_workspace(one["id"])["last_opened_at"] == before
    assert store.list_terminals(one["id"]) == []


@pytest.mark.asyncio
async def test_control_client_rejects_forged_https_from_untrusted_proxy_peer(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "one").mkdir()
    settings = Settings.create(
        root, state_dir=tmp_path / "state", login_password="correct-password",
        access_token="local-test-only", secure_cookie=True,
    )
    app = create_app(settings)
    workspace = app.state.workspaces.open("one")
    issued = app.state.control_credentials.issue(
        name="reader", capabilities=["read"], workspace_ids=[workspace["id"]],
    )
    headers = {
        "Authorization": f"Bearer {issued['token']}",
        "X-Forwarded-Proto": "https",
    }
    remote_transport = httpx.ASGITransport(
        app=app, client=("192.0.2.9", 41000), raise_app_exceptions=False,
    )
    async with httpx.AsyncClient(
        transport=remote_transport, base_url="http://public.example",
    ) as remote:
        forged = await remote.get("/api/control/v1/capabilities", headers=headers)
        assert forged.status_code == 426
        assert forged.json()["code"] == "secure_transport_required"

    loopback_transport = httpx.ASGITransport(
        app=app, client=("127.0.0.1", 41000), raise_app_exceptions=False,
    )
    async with httpx.AsyncClient(
        transport=loopback_transport, base_url="http://public.example",
    ) as trusted_proxy:
        trusted = await trusted_proxy.get("/api/control/v1/capabilities", headers=headers)
        assert trusted.status_code == 200


@pytest.mark.asyncio
async def test_control_quick_command_crud_requires_operate_and_is_revision_safe(
    tmp_path: Path,
) -> None:
    _, app, one, two = _control_test_app(tmp_path)
    store = app.state.store
    read = app.state.control_credentials.issue(
        name="read-only", capabilities=["read"], workspace_ids=[one["id"]],
    )
    operate = app.state.control_credentials.issue(
        name="operator", capabilities=["read", "operate"], workspace_ids=[one["id"]],
    )
    elsewhere = app.state.control_credentials.issue(
        name="elsewhere", capabilities=["read", "operate"], workspace_ids=[two["id"]],
    )
    read_headers = {"Authorization": f"Bearer {read['token']}"}
    write_headers = {"Authorization": f"Bearer {operate['token']}"}
    elsewhere_headers = {"Authorization": f"Bearer {elsewhere['token']}"}
    base = f"/api/control/v1/workspaces/{one['id']}/commands"
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver",
    ) as agent:
        listed = await agent.get(base, headers=read_headers)
        assert listed.status_code == 200 and listed.json()["commands"] == []
        first_revision = listed.json()["revision"]
        payload = {
            "action_id": "quick-create-0001", "expected_revision": first_revision,
            "name": "Run checks", "command": "printf checked",
        }
        for restricted_headers in (read_headers, elsewhere_headers):
            response = await agent.post(base, json=payload, headers=restricted_headers)
            assert response.status_code == 404
        assert (await agent.post(base, json=payload)).status_code == 401
        assert store.list_workspace_quick_commands(one["id"]) == ()

        invalid = await agent.post(
            base, headers=write_headers,
            json={**payload, "name": "Invalid\nname"},
        )
        assert invalid.status_code == 400
        created = await agent.post(base, json=payload, headers=write_headers)
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "observed"
        assert created.json()["phase"] == "result_verified"
        command_id = created.json()["result_resource_id"]
        assert len(command_id) == 32
        receipt = await agent.get(
            "/api/control/v1/actions/quick-create-0001", headers=write_headers,
        )
        assert receipt.status_code == 200 and receipt.json()["status"] == "observed"
        assert (
            await agent.get(
                "/api/control/v1/actions/quick-create-0001", headers=read_headers,
            )
        ).status_code == 404

        replay = await agent.post(base, json=payload, headers=write_headers)
        assert replay.status_code == 200
        assert replay.json()["result_resource_id"] == command_id
        assert len(store.list_workspace_quick_commands(one["id"])) == 1
        collision = await agent.post(
            base, headers=write_headers, json={**payload, "name": "Not the same"},
        )
        assert collision.status_code == 409
        assert collision.json()["code"] == "idempotency_conflict"
        stale_create = await agent.post(
            base, headers=write_headers,
            json={**payload, "action_id": "quick-create-stale"},
        )
        assert stale_create.status_code == 409
        assert stale_create.json()["code"] == "revision_conflict"
        assert (await agent.post(
            base, headers=write_headers,
            json={**payload, "action_id": "quick-create-stale"},
        )).status_code == 409

        rows = (await agent.get(base, headers=write_headers)).json()
        assert rows["identity_kind"] == "stable_id"
        assert rows["commands"] == [{
            "id": command_id, "name": "Run checks",
            "command": "printf checked", "revision": 1, "legacy_slot": 0,
        }]
        assert rows["revision"] != first_revision
        update_url = f"{base}/{command_id}"
        update_payload = {
            "action_id": "quick-update-0001", "name": "Check again",
            "command": "printf checked", "expected_revision": 1,
        }
        renamed = await agent.patch(
            update_url, headers=write_headers, json=update_payload,
        )
        assert renamed.status_code == 200, renamed.text
        renamed_record = store.list_workspace_quick_commands(one["id"])[0]
        assert renamed_record["id"] == command_id
        assert renamed_record["revision"] == 2
        assert renamed_record["command"] == "printf checked"

        stale_update = await agent.patch(
            update_url, headers=write_headers,
            json={**update_payload, "action_id": "quick-update-stale", "name": "No"},
        )
        assert stale_update.status_code == 409
        assert stale_update.json()["code"] == "revision_conflict"
        assert store.list_workspace_quick_commands(one["id"])[0]["name"] == "Check again"
        invalid_revision = await agent.patch(
            update_url, headers=write_headers,
            json={**update_payload, "action_id": "quick-update-bool",
                  "expected_revision": True},
        )
        assert invalid_revision.status_code == 400

        revision_now = (await agent.get(base, headers=write_headers)).json()["revision"]
        second = await agent.post(
            base, headers=write_headers,
            json={"action_id": "quick-create-0002", "expected_revision": revision_now,
                  "name": "Check again", "command": "printf second"},
        )
        assert second.status_code == 201
        second_id = second.json()["result_resource_id"]
        assert second_id != command_id
        assert [r["name"] for r in store.list_workspace_quick_commands(one["id"])] == [
            "Check again", "Check again",
        ]

        denied_delete = await agent.request(
            "DELETE", update_url, headers=read_headers,
            json={"action_id": "quick-delete-deny", "expected_revision": 2},
        )
        assert denied_delete.status_code == 404
        removed = await agent.request(
            "DELETE", update_url, headers=write_headers,
            json={"action_id": "quick-delete-0001", "expected_revision": 2},
        )
        assert removed.status_code == 200
        assert removed.json()["result_resource_id"] == command_id
        remaining = store.list_workspace_quick_commands(one["id"])
        assert [row["id"] for row in remaining] == [second_id]
        assert [row["command"] for row in remaining] == ["printf second"]
        late = await agent.patch(
            update_url, headers=write_headers,
            json={**update_payload, "action_id": "quick-update-deleted",
                  "expected_revision": 2},
        )
        assert late.status_code == 409
        assert late.json()["code"] == "command_not_found"
        assert store.list_workspace_quick_commands(one["id"]) == remaining
        assert store.list_workspace_commands(one["id"]) == ("printf second",)


@pytest.mark.asyncio
async def test_control_file_mutations_preserve_versions_scope_and_browser_session(
    tmp_path: Path,
) -> None:
    settings, app, one, two = _control_test_app(tmp_path)
    one_path = Path(one["path"])
    file_path = one_path / "notes.txt"
    file_path.write_text("human original\n", encoding="utf-8")
    (one_path / "private-link").symlink_to(settings.state_dir, target_is_directory=True)
    read_only = app.state.control_credentials.issue(
        name="read-only", capabilities=["read"], workspace_ids=[one["id"]],
    )
    operator = app.state.control_credentials.issue(
        name="operator", capabilities=["read", "operate"], workspace_ids=[one["id"]],
    )
    elsewhere = app.state.control_credentials.issue(
        name="elsewhere", capabilities=["read", "operate"], workspace_ids=[two["id"]],
    )
    ro_headers = {"Authorization": f"Bearer {read_only['token']}"}
    op_headers = {"Authorization": f"Bearer {operator['token']}"}
    other_headers = {"Authorization": f"Bearer {elsewhere['token']}"}
    base = f"/api/control/v1/workspaces/{one['id']}/files"
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    async with (
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as browser,
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as agent,
    ):
        await browser.post("/login", data={"password": "correct-password"})
        human_cookie = browser.cookies.get("termroom_session")
        human_page = await browser.get(f"/w/{two['id']}/files")
        assert human_page.status_code == 200

        observed = await agent.get(
            f"{base}/text", params={"path": "notes.txt"}, headers=op_headers,
        )
        assert observed.status_code == 200
        version = observed.json()
        write = {
            "action_id": "file-save-0001", "relative_path": "notes.txt",
            "content": "agent edit\n", "expected_digest": version["digest"],
            "expected_mtime_ns": version["mtime_ns"],
        }
        for restricted_headers in (ro_headers, other_headers):
            denied = await agent.put(
                f"{base}/text", json=write, headers=restricted_headers,
            )
            assert denied.status_code == 404
        assert file_path.read_text(encoding="utf-8") == "human original\n"

        invalid = await agent.put(
            f"{base}/text", headers=op_headers,
            json={**write, "relative_path": "../two/outside.txt"},
        )
        assert invalid.status_code == 400
        invalid = await agent.put(
            f"{base}/text", headers=op_headers,
            json={**write, "expected_mtime_ns": True},
        )
        assert invalid.status_code == 400
        assert (await agent.put(
            f"{base}/text", headers=op_headers, json={
                **write, "content": "x" * 131073,
            },
        )).status_code == 413

        saved = await agent.put(f"{base}/text", json=write, headers=op_headers)
        assert saved.status_code == 200, saved.text
        assert saved.json()["status"] == "observed"
        assert saved.json()["phase"] == "result_verified"
        assert file_path.read_text(encoding="utf-8") == "agent edit\n"
        assert saved.json()["version"]["digest"] == hashlib.sha256(b"agent edit\n").hexdigest()
        first_mtime = file_path.stat().st_mtime_ns
        replay = await agent.put(f"{base}/text", json=write, headers=op_headers)
        assert replay.status_code == 200
        assert replay.json()["result_resource_id"] == saved.json()["result_resource_id"]
        assert file_path.stat().st_mtime_ns == first_mtime
        collision = await agent.put(
            f"{base}/text", headers=op_headers, json={**write, "content": "other"},
        )
        assert collision.status_code == 409
        assert collision.json()["code"] == "idempotency_conflict"

        human_version = (await agent.get(
            f"{base}/text", params={"path": "notes.txt"}, headers=op_headers,
        )).json()
        file_path.write_text("human concurrent edit\n", encoding="utf-8")
        stale = await agent.put(
            f"{base}/text", headers=op_headers,
            json={
                **write, "action_id": "file-save-stale",
                "content": "do not overwrite the human edit",
                "expected_digest": human_version["digest"],
                "expected_mtime_ns": human_version["mtime_ns"],
            },
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["code"] == "file_conflict"
        assert stale.json()["current_version"]["digest"] == hashlib.sha256(
            b"human concurrent edit\n",
        ).hexdigest()
        assert "do not overwrite" not in stale.text
        assert file_path.read_text(encoding="utf-8") == "human concurrent edit\n"
        stale_receipt = await agent.get(
            "/api/control/v1/actions/file-save-stale", headers=op_headers,
        )
        assert stale_receipt.status_code == 200
        assert stale_receipt.json()["status"] == "refused"

        create = {"action_id": "file-create-0001", "parent": ".", "name": "fresh.txt"}
        created = await agent.post(f"{base}/create", json=create, headers=op_headers)
        assert created.status_code == 201, created.text
        assert (one_path / "fresh.txt").read_bytes() == b""
        repeated = await agent.post(f"{base}/create", json=create, headers=op_headers)
        assert repeated.status_code == 200
        assert repeated.json()["result_resource_id"] == created.json()["result_resource_id"]
        assert (await agent.post(
            f"{base}/create", headers=op_headers, json={**create, "name": "renamed.txt"},
        )).status_code == 409
        assert not (one_path / "renamed.txt").exists()
        exists = await agent.post(
            f"{base}/create", headers=op_headers,
            json={**create, "action_id": "file-create-exists"},
        )
        assert exists.status_code == 409
        assert exists.json()["code"] == "file_exists"
        forbidden = await agent.post(
            f"{base}/create", headers=op_headers,
            json={
                "action_id": "file-create-private", "parent": "private-link",
                "name": "do-not-create",
            },
        )
        assert forbidden.status_code == 409
        assert forbidden.json()["code"] == "resource_unavailable"
        assert not (settings.state_dir / "do-not-create").exists()
        denied_create = await agent.post(
            f"{base}/create", json=create, headers=ro_headers,
        )
        assert denied_create.status_code == 404
        assert browser.cookies.get("termroom_session") == human_cookie
        after_page = await browser.get(f"/w/{two['id']}/files")
        assert after_page.status_code == 200
        assert app.state.store.list_terminals(two["id"]) == []
        assert app.state.store.list_terminals(one["id"]) == []


@pytest.mark.asyncio
async def test_control_file_write_unknown_outcome_never_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, app, one, _ = _control_test_app(tmp_path)
    file_path = Path(one["path"]) / "uncertain.txt"
    file_path.write_text("before", encoding="utf-8")
    issued = app.state.control_credentials.issue(
        name="operator", capabilities=["read", "operate"], workspace_ids=[one["id"]],
    )
    headers = {"Authorization": f"Bearer {issued['token']}"}
    original = app.state.files.write_text
    calls: list[str] = []

    def saved_but_ack_lost(*args, **kwargs):
        calls.append(str(args[1]))
        original(*args, **kwargs)
        raise OSError("simulated post-save acknowledgement loss")

    monkeypatch.setattr(app.state.files, "write_text", saved_but_ack_lost)
    base = f"/api/control/v1/workspaces/{one['id']}/files/text"
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver",
    ) as agent:
        observed = (await agent.get(
            base, params={"path": "uncertain.txt"}, headers=headers,
        )).json()
        payload = {
            "action_id": "file-unknown-0001", "relative_path": "uncertain.txt",
            "content": "after", "expected_digest": observed["digest"],
            "expected_mtime_ns": observed["mtime_ns"],
        }
        first = await agent.put(base, headers=headers, json=payload)
        assert first.status_code == 409, first.text
        assert first.json()["status"] == "unknown"
        assert first.json()["phase"] == "dispatched"
        assert first.json()["code"] == "unknown_outcome"
        assert file_path.read_text(encoding="utf-8") == "after"
        replay = await agent.put(base, headers=headers, json=payload)
        assert replay.status_code == 409 and replay.json()["status"] == "unknown"
        assert calls == ["uncertain.txt"]
        altered = await agent.put(
            base, headers=headers, json={**payload, "content": "another"},
        )
        assert altered.status_code == 409
        assert altered.json()["code"] == "idempotency_conflict"
        receipt = await agent.get(
            "/api/control/v1/actions/file-unknown-0001", headers=headers,
        )
        assert receipt.status_code == 200
        assert receipt.json()["status"] == "unknown"


@pytest.mark.asyncio
async def test_control_named_command_launch_uses_stable_id_revision_and_one_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, app, one, two = _control_test_app(tmp_path)
    app.state.store.replace_workspace_commands(one["id"], ["printf synthetic-command"])
    row = app.state.store.list_workspace_quick_commands(one["id"])[0]
    read_only = app.state.control_credentials.issue(
        name="observer", capabilities=["read"], workspace_ids=[one["id"]],
    )
    operator = app.state.control_credentials.issue(
        name="operator", capabilities=["read", "operate"], workspace_ids=[one["id"]],
    )
    elsewhere = app.state.control_credentials.issue(
        name="elsewhere", capabilities=["read", "operate"], workspace_ids=[two["id"]],
    )
    op_headers = {"Authorization": f"Bearer {operator['token']}"}
    calls: list[dict] = []

    def simulated_managed_launch(workspace, *, slot, command, launch_id):
        calls.append({
            "workspace_id": workspace["id"], "slot": slot,
            "command": command, "launch_id": launch_id,
        })
        return {"id": "synthetic-terminal-id"}

    monkeypatch.setattr(app.state.terminals, "run_workspace_command", simulated_managed_launch)
    base = (
        f"/api/control/v1/workspaces/{one['id']}/commands/"
        f"{row['id']}/launch"
    )
    launch_id = uuid.uuid4().hex
    payload = {
        "action_id": "quick-launch-0001",
        "launch_id": launch_id, "expected_revision": row["revision"],
    }
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver",
    ) as agent:
        for token in (read_only["token"], elsewhere["token"]):
            denied = await agent.post(
                base, json=payload, headers={"Authorization": f"Bearer {token}"},
            )
            assert denied.status_code == 404
        launched = await agent.post(base, json=payload, headers=op_headers)
        assert launched.status_code == 202, launched.text
        assert launched.json()["status"] == "accepted"
        assert launched.json()["phase"] == "process_started"
        assert launched.json()["result_resource_id"] == launch_id
        assert launched.json()["terminal_id"] == "synthetic-terminal-id"
        assert calls == [{
            "workspace_id": one["id"], "slot": 0,
            "command": "printf synthetic-command", "launch_id": launch_id,
        }]
        receipt = await agent.get(
            "/api/control/v1/actions/quick-launch-0001", headers=op_headers,
        )
        assert receipt.status_code == 200
        assert receipt.json()["phase"] == "process_started"
        repeated = await agent.post(base, json=payload, headers=op_headers)
        assert repeated.status_code == 202
        assert len(calls) == 1
        collision = await agent.post(
            base, json={**payload, "expected_revision": 2}, headers=op_headers,
        )
        assert collision.status_code == 409
        assert collision.json()["code"] == "idempotency_conflict"
        reused_launch = await agent.post(
            base, json={**payload, "action_id": "quick-launch-reused"},
            headers=op_headers,
        )
        assert reused_launch.status_code == 409
        assert reused_launch.json()["code"] == "launch_identity_conflict"
        stale = await agent.post(
            base, json={
                **payload, "action_id": "quick-launch-stale",
                "expected_revision": row["revision"] + 1,
                "launch_id": uuid.uuid4().hex,
            },
            headers=op_headers,
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "revision_conflict"
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_control_file_run_launch_is_scoped_and_tracks_managed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, app, one, two = _control_test_app(tmp_path)
    (Path(one["path"]) / "demo.py").write_text("print('synthetic')\n", encoding="utf-8")
    read_only = app.state.control_credentials.issue(
        name="observer", capabilities=["read"], workspace_ids=[one["id"]],
    )
    operator = app.state.control_credentials.issue(
        name="operator", capabilities=["read", "operate"], workspace_ids=[one["id"]],
    )
    elsewhere = app.state.control_credentials.issue(
        name="elsewhere", capabilities=["read", "operate"], workspace_ids=[two["id"]],
    )
    headers = {"Authorization": f"Bearer {operator['token']}"}
    run_id = str(uuid.uuid4())
    calls: list[dict] = []

    def simulated_managed_start(workspace, path, *, expected_digest, idempotency_key):
        calls.append({
            "workspace_id": workspace["id"], "path": path,
            "digest": expected_digest, "key": idempotency_key,
        })
        return {
            "id": run_id, "state": "running", "relative_path": path,
            "terminal_id": "synthetic-file-terminal", "created_at": "synthetic",
            "started_at": "synthetic", "ended_at": None, "exit_code": None,
            "error_code": None, "lifecycle_revision": 2,
        }

    monkeypatch.setattr(app.state.file_runs, "start", simulated_managed_start)
    base = f"/api/control/v1/workspaces/{one['id']}/file-runs"
    payload = {
        "action_id": "file-run-start-0001", "relative_path": "demo.py",
        "expected_digest": hashlib.sha256(b"print('synthetic')\n").hexdigest(),
        "idempotency_key": str(uuid.uuid4()),
    }
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver",
    ) as agent:
        for token in (read_only["token"], elsewhere["token"]):
            denied = await agent.post(
                base, json=payload, headers={"Authorization": f"Bearer {token}"},
            )
            assert denied.status_code == 404
        invalid = await agent.post(
            base, json={**payload, "idempotency_key": "not-a-uuid"},
            headers=headers,
        )
        assert invalid.status_code == 400
        started = await agent.post(base, json=payload, headers=headers)
        assert started.status_code == 202, started.text
        assert started.json()["status"] == "accepted"
        assert started.json()["phase"] == "process_started"
        assert started.json()["result_resource_id"] == run_id
        assert started.json()["run"]["id"] == run_id
        assert calls == [{
            "workspace_id": one["id"], "path": "demo.py",
            "digest": payload["expected_digest"], "key": payload["idempotency_key"],
        }]
        replay = await agent.post(base, json=payload, headers=headers)
        assert replay.status_code == 202 and len(calls) == 1
        collision = await agent.post(
            base, json={**payload, "expected_digest": "f" * 64},
            headers=headers,
        )
        assert collision.status_code == 409
        assert collision.json()["code"] == "idempotency_conflict"


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
async def test_control_launches_real_local_quick_command_and_file_run(
    tmp_path: Path,
) -> None:
    _, app, one, _ = _control_test_app(tmp_path)
    script_content = "print('control-file-smoke')\n"
    (Path(one["path"]) / "smoke.py").write_text(script_content, encoding="utf-8")
    app.state.store.replace_workspace_commands(
        one["id"], ["printf 'control-quick-smoke\\n'"],
    )
    quick = app.state.store.list_workspace_quick_commands(one["id"])[0]
    operator = app.state.control_credentials.issue(
        name="operator", capabilities=["read", "operate"], workspace_ids=[one["id"]],
    )
    headers = {"Authorization": f"Bearer {operator['token']}"}
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver",
        ) as agent:
            launched = await agent.post(
                f"/api/control/v1/workspaces/{one['id']}/commands/{quick['id']}/launch",
                json={
                    "action_id": "real-quick-launch-0001",
                    "launch_id": uuid.uuid4().hex,
                    "expected_revision": quick["revision"],
                },
                headers=headers,
            )
            assert launched.status_code == 202, launched.text
            assert launched.json()["phase"] == "process_started"
            terminal_id = launched.json()["terminal_id"]
            assert app.state.store.get_terminal(terminal_id) is not None

            started = await agent.post(
                f"/api/control/v1/workspaces/{one['id']}/file-runs",
                json={
                    "action_id": "real-file-run-0001",
                    "relative_path": "smoke.py",
                    "expected_digest": hashlib.sha256(script_content.encode()).hexdigest(),
                    "idempotency_key": str(uuid.uuid4()),
                },
                headers=headers,
            )
            assert started.status_code == 202, started.text
            run_id = started.json()["run"]["id"]
            deadline = time.monotonic() + 5
            latest = app.state.file_runs.reconcile(run_id)
            while latest["state"] not in {"finished", "failed", "stopped"}:
                assert time.monotonic() < deadline, latest
                await asyncio.sleep(0.05)
                latest = app.state.file_runs.reconcile(run_id)
            assert latest["state"] == "finished", latest
            assert latest["exit_code"] == 0
            terminal = app.state.store.get_managed_terminal(one["id"], "file_run")
            assert terminal is not None
            output = app.state.terminals.capture_scrollback(one, terminal)
            assert "control-file-smoke" in output
    finally:
        # Only the uniquely named tmux session created for this temporary workspace.
        subprocess.run(
            ["tmux", "kill-session", "-t", str(one["tmux_session"])],
            check=False, capture_output=True,
        )

@pytest.mark.asyncio
async def test_control_named_launch_uncertain_dispatch_never_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, app, one, _ = _control_test_app(tmp_path)
    app.state.store.replace_workspace_commands(one["id"], ["printf uncertain"])
    row = app.state.store.list_workspace_quick_commands(one["id"])[0]
    operator = app.state.control_credentials.issue(
        name="operator", capabilities=["read", "operate"], workspace_ids=[one["id"]],
    )
    headers = {"Authorization": f"Bearer {operator['token']}"}
    calls: list[str] = []

    def lost_ack(workspace, *, slot, command, launch_id):
        calls.append(launch_id)
        raise OSError("synthetic lost acknowledgement")

    monkeypatch.setattr(app.state.terminals, "run_workspace_command", lost_ack)
    base = f"/api/control/v1/workspaces/{one['id']}/commands/{row['id']}/launch"
    payload = {
        "action_id": "quick-launch-unknown", "expected_revision": row["revision"],
        "launch_id": uuid.uuid4().hex,
    }
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver",
    ) as agent:
        first = await agent.post(base, headers=headers, json=payload)
        assert first.status_code == 409 and first.json()["status"] == "unknown"
        assert first.json()["result_resource_id"] == payload["launch_id"]
        second = await agent.post(base, headers=headers, json=payload)
        assert second.status_code == 409 and second.json()["status"] == "unknown"
        assert calls == [payload["launch_id"]]


@pytest.mark.asyncio
async def test_control_remote_run_requires_source_and_target_operate_grants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, app, one, two = _control_test_app(tmp_path)
    store = app.state.store
    target = store.create_computer(
        name="Synthetic remote", ssh_alias="", host="synthetic.example.invalid",
        port=22, username="synthetic", identity_file="",
        host_key_type="ssh-ed25519", host_key_data="fake",
        host_fingerprint="fake-synthetic",
    )
    registered_target = app.state.workspaces.open_remote(
        target["id"], "/home/synthetic/project",
    )
    source_only = app.state.control_credentials.issue(
        name="source-only", capabilities=["read", "operate"],
        workspace_ids=[one["id"]],
    )
    target_only = app.state.control_credentials.issue(
        name="target-only", capabilities=["read", "operate"],
        workspace_ids=[registered_target["id"]],
    )
    allowed = app.state.control_credentials.issue(
        name="source-and-target", capabilities=["read", "operate"],
        workspace_ids=[one["id"], registered_target["id"]],
    )
    headers = {"Authorization": f"Bearer {allowed['token']}"}
    calls: list[dict] = []

    async def simulated_create(payload):
        calls.append(dict(payload))
        return ({
            "id": payload["id"], "state": "preparing", "phase": "scanning",
            "exit_code": None, "lifecycle_revision": 1, "created_at": "synthetic",
            "started_at": None, "ended_at": None,
            "target_computer_id": payload["target_computer_id"],
        }, True)

    monkeypatch.setattr(app.state.remote_runs, "create", simulated_create)
    base = f"/api/control/v1/workspaces/{one['id']}/remote-runs"
    payload = {
        "action_id": "remote-run-start-0001",
        "target_computer_id": target["id"], "command": "printf synthetic",
        "source_path": ".", "source_options": {"explicitly_included": []},
    }
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver",
    ) as agent:
        for denied_client in (source_only, target_only):
            denied = await agent.post(
                base, json=payload,
                headers={"Authorization": f"Bearer {denied_client['token']}"},
            )
            assert denied.status_code == 404
        invalid = await agent.post(
            base, json={**payload, "source_path": "../two"}, headers=headers,
        )
        assert invalid.status_code == 400
        started = await agent.post(base, json=payload, headers=headers)
        assert started.status_code == 202, started.text
        assert started.json()["status"] == "accepted"
        assert started.json()["phase"] == "dispatched"
        run_id = started.json()["run_id"]
        assert uuid.UUID(run_id).version == 4
        assert started.json()["result_resource_id"] == run_id
        assert calls == [{
            "id": run_id, "source_kind": "workspace",
            "source_workspace_id": one["id"], "source_path": ".",
            "source_options": {"explicitly_included": []},
            "target_computer_id": target["id"], "command": "printf synthetic",
        }]
        repeated = await agent.post(base, json=payload, headers=headers)
        assert repeated.status_code == 202
        assert repeated.json()["run_id"] == run_id
        assert len(calls) == 1
        collision = await agent.post(
            base, json={**payload, "command": "printf changed"}, headers=headers,
        )
        assert collision.status_code == 409
        assert collision.json()["code"] == "idempotency_conflict"
        other_source = await agent.post(
            f"/api/control/v1/workspaces/{two['id']}/remote-runs",
            json={**payload, "action_id": "remote-run-elsewhere"},
            headers=headers,
        )
        assert other_source.status_code == 404
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_control_remote_run_review_exposes_only_scoped_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, app, one, two = _control_test_app(tmp_path)
    store = app.state.store
    target = store.create_computer(
        name="Review target", ssh_alias="", host="review.example.invalid",
        port=22, username="synthetic", identity_file="",
        host_key_type="ssh-ed25519", host_key_data="fake",
        host_fingerprint="fake-review",
    )
    target_workspace = app.state.workspaces.open_remote(
        target["id"], "/home/synthetic/review",
    )
    run_id = str(uuid.uuid4())
    store.create_remote_run({
        "id": run_id, "source_kind": "workspace",
        "source_workspace_id": one["id"], "source_label": "one",
        "target_computer_id": target["id"], "command": "printf synthetic",
        "run_base": "/home/synthetic/.cache/termroom/runs",
        "state": "finished", "source_options_json": "{}",
        "created_at": "2026-11-08T01:00:00+00:00",
        "expires_at": "2027-11-08T01:00:00+00:00",
    })
    source_only = app.state.control_credentials.issue(
        name="source-only", capabilities=["read"], workspace_ids=[one["id"]],
    )
    allowed = app.state.control_credentials.issue(
        name="source-and-target", capabilities=["read"],
        workspace_ids=[one["id"], target_workspace["id"]],
    )
    calls: list[str] = []

    class SyntheticPlan:
        def as_dict(self):
            return {
                "revision": "reviewed-revision", "apply_allowed": True,
                "items": [{"path": "one.txt", "change": "modified", "status": "ready"}],
            }

    async def simulated_review(value):
        calls.append(value)
        return SyntheticPlan()

    monkeypatch.setattr(app.state.run_results, "review", simulated_review)
    base = f"/api/control/v1/workspaces/{one['id']}/remote-runs/{run_id}/result/review"
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver",
    ) as agent:
        denied = await agent.get(
            base, headers={"Authorization": f"Bearer {source_only['token']}"},
        )
        assert denied.status_code == 404
        reviewed = await agent.get(
            base, headers={"Authorization": f"Bearer {allowed['token']}"},
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["plan"]["revision"] == "reviewed-revision"
        assert reviewed.json()["approval"] == "human_browser_required_for_apply"
        assert calls == [run_id]
        other_source = await agent.get(
            f"/api/control/v1/workspaces/{two['id']}/remote-runs/{run_id}/result/review",
            headers={"Authorization": f"Bearer {allowed['token']}"},
        )
        assert other_source.status_code == 404
        assert calls == [run_id]


@pytest.mark.asyncio
async def test_control_file_run_stop_targets_exact_managed_id_and_never_forces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, app, one, two = _control_test_app(tmp_path)
    store = app.state.store
    run_id = str(uuid.uuid4())
    claim, run = store.claim_file_run({
        "id": run_id, "workspace_id": one["id"],
        "idempotency_key": str(uuid.uuid4()),
        "relative_path": "example.py", "source_digest": "a" * 64,
        "runner_id": "python3", "runner_version": 1,
        "argv": ["python3", "--", "./example.py"],
    })
    assert claim == "created"
    operator = app.state.control_credentials.issue(
        name="operator", capabilities=["read", "operate"], workspace_ids=[one["id"]],
    )
    reader = app.state.control_credentials.issue(
        name="reader", capabilities=["read"], workspace_ids=[one["id"]],
    )
    elsewhere = app.state.control_credentials.issue(
        name="elsewhere", capabilities=["read", "operate"], workspace_ids=[two["id"]],
    )
    headers = {"Authorization": f"Bearer {operator['token']}"}
    calls: list[str] = []

    def stop_one(value):
        calls.append(value)
        return {"run": {**run, "state": "running"}, "needs_force": True}

    monkeypatch.setattr(app.state.file_runs, "stop", stop_one)
    monkeypatch.setattr(
        app.state.file_runs, "kill",
        lambda *_: pytest.fail("Control stop called force-kill"),
    )
    base = f"/api/control/v1/workspaces/{one['id']}/file-runs/{run_id}/stop"
    payload = {
        "action_id": "file-run-stop-0001",
        "expected_lifecycle_revision": run["lifecycle_revision"],
    }
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver",
    ) as agent:
        for credential in (reader, elsewhere):
            denied = await agent.post(
                base, json=payload,
                headers={"Authorization": f"Bearer {credential['token']}"},
            )
            assert denied.status_code == 404
        stale = await agent.post(
            base, headers=headers,
            json={**payload, "action_id": "file-run-stop-stale",
                  "expected_lifecycle_revision": run["lifecycle_revision"] + 1},
        )
        assert stale.status_code == 409 and stale.json()["code"] == "revision_conflict"
        stopped = await agent.post(base, json=payload, headers=headers)
        assert stopped.status_code == 202, stopped.text
        assert stopped.json()["status"] == "accepted"
        assert stopped.json()["phase"] == "dispatched"
        assert stopped.json()["result_resource_id"] == run_id
        assert stopped.json()["needs_human_force"] is True
        assert stopped.json()["stop_verified"] is False
        replay = await agent.post(base, json=payload, headers=headers)
        assert replay.status_code == 202
        assert calls == [run_id]
        other_target = await agent.post(
            f"/api/control/v1/workspaces/{two['id']}/file-runs/{run_id}/stop",
            headers={"Authorization": f"Bearer {elsewhere['token']}"},
            json={**payload, "action_id": "file-run-stop-other"},
        )
        assert other_target.status_code == 404


@pytest.mark.asyncio
async def test_control_remote_run_stop_requires_both_grants_and_exact_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, app, one, two = _control_test_app(tmp_path)
    store = app.state.store
    target = store.create_computer(
        name="Stop target", ssh_alias="", host="stop.example.invalid",
        port=22, username="synthetic", identity_file="",
        host_key_type="ssh-ed25519", host_key_data="fake",
        host_fingerprint="fake-stop",
    )
    target_workspace = app.state.workspaces.open_remote(
        target["id"], "/home/synthetic/stop",
    )
    run_id = str(uuid.uuid4())
    run, created = store.create_remote_run({
        "id": run_id, "source_kind": "workspace",
        "source_workspace_id": one["id"], "source_label": "one",
        "target_computer_id": target["id"], "command": "printf synthetic",
        "run_base": "/home/synthetic/.cache/termroom/runs",
        "state": "running", "source_options_json": "{}",
        "created_at": "2026-11-08T01:00:00+00:00",
        "expires_at": "2027-11-08T01:00:00+00:00",
    })
    assert created
    source_only = app.state.control_credentials.issue(
        name="source-only", capabilities=["read", "operate"],
        workspace_ids=[one["id"]],
    )
    operator = app.state.control_credentials.issue(
        name="both", capabilities=["read", "operate"],
        workspace_ids=[one["id"], target_workspace["id"]],
    )
    headers = {"Authorization": f"Bearer {operator['token']}"}
    calls: list[str] = []

    def stop_exact(value):
        calls.append(value)
        return {
            "run": {**run, "state": "running"},
            "stopped": False, "needs_kill": True, "cancellation_pending": True,
        }

    monkeypatch.setattr(app.state.remote_runs, "stop", stop_exact)
    monkeypatch.setattr(
        app.state.remote_runs, "kill",
        lambda *_: pytest.fail("Control stop called force-kill"),
    )
    base = f"/api/control/v1/workspaces/{one['id']}/remote-runs/{run_id}/stop"
    payload = {
        "action_id": "remote-run-stop-0001",
        "expected_lifecycle_revision": run["lifecycle_revision"],
    }
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver",
    ) as agent:
        denied = await agent.post(
            base, json=payload,
            headers={"Authorization": f"Bearer {source_only['token']}"},
        )
        assert denied.status_code == 404
        stale = await agent.post(
            base, json={**payload, "action_id": "remote-run-stop-stale",
                         "expected_lifecycle_revision": run["lifecycle_revision"] + 1},
            headers=headers,
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "revision_conflict"
        stopped = await agent.post(base, json=payload, headers=headers)
        assert stopped.status_code == 202, stopped.text
        assert stopped.json()["status"] == "accepted"
        assert stopped.json()["phase"] == "dispatched"
        assert stopped.json()["stop_verified"] is False
        assert stopped.json()["needs_human_kill"] is True
        assert stopped.json()["cancellation_pending"] is True
        assert stopped.json()["result_resource_id"] == run_id
        replay = await agent.post(base, json=payload, headers=headers)
        assert replay.status_code == 202
        assert calls == [run_id]
        denied_source = await agent.post(
            f"/api/control/v1/workspaces/{two['id']}/remote-runs/{run_id}/stop",
            json={**payload, "action_id": "remote-run-stop-other"},
            headers=headers,
        )
        assert denied_source.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("plan_change", ["revision", "plan_content"])
async def test_control_result_apply_requires_real_browser_approval_and_exact_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plan_change: str,
) -> None:
    from termroom.run_results import (
        CollectionPlan,
        CollectionPlanItem,
        CollectionReport,
        CollectionReportItem,
    )

    settings, app, one, _ = _control_test_app(tmp_path)
    source_file = Path(one["path"]) / "notes.txt"
    source_file.write_text("human original\n", encoding="utf-8")
    store = app.state.store
    target = store.create_computer(
        name="Synthetic approval target", ssh_alias="",
        host="approval.example.invalid", port=22,
        username="synthetic", identity_file="",
        host_key_type="ssh-ed25519", host_key_data="fake",
        host_fingerprint="fake-approval",
    )
    target_workspace = app.state.workspaces.open_remote(
        target["id"], "/home/synthetic/approval",
    )
    run_id = str(uuid.uuid4())
    store.create_remote_run({
        "id": run_id, "source_kind": "workspace",
        "source_workspace_id": one["id"], "source_label": "one",
        "target_computer_id": target["id"], "command": "printf synthetic",
        "run_base": "/home/synthetic/.cache/termroom/runs",
        "state": "finished", "source_options_json": "{}",
        "created_at": "2026-11-08T01:00:00+00:00",
        "expires_at": "2027-11-08T01:00:00+00:00",
    })
    operator = app.state.control_credentials.issue(
        name="Synthetic operator", capabilities=["read", "operate"],
        workspace_ids=[one["id"], target_workspace["id"]],
    )
    other_operator = app.state.control_credentials.issue(
        name="Other operator", capabilities=["read", "operate"],
        workspace_ids=[one["id"], target_workspace["id"]],
    )
    headers = {"Authorization": f"Bearer {operator['token']}"}
    other_headers = {"Authorization": f"Bearer {other_operator['token']}"}
    plan_revision = ["a" * 64]
    current_digest = ["d" * 64]
    apply_calls: list[tuple[str, str]] = []

    async def review_current(value):
        assert value == run_id
        return CollectionPlan(
            run_id=run_id, source_workspace_id=one["id"], source_path=".",
            revision=plan_revision[0], items=(
                CollectionPlanItem(
                    "notes.txt", "modified", "ready", "ready",
                    baseline_digest="b" * 64, result_digest="c" * 64,
                    current_digest=current_digest[0], current_mtime_ns=1,
                ),
            ),
        )

    async def apply_once(value, revision):
        apply_calls.append((value, revision))
        source_file.write_text("agent applied\n", encoding="utf-8")
        return CollectionReport(
            run_id=run_id, plan_revision=revision,
            items=(CollectionReportItem("notes.txt", "modified", "applied", "applied"),),
        )

    monkeypatch.setattr(app.state.run_results, "review", review_current)
    monkeypatch.setattr(app.state.run_results, "apply", apply_once)
    intent_url = (
        f"/api/control/v1/workspaces/{one['id']}/remote-runs/"
        f"{run_id}/result/apply-intents"
    )
    apply_url = (
        f"/api/control/v1/workspaces/{one['id']}/remote-runs/"
        f"{run_id}/result/apply"
    )
    browser_review = f"/remote-runs/{run_id}/collect"
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as agent,
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as browser,
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as outsider,
    ):
        await browser.post("/login", data={"password": "correct-password"})
        requested = await agent.post(
            intent_url, headers=headers,
            json={"action_id": "result-apply-0001", "revision": plan_revision[0]},
        )
        assert requested.status_code == 201, requested.text
        approval_id = requested.json()["approval_id"]
        approval_path = (
            f"/remote-runs/{run_id}/approve-control-apply/{approval_id}"
        )
        payload = {
            "action_id": "result-apply-0001",
            "revision": plan_revision[0], "approval_id": approval_id,
        }
        early = await agent.post(apply_url, headers=headers, json=payload)
        assert early.status_code == 409
        assert early.json()["code"] == "approval_required"
        assert source_file.read_text(encoding="utf-8") == "human original\n"

        page = await browser.get(browser_review)
        assert page.status_code == 200, page.text
        assert "Synthetic operator" in page.text
        assert "result-apply-0001" in page.text
        assert "notes.txt" in page.text
        assert "Approve this client request" in page.text
        first_match = re.search(
            r'name="plan_hash" value="([0-9a-f]{64})"', page.text,
        )
        assert first_match is not None
        plan_hash = first_match.group(1)
        forbidden = await outsider.post(
            approval_path,
            data={"_csrf": settings.csrf_token, "revision": plan_revision[0]},
        )
        assert forbidden.status_code == 401
        bad_csrf = await browser.post(
            approval_path, data={"_csrf": "incorrect", "revision": plan_revision[0]},
        )
        assert bad_csrf.status_code == 403
        status = await agent.get(f"{intent_url}/{approval_id}", headers=headers)
        assert status.status_code == 200 and status.json()["state"] == "pending"

        approved = await browser.post(
            approval_path,
            data={"_csrf": settings.csrf_token, "revision": plan_revision[0],
                  "plan_hash": plan_hash},
            follow_redirects=False,
        )
        assert approved.status_code == 303, approved.text
        assert source_file.read_text(encoding="utf-8") == "human original\n"
        status = await agent.get(f"{intent_url}/{approval_id}", headers=headers)
        assert status.json()["state"] == "approved"
        stolen = await agent.post(apply_url, headers=other_headers, json=payload)
        assert stolen.status_code == 409
        assert stolen.json()["code"] == "approval_required"
        assert apply_calls == []

        applied = await agent.post(apply_url, headers=headers, json=payload)
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "observed"
        assert applied.json()["phase"] == "result_verified"
        assert applied.json()["report"]["summary"]["applied"] == 1
        assert applied.json()["report_persisted"] is True
        assert source_file.read_text(encoding="utf-8") == "agent applied\n"
        assert apply_calls == [(run_id, "a" * 64)]
        replay = await agent.post(apply_url, headers=headers, json=payload)
        assert replay.status_code == 200
        assert apply_calls == [(run_id, "a" * 64)]
        collision = await agent.post(
            apply_url, headers=headers,
            json={**payload, "revision": "f" * 64},
        )
        assert collision.status_code == 409
        assert collision.json()["code"] == "idempotency_conflict"
        status = await agent.get(f"{intent_url}/{approval_id}", headers=headers)
        assert status.json()["state"] == "consumed"

        plan_revision[0] = "b" * 64
        second = await agent.post(
            intent_url, headers=headers,
            json={"action_id": "result-apply-0002", "revision": "b" * 64},
        )
        assert second.status_code == 201, second.text
        second_id = second.json()["approval_id"]
        second_page = await browser.get(browser_review)
        second_match = re.search(
            r'name="plan_hash" value="([0-9a-f]{64})"', second_page.text,
        )
        assert second_match is not None
        second_approved = await browser.post(
            f"/remote-runs/{run_id}/approve-control-apply/{second_id}",
            data={"_csrf": settings.csrf_token, "revision": "b" * 64,
                  "plan_hash": second_match.group(1)},
            follow_redirects=False,
        )
        assert second_approved.status_code == 303
        if plan_change == "revision":
            plan_revision[0] = "c" * 64
        else:
            current_digest[0] = "e" * 64
        stale = await agent.post(
            apply_url, headers=headers,
            json={
                "action_id": "result-apply-0002", "approval_id": second_id,
                "revision": "b" * 64,
            },
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "plan_revision_mismatch"
        assert apply_calls == [(run_id, "a" * 64)]


@pytest.mark.asyncio
async def test_control_result_apply_waiting_for_review_refuses_revoked_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from termroom.run_results import (
        CollectionPlan,
        CollectionPlanItem,
        CollectionReport,
        CollectionReportItem,
    )

    settings, app, one, _ = _control_test_app(tmp_path)
    source_file = Path(one["path"]) / "notes.txt"
    source_file.write_text("human original\n", encoding="utf-8")
    store = app.state.store
    target = store.create_computer(
        name="Synthetic revoked-apply target", ssh_alias="",
        host="approval.example.invalid", port=22,
        username="synthetic", identity_file="",
        host_key_type="ssh-ed25519", host_key_data="fake",
        host_fingerprint="fake-revoked-apply",
    )
    target_workspace = app.state.workspaces.open_remote(
        target["id"], "/home/synthetic/revoked-apply",
    )
    run_id = str(uuid.uuid4())
    store.create_remote_run({
        "id": run_id, "source_kind": "workspace",
        "source_workspace_id": one["id"], "source_label": "one",
        "target_computer_id": target["id"], "command": "printf synthetic",
        "run_base": "/home/synthetic/.cache/termroom/runs",
        "state": "finished", "source_options_json": "{}",
        "created_at": "2026-11-08T01:00:00+00:00",
        "expires_at": "2027-11-08T01:00:00+00:00",
    })
    issued = app.state.control_credentials.issue(
        name="revoked-apply-operator", capabilities=["read", "operate"],
        workspace_ids=[one["id"], target_workspace["id"]],
    )
    headers = {"Authorization": f"Bearer {issued['token']}"}
    revision = "a" * 64
    entered = asyncio.Event()
    proceed = asyncio.Event()
    hold_review = [False]
    applied: list[str] = []

    async def review(value):
        assert value == run_id
        if hold_review[0]:
            entered.set()
            await proceed.wait()
        return CollectionPlan(
            run_id=run_id, source_workspace_id=one["id"], source_path=".",
            revision=revision, items=(
                CollectionPlanItem(
                    "notes.txt", "modified", "ready", "ready",
                    baseline_digest="b" * 64, result_digest="c" * 64,
                    current_digest="d" * 64, current_mtime_ns=1,
                ),
            ),
        )

    async def apply(value, selected_revision):
        applied.append(value)
        source_file.write_text("applied after revoke\n", encoding="utf-8")
        return CollectionReport(
            run_id=run_id, plan_revision=selected_revision,
            items=(CollectionReportItem("notes.txt", "modified", "applied", "applied"),),
        )

    monkeypatch.setattr(app.state.run_results, "review", review)
    monkeypatch.setattr(app.state.run_results, "apply", apply)
    base = f"/api/control/v1/workspaces/{one['id']}/remote-runs/{run_id}"
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as agent,
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as browser,
    ):
        assert (await browser.post(
            "/login", data={"password": "correct-password"},
        )).status_code == 303
        intent = await agent.post(
            base + "/result/apply-intents", headers=headers,
            json={"action_id": "apply-revoked-0001", "revision": revision},
        )
        assert intent.status_code == 201, intent.text
        approval_id = intent.json()["approval_id"]
        page = await browser.get(f"/remote-runs/{run_id}/collect")
        assert page.status_code == 200, page.text
        match = re.search(r'name="plan_hash" value="([0-9a-f]{64})"', page.text)
        assert match is not None
        approved = await browser.post(
            f"/remote-runs/{run_id}/approve-control-apply/{approval_id}",
            data={"_csrf": settings.csrf_token, "revision": revision,
                  "plan_hash": match.group(1)},
            follow_redirects=False,
        )
        assert approved.status_code == 303, approved.text

        hold_review[0] = True
        pending = asyncio.create_task(agent.post(
            base + "/result/apply", headers=headers,
            json={"action_id": "apply-revoked-0001", "revision": revision,
                  "approval_id": approval_id},
        ))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            row = store.get_client_action(
                issued["id"], "apply-revoked-0001",
                instance_id=app.state.control_instance_id,
            )
            assert row is not None and row["phase"] == "accepted"
            revoked = await browser.post(
                f"/api/client-credentials/{issued['id']}/revoke",
                json={"confirm_client_id": issued["id"]},
                headers={"X-Termroom-CSRF": settings.csrf_token},
            )
            assert revoked.status_code == 200, revoked.text
        finally:
            proceed.set()
        refused = await asyncio.wait_for(pending, 5)
        assert refused.status_code == 409, refused.text
        assert refused.json()["status"] == "refused"
        assert refused.json()["code"] == "credential_revoked"
        assert applied == []
        assert source_file.read_text(encoding="utf-8") == "human original\n"
        row = store.get_client_action(
            issued["id"], "apply-revoked-0001",
            instance_id=app.state.control_instance_id,
        )
        assert row is not None and row["status"] == "refused"


@pytest.mark.asyncio
async def test_control_workspace_and_files_read_scope_privacy_and_bounds(tmp_path: Path) -> None:
    root = tmp_path / "root"
    one_path = root / "one"
    one_path.mkdir(parents=True)
    (root / "two").mkdir()
    state_dir = one_path / ".termroom-private"
    settings = Settings.create(
        root, state_dir=state_dir, login_password="correct-password",
        access_token="synthetic-test-only",
    )
    app = create_app(settings)
    one = app.state.workspaces.open("one")
    two = app.state.workspaces.open("two")
    (one_path / "hello.txt").write_text("hello 한글\n", encoding="utf-8")
    (one_path / "too-big.txt").write_bytes(b"x" * 131073)
    (state_dir / "private-key").write_text("DO-NOT-EXPOSE", encoding="utf-8")
    app.state.store.replace_workspace_commands(one["id"], ["echo hello"])
    issued = app.state.control_credentials.issue(
        name="read-only", capabilities=["read"], workspace_ids=[one["id"]],
    )
    before = app.state.store.get_workspace(one["id"])["last_opened_at"]
    headers = {"Authorization": f"Bearer {issued['token']}"}
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver",
    ) as agent:
        listed = await agent.get("/api/control/v1/workspaces", headers=headers)
        assert listed.status_code == 200, listed.text
        assert [x["id"] for x in listed.json()["workspaces"]] == [one["id"]]
        assert "root_path" not in listed.text and str(state_dir) not in listed.text
        assert listed.json()["workspaces"][0]["connection"]["state"] == "unknown"

        commands = await agent.get(
            f"/api/control/v1/workspaces/{one['id']}/commands", headers=headers,
        )
        assert commands.status_code == 200
        quick = app.state.store.list_workspace_quick_commands(one["id"])[0]
        assert commands.json()["commands"] == [{
            "id": quick["id"], "name": "echo hello", "command": "echo hello",
            "revision": 1, "legacy_slot": 0,
        }]
        assert len(commands.json()["revision"]) == 64

        listing = await agent.get(
            f"/api/control/v1/workspaces/{one['id']}/files", headers=headers,
        )
        assert listing.status_code == 200, listing.text
        names = {entry["name"] for entry in listing.json()["entries"]}
        assert {"hello.txt", "too-big.txt"} <= names
        assert ".termroom-private" not in names
        metadata = await agent.get(
            f"/api/control/v1/workspaces/{one['id']}/files/stat",
            params={"path": "hello.txt"}, headers=headers,
        )
        assert metadata.status_code == 200, metadata.text
        assert metadata.json()["entry"]["size"] == len("hello 한글\n".encode())
        private_metadata = await agent.get(
            f"/api/control/v1/workspaces/{one['id']}/files/stat",
            params={"path": ".termroom-private/private-key"}, headers=headers,
        )
        assert private_metadata.status_code == 404

        found = await agent.get(
            f"/api/control/v1/workspaces/{one['id']}/files/search",
            params={"q": "hello"}, headers=headers,
        )
        assert found.status_code == 200
        assert [entry["name"] for entry in found.json()["entries"]] == ["hello.txt"]
        hidden = await agent.get(
            f"/api/control/v1/workspaces/{one['id']}/files/search",
            params={"q": "private"}, headers=headers,
        )
        assert hidden.status_code == 200 and hidden.json()["entries"] == []

        read = await agent.get(
            f"/api/control/v1/workspaces/{one['id']}/files/text",
            params={"path": "hello.txt"}, headers=headers,
        )
        assert read.status_code == 200, read.text
        assert read.json()["content"] == "hello 한글\n"
        assert read.json()["digest"] == hashlib.sha256("hello 한글\n".encode()).hexdigest()
        assert "root_path" not in read.text
        too_big = await agent.get(
            f"/api/control/v1/workspaces/{one['id']}/files/text",
            params={"path": "too-big.txt"}, headers=headers,
        )
        assert too_big.status_code == 413
        assert too_big.json()["code"] == "resource_too_large"
        for path in (".termroom-private/private-key", "../two"):
            forbidden = await agent.get(
                f"/api/control/v1/workspaces/{one['id']}/files/text",
                params={"path": path}, headers=headers,
            )
            assert forbidden.status_code == 404
            assert "DO-NOT-EXPOSE" not in forbidden.text
        cross_scope = await agent.get(
            f"/api/control/v1/workspaces/{two['id']}/files", headers=headers,
        )
        assert cross_scope.status_code == 404
        assert cross_scope.json()["code"] == "resource_unavailable"

    assert app.state.store.get_workspace(one["id"])["last_opened_at"] == before
    assert app.state.store.list_terminals(one["id"]) == []


@pytest.mark.asyncio
async def test_control_terminal_input_handoff_refuses_partial_browser_line_and_never_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, app, one, _ = _control_test_app(tmp_path)
    terminal = app.state.store.create_terminal(one["id"], "shell", "@12")
    terminal_id = terminal["id"]
    control = app.state.terminal_control
    issued = app.state.control_credentials.issue(
        name="operator", capabilities=["read", "operate"], workspace_ids=[one["id"]],
    )
    headers = {"Authorization": f"Bearer {issued['token']}"}
    url = f"/api/control/v1/workspaces/{one['id']}/terminals/{terminal_id}"
    writes: list[tuple[str, str, bool]] = []
    mode = {
        "session": one["tmux_session"], "window": "@12", "pane": "%12",
        "dead": False, "in_mode": False, "pane_pid": 321,
    }

    def observe(_workspace, _terminal):
        return dict(mode)

    def paste(_workspace, _terminal, pane, value, *, append_enter):
        writes.append((pane, value, append_enter))
        return dict(mode)

    monkeypatch.setattr(app.state.terminals, "current_pane_mode", observe)
    monkeypatch.setattr(app.state.terminals, "paste_terminal_input", paste)
    monkeypatch.setattr(
        app.state.terminals, "ensure_workspace",
        lambda *_args: pytest.fail("Non-UI read/claim must not attach or resize"),
    )

    # A disconnected browser with an unfinished shell line is still not permission.
    person = control.register(terminal_id)
    control.mark_input(terminal_id, person)
    control.unregister(terminal_id, person)

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as agent:
        observed = await agent.get(url + "/control", headers=headers)
        assert observed.status_code == 200
        assert observed.json()["pane"]["pane"] == "%12"
        assert observed.json()["control"]["handoff_required"] is True
        revision = observed.json()["control"]["control_revision"]
        claim_payload = {
            "action_id": "terminal-claim-blocked", "expected_tmux_window": "@12",
            "expected_pane": "%12", "expected_control_revision": revision,
        }
        blocked = await agent.post(url + "/control/claim", headers=headers, json=claim_payload)
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "control_conflict"
        assert writes == []

        # A real browser activation and explicit release allow exactly one external interval.
        person = control.register(terminal_id)
        control.claim_control(terminal_id, person)
        assert control.release_browser_control(terminal_id, person)
        revision = control.external_state(terminal_id)["control_revision"]
        granted = await agent.post(
            url + "/control/claim", headers=headers,
            json={**claim_payload, "action_id": "terminal-claim-allowed",
                  "expected_control_revision": revision},
        )
        assert granted.status_code == 200, granted.text
        generation = granted.json()["generation"]
        claim_revision = granted.json()["control_revision"]
        assert control.can_external_input(
            terminal_id, issued["id"], generation, claim_revision,
        )

        payload = {
            "action_id": "terminal-input-0001", "command_id": "terminal-cmd-0001",
            "expected_tmux_window": "@12", "pane": "%12",
            "generation": generation, "expected_control_revision": claim_revision,
            "data": "pytest", "append_enter": True,
        }
        accepted = await agent.post(url + "/input", headers=headers, json=payload)
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["phase"] == "pty_delivered"
        assert accepted.json()["delivery_scope"] == "tmux_accepted_not_receiver_executed"
        assert writes == [("%12", "pytest", True)]
        repeated = await agent.post(url + "/input", headers=headers, json=payload)
        assert repeated.status_code == 202
        assert writes == [("%12", "pytest", True)]
        conflict = await agent.post(
            url + "/input", headers=headers, json={**payload, "data": "rm"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "idempotency_conflict"
        receipt = await agent.get(
            "/api/control/v1/actions/terminal-input-0001", headers=headers,
        )
        assert receipt.json()["status"] == "accepted"
        assert receipt.json()["phase"] == "pty_delivered"

        mode["pane"] = "%13"
        stale = await agent.post(
            url + "/input", headers=headers,
            json={**payload, "action_id": "terminal-input-stale",
                  "command_id": "terminal-cmd-stale"},
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "pane_changed"
        mode["pane"], mode["in_mode"] = "%12", True
        copy_mode = await agent.post(
            url + "/input", headers=headers,
            json={**payload, "action_id": "terminal-input-mode",
                  "command_id": "terminal-cmd-mode"},
        )
        assert copy_mode.status_code == 409
        assert copy_mode.json()["code"] == "pane_in_mode"
        mode["in_mode"], mode["dead"] = False, True
        ended = await agent.post(
            url + "/input", headers=headers,
            json={**payload, "action_id": "terminal-input-ended",
                  "command_id": "terminal-cmd-ended"},
        )
        assert ended.status_code == 409
        assert ended.json()["code"] == "pane_ended"
        mode["dead"] = False
        assert writes == [("%12", "pytest", True)]
        release = await agent.post(
            url + "/control/release", headers=headers,
            json={"action_id": "terminal-control-release", "generation": generation,
                  "expected_control_revision": claim_revision, "expected_tmux_window": "@12"},
        )
        assert release.status_code == 200
        viewer = control.register(terminal_id)
        control.claim_control(terminal_id, viewer)
        lost = await agent.post(
            url + "/input", headers=headers,
            json={**payload, "action_id": "terminal-input-lost",
                  "command_id": "terminal-cmd-lost"},
        )
        assert lost.status_code == 409
        assert lost.json()["code"] == "control_conflict"
        assert writes == [("%12", "pytest", True)]


@pytest.mark.asyncio
async def test_control_terminal_close_requires_bound_human_browser_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, app, one, _ = _control_test_app(tmp_path)
    selected = app.state.store.create_terminal(one["id"], "shell", "@14")
    other = app.state.store.create_terminal(one["id"], "shell", "@15")
    issued = app.state.control_credentials.issue(
        name="automation-client", capabilities=["read", "operate"], workspace_ids=[one["id"]],
    )
    headers = {"Authorization": f"Bearer {issued['token']}"}
    url = f"/api/control/v1/workspaces/{one['id']}/terminals/{selected['id']}"
    closed: list[str] = []

    def close(_workspace, terminal):
        closed.append(terminal["tmux_window"])
        app.state.store.delete_terminal(terminal["id"])
        return app.state.store.list_terminals(one["id"])

    monkeypatch.setattr(app.state.terminals, "close_terminal", close)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as agent,
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as browser,
    ):
        intent = await agent.post(
            url + "/close-intents", headers=headers,
            json={"action_id": "close-terminal-0001", "expected_tmux_window": "@14"},
        )
        assert intent.status_code == 201, intent.text
        approval_id = intent.json()["approval_id"]
        close_payload = {
            "action_id": "close-terminal-0001", "approval_id": approval_id,
            "expected_tmux_window": "@14",
        }
        assert (
            await agent.post(url + "/close", headers=headers, json=close_payload)
        ).status_code == 409
        assert closed == []
        assert (
            await browser.post("/login", data={"password": "correct-password"})
        ).status_code == 303
        review_url = intent.json()["human_review_url"]
        review = await browser.get(review_url)
        assert review.status_code == 200, review.text
        assert "automation-client" in review.text
        assert "@14" in review.text
        approval = await browser.post(
            review_url, follow_redirects=False,
            data={"_csrf": settings.csrf_token, "decision": "approve",
                  "expected_tmux_window": "@14"},
        )
        assert approval.status_code == 303
        status = await agent.get(
            url + "/close-intents/" + approval_id, headers=headers,
        )
        assert status.json()["state"] == "approved"
        closed_once = await agent.post(url + "/close", headers=headers, json=close_payload)
        assert closed_once.status_code == 200, closed_once.text
        assert closed_once.json()["closed"] is True
        assert closed == ["@14"]
        assert app.state.store.get_terminal(other["id"]) is not None
        repeated = await agent.post(url + "/close", headers=headers, json=close_payload)
        assert repeated.status_code == 200, repeated.text
        assert closed == ["@14"]
        assert (
            await agent.get("/api/control/v1/actions/close-terminal-0001", headers=headers)
        ).json()["status"] == "observed"


@pytest.mark.asyncio
async def test_control_terminal_close_waiting_for_lock_refuses_revoked_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, app, one, _ = _control_test_app(tmp_path)
    selected = app.state.store.create_terminal(one["id"], "shell", "@183")
    issued = app.state.control_credentials.issue(
        name="close-operator", capabilities=["read", "operate"],
        workspace_ids=[one["id"]],
    )
    headers = {"Authorization": f"Bearer {issued['token']}"}
    url = f"/api/control/v1/workspaces/{one['id']}/terminals/{selected['id']}"
    closed: list[str] = []

    def close(_workspace, terminal):
        closed.append(terminal["tmux_window"])
        app.state.store.delete_terminal(terminal["id"])
        return app.state.store.list_terminals(one["id"])

    monkeypatch.setattr(app.state.terminals, "close_terminal", close)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as agent,
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as browser,
    ):
        assert (await browser.post(
            "/login", data={"password": "correct-password"},
        )).status_code == 303
        intent = await agent.post(
            url + "/close-intents", headers=headers,
            json={"action_id": "close-revoked-0001", "expected_tmux_window": "@183"},
        )
        assert intent.status_code == 201, intent.text
        approval_id = intent.json()["approval_id"]
        review_url = intent.json()["human_review_url"]
        assert (await browser.get(review_url)).status_code == 200
        approved = await browser.post(
            review_url, follow_redirects=False,
            data={"_csrf": settings.csrf_token, "decision": "approve",
                  "expected_tmux_window": "@183"},
        )
        assert approved.status_code == 303, approved.text
        status = await agent.get(
            url + "/close-intents/" + approval_id, headers=headers,
        )
        assert status.json()["state"] == "approved"

        lock = app.state.terminal_control.input_lock(selected["id"])
        await lock.acquire()
        pending = asyncio.create_task(agent.post(
            url + "/close", headers=headers,
            json={"action_id": "close-revoked-0001", "approval_id": approval_id,
                  "expected_tmux_window": "@183"},
        ))
        try:
            receipt = None
            for _ in range(100):
                receipt = app.state.store.get_client_action(
                    issued["id"], "close-revoked-0001",
                    instance_id=app.state.control_instance_id,
                )
                if receipt is not None:
                    break
                await asyncio.sleep(0.02)
            assert receipt is not None and receipt["phase"] == "accepted"
            revoked = await browser.post(
                f"/api/client-credentials/{issued['id']}/revoke",
                json={"confirm_client_id": issued["id"]},
                headers={"X-Termroom-CSRF": settings.csrf_token},
            )
            assert revoked.status_code == 200, revoked.text
        finally:
            lock.release()

        refused = await asyncio.wait_for(pending, 5)
        assert refused.status_code == 409, refused.text
        assert refused.json()["status"] == "refused"
        assert refused.json()["code"] == "credential_revoked"
        assert closed == []
        assert app.state.store.get_terminal(selected["id"]) is not None
        receipt = app.state.store.get_client_action(
            issued["id"], "close-revoked-0001",
            instance_id=app.state.control_instance_id,
        )
        assert receipt is not None and receipt["status"] == "refused"
        assert (await agent.get(url + "/close-intents/" + approval_id,
                                headers=headers)).status_code == 401


@pytest.mark.asyncio
async def test_control_terminal_rename_waiting_for_lock_refuses_revoked_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, app, one, _ = _control_test_app(tmp_path)
    selected = app.state.store.create_terminal(one["id"], "shell", "@184")
    issued = app.state.control_credentials.issue(
        name="rename-operator", capabilities=["read", "operate"],
        workspace_ids=[one["id"]],
    )
    headers = {"Authorization": f"Bearer {issued['token']}"}
    renamed: list[str] = []

    def rename(_workspace, terminal, name):
        renamed.append(name)
        app.state.store.rename_terminal(terminal["id"], name)
        return app.state.store.get_terminal(terminal["id"])

    monkeypatch.setattr(app.state.terminals, "rename_terminal", rename)
    url = f"/api/control/v1/workspaces/{one['id']}/terminals/{selected['id']}/rename"
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as agent,
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as browser,
    ):
        await browser.post("/login", data={"password": "correct-password"})
        lock = app.state.terminal_control.input_lock(selected["id"])
        await lock.acquire()
        pending = asyncio.create_task(agent.post(
            url, headers=headers,
            json={"action_id": "rename-revoked-0001", "name": "renamed",
                  "expected_tmux_window": "@184"},
        ))
        try:
            receipt = None
            for _ in range(100):
                receipt = app.state.store.get_client_action(
                    issued["id"], "rename-revoked-0001",
                    instance_id=app.state.control_instance_id,
                )
                if receipt is not None:
                    break
                await asyncio.sleep(0.02)
            assert receipt is not None and receipt["phase"] == "accepted"
            revoked = await browser.post(
                f"/api/client-credentials/{issued['id']}/revoke",
                json={"confirm_client_id": issued["id"]},
                headers={"X-Termroom-CSRF": settings.csrf_token},
            )
            assert revoked.status_code == 200, revoked.text
        finally:
            lock.release()

        refused = await asyncio.wait_for(pending, 5)
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "credential_revoked"
        assert refused.json()["status"] == "refused"
        assert renamed == []
        assert app.state.store.get_terminal(selected["id"])["name"] == "shell"


@pytest.mark.asyncio
async def test_control_terminal_input_continues_after_lost_http_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, app, one, _ = _control_test_app(tmp_path)
    terminal = app.state.store.create_terminal(one["id"], "shell", "@81")
    control = app.state.terminal_control
    terminal_id = terminal["id"]
    credential = app.state.control_credentials.issue(
        name="operator", capabilities=["read", "operate"], workspace_ids=[one["id"]],
    )
    headers = {"Authorization": f"Bearer {credential['token']}"}
    mode = {"window": "@81", "pane": "%81", "dead": False, "in_mode": False}
    entered, finish = threading.Event(), threading.Event()
    calls: list[str] = []

    monkeypatch.setattr(app.state.terminals, "current_pane_mode", lambda *_args: dict(mode))

    def delayed(_workspace, _terminal, _pane, data, *, append_enter):
        entered.set()
        assert finish.wait(5), "Test did not release admitted input"
        calls.append(data)
        return dict(mode)

    monkeypatch.setattr(app.state.terminals, "paste_terminal_input", delayed)
    base = f"/api/control/v1/workspaces/{one['id']}/terminals/{terminal_id}"
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as agent:
        revision = control.external_state(terminal_id)["control_revision"]
        grant = await agent.post(base + "/control/claim", headers=headers, json={
            "action_id": "cancelled-claim-0001", "expected_tmux_window": "@81",
            "expected_pane": "%81", "expected_control_revision": revision,
        })
        assert grant.status_code == 200, grant.text
        payload = {
            "action_id": "cancelled-input-0001", "command_id": "cancelled-command-0001",
            "expected_tmux_window": "@81", "pane": "%81",
            "generation": grant.json()["generation"],
            "expected_control_revision": grant.json()["control_revision"],
            "data": "single-paste", "append_enter": True,
        }
        request = asyncio.create_task(agent.post(base + "/input", headers=headers, json=payload))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), 4)
            assert control.input_lock(terminal_id).locked()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert len(app.state.terminal_mutation_tasks) == 1
        finally:
            finish.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = (await agent.get(
                "/api/control/v1/actions/cancelled-input-0001", headers=headers,
            )).json()
            if status["phase"] == "pty_delivered":
                break
            await asyncio.sleep(0.05)
        assert status["phase"] == "pty_delivered", status
        assert calls == ["single-paste"]
        assert not app.state.terminal_mutation_tasks
        repeated = await agent.post(base + "/input", headers=headers, json=payload)
        assert repeated.status_code == 202
        assert calls == ["single-paste"]


@pytest.mark.asyncio
async def test_control_terminal_waiting_input_refuses_revoked_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, app, one, _ = _control_test_app(tmp_path)
    terminal = app.state.store.create_terminal(one["id"], "shell", "@181")
    terminal_id = terminal["id"]
    issued = app.state.control_credentials.issue(
        name="operator", capabilities=["read", "operate"], workspace_ids=[one["id"]],
    )
    headers = {"Authorization": f"Bearer {issued['token']}"}
    mode = {"window": "@181", "pane": "%181", "dead": False, "in_mode": False}
    delivered: list[str] = []
    monkeypatch.setattr(app.state.terminals, "current_pane_mode", lambda *_args: mode)

    def deliver(_workspace, _terminal, _pane, data, *, append_enter):
        delivered.append(data)
        return mode

    monkeypatch.setattr(app.state.terminals, "paste_terminal_input", deliver)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    base = f"/api/control/v1/workspaces/{one['id']}/terminals/{terminal_id}"
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as browser,
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as agent,
    ):
        await browser.post("/login", data={"password": "correct-password"})
        revision = app.state.terminal_control.external_state(terminal_id)["control_revision"]
        claimed = await agent.post(base + "/control/claim", headers=headers, json={
            "action_id": "revocation-claim-0001",
            "expected_tmux_window": "@181", "expected_pane": "%181",
            "expected_control_revision": revision,
        })
        assert claimed.status_code == 200, claimed.text
        payload = {
            "action_id": "revocation-waiting-input-0001",
            "command_id": "revocation-command-0001",
            "expected_tmux_window": "@181", "pane": "%181",
            "generation": claimed.json()["generation"],
            "expected_control_revision": claimed.json()["control_revision"],
            "data": "must-not-arrive", "append_enter": True,
        }
        lock = app.state.terminal_control.input_lock(terminal_id)
        await lock.acquire()
        pending = asyncio.create_task(agent.post(base + "/input", headers=headers, json=payload))
        try:
            for _ in range(100):
                row = app.state.store.get_client_action(
                    issued["id"], payload["action_id"],
                    instance_id=app.state.control_instance_id,
                )
                if row is not None:
                    break
                await asyncio.sleep(0.02)
            assert row is not None and row["phase"] == "accepted"
            revoked = await browser.post(
                f"/api/client-credentials/{issued['id']}/revoke",
                json={"confirm_client_id": issued["id"]},
                headers={"X-Termroom-CSRF": settings.csrf_token},
            )
            assert revoked.status_code == 200, revoked.text
            assert revoked.json()["client"]["revoked_at"]
        finally:
            lock.release()
        result = await asyncio.wait_for(pending, 5)
        assert result.status_code == 409, result.text
        assert result.json()["status"] == "refused"
        assert result.json()["code"] == "credential_revoked"
        assert delivered == []
        assert (await agent.get(base + "/control", headers=headers)).status_code == 401


@pytest.mark.asyncio
async def test_control_terminal_waiting_release_refuses_revoked_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, app, one, _ = _control_test_app(tmp_path)
    terminal = app.state.store.create_terminal(one["id"], "shell", "@185")
    issued = app.state.control_credentials.issue(
        name="release-operator", capabilities=["read", "operate"],
        workspace_ids=[one["id"]],
    )
    headers = {"Authorization": f"Bearer {issued['token']}"}
    monkeypatch.setattr(
        app.state.terminals, "current_pane_mode",
        lambda *_args: {"window": "@185", "pane": "%185", "dead": False, "in_mode": False},
    )
    control = app.state.terminal_control
    terminal_id = terminal["id"]
    base = f"/api/control/v1/workspaces/{one['id']}/terminals/{terminal_id}"
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as browser,
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as agent,
    ):
        await browser.post("/login", data={"password": "correct-password"})
        revision = control.external_state(terminal_id)["control_revision"]
        claimed = await agent.post(base + "/control/claim", headers=headers, json={
            "action_id": "release-claim-0001",
            "expected_tmux_window": "@185", "expected_pane": "%185",
            "expected_control_revision": revision,
        })
        assert claimed.status_code == 200, claimed.text
        before = control.external_state(terminal_id, issued["id"])
        assert before["generation"] == claimed.json()["generation"]

        lock = control.input_lock(terminal_id)
        await lock.acquire()
        pending = asyncio.create_task(agent.post(
            base + "/control/release", headers=headers, json={
                "action_id": "release-revoked-0001",
                "generation": claimed.json()["generation"],
                "expected_control_revision": claimed.json()["control_revision"],
                "expected_tmux_window": "@185",
            },
        ))
        try:
            row = None
            for _ in range(100):
                row = app.state.store.get_client_action(
                    issued["id"], "release-revoked-0001",
                    instance_id=app.state.control_instance_id,
                )
                if row is not None:
                    break
                await asyncio.sleep(0.02)
            assert row is not None and row["phase"] == "accepted"
            revoked = await browser.post(
                f"/api/client-credentials/{issued['id']}/revoke",
                json={"confirm_client_id": issued["id"]},
                headers={"X-Termroom-CSRF": settings.csrf_token},
            )
            assert revoked.status_code == 200, revoked.text
        finally:
            lock.release()
        refused = await asyncio.wait_for(pending, 5)
        assert refused.status_code == 409, refused.text
        assert refused.json()["status"] == "refused"
        assert refused.json()["code"] == "credential_revoked"
        assert control.external_state(terminal_id, issued["id"]) == before
        receipt = app.state.store.get_client_action(
            issued["id"], "release-revoked-0001",
            instance_id=app.state.control_instance_id,
        )
        assert receipt is not None and receipt["status"] == "refused"


@pytest.mark.asyncio
async def test_control_terminal_waiting_claim_refuses_revoked_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, app, one, _ = _control_test_app(tmp_path)
    terminal = app.state.store.create_terminal(one["id"], "shell", "@182")
    issued = app.state.control_credentials.issue(
        name="operator", capabilities=["read", "operate"], workspace_ids=[one["id"]],
    )
    headers = {"Authorization": f"Bearer {issued['token']}"}
    monkeypatch.setattr(
        app.state.terminals, "current_pane_mode",
        lambda *_args: {"window": "@182", "pane": "%182", "dead": False, "in_mode": False},
    )
    base = f"/api/control/v1/workspaces/{one['id']}/terminals/{terminal['id']}"
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as browser,
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as agent,
    ):
        await browser.post("/login", data={"password": "correct-password"})
        before = app.state.terminal_control.external_state(terminal["id"])
        lock = app.state.terminal_control.input_lock(terminal["id"])
        await lock.acquire()
        pending = asyncio.create_task(agent.post(
            base + "/control/claim", headers=headers, json={
                "action_id": "revocation-waiting-claim-0001",
                "expected_tmux_window": "@182", "expected_pane": "%182",
                "expected_control_revision": before["control_revision"],
            },
        ))
        try:
            row = None
            for _ in range(100):
                row = app.state.store.get_client_action(
                    issued["id"], "revocation-waiting-claim-0001",
                    instance_id=app.state.control_instance_id,
                )
                if row is not None:
                    break
                await asyncio.sleep(0.02)
            assert row is not None and row["phase"] == "accepted"
            revoked = await browser.post(
                f"/api/client-credentials/{issued['id']}/revoke",
                json={"confirm_client_id": issued["id"]},
                headers={"X-Termroom-CSRF": settings.csrf_token},
            )
            assert revoked.status_code == 200
        finally:
            lock.release()
        result = await asyncio.wait_for(pending, 5)
        assert result.status_code == 409, result.text
        assert result.json()["status"] == "refused"
        assert result.json()["code"] == "credential_revoked"
        assert app.state.terminal_control.external_state(terminal["id"]) == before


@pytest.mark.asyncio
async def test_control_terminal_create_and_rename_reconcile_exact_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, app, one, _ = _control_test_app(tmp_path)
    other = app.state.store.create_terminal(one["id"], "shell", "@82")
    issued = app.state.control_credentials.issue(
        name="operator", capabilities=["read", "operate"], workspace_ids=[one["id"]],
    )
    headers = {"Authorization": f"Bearer {issued['token']}"}
    created_ids: list[str] = []
    def create(_workspace, name):
        terminal = app.state.store.create_terminal(one["id"], name, "@83")
        created_ids.append(terminal["id"])
        return terminal
    def rename(_workspace, terminal, name):
        app.state.store.rename_terminal(terminal["id"], name)
        return app.state.store.get_terminal(terminal["id"])
    monkeypatch.setattr(app.state.terminals, "create_terminal", create)
    monkeypatch.setattr(app.state.terminals, "rename_terminal", rename)
    monkeypatch.setattr(
        app.state.terminals, "current_pane_mode",
        lambda _workspace, terminal: {"window": terminal["tmux_window"], "pane": "%83"},
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    url = f"/api/control/v1/workspaces/{one['id']}/terminals"
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as agent:
        created = await agent.post(url, headers=headers, json={
            "action_id": "terminal-create-0001", "name": "shell",
        })
        assert created.status_code == 200, created.text
        terminal_id = created.json()["terminal_id"]
        assert created_ids == [terminal_id]
        again = await agent.post(url, headers=headers, json={
            "action_id": "terminal-create-0001", "name": "shell",
        })
        assert again.status_code == 200
        assert created_ids == [terminal_id]
        wrong = await agent.post(
            url + f"/{terminal_id}/rename", headers=headers,
            json={"action_id": "terminal-rename-wrong", "name": "renamed",
                  "expected_tmux_window": "@82"},
        )
        assert wrong.status_code == 409
        assert wrong.json()["code"] == "terminal_identity_changed"
        renamed = await agent.post(
            url + f"/{terminal_id}/rename", headers=headers,
            json={"action_id": "terminal-rename-0001", "name": "renamed",
                  "expected_tmux_window": "@83"},
        )
        assert renamed.status_code == 200, renamed.text
        assert app.state.store.get_terminal(terminal_id)["name"] == "renamed"
        assert app.state.store.get_terminal(other["id"])["name"] == "shell"


@pytest.mark.asyncio
async def test_control_terminal_lifecycle_survives_lost_http_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, app, one, _ = _control_test_app(tmp_path)
    issued = app.state.control_credentials.issue(
        name="operator", capabilities=["read", "operate"], workspace_ids=[one["id"]],
    )
    headers = {"Authorization": f"Bearer {issued['token']}"}
    create_entered, create_release = threading.Event(), threading.Event()
    rename_entered, rename_release = threading.Event(), threading.Event()
    created_ids: list[str] = []
    renamed_ids: list[str] = []

    def create(_workspace, name):
        create_entered.set()
        assert create_release.wait(5)
        terminal = app.state.store.create_terminal(one["id"], name, "@91")
        created_ids.append(terminal["id"])
        return terminal

    def rename(_workspace, terminal, name):
        rename_entered.set()
        assert rename_release.wait(5)
        app.state.store.rename_terminal(terminal["id"], name)
        renamed_ids.append(terminal["id"])
        return app.state.store.get_terminal(terminal["id"])

    monkeypatch.setattr(app.state.terminals, "create_terminal", create)
    monkeypatch.setattr(app.state.terminals, "rename_terminal", rename)
    monkeypatch.setattr(
        app.state.terminals, "current_pane_mode",
        lambda _workspace, terminal: {
            "window": terminal["tmux_window"], "pane": "%91",
        },
    )
    base = f"/api/control/v1/workspaces/{one['id']}/terminals"
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver",
    ) as agent:
        async def observed(action_id: str) -> dict:
            result = {}
            for _ in range(100):
                response = await agent.get(
                    f"/api/control/v1/actions/{action_id}", headers=headers,
                )
                result = response.json()
                if result.get("status") == "observed":
                    return result
                await asyncio.sleep(0.03)
            pytest.fail(f"Operation did not settle: {result}")

        create_payload = {"action_id": "lifecycle-create-0001", "name": "shell"}
        create_request = asyncio.create_task(
            agent.post(base, headers=headers, json=create_payload)
        )
        try:
            assert await asyncio.wait_for(asyncio.to_thread(create_entered.wait, 3), 4)
            create_request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await create_request
            assert len(app.state.terminal_mutation_tasks) == 1
        finally:
            create_release.set()
        create_receipt = await observed(create_payload["action_id"])
        assert created_ids == [create_receipt["result_resource_id"]]
        again = await agent.post(base, headers=headers, json=create_payload)
        assert again.status_code == 200
        assert created_ids == [create_receipt["result_resource_id"]]

        terminal_id = created_ids[0]
        rename_url = base + f"/{terminal_id}/rename"
        rename_payload = {
            "action_id": "lifecycle-rename-0001", "name": "renamed",
            "expected_tmux_window": "@91",
        }
        rename_request = asyncio.create_task(
            agent.post(rename_url, headers=headers, json=rename_payload)
        )
        try:
            assert await asyncio.wait_for(asyncio.to_thread(rename_entered.wait, 3), 4)
            assert app.state.terminal_control.input_lock(terminal_id).locked()
            rename_request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await rename_request
            assert app.state.terminal_control.input_lock(terminal_id).locked()
            assert len(app.state.terminal_mutation_tasks) == 1
        finally:
            rename_release.set()
        rename_receipt = await observed(rename_payload["action_id"])
        assert rename_receipt["result_resource_id"] == terminal_id
        assert app.state.store.get_terminal(terminal_id)["name"] == "renamed"
        again = await agent.post(rename_url, headers=headers, json=rename_payload)
        assert again.status_code == 200
        assert renamed_ids == [terminal_id]
        assert not app.state.terminal_mutation_tasks


@pytest.mark.asyncio
async def test_control_terminal_snapshot_uses_observer_without_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, app, one, two = _control_test_app(tmp_path)
    terminal = app.state.store.create_terminal(one["id"], "shell", "@14")
    foreign_terminal = app.state.store.create_terminal(two["id"], "other", "@25")
    issued = app.state.control_credentials.issue(
        name="observer", capabilities=["read"], workspace_ids=[one["id"]],
    )
    calls: list[tuple[str, str, int]] = []

    def no_attach(*args, **kwargs):
        raise AssertionError("Read endpoint tried to reconcile/attach a Terminal")

    def fake_observe(workspace, item, *, lines):
        calls.append((workspace["id"], item["id"], lines))
        return "synthetic pane output\n", {"pane": "%14", "window": "@14", "dead": False}

    monkeypatch.setattr(app.state.terminals, "ensure_workspace", no_attach)
    monkeypatch.setattr(app.state.terminals, "observe_scrollback", fake_observe)
    headers = {"Authorization": f"Bearer {issued['token']}"}
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver",
    ) as agent:
        listed = await agent.get(
            f"/api/control/v1/workspaces/{one['id']}/terminals", headers=headers,
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["observation"] == "registered_not_live_verified"
        assert [item["id"] for item in listed.json()["terminals"]] == [terminal["id"]]
        detail = await agent.get(
            f"/api/control/v1/workspaces/{one['id']}/terminals/{terminal['id']}",
            headers=headers,
        )
        assert detail.status_code == 200
        assert detail.json()["terminal"]["id"] == terminal["id"]
        assert detail.json()["observation"] == "registered_not_live_verified"
        snapshot = await agent.get(
            f"/api/control/v1/workspaces/{one['id']}/terminals/{terminal['id']}/snapshot",
            params={"lines": "12"}, headers=headers,
        )
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["snapshot"] == "synthetic pane output\n"
        assert snapshot.json()["pane"] == "%14"
        assert snapshot.json()["continuity"] == "snapshot_only_no_event_cursor"
        assert calls == [(one["id"], terminal["id"], 12)]
        invalid = await agent.get(
            f"/api/control/v1/workspaces/{one['id']}/terminals/{terminal['id']}/snapshot",
            params={"lines": "10000"}, headers=headers,
        )
        assert invalid.status_code == 400
        oversized = await agent.get(
            f"/api/control/v1/workspaces/{one['id']}/terminals/{terminal['id']}/snapshot",
            params={"lines": "9" * 5000}, headers=headers,
        )
        assert oversized.status_code == 400
        forbidden = await agent.get(
            f"/api/control/v1/workspaces/{one['id']}/terminals/{foreign_terminal['id']}/snapshot",
            headers=headers,
        )
        missing = await agent.get(
            f"/api/control/v1/workspaces/{one['id']}/terminals/missing/snapshot",
            headers=headers,
        )
        assert forbidden.status_code == missing.status_code == 404
        assert forbidden.json() == missing.json()
    assert len(calls) == 1
@pytest.mark.asyncio
async def test_control_run_reads_are_persisted_scoped_and_paginated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, app, one, two = _control_test_app(tmp_path)
    store = app.state.store
    for index in range(27):
        with store.connect() as db:
            db.execute(
                "INSERT INTO file_runs("
                "id, workspace_id, idempotency_key, relative_path, source_digest, "
                "runner_id, runner_version, argv_json, state, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'finished', ?)",
                (
                    str(uuid.uuid4()), one["id"], f"key-{index}", "example.py",
                    "sensitive-source-digest", "python", 1, '["--secret"]',
                    f"2026-11-08T00:00:{index:02d}+00:00",
                ),
            )
    for index in range(3):
        assert store.claim_workspace_command(
            one["id"], str(uuid.uuid4()), index % 2, f"digest-{index}"
        ) == "created"
    assert store.claim_workspace_command(
        two["id"], str(uuid.uuid4()), 0, "other"
    ) == "created"

    host_b = store.create_computer(
        name="B", ssh_alias="", host="b.example.invalid", port=22,
        username="synthetic", identity_file="", host_key_type="ssh-ed25519",
        host_key_data="fake", host_fingerprint="fake-B",
    )
    host_c = store.create_computer(
        name="C", ssh_alias="", host="c.example.invalid", port=22,
        username="synthetic", identity_file="", host_key_type="ssh-ed25519",
        host_key_data="fake", host_fingerprint="fake-C",
    )
    granted_target = app.state.workspaces.open_remote(
        host_b["id"], "/home/synthetic/project"
    )
    remote_ids = {}
    for label, source, host in (
        ("allowed", one["id"], host_b),
        ("foreign-source", two["id"], host_b),
        ("foreign-target", one["id"], host_c),
    ):
        run_id = str(uuid.uuid4())
        remote_ids[label] = run_id
        store.create_remote_run({
            "id": run_id, "source_kind": "workspace",
            "source_workspace_id": source, "source_label": "secret-label",
            "target_computer_id": host["id"], "command": "cat /secret",
            "run_base": "/private/remote/run", "state": "finished",
            "source_options_json": "{}",
            "created_at": "2026-11-08T01:00:00+00:00",
            "expires_at": "2027-11-08T01:00:00+00:00",
        })

    credential = app.state.control_credentials.issue(
        name="scoped-observer", capabilities=["read"],
        workspace_ids=[one["id"], granted_target["id"]],
    )
    headers = {"Authorization": f"Bearer {credential['token']}"}
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    monkeypatch.setattr(
        app.state.remote_runs, "get",
        lambda *args: pytest.fail("Control read called live Remote Run manager"),
    )
    monkeypatch.setattr(
        app.state.file_runs, "get",
        lambda *args: pytest.fail("Control read called live File Run manager"),
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver",
    ) as agent:
        base = f"/api/control/v1/workspaces/{one['id']}"
        first = await agent.get(f"{base}/file-runs", headers=headers)
        assert first.status_code == 200, first.text
        assert len(first.json()["items"]) == 25
        assert first.json()["next_cursor"] is not None
        assert first.json()["freshness"] == "persisted_without_live_poll"
        assert "--secret" not in first.text
        second = await agent.get(
            f"{base}/file-runs", params={"before": first.json()["next_cursor"]},
            headers=headers,
        )
        assert second.status_code == 200, second.text
        assert len(second.json()["items"]) == 2
        assert second.json()["next_cursor"] is None
        file_id = first.json()["items"][0]["id"]
        detail = await agent.get(f"{base}/file-runs/{file_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["run"]["relative_path"] == "example.py"
        assert "argv_json" not in detail.text
        expired = await agent.get(
            f"{base}/file-runs",
            params={"before": str(uuid.uuid4())}, headers=headers,
        )
        assert expired.status_code == 410
        assert expired.json()["code"] == "page_expired"

        claims = await agent.get(f"{base}/workspace-runs", headers=headers)
        assert claims.status_code == 200
        assert len(claims.json()["items"]) == 3
        assert all(
            record["state"] == "admission_recorded_execution_outcome_unknown"
            for record in claims.json()["items"]
        )
        admitted = claims.json()["items"][0]["launch_id"]
        claim_detail = await agent.get(
            f"{base}/workspace-runs/{admitted}", headers=headers,
        )
        assert claim_detail.status_code == 200
        assert "execution_outcome_unknown" in claim_detail.text

        run_list = await agent.get(f"{base}/remote-runs", headers=headers)
        assert run_list.status_code == 200, run_list.text
        assert [item["id"] for item in run_list.json()["items"]] == [
            remote_ids["allowed"]
        ]
        assert "cat /secret" not in run_list.text
        assert "/private/remote/run" not in run_list.text
        remote_detail = await agent.get(
            f"{base}/remote-runs/{remote_ids['allowed']}", headers=headers,
        )
        assert remote_detail.status_code == 200
        assert remote_detail.json()["review"]["state"] == "no_recorded_report"
        snapshot_path = f"{base}/remote-runs/{remote_ids['allowed']}/snapshot"
        absent_snapshot = await agent.get(snapshot_path, headers=headers)
        assert absent_snapshot.status_code == 404

        run = store.get_remote_run(remote_ids["allowed"])
        assert run is not None
        transient = app.state.workspaces.open_remote_run(
            run, f"termroom-run-{run['id']}",
            f"/private/remote/run/{run['id']}/work",
        )
        slot = store.create_terminal(
            transient["id"], "Run", "@17", role="remote_run",
            managed_run_id=run["id"],
        )
        before_workspace = store.get_workspace(transient["id"])
        before_terminal = store.get_terminal(slot["id"])
        calls: list[tuple[str, str, int]] = []

        async def observe_managed(workspace, terminal, *, lines):
            calls.append((workspace["id"], terminal["id"], lines))
            return "Remote Run output\n", {"pane": "%17", "dead": False}

        monkeypatch.setattr(app.state.remote, "observe_scrollback", observe_managed)
        monkeypatch.setattr(
            app.state.remote.ssh, "ensure_workspace",
            lambda *args, **kwargs: pytest.fail("Read attached SSH workspace"),
        )
        good_snapshot = await agent.get(
            snapshot_path, params={"lines": "12"}, headers=headers,
        )
        assert good_snapshot.status_code == 200, good_snapshot.text
        assert good_snapshot.json()["snapshot"] == "Remote Run output\n"
        assert good_snapshot.json()["pane_identity_verified"] is True
        assert good_snapshot.json()["run_id"] == run["id"]
        assert calls == [(transient["id"], slot["id"], 12)]
        assert store.get_workspace(transient["id"]) == before_workspace
        assert store.get_terminal(slot["id"]) == before_terminal

        source_only = app.state.control_credentials.issue(
            name="source-without-target", capabilities=["read"],
            workspace_ids=[one["id"]],
        )
        denied_target = await agent.get(
            snapshot_path,
            headers={"Authorization": f"Bearer {source_only['token']}"},
        )
        assert denied_target.status_code == 404
        invalid_lines = await agent.get(
            snapshot_path, params={"lines": "201"}, headers=headers,
        )
        assert invalid_lines.status_code == 400
        oversized_lines = await agent.get(
            snapshot_path, params={"lines": "9" * 5000}, headers=headers,
        )
        assert oversized_lines.status_code == 400
        with store.connect() as db:
            db.execute(
                "UPDATE terminals SET managed_run_id = ? WHERE id = ?",
                (remote_ids["foreign-source"], slot["id"]),
            )
        mismatched_slot = await agent.get(snapshot_path, headers=headers)
        assert mismatched_slot.status_code == 404
        assert calls == [(transient["id"], slot["id"], 12)]
        for label in ("foreign-source", "foreign-target"):
            denied = await agent.get(
                f"{base}/remote-runs/{remote_ids[label]}", headers=headers,
            )
            assert denied.status_code == 404
            assert denied.json()["code"] == "resource_unavailable"
            denied_snapshot = await agent.get(
                f"{base}/remote-runs/{remote_ids[label]}/snapshot",
                headers=headers,
            )
            assert denied_snapshot.status_code == 404
        denied_run = await agent.get(
            f"/api/control/v1/workspaces/{two['id']}/file-runs", headers=headers,
        )
        assert denied_run.status_code == 404


@pytest.mark.asyncio
async def test_control_read_gate_limits_parallel_terminal_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, app, one, _ = _control_test_app(tmp_path)
    terminal = app.state.store.create_terminal(one["id"], "shell", "@13")
    tokens = [
        app.state.control_credentials.issue(
            name=f"concurrent-{index}", capabilities=["read"],
            workspace_ids=[one["id"]],
        )["token"]
        for index in range(2)
    ]
    lock = threading.Lock()
    active = 0
    peak = 0

    def fake_observe(workspace, row, *, lines):
        nonlocal active, peak
        assert row["id"] == terminal["id"]
        assert workspace["id"] == one["id"] and lines == 5
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.04)
        with lock:
            active -= 1
        return "bounded", {"pane": "%13", "window": "@13", "dead": False}

    monkeypatch.setattr(app.state.terminals, "observe_scrollback", fake_observe)
    monkeypatch.setattr(
        app.state.terminals, "ensure_workspace",
        lambda *args, **kwargs: pytest.fail("Observer changed tmux state"),
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://testserver",
    ) as agent:
        responses = await asyncio.gather(*(
            agent.get(
                f"/api/control/v1/workspaces/{one['id']}/terminals/{terminal['id']}/snapshot",
                params={"lines": "5"},
                headers={"Authorization": f"Bearer {tokens[index % 2]}"},
            )
            for index in range(12)
        ))
    assert all(item.status_code == 200 for item in responses)
    assert 1 <= peak <= 4
    assert active == 0


@pytest.mark.asyncio
async def test_control_read_waiting_for_slot_refuses_revoked_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, app, one, _ = _control_test_app(tmp_path)
    terminal = app.state.store.create_terminal(one["id"], "shell", "@13")
    holders = [
        app.state.control_credentials.issue(
            name=f"read-holder-{index}", capabilities=["read"],
            workspace_ids=[one["id"]],
        )
        for index in range(4)
    ]
    waiting = app.state.control_credentials.issue(
        name="read-waiter", capabilities=["read"], workspace_ids=[one["id"]],
    )
    started = asyncio.Event()
    waiter_at_workspace = asyncio.Event()
    release = threading.Event()
    lock = threading.Lock()
    loop = asyncio.get_running_loop()
    reads = 0

    def fake_observe(workspace, row, *, lines):
        nonlocal reads
        assert workspace["id"] == one["id"]
        assert row["id"] == terminal["id"] and lines == 5
        with lock:
            reads += 1
            if reads == 4:
                loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "Held observer did not get released"
        return "bounded", {"pane": "%13", "window": "@13", "dead": False}

    original_credential = app.state.store.get_client_credential
    waiter_credential_reads = 0

    def observed_credential(client_id):
        nonlocal waiter_credential_reads
        row = original_credential(client_id)
        if client_id == waiting["id"]:
            waiter_credential_reads += 1
            if waiter_credential_reads == 2:
                # Auth middleware checked once; the Workspace admission
                # check is the second read, before waiting for the read slot.
                loop.call_soon(waiter_at_workspace.set)
        return row

    monkeypatch.setattr(app.state.store, "get_client_credential", observed_credential)
    monkeypatch.setattr(app.state.terminals, "observe_scrollback", fake_observe)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    url = (
        f"/api/control/v1/workspaces/{one['id']}"
        f"/terminals/{terminal['id']}/snapshot"
    )
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as agent,
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as browser,
    ):
        await browser.post("/login", data={"password": "correct-password"})
        held = [
            asyncio.create_task(agent.get(
                url, params={"lines": "5"},
                headers={"Authorization": f"Bearer {item['token']}"},
            ))
            for item in holders
        ]
        queued = None
        try:
            await asyncio.wait_for(started.wait(), 5)
            queued = asyncio.create_task(agent.get(
                url, params={"lines": "5"},
                headers={"Authorization": f"Bearer {waiting['token']}"},
            ))
            await asyncio.wait_for(waiter_at_workspace.wait(), 5)
            revoked = await browser.post(
                f"/api/client-credentials/{waiting['id']}/revoke",
                json={"confirm_client_id": waiting["id"]},
                headers={"X-Termroom-CSRF": settings.csrf_token},
            )
            assert revoked.status_code == 200, revoked.text
        finally:
            release.set()
        responses = await asyncio.wait_for(
            asyncio.gather(*held, queued), 5,
        )
    assert all(item.status_code == 200 for item in responses[:4])
    assert responses[4].status_code == 409, responses[4].text
    assert responses[4].json()["code"] == "credential_revoked"
    assert reads == 4


@pytest.mark.asyncio
async def test_control_inflight_read_discards_result_after_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, app, one, _ = _control_test_app(tmp_path)
    terminal = app.state.store.create_terminal(one["id"], "shell", "@13")
    issued = app.state.control_credentials.issue(
        name="inflight-reader", capabilities=["read"], workspace_ids=[one["id"]],
    )
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def held_observe(workspace, row, *, lines):
        assert workspace["id"] == one["id"]
        assert row["id"] == terminal["id"] and lines == 5
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5), "Held observer did not get released"
        return "private-read-result", {"pane": "%13", "window": "@13", "dead": False}

    monkeypatch.setattr(app.state.terminals, "observe_scrollback", held_observe)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    url = (
        f"/api/control/v1/workspaces/{one['id']}"
        f"/terminals/{terminal['id']}/snapshot"
    )
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as agent,
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as browser,
    ):
        await browser.post("/login", data={"password": "correct-password"})
        pending = asyncio.create_task(agent.get(
            url, params={"lines": "5"},
            headers={"Authorization": f"Bearer {issued['token']}"},
        ))
        try:
            await asyncio.wait_for(started.wait(), 5)
            revoked = await browser.post(
                f"/api/client-credentials/{issued['id']}/revoke",
                json={"confirm_client_id": issued["id"]},
                headers={"X-Termroom-CSRF": settings.csrf_token},
            )
            assert revoked.status_code == 200, revoked.text
        finally:
            release.set()
        response = await asyncio.wait_for(pending, 5)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "credential_revoked"
    assert "private-read-result" not in response.text
