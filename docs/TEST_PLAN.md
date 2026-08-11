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

- generated mbsync configuration is verified as pull-only (`Pull New`, `Create Near`, and no
  far-side remove/expunge operation);
- only one configured account/folder channel can be invoked, never `mbsync -a`;
- the persistent mbsync Maildir mirror is outside canonical `mail/`;
- a disposable loopback IMAP integration environment seeds known raw RFC822 bytes, checks the
  server's UIDVALIDITY/UID, count, content, and flags after sync, then compares server, mirror,
  and canonical bytes exactly;
- native `.mbsyncstate` parsing is tested against the installed mbsync version; malformed or
  incomplete mapping may not create a remote link but must not lose canonical mail.

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

## 6. Backup tests

- successful backup run recorded;
- failed backup does not count;
- unverified backup does not count;
- verification failure revokes/blocks safety evidence;
- two records from the same logical repository do not accidentally count as two independent required copies if policy demands distinct repositories.

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
