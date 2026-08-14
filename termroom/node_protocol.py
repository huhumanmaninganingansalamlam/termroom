from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import secrets
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

NODE_PROTOCOL_VERSION = 1
NODE_REMOTE_RUN_VERSION = 1
NODE_REMOTE_RUN_SOURCE_VERSION = 1
NODE_WORKSPACE_USAGE_VERSION = 1
NODE_REMOTE_RUN_SOURCE_STREAM_WINDOW = 8
NODE_REQUIRED_CAPABILITIES = frozenset({"workspace", "terminal", "files"})
NODE_OPTIONAL_CAPABILITIES = frozenset(
    {"file_run", "recent", "remote_run", "remote_run_source", "workspace_usage"}
)
NODE_CAPABILITIES = NODE_REQUIRED_CAPABILITIES | NODE_OPTIONAL_CAPABILITIES
NODE_REQUEST_OPERATIONS = frozenset(
    {
        "workspace.roots",
        "workspace.browse",
        "workspace.create_project",
        "workspace.validate",
        "workspace.ensure",
        "workspace.usage",
        "terminal.create",
        "terminal.rename",
        "terminal.close",
        "terminal.activity",
        "terminal.scrollback",
        "terminal.attach",
        "files.list",
        "files.recent",
        "files.stat",
        "files.read_text.open",
        "files.read_preview",
        "files.write_text.open",
        "files.create",
        "files.rename",
        "files.delete",
        "files.download.open",
        "files.upload.open",
        "file_run.inspect",
        "file_run.start",
        "file_run.observe",
        "file_run.interrupt",
        "file_run.kill",
        "remote_run_source.manifest.open",
        "remote_run_source.stat",
        "remote_run_source.file.open",
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
)
MAX_NODE_MESSAGE_BYTES = 1024 * 1024
MAX_NODE_STREAM_CHUNK_BYTES = 64 * 1024
PAIRING_CODE_TTL_SECONDS = 10 * 60
PAIRING_CODE_PATTERN = re.compile(r"^[A-Z2-7]{16}$")
NODE_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
REQUEST_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


class NodeProtocolError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def generate_pairing_code() -> str:
    raw = base64.b32encode(secrets.token_bytes(10)).decode("ascii").rstrip("=")
    return "-".join(raw[index : index + 4] for index in range(0, len(raw), 4))


def normalize_pairing_code(value: str) -> str:
    normalized = value.strip().upper().replace("-", "").replace(" ", "")
    if not PAIRING_CODE_PATTERN.fullmatch(normalized):
        raise NodeProtocolError("Pairing code is invalid", code="pairing_invalid")
    return normalized


def secret_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pairing_code_digest(value: str) -> str:
    return secret_digest(f"termroom-node-pairing-v1\0{normalize_pairing_code(value)}")


def generate_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def private_key_pem(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def load_private_key(raw: bytes) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except (TypeError, ValueError) as exc:
        raise NodeProtocolError("Node private key is invalid", code="identity_invalid") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise NodeProtocolError("Node private key is invalid", code="identity_invalid")
    return key


def public_key_text(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_public_key(value: str) -> Ed25519PublicKey:
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        if len(raw) != 32:
            raise ValueError
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as exc:
        raise NodeProtocolError("Node public key is invalid", code="identity_invalid") from exc


def public_key_fingerprint(value: str) -> str:
    key = parse_public_key(value)
    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
    return f"SHA256:{digest}"


def auth_payload(node_id: str, nonce: str) -> bytes:
    validate_node_id(node_id)
    if not nonce or len(nonce) > 256:
        raise NodeProtocolError("Node challenge is invalid", code="challenge_invalid")
    return f"termroom-node-auth-v1\0{node_id}\0{nonce}".encode()


def sign_challenge(private_key: Ed25519PrivateKey, node_id: str, nonce: str) -> str:
    signature = private_key.sign(auth_payload(node_id, nonce))
    return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


def verify_challenge(public_key: str, node_id: str, nonce: str, signature: str) -> bool:
    try:
        padded = signature + "=" * (-len(signature) % 4)
        raw_signature = base64.urlsafe_b64decode(padded.encode("ascii"))
        parse_public_key(public_key).verify(raw_signature, auth_payload(node_id, nonce))
    except (InvalidSignature, NodeProtocolError, TypeError, ValueError):
        return False
    return True


def validate_node_id(value: str) -> str:
    node_id = str(value)
    if not NODE_ID_PATTERN.fullmatch(node_id):
        raise NodeProtocolError("Node id is invalid", code="identity_invalid")
    return node_id


def validate_request_id(value: str) -> str:
    request_id = str(value)
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise NodeProtocolError("Node request id is invalid", code="request_invalid")
    return request_id


def validate_request_operation(value: object) -> str:
    operation = str(value)
    if operation not in NODE_REQUEST_OPERATIONS:
        raise NodeProtocolError("Node operation is unsupported", code="operation_unsupported")
    return operation


def validate_protocol_version(value: object) -> int:
    if isinstance(value, bool):
        raise NodeProtocolError("Node protocol version is invalid", code="version_invalid")
    try:
        version = int(value)
    except (TypeError, ValueError) as exc:
        raise NodeProtocolError("Node protocol version is invalid", code="version_invalid") from exc
    if version != NODE_PROTOCOL_VERSION:
        raise NodeProtocolError(
            f"Node protocol version {version} is incompatible; update Termroom",
            code="version_incompatible",
        )
    return version


def normalize_capabilities(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise NodeProtocolError("Node capabilities are invalid", code="capabilities_invalid")
    normalized = tuple(sorted(set(value)))
    unknown = set(normalized) - NODE_CAPABILITIES
    if unknown:
        raise NodeProtocolError(
            "Node reported unsupported capabilities", code="capabilities_invalid"
        )
    return normalized


def encode_message(message: Mapping[str, Any]) -> str:
    if not isinstance(message.get("type"), str) or not message["type"]:
        raise NodeProtocolError("Node message type is required", code="message_invalid")
    try:
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise NodeProtocolError(
            "Node message is not JSON serializable", code="message_invalid"
        ) from exc
    if len(raw.encode("utf-8")) > MAX_NODE_MESSAGE_BYTES:
        raise NodeProtocolError("Node message exceeds the size limit", code="message_too_large")
    return raw


def decode_message(raw: str | bytes) -> dict[str, Any]:
    encoded = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    if len(encoded) > MAX_NODE_MESSAGE_BYTES:
        raise NodeProtocolError("Node message exceeds the size limit", code="message_too_large")
    try:
        message = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NodeProtocolError("Node message is invalid JSON", code="message_invalid") from exc
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise NodeProtocolError("Node message type is required", code="message_invalid")
    return message


def normalize_core_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise NodeProtocolError("Core URL must use http or https", code="core_url_invalid")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise NodeProtocolError(
            "Core URL must not contain credentials or query data", code="core_url_invalid"
        )
    if parsed.path not in {"", "/"}:
        raise NodeProtocolError("Core URL must not contain a path", code="core_url_invalid")
    if parsed.scheme == "http" and not _loopback_host(parsed.hostname):
        raise NodeProtocolError(
            "A non-loopback Core URL must use HTTPS", code="core_tls_required"
        )
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


def control_websocket_url(core_url: str, node_id: str) -> str:
    normalized = normalize_core_url(core_url)
    validate_node_id(node_id)
    parsed = urlsplit(normalized)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, "/api/node/control", f"node_id={node_id}", ""))


def _loopback_host(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False
