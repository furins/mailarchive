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
