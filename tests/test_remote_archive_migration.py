from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import termroom.db as db_module
from termroom.db import StateStore


def _create_legacy_database(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE roots (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE workspaces (
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
            CREATE TABLE computers (
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
                last_error TEXT,
                run_base_dir TEXT
            );
            CREATE TABLE terminals (
                id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                tmux_window TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_opened_at TEXT NOT NULL,
                last_output_at TEXT,
                UNIQUE(workspace_id, tmux_window)
            );
            CREATE TABLE remote_runs (
                id TEXT PRIMARY KEY,
                source_kind TEXT NOT NULL
                    CHECK(source_kind IN ('workspace', 'git', 'zip')),
                source_workspace_id TEXT REFERENCES workspaces(id) ON DELETE SET NULL,
                source_path TEXT,
                source_label TEXT NOT NULL,
                source_url TEXT,
                source_options_json TEXT NOT NULL DEFAULT '{}',
                source_revision TEXT,
                source_size INTEGER,
                target_computer_id TEXT NOT NULL REFERENCES computers(id),
                command TEXT NOT NULL,
                run_base TEXT NOT NULL,
                workspace_id TEXT REFERENCES workspaces(id) ON DELETE SET NULL,
                state TEXT NOT NULL,
                phase TEXT,
                exit_code INTEGER,
                error_code TEXT,
                error_detail TEXT,
                lifecycle_revision INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                stop_requested_at TEXT,
                ended_at TEXT,
                expires_at TEXT
            );
            CREATE TABLE events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                subject_revision INTEGER NOT NULL,
                primary_label TEXT NOT NULL,
                secondary_label TEXT NOT NULL,
                exit_code INTEGER,
                occurred_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                read_at TEXT,
                notify INTEGER NOT NULL DEFAULT 1 CHECK(notify IN (0, 1)),
                UNIQUE(subject_type, subject_id, subject_revision)
            );
            CREATE TABLE notification_devices (
                id TEXT PRIMARY KEY,
                start_sequence INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE TABLE event_notification_claims (
                event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                device_id TEXT NOT NULL REFERENCES notification_devices(id) ON DELETE CASCADE,
                claimed_at TEXT NOT NULL,
                PRIMARY KEY(event_id, device_id)
            );

            INSERT INTO roots VALUES
                ('root-local', '/srv/local', '2026-08-01T00:00:00+00:00'),
                ('root-remote', 'ssh://computer-1', '2026-08-01T00:00:00+00:00');
            INSERT INTO computers VALUES (
                'computer-1', 'GPU Remote', 'ssh', 'key', '', 'gpu.example.test', 2222,
                'runner', '/keys/gpu', 'ssh-ed25519', 'AAAATEST', 'SHA256:test',
                '2026-08-01T00:00:00+00:00', '2026-08-02T00:00:00+00:00', NULL,
                '/scratch/termroom-runs'
            );
            INSERT INTO workspaces VALUES
                ('workspace-local', 'root-local', 'project', 'Local project',
                 'termroom-local', '2026-08-03T00:00:00+00:00', 'files', 'local',
                 NULL, '/srv/local/project', 'workspace'),
                ('workspace-remote', 'root-remote', 'project', 'Remote project',
                 'termroom-remote', '2026-08-03T00:00:00+00:00', 'terminal', 'ssh',
                 'computer-1', '/srv/project', 'workspace'),
                ('workspace-run', 'root-remote', 'run-work', 'Archive result',
                 'termroom-run', '2026-08-03T00:00:00+00:00', 'files', 'ssh',
                 'computer-1', '/scratch/termroom-runs/run-archive/work', 'remote_run');
            INSERT INTO terminals VALUES
                ('terminal-local', 'workspace-local', 'Shell', '0',
                 '2026-08-03T00:00:00+00:00', '2026-08-03T00:00:00+00:00', NULL),
                ('terminal-remote', 'workspace-remote', 'Shell', '0',
                 '2026-08-03T00:00:00+00:00', '2026-08-03T00:00:00+00:00', NULL),
                ('terminal-run', 'workspace-run', 'Run', 'run',
                 '2026-08-03T00:00:00+00:00', '2026-08-03T00:00:00+00:00', NULL);
            INSERT INTO remote_runs VALUES
                ('run-workspace', 'workspace', 'workspace-local', '.', 'Local project', NULL,
                 '{"policy":1}', NULL, 12, 'computer-1', 'printf workspace',
                 '/scratch/termroom-runs', NULL, 'running', NULL, NULL, NULL, NULL, 2,
                 '2026-08-04T00:00:00+00:00', '2026-08-04T00:01:00+00:00', NULL, NULL, NULL),
                ('run-git', 'git', NULL, NULL, 'example/project',
                 'https://example.test/project.git', '{"policy":1}', 'abc123', NULL,
                 'computer-1', 'printf git', '/scratch/termroom-runs', NULL, 'preparing',
                 'cloning', NULL, NULL, NULL, 1, '2026-08-04T01:00:00+00:00', NULL,
                 NULL, NULL, NULL),
                ('run-archive', 'zip', NULL, NULL, 'source.zip', NULL,
                 '{"archive_name":"source.zip","policy":1}', NULL, 34, 'computer-1',
                 'printf archive', '/scratch/termroom-runs', 'workspace-run', 'finished',
                 NULL, 0, NULL, NULL, 3, '2026-08-04T02:00:00+00:00',
                 '2026-08-04T02:01:00+00:00', NULL, '2026-08-04T02:02:00+00:00',
                 '2026-08-05T02:02:00+00:00');
            INSERT INTO events(
                id, kind, subject_type, subject_id, subject_revision,
                primary_label, secondary_label, exit_code, occurred_at,
                created_at, read_at, notify
            ) VALUES (
                'event-archive', 'remote_run.completed', 'remote_run', 'run-archive', 3,
                'source.zip', 'GPU Remote', 0, '2026-08-04T02:02:00+00:00',
                '2026-08-04T02:02:00+00:00', '2026-08-04T03:00:00+00:00', 0
            );
            INSERT INTO notification_devices VALUES (
                'device-1', 0, '2026-08-04T00:00:00+00:00', '2026-08-04T03:00:00+00:00'
            );
            INSERT INTO event_notification_claims VALUES (
                'event-archive', 'device-1', '2026-08-04T02:03:00+00:00'
            );
            """
        )


def _durable_snapshot(path: Path) -> dict[str, object]:
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return {
            "computers": [dict(row) for row in db.execute("SELECT * FROM computers ORDER BY id")],
            "workspaces": [dict(row) for row in db.execute("SELECT * FROM workspaces ORDER BY id")],
            "terminals": [dict(row) for row in db.execute("SELECT * FROM terminals ORDER BY id")],
            "runs": [dict(row) for row in db.execute("SELECT * FROM remote_runs ORDER BY id")],
            "events": [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id")],
            "devices": [
                dict(row) for row in db.execute("SELECT * FROM notification_devices ORDER BY id")
            ],
            "claims": [
                dict(row)
                for row in db.execute("SELECT * FROM event_notification_claims ORDER BY event_id")
            ],
            "computer_schema": db.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'computers'"
            ).fetchone()[0],
            "run_schema": db.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'remote_runs'"
            ).fetchone()[0],
        }


def test_remote_archive_migration_preserves_identity_and_is_idempotent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    _create_legacy_database(database)

    store = StateStore(database)
    store.initialize()
    first = _durable_snapshot(database)

    computer = first["computers"][0]  # type: ignore[index]
    assert "kind" not in computer
    assert computer["connection_method"] == "ssh"
    assert computer["host"] == "gpu.example.test"
    assert computer["run_base_dir"] == "/scratch/termroom-runs"

    workspaces = {row["id"]: row for row in first["workspaces"]}  # type: ignore[union-attr]
    assert workspaces["workspace-local"]["backend_kind"] == "local"
    assert workspaces["workspace-remote"]["backend_kind"] == "remote"
    assert workspaces["workspace-run"]["backend_kind"] == "remote"
    assert workspaces["workspace-remote"]["tmux_session"] == "termroom-remote"
    assert workspaces["workspace-remote"]["canonical_path"] == "/srv/project"

    runs = {row["id"]: row for row in first["runs"]}  # type: ignore[union-attr]
    assert runs["run-workspace"]["source_kind"] == "workspace"
    assert runs["run-workspace"]["archive_format"] is None
    assert runs["run-git"]["source_kind"] == "git"
    assert runs["run-git"]["archive_format"] is None
    assert runs["run-archive"]["source_kind"] == "archive"
    assert runs["run-archive"]["archive_format"] == "zip"
    assert runs["run-archive"]["workspace_id"] == "workspace-run"
    assert runs["run-archive"]["lifecycle_revision"] == 3

    assert [row["id"] for row in first["events"]] == ["event-archive"]  # type: ignore[index]
    assert first["claims"] == [  # type: ignore[comparison-overlap]
        {
            "event_id": "event-archive",
            "device_id": "device-1",
            "claimed_at": "2026-08-04T02:03:00+00:00",
        }
    ]
    assert "connection_method" in str(first["computer_schema"])
    assert "source_kind IN ('workspace', 'git', 'archive')" in str(first["run_schema"])

    store.initialize()
    assert _durable_snapshot(database) == first
    with store.connect() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_remote_archive_migration_failure_rolls_back_all_model_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.sqlite3"
    _create_legacy_database(database)
    original_builder = db_module._remote_runs_table_sql

    def broken_remote_runs_table_sql(
        table: str = "remote_runs", *, if_not_exists: bool = False
    ) -> str:
        if if_not_exists:
            return original_builder(table, if_not_exists=True)
        return "CREATE TABLE remote_runs (id TEXT PRIMARY KEY);"

    with monkeypatch.context() as scoped:
        scoped.setattr(db_module, "_remote_runs_table_sql", broken_remote_runs_table_sql)
        with pytest.raises(sqlite3.OperationalError):
            StateStore(database).initialize()

    with sqlite3.connect(database) as db:
        computer_columns = {
            row[1] for row in db.execute("PRAGMA table_info(computers)").fetchall()
        }
        assert "kind" in computer_columns
        assert "connection_method" not in computer_columns
        assert db.execute(
            "SELECT backend_kind FROM workspaces WHERE id = 'workspace-remote'"
        ).fetchone()[0] == "ssh"
        assert db.execute(
            "SELECT source_kind FROM remote_runs WHERE id = 'run-archive'"
        ).fetchone()[0] == "zip"
        assert db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%legacy_remote_model'"
        ).fetchone()[0] == 0
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []

    StateStore(database).initialize()
    assert StateStore(database).get_remote_run("run-archive")["source_kind"] == "archive"  # type: ignore[index]
