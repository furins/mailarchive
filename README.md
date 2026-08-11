# MailArchive

Local-first, searchable and verifiable email archiving for multiple Gmail, IMAP and POP3 accounts.

## Status

M0 provides a local-only configuration, database, and CLI safety baseline. No provider
access or destructive remote-mail behavior exists.

## M0 quick start

Install the project with its development tools, then use a configuration that points to
paths you can write (the checked-in example intentionally uses production-style paths):

```bash
uv sync --all-groups
uv run mailarchive config check --config ./config.example.yaml
uv run mailarchive db init --config ./your-config.yaml
uv run mailarchive status --config ./your-config.yaml --json
```

`remote_deletion_enabled` defaults to `false`; M0 has no remote-mutation command or
provider implementation. `remote_retention_days: never` is the explicit never-delete
configuration. Configuration summaries redact `config_ref` values.

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
