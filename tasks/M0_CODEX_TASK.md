# Codex Task M0 — Repository Skeleton and Safety Baseline

## Objective

Implement milestone M0 from `docs/ROADMAP.md`.

Do not implement email-server network access or any destructive operation.

## Read first

- `AGENTS.md`
- `docs/REQUIREMENTS.md`
- `docs/SAFETY.md`
- `docs/ARCHITECTURE.md`
- `docs/DATA_MODEL.md`
- `docs/TEST_PLAN.md`
- `docs/ROADMAP.md`

## Deliverables

Create a Python 3.12+ project with:

1. `pyproject.toml`
2. package under `src/mailarchive/`
3. CLI executable named `mailarchive`
4. YAML configuration loading and validation
5. SQLite database initialization and migration mechanism
6. structured logging setup
7. pytest test suite
8. ruff configuration
9. type-check configuration
10. `mailarchive config check`
11. `mailarchive db init`
12. `mailarchive status` with minimal local-only output
13. a sample config derived from `config.example.yaml`

## Required safety behavior

- There must be no provider deletion implementation.
- There must be no command that can mutate a remote mailbox.
- `remote_deletion_enabled` defaults to false.
- Invalid/missing safety configuration fails closed.
- Secrets must be represented by references/placeholders and must not be printed in full.
- Time values must use timezone-aware UTC internally.
- Database foreign keys must be enabled.
- Schema versioning must be explicit.

## Suggested initial modules

```text
src/mailarchive/
├── __init__.py
├── cli.py
├── config.py
├── db.py
├── logging.py
├── models.py
├── paths.py
└── safety.py
```

You may choose a different layout if it is clearly justified.

## Initial schema

Implement only what M0 needs, but establish explicit migrations.

At minimum create:

- `schema_migrations`
- `accounts`
- `audit_events`

Fields may follow `docs/DATA_MODEL.md`.

Do not prematurely implement the full final schema if it complicates M0.

## CLI behavior

Examples:

```bash
mailarchive --help
mailarchive config check --config ./config.example.yaml
mailarchive db init --config ./config.example.yaml
mailarchive status --config ./config.example.yaml
```

Commands should have meaningful exit codes.

Support a machine-readable mode where easy, preferably `--json`.

## Tests

At minimum test:

1. valid example configuration;
2. invalid account kind;
3. deletion defaults to false;
4. invalid negative retention rejected;
5. explicit never-delete representation;
6. secret/config reference is redacted in display/log representation;
7. database initializes;
8. repeated database initialization is safe;
9. SQLite foreign keys are enabled;
10. audit insert works;
11. CLI help works;
12. config-check success/failure exit status;
13. no destructive remote command appears in CLI.

## Quality gates

Run and report:

```bash
pytest
ruff check .
```

Also run the configured type checker.

## Constraints

- Do not add Docker unless required for M0.
- Do not add IMAP/Gmail/POP3 libraries yet.
- Do not add notmuch/Recoll/Borg integrations yet.
- Do not create fake functionality that claims backups or verification occurred.
- Keep dependencies minimal.
- Update documentation if implementation choices materially differ.

## Completion report

When finished, report:

- files added/changed;
- commands executed;
- test results;
- unresolved questions;
- any deviations from the specification and why.

Do not proceed to M1 unless explicitly requested.
