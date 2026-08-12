from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

import mailarchive.cli as cli_module
import mailarchive.gmail as gmail_module
from mailarchive.cli import build_parser, main
from mailarchive.config import load_config
from mailarchive.db import account_id, connect, initialize


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


def test_no_destructive_remote_command_appears() -> None:
    help_text = build_parser().format_help()
    assert "delete" not in help_text.lower()
    assert "remote" not in help_text.lower()


def test_db_init_and_status(config_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["db", "init", "--config", str(config_file)]) == 0
    assert main(["status", "--config", str(config_file), "--json"]) == 0
    assert '"database_initialized": true' in capsys.readouterr().out


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


def test_cli_has_no_remote_or_network_command() -> None:
    help_text = build_parser().format_help().lower()
    assert "remote" not in help_text
    assert "network" not in help_text
