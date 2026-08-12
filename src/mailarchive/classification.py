"""Fail-closed local spam classification and immutable storage transitions."""
# ruff: noqa: E501

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from mailarchive.db import connect, insert_audit_event, utc_now
from mailarchive.ingest import IngestError, sha256_file
from mailarchive.models import AppConfig, CanonicalMessage


@dataclass(frozen=True)
class ClassificationResult:
    classification: str
    score: float | None
    reason: str
    classifier: str = "rspamd"
    classifier_version: str | None = None


class RspamdAdapter:
    """Observation-only loopback Rspamd /checkv2 client."""

    def __init__(
        self, endpoint: str = "http://127.0.0.1:11333/checkv2", timeout: float = 10
    ) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Rspamd endpoint must use loopback HTTP")
        self.endpoint, self.timeout = endpoint, timeout

    def classify(self, raw_bytes: bytes) -> ClassificationResult:
        request = urllib.request.Request(
            self.endpoint, raw_bytes, {"Content-Type": "message/rfc822"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 loopback validated
                payload = json.loads(response.read().decode("utf-8"))
        except TimeoutError:
            return ClassificationResult("suspect", None, "classifier-timeout")
        except urllib.error.URLError, OSError:
            return ClassificationResult("suspect", None, "classifier-unavailable")
        except UnicodeDecodeError, json.JSONDecodeError:
            return ClassificationResult("suspect", None, "classifier-malformed-response")
        if not isinstance(payload, dict):
            return ClassificationResult("suspect", None, "classifier-malformed-response")
        response = cast(dict[str, Any], payload)
        if response.get("is_skipped") is True:
            return ClassificationResult("suspect", None, "classifier-skipped")
        action, score = response.get("action"), response.get("score")
        if not isinstance(action, str) or not isinstance(score, (int, float)):
            return ClassificationResult("suspect", None, "classifier-malformed-response")
        if action == "no action":
            verdict = "ham"
        elif action.lower() in {"add header", "reject", "soft reject", "discard"}:
            verdict = "spam"
        elif action.lower() in {"greylist", "rewrite subject"}:
            verdict = "suspect"
        else:
            return ClassificationResult("suspect", float(score), "classifier-unknown-action")
        return ClassificationResult(verdict, float(score), f"rspamd-action:{action}"[:256])


def _target(config: AppConfig, account: str, digest: str, state: str) -> Path:
    root = {"archived": "mail", "quarantined": "quarantine", "pending": "staging"}[state]
    return config.archive.root / root / account / "cur" / f"{digest}.eml"


def _move_exact(source: Path, destination: Path, digest: str) -> None:
    """Idempotent move; an existing destination is accepted only with the expected hash."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    (destination.parent.parent / "new").mkdir(parents=True, exist_ok=True)
    (destination.parent.parent / "tmp").mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file() or sha256_file(destination) != digest:
            raise IngestError("destination exists with a different SHA-256")
        if source.exists() and source != destination:
            if sha256_file(source) != digest:
                raise IngestError("source fails SHA-256 verification")
            source.unlink()
        return
    if not source.is_file() or sha256_file(source) != digest:
        raise IngestError("source canonical bytes are unavailable or corrupt")
    try:
        os.replace(source, destination)
    except OSError as error:
        shutil.copyfile(source, destination)
        if sha256_file(destination) != digest:
            destination.unlink(missing_ok=True)
            raise IngestError("cross-device promotion hash verification failed") from error
        source.unlink()
    descriptor = os.open(destination.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def apply_classification(
    config: AppConfig,
    message: CanonicalMessage,
    result: ClassificationResult,
    *,
    manual: bool = False,
) -> CanonicalMessage:
    """Append a verdict then reconcile immutable bytes and authoritative lifecycle state."""
    if result.classification not in {"ham", "suspect", "spam"}:
        raise ValueError("invalid classification")
    # The account lookup is by SQLite ID; names are never inferred from MIME metadata.
    with connect(config.database.path) as db:
        row = db.execute("SELECT name FROM accounts WHERE id=?", (message.account_id,)).fetchone()
        if row is None:
            raise IngestError("canonical message account no longer exists")
        account = str(row[0])
    # A later automatic observation never displaces an operator's latest override.
    with connect(config.database.path) as db:
        override = (
            None
            if manual
            else db.execute(
                "SELECT classification FROM classifications WHERE canonical_message_id=? AND manual_override=1 ORDER BY id DESC LIMIT 1",
                (message.id,),
            ).fetchone()
        )
    effective = result.classification if override is None else str(override[0])
    next_state = "archived" if effective == "ham" else "quarantined"
    destination = _target(config, account, message.sha256, next_state)
    _move_exact(message.local_path, destination, message.sha256)
    now = utc_now()
    with connect(config.database.path) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(
                "INSERT INTO classifications(canonical_message_id,classification,score,reason,classifier,classifier_version,manual_override,classified_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    message.id,
                    result.classification,
                    result.score,
                    result.reason[:256],
                    result.classifier[:128],
                    result.classifier_version,
                    int(manual),
                    now,
                ),
            )
            archived_at = (
                now
                if next_state == "archived" and message.archived_at is None
                else message.archived_at
            )
            quarantined_at = now if next_state == "quarantined" else message.quarantined_at
            db.execute(
                "UPDATE canonical_messages SET local_path=?,storage_state=?,archived_at=?,quarantined_at=? WHERE id=?",
                (str(destination), next_state, archived_at, quarantined_at, message.id),
            )
            insert_audit_event(
                db,
                actor="mailarchive.classification",
                event_type="classification.manual_override"
                if manual
                else "classification.succeeded",
                result="success",
                account_id=message.account_id,
                canonical_message_id=message.id,
                details_json=json.dumps(
                    {
                        "classification": result.classification,
                        "reason": result.reason[:256],
                        "classifier": result.classifier[:128],
                    }
                ),
            )
            insert_audit_event(
                db,
                actor="mailarchive.classification",
                event_type="quarantine.entered"
                if next_state == "quarantined"
                else "quarantine.restored",
                result="success",
                account_id=message.account_id,
                canonical_message_id=message.id,
                details_json="{}",
            )
        except BaseException:
            db.rollback()
            raise
        else:
            db.commit()
    stored = replace(
        message,
        local_path=destination,
        storage_state=next_state,  # type: ignore[arg-type]
        archived_at=archived_at,
        quarantined_at=quarantined_at,
    )
    # Derived indexing never rolls back preserved bytes or authoritative SQLite.
    try:
        from mailarchive.notmuch import NotmuchAdapter

        NotmuchAdapter(config).refresh()
    except Exception:
        pass
    return stored


def effective_classification(db: sqlite3.Connection, canonical_id: str) -> sqlite3.Row | None:
    """Latest manual override wins; otherwise latest automatic result."""
    return db.execute(
        "SELECT * FROM classifications WHERE canonical_message_id=? ORDER BY manual_override DESC, id DESC LIMIT 1",
        (canonical_id,),
    ).fetchone()


def classify_pending(
    config: AppConfig, message: CanonicalMessage, adapter: RspamdAdapter | None = None
) -> CanonicalMessage:
    """Classify registered staging bytes; failure is deliberately SUSPECT quarantine."""
    if message.storage_state != "pending":
        return message
    raw_bytes = message.local_path.read_bytes()
    verdict = (adapter or RspamdAdapter()).classify(raw_bytes)
    return apply_classification(config, message, verdict)
