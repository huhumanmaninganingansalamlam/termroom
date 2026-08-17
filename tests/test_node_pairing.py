from __future__ import annotations

import asyncio
import gc
import json
import re
import ssl
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from termroom.app import create_app
from termroom.config import Settings
from termroom.db import StateStore
from termroom.node_agent import (
    NodeAgent,
    NodeAgentError,
    NodeConfig,
    load_node_config,
    pair_node,
)
from termroom.node_core import NodeConnection, NodeCore, NodeUnavailableError
from termroom.node_protocol import (
    NODE_PROTOCOL_VERSION,
    NODE_REQUEST_MAX_BUDGET_MS,
    NodeProtocolError,
    generate_pairing_code,
    generate_private_key,
    pairing_code_digest,
    public_key_fingerprint,
    public_key_text,
    request_budget_ms,
    secret_digest,
    sign_challenge,
    validate_request_budget_ms,
)


def _expires_at(*, seconds: int = 600) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _new_store(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    return store


def test_node_request_budgets_are_relative_bounded_and_strict() -> None:
    assert request_budget_ms(30) == 30_000
    assert validate_request_budget_ms(30_000) == 30.0

    for invalid_timeout in (True, 0, 301, float("nan"), float("inf")):
        with pytest.raises(NodeProtocolError) as invalid:
            request_budget_ms(invalid_timeout)  # type: ignore[arg-type]
        assert invalid.value.code == "budget_invalid"

    for invalid_budget in (True, 1.0, "1000", 0, NODE_REQUEST_MAX_BUDGET_MS + 1):
        with pytest.raises(NodeProtocolError) as invalid:
            validate_request_budget_ms(invalid_budget)
        assert invalid.value.code == "budget_invalid"


@pytest.mark.asyncio
async def test_node_request_envelope_carries_version_relative_budget_and_id() -> None:
    sent: list[dict[str, object]] = []

    class FakeWebSocket:
        async def send_text(self, raw: str) -> None:
            sent.append(json.loads(raw))

    connection = NodeConnection(FakeWebSocket(), "a" * 32)  # type: ignore[arg-type]
    pending = asyncio.create_task(connection.request("workspace.roots", {}, admission_timeout=5))
    await asyncio.sleep(0)

    assert len(sent) == 1
    request = sent[0]
    assert request["type"] == "request"
    assert request["operation"] == "workspace.roots"
    assert request["protocol_version"] == NODE_PROTOCOL_VERSION
    assert request["budget_ms"] == 5_000
    assert "deadline_ms" not in request
    request_id = str(request["id"])

    await connection.dispatch(
        {"type": "response", "id": request_id, "ok": True, "result": {"roots": []}}
    )
    assert await pending == {"roots": []}


@pytest.mark.asyncio
async def test_node_connection_ignores_late_frames_for_a_locally_closed_stream() -> None:
    sent: list[dict[str, object]] = []

    class FakeWebSocket:
        async def send_text(self, raw: str) -> None:
            sent.append(json.loads(raw))

    connection = NodeConnection(FakeWebSocket(), "a" * 32)  # type: ignore[arg-type]
    opening = asyncio.create_task(connection.open_stream("files.download.open", {}))
    await asyncio.sleep(0)
    request = sent[0]
    request_id = str(request["id"])
    stream_id = str(request["payload"]["stream_id"])  # type: ignore[index]
    await connection.dispatch(
        {
            "type": "response",
            "id": request_id,
            "ok": True,
            "result": {"stream_id": stream_id},
        }
    )
    _, stream = await opening

    await stream.close()
    await connection.dispatch({"type": "stream.data", "stream_id": stream_id, "data": ""})

    with pytest.raises(NodeProtocolError, match="Unknown Node stream"):
        await connection.dispatch({"type": "stream.data", "stream_id": "b" * 32, "data": ""})


@pytest.mark.asyncio
async def test_node_connection_ignores_a_response_after_its_caller_cancels() -> None:
    sent: list[dict[str, object]] = []

    class FakeWebSocket:
        async def send_text(self, raw: str) -> None:
            sent.append(json.loads(raw))

    connection = NodeConnection(FakeWebSocket(), "a" * 32)  # type: ignore[arg-type]
    pending = asyncio.create_task(connection.request("workspace.roots", {}))
    await asyncio.sleep(0)
    pending.cancel()
    request_id = str(sent[0]["id"])
    await connection.dispatch(
        {"type": "response", "id": request_id, "ok": True, "result": {"roots": []}}
    )
    with pytest.raises(asyncio.CancelledError):
        await pending

    next_request = asyncio.create_task(connection.request("workspace.roots", {}))
    await asyncio.sleep(0)
    next_request_id = str(sent[1]["id"])
    await connection.dispatch(
        {
            "type": "response",
            "id": next_request_id,
            "ok": True,
            "result": {"roots": ["/workspace"]},
        }
    )
    assert await next_request == {"roots": ["/workspace"]}

    with pytest.raises(NodeProtocolError, match="Unknown Node response"):
        await connection.dispatch({"type": "response", "id": "b" * 32, "ok": True, "result": {}})


@pytest.mark.asyncio
async def test_node_connection_retrieves_close_error_when_blocked_send_is_cancelled() -> None:
    send_started = asyncio.Event()

    class BlockingWebSocket:
        async def send_text(self, _raw: str) -> None:
            send_started.set()
            await asyncio.Future()

        async def close(self, *, code: int, reason: str) -> None:
            assert code == 1001
            assert reason == "Node disconnected"

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unhandled: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        connection = NodeConnection(BlockingWebSocket(), "a" * 32)  # type: ignore[arg-type]
        pending = asyncio.create_task(connection.request("workspace.roots", {}))
        await send_started.wait()

        await connection.close()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        del pending
        gc.collect()
        await asyncio.sleep(0)

        assert not [
            context
            for context in unhandled
            if context.get("message") == "Future exception was never retrieved"
        ]
    finally:
        loop.set_exception_handler(previous_handler)


def _new_agent(tmp_path: Path) -> NodeAgent:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    return NodeAgent(
        NodeConfig(
            core_url="http://localhost:8765",
            node_id="a" * 32,
            name="Budget Node",
            allowed_roots=(allowed,),
            state_dir=tmp_path / "state",
        ),
        generate_private_key(),
    )


@pytest.mark.asyncio
async def test_node_agent_accepts_legacy_requests_and_rejects_partial_or_incompatible_envelopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _new_agent(tmp_path)
    sent: list[dict[str, object]] = []

    async def capture(message: object) -> None:
        sent.append(dict(message))  # type: ignore[arg-type]

    monkeypatch.setattr(agent, "_send", capture)
    legacy_id = "b" * 32
    await agent._handle_request(
        {
            "type": "request",
            "id": legacy_id,
            "operation": "workspace.roots",
            "payload": {},
        }
    )
    assert sent[-1]["id"] == legacy_id
    assert sent[-1]["ok"] is True

    for request_id, partial in (
        ("c" * 32, {"protocol_version": NODE_PROTOCOL_VERSION}),
        ("d" * 32, {"budget_ms": 30_000}),
    ):
        await agent._handle_request(
            {
                "type": "request",
                "id": request_id,
                "operation": "workspace.roots",
                "payload": {},
                **partial,
            }
        )
        assert sent[-1]["id"] == request_id
        assert sent[-1]["ok"] is False
        assert sent[-1]["code"] == "request_invalid"

    incompatible_id = "e" * 32
    await agent._handle_request(
        {
            "type": "request",
            "id": incompatible_id,
            "protocol_version": NODE_PROTOCOL_VERSION + 1,
            "budget_ms": 30_000,
            "operation": "workspace.roots",
            "payload": {},
        }
    )
    assert sent[-1]["id"] == incompatible_id
    assert sent[-1]["ok"] is False
    assert sent[-1]["code"] == "version_incompatible"


@pytest.mark.asyncio
async def test_node_agent_budget_can_expire_while_waiting_for_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _new_agent(tmp_path)
    agent._request_limit = asyncio.Semaphore(0)
    sent: list[dict[str, object]] = []
    runtime_called = False

    async def capture(message: object) -> None:
        sent.append(dict(message))  # type: ignore[arg-type]

    async def unexpected_runtime(*_args: object) -> object:
        nonlocal runtime_called
        runtime_called = True
        raise AssertionError("request must expire before runtime admission")

    monkeypatch.setattr(agent, "_send", capture)
    monkeypatch.setattr(agent.runtime, "handle", unexpected_runtime)
    await agent._handle_request(
        {
            "type": "request",
            "id": "f" * 32,
            "protocol_version": NODE_PROTOCOL_VERSION,
            "budget_ms": 5,
            "operation": "workspace.roots",
            "payload": {},
        }
    )

    assert not runtime_called
    assert sent[-1]["ok"] is False
    assert sent[-1]["code"] == "deadline_exceeded"


@pytest.mark.asyncio
async def test_started_sync_node_request_completes_without_a_false_budget_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _new_agent(tmp_path)
    sent: list[dict[str, object]] = []
    started = threading.Event()
    release = threading.Event()

    async def capture(message: object) -> None:
        sent.append(dict(message))  # type: ignore[arg-type]

    def slow_sync(operation: str, _payload: object) -> dict[str, object]:
        assert operation == "workspace.roots"
        started.set()
        assert release.wait(timeout=1)
        return {"roots": []}

    monkeypatch.setattr(agent, "_send", capture)
    monkeypatch.setattr(agent.runtime, "_handle_sync", slow_sync)
    request = asyncio.create_task(
        agent._handle_request(
            {
                "type": "request",
                "id": "1" * 32,
                "protocol_version": NODE_PROTOCOL_VERSION,
                "budget_ms": 5,
                "operation": "workspace.roots",
                "payload": {},
            }
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    await asyncio.sleep(0.02)
    release.set()
    await request

    assert sent[-1]["ok"] is True
    assert sent[-1]["result"] == {"roots": []}


@pytest.mark.asyncio
async def test_core_waits_for_a_started_node_request_past_its_admission_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = _new_agent(tmp_path)
    started = threading.Event()
    release = threading.Event()
    agent_tasks: list[asyncio.Task[None]] = []

    def slow_sync(operation: str, _payload: object) -> dict[str, object]:
        assert operation == "workspace.roots"
        started.set()
        assert release.wait(timeout=1)
        return {"roots": []}

    class RelayWebSocket:
        async def send_text(self, raw: str) -> None:
            message = json.loads(raw)
            agent_tasks.append(
                asyncio.create_task(
                    agent._handle_request(message, received_at=asyncio.get_running_loop().time())
                )
            )

    connection = NodeConnection(RelayWebSocket(), "a" * 32)  # type: ignore[arg-type]

    async def relay_to_core(message: object) -> None:
        await connection.dispatch(dict(message))  # type: ignore[arg-type]

    monkeypatch.setattr(agent, "_send", relay_to_core)
    monkeypatch.setattr(agent.runtime, "_handle_sync", slow_sync)
    request = asyncio.create_task(connection.request("workspace.roots", {}, admission_timeout=0.01))

    assert await asyncio.to_thread(started.wait, 1)
    await asyncio.sleep(0.03)
    still_waiting = not request.done()
    release.set()
    results = await asyncio.gather(request, *agent_tasks, return_exceptions=True)

    assert still_waiting
    assert results[0] == {"roots": []}


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
    assert (
        store.submit_node_enrollment(
            code_hash=pairing_code_digest(code),
            name="Replay",
            public_key=public_key_text(generate_private_key().public_key()),
            fingerprint="SHA256:replay",
            protocol_version=NODE_PROTOCOL_VERSION,
            polling_secret_hash=secret_digest("replay"),
        )
        is None
    )
    assert (
        store.get_node_enrollment(str(enrollment["id"]), polling_secret_hash=secret_digest("wrong"))
        is None
    )
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


@pytest.mark.asyncio
async def test_node_revocation_wins_if_authentication_finishes_before_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _new_store(tmp_path)
    code = generate_pairing_code()
    store.create_node_pairing_code(code_hash=pairing_code_digest(code), expires_at=_expires_at())
    public_key = public_key_text(generate_private_key().public_key())
    enrollment = store.submit_node_enrollment(
        code_hash=pairing_code_digest(code),
        name="Racing Node",
        public_key=public_key,
        fingerprint=public_key_fingerprint(public_key),
        protocol_version=NODE_PROTOCOL_VERSION,
        polling_secret_hash=secret_digest("racing-node-poll-secret"),
    )
    assert enrollment is not None
    computer = store.approve_node_enrollment(str(enrollment["id"]))
    node_id = str(computer["id"])
    core = NodeCore(store)
    authenticated = asyncio.Event()
    release_registration = asyncio.Event()

    class FakeWebSocket:
        def __init__(self) -> None:
            self.closed: tuple[int, str] | None = None

        async def accept(self) -> None:
            return None

        async def close(self, *, code: int, reason: str) -> None:
            self.closed = (code, reason)

        async def send_text(self, _message: str) -> None:
            return None

    websocket = FakeWebSocket()

    async def authenticate(_websocket: object, requested_node_id: str) -> NodeConnection:
        assert requested_node_id == node_id
        store.update_node_connection(
            node_id,
            protocol_version=NODE_PROTOCOL_VERSION,
            capabilities=("files", "terminal", "workspace"),
        )
        authenticated.set()
        await release_registration.wait()
        return NodeConnection(websocket, node_id)  # type: ignore[arg-type]

    monkeypatch.setattr(core, "_authenticate", authenticate)
    handler = asyncio.create_task(core.handle_socket(websocket, node_id))  # type: ignore[arg-type]
    await authenticated.wait()
    store.revoke_node(node_id)
    await core.revoke(node_id)
    release_registration.set()

    await asyncio.wait_for(handler, timeout=1)

    assert websocket.closed == (4403, "Node revoked")
    assert not core.status(node_id).online
    with pytest.raises(NodeUnavailableError):
        core.connection(node_id)


def test_expired_pairing_code_and_duplicate_identity_are_rejected(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    expired_code = generate_pairing_code()
    store.create_node_pairing_code(
        code_hash=pairing_code_digest(expired_code), expires_at=_expires_at(seconds=-1)
    )
    public_key = public_key_text(generate_private_key().public_key())
    assert (
        store.submit_node_enrollment(
            code_hash=pairing_code_digest(expired_code),
            name="Expired",
            public_key=public_key,
            fingerprint=public_key_fingerprint(public_key),
            protocol_version=NODE_PROTOCOL_VERSION,
            polling_secret_hash=secret_digest("expired"),
        )
        is None
    )

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
    assert (
        store.submit_node_enrollment(
            code_hash=pairing_code_digest(second_code),
            name="Duplicate",
            public_key=public_key,
            fingerprint=public_key_fingerprint(public_key),
            protocol_version=NODE_PROTOCOL_VERSION,
            polling_secret_hash=secret_digest("second"),
        )
        is None
    )


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
        assert (
            client.post(
                "/api/node/enroll/status",
                json={"enrollment_id": enrollment_id, "polling_secret": "wrong"},
            ).status_code
            == 404
        )

        review = client.get("/computers/node/pair", params={"pairing_id": pairing_match.group(1)})
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
        assert "state-chip remote unchecked" in hub.text
        assert "Not checked yet" in hub.text
        remote_page = client.get(f"/open/{node_id}")
        assert remote_page.status_code == 200
        assert "state-chip remote unchecked" in remote_page.text
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
                        "signature": sign_challenge(private_key, node_id, challenge["nonce"]),
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
            online_hub = client.get("/open")
            assert "state-chip remote available" in online_hub.text
            assert "Available" in online_hub.text

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

        with client.websocket_connect(f"/api/node/control?node_id={node_id}") as rejected_socket:
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
        with client.websocket_connect(f"/api/node/control?node_id={node_id}") as websocket:
            challenge = json.loads(websocket.receive_text())
            websocket.send_text(
                json.dumps(
                    {
                        "type": "auth.response",
                        "node_id": node_id,
                        "signature": sign_challenge(private_key, node_id, challenge["nonce"]),
                        "protocol_version": NODE_PROTOCOL_VERSION,
                        "capabilities": ["workspace", "files"],
                    }
                )
            )
            closed = websocket.receive()
            assert closed["type"] == "websocket.close"
            assert closed["code"] == 4401

        with client.websocket_connect(f"/api/node/control?node_id={node_id}") as legacy_socket:
            challenge = json.loads(legacy_socket.receive_text())
            legacy_socket.send_text(
                json.dumps(
                    {
                        "type": "auth.response",
                        "node_id": node_id,
                        "signature": sign_challenge(private_key, node_id, challenge["nonce"]),
                        "protocol_version": NODE_PROTOCOL_VERSION - 1,
                        "capabilities": ["workspace", "terminal", "files"],
                    }
                )
            )
            closed = legacy_socket.receive()
            assert closed["type"] == "websocket.close"
            assert closed["code"] == 4406
            assert "update Termroom" in closed["reason"]

        assert not app.state.node_core.status(node_id).online
        persisted = app.state.store.get_computer(node_id)
        assert persisted is not None
        assert json.loads(persisted["node_capabilities_json"]) == []
