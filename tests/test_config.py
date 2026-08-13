from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mailarchive.config import ConfigError, display_config, load_config


def test_valid_example_configuration() -> None:
    config = load_config(Path("config.example.yaml"))
    assert len(config.accounts) == 4


def test_invalid_account_kind_rejected(config_file: Path) -> None:
    values = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    values["accounts"]["test"]["kind"] = "smtp"
    config_file.write_text(yaml.safe_dump(values), encoding="utf-8")
    with pytest.raises(ConfigError, match="kind"):
        load_config(config_file)


def test_deletion_defaults_to_false(config_file: Path) -> None:
    config = load_config(config_file)
    assert config.accounts[0].remote_deletion_enabled is False


def test_deletion_enablement_is_rejected_in_m0(config_file: Path) -> None:
    values = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    values["accounts"]["test"]["remote_deletion_enabled"] = True
    config_file.write_text(yaml.safe_dump(values), encoding="utf-8")
    with pytest.raises(ConfigError, match="unavailable before M12"):
        load_config(config_file)


def test_negative_retention_rejected(config_file: Path) -> None:
    values = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    values["accounts"]["test"]["remote_retention_days"] = -1
    config_file.write_text(yaml.safe_dump(values), encoding="utf-8")
    with pytest.raises(ConfigError, match="remote_retention_days"):
        load_config(config_file)


@pytest.mark.parametrize("value", [0, -1, True, "2"])
def test_required_verified_backups_must_be_positive(config_file: Path, value: object) -> None:
    values = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    values["accounts"]["test"]["required_verified_backups"] = value
    config_file.write_text(yaml.safe_dump(values), encoding="utf-8")
    with pytest.raises(ConfigError, match="required_verified_backups"):
        load_config(config_file)


@pytest.mark.parametrize("value", [0, -1, True, "2"])
def test_retention_default_required_backups_must_be_positive(
    config_file: Path, value: object
) -> None:
    values = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    values["retention"] = {"required_verified_backups_default": value}
    values["accounts"]["test"].pop("required_verified_backups")
    config_file.write_text(yaml.safe_dump(values), encoding="utf-8")
    with pytest.raises(ConfigError, match="required_verified_backups_default"):
        load_config(config_file)


def test_never_delete_is_explicit(config_file: Path) -> None:
    values = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    values["accounts"]["test"]["remote_retention_days"] = "never"
    config_file.write_text(yaml.safe_dump(values), encoding="utf-8")
    assert load_config(config_file).accounts[0].remote_retention_days is None


def test_secret_reference_is_redacted(config_file: Path) -> None:
    displayed = display_config(load_config(config_file))
    assert "MAILARCHIVE_TEST_SECRET" not in str(displayed)
    assert displayed["accounts"][0]["config_ref"] == "<redacted>"  # type: ignore[index]


@pytest.mark.parametrize("account_name", [42, "", ".", "..", "../outside", "a/b", r"a\b", "/tmp/a"])
def test_path_like_account_names_are_rejected(config_file: Path, account_name: object) -> None:
    values = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    account = values["accounts"].pop("test")
    values["accounts"][account_name] = account
    config_file.write_text(yaml.safe_dump(values), encoding="utf-8")
    with pytest.raises(ConfigError, match="account names"):
        load_config(config_file)


@pytest.mark.parametrize(
    "repository_ref",
    [
        "ssh://backup.example.test:2222/./mailarchive",
        "ssh://user@backup.example.test:2222/./mailarchive",
    ],
)
def test_borg_ssh_url_allows_optional_user_and_port(config_file: Path, repository_ref: str) -> None:
    values = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    values["backup"] = {
        "repositories": {
            "remote": {
                "repository_ref": repository_ref,
                "encryption_mode": "repokey-blake2",
                "passphrase_env": "MAILARCHIVE_BORG_TEST",
            }
        }
    }
    config_file.write_text(yaml.safe_dump(values), encoding="utf-8")
    assert load_config(config_file).backup_repositories[0].repository_ref == repository_ref


@pytest.mark.parametrize(
    "repository_ref",
    [
        "ssh://user:password@backup.example.test/repo",
        "ssh://backup.example.test/repo?unsafe=yes",
        "ssh://backup.example.test/repo#unsafe",
    ],
)
def test_borg_ssh_url_rejects_credentials_query_and_fragment(
    config_file: Path, repository_ref: str
) -> None:
    values = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    values["backup"] = {
        "repositories": {
            "remote": {
                "repository_ref": repository_ref,
                "encryption_mode": "repokey-blake2",
                "passphrase_env": "MAILARCHIVE_BORG_TEST",
            }
        }
    }
    config_file.write_text(yaml.safe_dump(values), encoding="utf-8")
    with pytest.raises(ConfigError, match="password-free"):
        load_config(config_file)
