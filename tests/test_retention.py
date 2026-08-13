from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mailarchive.retention import RetentionFacts, evaluate
from mailarchive.retention import evaluate_all, set_control
from mailarchive.config import load_config
from mailarchive.classification import ClassificationResult, apply_classification
from mailarchive.db import account_id, connect, initialize
from mailarchive.ingest import ingest_bytes


def facts(path: Path, **changes: object) -> RetentionFacts:
    value: dict[str, object] = {
        "remote_message_id": "remote",
        "account": "test",
        "account_enabled": True,
        "provider_kind": "imap",
        "remote_present": True,
        "identity_confidence": "proven",
        "identity_complete": True,
        "canonical_id": "canonical",
        "link_count": 1,
        "canonical_exists": True,
        "storage_state": "archived",
        "archived_at": "2025-01-01T00:00:00+00:00",
        "canonical_path": path,
        "expected_sha256": "",
        "integrity_status": "verified",
        "verified_repository_count": 2,
        "keep_online": False,
        "legal_hold": False,
    }
    has_expected_hash = "expected_sha256" in changes
    value.update(changes)
    import hashlib

    if not has_expected_hash:
        value["expected_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return RetentionFacts(**value)  # type: ignore[arg-type]


def test_exact_retention_boundary_and_multiple_reasons(tmp_path: Path) -> None:
    mail = tmp_path / "mail"
    mail.mkdir()
    message = mail / "m.eml"
    message.write_bytes(b"fixture")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    base = facts(message)
    assert evaluate(
        base, now=now, retention_days=365, required_verified_backups=2, managed_mail_root=mail
    ).eligible
    before = evaluate(
        base,
        now=now - timedelta(microseconds=1),
        retention_days=365,
        required_verified_backups=2,
        managed_mail_root=mail,
    )
    assert before.reason_codes == ("RETENTION_NOT_ELAPSED",)
    blocked = evaluate(
        facts(
            message,
            remote_present=False,
            keep_online=True,
            legal_hold=True,
            verified_repository_count=0,
        ),
        now=now,
        retention_days=365,
        required_verified_backups=2,
        managed_mail_root=mail,
    )
    assert blocked.reason_codes == (
        "REMOTE_NOT_PRESENT",
        "BACKUPS_INSUFFICIENT",
        "KEEP_ONLINE",
        "LEGAL_HOLD",
    )


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"storage_state": "quarantined"}, "QUARANTINED"),
        ({"integrity_status": "failed"}, "INTEGRITY_NOT_VERIFIED"),
        ({"identity_complete": False}, "REMOTE_IDENTITY_INCOMPLETE"),
        ({"link_count": 0, "canonical_id": None, "canonical_exists": False}, "REMOTE_LINK_MISSING"),
    ],
)
def test_fail_closed_conditions(tmp_path: Path, change: dict[str, object], expected: str) -> None:
    mail = tmp_path / "mail"
    mail.mkdir()
    message = mail / "m.eml"
    message.write_bytes(b"fixture")
    result = evaluate(
        facts(message, **change),
        now=datetime(2026, 1, 1, tzinfo=UTC),
        retention_days=365,
        required_verified_backups=2,
        managed_mail_root=mail,
    )
    assert expected in result.reason_codes


def test_never_and_path_and_hash_block(tmp_path: Path) -> None:
    mail = tmp_path / "mail"
    mail.mkdir()
    message = mail / "m.eml"
    message.write_bytes(b"fixture")
    outside = tmp_path / "outside.eml"
    outside.write_bytes(b"fixture")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert (
        "RETENTION_NEVER"
        in evaluate(
            facts(message),
            now=now,
            retention_days=None,
            required_verified_backups=2,
            managed_mail_root=mail,
        ).reason_codes
    )
    assert (
        "CANONICAL_PATH_INVALID"
        in evaluate(
            facts(outside),
            now=now,
            retention_days=365,
            required_verified_backups=2,
            managed_mail_root=mail,
        ).reason_codes
    )
    assert (
        "CANONICAL_HASH_MISMATCH"
        in evaluate(
            facts(message, expected_sha256="0" * 64),
            now=now,
            retention_days=365,
            required_verified_backups=2,
            managed_mail_root=mail,
        ).reason_codes
    )


def test_distinct_evidence_and_canonical_controls(config_file: Path) -> None:
    config = load_config(config_file)
    initialize(config.database.path, config.accounts)
    message = ingest_bytes(config, b"From: fixture\r\n\r\nbody", "test").canonical_message
    message = apply_classification(
        config, message, ClassificationResult("ham", None, "fixture", "pytest")
    )
    now = datetime(2026, 8, 13, tzinfo=UTC)
    with connect(config.database.path) as db:
        aid = account_id(db, "test")
        assert aid is not None
        db.execute(
            "UPDATE canonical_messages SET archived_at=? WHERE id=?",
            ("2025-08-13T00:00:00+00:00", message.id),
        )
        db.execute(
            """INSERT INTO remote_messages(id,account_id,provider_kind,remote_folder,uidvalidity,
            remote_uid,provider_message_id,provider_thread_id,message_id_header,first_seen_at,last_seen_at,
            remote_present,identity_confidence) VALUES('remote',?,'imap','INBOX',1,1,NULL,NULL,NULL,?,?,1,'proven')""",
            (aid, now.isoformat(), now.isoformat()),
        )
        db.execute(
            "INSERT INTO remote_canonical_links VALUES('remote',?,'test',?)",
            (message.id, now.isoformat()),
        )
        for repository, run in (("one", "run-one"), ("one", "run-two"), ("two", "run-three")):
            db.execute(
                """INSERT INTO backup_repositories(name,kind,repository_ref,repository_identity,enabled,
                encryption_mode,verification_policy,created_at,updated_at) VALUES(?,'borg',?,?,1,'none',
                'borg-archive-data-v1',?,?) ON CONFLICT(name) DO NOTHING""",
                (repository, f"/tmp/{repository}", repository, now.isoformat(), now.isoformat()),
            )
            repository_id = db.execute(
                "SELECT id FROM backup_repositories WHERE name=?", (repository,)
            ).fetchone()[0]
            db.execute(
                """INSERT INTO backup_runs(id,repository_id,started_at,completed_at,status,archive_name,
                verification_status,verified_at) VALUES(?,?,?,?,?,?,'verified',?)""",
                (
                    run,
                    repository_id,
                    now.isoformat(),
                    now.isoformat(),
                    "succeeded",
                    run,
                    now.isoformat(),
                ),
            )
            db.execute(
                "INSERT INTO message_backup_evidence VALUES(?,?,1,1,?)",
                (message.id, run, now.isoformat()),
            )
        db.commit()
    report = evaluate_all(config, now=now)[0]
    assert report["verified_repository_count"] == 2
    assert report["eligible"] is True
    set_control(config, message.id, "keep-online", "operator request", enabled=True)
    assert "KEEP_ONLINE" in evaluate_all(config, now=now)[0]["reason_codes"]
    set_control(config, message.id, "keep-online", "operator release", enabled=False)
    assert evaluate_all(config, now=now)[0]["eligible"] is True
