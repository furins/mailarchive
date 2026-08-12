from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import mailarchive.classification as classification_module
from mailarchive.classification import (
    ClassificationResult,
    RspamdAdapter,
    apply_classification,
    effective_classification,
    reconcile_pending,
)
from mailarchive.config import load_config
from mailarchive.db import connect
from mailarchive.ingest import ingest_bytes


class _Adapter:
    def __init__(self, result: ClassificationResult) -> None:
        self.result = result
        self.inputs: list[bytes] = []

    def classify(self, raw_bytes: bytes) -> ClassificationResult:
        self.inputs.append(raw_bytes)
        return self.result


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


def test_reconcile_pending_is_local_and_preserves_exact_bytes(config_file: Path) -> None:
    config = load_config(config_file)
    raw = b"From: sender@example.test\r\n\r\nrecover-me"
    pending = ingest_bytes(config, raw, "test").canonical_message
    adapter = _Adapter(ClassificationResult("spam", 12, "rspamd-action:reject"))
    assert reconcile_pending(config, account_name="test", adapter=adapter) == [pending.id]
    assert adapter.inputs == [raw]
    with connect(config.database.path) as db:
        row = db.execute(
            "SELECT storage_state,archived_at,quarantined_at,local_path "
            "FROM canonical_messages WHERE id=?",
            (pending.id,),
        ).fetchone()
        assert row is not None and tuple(row[:3]) == ("quarantined", None, row["quarantined_at"])
        assert Path(str(row["local_path"])).read_bytes() == raw
        assert (
            db.execute(
                "SELECT COUNT(*) FROM classifications WHERE canonical_message_id=?", (pending.id,)
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("corrupt", [False, True])
def test_reconcile_pending_fails_closed_for_missing_or_corrupt_bytes(
    config_file: Path, corrupt: bool
) -> None:
    config = load_config(config_file)
    pending = ingest_bytes(config, b"From: a\r\n\r\npending", "test").canonical_message
    if corrupt:
        pending.local_path.write_bytes(b"changed")
    else:
        pending.local_path.unlink()
    assert (
        reconcile_pending(config, adapter=_Adapter(ClassificationResult("ham", 0, "clean"))) == []
    )
    with connect(config.database.path) as db:
        assert (
            db.execute(
                "SELECT storage_state FROM canonical_messages WHERE id=?", (pending.id,)
            ).fetchone()[0]
            == "pending"
        )
        assert (
            db.execute(
                "SELECT event_type FROM audit_events WHERE canonical_message_id=? ORDER BY id DESC",
                (pending.id,),
            ).fetchone()[0]
            == "classification.failed"
        )


def test_transition_audit_matches_actual_state_change(config_file: Path) -> None:
    config = load_config(config_file)
    pending = ingest_bytes(config, b"From: a\r\n\r\nx", "test").canonical_message
    archived = apply_classification(config, pending, ClassificationResult("ham", 0, "clean"))
    quarantined = apply_classification(
        config, archived, ClassificationResult("spam", 9, "bad"), manual=True
    )
    apply_classification(
        config, quarantined, ClassificationResult("suspect", 5, "review"), manual=True
    )
    apply_classification(
        config, quarantined, ClassificationResult("ham", 0, "restore"), manual=True
    )
    with connect(config.database.path) as db:
        events = [
            str(row[0])
            for row in db.execute(
                "SELECT event_type FROM audit_events WHERE canonical_message_id=?", (pending.id,)
            )
        ]
    assert events.count("quarantine.entered") == 1
    assert events.count("quarantine.restored") == 1
    assert "classification.succeeded" in events


def test_classifier_failure_is_audited_as_fail_safe(config_file: Path) -> None:
    config = load_config(config_file)
    pending = ingest_bytes(config, b"From: a\r\n\r\nx", "test").canonical_message
    apply_classification(
        config, pending, ClassificationResult("suspect", None, "classifier-timeout")
    )
    with connect(config.database.path) as db:
        events = [
            str(row[0])
            for row in db.execute(
                "SELECT event_type FROM audit_events WHERE canonical_message_id=?", (pending.id,)
            )
        ]
    assert "classification.failed" in events and "quarantine.entered" in events


def test_cross_device_copy_failure_leaves_source_and_no_final_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "staging" / "test" / "cur" / "a.eml"
    destination = tmp_path / "mail" / "test" / "cur" / "a.eml"
    source.parent.mkdir(parents=True)
    raw = b"exact bytes"
    source.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    def cross_device(*_arguments: object) -> None:
        raise OSError("cross-device")

    def copy_failure(*_arguments: object, **_keywords: object) -> None:
        raise OSError("copy failed")

    monkeypatch.setattr(classification_module.os, "link", cross_device)
    monkeypatch.setattr(classification_module.shutil, "copyfileobj", copy_failure)
    with pytest.raises(OSError, match="copy failed"):
        classification_module._move_exact(  # pyright: ignore[reportPrivateUsage]
            source, destination, digest
        )
    assert source.read_bytes() == raw and not destination.exists()


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("no action", "ham"),
        ("add header", "spam"),
        ("reject", "spam"),
        ("soft reject", "spam"),
        ("discard", "spam"),
        ("greylist", "suspect"),
        ("rewrite subject", "suspect"),
    ],
)
def test_rspamd_mapping_and_exact_request_bytes(
    monkeypatch: pytest.MonkeyPatch, action: str, expected: str
) -> None:
    seen: dict[str, object] = {}

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return json.dumps({"action": action, "score": 3.5}).encode()

    def fake_urlopen(request: object, *, timeout: float) -> _Response:
        seen["content_type"] = request.get_header("Content-type")  # type: ignore[attr-defined]
        seen["body"] = request.data  # type: ignore[attr-defined]
        return _Response()

    monkeypatch.setattr("mailarchive.classification.urllib.request.urlopen", fake_urlopen)
    raw = b"From: exact\r\n\r\n\x00payload"
    result = RspamdAdapter().classify(raw)
    assert result.classification == expected and seen == {
        "content_type": "message/rfc822",
        "body": raw,
    }


@pytest.mark.parametrize("endpoint", ["https://example.test/checkv2", "http://10.0.0.4/checkv2"])
def test_rspamd_rejects_non_loopback_endpoint(endpoint: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        RspamdAdapter(endpoint)
