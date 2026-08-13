"""Fail-closed YAML configuration loading and validation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from re import fullmatch
from typing import Any, cast
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from mailarchive.models import (
    AccountConfig,
    AppConfig,
    ArchiveConfig,
    BackupRepositoryConfig,
    DatabaseConfig,
    FastPathConfig,
    GmailConfig,
    ImapConfig,
    Pop3Config,
)
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
    fast_path_raw = values.get("fast_path", {})
    fast_path_values = _mapping(fast_path_raw, f"{label}.imap.fast_path")
    idle_enabled = _required_bool(fast_path_values, "idle_enabled", f"{label}.imap.fast_path", True)
    reconcile = fast_path_values.get("reconcile_interval_seconds", 600)
    poll = fast_path_values.get("poll_interval_seconds", 90)
    if isinstance(reconcile, bool) or not isinstance(reconcile, int) or not 60 <= reconcile <= 1740:
        raise ConfigError(f"{label}.imap.fast_path.reconcile_interval_seconds must be 60..1740")
    if isinstance(poll, bool) or not isinstance(poll, int) or not 60 <= poll <= 120:
        raise ConfigError(f"{label}.imap.fast_path.poll_interval_seconds must be 60..120")
    return ImapConfig(
        host=host,
        port=port,
        username=_required_string(values, "username", f"{label}.imap"),
        tls_mode=cast("Any", tls_mode),
        folders=folders,
        connection_timeout_seconds=connect_timeout,
        fast_path=FastPathConfig(idle_enabled, reconcile, poll),
    )


def _absolute_secret_path(value: str, label: str, archive_root: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ConfigError(f"{label} must be an absolute path")
    # resolve(strict=False) also catches a path through an existing symlink.
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(archive_root.resolve(strict=False))
    except ValueError:
        return resolved
    raise ConfigError(f"{label} must not be inside archive.root")


def _gmail_config(account: Mapping[object, object], label: str, archive_root: Path) -> GmailConfig:
    if account.get("imap") is not None:
        raise ConfigError(f"{label}.imap is not permitted for kind=gmail")
    values = _mapping(account.get("gmail"), f"{label}.gmail")
    email = _required_string(values, "account_email", f"{label}.gmail")
    client_file = _absolute_secret_path(
        _required_string(values, "oauth_client_secret_file", f"{label}.gmail"),
        f"{label}.gmail.oauth_client_secret_file",
        archive_root,
    )
    poll = values.get("poll_interval_seconds", 90)
    if isinstance(poll, bool) or not isinstance(poll, int) or not 60 <= poll <= 120:
        raise ConfigError(f"{label}.gmail.poll_interval_seconds must be 60..120")
    return GmailConfig(email, client_file, poll)


def _pop3_config(account: Mapping[object, object], label: str) -> Pop3Config:
    if account.get("imap") is not None or account.get("gmail") is not None:
        raise ConfigError(f"{label} may not combine pop3 with another provider configuration")
    values = _mapping(account.get("pop3"), f"{label}.pop3")
    host = _required_string(values, "host", f"{label}.pop3")
    port = values.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError(f"{label}.pop3.port must be 1..65535")
    tls_mode = values.get("tls_mode")
    if tls_mode not in {"POP3S", "STARTTLS", "INSECURE_LOOPBACK"}:
        raise ConfigError(f"{label}.pop3.tls_mode must be POP3S, STARTTLS, or INSECURE_LOOPBACK")
    if tls_mode == "INSECURE_LOOPBACK" and host not in {"localhost", "127.0.0.1", "::1"}:
        raise ConfigError(f"{label}.pop3 INSECURE_LOOPBACK host must be loopback")
    timeout = values.get("connection_timeout_seconds", 30)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 300:
        raise ConfigError(f"{label}.pop3.connection_timeout_seconds must be 1..300")
    return Pop3Config(
        host=host,
        port=port,
        username=_required_string(values, "username", f"{label}.pop3"),
        tls_mode=cast("Any", tls_mode),
        connection_timeout_seconds=timeout,
    )


def _backup_repositories(
    values: Mapping[object, object], archive_root: Path
) -> tuple[BackupRepositoryConfig, ...]:
    backup = values.get("backup", {})
    backup_values = _mapping(backup, "backup")
    raw_repositories = backup_values.get("repositories", {})
    repositories = _mapping(raw_repositories, "backup.repositories")
    result: list[BackupRepositoryConfig] = []
    for raw_name, raw_repository in repositories.items():
        name = _account_name(raw_name)
        label = f"backup.repositories.{name}"
        item = _mapping(raw_repository, label)
        kind = item.get("kind", "borg")
        if kind != "borg":
            raise ConfigError(f"{label}.kind must be borg")
        ref = _required_string(item, "repository_ref", label)
        if any(char in ref for char in "\x00\r\n"):
            raise ConfigError(f"{label}.repository_ref must not contain CR, LF, or NUL")
        if ref.startswith("ssh://"):
            parsed = urlparse(ref)
            if (
                parsed.scheme != "ssh"
                or not parsed.hostname
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ConfigError(f"{label}.repository_ref must be a password-free ssh:// Borg URL")
            try:
                port = parsed.port
            except ValueError as error:
                raise ConfigError(f"{label}.repository_ref has an invalid ssh port") from error
            if port is not None and not 1 <= port <= 65535:
                raise ConfigError(f"{label}.repository_ref has an invalid ssh port")
            if not parsed.path:
                raise ConfigError(f"{label}.repository_ref must not embed a password")
        else:
            repository_path = Path(ref)
            if not repository_path.is_absolute():
                raise ConfigError(f"{label}.repository_ref must be an absolute path or ssh:// URL")
            try:
                repository_path.resolve(strict=False).relative_to(
                    archive_root.resolve(strict=False)
                )
            except ValueError:
                pass
            else:
                raise ConfigError(f"{label}.repository_ref must be outside archive.root")
        encryption = _required_string(item, "encryption_mode", label)
        if encryption not in {"repokey", "repokey-blake2", "none"}:
            raise ConfigError(f"{label}.encryption_mode is unsupported")
        passphrase_env = item.get("passphrase_env")
        if encryption != "none":
            if not isinstance(passphrase_env, str) or not fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", passphrase_env
            ):
                raise ConfigError(f"{label}.passphrase_env must name an environment variable")
        elif passphrase_env is not None:
            raise ConfigError(f"{label}.passphrase_env is only valid for encrypted repositories")
        timeout = item.get("command_timeout_seconds", 21600)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 86400:
            raise ConfigError(f"{label}.command_timeout_seconds must be 1..86400")
        policy = item.get("verification_policy", "borg-archive-data-v1")
        if policy != "borg-archive-data-v1":
            raise ConfigError(f"{label}.verification_policy must be borg-archive-data-v1")
        assert isinstance(policy, str)
        result.append(
            BackupRepositoryConfig(
                name,
                _required_bool(item, "enabled", label, True),
                ref,
                cast("Any", encryption),
                passphrase_env if isinstance(passphrase_env, str) else None,
                policy,
                timeout,
            )
        )
    return tuple(result)


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
    archive_root = Path(_required_string(archive_values, "root", "archive"))
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
        config_ref = _required_string(account, "config_ref", label)
        gmail = None
        pop3 = None
        if kind == "gmail":
            if not config_ref.startswith("file:"):
                raise ConfigError(f"{label}.config_ref must use file: for Gmail")
            _absolute_secret_path(config_ref[5:], f"{label}.config_ref", archive_root)
            gmail = _gmail_config(account, label, archive_root)
        if kind == "pop3":
            pop3 = _pop3_config(account, label)
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
                config_ref=config_ref,
                imap=_imap_config(account, label) if kind == "imap" else None,
                gmail=gmail,
                pop3=pop3,
            )
        )
    if not accounts:
        raise ConfigError("accounts must contain at least one account")
    return AppConfig(
        archive=ArchiveConfig(root=archive_root, timezone=timezone),
        database=DatabaseConfig(path=Path(_required_string(database_values, "path", "database"))),
        accounts=tuple(accounts),
        backup_repositories=_backup_repositories(values, archive_root),
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
                    "fast_path": {
                        "idle_enabled": account.imap.fast_path.idle_enabled,
                        "reconcile_interval_seconds": (
                            account.imap.fast_path.reconcile_interval_seconds
                        ),
                        "poll_interval_seconds": account.imap.fast_path.poll_interval_seconds,
                    },
                },
                "gmail": None
                if account.gmail is None
                else {
                    "account_email": account.gmail.account_email,
                    "oauth_client_secret_file": str(account.gmail.oauth_client_secret_file),
                    "poll_interval_seconds": account.gmail.poll_interval_seconds,
                },
                "pop3": None
                if account.pop3 is None
                else {
                    "host": account.pop3.host,
                    "port": account.pop3.port,
                    "username": account.pop3.username,
                    "tls_mode": account.pop3.tls_mode,
                    "connection_timeout_seconds": account.pop3.connection_timeout_seconds,
                },
            }
            for account in config.accounts
        ],
        "backup_repositories": [
            {
                "name": item.name,
                "enabled": item.enabled,
                "repository_ref": item.repository_ref,
                "encryption_mode": item.encryption_mode,
                "passphrase_env": item.passphrase_env,
                "verification_policy": item.verification_policy,
                "command_timeout_seconds": item.command_timeout_seconds,
            }
            for item in config.backup_repositories
        ],
    }
