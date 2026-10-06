"""PDF metadata extraction — identify a paper from its PDF file.

Strategy (mirrors Zotero's approach):
1. Check PDF embedded XMP metadata for DOI/title
2. Extract text from first pages, regex-match DOI
3. Query Semantic Scholar by DOI → full metadata
4. Fallback: extract candidate title, search S2 by title
5. Last resort: use LLM to parse first-page text into metadata
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import httpx
import pymupdf

from agent_lit.models.author import Author
from agent_lit.models.paper import Paper

# DOI regex — covers 10.xxxx/... patterns found in academic PDFs
_DOI_RE = re.compile(
    r"(?:doi[:\s]*|https?://doi\.org/)"
    r"(10\.\d{4,9}/[^\s\"'>,\]]+)",
    re.IGNORECASE,
)

# ArXiv ID regex
_ARXIV_RE = re.compile(
    r"(?:arXiv:?\s*)(\d{4}\.\d{4,5}(?:v\d+)?)", re.IGNORECASE
)

_S2_BASE = "https://api.semanticscholar.org/graph/v1"
_S2_FIELDS = (
    "paperId,title,year,venue,externalIds,url,abstract,"
    "citationCount,authors,fieldsOfStudy"
)


@dataclass
class MetadataResult:
    """Result of PDF metadata extraction."""

    paper: Paper | None = None
    method: str = ""
    confidence: float = 0.0
    pdf_path: str = ""


class PDFMetadataExtractor:
    """Extract and resolve metadata from a PDF file."""

    def __init__(self, *, s2_api_key: str | None = None) -> None:
        self._s2_api_key = s2_api_key

    def extract(self, pdf_path: Path) -> MetadataResult:
        """Main entry point — try all strategies to identify the paper.

        Args:
            pdf_path: Path to the PDF file.

        Returns:
            MetadataResult with the identified paper (or None if failed).
        """
        if not pdf_path.exists():
            return MetadataResult(method="file_not_found")

        doc = pymupdf.open(str(pdf_path))
        first_pages_text = self._extract_first_pages(doc, max_pages=3)
        xmp_meta = self._extract_xmp_metadata(doc)
        doc.close()

        # Strategy 1: XMP metadata DOI
        if xmp_meta.get("doi"):
            result = self._lookup_doi(xmp_meta["doi"])
            if result:
                result.method = "xmp_doi"
                result.confidence = 0.95
                result.pdf_path = str(pdf_path)
                return result

        # Strategy 2: Regex DOI from first pages
        doi = self._find_doi(first_pages_text)
        if doi:
            result = self._lookup_doi(doi)
            if result:
                result.method = "text_doi"
                result.confidence = 0.90
                result.pdf_path = str(pdf_path)
                return result

        # Strategy 3: ArXiv ID from text
        arxiv_id = self._find_arxiv_id(first_pages_text)
        if arxiv_id:
            result = self._lookup_arxiv(arxiv_id)
            if result:
                result.method = "arxiv_id"
                result.confidence = 0.85
                result.pdf_path = str(pdf_path)
                return result

        # Strategy 4: XMP title → S2 search
        if xmp_meta.get("title"):
            result = self._search_title(xmp_meta["title"])
            if result:
                result.method = "xmp_title"
                result.confidence = 0.75
                result.pdf_path = str(pdf_path)
                return result

        # Strategy 5: Extract title heuristic → S2 search
        candidate_title = self._extract_candidate_title(first_pages_text)
        if candidate_title:
            result = self._search_title(candidate_title)
            if result:
                result.method = "heuristic_title"
                result.confidence = 0.60
                result.pdf_path = str(pdf_path)
                return result

        # Strategy 6: Build paper from whatever we have
        return self._build_from_text(first_pages_text, pdf_path)

    # ── Text extraction ───────────────────────────────────────

    @staticmethod
    def _extract_first_pages(doc: pymupdf.Document, max_pages: int = 3) -> str:
        """Extract text from the first N pages of a PDF."""
        pages = []
        for i in range(min(max_pages, len(doc))):
            pages.append(doc[i].get_text())
        return "\n\n".join(pages)

    @staticmethod
    def _extract_xmp_metadata(doc: pymupdf.Document) -> dict[str, str]:
        """Extract metadata from PDF XMP/standard metadata."""
        meta: dict[str, str] = {}
        # Standard PDF metadata
        pdf_meta = doc.metadata or {}
        if pdf_meta.get("title"):
            meta["title"] = pdf_meta["title"].strip()
        if pdf_meta.get("author"):
            meta["author"] = pdf_meta["author"].strip()

        # Check for DOI in subject/keywords
        for field_key in ("subject", "keywords"):
            val = pdf_meta.get(field_key, "")
            if val:
                match = _DOI_RE.search(val)
                if match:
                    meta["doi"] = match.group(1).rstrip(".")
                    break

        # XMP metadata — try newer PyMuPDF API first, fall back gracefully
        xmp = ""
        try:
            if hasattr(doc, "xref_xml") and doc.xref_length() > 0:
                xmp = doc.xref_xml(0)
            elif hasattr(doc, "get_xml_metadata"):
                xmp = doc.get_xml_metadata() or ""
        except Exception:
            pass
        if xmp:
            doi_match = re.search(r"doi[:\s]*(10\.\d{4,9}/[^\s\"'<]+)", xmp, re.I)
            if doi_match:
                meta.setdefault("doi", doi_match.group(1).rstrip("."))

        return meta

    # ── Identifier extraction ─────────────────────────────────

    @staticmethod
    def _find_doi(text: str) -> str | None:
        """Find a DOI in extracted text."""
        match = _DOI_RE.search(text)
        if match:
            doi = match.group(1).rstrip(".")
            # Validate DOI structure
            if "/" in doi and len(doi) > 8:
                return doi
        return None

    @staticmethod
    def _find_arxiv_id(text: str) -> str | None:
        """Find an ArXiv ID in extracted text."""
        match = _ARXIV_RE.search(text)
        return match.group(1) if match else None

    @staticmethod
    def _extract_candidate_title(text: str) -> str | None:
        """Heuristic: extract the title from first page text.

        Strategy: find the first block of capitalized lines before
        author names appear. Multi-line titles are joined.
        """
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

        # Skip header lines (journal name, institution, etc.)
        start = 0
        for i, line in enumerate(lines[:10]):
            lower = line.lower()
            if any(
                skip in lower
                for skip in (
                    "contents lists",
                    "homepage",
                    "available online",
                    "journal homepage",
                    "university",
                    "department",
                    "school of",
                    "faculty of",
                )
            ):
                start = i + 1

        # Collect consecutive title-like lines
        title_parts = []
        for line in lines[start : start + 8]:
            lower = line.lower()
            # Stop at author/institution lines
            if any(
                stop in lower
                for stop in (
                    "university",
                    "department",
                    "email",
                    "@",
                    "abstract",
                    "keywords",
                    "introduction",
                    "school of",
                    "faculty of",
                    "corresponding",
                    "et al",
                )
            ):
                break
            # Stop at lines that look like author lists (comma-separated short words)
            if "," in line and len(line) < 300:
                parts = [p.strip() for p in line.split(",")]
                short_parts = sum(1 for p in parts if 1 <= len(p.split()) <= 3)
                if short_parts >= len(parts) * 0.5 and len(parts) >= 2:
                    break
            # Title lines should have some uppercase and be reasonable length
            if 5 < len(line) < 300 and any(c.isupper() for c in line):
                title_parts.append(line)

        if not title_parts:
            return None

        return " ".join(title_parts)

    # ── API lookups ───────────────────────────────────────────

    def _s2_headers(self) -> dict[str, str]:
        headers = {}
        if self._s2_api_key:
            headers["x-api-key"] = self._s2_api_key
        return headers

    def _lookup_doi(self, doi: str) -> MetadataResult | None:
        """Query Semantic Scholar by DOI."""
        try:
            resp = httpx.get(
                f"{_S2_BASE}/paper/DOI:{doi}",
                params={"fields": _S2_FIELDS},
                headers=self._s2_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            paper = self._s2_to_paper(resp.json())
            return MetadataResult(paper=paper)
        except httpx.HTTPError:
            return None

    def _lookup_arxiv(self, arxiv_id: str) -> MetadataResult | None:
        """Query Semantic Scholar by ArXiv ID."""
        try:
            resp = httpx.get(
                f"{_S2_BASE}/paper/ArXiv:{arxiv_id}",
                params={"fields": _S2_FIELDS},
                headers=self._s2_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            paper = self._s2_to_paper(resp.json())
            return MetadataResult(paper=paper)
        except httpx.HTTPError:
            return None

    def _search_title(self, title: str) -> MetadataResult | None:
        """Search Semantic Scholar by title."""
        try:
            resp = httpx.get(
                f"{_S2_BASE}/paper/search",
                params={
                    "query": title[:200],
                    "limit": 1,
                    "fields": _S2_FIELDS,
                },
                headers=self._s2_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
            if data:
                paper = self._s2_to_paper(data[0])
                return MetadataResult(paper=paper)
        except httpx.HTTPError:
            pass
        return None

    @staticmethod
    def _s2_to_paper(raw: dict) -> Paper:
        """Convert Semantic Scholar API response to Paper model."""
        import uuid

        external = raw.get("externalIds", {}) or {}
        authors = []
        for a in raw.get("authors", []) or []:
            authors.append(
                Author(
                    name=a.get("name", "Unknown"),
                    orcid=a.get("orcid"),
                )
            )
        return Paper(
            id=raw.get("paperId", uuid.uuid4().hex[:12]),
            title=raw.get("title", "Untitled"),
            authors=authors,
            year=raw.get("year"),
            venue=raw.get("venue"),
            doi=external.get("DOI"),
            url=raw.get("url"),
            abstract=raw.get("abstract"),
            citation_count=raw.get("citationCount"),
        )

    def _build_from_text(self, text: str, pdf_path: Path) -> MetadataResult:
        """Last-resort: build a Paper from text heuristics."""
        title = self._extract_candidate_title(text) or pdf_path.stem
        authors = self._extract_authors_heuristic(text)
        abstract = self._extract_abstract(text)
        keywords = self._extract_keywords(text)

        return MetadataResult(
            paper=Paper(
                title=title,
                authors=authors,
                abstract=abstract,
                keywords=keywords,
            ),
            method="fallback_text",
            confidence=0.30,
            pdf_path=str(pdf_path),
        )

    @staticmethod
    def _extract_authors_heuristic(text: str) -> list[Author]:
        """Heuristic to extract author names from first page text."""
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        authors: list[Author] = []

        for line in lines[:15]:
            lower = line.lower()
            if any(
                skip in lower
                for skip in (
                    "abstract",
                    "introduction",
                    "university",
                    "department",
                    "email",
                    "@",
                    "http",
                    "doi",
                    "keywords",
                    "journal",
                    "available online",
                    "contents lists",
                    "homepage",
                    "corresponding",
                )
            ):
                continue
            # Look for lines with multiple proper-case names separated by commas
            if "," in line and len(line) < 300:
                parts = [p.strip() for p in line.split(",")]
                for part in parts:
                    # Clean superscript numbers: "Chao Wang1" → "Chao Wang"
                    # Also clean trailing asterisks (corresponding author marker)
                    cleaned = re.sub(r"[\d*]+$", "", part.strip()).strip()
                    cleaned = re.sub(r"[\d*]+\s*,", ",", cleaned)
                    if not cleaned:
                        continue
                    words = cleaned.split()
                    if 1 <= len(words) <= 4 and any(
                        w[0].isupper() for w in words if w
                    ):
                        authors.append(Author(name=cleaned))
                if authors:
                    break

        return authors[:10]

    @staticmethod
    def _extract_abstract(text: str) -> str | None:
        """Extract the abstract section from PDF text."""
        # Find "Abstract" keyword and extract text until next section
        m = re.search(
            r"(?:^|\n)\s*Abstract\s*[:\n]\s*(.+?)(?:\n\s*(?:Keywords|Introduction|1[\.\s]))",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if m:
            abstract = m.group(1).strip()
            # Clean up whitespace
            abstract = re.sub(r"\s+", " ", abstract)
            return abstract[:2000] if len(abstract) > 20 else None
        return None

    @staticmethod
    def _extract_keywords(text: str) -> list[str]:
        """Extract keywords from 'Keywords:' line in PDF text."""
        m = re.search(
            r"Keywords?\s*[:\-=]\s*(.+?)(?:\n|$)",
            text,
            re.IGNORECASE,
        )
        if not m:
            return []
        raw = m.group(1).strip()
        # Split by common delimiters
        keywords = re.split(r"[;,·•·]\s*", raw)
        return [kw.strip().lower() for kw in keywords if 2 < len(kw.strip()) < 50]
