"""Disposable loopback Dovecot coverage for direct read-only IMAP acquisition."""

from __future__ import annotations

import hashlib
import imaplib
import json
import os
import pwd
import socket
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
import yaml

from mailarchive.classification import ClassificationResult, apply_classification
from mailarchive.cli import main
from mailarchive.config import load_config
from mailarchive.db import account_id, connect, initialize, utc_now
from mailarchive.fastpath import FastPathWatcher, fast_path_status
from mailarchive.imap import ImapAdapter, encode_mailbox_name
from mailarchive.imap_mutation import ImapClient, ImapMutationAdapter
from mailarchive.ingest import ingest_bytes
from mailarchive.models import AppConfig
from mailarchive.notmuch import (
    NotmuchAdapter,
    NotmuchError,
    SearchResult,
    search_canonical_messages,
)
from mailarchive.remote_mutation import ImapDeletionTarget


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


def _mark_deleted_fixture(port: int, uid: int) -> None:
    """Fixture-only setup of an unrelated deleted message; never MailArchive code."""
    client = imaplib.IMAP4("127.0.0.1", port)
    try:
        assert client.login("fixture", "fixture-password")[0] == "OK"
        assert client.select('"INBOX"', readonly=False)[0] == "OK"
        assert client.uid("STORE", str(uid), "+FLAGS.SILENT", r"(\Deleted)")[0] == "OK"
    finally:
        client.logout()


class _RecordingImapClient:
    """Narrow test proxy that forwards to Dovecot while recording adapter commands."""

    def __init__(self, client: imaplib.IMAP4) -> None:
        self.client = client
        self.commands: list[tuple[str, tuple[object, ...]]] = []

    def login(self, user: str, password: str) -> tuple[str, list[object]]:
        self.commands.append(("LOGIN", ()))
        return cast(tuple[str, list[object]], self.client.login(user, password))

    def capability(self) -> tuple[str, list[object]]:
        self.commands.append(("CAPABILITY", ()))
        return cast(tuple[str, list[object]], self.client.capability())

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[object]]:
        self.commands.append(("SELECT", (readonly,)))
        return cast(tuple[str, list[object]], self.client.select(mailbox, readonly=readonly))

    def response(self, code: str) -> tuple[str, list[object]]:
        self.commands.append(("RESPONSE", (code,)))
        return cast(tuple[str, list[object]], self.client.response(code))

    def uid(self, command: str, *args: str) -> tuple[str, list[object]]:
        self.commands.append((f"UID {command.upper()}", args))
        return cast(tuple[str, list[object]], self.client.uid(command, *args))

    def unselect(self) -> tuple[str, list[object]]:
        self.commands.append(("UNSELECT", ()))
        return cast(tuple[str, list[object]], self.client.unselect())

    def logout(self) -> tuple[str, list[object]]:
        self.commands.append(("LOGOUT", ()))
        return cast(tuple[str, list[object]], self.client.logout())


def _imap_observer(
    config_file: Path, port: int, monkeypatch: pytest.MonkeyPatch
) -> tuple[ImapMutationAdapter, list[_RecordingImapClient], int]:
    values = yaml.safe_load(config_file.read_text())
    values["accounts"]["test"]["imap"] = {
        "host": "127.0.0.1",
        "port": port,
        "username": "fixture",
        "tls_mode": "INSECURE_LOOPBACK",
        "folders": ["INBOX"],
    }
    config_file.write_text(yaml.safe_dump(values))
    monkeypatch.setenv("MAILARCHIVE_TEST_SECRET", "fixture-password")
    config = load_config(config_file)
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as db:
        local_account_id = account_id(db, "test")
    assert local_account_id is not None
    recordings: list[_RecordingImapClient] = []

    def client_factory(_account: object) -> ImapClient:
        client = _RecordingImapClient(imaplib.IMAP4("127.0.0.1", port))
        recordings.append(client)
        return cast(ImapClient, client)

    return (
        ImapMutationAdapter(config, "test", client_factory=client_factory),
        recordings,
        local_account_id,
    )


def _imap_reconciliation_config(
    config_file: Path, port: int, monkeypatch: pytest.MonkeyPatch
) -> tuple[AppConfig, int]:
    values = yaml.safe_load(config_file.read_text())
    values["accounts"]["test"]["imap"] = {
        "host": "127.0.0.1",
        "port": port,
        "username": "fixture",
        "tls_mode": "INSECURE_LOOPBACK",
        "folders": ["INBOX"],
    }
    config_file.write_text(yaml.safe_dump(values))
    monkeypatch.setenv("MAILARCHIVE_TEST_SECRET", "fixture-password")
    config = load_config(config_file)
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as db:
        local_account_id = account_id(db, "test")
    assert local_account_id is not None
    return config, local_account_id


def _observe_target(
    account_id_value: int, uidvalidity: int, uid: int, raw: bytes
) -> ImapDeletionTarget:
    return ImapDeletionTarget(
        remote_message_id="historical-remote",
        canonical_message_id="historical-canonical",
        account_id=account_id_value,
        account_name="test",
        provider_kind="imap",
        canonical_sha256=hashlib.sha256(raw).hexdigest(),
        remote_folder="INBOX",
        uidvalidity=uidvalidity,
        remote_uid=uid,
    )


def _assert_observe_trace_is_read_only(recordings: list[_RecordingImapClient]) -> None:
    commands = [command for recording in recordings for command, _args in recording.commands]
    assert not {"UID STORE", "UID EXPUNGE", "STORE", "EXPUNGE", "CLOSE"} & set(commands)
    assert any(
        command == "SELECT" and arguments == (True,)
        for recording in recordings
        for command, arguments in recording.commands
    )


def _seed_halted_imap_reconciliation(
    config: AppConfig,
    *,
    account_id_value: int,
    uidvalidity: int,
    uid: int,
    raw: bytes,
    historical_uidvalidity: int | None = None,
    include_later_planned: bool = False,
) -> tuple[int, int]:
    """Create only the local historical rows needed by the read-only recovery path."""
    canonical = apply_classification(
        config,
        ingest_bytes(config, raw, "test").canonical_message,
        ClassificationResult("ham", None, "fixture", "pytest"),
    )
    now = utc_now()
    namespace = uidvalidity if historical_uidvalidity is None else historical_uidvalidity
    with connect(config.database.path) as db:
        db.execute(
            """INSERT INTO remote_messages(
            id,account_id,provider_kind,remote_folder,uidvalidity,remote_uid,first_seen_at,
            last_seen_at,remote_present,identity_confidence)
            VALUES('historical',?,'imap','INBOX',?,?,?, ?,1,'proven')""",
            (account_id_value, namespace, uid, now, now),
        )
        db.execute(
            "INSERT INTO remote_canonical_links VALUES('historical',?,'fixture',?)",
            (canonical.id, now),
        )
        evaluation_run = db.execute(
            """INSERT INTO deletion_evaluation_runs(evaluated_at,policy_version)
            VALUES(?,'retention-v1')""",
            (now,),
        )
        evaluation = db.execute(
            """INSERT INTO deletion_evaluations(
            evaluation_run_id,remote_message_id,canonical_message_id,
            evaluated_at,eligible,reason_codes_json,policy_version,remote_retention_days,
            required_verified_backups,verified_repository_count,retention_deadline)
            VALUES(?, 'historical', ?, ?, 1, '[]', 'retention-v1', 365, 2, 2, ?)""",
            (evaluation_run.lastrowid, canonical.id, now, now),
        )
        source = db.execute(
            """INSERT INTO remote_mutation_runs(
            requested_at,completed_at,mode,status,account_filter,
            requested_limit,effective_max_per_run,effective_max_per_account,eligible_count,selected_count,
            skipped_limit_count,policy_version)
            VALUES(?,?,'dry-run','completed','test',NULL,10,10,1,1,0,'retention-v1')""",
            (now, now),
        )
        run = db.execute(
            """INSERT INTO remote_mutation_runs(
            requested_at,mode,status,account_filter,requested_limit,
            effective_max_per_run,effective_max_per_account,eligible_count,selected_count,skipped_limit_count,
            policy_version,source_plan_run_id,authorization_method)
            VALUES(?,'production-execute','halted','test',NULL,10,10,1,1,0,'retention-v1',?,
            'account-opt-in+explicit-plan-v1')""",
            (now, source.lastrowid),
        )
        mutation = db.execute(
            """INSERT INTO remote_mutations(mutation_run_id,deletion_evaluation_id,account_id,
            remote_message_id,canonical_message_id,provider_kind,operation,remote_folder,uidvalidity,
            remote_uid,provider_message_id,canonical_sha256,target_fingerprint_sha256,dry_run,requested_at,
            started_at,status,provider_response_summary,error_code)
            VALUES(?,?,?,?,?,'imap','delete','INBOX',?,?,NULL,?,?,0,?,?,'unknown',
            'outcome-uncertain','TRANSPORT_UNKNOWN')""",
            (
                run.lastrowid,
                evaluation.lastrowid,
                account_id_value,
                "historical",
                canonical.id,
                namespace,
                uid,
                canonical.sha256,
                "0" * 64,
                now,
                now,
            ),
        )
        if include_later_planned:
            db.execute(
                """INSERT INTO remote_messages(
                id,account_id,provider_kind,remote_folder,uidvalidity,remote_uid,first_seen_at,
                last_seen_at,remote_present,identity_confidence)
                VALUES('later',?,'imap','INBOX',?,?,?, ?,1,'proven')""",
                (account_id_value, namespace, uid + 1, now, now),
            )
            db.execute(
                """INSERT INTO remote_mutations(mutation_run_id,deletion_evaluation_id,account_id,
                remote_message_id,canonical_message_id,provider_kind,operation,remote_folder,uidvalidity,
                remote_uid,provider_message_id,canonical_sha256,target_fingerprint_sha256,dry_run,requested_at,status)
                VALUES(?,?,?,?,?,'imap','delete','INBOX',?,?,NULL,?,?,0,?,'planned')""",
                (
                    run.lastrowid,
                    evaluation.lastrowid,
                    account_id_value,
                    "later",
                    canonical.id,
                    namespace,
                    uid + 1,
                    canonical.sha256,
                    "1" * 64,
                    now,
                ),
            )
        db.commit()
    assert run.lastrowid is not None and mutation.lastrowid is not None
    return int(run.lastrowid), int(mutation.lastrowid)


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("present", "confirmed-present-match"),
        ("absent", "confirmed-absent"),
        ("deleted", "identity-conflict"),
        ("uidvalidity", "identity-conflict"),
        ("sha", "identity-conflict"),
    ],
)
def test_dovecot_imap_observe_is_read_only_and_exact(
    config_file: Path,
    dovecot_loopback: tuple[int, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected: str,
) -> None:
    """E1b2: real Dovecot observation never changes any mailbox identity or bytes."""
    port, mail, _ = dovecot_loopback
    target_raw = _raw("<observe-target@test>", "observe target")
    unrelated_raw = _raw("<observe-unrelated@test>", "observe unrelated")
    (mail / "new" / "target").write_bytes(target_raw)
    (mail / "new" / "unrelated").write_bytes(unrelated_raw)
    uidvalidity, snapshot = _snapshot(port)
    target_uid = next(uid for uid, (_flags, raw) in snapshot.items() if raw == target_raw)
    unrelated_uid = next(uid for uid, (_flags, raw) in snapshot.items() if raw == unrelated_raw)
    if case == "deleted":
        _mark_deleted_fixture(port, target_uid)
        uidvalidity, snapshot = _snapshot(port)

    adapter, recordings, local_account_id = _imap_observer(config_file, port, monkeypatch)
    target = _observe_target(local_account_id, uidvalidity, target_uid, target_raw)
    if case == "absent":
        target = replace(target, remote_uid=max(snapshot) + 100)
    elif case == "uidvalidity":
        target = replace(target, uidvalidity=uidvalidity + 1)
    elif case == "sha":
        target = replace(target, canonical_sha256="0" * 64)

    assert adapter.observe(target).state == expected
    _assert_observe_trace_is_read_only(recordings)
    assert _snapshot(port) == (uidvalidity, snapshot)
    assert _snapshot(port)[1][unrelated_uid] == snapshot[unrelated_uid]
    if case == "deleted":
        assert _snapshot(port)[1][target_uid] == snapshot[target_uid]


@pytest.mark.parametrize(
    ("case", "expected_status", "expected_error", "reconciled"),
    [
        ("absent", "succeeded", "NONE", True),
        ("present", "failed", "RECONCILED_PRESENT", True),
        ("uidvalidity", "unknown", "TRANSPORT_UNKNOWN", False),
    ],
)
def test_default_reconciliation_uses_real_dovecot_imap_observer(
    config_file: Path,
    dovecot_loopback: tuple[int, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    expected_status: str,
    expected_error: str,
    reconciled: bool,
) -> None:
    """E1e1: default reconciliation factory reaches the real read-only IMAP adapter."""
    port, mail, _ = dovecot_loopback
    target_raw = _raw("<reconcile-target@test>", "reconcile target")
    unrelated_raw = _raw("<reconcile-unrelated@test>", "reconcile unrelated")
    (mail / "new" / "target").write_bytes(target_raw)
    (mail / "new" / "unrelated").write_bytes(unrelated_raw)
    uidvalidity, snapshot = _snapshot(port)
    target_uid = next(uid for uid, (_flags, raw) in snapshot.items() if raw == target_raw)
    unrelated_uid = next(uid for uid, (_flags, raw) in snapshot.items() if raw == unrelated_raw)
    config, local_account_id = _imap_reconciliation_config(config_file, port, monkeypatch)
    historical_uid = target_uid if case != "absent" else max(snapshot) + 100
    historical_uidvalidity = uidvalidity if case != "uidvalidity" else uidvalidity + 1
    run_id, mutation_id = _seed_halted_imap_reconciliation(
        config,
        account_id_value=local_account_id,
        uidvalidity=uidvalidity,
        uid=historical_uid,
        raw=target_raw,
        historical_uidvalidity=historical_uidvalidity,
        include_later_planned=case == "absent",
    )
    recordings: list[_RecordingImapClient] = []
    real_imap4 = imaplib.IMAP4

    def recording_imap4(
        host: str = "", port: int = 143, timeout: float | None = None
    ) -> _RecordingImapClient:
        client = _RecordingImapClient(real_imap4(host, port, timeout=timeout))
        recordings.append(client)
        return client

    import mailarchive.imap_mutation as imap_mutation

    monkeypatch.setattr(imap_mutation.imaplib, "IMAP4", recording_imap4)

    def forbidden_delete(self: ImapMutationAdapter, target: object) -> object:
        del self, target
        raise AssertionError("reconciliation must not call delete")

    monkeypatch.setattr(ImapMutationAdapter, "delete", forbidden_delete)
    assert (
        main(
            [
                "remote-mutations",
                "reconcile",
                "--run-id",
                str(run_id),
                "--config",
                str(config_file),
                "--json",
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "halted" and summary["resumed"] is False
    _assert_observe_trace_is_read_only(recordings)
    assert _snapshot(port)[1][unrelated_uid] == snapshot[unrelated_uid]
    with connect(config.database.path) as db:
        row = db.execute(
            "SELECT status,error_code,reconciled_at FROM remote_mutations WHERE id=?",
            (mutation_id,),
        ).fetchone()
        assert tuple(row[:2]) == (expected_status, expected_error)
        assert (row["reconciled_at"] is not None) is reconciled
        run = db.execute(
            "SELECT status FROM remote_mutation_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert run[0] == "halted"
        remote_present = db.execute(
            "SELECT remote_present FROM remote_messages WHERE id='historical'"
        ).fetchone()[0]
        assert remote_present == (0 if case == "absent" else 1)
        if case == "absent":
            assert db.execute(
                """SELECT status FROM remote_mutations
                WHERE mutation_run_id=? AND remote_message_id='later'""",
                (run_id,),
            ).fetchone()[0] == "planned"


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


def test_dovecot_uidplus_exact_production_delete_preserves_unrelated_deleted(
    config_file: Path,
    dovecot_loopback: tuple[int, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """E2b CLI reaches the closed default IMAP production factory on Dovecot."""
    port, mail, _ = dovecot_loopback
    target_raw = _raw("<target@test>", "target")
    normal_raw = _raw("<normal@test>", "normal")
    deleted_raw = _raw("<deleted@test>", "deleted")
    for name, raw in (("target", target_raw), ("normal", normal_raw), ("deleted", deleted_raw)):
        (mail / "new" / name).write_bytes(raw)
    _uidvalidity, before = _snapshot(port)
    deleted_uid = next(uid for uid, (_flags, raw) in before.items() if raw == deleted_raw)
    _mark_deleted_fixture(port, deleted_uid)
    values = yaml.safe_load(config_file.read_text())
    values["accounts"]["test"]["remote_deletion_enabled"] = True
    values["accounts"]["test"]["imap"] = {
        "host": "127.0.0.1",
        "port": port,
        "username": "fixture",
        "tls_mode": "INSECURE_LOOPBACK",
        "folders": ["INBOX"],
    }
    config_file.write_text(yaml.safe_dump(values))
    monkeypatch.setenv("MAILARCHIVE_TEST_SECRET", "fixture-password")
    config = load_config(config_file)
    acquired = ImapAdapter(config).sync("test", "INBOX")
    target = next(
        item.canonical_message
        for item in acquired
        if item.canonical_message.local_path.read_bytes() == target_raw
    )
    target = apply_classification(
        config, target, ClassificationResult("ham", None, "fixture", "pytest")
    )
    local_before = target.local_path.read_bytes()
    with connect(config.database.path) as db:
        db.execute(
            "UPDATE canonical_messages SET archived_at='2025-01-01T00:00:00+00:00' WHERE id=?",
            (target.id,),
        )
        now = "2026-08-13T00:00:00+00:00"
        for name in ("m12b-one", "m12b-two"):
            db.execute(
                """INSERT INTO backup_repositories(
                name,kind,repository_ref,repository_identity,enabled,encryption_mode,
                verification_policy,created_at,updated_at)
                VALUES(?,'borg',?,?,1,'none','borg-archive-data-v1',?,?)""",
                (name, f"/tmp/{name}", name, now, now),
            )
            repository = db.execute(
                "SELECT id FROM backup_repositories WHERE name=?", (name,)
            ).fetchone()[0]
            db.execute(
                """INSERT INTO backup_runs(
                id,repository_id,started_at,completed_at,status,archive_name,
                verification_status,verified_at)
                VALUES(?,?,?,?,'succeeded',?,'verified',?)""",
                (f"{name}-run", repository, now, now, name, now),
            )
            db.execute(
                "INSERT INTO message_backup_evidence VALUES(?,?,1,1,?)",
                (target.id, f"{name}-run", now),
            )
        db.commit()
    client = imaplib.IMAP4("127.0.0.1", port)
    try:
        assert client.login("fixture", "fixture-password")[0] == "OK"
        assert b"UIDPLUS" in b" ".join(cast(list[bytes], client.capability()[1])).upper()
    finally:
        client.logout()
    assert (
        main(
            [
                "remote-delete",
                "--dry-run",
                "--account",
                "test",
                "--limit",
                "1",
                "--config",
                str(config_file),
                "--json",
            ]
        )
        == 0
    )
    source = int(json.loads(capsys.readouterr().out)["run_id"])
    assert (
        main(
            [
                "remote-delete",
                "--execute-plan",
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
    run = int(json.loads(capsys.readouterr().out)["production_run_id"])
    _validity, after = _snapshot(port)
    raws = [raw for _flags, raw in after.values()]
    assert target_raw not in raws and normal_raw in raws and deleted_raw in raws
    assert target.local_path.read_bytes() == local_before
    with connect(config.database.path) as db:
        deleted_remote = db.execute(
            """SELECT r.remote_present,m.status,m.error_code
            FROM remote_messages r JOIN remote_mutations m ON m.remote_message_id=r.id
            WHERE m.mutation_run_id=?""",
            (run,),
        ).fetchone()
        assert deleted_remote is not None and deleted_remote[0] == 0, tuple(deleted_remote or ())
        assert (
            db.execute("SELECT status FROM remote_mutation_runs WHERE id=?", (run,)).fetchone()[0]
            == "completed"
        )
        assert db.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT COUNT(*) FROM message_backup_evidence WHERE canonical_message_id=?",
                (target.id,),
            ).fetchone()[0]
            == 2
        )
