"""Tests for PDF metadata extraction module."""

from pathlib import Path

import pytest

from otlet.storage.pdf_metadata import PDFMetadataExtractor


class TestDOIExtraction:
    """Test DOI regex extraction from text."""

    def test_standard_doi(self):
        text = "DOI: 10.1038/s41586-020-2649-2"
        assert PDFMetadataExtractor._find_doi(text) == "10.1038/s41586-020-2649-2"

    def test_doi_url(self):
        text = "https://doi.org/10.5555/1234567"
        assert PDFMetadataExtractor._find_doi(text) == "10.5555/1234567"

    def test_doi_with_prefix(self):
        text = "doi 10.1145/3290605.3300843"
        assert PDFMetadataExtractor._find_doi(text) == "10.1145/3290605.3300843"

    def test_no_doi(self):
        text = "This paper has no identifier"
        assert PDFMetadataExtractor._find_doi(text) is None

    def test_invalid_short_doi(self):
        text = "10.1/ab"
        assert PDFMetadataExtractor._find_doi(text) is None


class TestArXivExtraction:
    """Test ArXiv ID extraction."""

    def test_standard_arxiv(self):
        text = "arXiv:2301.01234"
        assert PDFMetadataExtractor._find_arxiv_id(text) == "2301.01234"

    def test_arxiv_with_version(self):
        text = "arXiv:2301.01234v2"
        assert PDFMetadataExtractor._find_arxiv_id(text) == "2301.01234v2"

    def test_no_arxiv(self):
        text = "Regular paper text"
        assert PDFMetadataExtractor._find_arxiv_id(text) is None


class TestTitleExtraction:
    """Test heuristic title extraction."""

    def test_title_on_first_line(self):
        text = "Attention Is All You Need\nAshish Vaswani et al.\nAbstract"
        title = PDFMetadataExtractor._extract_candidate_title(text)
        assert title == "Attention Is All You Need"

    def test_skip_short_lines(self):
        text = "1\n2\nHello World\nA Deep Learning Approach to NLP Tasks\n"
        title = PDFMetadataExtractor._extract_candidate_title(text)
        assert title is not None
        assert len(title) > 15

    def test_skip_university_lines(self):
        text = (
            "Stanford University\n"
            "Department of CS\n"
            "Deep Reinforcement Learning for Robotics\n"
        )
        title = PDFMetadataExtractor._extract_candidate_title(text)
        assert title == "Deep Reinforcement Learning for Robotics"

    def test_no_title_found(self):
        text = "email@test.com\n123\nabstract"
        title = PDFMetadataExtractor._extract_candidate_title(text)
        assert title is None


class TestAuthorHeuristic:
    """Test author name heuristic extraction."""

    def test_comma_separated_authors(self):
        text = "Alice Smith, Bob Jones, Carol White\nAbstract"
        authors = PDFMetadataExtractor._extract_authors_heuristic(text)
        names = [a.name for a in authors]
        assert "Alice Smith" in names
        assert "Bob Jones" in names

    def test_no_authors_from_abstract(self):
        text = "Abstract: This paper presents a method"
        authors = PDFMetadataExtractor._extract_authors_heuristic(text)
        assert len(authors) == 0


class TestExtractorWithFakePDF:
    """Test the full extraction pipeline with a generated PDF."""

    @pytest.fixture
    def sample_pdf(self, tmp_path: Path) -> Path:
        """Create a minimal PDF with a DOI."""
        import pymupdf

        pdf_path = tmp_path / "test_paper.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text(
            (72, 72),
            "Attention Is All You Need\n"
            "Ashish Vaswani, Noam Shazeer\n\n"
            "doi: 10.5555/3295222.3295349\n",
        )
        doc.save(str(pdf_path))
        doc.close()
        return pdf_path

    def test_extract_doi_from_pdf(self, sample_pdf: Path):
        import pymupdf

        # Only test the text-based DOI extraction (no API calls)
        doc = pymupdf.open(str(sample_pdf))
        text = PDFMetadataExtractor._extract_first_pages(doc, 3)
        doc.close()

        doi = PDFMetadataExtractor._find_doi(text)
        assert doi == "10.5555/3295222.3295349"

    def test_nonexistent_file(self):
        extractor = PDFMetadataExtractor()
        result = extractor.extract(Path("/nonexistent/file.pdf"))
        assert result.paper is None
        assert result.method == "file_not_found"
