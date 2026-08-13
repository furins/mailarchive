from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mailarchive.retention import RetentionFacts, evaluate


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
