"""Tests for M3: claim verification (pure rules), the OpenAlex client
(MockTransport — no network), multi-route orchestration, and the
enrichment chain."""

from pathlib import Path

import httpx
import pytest

from otlet.agents.claims import (
    VERDICT_NONE,
    VERDICT_PARTIAL,
    VERDICT_SUPPORTS,
    assess_evidence,
    best_verdict,
    extract_terms,
    merge_candidates,
    norm_title,
    rank_for_claim,
    split_claims,
)
from otlet.agents.find_literature import run_find_literature
from otlet.agents.openalex import (
    OpenAlexClient,
    OpenAlexError,
    reconstruct_abstract,
    to_candidate,
)
from otlet.models.paper import Paper
from otlet.services.enrich import (
    build_patch,
    enrich_paper,
    title_compatible,
)
from otlet.storage.database import Database

# ── claims.py: splitting & terms ────────────────────────────


def test_split_claims_sentences_and_merge():
    text = (
        "Supply chain resilience improves recovery time! "
        "Supply chain resilience improves the recovery speed after "
        "disruption. Robust optimization handles uncertainty. short"
    )
    claims = split_claims(text)
    # the two near-identical sentences merged; "short" dropped
    assert len(claims) == 2
    assert any("recovery" in c for c in claims)
    assert any("Robust optimization" in c for c in claims)


def test_split_claims_chinese_punctuation():
    text = "冗余库存能提高供应链韧性。这是很短的话。物流网络设计影响恢复时间！"
    claims = split_claims(text)
    assert len(claims) == 2
    assert "冗余库存" in claims[0]


def test_extract_terms_bilingual():
    terms = extract_terms("two-stage robust 优化 of 供应链韧性")
    assert "robust" in terms and "of" not in terms  # <3 latin chars dropped
    # CJK singles + adjacent bigrams (供应链 is 3 chars — it matches via
    # the singles 供/应/链 and bigrams 供应/应链)
    assert "供" in terms and "供应" in terms and "应链" in terms
    assert "优化" in terms


def test_norm_title():
    assert norm_title("A Robust! Network-Design Model?") == "arobustnetworkdesignmodel"


# ── claims.py: three-tier evidence rules ────────────────────


def _cand(abstract=None, title="Some Paper", snippet=None):
    return {"title": title, "abstract": abstract, "snippet": snippet,
            "doi": "10.1/x", "citation_count": 5, "sources": ["openalex"]}


def test_supports_when_sentence_covers_claim():
    claim = "Redundant inventory improves supply chain resilience"
    abstract = (
        "We show through simulation that redundant inventory "
        "improves supply chain resilience after disruptions."
    )
    result = assess_evidence(claim, _cand(abstract=abstract))
    assert result["verdict"] == VERDICT_SUPPORTS
    assert "redundant inventory" in result["evidence_text"].lower()


def test_title_hit_only_is_partial():
    claim = "redundant inventory resilience"
    cand = _cand(
        title="Redundant inventory and supply chain resilience",
        abstract="Unrelated content about quantum computing methods.",
    )
    result = assess_evidence(claim, cand)
    assert result["verdict"] == VERDICT_PARTIAL
    assert result["source_field"] == "title"


def test_negation_polarity_mismatch_is_partial():
    claim = "Redundant inventory does not improve resilience"
    abstract = (
        "Redundant inventory improves resilience significantly "
        "according to our experiments."
    )
    result = assess_evidence(claim, _cand(abstract=abstract))
    assert result["verdict"] == VERDICT_PARTIAL
    assert result["polarity_mismatch"] is True


def test_direction_mismatch_is_partial():
    claim = "Backup suppliers increase total cost"
    abstract = (
        "We find that backup suppliers reduce total cost "
        "considerably in all scenarios."
    )
    result = assess_evidence(claim, _cand(abstract=abstract))
    assert result["verdict"] == VERDICT_PARTIAL


def test_none_with_cross_language_note():
    claim = "冗余库存提高供应链韧性"
    cand = _cand(abstract="We study machine learning for scheduling.")
    result = assess_evidence(claim, cand)
    assert result["verdict"] == VERDICT_NONE
    assert "English" in result["note"]


def test_best_verdict_across_variants():
    claim_cn = "冗余库存提高供应链韧性"
    claim_en = "Redundant inventory improves supply chain resilience"
    cand = _cand(abstract=(
        "Simulation shows redundant inventory improves supply chain "
        "resilience after major disruptions."
    ))
    best = best_verdict([
        assess_evidence(claim_cn, cand),
        assess_evidence(claim_en, cand),
    ])
    assert best["verdict"] == VERDICT_SUPPORTS


def test_merge_candidates_doi_beats_title_and_unions():
    a = _cand(title="Paper A", abstract=None)
    a.update(doi="10.1/x", id="W1", citation_count=10)
    b = _cand(title="paper a", abstract="Shared abstract text")
    b.update(doi="https://doi.org/10.1/x", id="W2", citation_count=30,
             sources=["s2"])
    merged = merge_candidates([[a], [b]])
    assert len(merged) == 1
    m = merged[0]
    assert m["citation_count"] == 30
    assert m["abstract"] == "Shared abstract text"
    assert set(m["sources"]) == {"openalex", "s2"}


def test_rank_prefers_coverage_and_citations():
    claim = "redundant inventory resilience"
    low = _cand(title="Redundant inventory resilience")
    low["citation_count"] = 1
    high = _cand(abstract="Redundant inventory improves resilience.")
    high["citation_count"] = 1000
    assert rank_for_claim(claim, high) > rank_for_claim(claim, low)


# ── openalex.py (MockTransport — no network) ────────────────


def _works_response(works: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"results": works, "meta": {}})


def _work(wid="W1", title="T", doi=None, abstract_words=None, **extra):
    work = {"id": f"https://openalex.org/{wid}", "title": title,
            "publication_year": 2024, "cited_by_count": 7, **extra}
    if doi:
        work["doi"] = f"https://doi.org/{doi}"
    if abstract_words:
        # build a real inverted index: word -> [positions]
        inv = {}
        for pos, word in enumerate(abstract_words):
            inv.setdefault(word, []).append(pos)
        work["abstract_inverted_index"] = inv
    return work


def test_reconstruct_abstract():
    text = reconstruct_abstract(
        {"supply": [0], "chain": [1], "resilience": [3], "improves": [2]}
    )
    assert text == "supply chain improves resilience"
    assert reconstruct_abstract(None) is None


def test_search_semantic_uses_standalone_param():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return _works_response([_work()])

    client = OpenAlexClient(
        mailto="me@example.org", transport=httpx.MockTransport(handler)
    )
    results = client.search_semantic("supply chain resilience")
    assert "search.semantic=supply+chain+resilience" in captured["url"]
    assert "mailto=me%40example.org" in captured["url"]
    assert results[0]["id"] == "W1"


def test_search_keyword_and_year_filter():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return _works_response([_work()])

    client = OpenAlexClient(transport=httpx.MockTransport(handler))
    client.search_keyword("robust design", year_from=2018, year_to=2024)
    assert "search=robust+design" in captured["url"]
    assert "publication_year%3A2018-2024" in captured["url"]


def test_query_timeout_error_surfaces():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "error": "Gateway timeout",
            "reason": "query_timeout",
        })

    client = OpenAlexClient(transport=httpx.MockTransport(handler))
    with pytest.raises(OpenAlexError, match="query_timeout"):
        client.search_semantic("broad query")


def test_retry_once_on_429_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return _works_response([_work()])

    client = OpenAlexClient(transport=httpx.MockTransport(handler))
    assert client.search_keyword("x")
    assert calls["n"] == 2


def test_to_candidate_maps_biblio_and_refs():
    cand = to_candidate(_work(
        doi="10.1/x",
        abstract_words=["we", "study", "resilience"],
        referenced_works=["https://openalex.org/W9"],
        primary_location={"source": {"display_name": "Operations Research"}},
        biblio={"volume": "68", "issue": "1", "first_page": "10",
                "last_page": "20"},
    ))
    assert cand["doi"] == "10.1/x"
    assert cand["abstract"] == "we study resilience"
    assert cand["venue"] == "Operations Research"
    assert cand["volume"] == "68" and cand["pages"] == "10-20"
    assert cand["refs"] == ["W9"]


# ── find_literature orchestration (fakes, no network) ───────


@pytest.fixture
def db(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "t.db")
    yield instance
    instance.close()


class FakeOpenAlex:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def search_semantic(self, q, **kw):
        self.calls.append(("semantic", q))
        return self.results

    def search_keyword(self, q, **kw):
        self.calls.append(("keyword", q))
        return self.results


class FakeS2:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def run(self, q, limit=8):
        self.calls.append(q)
        return self.results


def _oa_cand(title, abstract, doi=None, cited=10):
    return {"id": "W1", "doi": doi, "title": title, "year": 2023,
            "venue": "V", "citation_count": cited, "abstract": abstract,
            "snippet": None, "sources": ["openalex"]}


def test_run_find_literature_supports_via_remote(db):
    db.add_paper(Paper(id="p0", title="Unrelated", abstract="cats"))
    oa = FakeOpenAlex([_oa_cand(
        "Resilience Study",
        "Redundant inventory improves supply chain resilience "
        "after disruptions.",
        doi="10.9/r",
    )])
    result = run_find_literature(
        db,
        claims=["Redundant inventory improves supply chain resilience"],
        claims_en=["Redundant inventory improves supply chain resilience"],
        openalex=oa,
        search_agent=FakeS2([]),
    )
    assert result["summary"]["status"] == "supported"
    ev = result["claims"][0]["evidence"][0]
    assert ev["verdict"] == VERDICT_SUPPORTS
    assert ev["in_library"] is False


def test_run_find_literature_in_library_flag(db):
    db.add_paper(Paper(
        id="p0", title="Resilience Paper",
        doi="10.9/r",
        abstract="Redundant inventory improves supply chain resilience.",
    ))
    result = run_find_literature(
        db, claims=["Redundant inventory improves resilience"],
        openalex=None, search_agent=None,
    )
    ev = result["claims"][0]["evidence"][0]
    assert ev["in_library"] is True
    assert "local" in result["notes"][0]


def test_run_find_literature_local_only_note_and_budget(db):
    claims = [f"claim number {i} about resilience" for i in range(6)]
    oa = FakeOpenAlex([])
    result = run_find_literature(
        db, claims=claims, openalex=oa, search_agent=None,
    )
    notes = " ".join(result["notes"])
    assert "only the first 4" in notes
    # remote routes were hit for the first 4 claims only
    semantic_calls = [c for c in oa.calls if c[0] == "semantic"]
    assert len(semantic_calls) == 4


def test_run_find_literature_route_failure_recorded(db):
    class BrokenOpenAlex:
        def search_semantic(self, q, **kw):
            raise OpenAlexError("query_timeout")

        def search_keyword(self, q, **kw):
            raise OpenAlexError("HTTP 503")

    result = run_find_literature(
        db, claims=["some claim about logistics"],
        openalex=BrokenOpenAlex(), search_agent=None,
    )
    notes = " ".join(result["notes"])
    assert "query_timeout" in notes and "HTTP 503" in notes
    assert result["summary"]["status"] == "not_found"


def test_run_find_literature_not_supporting_visible(db):
    db.add_paper(Paper(
        id="p0", title="Resilience in Ecology",
        abstract="A review of resilience definitions in ecology and "
                 "ecosystem dynamics.",
    ))
    result = run_find_literature(
        db, claims=["Redundant inventory improves supply chain resilience"],
        openalex=None, search_agent=None,
    )
    claim_result = result["claims"][0]
    assert claim_result["verdict"] == VERDICT_NONE
    # recalled-but-not-supporting stays visible to the model
    assert claim_result["recalled_but_not_supporting"]
    assert "NOT disproved" in result["legend"][VERDICT_NONE]


# ── enrich chain (fake clients) ─────────────────────────────


def test_title_compatible():
    assert title_compatible(
        "A Robust Network Design Model", "a robust network design model!"
    )
    # plural suffix: prefix ratio 13/14 = 0.93 ≥ 0.9
    assert title_compatible("Resilient supply chains", "Resilient supply chain")
    # long subtitle variant stays rejected (strict 0.9 prefix rule)
    assert not title_compatible(
        "Resilient supply chain network design",
        "Resilient supply chain network design under uncertainty",
    )
    assert not title_compatible(
        "Resilient supply chains", "Facility location with disruptions"
    )


def test_build_patch_fills_only_empty():
    paper = Paper(id="p1", title="T", year=2020, abstract=None, venue=None)
    cand = _oa_cand("T", abstract="An abstract", cited=99)
    cand.update(year=2021, venue="OR", volume="7")
    patch = build_patch(paper, cand)
    assert patch == {"abstract": "An abstract", "venue": "OR", "volume": "7"}
    assert "year" not in patch  # local value wins


def test_enrich_paper_dry_run_and_write(db):
    paper = Paper(id="p1", title="Resilient Supply Chain Network Design")
    db.add_paper(paper)

    oa = FakeOpenAlex([_oa_cand(
        "Resilient Supply Chain Network Design",
        "We design resilient supply chains.", cited=120,
    )])
    # dry run computes the patch but does not write
    result = enrich_paper(db, db.get_paper("p1"), openalex=oa, dry_run=True)
    assert result["filled"]["abstract"].startswith("We design")
    assert db.get_paper("p1").abstract is None

    result = enrich_paper(db, db.get_paper("p1"), openalex=oa)
    updated = db.get_paper("p1")
    assert updated.abstract.startswith("We design")
    assert updated.citation_count == 120


def test_enrich_paper_title_verification_rejects(db):
    paper = Paper(id="p1", title="Resilient Supply Chain Design")
    db.add_paper(paper)
    oa = FakeOpenAlex([_oa_cand(
        "Completely Different Quantum Paper", "irrelevant", cited=1,
    )])
    result = enrich_paper(db, db.get_paper("p1"), openalex=oa)
    assert result["match"] is None
    assert db.get_paper("p1").abstract is None


def test_papers_needing_enrich_query(db):
    db.add_paper(Paper(id="full", title="A", abstract="x", year=2020,
                       venue="V"))
    db.add_paper(Paper(id="partial", title="B", abstract=None))
    db.add_paper(Paper(id="dead", title="C"))
    db.delete_paper("dead")
    assert db.papers_needing_enrich() == ["partial"]
