"""M10 local-only retention-candidate policy and evaluation history.

``eligible`` is a reporting result only.  It is never authorization to mutate a
provider, and this module deliberately has no provider or Borg adapter imports.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mailarchive.db import connect, insert_audit_event, utc_now
from mailarchive.models import AppConfig

POLICY_VERSION = "retention-v1"
REASON_CODES = (
    "ACCOUNT_DISABLED",
    "REMOTE_NOT_PRESENT",
    "REMOTE_IDENTITY_UNPROVEN",
    "REMOTE_IDENTITY_INCOMPLETE",
    "REMOTE_IDENTITY_INCOHERENT",
    "REMOTE_LINK_MISSING",
    "REMOTE_LINK_INCOHERENT",
    "CANONICAL_MISSING",
    "CANONICAL_NOT_ARCHIVED",
    "QUARANTINED",
    "ARCHIVED_AT_MISSING",
    "TIMESTAMP_INVALID",
    "RETENTION_NOT_ELAPSED",
    "RETENTION_NEVER",
    "CANONICAL_PATH_INVALID",
    "CANONICAL_FILE_MISSING",
    "CANONICAL_HASH_MISMATCH",
    "INTEGRITY_NOT_VERIFIED",
    "BACKUPS_INSUFFICIENT",
    "KEEP_ONLINE",
    "LEGAL_HOLD",
)


@dataclass(frozen=True)
class RetentionFacts:
    remote_message_id: str
    account: str
    account_enabled: bool
    provider_kind: str | None
    account_kind: str | None
    remote_present: bool
    identity_confidence: str | None
    identity_complete: bool
    canonical_id: str | None
    link_count: int
    link_account_coherent: bool
    canonical_exists: bool
    storage_state: str | None
    archived_at: str | None
    canonical_path: Path | None
    expected_sha256: str | None
    integrity_status: str | None
    verified_repository_count: int
    keep_online: bool
    legal_hold: bool


@dataclass(frozen=True)
class Evaluation:
    eligible: bool
    reason_codes: tuple[str, ...]
    retention_deadline: datetime | None
    verified_repository_count: int


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def evaluate(
    facts: RetentionFacts,
    *,
    now: datetime,
    retention_days: int | None,
    required_verified_backups: int,
    managed_mail_root: Path,
) -> Evaluation:
    """Pure, deterministic fail-closed policy evaluation; callers inject UTC ``now``."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    reasons: list[str] = []
    if not facts.account_enabled:
        reasons.append("ACCOUNT_DISABLED")
    if not facts.remote_present:
        reasons.append("REMOTE_NOT_PRESENT")
    if facts.identity_confidence != "proven":
        reasons.append("REMOTE_IDENTITY_UNPROVEN")
    if not facts.identity_complete:
        reasons.append("REMOTE_IDENTITY_INCOMPLETE")
    if facts.provider_kind != facts.account_kind:
        reasons.append("REMOTE_IDENTITY_INCOHERENT")
    if facts.link_count != 1:
        reasons.append("REMOTE_LINK_MISSING")
    if not facts.link_account_coherent:
        reasons.append("REMOTE_LINK_INCOHERENT")
    if not facts.canonical_exists:
        reasons.append("CANONICAL_MISSING")
    if facts.storage_state != "archived":
        reasons.append("CANONICAL_NOT_ARCHIVED")
    if facts.storage_state == "quarantined":
        reasons.append("QUARANTINED")
    parsed_archived = _parse_timestamp(facts.archived_at)
    if facts.archived_at is None:
        reasons.append("ARCHIVED_AT_MISSING")
    elif parsed_archived is None:
        reasons.append("TIMESTAMP_INVALID")
    deadline = (
        None
        if parsed_archived is None or retention_days is None
        else parsed_archived + timedelta(days=retention_days)
    )
    if retention_days is None:
        reasons.append("RETENTION_NEVER")
    elif deadline is not None and now.astimezone(UTC) < deadline:
        reasons.append("RETENTION_NOT_ELAPSED")
    path_ok = False
    if facts.canonical_path is not None:
        try:
            path_ok = facts.canonical_path.resolve(strict=False).is_relative_to(
                managed_mail_root.resolve(strict=False)
            )
        except OSError, ValueError:
            path_ok = False
    if not path_ok:
        reasons.append("CANONICAL_PATH_INVALID")
    else:
        path = facts.canonical_path
        assert path is not None
        if not path.is_file():
            reasons.append("CANONICAL_FILE_MISSING")
        else:
            try:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                reasons.append("CANONICAL_FILE_MISSING")
            else:
                if actual != facts.expected_sha256:
                    reasons.append("CANONICAL_HASH_MISMATCH")
    if facts.integrity_status != "verified":
        reasons.append("INTEGRITY_NOT_VERIFIED")
    if facts.verified_repository_count < required_verified_backups:
        reasons.append("BACKUPS_INSUFFICIENT")
    if facts.keep_online:
        reasons.append("KEEP_ONLINE")
    if facts.legal_hold:
        reasons.append("LEGAL_HOLD")
    ordered = tuple(code for code in REASON_CODES if code in reasons)
    return Evaluation(not ordered, ordered, deadline, facts.verified_repository_count)


def _identity_complete(row: sqlite3.Row) -> bool:
    kind = row["provider_kind"]
    if kind == "imap":
        return (
            all(
                row[key] is not None and str(row[key]) != ""
                for key in ("remote_folder", "uidvalidity", "remote_uid")
            )
            and int(row["uidvalidity"] or 0) > 0
            and int(row["remote_uid"] or 0) > 0
        )
    return (
        kind in {"gmail", "pop3"}
        and row["provider_message_id"] is not None
        and str(row["provider_message_id"]).strip() != ""
    )


def evaluate_all(
    config: AppConfig, *, now: datetime | None = None, account: str | None = None
) -> list[dict[str, object]]:
    """Collect local facts, append one evaluation row per remote identity, and return reports."""
    evaluated_at = now or datetime.now(UTC)
    if evaluated_at.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    with connect(config.database.path) as db:
        run = db.execute(
            "INSERT INTO deletion_evaluation_runs(evaluated_at,policy_version) VALUES(?,?)",
            (evaluated_at.isoformat(), POLICY_VERSION),
        )
        if run.lastrowid is None:
            raise RuntimeError("SQLite did not return an evaluation run identifier")
        run_id = int(run.lastrowid)
        sql = """
        SELECT r.*, a.name AS account, a.kind AS account_kind, a.enabled AS account_enabled,
               c.id AS canonical_id, c.account_id AS canonical_account_id, c.sha256,
               c.local_path, c.storage_state, c.archived_at, c.integrity_status,
               rc.keep_online, rc.legal_hold,
               (SELECT COUNT(*) FROM remote_canonical_links l WHERE l.remote_message_id=r.id)
                   AS link_count,
               (SELECT COUNT(DISTINCT br.repository_identity)
                FROM remote_canonical_links l
                JOIN message_backup_evidence e ON e.canonical_message_id=l.canonical_message_id
                JOIN backup_runs b ON b.id=e.backup_run_id
                JOIN backup_repositories br ON br.id=b.repository_id
                WHERE l.remote_message_id=r.id AND e.covered=1 AND e.verified=1
                  AND b.status='succeeded' AND b.verification_status='verified'
                  AND b.verified_at IS NOT NULL AND br.repository_identity IS NOT NULL)
                   AS verified_repositories
        FROM remote_messages r
        JOIN accounts a ON a.id=r.account_id
        LEFT JOIN remote_canonical_links l ON l.remote_message_id=r.id
        LEFT JOIN canonical_messages c ON c.id=l.canonical_message_id
        LEFT JOIN retention_controls rc ON rc.canonical_message_id=c.id
        """
        params: tuple[object, ...] = ()
        if account is not None:
            sql += " WHERE a.name=?"
            params = (account,)
        rows = db.execute(sql + " ORDER BY a.name,r.id", params).fetchall()
        reports: list[dict[str, object]] = []
        for row in rows:
            days = next(
                (a.remote_retention_days for a in config.accounts if a.name == row["account"]),
                config.retention.remote_retention_days_default,
            )
            required = next(
                (a.required_verified_backups for a in config.accounts if a.name == row["account"]),
                config.retention.required_verified_backups_default,
            )
            facts = RetentionFacts(
                str(row["id"]),
                str(row["account"]),
                bool(row["account_enabled"]),
                row["provider_kind"],
                row["account_kind"],
                bool(row["remote_present"]),
                row["identity_confidence"],
                _identity_complete(row),
                None if row["canonical_id"] is None else str(row["canonical_id"]),
                int(row["link_count"]),
                row["canonical_account_id"] is not None
                and int(row["account_id"]) == int(row["canonical_account_id"]),
                row["canonical_id"] is not None,
                row["storage_state"],
                row["archived_at"],
                None if row["local_path"] is None else Path(str(row["local_path"])),
                row["sha256"],
                row["integrity_status"],
                int(row["verified_repositories"]),
                bool(row["keep_online"] or 0),
                bool(row["legal_hold"] or 0),
            )
            result = evaluate(
                facts,
                now=evaluated_at,
                retention_days=days,
                required_verified_backups=required,
                managed_mail_root=config.archive.root / "mail",
            )
            db.execute(
                "INSERT INTO deletion_evaluations("
                "evaluation_run_id,remote_message_id,canonical_message_id,evaluated_at,eligible,"
                "reason_codes_json,policy_version,remote_retention_days,required_verified_backups,"
                "verified_repository_count,retention_deadline) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    facts.remote_message_id,
                    facts.canonical_id,
                    evaluated_at.isoformat(),
                    int(result.eligible),
                    json.dumps(result.reason_codes),
                    POLICY_VERSION,
                    days,
                    required,
                    result.verified_repository_count,
                    None
                    if result.retention_deadline is None
                    else result.retention_deadline.isoformat(),
                ),
            )
            reports.append(
                {
                    "remote_message_id": facts.remote_message_id,
                    "canonical_id": facts.canonical_id,
                    "account": facts.account,
                    "provider_kind": facts.provider_kind,
                    "eligible": result.eligible,
                    "execution_authorized": False,
                    "reason_codes": list(result.reason_codes),
                    "archived_at": facts.archived_at,
                    "retention_deadline": None
                    if result.retention_deadline is None
                    else result.retention_deadline.isoformat(),
                    "verified_repository_count": result.verified_repository_count,
                    "required_verified_backups": required,
                    "keep_online": facts.keep_online,
                    "legal_hold": facts.legal_hold,
                }
            )
        insert_audit_event(
            db,
            actor="mailarchive.retention",
            event_type="retention.evaluation.completed",
            result="success",
            details_json=json.dumps(
                {
                    "evaluated_count": len(reports),
                    "eligible_count": sum(bool(r["eligible"]) for r in reports),
                    "blocked_count": sum(not bool(r["eligible"]) for r in reports),
                    "policy_version": POLICY_VERSION,
                }
            ),
        )
        db.commit()
    return reports


def set_control(
    config: AppConfig, canonical_id: str, kind: str, reason: str, *, enabled: bool
) -> None:
    if kind not in {"keep-online", "legal-hold"}:
        raise ValueError("invalid retention control kind")
    reason = reason.strip()
    if not reason or len(reason) > 256:
        raise ValueError("--reason must be 1..256 characters")
    column = "keep_online" if kind == "keep-online" else "legal_hold"
    with connect(config.database.path) as db:
        if (
            db.execute("SELECT 1 FROM canonical_messages WHERE id=?", (canonical_id,)).fetchone()
            is None
        ):
            raise ValueError("unknown canonical ID")
        if enabled:
            db.execute(
                f"INSERT INTO retention_controls(canonical_message_id,{column},reason,updated_at) "
                f"VALUES(?,1,?,?) ON CONFLICT(canonical_message_id) DO UPDATE SET {column}=1,"
                "reason=excluded.reason,updated_at=excluded.updated_at",
                (canonical_id, reason, utc_now()),
            )
        else:
            other_column = "legal_hold" if column == "keep_online" else "keep_online"
            other = db.execute(
                f"SELECT {other_column} FROM retention_controls WHERE canonical_message_id=?",
                (canonical_id,),
            ).fetchone()
            if other is not None and bool(other[0]):
                db.execute(
                    f"UPDATE retention_controls SET {column}=0,reason=?,updated_at=? "
                    "WHERE canonical_message_id=?",
                    (reason, utc_now(), canonical_id),
                )
            else:
                db.execute(
                    "DELETE FROM retention_controls WHERE canonical_message_id=?",
                    (canonical_id,),
                )
        insert_audit_event(
            db,
            actor="mailarchive.retention",
            event_type="retention.control.set" if enabled else "retention.control.released",
            result="success",
            canonical_message_id=canonical_id,
            details_json=json.dumps({"kind": kind, "reason_length": len(reason)}),
        )
        db.commit()
