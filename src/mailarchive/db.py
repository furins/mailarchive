"""SQLite connection and explicit M0 schema migrations."""
# ruff: noqa: E501

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from mailarchive.models import AccountConfig, CanonicalMessage

Migration = Callable[[sqlite3.Connection], None]


def _migration_1(connection: sqlite3.Connection) -> None:
    connection.execute(
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
        )
        """
    )
    _create_audit_events(connection)


def _create_audit_events(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY,
            timestamp TEXT NOT NULL,
            actor TEXT NOT NULL,
            event_type TEXT NOT NULL,
            account_id INTEGER REFERENCES accounts(id),
            result TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )


def _migration_2(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE canonical_messages (
            id TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            sha256 TEXT NOT NULL UNIQUE CHECK (length(sha256) = 64),
            local_path TEXT NOT NULL UNIQUE,
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
            message_id_header TEXT,
            message_date TEXT,
            downloaded_at TEXT NOT NULL,
            archived_at TEXT NOT NULL,
            integrity_status TEXT NOT NULL CHECK (integrity_status IN ('verified', 'failed')),
            integrity_verified_at TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "ALTER TABLE audit_events ADD COLUMN canonical_message_id "
        "TEXT REFERENCES canonical_messages(id)"
    )


def _migration_3(connection: sqlite3.Connection) -> None:
    """Scope canonical byte objects to accounts without losing M1 audit history."""
    connection.execute(
        """
        CREATE TABLE canonical_messages_m1_replacement (
            id TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
            local_path TEXT NOT NULL UNIQUE,
            size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
            message_id_header TEXT,
            message_date TEXT,
            downloaded_at TEXT NOT NULL,
            archived_at TEXT NOT NULL,
            integrity_status TEXT NOT NULL CHECK (integrity_status IN ('verified', 'failed')),
            integrity_verified_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(account_id, sha256)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO canonical_messages_m1_replacement(
            id, account_id, sha256, local_path, size_bytes, message_id_header, message_date,
            downloaded_at, archived_at, integrity_status, integrity_verified_at, created_at
        )
        SELECT CAST(account_id AS TEXT) || ':' || sha256, account_id, sha256, local_path,
               size_bytes, message_id_header, message_date, downloaded_at, archived_at,
               integrity_status, integrity_verified_at, created_at
        FROM canonical_messages
        """
    )
    connection.execute(
        """
        CREATE TABLE audit_events_m1_replacement (
            id INTEGER PRIMARY KEY,
            timestamp TEXT NOT NULL,
            actor TEXT NOT NULL,
            event_type TEXT NOT NULL,
            account_id INTEGER REFERENCES accounts(id),
            canonical_message_id TEXT REFERENCES canonical_messages_m1_replacement(id),
            result TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO audit_events_m1_replacement(
            id, timestamp, actor, event_type, account_id, canonical_message_id, result, details_json
        )
        SELECT audit_events.id, audit_events.timestamp, audit_events.actor, audit_events.event_type,
               audit_events.account_id,
               CASE WHEN audit_events.canonical_message_id IS NULL THEN NULL
                    ELSE CAST(canonical_messages.account_id AS TEXT) || ':' ||
                         canonical_messages.sha256
               END,
               audit_events.result, audit_events.details_json
        FROM audit_events
        LEFT JOIN canonical_messages ON canonical_messages.id = audit_events.canonical_message_id
        """
    )
    connection.execute("DROP TABLE audit_events")
    connection.execute("DROP TABLE canonical_messages")
    connection.execute("ALTER TABLE canonical_messages_m1_replacement RENAME TO canonical_messages")
    connection.execute("ALTER TABLE audit_events_m1_replacement RENAME TO audit_events")


def _migration_4(connection: sqlite3.Connection) -> None:
    """Add M3 remote identity facts; UIDVALIDITY is part of every identity."""
    connection.execute(
        """
        CREATE TABLE remote_messages (
            id TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            remote_folder TEXT NOT NULL,
            uidvalidity INTEGER NOT NULL CHECK (uidvalidity > 0),
            remote_uid INTEGER NOT NULL CHECK (remote_uid > 0),
            message_id_header TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            remote_present INTEGER NOT NULL CHECK (remote_present IN (0, 1)),
            identity_confidence TEXT NOT NULL CHECK (identity_confidence IN ('proven')),
            UNIQUE(account_id, remote_folder, uidvalidity, remote_uid)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE remote_canonical_links (
            remote_message_id TEXT PRIMARY KEY REFERENCES remote_messages(id),
            canonical_message_id TEXT NOT NULL REFERENCES canonical_messages(id),
            link_reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(remote_message_id, canonical_message_id)
        )
        """
    )


def _migration_5(connection: sqlite3.Connection) -> None:
    """Persist bounded, secret-free M4 watcher operational health."""
    connection.execute(
        """
        CREATE TABLE fast_path_health (
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            remote_folder TEXT NOT NULL,
            mode TEXT NOT NULL CHECK (
                mode IN ('idle', 'poll', 'reconnecting', 'degraded', 'stopped')
            ),
            watcher_started_at TEXT,
            last_heartbeat_at TEXT,
            last_event_at TEXT,
            last_sync_started_at TEXT,
            last_sync_succeeded_at TEXT,
            last_index_succeeded_at TEXT,
            last_error_at TEXT,
            last_error_kind TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
            reconnect_count INTEGER NOT NULL DEFAULT 0 CHECK (reconnect_count >= 0),
            index_pending INTEGER NOT NULL DEFAULT 0 CHECK (index_pending IN (0, 1)),
            updated_at TEXT NOT NULL,
            PRIMARY KEY(account_id, remote_folder)
        )
        """
    )


def _migration_6(connection: sqlite3.Connection) -> None:
    """Generalize M3 identities for Gmail without changing M3 primary keys or links."""
    connection.execute(
        """
        CREATE TABLE remote_messages_v6 (
            id TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            provider_kind TEXT NOT NULL CHECK (provider_kind IN ('imap', 'gmail')),
            remote_folder TEXT,
            uidvalidity INTEGER,
            remote_uid INTEGER,
            provider_message_id TEXT,
            provider_thread_id TEXT,
            message_id_header TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            remote_present INTEGER NOT NULL CHECK (remote_present IN (0, 1)),
            identity_confidence TEXT NOT NULL CHECK (identity_confidence IN ('proven')),
            CHECK ((provider_kind='imap' AND remote_folder IS NOT NULL AND uidvalidity > 0
                   AND remote_uid > 0 AND provider_message_id IS NULL)
                OR (provider_kind='gmail' AND provider_message_id IS NOT NULL
                   AND remote_folder IS NULL AND uidvalidity IS NULL AND remote_uid IS NULL)),
            UNIQUE(id, account_id)
        )
        """
    )

    connection.execute("""
        INSERT INTO remote_messages_v6(
            id, account_id, provider_kind, remote_folder, uidvalidity, remote_uid,
            provider_message_id, provider_thread_id, message_id_header, first_seen_at,
            last_seen_at, remote_present, identity_confidence
        ) SELECT id, account_id, 'imap', remote_folder, uidvalidity, remote_uid,
                 NULL, NULL, message_id_header, first_seen_at, last_seen_at,
                 remote_present, identity_confidence FROM remote_messages
    """)
    connection.execute("""
        CREATE TABLE remote_canonical_links_v6 (
            remote_message_id TEXT PRIMARY KEY REFERENCES remote_messages_v6(id),
            canonical_message_id TEXT NOT NULL REFERENCES canonical_messages(id),
            link_reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(remote_message_id, canonical_message_id)
        )
    """)
    connection.execute("INSERT INTO remote_canonical_links_v6 SELECT * FROM remote_canonical_links")
    connection.execute("DROP TABLE remote_canonical_links")
    connection.execute("DROP TABLE remote_messages")
    connection.execute("ALTER TABLE remote_messages_v6 RENAME TO remote_messages")
    connection.execute("ALTER TABLE remote_canonical_links_v6 RENAME TO remote_canonical_links")
    connection.execute("""
        CREATE UNIQUE INDEX remote_messages_imap_identity
        ON remote_messages(account_id, remote_folder, uidvalidity, remote_uid)
        WHERE provider_kind='imap'
    """)
    connection.execute("""
        CREATE UNIQUE INDEX remote_messages_gmail_identity
        ON remote_messages(account_id, provider_message_id)
        WHERE provider_kind='gmail'
    """)
    connection.execute("""
        CREATE TABLE gmail_labels (
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            label_id TEXT NOT NULL,
            name TEXT NOT NULL,
            label_type TEXT NOT NULL CHECK (label_type IN ('system', 'user')),
            remote_present INTEGER NOT NULL CHECK (remote_present IN (0, 1)),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY(account_id, label_id)
        )
    """)
    connection.execute("""
        CREATE TABLE gmail_message_labels (
            remote_message_id TEXT NOT NULL,
            account_id INTEGER NOT NULL,
            label_id TEXT NOT NULL,
            PRIMARY KEY(remote_message_id, label_id),
            FOREIGN KEY(remote_message_id, account_id) REFERENCES remote_messages(id, account_id),
            FOREIGN KEY(account_id, label_id) REFERENCES gmail_labels(account_id, label_id)
        )
    """)
    connection.execute("""
        CREATE TABLE gmail_sync_state (
            account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
            history_id TEXT,
            full_sync_required INTEGER NOT NULL DEFAULT 1 CHECK (full_sync_required IN (0, 1)),
            last_sync_started_at TEXT,
            last_sync_succeeded_at TEXT,
            last_full_sync_succeeded_at TEXT,
            last_partial_sync_succeeded_at TEXT,
            last_error_at TEXT,
            last_error_kind TEXT,
            updated_at TEXT NOT NULL
        )
    """)


def _migration_7(connection: sqlite3.Connection) -> None:
    """Add POP3 UIDL identities without altering prior provider identities or links."""
    connection.execute(
        """
        CREATE TABLE remote_messages_v7 (
            id TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id),
            provider_kind TEXT NOT NULL CHECK (provider_kind IN ('imap', 'gmail', 'pop3')),
            remote_folder TEXT,
            uidvalidity INTEGER,
            remote_uid INTEGER,
            provider_message_id TEXT,
            provider_thread_id TEXT,
            message_id_header TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            remote_present INTEGER NOT NULL CHECK (remote_present IN (0, 1)),
            identity_confidence TEXT NOT NULL CHECK (identity_confidence IN ('proven')),
            CHECK ((provider_kind='imap' AND remote_folder IS NOT NULL AND uidvalidity > 0
                   AND remote_uid > 0 AND provider_message_id IS NULL)
                OR (provider_kind IN ('gmail', 'pop3') AND provider_message_id IS NOT NULL
                   AND remote_folder IS NULL AND uidvalidity IS NULL AND remote_uid IS NULL)),
            UNIQUE(id, account_id)
        )
        """
    )
    connection.execute("INSERT INTO remote_messages_v7 SELECT * FROM remote_messages")
    connection.execute(
        """CREATE TABLE remote_canonical_links_v7 (
            remote_message_id TEXT PRIMARY KEY REFERENCES remote_messages_v7(id),
            canonical_message_id TEXT NOT NULL REFERENCES canonical_messages(id),
            link_reason TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(remote_message_id, canonical_message_id))"""
    )
    connection.execute("INSERT INTO remote_canonical_links_v7 SELECT * FROM remote_canonical_links")
    connection.execute(
        """CREATE TABLE gmail_message_labels_v7 (
            remote_message_id TEXT NOT NULL,
            account_id INTEGER NOT NULL,
            label_id TEXT NOT NULL,
            PRIMARY KEY(remote_message_id, label_id),
            FOREIGN KEY(remote_message_id, account_id)
                REFERENCES remote_messages_v7(id, account_id),
            FOREIGN KEY(account_id, label_id) REFERENCES gmail_labels(account_id, label_id)
        )"""
    )
    connection.execute("INSERT INTO gmail_message_labels_v7 SELECT * FROM gmail_message_labels")
    connection.execute("DROP TABLE gmail_message_labels")
    connection.execute("DROP TABLE remote_canonical_links")
    connection.execute("DROP TABLE remote_messages")
    connection.execute("ALTER TABLE remote_messages_v7 RENAME TO remote_messages")
    connection.execute("ALTER TABLE remote_canonical_links_v7 RENAME TO remote_canonical_links")
    connection.execute("ALTER TABLE gmail_message_labels_v7 RENAME TO gmail_message_labels")
    connection.execute("""CREATE UNIQUE INDEX remote_messages_imap_identity
        ON remote_messages(account_id, remote_folder, uidvalidity, remote_uid)
        WHERE provider_kind='imap'""")
    connection.execute("""CREATE UNIQUE INDEX remote_messages_gmail_identity
        ON remote_messages(account_id, provider_message_id)
        WHERE provider_kind='gmail'""")
    connection.execute("""CREATE UNIQUE INDEX remote_messages_pop3_identity
        ON remote_messages(account_id, provider_message_id)
        WHERE provider_kind='pop3'""")


def _migration_8(connection: sqlite3.Connection) -> None:
    """M7 lifecycle state; pre-M7 rows were already admitted archive objects."""
    connection.execute("""
        CREATE TABLE canonical_messages_v8 (
            id TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id),
            sha256 TEXT NOT NULL CHECK(length(sha256)=64), local_path TEXT NOT NULL UNIQUE,
            size_bytes INTEGER NOT NULL CHECK(size_bytes>=0), message_id_header TEXT,
            message_date TEXT, downloaded_at TEXT NOT NULL, archived_at TEXT,
            storage_state TEXT NOT NULL CHECK(storage_state IN ('pending','archived','quarantined')),
            quarantined_at TEXT, integrity_status TEXT NOT NULL CHECK(integrity_status IN ('verified','failed')),
            integrity_verified_at TEXT, created_at TEXT NOT NULL, UNIQUE(account_id, sha256),
            CHECK(
                (storage_state='pending' AND archived_at IS NULL AND quarantined_at IS NULL)
                OR (storage_state='archived' AND archived_at IS NOT NULL)
                OR (storage_state='quarantined' AND quarantined_at IS NOT NULL)
            )
        )""")
    connection.execute("""INSERT INTO canonical_messages_v8(
        id,account_id,sha256,local_path,size_bytes,message_id_header,message_date,downloaded_at,
        archived_at,storage_state,quarantined_at,integrity_status,integrity_verified_at,created_at)
        SELECT id,account_id,sha256,local_path,size_bytes,message_id_header,message_date,downloaded_at,
        archived_at,'archived',NULL,integrity_status,integrity_verified_at,created_at FROM canonical_messages""")
    connection.execute("DROP TABLE canonical_messages")
    connection.execute("ALTER TABLE canonical_messages_v8 RENAME TO canonical_messages")
    connection.execute("""CREATE TABLE classifications (
        id INTEGER PRIMARY KEY, canonical_message_id TEXT NOT NULL REFERENCES canonical_messages(id),
        classification TEXT NOT NULL CHECK(classification IN ('ham','suspect','spam')),
        score REAL, reason TEXT NOT NULL CHECK(length(reason)<=256), classifier TEXT NOT NULL CHECK(length(classifier)<=128),
        classifier_version TEXT, manual_override INTEGER NOT NULL DEFAULT 0 CHECK(manual_override IN (0,1)),
        classified_at TEXT NOT NULL)""")
    connection.execute(
        "CREATE INDEX classifications_effective ON classifications(canonical_message_id, manual_override, id DESC)"
    )
    connection.execute("CREATE INDEX canonical_messages_state ON canonical_messages(storage_state)")


MIGRATIONS: tuple[tuple[int, Migration], ...] = (
    (1, _migration_1),
    (2, _migration_2),
    (3, _migration_3),
    (4, _migration_4),
    (5, _migration_5),
    (6, _migration_6),
    (7, _migration_7),
    (8, _migration_8),
)


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


def _apply_migration(connection: sqlite3.Connection, version: int, migration: Migration) -> None:
    """Apply one migration and its version record as one SQLite transaction."""
    # M7 rebuilds canonical_messages to make archived_at nullable.  SQLite cannot
    # drop a referenced table with FK enforcement enabled, even inside a transaction.
    rebuilds_canonical = version == 8
    try:
        if rebuilds_canonical:
            connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        migration(connection)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (version, utc_now()),
        )
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
    finally:
        if rebuilds_canonical:
            connection.execute("PRAGMA foreign_keys = ON")


def initialize(path: Path, accounts: tuple[AccountConfig, ...] = ()) -> None:
    """Apply migrations and reconcile M0 account metadata idempotently."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.commit()
        applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        for version, migration in MIGRATIONS:
            if version not in applied:
                _apply_migration(connection, version, migration)
        accounts_table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'accounts'"
        ).fetchone()
        if accounts_table_exists is None:
            return
        configured_names = tuple(account.name for account in accounts)
        if configured_names:
            placeholders = ", ".join("?" for _ in configured_names)
            connection.execute(
                f"UPDATE accounts SET enabled = 0, updated_at = ? "
                f"WHERE name NOT IN ({placeholders}) AND enabled != 0",
                (utc_now(), *configured_names),
            )
        else:
            connection.execute(
                "UPDATE accounts SET enabled = 0, updated_at = ? WHERE enabled != 0", (utc_now(),)
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


def account_id(connection: sqlite3.Connection, account_name: str) -> int | None:
    """Return the active local account ID, if configured and enabled."""
    row = connection.execute(
        "SELECT id FROM accounts WHERE name = ? AND enabled = 1", (account_name,)
    ).fetchone()
    return None if row is None else int(row[0])


def canonical_message_by_account_and_sha256(
    connection: sqlite3.Connection, account_id: int, sha256: str
) -> CanonicalMessage | None:
    """Find an account-scoped canonical byte object without relying on Message-ID."""
    row = connection.execute(
        """
        SELECT id, account_id, sha256, local_path, size_bytes, message_id_header, message_date,
               downloaded_at, archived_at, storage_state, quarantined_at, integrity_status, integrity_verified_at, created_at
        FROM canonical_messages WHERE account_id = ? AND sha256 = ?
        """,
        (account_id, sha256),
    ).fetchone()
    if row is None:
        return None
    return CanonicalMessage(
        id=str(row["id"]),
        account_id=int(row["account_id"]),
        sha256=str(row["sha256"]),
        local_path=Path(str(row["local_path"])),
        size_bytes=int(row["size_bytes"]),
        message_id_header=(
            None if row["message_id_header"] is None else str(row["message_id_header"])
        ),
        message_date=None if row["message_date"] is None else str(row["message_date"]),
        downloaded_at=str(row["downloaded_at"]),
        archived_at=None if row["archived_at"] is None else str(row["archived_at"]),
        storage_state=cast(Literal["pending", "archived", "quarantined"], row["storage_state"]),
        quarantined_at=None if row["quarantined_at"] is None else str(row["quarantined_at"]),
        integrity_status=str(row["integrity_status"]),
        integrity_verified_at=(
            None if row["integrity_verified_at"] is None else str(row["integrity_verified_at"])
        ),
        created_at=str(row["created_at"]),
    )


def canonical_message_by_id(
    connection: sqlite3.Connection, identifier: str
) -> CanonicalMessage | None:
    """Resolve one authoritative canonical row for local-only lifecycle workers."""
    row = connection.execute(
        """SELECT id, account_id, sha256, local_path, size_bytes, message_id_header, message_date,
        downloaded_at, archived_at, storage_state, quarantined_at, integrity_status,
        integrity_verified_at, created_at FROM canonical_messages WHERE id=?""",
        (identifier,),
    ).fetchone()
    if row is None:
        return None
    return CanonicalMessage(
        id=str(row["id"]),
        account_id=int(row["account_id"]),
        sha256=str(row["sha256"]),
        local_path=Path(str(row["local_path"])),
        size_bytes=int(row["size_bytes"]),
        message_id_header=None
        if row["message_id_header"] is None
        else str(row["message_id_header"]),
        message_date=None if row["message_date"] is None else str(row["message_date"]),
        downloaded_at=str(row["downloaded_at"]),
        archived_at=None if row["archived_at"] is None else str(row["archived_at"]),
        storage_state=cast(Literal["pending", "archived", "quarantined"], row["storage_state"]),
        quarantined_at=None if row["quarantined_at"] is None else str(row["quarantined_at"]),
        integrity_status=str(row["integrity_status"]),
        integrity_verified_at=None
        if row["integrity_verified_at"] is None
        else str(row["integrity_verified_at"]),
        created_at=str(row["created_at"]),
    )


def register_canonical_message(
    database_path: Path,
    message: CanonicalMessage,
    *,
    audit_account_id: int,
    audit_event_type: str,
    audit_details_json: str,
) -> tuple[CanonicalMessage, bool]:
    """Atomically register a canonical object and its successful ingest audit event."""
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing = canonical_message_by_account_and_sha256(
                connection, message.account_id, message.sha256
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO canonical_messages(
                        id, account_id, sha256, local_path, size_bytes, message_id_header,
                        message_date, downloaded_at, archived_at, storage_state, quarantined_at, integrity_status,
                        integrity_verified_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.id,
                        message.account_id,
                        message.sha256,
                        str(message.local_path),
                        message.size_bytes,
                        message.message_id_header,
                        message.message_date,
                        message.downloaded_at,
                        message.archived_at,
                        message.storage_state,
                        message.quarantined_at,
                        message.integrity_status,
                        message.integrity_verified_at,
                        message.created_at,
                    ),
                )
                stored = message
                created = True
            else:
                stored = existing
                created = False
            insert_audit_event(
                connection,
                actor="mailarchive.ingest",
                event_type=audit_event_type if created else "ingest.reused",
                result="success",
                account_id=audit_account_id,
                canonical_message_id=stored.id,
                details_json=audit_details_json,
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
    return stored, created


def insert_audit_event(
    connection: sqlite3.Connection,
    *,
    actor: str,
    event_type: str,
    result: str,
    account_id: int | None = None,
    canonical_message_id: str | None = None,
    details_json: str = "{}",
) -> int:
    """Append an audit event and return its stable identifier."""
    cursor = connection.execute(
        """
        INSERT INTO audit_events(
            timestamp, actor, event_type, account_id, canonical_message_id, result, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (utc_now(), actor, event_type, account_id, canonical_message_id, result, details_json),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite did not return an audit event identifier")
    return cursor.lastrowid
