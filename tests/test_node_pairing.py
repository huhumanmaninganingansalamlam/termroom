from __future__ import annotations

import json
import re
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from termroom.app import create_app
from termroom.config import Settings
from termroom.db import StateStore
from termroom.node_agent import NodeAgent, NodeAgentError, load_node_config, pair_node
from termroom.node_protocol import (
    NODE_PROTOCOL_VERSION,
    generate_pairing_code,
    generate_private_key,
    pairing_code_digest,
    public_key_fingerprint,
    public_key_text,
    secret_digest,
    sign_challenge,
)


def _expires_at(*, seconds: int = 600) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _new_store(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    return store


@pytest.mark.asyncio
async def test_node_pairing_persists_and_reuses_custom_ca_context(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    allowed = tmp_path / "projects"
    allowed.mkdir()
    ca_file = tmp_path / "core-ca.pem"
    ca_file.write_text("test bundle", encoding="utf-8")
    trusted_context = ssl.create_default_context()
    context_builds: list[str | None] = []
    pairing_contexts: list[ssl.SSLContext | None] = []

    def create_context(*, cafile=None):  # type: ignore[no-untyped-def]
        context_builds.append(cafile)
        return trusted_context

    def post(_core, path, _payload, *, ssl_context=None):  # type: ignore[no-untyped-def]
        pairing_contexts.append(ssl_context)
        if path == "/api/node/enroll":
            return {"enrollment_id": "enrollment"}
        return {"status": "approved", "node_id": "a" * 32}

    monkeypatch.setattr("termroom.node_agent.ssl.create_default_context", create_context)
    monkeypatch.setattr("termroom.node_agent._json_post", post)
    state_dir = tmp_path / "state"
    config = pair_node(
        state_dir=state_dir,
        core_url="https://core.example",
        code="one-time-code",
        allowed_roots=[allowed],
        ca_file=ca_file,
    )

    assert pairing_contexts == [trusted_context, trusted_context]
    assert context_builds == [str(ca_file)]
    assert config.ca_file == ca_file
    assert load_node_config(state_dir).ca_file == ca_file

    agent = NodeAgent(config, object())
    assert agent._ssl_context is trusted_context
    assert context_builds == [str(ca_file), str(ca_file)]

    connection_options: dict[str, object] = {}

    class _ConnectionProbe:
        async def __aenter__(self):
            raise RuntimeError("connection inspected")

        async def __aexit__(self, *_args):  # type: ignore[no-untyped-def]
            return False

    def connect(_url, **options):  # type: ignore[no-untyped-def]
        connection_options.update(options)
        return _ConnectionProbe()

    monkeypatch.setattr("termroom.node_agent.connect", connect)
    with pytest.raises(RuntimeError, match="connection inspected"):
        await agent.run_once()
    assert connection_options["ssl"] is trusted_context


def test_node_custom_ca_rejects_missing_or_invalid_bundle(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "projects"
    allowed.mkdir()

    with pytest.raises(NodeAgentError) as missing:
        pair_node(
            state_dir=tmp_path / "state",
            core_url="https://core.example",
            code="code",
            allowed_roots=[allowed],
            ca_file=tmp_path / "missing.pem",
        )
    assert missing.value.code == "ca_file_invalid"

    invalid = tmp_path / "invalid.pem"
    invalid.write_text("not a certificate", encoding="utf-8")
    with pytest.raises(NodeAgentError) as invalid_bundle:
        pair_node(
            state_dir=tmp_path / "other-state",
            core_url="https://core.example",
            code="code",
            allowed_roots=[allowed],
            ca_file=invalid,
        )
    assert invalid_bundle.value.code == "ca_file_invalid"

    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("not read for HTTP validation", encoding="utf-8")
    with pytest.raises(NodeAgentError) as insecure:
        pair_node(
            state_dir=tmp_path / "http-state",
            core_url="http://127.0.0.1:8765",
            code="code",
            allowed_roots=[allowed],
            ca_file=ca_file,
        )
    assert insecure.value.code == "ca_file_requires_https"


def test_node_pairing_is_single_use_approved_and_revocable(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    code = generate_pairing_code()
    pairing = store.create_node_pairing_code(
        code_hash=pairing_code_digest(code), expires_at=_expires_at()
    )
    private_key = generate_private_key()
    public_key = public_key_text(private_key.public_key())
    polling_secret = "poll-secret-do-not-store"

    enrollment = store.submit_node_enrollment(
        code_hash=pairing_code_digest(code),
        name="GPU Node",
        public_key=public_key,
        fingerprint=public_key_fingerprint(public_key),
        protocol_version=NODE_PROTOCOL_VERSION,
        polling_secret_hash=secret_digest(polling_secret),
    )

    assert enrollment is not None
    assert enrollment["status"] == "pending"
    assert store.submit_node_enrollment(
        code_hash=pairing_code_digest(code),
        name="Replay",
        public_key=public_key_text(generate_private_key().public_key()),
        fingerprint="SHA256:replay",
        protocol_version=NODE_PROTOCOL_VERSION,
        polling_secret_hash=secret_digest("replay"),
    ) is None
    assert store.get_node_enrollment(
        str(enrollment["id"]), polling_secret_hash=secret_digest("wrong")
    ) is None
    pending = store.get_node_pairing(str(pairing["id"]))
    assert pending is not None
    assert pending["fingerprint"] == public_key_fingerprint(public_key)

    computer = store.approve_node_enrollment(str(enrollment["id"]))
    assert computer["connection_method"] == "node"
    assert computer["node_public_key"] == public_key
    assert computer["node_fingerprint"] == public_key_fingerprint(public_key)
    assert store.approve_node_enrollment(str(enrollment["id"]))["id"] == computer["id"]
    approved = store.get_node_enrollment(
        str(enrollment["id"]), polling_secret_hash=secret_digest(polling_secret)
    )
    assert approved is not None
    assert approved["status"] == "approved"
    assert approved["computer_id"] == computer["id"]

    store.update_node_connection(
        str(computer["id"]),
        protocol_version=NODE_PROTOCOL_VERSION,
        capabilities=("files", "terminal", "workspace"),
    )
    connected = store.get_computer(str(computer["id"]))
    assert connected is not None
    assert json.loads(connected["node_capabilities_json"]) == [
        "files",
        "terminal",
        "workspace",
    ]
    assert connected["last_seen_at"]

    revoked = store.revoke_node(str(computer["id"]))
    assert revoked["node_revoked_at"]
    try:
        store.touch_node(str(computer["id"]))
    except KeyError:
        pass
    else:  # pragma: no cover - assertion branch
        raise AssertionError("revoked Node heartbeat was accepted")

    with store.connect() as db:
        persisted = "\n".join(
            str(value)
            for row in db.execute(
                "SELECT code_hash, created_at, expires_at, consumed_at FROM node_pairing_codes"
            )
            for value in row
            if value is not None
        )
        persisted += "\n" + "\n".join(
            str(value)
            for row in db.execute(
                "SELECT polling_secret_hash, public_key, fingerprint FROM node_enrollments"
            )
            for value in row
            if value is not None
        )
    assert code not in persisted
    assert polling_secret not in persisted


def test_expired_pairing_code_and_duplicate_identity_are_rejected(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    expired_code = generate_pairing_code()
    store.create_node_pairing_code(
        code_hash=pairing_code_digest(expired_code), expires_at=_expires_at(seconds=-1)
    )
    public_key = public_key_text(generate_private_key().public_key())
    assert store.submit_node_enrollment(
        code_hash=pairing_code_digest(expired_code),
        name="Expired",
        public_key=public_key,
        fingerprint=public_key_fingerprint(public_key),
        protocol_version=NODE_PROTOCOL_VERSION,
        polling_secret_hash=secret_digest("expired"),
    ) is None

    first_code = generate_pairing_code()
    first_pairing = store.create_node_pairing_code(
        code_hash=pairing_code_digest(first_code), expires_at=_expires_at()
    )
    first = store.submit_node_enrollment(
        code_hash=pairing_code_digest(first_code),
        name="First",
        public_key=public_key,
        fingerprint=public_key_fingerprint(public_key),
        protocol_version=NODE_PROTOCOL_VERSION,
        polling_secret_hash=secret_digest("first"),
    )
    assert first is not None
    assert store.get_node_pairing(str(first_pairing["id"]))["status"] == "pending"
    store.approve_node_enrollment(str(first["id"]))

    second_code = generate_pairing_code()
    store.create_node_pairing_code(
        code_hash=pairing_code_digest(second_code), expires_at=_expires_at()
    )
    assert store.submit_node_enrollment(
        code_hash=pairing_code_digest(second_code),
        name="Duplicate",
        public_key=public_key,
        fingerprint=public_key_fingerprint(public_key),
        protocol_version=NODE_PROTOCOL_VERSION,
        polling_secret_hash=secret_digest("second"),
    ) is None


def test_node_http_pairing_control_authentication_and_revocation(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
        login_password="test-password",
    )
    app = create_app(settings)
    private_key = generate_private_key()
    public_key = public_key_text(private_key.public_key())
    fingerprint = public_key_fingerprint(public_key)
    polling_secret = "polling-secret-with-at-least-32-bytes"

    with TestClient(app) as client:
        assert client.post("/login", data={"password": "test-password"}).status_code == 200
        created = client.post(
            "/computers/node/pair",
            data={"_csrf": settings.csrf_token},
        )
        assert created.status_code == 201
        code_match = re.search(r'name="code" value="([^"]+)"', created.text)
        pairing_match = re.search(r'name="pairing_id" value="([a-f0-9]{32})"', created.text)
        assert code_match is not None
        assert pairing_match is not None

        enrolled = client.post(
            "/api/node/enroll",
            json={
                "code": code_match.group(1),
                "name": "Separate Node",
                "public_key": public_key,
                "fingerprint": fingerprint,
                "protocol_version": NODE_PROTOCOL_VERSION,
                "polling_secret": polling_secret,
            },
        )
        assert enrolled.status_code == 202
        enrollment_id = enrolled.json()["enrollment_id"]
        pending = client.post(
            "/api/node/enroll/status",
            json={
                "enrollment_id": enrollment_id,
                "polling_secret": polling_secret,
            },
        )
        assert pending.json() == {"ok": True, "status": "pending", "node_id": None}
        assert client.post(
            "/api/node/enroll/status",
            json={"enrollment_id": enrollment_id, "polling_secret": "wrong"},
        ).status_code == 404

        review = client.get(
            "/computers/node/pair", params={"pairing_id": pairing_match.group(1)}
        )
        assert review.status_code == 200
        assert fingerprint in review.text
        approved = client.post(
            f"/computers/node/pair/{enrollment_id}/approve",
            data={"_csrf": settings.csrf_token},
            follow_redirects=False,
        )
        assert approved.status_code == 303
        node_id = approved.headers["location"].split("/")[2].split("?")[0]
        status = client.post(
            "/api/node/enroll/status",
            json={
                "enrollment_id": enrollment_id,
                "polling_secret": polling_secret,
            },
        )
        assert status.json() == {"ok": True, "status": "approved", "node_id": node_id}
        detail = client.get(approved.headers["location"])
        assert detail.status_code == 200
        hub = client.get("/open")
        assert "Separate Node" in hub.text
        assert "Offline" in hub.text
        remote_page = client.get(f"/open/{node_id}")
        assert remote_page.status_code == 200
        assert "Offline" in remote_page.text
        assert f"target_computer_id={node_id}" not in remote_page.text
        assert "<code>@</code>" not in remote_page.text
        offline_browse = client.get(f"/api/computers/{node_id}/browse-directories")
        assert offline_browse.status_code == 400
        assert "offline" in offline_browse.json()["error"].lower()

        with client.websocket_connect(f"/api/node/control?node_id={node_id}") as websocket:
            challenge = json.loads(websocket.receive_text())
            assert challenge["type"] == "auth.challenge"
            websocket.send_text(
                json.dumps(
                    {
                        "type": "auth.response",
                        "node_id": node_id,
                        "signature": sign_challenge(
                            private_key, node_id, challenge["nonce"]
                        ),
                        "protocol_version": NODE_PROTOCOL_VERSION,
                        "capabilities": ["workspace", "terminal", "files"],
                    }
                )
            )
            assert json.loads(websocket.receive_text()) == {
                "type": "auth.ok",
                "node_id": node_id,
            }
            websocket.send_text(json.dumps({"type": "heartbeat"}))
            assert json.loads(websocket.receive_text()) == {"type": "heartbeat.ack"}
            assert app.state.node_core.status(node_id).online
            assert "<small>Online</small>" in client.get("/open").text

            revoked = client.post(
                f"/computers/{node_id}/revoke",
                data={"_csrf": settings.csrf_token},
                follow_redirects=False,
            )
            assert revoked.status_code == 303
            closed = websocket.receive()
            assert closed["type"] == "websocket.close"
            assert closed["code"] == 4403

        computer = app.state.store.get_computer(node_id)
        assert computer is not None
        assert computer["node_revoked_at"]
        assert not app.state.node_core.status(node_id).online
        revoked_browse = client.get(f"/api/computers/{node_id}/browse-directories")
        assert revoked_browse.status_code == 400
        assert "revoked" in revoked_browse.json()["error"].lower()

        with client.websocket_connect(
            f"/api/node/control?node_id={node_id}"
        ) as rejected_socket:
            closed = rejected_socket.receive()
            assert closed["type"] == "websocket.close"
            assert closed["code"] == 4401


def test_node_control_rejects_missing_required_capabilities(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings.create(
        root,
        state_dir=tmp_path / "state",
        access_token="test-token",
        login_password="test-password",
    )
    app = create_app(settings)
    private_key = generate_private_key()
    public_key = public_key_text(private_key.public_key())
    code = generate_pairing_code()
    app.state.store.create_node_pairing_code(
        code_hash=pairing_code_digest(code), expires_at=_expires_at()
    )
    enrollment = app.state.store.submit_node_enrollment(
        code_hash=pairing_code_digest(code),
        name="Incomplete Node",
        public_key=public_key,
        fingerprint=public_key_fingerprint(public_key),
        protocol_version=NODE_PROTOCOL_VERSION,
        polling_secret_hash=secret_digest("capability-polling-secret"),
    )
    assert enrollment is not None
    computer = app.state.store.approve_node_enrollment(str(enrollment["id"]))
    node_id = str(computer["id"])

    with TestClient(app) as client:
        with client.websocket_connect(
            f"/api/node/control?node_id={node_id}"
        ) as websocket:
            challenge = json.loads(websocket.receive_text())
            websocket.send_text(
                json.dumps(
                    {
                        "type": "auth.response",
                        "node_id": node_id,
                        "signature": sign_challenge(
                            private_key, node_id, challenge["nonce"]
                        ),
                        "protocol_version": NODE_PROTOCOL_VERSION,
                        "capabilities": ["workspace", "files"],
                    }
                )
            )
            closed = websocket.receive()
            assert closed["type"] == "websocket.close"
            assert closed["code"] == 4401

        assert not app.state.node_core.status(node_id).online
        persisted = app.state.store.get_computer(node_id)
        assert persisted is not None
        assert json.loads(persisted["node_capabilities_json"]) == []
