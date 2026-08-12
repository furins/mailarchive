"""M4 IMAP notification watcher; canonical acquisition remains in :mod:`imap`."""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import imaplib
import json
import os
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Protocol, cast

from mailarchive.db import account_id, connect, initialize, insert_audit_event, utc_now
from mailarchive.imap import (
    ImapAdapter,
    ImapError,
    ImapSyncBusyError,
    credential_variable,
    encode_mailbox_name,
)
from mailarchive.models import AccountConfig, AppConfig
from mailarchive.notmuch import NotmuchAdapter

IDLE_WINDOW_SECONDS = 30
BURST_INTERVAL_SECONDS = 0.1
FAST_PATH_STALE_SECONDS = 180
_RELEVANT_EVENTS = {"EXISTS", "RECENT", "FETCH", "EXPUNGE"}
_BACKOFF = (1, 2, 5, 10, 30, 60)


class NotificationConnection(Protocol):
    def login(self, username: str, password: str) -> tuple[str, list[bytes]]: ...
    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]: ...
    def capability(self) -> tuple[str, list[bytes]]: ...
    def idle(self, duration: float) -> Any: ...
    def logout(self) -> Any: ...


class FastPathError(ImapError):
    """A watcher-only error that does not alter M3 acquisition semantics."""


class FastPathPermanentError(FastPathError):
    """A configuration or authentication failure that cannot safely fall back."""


class FastPathIdleRejectedError(FastPathError):
    """An otherwise valid IDLE session deterministically rejected IDLE."""


@dataclass(frozen=True)
class HealthRecord:
    account: str
    folder: str
    configured_mode: str
    effective_mode: str | None
    state: str
    last_heartbeat_at: str | None
    last_event_at: str | None
    last_sync_succeeded_at: str | None
    last_index_succeeded_at: str | None
    consecutive_failures: int
    reconnect_count: int
    index_pending: bool
    last_error_kind: str | None


def _event_type(response: object) -> str | None:
    """Extract only an unsolicited response atom name, never its payload."""
    if not isinstance(response, tuple) or not response:
        return None
    value = cast(tuple[object, ...], response)[0]
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    return value.upper() if isinstance(value, str) else None


@contextmanager
def watcher_lock(config: AppConfig, account: AccountConfig) -> Generator[None]:
    import fcntl

    digest = hashlib.sha256(f"{account.name}\0INBOX".encode()).hexdigest()[:16]
    path = config.archive.root.resolve() / "state" / "locks" / f"imap-watch-{digest}-inbox.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FastPathError("IMAP INBOX watcher is already running for this account") from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ImapNotificationConnection:
    """Read-only stdlib IMAP IDLE connection. It never acquires message bodies."""

    def __init__(self, config: AppConfig, account: AccountConfig) -> None:
        self.config, self.account = config, account
        self.client: NotificationConnection | None = None

    def open(self) -> bool:
        try:
            variable = credential_variable(self.account.config_ref)
        except ImapError:
            raise FastPathPermanentError("invalid IMAP credential configuration") from None
        password = os.environ.get(variable)
        if not password:
            raise FastPathPermanentError("missing credential environment variable")
        self.client = cast(
            NotificationConnection,
            ImapAdapter(self.config).open_notification_connection(self.account),
        )
        try:
            login_status, _ = self.client.login(self.account.imap.username, password)  # type: ignore[union-attr]
        except imaplib.IMAP4.error:
            raise FastPathPermanentError("IMAP authentication failed") from None
        if login_status != "OK":
            raise FastPathPermanentError("IMAP authentication failed")
        if self.client.select(encode_mailbox_name("INBOX"), readonly=True)[0] != "OK":
            raise FastPathError("IMAP INBOX cannot be selected read-only")
        status, data = self.client.capability()
        if status != "OK":
            raise FastPathError("IMAP capability discovery failed")
        return any(b"IDLE" in item.upper().split() for item in data)

    @contextmanager
    def idle(self, duration: float) -> Generator[Any]:
        if self.client is None:
            raise FastPathError("notification connection is not open")
        # Python 3.14 public IDLE context manager API: no private protocol handling.
        with self.client.idle(duration=duration) as idler:
            yield idler

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.logout()
            except OSError, imaplib.IMAP4.error:
                pass
            self.client = None


class FastPathWatcher:
    """Long-running INBOX watcher with arm-before-sync and bounded IDLE windows."""

    def __init__(
        self,
        config: AppConfig,
        account_name: str,
        stop_event: threading.Event,
        *,
        notification_factory: Callable[
            [AppConfig, AccountConfig], ImapNotificationConnection
        ] = ImapNotificationConnection,
        sync_adapter: ImapAdapter | None = None,
        index_adapter: NotmuchAdapter | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
        idle_window_seconds: float = IDLE_WINDOW_SECONDS,
    ) -> None:
        self.config, self.account_name, self.stop_event = config, account_name, stop_event
        self.notification_factory = notification_factory
        self.sync_adapter = sync_adapter or ImapAdapter(config)
        self.index_adapter = index_adapter or NotmuchAdapter(config)
        self.clock, self.idle_window_seconds = monotonic_clock, idle_window_seconds
        self.account = self._account()
        self.acquisition_degraded = False
        self._transport_recovered = False
        self._consecutive_transport_failures = 0
        self._reconnect_count = 0

    def _operational_mode(self, normal_mode: str) -> str:
        return "degraded" if self.acquisition_degraded or self._index_pending() else normal_mode

    def _account(self) -> AccountConfig:
        account = next(
            (item for item in self.config.accounts if item.name == self.account_name), None
        )
        if account is None or not account.enabled or account.kind != "imap" or account.imap is None:
            raise FastPathError(
                "watch requires one enabled ordinary IMAP account with IMAP configuration"
            )
        if "INBOX" not in account.imap.folders:
            raise FastPathError("watch requires INBOX in the configured IMAP folder list")
        return account

    def _health(self, mode: str, **values: object) -> None:
        initialize(self.config.database.path, self.config.accounts)
        now = utc_now()
        allowed = {
            "watcher_started_at",
            "last_heartbeat_at",
            "last_event_at",
            "last_sync_started_at",
            "last_sync_succeeded_at",
            "last_index_succeeded_at",
            "last_error_at",
            "last_error_kind",
            "consecutive_failures",
            "reconnect_count",
            "index_pending",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        with connect(self.config.database.path) as connection:
            aid = account_id(connection, self.account.name)
            if aid is None:
                raise FastPathError("account is not active in local state")
            columns = ["account_id", "remote_folder", "mode", "updated_at", *updates]
            params = [aid, "INBOX", mode, now, *updates.values()]
            assignments = ", ".join(
                f"{key}=excluded.{key}" for key in ["mode", "updated_at", *updates]
            )
            connection.execute(
                f"INSERT INTO fast_path_health ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
                f"ON CONFLICT(account_id, remote_folder) DO UPDATE SET {assignments}",
                params,
            )
            connection.commit()

    def _audit(self, event: str, result: str, details: dict[str, object]) -> None:
        with connect(self.config.database.path) as connection:
            insert_audit_event(
                connection,
                actor="mailarchive.fastpath",
                event_type=event,
                result=result,
                account_id=account_id(connection, self.account.name),
                details_json=json.dumps(details, sort_keys=True),
            )
            connection.commit()

    def _sync_and_refresh(self, mode: str) -> bool:
        self._health(mode, last_sync_started_at=utc_now())
        try:
            results = self.sync_adapter.sync(self.account.name, "INBOX")
        except ImapSyncBusyError:
            self._health(mode, last_error_at=utc_now(), last_error_kind="sync-busy")
            return False
        except Exception:
            self.acquisition_degraded = True
            self._health(
                "degraded",
                last_error_at=utc_now(),
                last_error_kind="acquisition",
                consecutive_failures=1,
            )
            self._audit(
                "imap.fast_sync.failed", "failed", {"folder": "INBOX", "error_kind": "acquisition"}
            )
            # Acquisition failures do not create guessed identities.  A later reconciliation retries.
            return False
        self._health(mode, last_sync_succeeded_at=utc_now(), consecutive_failures=0)
        self.acquisition_degraded = False
        self._audit(
            "imap.fast_sync.succeeded",
            "success",
            {
                "folder": "INBOX",
                "fetched": len(results),
                "imported": sum(item.created for item in results),
            },
        )
        self._refresh_index(mode)
        # Indexing is independent local derived state; never re-fetch bodies to repair it.
        return True

    def _refresh_index(self, mode: str) -> bool:
        was_pending = self._index_pending()
        try:
            self.index_adapter.refresh()
            # M7 keeps independent derived indexes; classification failure can
            # safely quarantine a just-acquired message.
            NotmuchAdapter(self.config, kind="quarantine").refresh()
        except Exception:
            self._health(
                "degraded", index_pending=1, last_error_at=utc_now(), last_error_kind="indexing"
            )
            self._audit(
                "notmuch.fast_refresh.failed",
                "failed",
                {"folder": "INBOX", "error_kind": "indexing"},
            )
            return False
        self._health(
            mode, index_pending=0, last_index_succeeded_at=utc_now(), consecutive_failures=0
        )
        if was_pending:
            self._audit("notmuch.fast_refresh.recovered", "success", {"folder": "INBOX"})
        return True

    def _run_poll(self) -> None:
        assert self.account.imap is not None
        interval = self.account.imap.fast_path.poll_interval_seconds
        self._health("poll", watcher_started_at=utc_now(), last_heartbeat_at=utc_now())
        self._audit("imap.watch.mode", "poll", {"folder": "INBOX", "mode": "poll"})
        while not self.stop_event.is_set():
            self._audit("imap.watch.poll", "started", {"folder": "INBOX"})
            self._sync_and_refresh("poll")
            self._health(self._operational_mode("poll"), last_heartbeat_at=utc_now())
            self.stop_event.wait(interval)

    def _run_idle(self, connection: ImapNotificationConnection) -> None:
        assert self.account.imap is not None
        pending, last_reconcile = True, self.clock()
        self._health("idle", watcher_started_at=utc_now(), last_heartbeat_at=utc_now())
        self._audit("imap.watch.mode", "idle", {"folder": "INBOX", "mode": "idle"})
        while not self.stop_event.is_set():
            try:
                # Arm first. M3 uses a separate connection while this one is inside IDLE.
                with connection.idle(self.idle_window_seconds) as idler:
                    if pending:
                        pending = not self._sync_and_refresh("idle")
                        last_reconcile = self.clock()
                    elif self._index_pending():
                        # Local-only retry; it sends no notification-connection command.
                        self._refresh_index("idle")
                    batch = list(idler.burst(interval=BURST_INTERVAL_SECONDS))
                    self._transport_recovered = True
                    if self._consecutive_transport_failures:
                        self._reconnect_count += 1
                        self._consecutive_transport_failures = 0
                        self._health(
                            "idle",
                            reconnect_count=self._reconnect_count,
                            consecutive_failures=0,
                        )
                        self._audit(
                            "imap.watch.reconnected",
                            "success",
                            {"folder": "INBOX", "reconnect_count": self._reconnect_count},
                        )
            except OSError, EOFError, TimeoutError, imaplib.IMAP4.abort:
                raise
            except imaplib.IMAP4.error as error:
                raise FastPathIdleRejectedError("IDLE command rejected") from error
            relevant = next(
                (kind for item in batch if (kind := _event_type(item)) in _RELEVANT_EVENTS), None
            )
            if relevant is not None:
                pending = True
                self._health("idle", last_event_at=utc_now())
                self._audit(
                    "imap.watch.event", "observed", {"folder": "INBOX", "event_type": relevant}
                )
            if (
                self.clock() - last_reconcile
                >= self.account.imap.fast_path.reconcile_interval_seconds
            ):
                pending = True
            self._health(self._operational_mode("idle"), last_heartbeat_at=utc_now())

    def _index_pending(self) -> bool:
        with connect(self.config.database.path) as connection:
            row = connection.execute(
                "SELECT index_pending FROM fast_path_health JOIN accounts ON accounts.id=fast_path_health.account_id WHERE accounts.name=? AND remote_folder='INBOX'",
                (self.account.name,),
            ).fetchone()
        return row is not None and bool(row[0])

    def run(self) -> None:
        initialize(self.config.database.path, self.config.accounts)
        with watcher_lock(self.config, self.account):
            self._audit("imap.watch.started", "started", {"folder": "INBOX"})
            connection: ImapNotificationConnection | None = None
            try:
                assert self.account.imap is not None
                if not self.account.imap.fast_path.idle_enabled:
                    self._run_poll()
                    return
                while not self.stop_event.is_set():
                    try:
                        connection = self.notification_factory(self.config, self.account)
                        self._transport_recovered = False
                        if not connection.open():
                            self._health(
                                "poll", last_error_at=utc_now(), last_error_kind="idle-unsupported"
                            )
                            self._audit(
                                "imap.watch.mode",
                                "poll",
                                {"folder": "INBOX", "reason": "idle-unsupported"},
                            )
                            self._run_poll()
                            return
                        self._run_idle(connection)
                        return
                    except FastPathIdleRejectedError:
                        self._health(
                            "poll", last_error_at=utc_now(), last_error_kind="imap-protocol"
                        )
                        self._run_poll()
                        return
                    except FastPathPermanentError:
                        self._health(
                            "stopped", last_error_at=utc_now(), last_error_kind="authentication"
                        )
                        self._audit(
                            "imap.watch.failed",
                            "failed",
                            {"folder": "INBOX", "error_kind": "authentication"},
                        )
                        raise
                    except OSError, EOFError, TimeoutError, imaplib.IMAP4.abort:
                        self._consecutive_transport_failures += 1
                        delay = _BACKOFF[
                            min(self._consecutive_transport_failures - 1, len(_BACKOFF) - 1)
                        ]
                        self._health(
                            "reconnecting",
                            last_error_at=utc_now(),
                            last_error_kind="network",
                            consecutive_failures=self._consecutive_transport_failures,
                            reconnect_count=self._reconnect_count,
                        )
                        self._audit(
                            "imap.watch.reconnecting",
                            "retrying",
                            {
                                "folder": "INBOX",
                                "reconnect_count": self._reconnect_count,
                                "error_kind": "network",
                            },
                        )
                        if self.stop_event.wait(delay):
                            return
                    finally:
                        if connection is not None:
                            connection.close()
                            connection = None
            finally:
                self._health("stopped", last_heartbeat_at=utc_now())
                self._audit("imap.watch.stopped", "stopped", {"folder": "INBOX"})


def fast_path_status(config: AppConfig) -> list[HealthRecord]:
    """Local SQLite/configuration status only; deliberately performs no network I/O."""
    if not config.database.path.exists():
        return [
            HealthRecord(
                account.name,
                "INBOX",
                "idle-preferred"
                if account.imap and account.imap.fast_path.idle_enabled
                else "poll-only",
                None,
                "not-started",
                None,
                None,
                None,
                None,
                0,
                0,
                False,
                None,
            )
            for account in config.accounts
            if account.kind == "imap"
            and account.imap is not None
            and "INBOX" in account.imap.folders
        ]
    now = datetime.now(UTC)
    records: list[HealthRecord] = []
    with connect(config.database.path) as connection:
        health_exists = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fast_path_health'"
            ).fetchone()
            is not None
        )
        for account in config.accounts:
            if (
                account.kind != "imap"
                or account.imap is None
                or "INBOX" not in account.imap.folders
            ):
                continue
            row = (
                None
                if not health_exists
                else connection.execute(
                    "SELECT * FROM fast_path_health JOIN accounts ON accounts.id=fast_path_health.account_id WHERE accounts.name=? AND remote_folder='INBOX'",
                    (account.name,),
                ).fetchone()
            )
            configured = "idle-preferred" if account.imap.fast_path.idle_enabled else "poll-only"
            if row is None:
                records.append(
                    HealthRecord(
                        account.name,
                        "INBOX",
                        configured,
                        None,
                        "not-started",
                        None,
                        None,
                        None,
                        None,
                        0,
                        0,
                        False,
                        None,
                    )
                )
                continue
            heartbeat = row["last_heartbeat_at"]
            stale = (
                not heartbeat
                or (now - datetime.fromisoformat(str(heartbeat))).total_seconds()
                > FAST_PATH_STALE_SECONDS
            )
            mode = str(row["mode"])
            state = "stopped" if mode == "stopped" else ("stale" if stale else "active")
            records.append(
                HealthRecord(
                    account.name,
                    "INBOX",
                    configured,
                    mode,
                    state,
                    heartbeat,
                    row["last_event_at"],
                    row["last_sync_succeeded_at"],
                    row["last_index_succeeded_at"],
                    int(row["consecutive_failures"]),
                    int(row["reconnect_count"]),
                    bool(row["index_pending"]),
                    row["last_error_kind"],
                )
            )
    return records
