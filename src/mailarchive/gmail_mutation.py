"""Narrow M12-C Gmail permanent-delete capability; never used by M5 acquisition."""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportPrivateUsage=false

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

import requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from mailarchive.db import account_id, connect
from mailarchive.gmail import (
    GmailAuthError,
    GmailError,
    _valid_id,
    _write_token,
    decode_raw,
    gmail_lock,
)
from mailarchive.models import AccountConfig, AppConfig
from mailarchive.remote_mutation import DeletionTarget, MutationResult, ProviderDeletionTarget

GMAIL_DELETE_SCOPE = "https://mail.google.com/"
_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"


class MutationTransport(Protocol):
    def profile(self) -> dict[str, object]: ...
    def get_raw(self, message_id: str) -> dict[str, object] | None: ...
    def delete_message_once(self, message_id: str) -> None: ...


class _GoogleMutationTransport:
    """Fixed-host minimal HTTP surface; DELETE is intentionally one request."""

    def __init__(self, credentials: Credentials) -> None:
        self.credentials, self.session = credentials, requests.Session()

    def _headers(self) -> dict[str, str]:
        token = self.credentials.token
        if not isinstance(token, str) or not token:
            raise GmailAuthError("Gmail deletion access token is unavailable")
        return {"Authorization": f"Bearer {token}"}

    def _get(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, object] | None:
        response = self.session.get(
            f"{_BASE_URL}/{path}", headers=self._headers(), params=params, timeout=30
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise GmailAuthError("Gmail deletion observation failed")
        data = response.json()
        if not isinstance(data, dict):
            raise GmailAuthError("Gmail deletion observation was malformed")
        return cast(dict[str, object], data)

    def profile(self) -> dict[str, object]:
        response = self._get("profile")
        if response is None:
            raise GmailAuthError("Gmail profile was absent")
        return response

    def get_raw(self, message_id: str) -> dict[str, object] | None:
        return self._get(
            f"messages/{_valid_id(message_id, 'message id')}", params={"format": "raw"}
        )

    def delete_message_once(self, message_id: str) -> None:
        # No loop and no retry: caller must resolve uncertainty with a GET.
        self.session.delete(
            f"{_BASE_URL}/messages/{_valid_id(message_id, 'message id')}",
            headers=self._headers(),
            timeout=30,
        )


def _delete_path(account: AccountConfig) -> Path:
    if account.gmail is None or account.gmail.remote_delete_token_file is None:
        raise GmailAuthError("Gmail remote delete token file is not configured")
    return account.gmail.remote_delete_token_file


def _safe_delete_token(path: Path, *, required: bool = True) -> None:
    if not path.exists():
        if required:
            raise GmailAuthError("Gmail remote delete token file is absent")
        return
    details = path.lstat()
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_mode & 0o077
    ):
        raise GmailAuthError("Gmail remote delete token file must be a regular 0600 file")


def _validate_delete_scopes(scopes: object) -> None:
    if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
        raise GmailAuthError("Gmail deletion credential scopes cannot be proven")
    mailbox = {scope for scope in scopes if "gmail" in scope or scope == GMAIL_DELETE_SCOPE}
    if mailbox != {GMAIL_DELETE_SCOPE}:
        raise GmailAuthError("Gmail deletion credential scope is not mail.google.com")


def _serialized_delete_credentials(credentials: Credentials) -> str:
    value = credentials.to_json()
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise GmailAuthError("Gmail deletion credential cannot be safely persisted") from error
    if not isinstance(payload, dict):
        raise GmailAuthError("Gmail deletion credential cannot be safely persisted")
    _validate_delete_scopes(payload.get("scopes"))
    return value


def load_delete_credentials(account: AccountConfig) -> Credentials:
    path = _delete_path(account)
    _safe_delete_token(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GmailAuthError("Gmail remote delete token file is invalid") from error
    if not isinstance(payload, dict):
        raise GmailAuthError("Gmail remote delete token file is invalid")
    _validate_delete_scopes(payload.get("scopes"))
    return Credentials.from_authorized_user_file(str(path))


def authorize_delete(
    account: AccountConfig,
    client_factory: Callable[[Credentials], MutationTransport] | None = None,
) -> str:
    if account.gmail is None:
        raise GmailAuthError("account is not configured for Gmail")
    secret = account.gmail.oauth_client_secret_file
    if not secret.is_file() or secret.is_symlink() or secret.stat().st_mode & 0o077:
        raise GmailAuthError("OAuth client-secret file is unsafe or absent")
    destination = _delete_path(account)
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), scopes=[GMAIL_DELETE_SCOPE])
    credentials = cast(
        Credentials,
        flow.run_local_server(host="127.0.0.1", port=0, open_browser=True, access_type="offline"),
    )
    if not credentials.refresh_token:
        raise GmailAuthError("OAuth authorization did not provide a refresh token")
    _validate_delete_scopes(json.loads(_serialized_delete_credentials(credentials)).get("scopes"))
    client = (
        client_factory(credentials)
        if client_factory is not None
        else _GoogleMutationTransport(credentials)
    )
    actual = _valid_id(client.profile().get("emailAddress"), "profile email")
    if actual.casefold() != account.gmail.account_email.casefold():
        raise GmailAuthError("authenticated Gmail profile does not match configured account")
    _safe_delete_token(destination, required=False)
    _write_token(destination, _serialized_delete_credentials(credentials))
    return actual


class GmailMutationAdapter:
    """At most one exact Message.id DELETE; all confirmation is GET-only."""

    def __init__(
        self,
        config: AppConfig,
        account_name: str,
        *,
        transport_factory: Callable[[Credentials], MutationTransport],
    ) -> None:
        account = next((item for item in config.accounts if item.name == account_name), None)
        if account is None or account.kind != "gmail" or account.gmail is None:
            raise GmailAuthError("account is not configured for Gmail mutation")
        with connect(config.database.path) as db:
            local_id = account_id(db, account_name)
        if local_id is None:
            raise GmailAuthError("account is not active in local state")
        self.config, self.account, self.account_id = config, account, local_id
        self.credentials = load_delete_credentials(account)
        self.transport_factory = transport_factory

    @staticmethod
    def _failure(code: str) -> MutationResult:
        return MutationResult("failure-confirmed-no-mutation", error_code=code)

    @staticmethod
    def _unknown() -> MutationResult:
        return MutationResult("outcome-unknown", error_code="TRANSPORT_UNKNOWN")

    def _target(self, target: DeletionTarget) -> ProviderDeletionTarget | None:
        if not isinstance(target, ProviderDeletionTarget) or target.provider_kind != "gmail":
            return None
        if target.account_id != self.account_id or target.account_name != self.account.name:
            return None
        try:
            _valid_id(target.provider_message_id, "message id")
        except GmailError:
            return None
        return target

    def _observe(
        self, transport: MutationTransport, target: ProviderDeletionTarget
    ) -> tuple[str, bool]:
        try:
            item = transport.get_raw(target.provider_message_id)
            if item is None:
                return "absent", True
            returned = item.get("id")
            if returned is not None and returned != target.provider_message_id:
                return "conflict", False
            raw = decode_raw(item.get("raw"))
            return (
                ("present", True)
                if hashlib.sha256(raw).hexdigest() == target.canonical_sha256
                else ("conflict", False)
            )
        except Exception:
            return "unobservable", False

    def delete(self, target: DeletionTarget) -> MutationResult:
        exact = self._target(target)
        if exact is None or not self.account.enabled or self.account.gmail is None:
            return self._failure("IDENTITY_MISMATCH")
        with gmail_lock(self.config, self.account, "sync"):
            transport = self.transport_factory(self.credentials)
            try:
                profile = _valid_id(transport.profile().get("emailAddress"), "profile email")
            except Exception:
                return self._failure("AUTHORIZATION_FAILED")
            if profile.casefold() != self.account.gmail.account_email.casefold():
                return self._failure("AUTHORIZATION_FAILED")
            state, proven = self._observe(transport, exact)
            if state == "absent":
                return MutationResult("success-confirmed", confirmed_absent=True)
            if state != "present" or not proven:
                return self._failure(
                    "IDENTITY_MISMATCH" if state == "conflict" else "PROVIDER_REJECTED"
                )
            try:
                transport.delete_message_once(exact.provider_message_id)
            except Exception:
                pass  # request may have reached Gmail: observe, never retry DELETE
            state, proven = self._observe(transport, exact)
            if state == "absent":
                return MutationResult("success-confirmed", confirmed_absent=True)
            if state == "present" and proven:
                return self._failure("PROVIDER_REJECTED")
            return self._unknown()
