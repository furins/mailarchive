from __future__ import annotations

from pathlib import Path
from shutil import which

import pytest

from mailarchive.config import load_config
from mailarchive.recoll import RecollAdapter, managed_config_text, managed_layout


def test_managed_recoll_config_is_attachment_only(config_file: Path) -> None:
    config = load_config(config_file)
    layout = managed_layout(config)
    text = managed_config_text(layout)
    assert str(config.archive.root / "attachments" / "sha256") in text
    assert "mail quarantine staging state" in text
    assert str(layout.database_directory) in text


def test_recoll_search_uses_argv_and_filters_paths(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config(config_file)
    blob = config.archive.root / "attachments" / "sha256" / "a0" / ("a" * 64)
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"x")
    seen: list[list[str]] = []
    adapter = RecollAdapter(config)

    def run(executable: str, args: list[str]):
        seen.append([executable, *args])
        import subprocess

        return subprocess.CompletedProcess(args, 0, f"{blob}\n/tmp/nope\n", "")

    monkeypatch.setattr(adapter, "_run", run)
    assert adapter.search_paths("word; not-a-shell") == [blob.resolve()]
    assert seen[0][-1] == "word; not-a-shell"


@pytest.mark.skipif(
    which("recollindex") is None or which("recollq") is None,
    reason="Recoll absent",
)
def test_real_recoll_indexes_extensionless_text_blob(config_file: Path) -> None:
    config = load_config(config_file)
    blob = config.archive.root / "attachments" / "sha256" / "a0" / ("a" * 64)
    blob.parent.mkdir(parents=True)
    blob.write_text("m8extensionlesstoken", encoding="utf-8")
    adapter = RecollAdapter(config, timeout_seconds=30)
    adapter.refresh()
    assert adapter.search_paths("m8extensionlesstoken") == [blob.resolve()]
