# ruff: noqa: E501

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import mailarchive.db as database
from mailarchive.config import load_config
from mailarchive.db import account_id, connect, initialize, insert_audit_event, utc_now


def test_database_initializes_idempotently(config_file: Path) -> None:
    config = load_config(config_file)
    initialize(config.database.path, config.accounts)
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 12
        assert connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


def test_real_populated_v11_upgrades_to_v12_without_rewriting_history(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M11 migration regression: begin with an actual complete v11 database."""
    config = load_config(config_file)
    original = database.MIGRATIONS
    monkeypatch.setattr(database, "MIGRATIONS", original[:11])
    initialize(config.database.path, config.accounts)
    now = utc_now()
    with connect(config.database.path) as db:
        aid = account_id(db, "test")
        assert aid is not None
        digest = "d" * 64
        db.execute(
            """INSERT INTO canonical_messages(id,account_id,sha256,local_path,size_bytes,downloaded_at,
            archived_at,storage_state,quarantined_at,integrity_status,integrity_verified_at,created_at)
            VALUES('v11-canonical',?,?,?,1,? ,?,'archived',NULL,'verified',?,?)""",
            (aid, digest, "/tmp/v11.eml", now, now, now, now),
        )
        db.execute(
            """INSERT INTO remote_messages(id,account_id,provider_kind,remote_folder,uidvalidity,
            remote_uid,first_seen_at,last_seen_at,remote_present,identity_confidence)
            VALUES('v11-remote',?,'imap','INBOX',1,2,?,?,1,'proven')""",
            (aid, now, now),
        )
        db.execute(
            "INSERT INTO remote_canonical_links VALUES('v11-remote','v11-canonical','fixture',?)",
            (now,),
        )
        db.execute(
            "INSERT INTO attachments(id,sha256,size_bytes,content_path,first_seen_at) VALUES(?,?,?,?,?)",
            ("e" * 64, "e" * 64, 1, "/tmp/v11-attachment", now),
        )
        db.execute(
            "INSERT INTO message_attachments VALUES('v11-canonical',?,0,'x','attachment','text/plain')",
            ("e" * 64,),
        )
        db.execute(
            """INSERT INTO attachment_extractions(canonical_message_id,source_sha256,status,
            attachment_count,extracted_at,last_error_kind,updated_at) VALUES(?,?,'success',1,?,NULL,?)""",
            ("v11-canonical", digest, now, now),
        )
        db.execute(
            """INSERT INTO backup_repositories(name,kind,repository_ref,repository_identity,enabled,
            encryption_mode,verification_policy,created_at,updated_at)
            VALUES('v11-repo','borg','/tmp/v11-borg','physical-v11',1,'none','borg-archive-data-v1',?,?)""",
            (now, now),
        )
        repository_id = db.execute(
            "SELECT id FROM backup_repositories WHERE name='v11-repo'"
        ).fetchone()[0]
        db.execute(
            """INSERT INTO backup_runs(id,repository_id,started_at,completed_at,status,archive_name,
            verification_status,verified_at) VALUES('v11-run',?,?,?,'succeeded','v11','verified',?)""",
            (repository_id, now, now, now),
        )
        db.execute(
            "INSERT INTO message_backup_evidence VALUES('v11-canonical','v11-run',1,1,?)", (now,)
        )
        db.execute(
            "INSERT INTO backup_restore_tests(backup_run_id,started_at,completed_at,status) VALUES('v11-run',?,?,'succeeded')",
            (now, now),
        )
        db.execute(
            "INSERT INTO retention_controls VALUES('v11-canonical',1,0,'fixture hold',?)", (now,)
        )
        evaluation_run = db.execute(
            "INSERT INTO deletion_evaluation_runs(evaluated_at,policy_version) VALUES(?, 'retention-v1')",
            (now,),
        )
        db.execute(
            """INSERT INTO deletion_evaluations(evaluation_run_id,remote_message_id,canonical_message_id,
            evaluated_at,eligible,reason_codes_json,policy_version,remote_retention_days,
            required_verified_backups,verified_repository_count,retention_deadline)
            VALUES(?,'v11-remote','v11-canonical',?,1,'[]','retention-v1',365,2,2,?)""",
            (evaluation_run.lastrowid, now, now),
        )
        db.commit()
    monkeypatch.setattr(database, "MIGRATIONS", original)
    initialize(config.database.path, config.accounts)
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 12
        assert (
            db.execute("SELECT sha256 FROM canonical_messages WHERE id='v11-canonical'").fetchone()[
                0
            ]
            == digest
        )
        assert (
            db.execute("SELECT remote_uid FROM remote_messages WHERE id='v11-remote'").fetchone()[0]
            == 2
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM message_attachments WHERE canonical_message_id='v11-canonical'"
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT repository_identity FROM backup_repositories WHERE name='v11-repo'"
            ).fetchone()[0]
            == "physical-v11"
        )
        assert (
            db.execute(
                "SELECT verified FROM message_backup_evidence WHERE backup_run_id='v11-run'"
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT keep_online FROM retention_controls WHERE canonical_message_id='v11-canonical'"
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT eligible FROM deletion_evaluations WHERE remote_message_id='v11-remote'"
            ).fetchone()[0]
            == 1
        )
        for table in ("remote_mutation_runs", "remote_mutations"):
            assert db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_real_v9_to_v10_preserves_m8_attachment_graph(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrade an already-existing complete M8 v9 database, rather than a fresh database."""
    config = load_config(config_file)
    original = database.MIGRATIONS
    monkeypatch.setattr(database, "MIGRATIONS", original[:9])
    initialize(config.database.path, config.accounts)
    now = utc_now()
    with connect(config.database.path) as db:
        account = account_id(db, "test")
        assert account is not None
        digest = "a" * 64
        db.execute(
            """INSERT INTO canonical_messages(id,account_id,sha256,local_path,size_bytes,downloaded_at,
            archived_at,storage_state,quarantined_at,integrity_status,integrity_verified_at,created_at)
            VALUES(?,?,?,?,?,?,?,'archived',NULL,'verified',?,?)""",
            ("m8-canonical", account, digest, "/tmp/m8.eml", 1, now, now, now, now),
        )
        db.execute(
            "INSERT INTO attachments(id,sha256,size_bytes,content_path,first_seen_at) VALUES(?,?,?,?,?)",
            ("b" * 64, "b" * 64, 2, "/tmp/blob", now),
        )
        db.execute(
            "INSERT INTO message_attachments VALUES(?,?,?,?,?,?)",
            ("m8-canonical", "b" * 64, 0, "fixture.bin", "attachment", "application/octet-stream"),
        )
        db.execute(
            """INSERT INTO attachment_extractions(canonical_message_id,source_sha256,status,
            attachment_count,extracted_at,last_error_kind,updated_at) VALUES(?,?,'success',1,?,NULL,?)""",
            ("m8-canonical", digest, now, now),
        )
        db.execute(
            """INSERT INTO classifications(canonical_message_id,classification,reason,classifier,
            manual_override,classified_at) VALUES(?,'ham','fixture','pytest',0,?)""",
            ("m8-canonical", now),
        )
        insert_audit_event(db, actor="pytest", event_type="fixture.v9", result="success")
        db.commit()
    monkeypatch.setattr(database, "MIGRATIONS", original)
    initialize(config.database.path, config.accounts)
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 12
        assert db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 1
        assert db.execute("SELECT status FROM attachment_extractions").fetchone()[0] == "success"
        for table in (
            "backup_repositories",
            "backup_runs",
            "message_backup_evidence",
            "backup_restore_tests",
        ):
            assert db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_real_v10_to_v11_preserves_m9_evidence_and_identity_graph(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrade a populated M9/v10 database without rewriting its evidence history."""
    config = load_config(config_file)
    original = database.MIGRATIONS
    monkeypatch.setattr(database, "MIGRATIONS", original[:10])
    initialize(config.database.path, config.accounts)
    now = utc_now()
    with connect(config.database.path) as db:
        aid = account_id(db, "test")
        assert aid is not None
        db.execute(
            """INSERT INTO canonical_messages(id,account_id,sha256,local_path,size_bytes,downloaded_at,
            archived_at,storage_state,quarantined_at,integrity_status,integrity_verified_at,created_at)
            VALUES(?,?,?,?,?,?,?,'archived',NULL,'verified',?,?)""",
            ("v10-canonical", aid, "c" * 64, "/tmp/v10.eml", 1, now, now, now, now),
        )
        db.execute(
            """INSERT INTO remote_messages(id,account_id,provider_kind,remote_folder,uidvalidity,
            remote_uid,provider_message_id,provider_thread_id,message_id_header,first_seen_at,last_seen_at,
            remote_present,identity_confidence) VALUES('v10-remote',?,'imap','INBOX',1,1,NULL,NULL,NULL,?,?,1,'proven')""",
            (aid, now, now),
        )
        db.execute(
            "INSERT INTO remote_canonical_links VALUES('v10-remote','v10-canonical','fixture',?)",
            (now,),
        )
        db.execute(
            "INSERT INTO attachments VALUES(?,?,?,?,?)",
            ("a" * 64, "a" * 64, 1, "/tmp/v10-attachment", now),
        )
        db.execute(
            "INSERT INTO message_attachments VALUES('v10-canonical',?,0,'x','attachment','text/plain')",
            ("a" * 64,),
        )
        db.execute(
            """INSERT INTO backup_repositories(name,kind,repository_ref,repository_identity,enabled,
            encryption_mode,verification_policy,created_at,updated_at) VALUES('v10','borg','/tmp/v10-borg',
            'physical-v10',1,'none','borg-archive-data-v1',?,?)""",
            (now, now),
        )
        repository_id = db.execute(
            "SELECT id FROM backup_repositories WHERE name='v10'"
        ).fetchone()[0]
        db.execute(
            """INSERT INTO backup_runs(id,repository_id,started_at,completed_at,status,archive_name,
            verification_status,verified_at) VALUES('v10-run',?,?,?,'succeeded','fixture','verified',?)""",
            (repository_id, now, now, now),
        )
        db.execute(
            "INSERT INTO message_backup_evidence VALUES('v10-canonical','v10-run',1,1,?)", (now,)
        )
        db.execute(
            "INSERT INTO backup_restore_tests(backup_run_id,started_at,completed_at,status) VALUES('v10-run',?,?,'succeeded')",
            (now, now),
        )
        db.commit()
    monkeypatch.setattr(database, "MIGRATIONS", original)
    initialize(config.database.path, config.accounts)
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 12
        assert (
            db.execute(
                "SELECT repository_identity FROM backup_repositories WHERE name='v10'"
            ).fetchone()[0]
            == "physical-v10"
        )
        assert (
            db.execute(
                "SELECT verified FROM message_backup_evidence WHERE backup_run_id='v10-run'"
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT canonical_message_id FROM remote_canonical_links WHERE remote_message_id='v10-remote'"
            ).fetchone()[0]
            == "v10-canonical"
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM backup_restore_tests WHERE backup_run_id='v10-run'"
            ).fetchone()[0]
            == 1
        )
        for table in ("retention_controls", "deletion_evaluation_runs", "deletion_evaluations"):
            assert db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_m3_schema_v4_upgrades_to_v5_without_changing_remote_links(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4 must upgrade an actual M3 schema, where links already exist in v4."""
    config = load_config(config_file)
    original = database.MIGRATIONS
    monkeypatch.setattr(database, "MIGRATIONS", original[:4])
    initialize(config.database.path, config.accounts)
    now = utc_now()
    with connect(config.database.path) as connection:
        aid = int(connection.execute("SELECT id FROM accounts WHERE name='test'").fetchone()[0])
        connection.execute(
            "INSERT INTO canonical_messages VALUES (?, ?, ?, ?, 1, NULL, NULL, ?, ?, 'verified', ?, ?)",
            ("canonical", aid, "c" * 64, "/tmp/canonical.eml", now, now, now, now),
        )
        connection.execute(
            "INSERT INTO remote_messages VALUES (?, ?, 'INBOX', 7, 9, NULL, ?, ?, 1, 'proven')",
            ("remote", aid, now, now),
        )
        connection.execute(
            "INSERT INTO remote_canonical_links VALUES ('remote', 'canonical', 'imap-uid-body-peek', ?)",
            (now,),
        )
        connection.commit()
    monkeypatch.setattr(database, "MIGRATIONS", original)
    initialize(config.database.path, config.accounts)
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 12
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='fast_path_health'"
        ).fetchone()
        assert tuple(connection.execute("SELECT * FROM remote_canonical_links").fetchone()) == (
            "remote",
            "canonical",
            "imap-uid-body-peek",
            now,
        )


def test_real_v5_to_v6_preserves_imap_identity_links_and_health(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(config_file)
    original = database.MIGRATIONS
    monkeypatch.setattr(database, "MIGRATIONS", original[:5])
    initialize(config.database.path, config.accounts)
    now = utc_now()
    with connect(config.database.path) as db:
        aid = int(db.execute("SELECT id FROM accounts WHERE name='test'").fetchone()[0])
        db.execute(
            "INSERT INTO canonical_messages VALUES (?, ?, ?, ?, 1, NULL, NULL, ?, ?, 'verified', ?, ?)",
            ("canonical", aid, "a" * 64, "/tmp/a.eml", now, now, now, now),
        )
        db.execute(
            "INSERT INTO remote_messages VALUES (?, ?, 'INBOX', 9, 7, NULL, ?, ?, 1, 'proven')",
            ("unchanged-id", aid, now, now),
        )
        db.execute(
            "INSERT INTO remote_canonical_links VALUES (?, 'canonical', 'imap-uid-body-peek', ?)",
            ("unchanged-id", now),
        )
        db.execute(
            "INSERT INTO fast_path_health(account_id,remote_folder,mode,consecutive_failures,reconnect_count,index_pending,updated_at) VALUES (?, 'INBOX', 'idle', 2, 3, 1, ?)",
            (aid, now),
        )
        db.commit()
    monkeypatch.setattr(database, "MIGRATIONS", original)
    initialize(config.database.path, config.accounts)
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 12
        assert tuple(
            db.execute(
                "SELECT id,provider_kind,remote_folder,uidvalidity,remote_uid FROM remote_messages"
            ).fetchone()
        ) == ("unchanged-id", "imap", "INBOX", 9, 7)
        assert tuple(
            db.execute(
                "SELECT remote_message_id,canonical_message_id FROM remote_canonical_links"
            ).fetchone()
        ) == ("unchanged-id", "canonical")
        assert tuple(
            db.execute(
                "SELECT mode,consecutive_failures,reconnect_count,index_pending FROM fast_path_health"
            ).fetchone()
        ) == ("idle", 2, 3, 1)
        assert db.execute("SELECT name FROM sqlite_master WHERE name='gmail_labels'").fetchone()
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO remote_messages(id,account_id,provider_kind,remote_folder,uidvalidity,remote_uid,provider_message_id,provider_thread_id,message_id_header,first_seen_at,last_seen_at,remote_present,identity_confidence) VALUES ('imap-duplicate',?,'imap','INBOX',9,7,NULL,NULL,NULL,?, ?,1,'proven')",
                (aid, now, now),
            )
        db.execute(
            "INSERT INTO remote_messages(id,account_id,provider_kind,remote_folder,uidvalidity,remote_uid,provider_message_id,provider_thread_id,message_id_header,first_seen_at,last_seen_at,remote_present,identity_confidence) VALUES ('gmail-one',?,'gmail',NULL,NULL,NULL,'same',NULL,NULL,?, ?,1,'proven')",
            (aid, now, now),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO remote_messages(id,account_id,provider_kind,remote_folder,uidvalidity,remote_uid,provider_message_id,provider_thread_id,message_id_header,first_seen_at,last_seen_at,remote_present,identity_confidence) VALUES ('gmail-two',?,'gmail',NULL,NULL,NULL,'same',NULL,NULL,?, ?,1,'proven')",
                (aid, now, now),
            )


def test_v7_rebuild_preserves_gmail_label_foreign_key_graph(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repository-integrity regression: v7 rebuilds every table referencing identities."""
    config = load_config(config_file)
    original = database.MIGRATIONS
    monkeypatch.setattr(database, "MIGRATIONS", original[:6])
    initialize(config.database.path, config.accounts)
    now = utc_now()
    with connect(config.database.path) as db:
        aid = int(db.execute("SELECT id FROM accounts WHERE name='test'").fetchone()[0])
        db.execute(
            "INSERT INTO canonical_messages VALUES (?, ?, ?, ?, 1, NULL, NULL, ?, ?, 'verified', ?, ?)",
            ("gmail-canonical", aid, "g" * 64, "/tmp/gmail.eml", now, now, now, now),
        )
        db.execute(
            """INSERT INTO remote_messages VALUES
               ('gmail-remote', ?, 'gmail', NULL, NULL, NULL, 'G1', 'T1', NULL, ?, ?, 1, 'proven')""",
            (aid, now, now),
        )
        db.execute(
            "INSERT INTO remote_canonical_links VALUES ('gmail-remote', 'gmail-canonical', 'gmail-api-raw', ?)",
            (now,),
        )
        db.execute(
            "INSERT INTO gmail_labels VALUES (?, 'Label_1', 'Project', 'user', 1, ?, ?)",
            (aid, now, now),
        )
        db.execute("INSERT INTO gmail_message_labels VALUES ('gmail-remote', ?, 'Label_1')", (aid,))
        db.execute("INSERT INTO gmail_sync_state(account_id,updated_at) VALUES (?,?)", (aid, now))
        db.commit()
    monkeypatch.setattr(database, "MIGRATIONS", original)
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 12
        assert tuple(
            db.execute(
                "SELECT remote_message_id,account_id,label_id FROM gmail_message_labels"
            ).fetchone()
        ) == ("gmail-remote", aid, "Label_1")
        assert tuple(
            db.execute(
                "SELECT remote_message_id,canonical_message_id FROM remote_canonical_links"
            ).fetchone()
        ) == ("gmail-remote", "gmail-canonical")
        assert db.execute("SELECT provider_kind FROM remote_messages").fetchone()[0] == "gmail"
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_foreign_keys_are_enabled(config_file: Path) -> None:
    config = load_config(config_file)
    initialize(config.database.path)
    with connect(config.database.path) as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_v8_failure_restores_foreign_keys_and_retries(config_file: Path) -> None:
    config = load_config(config_file)
    original = database.MIGRATIONS
    database.MIGRATIONS = original[:7]
    initialize(config.database.path, config.accounts)
    database.MIGRATIONS = original
    with connect(config.database.path) as db:

        def fail(_connection: sqlite3.Connection) -> None:
            raise RuntimeError("v8 injected failure")

        with pytest.raises(RuntimeError, match="injected"):
            database._apply_migration(db, 8, fail)  # pyright: ignore[reportPrivateUsage]
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        database._apply_migration(db, 8, database._migration_8)  # pyright: ignore[reportPrivateUsage]
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    initialize(config.database.path, config.accounts)


def test_v8_lifecycle_constraints_reject_impossible_timestamp_combinations(
    config_file: Path,
) -> None:
    config = load_config(config_file)
    initialize(config.database.path, config.accounts)
    now = utc_now()
    with connect(config.database.path) as db:
        aid = account_id(db, "test")
        assert aid is not None
        for state, archived_at, quarantined_at in (
            ("pending", now, None),
            ("pending", None, now),
            ("archived", None, None),
            ("quarantined", None, None),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    """INSERT INTO canonical_messages(id,account_id,sha256,local_path,size_bytes,
                    downloaded_at,archived_at,storage_state,quarantined_at,integrity_status,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"{state}-{archived_at}-{quarantined_at}",
                        aid,
                        "f" * 64,
                        f"/tmp/{state}-{archived_at}-{quarantined_at}",
                        1,
                        now,
                        archived_at,
                        state,
                        quarantined_at,
                        "verified",
                        now,
                    ),
                )
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_v8_to_v9_preserves_state_and_adds_attachment_constraints(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(config_file)
    original = database.MIGRATIONS
    monkeypatch.setattr(database, "MIGRATIONS", original[:8])
    initialize(config.database.path, config.accounts)
    now = utc_now()
    with connect(config.database.path) as db:
        aid = account_id(db, "test")
        assert aid is not None
        db.execute(
            """INSERT INTO canonical_messages(id,account_id,sha256,local_path,size_bytes,downloaded_at,
            archived_at,storage_state,quarantined_at,integrity_status,integrity_verified_at,created_at)
            VALUES(?,?,?,?,?,?,?,'archived',NULL,'verified',?,?)""",
            ("v8-message", aid, "9" * 64, "/tmp/v8.eml", 1, now, now, now, now),
        )
        db.execute(
            """INSERT INTO classifications(canonical_message_id,classification,score,reason,classifier,
            classifier_version,manual_override,classified_at) VALUES(?, 'ham', NULL, 'fixture', 'test', NULL, 0, ?)""",
            ("v8-message", now),
        )
        insert_audit_event(db, actor="pytest", event_type="fixture.v8", result="success")
        db.commit()
    monkeypatch.setattr(database, "MIGRATIONS", original)
    initialize(config.database.path, config.accounts)
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 12
        assert db.execute("SELECT id FROM canonical_messages").fetchone()[0] == "v8-message"
        assert db.execute("SELECT classification FROM classifications").fetchone()[0] == "ham"
        assert db.execute("SELECT name FROM sqlite_master WHERE name='attachments'").fetchone()
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name='message_attachments'"
        ).fetchone()
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


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
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'migration_probe'"
            ).fetchone()[0]
            == 0
        )
        migration_row = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 1"
        ).fetchone()
        assert migration_row[0] == 0

    def corrected_migration(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE migration_probe (id INTEGER PRIMARY KEY)")

    monkeypatch.setattr(database, "MIGRATIONS", ((1, corrected_migration),))
    initialize(database_path)
    with connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'migration_probe'"
            ).fetchone()[0]
            == 1
        )
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


def test_account_scoped_migration_preserves_existing_message_and_audit(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(config_file)
    original_migrations = database.MIGRATIONS
    monkeypatch.setattr(database, "MIGRATIONS", original_migrations[:2])
    initialize(config.database.path, config.accounts)
    sha256 = "a" * 64
    now = utc_now()
    with connect(config.database.path) as connection:
        account_row = connection.execute("SELECT id FROM accounts WHERE name = 'test'").fetchone()
        account_id = int(account_row[0])
        connection.execute(
            """
            INSERT INTO canonical_messages(
                id, account_id, sha256, local_path, size_bytes, message_id_header, message_date,
                downloaded_at, archived_at, integrity_status, integrity_verified_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sha256,
                account_id,
                sha256,
                "/tmp/legacy.eml",
                1,
                None,
                None,
                now,
                now,
                "verified",
                now,
                now,
            ),
        )
        insert_audit_event(
            connection,
            actor="pytest",
            event_type="ingest.succeeded",
            result="success",
            account_id=account_id,
            canonical_message_id=sha256,
        )
    monkeypatch.setattr(database, "MIGRATIONS", original_migrations)
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as connection:
        message_row = connection.execute("SELECT id FROM canonical_messages").fetchone()
        audit_row = connection.execute("SELECT canonical_message_id FROM audit_events").fetchone()
    expected_id = f"{account_id}:{sha256}"
    assert message_row[0] == expected_id
    assert audit_row[0] == expected_id
