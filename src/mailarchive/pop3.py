"""Direct, read-only POP3 fallback acquisition with UIDL identity.

The stdlib POP3 client rebuilds RETR data from lines, so it is deliberately not
used here: canonical bytes must be the unstuffed POP3 octets, not reconstructed
text.  This adapter contains no DELE command or configuration switch for one.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import ssl
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass

from mailarchive.db import account_id, connect, initialize, insert_audit_event, utc_now
from mailarchive.imap import credential_variable
from mailarchive.ingest import ingest_bytes
from mailarchive.models import AccountConfig, AppConfig, CanonicalMessage, Pop3Config


class Pop3Error(RuntimeError):
    """A POP3 operation was rejected or could not prove a safe local state."""


class Pop3SyncBusyError(Pop3Error):
    """The account's shared acquisition/mutation POP3 lock is already held."""


@contextmanager
def pop3_lock(config: AppConfig, account: AccountConfig) -> Generator[None]:
    """Serialize every POP3 provider snapshot for one account, non-blockingly."""
    import fcntl

    digest = hashlib.sha256(account.name.encode()).hexdigest()[:16]
    path = config.archive.root.resolve() / "state" / "locks" / f"pop3-{digest}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Pop3SyncBusyError("POP3 account synchronization is already running") from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class Pop3SyncResult:
    seen: int
    imported: int
    reused: int
    provider_identities: int


class _Pop3Wire:
    """Small POP3 client which retains RETR octets exactly after dot unstuffing."""

    def __init__(self, settings: Pop3Config) -> None:
        self.settings = settings
        self.sock: socket.socket | ssl.SSLSocket | None = None
        self.buffer = b""
        self.commands: list[str] = []

    def open(self) -> None:
        raw = socket.create_connection(
            (self.settings.host, self.settings.port), self.settings.connection_timeout_seconds
        )
        if self.settings.tls_mode == "POP3S":
            self.sock = ssl.create_default_context().wrap_socket(
                raw, server_hostname=self.settings.host
            )
        else:
            self.sock = raw
        self._positive_line()
        if self.settings.tls_mode == "STARTTLS":
            self.command("STLS")
            assert self.sock is not None
            self.sock = ssl.create_default_context().wrap_socket(
                self.sock, server_hostname=self.settings.host
            )

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.command("QUIT")
            except OSError, Pop3Error:
                pass
            self.sock.close()
            self.sock = None

    def _read_until(self, marker: bytes) -> bytes:
        assert self.sock is not None
        while marker not in self.buffer:
            data = self.sock.recv(65536)
            if not data:
                raise Pop3Error("POP3 server closed the connection unexpectedly")
            self.buffer += data
        value, self.buffer = self.buffer.split(marker, 1)
        return value

    def _positive_line(self) -> bytes:
        line = self._read_until(b"\r\n")
        if not line.startswith(b"+OK"):
            raise Pop3Error("POP3 server rejected a read-only command")
        return line[3:].lstrip()

    def _read_wire_line(self) -> bytes:
        """Read one complete POP3 wire line, retaining its CRLF exactly."""
        assert self.sock is not None
        while b"\r\n" not in self.buffer:
            data = self.sock.recv(65536)
            if not data:
                raise Pop3Error("POP3 server closed the connection unexpectedly")
            self.buffer += data
        line, self.buffer = self.buffer.split(b"\r\n", 1)
        return line + b"\r\n"

    def command(self, command: str) -> bytes:
        if command.split(" ", 1)[0].upper() == "DELE":
            raise AssertionError("POP3 DELE is forbidden")
        if "\r" in command or "\n" in command:
            raise Pop3Error("invalid POP3 command")
        assert self.sock is not None
        self.commands.append(command.split(" ", 1)[0].upper())
        self.sock.sendall(command.encode("ascii") + b"\r\n")
        return self._positive_line()

    def multiline(self, command: str) -> bytes:
        self.command(command)
        result = bytearray()
        while True:
            line = self._read_wire_line()
            if line == b".\r\n":
                return bytes(result)
            if line.startswith(b".."):
                line = line[1:]
            result.extend(line)

    def uidls(self) -> dict[int, str]:
        data = self.multiline("UIDL")
        result: dict[int, str] = {}
        for line in data.split(b"\r\n"):
            if not line:
                continue
            fields = line.split()
            if len(fields) != 2 or not fields[0].isdigit() or int(fields[0]) <= 0:
                raise Pop3Error("POP3 UIDL response is malformed")
            uidl = fields[1].decode("ascii", "strict")
            if (
                not uidl
                or any(c.isspace() for c in uidl)
                or int(fields[0]) in result
                or uidl in result.values()
            ):
                raise Pop3Error("POP3 UIDL response is ambiguous")
            result[int(fields[0])] = uidl
        if not result and data:
            raise Pop3Error("POP3 UIDL response is malformed")
        return result

    def retr(self, number: int) -> bytes:
        if number <= 0:
            raise Pop3Error("invalid POP3 message number")
        return self.multiline(f"RETR {number}")


# Public only for protocol-level acceptance tests; the adapter remains the runtime API.
Pop3Wire = _Pop3Wire


def _audit(
    config: AppConfig, account_name: str, event: str, result: str, details: dict[str, object]
) -> None:
    with connect(config.database.path) as db:
        insert_audit_event(
            db,
            actor="mailarchive.pop3",
            event_type=event,
            result=result,
            account_id=account_id(db, account_name),
            details_json=json.dumps(details, sort_keys=True),
        )
        db.commit()


def _linked_uidls(db: sqlite3.Connection, aid: int) -> set[str]:
    rows = db.execute(
        """SELECT provider_message_id FROM remote_messages JOIN remote_canonical_links
           ON remote_canonical_links.remote_message_id=remote_messages.id
           WHERE account_id=? AND provider_kind='pop3' AND identity_confidence='proven'""",
        (aid,),
    ).fetchall()
    return {str(row[0]) for row in rows}


def register_pop3_link(
    config: AppConfig, account_name: str, uidl: str, canonical: CanonicalMessage
) -> None:
    """Atomically bind exactly one account-scoped UIDL to a canonical object."""
    conflict = False
    with connect(config.database.path) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            aid = account_id(db, account_name)
            if aid is None or aid != canonical.account_id:
                raise Pop3Error("canonical account is not active for POP3 linking")
            now = utc_now()
            remote_id = f"{aid}:{hashlib.sha256(('pop3\\0' + uidl).encode()).hexdigest()}"
            db.execute(
                """INSERT INTO remote_messages(
                   id,account_id,provider_kind,remote_folder,uidvalidity,
                   remote_uid,provider_message_id,provider_thread_id,message_id_header,first_seen_at,
                   last_seen_at,remote_present,identity_confidence)
                   VALUES(?,?,'pop3',NULL,NULL,NULL,?,NULL,?,?,?,1,'proven')
                   ON CONFLICT(account_id,provider_message_id) WHERE provider_kind='pop3'
                   DO UPDATE SET last_seen_at=excluded.last_seen_at,remote_present=1""",
                (remote_id, aid, uidl, canonical.message_id_header, now, now),
            )
            row = db.execute(
                """SELECT id FROM remote_messages WHERE account_id=?
                   AND provider_kind='pop3' AND provider_message_id=?""",
                (aid, uidl),
            ).fetchone()
            existing = db.execute(
                "SELECT canonical_message_id FROM remote_canonical_links WHERE remote_message_id=?",
                (str(row[0]),),
            ).fetchone()
            if existing is not None and str(existing[0]) != canonical.id:
                conflict = True
            else:
                db.execute(
                    """INSERT OR IGNORE INTO remote_canonical_links(
                       remote_message_id,canonical_message_id,link_reason,created_at)
                       VALUES(?,?,'pop3-uidl-retr',?)""",
                    (str(row[0]), canonical.id, now),
                )
        except BaseException:
            db.rollback()
            raise
        else:
            db.commit()
    if conflict:
        _audit(config, account_name, "pop3.remote_link.failed", "conflict", {"uidl": uidl})
        raise Pop3Error("POP3 UIDL conflicts with a different canonical message")


class Pop3Adapter:
    """POP3 fallback; UIDL is mandatory and no remote mutation exists."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def sync(self, account_name: str) -> Pop3SyncResult:
        account = next((item for item in self.config.accounts if item.name == account_name), None)
        if account is None or account.kind != "pop3" or account.pop3 is None:
            raise Pop3Error("account is not configured for POP3 synchronization")
        if not account.enabled:
            raise Pop3Error("account is disabled")
        initialize(self.config.database.path, self.config.accounts)
        from mailarchive.classification import reconcile_pending

        reconcile_pending(self.config, account_name=account_name)
        try:
            with pop3_lock(self.config, account):
                return self._sync_locked(account_name, account)
        except Exception as error:
            _audit(
                self.config,
                account_name,
                "pop3.sync.failed",
                "failed",
                {"reason": type(error).__name__},
            )
            raise

    def _sync_locked(self, account_name: str, account: AccountConfig) -> Pop3SyncResult:
        """Keep one provider UIDL snapshot and every derived local write under pop3_lock."""
        assert account.pop3 is not None
        password = os.environ.get(credential_variable(account.config_ref))
        if not password:
            raise Pop3Error("missing POP3 credential environment variable")
        client = _Pop3Wire(account.pop3)
        imported = reused = 0
        try:
            client.open()
            client.command(f"USER {account.pop3.username}")
            client.command(f"PASS {password}")
            uidls = client.uidls()
            if len(set(uidls.values())) != len(uidls):
                raise Pop3Error("POP3 UIDL response is ambiguous")
            with connect(self.config.database.path) as db:
                aid = account_id(db, account_name)
                if aid is None:
                    raise Pop3Error("account is not active in local state")
                known = _linked_uidls(db, aid)
                now = utc_now()
                for uidl in set(uidls.values()) & known:
                    db.execute(
                        """UPDATE remote_messages SET last_seen_at=?,remote_present=1
                           WHERE account_id=? AND provider_kind='pop3' AND provider_message_id=?""",
                        (now, aid, uidl),
                    )
                db.commit()
            for number, uidl in sorted(uidls.items()):
                if uidl in known:
                    continue
                result = ingest_bytes(
                    self.config, client.retr(number), account_name, source_kind="pop3-retr"
                )
                register_pop3_link(self.config, account_name, uidl, result.canonical_message)
                if result.created:
                    from mailarchive.classification import classify_pending

                    classify_pending(self.config, result.canonical_message)
                imported += int(result.created)
                reused += int(not result.created)
            if "DELE" in client.commands:
                raise AssertionError("POP3 adapter issued forbidden DELE")
            _audit(
                self.config,
                account_name,
                "pop3.sync.succeeded",
                "success",
                {"seen": len(uidls), "imported": imported, "reused": reused},
            )
            return Pop3SyncResult(len(uidls), imported, reused, len(uidls))
        finally:
            client.close()
