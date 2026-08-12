"""Isolated, rebuildable notmuch integration for the local Maildir archive."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from mailarchive.db import connect
from mailarchive.models import AppConfig, CanonicalMessage

COMMAND_TIMEOUT_SECONDS: Final = 60.0
REFRESH_TIMEOUT_SECONDS: Final = 600.0
_NOTMUCH_ENVIRONMENT_OVERRIDES: Final = (
    "NOTMUCH_DATABASE",
    "NOTMUCH_CONFIG",
    "NOTMUCH_PROFILE",
    "MAILDIR",
)


class NotmuchError(RuntimeError):
    """Raised when the managed notmuch subprocess cannot complete safely."""


@dataclass(frozen=True)
class NotmuchLayout:
    """Filesystem locations for entirely derived notmuch state."""

    mail_root: Path
    state_root: Path
    config_path: Path
    database_path: Path
    hook_directory: Path


@dataclass(frozen=True)
class SearchResult:
    """A file-level notmuch result resolved to authoritative SQLite metadata."""

    canonical_message: CanonicalMessage
    account: str

    def as_dict(self) -> dict[str, object]:
        message = self.canonical_message
        return {
            "canonical_message_id": message.id,
            "account": self.account,
            "sha256": message.sha256,
            "local_path": str(message.local_path),
            "message_id": message.message_id_header,
            "message_date": message.message_date,
        }


def managed_layout(config: AppConfig) -> NotmuchLayout:
    """Return paths deliberately outside the canonical mail tree."""
    archive_root = config.archive.root.resolve()
    mail_root = archive_root / "mail"
    state_root = archive_root / "state" / "notmuch"
    database_path = state_root / "db"
    try:
        database_path.relative_to(mail_root)
    except ValueError:
        pass
    else:  # pragma: no cover - defensive against a future layout regression
        raise NotmuchError("notmuch database must not be inside the canonical mail root")
    return NotmuchLayout(
        mail_root=mail_root,
        state_root=state_root,
        config_path=state_root / "config",
        database_path=database_path,
        hook_directory=state_root / "hooks",
    )


def managed_config_text(layout: NotmuchLayout) -> str:
    """Return deterministic non-interactive notmuch configuration."""
    return "\n".join(
        (
            "# Managed by MailArchive. Do not add user hooks or credentials.",
            "[database]",
            f"mail_root={layout.mail_root}",
            f"path={layout.database_path}",
            f"hook_dir={layout.hook_directory}",
            "",
            "[user]",
            "name=MailArchive local archive",
            "primary_email=mailarchive@localhost.invalid",
            "other_email=",
            "",
            "[maildir]",
            "synchronize_flags=false",
            "",
            "[new]",
            "tags=archive",
            "",
            "[index]",
            "decrypt=false",
            "",
        )
    )


def write_managed_config(config: AppConfig) -> NotmuchLayout:
    """Atomically write the managed configuration and create an empty hook directory."""
    layout = managed_layout(config)
    layout.mail_root.mkdir(parents=True, exist_ok=True)
    layout.hook_directory.mkdir(parents=True, exist_ok=True)
    layout.config_path.parent.mkdir(parents=True, exist_ok=True)
    desired = managed_config_text(layout)
    if layout.config_path.is_file() and layout.config_path.read_text(encoding="utf-8") == desired:
        return layout
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="config.", suffix=".tmp", dir=layout.config_path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(desired)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, layout.config_path)
        os.chmod(layout.config_path, 0o600)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return layout


def isolated_notmuch_environment() -> dict[str, str]:
    """Copy ordinary process settings while removing notmuch/user-Maildir overrides."""
    environment = os.environ.copy()
    for variable in _NOTMUCH_ENVIRONMENT_OVERRIDES:
        environment.pop(variable, None)
    return environment


class NotmuchAdapter:
    """Run only managed-config notmuch commands with bounded subprocess behavior."""

    def __init__(
        self,
        config: AppConfig,
        *,
        executable: str = "notmuch",
        command_timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
        refresh_timeout_seconds: float = REFRESH_TIMEOUT_SECONDS,
    ) -> None:
        self.config = config
        self.executable = executable
        self.command_timeout_seconds = command_timeout_seconds
        self.refresh_timeout_seconds = refresh_timeout_seconds

    def _run(
        self, arguments: list[str], *, timeout_seconds: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        layout = write_managed_config(self.config)
        command = [self.executable, f"--config={layout.config_path}", *arguments]
        timeout = self.command_timeout_seconds if timeout_seconds is None else timeout_seconds
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=isolated_notmuch_environment(),
            )
        except FileNotFoundError as error:
            raise NotmuchError(
                "notmuch executable is unavailable; install the 'notmuch' package and retry"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise NotmuchError(f"notmuch command timed out after {timeout:g} seconds") from error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "no diagnostic output"
            raise NotmuchError(f"notmuch command failed (exit {completed.returncode}): {detail}")
        return completed

    def version(self) -> str:
        """Return the installed notmuch version string."""
        completed = self._run(["--version"])
        return completed.stdout.strip()

    def refresh(self) -> None:
        """Create or incrementally update the derived index without running hooks."""
        self._run(["new", "--no-hooks"], timeout_seconds=self.refresh_timeout_seconds)

    def search_files(self, query: str) -> list[Path]:
        """Return absolute canonical file paths from a file-level JSON search."""
        completed = self._run(["search", "--output=files", "--format=json", "--", query])
        try:
            output: object = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise NotmuchError("notmuch returned invalid JSON for file search") from error
        if not isinstance(output, list):
            raise NotmuchError("notmuch returned an unexpected file-search JSON format")
        file_names = cast(list[object], output)
        if not all(isinstance(item, str) for item in file_names):
            raise NotmuchError("notmuch returned an unexpected file-search JSON format")
        layout = managed_layout(self.config)
        return [
            (Path(item) if Path(item).is_absolute() else layout.mail_root / item).resolve()
            for item in cast(list[str], file_names)
        ]

    def tag(self, changes: list[str], query: str) -> None:
        """Apply index-only tags; Maildir flag synchronization stays disabled in config."""
        if not changes or any(not change.startswith(("+", "-")) for change in changes):
            raise ValueError("notmuch tag changes must be non-empty +tag or -tag values")
        self._run(["tag", *changes, "--", query])


def search_canonical_messages(config: AppConfig, query: str) -> list[SearchResult]:
    """Resolve file-level notmuch hits through SQLite, never Message-ID identity."""
    paths = NotmuchAdapter(config).search_files(query)
    if not paths:
        return []
    with connect(config.database.path) as connection:
        placeholders = ", ".join("?" for _ in paths)
        rows = connection.execute(
            f"""
            SELECT canonical_messages.id, canonical_messages.account_id, sha256, local_path,
                   size_bytes, message_id_header, message_date, downloaded_at, archived_at,
                   integrity_status, integrity_verified_at, canonical_messages.created_at,
                   accounts.name AS account_name
            FROM canonical_messages
            JOIN accounts ON accounts.id = canonical_messages.account_id
            WHERE local_path IN ({placeholders})
            """,
            tuple(str(path) for path in paths),
        ).fetchall()
    by_path = {
        Path(str(row["local_path"])).resolve(): SearchResult(
            canonical_message=CanonicalMessage(
                id=str(row["id"]),
                account_id=int(row["account_id"]),
                sha256=str(row["sha256"]),
                local_path=Path(str(row["local_path"])),
                size_bytes=int(row["size_bytes"]),
                message_id_header=(
                    None if row["message_id_header"] is None else str(row["message_id_header"])
                ),
                message_date=None if row["message_date"] is None else str(row["message_date"]),
                downloaded_at=str(row["downloaded_at"]),
                archived_at=str(row["archived_at"]),
                integrity_status=str(row["integrity_status"]),
                integrity_verified_at=(
                    None
                    if row["integrity_verified_at"] is None
                    else str(row["integrity_verified_at"])
                ),
                created_at=str(row["created_at"]),
            ),
            account=str(row["account_name"]),
        )
        for row in rows
    }
    return [by_path[path] for path in paths if path in by_path]
