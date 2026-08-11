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

- mbsync adapter;
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

## M5 — Gmail semantics

Deliver:

- Gmail-specific acquisition strategy;
- safe label mapping;
- duplicate analysis;
- provider identifiers.

Acceptance:

- Gmail labels do not create unsafe canonical identity assumptions;
- deletion mapping is not implemented yet.

## M6 — POP3 fallback

Deliver:

- getmail6 adapter;
- state tracking;
- no delete-after-retrieve by default.

## M7 — Spam quarantine

Deliver:

- classifier adapter;
- HAM/SUSPECT/SPAM;
- quarantine storage/state;
- manual override;
- notmuch tags;
- audit.

No automatic permanent deletion.

## M8 — Attachments and Recoll

Deliver:

- MIME attachment extraction;
- SHA-256 content-addressable attachment store;
- SQLite attachment catalog;
- Recoll adapter/index;
- search examples.

Canonical email remains unchanged.

## M9 — Borg backup and verification

Deliver:

- Borg adapter;
- repository definitions;
- backup runs;
- verification;
- evidence model;
- restore test workflow.

## M10 — Retention engine

Deliver:

- eligibility policy;
- explainable reason codes;
- `deletion-candidates`;
- reports;
- extensive combinatorial tests.

Still no remote deletion.

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
