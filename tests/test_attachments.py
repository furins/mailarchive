# ruff: noqa: E501

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

import mailarchive.attachments as attachment_module
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


@pytest.mark.parametrize("payload", ("YWJ@", "YQ=", "Y"))
def test_malformed_base64_fails_closed_without_partial_relationships(
    config_file: Path, payload: str
) -> None:
    raw = _message(
        "--x\r\nContent-Disposition: attachment; filename=broken.bin\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
        f"{payload}\r\n"
    )
    config, message = _archive(config_file, raw)
    assert reconcile_attachments(config) == []
    assert message.local_path.read_bytes() == raw
    with connect(config.database.path) as db:
        assert (
            db.execute("SELECT last_error_kind FROM attachment_extractions").fetchone()[0]
            == "attachment-decode"
        )
        assert db.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 0
        details = db.execute(
            "SELECT details_json FROM audit_events WHERE event_type='attachments.extraction.failed'"
        ).fetchone()[0]
        assert details == '{"error_kind": "attachment-decode"}'


def test_canonical_io_failure_is_message_local(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, first = _archive(
        config_file, _message("--x\r\nContent-Disposition: attachment\r\n\r\na\r\n")
    )
    _, broken = _archive(
        config_file, _message("--x\r\nContent-Disposition: attachment\r\n\r\nb\r\n")
    )
    _, third = _archive(
        config_file, _message("--x\r\nContent-Disposition: attachment\r\n\r\nc\r\n")
    )
    original = attachment_module.sha256_file

    def hash_or_fail(path: Path) -> str:
        if path == broken.local_path:
            raise OSError("fixture")
        return original(path)

    monkeypatch.setattr(attachment_module, "sha256_file", hash_or_fail)
    assert reconcile_attachments(config) == sorted([first.id, third.id])
    with connect(config.database.path) as db:
        assert (
            db.execute(
                "SELECT last_error_kind FROM attachment_extractions WHERE canonical_message_id=?",
                (broken.id,),
            ).fetchone()[0]
            == "canonical-io"
        )


def test_retry_failed_and_force_semantics(config_file: Path) -> None:
    config, message = _archive(
        config_file, _message("--x\r\nContent-Disposition: attachment\r\n\r\nx\r\n")
    )
    message.local_path.unlink()
    assert reconcile_attachments(config) == []
    with connect(config.database.path) as db:
        first_update = db.execute("SELECT updated_at FROM attachment_extractions").fetchone()[0]
    assert reconcile_attachments(config, retry_failed=False) == []
    with connect(config.database.path) as db:
        assert (
            db.execute("SELECT updated_at FROM attachment_extractions").fetchone()[0]
            == first_update
        )
    message.local_path.parent.mkdir(parents=True, exist_ok=True)
    message.local_path.write_bytes(_message("--x\r\nContent-Disposition: attachment\r\n\r\nx\r\n"))
    assert reconcile_attachments(config, retry_failed=True) == [message.id]
    assert reconcile_attachments(config, force=True) == [message.id]


def test_relative_archive_root_uses_absolute_attachment_paths(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = load_config(config_file)
    monkeypatch.chdir(tmp_path)
    relative_config = replace(config, archive=replace(config.archive, root=Path("relative-root")))
    result = ingest_bytes(
        relative_config,
        _message("--x\r\nContent-Disposition: attachment\r\n\r\nrelative\r\n"),
        "test",
    )
    message = apply_classification(
        relative_config, result.canonical_message, ClassificationResult("ham", None, "test")
    )
    assert reconcile_attachments(relative_config) == [message.id]
    with connect(relative_config.database.path) as db:
        path = Path(db.execute("SELECT content_path FROM attachments").fetchone()[0])
    assert path.is_absolute()
    assert path.is_relative_to((tmp_path / "relative-root").resolve())
