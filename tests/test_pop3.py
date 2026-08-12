from __future__ import annotations

# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false, reportArgumentType=false
import socketserver
import threading
from pathlib import Path

import pytest

from mailarchive.config import load_config
from mailarchive.db import connect
from mailarchive.ingest import ingest_bytes
from mailarchive.pop3 import Pop3Adapter, Pop3Error, Pop3Wire, register_pop3_link


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


def _pop3_wire_data(logical_retr_bytes: bytes) -> bytes:
    """Encode logical RETR bytes into multiline POP3 wire data plus terminator."""
    assert logical_retr_bytes.endswith(b"\r\n")
    lines = logical_retr_bytes[:-2].split(b"\r\n")
    return (
        b"".join((b"." + line if line.startswith(b".") else line) + b"\r\n" for line in lines)
        + b".\r\n"
    )


class FragmentedSocket:
    def __init__(self, fragments: list[bytes]) -> None:
        self.fragments = fragments
        self.sent: list[bytes] = []

    def recv(self, _size: int) -> bytes:
        return self.fragments.pop(0) if self.fragments else b""

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)


@pytest.mark.parametrize(
    ("logical", "fragments"),
    [
        (b".first\r\n", [b"+OK\r", b"\n..", b"first\r", b"\n.\r", b"\n"]),
        (
            b"one\r\n.second\r\n..third\r\n",
            [b"+OK\r\n", b"one\r\n..second\r", b"\n...third\r\n.\r\n"],
        ),
        (b"body\r\n", [b"+OK\r\nbody\r\n.\r\n"]),
        (
            b"Subject: folded\r\n value\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n"
            b"--x\r\nContent-Transfer-Encoding: base64\r\n\r\nAAEC/4A=\r\n--x--\r\n",
            [
                b"+OK\r\nSubject: folded\r\n value\r\n"
                b"Content-Type: multipart/mixed; boundary=x\r\n",
                b"\r\n--x\r\nContent-Transfer-Encoding: base64\r\n\r\nAAEC/4A=\r\n--x--\r\n.\r\n",
            ],
        ),
    ],
    ids=["first-dot-fragmented", "internal-dots", "final-crlf", "mime-base64"],
)
def test_pop3_multiline_decodes_exact_logical_retr_bytes(
    tmp_path: Path, logical: bytes, fragments: list[bytes]
) -> None:
    config = load_config(_config(tmp_path))
    socket = FragmentedSocket(fragments)
    wire = Pop3Wire(config.accounts[0].pop3)  # type: ignore[arg-type]
    wire.sock = socket  # type: ignore[assignment]
    assert wire.multiline("RETR 1") == logical
    assert socket.sent == [b"RETR 1\r\n"]


def test_direct_loopback_pop3_preserves_raw_octets_and_issues_no_dele(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disposable server exercises real POP3 commands, not a mocked adapter."""
    messages = {
        1: ("U1", b"Subject: folded\r\n value\r\n\r\n.first\r\n"),
        2: ("U2", b"Message-ID: <duplicate>\r\n\r\nmalformed\x00bytes\r\n"),
        3: ("U3", b"Message-ID: <duplicate>\r\n\r\n--x\r\nAAEC/4A=\r\n--x--\r\n"),
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
                    self.wfile.write(b"+OK\r\n" + _pop3_wire_data(messages[number][1]))
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


def test_linked_pending_is_recovered_without_retr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(_config(tmp_path))
    raw = b"Message-ID: <pending@example.test>\r\n\r\nlocal\r\n"
    pending = ingest_bytes(config, raw, "pop")
    register_pop3_link(config, "pop", "U1", pending.canonical_message)
    calls: list[str] = []

    class KnownWire(FakeWire):
        messages = {1: ("U1", raw)}

        def retr(self, number: int) -> bytes:
            calls.append(f"RETR {number}")
            return super().retr(number)

    monkeypatch.setenv("POP_TEST_PASSWORD", "safe")
    monkeypatch.setattr("mailarchive.pop3._Pop3Wire", KnownWire)
    from mailarchive.classification import ClassificationResult
    from mailarchive.classification import reconcile_pending as real_reconcile

    class Ham:
        def classify(self, _raw: bytes) -> ClassificationResult:
            return ClassificationResult("ham", 0.0, "test-ham")

    monkeypatch.setattr(
        "mailarchive.classification.reconcile_pending",
        lambda cfg, **kwargs: real_reconcile(cfg, adapter=Ham(), **kwargs),
    )
    Pop3Adapter(config).sync("pop")
    assert calls == [] and "DELE" not in KnownWire.commands
    with connect(config.database.path) as db:
        row = db.execute(
            "SELECT storage_state,sha256,local_path FROM canonical_messages"
        ).fetchone()
        assert row is not None and row[0] == "archived"
        assert row[1] == pending.canonical_message.sha256
        assert Path(str(row[2])).read_bytes() == raw
        assert db.execute("SELECT COUNT(*) FROM remote_messages").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM remote_canonical_links").fetchone()[0] == 1


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


def test_duplicate_uidls_fail_closed_before_retrieval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(_config(tmp_path))
    FakeWire.messages = {
        1: ("UID-X", b"Message-ID: <a>\r\n\r\nfirst\r\n"),
        2: ("UID-X", b"Message-ID: <b>\r\n\r\nsecond\r\n"),
    }
    monkeypatch.setenv("POP_TEST_PASSWORD", "safe")
    monkeypatch.setattr("mailarchive.pop3._Pop3Wire", FakeWire)
    with pytest.raises(Pop3Error, match="ambiguous"):
        Pop3Adapter(config).sync("pop")
    assert "RETR" not in FakeWire.commands
    with connect(config.database.path) as db:
        assert db.execute("SELECT COUNT(*) FROM canonical_messages").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM remote_messages").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM remote_canonical_links").fetchone()[0] == 0


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
