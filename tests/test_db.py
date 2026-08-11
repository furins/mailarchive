from __future__ import annotations

from pathlib import Path

from mailarchive.config import load_config
from mailarchive.db import connect, initialize, insert_audit_event


def test_database_initializes_idempotently(config_file: Path) -> None:
    config = load_config(config_file)
    initialize(config.database.path, config.accounts)
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
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
