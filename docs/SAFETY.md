# Safety Model

## 1. Primary safety objective

No message stored on an origin server shall be destroyed because of an incomplete archive, unverified backup, uncertain identity, ambiguous provider state, software failure, or configuration default.

Safety takes precedence over freeing space.

## 2. Fail-closed principle

Any unknown, null, stale, contradictory or unverifiable condition SHALL prevent remote deletion.

Examples:

- missing hash -> not eligible;
- missing local file -> not eligible;
- backup state unknown -> not eligible;
- retention timestamp missing -> not eligible;
- quarantine state uncertain -> not eligible;
- account deletion switch disabled -> not eligible;
- UID mapping uncertain -> not eligible.

## 3. Deletion safety predicate

Remote deletion eligibility SHALL be computed by a pure or near-pure policy function that is separately testable.

Conceptual predicate:

```text
eligible =
    remote_deletion_enabled
    AND canonical_local_copy_exists
    AND canonical_hash_matches
    AND archived_at_is_known
    AND now >= archived_at + remote_retention
    AND verified_backup_count >= required_verified_backups
    AND NOT keep_online
    AND NOT legal_hold
    AND NOT quarantined
    AND NOT integrity_error
    AND remote_identity_is_unambiguous
```

Additional provider-specific conditions MAY make the predicate stricter.

No condition may make it weaker without a documented architecture decision and explicit operator approval.

## 4. Retention timestamp

`archived_at` is set only after the canonical local copy has been successfully admitted to the archive.

It SHALL NOT be backdated to the email's original Date header.

If a 2010 email is first archived on 2026-08-11 and retention is 365 days, it does not become eligible merely because the message itself is old.

## 5. Backup verification

A backup SHALL count toward deletion safety only when the system has evidence that:

- a relevant backup operation completed;
- the message/archive object falls within its protected scope;
- the repository has passed the configured verification policy.

The design SHOULD support at least two independently tracked verified backup destinations.

M9 binds each configured Borg name to its physical repository identity. It backs up only finalized
archived/quarantined canonical bytes, their referenced attachment blobs, and an online SQLite
snapshot. Pending objects and rebuildable indexes are excluded. Verification anchors a deterministic
manifest SHA-256, checks every regular archive file SHA-256, and runs Borg verify-data. Any later
failed re-verification revokes positive message evidence. Borg keys and passphrases are operator
recovery material and are never backed up into that same MailArchive dataset.

## 6. Separation of duties in CLI

Candidate computation and deletion execution SHALL be different commands and code paths.

Example:

```text
mailarchive deletion-candidates
mailarchive remote-delete --dry-run
mailarchive remote-delete --execute-plan RUN_ID --account ACCOUNT
```

M12 uses the exact `--execute-plan RUN_ID --account ACCOUNT` mechanism; there is no generic
`--execute`, force, retry, or resume option.

A default invocation MUST NOT delete.

### M10 candidate policy is not deletion authorization

M10 computes local candidate-policy eligibility only. Its `deletion-candidates` and retention
reports never contact a provider or Borg and always expose `execution_authorized=false`. Eligibility
is necessary but never by itself authorizes a provider mutation.

### M11 planning is not production execution authorization

M11/M12 dry-run requires `remote-delete --dry-run`, freshly reruns M10 from current local state,
and records an immutable local target snapshot. It makes no provider connection. A production
execution may consume exactly one completed, account-filtered source plan only once. It requires
per-account `remote_deletion_enabled: true`, expires after 60 minutes, repeats M10 safety and
target-fingerprint validation before setup and each call, and creates a separate production run.

The closed production factory selects only the configured IMAP, Gmail, or POP3 mutation capability.
Before every destructive call, the run has a committed `started` mutation and bounded audit event.
Execution is serial and halts on the first stale, failed, or unknown result; later planned rows
remain planned. There is no automatic retry or resume.

IMAP requires exact folder, UIDVALIDITY, UID, `BODY.PEEK[]` SHA-256, and UIDPLUS, then may use only
exact UID STORE `\\Deleted` and exact UID EXPUNGE. Global EXPUNGE and CLOSE are forbidden. Gmail
acquisition remains `gmail.readonly`; mutation uses a separate 0600 non-symlink credential outside
`archive.root` with only `https://mail.google.com/`, validates profile and exact Message.id RAW
SHA-256, sends at most one DELETE, and confirms with GET. POP3 treats UIDL as durable identity,
resolves the session-local message number freshly, RETRs and hashes it, sends at most one DELE, and
commits only with deliberate QUIT. POP3 acquisition, mutation, and observation share an account
lock.

If completion cannot be proven, the mutation is `unknown`, local remote presence is not cleared,
the run is halted, and the operator must use `remote-mutations reconcile --run-id RUN_ID`.
Reconciliation is read-only, observes the historical target, never resumes planned work, and can
resolve only confirmed absence or exact current presence. See `REMOTE_DELETION_RUNBOOK.md` for the
operator procedure.

## 7. Rate limiting and blast-radius control

For controlled remote deletion:

- per-run deletion limits SHALL exist;
- per-account limits SHOULD exist;
- initial production rollout SHALL use very small batches;
- bulk expunge SHOULD be avoided unless provider semantics require it and are well understood;
- failure SHALL stop or reduce further destructive actions.

Suggested rollout:

1. reports only;
2. dry-run;
3. test account;
4. one message;
5. a very small batch only after review.

## 8. Spam safety

Spam classification SHALL never be equivalent to immediate destruction.

A quarantined message SHALL remain:

- locally readable;
- searchable;
- recoverable during quarantine;
- protected from ordinary archive-deletion logic.

Manual false-positive recovery SHALL be supported.

## 9. Canonical immutability

The canonical RFC822/MIME object SHALL not be modified to:

- remove attachments;
- rewrite headers for normalization;
- rewrite Message-ID;
- collapse MIME structure;
- add application metadata.

Application metadata belongs in SQLite/notmuch tags/sidecars, not in the canonical bytes.

## 10. Integrity

SHA-256 SHALL be computed for canonical messages.

Integrity verification SHALL compare stored hashes with actual bytes.

An integrity failure SHALL:

- create an audit event;
- mark the object unsafe;
- prevent deletion;
- be visible in status/reporting.

## 11. Credentials

Credentials and OAuth tokens SHALL:

- not be stored in the repository;
- not appear in normal logs;
- use OS secret storage, restricted files, environment indirection or provider-supported token storage;
- be redacted in diagnostic output.

For M5 Gmail, OAuth token and client-secret files are absolute paths outside `archive.root`; token
files must be 0600 and no token material is stored in SQLite or audit data. The production OAuth
scope is `gmail.readonly` only.
Stored authorized-user JSON must explicitly prove that Gmail scope; missing or Gmail write-capable
scope metadata fails closed.

## 12. Testing destructive logic

Tests for remote deletion SHALL use:

- fake provider adapters;
- disposable local IMAP test servers;
- dedicated non-production test accounts only when an integration test is explicitly required.

Production mailboxes SHALL never be test fixtures.

## 13. Recovery assumption

Every destructive design decision must answer:

> If the process crashes immediately after this operation, can an operator determine exactly what happened and recover safely?

If the answer is no, the operation is not ready for production.
