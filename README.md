# MailArchive

Local-first, searchable and verifiable email archiving for multiple Gmail, IMAP and POP3 accounts.

## Status

M2 adds a local, rebuildable notmuch search index over M1's immutable Maildir archive.
SQLite inventory and exact RFC822/MIME bytes remain authoritative. No provider access or
destructive remote-mail behavior exists.

## M2 quick start

Install the project with its development tools, then use a configuration that points to
paths you can write (the checked-in example intentionally uses production-style paths):

```bash
uv sync --all-groups
uv run mailarchive config check --config ./config.example.yaml
uv run mailarchive db init --config ./your-config.yaml
uv run mailarchive ingest ./message.eml --account personal --config ./your-config.yaml --json
uv run mailarchive index refresh --config ./your-config.yaml --json
uv run mailarchive search 'from:mario@example.com' --config ./your-config.yaml --json
uv run mailarchive search 'subject:"contratto"' --config ./your-config.yaml --json
uv run mailarchive search 'date:2020-01-01..2022-12-31 AND fattura' --config ./your-config.yaml --json
uv run mailarchive status --config ./your-config.yaml --json
```

`remote_deletion_enabled` defaults to `false`; M0 has no remote-mutation command or
provider implementation. `remote_retention_days: never` is the explicit never-delete
configuration. Configuration summaries redact `config_ref` values.

M1 stores each account's byte-unique message at
`<archive.root>/mail/<account>/cur/<sha256>.eml`. The source is read in binary mode and
the canonical file has precisely the same bytes. Reingesting identical bytes within an
account returns the existing canonical object; identical bytes from another configured
account retain a separate account-scoped file and database record. Message-ID is
intentionally not unique in the database. A file left after a database registration
failure is safely registered by a retry after its hash is verified. Account names are
validated as safe single filesystem components, and canonical paths are checked to remain
beneath `<archive.root>/mail`. During database initialization, account rows absent from
the current configuration are disabled (not deleted) to fail closed while preserving
history.

## Search index safety

M2 generates its own non-interactive notmuch configuration at
`<archive.root>/state/notmuch/config` and passes it explicitly to every notmuch command.
Each subprocess also removes `NOTMUCH_DATABASE`, `NOTMUCH_CONFIG`, `NOTMUCH_PROFILE`, and
`MAILDIR` from its inherited environment, preventing user configuration from redirecting the
managed index. Routine commands use a 60-second timeout; index refresh has a bounded
10-minute timeout for first builds and rebuilds.
Its Xapian database is `<archive.root>/state/notmuch/db`, outside the canonical Maildir;
it can be removed and recreated by running `mailarchive index refresh`. The managed
configuration has `maildir.synchronize_flags=false`, so notmuch tags cannot rename
canonical Maildir files, and `index.decrypt=false`, so indexing does not access private
keys. Refresh uses `notmuch new --no-hooks` and an isolated MailArchive hook directory.

Every new file receives the `archive` tag only. `inbox` and `unread` are deliberately not
archival semantics. The names `keep-online`, `ham`, `suspect`, `spam`, and `quarantine`
are reserved for later milestones; M2 implements none of their behavior.

Search uses file-level notmuch results and resolves each path through SQLite before it is
shown. Output therefore contains the canonical ID, account, SHA-256, local path, optional
Message-ID, and message date. notmuch may present files sharing a Message-ID as one logical
message; this never deduplicates MailArchive records. Same-Message-ID/different-byte files
and identical bytes in different accounts remain independently preserved and mapped by their
canonical paths. Recoll remains planned for later attachment-content searching.

## Core design

- Canonical storage: Maildir, preserving original RFC822/MIME bytes.
- Metadata/inventory: SQLite.
- Email search: notmuch.
- Attachment-content search: Recoll.
- IMAP acquisition: mbsync/isync adapter.
- Gmail: Gmail-aware adapter, initially selected during implementation validation.
- POP3 fallback: getmail6 adapter.
- Spam classification: local classifier adapter, quarantine-first.
- Backup: Borg adapter.
- Automation: systemd services/timers.
- Fast path: event-driven INBOX acquisition + `notmuch new`.
- Slow path: spam refinement, attachment extraction, Recoll, hashing, dedup analysis, backup, retention processing.

## Safety principle

Remote deletion is a separate, late-stage capability and is impossible unless all deletion-safety predicates pass.

See `docs/SAFETY.md`.
