"""Per-page PDF full-text index built on pdf_text + FTS5 trigram.

Two cooperating stores (see storage/database.py):
- ``pdf_text`` keeps the authoritative zlib-compressed per-page texts
  (page reads for the chat agent, and the <3-char fallback scan);
- ``pdf_fts`` is the FTS5 trigram mirror searched for >=3-char queries.
  The trigram tokenizer gives substring semantics and works for CJK
  and Latin text alike — the reason it was chosen over the default
  unicode61 tokenizer, which treats a whole Chinese sentence as one
  token.
"""

from __future__ import annotations

import json
import sqlite3
import zlib

import pymupdf

from otlet.storage.database import Database
from otlet.storage.pdf_store import PDFStore

# Below this length a trigram index has no tokens to match — fall back
# to decompressing and scanning pdf_text directly
_FTS_MIN_QUERY = 3


class PDFIndex:
    """Builds and queries the per-page full-text index."""

    def __init__(self, db: Database, pdf_store: PDFStore) -> None:
        self._db = db
        self._store = pdf_store

    # ── Building ────────────────────────────────────────────

    def build(self, paper_id: str) -> int | None:
        """(Re)index a paper's stored PDF. Returns the page count,
        or None when there is no stored PDF to index."""
        path = self._store.get_path(paper_id)
        if not path:
            return None

        doc = pymupdf.open(str(path))
        try:
            pages = [page.get_text() for page in doc]
        finally:
            doc.close()

        blob = zlib.compress(
            json.dumps(pages, ensure_ascii=False).encode("utf-8"), 1
        )
        self._db.upsert_pdf_text(paper_id, len(pages), blob)
        self._db.replace_fts_rows(paper_id, pages)
        return len(pages)

    # ── Querying ────────────────────────────────────────────

    def search(self, query: str, *, limit: int = 50) -> list[dict]:
        """Find pages containing the query.

        Returns [{paper_id, page, snippet}]. >=3 characters go through
        the FTS trigram index; shorter queries (e.g. two-character
        Chinese words) decompress and scan instead.
        """
        q = query.strip()
        if not q:
            return []
        if len(q) >= _FTS_MIN_QUERY:
            phrase = '"' + q.replace('"', " ") + '"'
            try:
                return self._db.fts_search(phrase, limit=limit)
            except sqlite3.OperationalError:
                pass  # unparsable phrase — fall back to the scan
        return self._scan(q, limit=limit)

    def get_pages(
        self,
        paper_id: str,
        *,
        from_page: int = 1,
        to_page: int | None = None,
    ) -> list[dict] | None:
        """Page texts for the chat agent's read-by-page tool.

        Returns [{page, text, char_total}] for pages in [from_page,
        to_page] (1-based, to_page=None means "to the end"), or None
        when the paper has no index.
        """
        record = self._db.get_pdf_text(paper_id)
        if record is None:
            return None
        page_count, blob = record
        pages: list[str] = json.loads(zlib.decompress(blob))
        last = min(to_page or page_count, page_count)
        first = max(1, from_page)
        if first > last:
            return []
        return [
            {"page": i, "text": pages[i - 1], "char_total": len(pages[i - 1])}
            for i in range(first, last + 1)
        ]

    # ── Internal ────────────────────────────────────────────

    def _scan(self, query: str, *, limit: int) -> list[dict]:
        needle = query.lower()
        hits: list[dict] = []
        for paper_id, blob in self._db.list_pdf_texts():
            pages: list[str] = json.loads(zlib.decompress(blob))
            for i, text in enumerate(pages, start=1):
                pos = text.lower().find(needle)
                if pos >= 0:
                    hits.append(
                        {
                            "paper_id": paper_id,
                            "page": i,
                            "snippet": self._clip(text, pos, needle),
                        }
                    )
                    if len(hits) >= limit:
                        return hits
        return hits

    @staticmethod
    def _clip(text: str, pos: int, needle: str, radius: int = 40) -> str:
        start = max(0, pos - radius)
        end = min(len(text), pos + len(needle) + radius)
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(text) else ""
        fragment = " ".join(text[start:end].split())
        return f"{prefix}{fragment}{suffix}"
