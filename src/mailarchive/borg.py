"""Narrow, fail-closed Borg 1.x backup support.

This module deliberately exposes no Borg maintenance or destructive commands.
"""
# ruff: noqa: E501

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from mailarchive.db import connect, initialize, utc_now
from mailarchive.models import AppConfig, BackupRepositoryConfig

CONTROL_ENV = {
    "BORG_REPO",
    "BORG_PASSPHRASE",
    "BORG_PASSCOMMAND",
    "BORG_RSH",
    "BORG_REMOTE_PATH",
    "BORG_CACHE_DIR",
    "BORG_SECURITY_DIR",
    "BORG_CONFIG_DIR",
}


class BorgError(RuntimeError):
    def __init__(self, kind: str, message: str = "Borg operation failed") -> None:
        self.kind = kind
        super().__init__(message)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


class BorgAdapter:
    def __init__(self, config: AppConfig, repository: BackupRepositoryConfig) -> None:
        self.config, self.repository = config, repository
        self.state = config.archive.root.resolve() / "state" / "borg" / repository.name

    def _environment(self) -> dict[str, str]:
        environment = {key: value for key, value in os.environ.items() if key not in CONTROL_ENV}
        cache, security = self.state / "cache", self.state / "security"
        cache.mkdir(parents=True, exist_ok=True)
        security.mkdir(parents=True, exist_ok=True)
        environment.update({"BORG_CACHE_DIR": str(cache), "BORG_SECURITY_DIR": str(security)})
        if self.repository.passphrase_env:
            passphrase = os.environ.get(self.repository.passphrase_env)
            if not passphrase:
                raise BorgError("passphrase-missing", "configured Borg passphrase is unavailable")
            environment["BORG_PASSPHRASE"] = passphrase
        return environment

    def command(
        self, args: list[str], *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["borg", *args],
                shell=False,
                cwd=str(cwd or self.config.archive.root),
                env=self._environment(),
                text=True,
                capture_output=True,
                timeout=self.repository.command_timeout_seconds,
                check=False,
            )
        except FileNotFoundError as error:
            raise BorgError("borg-unavailable") from error
        except subprocess.TimeoutExpired as error:
            raise BorgError("timeout") from error
        if result.returncode != 0:
            raise BorgError("borg-nonzero", f"Borg exited {result.returncode}")
        return result

    def version(self) -> str:
        result = self.command(["--version"])
        version = result.stdout.strip()
        if not version.startswith("borg 1."):
            raise BorgError("unsupported-version", "M9 requires Borg 1.x >= 1.2.8")
        pieces = version.split()[1].split(".")
        if len(pieces) < 2 or int(pieces[1]) < 2:
            raise BorgError("unsupported-version", "M9 requires Borg 1.x >= 1.2.8")
        return version

    def identity(self) -> str:
        result = self.command(["info", "--json", self.repository.repository_ref])
        try:
            identity = str(json.loads(result.stdout)["repository"]["id"])
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise BorgError("info-parse") from error
        if len(identity) != 64:
            raise BorgError("info-parse")
        return identity

    def init(self) -> str:
        self.version()
        try:
            identity = self.identity()
        except BorgError as error:
            if error.kind not in {"borg-nonzero", "info-parse"}:
                raise
            self.command(
                [
                    "init",
                    f"--encryption={self.repository.encryption_mode}",
                    self.repository.repository_ref,
                ]
            )
            identity = self.identity()
        return identity

    def create(self, archive_name: str, snapshot: Path) -> None:
        self.command(
            ["create", "--json", f"{self.repository.repository_ref}::{archive_name}", "."],
            cwd=snapshot,
        )

    def manifest_bytes(self, archive_name: str) -> bytes:
        result = self.command(
            [
                "extract",
                "--stdout",
                f"{self.repository.repository_ref}::{archive_name}",
                "metadata/backup-manifest.jsonl",
            ]
        )
        return result.stdout.encode()

    def inventory(self, archive_name: str) -> dict[str, tuple[int, str]]:
        # Borg 1.x JSON-lines emits type/path/size but no file digest.  Its documented
        # list formatter's {sha256} supplies the digest, paired here with a safe delimiter.
        result = self.command(
            [
                "list",
                "--format",
                "{path}\x1f{size}\x1f{sha256}{NL}",
                f"{self.repository.repository_ref}::{archive_name}",
            ]
        )
        output: dict[str, tuple[int, str]] = {}
        for line in result.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 3:
                raise BorgError("inventory-parse")
            path, size, digest = parts
            if digest:  # directories intentionally have no sha256
                output[path] = (int(size), digest)
        return output

    def verify_data(self, archive_name: str) -> None:
        self.command(
            [
                "check",
                "--archives-only",
                "--verify-data",
                f"{self.repository.repository_ref}::{archive_name}",
            ]
        )

    def extract(self, archive_name: str, destination: Path) -> None:
        self.command(
            ["extract", f"{self.repository.repository_ref}::{archive_name}"], cwd=destination
        )


def _repository(config: AppConfig, name: str) -> BackupRepositoryConfig:
    item = next((value for value in config.backup_repositories if value.name == name), None)
    if item is None:
        raise BorgError("repository-unknown")
    return item


@contextmanager
def _lock(config: AppConfig, name: str) -> Generator[None]:
    lock_path = config.archive.root / "state" / "locks" / f"borg-{name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BorgError("concurrent-run") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _snapshot_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as live, sqlite3.connect(destination) as snapshot:
        live.backup(snapshot)
    with sqlite3.connect(destination) as snapshot:
        if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BorgError("sqlite-snapshot")
        # A snapshot must be one explicit regular file; never accidentally archive WAL state.
        snapshot.execute("PRAGMA journal_mode=DELETE")
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{destination}{suffix}")
        if sidecar.exists():
            sidecar.unlink()


def _copy_verified(source: Path, destination: Path, digest: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        with source.open("rb") as input_stream, destination.open("wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    if _sha256(destination) != digest:
        raise BorgError("snapshot-sha-mismatch")


def _build_snapshot(config: AppConfig, run_id: str) -> tuple[Path, list[str], str]:
    root = config.archive.root.resolve()
    snapshot = root / "state" / "backup-snapshots" / run_id
    sqlite_path = snapshot / "state" / "mailarchive.sqlite3"
    snapshot.mkdir(parents=True, exist_ok=False)
    _snapshot_database(config.database.path, sqlite_path)
    entries: list[dict[str, object]] = []
    canonical_ids: list[str] = []
    with sqlite3.connect(sqlite_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT id,sha256,size_bytes,local_path,storage_state FROM canonical_messages WHERE storage_state IN ('archived','quarantined') ORDER BY id"
        ).fetchall()
        for row in rows:
            path = Path(str(row["local_path"])).resolve(strict=False)
            allowed = root / ("mail" if row["storage_state"] == "archived" else "quarantine")
            if not _inside(path, allowed) or not path.is_file() or _sha256(path) != row["sha256"]:
                raise BorgError("canonical-invalid")
            relative = path.relative_to(root).as_posix()
            _copy_verified(path, snapshot / relative, str(row["sha256"]))
            entries.append(
                {
                    "kind": "canonical",
                    "canonical_id": row["id"],
                    "sha256": row["sha256"],
                    "size_bytes": row["size_bytes"],
                    "storage_state": row["storage_state"],
                    "path": relative,
                }
            )
            canonical_ids.append(str(row["id"]))
        attachments = db.execute("""SELECT DISTINCT a.id,a.sha256,a.size_bytes,a.content_path FROM attachments a
          JOIN message_attachments ma ON ma.attachment_id=a.id JOIN canonical_messages c ON c.id=ma.canonical_message_id
          WHERE c.storage_state IN ('archived','quarantined') ORDER BY a.id""").fetchall()
        for row in attachments:
            digest = str(row["sha256"])
            path = Path(str(row["content_path"])).resolve(strict=False)
            expected = (root / "attachments" / "sha256" / digest[:2] / digest).resolve(strict=False)
            if path != expected or not path.is_file() or _sha256(path) != digest:
                raise BorgError("attachment-invalid")
            relative = path.relative_to(root).as_posix()
            _copy_verified(path, snapshot / relative, digest)
            entries.append(
                {
                    "kind": "attachment",
                    "attachment_id": row["id"],
                    "sha256": digest,
                    "size_bytes": row["size_bytes"],
                    "path": relative,
                }
            )
    entries.append(
        {
            "kind": "sqlite",
            "path": "state/mailarchive.sqlite3",
            "sha256": _sha256(sqlite_path),
            "size_bytes": sqlite_path.stat().st_size,
        }
    )
    entries.sort(key=lambda item: (str(item["kind"]), str(item["path"])))
    manifest = b"".join(
        json.dumps(item, sort_keys=True, separators=(",", ":")).encode() + b"\n" for item in entries
    )
    manifest_path = snapshot / "metadata" / "backup-manifest.jsonl"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(manifest)
    return snapshot, canonical_ids, hashlib.sha256(manifest).hexdigest()


def _db_repository(config: AppConfig, name: str) -> sqlite3.Row:
    with connect(config.database.path) as db:
        row = db.execute("SELECT * FROM backup_repositories WHERE name=?", (name,)).fetchone()
    if row is None:
        raise BorgError("repository-unknown")
    return row


def repo_init(config: AppConfig, name: str) -> str:
    initialize(config.database.path, config.accounts, config.backup_repositories)
    repository = _repository(config, name)
    adapter = BorgAdapter(config, repository)
    with _lock(config, name):
        with connect(config.database.path) as db:
            row = db.execute(
                "SELECT repository_identity FROM backup_repositories WHERE name=?", (name,)
            ).fetchone()
        known_identity = None if row is None else row[0]
        # A bound name probes only: it can never initialize a replacement destination.
        identity = adapter.identity() if known_identity is not None else adapter.init()
        if known_identity is not None and known_identity != identity:
            raise BorgError("repository-identity-mismatch")
        with connect(config.database.path) as db:
            if row is not None and row[0] is not None and row[0] != identity:
                raise BorgError("repository-identity-mismatch")
            db.execute(
                "UPDATE backup_repositories SET repository_identity=?,updated_at=? WHERE name=?",
                (identity, utc_now(), name),
            )
            db.commit()
    return identity


def backup_run(config: AppConfig, name: str) -> str:
    initialize(config.database.path, config.accounts, config.backup_repositories)
    repository = _repository(config, name)
    if not repository.enabled:
        raise BorgError("repository-disabled")
    adapter, run_id = BorgAdapter(config, repository), uuid.uuid4().hex
    archive = f"mailarchive-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{run_id}"
    with _lock(config, name):
        db_repository = _db_repository(config, name)
        adapter.version()
        if (
            db_repository["repository_identity"] is None
            or adapter.identity() != db_repository["repository_identity"]
        ):
            raise BorgError("repository-identity-mismatch")
        with connect(config.database.path) as db:
            db.execute(
                "UPDATE backup_runs SET status='failed',completed_at=?,verification_status='failed',last_error_kind='interrupted' WHERE repository_id=? AND status='running'",
                (utc_now(), db_repository["id"]),
            )
            db.execute(
                "INSERT INTO backup_runs(id,repository_id,started_at,status,archive_name,verification_status,details_json) VALUES (?,?,?,?,?,'unverified','{}')",
                (run_id, db_repository["id"], utc_now(), "running", archive),
            )
            db.commit()
        snapshot: Path | None = None
        try:
            snapshot, canonical_ids, manifest = _build_snapshot(config, run_id)
            adapter.create(archive, snapshot)
            with connect(config.database.path) as db:
                db.execute(
                    "UPDATE backup_runs SET status='succeeded',completed_at=?,command_exit_code=0,manifest_sha256=? WHERE id=?",
                    (utc_now(), manifest, run_id),
                )
                db.executemany(
                    "INSERT INTO message_backup_evidence(canonical_message_id,backup_run_id,covered,verified,recorded_at) VALUES (?, ?, 1, 0, ?)",
                    [(item, run_id, utc_now()) for item in canonical_ids],
                )
                db.commit()
        except BorgError as error:
            with connect(config.database.path) as db:
                db.execute(
                    "UPDATE backup_runs SET status='failed',completed_at=?,verification_status='failed',last_error_kind=? WHERE id=?",
                    (utc_now(), error.kind, run_id),
                )
                db.commit()
            raise
        finally:
            if snapshot is not None:
                shutil.rmtree(snapshot, ignore_errors=True)
    return run_id


def _fail_verification(config: AppConfig, run_id: str, kind: str) -> None:
    with connect(config.database.path) as db:
        db.execute(
            "UPDATE backup_runs SET verification_status='failed',verified_at=NULL,last_error_kind=? WHERE id=?",
            (kind, run_id),
        )
        db.execute("UPDATE message_backup_evidence SET verified=0 WHERE backup_run_id=?", (run_id,))
        db.commit()


def verify_run(config: AppConfig, run_id: str) -> None:
    initialize(config.database.path, config.accounts, config.backup_repositories)
    with connect(config.database.path) as db:
        run = db.execute(
            "SELECT r.*, b.name,b.repository_identity FROM backup_runs r JOIN backup_repositories b ON b.id=r.repository_id WHERE r.id=?",
            (run_id,),
        ).fetchone()
    if run is None or run["status"] != "succeeded" or not run["manifest_sha256"]:
        raise BorgError("run-invalid")
    repository = _repository(config, str(run["name"]))
    adapter = BorgAdapter(config, repository)
    try:
        with _lock(config, repository.name):
            if adapter.identity() != run["repository_identity"]:
                raise BorgError("repository-identity-mismatch")
            manifest = adapter.manifest_bytes(str(run["archive_name"]))
            if hashlib.sha256(manifest).hexdigest() != run["manifest_sha256"]:
                raise BorgError("manifest-mismatch")
            entries = [json.loads(line) for line in manifest.splitlines()]
            expected = {
                str(item["path"]): (int(item["size_bytes"]), str(item["sha256"]))
                for item in entries
            }
            expected["metadata/backup-manifest.jsonl"] = (
                len(manifest),
                hashlib.sha256(manifest).hexdigest(),
            )
            if adapter.inventory(str(run["archive_name"])) != expected:
                raise BorgError("inventory-mismatch")
            adapter.verify_data(str(run["archive_name"]))
    except BorgError as error:
        _fail_verification(config, run_id, error.kind)
        raise
    with connect(config.database.path) as db:
        now = utc_now()
        db.execute(
            "UPDATE backup_runs SET verification_status='verified',verified_at=?,last_error_kind=NULL WHERE id=?",
            (now, run_id),
        )
        db.execute(
            "UPDATE message_backup_evidence SET verified=1 WHERE backup_run_id=? AND covered=1",
            (run_id,),
        )
        db.commit()


def restore_test(config: AppConfig, run_id: str, destination: Path) -> None:
    initialize(config.database.path, config.accounts, config.backup_repositories)
    destination = destination.resolve(strict=False)
    if _inside(destination, config.archive.root) or any(
        destination.iterdir() if destination.exists() else ()
    ):
        raise BorgError("restore-destination-invalid")
    with connect(config.database.path) as db:
        run = db.execute(
            "SELECT r.*,b.name FROM backup_runs r JOIN backup_repositories b ON b.id=r.repository_id WHERE r.id=?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise BorgError("run-invalid")
        db.execute(
            "INSERT INTO backup_restore_tests(backup_run_id,started_at,status) VALUES (?,?,'running')",
            (run_id, utc_now()),
        )
        test_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.commit()
    try:
        destination.mkdir(parents=True, exist_ok=True)
        adapter = BorgAdapter(config, _repository(config, str(run["name"])))
        adapter.extract(str(run["archive_name"]), destination)
        manifest = (destination / "metadata" / "backup-manifest.jsonl").read_bytes()
        if hashlib.sha256(manifest).hexdigest() != run["manifest_sha256"]:
            raise BorgError("manifest-mismatch")
        with sqlite3.connect(destination / "state" / "mailarchive.sqlite3") as db:
            if (
                db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                or db.execute("PRAGMA foreign_key_check").fetchone() is not None
            ):
                raise BorgError("sqlite-restore-invalid")
        for item in (json.loads(line) for line in manifest.splitlines()):
            path = destination / str(item["path"])
            if (
                not path.is_file()
                or path.stat().st_size != int(item["size_bytes"])
                or _sha256(path) != item["sha256"]
            ):
                raise BorgError("restore-object-mismatch")
    except BorgError as error:
        with connect(config.database.path) as db:
            db.execute(
                "UPDATE backup_restore_tests SET completed_at=?,status='failed',error_kind=? WHERE id=?",
                (utc_now(), error.kind, test_id),
            )
            db.commit()
        raise
    with connect(config.database.path) as db:
        db.execute(
            "UPDATE backup_restore_tests SET completed_at=?,status='succeeded' WHERE id=?",
            (utc_now(), test_id),
        )
        db.commit()


def status(config: AppConfig) -> list[dict[str, object]]:
    if not config.database.path.exists():
        return []
    with connect(config.database.path) as db:
        rows = db.execute("""SELECT b.name,b.enabled,b.repository_identity,
          (SELECT status FROM backup_runs r WHERE r.repository_id=b.id ORDER BY started_at DESC LIMIT 1) last_backup_run,
          (SELECT status FROM backup_runs r WHERE r.repository_id=b.id AND r.status='succeeded' ORDER BY started_at DESC LIMIT 1) last_successful_backup_run,
          (SELECT verification_status FROM backup_runs r WHERE r.repository_id=b.id ORDER BY started_at DESC LIMIT 1) last_verification_status,
          (SELECT verified_at FROM backup_runs r WHERE r.repository_id=b.id ORDER BY started_at DESC LIMIT 1) last_verified_at,
          (SELECT COUNT(*) FROM message_backup_evidence e JOIN backup_runs r ON r.id=e.backup_run_id WHERE r.repository_id=b.id AND e.verified=1) verified_message_evidence_count FROM backup_repositories b ORDER BY b.name""").fetchall()
    return [
        {**dict(row), "repository_identity_known": row["repository_identity"] is not None}
        for row in rows
    ]
