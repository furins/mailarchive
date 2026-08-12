from __future__ import annotations

import imaplib
import ssl
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from mailarchive.config import ConfigError, load_config
from mailarchive.db import connect
from mailarchive.imap import (
    ImapAdapter,
    ImapError,
    folder_lock,
    parse_fetch_response,
    register_remote_link,
)
from mailarchive.ingest import ingest_file
from mailarchive.models import AccountConfig


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


def test_plaintext_non_loopback_is_rejected(config_file: Path) -> None:
    _configure_imap(config_file, host="imap.example.test")
    with pytest.raises(ConfigError, match="loopback"):
        load_config(config_file)


def test_direct_fetch_parser_requires_one_matching_uid() -> None:
    response = [(b"1 (UID 123 BODY[] {4}", b"body"), b")"]
    assert parse_fetch_response(123, response).raw_bytes == b"body"
    with pytest.raises(ImapError, match="unexpected UID"):
        parse_fetch_response(124, response)
    with pytest.raises(ImapError, match="lacks UID"):
        parse_fetch_response(123, [(b"1 (BODY[] {4}", b"body")])
    with pytest.raises(ImapError, match="BODY"):
        parse_fetch_response(123, [(b"1 (UID 123 FLAGS (\\Seen)", b"body")])
    with pytest.raises(ImapError, match="missing or duplicated"):
        parse_fetch_response(123, [response[0], response[0]])
    with pytest.raises(ImapError, match="malformed"):
        parse_fetch_response(123, [(b"UID 123", "not-bytes")])
    with pytest.raises(ImapError, match="unexpected IMAP FETCH response fragment"):
        parse_fetch_response(123, [response[0], b"unexpected"])
    with pytest.raises(ImapError, match="lacks UID"):
        parse_fetch_response(123, [(b"1 (UID 123 UID 124 BODY[] {4}", b"body")])


class _ReadOnlyClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def login(self, username: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("login", (username, password), {}))
        return "OK", [b"logged in"]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", (mailbox,), {"readonly": readonly}))
        return "OK", [b"1"]

    def response(self, code: str) -> tuple[str, list[bytes]]:
        self.calls.append(("response", (code,), {}))
        return "UIDVALIDITY", [b"9"]

    def uid(self, command: str, *args: str) -> tuple[str, list[object]]:
        self.calls.append(("uid", (command, *args), {}))
        if command == "search":
            return "OK", [b"1"]
        if command == "fetch":
            return "OK", [(b"1 (UID 1 BODY[] {4}", b"body"), b")"]
        raise AssertionError(f"unexpected UID operation: {command}")

    def logout(self) -> tuple[str, list[bytes]]:
        self.calls.append(("logout", (), {}))
        return "BYE", [b"bye"]

    def __getattr__(self, name: str) -> object:
        if name.lower() in {
            "store",
            "copy",
            "move",
            "expunge",
            "append",
            "delete",
            "create",
            "rename",
            "close",
        }:
            raise AssertionError(f"unexpected mutating IMAP method: {name}")
        raise AttributeError(name)


def test_sync_uses_only_read_only_uid_operations(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_imap(config_file)
    config = load_config(config_file)
    client = _ReadOnlyClient()
    monkeypatch.setenv("MAILARCHIVE_TEST_SECRET", "fixture")

    def fake_open(_adapter: ImapAdapter, _account: AccountConfig) -> _ReadOnlyClient:
        return client

    monkeypatch.setattr(ImapAdapter, "_open", fake_open)
    results = ImapAdapter(config).sync("test", "INBOX")
    assert len(results) == 1
    assert ("select", ("INBOX",), {"readonly": True}) in client.calls
    assert ("uid", ("search", "ALL"), {}) in client.calls
    assert ("uid", ("fetch", "1", "(UID BODY.PEEK[])"), {}) in client.calls
    assert all(
        call[0]
        not in {"store", "copy", "move", "expunge", "append", "delete", "create", "rename", "close"}
        for call in client.calls
    )


def test_tls_connections_use_verifying_default_context(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_imap(config_file, tls_mode="IMAPS")
    config = load_config(config_file)
    context = ssl.create_default_context()
    seen: dict[str, object] = {}

    class _SslClient:
        pass

    def fake_ssl(*args: object, **kwargs: object) -> _SslClient:
        seen["ssl"] = (args, kwargs)
        return _SslClient()

    monkeypatch.setattr("mailarchive.imap.ssl.create_default_context", lambda: context)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", fake_ssl)
    ssl_client = ImapAdapter(config)._open(config.accounts[0])  # pyright: ignore[reportPrivateUsage]
    assert isinstance(ssl_client, _SslClient)
    _, ssl_kwargs = cast(tuple[tuple[object, ...], dict[str, object]], seen["ssl"])
    assert ssl_kwargs["ssl_context"] is context
    assert context.check_hostname and context.verify_mode is ssl.CERT_REQUIRED

    _configure_imap(config_file, tls_mode="STARTTLS")
    starttls_config = load_config(config_file)

    class _StartTlsClient:
        def starttls(self, *, ssl_context: ssl.SSLContext) -> tuple[str, list[bytes]]:
            seen["starttls"] = ssl_context
            return "OK", []

    def fake_imap4(*_args: object, **_kwargs: object) -> _StartTlsClient:
        return _StartTlsClient()

    monkeypatch.setattr(imaplib, "IMAP4", fake_imap4)
    starttls_client = ImapAdapter(starttls_config)._open(  # pyright: ignore[reportPrivateUsage]
        starttls_config.accounts[0]
    )
    assert isinstance(starttls_client, _StartTlsClient)
    assert seen["starttls"] is context


def test_missing_credential_and_unconfigured_folder_fail_closed(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_imap(config_file)
    config = load_config(config_file)
    monkeypatch.delenv("MAILARCHIVE_TEST_SECRET", raising=False)
    with pytest.raises(ImapError, match="MAILARCHIVE_TEST_SECRET"):
        ImapAdapter(config).sync("test", "INBOX")
    monkeypatch.setenv("MAILARCHIVE_TEST_SECRET", "fixture")
    with pytest.raises(ImapError, match="not configured"):
        ImapAdapter(config).sync("test", "Sent Items")


@pytest.mark.parametrize("folder", ["INBOX", "Sent Items", "Archive/2025", "Trash"])
def test_remote_folder_name_is_preserved_in_sqlite(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, folder: str
) -> None:
    _configure_imap(config_file, folders=[folder])
    config = load_config(config_file)
    client = _ReadOnlyClient()
    monkeypatch.setenv("MAILARCHIVE_TEST_SECRET", "fixture")

    def fake_open(_adapter: ImapAdapter, _account: AccountConfig) -> _ReadOnlyClient:
        return client

    monkeypatch.setattr(ImapAdapter, "_open", fake_open)
    ImapAdapter(config).sync("test", folder)
    with connect(config.database.path) as connection:
        stored = connection.execute("SELECT remote_folder FROM remote_messages").fetchone()
    assert stored is not None and str(stored[0]) == folder


def test_remote_identity_cannot_link_to_two_canonical_messages(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_imap(config_file)
    config = load_config(config_file)
    client = _ReadOnlyClient()
    monkeypatch.setenv("MAILARCHIVE_TEST_SECRET", "fixture")

    def fake_open(_adapter: ImapAdapter, _account: AccountConfig) -> _ReadOnlyClient:
        return client

    monkeypatch.setattr(ImapAdapter, "_open", fake_open)
    first = ImapAdapter(config).sync("test", "INBOX")[0].canonical_message
    alternate = tmp_path / "alternate.eml"
    alternate.write_bytes(b"From: alternate@example.test\r\n\r\nalternate\r\n")
    second = ingest_file(config, alternate, "test").canonical_message
    with pytest.raises(ImapError, match="conflicts"):
        register_remote_link(config, "test", "INBOX", 9, 1, second)
    with connect(config.database.path) as connection:
        links = connection.execute(
            "SELECT canonical_message_id FROM remote_canonical_links"
        ).fetchall()
        failure = connection.execute(
            "SELECT result FROM audit_events WHERE event_type='imap.remote_link.failed'"
        ).fetchone()
    assert [str(row[0]) for row in links] == [first.id]
    assert failure is not None and str(failure[0]) == "conflict"


def test_uidvalidity_change_creates_a_new_remote_identity(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_imap(config_file)
    config = load_config(config_file)
    client = _ReadOnlyClient()
    monkeypatch.setenv("MAILARCHIVE_TEST_SECRET", "fixture")

    def fake_open(_adapter: ImapAdapter, _account: AccountConfig) -> _ReadOnlyClient:
        return client

    monkeypatch.setattr(ImapAdapter, "_open", fake_open)
    canonical = ImapAdapter(config).sync("test", "INBOX")[0].canonical_message
    register_remote_link(config, "test", "INBOX", 10, 1, canonical)
    with connect(config.database.path) as connection:
        identities = connection.execute(
            "SELECT uidvalidity FROM remote_messages WHERE remote_uid=1 ORDER BY uidvalidity"
        ).fetchall()
    assert [int(row[0]) for row in identities] == [9, 10]


def test_account_folder_lock_rejects_overlap(config_file: Path) -> None:
    _configure_imap(config_file)
    config = load_config(config_file)
    account = config.accounts[0]
    with folder_lock(config, account, "INBOX"):
        with pytest.raises(ImapError, match="already running"):
            with folder_lock(config, account, "INBOX"):
                pass
