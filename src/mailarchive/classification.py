"""Fail-closed local spam classification and crash-safe immutable transitions."""
# ruff: noqa: E501

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlparse

from mailarchive.db import canonical_message_by_id, connect, insert_audit_event, utc_now
from mailarchive.ingest import IngestError, sha256_file
from mailarchive.models import AppConfig, CanonicalMessage

Classification = Literal["ham", "suspect", "spam"]
_FAILURE_REASONS = frozenset(
    {
        "classifier-timeout",
        "classifier-unavailable",
        "classifier-malformed-response",
        "classifier-unknown-action",
        "classifier-skipped",
    }
)


@dataclass(frozen=True)
class ClassificationResult:
    classification: Classification
    score: float | None
    reason: str
    classifier: str = "rspamd"
    classifier_version: str | None = None


class ClassifierAdapter(Protocol):
    def classify(self, raw_bytes: bytes) -> ClassificationResult: ...


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
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 validated loopback
                payload = json.loads(response.read().decode("utf-8"))
        except TimeoutError:
            return ClassificationResult("suspect", None, "classifier-timeout")
        except urllib.error.URLError, OSError:
            return ClassificationResult("suspect", None, "classifier-unavailable")
        except UnicodeDecodeError, json.JSONDecodeError:
            return ClassificationResult("suspect", None, "classifier-malformed-response")
        if not isinstance(payload, dict):
            return ClassificationResult("suspect", None, "classifier-malformed-response")
        data = cast(dict[str, Any], payload)
        if data.get("is_skipped") is True:
            return ClassificationResult("suspect", None, "classifier-skipped")
        action, score = data.get("action"), data.get("score")
        if not isinstance(action, str) or not isinstance(score, (int, float)):
            return ClassificationResult("suspect", None, "classifier-malformed-response")
        if action == "no action":
            verdict: Classification = "ham"
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


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _move_exact(source: Path, destination: Path, digest: str) -> None:
    """Install without overwrite, then remove source only after durable destination install."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    for name in ("new", "tmp"):
        (destination.parent.parent / name).mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file() or sha256_file(destination) != digest:
            raise IngestError("destination exists with a different SHA-256")
    else:
        if not source.is_file() or sha256_file(source) != digest:
            raise IngestError("source canonical bytes are unavailable or corrupt")
        try:
            os.link(source, destination)
        except FileExistsError:
            if not destination.is_file() or sha256_file(destination) != digest:
                raise IngestError("destination exists with a different SHA-256") from None
        except OSError:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f"{digest}.", suffix=".tmp", dir=destination.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
                    shutil.copyfileobj(input_file, output)
                    output.flush()
                    os.fsync(output.fileno())
                if sha256_file(temporary) != digest:
                    raise IngestError("temporary transition bytes fail SHA-256 verification")
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    if not destination.is_file() or sha256_file(destination) != digest:
                        raise IngestError("destination exists with a different SHA-256") from None
            finally:
                temporary.unlink(missing_ok=True)
    _fsync_directory(destination.parent)
    if source != destination and source.exists():
        if sha256_file(source) != digest:
            raise IngestError("source canonical bytes are corrupt")
        source.unlink()
        _fsync_directory(source.parent)


def _effective_auto_result(
    db: sqlite3.Connection, message: CanonicalMessage, result: ClassificationResult
) -> Classification:
    override = db.execute(
        "SELECT classification FROM classifications WHERE canonical_message_id=? AND manual_override=1 ORDER BY id DESC LIMIT 1",
        (message.id,),
    ).fetchone()
    return result.classification if override is None else cast(Classification, str(override[0]))


def apply_classification(
    config: AppConfig,
    message: CanonicalMessage,
    result: ClassificationResult,
    *,
    manual: bool = False,
) -> CanonicalMessage:
    """Append a verdict and atomically record the corresponding completed local transition."""
    with connect(config.database.path) as db:
        account_row = db.execute(
            "SELECT name FROM accounts WHERE id=?", (message.account_id,)
        ).fetchone()
        if account_row is None:
            raise IngestError("canonical message account no longer exists")
        next_classification = (
            result.classification if manual else _effective_auto_result(db, message, result)
        )
    next_state: Literal["archived", "quarantined"] = (
        "archived" if next_classification == "ham" else "quarantined"
    )
    destination = _target(config, str(account_row[0]), message.sha256, next_state)
    _move_exact(message.local_path, destination, message.sha256)
    now = utc_now()
    archived_at = (
        now if next_state == "archived" and message.archived_at is None else message.archived_at
    )
    quarantined_at = now if next_state == "quarantined" else message.quarantined_at
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
            db.execute(
                "UPDATE canonical_messages SET local_path=?,storage_state=?,archived_at=?,quarantined_at=? WHERE id=?",
                (str(destination), next_state, archived_at, quarantined_at, message.id),
            )
            event = "classification.manual_override" if manual else "classification.succeeded"
            insert_audit_event(
                db,
                actor="mailarchive.classification",
                event_type=event,
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
            if not manual and result.reason in _FAILURE_REASONS:
                insert_audit_event(
                    db,
                    actor="mailarchive.classification",
                    event_type="classification.failed",
                    result="fail-safe",
                    account_id=message.account_id,
                    canonical_message_id=message.id,
                    details_json=json.dumps(
                        {"reason": result.reason, "classifier": result.classifier[:128]}
                    ),
                )
            transition = (message.storage_state, next_state)
            event = {
                ("pending", "quarantined"): "quarantine.entered",
                ("archived", "quarantined"): "quarantine.entered",
                ("quarantined", "archived"): "quarantine.restored",
            }.get(transition)
            if event is not None:
                insert_audit_event(
                    db,
                    actor="mailarchive.classification",
                    event_type=event,
                    result="success",
                    account_id=message.account_id,
                    canonical_message_id=message.id,
                )
        except BaseException:
            db.rollback()
            raise
        else:
            db.commit()
    stored = replace(
        message,
        local_path=destination,
        storage_state=next_state,
        archived_at=archived_at,
        quarantined_at=quarantined_at,
    )
    try:
        from mailarchive.notmuch import NotmuchAdapter

        NotmuchAdapter(config, kind="archive").refresh()
        NotmuchAdapter(config, kind="quarantine").refresh()
    except Exception:
        pass
    return stored


def effective_classification(db: sqlite3.Connection, canonical_id: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM classifications WHERE canonical_message_id=? ORDER BY manual_override DESC, id DESC LIMIT 1",
        (canonical_id,),
    ).fetchone()


def classify_pending(
    config: AppConfig, message: CanonicalMessage, adapter: ClassifierAdapter | None = None
) -> CanonicalMessage:
    if message.storage_state != "pending":
        return message
    if not message.local_path.is_file() or sha256_file(message.local_path) != message.sha256:
        raise IngestError("pending canonical bytes are unavailable or corrupt")
    return apply_classification(
        config, message, (adapter or RspamdAdapter()).classify(message.local_path.read_bytes())
    )


def reconcile_pending(
    config: AppConfig,
    *,
    account_name: str | None = None,
    limit: int | None = None,
    adapter: ClassifierAdapter | None = None,
) -> list[str]:
    """Local-only deterministic recovery for linked messages stranded in staging."""
    with connect(config.database.path) as db:
        sql = "SELECT c.id FROM canonical_messages c JOIN accounts a ON a.id=c.account_id WHERE c.storage_state='pending'"
        params: list[object] = []
        if account_name is not None:
            sql += " AND a.name=?"
            params.append(account_name)
        sql += " ORDER BY c.id"
        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be positive")
            sql += " LIMIT ?"
            params.append(limit)
        identifiers = [str(row[0]) for row in db.execute(sql, params)]
    completed: list[str] = []
    for identifier in identifiers:
        with connect(config.database.path) as db:
            message = canonical_message_by_id(db, identifier)
        if message is None or message.storage_state != "pending":
            continue
        try:
            classify_pending(config, message, adapter)
        except IngestError:
            with connect(config.database.path) as db:
                insert_audit_event(
                    db,
                    actor="mailarchive.classification",
                    event_type="classification.failed",
                    result="pending-integrity",
                    account_id=message.account_id,
                    canonical_message_id=message.id,
                    details_json='{"reason":"pending-integrity"}',
                )
            continue
        completed.append(identifier)
    return completed
