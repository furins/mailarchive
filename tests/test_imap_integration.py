"""Disposable loopback Dovecot coverage for direct read-only IMAP acquisition."""

from __future__ import annotations

import hashlib
import imaplib
import os
import pwd
import socket
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import cast

import pytest
import yaml

from mailarchive.config import load_config
from mailarchive.db import connect
from mailarchive.fastpath import FastPathWatcher, fast_path_status
from mailarchive.imap import ImapAdapter, encode_mailbox_name
from mailarchive.notmuch import (
    NotmuchAdapter,
    NotmuchError,
    SearchResult,
    search_canonical_messages,
)


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _raw(identifier: str, subject: str, body: str = "body") -> bytes:
    return (
        f"From: source@example.test\r\nTo: archive@example.test\r\nSubject: {subject}\r\n"
        f"Message-ID: {identifier}\r\n\r\n{body}\r\n"
    ).encode()


def _safe_dovecot_diagnostic(log: Path, process: subprocess.Popen[str]) -> str:
    """Return bounded fixture diagnostics without exposing its test password."""
    try:
        stdout, stderr = process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        stdout, stderr = "", ""
    contents = "\n".join(
        part
        for part in (
            log.read_text(encoding="utf-8", errors="replace") if log.exists() else "",
            stdout,
            stderr,
        )
        if part
    )
    return contents.replace("fixture-password", "<redacted>")[-2_000:] or "no Dovecot log available"


@pytest.fixture
def dovecot_loopback(tmp_path: Path) -> Generator[tuple[int, Path, Path]]:
    root = tmp_path / "dovecot"
    mail = root / "mail"
    user = pwd.getpwuid(os.getuid()).pw_name
    version = subprocess.run(
        ["dovecot", "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    dovecot_24 = version.startswith("2.4.")
    for part in ("cur", "new", "tmp"):
        (mail / part).mkdir(parents=True)
    port = _free_port()
    password = root / "passwd"
    password.write_text("fixture:{PLAIN}fixture-password\n")
    log = root / "dovecot.log"
    config = root / "dovecot.conf"
    version_settings = (
        ("dovecot_config_version = 2.4.0", "dovecot_storage_version = 2.4.0") if dovecot_24 else ()
    )
    auth_settings = (
        (
            "passdb passwd-file {",
            f"passwd_file_path = {password}",
            "}",
            "userdb static {",
            "static_allow_all_users = yes",
            "userdb_fields {",
            f"uid = {os.getuid()}",
            f"gid = {os.getgid()}",
            f"home = {root}",
            "}",
            "}",
        )
        if dovecot_24
        else (
            "disable_plaintext_auth = no",
            f"mail_location = maildir:{mail}",
            "passdb {",
            "driver = passwd-file",
            f"args = scheme=PLAIN username_format=%u {password}",
            "}",
            "userdb {",
            "driver = static",
            f"args = uid={os.getuid()} gid={os.getgid()} home={root}",
            "}",
        )
    )
    config.write_text(
        "\n".join(
            (
                *version_settings,
                f"default_internal_user = {user}",
                f"default_internal_group = {user}",
                f"default_login_user = {user}",
                "protocols = imap",
                "listen = 127.0.0.1",
                f"base_dir = {root / 'run'}",
                f"state_dir = {root / 'state'}",
                f"log_path = {log}",
                "ssl = no",
                "auth_mechanisms = plain",
                *(("mail_driver = maildir", f"mail_path = {mail}") if dovecot_24 else ()),
                *auth_settings,
                "service imap-login {",
                f"user = {user}",
                "chroot =",
                "inet_listener imap {",
                f"port = {port}",
                "}",
                "}",
                "service imap {",
                f"user = {user}",
                "chroot =",
                "}",
                "service anvil {",
                "chroot =",
                "}",
                "",
            )
        )
    )
    process = subprocess.Popen(
        ["dovecot", "-F", "-c", str(config)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), 0.1):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError(f"Dovecot did not start: {_safe_dovecot_diagnostic(log, process)}")
    try:
        yield port, mail, log
    finally:
        process.terminate()
        process.wait(timeout=10)


def _create_test_mailbox(port: int, folder: str) -> None:
    """Fixture setup only: production MailArchive never creates IMAP mailboxes."""
    client = imaplib.IMAP4("127.0.0.1", port)
    try:
        assert client.login("fixture", "fixture-password")[0] == "OK"
        assert client.create(encode_mailbox_name(folder))[0] == "OK"
    finally:
        client.logout()


def _snapshot(
    port: int, folder: str = "INBOX"
) -> tuple[int, dict[int, tuple[tuple[bytes, ...], bytes]]]:
    client = imaplib.IMAP4("127.0.0.1", port)
    try:
        assert client.login("fixture", "fixture-password")[0] == "OK"
        assert client.select(encode_mailbox_name(folder), readonly=True)[0] == "OK"
        uidvalidity = int(client.response("UIDVALIDITY")[1][0])
        status, values = client.uid("search", "ALL")
        assert status == "OK"
        result: dict[int, tuple[tuple[bytes, ...], bytes]] = {}
        for uid in values[0].split():
            status, data = client.uid("fetch", uid, "(UID FLAGS BODY.PEEK[])")
            assert status == "OK"
            metadata, raw = cast(tuple[bytes, bytes], data[0])
            flags = tuple(flag for flag in imaplib.ParseFlags(metadata) if flag != b"\\Recent")
            result[int(uid)] = (flags, raw)
        return uidvalidity, result
    finally:
        client.logout()


def test_direct_loopback_acquisition_preserves_server_bytes(
    config_file: Path, dovecot_loopback: tuple[int, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    port, mail, _ = dovecot_loopback
    first = _raw("<same@example.test>", "loopback one")
    (mail / "new" / "one").write_bytes(first)
    uidvalidity, before = _snapshot(port)
    sent_folder = "Sent Items"
    _create_test_mailbox(port, sent_folder)
    sent = _raw("<sent@example.test>", "loopback sent")
    (mail / ".Sent Items" / "new" / "sent-one").write_bytes(sent)
    sent_uidvalidity, sent_before = _snapshot(port, sent_folder)
    values = yaml.safe_load(config_file.read_text())
    values["accounts"]["test"]["imap"] = {
        "host": "127.0.0.1",
        "port": port,
        "username": "fixture",
        "tls_mode": "INSECURE_LOOPBACK",
        "folders": ["INBOX", sent_folder],
    }
    config_file.write_text(yaml.safe_dump(values))
    monkeypatch.setenv("MAILARCHIVE_TEST_SECRET", "fixture-password")
    config = load_config(config_file)
    results = ImapAdapter(config).sync("test", "INBOX")
    assert len(results) == 1 and _snapshot(port) == (uidvalidity, before)
    canonical = results[0].canonical_message
    assert canonical.local_path.read_bytes() == first
    assert canonical.sha256 == hashlib.sha256(first).hexdigest()
    with connect(config.database.path) as connection:
        remote = connection.execute(
            "SELECT uidvalidity, remote_uid FROM remote_messages"
        ).fetchone()
        link = connection.execute(
            "SELECT canonical_message_id FROM remote_canonical_links"
        ).fetchone()
    assert tuple(remote) == (uidvalidity, next(iter(before)))
    assert str(link[0]) == canonical.id
    sent_results = ImapAdapter(config).sync("test", sent_folder)
    assert len(sent_results) == 1
    assert sent_results[0].canonical_message.local_path.read_bytes() == sent
    assert _snapshot(port, sent_folder) == (sent_uidvalidity, sent_before)
    with connect(config.database.path) as connection:
        sent_remote = connection.execute(
            "SELECT remote_folder, uidvalidity FROM remote_messages WHERE remote_folder=?",
            (sent_folder,),
        ).fetchone()
    assert sent_remote is not None and tuple(sent_remote) == (sent_folder, sent_uidvalidity)
    assert ImapAdapter(config).sync("test", "INBOX") == []
    (mail / "new" / "two").write_bytes(
        _raw("<same@example.test>", "loopback two", "different bytes")
    )
    _, after = _snapshot(port)
    next_results = ImapAdapter(config).sync("test", "INBOX")
    assert len(next_results) == 1 and _snapshot(port)[1] == after
    NotmuchAdapter(config).refresh()
    assert search_canonical_messages(config, "loopback two", scope="all")


def _wait_until(predicate: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for bounded Dovecot IDLE acceptance condition")


def test_dovecot_idle_fast_path_acquires_and_indexes_without_poll(
    config_file: Path, dovecot_loopback: tuple[int, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real Python 3.14 IDLE -> M3 BODY.PEEK[] -> notmuch acceptance coverage."""
    port, _, _ = dovecot_loopback
    values = yaml.safe_load(config_file.read_text())
    values["accounts"]["test"]["imap"] = {
        "host": "127.0.0.1",
        "port": port,
        "username": "fixture",
        "tls_mode": "INSECURE_LOOPBACK",
        "folders": ["INBOX"],
        "fast_path": {
            "idle_enabled": True,
            "poll_interval_seconds": 120,
            "reconcile_interval_seconds": 1740,
        },
    }
    config_file.write_text(yaml.safe_dump(values))
    monkeypatch.setenv("MAILARCHIVE_TEST_SECRET", "fixture-password")
    config = load_config(config_file)
    stop = threading.Event()
    watcher = FastPathWatcher(config, "test", stop, idle_window_seconds=0.25)
    thread = threading.Thread(target=watcher.run, daemon=True)
    thread.start()
    _wait_until(lambda: fast_path_status(config)[0].effective_mode == "idle")

    def initial_sync_complete() -> bool:
        with connect(config.database.path) as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM audit_events WHERE event_type='imap.fast_sync.succeeded'"
                ).fetchone()
                is not None
            )

    _wait_until(initial_sync_complete)
    with connect(config.database.path) as connection:
        baseline_event_id = int(
            connection.execute("SELECT COALESCE(MAX(id), 0) FROM audit_events").fetchone()[0]
        )
    raw = _raw("<idle-acceptance@example.test>", "idle acceptance", "idle-distinctive-token")
    append_started = time.monotonic()
    client = imaplib.IMAP4("127.0.0.1", port)
    try:
        assert client.login("fixture", "fixture-password")[0] == "OK"
        assert client.append(encode_mailbox_name("INBOX"), None, None, raw)[0] == "OK"
    finally:
        client.logout()

    def searchable() -> bool:
        try:
            return bool(search_canonical_messages(config, "idle-distinctive-token", scope="all"))
        except NotmuchError:
            return False

    post_events: list[sqlite3.Row] = []

    def post_baseline_facts_complete() -> bool:
        nonlocal post_events
        if not searchable():
            return False
        with connect(config.database.path) as connection:
            post_events = connection.execute(
                "SELECT event_type, details_json FROM audit_events WHERE id>? ORDER BY id",
                (baseline_event_id,),
            ).fetchall()
        return any(row[0] == "imap.watch.event" and "EXISTS" in str(row[1]) for row in post_events)

    # Arm-before-sync intentionally permits acquisition to race ahead of notification audit
    # recording. The acceptance contract is prompt searchable acquisition while IDLE is armed,
    # EXISTS observation, and no polling—not a causal order between those audit events.
    _wait_until(post_baseline_facts_complete)
    latency = time.monotonic() - append_started
    print(f"Dovecot IDLE APPEND-to-searchable latency: {latency:.3f}s")
    assert latency <= 10, f"APPEND-to-searchable latency was {latency:.3f}s"
    uidvalidity, snapshot = _snapshot(port)
    assert len(snapshot) == 1
    uid, (flags, server_raw) = next(iter(snapshot.items()))
    found: list[SearchResult] = []

    def final_search() -> bool:
        nonlocal found
        found = search_canonical_messages(config, "idle-distinctive-token", scope="all")
        return bool(found)

    _wait_until(final_search)
    assert len(found) == 1
    canonical_path = found[0].canonical_message.local_path
    assert canonical_path.read_bytes() == server_raw == raw
    digest = hashlib.sha256(server_raw).hexdigest()
    print(f"Dovecot IDLE server/canonical SHA-256: {digest}")
    assert found[0].canonical_message.sha256 == digest
    assert _snapshot(port)[1][uid][0] == flags
    with connect(config.database.path) as connection:
        remote = connection.execute(
            "SELECT uidvalidity, remote_uid FROM remote_messages WHERE remote_folder='INBOX'"
        ).fetchone()
    assert tuple(remote) == (uidvalidity, uid)
    assert not any(row[0] == "imap.watch.poll" for row in post_events)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert fast_path_status(config)[0].effective_mode == "stopped"
