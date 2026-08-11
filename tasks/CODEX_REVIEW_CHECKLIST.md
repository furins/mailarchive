# Codex Review Checklist

Use after each milestone.

## Scope

- Did the implementation stay within the requested milestone?
- Did it avoid speculative features?
- Did it update documentation if architecture changed?

## Safety

- Any new remote mutation?
- Any weakening of fail-closed behavior?
- Any hidden default that enables deletion?
- Any place where missing data is treated as safe?
- Any credentials in code, fixtures or logs?
- Any canonical message rewriting?

## Data

- Are migrations explicit?
- Are time values timezone-aware?
- Are identifiers stable?
- Are invariants represented in constraints or tests where appropriate?

## Idempotency

- Can the operation be safely repeated?
- Are retries safe after a crash?
- Are partial states detectable?

## Tests

- Happy path covered?
- Failure path covered?
- Boundary conditions covered?
- Safety regression tests present?
- Tests independent of production services?

## Operations

- Meaningful logs?
- Useful exit codes?
- Machine-readable output where relevant?
- External processes have timeout and captured status?

## Documentation

- CLI documented?
- New configuration documented?
- Safety implications documented?
