"""Dedicated M12-D POP3 deletion capability, injectable until M12-E wiring.

It intentionally does not reuse the M6 acquisition wire: after ``DELE``, an
implicit ``QUIT`` would commit a deletion during exception cleanup.
"""

from __future__ import annotations

import hashlib
import os
import socket
import ssl
from collections.abc import Callable
from typing import Protocol

from mailarchive.db import account_id, connect
from mailarchive.imap import credential_variable
from mailarchive.models import AppConfig, Pop3Config
from mailarchive.pop3 import Pop3SyncBusyError, pop3_lock
from mailarchive.remote_mutation import DeletionTarget, MutationResult, ProviderDeletionTarget


class Pop3MutationError(RuntimeError):
    """Bounded local/protocol failure; details are never persisted."""


class Pop3MutationWire(Protocol):
    def open(self) -> None: ...
    def authenticate(self, username: str, password: str) -> None: ...
    def uidls(self) -> dict[int, str]: ...
    def retr(self, number: int) -> bytes: ...
    def dele(self, number: int) -> None: ...
    def abort_without_quit(self) -> None: ...
    def quit_and_commit(self) -> None: ...


class _MutationWire:
    """Tiny POP3 wire with deliberate, never-implicit QUIT semantics."""

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
            self._command("STLS")
            assert self.sock is not None
            self.sock = ssl.create_default_context().wrap_socket(
                self.sock, server_hostname=self.settings.host
            )

    def abort_without_quit(self) -> None:
        """Close transport only: this must never commit a pending DELE."""
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def quit_and_commit(self) -> None:
        """Deliberately enter POP3 UPDATE after one validated DELE."""
        try:
            self._command("QUIT")
        finally:
            self.abort_without_quit()

    def _read_until(self, marker: bytes) -> bytes:
        assert self.sock is not None
        while marker not in self.buffer:
            data = self.sock.recv(65536)
            if not data:
                raise Pop3MutationError("POP3 connection closed")
            self.buffer += data
        value, self.buffer = self.buffer.split(marker, 1)
        return value

    def _positive_line(self) -> bytes:
        line = self._read_until(b"\r\n")
        if not line.startswith(b"+OK"):
            raise Pop3MutationError("POP3 command rejected")
        return line[3:].lstrip()

    def _wire_line(self) -> bytes:
        return self._read_until(b"\r\n") + b"\r\n"

    def _command(self, command: str) -> bytes:
        if "\r" in command or "\n" in command:
            raise Pop3MutationError("invalid POP3 command")
        assert self.sock is not None
        self.commands.append(command.split(" ", 1)[0].upper())
        self.sock.sendall(command.encode("ascii") + b"\r\n")
        return self._positive_line()

    def _multiline(self, command: str) -> bytes:
        self._command(command)
        result = bytearray()
        while True:
            line = self._wire_line()
            if line == b".\r\n":
                return bytes(result)
            if line.startswith(b".."):
                line = line[1:]
            result.extend(line)

    def authenticate(self, username: str, password: str) -> None:
        self._command(f"USER {username}")
        self._command(f"PASS {password}")

    def uidls(self) -> dict[int, str]:
        data = self._multiline("UIDL")
        result: dict[int, str] = {}
        seen: set[str] = set()
        for line in data.split(b"\r\n"):
            if not line:
                continue
            fields = line.split()
            if len(fields) != 2 or not fields[0].isdigit() or int(fields[0]) <= 0:
                raise Pop3MutationError("POP3 UIDL response is malformed")
            try:
                uidl = fields[1].decode("ascii", "strict")
            except UnicodeDecodeError as error:
                raise Pop3MutationError("POP3 UIDL response is malformed") from error
            number = int(fields[0])
            if (
                not uidl
                or any(char.isspace() for char in uidl)
                or number in result
                or uidl in seen
            ):
                raise Pop3MutationError("POP3 UIDL response is ambiguous")
            result[number] = uidl
            seen.add(uidl)
        if not result and data:
            raise Pop3MutationError("POP3 UIDL response is malformed")
        return result

    def retr(self, number: int) -> bytes:
        if number <= 0:
            raise Pop3MutationError("invalid POP3 message number")
        return self._multiline(f"RETR {number}")

    def dele(self, number: int) -> None:
        if number <= 0:
            raise Pop3MutationError("invalid POP3 message number")
        self._command(f"DELE {number}")


def _open(settings: Pop3Config) -> Pop3MutationWire:
    return _MutationWire(settings)


class Pop3MutationAdapter:
    """One exact UIDL deletion attempt; only injectable into the M12-A engine."""

    def __init__(
        self,
        config: AppConfig,
        account_name: str,
        *,
        wire_factory: Callable[[Pop3Config], Pop3MutationWire] = _open,
    ) -> None:
        account = next((item for item in config.accounts if item.name == account_name), None)
        if account is None or account.kind != "pop3" or account.pop3 is None:
            raise Pop3MutationError("account is not configured for POP3 mutation")
        self.credential_variable = credential_variable(account.config_ref)
        with connect(config.database.path) as db:
            local_id = account_id(db, account_name)
        if local_id is None:
            raise Pop3MutationError("account is not active in local state")
        self.config, self.account, self.account_id, self.wire_factory = (
            config,
            account,
            local_id,
            wire_factory,
        )

    @staticmethod
    def _failure(code: str) -> MutationResult:
        return MutationResult("failure-confirmed-no-mutation", error_code=code)

    @staticmethod
    def _unknown() -> MutationResult:
        return MutationResult("outcome-unknown", error_code="TRANSPORT_UNKNOWN")

    def _target(self, target: DeletionTarget) -> ProviderDeletionTarget | None:
        if not isinstance(target, ProviderDeletionTarget) or target.provider_kind != "pop3":
            return None
        if target.account_id != self.account_id or target.account_name != self.account.name:
            return None
        uidl = target.provider_message_id
        if (
            not self.account.enabled
            or self.account.pop3 is None
            or not uidl
            or any(char.isspace() for char in uidl)
            or "\r" in uidl
            or "\n" in uidl
        ):
            return None
        return target

    def _session(self, password: str) -> Pop3MutationWire:
        assert self.account.pop3 is not None
        wire = self.wire_factory(self.account.pop3)
        wire.open()
        wire.authenticate(self.account.pop3.username, password)
        return wire

    def _observe(self, target: ProviderDeletionTarget, password: str) -> MutationResult:
        """Fresh read-only provider proof after an attempted operation."""
        wire: Pop3MutationWire | None = None
        try:
            wire = self._session(password)
            inverse = {uidl: number for number, uidl in wire.uidls().items()}
            number = inverse.get(target.provider_message_id)
            if number is None:
                return MutationResult("success-confirmed", confirmed_absent=True)
            if hashlib.sha256(wire.retr(number)).hexdigest() == target.canonical_sha256:
                return self._failure("PROVIDER_REJECTED")
            return self._unknown()
        except (OSError, Pop3MutationError):
            return self._unknown()
        finally:
            if wire is not None:
                wire.abort_without_quit()

    def delete(self, target: DeletionTarget) -> MutationResult:
        exact = self._target(target)
        if exact is None:
            return self._failure("IDENTITY_MISMATCH")
        password = os.environ.get(self.credential_variable)
        if not password:
            return self._failure("IDENTITY_MISMATCH")
        wire: Pop3MutationWire | None = None
        try:
            with pop3_lock(self.config, self.account):
                wire = self._session(password)
                uidls = wire.uidls()
                number = next(
                    (
                        item
                        for item, uidl in uidls.items()
                        if uidl == exact.provider_message_id
                    ),
                    None,
                )
                if number is None:
                    return MutationResult("success-confirmed", confirmed_absent=True)
                if hashlib.sha256(wire.retr(number)).hexdigest() != exact.canonical_sha256:
                    return self._failure("IDENTITY_MISMATCH")
                try:
                    wire.dele(number)
                except (OSError, Pop3MutationError):
                    wire.abort_without_quit()
                    wire = None
                    return self._observe(exact, password)
                try:
                    wire.quit_and_commit()
                except (OSError, Pop3MutationError):
                    return self._observe(exact, password)
                wire = None
                return self._observe(exact, password)
        except Pop3SyncBusyError:
            return self._failure("REMOTE_STATE_CONFLICT")
        except (OSError, Pop3MutationError):
            return self._failure("PROVIDER_REJECTED")
        finally:
            if wire is not None:
                wire.abort_without_quit()
