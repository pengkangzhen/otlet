"""Offline tests for the CLI command surface (cli.py).

All commands run against a throwaway data dir; network-dependent paths
(online search, DOI import, LLM) are not exercised here.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from otlet.cli import main
from otlet.config.settings import Settings


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path)


@pytest.fixture()
def bib_file(tmp_path: Path) -> Path:
    bib = tmp_path / "test.bib"
    bib.write_text(
        """\
@article{demo2020,
  title = {Resilient Supply Chain Network Design},
  author = {Wang, Wei and Zhang, San},
  year = {2020},
  journal = {Operations Research},
  doi = {10.1287/opre.2020.1234},
  keywords = {supply chain; resilience},
  abstract = {We study resilient supply chain network design.}
}
""",
        encoding="utf-8",
    )
    return bib


def _first_paper_id(settings: Settings) -> str:
    conn = sqlite3.connect(settings.db_path)
    row = conn.execute("SELECT id FROM papers LIMIT 1").fetchone()
    conn.close()
    return row[0]


# ── add / list / show ───────────────────────────────────────


def test_add_bibtex_then_list(settings: Settings, bib_file: Path):
    assert main(["add", "--bibtex", str(bib_file)], settings=settings) == 0
    assert main(["list"], settings=settings) == 0
    assert _count_papers(settings) == 1


def test_add_bibtex_dedup(settings: Settings, bib_file: Path):
    main(["add", "--bibtex", str(bib_file)], settings=settings)
    main(["add", "--bibtex", str(bib_file)], settings=settings)
    assert _count_papers(settings) == 1


def test_add_bibtex_missing_file(settings: Settings):
    rc = main(["add", "--bibtex", "/nonexistent.bib"], settings=settings)
    assert rc == 1


def test_list_json(settings: Settings, bib_file: Path, capsys):
    main(["add", "--bibtex", str(bib_file)], settings=settings)
    capsys.readouterr()  # discard the add-command output
    main(["list", "--json"], settings=settings)
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["title"] == "Resilient Supply Chain Network Design"


def test_list_empty(settings: Settings):
    assert main(["list"], settings=settings) == 0


def test_list_query_and_tag_filter(settings: Settings, bib_file: Path):
    main(["add", "--bibtex", str(bib_file)], settings=settings)
    pid = _first_paper_id(settings)
    main(["tag", "add", pid, "resilience"], settings=settings)

    main(["list", "-q", "resilient"], settings=settings)
    main(["list", "--tags", "resilience"], settings=settings)
    main(["list", "--tags", "nonexistent"], settings=settings)
    assert _count_papers(settings) == 1


def test_show(settings: Settings, bib_file: Path):
    main(["add", "--bibtex", str(bib_file)], settings=settings)
    assert main(["show", _first_paper_id(settings)], settings=settings) == 0


def test_show_unknown_id(settings: Settings):
    assert main(["show", "deadbeef"], settings=settings) == 1


def _count_papers(settings: Settings) -> int:
    conn = sqlite3.connect(settings.db_path)
    n = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    conn.close()
    return n


# ── tags / autotag ──────────────────────────────────────────


def test_tag_add_remove_and_tags_list(settings: Settings, bib_file: Path):
    main(["add", "--bibtex", str(bib_file)], settings=settings)
    pid = _first_paper_id(settings)

    assert main(["tag", "add", pid, "Review", "baseline"], settings=settings) == 0
    assert main(["tags"], settings=settings) == 0
    assert main(["tag", "remove", pid, "Review"], settings=settings) == 0


def test_tags_full_cycle(settings: Settings, bib_file: Path):
    main(["add", "--bibtex", str(bib_file)], settings=settings)
    pid = _first_paper_id(settings)
    main(["tag", "add", pid, "old-name"], settings=settings)

    assert main(["tags", "rename", "old-name", "new-name"], settings=settings) == 0
    assert main(["tags", "delete", "new-name"], settings=settings) == 0


def test_autotag_nlp(settings: Settings, bib_file: Path):
    main(["add", "--bibtex", str(bib_file)], settings=settings)
    pid = _first_paper_id(settings)
    assert main(["autotag", pid, "--method", "nlp"], settings=settings) == 0


# ── notes ───────────────────────────────────────────────────


def test_note_cycle(settings: Settings, bib_file: Path):
    main(["add", "--bibtex", str(bib_file)], settings=settings)
    pid = _first_paper_id(settings)

    assert main(["note", "add", pid, "Key baseline."], settings=settings) == 0
    assert main(["note", "list", pid], settings=settings) == 0

    conn = sqlite3.connect(settings.db_path)
    note_id = conn.execute(
        "SELECT id FROM paper_notes LIMIT 1"
    ).fetchone()[0]
    conn.close()
    assert main(["note", "rm", note_id], settings=settings) == 0


def test_note_rm_requires_verb(settings: Settings):
    with pytest.raises(SystemExit):
        main(["note"], settings=settings)


# ── trash ───────────────────────────────────────────────────


def test_rm_trash_restore_cycle(settings: Settings, bib_file: Path):
    main(["add", "--bibtex", str(bib_file)], settings=settings)
    pid = _first_paper_id(settings)

    main(["rm", pid], settings=settings)
    assert _live_papers(settings) == 0
    main(["trash"], settings=settings)
    assert main(["trash", "restore", pid], settings=settings) == 0
    assert _live_papers(settings) == 1


def test_trash_purge(settings: Settings, bib_file: Path):
    main(["add", "--bibtex", str(bib_file)], settings=settings)
    pid = _first_paper_id(settings)
    main(["rm", pid], settings=settings)
    assert main(["trash", "purge", pid, "--yes"], settings=settings) == 0
    assert _count_papers(settings) == 0


def _live_papers(settings: Settings) -> int:
    conn = sqlite3.connect(settings.db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM papers WHERE is_deleted = 0"
    ).fetchone()[0]
    conn.close()
    return n


# ── export ──────────────────────────────────────────────────


def test_export(settings: Settings, bib_file: Path, tmp_path: Path):
    main(["add", "--bibtex", str(bib_file)], settings=settings)
    out = tmp_path / "export.bib"
    assert main(["export", "-o", str(out)], settings=settings) == 0
    content = out.read_text(encoding="utf-8")
    assert "Resilient Supply Chain Network Design" in content


def test_export_empty_library(settings: Settings):
    assert main(["export"], settings=settings) == 1


# ── settings ────────────────────────────────────────────────


def test_settings_show(settings: Settings):
    assert main(["settings"], settings=settings) == 0


def test_settings_set_valid_and_invalid(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    # Never let a test touch the real ~/.otlet/config.yaml —
    # redirect Settings.save to a no-op recorder
    saved: dict = {}
    monkeypatch.setattr(
        Settings, "save", lambda self, p=None: saved.update(self.model_dump())
    )

    assert main(
        ["settings", "set", "model", "glm-4.7"], settings=settings
    ) == 0
    assert settings.model == "glm-4.7"
    assert saved["model"] == "glm-4.7"

    assert main(["settings", "set", "theme", "neon"], settings=settings) == 1
    assert main(["settings", "set", "no_such_key", "x"], settings=settings) == 1
