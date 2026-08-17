from __future__ import annotations

import json
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

WORKSPACE_KINDS = frozenset({"workspace", "remote_run", "server_terminal"})
WORKSPACE_BACKEND_KINDS = frozenset({"local", "remote"})
REMOTE_RUN_TERMINAL_STATES = frozenset({"finished", "stopped", "failed", "lost"})
FILE_RUN_STATES = frozenset(
    {"preparing", "running", "finished", "stopped", "failed", "lost"}
)
FILE_RUN_TERMINAL_STATES = frozenset({"finished", "stopped", "failed", "lost"})
TERMINAL_ROLES = frozenset({"shell", "file_run", "remote_run"})
SQLITE_MAX_INTEGER = (1 << 63) - 1
TERMINAL_ACTIVITY_READ_RETENTION = timedelta(days=30)
TERMINAL_ACTIVITY_CLOCK_SKEW_ALLOWANCE = timedelta(days=1)
TERMINAL_ACTIVITY_READ_CLEANUP_INTERVAL_SECONDS = 60 * 60
MAX_WORKSPACE_COMMANDS = 3
MAX_WORKSPACE_COMMAND_BYTES = 4096


def normalize_workspace_commands(values: Iterable[object]) -> tuple[str, ...]:
    """Validate the compact, explicit command list stored on a Workspace."""

    commands: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("Workspace commands must be text")
        if any(
            unicodedata.category(character) in {"Cc", "Zl", "Zp"}
            for character in value
        ):
            raise ValueError("Workspace commands must be one line without control characters")
        command = value.strip()
        if not command:
            continue
        if len(command.encode("utf-8")) > MAX_WORKSPACE_COMMAND_BYTES:
            raise ValueError("Workspace command exceeds 4096 UTF-8 bytes")
        commands.append(command)
    if len(commands) > MAX_WORKSPACE_COMMANDS:
        raise ValueError("A Workspace can store at most three commands")
    return tuple(commands)


def _computers_table_sql(
    table: str = "computers", *, if_not_exists: bool = False
) -> str:
    clause = "IF NOT EXISTS " if if_not_exists else ""
    return f"""
        CREATE TABLE {clause}{table} (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            connection_method TEXT NOT NULL
                CHECK(connection_method IN ('ssh', 'node')),
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
            last_seen_at TEXT,
            last_error TEXT,
            run_base_dir TEXT,
            node_public_key TEXT NOT NULL DEFAULT '',
            node_fingerprint TEXT NOT NULL DEFAULT '',
            node_protocol_version INTEGER,
            node_capabilities_json TEXT NOT NULL DEFAULT '[]',
            node_revoked_at TEXT
        );
    """


def _remote_runs_table_sql(
    table: str = "remote_runs", *, if_not_exists: bool = False
) -> str:
    clause = "IF NOT EXISTS " if if_not_exists else ""
    return f"""
        CREATE TABLE {clause}{table} (
            id TEXT PRIMARY KEY,
            source_kind TEXT NOT NULL
                CHECK(source_kind IN ('workspace', 'git', 'archive')),
            archive_format TEXT,
            source_workspace_id TEXT
                REFERENCES workspaces(id) ON DELETE SET NULL,
            source_path TEXT,
            source_label TEXT NOT NULL,
            source_url TEXT,
            source_options_json TEXT NOT NULL DEFAULT '{{}}',
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
            lifecycle_revision INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT,
            stop_requested_at TEXT,
            ended_at TEXT,
            expires_at TEXT,
            CHECK(
                (source_kind = 'archive' AND archive_format = 'zip')
                OR (source_kind != 'archive' AND archive_format IS NULL)
            )
        );
    """


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
        self._terminal_activity_cleanup_at = 0.0

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
                f"""
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
                    workspace_commands_json TEXT NOT NULL DEFAULT '[]',
                    UNIQUE(root_id, relative_path)
                );

                {_computers_table_sql(if_not_exists=True)}

                CREATE TABLE IF NOT EXISTS terminals (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    tmux_window TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'shell'
                        CHECK(role IN ('shell', 'file_run', 'remote_run')),
                    managed_run_id TEXT,
                    created_at TEXT NOT NULL,
                    last_opened_at TEXT NOT NULL,
                    last_output_at TEXT,
                    activity_at INTEGER,
                    UNIQUE(workspace_id, tmux_window)
                );

                CREATE TABLE IF NOT EXISTS terminal_activity_reads (
                    terminal_id TEXT NOT NULL
                        REFERENCES terminals(id) ON DELETE CASCADE,
                    device_id TEXT NOT NULL,
                    acknowledged_activity_at INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(terminal_id, device_id)
                );

                CREATE TABLE IF NOT EXISTS command_history (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                    terminal_id TEXT NOT NULL REFERENCES terminals(id) ON DELETE CASCADE,
                    command TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS file_runs (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL
                        REFERENCES workspaces(id) ON DELETE CASCADE,
                    idempotency_key TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    runner_id TEXT NOT NULL,
                    runner_version INTEGER NOT NULL,
                    argv_json TEXT NOT NULL,
                    terminal_id TEXT REFERENCES terminals(id) ON DELETE SET NULL,
                    state TEXT NOT NULL
                        CHECK(state IN (
                            'preparing', 'running', 'finished',
                            'stopped', 'failed', 'lost'
                        )),
                    lifecycle_revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    stop_requested_at TEXT,
                    ended_at TEXT,
                    exit_code INTEGER,
                    error_code TEXT,
                    error_detail TEXT,
                    UNIQUE(workspace_id, idempotency_key)
                );

                {_remote_runs_table_sql(if_not_exists=True)}

                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    subject_type TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    subject_revision INTEGER NOT NULL,
                    primary_label TEXT NOT NULL,
                    secondary_label TEXT NOT NULL,
                    exit_code INTEGER,
                    duration_seconds INTEGER,
                    occurred_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    read_at TEXT,
                    notify INTEGER NOT NULL DEFAULT 1 CHECK(notify IN (0, 1)),
                    UNIQUE(subject_type, subject_id, subject_revision)
                );

                CREATE TABLE IF NOT EXISTS event_reads (
                    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    device_id TEXT NOT NULL,
                    read_at TEXT NOT NULL,
                    PRIMARY KEY(event_id, device_id)
                );

                CREATE TABLE IF NOT EXISTS notification_devices (
                    id TEXT PRIMARY KEY,
                    start_sequence INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS event_notification_claims (
                    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    device_id TEXT NOT NULL
                        REFERENCES notification_devices(id) ON DELETE CASCADE,
                    claimed_at TEXT NOT NULL,
                    PRIMARY KEY(event_id, device_id)
                );

                CREATE INDEX IF NOT EXISTS idx_workspaces_recent
                    ON workspaces(last_opened_at DESC);
                CREATE INDEX IF NOT EXISTS idx_history_recent
                    ON command_history(workspace_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_file_runs_workspace_recent
                    ON file_runs(workspace_id, created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_file_runs_workspace_active
                    ON file_runs(workspace_id)
                    WHERE state IN ('preparing', 'running');

                CREATE INDEX IF NOT EXISTS idx_computers_name
                    ON computers(name COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_remote_runs_recent
                    ON remote_runs(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_remote_runs_target
                    ON remote_runs(target_computer_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_remote_runs_expired
                    ON remote_runs(expires_at)
                    WHERE expires_at IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_events_recent
                    ON events(sequence DESC);
                CREATE INDEX IF NOT EXISTS idx_events_unread
                    ON events(sequence DESC)
                    WHERE read_at IS NULL;
                CREATE INDEX IF NOT EXISTS idx_event_notification_claims_device
                    ON event_notification_claims(device_id, claimed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_event_reads_device
                    ON event_reads(device_id, read_at DESC);
                CREATE INDEX IF NOT EXISTS idx_terminal_activity_reads_updated_at
                    ON terminal_activity_reads(updated_at);
                """
            )
            self._ensure_column(db, "terminals", "last_output_at", "TEXT")
            self._ensure_column(db, "terminals", "activity_at", "INTEGER")
            self._ensure_column(
                db, "terminals", "role", "TEXT NOT NULL DEFAULT 'shell'"
            )
            self._ensure_column(db, "terminals", "managed_run_id", "TEXT")
            self._ensure_column(db, "events", "duration_seconds", "INTEGER")
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
                db,
                "workspaces",
                "workspace_commands_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column(
                db, "computers", "auth_kind", "TEXT NOT NULL DEFAULT 'key'"
            )
            self._ensure_column(db, "computers", "run_base_dir", "TEXT")
            self._ensure_column(db, "computers", "last_seen_at", "TEXT")
            self._ensure_column(
                db, "computers", "node_public_key", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                db, "computers", "node_fingerprint", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(db, "computers", "node_protocol_version", "INTEGER")
            self._ensure_column(
                db,
                "computers",
                "node_capabilities_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column(db, "computers", "node_revoked_at", "TEXT")
            self._ensure_column(
                db,
                "remote_runs",
                "workspace_id",
                "TEXT REFERENCES workspaces(id) ON DELETE SET NULL",
            )
            self._ensure_column(
                db,
                "remote_runs",
                "lifecycle_revision",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._migrate_remote_archive_model(db)
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS node_pairing_codes (
                    id TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS node_enrollments (
                    id TEXT PRIMARY KEY,
                    pairing_code_id TEXT NOT NULL UNIQUE
                        REFERENCES node_pairing_codes(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    public_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    protocol_version INTEGER NOT NULL,
                    polling_secret_hash TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('pending', 'approved', 'rejected')),
                    computer_id TEXT REFERENCES computers(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_computers_name
                    ON computers(name COLLATE NOCASE);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_computers_node_public_key
                    ON computers(node_public_key)
                    WHERE connection_method = 'node';
                CREATE INDEX IF NOT EXISTS idx_node_pairing_codes_expiry
                    ON node_pairing_codes(expires_at);
                CREATE INDEX IF NOT EXISTS idx_node_enrollments_status
                    ON node_enrollments(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_remote_runs_recent
                    ON remote_runs(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_remote_runs_target
                    ON remote_runs(target_computer_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_remote_runs_expired
                    ON remote_runs(expires_at)
                    WHERE expires_at IS NOT NULL;
                """
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
            db.execute("DROP INDEX IF EXISTS idx_remote_workspace_unique")
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_remote_workspace_unique
                ON workspaces(computer_id, canonical_path, workspace_kind)
                WHERE backend_kind = 'remote'
                """
            )
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_server_terminal_computer
                ON workspaces(computer_id)
                WHERE workspace_kind = 'server_terminal'
                """
            )
            db.execute(
                """
                UPDATE terminals
                SET role = 'remote_run',
                    managed_run_id = (
                        SELECT remote_runs.id
                        FROM remote_runs
                        WHERE remote_runs.workspace_id = terminals.workspace_id
                    )
                WHERE role = 'shell'
                  AND name = 'run'
                  AND workspace_id IN (
                      SELECT workspace_id
                      FROM remote_runs
                      WHERE workspace_id IS NOT NULL
                  )
                """
            )
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_terminals_managed_role
                ON terminals(workspace_id, role)
                WHERE role != 'shell'
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
            for row in db.execute(
                """
                SELECT rr.*, computers.name AS target_name
                FROM remote_runs AS rr
                JOIN computers ON computers.id = rr.target_computer_id
                WHERE rr.state IN ('finished', 'stopped', 'failed', 'lost')
                ORDER BY rr.created_at
                """
            ).fetchall():
                self._insert_remote_run_event(db, row, historical=True)
        self.path.chmod(0o600)

    @staticmethod
    def _ensure_column(
        db: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {str(row["name"]) for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _migrate_remote_archive_model(db: sqlite3.Connection) -> None:
        """Atomically replace the legacy SSH/ZIP persistence vocabulary."""

        computer_columns = {
            str(row["name"]) for row in db.execute("PRAGMA table_info(computers)")
        }
        remote_run_columns = {
            str(row["name"]) for row in db.execute("PRAGMA table_info(remote_runs)")
        }
        remote_run_schema_row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'remote_runs'"
        ).fetchone()
        remote_run_schema = " ".join(
            str(remote_run_schema_row["sql"] or "").lower().split()
        )
        rebuild_computers = "connection_method" not in computer_columns
        rebuild_remote_runs = rebuild_computers or (
            "archive_format" not in remote_run_columns
            or "source_kind in ('workspace', 'git', 'archive')"
            not in remote_run_schema
        )
        legacy_workspace = db.execute(
            "SELECT 1 FROM workspaces WHERE backend_kind = 'ssh' LIMIT 1"
        ).fetchone()
        if not rebuild_computers and not rebuild_remote_runs and legacy_workspace is None:
            return

        computer_method_column = "kind" if rebuild_computers else "connection_method"
        invalid_computer = db.execute(
            f"SELECT id FROM computers WHERE {computer_method_column} "
            "NOT IN ('ssh', 'node') LIMIT 1"
        ).fetchone()
        if invalid_computer is not None:
            raise RuntimeError("Cannot migrate an unsupported Remote connection method")
        invalid_workspace = db.execute(
            """
            SELECT id FROM workspaces
            WHERE backend_kind NOT IN ('local', 'ssh', 'remote')
            LIMIT 1
            """
        ).fetchone()
        if invalid_workspace is not None:
            raise RuntimeError("Cannot migrate an unsupported Workspace backend")
        invalid_source = db.execute(
            """
            SELECT id FROM remote_runs
            WHERE source_kind NOT IN ('workspace', 'git', 'zip', 'archive')
            LIMIT 1
            """
        ).fetchone()
        if invalid_source is not None:
            raise RuntimeError("Cannot migrate an unsupported Remote Run Source")
        if "archive_format" in remote_run_columns:
            invalid_archive = db.execute(
                """
                SELECT id FROM remote_runs
                WHERE archive_format IS NOT NULL AND archive_format != 'zip'
                LIMIT 1
                """
            ).fetchone()
            if invalid_archive is not None:
                raise RuntimeError("Cannot migrate an unsupported Archive format")

        db.execute("SAVEPOINT remote_archive_model")
        try:
            if rebuild_computers:
                db.execute("ALTER TABLE computers RENAME TO computers_legacy_remote_model")
                db.execute(_computers_table_sql())
                db.execute(
                    """
                    INSERT INTO computers(
                        id, name, connection_method, auth_kind, ssh_alias, host, port,
                        username, identity_file, host_key_type, host_key_data,
                        host_fingerprint, created_at, last_connected_at, last_error,
                        run_base_dir
                    )
                    SELECT
                        id, name, kind, auth_kind, ssh_alias, host, port,
                        username, identity_file, host_key_type, host_key_data,
                        host_fingerprint, created_at, last_connected_at, last_error,
                        run_base_dir
                    FROM computers_legacy_remote_model
                    """
                )

            if rebuild_remote_runs:
                db.execute("ALTER TABLE remote_runs RENAME TO remote_runs_legacy_remote_model")
                db.execute(_remote_runs_table_sql())
                archive_format = (
                    "CASE "
                    "WHEN source_kind IN ('zip', 'archive') "
                    "THEN COALESCE(archive_format, 'zip') ELSE NULL END"
                    if "archive_format" in remote_run_columns
                    else "CASE WHEN source_kind = 'zip' THEN 'zip' ELSE NULL END"
                )
                db.execute(
                    f"""
                    INSERT INTO remote_runs(
                        id, source_kind, archive_format, source_workspace_id,
                        source_path, source_label, source_url, source_options_json,
                        source_revision, source_size, target_computer_id, command,
                        run_base, workspace_id, state, phase, exit_code, error_code,
                        error_detail, lifecycle_revision, created_at, started_at,
                        stop_requested_at, ended_at, expires_at
                    )
                    SELECT
                        id,
                        CASE WHEN source_kind = 'zip' THEN 'archive' ELSE source_kind END,
                        {archive_format},
                        source_workspace_id, source_path, source_label, source_url,
                        source_options_json, source_revision, source_size,
                        target_computer_id, command, run_base, workspace_id, state,
                        phase, exit_code, error_code, error_detail,
                        lifecycle_revision, created_at, started_at,
                        stop_requested_at, ended_at, expires_at
                    FROM remote_runs_legacy_remote_model
                    """
                )

            db.execute(
                "UPDATE workspaces SET backend_kind = 'remote' WHERE backend_kind = 'ssh'"
            )
            if rebuild_remote_runs:
                db.execute("DROP TABLE remote_runs_legacy_remote_model")
            if rebuild_computers:
                db.execute("DROP TABLE computers_legacy_remote_model")
            foreign_key_issue = db.execute("PRAGMA foreign_key_check").fetchone()
            if foreign_key_issue is not None:
                raise RuntimeError("Remote/Archive migration violated a foreign key")
        except Exception:
            db.execute("ROLLBACK TO remote_archive_model")
            db.execute("RELEASE remote_archive_model")
            raise
        db.execute("RELEASE remote_archive_model")

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
                  AND path NOT LIKE 'node://%'
                ORDER BY created_at, path COLLATE NOCASE
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def workspace_location_counts(self) -> tuple[dict[str, int], dict[str, int]]:
        """Count user-visible Workspaces per Local root and computer in one query."""

        with self.connect() as db:
            rows = db.execute(
                """
                SELECT 'root' AS location_kind, w.root_id AS location_id,
                       COUNT(*) AS workspace_count
                FROM workspaces AS w
                JOIN roots AS r ON r.id = w.root_id
                WHERE w.backend_kind = 'local'
                  AND w.workspace_kind = 'workspace'
                  AND r.path NOT LIKE 'ssh://%'
                  AND r.path NOT LIKE 'node://%'
                GROUP BY w.root_id

                UNION ALL

                SELECT 'computer' AS location_kind, c.id AS location_id,
                       COUNT(*) AS workspace_count
                FROM computers AS c
                JOIN workspaces AS w ON w.computer_id = c.id
                WHERE w.workspace_kind = 'workspace'
                  AND NOT EXISTS (
                    SELECT 1 FROM remote_runs AS rr WHERE rr.workspace_id = w.id
                  )
                GROUP BY c.id
                """
            ).fetchall()

        root_counts: dict[str, int] = {}
        computer_counts: dict[str, int] = {}
        for row in rows:
            count = int(row["workspace_count"])
            if row["location_kind"] == "root":
                root_counts[str(row["location_id"])] = count
            else:
                computer_counts[str(row["location_id"])] = count
        return root_counts, computer_counts

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
        if backend_kind not in WORKSPACE_BACKEND_KINDS:
            raise ValueError(f"Unsupported Workspace backend: {backend_kind}")
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

    @staticmethod
    def workspace_commands_from(workspace: Mapping[str, Any]) -> tuple[str, ...]:
        raw = workspace.get("workspace_commands_json") or "[]"
        try:
            values = json.loads(str(raw))
        except json.JSONDecodeError as exc:
            raise RuntimeError("Stored Workspace commands are invalid") from exc
        if not isinstance(values, list):
            raise RuntimeError("Stored Workspace commands are invalid")
        try:
            commands = normalize_workspace_commands(values)
        except ValueError as exc:
            raise RuntimeError("Stored Workspace commands are invalid") from exc
        if list(commands) != values:
            raise RuntimeError("Stored Workspace commands are invalid")
        return commands

    def list_workspace_commands(self, workspace_id: str) -> tuple[str, ...]:
        workspace = self.get_workspace(workspace_id)
        if workspace is None:
            raise KeyError(f"Unknown Workspace: {workspace_id}")
        return self.workspace_commands_from(workspace)

    def replace_workspace_commands(
        self, workspace_id: str, values: Iterable[object]
    ) -> tuple[str, ...]:
        commands = normalize_workspace_commands(values)
        encoded = json.dumps(commands, ensure_ascii=False, separators=(",", ":"))
        with self.connect() as db:
            row = db.execute(
                "SELECT workspace_kind FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown Workspace: {workspace_id}")
            if row["workspace_kind"] != "workspace":
                raise ValueError("Commands are available only for persistent Workspaces")
            db.execute(
                "UPDATE workspaces SET workspace_commands_json = ? WHERE id = ?",
                (encoded, workspace_id),
            )
        return commands

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
                WHERE backend_kind = 'remote'
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
                WHERE backend_kind = 'remote'
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

    def terminal_activity_workspaces(
        self, workspace_ids: Iterable[str]
    ) -> list[dict[str, Any]]:
        """Load an exact persistent Workspace scope in a constant number of queries."""

        unique_ids = list(dict.fromkeys(str(value) for value in workspace_ids))
        if not unique_ids:
            return []
        placeholders = ",".join("?" for _ in unique_ids)
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT w.*
                FROM workspaces AS w
                WHERE w.id IN ({placeholders})
                  AND w.workspace_kind = 'workspace'
                  AND NOT EXISTS (
                    SELECT 1 FROM remote_runs AS rr WHERE rr.workspace_id = w.id
                  )
                """,
                unique_ids,
            ).fetchall()
            workspaces = {str(row["id"]): dict(row) for row in rows}
            computer_ids = list(
                dict.fromkeys(
                    str(item["computer_id"])
                    for item in workspaces.values()
                    if item.get("backend_kind") == "remote"
                    and item.get("computer_id")
                )
            )
            computers: dict[str, dict[str, Any]] = {}
            if computer_ids:
                computer_placeholders = ",".join("?" for _ in computer_ids)
                computer_rows = db.execute(
                    f"SELECT * FROM computers WHERE id IN ({computer_placeholders})",
                    computer_ids,
                ).fetchall()
                computers = {str(row["id"]): dict(row) for row in computer_rows}

        result: list[dict[str, Any]] = []
        for workspace_id in unique_ids:
            workspace = workspaces.get(workspace_id)
            if workspace is None:
                continue
            if workspace.get("backend_kind") == "remote":
                computer = computers.get(str(workspace.get("computer_id") or ""))
                if computer is None:
                    continue
                workspace["computer"] = computer
            result.append(workspace)
        return result

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

    def update_workspace_display_name(self, workspace_id: str, display_name: str) -> None:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE workspaces SET display_name = ? WHERE id = ?",
                (display_name, workspace_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown Workspace: {workspace_id}")

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

    @staticmethod
    def _terminal_role(value: Any) -> str:
        role = str(value or "shell")
        if role not in TERMINAL_ROLES:
            raise ValueError(f"Unsupported Terminal role: {role}")
        return role

    @staticmethod
    def _terminal_activity_revision(
        value: Any,
        *,
        allow_none: bool = False,
    ) -> int | None:
        """Validate an exact provider revision without changing its unit."""

        if value is None and allow_none:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Terminal activity revision is invalid")
        if value > SQLITE_MAX_INTEGER:
            raise ValueError("Terminal activity revision is invalid")
        return value

    def create_terminal(
        self,
        workspace_id: str,
        name: str,
        tmux_window: str,
        *,
        role: str = "shell",
        managed_run_id: str | None = None,
        activity_at: int | None = None,
    ) -> dict[str, Any]:
        safe_role = self._terminal_role(role)
        safe_activity_at = self._terminal_activity_revision(
            activity_at, allow_none=True
        )
        safe_managed_run_id = str(managed_run_id) if managed_run_id else None
        if safe_role == "shell":
            safe_managed_run_id = None
        terminal_id = uuid.uuid4().hex
        now = utc_now()
        with self.connect() as db:
            db.execute(
                """
                INSERT INTO terminals(
                    id, workspace_id, name, tmux_window, role, managed_run_id,
                    created_at, last_opened_at, activity_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    terminal_id,
                    workspace_id,
                    name,
                    tmux_window,
                    safe_role,
                    safe_managed_run_id,
                    now,
                    now,
                    safe_activity_at,
                ),
            )
            row = db.execute("SELECT * FROM terminals WHERE id = ?", (terminal_id,)).fetchone()
            return dict(row)

    def get_managed_terminal(
        self, workspace_id: str, role: str
    ) -> dict[str, Any] | None:
        safe_role = self._terminal_role(role)
        if safe_role == "shell":
            raise ValueError("A shell Terminal is not a managed slot")
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM terminals WHERE workspace_id = ? AND role = ?",
                (workspace_id, safe_role),
            ).fetchone()
            return dict(row) if row else None

    def reconcile_terminals(
        self,
        workspace_id: str,
        windows: Iterable[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Project live tmux windows into SQLite without churning stable row IDs."""

        desired: list[dict[str, str | int | None]] = []
        seen_windows: set[str] = set()
        seen_managed_roles: set[str] = set()
        for raw in windows:
            tmux_window = str(raw.get("tmux_window") or "")
            if not tmux_window or tmux_window in seen_windows:
                raise ValueError("tmux exposed an invalid or duplicate window identity")
            seen_windows.add(tmux_window)
            role = self._terminal_role(raw.get("role"))
            managed_run_id = str(raw.get("managed_run_id") or "") or None
            try:
                activity_at = self._terminal_activity_revision(
                    raw.get("activity_at"), allow_none=True
                )
            except ValueError as exc:
                raise ValueError(
                    "tmux exposed an invalid Terminal activity revision"
                ) from exc
            if role == "shell":
                managed_run_id = None
            elif role in seen_managed_roles:
                raise ValueError(f"tmux exposed more than one {role} Terminal")
            else:
                seen_managed_roles.add(role)
            desired.append(
                {
                    "tmux_window": tmux_window,
                    "name": str(raw.get("name") or "shell"),
                    "role": role,
                    "managed_run_id": managed_run_id,
                    "activity_at": activity_at,
                }
            )

        now = utc_now()
        with self.connect() as db:
            stored_rows = db.execute(
                "SELECT * FROM terminals WHERE workspace_id = ? ORDER BY created_at",
                (workspace_id,),
            ).fetchall()
            stored = [dict(row) for row in stored_rows]
            by_window = {str(row["tmux_window"]): row for row in stored}
            by_managed_role = {
                str(row["role"]): row
                for row in stored
                if str(row.get("role") or "shell") != "shell"
            }
            claimed_ids: set[str] = set()
            assigned_by_window: dict[str, dict[str, Any]] = {}

            # A managed role is the stable identity. Match those rows before
            # considering tmux window ids so a server/window-id drift cannot
            # silently turn the File Run row into an ordinary shell row.
            for item in desired:
                if item["role"] == "shell":
                    continue
                candidate = by_managed_role.get(str(item["role"]))
                if candidate is None:
                    same_window = by_window.get(str(item["tmux_window"]))
                    if same_window is not None and str(
                        same_window.get("role") or "shell"
                    ) == "shell":
                        candidate = same_window
                if candidate is not None and str(candidate["id"]) not in claimed_ids:
                    claimed_ids.add(str(candidate["id"]))
                    assigned_by_window[str(item["tmux_window"])] = candidate

            # Shell rows may follow their exact live window, but never claim a
            # stored managed row merely because an id was reused.
            for item in desired:
                if item["role"] != "shell":
                    continue
                candidate = by_window.get(str(item["tmux_window"]))
                if not (
                    candidate is not None
                    and str(candidate.get("role") or "shell") == "shell"
                    and str(candidate["id"]) not in claimed_ids
                ):
                    remaining_shells = [
                        row
                        for row in stored
                        if str(row.get("role") or "shell") == "shell"
                        and str(row["id"]) not in claimed_ids
                    ]
                    candidate = next(
                        (
                            row
                            for row in remaining_shells
                            if str(row.get("name") or "shell") == item["name"]
                        ),
                        remaining_shells[0] if remaining_shells else None,
                    )
                if candidate is not None:
                    claimed_ids.add(str(candidate["id"]))
                    assigned_by_window[str(item["tmux_window"])] = candidate

            assignments = [
                (item, assigned_by_window.get(str(item["tmux_window"])))
                for item in desired
            ]

            for row in stored:
                if str(row["id"]) not in claimed_ids:
                    db.execute("DELETE FROM terminals WHERE id = ?", (str(row["id"]),))

            # Temporary values make externally-induced window-id and role swaps
            # safe under both Terminal uniqueness constraints.
            for _item, row in assignments:
                if row is not None:
                    db.execute(
                        """
                        UPDATE terminals
                        SET tmux_window = ?, role = 'shell', managed_run_id = NULL
                        WHERE id = ?
                        """,
                        (f"__termroom_sync__{row['id']}", str(row["id"])),
                    )

            for item, row in assignments:
                if row is None:
                    terminal_id = uuid.uuid4().hex
                    db.execute(
                        """
                        INSERT INTO terminals(
                            id, workspace_id, name, tmux_window, role, managed_run_id,
                            created_at, last_opened_at, activity_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            terminal_id,
                            workspace_id,
                            item["name"],
                            item["tmux_window"],
                            item["role"],
                            item["managed_run_id"],
                            now,
                            now,
                            item["activity_at"],
                        ),
                    )
                else:
                    db.execute(
                        """
                        UPDATE terminals
                        SET name = ?, tmux_window = ?, role = ?, managed_run_id = ?,
                            activity_at = CASE
                                WHEN ? IS NULL THEN activity_at
                                WHEN activity_at IS NULL THEN ?
                                ELSE MAX(activity_at, ?)
                            END
                        WHERE id = ?
                        """,
                        (
                            item["name"],
                            item["tmux_window"],
                            item["role"],
                            item["managed_run_id"],
                            item["activity_at"],
                            item["activity_at"],
                            item["activity_at"],
                            str(row["id"]),
                        ),
                    )
            rows = db.execute(
                "SELECT * FROM terminals WHERE workspace_id = ? ORDER BY created_at",
                (workspace_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def observe_terminal_activity(
        self,
        workspace_id: str,
        records: Iterable[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """Advance cached activity for known shell windows without reconciling inventory."""

        return self.observe_terminal_activity_batch({str(workspace_id): records}).get(
            str(workspace_id), []
        )

    def observe_terminal_activity_batch(
        self,
        records_by_workspace: Mapping[str, Iterable[Mapping[str, Any]]],
    ) -> dict[str, list[dict[str, Any]]]:
        """Advance activity for many workspaces in one SQLite transaction."""

        normalized: dict[str, dict[str, int]] = {}
        for raw_workspace_id, records in records_by_workspace.items():
            workspace_id = str(raw_workspace_id)
            observed: dict[str, int] = {}
            for record in records:
                window = str(record.get("tmux_window") or "")
                try:
                    revision = self._terminal_activity_revision(
                        record.get("activity_at")
                    )
                except ValueError as exc:
                    raise ValueError(
                        "tmux exposed an invalid Terminal activity record"
                    ) from exc
                if not window or window in observed or revision is None:
                    raise ValueError("tmux exposed an invalid Terminal activity record")
                observed[window] = revision
            if observed:
                normalized[workspace_id] = observed
        if not normalized:
            return {}
        observations = [
            (workspace_id, window, revision)
            for workspace_id, observed in normalized.items()
            for window, revision in observed.items()
        ]
        result: dict[str, list[dict[str, Any]]] = {
            workspace_id: [] for workspace_id in normalized
        }
        with self.connect() as db:
            db.execute(
                """
                CREATE TEMP TABLE observed_terminal_activity (
                    workspace_id TEXT NOT NULL,
                    tmux_window TEXT NOT NULL,
                    activity_at INTEGER NOT NULL,
                    PRIMARY KEY(workspace_id, tmux_window)
                ) WITHOUT ROWID
                """
            )
            db.executemany(
                """
                INSERT INTO observed_terminal_activity(
                    workspace_id, tmux_window, activity_at
                ) VALUES (?, ?, ?)
                """,
                observations,
            )
            db.execute(
                """
                UPDATE terminals
                SET activity_at = MAX(
                    COALESCE(terminals.activity_at, 0),
                    (
                        SELECT observed.activity_at
                        FROM observed_terminal_activity AS observed
                        WHERE observed.workspace_id = terminals.workspace_id
                          AND observed.tmux_window = terminals.tmux_window
                    )
                )
                WHERE terminals.role = 'shell'
                  AND EXISTS (
                      SELECT 1
                      FROM observed_terminal_activity AS observed
                      WHERE observed.workspace_id = terminals.workspace_id
                        AND observed.tmux_window = terminals.tmux_window
                  )
                """
            )
            rows = db.execute(
                """
                SELECT terminals.*
                FROM terminals
                JOIN observed_terminal_activity AS observed
                  ON observed.workspace_id = terminals.workspace_id
                 AND observed.tmux_window = terminals.tmux_window
                WHERE terminals.role = 'shell'
                ORDER BY terminals.workspace_id, terminals.created_at
                """
            ).fetchall()
            for row in rows:
                result[str(row["workspace_id"])].append(dict(row))
        return result

    def terminal_activity_targets(
        self, workspace_ids: Iterable[str]
    ) -> dict[str, list[str]]:
        """Return known shell window ids for explicit workspaces in one query."""

        unique_ids = list(dict.fromkeys(str(value) for value in workspace_ids))
        if not unique_ids:
            return {}
        placeholders = ",".join("?" for _ in unique_ids)
        with self.connect() as db:
            rows = db.execute(
                f"""
                SELECT workspace_id, tmux_window
                FROM terminals
                WHERE role = 'shell' AND workspace_id IN ({placeholders})
                ORDER BY created_at
                """,
                unique_ids,
            ).fetchall()
        targets = {workspace_id: [] for workspace_id in unique_ids}
        for row in rows:
            targets[str(row["workspace_id"])].append(str(row["tmux_window"]))
        return targets

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
        """Persist direct output without inventing an activity revision."""

        with self.connect() as db:
            cursor = db.execute(
                "UPDATE terminals SET last_output_at = ? WHERE id = ?",
                (utc_now(), terminal_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Unknown Terminal: {terminal_id}")

    def _cleanup_terminal_activity_reads(self, db: sqlite3.Connection) -> None:
        monotonic_now = time.monotonic()
        if monotonic_now < self._terminal_activity_cleanup_at:
            return
        self._terminal_activity_cleanup_at = (
            monotonic_now + TERMINAL_ACTIVITY_READ_CLEANUP_INTERVAL_SECONDS
        )
        cutoff = datetime.now(UTC) - (
            TERMINAL_ACTIVITY_READ_RETENTION
            + TERMINAL_ACTIVITY_CLOCK_SKEW_ALLOWANCE
        )
        db.execute(
            "DELETE FROM terminal_activity_reads WHERE updated_at < ?",
            (cutoff.isoformat(timespec="seconds"),),
        )

    def terminal_activity_summary(
        self,
        device_id: str,
        *,
        workspace_id: str | None = None,
        workspace_ids: Iterable[str] | None = None,
        terminal_id: str | None = None,
    ) -> dict[str, Any]:
        """Return cached shell activity, baselining a device on first observation."""

        safe_device_id = str(device_id).strip()
        if not safe_device_id:
            raise ValueError("Terminal activity device identity is required")
        conditions = ["terminals.role = 'shell'", "terminals.activity_at IS NOT NULL"]
        parameters: list[object] = []
        if workspace_id is not None:
            conditions.append("terminals.workspace_id = ?")
            parameters.append(str(workspace_id))
        if workspace_ids is not None:
            scoped_workspace_ids = list(
                dict.fromkeys(str(value) for value in workspace_ids)
            )
            if not scoped_workspace_ids:
                conditions.append("0")
            else:
                placeholders = ",".join("?" for _ in scoped_workspace_ids)
                conditions.append(f"terminals.workspace_id IN ({placeholders})")
                parameters.extend(scoped_workspace_ids)
        if terminal_id is not None:
            conditions.append("terminals.id = ?")
            parameters.append(str(terminal_id))
        where = " AND ".join(conditions)
        now = utc_now()
        with self.connect() as db:
            self._cleanup_terminal_activity_reads(db)
            db.execute(
                f"""
                INSERT INTO terminal_activity_reads(
                    terminal_id, device_id, acknowledged_activity_at,
                    created_at, updated_at
                )
                SELECT terminals.id, ?, terminals.activity_at, ?, ?
                FROM terminals
                WHERE {where}
                ON CONFLICT(terminal_id, device_id) DO NOTHING
                """,
                (safe_device_id, now, now, *parameters),
            )
            rows = db.execute(
                f"""
                SELECT terminals.id AS terminal_id, terminals.workspace_id,
                       terminals.activity_at, reads.acknowledged_activity_at
                FROM terminals
                JOIN terminal_activity_reads AS reads
                  ON reads.terminal_id = terminals.id AND reads.device_id = ?
                WHERE {where}
                ORDER BY terminals.activity_at DESC, terminals.created_at DESC
                """,
                (safe_device_id, *parameters),
            ).fetchall()
            terminal_rows = [
                {
                    "terminal_id": str(row["terminal_id"]),
                    "workspace_id": str(row["workspace_id"]),
                    "activity_at": int(row["activity_at"]),
                    "acknowledged_activity_at": int(row["acknowledged_activity_at"]),
                    "unread": int(row["activity_at"])
                    > int(row["acknowledged_activity_at"]),
                }
                for row in rows
            ]

        workspace_summaries: dict[str, dict[str, Any]] = {}
        for row in terminal_rows:
            current_workspace_id = str(row["workspace_id"])
            summary = workspace_summaries.setdefault(
                current_workspace_id,
                {
                    "workspace_id": current_workspace_id,
                    "terminal_count": 0,
                    "unread_terminal_count": 0,
                    "unread_count": 0,
                    "latest_unread_terminal_id": None,
                },
            )
            summary["terminal_count"] += 1
            if row["unread"]:
                summary["unread_terminal_count"] += 1
                summary["unread_count"] += 1
                if summary["latest_unread_terminal_id"] is None:
                    summary["latest_unread_terminal_id"] = row["terminal_id"]
        workspace_rows = list(workspace_summaries.values())
        unread_rows = [row for row in terminal_rows if row["unread"]]
        return {
            "terminals": terminal_rows,
            "workspaces": workspace_rows,
            "unread_count": len(unread_rows),
            "latest_unread_terminal_id": (
                unread_rows[0]["terminal_id"] if unread_rows else None
            ),
        }

    def acknowledge_terminal_activity(
        self,
        terminal_id: str,
        device_id: str,
        observed_activity_at: int,
    ) -> dict[str, Any]:
        """Advance one device only to the exact cached revision it observed."""

        normalized_observed_activity_at = self._terminal_activity_revision(
            observed_activity_at
        )
        assert normalized_observed_activity_at is not None
        safe_device_id = str(device_id).strip()
        if not safe_device_id:
            raise ValueError("Terminal activity device identity is required")
        now = utc_now()
        with self.connect() as db:
            self._cleanup_terminal_activity_reads(db)
            terminal = db.execute(
                "SELECT id, workspace_id, role, activity_at FROM terminals WHERE id = ?",
                (str(terminal_id),),
            ).fetchone()
            if terminal is None or str(terminal["role"]) != "shell":
                raise KeyError(f"Unknown shell Terminal: {terminal_id}")
            current = terminal["activity_at"]
            if current is None:
                raise KeyError(f"Terminal activity is unavailable: {terminal_id}")
            current_revision = int(current)
            if normalized_observed_activity_at > current_revision:
                raise ValueError("Terminal activity revision is newer than the server cache")
            db.execute(
                """
                INSERT INTO terminal_activity_reads(
                    terminal_id, device_id, acknowledged_activity_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(terminal_id, device_id) DO UPDATE SET
                    acknowledged_activity_at = MAX(
                        terminal_activity_reads.acknowledged_activity_at,
                        excluded.acknowledged_activity_at
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    str(terminal_id),
                    safe_device_id,
                    normalized_observed_activity_at,
                    now,
                    now,
                ),
            )
            acknowledged = int(
                db.execute(
                    """
                    SELECT acknowledged_activity_at FROM terminal_activity_reads
                    WHERE terminal_id = ? AND device_id = ?
                    """,
                    (str(terminal_id), safe_device_id),
                ).fetchone()["acknowledged_activity_at"]
            )
        return {
            "terminal_id": str(terminal_id),
            "workspace_id": str(terminal["workspace_id"]),
            "activity_at": current_revision,
            "acknowledged_activity_at": acknowledged,
            "unread": current_revision > acknowledged,
        }

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

    def claim_file_run(
        self, payload: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """Atomically create, replay, or reject a Workspace File Run claim."""

        required = {
            "id",
            "workspace_id",
            "idempotency_key",
            "relative_path",
            "source_digest",
            "runner_id",
            "runner_version",
            "argv",
        }
        missing = required - payload.keys()
        if missing:
            raise ValueError(f"Missing File Run fields: {', '.join(sorted(missing))}")
        values = {
            "id": str(payload["id"]),
            "workspace_id": str(payload["workspace_id"]),
            "idempotency_key": str(payload["idempotency_key"]),
            "relative_path": str(payload["relative_path"]),
            "source_digest": str(payload["source_digest"]),
            "runner_id": str(payload["runner_id"]),
            "runner_version": int(payload["runner_version"]),
            "argv_json": json.dumps(
                [str(value) for value in payload["argv"]],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                """
                SELECT * FROM file_runs
                WHERE workspace_id = ? AND idempotency_key = ?
                """,
                (values["workspace_id"], values["idempotency_key"]),
            ).fetchone()
            if existing is not None:
                row = dict(existing)
                compared = (
                    "relative_path",
                    "source_digest",
                    "runner_id",
                    "runner_version",
                    "argv_json",
                )
                if any(row[key] != values[key] for key in compared):
                    raise ValueError(
                        "File Run idempotency key was reused with a different payload"
                    )
                row["argv"] = json.loads(str(row["argv_json"]))
                return "idempotent", row

            active = db.execute(
                """
                SELECT * FROM file_runs
                WHERE workspace_id = ? AND state IN ('preparing', 'running')
                """,
                (values["workspace_id"],),
            ).fetchone()
            if active is not None:
                row = dict(active)
                row["argv"] = json.loads(str(row["argv_json"]))
                return "occupied", row

            now = utc_now()
            db.execute(
                """
                INSERT INTO file_runs(
                    id, workspace_id, idempotency_key, relative_path,
                    source_digest, runner_id, runner_version, argv_json,
                    state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'preparing', ?)
                """,
                (
                    values["id"],
                    values["workspace_id"],
                    values["idempotency_key"],
                    values["relative_path"],
                    values["source_digest"],
                    values["runner_id"],
                    values["runner_version"],
                    values["argv_json"],
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM file_runs WHERE id = ?", (values["id"],)
            ).fetchone()
            result = dict(row)
            result["argv"] = json.loads(str(result["argv_json"]))
            return "created", result

    @staticmethod
    def _file_run_select() -> str:
        return """
            SELECT file_runs.*, workspaces.display_name AS workspace_name,
                   workspaces.backend_kind AS workspace_backend_kind,
                   workspaces.workspace_kind AS workspace_kind
            FROM file_runs
            JOIN workspaces ON workspaces.id = file_runs.workspace_id
        """

    @staticmethod
    def _decode_file_run(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["argv"] = json.loads(str(result["argv_json"]))
        return result

    def get_file_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                f"{self._file_run_select()} WHERE file_runs.id = ?", (run_id,)
            ).fetchone()
            return self._decode_file_run(row)

    def get_file_run_by_idempotency(
        self, workspace_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                f"{self._file_run_select()} "
                "WHERE file_runs.workspace_id = ? "
                "AND file_runs.idempotency_key = ?",
                (workspace_id, idempotency_key),
            ).fetchone()
            return self._decode_file_run(row)

    def get_active_file_run(self, workspace_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                f"{self._file_run_select()} "
                "WHERE file_runs.workspace_id = ? "
                "AND file_runs.state IN ('preparing', 'running')",
                (workspace_id,),
            ).fetchone()
            return self._decode_file_run(row)

    def get_latest_file_run(
        self, workspace_id: str, relative_path: str | None = None
    ) -> dict[str, Any] | None:
        where = "WHERE file_runs.workspace_id = ?"
        parameters: list[Any] = [workspace_id]
        if relative_path is not None:
            where += " AND file_runs.relative_path = ?"
            parameters.append(relative_path)
        with self.connect() as db:
            row = db.execute(
                f"{self._file_run_select()} {where} "
                "ORDER BY file_runs.created_at DESC LIMIT 1",
                tuple(parameters),
            ).fetchone()
            return self._decode_file_run(row)

    def list_active_file_runs(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                f"{self._file_run_select()} "
                "WHERE file_runs.state IN ('preparing', 'running') "
                "ORDER BY file_runs.created_at"
            ).fetchall()
            return [self._decode_file_run(row) or {} for row in rows]

    def set_file_run_terminal(self, run_id: str, terminal_id: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE file_runs SET terminal_id = ?
                WHERE id = ? AND state IN ('preparing', 'running')
                """,
                (terminal_id, run_id),
            )
            return cursor.rowcount == 1

    def transition_file_run(
        self,
        run_id: str,
        *,
        expected_states: set[str] | frozenset[str],
        state: str | object = ...,
        started_at: str | None | object = ...,
        stop_requested_at: str | None | object = ...,
        ended_at: str | None | object = ...,
        exit_code: int | None | object = ...,
        error_code: str | None | object = ...,
        error_detail: str | None | object = ...,
    ) -> bool:
        if not expected_states or not expected_states <= FILE_RUN_STATES:
            raise ValueError("Invalid expected File Run states")
        if state is not ... and state not in FILE_RUN_STATES:
            raise ValueError("Invalid File Run state")
        assignments: list[str] = []
        parameters: list[Any] = []
        for column, value in {
            "state": state,
            "started_at": started_at,
            "stop_requested_at": stop_requested_at,
            "ended_at": ended_at,
            "exit_code": exit_code,
            "error_code": error_code,
            "error_detail": error_detail,
        }.items():
            if value is ...:
                continue
            assignments.append(f"{column} = ?")
            parameters.append(value)
        if not assignments:
            return False
        assignments.append("lifecycle_revision = lifecycle_revision + 1")
        ordered_states = sorted(expected_states)
        parameters.append(run_id)
        parameters.extend(ordered_states)
        with self.connect() as db:
            previous = db.execute(
                "SELECT state FROM file_runs WHERE id = ?", (run_id,)
            ).fetchone()
            cursor = db.execute(
                f"UPDATE file_runs SET {', '.join(assignments)} "
                f"WHERE id = ? AND state IN ({', '.join('?' for _ in ordered_states)})",
                tuple(parameters),
            )
            if cursor.rowcount != 1:
                return False
            updated = db.execute(
                f"{self._file_run_select()} WHERE file_runs.id = ?", (run_id,)
            ).fetchone()
            if (
                previous is not None
                and str(previous["state"]) not in FILE_RUN_TERMINAL_STATES
                and updated is not None
                and str(updated["state"]) in FILE_RUN_TERMINAL_STATES
            ):
                self._insert_file_run_event(db, updated)
            return True

    @staticmethod
    def _run_duration_seconds(run: Mapping[str, Any]) -> int | None:
        started = run.get("started_at")
        ended = run.get("ended_at")
        if not started or not ended:
            return None
        try:
            seconds = (
                datetime.fromisoformat(str(ended))
                - datetime.fromisoformat(str(started))
            ).total_seconds()
        except ValueError:
            return None
        return max(0, int(seconds))

    def _insert_file_run_event(
        self, db: sqlite3.Connection, run: Mapping[str, Any]
    ) -> None:
        values = dict(run)
        state = str(values.get("state") or "")
        if state not in FILE_RUN_TERMINAL_STATES:
            return
        duration = self._run_duration_seconds(values)
        if state == "finished":
            completed = values.get("exit_code") == 0
            kind = "file_run.completed" if completed else "file_run.failed"
            notify = not completed or bool(duration is not None and duration >= 30)
        elif state == "failed":
            kind = "file_run.failed"
            notify = True
        elif state == "stopped":
            kind = "file_run.stopped"
            notify = True
        else:
            kind = "file_run.attention"
            notify = True
        occurred_at = str(values.get("ended_at") or values.get("created_at") or utc_now())
        db.execute(
            """
            INSERT INTO events(
                id, kind, subject_type, subject_id, subject_revision,
                primary_label, secondary_label, exit_code, duration_seconds,
                occurred_at, created_at, notify
            ) VALUES (?, ?, 'file_run', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subject_type, subject_id, subject_revision) DO NOTHING
            """,
            (
                uuid.uuid4().hex,
                kind,
                str(values["id"]),
                int(values.get("lifecycle_revision") or 0),
                self._event_label(values.get("relative_path"), "File"),
                self._event_label(values.get("workspace_name"), "Workspace"),
                values.get("exit_code"),
                duration,
                occurred_at,
                utc_now(),
                1 if notify else 0,
            ),
        )

    def create_remote_run(
        self,
        values: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Insert a Remote Run or return the row already claimed by this UUID."""

        columns = (
            "id",
            "source_kind",
            "archive_format",
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
        """Attach one Remote Workspace shell to one Remote Run idempotently."""

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
            if workspace["backend_kind"] != "remote" or (
                str(workspace["computer_id"] or "")
                != str(run["target_computer_id"])
            ):
                raise ValueError("Remote Run Workspace must use its target Remote")

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

    def list_active_remote_runs(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT * FROM remote_runs
                WHERE state = 'running'
                   OR (
                        state = 'preparing'
                    AND (phase IS NULL OR phase != 'waiting_upload')
                   )
                ORDER BY created_at
                """
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
                  AND source_kind = 'archive'
                  AND archive_format = 'zip'
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
        assignments.append("lifecycle_revision = lifecycle_revision + 1")

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
            previous = db.execute(
                "SELECT state FROM remote_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            cursor = db.execute(
                f"UPDATE remote_runs SET {', '.join(assignments)} WHERE {' AND '.join(where)}",
                tuple(parameters),
            )
            if cursor.rowcount != 1:
                return False
            updated = db.execute(
                """
                SELECT rr.*, computers.name AS target_name
                FROM remote_runs AS rr
                JOIN computers ON computers.id = rr.target_computer_id
                WHERE rr.id = ?
                """,
                (run_id,),
            ).fetchone()
            if (
                previous is not None
                and str(previous["state"]) not in REMOTE_RUN_TERMINAL_STATES
                and updated is not None
                and str(updated["state"]) in REMOTE_RUN_TERMINAL_STATES
            ):
                self._insert_remote_run_event(db, updated)
            return True

    @staticmethod
    def _event_label(value: Any, fallback: str) -> str:
        label = " ".join(str(value or "").split())[:160]
        return label or fallback

    def _insert_remote_run_event(
        self,
        db: sqlite3.Connection,
        run: Mapping[str, Any],
        *,
        historical: bool = False,
    ) -> None:
        values = dict(run)
        state = str(values.get("state") or "")
        if state not in REMOTE_RUN_TERMINAL_STATES:
            return
        if state == "finished":
            kind = (
                "remote_run.completed"
                if values.get("exit_code") == 0
                else "remote_run.failed"
            )
        elif state == "failed":
            kind = "remote_run.failed"
        elif state == "stopped":
            kind = "remote_run.stopped"
        else:
            kind = "remote_run.attention"
        occurred_at = str(
            values.get("ended_at") or values.get("created_at") or utc_now()
        )
        db.execute(
            """
            INSERT INTO events(
                id, kind, subject_type, subject_id, subject_revision,
                primary_label, secondary_label, exit_code,
                occurred_at, created_at, read_at, notify
            ) VALUES (?, ?, 'remote_run', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subject_type, subject_id, subject_revision) DO NOTHING
            """,
            (
                uuid.uuid4().hex,
                kind,
                str(values["id"]),
                int(values.get("lifecycle_revision") or 0),
                self._event_label(values.get("source_label"), "Remote Run"),
                self._event_label(values.get("target_name"), "Remote"),
                values.get("exit_code"),
                occurred_at,
                utc_now(),
                occurred_at if historical else None,
                0 if historical else 1,
            ),
        )

    @staticmethod
    def _activity_select(device_id: str | None = None) -> str:
        read_at = (
            "COALESCE(event_reads.read_at, events.read_at)"
            if device_id is not None
            else "events.read_at"
        )
        read_join = (
            "LEFT JOIN event_reads "
            "ON event_reads.event_id = events.id AND event_reads.device_id = ?"
            if device_id is not None
            else ""
        )
        return f"""
            SELECT
                events.sequence,
                events.id,
                events.kind,
                events.subject_type,
                events.subject_id,
                events.subject_revision,
                events.primary_label,
                events.secondary_label,
                events.exit_code,
                events.duration_seconds,
                events.occurred_at,
                events.created_at,
                {read_at} AS read_at,
                events.notify,
                CASE
                    WHEN events.subject_type = 'remote_run'
                         AND remote_runs.id IS NOT NULL THEN 1
                    WHEN events.subject_type = 'file_run'
                         AND file_runs.id IS NOT NULL
                         AND file_workspaces.id IS NOT NULL THEN 1
                    ELSE 0
                END AS subject_exists,
                COALESCE(
                    remote_runs.source_label,
                    file_runs.relative_path,
                    events.primary_label
                )
                    AS current_primary_label,
                COALESCE(
                    computers.name,
                    file_workspaces.display_name,
                    events.secondary_label
                ) AS current_secondary_label,
                file_runs.workspace_id AS current_workspace_id,
                file_runs.relative_path AS current_relative_path,
                CASE
                    WHEN file_terminals.role = 'file_run'
                         AND file_terminals.managed_run_id = file_runs.id
                    THEN file_terminals.id
                    ELSE NULL
                END AS current_terminal_id
            FROM events
            {read_join}
            LEFT JOIN remote_runs
              ON events.subject_type = 'remote_run'
             AND remote_runs.id = events.subject_id
            LEFT JOIN computers
              ON computers.id = remote_runs.target_computer_id
            LEFT JOIN file_runs
              ON events.subject_type = 'file_run'
             AND file_runs.id = events.subject_id
            LEFT JOIN workspaces AS file_workspaces
              ON file_workspaces.id = file_runs.workspace_id
            LEFT JOIN terminals AS file_terminals
              ON file_terminals.id = file_runs.terminal_id
        """

    def list_activity_events(
        self,
        limit: int = 100,
        *,
        device_id: str | None = None,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        parameters: tuple[Any, ...] = (
            (device_id, safe_limit) if device_id is not None else (safe_limit,)
        )
        with self.connect() as db:
            rows = db.execute(
                f"{self._activity_select(device_id)} ORDER BY events.sequence DESC LIMIT ?",
                parameters,
            ).fetchall()
            return [dict(row) for row in rows]

    def get_activity_event(
        self,
        event_id: str,
        *,
        device_id: str | None = None,
    ) -> dict[str, Any] | None:
        parameters: tuple[Any, ...] = (
            (device_id, event_id) if device_id is not None else (event_id,)
        )
        with self.connect() as db:
            row = db.execute(
                f"{self._activity_select(device_id)} WHERE events.id = ?",
                parameters,
            ).fetchone()
            return dict(row) if row else None

    def count_unread_events(self, *, device_id: str | None = None) -> int:
        with self.connect() as db:
            if device_id is None:
                row = db.execute(
                    "SELECT COUNT(*) AS count FROM events WHERE read_at IS NULL"
                ).fetchone()
            else:
                row = db.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM events
                    LEFT JOIN event_reads
                      ON event_reads.event_id = events.id
                     AND event_reads.device_id = ?
                    WHERE COALESCE(event_reads.read_at, events.read_at) IS NULL
                    """,
                    (device_id,),
                ).fetchone()
            return int(row["count"] if row else 0)

    def mark_event_read(
        self,
        event_id: str,
        *,
        device_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self.connect() as db:
            if device_id is None:
                db.execute(
                    "UPDATE events SET read_at = COALESCE(read_at, ?) WHERE id = ?",
                    (utc_now(), event_id),
                )
            else:
                db.execute(
                    """
                    INSERT INTO event_reads(event_id, device_id, read_at)
                    SELECT id, ?, ? FROM events WHERE id = ?
                    ON CONFLICT(event_id, device_id) DO NOTHING
                    """,
                    (device_id, utc_now(), event_id),
                )
        return self.get_activity_event(event_id, device_id=device_id)

    def mark_all_events_read(self, *, device_id: str | None = None) -> int:
        with self.connect() as db:
            if device_id is None:
                cursor = db.execute(
                    "UPDATE events SET read_at = ? WHERE read_at IS NULL",
                    (utc_now(),),
                )
            else:
                cursor = db.execute(
                    """
                    INSERT INTO event_reads(event_id, device_id, read_at)
                    SELECT events.id, ?, ?
                    FROM events
                    WHERE events.read_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM event_reads
                          WHERE event_reads.event_id = events.id
                            AND event_reads.device_id = ?
                      )
                    """,
                    (device_id, utc_now(), device_id),
                )
            return cursor.rowcount

    def claim_event_notifications(
        self,
        device_id: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 50))
        now = utc_now()
        claimed_ids: list[str] = []
        with self.connect() as db:
            device = db.execute(
                "SELECT * FROM notification_devices WHERE id = ?",
                (device_id,),
            ).fetchone()
            if device is None:
                latest = db.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM events"
                ).fetchone()
                db.execute(
                    """
                    INSERT INTO notification_devices(
                        id, start_sequence, created_at, last_seen_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (device_id, int(latest["sequence"]), now, now),
                )
                return []
            db.execute(
                "UPDATE notification_devices SET last_seen_at = ? WHERE id = ?",
                (now, device_id),
            )
            candidates = db.execute(
                """
                SELECT events.id
                FROM events
                WHERE events.notify = 1
                  AND events.sequence > ?
                  AND NOT EXISTS (
                      SELECT 1 FROM event_notification_claims AS claims
                      WHERE claims.event_id = events.id
                        AND claims.device_id = ?
                  )
                ORDER BY events.sequence
                LIMIT ?
                """,
                (int(device["start_sequence"]), device_id, safe_limit),
            ).fetchall()
            for candidate in candidates:
                event_id = str(candidate["id"])
                cursor = db.execute(
                    """
                    INSERT INTO event_notification_claims(
                        event_id, device_id, claimed_at
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(event_id, device_id) DO NOTHING
                    """,
                    (event_id, device_id, now),
                )
                if cursor.rowcount == 1:
                    claimed_ids.append(event_id)
            if not claimed_ids:
                return []
            placeholders = ", ".join("?" for _ in claimed_ids)
            rows = db.execute(
                f"{self._activity_select()} "
                f"WHERE events.id IN ({placeholders}) ORDER BY events.sequence",
                tuple(claimed_ids),
            ).fetchall()
            return [dict(row) for row in rows]

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
                    id, name, connection_method, auth_kind, ssh_alias, host, port,
                    username, identity_file,
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

    def create_node_pairing_code(
        self, *, code_hash: str, expires_at: str
    ) -> dict[str, Any]:
        pairing_id = uuid.uuid4().hex
        now = utc_now()
        with self.connect() as db:
            db.execute(
                "DELETE FROM node_pairing_codes WHERE expires_at < ? AND consumed_at IS NULL",
                (now,),
            )
            db.execute(
                """
                INSERT INTO node_pairing_codes(id, code_hash, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (pairing_id, code_hash, now, expires_at),
            )
            row = db.execute(
                "SELECT * FROM node_pairing_codes WHERE id = ?", (pairing_id,)
            ).fetchone()
            return dict(row)

    def submit_node_enrollment(
        self,
        *,
        code_hash: str,
        name: str,
        public_key: str,
        fingerprint: str,
        protocol_version: int,
        polling_secret_hash: str,
    ) -> dict[str, Any] | None:
        now = utc_now()
        enrollment_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pairing = db.execute(
                """
                SELECT * FROM node_pairing_codes
                WHERE code_hash = ? AND consumed_at IS NULL AND expires_at >= ?
                """,
                (code_hash, now),
            ).fetchone()
            if pairing is None:
                return None
            existing = db.execute(
                "SELECT id FROM computers WHERE node_public_key = ? LIMIT 1",
                (public_key,),
            ).fetchone()
            if existing is not None:
                return None
            consumed = db.execute(
                """
                UPDATE node_pairing_codes SET consumed_at = ?
                WHERE id = ? AND consumed_at IS NULL
                """,
                (now, pairing["id"]),
            )
            if consumed.rowcount != 1:
                return None
            db.execute(
                """
                INSERT INTO node_enrollments(
                    id, pairing_code_id, name, public_key, fingerprint,
                    protocol_version, polling_secret_hash, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    enrollment_id,
                    pairing["id"],
                    normalize_computer_name(name),
                    public_key,
                    fingerprint,
                    protocol_version,
                    polling_secret_hash,
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM node_enrollments WHERE id = ?", (enrollment_id,)
            ).fetchone()
            return dict(row)

    def get_node_pairing(self, pairing_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """
                SELECT pc.*, ne.id AS enrollment_id, ne.name AS node_name,
                       ne.public_key, ne.fingerprint, ne.protocol_version,
                       ne.status, ne.computer_id, ne.decided_at
                FROM node_pairing_codes AS pc
                LEFT JOIN node_enrollments AS ne ON ne.pairing_code_id = pc.id
                WHERE pc.id = ?
                """,
                (pairing_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_node_enrollment(
        self, enrollment_id: str, *, polling_secret_hash: str | None = None
    ) -> dict[str, Any] | None:
        with self.connect() as db:
            if polling_secret_hash is None:
                row = db.execute(
                    "SELECT * FROM node_enrollments WHERE id = ?", (enrollment_id,)
                ).fetchone()
            else:
                row = db.execute(
                    """
                    SELECT * FROM node_enrollments
                    WHERE id = ? AND polling_secret_hash = ?
                    """,
                    (enrollment_id, polling_secret_hash),
                ).fetchone()
            return dict(row) if row else None

    def approve_node_enrollment(self, enrollment_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            enrollment = db.execute(
                "SELECT * FROM node_enrollments WHERE id = ?", (enrollment_id,)
            ).fetchone()
            if enrollment is None:
                raise KeyError(f"Unknown Node enrollment: {enrollment_id}")
            if enrollment["status"] != "pending":
                if enrollment["status"] == "approved" and enrollment["computer_id"]:
                    row = db.execute(
                        "SELECT * FROM computers WHERE id = ?",
                        (enrollment["computer_id"],),
                    ).fetchone()
                    if row is not None:
                        return dict(row)
                raise RuntimeError("Node enrollment is no longer pending")
            existing = db.execute(
                "SELECT id FROM computers WHERE node_public_key = ? LIMIT 1",
                (enrollment["public_key"],),
            ).fetchone()
            if existing is not None:
                raise RuntimeError("This Node identity is already registered")
            computer_id = uuid.uuid4().hex
            db.execute(
                """
                INSERT INTO computers(
                    id, name, connection_method, auth_kind, ssh_alias, host, port,
                    username, identity_file, host_key_type, host_key_data,
                    host_fingerprint, created_at, node_public_key,
                    node_fingerprint, node_protocol_version,
                    node_capabilities_json
                ) VALUES (?, ?, 'node', 'node', '', '', 0, '', '', '', '', '', ?, ?, ?, ?, '[]')
                """,
                (
                    computer_id,
                    enrollment["name"],
                    now,
                    enrollment["public_key"],
                    enrollment["fingerprint"],
                    enrollment["protocol_version"],
                ),
            )
            db.execute(
                """
                UPDATE node_enrollments
                SET status = 'approved', computer_id = ?, decided_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (computer_id, now, enrollment_id),
            )
            row = db.execute(
                "SELECT * FROM computers WHERE id = ?", (computer_id,)
            ).fetchone()
            return dict(row)

    def reject_node_enrollment(self, enrollment_id: str) -> None:
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE node_enrollments
                SET status = 'rejected', decided_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (utc_now(), enrollment_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Node enrollment is no longer pending")

    def update_node_connection(
        self,
        computer_id: str,
        *,
        protocol_version: int,
        capabilities: tuple[str, ...],
    ) -> None:
        now = utc_now()
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE computers
                SET node_protocol_version = ?, node_capabilities_json = ?,
                    last_seen_at = ?, last_connected_at = ?, last_error = NULL
                WHERE id = ? AND connection_method = 'node' AND node_revoked_at IS NULL
                """,
                (
                    protocol_version,
                    json.dumps(capabilities, separators=(",", ":")),
                    now,
                    now,
                    computer_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown or revoked Node: {computer_id}")

    def touch_node(self, computer_id: str) -> None:
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE computers SET last_seen_at = ?
                WHERE id = ? AND connection_method = 'node' AND node_revoked_at IS NULL
                """,
                (utc_now(), computer_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown or revoked Node: {computer_id}")

    def revoke_node(self, computer_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as db:
            cursor = db.execute(
                """
                UPDATE computers
                SET node_revoked_at = ?, last_error = 'node_revoked'
                WHERE id = ? AND connection_method = 'node' AND node_revoked_at IS NULL
                """,
                (now, computer_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown or already revoked Node: {computer_id}")
            row = db.execute(
                "SELECT * FROM computers WHERE id = ?", (computer_id,)
            ).fetchone()
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
            db.execute("DELETE FROM roots WHERE path = ?", (f"node://{computer_id}",))
            db.execute("DELETE FROM computers WHERE id = ?", (computer_id,))
            return workspace_ids
