"""Direct, UID-only, read-only IMAP acquisition preserving BODY.PEEK[] bytes."""

from __future__ import annotations

import hashlib
import imaplib
import json
import os
import re
import sqlite3
import ssl
import tempfile
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from mailarchive.db import account_id, connect, initialize, insert_audit_event, utc_now
from mailarchive.ingest import IngestResult, ingest_file
from mailarchive.models import AccountConfig, AppConfig, CanonicalMessage

_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
_UID_RESPONSE = re.compile(rb"(?:^|[\s(])UID\s+(\d+)(?=\s|\))")
_BODY_LITERAL = re.compile(rb"(?:^|[\s(])BODY(?:\.PEEK)?\[\]\s+\{(\d+)\}")


class ImapError(RuntimeError):
    """A direct IMAP action was refused or did not complete safely."""


@dataclass(frozen=True)
class FetchResult:
    uid: int
    raw_bytes: bytes


def _credential_variable(reference: str) -> str:
    if not reference.startswith("env:") or not _ENV_NAME.fullmatch(reference[4:]):
        raise ImapError("IMAP credentials must use config_ref: env:VARIABLE_NAME")
    return reference[4:]


def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def encode_mailbox_name(folder: str) -> str:
    """Encode an ASCII mailbox as an IMAP quoted string for imaplib command arguments."""
    if not folder or not folder.isascii() or any(character in folder for character in "\x00\r\n"):
        raise ImapError("IMAP mailbox must be a non-empty ASCII name without CR, LF, or NUL")
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_fetch_response(requested_uid: int, data: Sequence[object]) -> FetchResult:
    """Validate the one BODY.PEEK[] literal returned for an explicit UID FETCH."""
    matches: list[FetchResult] = []
    for item in data:
        if isinstance(item, bytes):
            # imaplib represents the closing FETCH parenthesis as a separate item.
            if item.strip() == b")":
                continue
            raise ImapError("unexpected IMAP FETCH response fragment")
        if not isinstance(item, tuple):
            raise ImapError("malformed IMAP FETCH response")
        pair = cast(tuple[object, ...], item)
        if len(pair) != 2:
            raise ImapError("malformed IMAP FETCH response")
        metadata, literal = pair
        if not isinstance(metadata, bytes) or not isinstance(literal, bytes):
            raise ImapError("malformed IMAP FETCH literal response")
        body_matches = _BODY_LITERAL.findall(metadata)
        if len(body_matches) != 1:
            raise ImapError("IMAP FETCH response lacks one BODY[] literal")
        if len(literal) != int(body_matches[0]):
            raise ImapError("IMAP FETCH literal length does not match response metadata")
        uid_matches = _UID_RESPONSE.findall(metadata)
        if len(uid_matches) != 1:
            raise ImapError("IMAP FETCH response lacks UID")
        uid = int(uid_matches[0])
        if uid != requested_uid:
            raise ImapError("IMAP FETCH response has an unexpected UID")
        matches.append(FetchResult(uid=uid, raw_bytes=literal))
    if len(matches) != 1:
        raise ImapError("IMAP FETCH response is missing or duplicated BODY[] literals")
    return matches[0]


def _parse_uidvalidity(client: imaplib.IMAP4) -> int:
    response = client.response("UIDVALIDITY")[1]
    if not response or not isinstance(response[0], bytes) or not response[0].isdigit():
        raise ImapError("selected IMAP mailbox lacks a valid UIDVALIDITY")
    value = int(response[0])
    if value <= 0:
        raise ImapError("selected IMAP mailbox has an invalid UIDVALIDITY")
    return value


@contextmanager
def folder_lock(
    config: AppConfig, account: AccountConfig, folder: str
) -> Generator[None, None, None]:
    import fcntl

    digest = hashlib.sha256(f"{account.name}\0{folder}".encode()).hexdigest()[:16]
    lock = config.archive.root.resolve() / "state" / "locks" / f"imap-{digest}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ImapError("IMAP account/folder synchronization is already running") from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _audit(
    config: AppConfig, account_name: str, event: str, result: str, details: dict[str, object]
) -> None:
    with connect(config.database.path) as connection:
        insert_audit_event(
            connection,
            actor="mailarchive.imap",
            event_type=event,
            result=result,
            account_id=account_id(connection, account_name),
            details_json=json.dumps(details, sort_keys=True),
        )
        connection.commit()


def _linked_uids(
    connection: sqlite3.Connection, account: int, folder: str, uidvalidity: int
) -> set[int]:
    rows = connection.execute(
        """SELECT remote_uid FROM remote_messages JOIN remote_canonical_links
           ON remote_canonical_links.remote_message_id = remote_messages.id
           WHERE account_id=? AND remote_folder=? AND uidvalidity=?
             AND identity_confidence='proven'""",
        (account, folder, uidvalidity),
    ).fetchall()
    return {int(row[0]) for row in rows}


def _refresh_last_seen(
    connection: sqlite3.Connection,
    account: int,
    folder: str,
    uidvalidity: int,
    remote_uids: set[int],
    observed_at: str,
) -> None:
    """Record that already-proven identities were observed without refetching bodies."""
    ordered_uids = sorted(remote_uids)
    for start in range(0, len(ordered_uids), 500):
        chunk = ordered_uids[start : start + 500]
        placeholders = ", ".join("?" for _ in chunk)
        connection.execute(
            "UPDATE remote_messages SET last_seen_at=? "
            "WHERE account_id=? AND remote_folder=? AND uidvalidity=? "
            "AND identity_confidence='proven' AND remote_uid IN (" + placeholders + ")",
            (observed_at, account, folder, uidvalidity, *chunk),
        )


def register_remote_link(
    config: AppConfig,
    account_name: str,
    folder: str,
    uidvalidity: int,
    uid: int,
    canonical: CanonicalMessage,
    observed_at: str | None = None,
) -> None:
    conflict = False
    with connect(config.database.path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            aid = account_id(connection, account_name)
            if aid is None or aid != canonical.account_id:
                raise ImapError("canonical account is not active for remote linking")
            remote_id = (
                f"{aid}:{hashlib.sha256(f'{folder}\0{uidvalidity}\0{uid}'.encode()).hexdigest()}"
            )
            now = observed_at or utc_now()
            connection.execute(
                """INSERT INTO remote_messages(
                   id, account_id, remote_folder, uidvalidity, remote_uid, message_id_header,
                   first_seen_at, last_seen_at, remote_present, identity_confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'proven')
                   ON CONFLICT(account_id, remote_folder, uidvalidity, remote_uid)
                   DO UPDATE SET last_seen_at=excluded.last_seen_at, remote_present=1""",
                (remote_id, aid, folder, uidvalidity, uid, canonical.message_id_header, now, now),
            )
            row = connection.execute(
                "SELECT id FROM remote_messages WHERE account_id=? AND remote_folder=? "
                "AND uidvalidity=? AND remote_uid=?",
                (aid, folder, uidvalidity, uid),
            ).fetchone()
            existing = connection.execute(
                "SELECT canonical_message_id FROM remote_canonical_links WHERE remote_message_id=?",
                (str(row[0]),),
            ).fetchone()
            if existing is not None and str(existing[0]) != canonical.id:
                conflict = True
            else:
                connection.execute(
                    "INSERT OR IGNORE INTO remote_canonical_links("
                    "remote_message_id, canonical_message_id, link_reason, created_at) "
                    "VALUES (?, ?, 'imap-uid-body-peek', ?)",
                    (str(row[0]), canonical.id, now),
                )
                insert_audit_event(
                    connection,
                    actor="mailarchive.imap",
                    event_type="imap.remote_link.created",
                    result="success",
                    account_id=aid,
                    canonical_message_id=canonical.id,
                    details_json=json.dumps(
                        {"folder": folder, "uid": uid, "uidvalidity": uidvalidity}
                    ),
                )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
    if conflict:
        _audit(
            config,
            account_name,
            "imap.remote_link.failed",
            "conflict",
            {"folder": folder, "uid": uid, "uidvalidity": uidvalidity},
        )
        raise ImapError("remote identity conflicts with a different canonical message")


class ImapAdapter:
    """Acquire exact IMAP literals without issuing any mutating IMAP command."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def _open(self, account: AccountConfig) -> imaplib.IMAP4:
        assert account.imap is not None
        settings = account.imap
        if settings.tls_mode == "IMAPS":
            return imaplib.IMAP4_SSL(
                settings.host,
                settings.port,
                ssl_context=_ssl_context(),
                timeout=settings.connection_timeout_seconds,
            )
        if settings.tls_mode == "STARTTLS":
            client = imaplib.IMAP4(
                settings.host, settings.port, timeout=settings.connection_timeout_seconds
            )
            client.starttls(ssl_context=_ssl_context())
            return client
        return imaplib.IMAP4(
            settings.host, settings.port, timeout=settings.connection_timeout_seconds
        )

    def sync(self, account_name: str, folder: str) -> list[IngestResult]:
        account = next((item for item in self.config.accounts if item.name == account_name), None)
        if account is None:
            raise ImapError(f"unknown account: {account_name}")
        if not account.enabled:
            raise ImapError(f"account is disabled: {account_name}")
        if account.kind != "imap" or account.imap is None:
            raise ImapError("account is not configured for IMAP synchronization")
        if folder not in account.imap.folders:
            raise ImapError(f"folder is not configured for account: {folder}")
        variable = _credential_variable(account.config_ref)
        password = os.environ.get(variable)
        if not password:
            raise ImapError(f"missing credential environment variable: {variable}")
        initialize(self.config.database.path, self.config.accounts)
        with folder_lock(self.config, account, folder):
            _audit(self.config, account_name, "imap.sync.started", "started", {"folder": folder})
            client: imaplib.IMAP4 | None = None
            try:
                client = self._open(account)
                if client.login(account.imap.username, password)[0] != "OK":
                    raise ImapError("IMAP authentication failed")
                mailbox = encode_mailbox_name(folder)
                if client.select(mailbox, readonly=True)[0] != "OK":
                    raise ImapError("IMAP mailbox cannot be selected read-only")
                uidvalidity = _parse_uidvalidity(client)
                status, uid_data = client.uid("search", "ALL")
                if status != "OK" or len(uid_data) != 1 or not isinstance(uid_data[0], bytes):
                    raise ImapError("IMAP UID discovery failed")
                uid_tokens = uid_data[0].split()
                if any(not value.isdigit() or int(value) <= 0 for value in uid_tokens):
                    raise ImapError("IMAP UID discovery returned an invalid UID")
                remote_uids = {int(value) for value in uid_tokens}
                observed_at = utc_now()
                with connect(self.config.database.path) as connection:
                    aid = account_id(connection, account_name)
                    if aid is None:
                        raise ImapError("account is not active in local state")
                    _refresh_last_seen(
                        connection, aid, folder, uidvalidity, remote_uids, observed_at
                    )
                    known = _linked_uids(connection, aid, folder, uidvalidity)
                results: list[IngestResult] = []
                for uid in sorted(remote_uids - known):
                    status, data = client.uid("fetch", str(uid), "(UID BODY.PEEK[])")
                    if status != "OK":
                        raise ImapError("IMAP BODY.PEEK[] fetch failed")
                    fetched = parse_fetch_response(uid, data)
                    with tempfile.NamedTemporaryFile(suffix=".eml", delete=False) as temporary:
                        temporary.write(fetched.raw_bytes)
                        source = Path(temporary.name)
                    try:
                        result = ingest_file(self.config, source, account_name)
                    finally:
                        source.unlink(missing_ok=True)
                    register_remote_link(
                        self.config,
                        account_name,
                        folder,
                        uidvalidity,
                        uid,
                        result.canonical_message,
                        observed_at,
                    )
                    results.append(result)
                    if result.created:
                        _audit(
                            self.config,
                            account_name,
                            "imap.message.imported",
                            "success",
                            {"folder": folder, "sha256": result.canonical_message.sha256},
                        )
            except Exception as error:
                _audit(
                    self.config,
                    account_name,
                    "imap.sync.failed",
                    "failed",
                    {"folder": folder, "reason": type(error).__name__},
                )
                raise
            finally:
                if client is not None:
                    try:
                        client.logout()
                    except (OSError, imaplib.IMAP4.error):
                        pass
            _audit(
                self.config,
                account_name,
                "imap.sync.succeeded",
                "success",
                {
                    "folder": folder,
                    "remote_seen": len(remote_uids),
                    "fetched": len(results),
                    "imported": sum(item.created for item in results),
                },
            )
            return results
