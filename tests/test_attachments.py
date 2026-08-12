# ruff: noqa: E501

from __future__ import annotations

import hashlib
from pathlib import Path

from mailarchive.attachments import attachment_blob_path, reconcile_attachments
from mailarchive.classification import ClassificationResult, apply_classification
from mailarchive.config import load_config
from mailarchive.db import connect
from mailarchive.ingest import ingest_bytes


def _message(parts: str) -> bytes:
    return (
        "From: a@example.test\r\nTo: b@example.test\r\nSubject: fixture\r\n"
        "MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n"
        "--x\r\nContent-Type: text/plain\r\n\r\nbody\r\n" + parts + "\r\n--x--\r\n"
    ).encode()


def _archive(config_file: Path, raw: bytes):
    config = load_config(config_file)
    result = ingest_bytes(config, raw, "test")
    return config, apply_classification(
        config, result.canonical_message, ClassificationResult("ham", None, "test")
    )


def test_decoded_content_storage_is_global_and_canonical_is_unchanged(config_file: Path) -> None:
    encoded = "--x\r\nContent-Type: application/octet-stream; name=../../unsafe.bin\r\nContent-Disposition: attachment; filename=../../unsafe.bin\r\nContent-Transfer-Encoding: base64\r\n\r\nYQBi\r\n"
    raw = _message(encoded)
    config, message = _archive(config_file, raw)
    assert reconcile_attachments(config) == [message.id]
    digest = hashlib.sha256(b"a\x00b").hexdigest()
    assert attachment_blob_path(config, digest).read_bytes() == b"a\x00b"
    assert message.local_path.read_bytes() == raw
    with connect(config.database.path) as db:
        row = db.execute(
            "SELECT filename_original,declared_mime_type FROM message_attachments"
        ).fetchone()
        assert tuple(row) == ("../../unsafe.bin", "application/octet-stream")
        assert (
            db.execute("SELECT attachment_count,status FROM attachment_extractions").fetchone()[0]
            == 1
        )


def test_zero_attachment_pending_and_lifecycle_reuse(config_file: Path) -> None:
    config, message = _archive(config_file, b"From: a@example.test\r\n\r\nbody\r\n")
    assert reconcile_attachments(config) == [message.id]
    assert reconcile_attachments(config) == []
    with connect(config.database.path) as db:
        assert tuple(
            db.execute("SELECT status,attachment_count FROM attachment_extractions").fetchone()
        ) == ("success", 0)
    apply_classification(config, message, ClassificationResult("spam", None, "test"), manual=True)
    assert reconcile_attachments(config) == []


def test_same_decoded_bytes_have_two_part_relationships(config_file: Path) -> None:
    part = "--x\r\nContent-Type: text/plain; name=a.txt\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\na=3Db\r\n"
    config, message = _archive(config_file, _message(part + part.rstrip("\r\n")))
    reconcile_attachments(config)
    with connect(config.database.path) as db:
        assert db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT COUNT(*) FROM message_attachments WHERE canonical_message_id=?",
                (message.id,),
            ).fetchone()[0]
            == 2
        )


def test_missing_canonical_fails_closed_without_relationships(config_file: Path) -> None:
    config, message = _archive(
        config_file, _message("--x\r\nContent-Disposition: attachment\r\n\r\nx\r\n")
    )
    message.local_path.unlink()
    assert reconcile_attachments(config) == []
    with connect(config.database.path) as db:
        assert (
            db.execute("SELECT last_error_kind FROM attachment_extractions").fetchone()[0]
            == "canonical-missing"
        )
        assert db.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 0
