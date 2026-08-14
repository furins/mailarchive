# ruff: noqa: E501

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

import mailarchive.cli as cli_module
import mailarchive.gmail as gmail_module
from mailarchive.classification import ClassificationResult, apply_classification
from mailarchive.cli import build_parser, main
from mailarchive.config import load_config
from mailarchive.db import account_id, connect, initialize
from mailarchive.ingest import ingest_bytes
from mailarchive.models import AccountConfig


def test_cli_help_works(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
    assert "MailArchive local safety baseline" in capsys.readouterr().out


def test_config_check_exit_status(config_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "check", "--config", str(config_file), "--json"]) == 0
    assert '"valid": true' in capsys.readouterr().out
    with pytest.raises(SystemExit) as result:
        main(["config", "check", "--config", str(config_file.with_name("absent.yaml"))])
    assert result.value.code == 2


def test_remote_delete_is_explicit_dry_run_only() -> None:
    help_text = build_parser().format_help()
    assert "deletion-candidates" in help_text
    assert "remote-delete" in help_text.lower()


def test_gmail_auth_and_auth_delete_use_separate_authorizers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "gmail.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "archive": {"root": str(tmp_path / "archive"), "timezone": "UTC"},
                "database": {"path": str(tmp_path / "state.db")},
                "accounts": {
                    "gmail": {
                        "kind": "gmail",
                        "enabled": True,
                        "remote_retention_days": 365,
                        "required_verified_backups": 2,
                        "config_ref": f"file:{tmp_path / 'readonly.json'}",
                        "gmail": {
                            "account_email": "user@example.test",
                            "oauth_client_secret_file": "/tmp/client.json",
                            "remote_delete_token_file": str(tmp_path / "delete.json"),
                        },
                    }
                },
            }
        )
    )
    calls: list[str] = []

    def readonly_authorize(_account: AccountConfig) -> str:
        calls.append("readonly")
        return "user@example.test"

    def delete_authorize(_account: AccountConfig) -> str:
        calls.append("delete")
        return "user@example.test"

    monkeypatch.setattr(
        cli_module, "authorize", readonly_authorize
    )
    monkeypatch.setattr(cli_module, "authorize_delete", delete_authorize)
    assert main(["gmail", "auth", "--account", "gmail", "--config", str(config_path)]) == 0
    assert calls == ["readonly"]
    assert main(["gmail", "auth-delete", "--account", "gmail", "--config", str(config_path)]) == 0
    assert calls == ["readonly", "delete"]


def test_db_init_and_status(config_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["db", "init", "--config", str(config_file)]) == 0
    assert main(["status", "--config", str(config_file), "--json"]) == 0
    assert '"database_initialized": true' in capsys.readouterr().out


def test_remote_mutations_status_is_local_and_global_status_is_truthful(
    config_file: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("local mutation status must not construct provider observers")

    monkeypatch.setattr(cli_module, "reconcile_production_run", forbidden)
    assert main(["remote-mutations", "status", "--config", str(config_file), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"runs": []}
    assert main(["status", "--config", str(config_file), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["remote_reconciliation_supported"] is True
    assert payload["remote_mutation_supported"] is False
    assert payload["remote_deletion_accounts"] == [{"account": "test", "enabled": False}]


def test_remote_mutations_reconcile_uses_one_exact_run_and_emits_bounded_summary(
    config_file: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []

    def reconcile(_config: object, run_id: int) -> dict[str, object]:
        seen.append(run_id)
        return {
            "run_id": run_id,
            "status": "halted",
            "observed": 1,
            "resolved_absent": 0,
            "resolved_present": 0,
            "unresolved": 1,
            "resumed": False,
        }

    monkeypatch.setattr(cli_module, "reconcile_production_run", reconcile)
    assert (
        main(
            [
                "remote-mutations",
                "reconcile",
                "--run-id",
                "42",
                "--config",
                str(config_file),
                "--json",
            ]
        )
        == 0
    )
    assert seen == [42]
    assert json.loads(capsys.readouterr().out)["resumed"] is False


def test_remote_mutations_status_filters_exact_account_locally(
    config_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    values = yaml.safe_load(config_file.read_text())
    values["accounts"]["account-a"] = values["accounts"].pop("test")
    values["accounts"]["account-b"] = dict(values["accounts"]["account-a"])
    config_file.write_text(yaml.safe_dump(values))
    config = load_config(config_file)
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as db:
        now = datetime.now(UTC).isoformat()
        ids: dict[str, int] = {}
        for account in ("account-a", "account-b"):
            row = db.execute(
                """INSERT INTO remote_mutation_runs(requested_at,completed_at,mode,status,account_filter,
                requested_limit,effective_max_per_run,effective_max_per_account,eligible_count,selected_count,
                skipped_limit_count,policy_version) VALUES(?,?,'dry-run','completed',?,NULL,1,1,0,0,0,'retention-v1')""",
                (now, now, account),
            )
            assert row.lastrowid is not None
            ids[account] = int(row.lastrowid)
        db.commit()
    assert main(["remote-mutations", "status", "--account", "account-a", "--config", str(config_file), "--json"]) == 0
    assert [run["account_filter"] for run in json.loads(capsys.readouterr().out)["runs"]] == ["account-a"]
    with pytest.raises(SystemExit) as error:
        main(["remote-mutations", "status", "--run-id", str(ids["account-b"]), "--account", "account-a", "--config", str(config_file)])
    assert error.value.code == 2


def test_search_scope_is_forwarded_without_lifecycle_query_parsing(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def search(config: object, query: str, *, scope: str) -> list[object]:
        seen.update(query=query, scope=scope)
        return []

    monkeypatch.setattr(cli_module, "search_canonical_messages", search)
    assert (
        main(["search", "tag:quarantine", "--scope", "quarantine", "--config", str(config_file)])
        == 0
    )
    assert seen == {"query": "tag:quarantine", "scope": "quarantine"}


def test_status_and_quarantine_list_are_local_and_use_effective_counts(
    config_file: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(config_file)
    pending = ingest_bytes(config, b"From: a\r\n\r\npending", "test").canonical_message
    spam = apply_classification(
        config,
        ingest_bytes(config, b"From: a\r\n\r\nspam", "test").canonical_message,
        ClassificationResult("spam", 9, "classifier-timeout"),
    )
    apply_classification(
        config, spam, ClassificationResult("suspect", 5, "operator", "manual"), manual=True
    )
    def no_notmuch(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("notmuch")

    monkeypatch.setattr(cli_module, "NotmuchAdapter", no_notmuch)
    assert main(["status", "--config", str(config_file), "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["lifecycle_counts"] == {"pending": 1, "quarantined": 1}
    assert status["quarantine_counts"] == {"suspect": 1}
    assert status["classifier_failure_event_count"] == 1
    assert main(["quarantine", "list", "--config", str(config_file), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [
        {
            "account": "test",
            "canonical_id": spam.id,
            "classification": "suspect",
            "classified_at": rows[0]["classified_at"],
            "local_path": str(spam.local_path),
            "quarantined_at": rows[0]["quarantined_at"],
            "score": 5.0,
        }
    ]
    assert pending.id != spam.id


def test_gmail_status_uninitialized_is_local_and_not_started(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "gmail.yaml"
    database = tmp_path / "state" / "mailarchive.sqlite3"
    config.write_text(
        yaml.safe_dump(
            {
                "archive": {"root": str(tmp_path / "archive"), "timezone": "UTC"},
                "database": {"path": str(database)},
                "accounts": {
                    "gmail": {
                        "kind": "gmail",
                        "enabled": True,
                        "remote_retention_days": 365,
                        "required_verified_backups": 2,
                        "config_ref": f"file:{tmp_path / 'token.json'}",
                        "gmail": {
                            "account_email": "user@example.test",
                            "oauth_client_secret_file": "/tmp/client.json",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert main(["status", "--config", str(config), "--json"]) == 0
    output = capsys.readouterr().out
    assert '"watcher_state": "not-started"' in output and not database.exists()
    assert not (tmp_path / "archive").exists()
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "token.json").exists()


@pytest.mark.parametrize(
    ("mode", "age_seconds", "expected"),
    [
        ("poll", 0, "active"),
        ("poll", 10_000, "stale"),
        ("degraded", 10_000, "stale"),
        ("stopped", 10_000, "stopped"),
    ],
)
def test_gmail_status_is_strictly_local_for_initialized_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    age_seconds: int,
    expected: str,
) -> None:
    config_path = tmp_path / "gmail.yaml"
    database = tmp_path / "state" / "mailarchive.sqlite3"
    config_path.write_text(
        yaml.safe_dump(
            {
                "archive": {"root": str(tmp_path / "archive"), "timezone": "UTC"},
                "database": {"path": str(database)},
                "accounts": {
                    "gmail": {
                        "kind": "gmail",
                        "enabled": True,
                        "remote_retention_days": 365,
                        "required_verified_backups": 2,
                        "config_ref": f"file:{tmp_path / 'token.json'}",
                        "gmail": {
                            "account_email": "user@example.test",
                            "oauth_client_secret_file": "/tmp/client.json",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_config(config_path)
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as db:
        aid = account_id(db, "gmail")
        assert aid is not None
        heartbeat = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
        db.execute(
            "INSERT INTO fast_path_health "
            "(account_id,remote_folder,mode,updated_at,last_heartbeat_at) "
            "VALUES (?,?,?,?,?)",
            (aid, "__GMAIL__", mode, heartbeat, heartbeat),
        )
        db.commit()

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("status must not touch Gmail/OAuth transport")

    monkeypatch.setattr(gmail_module, "load_credentials", forbidden)
    monkeypatch.setattr(gmail_module, "GmailApiClient", forbidden)
    monkeypatch.setattr(gmail_module, "_ManagedGmailSession", forbidden)
    monkeypatch.setattr(cli_module, "GmailAdapter", forbidden)
    monkeypatch.setattr(cli_module, "GmailWatcher", forbidden)
    assert main(["status", "--config", str(config_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["gmail"][0]["watcher_state"] == expected


def test_ingest_json_output(
    config_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "message.eml"
    source.write_bytes(b"Message-ID: <cli@example.test>\r\n\r\nbody\r\n")
    assert (
        main(
            [
                "ingest",
                str(source),
                "--account",
                "test",
                "--config",
                str(config_file),
                "--json",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert '"created": true' in output
    assert '"canonical_message_id"' in output


def test_remote_delete_cli_requires_one_explicit_mode(config_file: Path) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["remote-delete", "--config", str(config_file)])
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["remote-delete", "--dry-run", "--execute-plan", "1", "--config", str(config_file)]
        )
    with pytest.raises(SystemExit):
        main(["remote-delete", "--execute-plan", "1", "--config", str(config_file)])
    with pytest.raises(SystemExit):
        main(
            [
                "remote-delete", "--execute-plan", "1", "--account", "test", "--limit", "1",
                "--config", str(config_file),
            ]
        )
    assert main(["remote-delete", "--dry-run", "--limit", "1", "--config", str(config_file)]) == 0


def test_execute_plan_default_m12_a_factory_leaves_no_production_run(
    config_file: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from mailarchive.remote_mutation import plan_dry_run

    config = load_config(config_file)
    initialize(config.database.path, config.accounts)
    # An empty source plan is enough to prove parser gate behavior, while provider
    # construction is separately covered by the production-engine test.
    source = plan_dry_run(config, account="test")
    with pytest.raises(SystemExit) as result:
        main(
            [
                "remote-delete", "--execute-plan", str(source["run_id"]), "--account", "test",
                "--config", str(config_file),
            ]
        )
    assert result.value.code == 2
    error = capsys.readouterr().err.lower()
    assert "production" in error or "source" in error
