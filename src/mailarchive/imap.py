"""Strictly pull-only mbsync acquisition into a non-canonical persistent mirror."""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from mailarchive.db import account_id, connect, initialize, insert_audit_event, utc_now
from mailarchive.ingest import IngestResult, ingest_file
from mailarchive.models import AccountConfig, AppConfig, CanonicalMessage

_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
_UID = re.compile(r"(?:^|,)U=(\d+)(?=[:,]|$)")
_STATE_PAIR = re.compile(r"^(\d+)\s+(\d+)(?:\s+\d+){0,2}\s*$")
_SAFE = re.compile(r"[^A-Za-z0-9]+")
REQUIRED_DIRECTIVES: Final = (
    "Sync Pull New", "Create Near", "Remove None", "Expunge None",
    "MaxSize 0", "FSync yes", "SyncState *", "CopyArrivalDate yes", "AltMap no",
)
_DIAGNOSTIC_LIMIT: Final = 2_000


class ImapError(RuntimeError):
    """An IMAP action was refused or did not complete safely."""


@dataclass(frozen=True)
class ImapLayout:
    mirror_mailbox: Path
    config_path: Path
    channel: str


@dataclass(frozen=True)
class StateMapping:
    far_uidvalidity: int
    far_to_near: dict[int, int]


def _identifier(value: str, prefix: str) -> str:
    slug = _SAFE.sub("-", value).strip("-").lower()[:40] or "folder"
    return f"{prefix}-{slug}-{hashlib.sha256(value.encode()).hexdigest()[:12]}"


def _q(value: str) -> str:
    if any(character in value for character in "\r\n\x00"):
        raise ImapError("managed mbsync values must not contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _credential_variable(reference: str) -> str:
    if not reference.startswith("env:") or not _ENV_NAME.fullmatch(reference[4:]):
        raise ImapError("IMAP credentials must use config_ref: env:VARIABLE_NAME")
    return reference[4:]


def _configured_secret_values(config: AppConfig) -> tuple[str, ...]:
    """Return present configured environment secrets for error redaction only."""
    values: list[str] = []
    for account in config.accounts:
        if account.config_ref.startswith("env:") and _ENV_NAME.fullmatch(account.config_ref[4:]):
            value = os.environ.get(account.config_ref[4:])
            if value:
                values.append(value)
    return tuple(values)


def sanitize_mbsync_diagnostic(stderr: str, config: AppConfig) -> str:
    """Bound mbsync diagnostics while ensuring configured credentials cannot escape."""
    sanitized = stderr
    for secret in _configured_secret_values(config):
        sanitized = sanitized.replace(secret, "<redacted>")
    sanitized = sanitized.strip()
    if len(sanitized) > _DIAGNOSTIC_LIMIT:
        sanitized = sanitized[:_DIAGNOSTIC_LIMIT] + "… [truncated]"
    return sanitized or "no diagnostic output"


def managed_layout(config: AppConfig, account: AccountConfig, folder: str) -> ImapLayout:
    archive_root = config.archive.root.resolve()
    safe_folder = _identifier(folder, "folder")
    mirror_mailbox = archive_root / "staging" / "mbsync" / account.name / safe_folder
    mail_root = archive_root / "mail"
    if mirror_mailbox.is_relative_to(mail_root):  # defensive invariant
        raise ImapError("mbsync mirror must be outside canonical Maildir")
    return ImapLayout(
        mirror_mailbox=mirror_mailbox,
        config_path=(
            archive_root / "state" / "mbsync" / account.name / f"{_identifier(folder, 'folder')}.mbsyncrc"
        ),
        channel=_identifier(f"{account.name}:{folder}", "mailarchive"),
    )


def managed_config_text(config: AppConfig, account: AccountConfig, folder: str) -> str:
    if account.imap is None:
        raise ImapError("IMAP connection settings are required before synchronization")
    variable = _credential_variable(account.config_ref)
    layout = managed_layout(config, account, folder)
    imap = account.imap
    tls = {"IMAPS": "IMAPS", "STARTTLS": "STARTTLS", "INSECURE_LOOPBACK": "None"}[imap.tls_mode]
    far_store = _identifier(account.name, "far-store")
    near_store = _identifier(account.name + ":" + folder, "near-store")
    # The only shell fragment is fixed and contains the validated variable name, never its value.
    pass_command = f'printf %s "${{{variable}}}"'
    return "\n".join((
        "# Managed by MailArchive; pull-only and never use with mbsync -a.", "FSync yes", "",
        f"IMAPAccount {_identifier(account.name, 'far-account')}",
        f"Host {_q(imap.host)}", f"Port {imap.port}", f"User {_q(imap.username)}",
        f"PassCmd {_q(pass_command)}", f"SSLType {tls}",
        f"Timeout {imap.connection_timeout_seconds}", "",
        f"IMAPStore {far_store}", f"Account {_identifier(account.name, 'far-account')}", "",
        f"MaildirStore {near_store}",
        f"Path {_q(str(layout.mirror_mailbox) + '/')}",
        f"Inbox {_q(str(layout.mirror_mailbox / 'INBOX'))}", "AltMap no", "InfoDelimiter :", "",
        f"Channel {layout.channel}",
        f"Far {_q(':' + far_store + ':' + folder)}",
        f"Near {_q(':' + near_store + ':INBOX')}",
        "Sync Pull New", "Create Near", "Remove None", "Expunge None",
        "MaxSize 0", "CopyArrivalDate yes", "SyncState *", "",
    ))


def validate_managed_config(text: str, command: list[str] | None = None) -> None:
    """Reject any generated config/command that could mutate the IMAP (far) side."""
    directives: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition(" ")
        directives.append((name, value.strip()))
    if any((directive.split(" ", 1)[0], directive.split(" ", 1)[1]) not in directives for directive in REQUIRED_DIRECTIVES):
        raise ImapError("managed mbsync configuration lacks required pull-only directives")
    forbidden = {
        "Sync": {"Push", "Gone", "Flags", "Full"}, "Create": {"Far", "Both"},
        "Remove": {"Far", "Both"}, "Expunge": {"Far", "Both"},
        "ExpungeSolo": {"Far", "Both"}, "Trash": None, "TrashRemoteNew": None,
        "MaxMessages": None, "Patterns": None,
    }
    for name, value in directives:
        prohibited_values = forbidden.get(name)
        if name in forbidden and (prohibited_values is None or value in prohibited_values):
            raise ImapError("managed mbsync configuration permits an unsafe operation")
    if command is not None and ("-a" in command or "--all" in command or len(command) != 4 or command[1] != "-c"):
        raise ImapError("mbsync must receive exactly one managed explicit channel")


def write_managed_config(config: AppConfig, account: AccountConfig, folder: str) -> ImapLayout:
    layout = managed_layout(config, account, folder)
    layout.config_path.parent.mkdir(parents=True, exist_ok=True)
    layout.mirror_mailbox.mkdir(parents=True, exist_ok=True)
    desired = managed_config_text(config, account, folder)
    validate_managed_config(desired)
    if layout.config_path.is_file() and layout.config_path.read_text(encoding="utf-8") == desired:
        return layout
    descriptor, temporary_name = tempfile.mkstemp(prefix="mbsyncrc.", dir=layout.config_path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(desired)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, layout.config_path)
        os.chmod(layout.config_path, 0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return layout


def parse_mbsync_state(path: Path) -> StateMapping:
    """Parse the conservative M3 subset of native .mbsyncstate after a successful run."""
    companions = [path.parent / f"{path.name}{suffix}" for suffix in (".new", ".journal", ".lock")]
    if path.name != ".mbsyncstate" or not path.is_file() or any(item.exists() for item in companions):
        raise ImapError("authoritative completed .mbsyncstate is unavailable")
    far_uidvalidity: int | None = None
    mappings: dict[int, int] = {}
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ImapError("cannot read mbsync synchronization state") from error
    for line in lines:
        if not line or line.startswith("#"):
            continue
        if line.startswith("FarUidValidity "):
            value = line.removeprefix("FarUidValidity ")
            if not value.isdecimal() or int(value) <= 0:
                raise ImapError("malformed FarUidValidity in mbsync state")
            far_uidvalidity = int(value)
            continue
        match = _STATE_PAIR.fullmatch(line)
        if match:
            far, near = int(match.group(1)), int(match.group(2))
            if far <= 0 or near <= 0 or far in mappings:
                raise ImapError("ambiguous mbsync UID mapping")
            mappings[far] = near
    if far_uidvalidity is None:
        raise ImapError("mbsync state lacks FarUidValidity")
    return StateMapping(far_uidvalidity=far_uidvalidity, far_to_near=mappings)


def _near_uid(path: Path) -> int | None:
    match = _UID.search(path.name)
    return None if match is None else int(match.group(1))


@contextmanager
def _folder_lock(config: AppConfig, account: AccountConfig, folder: str) -> Generator[None, None, None]:
    import fcntl
    lock = config.archive.root.resolve() / "state" / "locks" / f"{_identifier(account.name + ':' + folder, 'imap')}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ImapError("IMAP account/folder synchronization is already running") from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _audit(config: AppConfig, account_name: str, event: str, result: str, details: dict[str, object]) -> None:
    with connect(config.database.path) as connection:
        identifier = account_id(connection, account_name)
        insert_audit_event(connection, actor="mailarchive.imap", event_type=event, result=result,
                           account_id=identifier, details_json=json.dumps(details, sort_keys=True))
        connection.commit()


def _register_link(
    config: AppConfig,
    account_name: str,
    folder: str,
    uidvalidity: int,
    uid: int,
    canonical: CanonicalMessage,
) -> bool:
    with connect(config.database.path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            aid = account_id(connection, account_name)
            if aid is None or aid != canonical.account_id:
                raise ImapError("canonical account is not active for remote linking")
            remote_id = f"{aid}:{hashlib.sha256((folder + '\\0' + str(uidvalidity) + '\\0' + str(uid)).encode()).hexdigest()}"
            now = utc_now()
            connection.execute("""INSERT INTO remote_messages(id, account_id, remote_folder, uidvalidity,
                remote_uid, message_id_header, first_seen_at, last_seen_at, remote_present, identity_confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'proven')
                ON CONFLICT(account_id, remote_folder, uidvalidity, remote_uid) DO UPDATE SET last_seen_at=excluded.last_seen_at, remote_present=1""",
                (remote_id, aid, folder, uidvalidity, uid, canonical.message_id_header, now, now))
            row = connection.execute("SELECT id FROM remote_messages WHERE account_id=? AND remote_folder=? AND uidvalidity=? AND remote_uid=?", (aid, folder, uidvalidity, uid)).fetchone()
            existing_link = connection.execute(
                "SELECT canonical_message_id FROM remote_canonical_links WHERE remote_message_id = ?",
                (str(row[0]),),
            ).fetchone()
            if existing_link is not None and str(existing_link[0]) != canonical.id:
                insert_audit_event(
                    connection,
                    actor="mailarchive.imap",
                    event_type="imap.remote_link.failed",
                    result="conflict",
                    account_id=aid,
                    canonical_message_id=canonical.id,
                    details_json=json.dumps({"folder": folder, "uidvalidity": uidvalidity, "uid": uid}),
                )
                connection.commit()
                return False
            connection.execute(
                "INSERT OR IGNORE INTO remote_canonical_links(remote_message_id, canonical_message_id, "
                "link_reason, created_at) VALUES (?, ?, 'mbsync-state-uid-map', ?)",
                (str(row[0]), canonical.id, now),
            )
            insert_audit_event(connection, actor="mailarchive.imap", event_type="imap.remote_link.created", result="success", account_id=aid, canonical_message_id=canonical.id, details_json=json.dumps({"folder": folder, "uidvalidity": uidvalidity, "uid": uid}))
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
            return True


class ImapMbsyncAdapter:
    def __init__(self, config: AppConfig, *, executable: str = "mbsync") -> None:
        self.config, self.executable = config, executable

    def sync(self, account_name: str, folder: str) -> list[IngestResult]:
        account = next((item for item in self.config.accounts if item.name == account_name), None)
        if account is None:
            raise ImapError(f"unknown account: {account_name}")
        if not account.enabled:
            raise ImapError(f"account is disabled: {account_name}")
        if account.kind != "imap" or account.imap is None:
            raise ImapError(f"account is not configured for IMAP synchronization: {account_name}")
        if folder not in account.imap.folders:
            raise ImapError(f"folder is not configured for account: {folder}")
        variable = _credential_variable(account.config_ref)
        if not os.environ.get(variable):
            raise ImapError(f"missing credential environment variable: {variable}")
        initialize(self.config.database.path, self.config.accounts)
        with _folder_lock(self.config, account, folder):
            layout = write_managed_config(self.config, account, folder)
            command = [self.executable, "-c", str(layout.config_path), layout.channel]
            validate_managed_config(layout.config_path.read_text(encoding="utf-8"), command)
            _audit(self.config, account_name, "imap.sync.started", "started", {"folder": folder})
            try:
                completed = subprocess.run(command, check=False, capture_output=True, text=True,
                    timeout=account.imap.sync_timeout_seconds, env=os.environ.copy())
            except FileNotFoundError as error:
                _audit(self.config, account_name, "imap.sync.failed", "failed", {"folder": folder, "reason": "binary-unavailable"})
                raise ImapError("mbsync executable is unavailable; install the 'isync' package and retry") from error
            except subprocess.TimeoutExpired as error:
                _audit(self.config, account_name, "imap.sync.failed", "failed", {"folder": folder, "reason": "timeout"})
                raise ImapError(f"mbsync synchronization timed out after {account.imap.sync_timeout_seconds} seconds") from error
            if completed.returncode != 0:
                _audit(self.config, account_name, "imap.sync.failed", "failed", {"folder": folder, "exit_code": completed.returncode})
                diagnostic = sanitize_mbsync_diagnostic(completed.stderr, self.config)
                raise ImapError(
                    f"mbsync synchronization failed (exit {completed.returncode}): {diagnostic}"
                )
            results: list[IngestResult] = []
            try:
                state = parse_mbsync_state(layout.mirror_mailbox / "INBOX" / ".mbsyncstate")
            except ImapError as error:
                state = None
                mapping_error = str(error)
            else:
                mapping_error = None
            for directory in (
                layout.mirror_mailbox / "INBOX" / "cur",
                layout.mirror_mailbox / "INBOX" / "new",
            ):
                if not directory.is_dir():
                    continue
                for source in sorted(item for item in directory.iterdir() if item.is_file()):
                    with tempfile.NamedTemporaryFile(suffix=".eml", delete=False) as temporary:
                        temporary.write(source.read_bytes())
                        temporary_path = Path(temporary.name)
                    try:
                        result = ingest_file(self.config, temporary_path, account_name)
                    finally:
                        temporary_path.unlink(missing_ok=True)
                    results.append(result)
                    near = _near_uid(source)
                    far = None if state is None or near is None else next((key for key, value in state.far_to_near.items() if value == near), None)
                    if far is None or state is None:
                        _audit(self.config, account_name, "imap.remote_link.failed", "unresolved", {"folder": folder, "reason": mapping_error or "near UID mapping unavailable", "sha256": result.canonical_message.sha256})
                    else:
                        _register_link(self.config, account_name, folder, state.far_uidvalidity, far, result.canonical_message)
                    if result.created:
                        _audit(self.config, account_name, "imap.message.imported", "success", {"folder": folder, "sha256": result.canonical_message.sha256})
            _audit(self.config, account_name, "imap.sync.succeeded", "success", {"folder": folder, "imported": sum(result.created for result in results), "seen": len(results)})
            return results
