from __future__ import annotations

from pathlib import Path

import pytest

from mailarchive.cli import build_parser, main


def test_cli_help_works(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
    assert "MailArchive local safety baseline" in capsys.readouterr().out


def test_config_check_exit_status(config_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "check", "--config", str(config_file), "--json"]) == 0
    assert '"valid": true' in capsys.readouterr().out
    with pytest.raises(SystemExit) as result:
        main(["config", "check", "--config", str(config_file.with_name("absent.yaml"))])
    assert result.value.code == 2


def test_no_destructive_remote_command_appears() -> None:
    help_text = build_parser().format_help()
    assert "delete" not in help_text.lower()
    assert "remote" not in help_text.lower()


def test_db_init_and_status(config_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["db", "init", "--config", str(config_file)]) == 0
    assert main(["status", "--config", str(config_file), "--json"]) == 0
    assert '"database_initialized": true' in capsys.readouterr().out


def test_ingest_json_output(
    config_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "message.eml"
    source.write_bytes(b"Message-ID: <cli@example.test>\r\n\r\nbody\r\n")
    assert (
        main(
            [
                "ingest",
                str(source),
                "--account",
                "test",
                "--config",
                str(config_file),
                "--json",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert '"created": true' in output
    assert '"canonical_message_id"' in output


def test_cli_has_no_remote_or_network_command() -> None:
    help_text = build_parser().format_help().lower()
    assert "remote" not in help_text
    assert "network" not in help_text
