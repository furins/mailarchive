"""Fail-closed YAML configuration loading and validation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from re import fullmatch
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from mailarchive.models import AccountConfig, AppConfig, ArchiveConfig, DatabaseConfig, ImapConfig
from mailarchive.safety import REMOTE_DELETION_DEFAULT, redact_secret_reference


class ConfigError(ValueError):
    """Raised when configuration cannot safely be used."""


def _mapping(value: object, label: str) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be a mapping")
    return cast(Mapping[object, object], value)


def _required_string(values: Mapping[object, object], field: str, label: str) -> str:
    value = values.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label}.{field} must be a non-empty string")
    return value


def _required_bool(values: Mapping[object, object], field: str, label: str, default: bool) -> bool:
    value = values.get(field, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{label}.{field} must be a boolean")
    return value


def _required_nonnegative_int(values: Mapping[object, object], field: str, label: str) -> int:
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


def _account_name(value: object) -> str:
    """Validate a portable single-component Maildir account name."""
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ConfigError("account names must be non-empty safe strings")
    if "/" in value or "\\" in value or Path(value).is_absolute():
        raise ConfigError("account names must not be paths")
    if fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) is None:
        raise ConfigError("account names must use only letters, digits, '.', '_' or '-'")
    return value


def _imap_config(account: Mapping[object, object], label: str) -> ImapConfig | None:
    raw = account.get("imap")
    if raw is None:
        return None
    values = _mapping(raw, f"{label}.imap")
    host = _required_string(values, "host", f"{label}.imap")
    port = _required_nonnegative_int(values, "port", f"{label}.imap")
    if not 1 <= port <= 65535:
        raise ConfigError(f"{label}.imap.port must be between 1 and 65535")
    tls_mode = _required_string(values, "tls_mode", f"{label}.imap")
    if tls_mode not in {"IMAPS", "STARTTLS", "INSECURE_LOOPBACK"}:
        raise ConfigError(f"{label}.imap.tls_mode must be IMAPS, STARTTLS, or INSECURE_LOOPBACK")
    if tls_mode == "INSECURE_LOOPBACK" and host.lower() not in {"localhost", "127.0.0.1", "::1"}:
        raise ConfigError(f"{label}.imap insecure mode is permitted only for a loopback host")
    folders_raw = values.get("folders", ["INBOX"])
    if not isinstance(folders_raw, list):
        raise ConfigError(f"{label}.imap.folders must be a non-empty list of remote folder names")
    folder_values = cast(list[object], folders_raw)
    if not folder_values or not all(
        isinstance(folder, str)
        and folder
        and folder.isascii()
        and not any(c in folder for c in "\x00\r\n")
        for folder in folder_values
    ):
        raise ConfigError(
            f"{label}.imap.folders must be non-empty ASCII remote folder names "
            "without CR, LF, or NUL"
        )
    folders_list = cast(list[str], folder_values)
    folders = tuple(folders_list)
    if len(set(folders)) != len(folders):
        raise ConfigError(f"{label}.imap.folders must not contain duplicates")
    if "sync_timeout_seconds" in values:
        raise ConfigError(
            f"{label}.imap.sync_timeout_seconds is unsupported for direct IMAP acquisition"
        )
    connect_timeout = (
        _required_nonnegative_int(values, "connection_timeout_seconds", f"{label}.imap")
        if "connection_timeout_seconds" in values
        else 60
    )
    if connect_timeout == 0:
        raise ConfigError(f"{label}.imap.connection_timeout_seconds must be positive")
    return ImapConfig(
        host=host,
        port=port,
        username=_required_string(values, "username", f"{label}.imap"),
        tls_mode=cast("Any", tls_mode),
        folders=folders,
        connection_timeout_seconds=connect_timeout,
    )


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
    for raw_name, raw_account in account_values.items():
        name = _account_name(raw_name)
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
                imap=_imap_config(account, label) if kind == "imap" else None,
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
                "imap": None
                if account.imap is None
                else {
                    "host": account.imap.host,
                    "port": account.imap.port,
                    "username": account.imap.username,
                    "tls_mode": account.imap.tls_mode,
                    "folders": list(account.imap.folders),
                },
            }
            for account in config.accounts
        ],
    }
