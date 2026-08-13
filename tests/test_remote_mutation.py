# ruff: noqa: E501

from __future__ import annotations

import socketserver
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from mailarchive.classification import ClassificationResult, apply_classification
from mailarchive.config import load_config
from mailarchive.db import account_id, connect, initialize, utc_now
from mailarchive.gmail_mutation import GmailMutationAdapter
from mailarchive.ingest import ingest_bytes
from mailarchive.models import AppConfig
from mailarchive.pop3_mutation import Pop3MutationAdapter
from mailarchive.remote_mutation import (
    DeletionTarget,
    ImapDeletionTarget,
    MutationResult,
    ProductionPlanError,
    ProviderDeletionTarget,
    RemoteMutationAdapter,
    _normalize_result,  # pyright: ignore[reportPrivateUsage]
    execute_fake,
    execute_production_plan,
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
                    "SELECT status FROM remote_mutations WHERE remote_message_id=? ORDER BY dry_run ASC LIMIT 1",
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


class ProductionAdapter:
    def __init__(self, database_path: Path, result: MutationResult) -> None:
        self.database_path = database_path
        self.result = result
        self.calls: list[DeletionTarget] = []
        self.started = False

    def delete(self, target: DeletionTarget) -> MutationResult:
        self.calls.append(target)
        with connect(self.database_path) as db:
            self.started = (
                db.execute(
                    "SELECT status FROM remote_mutations WHERE remote_message_id=? AND dry_run=0",
                    (target.remote_message_id,),
                ).fetchone()[0]
                == "started"
            )
        return self.result


class ProductionFactory:
    def __init__(self, adapter: RemoteMutationAdapter) -> None:
        self.adapter = adapter

    def __call__(self, config: AppConfig, account_name: str) -> RemoteMutationAdapter:
        return self.adapter


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


def production_enabled(config: AppConfig) -> AppConfig:
    return replace(
        config,
        accounts=tuple(
            replace(account, remote_deletion_enabled=True) for account in config.accounts
        ),
    )


class EngineGmail:
    def __init__(
        self,
        database: Path,
        messages: dict[str, bytes],
        *,
        uncertain: bool = False,
        keep_present: bool = False,
    ) -> None:
        self.database = database
        self.messages = messages
        self.uncertain = uncertain
        self.keep_present = keep_present
        self.deletes: list[str] = []
        self.started = False

    def profile(self) -> dict[str, object]:
        return {"emailAddress": "user@example.test"}

    def get_raw(self, message_id: str) -> dict[str, object] | None:
        if self.uncertain and self.deletes:
            raise OSError("unobservable")
        raw = self.messages.get(message_id)
        if raw is None:
            return None
        import base64

        return {"id": message_id, "raw": base64.urlsafe_b64encode(raw).decode().rstrip("=")}

    def delete_message_once(self, message_id: str) -> None:
        with connect(self.database) as db:
            self.started = (
                db.execute("SELECT status FROM remote_mutations WHERE dry_run=0").fetchone()[0]
                == "started"
            )
        self.deletes.append(message_id)
        if not self.uncertain and not self.keep_present:
            self.messages.pop(message_id, None)


def gmail_engine_config(config_file: Path, tmp_path: Path) -> None:
    import yaml

    values = yaml.safe_load(config_file.read_text())
    readonly = tmp_path / "readonly.json"
    readonly.write_text('{"scopes":["https://www.googleapis.com/auth/gmail.readonly"]}')
    readonly.chmod(0o600)
    token = tmp_path / "delete.json"
    token.write_text(
        '{"token":"x","refresh_token":"y","token_uri":"https://x","client_id":"x",'
        '"client_secret":"y","scopes":["https://mail.google.com/"],'
        '"expiry":"2099-01-01T00:00:00Z"}'
    )
    token.chmod(0o600)
    values["accounts"]["test"].update(
        {
            "kind": "gmail",
            "remote_deletion_enabled": True,
            "config_ref": f"file:{readonly}",
        }
    )
    values["accounts"]["test"]["gmail"] = {
        "account_email": "user@example.test",
        "oauth_client_secret_file": "/tmp/client.json",
        "remote_delete_token_file": str(token),
    }
    config_file.write_text(yaml.safe_dump(values))


def gmail_factory(fake: EngineGmail) -> Callable[[AppConfig, str], RemoteMutationAdapter]:
    def factory(config: AppConfig, name: str) -> RemoteMutationAdapter:
        return GmailMutationAdapter(config, name, transport_factory=lambda _credentials: fake)

    return factory


class EnginePop3Server:
    def __init__(
        self, database: Path, messages: dict[str, bytes], *, mode: str = "success"
    ) -> None:
        self.database, self.messages, self.mode = database, messages, mode
        self.commands: list[str] = []
        self.started = False
        self.sessions = 0
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _handler(self):
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                pending: str | None = None
                outer.sessions += 1
                if outer.mode == "unknown" and outer.sessions > 1:
                    return
                self.wfile.write(b"+OK test\r\n")
                while line := self.rfile.readline().decode("ascii").strip():
                    command, *arguments = line.split()
                    outer.commands.append(command)
                    if command in {"USER", "PASS"}:
                        self.wfile.write(b"+OK\r\n")
                    elif command == "UIDL":
                        self.wfile.write(b"+OK\r\n")
                        for number, uidl in enumerate(outer.messages, start=1):
                            self.wfile.write(f"{number} {uidl}\r\n".encode())
                        self.wfile.write(b".\r\n")
                    elif command == "RETR":
                        raw = outer.messages[list(outer.messages)[int(arguments[0]) - 1]]
                        self.wfile.write(b"+OK\r\n" + raw + b".\r\n")
                    elif command == "DELE":
                        with connect(outer.database) as db:
                            outer.started = (
                                db.execute(
                                    "SELECT status FROM remote_mutations WHERE dry_run=0"
                                ).fetchone()[0]
                                == "started"
                            )
                        pending = list(outer.messages)[int(arguments[0]) - 1]
                        if outer.mode in {"lost", "unknown"}:
                            return
                        self.wfile.write(b"+OK\r\n")
                    elif command == "QUIT":
                        if pending is not None and outer.mode == "success":
                            del outer.messages[pending]
                        self.wfile.write(b"+OK\r\n")
                        return

        return Handler

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def pop3_engine_config(config_file: Path, port: int) -> None:
    import yaml

    values = yaml.safe_load(config_file.read_text())
    values["accounts"]["test"].update(
        {"kind": "pop3", "remote_deletion_enabled": True, "config_ref": "env:POP_ENGINE_PASSWORD"}
    )
    values["accounts"]["test"]["pop3"] = {
        "host": "127.0.0.1",
        "port": port,
        "username": "test",
        "tls_mode": "INSECURE_LOOPBACK",
        "connection_timeout_seconds": 5,
    }
    config_file.write_text(yaml.safe_dump(values))


def test_gmail_adapter_executes_through_production_engine_preserving_local_graph(
    config_file: Path, tmp_path: Path
) -> None:
    gmail_engine_config(config_file, tmp_path)
    config, canonical = eligible_remote(
        config_file,
        provider_kind="gmail",
        remote_id="gmail-target",
        provider_message_id="target",
        body=b"target",
    )
    source = int(cast(int, plan_dry_run(config, account="test")["run_id"]))
    with connect(config.database.path) as db:
        before_attachments = [tuple(row) for row in db.execute("SELECT * FROM message_attachments")]
        before_evidence = [
            tuple(row) for row in db.execute("SELECT * FROM message_backup_evidence")
        ]
        canonical_path = Path(
            db.execute(
                "SELECT local_path FROM canonical_messages WHERE id=?", (canonical,)
            ).fetchone()[0]
        )
        bytes_before = canonical_path.read_bytes()
    fake = EngineGmail(config.database.path, {"target": bytes_before, "other": b"other"})
    run = execute_production_plan(config, source, "test", adapter_factory=gmail_factory(fake))
    assert fake.started and fake.deletes == ["target"] and "other" in fake.messages
    with connect(config.database.path) as db:
        assert (
            db.execute("SELECT status FROM remote_mutation_runs WHERE id=?", (source,)).fetchone()[
                0
            ]
            == "completed"
        )
        mutation = db.execute(
            "SELECT status,source_plan_mutation_id FROM remote_mutations WHERE mutation_run_id=?",
            (run,),
        ).fetchone()
        assert tuple(mutation) == ("succeeded", mutation[1]) and mutation[1] is not None
        assert (
            db.execute(
                "SELECT remote_present FROM remote_messages WHERE id='gmail-target'"
            ).fetchone()[0]
            == 0
        )
        assert [
            tuple(row) for row in db.execute("SELECT * FROM message_attachments")
        ] == before_attachments
        assert [
            tuple(row) for row in db.execute("SELECT * FROM message_backup_evidence")
        ] == before_evidence
    assert canonical_path.read_bytes() == bytes_before


def test_pop3_adapter_executes_through_production_engine_preserving_local_graph(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    database = Path(yaml.safe_load(config_file.read_text())["database"]["path"])
    server = EnginePop3Server(
        database, {"U1": b"From: test\r\n\r\ntarget\r\n", "U2": b"unrelated\r\n"}
    )
    try:
        pop3_engine_config(config_file, server.server.server_address[1])
        monkeypatch.setenv("POP_ENGINE_PASSWORD", "local-only")
        config, canonical = eligible_remote(
            config_file,
            provider_kind="pop3",
            remote_id="pop3-target",
            provider_message_id="U1",
            body=b"target\r\n",
        )
        source = int(cast(int, plan_dry_run(config, account="test")["run_id"]))
        with connect(config.database.path) as db:
            attachments = [tuple(row) for row in db.execute("SELECT * FROM message_attachments")]
            evidence = [tuple(row) for row in db.execute("SELECT * FROM message_backup_evidence")]
            path = Path(
                db.execute(
                    "SELECT local_path FROM canonical_messages WHERE id=?", (canonical,)
                ).fetchone()[0]
            )
            before = path.read_bytes()
        production = execute_production_plan(
            config,
            source,
            "test",
            adapter_factory=lambda cfg, name: Pop3MutationAdapter(cfg, name),
        )
        assert server.started, server.commands
        assert "U1" not in server.messages and server.messages["U2"] == b"unrelated\r\n"
        with connect(config.database.path) as db:
            row = db.execute(
                "SELECT status,source_plan_mutation_id FROM remote_mutations WHERE mutation_run_id=?",
                (production,),
            ).fetchone()
            assert tuple(row)[0] == "succeeded" and row[1] is not None
            assert (
                db.execute(
                    "SELECT remote_present FROM remote_messages WHERE id='pop3-target'"
                ).fetchone()[0]
                == 0
            )
            assert [
                tuple(row) for row in db.execute("SELECT * FROM message_attachments")
            ] == attachments
            assert [
                tuple(row) for row in db.execute("SELECT * FROM message_backup_evidence")
            ] == evidence
        assert path.read_bytes() == before
    finally:
        server.close()


@pytest.mark.parametrize(("mode", "expected"), [("failure", "failed"), ("unknown", "unknown")])
def test_pop3_adapter_engine_halts_on_failure_or_unknown(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, mode: str, expected: str
) -> None:
    import yaml

    database = Path(yaml.safe_load(config_file.read_text())["database"]["path"])
    server = EnginePop3Server(
        database,
        {
            "U1": b"From: test\r\n\r\nfirst\r\n",
            "U2": b"From: test\r\n\r\nlater\r\n",
        },
        mode=mode,
    )
    try:
        pop3_engine_config(config_file, server.server.server_address[1])
        monkeypatch.setenv("POP_ENGINE_PASSWORD", "local-only")
        config, _ = eligible_remote(
            config_file, provider_kind="pop3", remote_id="a", provider_message_id="U1", body=b"first\r\n"
        )
        eligible_remote(
            config_file, provider_kind="pop3", remote_id="b", provider_message_id="U2", body=b"later\r\n"
        )
        source = int(cast(int, plan_dry_run(config, account="test")["run_id"]))
        production = execute_production_plan(
            config, source, "test", adapter_factory=lambda cfg, name: Pop3MutationAdapter(cfg, name)
        )
        assert server.commands.count("DELE") == 1
        with connect(config.database.path) as db:
            rows = db.execute(
                "SELECT remote_message_id,status FROM remote_mutations WHERE mutation_run_id=? ORDER BY remote_message_id",
                (production,),
            ).fetchall()
            assert [tuple(row) for row in rows] == [("a", expected), ("b", "planned")]
            assert db.execute("SELECT remote_present FROM remote_messages WHERE id='a'").fetchone()[0] == 1
        with pytest.raises(ProductionPlanError):
            execute_production_plan(
                config, source, "test", adapter_factory=lambda cfg, name: Pop3MutationAdapter(cfg, name)
            )
        if mode == "unknown":
            fresh = int(cast(int, plan_dry_run(config, account="test")["run_id"]))
            with pytest.raises(ProductionPlanError, match="unresolved"):
                execute_production_plan(
                    config, fresh, "test", adapter_factory=lambda cfg, name: Pop3MutationAdapter(cfg, name)
                )
        assert server.commands.count("DELE") == 1
    finally:
        server.close()


@pytest.mark.parametrize(("uncertain", "expected"), [(False, "failed"), (True, "unknown")])
def test_gmail_adapter_engine_halts_failure_or_unknown(
    config_file: Path, tmp_path: Path, uncertain: bool, expected: str
) -> None:
    gmail_engine_config(config_file, tmp_path)
    config, _ = eligible_remote(
        config_file,
        provider_kind="gmail",
        remote_id="a-target",
        provider_message_id="target",
        body=b"target",
    )
    _config, _ = eligible_remote(
        config_file,
        provider_kind="gmail",
        remote_id="b-later",
        provider_message_id="later",
        body=b"later",
    )
    source = int(cast(int, plan_dry_run(config, account="test")["run_id"]))
    fake = EngineGmail(
        config.database.path,
        {"target": b"From: test\r\n\r\ntarget", "later": b"From: test\r\n\r\nlater"},
        uncertain=uncertain,
        keep_present=not uncertain,
    )
    production = execute_production_plan(
        config, source, "test", adapter_factory=gmail_factory(fake)
    )
    assert fake.started and fake.deletes == ["target"]
    with connect(config.database.path) as db:
        assert (
            db.execute(
                "SELECT status FROM remote_mutation_runs WHERE id=?", (production,)
            ).fetchone()[0]
            == "halted"
        )
        rows = db.execute(
            "SELECT remote_message_id,status FROM remote_mutations WHERE mutation_run_id=? ORDER BY remote_message_id",
            (production,),
        ).fetchall()
        assert [tuple(row) for row in rows] == [("a-target", expected), ("b-later", "planned")]
        assert (
            db.execute("SELECT remote_present FROM remote_messages WHERE id='a-target'").fetchone()[
                0
            ]
            == 1
        )
    with pytest.raises(ProductionPlanError):
        execute_production_plan(config, source, "test", adapter_factory=gmail_factory(fake))
    if uncertain:
        fresh = int(cast(int, plan_dry_run(config, account="test")["run_id"]))
        with pytest.raises(ProductionPlanError, match="unresolved"):
            execute_production_plan(config, fresh, "test", adapter_factory=gmail_factory(fake))
    assert fake.deletes == ["target"]


def test_production_executes_only_filtered_source_plan_and_preserves_history(
    config_file: Path,
) -> None:
    config, _ = eligible_remote(config_file)
    config = production_enabled(config)
    source_id = int(cast(int, plan_dry_run(config, account="test")["run_id"]))
    adapter = ProductionAdapter(
        config.database.path, MutationResult("success-confirmed", confirmed_absent=True)
    )
    production_id = execute_production_plan(
        config, source_id, "test", adapter_factory=ProductionFactory(adapter)
    )
    assert adapter.started is True and len(adapter.calls) == 1
    with connect(config.database.path) as db:
        source = db.execute(
            "SELECT mode,status FROM remote_mutation_runs WHERE id=?", (source_id,)
        ).fetchone()
        produced = db.execute(
            "SELECT * FROM remote_mutation_runs WHERE id=?", (production_id,)
        ).fetchone()
        mutation = db.execute(
            "SELECT * FROM remote_mutations WHERE mutation_run_id=?", (production_id,)
        ).fetchone()
        assert tuple(source) == ("dry-run", "completed")
        assert (
            produced["mode"] == "production-execute" and produced["source_plan_run_id"] == source_id
        )
        assert mutation["dry_run"] == 0 and mutation["source_plan_mutation_id"] is not None
        assert db.execute("SELECT remote_present FROM remote_messages").fetchone()[0] == 0
    with pytest.raises(ProductionPlanError, match="source plan"):
        execute_production_plan(
            config, source_id, "test", adapter_factory=ProductionFactory(adapter)
        )


@pytest.mark.parametrize("change", ("legal_hold", "keep_online", "remote_present"))
def test_production_stale_or_disabled_gate_never_calls_adapter(
    config_file: Path, change: str
) -> None:
    config, canonical_id = eligible_remote(config_file)
    config = production_enabled(config)
    source_id = int(cast(int, plan_dry_run(config, account="test")["run_id"]))
    with connect(config.database.path) as db:
        if change in {"legal_hold", "keep_online"}:
            db.execute(
                f"INSERT INTO retention_controls VALUES(?,{1 if change == 'keep_online' else 0},{1 if change == 'legal_hold' else 0},'test',?)",
                (canonical_id, utc_now()),
            )
        else:
            db.execute("UPDATE remote_messages SET remote_present=0")
        db.commit()
    adapter = ProductionAdapter(
        config.database.path, MutationResult("success-confirmed", confirmed_absent=True)
    )
    with pytest.raises(ProductionPlanError):
        execute_production_plan(
            config, source_id, "test", adapter_factory=ProductionFactory(adapter)
        )
    assert adapter.calls == []


def test_production_failure_halts_and_does_not_call_later_target(config_file: Path) -> None:
    config, _ = eligible_remote(config_file, remote_id="first", body=b"first")
    eligible_remote(config_file, remote_id="second", body=b"second", remote_uid=10)
    config = production_enabled(config)
    source_id = int(cast(int, plan_dry_run(config, account="test")["run_id"]))
    adapter = ProductionAdapter(
        config.database.path,
        MutationResult("failure-confirmed-no-mutation", error_code="PROVIDER_REJECTED"),
    )
    production_id = execute_production_plan(
        config, source_id, "test", adapter_factory=ProductionFactory(adapter)
    )
    assert [target.remote_message_id for target in adapter.calls] == ["first"]
    with connect(config.database.path) as db:
        assert (
            db.execute(
                "SELECT status FROM remote_mutation_runs WHERE id=?", (production_id,)
            ).fetchone()[0]
            == "halted"
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM remote_mutations WHERE mutation_run_id=? AND status='planned'",
                (production_id,),
            ).fetchone()[0]
            == 1
        )


def test_production_unknown_is_started_first_halts_and_blocks_new_plan(config_file: Path) -> None:
    config, _ = eligible_remote(config_file, remote_id="first", body=b"first")
    eligible_remote(config_file, remote_id="second", body=b"second", remote_uid=10)
    config = production_enabled(config)
    source_id = int(cast(int, plan_dry_run(config, account="test")["run_id"]))
    adapter = UnknownAfterCallAdapter(config.database.path)
    production_id = execute_production_plan(
        config, source_id, "test", adapter_factory=ProductionFactory(adapter)
    )
    assert adapter.started_was_committed and [
        target.remote_message_id for target in adapter.calls
    ] == ["first"]
    with connect(config.database.path) as db:
        assert (
            db.execute(
                "SELECT status FROM remote_mutation_runs WHERE id=?", (production_id,)
            ).fetchone()[0]
            == "halted"
        )
        assert (
            db.execute(
                "SELECT error_code FROM remote_mutations WHERE mutation_run_id=? ORDER BY id",
                (production_id,),
            ).fetchone()[0]
            == "ADAPTER_EXCEPTION"
        )
        assert (
            db.execute("SELECT remote_present FROM remote_messages WHERE id='first'").fetchone()[0]
            == 1
        )
    fresh_plan = int(cast(int, plan_dry_run(config, account="test")["run_id"]))
    with pytest.raises(ProductionPlanError, match="unresolved"):
        execute_production_plan(
            config, fresh_plan, "test", adapter_factory=ProductionFactory(adapter)
        )


@pytest.mark.parametrize("field,value", [("remote_uid", 10), ("provider_message_id", "changed")])
def test_production_preflight_rejects_changed_target_without_run(
    config_file: Path, field: str, value: object
) -> None:
    kind = "imap" if field == "remote_uid" else "gmail"
    config, _ = eligible_remote(
        config_file, provider_kind=kind, provider_message_id="gmail-a" if kind == "gmail" else ""
    )
    config = production_enabled(config)
    source_id = int(cast(int, plan_dry_run(config, account="test")["run_id"]))
    with connect(config.database.path) as db:
        db.execute(f"UPDATE remote_messages SET {field}=?", (value,))
        db.commit()
    adapter = ProductionAdapter(config.database.path, MutationResult("success-confirmed", True))
    with pytest.raises(ProductionPlanError, match="fingerprint"):
        execute_production_plan(
            config, source_id, "test", adapter_factory=ProductionFactory(adapter)
        )
    assert adapter.calls == []
    with connect(config.database.path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM remote_mutation_runs WHERE mode='production-execute'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("limit_field", ["max_per_run", "max_per_account"])
def test_production_rejects_current_stricter_limit(config_file: Path, limit_field: str) -> None:
    config, _ = eligible_remote(config_file, remote_id="first", body=b"first")
    eligible_remote(config_file, remote_id="second", body=b"second", remote_uid=10)
    config = production_enabled(config)
    source_id = int(cast(int, plan_dry_run(config, account="test")["run_id"]))
    config = replace(config, remote_deletion=replace(config.remote_deletion, **{limit_field: 1}))
    adapter = ProductionAdapter(config.database.path, MutationResult("success-confirmed", True))
    with pytest.raises(ProductionPlanError, match="safety limit"):
        execute_production_plan(
            config, source_id, "test", adapter_factory=ProductionFactory(adapter)
        )
    assert adapter.calls == []
    with connect(config.database.path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM remote_mutation_runs WHERE mode='production-execute'"
            ).fetchone()[0]
            == 0
        )


def test_default_m12_a_factory_does_not_consume_plan(config_file: Path) -> None:
    config, _ = eligible_remote(config_file)
    config = production_enabled(config)
    source_id = int(cast(int, plan_dry_run(config, account="test")["run_id"]))
    with pytest.raises(ProductionPlanError, match="not implemented"):
        execute_production_plan(config, source_id, "test")
    with connect(config.database.path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM remote_mutation_runs WHERE mode='production-execute'"
            ).fetchone()[0]
            == 0
        )


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
        config,
        run_id,
        FakeAdapter(MutationResult("outcome-unknown", error_code="TRANSPORT_UNKNOWN")),
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


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            MutationResult("success-confirmed", confirmed_absent=True),
            ("succeeded", "confirmed-absent", "NONE"),
        ),
        (
            MutationResult("success-confirmed", confirmed_absent=False),
            ("unknown", "outcome-uncertain", "INVALID_ADAPTER_RESULT"),
        ),
        (
            MutationResult(
                "success-confirmed", confirmed_absent=True, error_code="ADAPTER_EXCEPTION"
            ),
            ("unknown", "outcome-uncertain", "INVALID_ADAPTER_RESULT"),
        ),
        (
            MutationResult(
                "failure-confirmed-no-mutation",
                confirmed_absent=True,
                error_code="PROVIDER_REJECTED",
            ),
            ("unknown", "outcome-uncertain", "INVALID_ADAPTER_RESULT"),
        ),
        (
            MutationResult(
                "outcome-unknown", confirmed_absent=True, error_code="TRANSPORT_UNKNOWN"
            ),
            ("unknown", "outcome-uncertain", "INVALID_ADAPTER_RESULT"),
        ),
        (
            MutationResult("failure-confirmed-no-mutation", error_code="ADAPTER_EXCEPTION"),
            ("unknown", "outcome-uncertain", "INVALID_ADAPTER_RESULT"),
        ),
        (
            MutationResult("outcome-unknown", error_code="STALE_PLAN"),
            ("unknown", "outcome-uncertain", "INVALID_ADAPTER_RESULT"),
        ),
        (
            MutationResult("outcome-unknown", error_code="INVALID_ADAPTER_RESULT"),
            ("unknown", "outcome-uncertain", "INVALID_ADAPTER_RESULT"),
        ),
        (
            MutationResult("failure-confirmed-no-mutation", error_code="TARGET_NOT_FOUND"),
            ("failed", "confirmed-no-mutation", "TARGET_NOT_FOUND"),
        ),
        (
            MutationResult("outcome-unknown", error_code="TRANSPORT_UNKNOWN"),
            ("unknown", "outcome-uncertain", "TRANSPORT_UNKNOWN"),
        ),
    ],
)
def test_mutation_result_coherence_matrix(
    result: MutationResult, expected: tuple[str, str, str]
) -> None:
    assert _normalize_result(result) == expected


def test_invalid_success_result_halts_before_later_fake_mutation(config_file: Path) -> None:
    config, _ = eligible_remote(config_file, remote_id="first", body=b"first")
    eligible_remote(config_file, remote_id="second", body=b"second", remote_uid=10)
    run_id = int(cast(int, plan_dry_run(config)["run_id"]))
    adapter = FakeAdapter(MutationResult("success-confirmed", confirmed_absent=False))
    execute_fake(config, run_id, adapter)
    assert [target.remote_message_id for target in adapter.calls] == ["first"]
    with connect(config.database.path) as db:
        rows = db.execute(
            "SELECT remote_message_id,status,error_code FROM remote_mutations ORDER BY id"
        ).fetchall()
        assert tuple(rows[0]) == ("first", "unknown", "INVALID_ADAPTER_RESULT")
        assert tuple(rows[1][1:]) == ("dry-run", None)
        assert (
            db.execute("SELECT remote_present FROM remote_messages WHERE id='first'").fetchone()[0]
            == 1
        )


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


def test_acquisition_sources_remain_without_provider_write_path() -> None:
    root = Path(__file__).parents[1]
    mutation_source = (root / "src/mailarchive/remote_mutation.py").read_text(encoding="utf-8")
    cli_source = (root / "src/mailarchive/cli.py").read_text(encoding="utf-8")
    gmail_source = (root / "src/mailarchive/gmail.py").read_text(encoding="utf-8")
    for forbidden in (
        'client.uid("STORE"',
        'client.uid("EXPUNGE"',
        'client.uid("DELE"',
        "users.messages.delete",
        "users.messages.trash",
        "users.threads.delete",
        "gmail.modify",
    ):
        assert forbidden not in mutation_source
    assert "--execute" not in cli_source.replace("--execute-plan", "")
    assert "gmail.readonly" in gmail_source
