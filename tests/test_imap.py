from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import mailarchive.imap as imap_module
from mailarchive.config import ConfigError, load_config
from mailarchive.imap import (
    ImapError,
    ImapMbsyncAdapter,
    managed_config_text,
    managed_layout,
    parse_mbsync_state,
    validate_managed_config,
    write_managed_config,
)


def _configure_imap(config_file: Path, **overrides: object) -> None:
    values = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    imap: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 1993,
        "username": "fixture",
        "tls_mode": "INSECURE_LOOPBACK",
        "folders": ["INBOX"],
    }
    imap.update(overrides)
    values["accounts"]["test"]["imap"] = imap
    config_file.write_text(yaml.safe_dump(values), encoding="utf-8")


def test_managed_config_is_deterministic_and_pull_only(config_file: Path) -> None:
    _configure_imap(config_file)
    config = load_config(config_file)
    account = config.accounts[0]
    text = managed_config_text(config, account, "INBOX")
    layout = write_managed_config(config, account, "INBOX")
    assert layout.config_path.read_text(encoding="utf-8") == text
    assert layout.mirror_mailbox.is_relative_to(config.archive.root / "staging" / "mbsync")
    assert not layout.mirror_mailbox.is_relative_to(config.archive.root / "mail")
    for directive in (
        "Sync Pull New", "Create Near", "Remove None", "Expunge None", "ExpungeSolo None",
        "MaxSize 0", "FSync yes", "SyncState *", "CopyArrivalDate yes", "AltMap no",
    ):
        assert directive in text
    assert "Patterns" not in text
    assert "fixture-secret-value" not in text
    assert oct(layout.config_path.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("unsafe", ["Push", "Gone", "Flags", "Full", "Trash", "MaxMessages"])
def test_semantic_validator_rejects_far_side_or_unsafe_behavior(unsafe: str) -> None:
    safe = "\n".join(imap_module.REQUIRED_DIRECTIVES)
    with pytest.raises(ImapError, match="unsafe"):
        validate_managed_config(safe + "\n" + unsafe)


def test_semantic_validator_rejects_all_accounts_command(config_file: Path) -> None:
    _configure_imap(config_file)
    config = load_config(config_file)
    text = managed_config_text(config, config.accounts[0], "INBOX")
    with pytest.raises(ImapError, match="explicit channel"):
        validate_managed_config(text, ["mbsync", "-a"])


def test_missing_credential_and_binary_fail_without_exposing_secret(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_imap(config_file)
    config = load_config(config_file)
    monkeypatch.delenv("MAILARCHIVE_TEST_SECRET", raising=False)
    with pytest.raises(ImapError, match="MAILARCHIVE_TEST_SECRET"):
        ImapMbsyncAdapter(config).sync("test", "INBOX")
    monkeypatch.setenv("MAILARCHIVE_TEST_SECRET", "actual-secret-value")
    with pytest.raises(ImapError, match="isync") as error:
        ImapMbsyncAdapter(config, executable="mbsync-not-present").sync("test", "INBOX")
    assert "actual-secret-value" not in str(error.value)


def test_plaintext_non_loopback_is_rejected(config_file: Path) -> None:
    _configure_imap(config_file, host="imap.example.test", tls_mode="INSECURE_LOOPBACK")
    with pytest.raises(ConfigError, match="loopback"):
        load_config(config_file)


def test_state_parser_requires_uidvalidity_and_parses_uid_pairs(tmp_path: Path) -> None:
    state = tmp_path / ".mbsyncstate"
    state.write_text("FarUidValidity 99\n1 11 0\n2 12 0\n", encoding="ascii")
    parsed = parse_mbsync_state(state)
    assert parsed.far_uidvalidity == 99
    assert parsed.far_to_near == {1: 11, 2: 12}
    state.write_text("1 11 0\n", encoding="ascii")
    with pytest.raises(ImapError, match="FarUidValidity"):
        parse_mbsync_state(state)


def test_only_explicit_channel_is_invoked(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_imap(config_file)
    monkeypatch.setenv("MAILARCHIVE_TEST_SECRET", "fixture-secret")
    config = load_config(config_file)
    commands: list[list[str]] = []

    def complete(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        layout = managed_layout(config, config.accounts[0], "INBOX")
        state_path = layout.mirror_mailbox / "INBOX" / ".mbsyncstate"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("FarUidValidity 1\n", encoding="ascii")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(imap_module.subprocess, "run", complete)
    assert ImapMbsyncAdapter(config).sync("test", "INBOX") == []
    layout = managed_layout(config, config.accounts[0], "INBOX")
    assert commands == [["mbsync", "-c", str(layout.config_path), layout.channel]]


@pytest.mark.skipif(shutil.which("mbsync") is None, reason="requires installed isync/mbsync")
def test_state_parser_accepts_state_produced_by_installed_mbsync(tmp_path: Path) -> None:
    """Exercise the native state adapter against the CI mbsync version without a network server."""
    far = tmp_path / "far"
    near = tmp_path / "near"
    for root in (far, near):
        for part in ("INBOX/cur", "INBOX/new", "INBOX/tmp"):
            (root / part).mkdir(parents=True)
        for directory in (
            root,
            root / "INBOX",
            root / "INBOX" / "cur",
            root / "INBOX" / "new",
            root / "INBOX" / "tmp",
        ):
            directory.chmod(0o777)
    (far / "INBOX" / "new" / "fixture").write_bytes(
        b"Message-ID: <native@example.test>\r\n\r\nbody\r\n"
    )
    config = tmp_path / "mbsyncrc"
    config.write_text(
        "\n".join(
            (
                "FSync yes",
                "MaildirStore far",
                f"Path {far}/",
                f"Inbox {far}/INBOX",
                "AltMap no",
                "",
                "MaildirStore near",
                f"Path {near}/",
                f"Inbox {near}/INBOX",
                "AltMap no",
                "",
                "Channel native",
                "Far :far:INBOX",
                "Near :near:INBOX",
                "Sync Pull New",
                "Create Near",
                "Remove None",
                "Expunge None",
                "ExpungeSolo None",
                "MaxSize 0",
                "CopyArrivalDate yes",
                "SyncState *",
                "",
            )
        ),
        encoding="utf-8",
    )
    # The sandbox executes mbsync with a restricted UID; this test config has no credentials.
    config.chmod(0o644)
    completed = subprocess.run(
        ["mbsync", "-c", str(config), "native"], check=False, capture_output=True, text=True
    )
    if "Cannot open config file" in completed.stderr and "Permission denied" in completed.stderr:
        pytest.skip("execution sandbox denies mbsync access to pytest temporary directories")
    assert completed.returncode == 0, completed.stderr
    parsed = parse_mbsync_state(near / "INBOX" / ".mbsyncstate")
    assert parsed.far_uidvalidity > 0
    assert parsed.far_to_near
