"""Real-binary getmail6 POP3 byte-fidelity acceptance experiment.

Run manually with ``uv run pytest tests/test_getmail6_acceptance.py -v``.
This is deliberately an evaluation test: getmail 6.20.00 is expected to be
rejected because its POP3 path reconstructs message lines before Maildir
delivery. It must never become a production acquisition dependency.
"""

from __future__ import annotations

import hashlib
import shutil
import socketserver
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

GETMAIL = shutil.which("getmail")


@dataclass(frozen=True)
class Fixture:
    name: str
    uidl: str
    raw: bytes


FIXTURES = (
    Fixture(
        "normal-crlf",
        "UIDL-001",
        b"X-Test-Case: normal-crlf\r\nFrom: sender@example.test\r\nSubject: normal\r\n\r\nbody\r\n",
    ),
    Fixture(
        "folded-header",
        "UIDL-002",
        b"X-Test-Case: folded-header\r\nSubject: folded\r\n continuation\r\n\r\nbody\r\n",
    ),
    Fixture(
        "multipart",
        "UIDL-003",
        b"X-Test-Case: multipart\r\nContent-Type: multipart/mixed; boundary=abc\r\n\r\n"
        b"--abc\r\nContent-Type: text/plain\r\n\r\npart\r\n--abc--\r\n",
    ),
    Fixture(
        "base64-attachment",
        "UIDL-004",
        b"X-Test-Case: base64-attachment\r\nContent-Transfer-Encoding: base64\r\n\r\nAAEC/4A=\r\n",
    ),
    Fixture(
        "malformed-storable",
        "UIDL-005",
        b"X-Test-Case: malformed-storable\r\nMalformed header without colon\r\n\r\n"
        b"binary\x00body\r\n",
    ),
    Fixture(
        "no-final-newline",
        "UIDL-006",
        b"X-Test-Case: no-final-newline\r\n\r\nbody-without-final-newline",
    ),
    Fixture(
        "duplicate-message-id-a",
        "UIDL-007",
        b"X-Test-Case: duplicate-message-id-a\r\n"
        b"Message-ID: <duplicate@example.test>\r\n\r\nfirst\r\n",
    ),
    Fixture(
        "duplicate-message-id-b",
        "UIDL-008",
        b"X-Test-Case: duplicate-message-id-b\r\n"
        b"Message-ID: <duplicate@example.test>\r\n\r\nsecond\r\n",
    ),
    Fixture(
        "duplicate-bytes-a",
        "UIDL-009",
        b"X-Test-Case: duplicate-bytes\r\n"
        b"Message-ID: <identical@example.test>\r\n\r\nidentical\r\n",
    ),
    Fixture(
        "duplicate-bytes-b",
        "UIDL-010",
        b"X-Test-Case: duplicate-bytes\r\n"
        b"Message-ID: <identical@example.test>\r\n\r\nidentical\r\n",
    ),
    Fixture(
        "leading-dot",
        "UIDL-011",
        b"X-Test-Case: leading-dot\r\n\r\n.first\r\n..second\r\nthird\r\n",
    ),
)


def _wire_payload(raw: bytes) -> bytes:
    """Encode logical RFC822 bytes as POP3 multiline data with dot-stuffing."""
    lines = raw.split(b"\r\n")
    if raw.endswith(b"\r\n"):
        lines.pop()
    stuffed = [b"." + line if line.startswith(b".") else line for line in lines]
    return b"\r\n".join(stuffed) + b"\r\n.\r\n"


def _first_difference(left: bytes, right: bytes) -> int | None:
    for offset, (a, b) in enumerate(zip(left, right, strict=False)):
        if a != b:
            return offset
    return None if len(left) == len(right) else min(len(left), len(right))


def _context(data: bytes, offset: int | None) -> str:
    if offset is None:
        return "<identical>"
    return data[max(0, offset - 12) : offset + 12].hex(" ")


def _config(port: int, staging: Path) -> str:
    return f"""[retriever]
type = SimplePOP3Retriever
server = 127.0.0.1
port = {port}
username = acceptance-user
password = acceptance-password
timeout = 10

[destination]
type = Maildir
path = {staging}/

[options]
read_all = true
delete = false
delete_after = 0
delete_bigger_than = 0
delivered_to = false
received = false
mark_read = false
"""


@pytest.mark.skipif(GETMAIL is None, reason="getmail6 binary is unavailable")
def test_getmail_62000_real_pop3_byte_fidelity_experiment(tmp_path: Path) -> None:
    """Invoke actual getmail and preserve exact diagnostics for its acceptance decision."""
    commands: list[str] = []
    fixtures_by_number = {number: fixture for number, fixture in enumerate(FIXTURES, start=1)}

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            self.wfile.write(b"+OK MailArchive disposable POP3\r\n")
            while command := self.rfile.readline().decode("ascii").rstrip("\r\n"):
                verb = command.split(" ", 1)[0].upper()
                commands.append(verb)
                if verb == "CAPA":
                    self.wfile.write(b"+OK\r\nUIDL\r\n.\r\n")
                elif verb in {"USER", "PASS", "QUIT"}:
                    self.wfile.write(b"+OK\r\n")
                    if verb == "QUIT":
                        return
                elif verb == "UIDL":
                    self.wfile.write(b"+OK\r\n")
                    for number, fixture in fixtures_by_number.items():
                        self.wfile.write(f"{number} {fixture.uidl}\r\n".encode())
                    self.wfile.write(b".\r\n")
                elif verb == "LIST":
                    self.wfile.write(b"+OK\r\n")
                    for number, fixture in fixtures_by_number.items():
                        self.wfile.write(f"{number} {len(fixture.raw)}\r\n".encode())
                    self.wfile.write(b".\r\n")
                elif verb == "RETR":
                    fixture = fixtures_by_number[int(command.split()[1])]
                    self.wfile.write(b"+OK message follows\r\n" + _wire_payload(fixture.raw))
                elif verb == "DELE":
                    self.wfile.write(b"-ERR DELE forbidden\r\n")
                else:
                    self.wfile.write(b"-ERR unsupported command\r\n")

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    staging = tmp_path / "getmail-staging"
    state = tmp_path / "getmail-state"
    for directory in (staging / "cur", staging / "new", staging / "tmp", state):
        directory.mkdir(parents=True)
    rcfile = tmp_path / "getmailrc"
    rcfile.write_text(_config(int(server.server_address[1]), staging), encoding="utf-8")
    rcfile.chmod(0o600)
    try:
        command = [str(GETMAIL), "--getmaildir", str(state), "--rcfile", str(rcfile)]
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30)
    finally:
        server.shutdown()
        server.server_close()
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "DELE" not in commands
    assert set(commands) <= {"USER", "PASS", "UIDL", "LIST", "RETR", "QUIT"}
    assert len(fixtures_by_number) == len(FIXTURES)  # Server mailbox remained unchanged.
    delivered = list((staging / "new").iterdir())
    assert len(delivered) == len(FIXTURES)
    by_case = {
        next(
            line for line in path.read_bytes().splitlines() if line.startswith(b"X-Test-Case:")
        ): path.read_bytes()
        for path in delivered
    }
    results: list[str] = []
    for fixture in FIXTURES:
        marker_name = (
            "duplicate-bytes" if fixture.name.startswith("duplicate-bytes") else fixture.name
        )
        marker = b"X-Test-Case: " + marker_name.encode()
        actual = by_case[marker]
        offset = _first_difference(fixture.raw, actual)
        results.append(
            f"{fixture.name}: source={len(fixture.raw)}/{hashlib.sha256(fixture.raw).hexdigest()} "
            f"getmail={len(actual)}/{hashlib.sha256(actual).hexdigest()} offset={offset} "
            f"source_context={_context(fixture.raw, offset)} "
            f"actual_context={_context(actual, offset)}"
        )
        assert actual.startswith(b"Return-Path: <")
        assert b"\r\n" not in actual
    no_final = by_case[b"X-Test-Case: no-final-newline"]
    assert no_final.endswith(b"\n")
    leading_dot = by_case[b"X-Test-Case: leading-dot"]
    assert b"\n.first\n..second\n" in leading_dot
    # The source inspection and this real execution establish the intended rejection:
    # getmail creates Message(fromlines=...) and Maildir calls Message.flatten(), so it
    # normalizes POP3 CRLF data to native line endings and changes end-of-message framing.
    print(f"commands={commands}")
    print("\n".join(results))
    assert all(
        _first_difference(
            fixture.raw,
            by_case[
                b"X-Test-Case: "
                + (
                    "duplicate-bytes"
                    if fixture.name.startswith("duplicate-bytes")
                    else fixture.name
                ).encode()
            ],
        )
        is not None
        for fixture in FIXTURES
    ), "\n".join(results)
