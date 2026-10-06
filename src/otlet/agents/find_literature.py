"""find_literature orchestration: recall candidates for each claim
from several routes, then judge them with the pure rules in
agents/claims.py.

Routes (each fails independently and reports its reason):
- local: library metadata (title/abstract) via term search, plus
  per-page PDF full-text snippets when an index is provided
- openalex semantic: full claim sentence via search.semantic
- openalex keyword: extracted terms joined
- semantic scholar: the existing SearchAgent

Honesty contract (LitBoard): remote routes only cover the first
REMOTE_CLAIM_BUDGET claims — the note says so instead of pretending;
candidates recalled but not supporting a claim stay visible so the
model sees "recalled but not supporting" vs "nothing recalled".
"""

from __future__ import annotations

from otlet.agents.claims import (
    VERDICT_NONE,
    VERDICT_ORDER,
    VERDICT_PARTIAL,
    VERDICT_SUPPORTS,
    assess_evidence,
    best_verdict,
    extract_terms,
    merge_candidates,
    rank_for_claim,
    split_claims,
)
from otlet.agents.openalex import OpenAlexClient, OpenAlexError
from otlet.storage.database import Database, normalize_doi

REMOTE_CLAIM_BUDGET = 4   # remote routes cover at most this many claims
LOCAL_LIMIT = 5           # candidates kept per local sub-route
REMOTE_LIMIT = 8          # candidates kept per remote route


def run_find_literature(
    db: Database,
    claims: list[str] | None = None,
    claims_en: list[str] | None = None,
    claim_text: str | None = None,
    *,
    year_from: int | None = None,
    year_to: int | None = None,
    openalex: OpenAlexClient | None = None,
    search_agent=None,
    pdf_index=None,
) -> dict:
    """Verify claims against the library and external sources.

    Returns a JSON-ready dict: {claims: [{claim, verdict, evidence,
    candidates_not_supporting}], summary, notes}.
    """
    claims = [c.strip() for c in (claims or []) if c and c.strip()]
    if not claims and claim_text:
        claims = split_claims(claim_text)
    claims = claims[:12]
    claims_en = [c.strip() for c in (claims_en or []) if c and c.strip()]

    library_dois = {
        normalize_doi(d) for _, d in db.get_papers_with_dois()
    }
    notes: list[str] = []

    results = []
    for i, claim in enumerate(claims):
        variants = [claim]
        if i < len(claims_en):
            variants.append(claims_en[i])

        route_results: list[list[dict]] = []
        route_notes: list[str] = []

        local = _recall_local(db, variants, pdf_index)
        route_results.append(local)

        remote_ok = i < REMOTE_CLAIM_BUDGET
        if remote_ok and openalex is not None:
            for variant in variants[:2]:
                try:
                    route_results.append(openalex.search_semantic(
                        variant[:300], limit=REMOTE_LIMIT,
                        year_from=year_from, year_to=year_to,
                    ))
                    break
                except OpenAlexError as e:
                    route_notes.append(f"openalex semantic: {e}")
            keyword_query = " ".join(extract_terms(claim)[:6])
            try:
                route_results.append(openalex.search_keyword(
                    keyword_query, limit=REMOTE_LIMIT,
                    year_from=year_from, year_to=year_to,
                ))
            except OpenAlexError as e:
                route_notes.append(f"openalex keyword: {e}")
        if remote_ok and search_agent is not None:
            try:
                papers = search_agent.run(
                    variants[0][:200], limit=REMOTE_LIMIT
                )
                route_results.append([
                    _paper_to_candidate(p, source="s2") for p in papers
                ])
            except Exception as e:  # network failures are answerable
                route_notes.append(f"semantic scholar: {e}")

        candidates = merge_candidates(route_results)
        for cand in candidates:
            cand["in_library"] = bool(
                cand.get("doi") and normalize_doi(cand["doi"]) in library_dois
            )

        # judge each candidate against every variant, keep the best
        judged = []
        for cand in candidates:
            assessments = [
                assess_evidence(v, cand) for v in variants
            ]
            best = best_verdict(assessments)
            entry = dict(cand)
            entry["verdict"] = best["verdict"]
            entry["evidence_text"] = best["evidence_text"]
            entry["source_field"] = best["source_field"]
            entry["polarity_mismatch"] = best["polarity_mismatch"]
            entry["note"] = best["note"]
            entry["score"] = rank_for_claim(claim, cand)
            judged.append(entry)

        judged.sort(
            key=lambda c: (VERDICT_ORDER[c["verdict"]], c["score"]),
            reverse=True,
        )
        supporting = [
            c for c in judged
            if c["verdict"] in (VERDICT_SUPPORTS, VERDICT_PARTIAL)
        ]
        rest = [c for c in judged if c["verdict"] == VERDICT_NONE]

        verdict = VERDICT_NONE
        if any(c["verdict"] == VERDICT_SUPPORTS for c in supporting):
            verdict = VERDICT_SUPPORTS
        elif supporting:
            verdict = VERDICT_PARTIAL

        results.append({
            "claim": claim,
            "verdict": verdict,
            "evidence": [
                {
                    "title": c.get("title"),
                    "doi": c.get("doi"),
                    "year": c.get("year"),
                    "venue": c.get("venue"),
                    "citation_count": c.get("citation_count"),
                    "in_library": c.get("in_library", False),
                    "verdict": c["verdict"],
                    "evidence_text": c["evidence_text"],
                    "source_field": c["source_field"],
                    "polarity_mismatch": c["polarity_mismatch"],
                    "note": c["note"],
                }
                for c in supporting[:5]
            ],
            "recalled_but_not_supporting": [
                {"title": c.get("title"), "doi": c.get("doi")}
                for c in rest[:3]
            ],
        })
        notes.extend(f"claim {i + 1}: {n}" for n in route_notes)

    if len(claims) > REMOTE_CLAIM_BUDGET:
        notes.append(
            f"remote recall covered only the first {REMOTE_CLAIM_BUDGET} "
            f"of {len(claims)} claims; the rest were verified locally only"
        )
    if not openalex and not search_agent:
        notes.append("no external sources configured — local library only")

    supported = sum(1 for r in results if r["verdict"] == VERDICT_SUPPORTS)
    partial = sum(1 for r in results if r["verdict"] == VERDICT_PARTIAL)
    none = sum(1 for r in results if r["verdict"] == VERDICT_NONE)
    if supported:
        status = "supported"
    elif partial:
        status = "partially_supported"
    else:
        status = "not_found"

    return {
        "claims": results,
        "summary": {
            "status": status,
            "supported": supported,
            "partial": partial,
            "not_found": none,
        },
        "notes": notes,
        "legend": {
            VERDICT_SUPPORTS: "evidence sentence found and polarity aligns",
            VERDICT_PARTIAL: "related but title-only, polarity-mismatched, "
                             "or direction-unclear",
            VERDICT_NONE: "no supporting evidence found — this is "
                          "'not determined', NOT disproved",
        },
    }


# ── route helpers ──────────────────────────────────────────


def _recall_local(db: Database, variants: list[str], pdf_index) -> list[dict]:
    """Library metadata via claim terms + optional PDF full-text
    snippets. Returns candidates with abstracts from the library."""
    seen_ids: set[str] = set()
    candidates: list[dict] = []
    for variant in variants:
        terms = sorted(
            extract_terms(variant), key=len, reverse=True
        )[:4]
        for term in terms:
            for paper in db.search_papers(term):
                if paper.id in seen_ids:
                    continue
                seen_ids.add(paper.id)
                candidates.append(_paper_to_candidate(paper, source="local"))
                if len(candidates) >= LOCAL_LIMIT:
                    return candidates
    if pdf_index is not None:
        for variant in variants[:1]:
            for hit in pdf_index.search(variant[:60], limit=3):
                pid = hit["paper_id"]
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
                paper = db.get_paper(pid)
                if paper is None:
                    continue
                cand = _paper_to_candidate(paper, source="local")
                cand["snippet"] = hit["snippet"]
                candidates.append(cand)
    return candidates


def _paper_to_candidate(paper, *, source: str = "local") -> dict:
    return {
        "id": paper.id,
        "doi": normalize_doi(paper.doi),
        "title": paper.title,
        "year": paper.year,
        "venue": paper.venue,
        "citation_count": paper.citation_count,
        "abstract": paper.abstract,
        "snippet": None,
        "sources": [source],
    }
