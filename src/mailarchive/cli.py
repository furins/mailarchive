"""Local-only command-line interface for M0."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import NoReturn

from mailarchive.config import ConfigError, display_config, load_config
from mailarchive.db import connect, initialize


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
            schema_version = 0
            if initialized:
                with connect(config.database.path) as connection:
                    version_row = connection.execute(
                        "SELECT MAX(version) FROM schema_migrations"
                    ).fetchone()
                    account_row = connection.execute("SELECT COUNT(*) FROM accounts").fetchone()
                    schema_version = int(version_row[0] or 0)
                    account_count = int(account_row[0])
            _emit(
                {
                    "database_initialized": initialized,
                    "database_path": str(config.database.path),
                    "schema_version": schema_version,
                    "account_count": account_count,
                    "remote_mutation_supported": False,
                },
                args.json,
            )
            return 0
    except (ConfigError, OSError, sqlite3.DatabaseError) as error:
        _error(str(error))
    _error("unsupported command")


if __name__ == "__main__":
    raise SystemExit(main())
