from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest
import yaml

import mailarchive.notmuch as notmuch_module
from mailarchive.config import load_config
from mailarchive.db import connect
from mailarchive.ingest import ingest_file, verify_canonical_message
from mailarchive.models import AppConfig
from mailarchive.notmuch import (
    COMMAND_TIMEOUT_SECONDS,
    REFRESH_TIMEOUT_SECONDS,
    NotmuchAdapter,
    NotmuchError,
    isolated_notmuch_environment,
    managed_config_text,
    managed_layout,
    search_canonical_messages,
    write_managed_config,
)


def _write_message(path: Path, *, message_id: str, subject: str, body: str, sender: str) -> bytes:
    raw = (
        f"From: {sender}\r\n"
        "To: archive@example.test\r\n"
        f"Subject: {subject}\r\n"
        f"Message-ID: {message_id}\r\n"
        "Date: Wed, 1 Jan 2020 12:00:00 +0000\r\n"
        "\r\n"
        f"{body}\r\n"
    ).encode()
    path.write_bytes(raw)
    return raw


def _add_account(config_file: Path, name: str) -> None:
    values = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    values["accounts"][name] = {
        "kind": "imap",
        "enabled": True,
        "remote_retention_days": 365,
        "required_verified_backups": 2,
        "config_ref": f"env:{name.upper()}_SECRET",
    }
    config_file.write_text(yaml.safe_dump(values), encoding="utf-8")


def _result_ids(config: AppConfig, query: str) -> list[str]:
    return [item.canonical_message.id for item in search_canonical_messages(config, query)]


def test_managed_configuration_is_deterministic_and_separate(config_file: Path) -> None:
    config = load_config(config_file)
    layout = write_managed_config(config)
    contents = layout.config_path.read_text(encoding="utf-8")
    assert layout.mail_root == config.archive.root.resolve() / "mail"
    assert layout.database_path == config.archive.root.resolve() / "state" / "notmuch" / "db"
    assert not layout.database_path.is_relative_to(layout.mail_root)
    assert f"mail_root={layout.mail_root}" in contents
    assert f"path={layout.database_path}" in contents
    assert f"hook_dir={layout.hook_directory}" in contents
    assert "synchronize_flags=false" in contents
    assert "decrypt=false" in contents
    assert "tags=archive" in contents
    assert "inbox" not in contents
    assert "unread" not in contents
    assert contents == managed_config_text(layout)


def test_missing_binary_fails_actionably(config_file: Path) -> None:
    with pytest.raises(NotmuchError, match="install the 'notmuch' package"):
        NotmuchAdapter(load_config(config_file), executable="notmuch-definitely-absent").refresh()


def test_failed_subprocess_is_surfaced(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=7, stdout="", stderr="fixture failure"
        )

    monkeypatch.setattr(notmuch_module.subprocess, "run", fail)
    with pytest.raises(NotmuchError, match="exit 7.*fixture failure"):
        NotmuchAdapter(load_config(config_file)).refresh()


def test_refresh_disables_hooks(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def succeed(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(notmuch_module.subprocess, "run", succeed)
    NotmuchAdapter(load_config(config_file)).refresh()
    assert commands[0][-2:] == ["new", "--no-hooks"]
    assert commands[0][0] == "notmuch"
    assert any(item.startswith("--config=") for item in commands[0])


def test_subprocesses_use_isolated_environment_and_dedicated_timeouts(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], object, object]] = []
    for variable in ("NOTMUCH_DATABASE", "NOTMUCH_CONFIG", "NOTMUCH_PROFILE", "MAILDIR"):
        monkeypatch.setenv(variable, "/tmp/notmuch-decoy")

    def succeed(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs["timeout"], kwargs["env"]))
        return subprocess.CompletedProcess(command, 0, "0.39", "")

    monkeypatch.setattr(notmuch_module.subprocess, "run", succeed)
    adapter = NotmuchAdapter(load_config(config_file))
    adapter.version()
    adapter.refresh()
    adapter.tag(["+flagged"], "tag:archive")
    assert [call[1] for call in calls] == [
        COMMAND_TIMEOUT_SECONDS,
        REFRESH_TIMEOUT_SECONDS,
        COMMAND_TIMEOUT_SECONDS,
    ]
    for _, _, environment in calls:
        assert isinstance(environment, dict)
        typed_environment = cast(dict[str, str], environment)
        assert "PATH" in typed_environment
        assert not {
            "NOTMUCH_DATABASE",
            "NOTMUCH_CONFIG",
            "NOTMUCH_PROFILE",
            "MAILDIR",
        }.intersection(typed_environment)


def test_timeout_is_still_surfaced_with_its_dedicated_limit(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["notmuch", "new"], timeout=REFRESH_TIMEOUT_SECONDS)

    monkeypatch.setattr(notmuch_module.subprocess, "run", timeout)
    with pytest.raises(NotmuchError, match="timed out after 600 seconds"):
        NotmuchAdapter(load_config(config_file)).refresh()


@pytest.mark.skipif(shutil.which("notmuch") is None, reason="requires locally installed notmuch")
def test_notmuch_refresh_search_and_rebuild(config_file: Path, tmp_path: Path) -> None:
    config = load_config(config_file)
    first_source = tmp_path / "first.eml"
    second_source = tmp_path / "second.eml"
    _write_message(
        first_source,
        message_id="<first@example.test>",
        subject="signed contract",
        body="invoice reference distinctive-body-token",
        sender="mario@example.com",
    )
    first = ingest_file(config, first_source, "test")
    adapter = NotmuchAdapter(config)
    adapter.refresh()
    assert adapter.version()
    assert _result_ids(config, "from:mario@example.com") == [first.canonical_message.id]
    assert _result_ids(config, 'subject:"signed contract"') == [first.canonical_message.id]
    assert _result_ids(config, "distinctive-body-token") == [first.canonical_message.id]
    assert _result_ids(config, "date:2020-01-01..2020-12-31") == [first.canonical_message.id]
    assert len(search_canonical_messages(config, "tag:archive")) == 1

    _write_message(
        second_source,
        message_id="<second@example.test>",
        subject="incremental",
        body="incremental-token",
        sender="luigi@example.com",
    )
    second = ingest_file(config, second_source, "test")
    adapter.refresh()
    assert _result_ids(config, "incremental-token") == [second.canonical_message.id]

    layout = managed_layout(config)
    shutil.rmtree(layout.database_path)
    assert first.canonical_message.local_path.is_file()
    assert second.canonical_message.local_path.is_file()
    with connect(config.database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM canonical_messages").fetchone()[0] == 2
    adapter.refresh()
    assert set(_result_ids(config, "tag:archive")) == {
        first.canonical_message.id,
        second.canonical_message.id,
    }


@pytest.mark.skipif(shutil.which("notmuch") is None, reason="requires locally installed notmuch")
def test_notmuch_ignores_user_database_environment(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(config_file)
    source = tmp_path / "message.eml"
    _write_message(
        source,
        message_id="<isolation@example.test>",
        subject="environment isolation",
        body="managed-index-token",
        sender="isolation@example.test",
    )
    message = ingest_file(config, source, "test")
    decoy_root = tmp_path / "decoy"
    decoy_mail_root = decoy_root / "mail"
    decoy_database = decoy_root / "db"
    decoy_hooks = decoy_root / "hooks"
    decoy_mail_root.mkdir(parents=True)
    decoy_hooks.mkdir()
    decoy_config = decoy_root / "config"
    decoy_config.write_text(
        "\n".join(
            (
                "[database]",
                f"mail_root={decoy_mail_root}",
                f"path={decoy_database}",
                f"hook_dir={decoy_hooks}",
                "",
                "[user]",
                "name=Decoy",
                "primary_email=decoy@localhost.invalid",
                "",
            )
        ),
        encoding="utf-8",
    )
    subprocess.run(
        ["notmuch", f"--config={decoy_config}", "new", "--no-hooks"],
        check=True,
        capture_output=True,
        text=True,
        env=isolated_notmuch_environment(),
    )
    decoy_fingerprint = {
        path.relative_to(decoy_database): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in decoy_database.rglob("*")
        if path.is_file()
    }
    monkeypatch.setenv("NOTMUCH_DATABASE", str(decoy_database))
    monkeypatch.setenv("NOTMUCH_CONFIG", str(decoy_config))
    monkeypatch.setenv("NOTMUCH_PROFILE", "decoy")
    monkeypatch.setenv("MAILDIR", str(decoy_mail_root))

    adapter = NotmuchAdapter(config)
    adapter.refresh()
    assert _result_ids(config, "managed-index-token") == [message.canonical_message.id]
    adapter.tag(["+managed-isolation"], "id:isolation@example.test")
    assert _result_ids(config, "tag:managed-isolation") == [message.canonical_message.id]
    assert managed_layout(config).database_path.exists()
    assert {
        path.relative_to(decoy_database): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in decoy_database.rglob("*")
        if path.is_file()
    } == decoy_fingerprint
    decoy_count = subprocess.run(
        ["notmuch", f"--config={decoy_config}", "count", "--", "*"],
        check=True,
        capture_output=True,
        text=True,
        env=isolated_notmuch_environment(),
    )
    assert decoy_count.stdout.strip() == "0"


@pytest.mark.skipif(shutil.which("notmuch") is None, reason="requires locally installed notmuch")
def test_duplicate_message_ids_and_accounts_remain_canonical(
    config_file: Path, tmp_path: Path
) -> None:
    _add_account(config_file, "other")
    config = load_config(config_file)
    first_path = tmp_path / "first.eml"
    second_path = tmp_path / "second.eml"
    shared_path = tmp_path / "shared.eml"
    first_bytes = _write_message(
        first_path,
        message_id="<same@example.test>",
        subject="first duplicate",
        body="first-duplicate-token",
        sender="same@example.com",
    )
    second_bytes = _write_message(
        second_path,
        message_id="<same@example.test>",
        subject="second duplicate",
        body="second-duplicate-token",
        sender="same@example.com",
    )
    shared_bytes = _write_message(
        shared_path,
        message_id="<shared@example.test>",
        subject="shared account copy",
        body="shared-account-token",
        sender="shared@example.com",
    )
    first = ingest_file(config, first_path, "test")
    second = ingest_file(config, second_path, "test")
    shared_test = ingest_file(config, shared_path, "test")
    shared_other = ingest_file(config, shared_path, "other")
    original_path = first.canonical_message.local_path
    original_name = original_path.name
    original_hash = hashlib.sha256(first_bytes).hexdigest()
    NotmuchAdapter(config).refresh()
    NotmuchAdapter(config).tag(["+flagged"], "id:same@example.test")
    assert original_path == first.canonical_message.local_path
    assert original_path.name == original_name
    assert original_path.read_bytes() == first_bytes
    assert verify_canonical_message(first.canonical_message)
    assert first.canonical_message.sha256 == original_hash
    assert second.canonical_message.local_path.read_bytes() == second_bytes
    assert verify_canonical_message(second.canonical_message)
    duplicate_ids = {
        item.canonical_message.id
        for item in search_canonical_messages(config, "from:same@example.com")
    }
    assert duplicate_ids == {first.canonical_message.id, second.canonical_message.id}
    shared_results = search_canonical_messages(config, "shared-account-token")
    assert {item.canonical_message.id for item in shared_results} == {
        shared_test.canonical_message.id,
        shared_other.canonical_message.id,
    }
    assert {item.account for item in shared_results} == {"test", "other"}
    assert shared_test.canonical_message.local_path.read_bytes() == shared_bytes
    assert shared_other.canonical_message.local_path.read_bytes() == shared_bytes
    with connect(config.database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM canonical_messages").fetchone()[0] == 4
