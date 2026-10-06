"""Tests for the offline RAKE-style keyword extractor."""

import re

from otlet.storage.keyword_extract import extract_keywords

ABSTRACT = (
    "We propose a two-stage stochastic programming model for supply chain "
    "network design under demand uncertainty. The model is solved with a "
    "Benders decomposition algorithm and evaluated on supply chain instances "
    "from the literature."
)


def test_english_abstract_yields_domain_phrases():
    tags = extract_keywords(
        "Resilient supply chain network design under risk", ABSTRACT
    )
    assert isinstance(tags, list) and tags
    # Multi-word domain phrases survive as hyphenated tags
    assert "supply-chain" in tags
    assert all(t == t.lower() for t in tags)
    assert all(" " not in t for t in tags)


def test_title_repetition_outranks_abstract_noise():
    tags = extract_keywords("Empty container repositioning", ABSTRACT)
    assert tags, "title-only input should still produce tags"
    assert "empty-container-repositioning" in tags


def test_empty_input_returns_empty():
    assert extract_keywords("", "") == []
    assert extract_keywords(None, None) == []


def test_only_stopwords_returns_empty():
    assert extract_keywords("a study of the method we propose", "") == []


def test_chinese_input_returns_cjk_tags_without_crashing():
    # Best-effort without a tokenizer: bigram/trigram units, not real words
    tags = extract_keywords(
        "供应链韧性风险评估模型研究", "本文研究供应链韧性，提出供应链韧性评估模型。"
    )
    assert isinstance(tags, list)
    assert tags, "repeated CJK runs should yield at least one tag"
    assert all(re.search(r"[\u4e00-\u9fff]", t) for t in tags)


def test_max_tags_is_respected():
    tags = extract_keywords(
        "Alpha beta gamma delta epsilon zeta eta theta network optimization",
        "alpha beta gamma delta epsilon zeta eta theta optimization network",
        max_tags=3,
    )
    assert len(tags) <= 3
