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
from mailarchive.db import connect
from mailarchive.gmail import (
    GmailAdapter,
    GmailApiClient,
    GmailAuthError,
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
