# Controlled remote deletion runbook (M12)

> **Remote deletion is destructive.** Account opt-in defaults to `false`. A dry-run is local-only;
> execution requires an exact existing plan. M12 has no automatic retry, resume, or unattended
> scheduling. An unknown outcome requires explicit read-only reconciliation.

Use this procedure only after reviewing the current configuration and audit evidence. Do not edit
SQLite to repair a deletion state.

## Prerequisites

Before enabling an account, verify all of the following:

- canonical archive files exist and their SHA-256 hashes verify;
- `archived_at`, not the email Date header, has passed the configured retention period;
- the required number of distinct verified Borg repository identities protects the canonical object;
- there is no `keep-online` or `legal-hold` control and the message is not quarantined;
- the exact provider identity is coherent with the archived canonical bytes;
- the account is enabled; and
- `remote_deletion_enabled: true` is explicitly configured for that account.

Candidate eligibility is necessary but is not execution authorization. An old message Date never
backdates retention. Keep the checked-in/default account opt-in false, and use a dedicated
non-production or disposable account for the first production validation.

## Initial safe rollout and pre-flight

Keep `remote_deletion.max_per_run` and `max_per_account` small. Start with a plan limit of one,
inspect status and audit after every early execution, and increase only to a very small batch after
the test account behaves as expected. M12 does not implement automatic canaries or ramp-up.

Use these local reporting commands first (replace `ACCOUNT`, `CONFIG`, `RUN_ID`, and
`PRODUCTION_RUN_ID`):

```bash
mailarchive config check --config CONFIG
mailarchive status --config CONFIG --json
mailarchive retention report --account ACCOUNT --config CONFIG --json
mailarchive deletion-candidates --account ACCOUNT --config CONFIG --json
```

They do not authorize provider mutation. Confirm that top-level status reports
`remote_mutation_supported=true` and `remote_reconciliation_supported=true`; account-level
`remote_deletion_enabled` remains a separate opt-in fact.

## Create and inspect an exact plan

Create a local-only plan:

```bash
mailarchive remote-delete \
  --dry-run \
  --account ACCOUNT \
  --limit 1 \
  --config CONFIG \
  --json
```

This reruns M10 evaluation from current local state, snapshots the exact provider identity and
fingerprint, and creates an immutable account-filtered source run. It performs zero provider I/O.
The plan expires after 60 minutes; `--limit` applies only to planning. Record the returned `RUN_ID`.

Inspect it before any destructive action:

```bash
mailarchive remote-mutations status \
  --run-id RUN_ID \
  --config CONFIG \
  --json
```

Confirm the selected count, exact account filter, bounded mutation identities, and source-plan
status. Do not modify the database or attempt to reuse an old/expired plan.

## Execute one exact plan

> **Warning: this command can permanently delete the exact remote targets in `RUN_ID`.**

```bash
mailarchive remote-delete \
  --execute-plan RUN_ID \
  --account ACCOUNT \
  --config CONFIG \
  --json
```

Execution does no candidate discovery: it consumes that exact source plan for that exact account.
The source must still be current and eligible. Local factory and credential preflight occurs before
the production run is created. The production run is separate from the immutable source run; record
its returned `PRODUCTION_RUN_ID`.

Provider calls are serial. Before each destructive provider call, MailArchive commits the mutation
`started` state and bounded audit evidence. The first stale, failed, or unknown result halts the run;
later rows remain planned. A source plan cannot be executed twice.

## Inspect success

```bash
mailarchive remote-mutations status \
  --run-id PRODUCTION_RUN_ID \
  --config CONFIG \
  --json
```

For a fully successful execution, expect `mode=production-execute`, `status=completed`, succeeded
mutations, and `reconciliation_required=false`. Only confirmed remote absence allows
`remote_present=false`. Remote deletion never deletes canonical RFC822/MIME files, attachment data,
classifications, search state, or verified backup evidence.

## Unknown, crash, and uncertain outcomes

If a halted production run has `reconciliation_required=true`, **do not** rerun `--execute-plan`,
retry the same deletion, resume planned rows, manually change mutation status, or assume either
absence or failure. First inspect local evidence, then explicitly reconcile:

```bash
mailarchive remote-mutations status \
  --run-id PRODUCTION_RUN_ID \
  --config CONFIG \
  --json

mailarchive remote-mutations reconcile \
  --run-id PRODUCTION_RUN_ID \
  --config CONFIG \
  --json
```

Reconciliation is read-only and observes the immutable historical target. Confirmed absence makes
the historical mutation succeeded and reconciled. Confirmed exact current presence makes it failed
and reconciled. Identity conflict or an unprovable provider state remains unknown and unreconciled.
The production run remains halted in every case; reconciliation never resumes later planned rows.
Any later deletion requires a **new** dry-run after the operator understands the earlier result.

## Provider procedures

### IMAP

Deletion requires the exact configured folder, UIDVALIDITY, UID, `BODY.PEEK[]` SHA-256, and
UIDPLUS. MailArchive issues only `UID STORE` for the exact UID with `+FLAGS.SILENT \\Deleted`, then
`UID EXPUNGE` for that UID. Plain/global `EXPUNGE`, `CLOSE` as deletion cleanup, and expunging
unrelated `\\Deleted` messages are forbidden. A target already marked `\\Deleted` is a conflict.
Read-only observation/reconciliation uses readonly SELECT and no mutating command.

### Gmail

Acquisition keeps the `config_ref` credential at `gmail.readonly`. Permanent deletion uses the
separate `gmail.remote_delete_token_file`, which must be mode 0600, non-symlink, outside
`archive.root`, and limited to `https://mail.google.com/`. Create it with:

```bash
mailarchive gmail auth-delete --account ACCOUNT --config CONFIG
```

The command validates the authenticated profile and writes only the mutation token; it does not
replace the readonly acquisition token. Deletion verifies exact `Message.id` with `format=raw` and
the RAW SHA-256, sends at most one permanent DELETE of that ID, then confirms with exact GET. There
is no DELETE retry. If the mutation token is no longer operationally needed, revoke/remove it under
your credential policy without exposing its contents.

### POP3

UIDL is durable identity; POP3 message numbers are session-local and are never persisted as
identity. Each mutation uses fresh UIDL to resolve the current number, RETRs exact bytes for the
SHA-256 check, issues at most one DELE, and uses deliberate QUIT to commit. A reconnect/fresh UIDL
observation confirms the result. Acquisition, deletion, and reconciliation observation serialize on
one shared per-account lock, preventing a stale acquisition snapshot from restoring presence after
a deletion. There is no destructive retry.

## Emergency stop and Gmail credential failure

To stop new production execution for an account, set `remote_deletion_enabled: false` and validate
the configuration:

```bash
mailarchive config check --config CONFIG
```

This blocks new execution for that account. It cannot roll back a provider call already committed
and does not rewrite historical mutation evidence.

For Gmail, a missing, symlinked, unsafe, malformed, or wrong-scope mutation token fails local
preflight: no Gmail HTTP, production run, or `remote_delete.production.started` audit is created.
Repair the configuration/permissions or rerun `gmail auth-delete`, never reuse the readonly token;
then create or revalidate an appropriate plan as required.

## Audit model

A successful production run has bounded lifecycle evidence:

```text
remote_delete.production.started
remote_mutation.started
remote_mutation.succeeded
...
remote_delete.production.completed
```

A halted run records `remote_mutation.failed` or `remote_mutation.unknown` (or
`remote_mutation.stale_plan`) followed by `remote_delete.production.halted`. Reconciliation records
`remote_mutation.reconcile.started` and one of `.absent`, `.present`, or `.unknown`. Audit details
never store raw provider responses, message bodies, credentials, passwords, or tokens. Source
dry-run and production evidence remain separate.
