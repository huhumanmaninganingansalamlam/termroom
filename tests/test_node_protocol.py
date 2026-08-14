from __future__ import annotations

import json

import pytest

from termroom.node_protocol import (
    MAX_NODE_MESSAGE_BYTES,
    NODE_CAPABILITIES,
    NODE_OPTIONAL_CAPABILITIES,
    NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW,
    NODE_REQUEST_OPERATIONS,
    NODE_REQUIRED_CAPABILITIES,
    NodeProtocolError,
    control_websocket_url,
    decode_message,
    encode_message,
    generate_pairing_code,
    generate_private_key,
    normalize_capabilities,
    normalize_core_url,
    normalize_pairing_code,
    pairing_code_digest,
    public_key_fingerprint,
    public_key_text,
    sign_challenge,
    verify_challenge,
)


def test_node_identity_challenge_is_bound_to_node_and_nonce() -> None:
    private_key = generate_private_key()
    public_key = public_key_text(private_key.public_key())
    node_id = "a" * 32
    signature = sign_challenge(private_key, node_id, "nonce-one")

    assert public_key_fingerprint(public_key).startswith("SHA256:")
    assert verify_challenge(public_key, node_id, "nonce-one", signature)
    assert not verify_challenge(public_key, "b" * 32, "nonce-one", signature)
    assert not verify_challenge(public_key, node_id, "nonce-two", signature)
    other_public_key = public_key_text(generate_private_key().public_key())
    assert not verify_challenge(other_public_key, node_id, "nonce-one", signature)


def test_pairing_code_is_high_entropy_normalized_and_hashed() -> None:
    code = generate_pairing_code()
    normalized = normalize_pairing_code(code.lower())

    assert len(normalized) == 16
    assert pairing_code_digest(code) == pairing_code_digest(normalized)
    assert normalized not in pairing_code_digest(code)
    with pytest.raises(NodeProtocolError) as exc_info:
        normalize_pairing_code("ABCD")
    assert exc_info.value.code == "pairing_invalid"


def test_core_url_requires_tls_except_loopback() -> None:
    assert normalize_core_url("http://127.0.0.1:8765/") == "http://127.0.0.1:8765"
    assert normalize_core_url("http://[::1]:8765") == "http://[::1]:8765"
    assert normalize_core_url("https://termroom.example.com/") == "https://termroom.example.com"
    assert (
        control_websocket_url("https://termroom.example.com", "a" * 32)
        == f"wss://termroom.example.com/api/node/control?node_id={'a' * 32}"
    )
    with pytest.raises(NodeProtocolError) as exc_info:
        normalize_core_url("http://termroom.example.com")
    assert exc_info.value.code == "core_tls_required"
    with pytest.raises(NodeProtocolError):
        normalize_core_url("https://user:secret@termroom.example.com/path")


def test_node_messages_and_capabilities_are_bounded_and_typed() -> None:
    raw = encode_message({"type": "heartbeat", "label": "한글"})
    assert decode_message(raw) == {"type": "heartbeat", "label": "한글"}
    assert normalize_capabilities(["terminal", "files", "terminal", "workspace"]) == (
        "files",
        "terminal",
        "workspace",
    )
    with pytest.raises(NodeProtocolError) as exc_info:
        normalize_capabilities(["terminal", "tunnel"])
    assert exc_info.value.code == "capabilities_invalid"
    with pytest.raises(NodeProtocolError) as exc_info:
        decode_message(json.dumps({"type": "data", "data": "x" * MAX_NODE_MESSAGE_BYTES}))
    assert exc_info.value.code == "message_too_large"


def test_managed_runs_are_optional_and_expose_only_fixed_operations() -> None:
    assert NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW == 8
    assert {"workspace", "terminal", "files"} == NODE_REQUIRED_CAPABILITIES
    assert {
        "file_run",
        "recent",
        "remote_run",
        "remote_run_source",
        "workspace_usage",
    } == NODE_OPTIONAL_CAPABILITIES
    assert {
        "workspace",
        "terminal",
        "files",
        "file_run",
        "recent",
        "remote_run",
        "remote_run_source",
        "workspace_usage",
    } == NODE_CAPABILITIES
    workspace_operations = {
        operation
        for operation in NODE_REQUEST_OPERATIONS
        if operation.startswith("workspace.")
    }
    assert workspace_operations == {
        "workspace.roots",
        "workspace.browse",
        "workspace.create_project",
        "workspace.validate",
        "workspace.ensure",
        "workspace.usage",
    }
    terminal_operations = {
        operation
        for operation in NODE_REQUEST_OPERATIONS
        if operation.startswith("terminal.")
    }
    assert terminal_operations == {
        "terminal.activity",
        "terminal.attach",
        "terminal.close",
        "terminal.create",
        "terminal.rename",
        "terminal.scrollback",
    }
    file_run_operations = {
        operation
        for operation in NODE_REQUEST_OPERATIONS
        if operation.startswith("file_run.")
    }
    assert file_run_operations == {
        "file_run.inspect",
        "file_run.start",
        "file_run.observe",
        "file_run.interrupt",
        "file_run.kill",
    }
    remote_run_operations = {
        operation
        for operation in NODE_REQUEST_OPERATIONS
        if operation.startswith("remote_run.")
    }
    assert remote_run_operations == {
        "remote_run.preflight",
        "remote_run.create",
        "remote_run.snapshot.begin",
        "remote_run.snapshot.mkdir",
        "remote_run.snapshot.symlink",
        "remote_run.snapshot.file.open",
        "remote_run.snapshot.commit",
        "remote_run.metadata.write",
        "remote_run.metadata.open",
        "remote_run.start",
        "remote_run.git.start",
        "remote_run.observe",
        "remote_run.poll",
        "remote_run.interrupt",
        "remote_run.kill",
        "remote_run.exists",
        "remote_run.ensure_shell",
        "remote_run.delete",
    }
    remote_run_source_operations = {
        operation
        for operation in NODE_REQUEST_OPERATIONS
        if operation.startswith("remote_run_source.")
    }
    assert remote_run_source_operations == {
        "remote_run_source.manifest.open",
        "remote_run_source.stat",
        "remote_run_source.file.open",
    }
    assert "shell.exec" not in NODE_REQUEST_OPERATIONS
