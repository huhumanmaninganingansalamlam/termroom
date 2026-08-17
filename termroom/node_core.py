from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import secrets
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from termroom.db import StateStore
from termroom.node_protocol import (
    MAX_NODE_STREAM_CHUNK_BYTES,
    NODE_CAPABILITIES,
    NODE_PROTOCOL_VERSION,
    NODE_REQUIRED_CAPABILITIES,
    NodeProtocolError,
    decode_message,
    encode_message,
    normalize_capabilities,
    request_budget_ms,
    validate_node_id,
    validate_protocol_version,
    validate_request_id,
    validate_request_operation,
    verify_challenge,
)

NODE_AUTH_TIMEOUT_SECONDS = 10.0
NODE_REQUEST_ADMISSION_BUDGET_SECONDS = 30.0
NODE_STREAM_FINISH_TIMEOUT_SECONDS = 30.0
NODE_STREAM_QUEUE_DEPTH = 32
NODE_CLOSED_STREAM_TOMBSTONES = 256
NODE_CLOSED_REQUEST_TOMBSTONES = 256


class NodeCoreError(RuntimeError):
    def __init__(self, message: str, *, code: str = "node_error") -> None:
        super().__init__(message)
        self.code = code


class NodeUnavailableError(NodeCoreError):
    def __init__(self, message: str = "Node is offline") -> None:
        super().__init__(message, code="node_offline")


class NodeRequestError(NodeCoreError):
    pass


_STREAM_END = object()


class NodeStream:
    def __init__(self, connection: NodeConnection, stream_id: str) -> None:
        self.connection = connection
        self.stream_id = validate_request_id(stream_id)
        self._queue: asyncio.Queue[bytes | BaseException | object] = asyncio.Queue(
            maxsize=NODE_STREAM_QUEUE_DEPTH
        )
        self._closed = False
        self._result: dict[str, Any] = {}

    async def send(self, data: bytes) -> None:
        if self._closed:
            raise NodeUnavailableError("Node stream is closed")
        view = memoryview(data)
        while view:
            chunk = bytes(view[:MAX_NODE_STREAM_CHUNK_BYTES])
            view = view[len(chunk) :]
            await self.connection.send(
                {
                    "type": "stream.data",
                    "stream_id": self.stream_id,
                    "data": base64.b64encode(chunk).decode("ascii"),
                }
            )

    async def receive(self) -> bytes | None:
        item = await self._queue.get()
        if item is _STREAM_END:
            return None
        if isinstance(item, BaseException):
            raise item
        return bytes(item)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self.receive()
            if chunk is None:
                return
            yield chunk

    async def control(self, kind: str, **values: Any) -> None:
        if self._closed:
            return
        await self.connection.send(
            {
                "type": "stream.control",
                "stream_id": self.stream_id,
                "kind": str(kind),
                **values,
            }
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(NodeCoreError, RuntimeError):
            await self.connection.send({"type": "stream.close", "stream_id": self.stream_id})
        self.connection.drop_stream(self.stream_id)

    async def finish(
        self, *, timeout: float = NODE_STREAM_FINISH_TIMEOUT_SECONDS
    ) -> dict[str, Any]:
        if self._closed:
            raise NodeUnavailableError("Node stream is already closed")
        await self.connection.send({"type": "stream.close", "stream_id": self.stream_id})
        try:
            final = await asyncio.wait_for(self.receive(), timeout=timeout)
        except TimeoutError as exc:
            raise NodeUnavailableError("Node did not finish the stream in time") from exc
        finally:
            self.connection.drop_stream(self.stream_id)
        if final is not None:
            raise NodeRequestError("Node stream ended with unexpected data", code="stream_invalid")
        return dict(self._result)

    async def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(NodeCoreError, RuntimeError):
            await self.connection.send({"type": "stream.abort", "stream_id": self.stream_id})
        self.connection.drop_stream(self.stream_id)

    def feed_data(self, chunk: bytes) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(chunk)
        except asyncio.QueueFull as exc:
            self.feed_error(
                NodeCoreError("Node stream exceeded its buffer", code="stream_overflow")
            )
            raise NodeCoreError("Node stream exceeded its buffer", code="stream_overflow") from exc

    def feed_end(self, result: Mapping[str, Any] | None = None) -> None:
        if self._closed:
            return
        self._result = dict(result or {})
        self._closed = True
        self._replace_queue_tail(_STREAM_END)

    def feed_error(self, error: BaseException) -> None:
        if self._closed:
            return
        self._closed = True
        self._replace_queue_tail(error)

    def _replace_queue_tail(self, item: BaseException | object) -> None:
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        self._queue.put_nowait(item)


class NodeConnection:
    def __init__(self, websocket: WebSocket, node_id: str) -> None:
        self.websocket = websocket
        self.node_id = validate_node_id(node_id)
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._closed_requests: deque[str] = deque(maxlen=NODE_CLOSED_REQUEST_TOMBSTONES)
        self._streams: dict[str, NodeStream] = {}
        self._closed_streams: deque[str] = deque(maxlen=NODE_CLOSED_STREAM_TOMBSTONES)
        self._closed = False

    async def send(self, message: Mapping[str, Any]) -> None:
        if self._closed:
            raise NodeUnavailableError()
        raw = encode_message(message)
        try:
            async with self._send_lock:
                await self.websocket.send_text(raw)
        except (RuntimeError, WebSocketDisconnect) as exc:
            raise NodeUnavailableError() from exc

    async def request(
        self,
        operation: str,
        payload: Mapping[str, Any],
        *,
        admission_timeout: float = NODE_REQUEST_ADMISSION_BUDGET_SECONDS,
    ) -> dict[str, Any]:
        operation = validate_request_operation(operation)
        budget_ms = request_budget_ms(admission_timeout)
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self.send(
                {
                    "type": "request",
                    "id": request_id,
                    "protocol_version": NODE_PROTOCOL_VERSION,
                    "budget_ms": budget_ms,
                    "operation": operation,
                    "payload": dict(payload),
                }
            )
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            self._closed_requests.append(request_id)
            raise
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            else:
                with contextlib.suppress(asyncio.CancelledError):
                    future.exception()

    async def open_stream(
        self,
        operation: str,
        payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], NodeStream]:
        stream_id = uuid.uuid4().hex
        stream = NodeStream(self, stream_id)
        self._streams[stream_id] = stream
        try:
            result = await self.request(operation, {**payload, "stream_id": stream_id})
        except BaseException:
            self._streams.pop(stream_id, None)
            raise
        if result.get("stream_id") != stream_id:
            self._streams.pop(stream_id, None)
            raise NodeRequestError("Node returned the wrong stream", code="stream_invalid")
        return result, stream

    def drop_stream(self, stream_id: str) -> None:
        if self._streams.pop(stream_id, None) is not None:
            self._closed_streams.append(stream_id)

    async def dispatch(self, message: Mapping[str, Any]) -> None:
        kind = message.get("type")
        if kind == "response":
            self._dispatch_response(message)
            return
        if kind == "stream.data":
            self._dispatch_stream_data(message)
            return
        if kind == "stream.close":
            stream = self._stream(message)
            if stream is None:
                return
            result = message.get("result", {})
            if not isinstance(result, dict):
                raise NodeProtocolError("Node stream result is invalid", code="stream_invalid")
            stream.feed_end(result)
            self.drop_stream(stream.stream_id)
            return
        if kind == "stream.error":
            stream = self._stream(message)
            if stream is None:
                return
            stream.feed_error(
                NodeRequestError(
                    str(message.get("error") or "Node stream failed")[:500],
                    code=str(message.get("code") or "stream_failed")[:80],
                )
            )
            self.drop_stream(stream.stream_id)
            return
        raise NodeProtocolError("Unexpected Node message", code="message_unexpected")

    def _dispatch_response(self, message: Mapping[str, Any]) -> None:
        request_id = validate_request_id(str(message.get("id") or ""))
        future = self._pending.get(request_id)
        if future is None or future.done():
            if request_id in self._closed_requests:
                return
            raise NodeProtocolError("Unknown Node response", code="response_unknown")
        if message.get("ok") is True:
            result = message.get("result", {})
            if not isinstance(result, dict):
                raise NodeProtocolError("Node response is invalid", code="response_invalid")
            future.set_result(dict(result))
            return
        future.set_exception(
            NodeRequestError(
                str(message.get("error") or "Node request failed")[:500],
                code=str(message.get("code") or "request_failed")[:80],
            )
        )

    def _dispatch_stream_data(self, message: Mapping[str, Any]) -> None:
        stream = self._stream(message)
        if stream is None:
            return
        value = message.get("data")
        if not isinstance(value, str):
            raise NodeProtocolError("Node stream data is invalid", code="stream_invalid")
        try:
            chunk = base64.b64decode(value.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise NodeProtocolError("Node stream data is invalid", code="stream_invalid") from exc
        if len(chunk) > MAX_NODE_STREAM_CHUNK_BYTES:
            raise NodeProtocolError("Node stream chunk is too large", code="stream_too_large")
        stream.feed_data(chunk)

    def _stream(self, message: Mapping[str, Any]) -> NodeStream | None:
        stream_id = validate_request_id(str(message.get("stream_id") or ""))
        stream = self._streams.get(stream_id)
        if stream is None:
            if stream_id in self._closed_streams:
                return None
            raise NodeProtocolError("Unknown Node stream", code="stream_unknown")
        return stream

    async def close(self, *, code: int = 1001, reason: str = "Node disconnected") -> None:
        if self._closed:
            return
        self._closed = True
        error = NodeUnavailableError(reason)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        for stream in self._streams.values():
            stream.feed_error(error)
        self._pending.clear()
        self._streams.clear()
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await self.websocket.close(code=code, reason=reason[:123])


@dataclass(slots=True)
class NodeStatus:
    online: bool
    capabilities: tuple[str, ...]


class NodeCore:
    def __init__(self, store: StateStore) -> None:
        self.store = store
        self._connections: dict[str, NodeConnection] = {}
        self._lock = asyncio.Lock()

    def status(self, node_id: str) -> NodeStatus:
        computer = self.store.get_computer(validate_node_id(node_id))
        if computer is None or computer.get("connection_method") != "node":
            raise KeyError(f"Unknown Node: {node_id}")
        try:
            raw = json.loads(str(computer.get("node_capabilities_json") or "[]"))
            capabilities = normalize_capabilities(raw)
        except (json.JSONDecodeError, NodeProtocolError):
            capabilities = ()
        return NodeStatus(
            online=node_id in self._connections and computer.get("node_revoked_at") is None,
            capabilities=capabilities,
        )

    def connection(self, node_id: str) -> NodeConnection:
        node_id = validate_node_id(node_id)
        computer = self.store.get_computer(node_id)
        if (
            computer is None
            or computer.get("connection_method") != "node"
            or computer.get("node_revoked_at") is not None
        ):
            raise NodeUnavailableError()
        connection = self._connections.get(node_id)
        if connection is None:
            raise NodeUnavailableError()
        return connection

    async def handle_socket(self, websocket: WebSocket, node_id: str) -> None:
        await websocket.accept()
        try:
            connection = await self._authenticate(websocket, node_id)
        except NodeProtocolError as exc:
            close_code = 4406 if exc.code == "version_incompatible" else 4401
            close_reason = (
                "Node protocol is incompatible; update Termroom on Core and Node"
                if exc.code == "version_incompatible"
                else "Node authentication failed"
            )
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=close_code, reason=close_reason)
            return
        except (KeyError, NodeCoreError, TimeoutError, WebSocketDisconnect):
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(code=4401, reason="Node authentication failed")
            return

        previous: NodeConnection | None = None
        revoked = False
        async with self._lock:
            computer = self.store.get_computer(connection.node_id)
            if (
                computer is None
                or computer.get("connection_method") != "node"
                or computer.get("node_revoked_at") is not None
            ):
                revoked = True
            else:
                previous = self._connections.get(connection.node_id)
                self._connections[connection.node_id] = connection
        if revoked:
            await connection.close(code=4403, reason="Node revoked")
            return
        if previous is not None:
            await previous.close(code=4001, reason="Node reconnected")
        try:
            await connection.send({"type": "auth.ok", "node_id": connection.node_id})
            while True:
                message = await self._receive(websocket)
                if message.get("type") == "heartbeat":
                    self.store.touch_node(connection.node_id)
                    await connection.send({"type": "heartbeat.ack"})
                    continue
                await connection.dispatch(message)
        except (KeyError, NodeProtocolError, NodeCoreError, WebSocketDisconnect, RuntimeError):
            pass
        finally:
            async with self._lock:
                if self._connections.get(connection.node_id) is connection:
                    self._connections.pop(connection.node_id, None)
            await connection.close()

    async def revoke(self, node_id: str) -> None:
        node_id = validate_node_id(node_id)
        async with self._lock:
            connection = self._connections.pop(node_id, None)
        if connection is not None:
            await connection.close(code=4403, reason="Node revoked")

    async def shutdown(self) -> None:
        async with self._lock:
            connections = list(self._connections.values())
            self._connections.clear()
        for connection in connections:
            await connection.close(code=1001, reason="Core shutting down")

    async def _authenticate(self, websocket: WebSocket, node_id: str) -> NodeConnection:
        node_id = validate_node_id(node_id)
        computer = self.store.get_computer(node_id)
        if (
            computer is None
            or computer.get("connection_method") != "node"
            or computer.get("node_revoked_at") is not None
        ):
            raise KeyError(f"Unknown or revoked Node: {node_id}")
        nonce = secrets.token_urlsafe(32)
        await websocket.send_text(
            encode_message(
                {
                    "type": "auth.challenge",
                    "nonce": nonce,
                    "capabilities": sorted(NODE_CAPABILITIES),
                }
            )
        )
        message = await asyncio.wait_for(self._receive(websocket), NODE_AUTH_TIMEOUT_SECONDS)
        if message.get("type") != "auth.response" or message.get("node_id") != node_id:
            raise NodeProtocolError("Node authentication response is invalid", code="auth_invalid")
        version = validate_protocol_version(message.get("protocol_version"))
        capabilities = normalize_capabilities(message.get("capabilities"))
        if not NODE_REQUIRED_CAPABILITIES.issubset(capabilities):
            raise NodeProtocolError(
                "Node does not provide the required capabilities", code="capabilities_missing"
            )
        if not verify_challenge(
            str(computer.get("node_public_key") or ""),
            node_id,
            nonce,
            str(message.get("signature") or ""),
        ):
            raise NodeProtocolError("Node signature is invalid", code="auth_invalid")
        self.store.update_node_connection(
            node_id, protocol_version=version, capabilities=capabilities
        )
        return NodeConnection(websocket, node_id)

    @staticmethod
    async def _receive(websocket: WebSocket) -> dict[str, Any]:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        raw = message.get("text")
        if raw is None:
            raw = message.get("bytes")
        if raw is None:
            raise NodeProtocolError("Node message is empty", code="message_invalid")
        return decode_message(raw)


class NodePairingRateLimiter:
    def __init__(self) -> None:
        self._attempts: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(self, remote_key: str, action: str) -> bool:
        limits = {"enroll": 10, "status": 120}
        limit = limits.get(action, 10)
        now = asyncio.get_running_loop().time()
        attempts = self._attempts[(remote_key or "unknown", action)]
        while attempts and now - attempts[0] > 60:
            attempts.popleft()
        if len(attempts) >= limit:
            return False
        attempts.append(now)
        return True
