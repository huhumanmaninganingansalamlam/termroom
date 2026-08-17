from __future__ import annotations

import threading
import uuid


class TerminalControl:
    """Coordinates resize ownership across browser clients for one terminal.

    Only the client that most recently sent real user input may resize the
    shared tmux grid. Connections, focus changes, reloads, and passive viewport
    resizes never claim ownership. If the input owner disconnects, the existing
    grid stays unchanged until another client sends real user input.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: dict[str, list[str]] = {}
        self._input_owners: dict[str, str] = {}
        self._input_revisions: dict[str, int] = {}
        self._last_input_devices: dict[str, str] = {}
        self._applied_resizes: dict[str, tuple[str, int, int]] = {}

    def register(self, terminal_id: str) -> str:
        client_id = uuid.uuid4().hex
        with self._lock:
            clients = self._clients.setdefault(terminal_id, [])
            clients.append(client_id)
        return client_id

    def mark_input(self, terminal_id: str, client_id: str, device_id: str = "") -> None:
        with self._lock:
            if client_id in self._clients.get(terminal_id, []):
                self._input_owners[terminal_id] = client_id
                self._input_revisions[terminal_id] = self._input_revisions.get(terminal_id, 0) + 1
                if device_id:
                    self._last_input_devices[terminal_id] = device_id

    def can_resize(self, terminal_id: str, client_id: str) -> bool:
        with self._lock:
            return self._resize_owner(terminal_id) == client_id

    def should_resize(
        self,
        terminal_id: str,
        client_id: str,
        *,
        rows: int,
        cols: int,
    ) -> bool:
        """Claim one real resize when the owner or dimensions changed."""

        return self.resize_plan(terminal_id, client_id, rows=rows, cols=cols)[1]

    def resize_plan(
        self,
        terminal_id: str,
        client_id: str,
        *,
        rows: int,
        cols: int,
    ) -> tuple[bool, bool]:
        """Return grid ownership and whether that grid needs this resize."""

        with self._lock:
            if self._resize_owner(terminal_id) != client_id:
                return False, False
            requested = (client_id, rows, cols)
            if self._applied_resizes.get(terminal_id) == requested:
                return True, False
            self._applied_resizes[terminal_id] = requested
            return True, True

    def _resize_owner(self, terminal_id: str) -> str | None:
        clients = self._clients.get(terminal_id, [])
        if not clients:
            return None
        input_owner = self._input_owners.get(terminal_id)
        if input_owner in clients:
            return input_owner
        return None

    def unregister(self, terminal_id: str, client_id: str) -> None:
        with self._lock:
            clients = self._clients.get(terminal_id)
            if not clients:
                self._input_owners.pop(terminal_id, None)
                self._applied_resizes.pop(terminal_id, None)
                return
            try:
                clients.remove(client_id)
            except ValueError:
                return
            if not clients:
                self._clients.pop(terminal_id, None)
                self._input_owners.pop(terminal_id, None)
                self._applied_resizes.pop(terminal_id, None)
                return
            if self._input_owners.get(terminal_id) == client_id:
                self._input_owners.pop(terminal_id, None)
            if self._applied_resizes.get(terminal_id, (None, 0, 0))[0] == client_id:
                self._applied_resizes.pop(terminal_id, None)

    def client_count(self, terminal_id: str) -> int:
        with self._lock:
            return len(self._clients.get(terminal_id, []))

    def presence(self, terminal_id: str) -> dict[str, int | str]:
        with self._lock:
            return {
                "count": len(self._clients.get(terminal_id, [])),
                "input_revision": self._input_revisions.get(terminal_id, 0),
                "last_input_device_id": self._last_input_devices.get(terminal_id, ""),
            }
