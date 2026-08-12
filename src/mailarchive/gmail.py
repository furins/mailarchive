"""Read-only Gmail REST acquisition.  This module intentionally exposes no mutation API."""
# ruff: noqa: E501
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportPrivateUsage=false

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event
from typing import Any, Protocol, cast

import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from mailarchive.db import account_id, connect, initialize, insert_audit_event, utc_now
from mailarchive.ingest import IngestResult, ingest_bytes
from mailarchive.models import AccountConfig, AppConfig

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_WRITE_SCOPES = (
    "https://mail.google.com/",
    "gmail.modify",
    "gmail.compose",
    "gmail.send",
    "gmail.insert",
)
_BASE_URL = "https://gmail.googleapis.com/gmail/v1"


class GmailError(RuntimeError):
    pass


class GmailAuthError(GmailError):
    pass


class GmailTransientError(GmailError):
    pass


class GmailHistoryExpired(GmailError):
    pass


class GmailIdentityConflict(GmailError):
    pass


class GmailSyncBusyError(GmailError):
    pass


class GmailResponseError(GmailError):
    pass


class Response(Protocol):
    status_code: int
    headers: Any

    def json(self) -> Any: ...


def _valid_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
        raise GmailResponseError(f"Gmail response has invalid {field}")
    return value


def _history(value: object) -> str:
    value = _valid_id(value, "historyId")
    if not value.isdecimal():
        raise GmailResponseError("Gmail response has invalid historyId")
    return value


def _label_name(value: object) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise GmailResponseError("Gmail response has invalid label name")
    return value


def decode_raw(value: object) -> bytes:
    """Strict Gmail base64url decoding; no MIME parsing or byte normalization."""
    if not isinstance(value, str) or not value:
        raise GmailResponseError("Gmail RAW response lacks raw bytes")
    if any(
        c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in value
    ):
        raise GmailResponseError("Gmail RAW is not base64url")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except ValueError as error:
        raise GmailResponseError("Gmail RAW is invalid base64url") from error


class GmailApiClient:
    """Fixed-host, explicit GET-only surface required by M5."""

    def __init__(self, session: Any, *, base_url: str = _BASE_URL, timeout: float = 30.0) -> None:
        self._session, self._base_url, self._timeout = session, base_url.rstrip("/"), timeout

    def _get(
        self, operation: str, path: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        response: Response | None = None
        for attempt in range(3):
            try:
                response = cast(
                    Response,
                    self._session.get(
                        f"{self._base_url}/users/me/{path}", params=params, timeout=self._timeout
                    ),
                )
            except requests.RequestException as error:
                if attempt == 2:
                    raise GmailTransientError(f"Gmail {operation} transport failure") from error
                time.sleep(0.25 * (2**attempt))
                continue
            current = response
            if current.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                break
            retry_after = current.headers.get("Retry-After")
            try:
                delay = min(float(retry_after), 5.0) if retry_after else 0.25 * (2**attempt)
            except TypeError, ValueError:
                delay = 0.25 * (2**attempt)
            time.sleep(delay)
        assert response is not None
        if response.status_code == 404 and operation == "history.list":
            raise GmailHistoryExpired("Gmail history checkpoint expired")
        if response.status_code in {429, 500, 502, 503, 504}:
            raise GmailTransientError(f"Gmail {operation} transient HTTP {response.status_code}")
        if response.status_code in {401, 403}:
            raise GmailAuthError(f"Gmail {operation} authorization failed")
        if response.status_code != 200:
            raise GmailResponseError(f"Gmail {operation} HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as error:
            raise GmailResponseError(f"Gmail {operation} returned invalid JSON") from error
        if not isinstance(data, dict):
            raise GmailResponseError(f"Gmail {operation} response must be an object")
        return cast(dict[str, Any], data)

    def profile(self) -> dict[str, Any]:
        return self._get("profile", "profile")

    def labels(self) -> dict[str, Any]:
        return self._get("labels.list", "labels")

    def messages(self, page_token: str | None = None) -> dict[str, Any]:
        params = {"maxResults": "500", "includeSpamTrash": "true"}
        if page_token:
            params["pageToken"] = page_token
        return self._get("messages.list", "messages", params)

    def message(self, message_id: str, format: str) -> dict[str, Any]:
        if format not in {"raw", "minimal"}:
            raise GmailResponseError("unsupported Gmail message format")
        return self._get(
            "messages.get", f"messages/{_valid_id(message_id, 'message id')}", {"format": format}
        )

    def history(self, history_id: str, page_token: str | None = None) -> dict[str, Any]:
        params = {"startHistoryId": _history(history_id), "maxResults": "500"}
        if page_token:
            params["pageToken"] = page_token
        return self._get("history.list", "history", params)


def _token_path(account: AccountConfig) -> Path:
    if not account.config_ref.startswith("file:"):
        raise GmailAuthError("Gmail token must use file: config_ref")
    return Path(account.config_ref[5:])


def _safe_token_file(path: Path, *, required: bool = True) -> None:
    if not path.exists():
        if required:
            raise GmailAuthError("Gmail authorized-user token file is absent")
        return
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise GmailAuthError("Gmail token file must be a regular 0600 file")


def load_credentials(account: AccountConfig) -> Credentials:
    path = _token_path(account)
    _safe_token_file(path)
    credentials = Credentials.from_authorized_user_file(str(path), scopes=[GMAIL_READONLY_SCOPE])
    scopes = credentials.scopes or []
    if GMAIL_READONLY_SCOPE not in scopes or any(scope in _WRITE_SCOPES for scope in scopes):
        raise GmailAuthError("stored Gmail credential scope is not readonly")
    return credentials


def _write_token(path: Path, value: str) -> None:
    """Install credential JSON atomically; never follow an existing symlink."""
    _safe_token_file(path, required=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".mailarchive-token-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def authorize(
    account: AccountConfig, client_factory: Callable[[Credentials], GmailApiClient] | None = None
) -> str:
    """Run installed-app loopback OAuth and verify identity before installing a token."""
    if account.gmail is None:
        raise GmailAuthError("account is not configured for Gmail")
    secret = account.gmail.oauth_client_secret_file
    if not secret.is_file() or secret.is_symlink():
        raise GmailAuthError("OAuth client-secret file is unsafe or absent")
    if secret.stat().st_mode & 0o077:
        raise GmailAuthError("OAuth client-secret file is group/world readable")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), scopes=[GMAIL_READONLY_SCOPE])
    credentials = cast(
        Credentials,
        flow.run_local_server(host="127.0.0.1", port=0, open_browser=True, access_type="offline"),
    )
    if not credentials.refresh_token:
        raise GmailAuthError("OAuth authorization did not provide a refresh token")
    client = (client_factory or (lambda c: GmailApiClient(AuthorizedSession(c))))(credentials)
    profile = client.profile()
    actual = _valid_id(profile.get("emailAddress"), "profile email")
    if actual.casefold() != account.gmail.account_email.casefold():
        raise GmailAuthError("authenticated Gmail profile does not match configured account")
    _write_token(_token_path(account), credentials.to_json())
    return actual


def _audit(
    config: AppConfig, account: str, event: str, result: str, details: dict[str, object]
) -> None:
    with connect(config.database.path) as db:
        insert_audit_event(
            db,
            actor="mailarchive.gmail",
            event_type=event,
            result=result,
            account_id=account_id(db, account),
            details_json=json.dumps(details, sort_keys=True),
        )
        db.commit()


@dataclass(frozen=True)
class GmailSyncResult:
    mode: str
    remote_seen: int = 0
    fetched_raw: int = 0
    metadata_refreshed: int = 0
    imported: int = 0
    canonical_reused: int = 0
    labels_updated: int = 0
    remote_marked_absent: int = 0
    history_from: str | None = None
    history_to: str | None = None


@contextmanager
def gmail_lock(config: AppConfig, account: AccountConfig, purpose: str) -> Generator[None]:
    import fcntl

    digest = hashlib.sha256(account.name.encode()).hexdigest()[:20]
    path = config.archive.root / "state" / "locks" / f"gmail-{purpose}-{digest}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise GmailSyncBusyError("Gmail synchronization is already running") from error
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


class GmailAdapter:
    def __init__(
        self,
        config: AppConfig,
        client_factory: Callable[[AccountConfig], GmailApiClient] | None = None,
    ) -> None:
        self.config, self.client_factory = config, client_factory or self._authorized_client

    def _account(self, name: str) -> AccountConfig:
        account = next((a for a in self.config.accounts if a.name == name), None)
        if account is None or not account.enabled:
            raise GmailError("unknown or disabled Gmail account")
        if account.kind != "gmail" or account.gmail is None:
            raise GmailError("account is not configured for Gmail synchronization")
        return account

    def _authorized_client(self, account: AccountConfig) -> GmailApiClient:
        return GmailApiClient(AuthorizedSession(load_credentials(account)))

    def _verify(self, client: GmailApiClient, account: AccountConfig) -> None:
        assert account.gmail is not None
        profile = client.profile()
        actual = _valid_id(profile.get("emailAddress"), "profile email")
        if actual.casefold() != account.gmail.account_email.casefold():
            raise GmailAuthError("authenticated Gmail profile does not match configured account")

    def _labels(self, client: GmailApiClient, aid: int) -> set[str]:
        payload = client.labels()
        labels = payload.get("labels")
        if not isinstance(labels, list):
            raise GmailResponseError("Gmail labels response lacks labels")
        now, observed = utc_now(), set()
        with connect(self.config.database.path) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                for label in labels:
                    if not isinstance(label, dict):
                        raise GmailResponseError("Gmail label is malformed")
                    lid, name = (
                        _valid_id(label.get("id"), "label id"),
                        _label_name(label.get("name")),
                    )
                    typ = label.get("type")
                    if typ not in {"system", "user"}:
                        raise GmailResponseError("Gmail label type is invalid")
                    observed.add(lid)
                    db.execute(
                        """INSERT INTO gmail_labels(account_id,label_id,name,label_type,remote_present,first_seen_at,last_seen_at)
                    VALUES(?,?,?,?,1,?,?) ON CONFLICT(account_id,label_id) DO UPDATE SET name=excluded.name,label_type=excluded.label_type,remote_present=1,last_seen_at=excluded.last_seen_at""",
                        (aid, lid, name, typ, now, now),
                    )
                db.execute(
                    "UPDATE gmail_labels SET remote_present=0 WHERE account_id=? AND label_id NOT IN ("
                    + ",".join("?" for _ in observed)
                    + ")",
                    (aid, *observed),
                ) if observed else db.execute(
                    "UPDATE gmail_labels SET remote_present=0 WHERE account_id=?", (aid,)
                )
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()
        return observed

    def _register(
        self, aid: int, message: dict[str, Any], result: IngestResult | None, labels: set[str]
    ) -> tuple[bool, int]:
        mid = _valid_id(message.get("id"), "message id")
        thread = message.get("threadId")
        if thread is not None:
            thread = _valid_id(thread, "thread id")
        label_ids = message.get("labelIds", [])
        if not isinstance(label_ids, list) or any(not isinstance(x, str) for x in label_ids):
            raise GmailResponseError("Gmail message labels are malformed")
        if not set(label_ids).issubset(labels):
            raise GmailResponseError("Gmail message references an unknown label")
        now, remote_id = utc_now(), f"gmail:{aid}:{mid}"
        with connect(self.config.database.path) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute(
                    "SELECT id FROM remote_messages WHERE account_id=? AND provider_kind='gmail' AND provider_message_id=?",
                    (aid, mid),
                ).fetchone()
                if existing is not None and result is not None:
                    linked = db.execute(
                        "SELECT canonical_message_id FROM remote_canonical_links WHERE remote_message_id=?",
                        (str(existing[0]),),
                    ).fetchone()
                    if linked is not None and str(linked[0]) != result.canonical_message.id:
                        raise GmailIdentityConflict(
                            "Gmail provider identity maps to different canonical bytes"
                        )
                db.execute(
                    """INSERT INTO remote_messages(id,account_id,provider_kind,remote_folder,uidvalidity,remote_uid,provider_message_id,provider_thread_id,message_id_header,first_seen_at,last_seen_at,remote_present,identity_confidence)
                VALUES(?,?,'gmail',NULL,NULL,NULL,?,?,?, ?,?,1,'proven')
                ON CONFLICT(account_id,provider_message_id) WHERE provider_kind='gmail' DO UPDATE SET provider_thread_id=excluded.provider_thread_id,last_seen_at=excluded.last_seen_at,remote_present=1""",
                    (
                        remote_id,
                        aid,
                        mid,
                        thread,
                        result.canonical_message.message_id_header if result else None,
                        now,
                        now,
                    ),
                )
                row = db.execute(
                    "SELECT id FROM remote_messages WHERE account_id=? AND provider_kind='gmail' AND provider_message_id=?",
                    (aid, mid),
                ).fetchone()
                assert row is not None
                rid = str(row[0])
                if result is not None:
                    db.execute(
                        "INSERT OR IGNORE INTO remote_canonical_links(remote_message_id,canonical_message_id,link_reason,created_at) VALUES(?,?, 'gmail-api-raw',?)",
                        (rid, result.canonical_message.id, now),
                    )
                db.execute("DELETE FROM gmail_message_labels WHERE remote_message_id=?", (rid,))
                db.executemany(
                    "INSERT INTO gmail_message_labels(remote_message_id,account_id,label_id) VALUES(?,?,?)",
                    [(rid, aid, x) for x in label_ids],
                )
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()
        return existing is None, len(label_ids)

    def _known(self, aid: int, mid: str) -> bool:
        with connect(self.config.database.path) as db:
            return (
                db.execute(
                    "SELECT 1 FROM remote_messages WHERE account_id=? AND provider_kind='gmail' AND provider_message_id=? AND id IN (SELECT remote_message_id FROM remote_canonical_links)",
                    (aid, mid),
                ).fetchone()
                is not None
            )

    def _reconcile(
        self, client: GmailApiClient, account: AccountConfig, aid: int, mid: str, labels: set[str]
    ) -> tuple[int, int, int]:
        known = self._known(aid, mid)
        response = client.message(mid, "minimal" if known else "raw")
        if _valid_id(response.get("id"), "message id") != mid:
            raise GmailResponseError("Gmail returned mismatching message id")
        result = None
        if not known:
            result = ingest_bytes(
                self.config,
                decode_raw(response.get("raw")),
                account.name,
                source_kind="gmail-api-raw",
            )
        _, updated = self._register(aid, response, result, labels)
        return (0 if known else 1, int(result.created) if result else 0, updated)

    def sync(self, account_name: str) -> GmailSyncResult:
        account = self._account(account_name)
        initialize(self.config.database.path, self.config.accounts)
        with gmail_lock(self.config, account, "sync"):
            client = self.client_factory(account)
            self._verify(client, account)
            with connect(self.config.database.path) as db:
                aid = account_id(db, account_name)
                assert aid is not None
                state = db.execute(
                    "SELECT history_id,full_sync_required FROM gmail_sync_state WHERE account_id=?",
                    (aid,),
                ).fetchone()
                db.execute(
                    "INSERT OR IGNORE INTO gmail_sync_state(account_id,updated_at) VALUES(?,?)",
                    (aid, utc_now()),
                )
                db.commit()
            labels = self._labels(client, aid)
            if state is None or bool(state[1]) or state[0] is None:
                return self._full(client, account, aid, labels)
            try:
                return self._partial(client, account, aid, labels, str(state[0]))
            except GmailHistoryExpired:
                _audit(self.config, account_name, "gmail.history.expired", "safe-resync", {})
                return self._full(client, account, aid, labels)

    def _full(
        self, client: GmailApiClient, account: AccountConfig, aid: int, labels: set[str]
    ) -> GmailSyncResult:
        _audit(self.config, account.name, "gmail.sync.full.started", "started", {})
        seen: set[str] = set()
        token = None
        tokens: set[str] = set()
        fetched = created = updated = 0
        anchor = None
        while True:
            page = client.messages(token)
            anchor = page.get("historyId", anchor)
            entries = page.get("messages", [])
            if not isinstance(entries, list):
                raise GmailResponseError("Gmail messages list malformed")
            for item in entries:
                if not isinstance(item, dict):
                    raise GmailResponseError("Gmail message list item malformed")
                mid = _valid_id(item.get("id"), "message id")
                seen.add(mid)
                f, c, u = self._reconcile(client, account, aid, mid, labels)
                fetched += f
                created += c
                updated += u
            token = page.get("nextPageToken")
            if token is None:
                break
            token = _valid_id(token, "page token")
            if token in tokens:
                raise GmailResponseError("Gmail messages pagination token repeated")
            tokens.add(token)
        if anchor is not None:
            anchor = _history(anchor)
        with connect(self.config.database.path) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = db.execute(
                    "SELECT id,provider_message_id FROM remote_messages WHERE account_id=? AND provider_kind='gmail' AND remote_present=1",
                    (aid,),
                ).fetchall()
                absent = sum(1 for row in rows if str(row[1]) not in seen)
                db.executemany(
                    "UPDATE remote_messages SET remote_present=0 WHERE id=?",
                    [(str(r[0]),) for r in rows if str(r[1]) not in seen],
                )
                db.execute(
                    "UPDATE gmail_sync_state SET history_id=?,full_sync_required=?,last_sync_succeeded_at=?,last_full_sync_succeeded_at=?,updated_at=? WHERE account_id=?",
                    (anchor, 0 if anchor else 1, utc_now(), utc_now(), utc_now(), aid),
                )
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()
        result = GmailSyncResult(
            "full", len(seen), fetched, 0, created, fetched - created, updated, absent, None, anchor
        )
        _audit(self.config, account.name, "gmail.sync.full.succeeded", "success", asdict(result))
        return result

    def _partial(
        self, client: GmailApiClient, account: AccountConfig, aid: int, labels: set[str], start: str
    ) -> GmailSyncResult:
        affected: set[str] = set()
        deleted: set[str] = set()
        token = None
        tokens: set[str] = set()
        end = None
        _audit(self.config, account.name, "gmail.sync.partial.started", "started", {})
        while True:
            page = client.history(start, token)
            end = page.get("historyId", end)
            histories = page.get("history", [])
            if not isinstance(histories, list):
                raise GmailResponseError("Gmail history malformed")
            for record in histories:
                if not isinstance(record, dict):
                    raise GmailResponseError("Gmail history item malformed")
                for key in ("messagesAdded", "labelsAdded", "labelsRemoved"):
                    for change in record.get(key, []):
                        if not isinstance(change, dict) or not isinstance(
                            change.get("message"), dict
                        ):
                            raise GmailResponseError("Gmail history change malformed")
                        affected.add(_valid_id(change["message"].get("id"), "message id"))
                for change in record.get("messagesDeleted", []):
                    if not isinstance(change, dict) or not isinstance(change.get("message"), dict):
                        raise GmailResponseError("Gmail deletion malformed")
                    mid = _valid_id(change["message"].get("id"), "message id")
                    affected.add(mid)
                    deleted.add(mid)
            token = page.get("nextPageToken")
            if token is None:
                break
            token = _valid_id(token, "page token")
            if token in tokens:
                raise GmailResponseError("Gmail history pagination token repeated")
            tokens.add(token)
        end = _history(end) if end is not None else start
        fetched = created = updated = absent = 0
        for mid in affected:
            if mid in deleted:
                with connect(self.config.database.path) as db:
                    db.execute(
                        "UPDATE remote_messages SET remote_present=0 WHERE account_id=? AND provider_kind='gmail' AND provider_message_id=?",
                        (aid, mid),
                    )
                    db.commit()
                absent += 1
                continue
            f, c, u = self._reconcile(client, account, aid, mid, labels)
            fetched += f
            created += c
            updated += u
        with connect(self.config.database.path) as db:
            db.execute(
                "UPDATE gmail_sync_state SET history_id=?,full_sync_required=0,last_sync_succeeded_at=?,last_partial_sync_succeeded_at=?,updated_at=? WHERE account_id=?",
                (end, utc_now(), utc_now(), utc_now(), aid),
            )
            db.commit()
        result = GmailSyncResult(
            "partial",
            len(affected),
            fetched,
            0,
            created,
            fetched - created,
            updated,
            absent,
            start,
            end,
        )
        _audit(self.config, account.name, "gmail.sync.partial.succeeded", "success", asdict(result))
        return result


class GmailWatcher:
    """Stop-aware local polling; intentionally never uses users.watch."""

    def __init__(
        self,
        config: AppConfig,
        account: str,
        stop_event: Event,
        adapter: GmailAdapter | None = None,
        refresh: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.account_name = account
        self.stop_event = stop_event
        self.adapter = adapter or GmailAdapter(config)
        self.refresh = refresh

    def run(self) -> None:
        account = self.adapter._account(self.account_name)
        with gmail_lock(self.config, account, "watch"):
            while not self.stop_event.is_set():
                try:
                    self.adapter.sync(account.name)
                    if self.refresh:
                        self.refresh()
                except GmailSyncBusyError:
                    pass
                self.stop_event.wait(account.gmail.poll_interval_seconds if account.gmail else 90)
