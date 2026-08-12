from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mailarchive.borg import backup_run, repo_init, restore_test, verify_run
from mailarchive.config import load_config
from mailarchive.db import connect
from mailarchive.ingest import ingest_file


def test_real_borg_backup_verify_and_restore(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disposable Borg 1.x acceptance: create is unverified until exact verification."""
    raw = yaml.safe_load(config_file.read_text())
    raw["backup"] = {
        "repositories": {
            "local": {
                "kind": "borg",
                "enabled": True,
                "repository_ref": str(tmp_path / "borg-repository"),
                "encryption_mode": "repokey-blake2",
                "passphrase_env": "MAILARCHIVE_TEST_BORG_PASSPHRASE",
                "verification_policy": "borg-archive-data-v1",
                "command_timeout_seconds": 60,
            }
        }
    }
    config_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("MAILARCHIVE_TEST_BORG_PASSPHRASE", "disposable-test-passphrase")
    config = load_config(config_file)
    source = tmp_path / "message.eml"
    source.write_bytes(b"From: sender@example.test\r\nSubject: borg\r\n\r\nbody\r\n")
    message = ingest_file(config, source, "test").canonical_message
    assert repo_init(config, "local")
    run_id = backup_run(config, "local")
    with connect(config.database.path) as db:
        assert (
            db.execute(
                "SELECT verification_status FROM backup_runs WHERE id=?", (run_id,)
            ).fetchone()[0]
            == "unverified"
        )
    verify_run(config, run_id)
    restored = tmp_path / "restore"
    restore_test(config, run_id, restored)
    assert (
        restored / message.local_path.resolve().relative_to(config.archive.root.resolve())
    ).read_bytes() == source.read_bytes()


def test_missing_passphrase_fails_before_borg(config_file: Path, tmp_path: Path) -> None:
    raw = yaml.safe_load(config_file.read_text())
    raw["backup"] = {
        "repositories": {
            "local": {
                "repository_ref": str(tmp_path / "repo"),
                "encryption_mode": "repokey",
                "passphrase_env": "MISSING_BORG_PASSPHRASE",
            }
        }
    }
    config_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
    config = load_config(config_file)
    from mailarchive.borg import BorgAdapter, BorgError

    try:
        BorgAdapter(config, config.backup_repositories[0]).version()
    except BorgError as error:
        assert error.kind == "passphrase-missing"
    else:
        raise AssertionError("missing passphrase must fail")
