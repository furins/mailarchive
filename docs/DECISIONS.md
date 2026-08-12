# Architecture Decisions

This file records decisions already approved for v1.

## ADR-001 — Maildir is canonical storage

Status: accepted.

Reason:
Open, inspectable, resilient individual-message files and compatible with notmuch and standard mail tooling.

## ADR-002 — Original messages are immutable

Status: accepted.

Attachments are not stripped from canonical messages. Application metadata is external.

## ADR-003 — SQLite stores operational inventory

Status: accepted.

SQLite stores account/provider mappings, hashes, classification, backup evidence, retention state and audit information.

## ADR-004 — notmuch is the primary email search index

Status: accepted.

Indexes are rebuildable and not canonical.

## ADR-005 — Recoll indexes attachment contents

Status: accepted.

It complements notmuch rather than replacing it.

## ADR-006 — Borg provides deduplicated backup

Status: accepted.

At least two verified backup copies are recommended before ordinary mail can become deletion-eligible.

## ADR-007 — Remote retention starts at archive admission

Status: accepted.

Default: 365 days from `archived_at`, configurable per account. 730 days and never-delete are supported policies.

## ADR-008 — Fast path is separate from slow path

Status: accepted.

Incoming mail visibility must not wait for full archival processing.

## ADR-009 — IMAP IDLE plus fallback polling

Status: accepted in principle.

Use event-driven targeted INBOX acquisition and a 1–2 minute fallback poll.

## ADR-010 — Spam is quarantined, not immediately discarded

Status: accepted.

HAM/SUSPECT/SPAM states; manual recovery; separate quarantine retention.

## ADR-011 — Deduplication is non-destructive

Status: accepted.

Byte identity uses SHA-256. Message-ID alone is insufficient for destructive deduplication.

## ADR-012 — Remote deletion is a late capability

Status: accepted.

It is disabled by default, separate from candidate reporting, dry-run capable, auditable and rate limited.

## ADR-013 — Gmail requires provider-aware semantics

Status: accepted.

Gmail labels must not be naively equated with independent canonical folders/messages.

## ADR-014 — POP3 is fallback only

Status: accepted.

Prefer IMAP where available.

## ADR-015 — Direct IMAP literals are canonical acquisition bytes

Status: accepted.

The M3 Dovecot loopback fidelity test rejected isync/mbsync as a canonical acquisition
path: isync 1.4.4 converted CRLF to LF and injected `X-TUID` into its Maildir copy.
MailArchive now fetches each new UID with read-only `BODY.PEEK[]`; the returned literal is
written unchanged to canonical Maildir. mbsync is not a production dependency.

## ADR-016 — Gmail REST API is the M5 provider path

Status: accepted.

Gmail M5 uses Gmail REST API v1 only, with `gmail.readonly` and explicit GET-only methods for
profile, labels, messages, and history. Gmail `Message.id` is the provider-global remote
identity; RFC Message-ID, threadId, labels, and label names are not identity. A message with many
labels has one remote identity and may have one canonical byte object and many label relationships.
Canonical acquisition uses `messages.get(format=RAW)`, strict base64url decoding, and M1 byte
ingest without rewriting bytes. Gmail IMAP, Pub/Sub, users.watch, and all Gmail mailbox mutation
are deliberately out of scope. History polling defaults to 90 seconds; expired history requires a
safe full sync. Token and OAuth client-secret files must remain outside `archive.root`. SPAM and
TRASH are inventoried as provider labels only; M7 owns local spam/quarantine policy.

## ADR-017 — M6 POP3 uses direct controlled retrieval; getmail6 is rejected

Status: accepted.

The real `/home/stefano/.local/bin/getmail` binary, getmail 6.20.00, was tested against a
disposable loopback POP3 server by `uv run pytest tests/test_getmail6_acceptance.py -v`. The test
generates a 0600 temporary rcfile and invokes:

```text
/home/stefano/.local/bin/getmail --getmaildir <temporary-state> --rcfile <temporary-rcfile>
```

Its exact rcfile is:

```ini
[retriever]
type = SimplePOP3Retriever
server = 127.0.0.1
port = <temporary loopback port>
username = acceptance-user
password = acceptance-password
timeout = 10

[destination]
type = Maildir
path = <temporary staging Maildir>/

[options]
read_all = true
delete = false
delete_after = 0
delete_bigger_than = 0
delivered_to = false
received = false
mark_read = false
```

All eleven fixtures differed: normal CRLF, folded headers, multipart MIME, base64 attachment,
malformed-but-storable bytes, no final newline, two distinct messages sharing RFC Message-ID,
two UIDLs with identical bytes, and leading-dot body lines. Every delivered file began with the
added `Return-Path: <unknown>` header (first difference offset 0); getmail also reconstructed POP3 lines
with native LF line endings, and the no-final-newline fixture gained a final LF. Its Maildir
destination therefore cannot preserve canonical RFC822 bytes even with Delivered-To and Received
disabled. The server observed only USER, PASS, UIDL, LIST, RETR, and QUIT; DELE was absent and all
eleven mailbox entries remained present.

`mailarchive pop3 sync` consequently remains the production direct UIDL/RETR adapter. This is an
intentional roadmap deviation: canonical RFC822 byte immutability takes priority over the original
getmail6 preference. SQLite is authoritative for POP3 provider identity; any getmail oldmail file
would be subordinate, rebuildable state only. getmail6 is not a runtime dependency and never writes
MailArchive canonical storage.
