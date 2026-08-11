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
ImapMbsyncAdapter
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
│   └── locks/
├── metadata/
│   ├── exports/
│   └── reports/
└── logs/
```

Exact system paths SHALL be configurable.

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

A full `mbsync -a` MUST NOT be triggered for every incoming message.

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
