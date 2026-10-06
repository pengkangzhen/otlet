"""Claim verification core — pure functions, no I/O.

Philosophy (borrowed from LitBoard, docs/litboard-borrowing-plan.md
P0-3): "topically related ≠ supporting the claim". The three-tier
verdict (supports / partial / none) is computed entirely by testable
code rules — negation polarity, direction words, title-hit demotion —
so the model never decides verdicts; it only splits claims and
narrates the structured results.

Bilingual: claims may be Chinese or English; term extraction emits
Latin words (>=3 chars) plus CJK single characters and adjacent
bigrams. Lexical matching means a Chinese claim cannot match an
English abstract — callers should pass an English variant too and
take the best verdict.
"""

from __future__ import annotations

import math
import re

from otlet.storage.database import normalize_doi

# thresholds (LitBoard litsearch.js values)
MIN_CLAIM_CHARS = 8       # shorter fragments are dropped when splitting
SENTENCE_MIN_CHARS = 12   # evidence sentences shorter than this are noise
COVERAGE_THRESHOLD = 0.34  # fraction of claim terms that must appear
WORD_OVERLAP_MERGE = 0.70  # sentences this similar merge into one claim
MAX_CLAIMS = 12

VERDICT_SUPPORTS = "supports"
VERDICT_PARTIAL = "partial"
VERDICT_NONE = "none"
VERDICT_ORDER = {VERDICT_SUPPORTS: 2, VERDICT_PARTIAL: 1, VERDICT_NONE: 0}

_NEGATION_WORDS = {
    "not", "no", "cannot", "without", "unable", "fails", "fails",
    "never", "nor", "无法", "不能", "没有", "不会", "未能", "难以", "无",
}
# Opposite-direction word groups; a claim with a direction requires an
# evidence sentence pointing the same way
_WORDS_UP = {
    "increase", "increases", "improve", "improves", "enhance", "enhances",
    "raise", "raises", "boost", "promote", "higher", "strengthen",
    "提高", "提升", "增强", "改善", "促进", "升高",
}
_WORDS_DOWN = {
    "decrease", "decreases", "reduce", "reduces", "lower", "lowers",
    "degrade", "weaken", "suppress", "worsen", "fewer",
    "下降", "降低", "减弱", "减少", "抑制", "恶化",
}

_SENTENCE_SPLIT = re.compile(r"[.!?;。！？；\n]+")
_LATIN_TERM = re.compile(r"[a-z]{3,}")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_KEEP_CHARS = re.compile(r"[^a-z0-9\u4e00-\u9fff]")


# ── claim splitting ────────────────────────────────────────


def split_claims(text: str) -> list[str]:
    """Split a free-form statement into claims.

    Splits on sentence punctuation, drops fragments below
    MIN_CLAIM_CHARS, and merges sentences whose term overlap reaches
    WORD_OVERLAP_MERGE (keeping the longer one).
    """
    sentences = [
        s.strip()
        for s in _SENTENCE_SPLIT.split(text or "")
        if len(s.strip()) >= MIN_CLAIM_CHARS
    ]
    merged: list[str] = []
    for sent in sentences:
        terms = set(extract_terms(sent))
        absorbed = False
        for i, kept in enumerate(merged):
            other = set(extract_terms(kept))
            if not terms or not other:
                continue
            # overlap coefficient over the smaller term set
            overlap = len(terms & other) / min(len(terms), len(other))
            if overlap >= WORD_OVERLAP_MERGE:
                merged[i] = kept if len(kept) >= len(sent) else sent
                absorbed = True
                break
        if not absorbed:
            merged.append(sent)
    return merged[:MAX_CLAIMS]


# ── terms & normalization ──────────────────────────────────


def extract_terms(text: str) -> list[str]:
    """Latin words (>=3 chars) + CJK single chars and adjacent bigrams."""
    text = text.lower()
    terms: list[str] = _LATIN_TERM.findall(text)
    for run in _CJK_RUN.findall(text):
        terms.extend(run)
        terms.extend(run[i : i + 2] for i in range(len(run) - 1))
    # de-duplicate, keep order
    seen: set[str] = set()
    unique = [t for t in terms if not (t in seen or seen.add(t))]
    return unique


def norm_title(title: str) -> str:
    """Lowercase and keep only [a-z0-9] + CJK — the merge key for
    titles that differ only in punctuation/case/spacing."""
    return _KEEP_CHARS.sub("", (title or "").lower())


def split_sentences(text: str) -> list[str]:
    return [
        s.strip()
        for s in _SENTENCE_SPLIT.split(text or "")
        if len(s.strip()) >= SENTENCE_MIN_CHARS
    ]


def _has_negation(text: str) -> bool:
    terms = set(extract_terms(text))
    return any(w in terms for w in _NEGATION_WORDS)


def _direction(text: str) -> int:
    terms = set(extract_terms(text))
    up = any(w in terms for w in _WORDS_UP)
    down = any(w in terms for w in _WORDS_DOWN)
    if up and down:
        return 0  # ambiguous
    if up:
        return 1
    if down:
        return -1
    return 0


def _term_coverage(claim_terms: list[str], sentence: str) -> float:
    if not claim_terms:
        return 0.0
    low = sentence.lower()
    hit = sum(1 for t in claim_terms if t in low)
    return hit / len(claim_terms)


# ── evidence assessment ────────────────────────────────────


def assess_evidence(claim: str, candidate: dict) -> dict:
    """Judge one candidate against one claim. Pure code rules:

    - an abstract/snippet sentence covering >= 34% of claim terms is
      an evidence sentence; its verdict is `supports` unless
      * negation polarity differs (claim negated, sentence not, or
        vice versa) → partial
      * the claim is directional and the sentence doesn't point the
        same way → partial
    - a title-only hit is always `partial` — a title can never carry
      evidence
    - nothing hit → `none` (with a cross-language note when the claim
      is CJK and the candidate text is not)
    """
    claim_terms = extract_terms(claim)
    if not claim_terms:
        return {"verdict": VERDICT_NONE, "coverage": 0.0,
                "evidence_text": None, "source_field": None,
                "polarity_mismatch": False, "note": "claim has no terms"}

    best: dict | None = None
    for field in ("abstract", "snippet"):
        text = candidate.get(field) or ""
        if not text:
            continue
        for sent in split_sentences(text):
            coverage = _term_coverage(claim_terms, sent)
            if coverage < COVERAGE_THRESHOLD:
                continue
            verdict = VERDICT_SUPPORTS
            polarity_mismatch = _has_negation(claim) != _has_negation(sent)
            if polarity_mismatch:
                verdict = VERDICT_PARTIAL
            elif _direction(claim) not in (0, _direction(sent)):
                verdict = VERDICT_PARTIAL
            entry = {
                "verdict": verdict,
                "coverage": coverage,
                "evidence_text": sent,
                "source_field": field,
                "polarity_mismatch": polarity_mismatch,
                "note": "",
            }
            if best is None or (
                VERDICT_ORDER[verdict], coverage
            ) > (VERDICT_ORDER[best["verdict"]], best["coverage"]):
                best = entry

    if best is not None:
        return best

    # title hit: related, but a title cannot carry evidence
    title = candidate.get("title") or ""
    if title and _term_coverage(claim_terms, title) >= COVERAGE_THRESHOLD:
        return {
            "verdict": VERDICT_PARTIAL,
            "coverage": _term_coverage(claim_terms, title),
            "evidence_text": title,
            "source_field": "title",
            "polarity_mismatch": False,
            "note": "title match only — the title itself cannot "
                    "support a claim",
        }

    note = ""
    claim_cjk = bool(_CJK_RUN.search(claim))
    text_cjk = any(
        _CJK_RUN.search(candidate.get(f) or "")
        for f in ("abstract", "snippet", "title")
    )
    if claim_cjk and not text_cjk:
        note = ("all evidence text is non-Chinese; retry with an "
                "English variant of the claim")
    return {
        "verdict": VERDICT_NONE,
        "coverage": 0.0,
        "evidence_text": None,
        "source_field": None,
        "polarity_mismatch": False,
        "note": note or "no evidence sentence found",
    }


def best_verdict(assessments: list[dict]) -> dict:
    """Best assessment across claim variants (e.g. Chinese + English)."""
    return max(
        assessments, key=lambda a: (VERDICT_ORDER[a["verdict"]], a["coverage"])
    )


# ── candidate merging & ranking ────────────────────────────


def merge_candidates(lists: list[list[dict]]) -> list[dict]:
    """Deduplicate across routes/variants by DOI → id → normalized
    title, merging fields (abstract/snippet win over missing,
    citation_count takes the max, sources unite)."""
    by_key: dict[str, dict] = {}
    for lst in lists:
        for cand in lst:
            doi = normalize_doi(cand.get("doi"))
            key = (
                f"doi:{doi}" if doi
                else f"id:{cand['id']}" if cand.get("id")
                else f"title:{norm_title(cand.get('title') or '')}"
            )
            if key not in by_key:
                by_key[key] = dict(cand)
                by_key[key]["sources"] = list(cand.get("sources") or [])
                continue
            kept = by_key[key]
            for field in ("title", "abstract", "snippet", "year",
                          "venue", "doi"):
                if not kept.get(field) and cand.get(field):
                    kept[field] = cand[field]
            if (cand.get("citation_count") or 0) > (
                kept.get("citation_count") or 0
            ):
                kept["citation_count"] = cand["citation_count"]
            for src in cand.get("sources") or []:
                if src not in kept["sources"]:
                    kept["sources"].append(src)
    return list(by_key.values())


def rank_for_claim(claim: str, candidate: dict) -> float:
    """0.8 term coverage + 0.1 log citations + 0.05 multi-source
    + 0.05 has-abstract. Used to order candidates shown to the model."""
    claim_terms = extract_terms(claim)
    coverage = max(
        [_term_coverage(claim_terms, candidate.get(f) or "")
         for f in ("abstract", "snippet", "title")] or [0.0]
    )
    cited = candidate.get("citation_count") or 0
    return (
        0.8 * coverage
        + 0.1 * min(1.0, math.log10(1 + cited) / 4)
        + 0.05 * (len(candidate.get("sources") or []) > 1)
        + 0.05 * bool(candidate.get("abstract"))
    )
