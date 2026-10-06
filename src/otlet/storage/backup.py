"""Snapshot backup & restore.

Discipline (LitBoard backup.js, adapted):
- verify-then-publish: the archive is built at a temp path, its member
  hashes are re-verified from the zip itself, and only then is it
  atomically renamed into place — no half-written backups ever exist;
- rotation keeps the newest KEEP_BACKUPS archives;
- restore never deletes anything: the live database is renamed aside
  (`.pre-restore-<stamp>`), the archive is verified before extraction,
  and a failed post-restore validation rolls everything back;
- the config file is deliberately excluded (it carries API keys).

Archive layout (zip):
    manifest.json   {version, created_at, papers, pdfs: [{name, sha256}]}
    library.db      WAL-checkpointed SQLite copy
    pdfs/<id>.pdf   stored PDF files
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

KEEP_BACKUPS = 7
_MANIFEST = "manifest.json"
_DB_NAME = "library.db"


class BackupError(Exception):
    """Backup/restore failed; message is user-actionable."""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def create_backup(db_path: Path, pdf_dir: Path, *, out_dir: Path) -> Path:
    """Create a verified snapshot archive; returns its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final = out_dir / f"otlet-backup-{stamp}.zip"

    # Fold the WAL into the main file so the plain copy is complete
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        paper_count = conn.execute(
            "SELECT COUNT(*) FROM papers WHERE is_deleted = 0"
        ).fetchone()[0]
    finally:
        conn.close()

    pdfs = sorted(
        p for p in pdf_dir.glob("*.pdf") if p.is_file()
    ) if pdf_dir.is_dir() else []

    tmp = final.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_path, _DB_NAME)
            for pdf in pdfs:
                zf.write(pdf, f"pdfs/{pdf.name}")
            zf.writestr(_MANIFEST, json.dumps({
                "version": 1,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "papers": paper_count,
                "pdfs": [
                    {"name": p.name, "sha256": _sha256(p)} for p in pdfs
                ],
                "db_sha256": _sha256(db_path),
            }, ensure_ascii=False))
        verify_archive(tmp)  # refuse to publish anything unverified
        tmp.replace(final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    _rotate(out_dir)
    return final


def verify_archive(archive: Path) -> dict:
    """Check the archive is readable and every member matches its
    manifest hash. Returns the manifest; raises BackupError otherwise."""
    try:
        with zipfile.ZipFile(archive) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise BackupError(f"corrupt member in archive: {bad}")
            try:
                manifest = json.loads(zf.read(_MANIFEST))
            except (KeyError, json.JSONDecodeError) as e:
                raise BackupError(f"manifest missing or invalid: {e}")
            db_hash = manifest.get("db_sha256")
            if not db_hash:
                raise BackupError("manifest lacks db_sha256")
            actual = hashlib.sha256(zf.read(_DB_NAME)).hexdigest()
            if actual != db_hash:
                raise BackupError("library.db hash mismatch")
            names = set()
            for entry in manifest.get("pdfs", []):
                member = f"pdfs/{entry['name']}"
                names.add(member)
                try:
                    blob = zf.read(member)
                except KeyError:
                    raise BackupError(f"missing member: {member}")
                if hashlib.sha256(blob).hexdigest() != entry["sha256"]:
                    raise BackupError(f"hash mismatch: {member}")
            return manifest
    except zipfile.BadZipFile as e:
        raise BackupError(f"not a valid archive: {e}")


def restore_archive(
    archive: Path, db_path: Path, pdf_dir: Path
) -> dict:
    """Restore a verified archive over the live data directory.

    The current library is renamed aside (never deleted); PDFs are
    overwritten by name. If the restored database fails to open, the
    previous state is rolled back.
    """
    manifest = verify_archive(archive)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    moved: list[tuple[Path, Path]] = []

    def _aside(path: Path) -> None:
        if path.exists():
            dest = path.with_name(f"{path.name}.pre-restore-{stamp}")
            path.rename(dest)
            moved.append((dest, path))

    try:
        _aside(db_path)
        _aside(db_path.with_name(f"{db_path.name}-wal"))
        _aside(db_path.with_name(f"{db_path.name}-shm"))
        pdf_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(archive) as zf:
            zf.extract(_DB_NAME, db_path.parent)
            for entry in manifest.get("pdfs", []):
                zf.extract(f"pdfs/{entry['name']}", pdf_dir.parent)

        # post-restore validation: the database must open and be sane
        conn = sqlite3.connect(str(db_path))
        try:
            check = conn.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            conn.close()
        if check != "ok":
            raise BackupError(f"restored database failed quick_check: {check}")
    except BaseException:
        # roll back: remove half-restored files, move the aside copies back
        db_path.unlink(missing_ok=True)
        for dest, original in moved:
            if dest.exists():
                dest.rename(original)
        raise

    return {
        "papers": manifest.get("papers"),
        "pdfs": len(manifest.get("pdfs", [])),
        "moved_aside": [str(d) for d, _ in moved],
    }


def _rotate(out_dir: Path, keep: int = KEEP_BACKUPS) -> None:
    archives = sorted(
        out_dir.glob("otlet-backup-*.zip"),
        key=lambda p: p.name,  # timestamped names sort chronologically
    )
    for old in archives[:-keep]:
        old.unlink(missing_ok=True)


def list_backups(out_dir: Path) -> list[Path]:
    return sorted(out_dir.glob("otlet-backup-*.zip")) if out_dir.is_dir() else []
