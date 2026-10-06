"""Import pipelines shared by the GUI (web/api.py) and the CLI (cli.py).

Every entry point that adds papers to the library goes through this module,
so dedup rules, auto-tag extraction, and paper-type inference behave
identically no matter where the import was triggered from.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from otlet.models.author import Author
from otlet.models.paper import Paper
from otlet.storage.database import Database
from otlet.storage.pdf_index import PDFIndex
from otlet.storage.pdf_metadata import PDFMetadataExtractor
from otlet.storage.pdf_store import PDFStore

# ── Paper import flow ─────────────────────────────────────


def find_duplicate(
    db: Database, paper: Paper, *, fingerprint: str | None = None
) -> Paper | None:
    """Return the existing library paper matching `paper`, if any.

    Exact keys first (same rule everywhere): file fingerprint → DOI,
    then the fuzzy title match as the last resort.
    """
    dup = None
    if fingerprint:
        dup = db.get_paper_by_fingerprint(fingerprint)
    if not dup and paper.doi:
        dup = db.get_paper_by_doi(paper.doi)
    if not dup:
        dup = db.get_paper_by_title(paper.title)
    return dup


def import_pdf_file(
    db: Database,
    pdf_store: PDFStore,
    extractor: PDFMetadataExtractor,
    path: Path,
) -> dict:
    """Import one PDF file into the library.

    Returns a JSON-ready dict identical in shape across GUI and CLI:
      ok=True            → {"ok", "paper", "method", "confidence"}
      ok=False duplicate → {"ok", "duplicate", "existing_id", "attached"}
      ok=False           → {"ok", "error"}
    """
    if not path.exists():
        return {"ok": False, "error": f"File not found: {path}"}
    if path.suffix.lower() != ".pdf":
        return {"ok": False, "error": "Not a PDF file"}

    # One file read feeds everything: dedup fingerprint, metadata
    # extraction, and the stored copy
    data = path.read_bytes()
    fingerprint = hashlib.sha256(data).hexdigest()

    try:
        result = extractor.extract(path, data=data)
    except Exception as e:
        # unparseable/encrypted/corrupt PDF — one bad file must never
        # abort a batch import
        return {"ok": False, "error": f"unparseable PDF: {e}"}
    if result.paper is None:
        return {"ok": False, "error": "Could not identify paper"}

    paper = result.paper

    dup = find_duplicate(db, paper, fingerprint=fingerprint)
    if dup:
        # The dropped PDF is still valuable: attach it to the existing
        # paper when it has none, instead of discarding the file. A
        # paper without pdf_path has no stored copy by the storage
        # invariant, even when the fingerprint matched.
        attached = False
        if not dup.pdf_path:
            try:
                stored = pdf_store.import_bytes(data, paper_id=dup.id)
                db.update_paper(
                    dup.id,
                    pdf_path=str(stored),
                    pdf_fingerprint=fingerprint,
                )
                attached = True
            except Exception:
                pass
        if attached:
            try:
                PDFIndex(db, pdf_store).build(dup.id)
            except Exception:
                pass  # backfillable via `otlet index`
        return {
            "ok": False,
            "duplicate": True,
            "existing_id": dup.id,
            "attached": attached,
            "error": f"Already in library: {dup.title}",
        }

    # Infer paper type from venue if not already set
    if not paper.paper_type:
        paper.paper_type = infer_paper_type(paper.venue)

    # Auto-tag: extract from paper keywords + venue (kept out of manual tags)
    paper.auto_tags = extract_auto_tags(paper)

    try:
        stored = pdf_store.import_bytes(data, paper_id=paper.id)
        paper.pdf_path = str(stored)
        paper.pdf_fingerprint = fingerprint
        db.add_paper(paper)
    except Exception as e:
        return {"ok": False, "error": f"Save error: {e}"}

    # Full-text index right away — single imports are cheap to index;
    # failure is non-fatal (`otlet index` backfills)
    try:
        PDFIndex(db, pdf_store).build(paper.id)
    except Exception:
        pass

    return {
        "ok": True,
        "paper": paper.model_dump(mode="json"),
        "method": result.method,
        "confidence": result.confidence,
    }


def save_zotero_item(db: Database, pdf_store: PDFStore, item: dict) -> dict:
    """Save a single Zotero-scanned item (see zotero_import.import_from_zotero)."""
    if not item.get("ok") or not item.get("paper"):
        return {"ok": False, "error": "Invalid item"}

    paper = Paper(**item["paper"])
    fingerprint = None
    pdf_path = item.get("pdf_path")
    if pdf_path and Path(pdf_path).exists():
        fingerprint = hashlib.sha256(
            Path(pdf_path).read_bytes()
        ).hexdigest()
    dup = find_duplicate(db, paper, fingerprint=fingerprint)
    if dup:
        return {
            "ok": False,
            "duplicate": True,
            "existing_id": dup.id,
            "error": "Duplicate",
        }
    paper.auto_tags = extract_auto_tags(paper)
    if pdf_path:
        try:
            stored = pdf_store.import_file(Path(pdf_path), paper_id=paper.id)
            paper.pdf_path = str(stored)
            paper.pdf_fingerprint = fingerprint
        except Exception:
            pass  # PDF copy failure is non-fatal
    db.add_paper(paper)
    if paper.pdf_path:
        PDFIndex(db, pdf_store).build(paper.id)
    return {"ok": True, "id": paper.id}


# ── Auto-tag extraction (deterministic, import-time) ─────────


def extract_auto_tags(paper: Paper) -> list[str]:
    """Extract tags from paper metadata: keywords, venue, abstract."""
    tags: list[str] = []

    # 1. Paper keywords → direct tags
    for kw in paper.keywords:
        tag = _normalize_tag(kw)
        if tag and tag not in tags:
            tags.append(tag)

    # 2. Venue → high-level domain tag
    if paper.venue:
        venue_tag = _venue_to_tag(paper.venue)
        if venue_tag and venue_tag not in tags:
            tags.append(venue_tag)

    # 3. Abstract → extract key phrases via frequency heuristic
    if paper.abstract:
        abstract_tags = _extract_from_abstract(paper.abstract)
        for t in abstract_tags:
            if t not in tags:
                tags.append(t)

    return tags[:12]  # cap at 12 tags


def _normalize_tag(kw: str) -> str:
    """Normalize a keyword into a clean tag."""
    tag = kw.strip().lower()
    tag = tag.replace(" ", "-")
    # Remove very short or very long tags
    if len(tag) < 2 or len(tag) > 40:
        return ""
    return tag


# Well-known venue → domain mapping
_VENUE_MAP = {
    "neurips": "neural-networks",
    "nips": "neural-networks",
    "icml": "machine-learning",
    "iclr": "deep-learning",
    "aaai": "artificial-intelligence",
    "ijcai": "artificial-intelligence",
    "cvpr": "computer-vision",
    "iccv": "computer-vision",
    "eccv": "computer-vision",
    "acl": "natural-language-processing",
    "emnlp": "natural-language-processing",
    "naacl": "natural-language-processing",
    "sigkdd": "data-mining",
    "kdd": "data-mining",
    "www": "web-mining",
    "sigir": "information-retrieval",
    "icse": "software-engineering",
    "ase": "software-engineering",
    "ismir": "music-information-retrieval",
}


def _venue_to_tag(venue: str) -> str | None:
    """Map a venue name to a domain tag."""
    v = venue.lower().strip()
    for key, tag in _VENUE_MAP.items():
        if key in v:
            return tag
    return None


# Common academic phrases to ignore in abstract tag extraction
_IGNORE_WORDS = frozenset({
    "paper", "propose", "propose", "method", "approach", "result",
    "show", "use", "using", "used", "based", "propose", "novel",
    "propose", "also", "however", "study", "propose", "present",
    "proposed", "propose", "work", "propose", "provide", "introduce",
    "presented", "propose", "well", "two", "one", "new", "first",
    "second", "propose", "different", "several", "various", "given",
    "experimental", "experiments", "evaluation", "performance",
    "compare", "comparison", "state-of-the-art", "achieve", "obtained",
    "obtain", "demonstrate", "significant", "significantly",
    "effective", "efficient", "improve", "improvement",
})


def _extract_from_abstract(abstract: str) -> list[str]:
    """Extract potential tags from abstract via noun-phrase heuristic."""
    # Extract 2-3 word phrases that look like domain terms
    # Pattern: capitalized phrase or technical term
    phrases = re.findall(
        r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b', abstract
    )

    # Also extract common hyphenated terms
    hyphenated = re.findall(
        r'\b([a-z]+-[a-z]+(?:-[a-z]+)*)\b', abstract.lower()
    )

    tags = []
    for phrase in phrases:
        tag = phrase.lower().replace(" ", "-")
        words = tag.split("-")
        # Skip if any word is in ignore list or too common
        if any(w in _IGNORE_WORDS for w in words):
            continue
        if 3 <= len(tag) <= 35 and tag not in tags:
            tags.append(tag)

    for tag in hyphenated:
        if 3 <= len(tag) <= 35 and tag not in tags:
            tags.append(tag)

    return tags[:8]


# ── BibTeX Parsing ─────────────────────────────────────


def parse_bibtex(text: str) -> list[dict]:
    """Parse a BibTeX string into a list of entry dicts."""
    entries = []
    # Match @type{key, ... }
    for m in re.finditer(
        r"@(\w+)\s*\{\s*([^,\s]+)\s*,\s*(.*?)\n\s*\}",
        text,
        re.DOTALL,
    ):
        entry_type = m.group(1).lower()
        if entry_type in ("comment", "string", "preamble"):
            continue
        key = m.group(2)
        body = m.group(3)

        fields = {"_key": key, "_type": entry_type}
        # Parse field = {value} or field = "value" or field = number
        for fm in re.finditer(
            r"(\w+)\s*=\s*(?:\{(.*?)\}|\"(.*?)\"|(\S+))",
            body,
            re.DOTALL,
        ):
            fname = fm.group(1).lower()
            # Value is in one of the three capture groups
            fval = fm.group(2) or fm.group(3) or fm.group(4) or ""
            # Remove outer braces (nested brace handling)
            fval = fval.strip()
            # Un-escape BibTeX
            fval = fval.replace("\\&", "&")
            fields[fname] = fval

        if "title" in fields:
            entries.append(fields)
    return entries


def entry_to_paper(entry: dict) -> Paper:
    """Build a Paper (with auto tags) from one parse_bibtex entry."""
    paper = Paper(
        title=entry.get("title", "Untitled"),
        authors=bibtex_authors(entry.get("author", "")),
        year=bibtex_int(entry.get("year")),
        venue=entry.get("journal") or entry.get("booktitle"),
        volume=entry.get("volume"),
        issue=entry.get("number"),
        pages=entry.get("pages"),
        publisher=entry.get("publisher"),
        language=entry.get("language"),
        doi=entry.get("doi"),
        url=entry.get("url"),
        abstract=entry.get("abstract"),
        keywords=bibtex_keywords(entry.get("keywords", "")),
        bibtex_key=entry.get("_key"),
        paper_type=bibtex_type(entry.get("_type", "")),
    )
    paper.auto_tags = extract_auto_tags(paper)
    return paper


def bibtex_authors(author_str: str) -> list[Author]:
    """Parse 'Last, First and Last, First' into Author objects."""
    if not author_str:
        return []
    authors = []
    for part in re.split(r"\s+and\s+", author_str):
        part = part.strip()
        if not part:
            continue
        # "Last, First" → explicit first/last names for correct re-export
        if "," in part:
            segments = part.split(",", 1)
            last = segments[0].strip()
            first = segments[1].strip()
            authors.append(
                Author(
                    name=f"{first} {last}".strip(),
                    first_name=first,
                    last_name=last,
                )
            )
        else:
            authors.append(Author(name=part))
    return authors


def bibtex_int(val: str | None) -> int | None:
    if not val:
        return None
    m = re.search(r"\d{4}", val)
    return int(m.group()) if m else None


def bibtex_keywords(kw_str: str) -> list[str]:
    """Split 'kw1; kw2, kw3' into a clean list."""
    if not kw_str:
        return []
    # Semicolons first, then commas
    kws = re.split(r"[;,]", kw_str)
    return [k.strip() for k in kws if k.strip()]


# ── Paper Type Inference ──────────────────────────────

_BIBTEX_TYPE_MAP = {
    "article": "journal",
    "inproceedings": "conference",
    "conference": "conference",
    "proceedings": "conference",
    "book": "book",
    "incollection": "book",
    "phdthesis": "thesis",
    "mastersthesis": "thesis",
    "techreport": "report",
    "misc": "preprint",
    "unpublished": "preprint",
}


_VENUE_TYPE_PATTERNS = [
    (r"\b(?:conf(?:erence)?|proc(?:eedings)?|symposium|workshop)\b", "conference"),
    (r"\b(?:journal|trans(?:action)?s?|letters?|review)\b", "journal"),
    (r"\b(?:book|chapter|monograph)\b", "book"),
    (r"\b(?:thesis|dissertation)\b", "thesis"),
    (r"\b(?:arxiv|preprint)\b", "preprint"),
]


def bibtex_type(entry_type: str) -> str | None:
    """Map BibTeX entry type to paper_type."""
    return _BIBTEX_TYPE_MAP.get(entry_type.lower())


def infer_paper_type(venue: str | None) -> str | None:
    """Infer paper type from venue name using pattern matching."""
    if not venue:
        return None
    v = venue.lower()
    for pattern, ptype in _VENUE_TYPE_PATTERNS:
        if re.search(pattern, v):
            return ptype
    return None
