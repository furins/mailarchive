"""M11 planning and M12-A provider-neutral production execution foundation."""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from mailarchive.db import connect, insert_audit_event, utc_now
from mailarchive.models import AppConfig
from mailarchive.retention import POLICY_VERSION, evaluate_all


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


_FAILURE_ERROR_CODES = {
    "TARGET_NOT_FOUND",
    "IDENTITY_MISMATCH",
    "PROVIDER_REJECTED",
    "SAFE_DELETE_UNSUPPORTED",
    "REMOTE_STATE_CONFLICT",
    "AUTHORIZATION_FAILED",
}
_INTERNAL_ERROR_CODES = {"ADAPTER_EXCEPTION", "STALE_PLAN", "INVALID_ADAPTER_RESULT"}


def _normalize_result(
    result: MutationResult, *, internal_error_code: str | None = None
) -> tuple[str, str, str]:
    """Enforce the closed adapter coherence matrix; never retain adapter text."""
    if internal_error_code is not None:
        allowed_internal = _INTERNAL_ERROR_CODES - {"STALE_PLAN", "INVALID_ADAPTER_RESULT"}
        if internal_error_code not in allowed_internal:
            raise ValueError("invalid internal mutation error code")
        return "unknown", "outcome-uncertain", internal_error_code
    code = result.error_code or "NONE"
    if result.outcome == "success-confirmed" and result.confirmed_absent and code == "NONE":
        return "succeeded", "confirmed-absent", "NONE"
    if (
        result.outcome == "failure-confirmed-no-mutation"
        and not result.confirmed_absent
        and code in _FAILURE_ERROR_CODES
    ):
        return "failed", "confirmed-no-mutation", code
    if (
        result.outcome == "outcome-unknown"
        and not result.confirmed_absent
        and code == "TRANSPORT_UNKNOWN"
    ):
        return "unknown", "outcome-uncertain", code
    return "unknown", "outcome-uncertain", "INVALID_ADAPTER_RESULT"


class RemoteMutationAdapter(Protocol):
    def delete(self, target: DeletionTarget) -> MutationResult: ...


@dataclass(frozen=True)
class ObservationResult:
    """Closed, read-only proof about the exact historical mutation target."""

    state: str


_OBSERVATION_STATES = {
    "confirmed-absent",
    "confirmed-present-match",
    "identity-conflict",
    "unknown",
}


class RemoteObservationAdapter(Protocol):
    def observe(self, target: DeletionTarget) -> ObservationResult: ...


PLAN_TTL = timedelta(minutes=60)


class ProductionPlanError(RuntimeError):
    """The explicit M12 authorization chain cannot be proven locally."""


def production_adapter_factory(config: AppConfig, account_name: str) -> RemoteMutationAdapter:
    """M12-A deliberately has no provider adapter; factories must be local/no-network."""
    raise ProductionPlanError("production provider adapter not implemented in M12-A")


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
                dry_run,requested_at,status)
                VALUES(?,?,?,?,?,?, 'delete',?,?,?,?,?,?,1,?,'dry-run')""",
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


def _historical_target(db: sqlite3.Connection, mutation_id: int) -> DeletionTarget:
    """Rebuild the target that was attempted, never current provider identity."""
    row = db.execute(
        """SELECT m.*,a.name AS account_name FROM remote_mutations m
        JOIN accounts a ON a.id=m.account_id WHERE m.id=?""",
        (mutation_id,),
    ).fetchone()
    if row is None:
        raise ReconciliationError("historical mutation disappeared")
    remote_message_id = str(row["remote_message_id"])
    canonical_message_id = str(row["canonical_message_id"])
    account_id = int(row["account_id"])
    account_name = str(row["account_name"])
    provider_kind = str(row["provider_kind"])
    canonical_sha256 = str(row["canonical_sha256"])
    if row["provider_kind"] == "imap":
        return ImapDeletionTarget(
            remote_message_id,
            canonical_message_id,
            account_id,
            account_name,
            provider_kind,
            canonical_sha256,
            remote_folder=str(row["remote_folder"]),
            uidvalidity=int(row["uidvalidity"]),
            remote_uid=int(row["remote_uid"]),
        )
    return ProviderDeletionTarget(
        remote_message_id,
        canonical_message_id,
        account_id,
        account_name,
        provider_kind,
        canonical_sha256,
        str(row["provider_message_id"]),
    )


def _update_current_presence_if_historical_identity_matches(
    db: sqlite3.Connection, target: DeletionTarget, present: int
) -> None:
    """Atomically avoid applying proof about an old identity to a drifted row."""
    if isinstance(target, ImapDeletionTarget):
        db.execute(
            """UPDATE remote_messages SET remote_present=? WHERE id=? AND account_id=?
            AND provider_kind='imap' AND remote_folder=? AND uidvalidity=? AND remote_uid=?""",
            (
                present,
                target.remote_message_id,
                target.account_id,
                target.remote_folder,
                target.uidvalidity,
                target.remote_uid,
            ),
        )
        return
    if not isinstance(target, ProviderDeletionTarget):
        raise ValueError("unsupported historical target type")
    db.execute(
        """UPDATE remote_messages SET remote_present=? WHERE id=? AND account_id=?
        AND provider_kind=? AND provider_message_id=?""",
        (
            present,
            target.remote_message_id,
            target.account_id,
            target.provider_kind,
            target.provider_message_id,
        ),
    )


class ReconciliationError(RuntimeError):
    """A local production-run reconciliation precondition failed."""


def remote_mutation_status(
    config: AppConfig, *, run_id: int | None = None, account_name: str | None = None
) -> dict[str, object]:
    """Return bounded, entirely local mutation-run history in deterministic order."""
    if account_name is not None and not any(
        account.name == account_name for account in config.accounts
    ):
        raise ReconciliationError("status account is not configured")
    if not config.database.path.exists():
        if run_id is not None:
            raise ReconciliationError("remote mutation run does not exist")
        return {"runs": []}
    with connect(config.database.path) as db:
        clauses: list[str] = []
        values: list[object] = []
        if run_id is not None:
            clauses.append("id=?")
            values.append(run_id)
        if account_name is not None:
            clauses.append("account_filter=?")
            values.append(account_name)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        rows = db.execute(
            "SELECT * FROM remote_mutation_runs" + where + " ORDER BY id DESC", values
        ).fetchall()
        if run_id is not None and not rows:
            raise ReconciliationError("remote mutation run does not exist or does not match account")
        runs: list[dict[str, object]] = []
        statuses = ("planned", "dry-run", "started", "succeeded", "failed", "unknown")
        for run in rows:
            mutations = db.execute(
                "SELECT * FROM remote_mutations WHERE mutation_run_id=? ORDER BY id",
                (run["id"],),
            ).fetchall()
            counts = {status: 0 for status in statuses}
            details: list[dict[str, object]] = []
            for mutation in mutations:
                status = str(mutation["status"])
                if status in counts:
                    counts[status] += 1
                detail: dict[str, object] = {
                    "mutation_id": int(mutation["id"]),
                    "account_id": int(mutation["account_id"]),
                    "remote_message_id": str(mutation["remote_message_id"]),
                    "canonical_message_id": str(mutation["canonical_message_id"]),
                    "provider_kind": str(mutation["provider_kind"]),
                    "operation": str(mutation["operation"]),
                    "status": status,
                    "requested_at": mutation["requested_at"],
                    "started_at": mutation["started_at"],
                    "completed_at": mutation["completed_at"],
                    "reconciled_at": mutation["reconciled_at"],
                    "provider_response_summary": mutation["provider_response_summary"],
                    "error_code": mutation["error_code"],
                    "source_plan_mutation_id": mutation["source_plan_mutation_id"],
                }
                if str(mutation["provider_kind"]) == "imap":
                    detail.update(
                        remote_folder=mutation["remote_folder"],
                        uidvalidity=mutation["uidvalidity"],
                        remote_uid=mutation["remote_uid"],
                    )
                else:
                    detail["provider_message_id"] = mutation["provider_message_id"]
                details.append(detail)
            runs.append(
                {
                    "run_id": int(run["id"]),
                    "mode": str(run["mode"]),
                    "status": str(run["status"]),
                    "requested_at": run["requested_at"],
                    "completed_at": run["completed_at"],
                    "account_filter": run["account_filter"],
                    "source_plan_run_id": run["source_plan_run_id"],
                    "selected_count": int(run["selected_count"]),
                    "counts": counts,
                    "reconciliation_required": (
                        run["mode"] == "production-execute"
                        and (counts["started"] > 0 or counts["unknown"] > 0)
                    ),
                    "mutations": details,
                }
            )
    return {"runs": runs}


def observation_adapter_factory(
    config: AppConfig, account_name: str
) -> RemoteObservationAdapter:
    """Construct the closed, read-only provider observer without network I/O."""
    account = next((item for item in config.accounts if item.name == account_name), None)
    if account is None:
        raise ReconciliationError("reconciliation account is not configured")
    # These imports are deliberately local: each concrete adapter imports the
    # target/observation types above, while construction itself remains local.
    if account.kind == "imap":
        from mailarchive.imap_mutation import ImapMutationAdapter

        return ImapMutationAdapter(config, account_name)
    if account.kind == "gmail":
        from mailarchive.gmail_mutation import GmailMutationAdapter, google_mutation_transport

        return GmailMutationAdapter(
            config,
            account_name,
            transport_factory=google_mutation_transport,
        )
    if account.kind == "pop3":
        from mailarchive.pop3_mutation import Pop3MutationAdapter

        return Pop3MutationAdapter(config, account_name)
    raise ReconciliationError("reconciliation account provider is unsupported")


def reconcile_production_run(
    config: AppConfig,
    run_id: int,
    *,
    observer_factory: Callable[[AppConfig, str], RemoteObservationAdapter] | None = None,
) -> dict[str, object]:
    """Read-only recovery of started/unknown rows; never resumes a production run."""
    observer_factory = observation_adapter_factory if observer_factory is None else observer_factory
    counts = {"observed": 0, "resolved_absent": 0, "resolved_present": 0, "unresolved": 0}
    with connect(config.database.path) as db:
        run = db.execute("SELECT * FROM remote_mutation_runs WHERE id=?", (run_id,)).fetchone()
        if run is None or run["mode"] != "production-execute" or run["status"] != "halted":
            raise ReconciliationError("run is not a halted production execution")
        rows = db.execute(
            "SELECT id FROM remote_mutations WHERE mutation_run_id=? AND status IN ('started','unknown') ORDER BY id",
            (run_id,),
        ).fetchall()
    for item in rows:
        mutation_id = int(item["id"])
        with connect(config.database.path) as db:
            target = _historical_target(db, mutation_id)
            row = db.execute(
                "SELECT status FROM remote_mutations WHERE id=?", (mutation_id,)
            ).fetchone()
            insert_audit_event(
                db,
                actor="mailarchive.remote_mutation",
                event_type="remote_mutation.reconcile.started",
                result="started",
                details_json=json.dumps(
                    {
                        "run_id": run_id,
                        "mutation_id": mutation_id,
                        "account_id": target.account_id,
                        "provider_kind": target.provider_kind,
                    },
                    sort_keys=True,
                ),
            )
            db.commit()
        try:
            observed = observer_factory(config, target.account_name).observe(target)
            state = observed.state if observed.state in _OBSERVATION_STATES else "unknown"
        except Exception:
            state = "unknown"
        counts["observed"] += 1
        with connect(config.database.path) as db:
            now = utc_now()
            if state == "confirmed-absent":
                db.execute(
                    "UPDATE remote_mutations SET status='succeeded',completed_at=?,reconciled_at=?,provider_response_summary='confirmed-absent',error_code='NONE' WHERE id=?",
                    (now, now, mutation_id),
                )
                _update_current_presence_if_historical_identity_matches(db, target, 0)
                event, result = "remote_mutation.reconcile.absent", "success"
                counts["resolved_absent"] += 1
            elif state == "confirmed-present-match":
                db.execute(
                    "UPDATE remote_mutations SET status='failed',completed_at=?,reconciled_at=?,provider_response_summary='confirmed-no-mutation',error_code='RECONCILED_PRESENT' WHERE id=?",
                    (now, now, mutation_id),
                )
                _update_current_presence_if_historical_identity_matches(db, target, 1)
                event, result = "remote_mutation.reconcile.present", "failed"
                counts["resolved_present"] += 1
            else:
                if str(row["status"]) == "started":
                    db.execute(
                        "UPDATE remote_mutations SET status='unknown',completed_at=?,provider_response_summary='outcome-uncertain',error_code='TRANSPORT_UNKNOWN' WHERE id=?",
                        (now, mutation_id),
                    )
                event, result = "remote_mutation.reconcile.unknown", "unknown"
                counts["unresolved"] += 1
            insert_audit_event(
                db,
                actor="mailarchive.remote_mutation",
                event_type=event,
                result=result,
                details_json=json.dumps(
                    {
                        "run_id": run_id,
                        "mutation_id": mutation_id,
                        "account_id": target.account_id,
                        "provider_kind": target.provider_kind,
                        "state": state,
                    },
                    sort_keys=True,
                ),
            )
            db.commit()
    return {"run_id": run_id, "status": "halted", **counts, "resumed": False}


def _still_current(
    config: AppConfig,
    target: DeletionTarget,
    fingerprint: str,
    fresh_report: dict[str, object] | None = None,
) -> bool:
    fresh = (
        {str(item["remote_message_id"]): item for item in evaluate_all(config)}
        if fresh_report is None
        else {target.remote_message_id: fresh_report}
    )
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
        and str(report.get("remote_message_id")) == target.remote_message_id
        and str(report.get("canonical_id")) == target.canonical_message_id
        and _target(row).fingerprint() == fingerprint
    )


def execute_fake(config: AppConfig, run_id: int, adapter: RemoteMutationAdapter) -> None:
    """Test-only execution; pre-call started state is committed before each fake call."""
    with connect(config.database.path) as db:
        db.execute(
            """UPDATE remote_mutation_runs SET mode='fake-execute',status='planned',
            completed_at=NULL
            WHERE id=?""",
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
            status = _record_outcome(config, run_id, int(row["id"]), target, result)
        except Exception:
            # Provider exception text can contain remote data; retain only a bounded category.
            status = _record_outcome(
                config,
                run_id,
                int(row["id"]),
                target,
                MutationResult("outcome-unknown"),
                internal_error_code="ADAPTER_EXCEPTION",
            )
        if status != "succeeded":
            return


def execute_production_plan(
    config: AppConfig,
    source_plan_run_id: int,
    account_name: str,
    *,
    adapter_factory: Callable[[AppConfig, str], RemoteMutationAdapter] = production_adapter_factory,
) -> int:
    """Execute only one current, account-filtered M11 plan; serial and fail-closed."""
    account = next((item for item in config.accounts if item.name == account_name), None)
    if account is None or not account.enabled or not account.remote_deletion_enabled:
        raise ProductionPlanError(
            "account is not explicitly enabled for production remote deletion"
        )
    now = datetime.now(UTC)
    with connect(config.database.path) as db:
        source = db.execute(
            "SELECT * FROM remote_mutation_runs WHERE id=?", (source_plan_run_id,)
        ).fetchone()
        if source is None or source["mode"] != "dry-run" or source["status"] != "completed":
            raise ProductionPlanError("source run is not a completed dry-run plan")
        if source["account_filter"] != account_name or int(source["selected_count"]) < 1:
            raise ProductionPlanError(
                "source plan is not explicitly limited to the requested account"
            )
        if (
            db.execute(
                "SELECT 1 FROM remote_mutation_runs WHERE mode='production-execute' AND source_plan_run_id=?",
                (source_plan_run_id,),
            ).fetchone()
            is not None
        ):
            raise ProductionPlanError("source plan already has a production execution")
        try:
            requested_at = datetime.fromisoformat(str(source["requested_at"]))
        except ValueError as error:
            raise ProductionPlanError(
                "source plan has an invalid requested_at timestamp"
            ) from error
        if requested_at.tzinfo is None or requested_at.astimezone(UTC) > now:
            raise ProductionPlanError("source plan has an invalid requested_at timestamp")
        if now - requested_at.astimezone(UTC) > PLAN_TTL:
            raise ProductionPlanError("source plan expired; create a new dry-run")
        selected = int(source["selected_count"])
        limits = (
            int(source["effective_max_per_run"]),
            int(source["effective_max_per_account"]),
            config.remote_deletion.max_per_run,
            config.remote_deletion.max_per_account,
        )
        if any(selected > limit for limit in limits) or (
            int(source["effective_max_per_run"]) > config.remote_deletion.max_per_run
            or int(source["effective_max_per_account"]) > config.remote_deletion.max_per_account
        ):
            raise ProductionPlanError("source plan exceeds current safety limit")
        rows = db.execute(
            "SELECT * FROM remote_mutations WHERE mutation_run_id=? ORDER BY id",
            (source_plan_run_id,),
        ).fetchall()
        if len(rows) != int(source["selected_count"]) or any(
            row["status"] != "dry-run" or not bool(row["dry_run"]) for row in rows
        ):
            raise ProductionPlanError("source plan mutation state is invalid")
        account_row = db.execute("SELECT id FROM accounts WHERE name=?", (account_name,)).fetchone()
        if account_row is None:
            raise ProductionPlanError("account is not active locally")
        configured_account_id = int(account_row[0])
        if any(int(row["account_id"]) != configured_account_id for row in rows):
            raise ProductionPlanError("source plan contains another account")
        placeholders = ",".join("?" for _ in rows)
        unresolved = db.execute(
            f"""SELECT 1 FROM remote_mutations m JOIN remote_mutation_runs r ON r.id=m.mutation_run_id
            WHERE r.mode='production-execute' AND m.remote_message_id IN ({placeholders})
              AND m.status IN ('started','unknown')""",
            tuple(str(row["remote_message_id"]) for row in rows),
        ).fetchone()
        if unresolved is not None:
            raise ProductionPlanError("target has unresolved destructive production state")
        # Fresh M10 before cloning; never reuse prior plan eligibility.
        fresh = {
            str(item["remote_message_id"]): item
            for item in evaluate_all(config, account=account_name)
        }
        if any(
            not bool(fresh.get(str(row["remote_message_id"]), {}).get("eligible")) for row in rows
        ):
            raise ProductionPlanError("source plan became stale or ineligible")
        for row in rows:
            target = _planned_target(db, int(row["id"]))
            report = fresh[str(row["remote_message_id"])]
            if not _still_current(config, target, str(row["target_fingerprint_sha256"]), report):
                raise ProductionPlanError("source plan target fingerprint or identity is stale")
        # Required to be a local, non-network factory preflight in all future phases.
        adapter = adapter_factory(config, account_name)
        created = utc_now()
        run = db.execute(
            """INSERT INTO remote_mutation_runs(requested_at,mode,status,account_filter,
            requested_limit,effective_max_per_run,effective_max_per_account,eligible_count,selected_count,
            skipped_limit_count,policy_version,source_plan_run_id,authorization_method)
            VALUES(?,'production-execute','planned',?,?,?,?,?,?,?,?,?, 'account-opt-in+explicit-plan-v1')""",
            (
                created,
                account_name,
                None,
                source["effective_max_per_run"],
                source["effective_max_per_account"],
                len(rows),
                len(rows),
                0,
                POLICY_VERSION,
                source_plan_run_id,
            ),
        )
        production_run_id = int(cast(int, run.lastrowid))
        for row in rows:
            evaluation = fresh[str(row["remote_message_id"])]
            db.execute(
                """INSERT INTO remote_mutations(mutation_run_id,deletion_evaluation_id,account_id,remote_message_id,
                canonical_message_id,provider_kind,operation,remote_folder,uidvalidity,remote_uid,provider_message_id,
                canonical_sha256,target_fingerprint_sha256,dry_run,requested_at,status,source_plan_mutation_id)
                VALUES(?,?,?,?,?,?, 'delete',?,?,?,?,?,?,0,?,'planned',?)""",
                (
                    production_run_id,
                    int(cast(int, evaluation["deletion_evaluation_id"])),
                    row["account_id"],
                    row["remote_message_id"],
                    row["canonical_message_id"],
                    row["provider_kind"],
                    row["remote_folder"],
                    row["uidvalidity"],
                    row["remote_uid"],
                    row["provider_message_id"],
                    row["canonical_sha256"],
                    row["target_fingerprint_sha256"],
                    created,
                    row["id"],
                ),
            )
        insert_audit_event(
            db,
            actor="mailarchive.remote_mutation",
            event_type="remote_delete.production.started",
            result="started",
            details_json=json.dumps(
                {"source_plan_run_id": source_plan_run_id, "selected": len(rows)}, sort_keys=True
            ),
        )
        db.commit()
    _execute_serial(config, production_run_id, adapter)
    return production_run_id


def _execute_serial(config: AppConfig, run_id: int, adapter: RemoteMutationAdapter) -> None:
    while True:
        with connect(config.database.path) as db:
            row = db.execute(
                "SELECT * FROM remote_mutations WHERE mutation_run_id=? AND status='planned' ORDER BY id LIMIT 1",
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
            db.commit()
        try:
            status = _record_outcome(config, run_id, int(row["id"]), target, adapter.delete(target))
        except Exception:
            status = _record_outcome(
                config,
                run_id,
                int(row["id"]),
                target,
                MutationResult("outcome-unknown"),
                internal_error_code="ADAPTER_EXCEPTION",
            )
        if status != "succeeded":
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
    config: AppConfig,
    run_id: int,
    mutation_id: int,
    target: DeletionTarget,
    result: MutationResult,
    *,
    internal_error_code: str | None = None,
) -> str:
    status, summary, error_code = _normalize_result(result, internal_error_code=internal_error_code)
    with connect(config.database.path) as db:
        now = utc_now()
        db.execute(
            """UPDATE remote_mutations SET status=?,completed_at=?,provider_response_summary=?,
            error_code=? WHERE id=?""",
            (status, now, summary, error_code, mutation_id),
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
    return status
