"""M5 Gmail semantics against an injected, non-network fake API."""

from __future__ import annotations

import base64
from pathlib import Path

import yaml

from mailarchive.config import load_config
from mailarchive.db import connect
from mailarchive.gmail import GmailAdapter, GmailApiClient, decode_raw


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

    def messages(self, page_token: str | None = None) -> dict[str, object]:
        assert page_token is None
        return {"messages": [{"id": "G1"}], "historyId": "10"}

    def message(self, message_id: str, format: str) -> dict[str, object]:
        assert message_id == "G1"
        result: dict[str, object] = {
            "id": "G1",
            "threadId": "T1",
            "labelIds": ["INBOX", "IMPORTANT", "Label_123"],
        }
        if format == "raw":
            self.raw_gets += 1
            result["raw"] = base64.urlsafe_b64encode(self.raw).decode().rstrip("=")
        return result

    def history(self, history_id: str, page_token: str | None = None) -> dict[str, object]:
        assert history_id == "10" and page_token is None
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
                        "config_ref": "file:/tmp/mailarchive-test-token.json",
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
