# Roadmap

## M0 — Repository skeleton and safety baseline

Deliver:

- Python package skeleton;
- CLI entry point;
- YAML configuration model;
- SQLite connection/migration framework;
- structured logging;
- pytest;
- ruff;
- type checking;
- sample configuration;
- safety constants/policy placeholders.

No network access required.
No remote mutation code allowed.

Acceptance:

- tests pass;
- `mailarchive --help` works;
- `mailarchive config check` validates example config;
- database initializes in a temporary directory;
- deletion is absent or explicitly unsupported.

## M1 — Canonical local ingest

Deliver:

- `.eml` ingest;
- Maildir-compatible canonical storage;
- SHA-256;
- SQLite inventory;
- idempotency;
- basic audit events;
- status command.

Acceptance:

- same bytes do not create inconsistent duplicate canonical objects;
- same Message-ID with different bytes remains distinguishable;
- canonical bytes are unchanged.

## M2 — notmuch integration

Deliver:

- adapter;
- `notmuch new`;
- tag conventions;
- search documentation/helpers.

Acceptance:

- imported fixture mail is searchable;
- notmuch index can be deleted and rebuilt.

Implementation notes: notmuch is a rebuildable index under `state/notmuch`, separate from
the canonical Maildir. Its managed configuration disables Maildir flag synchronization and
automatic decryption; refresh runs without hooks. Initial indexing assigns `archive`, not
the normal `inbox`/`unread` defaults. File-level search maps paths back to SQLite because
notmuch can group multiple canonical files sharing a Message-ID.

## M3 — IMAP read-only acquisition

Deliver:

- direct read-only IMAP adapter;
- account/folder mapping;
- UID metadata capture;
- read-only/default-safe configuration;
- fake/local IMAP tests.

No deletion, expunge or Trash behavior.

## M4 — Fast path

Deliver:

- IMAP IDLE watcher integration;
- targeted INBOX sync;
- reconnect logic;
- 1–2 minute polling fallback;
- `notmuch new` after acquisition;
- health/status metrics.

Acceptance:

- slow workers can be disabled/broken without preventing fast-path visibility.

Implemented for ordinary IMAP INBOX: stdlib Python 3.14 `imaplib.idle()` notification, separate
M3 canonical acquisition connection, bounded/reconnecting watcher, polling fallback, notmuch
refresh and local health. M5 Gmail semantics remains explicitly out of scope.

## M5 — Gmail semantics

Deliver:

- Gmail-specific acquisition strategy;
- safe label mapping;
- duplicate analysis;
- provider identifiers.

Acceptance:

- Gmail labels do not create unsafe canonical identity assumptions;
- deletion mapping is not implemented yet.

Implemented with Gmail REST API v1, `gmail.readonly`, provider-global `Message.id`, label catalog
relationships, RAW byte ingest, history polling, and no Gmail mutation path.

## M6 — POP3 fallback

Deliver:

- getmail6 adapter;
- state tracking;
- no delete-after-retrieve by default.

Implemented as direct controlled POP3 UIDL/RETR acquisition. The real getmail 6.20.00 acceptance
experiment definitively rejected its Maildir destination for canonical use: it adds `Return-Path`
and reconstructs POP3 data with LF line endings, even with all known decoration and deletion options
explicitly disabled. Direct retrieval remains the production adapter because canonical byte
immutability takes priority over the original getmail6 preference. No delete option or remote
mutation path is exposed.

## M7 — Spam quarantine

Deliver:

- classifier adapter;
- HAM/SUSPECT/SPAM;
- quarantine storage/state;
- manual override;
- notmuch tags;
- audit.

No automatic permanent deletion.

M7 remains local-only: no provider mutation and no permanent deletion. It introduces staging,
append-only classifications, fail-closed quarantine, manual local overrides, and rebuildable tags.

## M8 — Attachments and Recoll

Deliver:

- MIME attachment extraction;
- SHA-256 content-addressable attachment store;
- SQLite attachment catalog;
- Recoll adapter/index;
- search examples.

Canonical email remains unchanged.

Implemented local-only with decoded-MIME SHA-256 blobs under `attachments/sha256`, a
v9 SQLite relationship catalog, explicit extraction reconciliation, and a managed,
rebuildable Recoll index. Attachment query visibility is lifecycle-filtered by SQLite.

## M9 — Borg backup and verification

Deliver:

- Borg adapter;
- repository definitions;
- backup runs;
- verification;
- evidence model;
- restore test workflow.

Implemented with Borg 1.x repositories, immutable repository-identity binding, controlled
SQLite-backed snapshots, deterministic SHA-256 manifests, exact archive inventory plus
`borg check --archives-only --verify-data`, and explicit out-of-place restore tests. Create
alone is unverified; a failed re-verification revokes message evidence. M9 has no retention,
remote deletion, or Borg prune/delete/compact operation.

## M10 — Retention engine

Deliver:

- eligibility policy;
- explainable reason codes;
- `deletion-candidates`;
- reports;
- extensive combinatorial tests.

Still no remote deletion.

Implemented as schema v11 local-only policy reporting: per-remote-identity evaluations, bounded
reason codes, canonical holds, append-only history, exact file SHA-256 validation and distinct M9
verified repository evidence counting. Candidate eligibility is explicitly not execution authority.

## M11 — Remote deletion adapter, dry-run first

Deliver:

- provider-specific remote mutation interface;
- dry-run;
- rate limits;
- audit;
- fake-provider destructive tests.

Production execution remains disabled by default.

## M12 — Controlled production enablement

Deliver:

- explicit per-account enable flag;
- limited batches;
- operational runbook;
- first test-account procedure;
- failure recovery.

## M13 — Operations

Deliver:

- systemd services/timers;
- status/health;
- log rotation;
- maintenance commands;
- upgrade/migration runbook;
- backup/restore runbook.
