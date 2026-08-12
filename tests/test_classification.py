from __future__ import annotations

import hashlib
from pathlib import Path

from mailarchive.classification import (
    ClassificationResult,
    apply_classification,
    effective_classification,
)
from mailarchive.config import load_config
from mailarchive.db import connect
from mailarchive.ingest import ingest_bytes


def test_ham_promotes_staging_bytes_only_at_classification(config_file: Path) -> None:
    config = load_config(config_file)
    raw = b"Message-ID: <m@test>\r\n\r\nexact\x00bytes"
    pending = ingest_bytes(config, raw, "test").canonical_message
    assert pending.storage_state == "pending" and pending.archived_at is None
    archived = apply_classification(
        config, pending, ClassificationResult("ham", 0.1, "rspamd-action:no action")
    )
    assert archived.storage_state == "archived" and archived.archived_at is not None
    assert archived.local_path.read_bytes() == raw
    assert archived.sha256 == hashlib.sha256(raw).hexdigest()


def test_suspect_quarantine_and_manual_restore_is_append_only(config_file: Path) -> None:
    config = load_config(config_file)
    raw = b"Message-ID: <m@test>\r\r\nspam-body"
    pending = ingest_bytes(config, raw, "test").canonical_message
    quarantined = apply_classification(
        config, pending, ClassificationResult("spam", 9.0, "rspamd-action:reject")
    )
    assert quarantined.storage_state == "quarantined" and quarantined.archived_at is None
    restored = apply_classification(
        config,
        quarantined,
        ClassificationResult("ham", None, "operator confirmed", "manual"),
        manual=True,
    )
    assert restored.id == pending.id and restored.storage_state == "archived"
    assert restored.local_path.read_bytes() == raw
    with connect(config.database.path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM classifications WHERE canonical_message_id=?", (pending.id,)
            ).fetchone()[0]
            == 2
        )
        effective = effective_classification(db, pending.id)
        assert effective is not None and effective["classification"] == "ham"


def test_latest_manual_override_wins_over_later_automatic_result(config_file: Path) -> None:
    config = load_config(config_file)
    pending = ingest_bytes(config, b"From: a\r\n\r\nx", "test").canonical_message
    message = apply_classification(config, pending, ClassificationResult("spam", 9, "spam"))
    message = apply_classification(
        config, message, ClassificationResult("ham", None, "review", "manual"), manual=True
    )
    apply_classification(config, message, ClassificationResult("spam", 10, "retry"))
    with connect(config.database.path) as db:
        effective = effective_classification(db, message.id)
        assert effective is not None and effective["classification"] == "ham"
