from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from mailarchive.config import ConfigError, load_config
from mailarchive.db import account_id, connect, initialize
from mailarchive.gmail_mutation import (
    GMAIL_DELETE_SCOPE,
    GmailMutationAdapter,
    MutationAuthorizationError,
)
from mailarchive.remote_mutation import ProviderDeletionTarget


class FakeGmail:
    def __init__(self, messages: dict[str, bytes], *, profile: str = "user@example.test") -> None:
        self.messages, self.profile_email = messages, profile
        self.deletes: list[str] = []
        self.delete_error = False
        self.unobservable = False
        self.auth_after_delete = False

    def profile(self) -> dict[str, object]:
        return {"emailAddress": self.profile_email}

    def get_raw(self, message_id: str) -> dict[str, object] | None:
        if self.auth_after_delete and self.deletes:
            self.auth_after_delete = False
            raise MutationAuthorizationError("auth")
        if self.unobservable:
            raise OSError("unobservable")
        raw = self.messages.get(message_id)
        if raw is None:
            return None
        return {"id": message_id, "raw": base64.urlsafe_b64encode(raw).decode().rstrip("=")}

    def delete_message_once(self, message_id: str) -> None:
        self.deletes.append(message_id)
        if self.delete_error:
            raise OSError("lost")
        self.messages.pop(message_id, None)


def _adapter(
    config_file: Path, tmp_path: Path, fake: FakeGmail
) -> tuple[GmailMutationAdapter, ProviderDeletionTarget]:
    values = yaml.safe_load(config_file.read_text())
    token = tmp_path / "delete.json"
    token.write_text(
        json.dumps(
            {
                "token": "x",
                "refresh_token": "y",
                "token_uri": "https://x",
                "client_id": "x",
                "client_secret": "y",
                "scopes": [GMAIL_DELETE_SCOPE],
                "expiry": "2099-01-01T00:00:00Z",
            }
        )
    )
    token.chmod(0o600)
    values["accounts"]["test"] = {
        "kind": "gmail",
        "enabled": True,
        "remote_retention_days": 365,
        "required_verified_backups": 2,
        "config_ref": f"file:{tmp_path / 'readonly.json'}",
        "gmail": {
            "account_email": "user@example.test",
            "oauth_client_secret_file": "/tmp/client.json",
            "remote_delete_token_file": str(token),
        },
    }
    config_file.write_text(yaml.safe_dump(values))
    config = load_config(config_file)
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as db:
        aid = account_id(db, "test")
    assert aid is not None
    raw = fake.messages.get("target", b"target")
    target = ProviderDeletionTarget(
        "remote", "canonical", aid, "test", "gmail", hashlib.sha256(raw).hexdigest(), "target"
    )
    return GmailMutationAdapter(config, "test", transport_factory=lambda _credentials: fake), target


def test_exact_message_id_delete_requires_post_get(config_file: Path, tmp_path: Path) -> None:
    fake = FakeGmail({"target": b"target", "other": b"other"})
    adapter, target = _adapter(config_file, tmp_path, fake)
    result = adapter.delete(target)
    assert (
        result.outcome == "success-confirmed"
        and fake.deletes == ["target"]
        and "other" in fake.messages
    )


@pytest.mark.parametrize(
    ("setup", "expected", "delete_count"),
    [
        ("absent", "success-confirmed", 0),
        ("hash", "IDENTITY_MISMATCH", 0),
        ("profile", "AUTHORIZATION_FAILED", 0),
    ],
)
def test_gmail_predelete_gates(
    config_file: Path, tmp_path: Path, setup: str, expected: str, delete_count: int
) -> None:
    fake = FakeGmail({"target": b"target"})
    adapter, target = _adapter(config_file, tmp_path, fake)
    if setup == "absent":
        fake.messages.clear()
    elif setup == "hash":
        fake.messages["target"] = b"other"
    else:
        fake.profile_email = "wrong@example.test"
    result = adapter.delete(target)
    assert (result.outcome if expected == "success-confirmed" else result.error_code) == expected
    assert len(fake.deletes) == delete_count


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        ("absent", "success-confirmed"),
        ("present", "PROVIDER_REJECTED"),
        ("unknown", "TRANSPORT_UNKNOWN"),
    ],
)
def test_uncertain_delete_is_observed_never_retried(
    config_file: Path, tmp_path: Path, after: str, expected: str
) -> None:
    fake = FakeGmail({"target": b"target"})
    adapter, target = _adapter(config_file, tmp_path, fake)
    fake.delete_error = True
    original = fake.delete_message_once

    def uncertain(identifier: str) -> None:
        if after == "absent":
            fake.messages.pop(identifier, None)
        elif after == "unknown":
            fake.unobservable = True
        original(identifier)

    fake.delete_message_once = uncertain  # type: ignore[method-assign]
    result = adapter.delete(target)
    assert (result.outcome if expected == "success-confirmed" else result.error_code) == expected
    assert fake.deletes == ["target"]


def test_delete_token_configuration_is_separate_and_safe(config_file: Path, tmp_path: Path) -> None:
    fake = FakeGmail({"target": b"target"})
    adapter, _target = _adapter(config_file, tmp_path, fake)
    assert adapter.account.gmail is not None
    assert str(adapter.account.gmail.remote_delete_token_file) not in adapter.account.config_ref
    values = yaml.safe_load(config_file.read_text())
    values["accounts"]["test"]["gmail"]["remote_delete_token_file"] = str(
        values["archive"]["root"] + "/bad.json"
    )
    config_file.write_text(yaml.safe_dump(values))
    with pytest.raises(ConfigError):
        load_config(config_file)


@pytest.mark.parametrize("path", ["same", "normalized"])
def test_delete_token_cannot_alias_readonly_token(
    config_file: Path, tmp_path: Path, path: str
) -> None:
    values = yaml.safe_load(config_file.read_text())
    readonly = tmp_path / "readonly.json"
    values["accounts"]["test"] = {
        "kind": "gmail",
        "enabled": True,
        "remote_retention_days": 365,
        "required_verified_backups": 2,
        "config_ref": f"file:{readonly}",
        "gmail": {
            "account_email": "user@example.test",
            "oauth_client_secret_file": "/tmp/client.json",
            "remote_delete_token_file": str(
                readonly if path == "same" else tmp_path / "x" / ".." / "readonly.json"
            ),
        },
    }
    config_file.write_text(yaml.safe_dump(values))
    with pytest.raises(ConfigError, match="differ"):
        load_config(config_file)


def test_expired_delete_credential_refreshes_before_delete(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGmail({"target": b"target"})
    adapter, target = _adapter(config_file, tmp_path, fake)
    readonly_before = Path(adapter.account.config_ref[5:])
    readonly_before.write_text("readonly")
    refreshed = {"done": False}
    monkeypatch.setattr(type(adapter.credentials), "expired", property(lambda _self: True))

    def refresh(_request: object) -> None:
        refreshed.update(done=True)

    monkeypatch.setattr(adapter.credentials, "refresh", refresh)
    monkeypatch.setattr(
        adapter.credentials, "to_json", lambda: json.dumps({"scopes": [GMAIL_DELETE_SCOPE]})
    )
    assert adapter.delete(target).outcome == "success-confirmed"
    assert refreshed["done"] and readonly_before.read_text() == "readonly"
    assert adapter.account.gmail is not None
    token_path = adapter.account.gmail.remote_delete_token_file
    assert token_path is not None and token_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        ("absent", "success-confirmed"),
        ("present", "PROVIDER_REJECTED"),
        ("unknown", "TRANSPORT_UNKNOWN"),
    ],
)
def test_post_delete_auth_refresh_retries_only_observation(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after: str, expected: str
) -> None:
    fake = FakeGmail({"target": b"target"})
    adapter, target = _adapter(config_file, tmp_path, fake)
    fake.auth_after_delete = True
    original_delete = fake.delete_message_once

    def deleted(identifier: str) -> None:
        if after == "absent":
            fake.messages.pop(identifier, None)
        elif after == "unknown":
            fake.unobservable = True
        if after != "present":
            original_delete(identifier)
        else:
            fake.deletes.append(identifier)

    fake.delete_message_once = deleted  # type: ignore[method-assign]
    refreshed = {"count": 0}
    def refresh(_request: object) -> None:
        refreshed.update(count=1)

    monkeypatch.setattr(adapter.credentials, "refresh", refresh)
    monkeypatch.setattr(
        adapter.credentials, "to_json", lambda: json.dumps({"scopes": [GMAIL_DELETE_SCOPE]})
    )
    result = adapter.delete(target)
    assert (result.outcome if expected == "success-confirmed" else result.error_code) == expected
    assert fake.deletes == ["target"] and refreshed["count"] == 1


def test_gmail_acquisition_stays_readonly_and_mutation_is_narrow() -> None:
    root = Path(__file__).parents[1] / "src" / "mailarchive"
    acquisition = (root / "gmail.py").read_text()
    mutation = (root / "gmail_mutation.py").read_text()
    assert "gmail.readonly" in acquisition
    for forbidden in ("batchDelete", "threads.delete", "trash", "modify", "batchModify"):
        assert forbidden not in mutation
