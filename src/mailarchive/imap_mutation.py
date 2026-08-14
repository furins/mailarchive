"""Dedicated, injectable M12-B IMAP mutation adapter.

This module is deliberately separate from read-only acquisition.  It is not
wired into the production CLI factory until the later M12 reconciliation phase.
"""

from __future__ import annotations

import hashlib
import imaplib
import os
import re
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from mailarchive.db import account_id, connect
from mailarchive.imap import (
    ImapError,
    credential_variable,
    encode_mailbox_name,
    folder_lock,
    parse_fetch_response,
    parse_uidvalidity,
)
from mailarchive.models import AccountConfig, AppConfig
from mailarchive.remote_mutation import (
    DeletionTarget,
    ImapDeletionTarget,
    MutationResult,
    ObservationResult,
)

_FLAGS = re.compile(rb"(?:^|\s)FLAGS \(([^()]*)\)")


class ImapClient(Protocol):
    def login(self, user: str, password: str) -> tuple[str, Sequence[object]]: ...
    def capability(self) -> tuple[str, Sequence[object]]: ...
    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, Sequence[object]]: ...
    def response(self, code: str) -> tuple[str, Sequence[object]]: ...
    def uid(self, command: str, *args: str) -> tuple[str, Sequence[object]]: ...
    def logout(self) -> tuple[str, Sequence[object]]: ...


@dataclass(frozen=True)
class _Observed:
    present: bool
    raw: bytes | None = None
    deleted: bool = False


def _open(account: AccountConfig) -> ImapClient:
    assert account.imap is not None
    settings = account.imap
    context = ssl.create_default_context()
    if settings.tls_mode == "IMAPS":
        return cast(
            ImapClient,
            imaplib.IMAP4_SSL(
                settings.host,
                settings.port,
                ssl_context=context,
                timeout=settings.connection_timeout_seconds,
            ),
        )
    if settings.tls_mode == "STARTTLS":
        client = imaplib.IMAP4(
            settings.host, settings.port, timeout=settings.connection_timeout_seconds
        )
        client.starttls(ssl_context=context)
        return cast(ImapClient, client)
    return cast(
        ImapClient,
        imaplib.IMAP4(settings.host, settings.port, timeout=settings.connection_timeout_seconds),
    )


def _capabilities(data: Sequence[object]) -> set[bytes]:
    values: set[bytes] = set()
    for item in data:
        if not isinstance(item, bytes):
            raise ImapError("malformed IMAP capability response")
        values.update(token.upper() for token in item.split())
    return values


def _flags(metadata: bytes) -> tuple[bytes, ...]:
    matches = _FLAGS.findall(metadata)
    if len(matches) != 1:
        raise ImapError("IMAP FETCH response lacks one valid FLAGS value")
    values = tuple(matches[0].split())
    if any(not value or b"(" in value or b")" in value for value in values):
        raise ImapError("IMAP FETCH response has invalid FLAGS")
    return values


def _fetch_observation(client: ImapClient, uid: int) -> _Observed:
    status, data = client.uid("FETCH", str(uid), "(UID FLAGS BODY.PEEK[])")
    if status != "OK":
        raise ImapError("exact IMAP UID FETCH failed")
    # imaplib's documented no-match form is [None]; accept no other ambiguity.
    if not data or (len(data) == 1 and data[0] in (None, b"")):
        return _Observed(False)
    fetched = parse_fetch_response(uid, data)
    metadata = cast(bytes, cast(tuple[object, ...], data[0])[0])
    flags = _flags(metadata)
    return _Observed(True, fetched.raw_bytes, b"\\Deleted" in flags)


class ImapMutationAdapter:
    """One exact UID STORE/UID EXPUNGE attempt behind M12-A injection only."""

    def __init__(
        self,
        config: AppConfig,
        account_name: str,
        *,
        client_factory: Callable[[AccountConfig], ImapClient] = _open,
    ) -> None:
        account = next((item for item in config.accounts if item.name == account_name), None)
        if account is None or account.kind != "imap" or account.imap is None:
            raise ImapError("account is not configured for IMAP mutation")
        # This validates only the variable *name*; the secret is read in delete().
        self.credential_variable = credential_variable(account.config_ref)
        with connect(config.database.path) as db:
            local_id = account_id(db, account_name)
        if local_id is None:
            raise ImapError("account is not active in local state")
        self.config, self.account, self.account_id, self.client_factory = (
            config,
            account,
            local_id,
            client_factory,
        )

    def _failure(self, code: str) -> MutationResult:
        return MutationResult("failure-confirmed-no-mutation", error_code=code)

    def _unknown(self) -> MutationResult:
        return MutationResult("outcome-unknown", error_code="TRANSPORT_UNKNOWN")

    @staticmethod
    def _observation(state: str) -> ObservationResult:
        return ObservationResult(state)

    def _valid(self, target: DeletionTarget) -> ImapDeletionTarget | None:
        if not isinstance(target, ImapDeletionTarget) or target.provider_kind != "imap":
            return None
        if target.account_id != self.account_id or target.account_name != self.account.name:
            return None
        if (
            not self.account.enabled
            or self.account.imap is None
            or target.remote_folder not in self.account.imap.folders
            or target.uidvalidity <= 0
            or target.remote_uid <= 0
        ):
            return None
        return target

    def _same_namespace(self, client: ImapClient, target: ImapDeletionTarget) -> bool:
        return parse_uidvalidity(cast(imaplib.IMAP4, client)) == target.uidvalidity

    def _observe_selected(
        self, client: ImapClient, target: ImapDeletionTarget
    ) -> ObservationResult:
        """Observe one exact UID in an already read-only selected mailbox."""
        if not self._same_namespace(client, target):
            return self._observation("identity-conflict")
        observed = _fetch_observation(client, target.remote_uid)
        if not observed.present:
            return self._observation("confirmed-absent")
        if observed.deleted or observed.raw is None:
            return self._observation("identity-conflict")
        if hashlib.sha256(observed.raw).hexdigest() != target.canonical_sha256:
            return self._observation("identity-conflict")
        return self._observation("confirmed-present-match")

    def observe(self, target: DeletionTarget) -> ObservationResult:
        """Read-only proof about one exact IMAP UID namespace and byte object."""
        exact = self._valid(target)
        if exact is None:
            return self._observation("identity-conflict")
        password = os.environ.get(self.credential_variable)
        if not password:
            return self._observation("unknown")
        client: ImapClient | None = None
        try:
            with folder_lock(self.config, self.account, exact.remote_folder):
                client = self.client_factory(self.account)
                assert self.account.imap is not None
                if client.login(self.account.imap.username, password)[0] != "OK":
                    return self._observation("unknown")
                if (
                    client.select(encode_mailbox_name(exact.remote_folder), readonly=True)[0]
                    != "OK"
                ):
                    return self._observation("unknown")
                return self._observe_selected(client, exact)
        except OSError, imaplib.IMAP4.error, ImapError:
            return self._observation("unknown")
        finally:
            if client is not None:
                try:
                    unselect = getattr(client, "unselect", None)
                    if callable(unselect):
                        unselect()
                    client.logout()
                except OSError, imaplib.IMAP4.error:
                    pass

    def _clean_or_unknown(self, client: ImapClient, target: ImapDeletionTarget) -> MutationResult:
        try:
            if not self._same_namespace(client, target):
                return self._unknown()
            observed = _fetch_observation(client, target.remote_uid)
            if (
                observed.present
                and observed.raw is not None
                and (hashlib.sha256(observed.raw).hexdigest() == target.canonical_sha256)
                and not observed.deleted
            ):
                return self._failure("PROVIDER_REJECTED")
        except OSError, imaplib.IMAP4.error, ImapError:
            pass
        return self._unknown()

    def _after_expunge(
        self, client: ImapClient, target: ImapDeletionTarget, password: str
    ) -> MutationResult:
        """Resolve an EXPUNGE response loss without issuing any second EXPUNGE."""
        for observer in (client,):
            try:
                if not self._same_namespace(observer, target):
                    return self._unknown()
                observed = _fetch_observation(observer, target.remote_uid)
                if not observed.present:
                    return MutationResult("success-confirmed", confirmed_absent=True)
                if (
                    observed.raw is not None
                    and (hashlib.sha256(observed.raw).hexdigest() == target.canonical_sha256)
                    and not observed.deleted
                ):
                    return self._failure("PROVIDER_REJECTED")
                return self._unknown()
            except OSError, imaplib.IMAP4.error, ImapError:
                pass
        # A fresh read is the only allowed recovery action; it never retries EXPUNGE.
        observer: ImapClient | None = None
        try:
            observer = self.client_factory(self.account)
            assert self.account.imap is not None
            if observer.login(self.account.imap.username, password)[0] != "OK":
                return self._unknown()
            if observer.select(encode_mailbox_name(target.remote_folder), readonly=True)[0] != "OK":
                return self._unknown()
            if not self._same_namespace(observer, target):
                return self._unknown()
            observed = _fetch_observation(observer, target.remote_uid)
            if not observed.present:
                return MutationResult("success-confirmed", confirmed_absent=True)
            if (
                observed.raw is not None
                and (hashlib.sha256(observed.raw).hexdigest() == target.canonical_sha256)
                and not observed.deleted
            ):
                return self._failure("PROVIDER_REJECTED")
        except OSError, imaplib.IMAP4.error, ImapError:
            pass
        finally:
            if observer is not None:
                try:
                    observer.logout()
                except OSError, imaplib.IMAP4.error:
                    pass
        return self._unknown()

    def delete(self, target: DeletionTarget) -> MutationResult:
        exact = self._valid(target)
        if exact is None:
            return self._failure("IDENTITY_MISMATCH")
        password = os.environ.get(self.credential_variable)
        if not password:
            return self._failure("PROVIDER_REJECTED")
        client: ImapClient | None = None
        try:
            with folder_lock(self.config, self.account, exact.remote_folder):
                client = self.client_factory(self.account)
                assert self.account.imap is not None
                if client.login(self.account.imap.username, password)[0] != "OK":
                    return self._failure("PROVIDER_REJECTED")
                status, capability = client.capability()
                if status != "OK" or b"UIDPLUS" not in _capabilities(capability):
                    return self._failure("SAFE_DELETE_UNSUPPORTED")
                if (
                    client.select(encode_mailbox_name(exact.remote_folder), readonly=False)[0]
                    != "OK"
                ):
                    return self._failure("PROVIDER_REJECTED")
                if not self._same_namespace(client, exact):
                    return self._failure("IDENTITY_MISMATCH")
                try:
                    observed = _fetch_observation(client, exact.remote_uid)
                except OSError, imaplib.IMAP4.error, ImapError:
                    return self._failure("PROVIDER_REJECTED")
                if not observed.present:
                    return MutationResult("success-confirmed", confirmed_absent=True)
                assert observed.raw is not None
                if hashlib.sha256(observed.raw).hexdigest() != exact.canonical_sha256:
                    return self._failure("IDENTITY_MISMATCH")
                if observed.deleted:
                    return self._failure("REMOTE_STATE_CONFLICT")
                try:
                    stored, _ = client.uid(
                        "STORE", str(exact.remote_uid), "+FLAGS.SILENT", r"(\Deleted)"
                    )
                except OSError, imaplib.IMAP4.error:
                    return self._clean_or_unknown(client, exact)
                if stored != "OK":
                    return self._clean_or_unknown(client, exact)
                try:
                    expunged, _ = client.uid("EXPUNGE", str(exact.remote_uid))
                except OSError, imaplib.IMAP4.error:
                    return self._after_expunge(client, exact, password)
                if expunged != "OK":
                    return self._after_expunge(client, exact, password)
                try:
                    if (
                        self._same_namespace(client, exact)
                        and not _fetch_observation(client, exact.remote_uid).present
                    ):
                        return MutationResult("success-confirmed", confirmed_absent=True)
                except OSError, imaplib.IMAP4.error, ImapError:
                    return self._after_expunge(client, exact, password)
                return self._after_expunge(client, exact, password)
        except OSError, imaplib.IMAP4.error, ImapError:
            return self._failure("PROVIDER_REJECTED")
        finally:
            if client is not None:
                # UNSELECT is non-expunging; deliberately never invoke CLOSE here.
                try:
                    unselect = getattr(client, "unselect", None)
                    if callable(unselect):
                        unselect()
                    client.logout()
                except OSError, imaplib.IMAP4.error:
                    pass
