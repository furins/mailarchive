"""Disposable loopback Dovecot + real mbsync acquisition integration coverage."""

from __future__ import annotations

import hashlib
import imaplib
import os
import pwd
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Generator
from pathlib import Path

import pytest
import yaml

import mailarchive.imap as imap_module
from mailarchive.config import load_config
from mailarchive.db import connect
from mailarchive.imap import ImapMbsyncAdapter, managed_layout
from mailarchive.notmuch import NotmuchAdapter, search_canonical_messages


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _raw(message_id: str, subject: str) -> bytes:
    return (
        f"From: source@example.test\r\nTo: archive@example.test\r\nSubject: {subject}\r\n"
        f"Message-ID: {message_id}\r\nDate: Wed, 1 Jan 2020 12:00:00 +0000\r\n\r\n"
        f"body for {subject}\r\n"
    ).encode()


def _safe_dovecot_log(path: Path) -> str:
    """Attach bounded server diagnostics without password material on test failure."""
    if not path.exists():
        return "no Dovecot log available"
    return path.read_text(encoding="utf-8", errors="replace").replace(
        "fixture-password", "<redacted>"
    )[-2_000:]


@pytest.fixture
def mbsync_visible_root() -> Generator[Path, None, None]:
    """The sandbox runs mbsync under a restricted UID, unlike normal CI runners."""
    root = Path(tempfile.mkdtemp(prefix="mailarchive-mbsync-", dir="/tmp"))
    root.chmod(0o777)
    try:
        yield root
    finally:
        shutil.rmtree(root)


@pytest.fixture
def dovecot_loopback(
    tmp_path: Path,
) -> Generator[tuple[int, Path, Path, subprocess.Popen[str]], None, None]:
    """Start an empty, plaintext-only-on-loopback Dovecot instance for tests."""
    root = tmp_path / "dovecot"
    local_user = pwd.getpwuid(os.getuid()).pw_name
    mail = root / "mail"
    for part in ("cur", "new", "tmp"):
        (mail / part).mkdir(parents=True)
    port = _free_port()
    password_file = root / "passwd"
    password_file.write_text("fixture:{PLAIN}fixture-password\n", encoding="utf-8")
    config = root / "dovecot.conf"
    log_path = root / "dovecot.log"
    config.write_text(
        "\n".join(
            (
                "dovecot_config_version = 2.4.0",
                "dovecot_storage_version = 2.4.0",
                f"default_internal_user = {local_user}",
                f"default_internal_group = {local_user}",
                f"default_login_user = {local_user}",
                "protocols = imap",
                "listen = 127.0.0.1",
                f"base_dir = {root / 'run'}",
                f"state_dir = {root / 'state'}",
                f"log_path = {log_path}",
                "ssl = no",
                "auth_mechanisms = plain",
                "mail_driver = maildir",
                f"mail_path = {mail}",
                "passdb passwd-file {",
                f"  passwd_file_path = {password_file}",
                "}",
                "userdb static {",
                "  static_allow_all_users = yes",
                "  userdb_fields {",
                f"    uid = {os.getuid()}",
                f"    gid = {os.getgid()}",
                f"    home = {root}",
                "  }",
                "}",
                "service imap-login {",
                f"  user = {local_user}",
                "  chroot =",
                "  inet_listener imap {",
                f"    port = {port}",
                "  }",
                "}",
                "service imap {",
                f"  user = {local_user}",
                "  chroot =",
                "}",
                "",
            )
        ),
        encoding="utf-8",
    )
    process = subprocess.Popen(
        ["dovecot", "-F", "-c", str(config)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            if time.monotonic() >= deadline:
                stdout, stderr = process.communicate(timeout=1)
                raise RuntimeError(f"Dovecot did not start: {stdout}\n{stderr}") from None
            time.sleep(0.05)
    try:
        yield port, mail, log_path, process
    finally:
        process.terminate()
        process.wait(timeout=10)


def _imap_snapshot(port: int) -> tuple[int, dict[int, tuple[tuple[bytes, ...], bytes]]]:
    client = imaplib.IMAP4("127.0.0.1", port)
    try:
        assert client.login("fixture", "fixture-password")[0] == "OK"
        assert client.select("INBOX", readonly=True)[0] == "OK"
        uidvalidity = int(client.response("UIDVALIDITY")[1][0])
        status, data = client.uid("search", "ALL")
        assert status == "OK"
        snapshot: dict[int, tuple[tuple[bytes, ...], bytes]] = {}
        for uid in data[0].split():
            status, fetched = client.uid("fetch", uid, "(FLAGS BODY.PEEK[])")
            assert status == "OK"
            metadata, raw = fetched[0]
            assert isinstance(metadata, bytes) and isinstance(raw, bytes)
            flags = tuple(sorted(imaplib.ParseFlags(metadata)))
            snapshot[int(uid)] = (flags, raw)
        return uidvalidity, snapshot
    finally:
        client.logout()


@pytest.mark.skipif(not Path("/usr/sbin/dovecot").exists(), reason="requires dovecot-imapd")
def test_loopback_dovecot_mbsync_preserves_server_and_canonical_bytes(
    config_file: Path,
    dovecot_loopback: tuple[int, Path, Path, subprocess.Popen[str]],
    mbsync_visible_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, mail, log_path, _ = dovecot_loopback
    first = _raw("<loopback-one@example.test>", "loopback one")
    (mail / "new" / "seed-one").write_bytes(first)
    uidvalidity, before = _imap_snapshot(port)
    values = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    values["accounts"]["test"]["imap"] = {
        "host": "127.0.0.1", "port": port, "username": "fixture",
        "tls_mode": "INSECURE_LOOPBACK", "folders": ["INBOX"],
    }
    values["archive"]["root"] = str(mbsync_visible_root / "archive")
    values["database"]["path"] = str(mbsync_visible_root / "state" / "mailarchive.sqlite3")
    config_file.write_text(yaml.safe_dump(values), encoding="utf-8")
    monkeypatch.setenv("MAILARCHIVE_TEST_SECRET", "fixture-password")
    config = load_config(config_file)
    original_write = imap_module.write_managed_config

    def sandbox_visible_config(*args: object) -> object:
        layout = original_write(*args)  # type: ignore[arg-type]
        layout.config_path.chmod(0o644)
        return layout

    monkeypatch.setattr(imap_module, "write_managed_config", sandbox_visible_config)
    try:
        results = ImapMbsyncAdapter(config).sync("test", "INBOX")
    except Exception as error:
        diagnostic = _safe_dovecot_log(log_path)
        raise AssertionError(f"loopback acquisition failed; Dovecot log:\n{diagnostic}") from error
    assert len(results) == len(before) == 1
    assert _imap_snapshot(port) == (uidvalidity, before)
    layout = managed_layout(config, config.accounts[0], "INBOX")
    mirror = next((layout.mirror_mailbox / "INBOX" / "cur").iterdir())
    assert mirror.read_bytes() == first == results[0].canonical_message.local_path.read_bytes()
    assert results[0].canonical_message.sha256 == hashlib.sha256(first).hexdigest()
    with connect(config.database.path) as connection:
        remote = connection.execute(
            "SELECT uidvalidity, remote_uid FROM remote_messages"
        ).fetchone()
        link = connection.execute(
            "SELECT canonical_message_id FROM remote_canonical_links"
        ).fetchone()
    assert tuple(remote) == (uidvalidity, next(iter(before)))
    assert str(link[0]) == results[0].canonical_message.id
    assert ImapMbsyncAdapter(config).sync("test", "INBOX")[0].created is False
    second = _raw("<loopback-two@example.test>", "loopback two")
    (mail / "new" / "seed-two").write_bytes(second)
    _, after_add = _imap_snapshot(port)
    next_results = ImapMbsyncAdapter(config).sync("test", "INBOX")
    assert sum(item.created for item in next_results) == 1
    assert _imap_snapshot(port)[1] == after_add
    NotmuchAdapter(config).refresh()
    assert search_canonical_messages(config, "loopback two")
