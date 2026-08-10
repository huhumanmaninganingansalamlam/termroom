from __future__ import annotations

import sqlite3
import unicodedata
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

WORKSPACE_KINDS = frozenset({"workspace", "remote_run", "server_terminal"})


def normalize_computer_name(value: str) -> str:
    """Return a safe one-line display label for a registered computer."""

    name = value.strip()
    if (
        not name
        or len(name) > 80
        or any(
            unicodedata.category(character) in {"Cc", "Zl", "Zp"}
            for character in name
        )
    ):
        raise ValueError("Computer display name must be a single line of 1 to 80 characters")
    return name


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
                    workspace_kind TEXT NOT NULL DEFAULT 'workspace',
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

                CREATE TABLE IF NOT EXISTS remote_runs (
                    id TEXT PRIMARY KEY,
                    source_kind TEXT NOT NULL
                        CHECK(source_kind IN ('workspace', 'git', 'zip')),
                    source_workspace_id TEXT
                        REFERENCES workspaces(id) ON DELETE SET NULL,
                    source_path TEXT,
                    source_label TEXT NOT NULL,
                    source_url TEXT,
                    source_options_json TEXT NOT NULL DEFAULT '{}',
                    source_revision TEXT,
                    source_size INTEGER,
                    target_computer_id TEXT NOT NULL REFERENCES computers(id),
                    command TEXT NOT NULL,
                    run_base TEXT NOT NULL,
                    workspace_id TEXT
                        REFERENCES workspaces(id) ON DELETE SET NULL,
                    state TEXT NOT NULL
                        CHECK(state IN (
                            'preparing', 'running', 'finished',
                            'stopped', 'failed', 'lost'
                        )),
                    phase TEXT,
                    exit_code INTEGER,
                    error_code TEXT,
                    error_detail TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    stop_requested_at TEXT,
                    ended_at TEXT,
                    expires_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_workspaces_recent
                    ON workspaces(last_opened_at DESC);
                CREATE INDEX IF NOT EXISTS idx_history_recent
                    ON command_history(workspace_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_computers_name
                    ON computers(name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_remote_runs_recent
                    ON remote_runs(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_remote_runs_target
                    ON remote_runs(target_computer_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_remote_runs_expired
                    ON remote_runs(expires_at)
                    WHERE expires_at IS NOT NULL;
                """
            )
            self._ensure_column(db, "terminals", "last_output_at", "TEXT")
            self._ensure_column(
                db, "workspaces", "backend_kind", "TEXT NOT NULL DEFAULT 'local'"
            )
            self._ensure_column(db, "workspaces", "computer_id", "TEXT")
            self._ensure_column(db, "workspaces", "canonical_path", "TEXT")
            self._ensure_column(
                db,
                "workspaces",
                "workspace_kind",
                "TEXT NOT NULL DEFAULT 'workspace'",
            )
            self._ensure_column(
                db, "computers", "auth_kind", "TEXT NOT NULL DEFAULT 'key'"
            )
            self._ensure_column(db, "computers", "run_base_dir", "TEXT")
            self._ensure_column(
                db,
                "remote_runs",
                "workspace_id",
                "TEXT REFERENCES workspaces(id) ON DELETE SET NULL",
            )
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_remote_runs_workspace
                ON remote_runs(workspace_id)
                WHERE workspace_id IS NOT NULL
                """
            )
            db.execute(
                """
                UPDATE workspaces
                SET workspace_kind = 'remote_run'
                WHERE id IN (
                    SELECT workspace_id FROM remote_runs WHERE workspace_id IS NOT NULL
                )
                """
            )
            remote_workspace_index_columns = [
                str(row["name"])
                for row in db.execute("PRAGMA index_info(idx_remote_workspace_unique)")
            ]
            if remote_workspace_index_columns != [
                "computer_id",
                "canonical_path",
                "workspace_kind",
            ]:
                db.execute("DROP INDEX IF EXISTS idx_remote_workspace_unique")
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_remote_workspace_unique
                ON workspaces(computer_id, canonical_path, workspace_kind)
                WHERE backend_kind = 'ssh'
                """
            )
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_server_terminal_computer
                ON workspaces(computer_id)
                WHERE workspace_kind = 'server_terminal'
                """
            )
            # Removed in the password-login redesign. Drop prototype pairing
            # state during migration so old installations do not retain dead
            # authentication records indefinitely.
            db.execute("DROP TABLE IF EXISTS pairing_codes")
            db.execute("DROP TABLE IF EXISTS device_sessions")
            # Quick Run is no longer reachable from application code, but its
            # rows may still identify remote folders that need manual recovery
            # or cleanup. Retire the live table name without destroying that
            # metadata during an upgrade.
            tables = {
                str(row["name"])
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "runs" in tables and "legacy_runs" not in tables:
                db.execute("ALTER TABLE runs RENAME TO legacy_runs")
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
                "INSERT OR IGNORE INTO roots(id, path, created_at) VALUES (?, ?, ?)",
                (root_id, normalized, utc_now()),
            )
            stored = db.execute(
                "SELECT * FROM roots WHERE path = ?", (normalized,)
            ).fetchone()
            if stored is None:
                raise RuntimeError(f"Could not register Workspace root: {normalized}")
            return dict(stored)

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
        workspace_kind: str = "workspace",
    ) -> dict[str, Any]:
        if workspace_kind not in WORKSPACE_KINDS:
            raise ValueError(f"Unsupported Workspace kind: {workspace_kind}")
        workspace_id = uuid.uuid4().hex
        tmux_session = tmux_session or f"termroom-{workspace_id[:12]}"
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO workspaces(
                    id, root_id, relative_path, display_name, tmux_session, last_opened_at,
                    backend_kind, computer_id, canonical_path, workspace_kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    workspace_kind,
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

    def find_remote_workspace(
        self,
        computer_id: str,
        canonical_path: str,
        *,
        workspace_kind: str = "workspace",
    ) -> dict[str, Any] | None:
        if workspace_kind not in WORKSPACE_KINDS:
            raise ValueError(f"Unsupported Workspace kind: {workspace_kind}")
        with self.connect() as db:
            row = db.execute(
                """
                SELECT * FROM workspaces
                WHERE backend_kind = 'ssh'
                  AND computer_id = ?
                  AND canonical_path = ?
                  AND workspace_kind = ?
                """,
                (computer_id, canonical_path, workspace_kind),
            ).fetchone()
            return dict(row) if row else None

    def find_server_terminal_workspace(self, computer_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT * FROM workspaces
                WHERE backend_kind = 'ssh'
                  AND computer_id = ?
                  AND workspace_kind = 'server_terminal'
                """,
                (computer_id,),
            ).fetchone()
            return dict(row) if row else None

    def update_workspace_kind(self, workspace_id: str, workspace_kind: str) -> None:
        if workspace_kind not in WORKSPACE_KINDS:
            raise ValueError(f"Unsupported Workspace kind: {workspace_kind}")
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE workspaces SET workspace_kind = ? WHERE id = ?",
                (workspace_kind, workspace_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown Workspace: {workspace_id}")

    def list_recent_workspaces(self, limit: int | None = 20) -> list[dict[str, Any]]:
        limit_clause = "" if limit is None else "LIMIT ?"
        parameters: tuple[object, ...] = () if limit is None else (limit,)
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT w.*, r.path AS root_path
                FROM workspaces AS w
                JOIN roots AS r ON r.id = w.root_id
                WHERE w.workspace_kind = 'workspace'
                  AND NOT EXISTS (
                    SELECT 1 FROM remote_runs AS rr WHERE rr.workspace_id = w.id
                )
                ORDER BY w.last_opened_at DESC
                {limit_clause}
                """,
                parameters,
            ).fetchall()
            return [dict(row) for row in rows]

    def list_workspaces_for_computer(self, computer_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM workspaces
                WHERE computer_id = ?
                  AND workspace_kind = 'workspace'
                  AND NOT EXISTS (
                      SELECT 1 FROM remote_runs AS rr WHERE rr.workspace_id = workspaces.id
                  )
                ORDER BY last_opened_at DESC
                """,
                (computer_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_registered_workspaces_for_computer(
        self, computer_id: str
    ) -> list[dict[str, Any]]:
        """Return internal and user-visible bridge rows for registration cleanup."""

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
                WHERE w.root_id = ?
                  AND w.backend_kind = 'local'
                  AND w.workspace_kind = 'workspace'
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
                WHERE w.backend_kind = 'local' AND w.workspace_kind = 'workspace'
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

    def create_remote_run(
        self,
        values: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Insert a Remote Run or return the row already claimed by this UUID."""

        columns = (
            "id",
            "source_kind",
            "source_workspace_id",
            "source_path",
            "source_label",
            "source_url",
            "source_options_json",
            "source_revision",
            "source_size",
            "target_computer_id",
            "command",
            "run_base",
            "workspace_id",
            "state",
            "phase",
            "created_at",
            "expires_at",
        )
        row_values = tuple(values.get(column) for column in columns)
        with self.connect() as db:
            cursor = db.execute(
                f"""
                INSERT INTO remote_runs({", ".join(columns)})
                VALUES ({", ".join("?" for _ in columns)})
                ON CONFLICT(id) DO NOTHING
                """,
                row_values,
            )
            row = db.execute(
                "SELECT * FROM remote_runs WHERE id = ?", (values["id"],)
            ).fetchone()
            if row is None:
                raise RuntimeError("Remote Run insert did not produce a row")
            return dict(row), cursor.rowcount == 1

    def get_remote_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM remote_runs WHERE id = ?", (run_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_remote_run_for_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM remote_runs WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_workspace_for_remote_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT w.*, roots.path AS root_path
                FROM remote_runs AS rr
                JOIN workspaces AS w ON w.id = rr.workspace_id
                JOIN roots ON roots.id = w.root_id
                WHERE rr.id = ?
                """,
                (run_id,),
            ).fetchone()
            return dict(row) if row else None

    def attach_remote_run_workspace(
        self,
        run_id: str,
        workspace_id: str,
    ) -> dict[str, Any]:
        """Attach one SSH Workspace shell to one Remote Run idempotently."""

        with self.connect() as db:
            run = db.execute(
                "SELECT * FROM remote_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(f"Unknown Remote Run: {run_id}")
            workspace = db.execute(
                "SELECT * FROM workspaces WHERE id = ?",
                (workspace_id,),
            ).fetchone()
            if workspace is None:
                raise KeyError(f"Unknown Workspace: {workspace_id}")
            if workspace["backend_kind"] != "ssh" or (
                str(workspace["computer_id"] or "")
                != str(run["target_computer_id"])
            ):
                raise ValueError("Remote Run Workspace must use its SSH target computer")

            attached = run["workspace_id"]
            if attached is not None and str(attached) != workspace_id:
                raise RuntimeError("Remote Run already has a different Workspace")
            owner = db.execute(
                "SELECT id FROM remote_runs WHERE workspace_id = ? AND id != ?",
                (workspace_id, run_id),
            ).fetchone()
            if owner is not None:
                raise RuntimeError("Workspace is already attached to another Remote Run")
            if attached is None:
                db.execute(
                    "UPDATE remote_runs SET workspace_id = ? WHERE id = ?",
                    (workspace_id, run_id),
                )
            updated = db.execute(
                "SELECT * FROM remote_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            return dict(updated)

    def detach_remote_run_workspace(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
    ) -> str | None:
        """Detach and return the transient Workspace id without deleting it."""

        with self.connect() as db:
            row = db.execute(
                "SELECT workspace_id FROM remote_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown Remote Run: {run_id}")
            attached = row["workspace_id"]
            if attached is None:
                return None
            attached_id = str(attached)
            if workspace_id is not None and workspace_id != attached_id:
                return None
            db.execute(
                "UPDATE remote_runs SET workspace_id = NULL WHERE id = ? AND workspace_id = ?",
                (run_id, attached_id),
            )
            return attached_id

    def delete_remote_run_workspace(self, run_id: str) -> bool:
        """Detach and delete a Run's transient Workspace and terminal rows."""

        with self.connect() as db:
            row = db.execute(
                "SELECT workspace_id FROM remote_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown Remote Run: {run_id}")
            workspace_id = row["workspace_id"]
            if workspace_id is None:
                return False
            db.execute(
                "UPDATE remote_runs SET workspace_id = NULL WHERE id = ?",
                (run_id,),
            )
            db.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
            return True

    def list_recent_remote_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM remote_runs
                ORDER BY
                    CASE WHEN state IN ('preparing', 'running') THEN 0 ELSE 1 END,
                    created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_remote_runs_for_computer(self, computer_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM remote_runs
                WHERE target_computer_id = ?
                ORDER BY created_at DESC
                """,
                (computer_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_expired_remote_runs(self, now: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM remote_runs
                WHERE state IN ('finished', 'stopped', 'failed', 'lost')
                  AND expires_at IS NOT NULL
                  AND expires_at <= ?
                ORDER BY expires_at
                """,
                (now,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_abandoned_remote_run_uploads(self, now: str) -> list[dict[str, Any]]:
        """Return ZIP Runs whose browser upload window elapsed before starting."""

        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM remote_runs
                WHERE state = 'preparing'
                  AND phase = 'waiting_upload'
                  AND source_kind = 'zip'
                  AND expires_at IS NOT NULL
                  AND expires_at <= ?
                ORDER BY expires_at
                """,
                (now,),
            ).fetchall()
            return [dict(row) for row in rows]

    def transition_remote_run(
        self,
        run_id: str,
        *,
        expected_states: set[str] | frozenset[str],
        expected_phase: str | None | object = ...,
        state: str | object = ...,
        phase: str | None | object = ...,
        started_at: str | None | object = ...,
        stop_requested_at: str | None | object = ...,
        ended_at: str | None | object = ...,
        expires_at: str | None | object = ...,
        exit_code: int | None | object = ...,
        error_code: str | None | object = ...,
        error_detail: str | None | object = ...,
        source_revision: str | None | object = ...,
        source_size: int | None | object = ...,
    ) -> bool:
        """Compare-and-set a Remote Run lifecycle row."""

        allowed_states = {
            "preparing",
            "running",
            "finished",
            "stopped",
            "failed",
            "lost",
        }
        if not expected_states or not expected_states <= allowed_states:
            raise ValueError("Invalid expected Remote Run states")
        if state is not ... and state not in allowed_states:
            raise ValueError("Invalid Remote Run state")

        assignments: list[str] = []
        parameters: list[Any] = []
        updates = {
            "state": state,
            "phase": phase,
            "started_at": started_at,
            "stop_requested_at": stop_requested_at,
            "ended_at": ended_at,
            "expires_at": expires_at,
            "exit_code": exit_code,
            "error_code": error_code,
            "error_detail": error_detail,
            "source_revision": source_revision,
            "source_size": source_size,
        }
        for column, value in updates.items():
            if value is ...:
                continue
            assignments.append(f"{column} = ?")
            parameters.append(value)
        if not assignments:
            return False

        ordered_states = sorted(expected_states)
        where = [
            "id = ?",
            f"state IN ({', '.join('?' for _ in ordered_states)})",
        ]
        parameters.append(run_id)
        parameters.extend(ordered_states)
        if expected_phase is not ...:
            if expected_phase is None:
                where.append("phase IS NULL")
            else:
                where.append("phase = ?")
                parameters.append(expected_phase)

        with self.connect() as db:
            cursor = db.execute(
                f"UPDATE remote_runs SET {', '.join(assignments)} WHERE {' AND '.join(where)}",
                tuple(parameters),
            )
            return cursor.rowcount == 1

    def expire_remote_run_now(self, run_id: str, now: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE remote_runs
                SET expires_at = ?
                WHERE id = ?
                  AND state IN ('finished', 'stopped', 'failed', 'lost')
                """,
                (now, run_id),
            )
            return cursor.rowcount == 1

    def delete_remote_run(self, run_id: str) -> None:
        with self.connect() as db:
            row = db.execute(
                "SELECT workspace_id FROM remote_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is not None and row["workspace_id"] is not None:
                raise RuntimeError("Delete the Remote Run Workspace before its record")
            db.execute("DELETE FROM remote_runs WHERE id = ?", (run_id,))

    def update_computer_run_base(self, computer_id: str, run_base_dir: str | None) -> None:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE computers SET run_base_dir = ? WHERE id = ?",
                (run_base_dir, computer_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown computer: {computer_id}")

    def update_computer_name(self, computer_id: str, name: str) -> dict[str, Any]:
        safe_name = normalize_computer_name(name)
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE computers SET name = ? WHERE id = ?",
                (safe_name, computer_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown computer: {computer_id}")
            row = db.execute(
                "SELECT * FROM computers WHERE id = ?", (computer_id,)
            ).fetchone()
            return dict(row)

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
                    normalize_computer_name(name.strip() or host[:80]),
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
            run_count = db.execute(
                "SELECT COUNT(*) AS count FROM remote_runs WHERE target_computer_id = ?",
                (computer_id,),
            ).fetchone()["count"]
            if run_count:
                raise RuntimeError("Delete this computer's Remote Runs first")
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
            run_count = db.execute(
                "SELECT COUNT(*) AS count FROM remote_runs WHERE target_computer_id = ?",
                (computer_id,),
            ).fetchone()["count"]
            if run_count:
                raise RuntimeError("Delete this computer's Remote Runs first")
            rows = db.execute(
                "SELECT id FROM workspaces WHERE computer_id = ?",
                (computer_id,),
            ).fetchall()
            workspace_ids = [str(row["id"]) for row in rows]
            db.execute("DELETE FROM workspaces WHERE computer_id = ?", (computer_id,))
            db.execute("DELETE FROM roots WHERE path = ?", (f"ssh://{computer_id}",))
            db.execute("DELETE FROM computers WHERE id = ?", (computer_id,))
            return workspace_ids
