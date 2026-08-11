"""M0 safety constants. Remote mutation is deliberately unsupported."""

from __future__ import annotations

REMOTE_DELETION_DEFAULT = False
REMOTE_MUTATION_SUPPORTED = False
NEVER_DELETE: None = None


def redact_secret_reference(value: str) -> str:
    """Return a display-safe representation of a configuration reference."""
    if not value:
        return "<empty>"
    return "<redacted>"
