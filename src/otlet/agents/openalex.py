"""OpenAlex client for literature recall and metadata enrichment.

Throttle-first discipline (borrowed from LitBoard): a per-instance
minimum interval between requests prevents tripping rate limits,
rather than backing off after the fact. 429/5xx retry once respecting
Retry-After. Semantic search uses the standalone ``search.semantic``
query parameter (verified 2026-10: the old ``filter=semantic.search``
form 500s server-side) and may time out on broad queries — callers
catch OpenAlexError and fall back to keyword search.

Fact boundary: ``search`` ranks over title/abstract/fulltext — it is
OpenAlex's own relevance ranking, not a text→vector semantic search.
"""

from __future__ import annotations

import threading
import time

import httpx

from otlet.storage.database import normalize_doi

_BASE = "https://api.openalex.org/works"
_SELECT = ",".join([
    "id", "doi", "title", "publication_year", "cited_by_count",
    "abstract_inverted_index", "primary_location", "language", "biblio",
    "referenced_works",
])
_MIN_INTERVAL = 0.25  # seconds between requests (polite)


class OpenAlexError(Exception):
    """Request failed after one retry; message carries the reason."""


def reconstruct_abstract(inverted: dict | None) -> str | None:
    """OpenAlex stores abstracts as word → [positions] inverted
    indexes; rebuild the sentence in original word order."""
    if not inverted:
        return None
    positions: list[tuple[int, str]] = []
    for word, idxs in inverted.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(w for _, w in positions) or None


def to_candidate(work: dict) -> dict:
    """Normalize an OpenAlex work JSON into the shared candidate shape
    {id, doi, title, year, venue, citation_count, abstract, sources}."""
    location = work.get("primary_location") or {}
    source = location.get("source") or {}
    biblio = work.get("biblio") or {}
    pages = None
    if biblio.get("first_page"):
        pages = (
            f"{biblio['first_page']}-{biblio['last_page']}"
            if biblio.get("last_page")
            else str(biblio["first_page"])
        )
    return {
        "id": (work.get("id") or "").rsplit("/", 1)[-1] or None,
        "doi": normalize_doi(work.get("doi")),
        "title": work.get("title"),
        "year": work.get("publication_year"),
        "venue": source.get("display_name"),
        "volume": biblio.get("volume") or None,
        "issue": biblio.get("issue") or None,
        "pages": pages,
        "publisher": source.get("host_organization_name"),
        "language": work.get("language"),
        "citation_count": work.get("cited_by_count"),
        "abstract": reconstruct_abstract(
            work.get("abstract_inverted_index")
        ),
        "refs": [
            r.rsplit("/", 1)[-1]
            for r in work.get("referenced_works") or []
        ],
        "sources": ["openalex"],
    }


class OpenAlexClient:
    def __init__(
        self,
        *,
        mailto: str | None = None,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # transport is the test seam (httpx.MockTransport)
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._mailto = mailto
        self._lock = threading.Lock()
        self._last_request = 0.0

    def search_semantic(
        self, query: str, *, limit: int = 8,
        year_from: int | None = None, year_to: int | None = None,
    ) -> list[dict]:
        params = self._common(limit, year_from, year_to)
        params["search.semantic"] = query
        return self._search(params)

    def search_keyword(
        self, query: str, *, limit: int = 8,
        year_from: int | None = None, year_to: int | None = None,
    ) -> list[dict]:
        params = self._common(limit, year_from, year_to)
        params["search"] = query
        return self._search(params)

    def fetch_by_doi(self, doi: str) -> dict | None:
        params = self._common(1)
        params["filter"] = f"doi:{normalize_doi(doi)}"
        works = self._search(params)
        return works[0] if works else None

    # ── internals ───────────────────────────────────────────

    def _common(
        self, limit: int,
        year_from: int | None = None, year_to: int | None = None,
    ) -> dict:
        params: dict = {
            "per-page": max(1, min(50, limit)),
            "select": _SELECT,
        }
        if self._mailto:
            params["mailto"] = self._mailto
        if year_from or year_to:
            lo = year_from or 1000
            hi = year_to or 9999
            params["filter"] = f"publication_year:{lo}-{hi}"
        return params

    def _search(self, params: dict) -> list[dict]:
        data = self._get(_BASE, params)
        return [
            to_candidate(w)
            for w in data.get("results") or []
            if (w.get("title") or w.get("doi"))
        ]

    def _get(self, url: str, params: dict) -> dict:
        delay = None
        for attempt in (1, 2):
            self._throttle()
            try:
                resp = self._client.get(url, params=params)
            except httpx.HTTPError as e:
                if attempt == 2:
                    raise OpenAlexError(f"network: {e}") from e
                time.sleep(1.0)
                continue
            if resp.status_code == 200:
                body = resp.json()
                if "error" in body:  # e.g. query_timeout on semantic
                    raise OpenAlexError(
                        f"{body.get('reason', body['error'])}"
                    )
                return body
            if resp.status_code == 429 and attempt == 1:
                retry_after = resp.headers.get("Retry-After")
                delay = (
                    min(float(retry_after), 60.0)
                    if retry_after and retry_after.isdigit()
                    else 2.0
                )
                time.sleep(delay)
                continue
            if 500 <= resp.status_code < 600 and attempt == 1:
                time.sleep(1.0)
                continue
            raise OpenAlexError(f"HTTP {resp.status_code}")
        raise OpenAlexError("unreachable")

    def _throttle(self) -> None:
        with self._lock:
            wait = self._last_request + _MIN_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
