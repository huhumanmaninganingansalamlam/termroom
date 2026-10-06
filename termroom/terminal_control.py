from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class TerminalResizePlan:
    terminal_id: str
    client_id: str
    rows: int
    cols: int
    revision: int
    token: str
    bootstrap: bool
    apply: bool


class TerminalControl:
    """Coordinates resize ownership across browser clients for one terminal.

    Only the client that most recently sent real user input may resize the
    shared tmux grid. Connections, focus changes, reloads, and passive viewport
    resizes never claim ownership. A newly created tmux grid is the sole
    exception: its first browser client may establish the default 80x24 pane's
    initial size so a fresh Workspace does not render tmux padding until the
    first keystroke. Successful apply and passive demotion consume that
    bootstrap permission; only real input can establish a continuous owner.
    If the input owner disconnects, the existing grid stays unchanged until
    another client sends real user input.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: dict[str, list[str]] = {}
        self._client_devices: dict[str, dict[str, str]] = {}
        self._input_owners: dict[str, str] = {}
        self._bootstrap_pending: set[str] = set()
        self._bootstrap_owners: dict[str, str] = {}
        self._input_revisions: dict[str, int] = {}
        self._last_input_devices: dict[str, str] = {}
        self._applied_resizes: dict[str, tuple[str, int, int]] = {}
        self._resize_plans: dict[str, TerminalResizePlan] = {}
        self._bootstrap_applied: set[str] = set()
        self._applied_plan_tokens: set[str] = set()

    def register(self, terminal_id: str, *, device_id: str = "") -> str:
        client_id = uuid.uuid4().hex
        safe_device_id = str(device_id).strip()
        with self._lock:
            clients = self._clients.setdefault(terminal_id, [])
            clients.append(client_id)
            if safe_device_id:
                self._client_devices.setdefault(terminal_id, {})[client_id] = safe_device_id
            if (
                terminal_id in self._bootstrap_pending
                and terminal_id not in self._input_owners
                and terminal_id not in self._bootstrap_owners
                and terminal_id not in self._resize_plans
            ):
                self._bootstrap_owners[terminal_id] = client_id
        return client_id

    def mark_grid_fresh(self, terminal_id: str, *, client_id: str = "") -> None:
        """Let the first browser establish a newly created tmux grid before input."""

        with self._lock:
            self._bootstrap_pending.add(terminal_id)
            if (
                client_id
                and client_id in self._clients.get(terminal_id, [])
                and terminal_id not in self._input_owners
                and terminal_id not in self._bootstrap_owners
            ):
                self._bootstrap_owners[terminal_id] = client_id

    def mark_input(self, terminal_id: str, client_id: str, device_id: str = "") -> None:
        with self._lock:
            if client_id in self._clients.get(terminal_id, []):
                self._input_owners[terminal_id] = client_id
                self._bootstrap_pending.discard(terminal_id)
                self._bootstrap_owners.pop(terminal_id, None)
                self._input_revisions[terminal_id] = self._input_revisions.get(terminal_id, 0) + 1
                if device_id:
                    self._last_input_devices[terminal_id] = device_id

    def mark_grid_existing(self, terminal_id: str) -> None:
        """Reconcile a reconnect with the backend's consumed fresh-grid marker."""
        with self._lock:
            self._bootstrap_pending.discard(terminal_id)
            self._bootstrap_owners.pop(terminal_id, None)

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
        """Inspect whether the owner needs a resize; this does not commit it."""

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
            return True, True

    def _resize_owner(self, terminal_id: str) -> str | None:
        clients = self._clients.get(terminal_id, [])
        if not clients:
            return None
        input_owner = self._input_owners.get(terminal_id)
        if input_owner in clients:
            return input_owner
        bootstrap_owner = self._bootstrap_owners.get(terminal_id)
        if bootstrap_owner in clients and terminal_id not in self._bootstrap_applied:
            return bootstrap_owner
        return None

    def begin_resize(
        self, terminal_id: str, client_id: str, *, rows: int, cols: int
    ) -> TerminalResizePlan | None:
        """Reserve a resize without recording backend success."""

        with self._lock:
            if self._resize_owner(terminal_id) != client_id or terminal_id in self._resize_plans:
                return None
            plan = TerminalResizePlan(
                terminal_id,
                client_id,
                rows,
                cols,
                self._input_revisions.get(terminal_id, 0),
                uuid.uuid4().hex,
                self._bootstrap_owners.get(terminal_id) == client_id,
                self._applied_resizes.get(terminal_id) != (client_id, rows, cols),
            )
            self._resize_plans[terminal_id] = plan
            return plan

    def _plan_current(self, plan: TerminalResizePlan) -> bool:
        owner = (
            self._bootstrap_owners.get(plan.terminal_id)
            if plan.bootstrap
            else self._input_owners.get(plan.terminal_id)
        )
        return (
            self._resize_plans.get(plan.terminal_id) == plan
            and plan.client_id in self._clients.get(plan.terminal_id, [])
            and owner == plan.client_id
            and self._input_revisions.get(plan.terminal_id, 0) == plan.revision
        )

    def resize_plan_current(self, plan: TerminalResizePlan) -> bool:
        with self._lock:
            return self._plan_current(plan)

    def resize_applied(self, plan: TerminalResizePlan) -> bool:
        """Record the physical bootstrap boundary before passive demotion."""

        with self._lock:
            if self._resize_plans.get(plan.terminal_id) != plan:
                return False
            self._applied_plan_tokens.add(plan.token)
            if plan.bootstrap:
                # Even a stale apply must never reopen the same fresh grid.
                self._bootstrap_pending.discard(plan.terminal_id)
                self._bootstrap_applied.add(plan.terminal_id)
            return self._plan_current(plan)

    def commit_resize(self, plan: TerminalResizePlan) -> bool:
        """Commit only the exact live plan after apply and required demotion."""

        with self._lock:
            if not self._plan_current(plan):
                return False
            if plan.apply and plan.token not in self._applied_plan_tokens:
                return False
            if plan.bootstrap and plan.terminal_id not in self._bootstrap_applied:
                return False
            self._applied_resizes[plan.terminal_id] = (plan.client_id, plan.rows, plan.cols)
            self._resize_plans.pop(plan.terminal_id, None)
            self._applied_plan_tokens.discard(plan.token)
            if plan.bootstrap:
                self._bootstrap_pending.discard(plan.terminal_id)
                self._bootstrap_owners.pop(plan.terminal_id, None)
                self._bootstrap_applied.discard(plan.terminal_id)
            return True

    def abort_resize(self, plan: TerminalResizePlan) -> None:
        with self._lock:
            if self._resize_plans.get(plan.terminal_id) != plan:
                return
            self._resize_plans.pop(plan.terminal_id, None)
            self._applied_plan_tokens.discard(plan.token)
            if plan.bootstrap and plan.terminal_id in self._bootstrap_applied:
                # Applied but uncommitted: fail closed, including on disconnect.
                self._bootstrap_owners.pop(plan.terminal_id, None)
                self._bootstrap_applied.discard(plan.terminal_id)
            elif (
                plan.terminal_id in self._bootstrap_pending
                and plan.terminal_id not in self._bootstrap_owners
                and (clients := self._clients.get(plan.terminal_id))
            ):
                self._bootstrap_owners[plan.terminal_id] = clients[0]

    def unregister(self, terminal_id: str, client_id: str) -> None:
        with self._lock:
            clients = self._clients.get(terminal_id)
            if not clients:
                self._client_devices.pop(terminal_id, None)
                self._input_owners.pop(terminal_id, None)
                self._bootstrap_owners.pop(terminal_id, None)
                self._applied_resizes.pop(terminal_id, None)
                return
            try:
                clients.remove(client_id)
            except ValueError:
                return
            client_devices = self._client_devices.get(terminal_id)
            if client_devices is not None:
                client_devices.pop(client_id, None)
            if not clients:
                self._clients.pop(terminal_id, None)
                self._client_devices.pop(terminal_id, None)
                self._input_owners.pop(terminal_id, None)
                self._bootstrap_owners.pop(terminal_id, None)
                self._applied_resizes.pop(terminal_id, None)
                return
            if self._input_owners.get(terminal_id) == client_id:
                self._input_owners.pop(terminal_id, None)
            if self._bootstrap_owners.get(terminal_id) == client_id:
                self._bootstrap_owners.pop(terminal_id, None)
                if terminal_id in self._bootstrap_pending and terminal_id not in self._resize_plans:
                    self._bootstrap_owners[terminal_id] = clients[0]
            if self._applied_resizes.get(terminal_id, (None, 0, 0))[0] == client_id:
                self._applied_resizes.pop(terminal_id, None)

    def client_count(self, terminal_id: str) -> int:
        with self._lock:
            return len(self._clients.get(terminal_id, []))

    def presence(self, terminal_id: str) -> dict[str, int | str]:
        with self._lock:
            clients = self._clients.get(terminal_id, [])
            client_devices = self._client_devices.get(terminal_id, {})
            known_devices = {
                device_id
                for client_id in clients
                if (device_id := client_devices.get(client_id, ""))
            }
            anonymous_clients = sum(
                1 for client_id in clients if not client_devices.get(client_id, "")
            )
            return {
                "count": len(known_devices) + anonymous_clients,
                "input_revision": self._input_revisions.get(terminal_id, 0),
                "last_input_device_id": self._last_input_devices.get(terminal_id, ""),
            }
