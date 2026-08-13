from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from mailarchive.config import ConfigError, load_config
from mailarchive.db import account_id, connect, initialize
from mailarchive.gmail import GmailAuthError
from mailarchive.gmail_mutation import (
    GMAIL_DELETE_SCOPE,
    GmailMutationAdapter,
    MutationAuthorizationError,
    authorize_delete,
)
from mailarchive.remote_mutation import ProviderDeletionTarget


class FakeGmail:
    def __init__(self, messages: dict[str, bytes], *, profile: str = "user@example.test") -> None:
        self.messages, self.profile_email = messages, profile
        self.deletes: list[str] = []
        self.delete_error = False
        self.unobservable = False
        self.auth_after_delete = False
        self.profile_authorization = False
        self.message_authorization = False
        self.returned_id: str | None = None
        self.malformed_raw = False
        self.calls: list[str] = []

    def profile(self) -> dict[str, object]:
        self.calls.append("profile")
        if self.profile_authorization:
            raise MutationAuthorizationError("auth")
        return {"emailAddress": self.profile_email}

    def get_raw(self, message_id: str) -> dict[str, object] | None:
        self.calls.append("get_raw")
        if self.message_authorization:
            raise MutationAuthorizationError("auth")
        if self.auth_after_delete and self.deletes:
            self.auth_after_delete = False
            raise MutationAuthorizationError("auth")
        if self.unobservable:
            raise OSError("unobservable")
        raw = self.messages.get(message_id)
        if raw is None:
            return None
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        return {
            "id": self.returned_id or message_id,
            "raw": "not valid!" if self.malformed_raw else encoded,
        }

    def delete_message_once(self, message_id: str) -> None:
        self.calls.append("delete")
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


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("absent", "confirmed-absent"),
        ("present", "confirmed-present-match"),
        ("sha", "identity-conflict"),
        ("returned-id", "identity-conflict"),
        ("profile", "identity-conflict"),
        ("malformed", "unknown"),
        ("network", "unknown"),
    ],
)
def test_observe_exact_gmail_message_is_read_only(
    config_file: Path, tmp_path: Path, setup: str, expected: str
) -> None:
    fake = FakeGmail({"target": b"target"})
    adapter, target = _adapter(config_file, tmp_path, fake)
    readonly = Path(adapter.account.config_ref[5:])
    readonly.write_bytes(b"readonly-original")
    if setup == "absent":
        fake.messages.clear()
    elif setup == "sha":
        fake.messages["target"] = b"other"
    elif setup == "returned-id":
        fake.returned_id = "different-id"
    elif setup == "profile":
        fake.profile_email = "wrong@example.test"
    elif setup == "malformed":
        fake.malformed_raw = True
    elif setup == "network":
        fake.unobservable = True
    assert adapter.observe(target).state == expected
    assert fake.deletes == []
    assert readonly.read_bytes() == b"readonly-original"


def test_observe_invalid_target_never_constructs_transport(
    config_file: Path, tmp_path: Path
) -> None:
    adapter, target = _adapter(config_file, tmp_path, FakeGmail({"target": b"target"}))
    calls = 0

    def unexpected_transport(_credentials: object) -> FakeGmail:
        nonlocal calls
        calls += 1
        raise AssertionError("transport must not be constructed")

    adapter = GmailMutationAdapter(
        adapter.config,
        "test",
        transport_factory=unexpected_transport,
    )
    invalid = replace(target, provider_message_id="invalid id")
    assert adapter.observe(invalid).state == "identity-conflict"
    assert calls == 0


def test_observe_refreshes_only_mutation_token_before_provider_access(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeGmail({"target": b"target"})
    adapter, target = _adapter(config_file, tmp_path, fake)
    readonly = Path(adapter.account.config_ref[5:])
    readonly.write_bytes(b"readonly-original")
    assert adapter.account.gmail is not None
    mutation_token = adapter.account.gmail.remote_delete_token_file
    assert mutation_token is not None
    before = mutation_token.read_bytes()
    monkeypatch.setattr(type(adapter.credentials), "valid", property(lambda _self: False))

    def refresh(_request: object) -> None:
        fake.calls.append("refresh")

    monkeypatch.setattr(adapter.credentials, "refresh", refresh)
    monkeypatch.setattr(
        adapter.credentials, "to_json", lambda: json.dumps({"scopes": [GMAIL_DELETE_SCOPE]})
    )
    assert adapter.observe(target).state == "confirmed-present-match"
    assert fake.calls[:2] == ["refresh", "profile"]
    assert mutation_token.read_bytes() != before
    assert mutation_token.stat().st_mode & 0o777 == 0o600
    assert readonly.read_bytes() == b"readonly-original"
    assert fake.deletes == []


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        ("absent", "confirmed-absent"),
        ("present", "confirmed-present-match"),
        ("unknown", "unknown"),
    ],
)
def test_observe_retries_read_only_get_once_after_authorization_failure(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after: str, expected: str
) -> None:
    initial = FakeGmail({"target": b"target"})
    initial.message_authorization = True
    retry = FakeGmail({} if after == "absent" else {"target": b"target"})
    retry.unobservable = after == "unknown"
    adapter, target = _adapter(config_file, tmp_path, initial)
    readonly = Path(adapter.account.config_ref[5:])
    readonly.write_bytes(b"readonly-original")
    transports = [initial, retry]
    factory_calls = 0

    def transport_factory(_credentials: object) -> FakeGmail:
        nonlocal factory_calls
        current = transports[factory_calls]
        factory_calls += 1
        return current

    adapter = GmailMutationAdapter(adapter.config, "test", transport_factory=transport_factory)
    refreshed = {"count": 0}

    def refresh(_request: object) -> None:
        refreshed["count"] += 1

    monkeypatch.setattr(adapter.credentials, "refresh", refresh)
    monkeypatch.setattr(
        adapter.credentials, "to_json", lambda: json.dumps({"scopes": [GMAIL_DELETE_SCOPE]})
    )
    assert adapter.observe(target).state == expected
    assert refreshed["count"] == 1 and factory_calls == 2
    assert initial.deletes == [] and retry.deletes == []
    assert readonly.read_bytes() == b"readonly-original"


def test_gmail_acquisition_stays_readonly_and_mutation_is_narrow() -> None:
    root = Path(__file__).parents[1] / "src" / "mailarchive"
    acquisition = (root / "gmail.py").read_text()
    mutation = (root / "gmail_mutation.py").read_text()
    assert "gmail.readonly" in acquisition
    for forbidden in ("batchDelete", "threads.delete", "trash", "modify", "batchModify"):
        assert forbidden not in mutation


@pytest.mark.parametrize(
    "scenario", ["success", "profile", "refresh", "readonly", "modify", "mixed"]
)
def test_authorize_delete_fake_oauth_isolated_and_fail_closed(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    fake = FakeGmail(
        {}, profile="wrong@example.test" if scenario == "profile" else "user@example.test"
    )
    adapter, _target = _adapter(config_file, tmp_path, fake)
    account = adapter.account
    assert account.gmail is not None
    secret = account.gmail.oauth_client_secret_file
    secret.write_text("{}")
    secret.chmod(0o600)
    readonly = Path(account.config_ref[5:])
    readonly.write_bytes(b"readonly-original")
    destination = account.gmail.remote_delete_token_file
    assert destination is not None
    destination.unlink()
    scopes = {
        "readonly": ["https://www.googleapis.com/auth/gmail.readonly"],
        "modify": ["https://www.googleapis.com/auth/gmail.modify"],
        "mixed": [GMAIL_DELETE_SCOPE, "https://www.googleapis.com/auth/gmail.modify"],
    }.get(scenario, [GMAIL_DELETE_SCOPE])

    class CredentialsFake:
        refresh_token = None if scenario == "refresh" else "refresh"
        def to_json(self) -> str:
            return json.dumps({"scopes": scopes, "refresh_token": self.refresh_token})

    requested: list[list[str]] = []
    class Flow:
        @classmethod
        def from_client_secrets_file(cls, _path: str, *, scopes: list[str]) -> Flow:
            requested.append(scopes)
            return cls()
        def run_local_server(self, **_kwargs: object) -> CredentialsFake:
            return CredentialsFake()

    monkeypatch.setattr("mailarchive.gmail_mutation.InstalledAppFlow", Flow)
    if scenario == "success":
        assert authorize_delete(account, lambda _credentials: fake) == "user@example.test"
        assert requested == [[GMAIL_DELETE_SCOPE]]
        assert destination.stat().st_mode & 0o777 == 0o600
        assert readonly.read_bytes() == b"readonly-original"
    else:
        with pytest.raises(GmailAuthError):
            authorize_delete(account, lambda _credentials: fake)
        assert not destination.exists()
        assert readonly.read_bytes() == b"readonly-original"


@pytest.mark.parametrize("unsafe", ["symlink", "directory", "world-readable"])
def test_mutation_token_load_rejects_unsafe_destination(
    config_file: Path, tmp_path: Path, unsafe: str
) -> None:
    adapter, _target = _adapter(config_file, tmp_path, FakeGmail({}))
    assert adapter.account.gmail is not None
    destination = adapter.account.gmail.remote_delete_token_file
    assert destination is not None
    destination.unlink()
    if unsafe == "symlink":
        target = tmp_path / "other-token.json"
        target.write_text('{"refresh_token":"do-not-expose"}')
        target.chmod(0o600)
        destination.symlink_to(target)
    elif unsafe == "directory":
        destination.mkdir()
    else:
        destination.write_text('{"refresh_token":"do-not-expose"}')
        destination.chmod(0o644)
    with pytest.raises(GmailAuthError) as error:
        GmailMutationAdapter(
            adapter.config, "test", transport_factory=lambda _credentials: FakeGmail({})
        )
    assert "do-not-expose" not in str(error.value)
