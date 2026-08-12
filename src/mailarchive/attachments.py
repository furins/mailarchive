"""Local, immutable MIME attachment extraction and authoritative catalog lookup."""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from email import policy
from email.errors import (
    InvalidBase64CharactersDefect,
    InvalidBase64LengthDefect,
    InvalidBase64PaddingDefect,
)
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import Literal

from mailarchive.db import canonical_message_by_id, connect, insert_audit_event, utc_now
from mailarchive.ingest import sha256_file
from mailarchive.models import AppConfig, CanonicalMessage

ExtractionErrorKind = Literal[
    "canonical-missing",
    "canonical-sha-mismatch",
    "canonical-io",
    "mime-parse",
    "attachment-decode",
    "attachment-storage",
    "database",
]


class AttachmentError(RuntimeError):
    """A local M8 extraction/storage operation could not safely complete."""


@dataclass(frozen=True)
class ExtractedPart:
    part_index: int
    payload: bytes
    sha256: str
    filename_original: str | None
    content_disposition: str | None
    declared_mime_type: str


@dataclass(frozen=True)
class AttachmentSearchResult:
    canonical_id: str
    account: str
    attachment_sha256: str
    size_bytes: int
    part_index: int
    filename_original: str | None
    declared_mime_type: str | None
    content_disposition: str | None
    content_path: Path
    canonical_storage_state: str

    def as_dict(self) -> dict[str, object]:
        return {
            "canonical_id": self.canonical_id,
            "account": self.account,
            "attachment_sha256": self.attachment_sha256,
            "size_bytes": self.size_bytes,
            "part_index": self.part_index,
            "filename_original": self.filename_original,
            "declared_mime_type": self.declared_mime_type,
            "content_disposition": self.content_disposition,
            "content_path": str(self.content_path),
            "canonical_storage_state": self.canonical_storage_state,
        }


def attachment_blob_path(config: AppConfig, digest: str) -> Path:
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise AttachmentError("attachment digest is invalid")
    return config.archive.root.resolve() / "attachments" / "sha256" / digest[:2] / digest


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def install_attachment_blob(config: AppConfig, payload: bytes, digest: str) -> Path:
    """Durably install immutable decoded bytes, never replacing a conflicting object."""
    if hashlib.sha256(payload).hexdigest() != digest:
        raise AttachmentError("attachment payload does not match its SHA-256")
    destination = attachment_blob_path(config, digest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file() or sha256_file(destination) != digest:
            raise AttachmentError("attachment destination exists with a different SHA-256")
        return destination
    descriptor, name = tempfile.mkstemp(prefix=f"{digest}.", suffix=".tmp", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        if sha256_file(temporary) != digest:
            raise AttachmentError("temporary attachment bytes fail SHA-256 verification")
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if not destination.is_file() or sha256_file(destination) != digest:
                raise AttachmentError(
                    "attachment destination exists with a different SHA-256"
                ) from None
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _is_attachment(part: Message) -> bool:
    disposition = (part.get_content_disposition() or "").lower()
    return disposition == "attachment" or part.get_filename() is not None


def parse_attachments(raw_bytes: bytes) -> list[ExtractedPart]:
    """Parse exact source bytes; attachments are attachment-disposition leaves or named leaves."""
    try:
        parsed = BytesParser(policy=policy.compat32).parsebytes(raw_bytes)
    except (TypeError, ValueError) as error:
        raise AttachmentError("MIME parse failed") from error
    results: list[ExtractedPart] = []
    for part in parsed.walk():
        if part.is_multipart() or not _is_attachment(part):
            continue
        try:
            payload = part.get_payload(decode=True)
        except (TypeError, ValueError) as error:
            raise AttachmentError("MIME attachment decoding failed") from error
        if not isinstance(payload, bytes):
            raise AttachmentError("MIME attachment decoding produced no bytes")
        if part.get("Content-Transfer-Encoding", "").lower() == "base64" and any(
            isinstance(
                defect,
                (
                    InvalidBase64CharactersDefect,
                    InvalidBase64PaddingDefect,
                    InvalidBase64LengthDefect,
                ),
            )
            for defect in part.defects
        ):
            raise AttachmentError("MIME attachment base64 decoding defect")
        results.append(
            ExtractedPart(
                part_index=len(results),
                payload=payload,
                sha256=hashlib.sha256(payload).hexdigest(),
                filename_original=part.get_filename(),
                content_disposition=part.get_content_disposition(),
                declared_mime_type=part.get_content_type(),
            )
        )
    return results


def _record_failure(
    config: AppConfig, message: CanonicalMessage, kind: ExtractionErrorKind
) -> None:
    now = utc_now()
    with connect(config.database.path) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                """INSERT INTO attachment_extractions(canonical_message_id,source_sha256,status,
                attachment_count,extracted_at,last_error_kind,updated_at) VALUES (?,?, 'failed',0,NULL,?,?)
                ON CONFLICT(canonical_message_id) DO UPDATE SET source_sha256=excluded.source_sha256,
                status='failed',attachment_count=0,extracted_at=NULL,last_error_kind=excluded.last_error_kind,
                updated_at=excluded.updated_at""",
                (message.id, message.sha256, kind, now),
            )
            insert_audit_event(
                db,
                actor="mailarchive.attachments",
                event_type="attachments.extraction.failed",
                result="failed",
                account_id=message.account_id,
                canonical_message_id=message.id,
                details_json=json.dumps({"error_kind": kind}),
            )
        except BaseException:
            db.rollback()
            raise
        else:
            db.commit()


def _extract_one(
    config: AppConfig, message: CanonicalMessage, *, retry_failed: bool, force: bool
) -> bool:
    if message.storage_state not in {"archived", "quarantined"}:
        return False
    with connect(config.database.path) as db:
        prior = db.execute(
            "SELECT status,source_sha256 FROM attachment_extractions WHERE canonical_message_id=?",
            (message.id,),
        ).fetchone()
    if (
        not force
        and prior is not None
        and prior["status"] == "success"
        and prior["source_sha256"] == message.sha256
    ):
        return False
    if not force and prior is not None and prior["status"] == "failed" and not retry_failed:
        return False
    if not message.local_path.is_file():
        _record_failure(config, message, "canonical-missing")
        return False
    try:
        canonical_sha256 = sha256_file(message.local_path)
        raw_bytes = message.local_path.read_bytes()
    except OSError:
        _record_failure(config, message, "canonical-io")
        return False
    if canonical_sha256 != message.sha256:
        _record_failure(config, message, "canonical-sha-mismatch")
        return False
    try:
        parts = parse_attachments(raw_bytes)
    except AttachmentError as error:
        _record_failure(
            config, message, "attachment-decode" if "decod" in str(error) else "mime-parse"
        )
        return False
    try:
        for part in parts:
            install_attachment_blob(config, part.payload, part.sha256)
    except AttachmentError, OSError:
        _record_failure(config, message, "attachment-storage")
        return False
    now = utc_now()
    try:
        with connect(config.database.path) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "DELETE FROM message_attachments WHERE canonical_message_id=?", (message.id,)
                )
                for part in parts:
                    path = attachment_blob_path(config, part.sha256)
                    db.execute(
                        """INSERT INTO attachments(id,sha256,size_bytes,content_path,first_seen_at)
                        VALUES(?,?,?,?,?) ON CONFLICT(sha256) DO NOTHING""",
                        (part.sha256, part.sha256, len(part.payload), str(path), now),
                    )
                    db.execute(
                        """INSERT INTO message_attachments(canonical_message_id,attachment_id,part_index,
                        filename_original,content_disposition,declared_mime_type) VALUES(?,?,?,?,?,?)""",
                        (
                            message.id,
                            part.sha256,
                            part.part_index,
                            part.filename_original,
                            part.content_disposition,
                            part.declared_mime_type,
                        ),
                    )
                db.execute(
                    """INSERT INTO attachment_extractions(canonical_message_id,source_sha256,status,
                    attachment_count,extracted_at,last_error_kind,updated_at) VALUES(?,?, 'success',?,?,NULL,?)
                    ON CONFLICT(canonical_message_id) DO UPDATE SET source_sha256=excluded.source_sha256,
                    status='success',attachment_count=excluded.attachment_count,extracted_at=excluded.extracted_at,
                    last_error_kind=NULL,updated_at=excluded.updated_at""",
                    (message.id, message.sha256, len(parts), now, now),
                )
                insert_audit_event(
                    db,
                    actor="mailarchive.attachments",
                    event_type="attachments.extraction.succeeded",
                    result="success",
                    account_id=message.account_id,
                    canonical_message_id=message.id,
                    details_json=json.dumps({"attachment_count": len(parts)}),
                )
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()
    except sqlite3.DatabaseError:
        _record_failure(config, message, "database")
        return False
    return True


def reconcile_attachments(
    config: AppConfig,
    canonical_id: str | None = None,
    *,
    retry_failed: bool = True,
    force: bool = False,
) -> list[str]:
    """Reconcile finalized local messages only; provider acquisition is never involved."""
    with connect(config.database.path) as db:
        if canonical_id is not None:
            message = canonical_message_by_id(db, canonical_id)
            messages = [] if message is None else [message]
        else:
            rows = db.execute(
                "SELECT id FROM canonical_messages WHERE storage_state IN ('archived','quarantined') ORDER BY id"
            ).fetchall()
            messages = [canonical_message_by_id(db, str(row["id"])) for row in rows]
            messages = [message for message in messages if message is not None]
    return [
        message.id
        for message in messages
        if _extract_one(config, message, retry_failed=retry_failed, force=force)
    ]


def search_attachment_relationships(
    config: AppConfig, digests: list[str], scope: str = "archived"
) -> list[AttachmentSearchResult]:
    if scope not in {"archived", "quarantine", "all"}:
        raise ValueError("search scope must be archived, quarantine, or all")
    if not digests:
        return []
    states = (
        ("archived",)
        if scope == "archived"
        else ("quarantined",)
        if scope == "quarantine"
        else ("archived", "quarantined")
    )
    with connect(config.database.path) as db:
        marks = ",".join("?" for _ in digests)
        state_marks = ",".join("?" for _ in states)
        rows = db.execute(
            f"""SELECT c.id canonical_id,a.name account,at.sha256,at.size_bytes,ma.part_index,
            ma.filename_original,ma.declared_mime_type,ma.content_disposition,at.content_path,c.storage_state
            FROM attachments at JOIN message_attachments ma ON ma.attachment_id=at.id
            JOIN canonical_messages c ON c.id=ma.canonical_message_id JOIN accounts a ON a.id=c.account_id
            WHERE at.sha256 IN ({marks}) AND c.storage_state IN ({state_marks})
            ORDER BY c.id,ma.part_index,at.sha256""",
            (*digests, *states),
        ).fetchall()
    return [
        AttachmentSearchResult(
            str(row["canonical_id"]),
            str(row["account"]),
            str(row["sha256"]),
            int(row["size_bytes"]),
            int(row["part_index"]),
            row["filename_original"],
            row["declared_mime_type"],
            row["content_disposition"],
            Path(str(row["content_path"])),
            str(row["storage_state"]),
        )
        for row in rows
    ]
