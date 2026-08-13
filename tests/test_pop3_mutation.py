from __future__ import annotations

import hashlib
import socketserver
import threading
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from mailarchive.config import load_config
from mailarchive.db import account_id, connect, initialize
from mailarchive.pop3_mutation import Pop3MutationAdapter, Pop3MutationError
from mailarchive.remote_mutation import ProviderDeletionTarget


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "pop3.yaml"
    path.write_text(
        "\n".join(
            (
                "archive:",
                f"  root: {tmp_path / 'archive'}",
                "  timezone: UTC",
                "database:",
                f"  path: {tmp_path / 'state.db'}",
                "accounts:",
                "  pop:",
                "    kind: pop3",
                "    enabled: true",
                "    remote_retention_days: 365",
                "    remote_deletion_enabled: true",
                "    required_verified_backups: 1",
                "    config_ref: env:POP_TEST_PASSWORD",
                "    pop3:",
                "      host: 127.0.0.1",
                "      port: 11110",
                "      username: test",
                "      tls_mode: INSECURE_LOOPBACK",
                "      connection_timeout_seconds: 5",
            )
        )
    )
    return path


@dataclass
class FakeServer:
    messages: dict[str, bytes]
    mode: str = "success"
    dele_calls: list[int] | None = None
    quit_calls: int = 0
    abort_calls: int = 0
    sessions: int = 0

    def __post_init__(self) -> None:
        self.dele_calls = []


class FakeWire:
    def __init__(self, server: FakeServer) -> None:
        self.server = server
        self.pending: str | None = None

    def open(self) -> None:
        self.server.sessions += 1
        if self.server.mode.endswith("unobservable") and self.server.sessions > 1:
            raise OSError("unobservable")

    def authenticate(self, username: str, password: str) -> None:
        del username, password
        return None

    def uidls(self) -> dict[int, str]:
        if self.server.mode == "ambiguous":
            raise Pop3MutationError("ambiguous")
        return {number: uidl for number, uidl in enumerate(self.server.messages, start=7)}

    def retr(self, number: int) -> bytes:
        uidl = list(self.server.messages)[number - 7]
        return self.server.messages[uidl]

    def dele(self, number: int) -> None:
        assert self.server.dele_calls is not None
        self.server.dele_calls.append(number)
        self.pending = list(self.server.messages)[number - 7]
        if self.server.mode == "dele-rejected":
            self.pending = None
            raise Pop3MutationError("rejected")
        if self.server.mode == "dele-lost-absent":
            del self.server.messages[self.pending]
            self.pending = None
            raise OSError("lost")
        if self.server.mode in {"dele-lost-present", "dele-lost-unobservable"}:
            self.pending = None
            raise OSError("lost")

    def quit_and_commit(self) -> None:
        self.server.quit_calls += 1
        if self.pending is not None:
            del self.server.messages[self.pending]
            self.pending = None
        if self.server.mode in {"quit-lost-absent", "quit-lost-present", "quit-lost-unobservable"}:
            if self.server.mode == "quit-lost-present":
                self.server.messages["U1"] = b"target\r\n"
            raise OSError("lost")

    def abort_without_quit(self) -> None:
        self.server.abort_calls += 1
        self.pending = None


def _adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: FakeServer
) -> tuple[Pop3MutationAdapter, ProviderDeletionTarget]:
    config = load_config(_config(tmp_path))
    initialize(config.database.path, config.accounts)
    monkeypatch.setenv("POP_TEST_PASSWORD", "never-log")
    with connect(config.database.path) as db:
        aid = account_id(db, "pop")
    assert aid is not None
    raw = server.messages.get("U1", b"target\r\n")
    target = ProviderDeletionTarget(
        "remote", "canonical", aid, "pop", "pop3", hashlib.sha256(raw).hexdigest(), "U1"
    )
    adapter = Pop3MutationAdapter(config, "pop", wire_factory=lambda _settings: FakeWire(server))
    return adapter, target


@pytest.mark.parametrize(
    ("mode", "expected", "dele_count", "quit_count"),
    [
        ("success", "success-confirmed", 1, 1),
        ("dele-rejected", "failure-confirmed-no-mutation", 1, 0),
        ("dele-lost-present", "failure-confirmed-no-mutation", 1, 0),
        ("dele-lost-absent", "success-confirmed", 1, 0),
        ("quit-lost-absent", "success-confirmed", 1, 1),
        ("quit-lost-present", "failure-confirmed-no-mutation", 1, 1),
    ],
)
def test_pop3_mutation_exact_dele_quit_and_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected: str,
    dele_count: int,
    quit_count: int,
) -> None:
    server = FakeServer({"U1": b"target\r\n", "U2": b"other\r\n"}, mode)
    adapter, target = _adapter(tmp_path, monkeypatch, server)
    result = adapter.delete(target)
    assert result.outcome == expected
    assert server.dele_calls == [7] * dele_count
    assert server.quit_calls == quit_count
    if expected == "success-confirmed":
        assert "U1" not in server.messages and server.messages["U2"] == b"other\r\n"
    else:
        assert server.messages["U1"] == b"target\r\n"


def test_pop3_mutation_already_absent_hash_mismatch_and_ambiguous_uidl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    absent = FakeServer({"U2": b"other\r\n"})
    adapter, target = _adapter(tmp_path, monkeypatch, absent)
    assert adapter.delete(target).outcome == "success-confirmed"
    assert absent.dele_calls == [] and absent.quit_calls == 0
    mismatch = FakeServer({"U1": b"different\r\n"})
    adapter, target = _adapter(tmp_path, monkeypatch, mismatch)
    result = adapter.delete(replace(target, canonical_sha256="0" * 64))
    assert result.error_code == "IDENTITY_MISMATCH"
    assert mismatch.dele_calls == []
    ambiguous = FakeServer({"U1": b"target\r\n"}, "ambiguous")
    adapter, target = _adapter(tmp_path, monkeypatch, ambiguous)
    assert adapter.delete(target).error_code == "PROVIDER_REJECTED"
    assert ambiguous.dele_calls == []


def test_pop3_mutation_unobservable_outcomes_never_retry_dele(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeServer({"U1": b"target\r\n"}, "dele-lost-unobservable")
    adapter, target = _adapter(tmp_path, monkeypatch, server)
    result = adapter.delete(target)
    assert result.outcome == "outcome-unknown"
    assert server.dele_calls == [7] and server.quit_calls == 0


def test_loopback_pop3_dele_commits_only_on_explicit_quit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages = {"U1": b"target\r\n", "U2": b"unrelated\r\n"}
    commands: list[str] = []

    def multiline(raw: bytes) -> bytes:
        return b"".join(
            (b"." + line if line.startswith(b".") else line) + b"\r\n"
            for line in raw.removesuffix(b"\r\n").split(b"\r\n")
        ) + b".\r\n"

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            pending: str | None = None
            self.wfile.write(b"+OK test\r\n")
            while line := self.rfile.readline().decode("ascii").strip():
                commands.append(line.split()[0].upper())
                command, *arguments = line.split()
                if command == "UIDL":
                    self.wfile.write(b"+OK\r\n")
                    for number, uidl in enumerate(messages, start=1):
                        self.wfile.write(f"{number} {uidl}\r\n".encode())
                    self.wfile.write(b".\r\n")
                elif command == "RETR":
                    uidl = list(messages)[int(arguments[0]) - 1]
                    self.wfile.write(b"+OK\r\n" + multiline(messages[uidl]))
                elif command == "DELE":
                    pending = list(messages)[int(arguments[0]) - 1]
                    self.wfile.write(b"+OK\r\n")
                elif command == "QUIT":
                    if pending is not None:
                        del messages[pending]
                    self.wfile.write(b"+OK\r\n")
                    return
                else:
                    self.wfile.write(b"+OK\r\n")

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        path = _config(tmp_path)
        path.write_text(
            path.read_text().replace("port: 11110", f"port: {server.server_address[1]}")
        )
        config = load_config(path)
        initialize(config.database.path, config.accounts)
        monkeypatch.setenv("POP_TEST_PASSWORD", "local-only")
        with connect(config.database.path) as db:
            aid = account_id(db, "pop")
        assert aid is not None
        target = ProviderDeletionTarget(
            "target",
            "canonical",
            aid,
            "pop",
            "pop3",
            hashlib.sha256(messages["U1"]).hexdigest(),
            "U1",
        )
        result = Pop3MutationAdapter(config, "pop").delete(target)
        assert result.outcome == "success-confirmed"
        assert commands.count("DELE") == 1 and commands.count("QUIT") == 1
        assert "U1" not in messages and messages["U2"] == b"unrelated\r\n"
    finally:
        server.shutdown()
        server.server_close()
