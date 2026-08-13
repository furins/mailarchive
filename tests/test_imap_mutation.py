from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
import yaml

from mailarchive.config import load_config
from mailarchive.db import account_id, connect, initialize
from mailarchive.imap_mutation import ImapClient, ImapMutationAdapter
from mailarchive.remote_mutation import ImapDeletionTarget


class ScriptedImap:
    def __init__(
        self,
        raw: bytes | None,
        *,
        uidvalidity: int = 7,
        uidplus: bool = True,
        deleted: bool = False,
    ) -> None:
        self.raw, self.uidvalidity, self.uidplus, self.deleted = raw, uidvalidity, uidplus, deleted
        self.commands: list[tuple[str, tuple[str, ...]]] = []
        self.store_result = "OK"
        self.expunge_result = "OK"
        self.store_raises = False
        self.expunge_raises = False

    def login(self, _user: str, _password: str) -> tuple[str, list[bytes]]:
        return "OK", [b"logged"]

    def capability(self) -> tuple[str, list[bytes]]:
        return "OK", [b"IMAP4rev1 UIDPLUS" if self.uidplus else b"IMAP4rev1"]

    def select(self, _mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.commands.append(("SELECT", (str(readonly),)))
        return "OK", [b"1"]

    def response(self, _code: str) -> tuple[str, list[bytes]]:
        return "OK", [str(self.uidvalidity).encode()]

    def uid(self, command: str, *args: str) -> tuple[str, list[object]]:
        self.commands.append((command, args))
        if command == "FETCH":
            if self.raw is None:
                return "OK", [None]
            flags = b"\\Deleted" if self.deleted else b""
            metadata = (
                f"1 (UID {args[0]} FLAGS ({flags.decode()}) BODY[] {{{len(self.raw)}}}".encode()
            )
            return "OK", [(metadata, self.raw), b")"]
        if command == "STORE":
            if self.store_raises:
                raise OSError("lost")
            if self.store_result == "OK":
                self.deleted = True
            return self.store_result, []
        if command == "EXPUNGE":
            if self.expunge_raises:
                raise OSError("lost")
            if self.expunge_result == "OK":
                self.raw = None
            return self.expunge_result, []
        raise AssertionError(command)

    def unselect(self) -> tuple[str, list[bytes]]:
        self.commands.append(("UNSELECT", ()))
        return "OK", []

    def logout(self) -> tuple[str, list[bytes]]:
        self.commands.append(("LOGOUT", ()))
        return "BYE", []


def _adapter(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, client: ScriptedImap
) -> tuple[ImapMutationAdapter, ImapDeletionTarget]:
    values = yaml.safe_load(config_file.read_text())
    values["accounts"]["test"]["imap"] = {
        "host": "127.0.0.1",
        "port": 1143,
        "username": "fixture",
        "tls_mode": "INSECURE_LOOPBACK",
        "folders": ["INBOX"],
    }
    config_file.write_text(yaml.safe_dump(values))
    monkeypatch.setenv("MAILARCHIVE_TEST_SECRET", "fixture-password")
    config = load_config(config_file)
    initialize(config.database.path, config.accounts)
    with connect(config.database.path) as db:
        aid = account_id(db, "test")
    assert aid is not None
    raw = client.raw or b"body"
    target = ImapDeletionTarget(
        "remote", "canonical", aid, "test", "imap", hashlib.sha256(raw).hexdigest(), "INBOX", 7, 9
    )
    return ImapMutationAdapter(
        config, "test", client_factory=lambda _account: cast(ImapClient, client)
    ), target


def _mutations(client: ScriptedImap) -> list[tuple[str, tuple[str, ...]]]:
    return [command for command in client.commands if command[0] in {"STORE", "EXPUNGE"}]


def _without_uidplus(client: ScriptedImap) -> None:
    client.uidplus = False


def _other_uidvalidity(client: ScriptedImap) -> None:
    client.uidvalidity = 8


def _other_bytes(client: ScriptedImap) -> None:
    client.raw = b"other"


def _already_deleted(client: ScriptedImap) -> None:
    client.deleted = True


def test_exact_uidplus_store_expunge_and_confirmation(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = ScriptedImap(b"target")
    adapter, target = _adapter(config_file, monkeypatch, client)
    result = adapter.delete(target)
    assert result.outcome == "success-confirmed" and result.confirmed_absent
    assert _mutations(client) == [
        ("STORE", ("9", "+FLAGS.SILENT", r"(\Deleted)")),
        ("EXPUNGE", ("9",)),
    ]
    assert all(command[0] != "CLOSE" for command in client.commands)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (_without_uidplus, "SAFE_DELETE_UNSUPPORTED"),
        (_other_uidvalidity, "IDENTITY_MISMATCH"),
        (_other_bytes, "IDENTITY_MISMATCH"),
        (_already_deleted, "REMOTE_STATE_CONFLICT"),
    ],
)
def test_pre_store_refusals_never_mutate(
    config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[ScriptedImap], None],
    code: str,
) -> None:
    client = ScriptedImap(b"target")
    adapter, target = _adapter(config_file, monkeypatch, client)
    mutate(client)
    result = adapter.delete(target)
    assert result.error_code == code and _mutations(client) == []


def test_already_absent_is_confirmed_without_mutation(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = ScriptedImap(None)
    adapter, target = _adapter(config_file, monkeypatch, client)
    result = adapter.delete(target)
    assert (
        result.outcome == "success-confirmed"
        and result.confirmed_absent
        and _mutations(client) == []
    )


def test_store_rejection_reobserves_clean_target(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = ScriptedImap(b"target")
    client.store_result = "NO"
    adapter, target = _adapter(config_file, monkeypatch, client)
    result = adapter.delete(target)
    assert result.error_code == "PROVIDER_REJECTED"
    assert _mutations(client) == [("STORE", ("9", "+FLAGS.SILENT", r"(\Deleted)"))]


@pytest.mark.parametrize(
    ("deleted", "expected"), [(False, "PROVIDER_REJECTED"), (True, "TRANSPORT_UNKNOWN")]
)
def test_uncertain_store_is_reobserved(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, deleted: bool, expected: str
) -> None:
    client = ScriptedImap(b"target")
    client.store_raises = True
    # Model provider state visible after a lost response.
    original = client.uid

    def uid(command: str, *args: str) -> tuple[str, list[object]]:
        if command == "STORE":
            client.deleted = deleted
        return original(command, *args)

    client.uid = uid  # type: ignore[method-assign]
    adapter, target = _adapter(config_file, monkeypatch, client)
    assert adapter.delete(target).error_code == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [("absent", None), ("clean", "PROVIDER_REJECTED"), ("deleted", "TRANSPORT_UNKNOWN")],
)
def test_uid_expunge_failure_is_observed_without_retry(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, state: str, expected: str | None
) -> None:
    client = ScriptedImap(b"target")
    client.expunge_raises = True
    original = client.uid

    def uid(command: str, *args: str) -> tuple[str, list[object]]:
        if command == "EXPUNGE":
            client.raw = None if state == "absent" else b"target"
            client.deleted = state == "deleted"
        return original(command, *args)

    client.uid = uid  # type: ignore[method-assign]
    adapter, target = _adapter(config_file, monkeypatch, client)
    result = adapter.delete(target)
    if expected is None:
        assert result.outcome == "success-confirmed" and result.confirmed_absent
    else:
        assert result.error_code == expected
    assert len([item for item in _mutations(client) if item[0] == "EXPUNGE"]) == 1


def test_acquisition_and_mutation_command_boundaries() -> None:
    root = Path(__file__).parents[1] / "src" / "mailarchive"
    acquisition = (root / "imap.py").read_text()
    mutation = (root / "imap_mutation.py").read_text()
    assert 'client.uid("STORE"' not in acquisition and 'client.uid("EXPUNGE"' not in acquisition
    assert ".expunge(" not in mutation and "client.expunge(" not in mutation
    assert '"STORE"' in mutation and '"EXPUNGE"' in mutation
