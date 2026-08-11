"""Fail-closed YAML configuration loading and validation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from mailarchive.models import AccountConfig, AppConfig, ArchiveConfig, DatabaseConfig
from mailarchive.safety import REMOTE_DELETION_DEFAULT, redact_secret_reference


class ConfigError(ValueError):
    """Raised when configuration cannot safely be used."""


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be a mapping")
    return cast(Mapping[str, object], value)


def _required_string(values: Mapping[str, object], field: str, label: str) -> str:
    value = values.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label}.{field} must be a non-empty string")
    return value


def _required_bool(values: Mapping[str, object], field: str, label: str, default: bool) -> bool:
    value = values.get(field, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{label}.{field} must be a boolean")
    return value


def _required_nonnegative_int(values: Mapping[str, object], field: str, label: str) -> int:
    value = values.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{label}.{field} must be a non-negative integer")
    return value


def _retention_days(value: object, label: str) -> int | None:
    if value == "never":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(
            f"{label}.remote_retention_days must be a non-negative integer or 'never'"
        )
    return value


def load_config(path: Path) -> AppConfig:
    """Load a configuration file; absent or malformed safety settings are rejected."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"cannot read configuration: {path}") from error
    except yaml.YAMLError as error:
        raise ConfigError("configuration is not valid YAML") from error
    values = _mapping(raw, "configuration")
    archive_values = _mapping(values.get("archive"), "archive")
    database_values = _mapping(values.get("database"), "database")
    account_values = _mapping(values.get("accounts"), "accounts")
    timezone = _required_string(archive_values, "timezone", "archive")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ConfigError(f"archive.timezone is invalid: {timezone}") from error
    accounts: list[AccountConfig] = []
    for name, raw_account in account_values.items():
        if not name:
            raise ConfigError("account names must be non-empty strings")
        label = f"accounts.{name}"
        account = _mapping(raw_account, label)
        kind = _required_string(account, "kind", label)
        if kind not in {"imap", "gmail", "pop3"}:
            raise ConfigError(f"{label}.kind must be one of: imap, gmail, pop3")
        remote_deletion_enabled = _required_bool(
            account, "remote_deletion_enabled", label, REMOTE_DELETION_DEFAULT
        )
        if remote_deletion_enabled:
            raise ConfigError(f"{label}.remote_deletion_enabled is unsupported in M0")
        accounts.append(
            AccountConfig(
                name=name,
                kind=cast("Any", kind),
                enabled=_required_bool(account, "enabled", label, True),
                remote_retention_days=_retention_days(account.get("remote_retention_days"), label),
                remote_deletion_enabled=remote_deletion_enabled,
                required_verified_backups=_required_nonnegative_int(
                    account, "required_verified_backups", label
                ),
                config_ref=_required_string(account, "config_ref", label),
            )
        )
    if not accounts:
        raise ConfigError("accounts must contain at least one account")
    return AppConfig(
        archive=ArchiveConfig(
            root=Path(_required_string(archive_values, "root", "archive")), timezone=timezone
        ),
        database=DatabaseConfig(path=Path(_required_string(database_values, "path", "database"))),
        accounts=tuple(accounts),
    )


def display_config(config: AppConfig) -> dict[str, object]:
    """Return safe summary data suitable for human or JSON output."""
    return {
        "archive_root": str(config.archive.root),
        "timezone": config.archive.timezone,
        "database_path": str(config.database.path),
        "accounts": [
            {
                "name": account.name,
                "kind": account.kind,
                "enabled": account.enabled,
                "remote_retention_days": account.remote_retention_days,
                "remote_deletion_enabled": account.remote_deletion_enabled,
                "required_verified_backups": account.required_verified_backups,
                "config_ref": redact_secret_reference(account.config_ref),
            }
            for account in config.accounts
        ],
    }
