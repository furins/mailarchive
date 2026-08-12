# AGENTS.md

## Project

MailArchive is a local-first email archival system designed to:

- acquire email from multiple Gmail, IMAP and POP3 accounts;
- preserve original RFC822/MIME messages locally;
- provide fast local search from CLI;
- index attachment contents;
- quarantine spam without immediate destruction;
- deduplicate safely where possible;
- create verifiable backups;
- delete messages from origin servers only after strict retention and backup requirements are satisfied.

The repository documentation under `docs/` is the source of truth.

M5 Gmail production acquisition is Gmail REST API v1, read-only only; Gmail IMAP and remote
mutation are not permitted until their explicitly approved future milestones.

Read, in this order, before changing code:

1. `docs/REQUIREMENTS.md`
2. `docs/SAFETY.md`
3. `docs/ARCHITECTURE.md`
4. `docs/DATA_MODEL.md`
5. `docs/TEST_PLAN.md`
6. `docs/ROADMAP.md`

## Non-negotiable rules

1. NEVER delete, expunge, move to Trash, alter, or overwrite remote mail unless the current milestone explicitly enables destructive operations.
2. Remote deletion MUST be disabled by default.
3. Every destructive operation MUST support `--dry-run`.
4. Tests MUST NOT use production mail accounts.
5. The canonical archived message MUST preserve the downloaded RFC822/MIME bytes unchanged.
6. Spam classification MUST NOT destroy a message.
7. `archived_at`, not the original message Date header, starts the remote-retention clock.
8. A message MUST NOT become deletion-eligible before its account retention interval has elapsed.
9. Required backup verification MUST be recorded before a message can become deletion-eligible.
10. `keep_online`, `legal_hold`, quarantine, uncertainty, or incomplete metadata MUST fail closed and prevent deletion.
11. Fast-path mail reception MUST NOT wait for Recoll, attachment extraction, deduplication, Borg, or other slow-path work.
12. Every state transition relevant to retention or deletion MUST be auditable.
13. Unknown states MUST be treated as unsafe.
14. Idempotency is required for acquisition, indexing and metadata updates.
15. No implementation may silently modify the canonical Maildir message body or MIME structure.
16. A failed verification MUST reduce privileges/state; it must never advance a message toward deletion.
17. Database schema migrations MUST be explicit and tested.
18. External commands MUST be invoked through adapters with timeouts, captured exit status and structured logs.
19. Credentials MUST never be committed to the repository or written to ordinary logs.
20. Production-destructive features belong to the final milestones and require dedicated tests and explicit operator opt-in.

## Coding principles

- Primary implementation language: Python 3.14+.
- CLI: Typer or argparse; prefer Typer if it does not complicate packaging.
- Database: SQLite, WAL mode where appropriate.
- ORM is optional. Prefer explicit SQL or a lightweight layer if it keeps invariants clear.
- Configuration: YAML with schema validation.
- Logging: structured and machine-readable capable.
- Hashing: SHA-256 for message and attachment identity/integrity.
- Tests: pytest.
- Type checking: mypy or pyright.
- Formatting/linting: ruff.
- Time values: timezone-aware UTC internally; human display may use local timezone.
- Identifiers must be stable and explicit.

## Change discipline

For every task:

1. Inspect the relevant documentation.
2. State assumptions in the PR/task summary.
3. Implement the smallest coherent change.
4. Add or update tests.
5. Run the relevant test suite.
6. Do not weaken safety checks to make tests pass.
7. Update docs when behavior or schema changes.
8. Report any ambiguity rather than inventing destructive behavior.

## Definition of done

A task is done only when:

- implementation is complete;
- tests pass;
- safety invariants remain intact;
- new behavior is documented;
- no production credentials are required for tests;
- dry-run behavior exists where relevant;
- commands fail safely on partial state.
