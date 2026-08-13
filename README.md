# MailArchive

Local-first, searchable and verifiable email archiving for multiple Gmail, IMAP and POP3 accounts.

## Status

M5 adds read-only Gmail REST acquisition. Gmail `Message.id` is global provider identity, while
labels and thread IDs are metadata only; `messages.get(format=RAW)` bytes are canonically ingested
unchanged. Gmail history is polled every 90 seconds by default, includes SPAM/TRASH inventory, and
never uses Gmail IMAP, Pub/Sub, or mailbox mutation. See `docs/GMAIL_SETUP.md`.

## M4 IMAP fast path

Python 3.14+ is required. `mailarchive imap watch --account NAME --config CONFIG` uses the
public stdlib `imaplib.idle()` API only; there is no IMAPClient dependency. Its read-only
notification connection selects INBOX and only observes bounded 30-second IDLE windows. A
separate M3 connection remains the sole canonical acquisition path (`UID SEARCH` then `UID FETCH
BODY.PEEK[]`). The watcher arms IDLE before each catch-up sync, coalesces event bursts, reconciles
every ten minutes, and falls back to a stop-aware 90-second poll when IDLE is disabled or absent.
It holds a dedicated lifetime watcher lock, while manual M3 sync remains available. Health is local
SQLite state with a 180-second stale heartbeat threshold; indexing failures are retried locally and
never cause body refetch. No systemd service, Gmail semantics, remote mutation, or slow-path worker
is introduced by M4.

## M3 IMAP acquisition

Each IMAP folder is an explicit network action; no configuration check, status, search, or
database command contacts a server:

```bash
export MAILARCHIVE_PERSONAL_IMAP_PASSWORD='...'
uv run mailarchive imap sync --account personal --folder INBOX --config ./your-config.yaml --json
```

Use a disposable/local test account first. The adapter selects exactly the requested configured
folder read-only, discovers UIDs using `UID SEARCH`, and obtains each unseen message using
`UID FETCH (UID BODY.PEEK[])`. The returned IMAP literal is written unchanged to canonical
Maildir; there is no mbsync mirror or generated mbsync configuration. It never issues STORE,
COPY, MOVE, EXPUNGE, APPEND, DELETE, CREATE, RENAME, or CLOSE.

Configured folder names are retained exactly in SQLite and lock identity, while MailArchive
encodes them as IMAP quoted strings for protocol commands. M3 supports ASCII folder names
(including spaces, `/`, `\\`, and quotes); non-ASCII names are rejected because modified UTF-7
is intentionally not implemented yet.

IMAP credentials are only `config_ref: env:VARIABLE_NAME`; the secret value is not written to
SQLite, audit events, normal errors, or JSON output. Production settings use `IMAPS` or
`STARTTLS` with normal certificate and hostname verification. `INSECURE_LOOPBACK` exists only
for disposable loopback tests. Each configured folder gets a hashed safe process lock while
SQLite retains the original remote folder name. Remote identity is UID plus UIDVALIDITY, never
Message-ID alone.

The former mbsync experiment was deliberately rejected: its Dovecot integration test showed
isync 1.4.4 converting CRLF to LF and injecting `X-TUID`. See ADR-015. The direct-IMAP loopback
integration test proves server-to-canonical byte equality; canonical bytes are never normalized
or rewritten.

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

## M10 retention reports

`mailarchive deletion-candidates --config CONFIG` and `mailarchive retention report --config CONFIG`
are local policy reports only. A candidate is one proven remote identity linked to a canonical
object; `eligible: true` never authorizes deletion (`execution_authorized` is always false in M10).
The retention clock is 365 days from `archived_at` by default, requires two distinct currently
verified Borg repository identities (not configured repository names), and checks the current
canonical file SHA-256. Local holds use
`mailarchive retention hold|release --canonical-id ID --kind keep-online|legal-hold --reason TEXT`.
No report, control, or status command contacts a provider or Borg.

## M11 remote-delete dry run

`mailarchive remote-delete --dry-run --config CONFIG` performs a fresh local M10
evaluation and appends an exact, rate-limited target snapshot. It cannot execute a
provider mutation: M11 has no `--execute` command, write-capable adapter, or Gmail
write scope. `remote_deletion.max_per_run` and `max_per_account` both default to 10.

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
- IMAP acquisition: direct read-only IMAP adapter.
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

## M8 attachment content search

Run the local slow worker separately from acquisition:

```bash
uv run mailarchive attachments extract --config ./your-config.yaml --json
uv run mailarchive attachments index refresh --config ./your-config.yaml --json
uv run mailarchive attachments search 'invoice-token' --scope archived --config ./your-config.yaml --json
```

The worker reads canonical bytes but never modifies them. It stores decoded MIME attachment
payloads globally at `<archive.root>/attachments/sha256/<prefix>/<sha256>` and treats filenames as
untrusted metadata. Recoll indexes only that store with its managed config at
`state/recoll/config`; its database at `state/recoll/db` can be rebuilt. Search defaults to
archived relationships and SQLite authoritatively enforces `archived`, `quarantine`, or `all`
scope. No M8 command contacts a mail provider or performs remote mutation.
# M9 Borg backups

Configure named Borg destinations under `backup.repositories`, then explicitly run
`mailarchive backup repo init`, `backup run`, `backup verify`, and optionally `backup restore-test`.
`mailarchive backup status` is local SQLite reporting only and never contacts Borg.
