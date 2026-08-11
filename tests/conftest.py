from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "archive": {"root": str(tmp_path / "archive"), "timezone": "UTC"},
                "database": {"path": str(tmp_path / "state" / "mailarchive.sqlite3")},
                "accounts": {
                    "test": {
                        "kind": "imap",
                        "enabled": True,
                        "remote_retention_days": 365,
                        "required_verified_backups": 2,
                        "config_ref": "env:MAILARCHIVE_TEST_SECRET",
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path
