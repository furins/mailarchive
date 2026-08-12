"""Local-only command-line interface for canonical ingest and derived search."""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import sys
import threading
from pathlib import Path
from typing import NoReturn

from mailarchive.config import ConfigError, display_config, load_config
from mailarchive.db import account_id, connect, initialize
from mailarchive.fastpath import FastPathWatcher, fast_path_status
from mailarchive.gmail import GmailAdapter, GmailError, GmailWatcher, authorize
from mailarchive.imap import ImapAdapter, ImapError
from mailarchive.ingest import IngestError, ingest_file
from mailarchive.notmuch import NotmuchAdapter, NotmuchError, search_canonical_messages


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
    search.add_argument("--config", required=True, help="path to YAML configuration")
    search.add_argument("--json", action="store_true", help="emit JSON")
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
            initialize(config.database.path, config.accounts)
            _emit({"initialized": True, "database_path": str(config.database.path)}, args.json)
            return 0
        if args.command == "status":
            initialized = config.database.path.exists()
            account_count = 0
            canonical_message_count = 0
            schema_version = 0
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
            health = [record.__dict__ for record in fast_path_status(config)] if initialized else []
            gmail_status: list[dict[str, object]] = []
            if initialized:
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
                                "last_safe_error_category": None
                                if state is None
                                else state["last_error_kind"],
                            }
                        )
            _emit(
                {
                    "database_initialized": initialized,
                    "database_path": str(config.database.path),
                    "schema_version": schema_version,
                    "account_count": account_count,
                    "canonical_message_count": canonical_message_count,
                    "remote_mutation_supported": False,
                    "fast_path": health,
                    "gmail": gmail_status,
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
            adapter = NotmuchAdapter(config)
            adapter.refresh()
            _emit({"refreshed": True, "notmuch_version": adapter.version()}, args.json)
            return 0
        if args.command == "search":
            results = search_canonical_messages(config, args.query)
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
    except (
        ConfigError,
        IngestError,
        ImapError,
        GmailError,
        NotmuchError,
        OSError,
        sqlite3.DatabaseError,
    ) as error:
        _error(str(error))
    _error("unsupported command")


if __name__ == "__main__":
    raise SystemExit(main())
