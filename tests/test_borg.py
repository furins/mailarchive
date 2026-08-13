from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

import pytest
import yaml

from mailarchive.borg import BorgAdapter, BorgError, backup_run, repo_init, restore_test, verify_run
from mailarchive.classification import ClassificationResult, apply_classification
from mailarchive.config import load_config
from mailarchive.db import connect
from mailarchive.ingest import ingest_file
from mailarchive.models import AppConfig, BackupRepositoryConfig


def _repositories(tmp_path: Path, names: tuple[str, ...] = ("local",)) -> dict[str, object]:
    return {
        "repositories": {
            name: {
                "kind": "borg",
                "enabled": True,
                "repository_ref": str(tmp_path / f"borg-{name}"),
                "encryption_mode": "repokey-blake2",
                "passphrase_env": "MAILARCHIVE_TEST_BORG_PASSPHRASE",
                "verification_policy": "borg-archive-data-v1",
                "command_timeout_seconds": 60,
            }
            for name in names
        }
    }


def test_real_borg_backup_verify_and_restore(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disposable Borg 1.x acceptance: create is unverified until exact verification."""
    raw = yaml.safe_load(config_file.read_text())
    raw["backup"] = _repositories(tmp_path)
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
    try:
        BorgAdapter(config, config.backup_repositories[0]).version()
    except BorgError as error:
        assert error.kind == "passphrase-missing"
    else:
        raise AssertionError("missing passphrase must fail")


@pytest.mark.parametrize(
    ("output", "accepted"),
    [("borg 1.2.7", False), ("borg 1.2.8", True), ("borg 1.4.1", True), ("borg 2.0.0", False)],
)
def test_borg_version_floor(
    config_file: Path, output: str, accepted: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(config_file)
    repository = BackupRepositoryConfig(
        "version", True, "/tmp/version-repo", "none", None, "borg-archive-data-v1", 60
    )
    adapter = BorgAdapter(config, repository)

    def command(_args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, output, "")

    monkeypatch.setattr(
        adapter,
        "command",
        command,
    )
    if accepted:
        assert adapter.version() == output
    else:
        with pytest.raises(BorgError, match="M9 requires"):
            adapter.version()


def test_reverification_parser_failure_revokes_evidence(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = yaml.safe_load(config_file.read_text())
    raw["backup"] = _repositories(tmp_path)
    config_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("MAILARCHIVE_TEST_BORG_PASSPHRASE", "disposable-test-passphrase")
    config = load_config(config_file)
    source = tmp_path / "message.eml"
    source.write_bytes(b"Subject: parser\r\n\r\nbody\r\n")
    ingest_file(config, source, "test")
    repo_init(config, "local")
    run_id = backup_run(config, "local")
    verify_run(config, run_id)

    def invalid_inventory(_adapter: BorgAdapter, _archive_name: str) -> dict[str, tuple[int, str]]:
        return cast(dict[str, tuple[int, str]], {"bad": ("x", "y")})

    monkeypatch.setattr(BorgAdapter, "inventory", invalid_inventory)
    with pytest.raises(BorgError):
        verify_run(config, run_id)
    with connect(config.database.path) as db:
        row = db.execute(
            "SELECT verification_status,verified_at FROM backup_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert tuple(row) == ("failed", None)
        assert (
            db.execute(
                "SELECT verified FROM message_backup_evidence WHERE backup_run_id=?", (run_id,)
            ).fetchone()[0]
            == 0
        )


def test_backup_local_failure_finalizes_running_row(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = yaml.safe_load(config_file.read_text())
    raw["backup"] = _repositories(tmp_path)
    config_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("MAILARCHIVE_TEST_BORG_PASSPHRASE", "disposable-test-passphrase")
    config = load_config(config_file)
    repo_init(config, "local")

    def failing_snapshot(_config: AppConfig, _run_id: str) -> tuple[Path, list[str], str]:
        raise OSError("fixture")

    monkeypatch.setattr("mailarchive.borg._build_snapshot", failing_snapshot)
    with pytest.raises(BorgError) as raised:
        backup_run(config, "local")
    assert raised.value.kind == "local-operation"
    with connect(config.database.path) as db:
        assert db.execute(
            "SELECT status,verification_status,last_error_kind,completed_at FROM backup_runs"
        ).fetchone()[0:3] == ("failed", "failed", "local-operation")
        assert db.execute("SELECT completed_at FROM backup_runs").fetchone()[0] is not None


def test_restore_identity_mismatch_is_recorded(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = yaml.safe_load(config_file.read_text())
    raw["backup"] = _repositories(tmp_path)
    config_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("MAILARCHIVE_TEST_BORG_PASSPHRASE", "disposable-test-passphrase")
    config = load_config(config_file)
    source = tmp_path / "message.eml"
    source.write_bytes(b"Subject: restore identity\r\n\r\nbody\r\n")
    ingest_file(config, source, "test")
    repo_init(config, "local")
    run_id = backup_run(config, "local")
    with connect(config.database.path) as db:
        db.execute(
            "UPDATE backup_repositories "
            "SET repository_identity='0' || substr(repository_identity,2)"
        )
        db.commit()
    with pytest.raises(BorgError, match="Borg operation failed"):
        restore_test(config, run_id, tmp_path / "restore-mismatch")
    with connect(config.database.path) as db:
        row = db.execute("SELECT status,error_kind FROM backup_restore_tests").fetchone()
        assert tuple(row) == (
            "failed",
            "repository-identity-mismatch",
        )


def test_two_real_repositories_are_independent_and_pending_is_excluded(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = yaml.safe_load(config_file.read_text())
    raw["backup"] = _repositories(tmp_path, ("first", "second"))
    config_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("MAILARCHIVE_TEST_BORG_PASSPHRASE", "disposable-test-passphrase")
    config = load_config(config_file)
    archived_file, quarantined_file, pending_file = (
        tmp_path / name for name in ("archived.eml", "quarantined.eml", "pending.eml")
    )
    for file, subject in (
        (archived_file, b"archived"),
        (quarantined_file, b"quarantined"),
        (pending_file, b"pending"),
    ):
        file.write_bytes(b"Subject: " + subject + b"\r\n\r\nbody\r\n")
    archived = ingest_file(config, archived_file, "test").canonical_message
    quarantined = ingest_file(config, quarantined_file, "test").canonical_message
    apply_classification(
        config, quarantined, ClassificationResult("spam", None, "fixture", "pytest")
    )
    pending = ingest_file(config, pending_file, "test").canonical_message
    with connect(config.database.path) as db:
        db.execute(
            "UPDATE canonical_messages SET storage_state='pending',archived_at=NULL WHERE id=?",
            (pending.id,),
        )
        db.commit()
    runs: list[str] = []
    for name in ("first", "second"):
        repo_init(config, name)
        run_id = backup_run(config, name)
        verify_run(config, run_id)
        runs.append(run_id)
    with connect(config.database.path) as db:
        rows = db.execute(
            """SELECT e.canonical_message_id,e.verified,r.repository_id
            FROM message_backup_evidence e JOIN backup_runs r ON r.id=e.backup_run_id
            ORDER BY e.canonical_message_id,r.repository_id"""
        ).fetchall()
        assert {(row[0], row[1]) for row in rows} == {(archived.id, 1), (quarantined.id, 1)}
        assert len({row[2] for row in rows}) == 2
        assert (
            db.execute(
                "SELECT COUNT(*) FROM message_backup_evidence WHERE canonical_message_id=?",
                (pending.id,),
            ).fetchone()[0]
            == 0
        )
