from __future__ import annotations

import threading
import uuid


class TerminalControl:
    """Coordinates resize ownership across browser clients for one terminal.

    Before any client sends real terminal input, the newest connection owns
    resize. That makes reconnects, rotations, and device switches immediately
    fit the visible browser. Once a client sends real input, that client keeps
    resize ownership until it disconnects or another client sends real input.
    Passive observers therefore do not continuously fight over tmux dimensions.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: dict[str, list[str]] = {}
        self._input_owners: dict[str, str] = {}
        self._view_owners: dict[str, str] = {}
        self._input_revisions: dict[str, int] = {}
        self._last_input_devices: dict[str, str] = {}

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
                self._view_owners[terminal_id] = client_id
                self._input_revisions[terminal_id] = self._input_revisions.get(terminal_id, 0) + 1
                if device_id:
                    self._last_input_devices[terminal_id] = device_id

    def claim_view(self, terminal_id: str, client_id: str) -> None:
        """Let a focused browser own resize while nobody else is typing."""

        with self._lock:
            clients = self._clients.get(terminal_id, [])
            if client_id not in clients:
                return
            input_owner = self._input_owners.get(terminal_id)
            if input_owner not in clients:
                self._view_owners[terminal_id] = client_id

    def can_resize(self, terminal_id: str, client_id: str) -> bool:
        with self._lock:
            clients = self._clients.get(terminal_id, [])
            if not clients:
                return False
            input_owner = self._input_owners.get(terminal_id)
            if input_owner in clients:
                return input_owner == client_id
            view_owner = self._view_owners.get(terminal_id)
            if view_owner in clients:
                return view_owner == client_id
            return clients[-1] == client_id

    def unregister(self, terminal_id: str, client_id: str) -> None:
        with self._lock:
            clients = self._clients.get(terminal_id)
            if not clients:
                self._input_owners.pop(terminal_id, None)
                self._view_owners.pop(terminal_id, None)
                return
            try:
                clients.remove(client_id)
            except ValueError:
                return
            if not clients:
                self._clients.pop(terminal_id, None)
                self._input_owners.pop(terminal_id, None)
                self._view_owners.pop(terminal_id, None)
                return
            if self._input_owners.get(terminal_id) == client_id:
                self._input_owners.pop(terminal_id, None)
            if self._view_owners.get(terminal_id) == client_id:
                self._view_owners.pop(terminal_id, None)

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
