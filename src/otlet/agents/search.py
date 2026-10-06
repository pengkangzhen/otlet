"""Search Agent — query Semantic Scholar API for academic papers."""

from __future__ import annotations

import uuid

import httpx

from agent_lit.agents.base import AgentBase
from agent_lit.models.author import Author
from agent_lit.models.paper import Paper

_S2_BASE = "https://api.semanticscholar.org/graph/v1"
_S2_FIELDS = (
    "paperId,title,year,venue,externalIds,url,abstract,"
    "citationCount,authors.name,authors.affiliations,fieldsOfStudy"
)
_S2_BATCH_LIMIT = 500  # max ids per /paper/batch request


class SearchAgent(AgentBase):
    """Agent responsible for searching and retrieving literature from
    Semantic Scholar."""

    name = "search"

    def __init__(self, *, api_key: str | None = None) -> None:
        self._api_key = api_key

    def run(self, query: str, *, limit: int = 10) -> list[Paper]:  # type: ignore[override]
        """Search for papers matching the query.

        Args:
            query: Search keywords or phrases.
            limit: Maximum number of results to return.

        Returns:
            List of Paper objects matching the query.
        """
        params = {
            "query": query,
            "limit": limit,
            "fields": _S2_FIELDS,
        }
        headers = {}
        if self._api_key:
            headers["x-api-key"] = self._api_key

        try:
            resp = httpx.get(
                f"{_S2_BASE}/paper/search",
                params=params,
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            return []

        data = resp.json().get("data", [])
        return [self._to_paper(item) for item in data if item]

    def fetch_by_doi(self, doi: str) -> Paper | None:
        """Fetch a single paper by DOI."""
        headers = {}
        if self._api_key:
            headers["x-api-key"] = self._api_key

        try:
            resp = httpx.get(
                f"{_S2_BASE}/paper/DOI:{doi}",
                params={"fields": _S2_FIELDS},
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            return None

        return self._to_paper(resp.json())

    def fetch_affiliations(self, dois: list[str]) -> dict[str, dict[str, str]]:
        """Batch-fetch current author affiliations from Semantic Scholar.

        Returns {doi_lower: {author_name: affiliation}}. Papers or authors
        without affiliation data on S2 are simply omitted; network errors
        skip the chunk rather than fail the whole call.
        """
        result: dict[str, dict[str, str]] = {}
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        for i in range(0, len(dois), _S2_BATCH_LIMIT):
            chunk = dois[i : i + _S2_BATCH_LIMIT]
            try:
                resp = httpx.post(
                    f"{_S2_BASE}/paper/batch",
                    params={"fields": "externalIds,authors.name,authors.affiliations"},
                    headers=headers,
                    json={"ids": [f"DOI:{d}" for d in chunk]},
                    timeout=30,
                )
                resp.raise_for_status()
            except httpx.HTTPError:
                continue
            for item in resp.json():
                if not item:
                    continue
                doi = ((item.get("externalIds") or {}).get("DOI") or "").lower()
                if not doi:
                    continue
                mapping: dict[str, str] = {}
                for a in item.get("authors") or []:
                    affs = a.get("affiliations") or []
                    name = a.get("name")
                    if name and affs and affs[0]:
                        mapping[name] = affs[0].strip()
                if mapping:
                    result[doi] = mapping
        return result

    @staticmethod
    def _to_paper(raw: dict) -> Paper:
        external = raw.get("externalIds", {}) or {}
        authors = []
        for a in raw.get("authors", []) or []:
            affs = a.get("affiliations") or []
            authors.append(
                Author(
                    name=a.get("name", "Unknown"),
                    orcid=a.get("orcid"),
                    affiliation=affs[0].strip() if affs and affs[0] else None,
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
