from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mailarchive.config import ConfigError, display_config, load_config


def test_valid_example_configuration() -> None:
    config = load_config(Path("config.example.yaml"))
    assert len(config.accounts) == 3


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
    with pytest.raises(ConfigError, match="unsupported in M0"):
        load_config(config_file)


def test_negative_retention_rejected(config_file: Path) -> None:
    values = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    values["accounts"]["test"]["remote_retention_days"] = -1
    config_file.write_text(yaml.safe_dump(values), encoding="utf-8")
    with pytest.raises(ConfigError, match="remote_retention_days"):
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
