# ruff: noqa: E501

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from mailarchive.classification import ClassificationResult, apply_classification
from mailarchive.config import load_config
from mailarchive.db import account_id, connect, initialize
from mailarchive.ingest import ingest_bytes
from mailarchive.models import AppConfig
from mailarchive.remote_mutation import (
    DeletionTarget,
    MutationResult,
    execute_fake,
    plan_dry_run,
)


class FakeAdapter:
    def __init__(self, outcome: MutationResult) -> None:
        self.outcome = outcome
        self.calls: list[DeletionTarget] = []

    def delete(self, target: DeletionTarget) -> MutationResult:
        self.calls.append(target)
        return self.outcome


def eligible_remote(config_file: Path) -> tuple[AppConfig, str]:
    config = load_config(config_file)
    initialize(config.database.path, config.accounts)
    canonical = apply_classification(
        config,
        ingest_bytes(config, b"From: test\r\n\r\nbody", "test").canonical_message,
        ClassificationResult("ham", None, "fixture", "pytest"),
    )
    now = datetime(2026, 8, 13, tzinfo=UTC).isoformat()
    with connect(config.database.path) as db:
        aid = account_id(db, "test")
        assert aid is not None
        db.execute("UPDATE canonical_messages SET archived_at=? WHERE id=?", ("2025-08-13T00:00:00+00:00", canonical.id))
        db.execute("""INSERT INTO remote_messages(id,account_id,provider_kind,remote_folder,uidvalidity,
        remote_uid,first_seen_at,last_seen_at,remote_present,identity_confidence)
        VALUES('remote',?,'imap','INBOX',7,9,?,?,1,'proven')""", (aid, now, now))
        db.execute("INSERT INTO remote_canonical_links VALUES('remote',?,'fixture',?)", (canonical.id, now))
        for name in ("one", "two"):
            db.execute("""INSERT INTO backup_repositories(name,kind,repository_ref,repository_identity,enabled,
            encryption_mode,verification_policy,created_at,updated_at) VALUES(?,'borg',?,?,1,'none',
            'borg-archive-data-v1',?,?)""", (name, f"/tmp/{name}", name, now, now))
            repository_id = db.execute("SELECT id FROM backup_repositories WHERE name=?", (name,)).fetchone()[0]
            run_id = f"run-{name}"
            db.execute("""INSERT INTO backup_runs(id,repository_id,started_at,completed_at,status,archive_name,
            verification_status,verified_at) VALUES(?,?,?,?,'succeeded',?,'verified',?)""", (run_id, repository_id, now, now, run_id, now))
            db.execute("INSERT INTO message_backup_evidence VALUES(?,?,1,1,?)", (canonical.id, run_id, now))
        db.commit()
    return config, canonical.id


def test_dry_run_anchors_fresh_evaluation_and_exact_imap_target(config_file: Path) -> None:
    config, _ = eligible_remote(config_file)
    plan = plan_dry_run(config)
    assert plan["candidate_eligible"] and plan["planned"] and plan["dry_run"]
    assert plan["production_execution_authorized"] is False
    with connect(config.database.path) as db:
        row = db.execute("SELECT * FROM remote_mutations").fetchone()
        assert row["remote_folder"] == "INBOX" and row["uidvalidity"] == 7 and row["remote_uid"] == 9
        assert len(row["target_fingerprint_sha256"]) == 64
        assert row["deletion_evaluation_id"] is not None


def test_fake_success_persists_result_and_only_changes_remote_observation(config_file: Path) -> None:
    config, canonical_id = eligible_remote(config_file)
    run_id = int(cast(int, plan_dry_run(config)["run_id"]))
    adapter = FakeAdapter(MutationResult("success-confirmed", confirmed_absent=True, summary="absent"))
    execute_fake(config, run_id, adapter)
    assert len(adapter.calls) == 1
    with connect(config.database.path) as db:
        assert db.execute("SELECT status FROM remote_mutations").fetchone()[0] == "succeeded"
        assert db.execute("SELECT remote_present FROM remote_messages").fetchone()[0] == 0
        assert db.execute("SELECT sha256 FROM canonical_messages WHERE id=?", (canonical_id,)).fetchone() is not None


def test_fake_unknown_halts_and_does_not_claim_absence(config_file: Path) -> None:
    config, _ = eligible_remote(config_file)
    run_id = int(cast(int, plan_dry_run(config)["run_id"]))
    execute_fake(config, run_id, FakeAdapter(MutationResult("outcome-unknown", error_code="TRANSPORT")))
    with connect(config.database.path) as db:
        assert db.execute("SELECT status FROM remote_mutations").fetchone()[0] == "unknown"
        assert db.execute("SELECT remote_present FROM remote_messages").fetchone()[0] == 1
        assert db.execute("SELECT status FROM remote_mutation_runs WHERE id=?", (run_id,)).fetchone()[0] == "halted"


def test_stale_hold_stops_before_fake_call(config_file: Path) -> None:
    config, canonical_id = eligible_remote(config_file)
    run_id = int(cast(int, plan_dry_run(config)["run_id"]))
    with connect(config.database.path) as db:
        db.execute("INSERT INTO retention_controls VALUES(?,1,0,'test hold',?)", (canonical_id, datetime.now(UTC).isoformat()))
        db.commit()
    adapter = FakeAdapter(MutationResult("success-confirmed", confirmed_absent=True))
    execute_fake(config, run_id, adapter)
    assert adapter.calls == []
    with connect(config.database.path) as db:
        assert db.execute("SELECT error_code FROM remote_mutations").fetchone()[0] == "STALE_PLAN"
