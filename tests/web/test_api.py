"""Regression tests for the drag-and-drop import chain (Api.import_pdf_base64)."""

import base64
import json
from pathlib import Path

import pymupdf
import pytest

from agent_lit.config.settings import Settings
from agent_lit.storage.pdf_metadata import PDFMetadataExtractor
from agent_lit.web.api import Api


@pytest.fixture()
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Api:
    # Keep the extractor offline: S2 lookups return nothing so extraction
    # falls through to the local text-fallback strategy
    for m in ("_lookup_doi", "_lookup_arxiv", "_search_title"):
        monkeypatch.setattr(PDFMetadataExtractor, m, lambda self, *a: None)

    settings = Settings(data_dir=tmp_path)
    settings.pdf_dir.mkdir(parents=True, exist_ok=True)
    instance = Api(settings)
    yield instance
    instance.close()


def _pdf_bytes(path: Path, text: str) -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    doc.save(path)
    doc.close()
    return path.read_bytes()


def test_import_pdf_base64_returns_without_raising(api: Api, tmp_path: Path):
    """The finally-block cleanup must never swallow the import result.

    Regression (2026-08-22): tmp_dir.rmdir(missing_ok=True) raised TypeError
    after the import had succeeded, so every drag-and-drop import reported
    failure to the frontend while papers were silently written (or not).
    """
    pdf = _pdf_bytes(
        tmp_path / "paper.pdf",
        "Regression Test Paper On Xylophone Logistics\nAbstract: none",
    )
    b64 = base64.b64encode(pdf).decode()

    result = json.loads(api.import_pdf_base64("paper.pdf", b64))

    assert result["ok"] is True
    assert result["paper"]["pdf_path"]
    assert api._db.get_paper(result["paper"]["id"]) is not None


def test_import_pdf_base64_duplicate_detected(api: Api, tmp_path: Path):
    pdf = _pdf_bytes(
        tmp_path / "paper.pdf",
        "Duplicate Detection Test Paper\nAbstract: none",
    )
    b64 = base64.b64encode(pdf).decode()

    first = json.loads(api.import_pdf_base64("paper.pdf", b64))
    second = json.loads(api.import_pdf_base64("paper.pdf", b64))

    assert first["ok"] is True
    assert second["duplicate"] is True
    assert second["existing_id"] == first["paper"]["id"]


def test_duplicate_drop_attaches_pdf_and_group_filing(api: Api, tmp_path: Path):
    """A duplicate drop must attach its PDF to the existing paper, and papers
    can be filed directly into a project (group_papers) and show up in the
    perspective view."""
    pdf = _pdf_bytes(
        tmp_path / "paper.pdf",
        "Attach On Duplicate Test Paper\nAbstract: none",
    )
    b64 = base64.b64encode(pdf).decode()

    first = json.loads(api.import_pdf_base64("paper.pdf", b64))
    # Simulate a paper imported via search (no PDF), then the PDF is dropped
    api._db.update_paper_pdf(first["paper"]["id"], "")
    second = json.loads(api.import_pdf_base64("paper.pdf", b64))

    assert second["duplicate"] is True
    assert second["attached"] is True
    stored = api._db.get_paper(first["paper"]["id"])
    assert stored.pdf_path  # PDF now attached to the existing paper

    # Project filing: perspective view exposes paper_ids for the group
    pid = json.loads(api.create_perspective("Default"))["id"]
    gid = json.loads(api.create_group(pid, "Project A"))["id"]
    paper_id = first["paper"]["id"]
    assert json.loads(api.add_paper_to_group(paper_id, gid))["ok"] is True

    view = json.loads(api.get_perspective_view(pid))["groups"]
    assert view[0]["paper_ids"] == [paper_id]
    assert api._db.get_group_papers(gid)[0]["paper_id"] == paper_id

    api.remove_paper_from_group(paper_id, gid)
    assert api._db.get_group_papers(gid) == []


def test_auto_tag_paper_links_auto_tags(api: Api, monkeypatch: pytest.MonkeyPatch):
    """auto_tag_paper stores LLM suggestions as auto-source tag links."""
    from agent_lit.agents.classify import ClassifyAgent
    from agent_lit.models.paper import Paper

    api._db.add_paper(Paper(id="p1", title="Resilience", abstract="port resilience"))

    def fake_run(self, paper):
        return ["port-resilience", "bayesian-network"]

    monkeypatch.setattr(ClassifyAgent, "run", fake_run)
    d = json.loads(api.auto_tag_paper("p1"))
    assert d["ok"] is True
    assert d["count"] == 2

    paper = api._db.get_paper("p1")
    assert set(paper.auto_tags) == {"port-resilience", "bayesian-network"}
    assert paper.tags == []  # stays out of the manual sidebar view

    # Unknown paper id is reported, not raised
    d2 = json.loads(api.auto_tag_paper("missing"))
    assert d2["ok"] is False


def test_open_file_dialog_multi_select_returns_paths_array(api: Api, tmp_path: Path):
    """Browse must allow selecting several PDFs at once and return them all.

    The frontend bulk-import path feeds each entry of `paths` into
    bulkImportPaths, so a single-path return value would silently drop
    every file after the first.
    """
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.touch()
    b.touch()
    calls: dict = {}

    class FakeWindow:
        def create_file_dialog(self, dialog_type, **kwargs):
            calls["kwargs"] = kwargs
            return (str(a), str(b))

    api._window = FakeWindow()
    result = json.loads(api.open_file_dialog())

    assert calls["kwargs"].get("allow_multiple") is True
    assert result["paths"] == [str(a.resolve()), str(b.resolve())]


def test_export_bibtex_subset_writes_only_selected_papers(api: Api, tmp_path: Path):
    """paper_ids restricts the export (single paper / one project)."""
    from agent_lit.models.paper import Paper

    api._db.add_paper(Paper(id="p1", title="Alpha Paper On Ports", year=2021))
    api._db.add_paper(Paper(id="p2", title="Beta Paper On Rails", year=2022))

    out = tmp_path / "subset.bib"
    d = json.loads(api.export_bibtex(str(out), json.dumps(["p2"]), "beta.bib"))

    assert d["ok"] is True
    assert d["count"] == 1
    text = out.read_text(encoding="utf-8")
    assert "Beta Paper On Rails" in text
    assert "Alpha Paper" not in text

    # Empty subset / unknown ids must not write a file
    d2 = json.loads(
        api.export_bibtex(str(tmp_path / "empty.bib"), json.dumps(["nope"]))
    )
    assert d2["ok"] is False
    assert not (tmp_path / "empty.bib").exists()


def test_auto_tag_paper_nlp_method_needs_no_llm(api: Api):
    """method='nlp' runs the offline extractor with zero LLM configuration."""
    from agent_lit.models.paper import Paper

    api._db.add_paper(Paper(
        id="p9",
        title="Resilient Supply Chain Network Design",
        abstract="Supply chain network design under demand uncertainty.",
    ))
    d = json.loads(api.auto_tag_paper("p9", "nlp"))
    assert d["ok"] is True
    assert d["count"] >= 1

    paper = api._db.get_paper("p9")
    assert paper.auto_tags, "extracted tags must be linked as auto source"


def test_reveal_pdf_requires_existing_file(
    api: Api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Reveal in Finder: runs `open -R` only when a real PDF file is attached."""
    import subprocess

    from agent_lit.models.paper import Paper

    # No PDF attached → refused
    api._db.add_paper(Paper(id="pr", title="Reveal Me"))
    d = json.loads(api.reveal_pdf("pr"))
    assert d["ok"] is False

    # PDF attached but missing on disk → refused
    api._db.update_paper_pdf("pr", str(tmp_path / "gone.pdf"))
    d = json.loads(api.reveal_pdf("pr"))
    assert d["ok"] is False

    # Real file → `open -R <path>` invoked
    pdf = tmp_path / "real.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    api._db.update_paper_pdf("pr", str(pdf))
    calls: list = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or True)
    d = json.loads(api.reveal_pdf("pr"))
    assert d["ok"] is True
    assert calls and list(calls[0][0][:2]) == ["open", "-R"]
