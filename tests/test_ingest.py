from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
import yaml

import mailarchive.ingest as ingest_module
from mailarchive.config import load_config
from mailarchive.db import canonical_message_by_account_and_sha256, connect
from mailarchive.ingest import IngestError, ingest_file, verify_canonical_message


def _write_message(path: Path, headers: bytes, body: bytes = b"body\r\n") -> bytes:
    raw_bytes = headers + b"\r\n" + body
    path.write_bytes(raw_bytes)
    return raw_bytes


def _message_headers(
    message_id: str | None = "<message@example.test>", date: str | None = None
) -> bytes:
    lines = [b"From: sender@example.test", b"To: receiver@example.test", b"Subject: fixture"]
    if message_id is not None:
        lines.append(f"Message-ID: {message_id}".encode())
    if date is not None:
        lines.append(f"Date: {date}".encode())
    return b"\r\n".join(lines)


def test_ingest_preserves_exact_bytes_and_hash(config_file: Path, tmp_path: Path) -> None:
    source = tmp_path / "message.eml"
    raw_bytes = _write_message(
        source,
        _message_headers(date="Tue, 1 Jan 2019 10:00:00 +0100"),
        b"body\r\n\x00unaltered\n",
    )
    result = ingest_file(load_config(config_file), source, "test")
    assert result.created is True
    assert result.canonical_message.local_path.read_bytes() == raw_bytes
    assert source.read_bytes() == raw_bytes
    assert result.canonical_message.sha256 == hashlib.sha256(raw_bytes).hexdigest()
    assert result.canonical_message.local_path.parent.name == "cur"
    assert result.canonical_message.local_path.parent.parent.joinpath("new").is_dir()
    assert result.canonical_message.local_path.parent.parent.joinpath("tmp").is_dir()
    assert result.canonical_message.message_date == "2019-01-01T09:00:00+00:00"


def test_identical_bytes_are_idempotent_across_source_names(
    config_file: Path, tmp_path: Path
) -> None:
    first_source = tmp_path / "first.eml"
    raw_bytes = _write_message(first_source, _message_headers())
    second_source = tmp_path / "second.eml"
    second_source.write_bytes(raw_bytes)
    config = load_config(config_file)
    first = ingest_file(config, first_source, "test")
    second = ingest_file(config, second_source, "test")
    assert second.created is False
    assert second.canonical_message.id == first.canonical_message.id
    with connect(config.database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM canonical_messages").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 3


def test_identical_bytes_are_preserved_per_account(config_file: Path, tmp_path: Path) -> None:
    values = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    values["accounts"]["other"] = {
        "kind": "imap",
        "enabled": True,
        "remote_retention_days": 365,
        "required_verified_backups": 2,
        "config_ref": "env:OTHER_ACCOUNT",
    }
    config_file.write_text(yaml.safe_dump(values), encoding="utf-8")
    source = tmp_path / "message.eml"
    raw_bytes = _write_message(source, _message_headers())
    config = load_config(config_file)
    first = ingest_file(config, source, "test")
    second = ingest_file(config, source, "other")
    expected_mail_root = (config.archive.root / "mail").resolve()
    assert first.created is True
    assert second.created is True
    assert first.canonical_message.id != second.canonical_message.id
    assert first.canonical_message.sha256 == second.canonical_message.sha256
    assert first.canonical_message.local_path.read_bytes() == raw_bytes
    assert second.canonical_message.local_path.read_bytes() == raw_bytes
    assert first.canonical_message.local_path.parent.parent.name == "test"
    assert second.canonical_message.local_path.parent.parent.name == "other"
    assert first.canonical_message.local_path.is_relative_to(expected_mail_root)
    assert second.canonical_message.local_path.is_relative_to(expected_mail_root)
    with connect(config.database.path) as connection:
        account_rows = connection.execute("SELECT id, name FROM accounts").fetchall()
        account_ids = {str(row["name"]): int(row["id"]) for row in account_rows}
        canonical_rows = connection.execute(
            "SELECT account_id, sha256 FROM canonical_messages ORDER BY account_id"
        ).fetchall()
    assert {int(row["account_id"]) for row in canonical_rows} == {
        account_ids["test"],
        account_ids["other"],
    }
    assert {str(row["sha256"]) for row in canonical_rows} == {first.canonical_message.sha256}


def test_same_message_id_with_different_bytes_stays_distinct(
    config_file: Path, tmp_path: Path
) -> None:
    first_source = tmp_path / "first.eml"
    second_source = tmp_path / "second.eml"
    _write_message(first_source, _message_headers("<same@example.test>"), b"first\r\n")
    _write_message(second_source, _message_headers("<same@example.test>"), b"second\r\n")
    config = load_config(config_file)
    first = ingest_file(config, first_source, "test")
    second = ingest_file(config, second_source, "test")
    assert first.canonical_message.id != second.canonical_message.id
    with connect(config.database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM canonical_messages").fetchone()[0] == 2


def test_identical_bytes_reuse_first_metadata(config_file: Path, tmp_path: Path) -> None:
    source = tmp_path / "first.eml"
    raw_bytes = _write_message(source, _message_headers("<first@example.test>"))
    copied_source = tmp_path / "second.eml"
    copied_source.write_bytes(raw_bytes)
    config = load_config(config_file)
    first = ingest_file(config, source, "test")
    second = ingest_file(config, copied_source, "test")
    assert second.created is False
    assert second.canonical_message.message_id_header == "<first@example.test>"
    assert first.canonical_message.id == second.canonical_message.id


@pytest.mark.parametrize(
    ("headers", "expected_message_id", "expected_date"),
    [
        (_message_headers(message_id=None), None, None),
        (_message_headers(date="not a date"), "<message@example.test>", None),
        (
            b"Message-ID: <broken@example.test>\r\nContent-Type: multipart/mixed; boundary=broken",
            "<broken@example.test>",
            None,
        ),
    ],
)
def test_incomplete_or_malformed_metadata_is_still_preserved(
    config_file: Path,
    tmp_path: Path,
    headers: bytes,
    expected_message_id: str | None,
    expected_date: str | None,
) -> None:
    source = tmp_path / "message.eml"
    raw_bytes = _write_message(source, headers, b"unclosed MIME body\r\n")
    result = ingest_file(load_config(config_file), source, "test")
    assert result.canonical_message.local_path.read_bytes() == raw_bytes
    assert result.canonical_message.message_id_header == expected_message_id
    assert result.canonical_message.message_date == expected_date


def test_invalid_source_and_unknown_account_are_rejected(config_file: Path, tmp_path: Path) -> None:
    config = load_config(config_file)
    with pytest.raises(IngestError, match="existing .eml"):
        ingest_file(config, tmp_path / "absent.eml", "test")
    source = tmp_path / "message.eml"
    _write_message(source, _message_headers())
    with pytest.raises(IngestError, match="unknown account"):
        ingest_file(config, source, "absent")


def test_atomic_file_write_leaves_no_canonical_file_after_link_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "mail" / "test" / "cur" / "digest.eml"
    raw_bytes = b"bytes\r\n"
    digest = hashlib.sha256(raw_bytes).hexdigest()

    def fail_link(source: Path, target: Path) -> None:
        raise OSError("simulated link failure")

    monkeypatch.setattr(ingest_module.os, "link", fail_link)
    with pytest.raises(OSError, match="simulated"):
        ingest_module.create_canonical_file(destination, raw_bytes, digest)
    assert not destination.exists()
    assert list(destination.parent.parent.joinpath("tmp").iterdir()) == []


def test_canonical_write_never_overwrites_an_existing_file(tmp_path: Path) -> None:
    destination = tmp_path / "mail" / "test" / "cur" / "digest.eml"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"existing")
    raw_bytes = b"new bytes"
    digest = hashlib.sha256(raw_bytes).hexdigest()
    with pytest.raises(IngestError, match="does not match"):
        ingest_module.create_canonical_file(destination, raw_bytes, digest)
    assert destination.read_bytes() == b"existing"


def test_database_failure_file_is_retryable(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "message.eml"
    raw_bytes = _write_message(source, _message_headers())
    config = load_config(config_file)
    real_register = ingest_module.register_canonical_message

    def fail_registration(*args: object, **kwargs: object) -> tuple[object, bool]:
        raise sqlite3.OperationalError("simulated database failure")

    monkeypatch.setattr(ingest_module, "register_canonical_message", fail_registration)
    with pytest.raises(sqlite3.OperationalError, match="simulated"):
        ingest_file(config, source, "test")
    filename = f"{hashlib.sha256(raw_bytes).hexdigest()}.eml"
    canonical_path = config.archive.root / "staging" / "test" / "cur" / filename
    assert canonical_path.read_bytes() == raw_bytes
    monkeypatch.setattr(ingest_module, "register_canonical_message", real_register)
    result = ingest_file(config, source, "test")
    assert result.created is True
    with connect(config.database.path) as connection:
        assert (
            canonical_message_by_account_and_sha256(
                connection, result.canonical_message.account_id, result.canonical_message.sha256
            )
            is not None
        )


def test_integrity_verification_detects_modification(config_file: Path, tmp_path: Path) -> None:
    source = tmp_path / "message.eml"
    _write_message(source, _message_headers())
    result = ingest_file(load_config(config_file), source, "test")
    assert verify_canonical_message(result.canonical_message) is True
    result.canonical_message.local_path.write_bytes(b"changed")
    assert verify_canonical_message(result.canonical_message) is False


def test_successful_ingest_creates_audit_event(config_file: Path, tmp_path: Path) -> None:
    source = tmp_path / "message.eml"
    _write_message(source, _message_headers())
    config = load_config(config_file)
    result = ingest_file(config, source, "test")
    with connect(config.database.path) as connection:
        event = connection.execute(
            "SELECT event_type, canonical_message_id, details_json FROM audit_events"
        ).fetchone()
    assert event["event_type"] == "ingest.succeeded"
    assert event["canonical_message_id"] == result.canonical_message.id
    assert result.canonical_message.sha256 in event["details_json"]
