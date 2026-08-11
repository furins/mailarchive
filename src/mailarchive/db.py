"""SQLite connection and explicit M0 schema migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from mailarchive.models import AccountConfig

Migration = Callable[[sqlite3.Connection], None]


def _migration_1(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL CHECK (kind IN ('imap', 'gmail', 'pop3')),
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            remote_retention_days INTEGER
                CHECK (remote_retention_days IS NULL OR remote_retention_days >= 0),
            remote_deletion_enabled INTEGER NOT NULL DEFAULT 0 CHECK (remote_deletion_enabled = 0),
            required_verified_backups INTEGER NOT NULL CHECK (required_verified_backups >= 0),
            config_ref TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY,
            timestamp TEXT NOT NULL,
            actor TEXT NOT NULL,
            event_type TEXT NOT NULL,
            account_id INTEGER REFERENCES accounts(id),
            result TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}'
        );
        """
    )


MIGRATIONS: tuple[tuple[int, Migration], ...] = ((1, _migration_1),)


def utc_now() -> str:
    """Return a timezone-aware UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    """Connect with the required SQLite safety settings enabled."""
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize(path: Path, accounts: tuple[AccountConfig, ...] = ()) -> None:
    """Apply migrations and reconcile M0 account metadata idempotently."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        for version, migration in MIGRATIONS:
            if version not in applied:
                migration(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, utc_now()),
                )
        for account in accounts:
            now = utc_now()
            connection.execute(
                """
                INSERT INTO accounts (
                    name, kind, enabled, remote_retention_days, remote_deletion_enabled,
                    required_verified_backups, config_ref, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    kind = excluded.kind,
                    enabled = excluded.enabled,
                    remote_retention_days = excluded.remote_retention_days,
                    remote_deletion_enabled = 0,
                    required_verified_backups = excluded.required_verified_backups,
                    config_ref = excluded.config_ref,
                    updated_at = excluded.updated_at
                """,
                (
                    account.name,
                    account.kind,
                    account.enabled,
                    account.remote_retention_days,
                    account.required_verified_backups,
                    account.config_ref,
                    now,
                    now,
                ),
            )


def insert_audit_event(
    connection: sqlite3.Connection,
    *,
    actor: str,
    event_type: str,
    result: str,
    account_id: int | None = None,
    details_json: str = "{}",
) -> int:
    """Append an audit event and return its stable identifier."""
    cursor = connection.execute(
        """
        INSERT INTO audit_events(timestamp, actor, event_type, account_id, result, details_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (utc_now(), actor, event_type, account_id, result, details_json),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite did not return an audit event identifier")
    return cursor.lastrowid
