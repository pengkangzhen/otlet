"""Tests for the shared import pipeline (services/importers.py)."""

from pathlib import Path

import pymupdf
import pytest

from agent_lit.config.settings import Settings
from agent_lit.models.author import Author
from agent_lit.models.paper import Paper
from agent_lit.services.importers import (
    entry_to_paper,
    extract_auto_tags,
    find_duplicate,
    import_pdf_file,
    infer_paper_type,
    parse_bibtex,
    save_zotero_item,
)
from agent_lit.storage.database import Database
from agent_lit.storage.pdf_metadata import PDFMetadataExtractor
from agent_lit.storage.pdf_store import PDFStore

_BIB = """\
@inproceedings{vaswani2017attention,
  title = {Attention Is All You Need},
  author = {Vaswani, Ashish and Shazeer, Noam},
  year = {2017},
  booktitle = {Advances in Neural Information Processing Systems},
  doi = {10.5555/3294771},
  keywords = {transformer, attention; sequence-to-sequence},
  abstract = {We propose the Transformer, a state-machine architecture \\
based solely on attention mechanisms.}
}

@article{demo2020,
  title = {Resilient Supply Chain Network Design},
  author = {Wang, Wei},
  year = {2020},
  journal = {Operations Research},
  volume = {68},
  number = {1},
  pages = {1--20},
  publisher = {INFORMS},
  doi = {10.1287/opre.2020.1},
}
"""


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    settings = Settings(data_dir=tmp_path)
    instance = Database(settings.db_path)
    yield instance
    instance.close()


# ── BibTeX parsing ─────────────────────────────────────────


def test_parse_bibtex_extracts_entries_and_fields():
    entries = parse_bibtex(_BIB)
    assert len(entries) == 2
    first = entries[0]
    assert first["_key"] == "vaswani2017attention"
    assert first["_type"] == "inproceedings"
    assert first["title"] == "Attention Is All You Need"
    assert first["doi"] == "10.5555/3294771"


def test_parse_bibtex_skips_non_entry_blocks():
    text = "@comment{ignore me}\n" + _BIB
    entries = parse_bibtex(text)
    assert len(entries) == 2


def test_entry_to_paper_maps_fields():
    entry = parse_bibtex(_BIB)[0]
    paper = entry_to_paper(entry)
    assert paper.title == "Attention Is All You Need"
    assert paper.bibtex_key == "vaswani2017attention"
    assert paper.paper_type == "conference"
    # "Last, First and Last, First" → full names in display order
    assert [a.name for a in paper.authors] == ["Ashish Vaswani", "Noam Shazeer"]
    assert a_first_last(paper.authors[0]) == ("Ashish", "Vaswani")
    assert paper.keywords == ["transformer", "attention", "sequence-to-sequence"]
    # keywords become deterministic auto tags on import
    assert "transformer" in paper.auto_tags
    assert "attention" in paper.auto_tags


def a_first_last(author: Author) -> tuple[str, str]:
    return (author.first_name or "", author.last_name or "")


# ── auto tags & paper type ──────────────────────────────────


def test_extract_auto_tags_venue_mapping():
    paper = Paper(title="T", venue="NeurIPS 2019")
    assert "neural-networks" in extract_auto_tags(paper)


def test_extract_auto_tags_caps_output():
    paper = Paper(
        title="T",
        keywords=[f"kw{i}" for i in range(20)],
    )
    assert len(extract_auto_tags(paper)) <= 12


def test_infer_paper_type_patterns():
    assert (
        infer_paper_type("International Conference on Machine Learning")
        == "conference"
    )
    assert infer_paper_type("IEEE Transactions on Logistics") == "journal"
    assert infer_paper_type("arXiv preprint") == "preprint"
    assert infer_paper_type(None) is None


# ── dedup ───────────────────────────────────────────────────


def test_find_duplicate_by_doi_then_title(db: Database):
    db.add_paper(Paper(
        title="Original Title",
        doi="10.1/x",
        authors=[Author(name="A B")],
    ))
    by_doi = find_duplicate(db, Paper(title="Different", doi="10.1/X"))
    assert by_doi is not None and by_doi.doi == "10.1/x"
    by_title = find_duplicate(db, Paper(title="original   title"))
    assert by_title is not None


def test_find_duplicate_no_match(db: Database):
    assert find_duplicate(db, Paper(title="Brand New")) is None


# ── PDF import flow ─────────────────────────────────────────


@pytest.fixture()
def offline_extractor(monkeypatch: pytest.MonkeyPatch) -> PDFMetadataExtractor:
    """Extractor with S2 network lookups disabled → text fallback strategy."""
    for m in ("_lookup_doi", "_lookup_arxiv", "_search_title"):
        monkeypatch.setattr(PDFMetadataExtractor, m, lambda self, *a: None)
    return PDFMetadataExtractor()


def _make_pdf(path: Path, text: str) -> Path:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    doc.save(path)
    doc.close()
    return path


def test_import_pdf_file_success(
    db: Database, tmp_path: Path, offline_extractor: PDFMetadataExtractor
):
    pdf = _make_pdf(
        tmp_path / "paper.pdf",
        "Fallback Import Test Paper On Ports\nAbstract: none",
    )
    store = PDFStore(tmp_path / "pdfs")

    result = import_pdf_file(db, store, offline_extractor, pdf)

    assert result["ok"] is True
    paper = db.get_paper(result["paper"]["id"])
    assert paper is not None
    assert paper.pdf_path and Path(paper.pdf_path).exists()


def test_import_pdf_file_duplicate_attaches_pdf(
    db: Database, tmp_path: Path, offline_extractor: PDFMetadataExtractor
):
    db.add_paper(Paper(title="Fallback Import Test Paper On Ports"))
    pdf = _make_pdf(
        tmp_path / "paper.pdf",
        "Fallback Import Test Paper On Ports\nAbstract: none",
    )
    store = PDFStore(tmp_path / "pdfs")

    result = import_pdf_file(db, store, offline_extractor, pdf)

    assert result["ok"] is False
    assert result["duplicate"] is True
    assert result["attached"] is True
    dup = db.get_paper(result["existing_id"])
    assert dup.pdf_path and Path(dup.pdf_path).exists()


def test_import_pdf_file_missing(db: Database, tmp_path: Path,
                                 offline_extractor: PDFMetadataExtractor):
    store = PDFStore(tmp_path / "pdfs")
    result = import_pdf_file(
        db, store, offline_extractor, tmp_path / "nope.pdf"
    )
    assert result["ok"] is False
    assert "not found" in result["error"].lower()


# ── Zotero item save ────────────────────────────────────────


def test_save_zotero_item_roundtrip(db: Database, tmp_path: Path):
    store = PDFStore(tmp_path / "pdfs")
    item = {
        "ok": True,
        "paper": Paper(title="Zotero Paper", doi="10.9/z").model_dump(mode="json"),
        "pdf_path": None,
    }
    result = save_zotero_item(db, store, item)
    assert result["ok"] is True
    assert db.get_paper(result["id"]) is not None


def test_save_zotero_item_duplicate(db: Database, tmp_path: Path):
    db.add_paper(Paper(title="Zotero Paper", doi="10.9/z"))
    store = PDFStore(tmp_path / "pdfs")
    item = {
        "ok": True,
        "paper": Paper(title="Zotero Paper", doi="10.9/z").model_dump(mode="json"),
        "pdf_path": None,
    }
    result = save_zotero_item(db, store, item)
    assert result["ok"] is False and result["duplicate"] is True


def test_save_zotero_item_invalid(db: Database, tmp_path: Path):
    store = PDFStore(tmp_path / "pdfs")
    result = save_zotero_item(db, store, {"ok": False})
    assert result["ok"] is False
