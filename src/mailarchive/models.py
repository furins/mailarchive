"""Typed configuration and canonical-message models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

AccountKind = Literal["imap", "gmail", "pop3"]
TlsMode = Literal["IMAPS", "STARTTLS", "INSECURE_LOOPBACK"]
Pop3TlsMode = Literal["POP3S", "STARTTLS", "INSECURE_LOOPBACK"]
BackupEncryptionMode = Literal["repokey", "repokey-blake2", "none"]


@dataclass(frozen=True)
class ArchiveConfig:
    root: Path
    timezone: str


@dataclass(frozen=True)
class DatabaseConfig:
    path: Path


@dataclass(frozen=True)
class ImapConfig:
    host: str
    port: int
    username: str
    tls_mode: TlsMode
    folders: tuple[str, ...]
    connection_timeout_seconds: int
    fast_path: FastPathConfig


@dataclass(frozen=True)
class FastPathConfig:
    idle_enabled: bool = True
    reconcile_interval_seconds: int = 600
    poll_interval_seconds: int = 90


@dataclass(frozen=True)
class GmailConfig:
    """Read-only Gmail API configuration; secrets are always file references."""

    account_email: str
    oauth_client_secret_file: Path
    poll_interval_seconds: int = 90
    remote_delete_token_file: Path | None = None


@dataclass(frozen=True)
class Pop3Config:
    """POP3 connection facts. Credentials remain an external config reference."""

    host: str
    port: int
    username: str
    tls_mode: Pop3TlsMode
    connection_timeout_seconds: int


@dataclass(frozen=True)
class BackupRepositoryConfig:
    """A named Borg 1.x destination; credentials remain environment references."""

    name: str
    enabled: bool
    repository_ref: str
    encryption_mode: BackupEncryptionMode
    passphrase_env: str | None
    verification_policy: str
    command_timeout_seconds: int


@dataclass(frozen=True)
class AccountConfig:
    name: str
    kind: AccountKind
    enabled: bool
    remote_retention_days: int | None
    remote_deletion_enabled: bool
    required_verified_backups: int
    config_ref: str
    imap: ImapConfig | None = None
    gmail: GmailConfig | None = None
    pop3: Pop3Config | None = None


@dataclass(frozen=True)
class RetentionConfig:
    """Defaults used to resolve per-account local retention policy."""

    remote_retention_days_default: int = 365
    required_verified_backups_default: int = 2


@dataclass(frozen=True)
class RemoteDeletionConfig:
    """M12-A production-execution limits; provider wiring remains unavailable."""

    max_per_run: int = 10
    max_per_account: int = 10


@dataclass(frozen=True)
class AppConfig:
    archive: ArchiveConfig
    database: DatabaseConfig
    accounts: tuple[AccountConfig, ...]
    backup_repositories: tuple[BackupRepositoryConfig, ...] = ()
    retention: RetentionConfig = RetentionConfig()
    remote_deletion: RemoteDeletionConfig = RemoteDeletionConfig()


@dataclass(frozen=True)
class CanonicalMessage:
    """Inventory metadata for one immutable, byte-identified message."""

    id: str
    account_id: int
    sha256: str
    local_path: Path
    size_bytes: int
    message_id_header: str | None
    message_date: str | None
    downloaded_at: str
    archived_at: str | None
    integrity_status: str
    integrity_verified_at: str | None
    created_at: str
    storage_state: Literal["pending", "archived", "quarantined"] = "archived"
    quarantined_at: str | None = None
