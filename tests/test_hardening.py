"""Production-hardening tests: backup/restore, fail-closed corruption
probe, per-file import resilience, and the rotating log."""

import zipfile
from pathlib import Path

import pytest

from otlet.config.settings import Settings
from otlet.logs import log_path, setup_logging
from otlet.models.paper import Paper
from otlet.services.importers import import_pdf_file
from otlet.storage.backup import (
    BackupError,
    _rotate,
    create_backup,
    list_backups,
    restore_archive,
    verify_archive,
)
from otlet.storage.database import Database, DatabaseCorruptError
from otlet.storage.pdf_metadata import PDFMetadataExtractor
from otlet.storage.pdf_store import PDFStore


@pytest.fixture
def env(tmp_path: Path):
    """A populated data dir: 2 papers, 1 stored PDF."""
    settings = Settings(data_dir=tmp_path)
    db = Database(settings.db_path)
    db.add_paper(Paper(id="p1", title="Backup Paper One"))
    db.add_paper(Paper(id="p2", title="Backup Paper Two"))
    store = PDFStore(settings.pdf_dir)
    (tmp_path / "src.pdf").write_bytes(b"%PDF-1.4 fake but ours")
    store.import_file(tmp_path / "src.pdf", paper_id="p1")
    db.update_paper("p1", pdf_path=str(store.get_path("p1")))
    db.close()
    return settings


# ── backup / restore ───────────────────────────────────────


def test_backup_roundtrip_with_rotation(env):
    backups = env.data_dir / "backups"
    archive = create_backup(env.db_path, env.pdf_dir, out_dir=backups)
    assert archive.exists()
    manifest = verify_archive(archive)
    assert manifest["papers"] == 2
    assert len(manifest["pdfs"]) == 1

    # mutate the live library after the backup
    db = Database(env.db_path)
    db.add_paper(Paper(id="p3", title="After Backup"))
    db.close()

    result = restore_archive(archive, env.db_path, env.pdf_dir)
    assert result["papers"] == 2
    db = Database(env.db_path)
    assert db.get_paper("p3") is None          # rolled back to snapshot
    assert db.get_paper("p1") is not None
    db.close()
    # the replaced library was renamed aside, never deleted
    asides = list(env.data_dir.glob("library.db.pre-restore-*"))
    assert asides

    # rotation keeps only the newest 7
    for i in range(10):
        (backups / f"otlet-backup-2026010{i + 1}-000000.zip").touch()
    _rotate(backups)
    assert len(list_backups(backups)) == 7


def _tamper(archive: Path, dest: Path) -> None:
    """Copy the archive, replacing library.db's content with garbage."""
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(
        dest, "w", zipfile.ZIP_DEFLATED
    ) as out:
        for name in src.namelist():
            data = (
                b"garbage" * 100 if name == "library.db" else src.read(name)
            )
            out.writestr(name, data)


def test_tampered_archive_refused(env):
    backups = env.data_dir / "backups"
    archive = create_backup(env.db_path, env.pdf_dir, out_dir=backups)
    tampered = backups / "tampered.zip"
    _tamper(archive, tampered)

    with pytest.raises(BackupError, match="hash mismatch"):
        verify_archive(tampered)

    # restore of a tampered archive leaves the live library untouched
    before = Database(env.db_path)
    before.close()
    with pytest.raises(BackupError):
        restore_archive(tampered, env.db_path, env.pdf_dir)
    db = Database(env.db_path)  # still openable, unchanged
    assert db.get_paper("p1") is not None
    db.close()


def test_backup_excludes_config(env):
    (env.data_dir / "config.yaml").write_text(
        "api_key: sk-secret\n", encoding="utf-8"
    )
    archive = create_backup(
        env.db_path, env.pdf_dir, out_dir=env.data_dir / "backups"
    )
    with zipfile.ZipFile(archive) as zf:
        assert not any("config" in n for n in zf.namelist())


# ── fail-closed corruption probe ───────────────────────────


def test_garbage_database_fails_closed(tmp_path: Path):
    db_path = tmp_path / "library.db"
    db_path.write_bytes(b"this is definitely not sqlite" * 100)
    with pytest.raises(DatabaseCorruptError, match="otlet restore"):
        Database(db_path)


def test_corrupt_error_from_cli_main(tmp_path, capsys):
    from otlet import cli

    settings = Settings(data_dir=tmp_path)
    db_path = settings.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"garbage" * 50)
    rc = cli.main(["list"], settings=settings)
    out = capsys.readouterr().out
    assert rc == 2
    assert "otlet restore" in out


def test_valid_database_passes_probe(env):
    db = Database(env.db_path)  # no raise
    db.close()


# ── per-file import resilience ─────────────────────────────


def test_unparseable_pdf_returns_error_not_exception(env):
    bad = env.data_dir / "broken.pdf"
    bad.write_bytes(b"%PDF-1.4\n%%EOF\ntruncated garbage")
    db = Database(env.db_path)
    result = import_pdf_file(
        db, PDFStore(env.pdf_dir), PDFMetadataExtractor(), bad
    )
    db.close()
    assert result["ok"] is False
    assert "unparseable" in result["error"].lower()


def test_batch_continues_past_bad_file(env, monkeypatch):
    """The folder-import loop survives one corrupt PDF."""
    from otlet import cli

    good = env.data_dir / "good.pdf"
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Batch Import Good Paper\nAbstract: x")
    doc.save(good)
    doc.close()
    bad = env.data_dir / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")

    monkeypatch.setattr(
        PDFMetadataExtractor, "_lookup_doi", lambda self, d: None
    )
    monkeypatch.setattr(
        PDFMetadataExtractor, "_lookup_arxiv", lambda self, a: None
    )
    monkeypatch.setattr(
        PDFMetadataExtractor, "_search_title", lambda self, t: None
    )

    rc = cli.main(["add", "--folder", str(env.data_dir)], settings=env)
    assert rc == 0  # loop must finish despite the bad file
    db = Database(env.db_path)
    assert db.get_paper_by_title("Batch Import Good Paper") is not None
    db.close()


# ── logging ────────────────────────────────────────────────


def test_setup_logging_writes_rotating_file(tmp_path):
    path = setup_logging(tmp_path)
    assert path == log_path(tmp_path)
    import logging

    logging.getLogger("otlet").info("hello from the hardening test")
    for h in logging.getLogger("otlet").handlers:
        h.flush()
    assert "hello from the hardening test" in path.read_text(
        encoding="utf-8"
    )
    # idempotent: no duplicate handlers
    setup_logging(tmp_path)
    assert len(logging.getLogger("otlet").handlers) == 1


def test_cmd_log_prints_tail(tmp_path, capsys):
    from otlet import cli

    setup_logging(tmp_path)
    import logging

    logging.getLogger("otlet").error("boom-marker")
    for h in logging.getLogger("otlet").handlers:
        h.flush()
    rc = cli.main(["log", "--lines", "5"], settings=Settings(data_dir=tmp_path))
    out = capsys.readouterr().out
    assert rc == 0 and "boom-marker" in out
