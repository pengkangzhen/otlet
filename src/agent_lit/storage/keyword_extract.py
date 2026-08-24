"""RAKE-style keyword extraction for auto-tagging without an LLM.

Free and fully offline: text is split into candidate phrases on stopwords,
each phrase is scored by the classic RAKE measure (sum of degree/frequency
over its member words), and the top phrases come back as kebab-case tags.
English stopwords are built in; CJK runs are handled as character bigrams
so Chinese titles and abstracts still yield tags instead of garbage.
"""

from __future__ import annotations

import re

# English function words plus generic academic filler ("we propose a method
# to model..." carries no tag signal). Substantive words like "model" or
# "network" stay out of this list on purpose.
_STOPWORDS = frozenset("""
a about above after again against all am an and any are as at be because been
before being below between both but by can could did do does doing down
during each few for from further had has have having he her here hers
herself him himself his how i if in into is it its itself just me more most
my myself no nor not of off on once only or other our ours ourselves out
over own same she should so some such than that the their theirs them
themselves then there these they this those through to too under until up
very was we were what when where which while who whom why will with you
your yours yourself yourselves
also among however moreover furthermore thus hence therefore via etc eg ie
paper papers propose proposed proposes presenting presented presents present
study studies method methods approach approaches based using use uses used
using results result show shows shown showing novel new recently
""".split())

_WORD_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


def extract_keywords(
    title: str, abstract: str | None = None, max_tags: int = 5
) -> list[str]:
    """Extract kebab-case keyword tags from a title (and abstract).

    The title is repeated three times so its phrases outrank abstract noise.
    """
    title = title or ""
    abstract = abstract or ""
    # Title repeated for weight; sentence punctuation separates the repeats
    text = ". ".join([title] * 3) + ". " + abstract
    lowered = text.lower()

    # English phrases: split on punctuation first, then on stopwords
    phrases: list[tuple[str, ...]] = []
    for segment in re.split(r"[^a-z0-9\s-]+", lowered):
        current: list[str] = []
        for tok in _WORD_RE.findall(segment):
            if tok in _STOPWORDS:
                if current:
                    phrases.append(tuple(current))
                    current = []
            else:
                current.append(tok)
        if current:
            phrases.append(tuple(current))

    # Long phrases (4+ words) can't be tags themselves; they contribute their
    # adjacent word pairs so "supply chain network design" still yields
    # supply-chain / network-design
    extra: list[tuple[str, ...]] = []
    for phrase in phrases:
        if len(phrase) > 3:
            pairs = [(phrase[i], phrase[i + 1]) for i in range(len(phrase) - 1)]
            extra.extend(pairs)
    phrases.extend(extra)

    # CJK bigrams act as words; adjacent overlapping bigrams are folded once
    # (供应+应链 → 供应链, capped at 3 chars) so tags look more like words.
    # Chinese extraction is best-effort without a tokenizer — for good
    # Chinese tags use a local Ollama model or a cloud LLM instead.
    def _fold(bigrams: list[str]) -> list[str]:
        out: list[str] = []
        for bg in bigrams:
            if out and out[-1][-1] == bg[0] and len(out[-1]) < 3:
                out[-1] = out[-1] + bg[-1]
            else:
                out.append(bg)
        return out

    cjk_counter: dict[str, int] = {}
    for run in _CJK_RUN_RE.findall(text):
        grams = [run[i : i + 2] for i in range(len(run) - 1)]
        for g in _fold(grams):
            cjk_counter[g] = cjk_counter.get(g, 0) + 1
    by_freq = sorted(cjk_counter.items(), key=lambda kv: -kv[1])
    cjk_tags = [g for g, _ in by_freq if cjk_counter[g] >= 2][:max_tags]

    # RAKE scoring: freq and degree per word, score = sum(degree/freq)
    freq: dict[str, int] = {}
    degree: dict[str, int] = {}
    phrase_freq: dict[tuple[str, ...], int] = {}
    for phrase in phrases:
        phrase_freq[phrase] = phrase_freq.get(phrase, 0) + 1
        for word in phrase:
            freq[word] = freq.get(word, 0) + 1
            degree[word] = degree.get(word, 0) + len(phrase) - 1
    for word in freq:
        degree[word] += freq[word]  # single-word phrases contribute deg = freq

    scored = []
    seen: set[str] = set()
    for phrase in phrases:
        if not (1 <= len(phrase) <= 3):
            continue
        if phrase_freq[phrase] < 2:
            continue  # one-off phrases are noise; repeats (title-weighted) rank
        if any(w.isdigit() for w in phrase):
            continue
        joined = "-".join(phrase)
        if len(joined) < 3 or joined in seen:
            continue
        seen.add(joined)
        score = sum(degree[w] / freq[w] for w in phrase)
        scored.append((score, joined))
    scored.sort(key=lambda t: -t[0])

    tags = [t for _, t in scored[:max_tags]]
    for g in cjk_tags:
        if len(tags) >= max_tags:
            break
        if g not in tags:
            tags.append(g)
    return tags
