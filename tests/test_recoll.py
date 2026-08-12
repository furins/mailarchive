from __future__ import annotations

from pathlib import Path
from shutil import which

import pytest

from mailarchive.attachments import attachment_blob_path, reconcile_attachments
from mailarchive.classification import ClassificationResult, apply_classification
from mailarchive.config import load_config
from mailarchive.ingest import ingest_bytes
from mailarchive.recoll import (
    RecollAdapter,
    managed_config_text,
    managed_layout,
    search_attachments,
)


def _message(subject: str, attachment: bytes, filename: str = "attachment.bin") -> bytes:
    return (
        (
            f"From: a@example.test\r\nTo: b@example.test\r\nSubject: {subject}\r\n"
            "MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n"
            "--x\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
            f"--x\r\nContent-Type: application/octet-stream; name={filename}\r\n"
            "Content-Disposition: attachment\r\nContent-Transfer-Encoding: base64\r\n\r\n"
        ).encode()
        + __import__("base64").b64encode(attachment)
        + b"\r\n--x--\r\n"
    )


def _finalized(config_file: Path, subject: str, attachment: bytes, state: str):
    config = load_config(config_file)
    result = ingest_bytes(config, _message(subject, attachment), "test")
    return config, apply_classification(
        config,
        result.canonical_message,
        ClassificationResult("ham" if state == "archived" else "spam", None, "fixture"),
    )


def _pdf_with_text(text: str) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        (
            f"<< /Length {len(text) + 35} >>\nstream\n"
            f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET\nendstream"
        ).encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, object_data in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + object_data + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    output.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


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


def test_relative_root_candidate_maps_to_attachment(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    config = load_config(config_file)
    # The existing configuration uses an absolute root, replace only M8's path input.
    from dataclasses import replace

    relative = replace(config, archive=replace(config.archive, root=Path("relative-root")))
    # Recreate under the relative root then feed an absolute Recoll candidate.
    result = ingest_bytes(relative, _message("relative-two", b"relative-token"), "test")
    relative_message = apply_classification(
        relative, result.canonical_message, ClassificationResult("ham", None, "fixture")
    )
    reconcile_attachments(relative)
    digest = next(
        iter((relative.archive.root.resolve() / "attachments" / "sha256").glob("*/*"))
    ).name
    path = attachment_blob_path(relative, digest)

    def candidate(_self: RecollAdapter, _query: str) -> list[Path]:
        return [path.resolve()]

    monkeypatch.setattr(RecollAdapter, "search_paths", candidate)  # pyright: ignore[reportUnknownArgumentType]
    assert [
        result.canonical_id for result in search_attachments(relative, "relative-token")
    ] == [relative_message.id]


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


@pytest.mark.skipif(
    which("recollindex") is None or which("recollq") is None,
    reason="Recoll absent",
)
def test_real_recoll_indexes_extensionless_pdf_blob(config_file: Path) -> None:
    config = load_config(config_file)
    payload = _pdf_with_text("m8extensionlesspdftoken")
    import hashlib

    blob = attachment_blob_path(config, hashlib.sha256(payload).hexdigest())
    blob.parent.mkdir(parents=True)
    blob.write_bytes(payload)
    adapter = RecollAdapter(config, timeout_seconds=30)
    adapter.refresh()
    assert adapter.search_paths("m8extensionlesspdftoken") == [blob.resolve()]


@pytest.mark.skipif(
    which("recollindex") is None or which("recollq") is None,
    reason="Recoll absent",
)
def test_real_recoll_search_scopes_and_rebuild_preserve_authoritative_state(
    config_file: Path,
) -> None:
    config, archived_only = _finalized(config_file, "archive-only", b"m8archiveonly", "archived")
    _, quarantined_only = _finalized(
        config_file, "quarantine-only", b"m8quarantineonly", "quarantined"
    )
    _, archived_shared = _finalized(config_file, "archive-shared", b"m8sharedcontent", "archived")
    _, quarantined_shared = _finalized(
        config_file, "quarantine-shared", b"m8sharedcontent", "quarantined"
    )
    reconcile_attachments(config)
    from mailarchive.db import connect

    with connect(config.database.path) as db:
        catalog_before = [
            tuple(row) for row in db.execute("SELECT id,content_path FROM attachments ORDER BY id")
        ]
    canonical_before = {
        item.id: item.local_path.read_bytes()
        for item in (archived_only, quarantined_only, archived_shared, quarantined_shared)
    }
    adapter = RecollAdapter(config, timeout_seconds=30)
    adapter.refresh()
    assert [item.canonical_id for item in search_attachments(config, "m8archiveonly")] == [
        archived_only.id
    ]
    assert search_attachments(config, "m8quarantineonly") == []
    assert [
        item.canonical_id
        for item in search_attachments(config, "m8quarantineonly", scope="quarantine")
    ] == [quarantined_only.id]
    assert [item.canonical_id for item in search_attachments(config, "m8sharedcontent")] == [
        archived_shared.id
    ]
    assert [
        item.canonical_id
        for item in search_attachments(config, "m8sharedcontent", scope="quarantine")
    ] == [quarantined_shared.id]
    expected_all = [archived_shared.id, quarantined_shared.id]
    assert [
        item.canonical_id for item in search_attachments(config, "m8sharedcontent", scope="all")
    ] == expected_all
    adapter.rebuild()
    assert [
        item.canonical_id for item in search_attachments(config, "m8sharedcontent", scope="all")
    ] == expected_all
    with connect(config.database.path) as db:
        assert [
            tuple(row) for row in db.execute("SELECT id,content_path FROM attachments ORDER BY id")
        ] == catalog_before
    assert {
        item.id: item.local_path.read_bytes()
        for item in (archived_only, quarantined_only, archived_shared, quarantined_shared)
    } == canonical_before
