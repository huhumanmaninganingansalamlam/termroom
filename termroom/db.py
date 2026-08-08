from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS roots (
                    id TEXT PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY,
                    root_id TEXT NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
                    relative_path TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    tmux_session TEXT NOT NULL UNIQUE,
                    last_opened_at TEXT NOT NULL,
                    last_tab TEXT NOT NULL DEFAULT 'terminal',
                    backend_kind TEXT NOT NULL DEFAULT 'local',
                    computer_id TEXT,
                    canonical_path TEXT,
                    UNIQUE(root_id, relative_path)
                );

                CREATE TABLE IF NOT EXISTS computers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('ssh')),
                    auth_kind TEXT NOT NULL DEFAULT 'key',
                    ssh_alias TEXT NOT NULL DEFAULT '',
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL DEFAULT 22,
                    username TEXT NOT NULL,
                    identity_file TEXT NOT NULL DEFAULT '',
                    host_key_type TEXT NOT NULL,
                    host_key_data TEXT NOT NULL,
                    host_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_connected_at TEXT,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS terminals (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    tmux_window TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_opened_at TEXT NOT NULL,
                    last_output_at TEXT,
                    UNIQUE(workspace_id, tmux_window)
                );

                CREATE TABLE IF NOT EXISTS command_history (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                    terminal_id TEXT NOT NULL REFERENCES terminals(id) ON DELETE CASCADE,
                    command TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_workspaces_recent
                    ON workspaces(last_opened_at DESC);
                CREATE INDEX IF NOT EXISTS idx_history_recent
                    ON command_history(workspace_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_computers_name
                    ON computers(name COLLATE NOCASE);
                """
            )
            self._ensure_column(db, "terminals", "last_output_at", "TEXT")
            self._ensure_column(
                db, "workspaces", "backend_kind", "TEXT NOT NULL DEFAULT 'local'"
            )
            self._ensure_column(db, "workspaces", "computer_id", "TEXT")
            self._ensure_column(db, "workspaces", "canonical_path", "TEXT")
            self._ensure_column(
                db, "computers", "auth_kind", "TEXT NOT NULL DEFAULT 'key'"
            )
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_remote_workspace_unique
                ON workspaces(computer_id, canonical_path)
                WHERE backend_kind = 'ssh'
                """
            )
            # Removed in the password-login redesign. Drop prototype pairing
            # state during migration so old installations do not retain dead
            # authentication records indefinitely.
            db.execute("DROP TABLE IF EXISTS pairing_codes")
            db.execute("DROP TABLE IF EXISTS device_sessions")
        self.path.chmod(0o600)

    @staticmethod
    def _ensure_column(
        db: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {str(row["name"]) for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def ensure_root(self, path: Path) -> dict[str, Any]:
        normalized = str(path.resolve())
        return self.ensure_root_value(normalized)

    def ensure_root_value(self, normalized: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM roots WHERE path = ?", (normalized,)).fetchone()
            if row:
                return dict(row)
            root_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO roots(id, path, created_at) VALUES (?, ?, ?)",
                (root_id, normalized, utc_now()),
            )
            return dict(db.execute("SELECT * FROM roots WHERE id = ?", (root_id,)).fetchone())

    def get_root(self, root_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM roots WHERE id = ?", (root_id,)).fetchone()
            return dict(row) if row else None

    def list_local_roots(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM roots
                WHERE path NOT LIKE 'ssh://%'
                ORDER BY created_at, path COLLATE NOCASE
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def find_workspace(self, root_id: str, relative_path: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM workspaces WHERE root_id = ? AND relative_path = ?",
                (root_id, relative_path),
            ).fetchone()
            return dict(row) if row else None

    def create_workspace(
        self,
        root_id: str,
        relative_path: str,
        display_name: str,
        tmux_session: str | None = None,
        *,
        backend_kind: str = "local",
        computer_id: str | None = None,
        canonical_path: str | None = None,
    ) -> dict[str, Any]:
        workspace_id = uuid.uuid4().hex
        tmux_session = tmux_session or f"termroom-{workspace_id[:12]}"
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO workspaces(
                    id, root_id, relative_path, display_name, tmux_session, last_opened_at,
                    backend_kind, computer_id, canonical_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    root_id,
                    relative_path,
                    display_name,
                    tmux_session,
                    now,
                    backend_kind,
                    computer_id,
                    canonical_path,
                ),
            )
            return dict(
                db.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
            )

    def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT w.*, r.path AS root_path
                FROM workspaces AS w
                JOIN roots AS r ON r.id = w.root_id
                WHERE w.id = ?
                """,
                (workspace_id,),
            ).fetchone()
            return dict(row) if row else None

    def find_remote_workspace(self, computer_id: str, canonical_path: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT * FROM workspaces
                WHERE backend_kind = 'ssh' AND computer_id = ? AND canonical_path = ?
                """,
                (computer_id, canonical_path),
            ).fetchone()
            return dict(row) if row else None

    def list_recent_workspaces(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT w.*, r.path AS root_path
                FROM workspaces AS w
                JOIN roots AS r ON r.id = w.root_id
                ORDER BY w.last_opened_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_workspaces_for_computer(self, computer_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM workspaces
                WHERE computer_id = ?
                ORDER BY last_opened_at DESC
                """,
                (computer_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_workspaces_for_root(self, root_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT w.*, r.path AS root_path
                FROM workspaces AS w
                JOIN roots AS r ON r.id = w.root_id
                WHERE w.root_id = ? AND w.backend_kind = 'local'
                ORDER BY w.last_opened_at DESC
                """,
                (root_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def find_workspace_for_path(self, path: Path) -> dict[str, Any] | None:
        target = path.expanduser().resolve(strict=True)
        matches: list[tuple[int, dict[str, Any]]] = []
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT w.*, r.path AS root_path
                FROM workspaces AS w
                JOIN roots AS r ON r.id = w.root_id
                WHERE w.backend_kind = 'local'
                """
            ).fetchall()
        for row in rows:
            item = dict(row)
            workspace_path = (Path(item["root_path"]) / item["relative_path"]).resolve()
            try:
                target.relative_to(workspace_path)
            except ValueError:
                continue
            item["path"] = workspace_path
            matches.append((len(workspace_path.parts), item))
        if not matches:
            return None
        return max(matches, key=lambda match: match[0])[1]

    def touch_workspace(self, workspace_id: str, *, tab: str | None = None) -> None:
        with self.connect() as db:
            if tab:
                db.execute(
                    "UPDATE workspaces SET last_opened_at = ?, last_tab = ? WHERE id = ?",
                    (utc_now(), tab, workspace_id),
                )
            else:
                db.execute(
                    "UPDATE workspaces SET last_opened_at = ? WHERE id = ?",
                    (utc_now(), workspace_id),
                )

    def delete_workspace(self, workspace_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))

    def list_terminals(self, workspace_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM terminals WHERE workspace_id = ? ORDER BY created_at",
                (workspace_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_terminal(self, terminal_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM terminals WHERE id = ?", (terminal_id,)).fetchone()
            return dict(row) if row else None

    def create_terminal(
        self, workspace_id: str, name: str, tmux_window: str
    ) -> dict[str, Any]:
        terminal_id = uuid.uuid4().hex
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO terminals(
                    id, workspace_id, name, tmux_window, created_at, last_opened_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (terminal_id, workspace_id, name, tmux_window, now, now),
            )
            row = db.execute("SELECT * FROM terminals WHERE id = ?", (terminal_id,)).fetchone()
            return dict(row)

    def reset_terminals(self, workspace_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM terminals WHERE workspace_id = ?", (workspace_id,))

    def touch_terminal(self, terminal_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE terminals SET last_opened_at = ? WHERE id = ?",
                (utc_now(), terminal_id),
            )

    def touch_terminal_output(self, terminal_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE terminals SET last_output_at = ? WHERE id = ?",
                (utc_now(), terminal_id),
            )

    def rename_terminal(self, terminal_id: str, name: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE terminals SET name = ? WHERE id = ?",
                (name, terminal_id),
            )

    def delete_terminal(self, terminal_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM terminals WHERE id = ?", (terminal_id,))

    def add_command(self, workspace_id: str, terminal_id: str, command: str) -> None:
        command = command.strip()
        if not command:
            return
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO command_history(id, workspace_id, terminal_id, command, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (uuid.uuid4().hex, workspace_id, terminal_id, command, utc_now()),
            )
            db.execute(
                """
                DELETE FROM command_history
                WHERE workspace_id = ? AND id NOT IN (
                    SELECT id FROM command_history
                    WHERE workspace_id = ?
                    ORDER BY created_at DESC LIMIT 100
                )
                """,
                (workspace_id, workspace_id),
            )

    def list_commands(self, workspace_id: str, limit: int = 20) -> list[str]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT command, MAX(created_at) AS most_recent
                FROM command_history
                WHERE workspace_id = ?
                GROUP BY command
                ORDER BY most_recent DESC
                LIMIT ?
                """,
                (workspace_id, limit),
            ).fetchall()
            return [str(row["command"]) for row in rows]

    def clear_commands(self, workspace_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM command_history WHERE workspace_id = ?", (workspace_id,))

    def create_computer(
        self,
        *,
        name: str,
        ssh_alias: str,
        host: str,
        port: int,
        username: str,
        identity_file: str,
        auth_kind: str = "key",
        host_key_type: str,
        host_key_data: str,
        host_fingerprint: str,
    ) -> dict[str, Any]:
        computer_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO computers(
                    id, name, kind, auth_kind, ssh_alias, host, port, username, identity_file,
                    host_key_type, host_key_data, host_fingerprint, created_at
                ) VALUES (?, ?, 'ssh', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    computer_id,
                    name.strip()[:80] or host,
                    auth_kind,
                    ssh_alias.strip(),
                    host,
                    port,
                    username,
                    identity_file,
                    host_key_type,
                    host_key_data,
                    host_fingerprint,
                    utc_now(),
                ),
            )
            row = db.execute("SELECT * FROM computers WHERE id = ?", (computer_id,)).fetchone()
            return dict(row)

    def get_computer(self, computer_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM computers WHERE id = ?", (computer_id,)).fetchone()
            return dict(row) if row else None

    def list_computers(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM computers ORDER BY name COLLATE NOCASE").fetchall()
            return [dict(row) for row in rows]

    def update_computer_connection(
        self, computer_id: str, *, error: str | None = None
    ) -> None:
        with self.connect() as db:
            if error:
                db.execute(
                    "UPDATE computers SET last_error = ? WHERE id = ?",
                    (error[:500], computer_id),
                )
            else:
                db.execute(
                    """
                    UPDATE computers
                    SET last_connected_at = ?, last_error = NULL
                    WHERE id = ?
                    """,
                    (utc_now(), computer_id),
                )

    def delete_computer(self, computer_id: str) -> None:
        with self.connect() as db:
            count = db.execute(
                "SELECT COUNT(*) AS count FROM workspaces WHERE computer_id = ?",
                (computer_id,),
            ).fetchone()["count"]
            if count:
                raise RuntimeError("Remove this computer's Workspaces first")
            db.execute("DELETE FROM computers WHERE id = ?", (computer_id,))

    def remove_computer_registration(self, computer_id: str) -> list[str]:
        """Remove Termroom records for a computer without touching remote processes."""

        with self.connect() as db:
            rows = db.execute(
                "SELECT id FROM workspaces WHERE computer_id = ?",
                (computer_id,),
            ).fetchall()
            workspace_ids = [str(row["id"]) for row in rows]
            db.execute("DELETE FROM workspaces WHERE computer_id = ?", (computer_id,))
            db.execute("DELETE FROM roots WHERE path = ?", (f"ssh://{computer_id}",))
            db.execute("DELETE FROM computers WHERE id = ?", (computer_id,))
            return workspace_ids
