"""Managed, rebuildable Recoll index for immutable attachment blobs only."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from mailarchive.attachments import (
    AttachmentSearchResult,
    attachment_blob_path,
    search_attachment_relationships,
)
from mailarchive.db import connect, insert_audit_event
from mailarchive.models import AppConfig


class RecollError(RuntimeError):
    """The local derived Recoll index could not be safely used."""


@dataclass(frozen=True)
class RecollLayout:
    config_directory: Path
    database_directory: Path
    top_directory: Path


def managed_layout(config: AppConfig) -> RecollLayout:
    root = config.archive.root
    return RecollLayout(
        root / "state" / "recoll" / "config",
        root / "state" / "recoll" / "db",
        root / "attachments" / "sha256",
    )


def managed_config_text(layout: RecollLayout) -> str:
    # recoll.conf's two required indexing controls.  Extensionless SHA blobs are
    # deliberately indexed directly: their path is controlled and their bytes are canonical.
    return "\n".join(
        (
            "# Managed by MailArchive; no user configuration is consulted.",
            f"topdirs = {layout.top_directory}",
            f"dbdir = {layout.database_directory}",
            "skippedNames = mail quarantine staging state config logs metadata",
            "",
        )
    )


def write_managed_config(config: AppConfig) -> RecollLayout:
    layout = managed_layout(config)
    layout.config_directory.mkdir(parents=True, exist_ok=True)
    layout.top_directory.mkdir(parents=True, exist_ok=True)
    desired = managed_config_text(layout)
    path = layout.config_directory / "recoll.conf"
    if path.is_file() and path.read_text(encoding="utf-8") == desired:
        return layout
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="recoll.", suffix=".tmp", dir=layout.config_directory
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(desired)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return layout


class RecollAdapter:
    """Subprocess-only adapter with explicit managed config and bounded timeouts."""

    def __init__(
        self,
        config: AppConfig,
        *,
        index_executable: str = "recollindex",
        query_executable: str = "recollq",
        timeout_seconds: float = 600,
    ) -> None:
        self.config, self.index_executable, self.query_executable, self.timeout_seconds = (
            config,
            index_executable,
            query_executable,
            timeout_seconds,
        )

    def _run(self, executable: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        layout = write_managed_config(self.config)
        environment = os.environ.copy()
        for name in ("RECOLL_CONFDIR", "RECOLL_USERCONFIG", "HOME"):
            environment.pop(name, None)
        try:
            completed = subprocess.run(
                [executable, "-c", str(layout.config_directory), *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=environment,
            )
        except FileNotFoundError as error:
            raise RecollError(
                "recoll executable is unavailable; install the 'recoll' package and retry"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise RecollError(
                f"recoll command timed out after {self.timeout_seconds:g} seconds"
            ) from error
        if completed.returncode:
            raise RecollError(f"recoll command failed (exit {completed.returncode})")
        return completed

    def version(self) -> str:
        completed = self._run(self.index_executable, ["-h"])
        return next(
            (line.strip() for line in completed.stdout.splitlines() if "Recoll version:" in line),
            "Recoll version unavailable",
        )

    def refresh(self) -> None:
        try:
            self._run(self.index_executable, [])
        except RecollError:
            self._audit("recoll.refresh.failed", "failed")
            raise
        self._audit("recoll.refresh.succeeded", "success")

    def rebuild(self) -> None:
        layout = write_managed_config(self.config)
        # Rebuild affects only derived state. rmtree is intentionally confined to this exact path.
        try:
            if layout.database_directory.exists():
                shutil.rmtree(layout.database_directory)
            self._run(self.index_executable, [])
        except OSError, RecollError:
            self._audit("recoll.rebuild.failed", "failed")
            raise
        self._audit("recoll.rebuild.succeeded", "success")

    def search_paths(self, query: str) -> list[Path]:
        # Recoll's --paths-only emits controlled local paths without human snippets.
        completed = self._run(self.query_executable, ["--paths-only", "--", query])
        layout = managed_layout(self.config)
        paths: list[Path] = []
        for item in completed.stdout.splitlines():
            value = item.strip()
            if value.startswith("file://"):
                value = value[7:]
            path = Path(value).resolve()
            try:
                path.relative_to(layout.top_directory.resolve())
            except ValueError:
                continue
            if path.is_file():
                paths.append(path)
        return sorted(set(paths))

    def _audit(self, event_type: str, result: str) -> None:
        if not self.config.database.path.exists():
            return
        with connect(self.config.database.path) as db:
            insert_audit_event(db, actor="mailarchive.recoll", event_type=event_type, result=result)

    def record_search_failure(self) -> None:
        """Record a bounded derived-search failure without changing canonical state."""
        self._audit("recoll.refresh.failed", "failed")


def search_attachments(
    config: AppConfig, query: str, *, scope: str = "archived"
) -> list[AttachmentSearchResult]:
    """Search derived file candidates then apply authoritative lifecycle filtering in SQLite."""
    adapter = RecollAdapter(config)
    try:
        paths = adapter.search_paths(query)
    except RecollError:
        adapter.record_search_failure()
        raise
    digests = [
        path.name
        for path in paths
        if len(path.name) == 64 and attachment_blob_path(config, path.name) == path
    ]
    return search_attachment_relationships(config, digests, scope)
