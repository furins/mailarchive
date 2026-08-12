# Data Model

## 1. Design approach

SQLite stores operational metadata and auditability. Canonical email bytes remain in Maildir.

Use UTC timestamps.

The exact schema may evolve through migrations, but the following entities/invariants are required.

## 2. accounts

Suggested fields:

```text
id
name
kind                    # imap | gmail | pop3
enabled
remote_retention_days   # nullable only if explicit 'never'
remote_delete_policy    # disabled | enabled
required_verified_backups
config_ref
created_at
updated_at
```

Do not store plaintext passwords.

## 3. remote_messages

Represents a provider-side identity/appearance.

Suggested fields:

```text
id
account_id
remote_folder
uidvalidity               # required with remote_uid for ordinary IMAP identity
remote_uid
provider_message_id
provider_thread_id
message_id_header
remote_flags_json
remote_labels_json
first_seen_at
last_seen_at
remote_present
identity_confidence
```

Unique constraints SHALL reflect provider semantics.

M5 generalizes this to `provider_kind`: IMAP retains folder+UIDVALIDITY+UID, Gmail uses only
account+provider_message_id. `gmail_labels` is keyed by account+label_id and
`gmail_message_labels` is a cross-account-safe relationship table. `gmail_sync_state` stores only
opaque history checkpoints and local timestamps/error categories.
The full-sync checkpoint is committed only after catch-up history replay succeeds.

M6 adds `provider_kind='pop3'`. POP3 stores the server UIDL in `provider_message_id`, with a
partial unique key on `(account_id, provider_message_id)` for POP3. RFC Message-ID is metadata
only. A UIDL can link to exactly one canonical object; a conflicting re-link fails closed. Two
UIDLs may link to the same account-scoped SHA-256 canonical object.

## 4. canonical_messages

Represents preserved message bytes.

```text
id
sha256
local_path
size_bytes
message_id_header
message_date
downloaded_at
archived_at
storage_state             # pending | archived | quarantined
quarantined_at
integrity_status
integrity_verified_at
created_at
```

`sha256` is account-scoped canonical byte identity. In M7, `archived_at` is nullable and is set
only at first HAM promotion; a quarantined never-HAM object has no archive-admission timestamp.

Multiple remote identities may map to one canonical object only where the mapping is proven safe.

## 5. remote_canonical_links

```text
remote_message_id
canonical_message_id
link_reason
created_at
```

This table permits multiple provider presentations to map to one byte object without assuming Message-ID uniqueness.

## 6. classifications

```text
id
canonical_message_id
classification          # ham | suspect | spam
score
reason
classifier
classifier_version
manual_override
classified_at
```

The current effective classification may be calculated from latest/manual rules.
The latest valid manual override wins; otherwise the latest automatic verdict wins. History is never
rewritten for an override.

notmuch classification tags are derived aggregate hints at its RFC Message-ID group boundary. They
are not a per-canonical classification, identity, retention, or deletion input; SQLite is the
authoritative source for those facts.

## 7. protections

```text
canonical_message_id
keep_online
legal_hold
protection_reason
updated_at
```

Defaults must be conservative.

## 8. attachments

Represents attachment content identity.

```text
id
sha256
size_bytes
mime_type
content_path
first_seen_at
```

## 9. message_attachments

```text
canonical_message_id
attachment_id
part_index
filename_original
content_disposition
```

Extraction failure SHALL not damage the canonical message.

## 10. backup_repositories

```text
id
name
kind
repository_ref
enabled
verification_policy
created_at
```

Secrets SHALL not be embedded in `repository_ref` if avoidable.

## 11. backup_runs

```text
id
repository_id
started_at
completed_at
status
archive_name
command_exit_code
verification_status
verified_at
details_json
```

## 12. message_backup_evidence

If message-level evidence is needed:

```text
canonical_message_id
backup_run_id
covered
verified
recorded_at
```

Implementation MAY optimize evidence by snapshot/range/inventory if it remains possible to prove whether a message was protected.

## 13. deletion_evaluations

```text
id
remote_message_id
canonical_message_id
evaluated_at
eligible
reason_codes_json
policy_version
```

Reason codes SHALL make failures explainable.

Example:

```json
[
  "RETENTION_NOT_ELAPSED",
  "BACKUPS_INSUFFICIENT"
]
```

## 14. remote_mutations

```text
id
account_id
remote_message_id
operation
dry_run
requested_at
completed_at
status
provider_response_summary
error_code
```

## 15. audit_events

```text
id
timestamp
actor
event_type
account_id
remote_message_id
canonical_message_id
result
details_json
```

Audit events SHALL be append-oriented.

## 16. integrity invariants

## 17. fast_path_health (M4)

One local operational row per `(account_id, remote_folder)` records only bounded safe facts:
effective mode, UTC heartbeats, last event/sync/index successes, failure category, reconnect count,
and pending local index work. It contains no credentials, IMAP response payloads, or RFC822 bytes.

At minimum:

1. Every archived canonical object has a path and SHA-256.
2. `archived_at >= downloaded_at`.
3. Remote retention derives from `archived_at`.
4. A quarantined message cannot be an ordinary deletion candidate.
5. `keep_online` or `legal_hold` blocks deletion.
6. Backup count includes only verified repositories/runs.
7. Missing canonical bytes block deletion.
8. Hash mismatch blocks deletion.
9. Unknown remote identity blocks deletion.
