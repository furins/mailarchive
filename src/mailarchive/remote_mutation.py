"""M11 local planning plus an injected-fake-only mutation state machine.

No production network-writing adapter exists here.  The CLI calls ``plan_dry_run``
only; tests may pass an in-memory adapter to ``execute_fake`` directly.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Protocol, cast

from mailarchive.db import connect, insert_audit_event, utc_now
from mailarchive.models import AppConfig
from mailarchive.retention import POLICY_VERSION, evaluate_all


class ProductionExecutionUnavailable(RuntimeError):
    """Production deletion is intentionally not constructible in M11."""


@dataclass(frozen=True)
class DeletionTarget:
    remote_message_id: str
    canonical_message_id: str
    account_id: int
    account_name: str
    provider_kind: str
    canonical_sha256: str

    def fingerprint(self) -> str:
        payload = {
            "format_version": 1,
            "account_id": self.account_id,
            "provider_kind": self.provider_kind,
            "remote_message_id": self.remote_message_id,
            "canonical_message_id": self.canonical_message_id,
            "canonical_sha256": self.canonical_sha256,
            **self.identity_facts(),
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def identity_facts(self) -> dict[str, object]:
        raise NotImplementedError


@dataclass(frozen=True)
class ImapDeletionTarget(DeletionTarget):
    remote_folder: str
    uidvalidity: int
    remote_uid: int

    def identity_facts(self) -> dict[str, object]:
        return {
            "folder": self.remote_folder,
            "uidvalidity": self.uidvalidity,
            "uid": self.remote_uid,
        }


@dataclass(frozen=True)
class ProviderDeletionTarget(DeletionTarget):
    provider_message_id: str

    def identity_facts(self) -> dict[str, object]:
        return {"provider_message_id": self.provider_message_id}


@dataclass(frozen=True)
class MutationResult:
    outcome: str
    confirmed_absent: bool = False
    summary: str = ""
    error_code: str | None = None


class RemoteMutationAdapter(Protocol):
    def delete(self, target: DeletionTarget) -> MutationResult: ...


def production_adapter() -> RemoteMutationAdapter:
    raise ProductionExecutionUnavailable("production remote deletion is unavailable before M12")


def _target(row: sqlite3.Row) -> DeletionTarget:
    remote_message_id = str(row["remote_message_id"])
    canonical_message_id = str(row["canonical_message_id"])
    account_id = int(row["account_id"])
    account_name = str(row["account_name"])
    provider_kind = str(row["provider_kind"])
    canonical_sha256 = str(row["sha256"])
    if row["provider_kind"] == "imap":
        return ImapDeletionTarget(
            remote_message_id=remote_message_id,
            canonical_message_id=canonical_message_id,
            account_id=account_id,
            account_name=account_name,
            provider_kind=provider_kind,
            canonical_sha256=canonical_sha256,
            remote_folder=str(row["remote_folder"]),
            uidvalidity=int(row["uidvalidity"]),
            remote_uid=int(row["remote_uid"]),
        )
    return ProviderDeletionTarget(
        remote_message_id=remote_message_id,
        canonical_message_id=canonical_message_id,
        account_id=account_id,
        account_name=account_name,
        provider_kind=provider_kind,
        canonical_sha256=canonical_sha256,
        provider_message_id=str(row["provider_message_id"]),
    )


def plan_dry_run(
    config: AppConfig, *, account: str | None = None, limit: int | None = None
) -> dict[str, object]:
    """Fresh M10 evaluation, deterministic limits, then append a local dry-run plan."""
    if limit is not None and not 1 <= limit <= config.remote_deletion.max_per_run:
        raise ValueError("--limit must be >= 1 and cannot exceed remote_deletion.max_per_run")
    reports = evaluate_all(config, account=account)
    eligible = [report for report in reports if bool(report["eligible"])]
    max_run = config.remote_deletion.max_per_run if limit is None else limit
    ids = tuple(str(report["remote_message_id"]) for report in eligible)
    with connect(config.database.path) as db:
        rows: list[sqlite3.Row] = []
        if ids:
            placeholders = ",".join("?" for _ in ids)
            rows = db.execute(
                f"""SELECT r.*, r.id AS remote_message_id, a.name AS account_name,
                c.sha256, c.id AS canonical_message_id
                FROM remote_messages r JOIN accounts a ON a.id=r.account_id
                JOIN remote_canonical_links l ON l.remote_message_id=r.id
                JOIN canonical_messages c ON c.id=l.canonical_message_id
                WHERE r.id IN ({placeholders}) ORDER BY a.name,r.id""",
                ids,
            ).fetchall()
        reports_by_remote = {str(report["remote_message_id"]): report for report in reports}
        selected: list[sqlite3.Row] = []
        per_account: dict[int, int] = {}
        skipped_account = 0
        skipped_global = 0
        for row in rows:
            account_id = int(row["account_id"])
            if per_account.get(account_id, 0) >= config.remote_deletion.max_per_account:
                skipped_account += 1
                continue
            if len(selected) >= max_run:
                skipped_global += 1
                continue
            selected.append(row)
            per_account[account_id] = per_account.get(account_id, 0) + 1
        now = utc_now()
        run = db.execute(
            """INSERT INTO remote_mutation_runs(
            requested_at,completed_at,mode,status,account_filter,requested_limit,
            effective_max_per_run,effective_max_per_account,eligible_count,selected_count,
            skipped_limit_count,policy_version)
            VALUES(?,?,'dry-run','completed',?,?,?,?,?,?,?,?)""",
            (
                now,
                now,
                account,
                limit,
                max_run,
                config.remote_deletion.max_per_account,
                len(eligible),
                len(selected),
                skipped_account + skipped_global,
                POLICY_VERSION,
            ),
        )
        if run.lastrowid is None:
            raise RuntimeError("SQLite did not return a mutation run identifier")
        run_id = int(run.lastrowid)
        for row in selected:
            target = _target(row)
            evaluation = reports_by_remote[target.remote_message_id]
            db.execute(
                """INSERT INTO remote_mutations(
                mutation_run_id,deletion_evaluation_id,account_id,remote_message_id,
                canonical_message_id,provider_kind,operation,remote_folder,uidvalidity,
                remote_uid,provider_message_id,canonical_sha256,target_fingerprint_sha256,
                dry_run,requested_at,status,provider_response_summary)
                VALUES(?,?,?,?,?,?, 'delete',?,?,?,?,?,?,1,?,'dry-run','local-plan')""",
                (
                    run_id,
                    int(cast(int, evaluation["deletion_evaluation_id"])),
                    target.account_id,
                    target.remote_message_id,
                    target.canonical_message_id,
                    target.provider_kind,
                    getattr(target, "remote_folder", None),
                    getattr(target, "uidvalidity", None),
                    getattr(target, "remote_uid", None),
                    getattr(target, "provider_message_id", None),
                    target.canonical_sha256,
                    target.fingerprint(),
                    now,
                ),
            )
        details = {
            "eligible": len(eligible),
            "selected": len(selected),
            "skipped_limit": skipped_account + skipped_global,
            "policy_version": POLICY_VERSION,
        }
        insert_audit_event(
            db,
            actor="mailarchive.remote_mutation",
            event_type="remote_delete.dry_run.completed",
            result="success",
            details_json=json.dumps(details, sort_keys=True),
        )
        db.commit()
    return {
        "run_id": run_id,
        "candidate_eligible": bool(eligible),
        "planned": True,
        "dry_run": True,
        "production_execution_authorized": False,
        "eligible": len(eligible),
        "selected": len(selected),
        "skipped_due_to_global_limit": skipped_global,
        "skipped_due_to_account_limit": skipped_account,
        "policy_version": POLICY_VERSION,
    }


def _planned_target(db: sqlite3.Connection, mutation_id: int) -> DeletionTarget:
    row = db.execute(
        """SELECT m.*,a.name AS account_name,c.sha256 FROM remote_mutations m
        JOIN accounts a ON a.id=m.account_id
        JOIN canonical_messages c ON c.id=m.canonical_message_id
        WHERE m.id=?""",
        (mutation_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("planned mutation disappeared")
    return _target(row)


def _still_current(config: AppConfig, target: DeletionTarget, fingerprint: str) -> bool:
    fresh = {str(item["remote_message_id"]): item for item in evaluate_all(config)}
    report = fresh.get(target.remote_message_id)
    if report is None or not bool(report["eligible"]):
        return False
    with connect(config.database.path) as db:
        row = db.execute(
            """SELECT r.*,r.id AS remote_message_id,a.name AS account_name,
            c.sha256,c.id AS canonical_message_id
            FROM remote_messages r JOIN accounts a ON a.id=r.account_id
            JOIN remote_canonical_links l ON l.remote_message_id=r.id
            JOIN canonical_messages c ON c.id=l.canonical_message_id
            WHERE r.id=? AND c.id=?""",
            (target.remote_message_id, target.canonical_message_id),
        ).fetchone()
    return (
        row is not None
        and bool(row["remote_present"])
        and _target(row).fingerprint() == fingerprint
    )


def execute_fake(config: AppConfig, run_id: int, adapter: RemoteMutationAdapter) -> None:
    """Test-only execution; pre-call started state is committed before each fake call."""
    with connect(config.database.path) as db:
        db.execute(
            "UPDATE remote_mutation_runs SET mode='fake-execute',status='planned' WHERE id=?",
            (run_id,),
        )
        db.commit()
    while True:
        with connect(config.database.path) as db:
            row = db.execute(
                """SELECT * FROM remote_mutations WHERE mutation_run_id=? AND status='dry-run'
                ORDER BY id LIMIT 1""",
                (run_id,),
            ).fetchone()
            if row is None:
                db.execute(
                    "UPDATE remote_mutation_runs SET status='completed',completed_at=? WHERE id=?",
                    (utc_now(), run_id),
                )
                db.commit()
                return
            target = _planned_target(db, int(row["id"]))
        if not _still_current(config, target, str(row["target_fingerprint_sha256"])):
            _halt_stale(config, run_id, int(row["id"]))
            return
        with connect(config.database.path) as db:
            db.execute(
                "UPDATE remote_mutations SET status='started',started_at=? WHERE id=?",
                (utc_now(), row["id"]),
            )
            insert_audit_event(
                db,
                actor="mailarchive.remote_mutation",
                event_type="remote_mutation.started",
                result="started",
                details_json="{}",
            )
            db.commit()
        try:
            result = adapter.delete(target)
        except Exception:
            # Provider exception text can contain remote data; retain only a bounded category.
            result = MutationResult("outcome-unknown", error_code="ADAPTER_EXCEPTION")
        _record_outcome(config, run_id, int(row["id"]), target, result)
        if result.outcome != "success-confirmed":
            return


def _halt_stale(config: AppConfig, run_id: int, mutation_id: int) -> None:
    with connect(config.database.path) as db:
        now = utc_now()
        db.execute(
            "UPDATE remote_mutations SET status='failed',completed_at=?, "
            "error_code='STALE_PLAN' WHERE id=?",
            (now, mutation_id),
        )
        db.execute(
            "UPDATE remote_mutation_runs SET status='halted',completed_at=? WHERE id=?",
            (now, run_id),
        )
        insert_audit_event(
            db,
            actor="mailarchive.remote_mutation",
            event_type="remote_mutation.stale_plan",
            result="failed",
            details_json="{}",
        )
        db.commit()


def _record_outcome(
    config: AppConfig, run_id: int, mutation_id: int, target: DeletionTarget, result: MutationResult
) -> None:
    statuses = {
        "success-confirmed": "succeeded",
        "failure-confirmed-no-mutation": "failed",
        "outcome-unknown": "unknown",
    }
    status = statuses.get(result.outcome, "unknown")
    with connect(config.database.path) as db:
        now = utc_now()
        db.execute(
            """UPDATE remote_mutations SET status=?,completed_at=?,provider_response_summary=?,
            error_code=? WHERE id=?""",
            (status, now, result.summary[:64] or None, result.error_code, mutation_id),
        )
        if status == "succeeded" and result.confirmed_absent:
            db.execute(
                "UPDATE remote_messages SET remote_present=0 WHERE id=?",
                (target.remote_message_id,),
            )
        insert_audit_event(
            db,
            actor="mailarchive.remote_mutation",
            event_type=f"remote_mutation.{status}",
            result=status,
            details_json="{}",
        )
        if status != "succeeded":
            db.execute(
                "UPDATE remote_mutation_runs SET status='halted',completed_at=? WHERE id=?",
                (now, run_id),
            )
        db.commit()
