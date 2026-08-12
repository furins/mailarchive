"""Deterministic M4 watcher orchestration and safety regressions."""
# ruff: noqa: E501
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportArgumentType=false

from __future__ import annotations

import imaplib
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from mailarchive.config import ConfigError, load_config
from mailarchive.fastpath import (
    FastPathPermanentError,
    FastPathWatcher,
    fast_path_status,
    watcher_lock,
)
from mailarchive.imap import ImapSyncBusyError


def _configure(config_file: Path, *, idle_enabled: bool = True) -> None:
    values = yaml.safe_load(config_file.read_text())
    values["accounts"]["test"]["imap"] = {
        "host": "127.0.0.1", "port": 1993, "username": "fixture",
        "tls_mode": "INSECURE_LOOPBACK", "folders": ["INBOX"],
        "fast_path": {"idle_enabled": idle_enabled, "reconcile_interval_seconds": 600,
                      "poll_interval_seconds": 90},
    }
    config_file.write_text(yaml.safe_dump(values))


class _Idler:
    def __init__(self, batches: list[list[object]], log: list[str], stop: threading.Event) -> None:
        self.batches, self.log, self.stop = batches, log, stop

    def burst(self, *, interval: float) -> list[object]:
        self.log.append("burst")
        batch = self.batches.pop(0) if self.batches else []
        if not self.batches:
            self.stop.set()
        return batch


class _Notification:
    def __init__(self, batches: list[list[object]], log: list[str], stop: threading.Event, idle: bool = True) -> None:
        self.batches, self.log, self.stop, self.idle_supported = batches, log, stop, idle

    def open(self) -> bool:
        self.log.extend(["connect", "login", "select-readonly", "capability"])
        return self.idle_supported

    @contextmanager
    def idle(self, duration: float) -> Generator[_Idler]:
        self.log.append(f"idle-enter:{duration}")
        try:
            yield _Idler(self.batches, self.log, self.stop)
        finally:
            self.log.append("idle-exit")

    def close(self) -> None:
        self.log.append("logout")

    def __getattr__(self, name: str) -> object:
        if name.lower() in {"fetch", "uid", "store", "copy", "move", "append", "expunge", "delete", "create", "rename", "close", "subscribe", "unsubscribe"}:
            raise AssertionError(f"forbidden notification method: {name}")
        raise AttributeError(name)


class _Sync:
    def __init__(self, log: list[str], outcomes: list[object] | None = None) -> None:
        self.log, self.outcomes, self.calls = log, outcomes or [], []

    def sync(self, account: str, folder: str) -> list[Any]:
        self.log.append(f"sync:{folder}")
        self.calls.append((account, folder))
        if self.outcomes:
            result = self.outcomes.pop(0)
            if isinstance(result, BaseException):
                raise result
        return []


class _Index:
    def __init__(self, log: list[str], outcomes: list[object] | None = None) -> None:
        self.log, self.outcomes, self.calls = log, outcomes or [], 0

    def refresh(self) -> None:
        self.log.append("refresh")
        self.calls += 1
        if self.outcomes:
            result = self.outcomes.pop(0)
            if isinstance(result, BaseException):
                raise result


def _watcher(config_file: Path, batches: list[list[object]], log: list[str], stop: threading.Event, **kwargs: object) -> FastPathWatcher:
    config = load_config(config_file)
    notification = _Notification(batches, log, stop, kwargs.pop("idle", True))
    return FastPathWatcher(config, "test", stop, notification_factory=lambda *_: notification,
                           sync_adapter=kwargs.pop("sync", _Sync(log)), index_adapter=kwargs.pop("index", _Index(log)),
                           idle_window_seconds=30, **kwargs)


def test_startup_arms_public_idle_before_m3_sync(config_file: Path) -> None:
    _configure(config_file)
    stop, log = threading.Event(), []
    watcher = _watcher(config_file, [[]], log, stop)
    watcher.run()
    assert log.index("idle-enter:30") < log.index("sync:INBOX") < log.index("refresh")
    assert log.index("burst") < log.index("idle-exit") < log.index("logout")


def test_burst_is_coalesced_and_never_uses_sequence_as_uid(config_file: Path) -> None:
    _configure(config_file)
    stop, log = threading.Event(), []
    watcher = _watcher(config_file, [[(b"EXISTS", b"99"), (b"FETCH", b"100")], []], log, stop)
    watcher.run()
    assert log.count("sync:INBOX") == 2  # startup + one event-triggered catch-up
    assert all(folder == "INBOX" for _, folder in watcher.sync_adapter.calls)  # type: ignore[attr-defined]
    assert log.index("idle-exit") < log[log.index("sync:INBOX") + 1 :].index("sync:INBOX") + log.index("sync:INBOX") + 1


def test_empty_idle_window_does_not_sync_again(config_file: Path) -> None:
    _configure(config_file)
    stop, log = threading.Event(), []
    watcher = _watcher(config_file, [[], []], log, stop)
    watcher.run()
    assert log.count("sync:INBOX") == 1


def test_unsupported_idle_and_disabled_idle_use_poll_stop_aware(config_file: Path) -> None:
    _configure(config_file, idle_enabled=False)
    stop, log = threading.Event(), []
    stop.set()
    watcher = _watcher(config_file, [], log, stop)
    watcher.run()
    assert "sync:INBOX" not in log
    _configure(config_file)
    stop, log = threading.Event(), []
    stop.set()
    watcher = _watcher(config_file, [], log, stop, idle=False)
    watcher.run()
    assert "sync:INBOX" not in log


def test_sync_busy_stays_pending_but_index_failure_does_not_refetch(config_file: Path) -> None:
    _configure(config_file)
    stop, log = threading.Event(), []
    sync, index = _Sync(log, [ImapSyncBusyError("busy"), None]), _Index(log, [RuntimeError(), None])
    watcher = _watcher(config_file, [[], [], []], log, stop, sync=sync, index=index)
    watcher.run()
    assert sync.calls == [("test", "INBOX"), ("test", "INBOX")]
    assert index.calls >= 2


def test_status_is_local_and_stale_heartbeat_wins(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(config_file)
    config = load_config(config_file)
    stop, log = threading.Event(), []
    watcher = _watcher(config_file, [], log, stop)
    watcher._health("idle", last_heartbeat_at="2000-01-01T00:00:00+00:00")  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr("mailarchive.imap.imaplib.IMAP4", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()))
    status = fast_path_status(config)[0]
    assert status.effective_mode == "idle" and status.state == "stale"
    watcher._health("stopped", last_heartbeat_at="2000-01-01T00:00:00+00:00")  # pyright: ignore[reportPrivateUsage]
    assert fast_path_status(config)[0].state == "stopped"


def test_authentication_failure_stops_without_polling_or_sync(config_file: Path) -> None:
    _configure(config_file)
    stop, log = threading.Event(), []

    class _Rejected(_Notification):
        def open(self) -> bool:
            raise FastPathPermanentError("IMAP authentication failed")

    config = load_config(config_file)
    watcher = FastPathWatcher(config, "test", stop, notification_factory=lambda *_: _Rejected([], log, stop), sync_adapter=_Sync(log), index_adapter=_Index(log))  # pyright: ignore[reportArgumentType]
    with pytest.raises(FastPathPermanentError):
        watcher.run()
    assert "sync:INBOX" not in log and "poll" not in log
    assert fast_path_status(config)[0].state == "stopped"


def test_real_imaplib_login_rejection_is_safe_permanent_failure(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(config_file)
    monkeypatch.setenv("MAILARCHIVE_TEST_SECRET", "secret-value")
    config, stop, log = load_config(config_file), threading.Event(), []

    class _Client:
        def login(self, _user: str, _password: str) -> tuple[str, list[bytes]]:
            raise imaplib.IMAP4.error("fixture authentication rejected")

        def logout(self) -> None:
            log.append("logout")

    monkeypatch.setattr("mailarchive.fastpath.ImapAdapter.open_notification_connection", lambda *_: _Client())
    watcher = FastPathWatcher(config, "test", stop, sync_adapter=_Sync(log), index_adapter=_Index(log))
    with pytest.raises(FastPathPermanentError):
        watcher.run()
    assert log == ["logout"]
    status = fast_path_status(config)[0]
    assert status.state == "stopped" and status.last_error_kind == "authentication"
    from mailarchive.db import connect
    with connect(config.database.path) as connection:
        stored = str(connection.execute("SELECT group_concat(details_json) FROM audit_events").fetchone()[0])
    assert "fixture authentication rejected" not in stored and "secret-value" not in stored
    with watcher_lock(config, config.accounts[0]):
        pass


def test_acquisition_degradation_persists_until_success_in_idle_and_poll(config_file: Path) -> None:
    _configure(config_file)
    stop, log = threading.Event(), []
    sync = _Sync(log, [RuntimeError("acquire"), None, RuntimeError("acquire"), None])
    watcher = _watcher(config_file, [], log, stop, sync=sync)
    assert not watcher._sync_and_refresh("idle")  # pyright: ignore[reportPrivateUsage]
    watcher._health(watcher._operational_mode("idle"), last_heartbeat_at="2026-01-01T00:00:00+00:00")  # pyright: ignore[reportPrivateUsage]
    assert fast_path_status(load_config(config_file))[0].effective_mode == "degraded"
    assert watcher._sync_and_refresh("idle")  # pyright: ignore[reportPrivateUsage]
    watcher._health(watcher._operational_mode("idle"))  # pyright: ignore[reportPrivateUsage]
    assert fast_path_status(load_config(config_file))[0].effective_mode == "idle"
    assert not watcher._sync_and_refresh("poll")  # pyright: ignore[reportPrivateUsage]
    watcher._health(watcher._operational_mode("poll"))  # pyright: ignore[reportPrivateUsage]
    assert fast_path_status(load_config(config_file))[0].effective_mode == "degraded"
    assert watcher._sync_and_refresh("poll")  # pyright: ignore[reportPrivateUsage]
    watcher._health(watcher._operational_mode("poll"))  # pyright: ignore[reportPrivateUsage]
    assert fast_path_status(load_config(config_file))[0].effective_mode == "poll"


def test_reconnect_count_is_cumulative_and_backoff_resets(config_file: Path) -> None:
    _configure(config_file)
    stop, log, waits = threading.Event(), [], []

    class _Broken(_Notification):
        @contextmanager
        def idle(self, duration: float) -> Generator[_Idler]:
            self.log.append(f"idle-enter:{duration}")
            raise OSError("fixture disconnect")
            yield _Idler([], self.log, self.stop)

    class _EventuallyBroken(_Notification):
        def __init__(self, *args: object) -> None:
            super().__init__(*args)  # type: ignore[arg-type]
            self.entries = 0

        @contextmanager
        def idle(self, duration: float) -> Generator[_Idler]:
            self.entries += 1
            if self.entries >= 3:
                raise OSError("fixture disconnect")
            with super().idle(duration) as idler:
                yield idler

    connections = [
        _Broken([], log, stop),
        _EventuallyBroken([[], [], []], log, stop),
        _Notification([[]], log, stop),
    ]
    config = load_config(config_file)
    watcher = FastPathWatcher(config, "test", stop, notification_factory=lambda *_: connections.pop(0), sync_adapter=_Sync(log), index_adapter=_Index(log))  # pyright: ignore[reportArgumentType]
    def wait(delay: float) -> bool:
        waits.append(delay)
        return False
    stop.wait = wait  # type: ignore[method-assign]
    watcher.run()
    status = fast_path_status(config)[0]
    assert waits == [1, 1]
    assert status.reconnect_count == 2 and status.consecutive_failures == 0


def test_abort_uses_capped_consecutive_transport_backoff(config_file: Path) -> None:
    _configure(config_file)
    stop, log, waits = threading.Event(), [], []

    class _Abort(_Notification):
        @contextmanager
        def idle(self, duration: float) -> Generator[_Idler]:
            raise imaplib.IMAP4.abort("fixture abort")
            yield _Idler([], self.log, self.stop)

    config = load_config(config_file)
    connections = [_Abort([], log, stop) for _ in range(7)]
    watcher = FastPathWatcher(config, "test", stop, notification_factory=lambda *_: connections.pop(0), sync_adapter=_Sync(log), index_adapter=_Index(log))  # pyright: ignore[reportArgumentType]
    def wait(delay: float) -> bool:
        waits.append(delay)
        return len(waits) == 7
    stop.wait = wait  # type: ignore[method-assign]
    watcher.run()
    assert waits == [1, 2, 5, 10, 30, 60, 60]


def test_watcher_lock_is_distinct_and_released(config_file: Path) -> None:
    _configure(config_file)
    config = load_config(config_file)
    account = config.accounts[0]
    with watcher_lock(config, account):
        with pytest.raises(Exception, match="watcher is already"):
            with watcher_lock(config, account):
                pass
    with watcher_lock(config, account):
        pass


@pytest.mark.parametrize("key,value", [("reconcile_interval_seconds", 59), ("reconcile_interval_seconds", 1741), ("poll_interval_seconds", 59), ("poll_interval_seconds", 121)])
def test_fast_path_timing_validation(config_file: Path, key: str, value: int) -> None:
    _configure(config_file)
    values = yaml.safe_load(config_file.read_text())
    values["accounts"]["test"]["imap"]["fast_path"][key] = value
    config_file.write_text(yaml.safe_dump(values))
    with pytest.raises(ConfigError):
        load_config(config_file)
