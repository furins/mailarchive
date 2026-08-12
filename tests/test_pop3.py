from __future__ import annotations

import socketserver
import threading
from pathlib import Path

import pytest

from mailarchive.config import load_config
from mailarchive.db import connect
from mailarchive.pop3 import Pop3Adapter, Pop3Error, register_pop3_link


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
                "    remote_deletion_enabled: false",
                "    required_verified_backups: 1",
                "    config_ref: env:POP_TEST_PASSWORD",
                "    pop3:",
                "      host: 127.0.0.1",
                "      port: 11110",
                "      username: test",
                "      tls_mode: INSECURE_LOOPBACK",
                "      connection_timeout_seconds: 5",
                "",
            )
        )
    )
    return path


class FakeWire:
    messages = {
        1: ("U1", b"Message-ID: <same>\r\n\r\nA"),
        2: ("U2", b"Message-ID: <same>\r\n\r\nA"),
    }
    commands: list[str] = []

    def __init__(self, _settings: object) -> None:
        self.commands = []
        type(self).commands = self.commands

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def command(self, command: str) -> bytes:
        self.commands.append(command.split(" ", 1)[0])
        return b""

    def uidls(self) -> dict[int, str]:
        return {number: value[0] for number, value in self.messages.items()}

    def retr(self, number: int) -> bytes:
        return self.messages[number][1]


def test_direct_loopback_pop3_preserves_raw_octets_and_issues_no_dele(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disposable server exercises real POP3 commands, not a mocked adapter."""
    messages = {
        1: ("U1", b"Subject: folded\r\n value\r\n\r\n--x\r\nAAEC\r\n--x--"),
        2: ("U2", b"Message-ID: <duplicate>\r\n\r\nmalformed\x00bytes"),
        3: ("U3", b"Message-ID: <duplicate>\r\n\r\nno-final-newline"),
    }
    commands: list[str] = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            self.wfile.write(b"+OK test\r\n")
            while line := self.rfile.readline().decode("ascii").strip():
                commands.append(line.split()[0].upper())
                if line.startswith("UIDL"):
                    self.wfile.write(b"+OK\r\n")
                    for number, (uidl, _raw) in messages.items():
                        self.wfile.write(f"{number} {uidl}\r\n".encode())
                    self.wfile.write(b".\r\n")
                elif line.startswith("RETR "):
                    number = int(line.split()[1])
                    self.wfile.write(b"+OK\r\n" + messages[number][1] + b"\r\n.\r\n")
                else:
                    self.wfile.write(b"+OK\r\n")
                if line.startswith("QUIT"):
                    return

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config_path = _config(tmp_path)
        config_path.write_text(
            config_path.read_text().replace("port: 11110", f"port: {server.server_address[1]}")
        )
        config = load_config(config_path)
        monkeypatch.setenv("POP_TEST_PASSWORD", "local-only")
        result = Pop3Adapter(config).sync("pop")
        assert result.seen == len(messages)
        with connect(config.database.path) as db:
            paths = [
                Path(row[0]) for row in db.execute("SELECT local_path FROM canonical_messages")
            ]
        assert {path.read_bytes() for path in paths} == {raw for _uidl, raw in messages.values()}
        assert "DELE" not in commands
        assert len(messages) == 3  # Retrieval did not alter disposable mailbox state.
    finally:
        server.shutdown()
        server.server_close()


def test_pop3_uidl_is_idempotent_and_never_deletes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(_config(tmp_path))
    FakeWire.messages = {
        1: ("U1", b"Message-ID: <same>\r\n\r\nA"),
        2: ("U2", b"Message-ID: <same>\r\n\r\nA"),
    }
    monkeypatch.setenv("POP_TEST_PASSWORD", "not-in-output")
    monkeypatch.setattr("mailarchive.pop3._Pop3Wire", FakeWire)
    first = Pop3Adapter(config).sync("pop")
    second = Pop3Adapter(config).sync("pop")
    assert (first.seen, first.imported, first.reused) == (2, 1, 1)
    assert (second.seen, second.imported, second.reused) == (2, 0, 0)
    assert "DELE" not in FakeWire.commands
    with connect(config.database.path) as db:
        assert db.execute("SELECT COUNT(*) FROM canonical_messages").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT COUNT(*) FROM remote_messages WHERE provider_kind='pop3'"
            ).fetchone()[0]
            == 2
        )
        assert db.execute("SELECT COUNT(*) FROM remote_canonical_links").fetchone()[0] == 2


def test_pop3_uidl_cannot_remap_to_another_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(_config(tmp_path))
    FakeWire.messages = {1: ("U1", b"Message-ID: <same>\r\n\r\nA")}
    monkeypatch.setenv("POP_TEST_PASSWORD", "safe")
    monkeypatch.setattr("mailarchive.pop3._Pop3Wire", FakeWire)
    Pop3Adapter(config).sync("pop")
    FakeWire.messages = {1: ("U1", b"Message-ID: <same>\r\n\r\nchanged")}
    # A known UIDL skips retrieval; attempting an explicit incompatible registration fails closed.
    from mailarchive.ingest import ingest_bytes

    other = ingest_bytes(config, b"Message-ID: <same>\r\n\r\nchanged", "pop", source_kind="test")
    with pytest.raises(Pop3Error, match="conflicts"):
        register_pop3_link(config, "pop", "U1", other.canonical_message)


def test_pop3_password_is_not_in_error_or_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(_config(tmp_path))
    FakeWire.messages = {1: ("U1", b"Message-ID: <same>\r\n\r\nA")}
    secret = "do-not-log-this-password"
    monkeypatch.setenv("POP_TEST_PASSWORD", secret)
    monkeypatch.setattr("mailarchive.pop3._Pop3Wire", FakeWire)
    Pop3Adapter(config).sync("pop")
    assert secret not in str(config.database.path.read_bytes())


def test_pop3_config_requires_uidl_capable_explicit_configuration(tmp_path: Path) -> None:
    text = _config(tmp_path).read_text().replace("INSECURE_LOOPBACK", "INSECURE")
    (tmp_path / "bad.yaml").write_text(text)
    with pytest.raises(ValueError, match="tls_mode"):
        load_config(tmp_path / "bad.yaml")
