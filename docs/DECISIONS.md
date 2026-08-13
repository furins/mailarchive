# Architecture Decisions

This file records decisions already approved for v1.

## ADR-001 — Maildir is canonical storage

Status: accepted.

Reason:
Open, inspectable, resilient individual-message files and compatible with notmuch and standard mail tooling.

## ADR-002 — Original messages are immutable

Status: accepted.

Attachments are not stripped from canonical messages. Application metadata is external.

## ADR-003 — SQLite stores operational inventory

Status: accepted.

SQLite stores account/provider mappings, hashes, classification, backup evidence, retention state and audit information.

## ADR-004 — notmuch is the primary email search index

Status: accepted.

Indexes are rebuildable and not canonical.

## ADR-005 — Recoll indexes attachment contents

Status: accepted.

It complements notmuch rather than replacing it.

## ADR-006 — Borg provides deduplicated backup

Status: accepted.

At least two verified backup copies are recommended before ordinary mail can become deletion-eligible.

## ADR-007 — Remote retention starts at archive admission

Status: accepted.

Default: 365 days from `archived_at`, configurable per account. 730 days and never-delete are supported policies.

## ADR-008 — Fast path is separate from slow path

Status: accepted.

Incoming mail visibility must not wait for full archival processing.

## ADR-009 — IMAP IDLE plus fallback polling

Status: accepted in principle.

Use event-driven targeted INBOX acquisition and a 1–2 minute fallback poll.

## ADR-010 — Spam is quarantined, not immediately discarded

Status: accepted.

HAM/SUSPECT/SPAM states; manual recovery; separate quarantine retention.

## ADR-011 — Deduplication is non-destructive

Status: accepted.

Byte identity uses SHA-256. Message-ID alone is insufficient for destructive deduplication.

## ADR-012 — Remote deletion is a late capability

Status: accepted.

It is disabled by default, separate from candidate reporting, dry-run capable, auditable and rate limited.

## ADR-013 — Gmail requires provider-aware semantics

Status: accepted.

Gmail labels must not be naively equated with independent canonical folders/messages.

## ADR-014 — POP3 is fallback only

Status: accepted.

Prefer IMAP where available.

## ADR-015 — Direct IMAP literals are canonical acquisition bytes

Status: accepted.

The M3 Dovecot loopback fidelity test rejected isync/mbsync as a canonical acquisition
path: isync 1.4.4 converted CRLF to LF and injected `X-TUID` into its Maildir copy.
MailArchive now fetches each new UID with read-only `BODY.PEEK[]`; the returned literal is
written unchanged to canonical Maildir. mbsync is not a production dependency.

## ADR-016 — Gmail REST API is the M5 provider path

Status: accepted.

Gmail M5 uses Gmail REST API v1 only, with `gmail.readonly` and explicit GET-only methods for
profile, labels, messages, and history. Gmail `Message.id` is the provider-global remote
identity; RFC Message-ID, threadId, labels, and label names are not identity. A message with many
labels has one remote identity and may have one canonical byte object and many label relationships.
Canonical acquisition uses `messages.get(format=RAW)`, strict base64url decoding, and M1 byte
ingest without rewriting bytes. Gmail IMAP, Pub/Sub, users.watch, and all Gmail mailbox mutation
are deliberately out of scope. History polling defaults to 90 seconds; expired history requires a
safe full sync. Token and OAuth client-secret files must remain outside `archive.root`. SPAM and
TRASH are inventoried as provider labels only; M7 owns local spam/quarantine policy.

## ADR-017 — M6 POP3 uses direct controlled retrieval; getmail6 is rejected

Status: accepted.

The real `/home/stefano/.local/bin/getmail` binary, getmail 6.20.00, was tested against a
disposable loopback POP3 server by `uv run pytest tests/test_getmail6_acceptance.py -v`. The test
generates a 0600 temporary rcfile and invokes:

```text
/home/stefano/.local/bin/getmail --getmaildir <temporary-state> --rcfile <temporary-rcfile>
```

Its exact rcfile is:

```ini
[retriever]
type = SimplePOP3Retriever
server = 127.0.0.1
port = <temporary loopback port>
username = acceptance-user
password = acceptance-password
timeout = 10

[destination]
type = Maildir
path = <temporary staging Maildir>/

[options]
read_all = true
delete = false
delete_after = 0
delete_bigger_than = 0
delivered_to = false
received = false
mark_read = false
```

All eleven fixtures differed: normal CRLF, folded headers, multipart MIME, base64 attachment,
malformed-but-storable bytes, no final newline, two distinct messages sharing RFC Message-ID,
two UIDLs with identical bytes, and leading-dot body lines. Every delivered file began with the
added `Return-Path: <unknown>` header (first difference offset 0); getmail also reconstructed POP3 lines
with native LF line endings, and the no-final-newline fixture gained a final LF. Its Maildir
destination therefore cannot preserve canonical RFC822 bytes even with Delivered-To and Received
disabled. The server observed only USER, PASS, UIDL, LIST, RETR, and QUIT; DELE was absent and all
eleven mailbox entries remained present.

`mailarchive pop3 sync` consequently remains the production direct UIDL/RETR adapter. This is an
intentional roadmap deviation: canonical RFC822 byte immutability takes priority over the original
getmail6 preference. SQLite is authoritative for POP3 provider identity; any getmail oldmail file
would be subordinate, rebuildable state only. getmail6 is not a runtime dependency and never writes
MailArchive canonical storage.

The canonical POP3 byte representation is the exact unstuffed data exposed by POP3 `RETR`: every
CRLF-terminated wire data line is retained, exactly one stuffing dot is removed from dot-leading
lines, and the `.` terminator line is excluded. POP3 cannot expose an unknowable pre-protocol local
file representation without a final line delimiter. The adapter never treats such a representation
as canonical. Duplicate UIDLs in one listing fail closed before any RETR or local state change.

## ADR-018 — M7 local classification is fail-closed quarantine

Status: accepted.

New exact RFC822 bytes first enter `staging/<account>/cur`. `archived_at` remains NULL until a
HAM verdict promotes those unchanged bytes to `mail/<account>/cur`; this is archive admission and
starts the future retention clock. SUSPECT and SPAM move unchanged bytes to
`quarantine/<account>/cur` and set `quarantined_at`; M7 performs no permanent deletion and no
provider mutation.

The production observation-only adapter POSTs the exact bytes as `message/rfc822` to loopback-only
Rspamd `/checkv2`. `no action` maps to HAM; explicit spam actions map to SPAM; intermediate,
unknown, skipped, malformed, timeout, and transport responses map to SUSPECT quarantine. No
Rspamd rewritten content or learning endpoint is used. Provider spam labels are evidence only.

SQLite records append-only verdicts. The latest manual override wins over automatic history. A
manual HAM restores the same canonical ID/SHA to mail and assigns `archived_at` only if absent;
manual SUSPECT/SPAM quarantines it while retaining any historical `archived_at`.

notmuch indexes `archive.root`, ignoring staging and state roots. Quarantine receives explicit
derived tags and is excluded from ordinary searches through `exclude_tags=quarantine`; tags can be
reapplied solely from SQLite after rebuilding the index.

notmuch groups files by RFC Message-ID and `--output=files` may emit every filename in a matching
group. MailArchive therefore maintains two independent derived indexes: `mail/` for archived search
and `quarantine/` for quarantine search. This prevents quarantined content from influencing an
ordinary archive query. SQLite remains authoritative for each canonical object's lifecycle,
classification, path, retention, and visibility; no safety decision relies on a notmuch tag.

Each derived index owns independent managed configuration and hook paths as well as its database.
Concurrent archive/quarantine refreshes therefore cannot swap mail roots by rewriting a shared
notmuch configuration. Staging is indexed nowhere. M7 performs no permanent deletion and no
provider mutation; provider SPAM metadata is non-authoritative and classifier failure becomes
SUSPECT quarantine.

For a canonical message without Message-ID, notmuch's real generated `notmuch-sha1-<digest>` ID
is used only to select its derived index message after verifying the selector resolves to the
expected local path. The SHA-1 reproduces notmuch state only; canonical identity and integrity
remain account-scoped SHA-256.

Pending state is locally recoverable: every provider sync reconciles account-scoped pending rows
from SQLite before provider acquisition. This verifies the stored SHA and classifies only the local
exact bytes; it neither re-downloads a known provider message nor mutates a provider. File moves
install the destination without overwrite (hard-link where possible; fsynced temporary copy and
link installation otherwise), then remove the source only after the durable destination exists.

## ADR-019 — M8 attachment extraction and Recoll are local derived state

Status: accepted.

M8 reads immutable canonical RFC822 bytes with Python's byte-oriented MIME parser and never
serializes or rewrites them. A non-multipart leaf is an attachment iff it has
`Content-Disposition: attachment` or a filename parameter. Attachment identity is SHA-256 of
the payload after Content-Transfer-Encoding decoding, without archive expansion, format
conversion, or charset normalization. Blobs are globally deduplicated at
`<archive.root>/attachments/sha256/<first-two-hex>/<sha256>`; untrusted original filenames are
SQLite metadata only and never form a filesystem path.

The v9 catalog separates global immutable content from per-message part facts (original filename,
disposition, and declared MIME type). A successful `attachment_extractions` row also records a
zero-attachment scan. Failed parses, integrity checks, storage, and database operations are
message-local bounded errors; retries rederive safely and orphan immutable blobs are harmless.
Relationships attach to canonical ID, so M7 HAM/quarantine transitions do not re-extract content.

Recoll is a single managed, rebuildable derived index under `state/recoll/config` and
`state/recoll/db`, with the content-addressed attachment store as its only topdir. Local Recoll
1.43.0 and CI's Ubuntu 24.04 Recoll 1.36.1 characterize extensionless text and PDF blobs through
the direct store, so no alias view is needed. Recoll returns candidates only: SQLite maps each SHA
to relationships and applies archived/quarantine/all lifecycle scope at query time. Bounded audit
events include refresh, rebuild, and `recoll.search.failed`; commands use argv lists, explicit
`-c` configuration and bounded timeouts. M8 does not call providers, alter fast-path acquisition,
mutate remote mail, or implement deletion.
# ADR-019: Borg 1.x verified backup evidence (M9)

M9 supports Borg 1.x (CI baseline 1.2.8), rejecting Borg 2.x. Repositories are absolute local
paths outside `archive.root` or password-free `ssh://` URLs; ordinary SSH host-key checking remains
in force. Encryption mode is explicit and encrypted repositories receive a passphrase only through
a configured environment-variable name at execution time.

MailArchive makes a temporary `state/backup-snapshots/<run-id>` tree, using SQLite's online backup
API and hash-checked hardlinks/copies of finalized archive/quarantine messages and their referenced
content-addressed blobs. It excludes pending objects, credentials, caches, indexes, logs, and other
derived state. The snapshot contains deterministic `metadata/backup-manifest.jsonl`; its SHA-256
anchors verification after cleanup.

Create is not verification. Verification reads the archived manifest, compares every archive regular
file path/size/SHA-256, then runs `borg check --archives-only --verify-data`. Failure, including a
later re-check, clears verified evidence. A restore test extracts only to an empty external directory
and validates manifest objects and SQLite integrity. Runs in one repository are one destination;
M10 must count distinct verified Borg repository identities, never merely configured repository
rows. There is no retention/deletion work, nor Borg
prune/delete/compact/repair in M9. Borg key/passphrase recovery remains an external operator duty.

# ADR-020: Local retention candidate policy (M10)

M10 uses a 365-day default calculated exclusively from `archived_at`; the RFC822 Message-Date is
irrelevant. It evaluates each proven remote identity, not Message-ID or just canonical bytes.
Canonical-level keep-online and legal-hold controls conservatively block every linked identity, and
quarantine is a hard blocker. A candidate requires the live archive file to resolve beneath managed
`mail/` and exactly match its stored SHA-256, plus evidence from distinct currently verified Borg
repository identities. M9 evidence is consumed from SQLite only: M10 never invokes Borg or
providers. Contradictory provider/account and remote/canonical-account facts fail closed.

Evaluation rows retain bounded ordered reason codes for explainability. Candidate eligibility is not
deletion authorization; M10 provides no provider mutation or remote deletion interface.

# ADR-021: M11 remote deletion is dry-run planning only

M10 eligibility, M11 planned targets and production execution authorization are separate facts.
Every M11 plan freshly re-evaluates M10, then records a deterministic versioned SHA-256 fingerprint
of account/canonical identity and provider identity (IMAP folder/UIDVALIDITY/UID, Gmail Message.id,
or POP3 UIDL). Limits are deterministic: account name plus remote ID ordering, max 10 per run and
per account by default. Production execution is always unavailable in M11: there is no execute
CLI, write OAuth scope, or network-writing adapter. Tests inject disposable in-process fakes only.

The fake state machine commits `started` before calling a fake adapter. Confirmed no-mutation
failure and unknown outcomes are distinct; unknown is never retried and halts the run. A local
fresh-policy and fingerprint revalidation rejects stale targets before a fake call. M12 exclusively
owns any discussion or implementation of real provider writes.
