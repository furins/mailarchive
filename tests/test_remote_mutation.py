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
    ImapDeletionTarget,
    MutationResult,
    ProviderDeletionTarget,
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


class IdentityFakeAdapter:
    """Disposable in-process provider namespace keyed by exact provider identity."""

    def __init__(self, objects: set[str], outcome: MutationResult | None = None) -> None:
        self.objects = objects
        self.outcome = outcome or MutationResult("success-confirmed", confirmed_absent=True)
        self.calls: list[DeletionTarget] = []

    def delete(self, target: DeletionTarget) -> MutationResult:
        self.calls.append(target)
        assert isinstance(target, ProviderDeletionTarget)
        self.objects.discard(target.provider_message_id)
        return self.outcome


class ImapFakeAdapter:
    """Disposable IMAP namespace keyed precisely by folder, UIDVALIDITY and UID."""

    def __init__(self, objects: set[tuple[str, int, int]]) -> None:
        self.objects = objects
        self.calls: list[ImapDeletionTarget] = []

    def delete(self, target: DeletionTarget) -> MutationResult:
        assert isinstance(target, ImapDeletionTarget)
        self.calls.append(target)
        self.objects.discard((target.remote_folder, target.uidvalidity, target.remote_uid))
        return MutationResult("success-confirmed", confirmed_absent=True)


class UnknownAfterCallAdapter:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.calls: list[DeletionTarget] = []
        self.started_was_committed = False

    def delete(self, target: DeletionTarget) -> MutationResult:
        self.calls.append(target)
        with connect(self.database_path) as db:
            self.started_was_committed = (
                db.execute(
                    "SELECT status FROM remote_mutations WHERE remote_message_id=?",
                    (target.remote_message_id,),
                ).fetchone()[0]
                == "started"
            )
        # Model a failure after an irreversible provider operation may have happened.
        raise ConnectionError("untrusted provider response")


class RunStateAdapter:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.run_state: tuple[object, ...] | None = None
        self.mutation_status: str | None = None

    def delete(self, target: DeletionTarget) -> MutationResult:
        with connect(self.database_path) as db:
            self.run_state = tuple(
                db.execute("SELECT mode,status,completed_at FROM remote_mutation_runs").fetchone()
            )
            self.mutation_status = str(
                db.execute(
                    "SELECT status FROM remote_mutations WHERE remote_message_id=?",
                    (target.remote_message_id,),
                ).fetchone()[0]
            )
        return MutationResult("success-confirmed", confirmed_absent=True)


class RawExceptionAdapter:
    def delete(self, target: DeletionTarget) -> MutationResult:
        raise RuntimeError("token=super-secret RFC822 payload From: private@example.test")


def eligible_remote(
    config_file: Path,
    *,
    provider_kind: str = "imap",
    remote_id: str = "remote",
    provider_message_id: str = "",
    body: bytes = b"body",
    remote_uid: int = 9,
) -> tuple[AppConfig, str]:
    config = load_config(config_file)
    initialize(config.database.path, config.accounts)
    canonical = apply_classification(
        config,
        ingest_bytes(config, b"From: test\r\n\r\n" + body, "test").canonical_message,
        ClassificationResult("ham", None, "fixture", "pytest"),
    )
    now = datetime(2026, 8, 13, tzinfo=UTC).isoformat()
    with connect(config.database.path) as db:
        aid = account_id(db, "test")
        assert aid is not None
        db.execute("UPDATE accounts SET kind=? WHERE id=?", (provider_kind, aid))
        db.execute(
            "UPDATE canonical_messages SET archived_at=? WHERE id=?",
            ("2025-08-13T00:00:00+00:00", canonical.id),
        )
        if provider_kind == "imap":
            db.execute(
                """INSERT INTO remote_messages(id,account_id,provider_kind,remote_folder,uidvalidity,
            remote_uid,first_seen_at,last_seen_at,remote_present,identity_confidence)
            VALUES(? ,?,'imap','INBOX',7,?,?,?,1,'proven')""",
                (remote_id, aid, remote_uid, now, now),
            )
        else:
            db.execute(
                """INSERT INTO remote_messages(id,account_id,provider_kind,provider_message_id,
            first_seen_at,last_seen_at,remote_present,identity_confidence)
            VALUES(?,?,?, ?,?,?,1,'proven')""",
                (remote_id, aid, provider_kind, provider_message_id, now, now),
            )
        db.execute(
            "INSERT INTO remote_canonical_links VALUES(?,?,'fixture',?)",
            (remote_id, canonical.id, now),
        )
        db.execute(
            "INSERT INTO attachments(id,sha256,size_bytes,content_path,first_seen_at) VALUES(?,?,?,?,?)",
            (canonical.sha256, canonical.sha256, 1, f"/tmp/{canonical.sha256}", now),
        )
        db.execute(
            "INSERT INTO message_attachments VALUES(?,?,?,?,?,?)",
            (
                canonical.id,
                canonical.sha256,
                0,
                "fixture.bin",
                "attachment",
                "application/octet-stream",
            ),
        )
        db.execute(
            """INSERT INTO attachment_extractions(canonical_message_id,source_sha256,status,
            attachment_count,extracted_at,last_error_kind,updated_at) VALUES(?,?,'success',1,?,NULL,?)""",
            (canonical.id, canonical.sha256, now, now),
        )
        for name in ("one", "two"):
            db.execute(
                """INSERT OR IGNORE INTO backup_repositories(name,kind,repository_ref,repository_identity,enabled,
            encryption_mode,verification_policy,created_at,updated_at) VALUES(?,'borg',?,?,1,'none',
            'borg-archive-data-v1',?,?)""",
                (name, f"/tmp/{name}", name, now, now),
            )
            repository_id = db.execute(
                "SELECT id FROM backup_repositories WHERE name=?", (name,)
            ).fetchone()[0]
            run_id = f"run-{name}"
            db.execute(
                """INSERT OR IGNORE INTO backup_runs(id,repository_id,started_at,completed_at,status,archive_name,
            verification_status,verified_at) VALUES(?,?,?,?,'succeeded',?,'verified',?)""",
                (run_id, repository_id, now, now, run_id, now),
            )
            db.execute(
                "INSERT INTO message_backup_evidence VALUES(?,?,1,1,?)", (canonical.id, run_id, now)
            )
        db.commit()
    return config, canonical.id


def test_dry_run_anchors_fresh_evaluation_and_exact_imap_target(config_file: Path) -> None:
    config, _ = eligible_remote(config_file)
    plan = plan_dry_run(config)
    assert plan["candidate_eligible"] and plan["planned"] and plan["dry_run"]
    assert plan["production_execution_authorized"] is False
    with connect(config.database.path) as db:
        row = db.execute("SELECT * FROM remote_mutations").fetchone()
        assert (
            row["remote_folder"] == "INBOX" and row["uidvalidity"] == 7 and row["remote_uid"] == 9
        )
        assert len(row["target_fingerprint_sha256"]) == 64
        assert row["deletion_evaluation_id"] is not None


def test_fake_success_persists_result_and_only_changes_remote_observation(
    config_file: Path,
) -> None:
    config, canonical_id = eligible_remote(config_file)
    run_id = int(cast(int, plan_dry_run(config)["run_id"]))
    adapter = FakeAdapter(
        MutationResult("success-confirmed", confirmed_absent=True, summary="absent")
    )
    execute_fake(config, run_id, adapter)
    assert len(adapter.calls) == 1
    with connect(config.database.path) as db:
        assert db.execute("SELECT status FROM remote_mutations").fetchone()[0] == "succeeded"
        assert db.execute("SELECT remote_present FROM remote_messages").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT sha256 FROM canonical_messages WHERE id=?", (canonical_id,)
            ).fetchone()
            is not None
        )


def test_imap_fake_deletes_only_exact_namespace_identity_and_preserves_local_state(
    config_file: Path,
) -> None:
    config, canonical_id = eligible_remote(config_file)
    with connect(config.database.path) as db:
        before = db.execute(
            "SELECT local_path FROM canonical_messages WHERE id=?", (canonical_id,)
        ).fetchone()
        attachment_count = db.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0]
        evidence_count = db.execute("SELECT COUNT(*) FROM message_backup_evidence").fetchone()[0]
    assert before is not None
    bytes_before = Path(str(before["local_path"])).read_bytes()
    run_id = int(cast(int, plan_dry_run(config)["run_id"]))
    fake = ImapFakeAdapter({("INBOX", 7, 9), ("INBOX", 7, 10)})
    execute_fake(config, run_id, fake)
    assert fake.objects == {("INBOX", 7, 10)}
    assert fake.calls[0].remote_folder == "INBOX"
    assert fake.calls[0].uidvalidity == 7
    assert fake.calls[0].remote_uid == 9
    with connect(config.database.path) as db:
        assert (
            db.execute("SELECT remote_present FROM remote_messages WHERE id='remote'").fetchone()[0]
            == 0
        )
        assert (
            db.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == attachment_count
        )
        assert (
            db.execute("SELECT COUNT(*) FROM message_backup_evidence").fetchone()[0]
            == evidence_count
        )
    assert Path(str(before["local_path"])).read_bytes() == bytes_before


def test_imap_changed_uidvalidity_is_stale_before_fake_call(config_file: Path) -> None:
    config, _ = eligible_remote(config_file)
    run_id = int(cast(int, plan_dry_run(config)["run_id"]))
    with connect(config.database.path) as db:
        db.execute("UPDATE remote_messages SET uidvalidity=8 WHERE id='remote'")
        db.commit()
    fake = ImapFakeAdapter({("INBOX", 7, 9), ("INBOX", 7, 10)})
    execute_fake(config, run_id, fake)
    assert fake.calls == []
    assert fake.objects == {("INBOX", 7, 9), ("INBOX", 7, 10)}
    with connect(config.database.path) as db:
        assert tuple(db.execute("SELECT status,error_code FROM remote_mutations").fetchone()) == (
            "failed",
            "STALE_PLAN",
        )
        assert (
            db.execute("SELECT status FROM remote_mutation_runs WHERE id=?", (run_id,)).fetchone()[
                0
            ]
            == "halted"
        )


def test_imap_changed_uid_is_stale_before_fake_call(config_file: Path) -> None:
    config, _ = eligible_remote(config_file)
    run_id = int(cast(int, plan_dry_run(config)["run_id"]))
    with connect(config.database.path) as db:
        db.execute("UPDATE remote_messages SET remote_uid=10 WHERE id='remote'")
        db.commit()
    fake = ImapFakeAdapter({("INBOX", 7, 9), ("INBOX", 7, 10)})
    execute_fake(config, run_id, fake)
    assert fake.calls == []
    assert fake.objects == {("INBOX", 7, 9), ("INBOX", 7, 10)}


def test_fake_run_clears_dry_run_completion_before_started_adapter_call(config_file: Path) -> None:
    config, _ = eligible_remote(config_file)
    run_id = int(cast(int, plan_dry_run(config)["run_id"]))
    adapter = RunStateAdapter(config.database.path)
    execute_fake(config, run_id, adapter)
    assert adapter.run_state == ("fake-execute", "planned", None)
    assert adapter.mutation_status == "started"


def test_fake_unknown_halts_and_does_not_claim_absence(config_file: Path) -> None:
    config, _ = eligible_remote(config_file)
    run_id = int(cast(int, plan_dry_run(config)["run_id"]))
    execute_fake(
        config, run_id, FakeAdapter(MutationResult("outcome-unknown", error_code="TRANSPORT"))
    )
    with connect(config.database.path) as db:
        assert db.execute("SELECT status FROM remote_mutations").fetchone()[0] == "unknown"
        assert db.execute("SELECT remote_present FROM remote_messages").fetchone()[0] == 1
        assert (
            db.execute("SELECT status FROM remote_mutation_runs WHERE id=?", (run_id,)).fetchone()[
                0
            ]
            == "halted"
        )


def test_raw_adapter_summary_is_not_persisted(config_file: Path) -> None:
    config, _ = eligible_remote(config_file)
    run_id = int(cast(int, plan_dry_run(config)["run_id"]))
    raw = "raw provider response token=abc MIME payload" * 20
    execute_fake(
        config,
        run_id,
        FakeAdapter(
            MutationResult(
                "failure-confirmed-no-mutation", summary=raw, error_code="PROVIDER_REJECTED"
            )
        ),
    )
    with connect(config.database.path) as db:
        row = db.execute(
            "SELECT provider_response_summary,error_code FROM remote_mutations"
        ).fetchone()
        assert tuple(row) == ("confirmed-no-mutation", "PROVIDER_REJECTED")
        assert raw not in str(row["provider_response_summary"])


def test_invalid_adapter_result_is_unknown_and_raw_exception_is_not_persisted(
    config_file: Path,
) -> None:
    config, _ = eligible_remote(config_file)
    run_id = int(cast(int, plan_dry_run(config)["run_id"]))
    execute_fake(
        config, run_id, FakeAdapter(MutationResult("bad-outcome", summary="raw", error_code="BAD"))
    )
    with connect(config.database.path) as db:
        row = db.execute(
            "SELECT status,provider_response_summary,error_code FROM remote_mutations"
        ).fetchone()
        assert tuple(row) == ("unknown", "outcome-uncertain", "INVALID_ADAPTER_RESULT")
        assert (
            db.execute("SELECT status FROM remote_mutation_runs WHERE id=?", (run_id,)).fetchone()[
                0
            ]
            == "halted"
        )
    config, _ = eligible_remote(
        config_file, remote_id="exception-remote", body=b"exception", remote_uid=10
    )
    run_id = int(cast(int, plan_dry_run(config)["run_id"]))
    execute_fake(config, run_id, RawExceptionAdapter())
    with connect(config.database.path) as db:
        persisted = (
            "\n".join(
                str(value)
                for row in db.execute(
                    "SELECT provider_response_summary,error_code FROM remote_mutations"
                ).fetchall()
                for value in row
            )
            + "\n"
            + "\n".join(str(row[0]) for row in db.execute("SELECT details_json FROM audit_events"))
        )
        assert "super-secret" not in persisted
        assert "private@example.test" not in persisted


def test_exception_after_fake_call_is_unknown_started_first_and_stops_later_rows(
    config_file: Path,
) -> None:
    config, _ = eligible_remote(config_file, remote_id="first", body=b"first")
    eligible_remote(config_file, remote_id="second", body=b"second", remote_uid=10)
    run_id = int(cast(int, plan_dry_run(config)["run_id"]))
    adapter = UnknownAfterCallAdapter(config.database.path)
    execute_fake(config, run_id, adapter)
    assert adapter.started_was_committed is True
    assert [target.remote_message_id for target in adapter.calls] == ["first"]
    with connect(config.database.path) as db:
        rows = db.execute(
            "SELECT remote_message_id,status,completed_at,error_code FROM remote_mutations ORDER BY id"
        ).fetchall()
        assert tuple(rows[0]) == ("first", "unknown", rows[0][2], "ADAPTER_EXCEPTION")
        assert rows[0][2] is not None
        assert tuple(rows[1][1:]) == ("dry-run", None, None)
        assert (
            db.execute("SELECT remote_present FROM remote_messages WHERE id='first'").fetchone()[0]
            == 1
        )


def test_stale_hold_stops_before_fake_call(config_file: Path) -> None:
    config, canonical_id = eligible_remote(config_file)
    run_id = int(cast(int, plan_dry_run(config)["run_id"]))
    with connect(config.database.path) as db:
        db.execute(
            "INSERT INTO retention_controls VALUES(?,1,0,'test hold',?)",
            (canonical_id, datetime.now(UTC).isoformat()),
        )
        db.commit()
    adapter = FakeAdapter(MutationResult("success-confirmed", confirmed_absent=True))
    execute_fake(config, run_id, adapter)
    assert adapter.calls == []
    with connect(config.database.path) as db:
        assert db.execute("SELECT error_code FROM remote_mutations").fetchone()[0] == "STALE_PLAN"


def test_gmail_fake_deletes_only_exact_message_id_and_preserves_local_state(
    config_file: Path,
) -> None:
    config, canonical_id = eligible_remote(
        config_file, provider_kind="gmail", remote_id="gmail-remote", provider_message_id="gmail-a"
    )
    with connect(config.database.path) as db:
        before = db.execute(
            "SELECT sha256,local_path FROM canonical_messages WHERE id=?", (canonical_id,)
        ).fetchone()
        attachment_count = db.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0]
        evidence_count = db.execute("SELECT COUNT(*) FROM message_backup_evidence").fetchone()[0]
    assert before is not None
    bytes_before = Path(str(before["local_path"])).read_bytes()
    run_id = int(cast(int, plan_dry_run(config)["run_id"]))
    fake = IdentityFakeAdapter({"gmail-a", "gmail-b"})
    execute_fake(config, run_id, fake)
    assert fake.objects == {"gmail-b"}
    assert fake.calls[0].provider_kind == "gmail"
    with connect(config.database.path) as db:
        assert db.execute("SELECT remote_present FROM remote_messages").fetchone()[0] == 0
        assert (
            db.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == attachment_count
        )
        assert (
            db.execute("SELECT COUNT(*) FROM message_backup_evidence").fetchone()[0]
            == evidence_count
        )
    assert Path(str(before["local_path"])).read_bytes() == bytes_before


def test_gmail_changed_message_id_is_stale_before_fake_call(config_file: Path) -> None:
    config, _ = eligible_remote(
        config_file, provider_kind="gmail", remote_id="gmail-remote", provider_message_id="gmail-a"
    )
    run_id = int(cast(int, plan_dry_run(config)["run_id"]))
    with connect(config.database.path) as db:
        db.execute("UPDATE remote_messages SET provider_message_id='gmail-changed'")
        db.commit()
    fake = IdentityFakeAdapter({"gmail-a", "gmail-changed"})
    execute_fake(config, run_id, fake)
    assert fake.calls == []
    with connect(config.database.path) as db:
        row = db.execute("SELECT status,error_code FROM remote_mutations").fetchone()
        assert tuple(row) == ("failed", "STALE_PLAN")
        assert (
            db.execute("SELECT status FROM remote_mutation_runs WHERE id=?", (run_id,)).fetchone()[
                0
            ]
            == "halted"
        )


def test_pop3_fake_deletes_only_exact_uidl_and_preserves_local_state(config_file: Path) -> None:
    config, canonical_id = eligible_remote(
        config_file, provider_kind="pop3", remote_id="pop3-remote", provider_message_id="uidl-a"
    )
    with connect(config.database.path) as db:
        before = db.execute(
            "SELECT local_path FROM canonical_messages WHERE id=?", (canonical_id,)
        ).fetchone()
        attachment_count = db.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0]
        evidence_count = db.execute("SELECT COUNT(*) FROM message_backup_evidence").fetchone()[0]
    assert before is not None
    bytes_before = Path(str(before["local_path"])).read_bytes()
    run_id = int(cast(int, plan_dry_run(config)["run_id"]))
    fake = IdentityFakeAdapter({"uidl-a", "uidl-b"})
    execute_fake(config, run_id, fake)
    assert fake.objects == {"uidl-b"}
    assert fake.calls[0].provider_kind == "pop3"
    with connect(config.database.path) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == attachment_count
        )
        assert (
            db.execute("SELECT COUNT(*) FROM message_backup_evidence").fetchone()[0]
            == evidence_count
        )
    assert Path(str(before["local_path"])).read_bytes() == bytes_before


def test_pop3_changed_uidl_is_stale_before_fake_call(config_file: Path) -> None:
    config, _ = eligible_remote(
        config_file, provider_kind="pop3", remote_id="pop3-remote", provider_message_id="uidl-a"
    )
    run_id = int(cast(int, plan_dry_run(config)["run_id"]))
    with connect(config.database.path) as db:
        db.execute("UPDATE remote_messages SET provider_message_id='uidl-changed'")
        db.commit()
    fake = IdentityFakeAdapter({"uidl-a", "uidl-changed"})
    execute_fake(config, run_id, fake)
    assert fake.calls == []
    with connect(config.database.path) as db:
        assert db.execute("SELECT error_code FROM remote_mutations").fetchone()[0] == "STALE_PLAN"


def test_m11_production_sources_expose_no_provider_write_path() -> None:
    root = Path(__file__).parents[1]
    mutation_source = (root / "src/mailarchive/remote_mutation.py").read_text(encoding="utf-8")
    cli_source = (root / "src/mailarchive/cli.py").read_text(encoding="utf-8")
    gmail_source = (root / "src/mailarchive/gmail.py").read_text(encoding="utf-8")
    for forbidden in (
        "STORE",
        "EXPUNGE",
        "UID EXPUNGE",
        "DELE",
        "users.messages.delete",
        "users.messages.trash",
        "users.threads.delete",
        "gmail.modify",
    ):
        assert forbidden not in mutation_source
    assert "--execute" not in cli_source
    assert "gmail.readonly" in gmail_source
