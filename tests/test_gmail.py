"""M5 Gmail semantics against an injected, non-network fake API."""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import yaml

from mailarchive.config import load_config
from mailarchive.db import connect, initialize
from mailarchive.gmail import (
    GmailAdapter,
    GmailApiClient,
    GmailAuthError,
    GmailHistoryExpired,
    GmailResponseError,
    GmailTransientError,
    GmailWatcher,
    decode_raw,
    load_credentials,
)


class FakeGmail:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.raw_gets = 0

    def profile(self) -> dict[str, object]:
        return {"emailAddress": "user@example.test"}

    def labels(self) -> dict[str, object]:
        return {
            "labels": [
                {"id": "INBOX", "name": "INBOX", "type": "system"},
                {"id": "IMPORTANT", "name": "IMPORTANT", "type": "system"},
                {"id": "Label_123", "name": "Project A", "type": "user"},
            ]
        }

    def messages(
        self, page_token: str | None = None, *, max_results: int = 500
    ) -> dict[str, object]:
        assert page_token is None
        return {"messages": [{"id": "G1"}]}

    def message(self, message_id: str, format: str) -> dict[str, object]:
        assert message_id == "G1"
        result: dict[str, object] = {
            "id": "G1",
            "threadId": "T1",
            "historyId": "10",
            "labelIds": ["INBOX", "IMPORTANT", "Label_123"],
        }
        if format == "raw":
            self.raw_gets += 1
            result["raw"] = base64.urlsafe_b64encode(self.raw).decode().rstrip("=")
        return result

    def history(self, history_id: str, page_token: str | None = None) -> dict[str, object]:
        assert history_id in {"10", "11"} and page_token is None
        return {"historyId": "11", "history": [{"labelsAdded": [{"message": {"id": "G1"}}]}]}


def _gmail_config(tmp_path: Path) -> Path:
    path = tmp_path / "gmail.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "archive": {"root": str(tmp_path / "archive"), "timezone": "UTC"},
                "database": {"path": str(tmp_path / "state" / "db.sqlite3")},
                "accounts": {
                    "gmail": {
                        "kind": "gmail",
                        "enabled": True,
                        "remote_retention_days": 365,
                        "required_verified_backups": 2,
                        "config_ref": f"file:{tmp_path / 'token.json'}",
                        "gmail": {
                            "account_email": "user@example.test",
                            "oauth_client_secret_file": "/tmp/client.json",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_raw_decode_and_label_multiplicity_do_not_duplicate_canonical(tmp_path: Path) -> None:
    raw = b"Message-ID: <a@example.test>\r\nX-Folded: a\r\n b\r\n\r\nbody\x00"
    assert decode_raw(base64.urlsafe_b64encode(raw).decode().rstrip("=")) == raw
    config = load_config(_gmail_config(tmp_path))
    fake = FakeGmail(raw)
    adapter = GmailAdapter(config, lambda _account: fake)  # type: ignore[arg-type]
    first = adapter.sync("gmail")
    second = adapter.sync("gmail")
    assert first.mode == "full" and first.fetched_raw == 1
    assert second.mode == "partial" and fake.raw_gets == 1
    with connect(config.database.path) as db:
        assert db.execute("SELECT COUNT(*) FROM remote_messages").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM remote_canonical_links").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM gmail_message_labels").fetchone()[0] == 3
        stored = Path(str(db.execute("SELECT local_path FROM canonical_messages").fetchone()[0]))
    assert stored.read_bytes() == raw


def test_client_uses_only_get_for_mailbox_calls() -> None:
    calls: list[str] = []

    class Response:
        status_code = 200
        headers: dict[str, str] = {}

        def json(self) -> object:
            return {"emailAddress": "user@example.test"}

    class Session:
        def get(self, *args: object, **kwargs: object) -> Response:
            calls.append("GET")
            return Response()

    GmailApiClient(Session()).profile()
    assert calls == ["GET"]


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_get_retries_with_injected_sleeper(status: int) -> None:
    delays: list[float] = []

    class Response:
        def __init__(self, code: int) -> None:
            self.status_code = code
            self.headers = {"Retry-After": "1"}

        def json(self) -> object:
            return {"emailAddress": "user@example.test"}

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, *args: object, **kwargs: object) -> Response:
            self.calls += 1
            return Response(status if self.calls < 3 else 200)

    session = Session()
    GmailApiClient(session, sleeper=delays.append).profile()
    assert session.calls == 3 and delays == [1.0, 1.0]


def test_http_retry_exhaustion_and_auth_categories() -> None:
    class Response:
        def __init__(self, status: int, retry: str | None = None) -> None:
            self.status_code, self.headers = status, {} if retry is None else {"Retry-After": retry}

        def json(self) -> object:
            return {}

    class Session:
        def __init__(self, status: int) -> None:
            self.status, self.calls = status, 0

        def get(self, *args: object, **kwargs: object) -> Response:
            self.calls += 1
            return Response(self.status, "not-a-number")

    for status, error in (
        (429, GmailTransientError),
        (500, GmailTransientError),
        (401, GmailAuthError),
        (403, GmailAuthError),
        (418, GmailResponseError),
    ):
        session = Session(status)
        typed_sleeps: list[float] = []
        with pytest.raises(error):
            GmailApiClient(session, sleeper=typed_sleeps.append).profile()
        assert session.calls == (3 if status in {429, 500} else 1)


def test_full_list_failure_and_cyclic_token_fail_closed(tmp_path: Path) -> None:
    class Listing(FakeGmail):
        def __init__(self, cyclic: bool) -> None:
            super().__init__(b"Message-ID: <g1>\r\n\r\none")
            self.cyclic = cyclic

        def messages(
            self, page_token: str | None = None, *, max_results: int = 500
        ) -> dict[str, object]:
            if max_results == 1:
                return {"messages": [{"id": "G1"}]}
            if page_token is None:
                return {"messages": [{"id": "G1"}], "nextPageToken": "p2"}
            if self.cyclic:
                return {"messages": [], "nextPageToken": "p2"}
            raise GmailResponseError("page two failed")

        def message(self, message_id: str, format: str) -> dict[str, object]:
            return {
                "id": "G1",
                "threadId": "T",
                "historyId": "1",
                "labelIds": ["INBOX"],
                "raw": base64.urlsafe_b64encode(self.raw).decode().rstrip("="),
            }

    for cyclic in (False, True):
        case = tmp_path / str(cyclic)
        case.mkdir()
        config = load_config(_gmail_config(case))
        fake = Listing(cyclic)
        with pytest.raises(GmailResponseError):
            GmailAdapter(config, lambda _account, value=fake: value).sync("gmail")  # type: ignore[arg-type]
        with connect(config.database.path) as db:
            assert db.execute("SELECT full_sync_required FROM gmail_sync_state").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM canonical_messages").fetchone()[0] == 1


def test_notmuch_recovery_persists_target_mode_atomically(tmp_path: Path) -> None:
    config = load_config(_gmail_config(tmp_path))
    watcher = GmailWatcher(config, "gmail", threading.Event(), refresh=lambda: None)
    watcher._health("degraded", index_pending=1)  # pyright: ignore[reportPrivateUsage]
    assert watcher._refresh()  # pyright: ignore[reportPrivateUsage]
    with connect(config.database.path) as db:
        assert tuple(
            db.execute(
                "SELECT mode,index_pending,last_index_succeeded_at FROM fast_path_health"
            ).fetchone()
        )[:2] == ("poll", 0)
    watcher.acquisition_degraded = True
    watcher._health("degraded", index_pending=1)  # pyright: ignore[reportPrivateUsage]
    assert watcher._refresh()  # pyright: ignore[reportPrivateUsage]
    with connect(config.database.path) as db:
        assert tuple(db.execute("SELECT mode,index_pending FROM fast_path_health").fetchone()) == (
            "degraded",
            0,
        )


def test_loopback_client_exercises_only_documented_get_routes() -> None:
    requests_seen: list[tuple[str, str, dict[str, list[str]]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            requests_seen.append(("GET", parsed.path, parse_qs(parsed.query)))
            payload: dict[str, object]
            if parsed.path.endswith("/profile"):
                payload = {"emailAddress": "user@example.test"}
            elif parsed.path.endswith("/labels"):
                payload = {"labels": []}
            elif parsed.path.endswith("/history"):
                payload = {"historyId": "2", "history": []}
            elif parsed.path.endswith("/messages"):
                payload = {"messages": []}
            else:
                payload = {"id": "G1", "historyId": "1", "labelIds": []}
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        client = GmailApiClient(
            requests.Session(), base_url=f"http://127.0.0.1:{server.server_port}"
        )
        client.profile()
        client.labels()
        client.messages()
        client.message("G1", "minimal")
        client.history("1")
    finally:
        server.shutdown()
        thread.join()
    assert all(method == "GET" for method, _, _ in requests_seen)
    assert requests_seen[2][2]["includeSpamTrash"] == ["true"]


def test_loopback_full_and_partial_sync_acceptance(tmp_path: Path) -> None:
    raw = {
        "G1": b"Message-ID: <g1>\r\nX-Folded: one\r\n two\r\n\r\nbody-1",
        "G2": b"Message-ID: <g2>\r\n\r\nbody-2",
        "G3": b"Message-ID: <g3>\r\n\r\nbody-3",
        "G4": b"Message-ID: <g4>\r\n\r\nbody-4",
        "G6": b"Message-ID: <g6>\r\n\r\nbody-6",
    }
    state: dict[str, object] = {
        "phase": "full",
        "calls": [],
        "raw_gets": [],
        "labels": [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "IMPORTANT", "name": "IMPORTANT", "type": "system"},
            {"id": "SENT", "name": "SENT", "type": "system"},
            {"id": "SPAM", "name": "SPAM", "type": "system"},
            {"id": "TRASH", "name": "TRASH", "type": "system"},
            {"id": "STARRED", "name": "STARRED", "type": "system"},
            {"id": "Label_A", "name": "Project A", "type": "user"},
        ],
        "messages": {
            "G1": ["INBOX", "IMPORTANT", "Label_A"],
            "G2": ["SENT", "Label_A"],
            "G3": ["SPAM"],
            "G4": ["TRASH"],
            "G6": ["STARRED", "Label_A"],
        },
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed, query = urlparse(self.path), parse_qs(urlparse(self.path).query)
            cast(list[tuple[str, str, dict[str, list[str]]]], state["calls"]).append(
                ("GET", parsed.path, query)
            )
            route = parsed.path.rsplit("/", 1)[-1]
            if parsed.path.endswith("/profile"):
                payload: dict[str, object] = {"emailAddress": "user@example.test"}
            elif parsed.path.endswith("/labels"):
                payload = {"labels": state["labels"]}
            elif parsed.path.endswith("/history"):
                start = query["startHistoryId"][0]
                if state["phase"] == "full":
                    assert start == "1"
                    payload = {"historyId": "2", "history": []}
                else:
                    assert start == "2"
                    payload = {
                        "historyId": "3",
                        "history": [
                            {
                                "messagesAdded": [{"message": {"id": "G6"}}],
                                "labelsAdded": [
                                    {"message": {"id": "G1"}},
                                    {"message": {"id": "G1"}},
                                ],
                                "messagesDeleted": [{"message": {"id": "G2"}}],
                            }
                        ],
                    }
            elif parsed.path.endswith("/messages"):
                if query.get("maxResults") == ["1"]:
                    payload = {"messages": [{"id": "G4"}]}
                elif query.get("pageToken") == ["p2"]:
                    payload = {"messages": [{"id": "G3"}, {"id": "G4"}]}
                else:
                    payload = {"messages": [{"id": "G1"}, {"id": "G2"}], "nextPageToken": "p2"}
            else:
                mid, fmt = route, query["format"][0]
                labels = cast(dict[str, list[str]], state["messages"])[mid]
                payload = {
                    "id": mid,
                    "threadId": f"T{mid[-1]}",
                    "historyId": "1" if state["phase"] == "full" else "3",
                    "labelIds": labels,
                }
                if fmt == "raw":
                    cast(list[str], state["raw_gets"]).append(mid)
                    payload["raw"] = base64.urlsafe_b64encode(raw[mid]).decode().rstrip("=")
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        config = load_config(_gmail_config(tmp_path))

        def client(_account: object) -> GmailApiClient:
            return GmailApiClient(
                requests.Session(), base_url=f"http://127.0.0.1:{server.server_port}"
            )

        adapter = GmailAdapter(config, client)  # type: ignore[arg-type]
        full = adapter.sync("gmail")
        assert full.mode == "full" and full.history_to == "2"
        state["phase"] = "partial"
        state["labels"] = [
            *cast(list[object], state["labels"])[:-1],
            {"id": "Label_A", "name": "Archived Project A", "type": "user"},
        ]
        state["messages"] = {
            **cast(dict[str, list[str]], state["messages"]),
            "G1": ["INBOX", "Label_A"],
        }
        partial = adapter.sync("gmail")
    finally:
        server.shutdown()
        thread.join()
    assert partial.history_to == "3"
    assert cast(list[str], state["raw_gets"]).count("G6") == 1
    assert cast(list[str], state["raw_gets"]).count("G1") == 1
    assert all(
        method == "GET"
        for method, _, _ in cast(list[tuple[str, str, dict[str, list[str]]]], state["calls"])
    )
    assert any(
        query.get("includeSpamTrash") == ["true"]
        for _, path, query in cast(list[tuple[str, str, dict[str, list[str]]]], state["calls"])
        if path.endswith("/messages")
    )
    with connect(config.database.path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM remote_messages WHERE provider_kind='gmail'"
            ).fetchone()[0]
            == 5
        )
        assert db.execute("SELECT COUNT(*) FROM canonical_messages").fetchone()[0] == 5
        assert (
            db.execute(
                "SELECT remote_present FROM remote_messages WHERE provider_message_id='G2'"
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute("SELECT name FROM gmail_labels WHERE label_id='Label_A'").fetchone()[0]
            == "Archived Project A"
        )
        assert db.execute("SELECT history_id FROM gmail_sync_state").fetchone()[0] == "3"
        for mid, expected in raw.items():
            if mid == "G2" or mid in {"G1", "G3", "G4", "G6"}:
                row = db.execute(
                    "SELECT local_path FROM canonical_messages JOIN remote_canonical_links "
                    "ON canonical_messages.id=remote_canonical_links.canonical_message_id "
                    "JOIN remote_messages ON remote_messages.id="
                    "remote_canonical_links.remote_message_id "
                    "WHERE provider_message_id=?",
                    (mid,),
                ).fetchone()
                assert Path(str(row[0])).read_bytes() == expected


def test_stored_gmail_scope_is_proven_before_credentials_are_constructed(tmp_path: Path) -> None:
    config = load_config(_gmail_config(tmp_path))
    token = tmp_path / "token.json"
    token.chmod(0o600) if token.exists() else None
    acceptable = ["https://www.googleapis.com/auth/gmail.readonly"]
    rejected = [
        ["https://mail.google.com/"],
        ["https://www.googleapis.com/auth/gmail.modify"],
        ["https://www.googleapis.com/auth/gmail.compose"],
        ["https://www.googleapis.com/auth/gmail.send"],
        ["https://www.googleapis.com/auth/gmail.insert"],
        acceptable + ["https://www.googleapis.com/auth/gmail.modify"],
        None,
    ]
    token.write_text(
        json.dumps(
            {
                "token": "secret",
                "refresh_token": "secret",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "id",
                "client_secret": "secret",
                "scopes": acceptable,
            }
        ),
        encoding="utf-8",
    )
    token.chmod(0o600)
    scopes = cast(list[str] | None, load_credentials(config.accounts[0]).scopes)  # pyright: ignore[reportUnknownMemberType]
    assert list(scopes or []) == acceptable
    for scopes in rejected:
        payload: dict[str, object] = {
            "token": "secret",
            "refresh_token": "secret",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "id",
            "client_secret": "secret",
        }
        if scopes is not None:
            payload["scopes"] = scopes
        token.write_text(json.dumps(payload), encoding="utf-8")
        token.chmod(0o600)
        with pytest.raises(GmailAuthError, match="readonly"):
            load_credentials(config.accounts[0])


def test_full_sync_history_catchup_preserves_post_anchor_message_presence(tmp_path: Path) -> None:
    class RaceGmail(FakeGmail):
        def __init__(self) -> None:
            super().__init__(b"Message-ID: <unused>\r\n\r\nunused")
            self.bodies = {
                "G1": b"Message-ID: <one>\r\n\r\none",
                "G2": b"Message-ID: <two>\r\n\r\ntwo",
                "G3": b"Message-ID: <three>\r\n\r\nthree",
                "G5": b"Message-ID: <five>\r\n\r\nfive",
            }

        def labels(self) -> dict[str, object]:
            return {
                "labels": [
                    {"id": "INBOX", "name": "INBOX", "type": "system"},
                    {"id": "STARRED", "name": "STARRED", "type": "system"},
                    {"id": "Label_A", "name": "A", "type": "user"},
                ]
            }

        def messages(
            self, page_token: str | None = None, *, max_results: int = 500
        ) -> dict[str, object]:
            if max_results == 1:
                return {"messages": [{"id": "G1"}]}
            if page_token is None:
                return {"messages": [{"id": "G1"}, {"id": "G2"}], "nextPageToken": "page-2"}
            assert page_token == "page-2"
            return {"messages": [{"id": "G3"}]}

        def message(self, message_id: str, format: str) -> dict[str, object]:
            labels = {
                "G1": ["STARRED", "Label_A"],
                "G2": ["INBOX"],
                "G3": ["INBOX"],
                "G5": ["INBOX"],
            }[message_id]
            result: dict[str, object] = {
                "id": message_id,
                "threadId": "shared",
                "historyId": "1",
                "labelIds": labels,
            }
            if format == "raw":
                result["raw"] = (
                    base64.urlsafe_b64encode(self.bodies[message_id]).decode().rstrip("=")
                )
            return result

        def history(self, history_id: str, page_token: str | None = None) -> dict[str, object]:
            assert history_id == "1" and page_token is None
            return {
                "historyId": "2",
                "history": [
                    {
                        "messagesAdded": [{"message": {"id": "G5"}}],
                        "labelsAdded": [{"message": {"id": "G1"}}],
                        "messagesDeleted": [{"message": {"id": "G2"}}],
                    }
                ],
            }

    config = load_config(_gmail_config(tmp_path))
    result = GmailAdapter(config, lambda _account: RaceGmail()).sync("gmail")  # type: ignore[arg-type]
    assert result.history_to == "2"
    with connect(config.database.path) as db:
        present = {
            str(row[0]): int(row[1])
            for row in db.execute("SELECT provider_message_id,remote_present FROM remote_messages")
        }
        assert present == {"G1": 1, "G2": 0, "G3": 1, "G5": 1}
        assert db.execute("SELECT history_id FROM gmail_sync_state").fetchone()[0] == "2"
        assert (
            db.execute(
                "SELECT COUNT(*) FROM remote_canonical_links WHERE remote_message_id IN "
                "(SELECT id FROM remote_messages WHERE provider_message_id='G5')"
            ).fetchone()[0]
            == 1
        )


def test_partial_failure_replays_old_checkpoint_without_forcing_full_sync(tmp_path: Path) -> None:
    class ReplayGmail(FakeGmail):
        def __init__(self) -> None:
            super().__init__(b"")
            self.fail_g2 = True
            self.history_starts: list[str] = []

        def labels(self) -> dict[str, object]:
            return {"labels": [{"id": "INBOX", "name": "INBOX", "type": "system"}]}

        def history(self, history_id: str, page_token: str | None = None) -> dict[str, object]:
            self.history_starts.append(history_id)
            return {
                "historyId": "2",
                "history": [
                    {"messagesAdded": [{"message": {"id": "G1"}}, {"message": {"id": "G2"}}]}
                ],
            }

        def message(self, message_id: str, format: str) -> dict[str, object]:
            if message_id == "G2" and self.fail_g2:
                raise GmailResponseError("injected failure")
            raw = f"Message-ID: <{message_id}>\r\n\r\n{message_id}".encode()
            return {
                "id": message_id,
                "threadId": message_id,
                "historyId": "2",
                "labelIds": ["INBOX"],
                "raw": base64.urlsafe_b64encode(raw).decode().rstrip("="),
            }

    config = load_config(_gmail_config(tmp_path))
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as db:
        aid = int(db.execute("SELECT id FROM accounts WHERE name='gmail'").fetchone()[0])
        db.execute(
            "INSERT INTO gmail_sync_state(account_id,history_id,full_sync_required,updated_at) "
            "VALUES (?, '1', 0, 'now')",
            (aid,),
        )
        db.commit()
    fake = ReplayGmail()
    adapter = GmailAdapter(config, lambda _account: fake)  # type: ignore[arg-type]
    with pytest.raises(GmailResponseError):
        adapter.sync("gmail")
    with connect(config.database.path) as db:
        assert tuple(
            db.execute("SELECT history_id,full_sync_required FROM gmail_sync_state").fetchone()
        ) == ("1", 0)
        assert db.execute("SELECT COUNT(*) FROM canonical_messages").fetchone()[0] == 1
    fake.fail_g2 = False
    assert adapter.sync("gmail").history_to == "2"
    assert fake.history_starts == ["1", "1"]


def test_expired_history_full_failure_and_empty_fallback_keep_safe_state(tmp_path: Path) -> None:
    class ExpiredGmail(FakeGmail):
        def __init__(self, fail_full: bool) -> None:
            super().__init__(b"")
            self.fail_full = fail_full

        def labels(self) -> dict[str, object]:
            return {"labels": []}

        def history(self, history_id: str, page_token: str | None = None) -> dict[str, object]:
            raise GmailHistoryExpired("expired")

        def messages(
            self, page_token: str | None = None, *, max_results: int = 500
        ) -> dict[str, object]:
            if self.fail_full:
                raise GmailResponseError("full listing failed")
            return {"messages": []}

    for fail_full in (True, False):
        case = tmp_path / str(fail_full)
        case.mkdir()
        config = load_config(_gmail_config(case))
        initialize(config.database.path, config.accounts)
        with connect(config.database.path) as db:
            aid = int(db.execute("SELECT id FROM accounts WHERE name='gmail'").fetchone()[0])
            db.execute(
                "INSERT INTO gmail_sync_state(account_id,history_id,full_sync_required,updated_at) "
                "VALUES (?, '1', 0, 'now')",
                (aid,),
            )
            db.commit()
        adapter = GmailAdapter(config, lambda _account, value=fail_full: ExpiredGmail(value))  # type: ignore[arg-type]
        if fail_full:
            with pytest.raises(GmailResponseError):
                adapter.sync("gmail")
        else:
            assert adapter.sync("gmail").mode == "full"
        with connect(config.database.path) as db:
            assert tuple(
                db.execute("SELECT history_id,full_sync_required FROM gmail_sync_state").fetchone()
            ) == (None if not fail_full else "1", 1)
