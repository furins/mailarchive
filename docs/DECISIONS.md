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
