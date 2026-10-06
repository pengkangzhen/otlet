"""Tests for BibTeX export generation."""

from otlet.models.author import Author
from otlet.models.paper import Paper
from otlet.services.importers import parse_bibtex
from otlet.storage.bibtex_export import generate_bibtex, paper_to_bibtex


def _paper(**kwargs) -> Paper:
    defaults = dict(
        title="Attention Is All You Need",
        authors=[Author(name="Ashish Vaswani"), Author(name="Samy Bengio")],
        year=2017,
        venue="NeurIPS",
        doi="10.5555/3294771",
        paper_type="conference",
        tags=["transformer", "nlp"],
    )
    defaults.update(kwargs)
    return Paper(**defaults)


def test_entry_type_and_fields():
    entry = paper_to_bibtex(_paper(), "vaswani2017attention")
    assert entry.startswith("@inproceedings{vaswani2017attention,")
    assert "title = {Attention Is All You Need}" in entry
    assert "year = {2017}" in entry
    assert "booktitle = {NeurIPS}" in entry
    assert "doi = {10.5555/3294771}" in entry
    # BibTeX author convention: "Last, First and Last, First"
    assert "author = {Vaswani, Ashish and Bengio, Samy}" in entry
    # tags merged into keywords
    assert "transformer" in entry and "nlp" in entry


def test_journal_uses_journal_field():
    entry = paper_to_bibtex(_paper(paper_type="journal", venue="Nature"), "k1")
    assert entry.startswith("@article{k1,")
    assert "journal = {Nature}" in entry


def test_key_generation_and_dedup():
    papers = [
        _paper(),
        _paper(),  # same author/year/title → key collision
        _paper(authors=[Author(name="No Name")], bibtex_key=""),
    ]
    bib = generate_bibtex(papers)
    # First two collide: vaswani2017attention, vaswani2017attention2
    assert "@inproceedings{vaswani2017attention," in bib
    assert "@inproceedings{vaswani2017attention2," in bib
    # Third paper has no bibtex_key → generated from author/year
    # ("No Name" → last name "Name")
    assert "@inproceedings{name2017attention," in bib


def test_escape_special_characters():
    entry = paper_to_bibtex(_paper(title="A & B # 100%"), "k")
    assert r"\&" in entry and r"\#" in entry and r"\%" in entry


def test_roundtrip_with_bibtex_parser():
    """Generated .bib must be re-parseable by the app's own importer."""
    bib = generate_bibtex([_paper(), _paper(title="Deep Residual Learning")])
    entries = parse_bibtex(bib)
    assert len(entries) == 2
    titles = {e["title"] for e in entries}
    assert "Attention Is All You Need" in titles
    assert "Deep Residual Learning" in titles


def test_author_first_last_preferred_over_split():
    """Explicit first/last names beat heuristic name splitting."""
    paper = _paper(authors=[
        Author(name="Ashish Vaswani", first_name="Ashish", last_name="Vaswani")
    ])
    entry = paper_to_bibtex(paper, "k")
    assert "author = {Vaswani, Ashish}" in entry


def test_volume_issue_pages_publisher_emitted():
    paper = _paper(
        paper_type="journal", venue="Nature",
        volume="613", issue="3", pages="100-110", publisher="Springer",
    )
    entry = paper_to_bibtex(paper, "k")
    assert "volume = {613}" in entry
    assert "number = {3}" in entry
    assert "pages = {100-110}" in entry
    assert "publisher = {Springer}" in entry
