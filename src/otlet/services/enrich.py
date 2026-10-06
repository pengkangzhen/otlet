"""Metadata enrichment: OpenAlex → Semantic Scholar fallback chain.

Discipline (LitBoard enrich.js):
- fill-only-empty — a field is written only when the local value is
  missing; user edits are never overwritten (citation_count is the
  exception, it tracks the source of record);
- a DOI match is trusted outright; a title-search match must pass a
  title-compatibility check (normalized equality, or one is a ≥0.9
  prefix of the other) to prevent enriching the wrong paper;
- network failure on one provider degrades to the next instead of
  failing the batch.
"""

from __future__ import annotations

from otlet.agents.claims import norm_title
from otlet.agents.openalex import OpenAlexClient, OpenAlexError
from otlet.storage.database import Database

# Fields the chain can backfill (citation_count handled separately)
FILL_FIELDS = (
    "year", "venue", "volume", "issue", "pages",
    "publisher", "language", "abstract",
)


def title_compatible(local: str, remote: str) -> bool:
    """Same title up to case/punctuation, or one prefixes the other
    with ≥0.9 length ratio (guards against subtitle variants)."""
    a, b = norm_title(local), norm_title(remote)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer.startswith(shorter) and len(shorter) / len(longer) >= 0.9


def find_candidate(paper, *, openalex: OpenAlexClient, search_agent=None):
    """Locate the best external record. Returns (candidate, match_key)
    where match_key is 'doi:<doi>' or 'title:<title>' — or (None, None)
    when nothing matched or the network failed."""
    if paper.doi and openalex is not None:
        try:
            cand = openalex.fetch_by_doi(paper.doi)
            if cand and cand.get("title"):
                return cand, f"doi:{paper.doi}"
        except OpenAlexError:
            pass
    if openalex is not None:
        try:
            for cand in openalex.search_keyword(
                paper.title, limit=3
            ):
                if cand.get("title") and title_compatible(
                    paper.title, cand["title"]
                ):
                    return cand, "title"
        except OpenAlexError:
            pass
    if search_agent is not None:
        try:
            found = (
                search_agent.fetch_by_doi(paper.doi)
                if paper.doi
                else None
            )
            if found is None:
                results = search_agent.run(paper.title, limit=3)
                found = next(
                    (p for p in results
                     if title_compatible(paper.title, p.title)),
                    None,
                )
            if found is not None:
                from otlet.agents.find_literature import _paper_to_candidate
                cand = _paper_to_candidate(found, source="s2")
                cand["citation_count"] = found.citation_count
                return cand, "doi" if paper.doi else "title"
        except Exception:
            pass
    return None, None


def build_patch(paper, candidate: dict) -> dict:
    """Fill-only-empty patch: {field: value} for empty local fields."""
    patch: dict = {}
    for field in FILL_FIELDS:
        value = candidate.get(field)
        if value in (None, "", []) and getattr(paper, field, None) in (
            None, "", []
        ):
            continue
        if getattr(paper, field, None) in (None, "") and value not in (
            None, ""
        ):
            patch[field] = value
    return patch


def enrich_paper(
    db: Database,
    paper,
    *,
    openalex: OpenAlexClient,
    search_agent=None,
    dry_run: bool = False,
) -> dict:
    """Enrich one paper. Returns {paper_id, title, match, filled,
    citation_count, error}. `filled` maps field → incoming value."""
    candidate, match_key = find_candidate(
        paper, openalex=openalex, search_agent=search_agent
    )
    if candidate is None:
        return {
            "paper_id": paper.id, "title": paper.title,
            "match": None, "filled": {}, "citation_count": None,
        }

    patch = build_patch(paper, candidate)
    cited = candidate.get("citation_count")
    if cited is not None and cited != paper.citation_count:
        patch["citation_count"] = cited

    if patch and not dry_run:
        db.update_paper(paper.id, **patch)
    return {
        "paper_id": paper.id,
        "title": paper.title,
        "match": match_key,
        "filled": patch,
        "citation_count": cited,
    }
