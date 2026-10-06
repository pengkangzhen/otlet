"""Import papers directly from Zotero's local SQLite database."""

from __future__ import annotations

import re
import shutil
import sqlite3
import uuid
from pathlib import Path

from agent_lit.models.author import Author
from agent_lit.models.paper import Paper

# Default Zotero data directory
_DEFAULT_ZOTERO_DIR = Path.home() / "Zotero"

# Zotero item type → Agent-Lit paper_type
_TYPE_MAP = {
    "journalArticle": "journal",
    "conferencePaper": "conference",
    "book": "book",
    "bookSection": "book",
    "thesis": "thesis",
    "report": "report",
    "preprint": "preprint",
    "manuscript": "preprint",
    "document": "other",
    "magazineArticle": "other",
    "newspaperArticle": "other",
    "webpage": "other",
    "presentation": "other",
    "patent": "other",
}

# Fields we want to extract
_FIELDS_OF_INTEREST = {
    "title", "abstractNote", "DOI", "url", "date",
    "publicationTitle", "journalAbbreviation", "bookTitle",
    "proceedingsTitle", "conferenceName", "pages", "volume",
    "issue", "publisher", "ISBN", "ISSN", "language",
}

# Date field → year extraction
_YEAR_RE = re.compile(r"(\d{4})")


def find_zotero_db() -> Path | None:
    """Locate Zotero's zotero.sqlite. Returns None if not found."""
    candidate = _DEFAULT_ZOTERO_DIR / "zotero.sqlite"
    if candidate.exists():
        return candidate
    return None


def _open_zotero_db(path: Path) -> sqlite3.Connection:
    """Open a read-only copy of the Zotero database (avoids lock issues)."""
    tmp = Path(f"/tmp/_zotero_import_{uuid.uuid4().hex[:8]}.sqlite")
    shutil.copy2(path, tmp)
    conn = sqlite3.connect(str(tmp))
    conn.row_factory = sqlite3.Row
    return conn


def _cleanup_db(conn: sqlite3.Connection) -> None:
    """Close and remove the temporary copy."""
    path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    conn.close()
    path.unlink(missing_ok=True)


# ── Collections ───────────────────────────────────────

def list_collections(db_path: Path) -> list[dict]:
    """Return all non-deleted Zotero collections as [{id, name, parent_id, count}]."""
    conn = _open_zotero_db(db_path)
    try:
        rows = conn.execute("""
            SELECT c.collectionID, c.collectionName, c.parentCollectionID
            FROM collections c
            WHERE c.collectionID NOT IN (SELECT collectionID FROM deletedCollections)
            ORDER BY c.collectionName
        """).fetchall()

        result = []
        for r in rows:
            cid = r["collectionID"]
            count = conn.execute(
                "SELECT COUNT(*) FROM collectionItems WHERE collectionID = ?",
                (cid,),
            ).fetchone()[0]
            result.append({
                "id": cid,
                "name": r["collectionName"],
                "parent_id": r["parentCollectionID"],
                "count": count,
            })
        return result
    finally:
        _cleanup_db(conn)


# ── Full item import ──────────────────────────────────

def import_from_zotero(
    db_path: Path,
    zotero_dir: Path,
    collection_ids: list[int] | None = None,
    *,
    include_pdfs: bool = True,
) -> list[dict]:
    """Import items from Zotero.

    Args:
        db_path:     Path to zotero.sqlite (or temp copy)
        zotero_dir:  Zotero data directory (~/Zotero)
        collection_ids: If None → import all. Otherwise only these collections.
        include_pdfs:  Whether to copy PDF attachments.

    Returns:
        List of {"ok": bool, "paper": dict|None, "error": str|None}
    """
    conn = _open_zotero_db(db_path)
    try:
        # Gather item IDs
        if collection_ids:
            placeholders = ",".join("?" for _ in collection_ids)
            item_rows = conn.execute(f"""
                SELECT DISTINCT i.itemID, it.typeName, i.key
                FROM items i
                JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
                JOIN collectionItems ci ON i.itemID = ci.itemID
                WHERE ci.collectionID IN ({placeholders})
                  AND it.typeName NOT IN ('attachment', 'note', 'annotation')
                  AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
                ORDER BY i.itemID
            """, collection_ids).fetchall()
        else:
            item_rows = conn.execute("""
                SELECT i.itemID, it.typeName, i.key
                FROM items i
                JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
                WHERE it.typeName NOT IN ('attachment', 'note', 'annotation')
                  AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
                ORDER BY i.itemID
            """).fetchall()

        # Build field lookup: fieldName → fieldID
        field_map = {}
        placeholders = ",".join(repr(f) for f in _FIELDS_OF_INTEREST)
        for row in conn.execute(
            f"SELECT fieldID, fieldName FROM fields"
            f" WHERE fieldName IN ({placeholders})"
        ).fetchall():
            field_map[row["fieldName"]] = row["fieldID"]

        # Build value cache: (itemID, fieldID) → value
        val_cache: dict[tuple[int, int], str] = {}
        data_rows = conn.execute("""
            SELECT id.itemID, id.fieldID, idv.value
            FROM itemData id
            JOIN itemDataValues idv ON id.valueID = idv.valueID
        """).fetchall()
        for dr in data_rows:
            val_cache[(dr["itemID"], dr["fieldID"])] = dr["value"]

        # Build creator cache: itemID → [{firstName, lastName, creatorType}]
        creator_cache: dict[int, list[dict]] = {}
        creator_rows = conn.execute("""
            SELECT ic.itemID, c.firstName, c.lastName, ct.creatorType
            FROM itemCreators ic
            JOIN creators c ON ic.creatorID = c.creatorID
            JOIN creatorTypes ct ON ic.creatorTypeID = ct.creatorTypeID
            ORDER BY ic.itemID, ic.orderIndex
        """).fetchall()
        for cr in creator_rows:
            creator_cache.setdefault(cr["itemID"], []).append(dict(cr))

        # Build tag cache: itemID → [tagName]
        tag_cache: dict[int, list[str]] = {}
        tag_rows = conn.execute("""
            SELECT it.itemID, t.name
            FROM itemTags it JOIN tags t ON it.tagID = t.tagID
        """).fetchall()
        for tr in tag_rows:
            tag_cache.setdefault(tr["itemID"], []).append(tr["name"])

        # Build PDF attachment cache: itemID → relative path
        pdf_cache: dict[int, str] = {}
        if include_pdfs:
            pdf_rows = conn.execute("""
                SELECT ia.parentItemID, ia.path
                FROM itemAttachments ia
                WHERE ia.contentType = 'application/pdf'
                  AND ia.parentItemID IS NOT NULL
                  AND ia.itemID NOT IN (SELECT itemID FROM deletedItems)
            """).fetchall()
            for pr in pdf_rows:
                pid = pr["parentItemID"]
                # Keep first PDF if multiple
                if pid not in pdf_cache:
                    pdf_cache[pid] = pr["path"]

        # Convert each item
        results = []
        for item in item_rows:
            iid = item["itemID"]
            typename = item["typeName"]
            key = item["key"]

            # Gather field values
            def get_field(name: str) -> str | None:
                fid = field_map.get(name)
                if fid is None:
                    return None
                return val_cache.get((iid, fid))

            title = get_field("title") or "Untitled"

            # Authors
            authors = []
            for c in creator_cache.get(iid, []):
                first = (c["firstName"] or "").strip()
                last = (c["lastName"] or "").strip()
                if last and first:
                    name = f"{first} {last}"
                else:
                    name = last or first or "Unknown"
                authors.append(Author(name=name))

            # Year from date field
            date_str = get_field("date") or ""
            year = None
            m = _YEAR_RE.search(date_str)
            if m:
                year = int(m.group(1))

            # Venue
            venue = (
                get_field("publicationTitle")
                or get_field("journalAbbreviation")
                or get_field("conferenceName")
                or get_field("bookTitle")
                or get_field("proceedingsTitle")
            )

            # DOI cleanup
            doi = get_field("DOI")
            if doi:
                doi = doi.strip()

            paper = Paper(
                title=title,
                authors=authors,
                year=year,
                venue=venue,
                volume=get_field("volume"),
                issue=get_field("issue"),
                pages=get_field("pages"),
                publisher=get_field("publisher"),
                language=get_field("language"),
                doi=doi,
                url=get_field("url"),
                abstract=get_field("abstractNote"),
                keywords=[],
                tags=tag_cache.get(iid, []),
                bibtex_key=get_field("citationKey"),
                paper_type=_TYPE_MAP.get(typename, "other"),
            )

            # PDF path
            pdf_rel = pdf_cache.get(iid)
            pdf_abs = None
            if pdf_rel:
                # Zotero stores paths as "storage:filename.pdf"
                if pdf_rel.startswith("storage:"):
                    fname = pdf_rel[len("storage:"):]
                    candidate = zotero_dir / "storage" / key / fname
                    if candidate.exists():
                        pdf_abs = str(candidate)

            results.append({
                "ok": True,
                "paper": paper.model_dump(mode="json"),
                "pdf_path": pdf_abs,
                "zotero_type": typename,
            })

        return results
    finally:
        _cleanup_db(conn)
