# Test Plan

## 1. Strategy

Safety-critical behavior must be test-first or accompanied by exhaustive tests.

Test layers:

1. unit tests;
2. filesystem/Maildir integration tests;
3. SQLite migration/invariant tests;
4. fake provider adapter tests;
5. local disposable IMAP integration tests;
6. external-tool adapter tests;
7. optional dedicated-provider integration tests;
8. restore/recovery tests.

Production accounts are never test fixtures.

## 2. Required early tests

### Configuration

- valid configuration loads;
- missing required values fail;
- invalid retention fails closed;
- deletion is disabled by default;
- secret values are not printed.

### M3 IMAP acquisition

- only one configured account/folder can be selected, read-only, using UID SEARCH and UID FETCH
  BODY.PEEK[]; mutating IMAP commands are absent;
- FETCH parsing rejects missing, mismatching, malformed, or ambiguous UID/literal responses;
- a disposable loopback IMAP integration environment seeds known raw RFC822 bytes, checks the
  server's UIDVALIDITY/UID, count, content, and flags after sync, then compares server and
  canonical bytes exactly;
- UID plus UIDVALIDITY creates a one-to-one remote-to-canonical link; duplicate or conflicting
  linkage fails closed without altering the server or canonical bytes.

### M5 Gmail acquisition

- use an injected/fake REST transport only, never a production account;
- assert all mailbox calls are GET and Gmail write methods are unavailable;
- verify RAW base64url byte fidelity, label-independent provider identity, full pagination,
  history replay/expiry, and failure-safe checkpoints.

### Ingest

- import one `.eml`;
- import same file twice;
- same Message-ID with different bytes;
- different Message-ID with same bytes;
- malformed MIME still preserved;
- hash stored correctly;
- atomic write behavior;
- interrupted import recovery.

### SQLite

- migrations are repeatable;
- foreign keys enabled;
- constraints protect invariants;
- concurrent read/write behavior is acceptable.

## 3. Spam tests

- HAM remains readable;
- SUSPECT enters quarantine;
- SPAM enters quarantine;
- manual override restores message;
- provider spam flag alone is not destructive;
- classification failure results in safe state;
- quarantine never silently expires into deletion without policy checks.

### M7 spam quarantine

- exact bytes enter staging before classification; HAM promotion alone sets `archived_at`;
- SUSPECT/SPAM and classifier failure remain exact-byte quarantine objects with NULL `archived_at`
  unless previously admitted HAM;
- overrides append history and preserve canonical ID/SHA/bytes; the latest manual override wins;
- Rspamd input is exact RFC822 bytes and malformed, skipped, unknown, timeout, and unavailable
  responses fail closed to SUSPECT; no response body is canonical content;
- index rebuild reapplies tags from SQLite, excludes quarantine by default, and permits explicit
  `tag:quarantine` searches; status and quarantine listing remain local-only.
- RFC Message-ID collisions with different canonical bytes must preserve both objects. Archive and
  quarantine have independent derived notmuch indexes so quarantine-only content cannot influence
  archive query candidates; SQLite still verifies every returned canonical row.
- a linked pending message can be reconciled locally without provider RAW/RETR/BODY.PEEK refetch;
  missing or hash-mismatched pending files fail closed and remain pending.

## 4. Deletion predicate tests

Before any remote deletion adapter exists, the eligibility policy SHALL have combinatorial coverage.

Positive case requires all conditions.

Each single failure must independently block eligibility:

- deletion disabled;
- no canonical file;
- hash mismatch;
- missing archived_at;
- retention not elapsed;
- zero verified backups;
- insufficient verified backups;
- keep_online;
- legal_hold;
- quarantined;
- integrity error;
- ambiguous remote identity.

Boundary tests:

- exactly one second before retention;
- exactly at retention;
- leap-year/date arithmetic;
- 365 vs 730-day accounts;
- never-delete account;
- verification invalidated after previous success.

Property-based tests are encouraged.

## 5. Fast-path tests

Use a fake event source or disposable IMAP server.

Verify:

- notification triggers only target INBOX sync;
- full sync is not required;
- notmuch update follows acquisition;
- Recoll/Borg failure does not block message visibility;
- duplicate notifications are safe;
- watcher reconnects;
- polling fallback recovers missed notification.

M4 additionally requires a genuine disposable Dovecot IDLE acceptance test proving an APPEND
produces an IDLE trigger, separate M3 acquisition, exact BODY.PEEK[] canonical bytes, unchanged
server flags/body, and notmuch visibility. Unit tests cover arm-before-sync, bounded IDLE lifecycle,
burst coalescing, polling, reconnection, watcher locking, stale local health and no-network status.

## 6. Backup tests

- successful backup run recorded;
- failed backup does not count;
- unverified backup does not count;
- verification failure revokes/blocks safety evidence;
- two records from the same logical repository do not accidentally count as two independent required copies if policy demands distinct repositories.

### M6 POP3 acquisition

- a disposable local POP3 server verifies UIDL/RETR byte preservation for CRLF, folded headers,
  multipart bodies, binary/base64 payloads, malformed messages, duplicate Message-ID, identical
  bytes, and no final newline;
- no DELE command is accepted or issued and a sync leaves the mailbox unchanged;
- UIDL idempotency skips already-linked bodies; a UIDL cannot be relinked to a different SHA;
- failed retrieval does not register a provider link, and a rerun safely completes partial local
  success;
- migration from M5 retains IMAP/Gmail identities, links and `fast_path_health`, with a clean
  `foreign_key_check`;
- `tests/test_getmail6_acceptance.py` invokes the real getmail 6.20.00 binary against a disposable
  server using explicit non-destructive settings. It proves every seeded fixture changes in staging
  (added `Return-Path`, LF reconstruction, and final-LF addition where applicable), proves DELE is
  absent, and proves the source mailbox remains unchanged. Reproduce with
  `uv run pytest tests/test_getmail6_acceptance.py -v`; the test cleanly skips if getmail is absent.

- direct POP3 tests decode fragmented multiline wire data line by line, including a first dot-leading
  line, internal single/double-dot lines, final CRLF, folded/multipart/base64 data, and terminator
  exclusion. Duplicate UIDLs fail before RETR, canonical ingest, provider identity, or link creation.

## 7. Integrity tests

- modified local `.eml` produces hash mismatch;
- missing file detected;
- corruption blocks deletion;
- audit event produced.

## 8. Remote-mutation tests

These tests belong to the late milestone.

Initially fake adapter only.

Verify:

- default command is non-destructive;
- `--dry-run` cannot invoke destructive provider method;
- execution requires account opt-in;
- rate limit applies;
- ambiguous identity aborts;
- partial provider failure is recorded;
- retry does not delete an unrelated message;
- crash recovery can determine prior outcome.

## 9. Recovery tests

Periodically prove:

- Maildir message readable without application database;
- notmuch index rebuild works;
- Recoll index rebuild works;
- SQLite restore works;
- Borg restore retrieves canonical messages;
- hashes validate after restore.
