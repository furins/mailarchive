# MailArchive

Local-first, searchable and verifiable email archiving for multiple Gmail, IMAP and POP3 accounts.

## Status

Specification phase. No destructive remote-mail behavior is permitted in the initial milestones.

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
