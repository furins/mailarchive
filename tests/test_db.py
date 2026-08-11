from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import mailarchive.db as database
from mailarchive.config import load_config
from mailarchive.db import connect, initialize, insert_audit_event


def test_database_initializes_idempotently(config_file: Path) -> None:
    config = load_config(config_file)
    initialize(config.database.path, config.accounts)
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


def test_foreign_keys_are_enabled(config_file: Path) -> None:
    config = load_config(config_file)
    initialize(config.database.path)
    with connect(config.database.path) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_audit_insert_works(config_file: Path) -> None:
    config = load_config(config_file)
    initialize(config.database.path)
    with connect(config.database.path) as connection:
        event_id = insert_audit_event(
            connection, actor="pytest", event_type="database.initialized", result="success"
        )
        assert event_id == 1
        event_row = connection.execute("SELECT event_type FROM audit_events").fetchone()
        assert event_row[0] == "database.initialized"


def test_failed_migration_rolls_back_and_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "state" / "mailarchive.sqlite3"

    def failing_migration(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE migration_probe (id INTEGER PRIMARY KEY)")
        connection.execute("THIS IS INVALID SQL")

    monkeypatch.setattr(database, "MIGRATIONS", ((1, failing_migration),))
    with pytest.raises(sqlite3.OperationalError):
        initialize(database_path)

    with connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'migration_probe'"
        ).fetchone()[0] == 0
        migration_row = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 1"
        ).fetchone()
        assert migration_row[0] == 0

    def corrected_migration(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE migration_probe (id INTEGER PRIMARY KEY)")

    monkeypatch.setattr(database, "MIGRATIONS", ((1, corrected_migration),))
    initialize(database_path)
    with connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'migration_probe'"
        ).fetchone()[0] == 1
        migration_row = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 1"
        ).fetchone()
        assert migration_row[0] == 1


def test_removed_account_is_disabled(config_file: Path) -> None:
    config = load_config(config_file)
    initial_accounts = config.accounts + (
        config.accounts[0].__class__(
            name="removed",
            kind="imap",
            enabled=True,
            remote_retention_days=365,
            remote_deletion_enabled=False,
            required_verified_backups=2,
            config_ref="env:REMOVED_ACCOUNT",
        ),
    )
    initialize(config.database.path, initial_accounts)
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as connection:
        removed_row = connection.execute(
            "SELECT enabled FROM accounts WHERE name = 'removed'"
        ).fetchone()
        assert removed_row[0] == 0
