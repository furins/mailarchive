# MailArchive

Local-first, searchable and verifiable email archiving for multiple Gmail, IMAP and POP3 accounts.

## Status

M1 provides local-only RFC822/MIME ingest into an immutable Maildir-compatible archive,
with SQLite inventory and SHA-256 integrity metadata. No provider access or destructive
remote-mail behavior exists.

## M1 quick start

Install the project with its development tools, then use a configuration that points to
paths you can write (the checked-in example intentionally uses production-style paths):

```bash
uv sync --all-groups
uv run mailarchive config check --config ./config.example.yaml
uv run mailarchive db init --config ./your-config.yaml
uv run mailarchive ingest ./message.eml --account personal --config ./your-config.yaml --json
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
