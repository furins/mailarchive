# Requirements

## 1. Goals

MailArchive SHALL provide a local archival system for multiple email accounts while preserving the ability to:

- read messages and attachments without access to the origin provider;
- search email metadata and bodies from CLI;
- search inside common attachment formats;
- maintain original message fidelity;
- identify duplicates safely;
- maintain at least partial and preferably redundant backups;
- free remote-server space only after conservative retention and verification;
- quarantine unwanted spam that escaped provider filters;
- receive newly arrived email locally with low latency.

## 2. Supported source classes

The system SHALL support:

1. Standard IMAP accounts.
2. Gmail accounts.
3. POP3 accounts only where IMAP is unavailable.

The account abstraction SHALL preserve provider-specific behavior when necessary rather than forcing all providers into an inaccurate common model.

## 3. Canonical message storage

The canonical local message representation SHALL be individual RFC822/MIME files in Maildir-compatible storage.

The canonical archived copy SHALL:

- preserve downloaded bytes;
- not be rewritten to remove attachments;
- not be modified for deduplication;
- remain readable by standard email tools;
- be independently hashable.

Indexes and databases SHALL be reconstructable from canonical data wherever feasible.

## 4. Acquisition

Acquisition SHALL be idempotent.

For each acquired message the system SHALL record, when available:

- source account;
- source folder/label context;
- remote UID or provider identifier;
- Message-ID;
- local canonical path;
- SHA-256;
- download time;
- archive admission time;
- original message date;
- size;
- acquisition adapter/version metadata.

Normal acquisition SHALL NOT imply remote deletion.

## 5. Fast path

New incoming mail SHALL be made locally readable with minimal delay.

The fast path SHALL include only what is necessary for immediate availability:

1. notification or short fallback poll;
2. INBOX acquisition;
3. local canonical/staging write;
4. minimal database registration;
5. `notmuch new`.

The fast path SHALL NOT block on:

- full mailbox synchronization;
- Recoll indexing;
- attachment extraction;
- deduplication;
- Borg backup;
- retention processing;
- full spam analysis.

Target service objective after source-server availability:

- preferred: tens of seconds;
- fallback polling: no more than approximately 1–2 minutes under normal operation.

This is an operational target, not a guarantee about third-party provider delivery.

## 6. Slow path

Slow-path workers MAY perform:

- complete mailbox/folder synchronization;
- provider label reconciliation;
- spam classification and reclassification;
- attachment extraction;
- attachment hashing;
- Recoll indexing;
- deduplication analysis;
- backup;
- backup verification;
- integrity audits;
- retention eligibility calculations;
- reporting.

Slow-path failure MUST NOT stop fast-path acquisition.

## 7. Search

### 7.1 Email search

notmuch SHALL be the primary email index/search interface.

The system SHALL expose or document searches for at least:

- sender/recipient;
- subject;
- full-text message body;
- date range;
- folder/account;
- tags;
- attachment filename/MIME type;
- Message-ID;
- thread.

### 7.2 Attachment-content search

Recoll SHALL be used to index searchable content of supported attachment types.

The canonical MIME message SHALL remain unchanged.

## 8. Spam and quarantine

Messages SHALL support at least these classifications:

- HAM
- SUSPECT
- SPAM

Classification SHALL be non-destructive.

Provider spam metadata MAY influence classification but SHALL NOT be the sole irreversible decision.

Local spam classification SHOULD consider:

- provider spam folder/label;
- provider spam headers;
- local classifier score;
- prior correspondence;
- known contacts;
- manual overrides.

Messages classified as SUSPECT or SPAM SHALL be placed in or associated with a quarantine domain.

Quarantine SHALL remain searchable.

Manual overrides SHALL be supported and auditable.

## 9. Deduplication

Deduplication SHALL distinguish:

1. provider presentation duplicates, such as Gmail label views;
2. byte-identical messages;
3. logically identical messages with differing bytes;
4. duplicate attachments.

The system SHALL NOT delete canonical messages merely because Message-ID values match.

SHA-256 byte identity MAY be used as a safe duplicate signal.

Attachments MAY be exported to a content-addressable store keyed by SHA-256, but removal of attachments from canonical email messages is forbidden.

## 10. Backup

The system SHALL support Borg repositories.

Backup policy SHALL support:

- multiple repositories;
- local/NAS backup;
- remote or offline backup;
- retention;
- verification evidence;
- restore testing.

A backup existing is not equivalent to a backup being verified.

Verification events SHALL be recorded.

## 11. Remote retention

Default remote retention for ordinary archived mail SHALL be 365 days from `archived_at`.

The retention clock MUST NOT use the email Date header.

Per-account configuration SHALL permit values such as:

- 365 days;
- 730 days;
- custom duration;
- never delete.

Messages MAY be protected with `keep_online`.

`legal_hold` is a canonical-level retention control and SHALL block ordinary remote deletion.

## 12. Remote deletion

Remote deletion is a late-stage optional feature.

A message SHALL be deletion-eligible only when all configured safety conditions hold.

Deletion SHALL be:

- disabled by default;
- separately enabled per account;
- dry-run capable;
- rate limited;
- auditable;
- no automatic retry or resume after a destructive attempt;
- conservative on ambiguity.

The operator SHALL be able to produce deletion-candidate reports without performing any remote mutation.

## 13. Quarantine retention

Quarantine SHALL use a separate policy from permanent archival.

Initial recommended defaults:

- SUSPECT: retain locally for 365 days;
- SPAM: retain locally for 180–365 days;
- remote deletion from spam folders: separately configurable;
- quarantine backup: shorter retention is permitted.

No quarantined message may enter the ordinary remote-deletion workflow until its state is explicitly safe.

## 14. Auditability

The system SHALL record significant events including:

- acquisition;
- hash verification;
- classification;
- manual reclassification;
- archival admission;
- backup completion;
- backup verification;
- retention eligibility;
- deletion-candidate evaluation;
- remote mutation attempts;
- remote mutation result;
- integrity failure.

Audit events SHALL include timestamp, actor/process, object, action and result.

## 15. CLI

The CLI SHOULD eventually include commands similar to:

```text
mailarchive account list
mailarchive sync <account>
mailarchive sync --fast <account>
mailarchive ingest <path>
mailarchive search ...
mailarchive status
mailarchive quarantine list
mailarchive quarantine restore <id>
mailarchive backup run
mailarchive backup verify
mailarchive integrity verify
mailarchive deletion-candidates
mailarchive remote-delete --dry-run
mailarchive remote-delete --execute-plan RUN_ID --account ACCOUNT
mailarchive remote-mutations reconcile --run-id PRODUCTION_RUN_ID
```

Names may evolve, but destructive and reporting commands MUST remain distinct.

## 16. Configuration

Configuration SHALL be declarative, validated and fail closed.

Secrets SHALL be referenced, not embedded in example configuration.

Example policy:

```yaml
accounts:
  personal:
    kind: imap
    remote_retention_days: 365
    remote_deletion_enabled: false

  work:
    kind: imap
    remote_retention_days: 730
    remote_deletion_enabled: false

spam:
  enabled: true
  suspect_quarantine_days: 365
  spam_quarantine_days: 180

backup:
  required_verified_copies: 2
```

## 17. Portability and recovery

The archive SHALL remain useful even if MailArchive itself is unavailable.

A recovery operator SHOULD be able to:

- read canonical `.eml`/Maildir messages;
- rebuild notmuch;
- rebuild Recoll;
- restore SQLite from backup or reconstruct key inventory fields where feasible;
- verify SHA-256 values;
- restore Borg data with standard Borg tooling.
