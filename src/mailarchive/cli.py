"""Local-only command-line interface for canonical ingest and derived search."""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from mailarchive.attachments import AttachmentError, reconcile_attachments
from mailarchive.borg import BorgError, backup_run, repo_init, restore_test, verify_run
from mailarchive.borg import status as backup_status
from mailarchive.classification import ClassificationResult, apply_classification
from mailarchive.config import ConfigError, display_config, load_config
from mailarchive.db import account_id, connect, initialize
from mailarchive.fastpath import FAST_PATH_STALE_SECONDS, FastPathWatcher, fast_path_status
from mailarchive.gmail import GmailAdapter, GmailError, GmailWatcher, authorize
from mailarchive.imap import ImapAdapter, ImapError
from mailarchive.ingest import IngestError, ingest_file
from mailarchive.notmuch import NotmuchAdapter, NotmuchError, search_canonical_messages
from mailarchive.pop3 import Pop3Adapter, Pop3Error
from mailarchive.recoll import RecollAdapter, RecollError, search_attachments
from mailarchive.remote_mutation import ProductionPlanError, execute_production_plan, plan_dry_run
from mailarchive.retention import evaluate_all, set_control


def _emit(value: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, sort_keys=True))
    elif isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, indent=2, sort_keys=True))


def _config_path(args: argparse.Namespace) -> Path:
    return Path(args.config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailarchive", description="MailArchive local safety baseline"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    config = subcommands.add_parser("config", help="validate local configuration")
    config_subcommands = config.add_subparsers(dest="config_command", required=True)
    check = config_subcommands.add_parser("check", help="validate a YAML configuration")
    check.add_argument("--config", required=True, help="path to YAML configuration")
    check.add_argument("--json", action="store_true", help="emit JSON")
    db = subcommands.add_parser("db", help="local database operations")
    db_subcommands = db.add_subparsers(dest="db_command", required=True)
    init = db_subcommands.add_parser("init", help="initialize the local SQLite database")
    init.add_argument("--config", required=True, help="path to YAML configuration")
    init.add_argument("--json", action="store_true", help="emit JSON")
    status = subcommands.add_parser("status", help="show minimal local database status")
    status.add_argument("--config", required=True, help="path to YAML configuration")
    status.add_argument("--json", action="store_true", help="emit JSON")
    ingest = subcommands.add_parser("ingest", help="ingest one local RFC822 .eml file")
    ingest.add_argument("path", help="path to a local .eml file")
    ingest.add_argument("--account", required=True, help="configured destination account")
    ingest.add_argument("--config", required=True, help="path to YAML configuration")
    ingest.add_argument("--json", action="store_true", help="emit JSON")
    index = subcommands.add_parser("index", help="rebuildable local notmuch index operations")
    index_subcommands = index.add_subparsers(dest="index_command", required=True)
    refresh = index_subcommands.add_parser("refresh", help="run managed notmuch new without hooks")
    refresh.add_argument("--config", required=True, help="path to YAML configuration")
    refresh.add_argument("--json", action="store_true", help="emit JSON")
    search = subcommands.add_parser("search", help="search the local notmuch index")
    search.add_argument("query", help="notmuch query")
    search.add_argument("--scope", choices=("archived", "quarantine", "all"), default="archived")
    search.add_argument("--config", required=True, help="path to YAML configuration")
    search.add_argument("--json", action="store_true", help="emit JSON")
    quarantine = subcommands.add_parser("quarantine", help="local-only quarantine operations")
    quarantine_subcommands = quarantine.add_subparsers(dest="quarantine_command", required=True)
    quarantine_list = quarantine_subcommands.add_parser(
        "list", help="list locally quarantined messages"
    )
    quarantine_list.add_argument("--config", required=True)
    quarantine_list.add_argument("--json", action="store_true")
    classify = subcommands.add_parser("classify", help="local classification overrides")
    classify_subcommands = classify.add_subparsers(dest="classify_command", required=True)
    override = classify_subcommands.add_parser("override", help="append a manual local verdict")
    override.add_argument("--canonical-id", required=True)
    override.add_argument("--classification", choices=("ham", "suspect", "spam"), required=True)
    override.add_argument("--reason", required=True)
    override.add_argument("--config", required=True)
    override.add_argument("--json", action="store_true")
    imap = subcommands.add_parser("imap", help="explicit read-only IMAP acquisition")
    imap_subcommands = imap.add_subparsers(dest="imap_command", required=True)
    sync = imap_subcommands.add_parser(
        "sync", help="pull one configured IMAP folder into the local archive"
    )
    sync.add_argument("--account", required=True, help="one configured IMAP account")
    sync.add_argument("--folder", required=True, help="one configured remote folder")
    sync.add_argument("--config", required=True, help="path to YAML configuration")
    sync.add_argument("--json", action="store_true", help="emit JSON")
    watch = imap_subcommands.add_parser(
        "watch", help="watch one IMAP INBOX using IDLE or safe polling"
    )
    watch.add_argument("--account", required=True, help="one configured ordinary IMAP account")
    watch.add_argument("--config", required=True, help="path to YAML configuration")
    watch.add_argument("--json", action="store_true", help="emit JSON startup/errors only")
    gmail = subcommands.add_parser("gmail", help="read-only Gmail REST acquisition")
    gmail_subcommands = gmail.add_subparsers(dest="gmail_command", required=True)
    gmail_auth = gmail_subcommands.add_parser(
        "auth", help="authorize a Gmail installed application"
    )
    gmail_auth.add_argument("--account", required=True)
    gmail_auth.add_argument("--config", required=True)
    gmail_auth.add_argument("--json", action="store_true")
    gmail_sync = gmail_subcommands.add_parser(
        "sync", help="synchronize Gmail through read-only REST"
    )
    gmail_sync.add_argument("--account", required=True)
    gmail_sync.add_argument("--config", required=True)
    gmail_sync.add_argument("--json", action="store_true")
    gmail_watch = gmail_subcommands.add_parser(
        "watch", help="poll Gmail history and refresh local notmuch"
    )
    gmail_watch.add_argument("--account", required=True)
    gmail_watch.add_argument("--config", required=True)
    gmail_watch.add_argument("--json", action="store_true")
    pop3 = subcommands.add_parser("pop3", help="read-only POP3 fallback acquisition")
    pop3_subcommands = pop3.add_subparsers(dest="pop3_command", required=True)
    pop3_sync = pop3_subcommands.add_parser("sync", help="retrieve new UIDLs without deletion")
    pop3_sync.add_argument("--account", required=True)
    pop3_sync.add_argument("--config", required=True)
    pop3_sync.add_argument("--json", action="store_true")
    attachments = subcommands.add_parser(
        "attachments", help="local-only attachment extraction and search"
    )
    attachment_subcommands = attachments.add_subparsers(dest="attachments_command", required=True)
    extract = attachment_subcommands.add_parser(
        "extract", help="reconcile finalized canonical messages"
    )
    extract.add_argument("--canonical-id")
    extract.add_argument("--force", action="store_true")
    extract.add_argument("--config", required=True)
    extract.add_argument("--json", action="store_true")
    attachment_index = attachment_subcommands.add_parser(
        "index", help="managed derived Recoll index"
    )
    attachment_index_subcommands = attachment_index.add_subparsers(
        dest="attachment_index_command", required=True
    )
    for action in ("refresh", "rebuild"):
        command = attachment_index_subcommands.add_parser(action)
        command.add_argument("--config", required=True)
        command.add_argument("--json", action="store_true")
    attachment_search = attachment_subcommands.add_parser(
        "search", help="search attachment content locally"
    )
    attachment_search.add_argument("query")
    attachment_search.add_argument(
        "--scope", choices=("archived", "quarantine", "all"), default="archived"
    )
    attachment_search.add_argument("--config", required=True)
    attachment_search.add_argument("--json", action="store_true")
    backup = subcommands.add_parser("backup", help="Borg 1.x backup and verification")
    backup_subcommands = backup.add_subparsers(dest="backup_command", required=True)
    backup_repo = backup_subcommands.add_parser("repo", help="explicit Borg repository operations")
    backup_repo_subcommands = backup_repo.add_subparsers(dest="backup_repo_command", required=True)
    backup_init = backup_repo_subcommands.add_parser(
        "init", help="initialize or safely probe one repository"
    )
    backup_init.add_argument("--repository", required=True)
    backup_init.add_argument("--config", required=True)
    backup_init.add_argument("--json", action="store_true")
    backup_run_command = backup_subcommands.add_parser(
        "run", help="create one controlled local snapshot archive"
    )
    backup_run_command.add_argument("--repository", required=True)
    backup_run_command.add_argument("--config", required=True)
    backup_run_command.add_argument("--json", action="store_true")
    backup_verify = backup_subcommands.add_parser(
        "verify", help="verify the exact archive anchored by one run"
    )
    backup_verify.add_argument("--run-id", required=True)
    backup_verify.add_argument("--config", required=True)
    backup_verify.add_argument("--json", action="store_true")
    backup_restore = backup_subcommands.add_parser(
        "restore-test", help="extract and validate into an empty directory"
    )
    backup_restore.add_argument("--run-id", required=True)
    backup_restore.add_argument("--destination", required=True)
    backup_restore.add_argument("--config", required=True)
    backup_restore.add_argument("--json", action="store_true")
    backup_local_status = backup_subcommands.add_parser(
        "status", help="show local backup facts only"
    )
    backup_local_status.add_argument("--config", required=True)
    backup_local_status.add_argument("--json", action="store_true")
    candidates = subcommands.add_parser(
        "deletion-candidates", help="report local retention candidates; never deletes"
    )
    candidates.add_argument("--config", required=True)
    candidates.add_argument("--account")
    candidates.add_argument("--json", action="store_true")
    remote_delete = subcommands.add_parser(
        "remote-delete", help="plan local-only remote deletion dry-run"
    )
    mode = remote_delete.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run", action="store_true", help="local-only plan; no provider connection"
    )
    mode.add_argument(
        "--execute-plan", type=int, metavar="RUN_ID", help="execute one existing account plan"
    )
    remote_delete.add_argument("--account")
    remote_delete.add_argument("--limit", type=int)
    remote_delete.add_argument("--config", required=True)
    remote_delete.add_argument("--json", action="store_true")
    retention = subcommands.add_parser(
        "retention", help="local-only retention policy controls and reports"
    )
    retention_subcommands = retention.add_subparsers(dest="retention_command", required=True)
    report = retention_subcommands.add_parser(
        "report", help="append and report all local candidate evaluations"
    )
    report.add_argument("--config", required=True)
    report.add_argument("--account")
    report.add_argument("--eligible-only", action="store_true")
    report.add_argument("--blocked-only", action="store_true")
    report.add_argument("--json", action="store_true")
    for action in ("hold", "release"):
        command = retention_subcommands.add_parser(
            action, help="set or release a local canonical retention control"
        )
        command.add_argument("--canonical-id", required=True)
        command.add_argument("--kind", choices=("keep-online", "legal-hold"), required=True)
        command.add_argument("--reason", required=True)
        command.add_argument("--config", required=True)
        command.add_argument("--json", action="store_true")
    return parser


def _error(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(_config_path(args))
        if args.command == "config":
            _emit({"valid": True, "config": display_config(config)}, args.json)
            return 0
        if args.command == "db":
            initialize(config.database.path, config.accounts, config.backup_repositories)
            _emit({"initialized": True, "database_path": str(config.database.path)}, args.json)
            return 0
        if args.command == "deletion-candidates":
            initialize(config.database.path, config.accounts, config.backup_repositories)
            results = [
                item for item in evaluate_all(config, account=args.account) if item["eligible"]
            ]
            _emit(results, args.json)
            return 0
        if args.command == "remote-delete":
            if args.execute_plan is not None and (args.account is None or args.limit is not None):
                _error("--execute-plan requires --account and cannot be combined with --limit")
            initialize(config.database.path, config.accounts, config.backup_repositories)
            if args.dry_run:
                _emit(plan_dry_run(config, account=args.account, limit=args.limit), args.json)
                return 0
            assert args.execute_plan is not None and args.account is not None
            _emit(
                {
                    "production_run_id": execute_production_plan(
                        config, args.execute_plan, args.account
                    )
                },
                args.json,
            )
            return 0
        if args.command == "retention":
            initialize(config.database.path, config.accounts, config.backup_repositories)
            if args.retention_command in {"hold", "release"}:
                set_control(
                    config,
                    args.canonical_id,
                    args.kind,
                    args.reason,
                    enabled=args.retention_command == "hold",
                )
                _emit(
                    {
                        "canonical_id": args.canonical_id,
                        "kind": args.kind,
                        "held": args.retention_command == "hold",
                        "execution_authorized": False,
                    },
                    args.json,
                )
                return 0
            if args.eligible_only and args.blocked_only:
                _error("--eligible-only and --blocked-only are mutually exclusive")
            results = evaluate_all(config, account=args.account)
            if args.eligible_only:
                results = [item for item in results if item["eligible"]]
            if args.blocked_only:
                results = [item for item in results if not item["eligible"]]
            _emit(results, args.json)
            return 0
        if args.command == "status":
            initialized = config.database.path.exists()
            account_count = 0
            canonical_message_count = 0
            schema_version = 0
            lifecycle: dict[str, int] = {}
            quarantine_counts: dict[str, int] = {}
            classifier_failure_count = 0
            attachment_blob_count = attachment_reference_count = 0
            attachment_extraction_counts: dict[str, int] = {}
            if initialized:
                with connect(config.database.path) as connection:
                    version_row = connection.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()
                    account_row = connection.execute("SELECT COUNT(*) FROM accounts").fetchone()
                    schema_version = int(version_row[0] or 0)
                    account_count = int(account_row[0])
                    if schema_version >= 2:
                        count_row = connection.execute(
                            "SELECT COUNT(*) FROM canonical_messages"
                        ).fetchone()
                        canonical_message_count = int(count_row[0])
                    lifecycle = (
                        {
                            str(row[0]): int(row[1])
                            for row in connection.execute(
                                "SELECT storage_state,COUNT(*) FROM canonical_messages GROUP BY storage_state"
                            )
                        }
                        if schema_version >= 8
                        else {}
                    )
                    if schema_version >= 8:
                        quarantine_counts = {
                            str(row[0]): int(row[1])
                            for row in connection.execute(
                                """SELECT x.classification,COUNT(*) FROM canonical_messages c
                                JOIN classifications x ON x.id=(SELECT id FROM classifications
                                WHERE canonical_message_id=c.id ORDER BY manual_override DESC,id DESC LIMIT 1)
                                WHERE c.storage_state='quarantined' GROUP BY x.classification"""
                            )
                        }
                        classifier_failure_count = int(
                            connection.execute(
                                "SELECT COUNT(*) FROM audit_events "
                                "WHERE event_type='classification.failed' AND result='fail-safe'"
                            ).fetchone()[0]
                        )
                    if schema_version >= 9:
                        attachment_blob_count = int(
                            connection.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
                        )
                        attachment_reference_count = int(
                            connection.execute(
                                "SELECT COUNT(*) FROM message_attachments"
                            ).fetchone()[0]
                        )
                        attachment_extraction_counts = {
                            str(row[0]): int(row[1])
                            for row in connection.execute(
                                "SELECT status,COUNT(*) FROM attachment_extractions GROUP BY status"
                            )
                        }
            health = [record.__dict__ for record in fast_path_status(config)] if initialized else []
            gmail_status: list[dict[str, object]] = [
                {
                    "account": account.name,
                    "provider": "gmail",
                    "token_file_present": Path(account.config_ref[5:]).is_file(),
                    "full_sync_required": True,
                    "last_successful_sync": None,
                    "last_full_sync": None,
                    "last_partial_sync": None,
                    "known_provider_messages": 0,
                    "remote_present_provider_messages": 0,
                    "known_labels": 0,
                    "watcher_mode": None,
                    "watcher_state": "not-started",
                    "last_heartbeat": None,
                    "last_successful_index": None,
                    "index_pending": False,
                    "last_safe_error_category": None,
                }
                for account in config.accounts
                if account.kind == "gmail" and account.gmail is not None
            ]
            if initialized:
                gmail_status = []
                with connect(config.database.path) as connection:
                    for account in config.accounts:
                        if account.kind != "gmail" or account.gmail is None:
                            continue
                        aid = account_id(connection, account.name)
                        state = (
                            None
                            if aid is None
                            else connection.execute(
                                "SELECT * FROM gmail_sync_state WHERE account_id=?", (aid,)
                            ).fetchone()
                        )
                        counts = (
                            (0, 0, 0)
                            if aid is None
                            else connection.execute(
                                "SELECT COUNT(*), SUM(remote_present), "
                                "(SELECT COUNT(*) FROM gmail_labels WHERE account_id=?) "
                                "FROM remote_messages WHERE account_id=? AND provider_kind='gmail'",
                                (aid, aid),
                            ).fetchone()
                        )
                        watcher = (
                            None
                            if aid is None
                            else connection.execute(
                                "SELECT mode,last_heartbeat_at,last_index_succeeded_at,index_pending,last_error_kind "
                                "FROM fast_path_health WHERE account_id=? AND remote_folder='__GMAIL__'",
                                (aid,),
                            ).fetchone()
                        )
                        gmail_status.append(
                            {
                                "account": account.name,
                                "provider": "gmail",
                                "token_file_present": Path(account.config_ref[5:]).is_file(),
                                "full_sync_required": True
                                if state is None
                                else bool(state["full_sync_required"]),
                                "last_successful_sync": None
                                if state is None
                                else state["last_sync_succeeded_at"],
                                "last_full_sync": None
                                if state is None
                                else state["last_full_sync_succeeded_at"],
                                "last_partial_sync": None
                                if state is None
                                else state["last_partial_sync_succeeded_at"],
                                "known_provider_messages": int(counts[0] or 0),
                                "remote_present_provider_messages": int(counts[1] or 0),
                                "known_labels": int(counts[2] or 0),
                                "watcher_mode": None if watcher is None else watcher["mode"],
                                "watcher_state": (
                                    "not-started"
                                    if watcher is None
                                    else "stopped"
                                    if watcher["mode"] == "stopped"
                                    else "stale"
                                    if not watcher["last_heartbeat_at"]
                                    or (
                                        datetime.now(UTC)
                                        - datetime.fromisoformat(str(watcher["last_heartbeat_at"]))
                                    ).total_seconds()
                                    > FAST_PATH_STALE_SECONDS
                                    else "active"
                                ),
                                "last_heartbeat": None
                                if watcher is None
                                else watcher["last_heartbeat_at"],
                                "last_successful_index": None
                                if watcher is None
                                else watcher["last_index_succeeded_at"],
                                "index_pending": False
                                if watcher is None
                                else bool(watcher["index_pending"]),
                                "last_safe_error_category": None
                                if state is None and watcher is None
                                else (
                                    state["last_error_kind"]
                                    if state is not None and state["last_error_kind"]
                                    else watcher["last_error_kind"]
                                    if watcher is not None
                                    else None
                                ),
                            }
                        )
            _emit(
                {
                    "database_initialized": initialized,
                    "database_path": str(config.database.path),
                    "schema_version": schema_version,
                    "account_count": account_count,
                    "canonical_message_count": canonical_message_count,
                    "lifecycle_counts": lifecycle if initialized else {},
                    "quarantine_counts": quarantine_counts if initialized else {},
                    "classifier_failure_event_count": classifier_failure_count,
                    "attachment_blob_count": attachment_blob_count,
                    "attachment_reference_count": attachment_reference_count,
                    "attachment_extraction_counts": attachment_extraction_counts,
                    "remote_mutation_supported": False,
                    "fast_path": health,
                    "gmail": gmail_status,
                },
                args.json,
            )
            return 0
        if args.command == "backup":
            if args.backup_command == "status":
                _emit(backup_status(config), args.json)
                return 0
            if args.backup_command == "repo":
                _emit(
                    {
                        "repository": args.repository,
                        "repository_identity": repo_init(config, args.repository),
                    },
                    args.json,
                )
                return 0
            if args.backup_command == "run":
                run_id = backup_run(config, args.repository)
                _emit({"run_id": run_id, "created": True, "verified": False}, args.json)
                return 0
            if args.backup_command == "verify":
                verify_run(config, args.run_id)
                _emit({"run_id": args.run_id, "verified": True}, args.json)
                return 0
            restore_test(config, args.run_id, Path(args.destination))
            _emit({"run_id": args.run_id, "restore_test": "succeeded"}, args.json)
            return 0
        if args.command == "attachments":
            initialize(config.database.path, config.accounts)
            if args.attachments_command == "extract":
                extracted = reconcile_attachments(config, args.canonical_id, force=args.force)
                _emit({"extracted_canonical_ids": extracted, "count": len(extracted)}, args.json)
                return 0
            if args.attachments_command == "index":
                adapter = RecollAdapter(config)
                if args.attachment_index_command == "refresh":
                    adapter.refresh()
                else:
                    adapter.rebuild()
                _emit({"indexed": True, "recoll_version": adapter.version()}, args.json)
                return 0
            results = search_attachments(config, args.query, scope=args.scope)
            _emit([result.as_dict() for result in results], args.json)
            return 0
        if args.command == "quarantine":
            if not config.database.path.exists():
                _emit([], args.json)
                return 0
            with connect(config.database.path) as connection:
                rows = connection.execute("""SELECT c.id AS canonical_id,a.name AS account,c.local_path,c.quarantined_at,x.classification,x.score,x.classified_at
                    FROM canonical_messages c JOIN accounts a ON a.id=c.account_id
                    LEFT JOIN classifications x ON x.id=(SELECT id FROM classifications WHERE canonical_message_id=c.id ORDER BY manual_override DESC,id DESC LIMIT 1)
                    WHERE c.storage_state='quarantined' ORDER BY c.quarantined_at DESC,c.id""").fetchall()
            _emit([dict(row) for row in rows], args.json)
            return 0
        if args.command == "classify":
            if not args.reason.strip():
                _error("--reason must be non-empty")
            from mailarchive.db import canonical_message_by_account_and_sha256

            with connect(config.database.path) as connection:
                row = connection.execute(
                    "SELECT account_id,sha256 FROM canonical_messages WHERE id=?",
                    (args.canonical_id,),
                ).fetchone()
                if row is None:
                    _error("unknown canonical ID")
                message = canonical_message_by_account_and_sha256(
                    connection, int(row["account_id"]), str(row["sha256"])
                )
                assert message is not None
            stored = apply_classification(
                config,
                message,
                ClassificationResult(
                    args.classification, None, args.reason.strip()[:256], "manual"
                ),
                manual=True,
            )
            _emit(
                {
                    "canonical_message_id": stored.id,
                    "classification": args.classification,
                    "storage_state": stored.storage_state,
                    "local_path": str(stored.local_path),
                },
                args.json,
            )
            return 0
        if args.command == "ingest":
            result = ingest_file(config, Path(args.path), args.account)
            message = result.canonical_message
            _emit(
                {
                    "canonical_message_id": message.id,
                    "sha256": message.sha256,
                    "local_path": str(message.local_path),
                    "size_bytes": message.size_bytes,
                    "created": result.created,
                },
                args.json,
            )
            return 0
        if args.command == "index":
            archive_adapter = NotmuchAdapter(config, kind="archive")
            archive_adapter.refresh()
            NotmuchAdapter(config, kind="quarantine").refresh()
            _emit({"refreshed": True, "notmuch_version": archive_adapter.version()}, args.json)
            return 0
        if args.command == "search":
            results = search_canonical_messages(config, args.query, scope=args.scope)
            _emit([result.as_dict() for result in results], args.json)
            return 0
        if args.command == "imap":
            if args.imap_command == "watch":
                stop = threading.Event()
                previous = {
                    name: signal.getsignal(name) for name in (signal.SIGINT, signal.SIGTERM)
                }

                def request_stop(_signum: int, _frame: object) -> None:
                    stop.set()

                try:
                    signal.signal(signal.SIGINT, request_stop)
                    signal.signal(signal.SIGTERM, request_stop)
                    if args.json:
                        _emit({"account": args.account, "folder": "INBOX", "watching": True}, True)
                    FastPathWatcher(config, args.account, stop).run()
                finally:
                    for name, handler in previous.items():
                        signal.signal(name, handler)
                return 0
            results = ImapAdapter(config).sync(args.account, args.folder)
            _emit(
                {
                    "account": args.account,
                    "folder": args.folder,
                    "seen": len(results),
                    "imported": sum(result.created for result in results),
                },
                args.json,
            )
            return 0
        if args.command == "gmail":
            account = next((item for item in config.accounts if item.name == args.account), None)
            if account is None or account.kind != "gmail" or account.gmail is None:
                _error("account is not configured for Gmail")
            if args.gmail_command == "auth":
                email = authorize(account)
                _emit(
                    {"authorized": True, "account": account.name, "profile_email": email}, args.json
                )
                return 0
            if args.gmail_command == "sync":
                result = GmailAdapter(config).sync(args.account)
                _emit(result.__dict__, args.json)
                return 0
            stop = threading.Event()
            previous = {name: signal.getsignal(name) for name in (signal.SIGINT, signal.SIGTERM)}

            def request_stop(_signum: int, _frame: object) -> None:
                stop.set()

            try:
                signal.signal(signal.SIGINT, request_stop)
                signal.signal(signal.SIGTERM, request_stop)
                if args.json:
                    _emit({"account": args.account, "watching": True, "mode": "poll"}, True)
                GmailWatcher(
                    config, args.account, stop, refresh=NotmuchAdapter(config).refresh
                ).run()
            finally:
                for name, handler in previous.items():
                    signal.signal(name, handler)
            return 0
        if args.command == "pop3":
            result = Pop3Adapter(config).sync(args.account)
            _emit(result.__dict__, args.json)
            return 0
    except (
        ConfigError,
        IngestError,
        ImapError,
        GmailError,
        Pop3Error,
        NotmuchError,
        AttachmentError,
        RecollError,
        BorgError,
        ProductionPlanError,
        OSError,
        sqlite3.DatabaseError,
    ) as error:
        _error(str(error))
    _error("unsupported command")


if __name__ == "__main__":
    raise SystemExit(main())
