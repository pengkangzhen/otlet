"""Tests for the per-page PDF full-text index (storage/pdf_index.py)."""

import json
import zlib
from pathlib import Path

import pymupdf
import pytest

from otlet.models.paper import Paper
from otlet.storage.database import Database
from otlet.storage.pdf_index import PDFIndex
from otlet.storage.pdf_store import PDFStore


@pytest.fixture
def db(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "test.db")
    yield instance
    instance.close()


@pytest.fixture
def store(tmp_path: Path) -> PDFStore:
    return PDFStore(tmp_path / "pdfs")


@pytest.fixture
def index(db: Database, store: PDFStore) -> PDFIndex:
    return PDFIndex(db, store)


def _make_pdf(path: Path, *page_texts: str) -> Path:
    doc = pymupdf.open()
    for text in page_texts:
        doc.new_page().insert_text((72, 72), text)
    doc.save(path)
    doc.close()
    return path


def test_build_and_search_english_pages(
    db: Database, store: PDFStore, index: PDFIndex, tmp_path: Path
):
    db.add_paper(Paper(id="p1", title="Network Design"))
    pdf = _make_pdf(
        tmp_path / "p1.pdf",
        "robust supply chain network design under disruption",
        "facility location model with backup facilities",
    )
    store.import_file(pdf, paper_id="p1")
    db.update_paper("p1", pdf_path=str(store.get_path("p1")))

    assert index.build("p1") == 2

    hits = index.search("supply chain")
    assert [(h["paper_id"], h["page"]) for h in hits] == [("p1", 1)]
    # snippet highlights the match; its token budget (trigram tokens)
    # may clip a word tail, so assert on the leading word
    assert "supply" in hits[0]["snippet"].lower()

    # substring semantics: "locat" matches "location"
    hits = index.search("locat")
    assert hits and hits[0]["page"] == 2

    assert index.search("quantum computing") == []


def test_search_chinese_trigram(
    db: Database, store: PDFStore, index: PDFIndex
):
    """CJK search: trigram phrase via FTS, two-char word via fallback scan."""
    db.add_paper(Paper(id="p1", title="供应链韧性"))
    pages = [
        "供应链韧性是物流网络设计的关键目标",
        "本文提出两阶段鲁棒优化模型",
    ]
    blob = zlib.compress(json.dumps(pages, ensure_ascii=False).encode(), 1)
    db.upsert_pdf_text("p1", len(pages), blob)
    db.replace_fts_rows("p1", pages)

    # >=3 chars → FTS trigram phrase
    hits = index.search("网络设计")
    assert [(h["paper_id"], h["page"]) for h in hits] == [("p1", 1)]

    # <3 chars (two-character Chinese word) → decompressed scan
    hits = index.search("韧性")
    assert hits and hits[0]["page"] == 1

    # English against Chinese pages still works via trigram
    hits = index.search("鲁棒优化")
    assert hits and hits[0]["page"] == 2


def test_rebuild_replaces_previous_rows(
    db: Database, store: PDFStore, index: PDFIndex, tmp_path: Path
):
    db.add_paper(Paper(id="p1", title="Rev"))
    pdf = _make_pdf(tmp_path / "p1.pdf", "old keyword alpha")
    store.import_file(pdf, paper_id="p1")
    db.update_paper("p1", pdf_path=str(store.get_path("p1")))
    index.build("p1")
    assert index.search("alpha")

    # Same paper, new content replaces the old index rows entirely
    pdf2 = _make_pdf(tmp_path / "p1-v2.pdf", "brand new keyword beta")
    store._dir.joinpath("p1.pdf").unlink()
    store.import_file(pdf2, paper_id="p1")
    index.build("p1")

    assert index.search("alpha") == []
    assert index.search("brand new keyword")


def test_soft_deleted_paper_excluded_from_search(
    db: Database, store: PDFStore, index: PDFIndex
):
    db.add_paper(Paper(id="p1", title="Gone"))
    db.replace_fts_rows("p1", ["unique disappearing keyword zebra"])
    assert index.search("disappearing keyword")

    db.delete_paper("p1")
    assert index.search("disappearing keyword") == []


def test_get_pages_window(
    db: Database, store: PDFStore, index: PDFIndex, tmp_path: Path
):
    db.add_paper(Paper(id="p1", title="Pages"))
    pdf = _make_pdf(tmp_path / "p1.pdf", "page one text", "page two text",
                    "page three text")
    store.import_file(pdf, paper_id="p1")
    db.update_paper("p1", pdf_path=str(store.get_path("p1")))
    index.build("p1")

    pages = index.get_pages("p1")
    assert [p["page"] for p in pages] == [1, 2, 3]
    assert pages[0]["text"].startswith("page one")
    assert pages[0]["char_total"] == len(pages[0]["text"])

    window = index.get_pages("p1", from_page=2, to_page=3)
    assert [p["page"] for p in window] == [2, 3]

    assert index.get_pages("unknown") is None


def test_papers_missing_index_backfill_list(
    db: Database, store: PDFStore, index: PDFIndex, tmp_path: Path
):
    db.add_paper(Paper(id="p1", title="Indexed"))
    pdf = _make_pdf(tmp_path / "p1.pdf", "content")
    store.import_file(pdf, paper_id="p1")
    db.update_paper("p1", pdf_path=str(store.get_path("p1")))

    db.add_paper(Paper(id="p2", title="No PDF"))  # no pdf_path → ignored

    assert db.papers_missing_index() == ["p1"]
    index.build("p1")
    assert db.papers_missing_index() == []
