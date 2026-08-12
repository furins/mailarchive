"""Safe local RFC822/MIME ingestion into immutable Maildir-compatible storage."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

from mailarchive.db import (
    account_id,
    canonical_message_by_account_and_sha256,
    connect,
    initialize,
    register_canonical_message,
    utc_now,
)
from mailarchive.models import AppConfig, CanonicalMessage


class IngestError(ValueError):
    """Raised when a local message cannot be safely admitted to the archive."""


@dataclass(frozen=True)
class IngestResult:
    canonical_message: CanonicalMessage
    created: bool


def sha256_file(path: Path) -> str:
    """Calculate a SHA-256 digest from the file's exact bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_canonical_message(message: CanonicalMessage) -> bool:
    """Return whether the canonical file still exists and matches its recorded hash."""
    return message.local_path.is_file() and sha256_file(message.local_path) == message.sha256


def _metadata(raw_bytes: bytes) -> tuple[str | None, str | None]:
    """Extract optional metadata without modifying or rejecting the source bytes."""
    try:
        parsed = BytesParser(policy=policy.compat32).parsebytes(raw_bytes)
        message_id = parsed.get("Message-ID")
        date_header = parsed.get("Date")
    except TypeError, ValueError:
        return None, None
    if not date_header:
        return message_id, None
    try:
        message_date = parsedate_to_datetime(date_header)
    except TypeError, ValueError, IndexError, OverflowError:
        return message_id, None
    if message_date.tzinfo is None:
        return message_id, None
    return message_id, message_date.astimezone(UTC).isoformat()


def _maildir_path(
    archive_root: Path, account_name: str, sha256: str, root: str = "staging"
) -> Path:
    mail_root = (archive_root / root).resolve()
    destination = mail_root / account_name / "cur" / f"{sha256}.eml"
    try:
        destination.resolve().relative_to(mail_root)
    except ValueError as error:
        raise IngestError("canonical path escapes the archive mail root") from error
    return destination


def create_canonical_file(destination: Path, raw_bytes: bytes, sha256: str) -> None:
    """Atomically admit exact bytes, or validate an already-present retry artifact."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_directory = destination.parent.parent / "tmp"
    temporary_directory.mkdir(parents=True, exist_ok=True)
    (destination.parent.parent / "new").mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file() or sha256_file(destination) != sha256:
            raise IngestError(
                f"canonical path exists but does not match expected bytes: {destination}"
            )
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{sha256}.", suffix=".tmp", dir=temporary_directory
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(raw_bytes)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        try:
            os.link(temporary_path, destination)
        except FileExistsError:
            if not destination.is_file() or sha256_file(destination) != sha256:
                raise IngestError(
                    f"canonical path exists but does not match expected bytes: {destination}"
                ) from None
        directory_descriptor = os.open(destination.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        temporary_path.unlink()
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def ingest_bytes(
    config: AppConfig, raw_bytes: bytes, account_name: str, *, source_kind: str = "bytes"
) -> IngestResult:
    """Admit exact provider bytes without a temporary RFC822 file or MIME rewrite."""
    account = next((item for item in config.accounts if item.name == account_name), None)
    if account is None:
        raise IngestError(f"unknown account: {account_name}")
    if not account.enabled:
        raise IngestError(f"account is disabled: {account_name}")
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    message_id, message_date = _metadata(raw_bytes)
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as connection:
        local_account_id = account_id(connection, account_name)
        if local_account_id is None:
            raise IngestError(f"account is not active in local state: {account_name}")
        existing = canonical_message_by_account_and_sha256(connection, local_account_id, sha256)
    if existing is not None:
        if not verify_canonical_message(existing):
            raise IngestError("existing canonical record is missing or fails its SHA-256 check")
        stored, created = register_canonical_message(
            config.database.path,
            existing,
            audit_account_id=local_account_id,
            audit_event_type="ingest.succeeded",
            audit_details_json=json.dumps({"sha256": sha256, "source_kind": source_kind}),
        )
        return IngestResult(canonical_message=stored, created=created)
    destination = _maildir_path(config.archive.root, account_name, sha256)
    create_canonical_file(destination, raw_bytes, sha256)
    now = utc_now()
    candidate = CanonicalMessage(
        id=f"{local_account_id}:{sha256}",
        account_id=local_account_id,
        sha256=sha256,
        local_path=destination,
        size_bytes=len(raw_bytes),
        message_id_header=message_id,
        message_date=message_date,
        downloaded_at=now,
        archived_at=None,
        storage_state="pending",
        quarantined_at=None,
        integrity_status="verified",
        integrity_verified_at=now,
        created_at=now,
    )
    stored, created = register_canonical_message(
        config.database.path,
        candidate,
        audit_account_id=local_account_id,
        audit_event_type="ingest.succeeded",
        audit_details_json=json.dumps(
            {"sha256": sha256, "source_kind": source_kind, "message_id": message_id}
        ),
    )
    if not verify_canonical_message(stored):
        raise IngestError("canonical file is missing or fails its SHA-256 check after registration")
    return IngestResult(canonical_message=stored, created=created)


def ingest_file(config: AppConfig, source_path: Path, account_name: str) -> IngestResult:
    """Import a local .eml as explicit operator HAM archive admission.

    Provider acquisition calls :func:`ingest_bytes` directly and stays pending
    until classifier policy runs. This retains the M1 local-import workflow.
    """
    if source_path.suffix.lower() != ".eml" or not source_path.is_file():
        raise IngestError("source must be an existing .eml file")
    result = ingest_bytes(config, source_path.read_bytes(), account_name, source_kind="file")
    if result.canonical_message.storage_state == "pending":
        from mailarchive.classification import ClassificationResult, apply_classification

        message = apply_classification(
            config,
            result.canonical_message,
            ClassificationResult("ham", None, "operator-local-import", "local-import"),
            manual=True,
        )
        return IngestResult(canonical_message=message, created=result.created)
    return result
