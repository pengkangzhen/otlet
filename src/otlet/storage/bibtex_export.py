"""Generate BibTeX (.bib) exports from the library."""

import re

from agent_lit.models.author import Author
from agent_lit.models.paper import Paper

# paper_type → BibTeX entry type
_TYPE_TO_ENTRY = {
    "journal": "article",
    "conference": "inproceedings",
    "book": "book",
    "thesis": "phdthesis",
    "report": "techreport",
    "preprint": "misc",
}


def _escape(value: str) -> str:
    """Escape characters that are special in BibTeX field values."""
    return (
        value.replace("\\", "")
        .replace("&", r"\&")
        .replace("#", r"\#")
        .replace("%", r"\%")
    )


def _format_author(author: Author) -> str:
    """'First Last' → 'Last, First' (BibTeX convention)."""
    if author.first_name and author.last_name:
        return f"{author.last_name}, {author.first_name}"
    parts = author.name.split()
    if len(parts) >= 2:
        return f"{parts[-1]}, {' '.join(parts[:-1])}"
    return author.name


def _make_key(paper: Paper, used: set[str]) -> str:
    """Build a unique citation key: lastnameYEARfirstword or bibtex_key."""
    if paper.bibtex_key:
        base = re.sub(r"[^A-Za-z0-9_-]", "", paper.bibtex_key)
    else:
        if paper.authors:
            last = paper.authors[0].name.split()[-1].lower()
            last = re.sub(r"[^a-z0-9]", "", last)
        else:
            last = "anon"
        year = paper.year if paper.year else "nd"
        words = re.findall(r"[A-Za-z]+", paper.title or "")
        first = words[0].lower() if words else "paper"
        base = f"{last}{year}{first}"
    if not base:
        base = "paper"

    key, i = base, 2
    while key in used:
        key = f"{base}{i}"
        i += 1
    used.add(key)
    return key


def _entry_type(paper: Paper) -> str:
    return _TYPE_TO_ENTRY.get(paper.paper_type or "", "misc")


def paper_to_bibtex(paper: Paper, key: str) -> str:
    """Render one Paper as a BibTeX entry string."""
    fields: list[tuple[str, str]] = []

    if paper.authors:
        names = " and ".join(_format_author(a) for a in paper.authors)
        fields.append(("author", _escape(names)))
    fields.append(("title", _escape(paper.title)))
    if paper.year:
        fields.append(("year", str(paper.year)))
    if paper.venue:
        venue_field = "journal" if _entry_type(paper) == "article" else "booktitle"
        fields.append((venue_field, _escape(paper.venue)))
    if paper.volume:
        fields.append(("volume", _escape(paper.volume)))
    if paper.issue:
        fields.append(("number", _escape(paper.issue)))
    if paper.pages:
        fields.append(("pages", _escape(paper.pages)))
    if paper.publisher:
        fields.append(("publisher", _escape(paper.publisher)))
    if paper.doi:
        fields.append(("doi", paper.doi))
    if paper.url:
        fields.append(("url", paper.url))
    keywords = list(paper.keywords)
    for tag in paper.tags:
        if tag not in keywords:
            keywords.append(tag)
    if keywords:
        fields.append(("keywords", _escape("; ".join(keywords))))
    if paper.abstract:
        fields.append(("abstract", _escape(" ".join(paper.abstract.split()))))

    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields)
    return f"@{_entry_type(paper)}{{{key},\n{body}\n}}"


def generate_bibtex(papers: list[Paper]) -> str:
    """Render a full .bib document from papers, with unique citation keys."""
    used: set[str] = set()
    entries = [paper_to_bibtex(p, _make_key(p, used)) for p in papers]
    return "\n\n".join(entries) + "\n"
