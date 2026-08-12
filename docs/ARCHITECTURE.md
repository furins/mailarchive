# Architecture

## 1. Overview

MailArchive is split into a low-latency fast path and an independent slow path.

```text
                         FAST PATH

     IMAP/Gmail
         |
   IDLE/notification
         |
         v
   provider watcher
         |
         v
    INBOX acquire
         |
         v
 staging/canonical Maildir
         |
         +----> SQLite minimal registration
         |
         v
     notmuch new
         |
         v
   locally readable


                         SLOW PATH

 canonical/staging Maildir
         |
         +--> full mailbox reconciliation
         +--> spam classification
         +--> quarantine handling
         +--> attachment extraction
         +--> SHA-256/catalog
         +--> Recoll
         +--> dedup analysis
         +--> Borg backup
         +--> backup verification
         +--> integrity checks
         +--> retention evaluation
         +--> candidate reports
         +--> eventual remote deletion
```

## 2. Components

### 2.1 Core orchestrator

Python package `mailarchive`.

Responsibilities:

- configuration;
- SQLite state;
- audit log;
- subprocess adapters;
- policy engine;
- CLI;
- scheduling hooks;
- state transitions.

### 2.2 Provider adapters

Provider-specific adapters SHALL isolate protocol/tool behavior.

Initial conceptual adapters:

```text
ImapAdapter
GmailAdapter
Pop3GetmailAdapter
```

The Gmail implementation SHALL be validated during its milestone. Gmail labels must not be naively treated as independent canonical messages.

### 2.3 Search adapters

```text
NotmuchAdapter
RecollAdapter
```

notmuch is authoritative only for its index/tag domain, not for canonical message bytes.

Recoll indexes document contents but is reconstructable.

### 2.4 Spam adapter

```text
SpamClassifierAdapter
```

Possible implementation backends include rspamd or another suitable classifier.

The core system consumes normalized results:

```text
classification
score
reason
classifier
classifier_version
```

### 2.5 Backup adapter

```text
BorgAdapter
```

Responsibilities include:

- backup invocation;
- exit-status capture;
- backup metadata;
- verification result;
- protected-scope evidence.

### 2.6 Filesystem layout

Recommended:

```text
/srv/mailarchive/
├── mail/
│   ├── <account>/
│   └── ...
├── staging/
│   ├── <account>/
│   └── ...
├── quarantine/
│   ├── <account>/
│   └── ...
├── attachments/
│   └── sha256/
├── state/
│   ├── mailarchive.sqlite3
│   ├── notmuch/
│   │   ├── config
│   │   ├── db/
│   │   └── hooks/
│   └── locks/
├── metadata/
│   ├── exports/
│   └── reports/
└── logs/
```

Exact system paths SHALL be configurable.

For M3 IMAP, the adapter selects one explicitly configured remote folder read-only, uses UID
SEARCH and UID FETCH with BODY.PEEK[], and sends the returned literal directly to canonical
ingest. There is no Maildir mirror or mbsync state. Each requested folder has one process lock.
The adapter never issues a mutating IMAP command; UID plus UIDVALIDITY is the remote identity.
ADR-015 records why mbsync was rejected: its Maildir output did not preserve IMAP literal bytes.

notmuch configuration and database files are derived state and live outside `mail/`.
MailArchive always supplies its managed configuration explicitly; it disables Maildir flag
synchronization and indexing decryption. `notmuch new` runs with hooks disabled. notmuch
file search results are resolved through `canonical_messages.local_path`; its Message-ID
grouping and tags are not canonical identity, account ownership, integrity, or retention
state.

## 3. Message lifecycle

Conceptual states:

```text
DISCOVERED
    |
    v
DOWNLOADED
    |
    v
HASHED
    |
    +--------> QUARANTINED
    |
    v
ARCHIVED
    |
    v
BACKED_UP
    |
    v
BACKUP_VERIFIED
    |
    v
RETENTION_WAIT
    |
    v
DELETION_CANDIDATE
    |
    v
REMOTE_DELETED
```

States need not be represented as one enum if independent facts are safer. The data model should prefer explicit facts over a fragile single state machine where appropriate.

## 4. Fast path design

### IMAP

Preferred event path:

```text
IMAP IDLE watcher
    -> trigger account INBOX sync
    -> register new local files
    -> notmuch new
```

Fallback:

```text
short periodic INBOX poll, approximately every 1–2 minutes
```

M3 has no watcher or automatic IMAP polling; every network acquisition is an explicit command.

M4 adds `mailarchive imap watch` for ordinary IMAP INBOX only. Python 3.14 stdlib
`imaplib.idle()` runs on a dedicated, read-only notification connection; it never downloads a
message body. The already-existing M3 adapter uses a separate connection for canonical
`UID FETCH BODY.PEEK[]` acquisition. IDLE is armed before each catch-up sync, uses 30-second
bounded windows and 0.1-second burst coalescing, then re-arms. A 600-second reconciliation and
90-second polling fallback preserve correctness when notification is unavailable. The watcher has
its own lifetime lock and SQLite heartbeat health; notmuch failure is degraded local indexing and
is retried without refetching. Slow-path workers remain independent.

### Gmail

The implementation milestone SHALL choose between a Gmail API aware path and a carefully configured Gmail IMAP path based on current operational validation.

Required property:

- labels must not cause unsafe canonical duplication or ambiguous deletion mapping.

## 5. Slow-path scheduling

Initial scheduling proposal:

- fast INBOX: event-driven;
- fallback INBOX poll: 1–2 minutes;
- full folder sync: 10–15 minutes;
- spam/refinement: 10–30 minutes;
- Recoll: 15–30 minutes;
- attachment catalog: hourly or incremental;
- Borg: multiple times daily or daily, configurable;
- integrity: daily incremental + periodic deeper run;
- retention evaluation: daily;
- remote deletion: separately scheduled and disabled by default.

## 6. Concurrency

Concurrency controls SHALL prevent two workers from:

- importing the same filesystem object inconsistently;
- updating the same provider UID mapping unsafely;
- running destructive operations concurrently for the same account.

SQLite transactions and process locks MAY be combined.

Long external processes MUST NOT hold unnecessarily broad database locks.

## 7. Idempotency

Re-running any non-destructive job SHOULD be safe.

Examples:

- importing the same `.eml` twice must not create inconsistent metadata;
- discovering the same IMAP UID twice must reconcile;
- rerunning `notmuch new` is safe;
- reprocessing an already hashed object should validate/reuse the hash;
- backup event recording must not falsely create multiple verified copies from one repository.

## 8. Observability

The CLI SHALL eventually expose:

```text
mailarchive status
```

including:

- last successful fast sync per account;
- last full sync;
- number of unprocessed messages;
- quarantine count;
- integrity failures;
- last successful backup;
- verified backup destinations;
- deletion candidate count;
- watcher health.

Machine-readable JSON output SHOULD be supported.
