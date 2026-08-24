"""PDF file storage — download and extract text from PDFs."""

from pathlib import Path

import httpx
import pymupdf


class PDFStore:
    """Manages PDF files on disk: download, store, extract text."""

    def __init__(self, pdf_dir: Path) -> None:
        self._dir = pdf_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def download(self, url: str, *, paper_id: str) -> Path:
        """Download a PDF from URL and store it."""
        dest = self._dir / f"{paper_id}.pdf"
        if dest.exists():
            return dest

        resp = httpx.get(url, follow_redirects=True, timeout=60)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        return dest

    def import_file(self, source: Path, *, paper_id: str) -> Path:
        """Copy an existing PDF into the store."""
        dest = self._dir / f"{paper_id}.pdf"
        if dest.exists():
            return dest
        dest.write_bytes(source.read_bytes())
        return dest

    def get_path(self, paper_id: str) -> Path | None:
        """Return the PDF path if it exists."""
        p = self._dir / f"{paper_id}.pdf"
        return p if p.exists() else None

    def extract_text(self, paper_id: str) -> str | None:
        """Extract full text from a stored PDF."""
        path = self.get_path(paper_id)
        if not path:
            return None

        doc = pymupdf.open(str(path))
        pages = [page.get_text() for page in doc]
        doc.close()
        return "\n\n".join(pages)

    def exists(self, paper_id: str) -> bool:
        return (self._dir / f"{paper_id}.pdf").exists()

    def remove(self, paper_id: str) -> None:
        """Delete the stored PDF for a paper (permanent removal)."""
        p = self._dir / f"{paper_id}.pdf"
        p.unlink(missing_ok=True)
