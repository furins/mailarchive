"""Typed configuration models used by the M0 safety baseline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

AccountKind = Literal["imap", "gmail", "pop3"]


@dataclass(frozen=True)
class ArchiveConfig:
    root: Path
    timezone: str


@dataclass(frozen=True)
class DatabaseConfig:
    path: Path


@dataclass(frozen=True)
class AccountConfig:
    name: str
    kind: AccountKind
    enabled: bool
    remote_retention_days: int | None
    remote_deletion_enabled: bool
    required_verified_backups: int
    config_ref: str


@dataclass(frozen=True)
class AppConfig:
    archive: ArchiveConfig
    database: DatabaseConfig
    accounts: tuple[AccountConfig, ...]
