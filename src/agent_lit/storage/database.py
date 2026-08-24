"""SQLite-backed storage for papers, tags, conversations, and messages."""

import functools
import json
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Sequence

from agent_lit.models.author import Author
from agent_lit.models.paper import Paper
from agent_lit.models.tag import Tag

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS tags (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    parent_id   TEXT REFERENCES tags(id),
    color       TEXT,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS authors (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    first_name  TEXT DEFAULT '',
    last_name   TEXT DEFAULT '',
    affiliation TEXT,
    orcid       TEXT,
    email       TEXT
);

CREATE TABLE IF NOT EXISTS papers (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    year        INTEGER,
    venue       TEXT,
    volume      TEXT,
    issue       TEXT,
    pages       TEXT,
    publisher   TEXT,
    language    TEXT,
    doi         TEXT UNIQUE,
    url         TEXT,
    abstract    TEXT,
    keywords    TEXT DEFAULT '[]',
    citation_count INTEGER,
    added_date  TEXT DEFAULT (date('now')),
    pdf_path    TEXT,
    bibtex_key  TEXT,
    paper_type  TEXT,
    is_deleted  INTEGER DEFAULT 0,
    deleted_date TEXT
);
CREATE TABLE IF NOT EXISTS paper_tags (
    paper_id    TEXT REFERENCES papers(id) ON DELETE CASCADE,
    tag_id      TEXT REFERENCES tags(id) ON DELETE CASCADE,
    source      TEXT DEFAULT 'manual',   -- 'manual' | 'auto'
    PRIMARY KEY (paper_id, tag_id)
);

-- Notes (multiple timestamped notes per paper)
CREATE TABLE IF NOT EXISTS paper_notes (
    id          TEXT PRIMARY KEY,
    paper_id    TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS paper_authors (
    paper_id    TEXT REFERENCES papers(id) ON DELETE CASCADE,
    author_id   TEXT REFERENCES authors(id) ON DELETE CASCADE,
    position    INTEGER,
    PRIMARY KEY (paper_id, author_id)
);

CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    paper_id    TEXT REFERENCES papers(id) ON DELETE CASCADE,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    content         TEXT NOT NULL,
    created_at      TEXT DEFAULT (datetime('now'))
);

-- Multi-perspective tag system
CREATE TABLE IF NOT EXISTS perspectives (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    icon        TEXT DEFAULT '',
    sort_order  INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tag_groups (
    id              TEXT PRIMARY KEY,
    perspective_id  TEXT NOT NULL REFERENCES perspectives(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    sort_order      INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tag_group_members (
    group_id    TEXT NOT NULL REFERENCES tag_groups(id) ON DELETE CASCADE,
    tag_id      TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, tag_id)
);

CREATE TABLE IF NOT EXISTS group_papers (
    group_id  TEXT NOT NULL REFERENCES tag_groups(id) ON DELETE CASCADE,
    paper_id  TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, paper_id)
);
CREATE INDEX IF NOT EXISTS idx_group_papers_paper ON group_papers(paper_id);

CREATE INDEX IF NOT EXISTS idx_paper_tags_tag ON paper_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_paper_tags_paper ON paper_tags(paper_id);
CREATE INDEX IF NOT EXISTS idx_paper_notes_paper ON paper_notes(paper_id);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_conversations_paper ON conversations(paper_id);
CREATE INDEX IF NOT EXISTS idx_tag_groups_perspective ON tag_groups(perspective_id);
"""

# Migrations for existing databases
_MIGRATIONS = [
    "ALTER TABLE papers ADD COLUMN paper_type TEXT",
    "ALTER TABLE paper_tags ADD COLUMN source TEXT DEFAULT 'manual'",
    "ALTER TABLE papers ADD COLUMN is_deleted INTEGER DEFAULT 0",
    "ALTER TABLE papers ADD COLUMN deleted_date TEXT",
    "ALTER TABLE authors ADD COLUMN first_name TEXT DEFAULT ''",
    "ALTER TABLE authors ADD COLUMN last_name TEXT DEFAULT ''",
    "ALTER TABLE papers ADD COLUMN volume TEXT",
    "ALTER TABLE papers ADD COLUMN issue TEXT",
    "ALTER TABLE papers ADD COLUMN pages TEXT",
    "ALTER TABLE papers ADD COLUMN publisher TEXT",
    "ALTER TABLE papers ADD COLUMN language TEXT",
    "ALTER TABLE tags ADD COLUMN category TEXT",
]

# Research-facet classification for the project overview tag tree
_TAG_CATEGORIES = {"problem", "model", "algorithm"}


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _normalize_tag_name(name: str) -> str:
    """Normalize a tag name: lowercase, collapse whitespace, strip."""
    return re.sub(r"\s+", "-", name.strip()).lower()


def _locked(func):
    """Serialize access to the shared SQLite connection.

    pywebview executes bridge calls on worker threads, so every public
    Database method must hold the lock for its full read/write sequence.
    RLock allows nested calls between decorated methods.
    """
    @functools.wraps(func)
    def wrapper(self: "Database", *args, **kwargs):
        with self._lock:
            return func(self, *args, **kwargs)
    return wrapper


class Database:
    """Manages the SQLite database for agent-lit."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._lock = threading.RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(
                str(self._path), check_same_thread=False
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            # Tolerate a stale lock left behind by a crashed previous run
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
            # Run migrations (ignore errors if column already exists)
            for sql in _MIGRATIONS:
                try:
                    self._conn.execute(sql)
                except sqlite3.OperationalError:
                    pass
            # One-time: fold the legacy single notes field into paper_notes
            try:
                cols = [r[1] for r in self._conn.execute("PRAGMA table_info(papers)")]
                if "notes" in cols:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO paper_notes "
                        "(id, paper_id, content) "
                        "SELECT lower(hex(randomblob(6))), id, notes "
                        "FROM papers WHERE notes IS NOT NULL AND TRIM(notes) <> ''"
                    )
                    self._conn.execute("ALTER TABLE papers DROP COLUMN notes")
                    self._conn.commit()
            except sqlite3.OperationalError:
                pass
            # Merge duplicate tags on startup (only if explicitly requested)
            # self.merge_duplicate_tags()
        except sqlite3.Error as e:
            raise RuntimeError(
                f"SQLite error at {self._path}: {e}"
            ) from e

    @_locked
    def close(self) -> None:
        """Close the database, flushing WAL first. Safe to call twice."""
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self._conn.close()

    # ── Tags ──────────────────────────────────────────────────

    @_locked
    def create_tag(
        self,
        name: str,
        *,
        parent_id: str | None = None,
        color: str | None = None,
    ) -> Tag:
        norm = _normalize_tag_name(name)
        # Return existing tag if a normalized match already exists
        existing = self.get_tag_by_name(norm)
        if existing:
            return existing
        tag = Tag(id=_new_id(), name=norm, parent_id=parent_id, color=color)
        self._conn.execute(
            "INSERT INTO tags (id, name, parent_id, color) VALUES (?, ?, ?, ?)",
            (tag.id, tag.name, tag.parent_id, tag.color),
        )
        self._conn.commit()
        return tag

    @_locked
    def get_tag(self, tag_id: str) -> Tag | None:
        row = self._conn.execute(
            "SELECT * FROM tags WHERE id = ?", (tag_id,)
        ).fetchone()
        return Tag(**dict(row)) if row else None

    @_locked
    def get_tag_by_name(self, name: str) -> Tag | None:
        row = self._conn.execute(
            "SELECT * FROM tags WHERE name = ?", (name,)
        ).fetchone()
        return Tag(**dict(row)) if row else None

    @_locked
    def list_tags(self, include_auto: bool = False) -> list[Tag]:
        """Tags visible in the sidebar.

        Without include_auto: only tags with at least one manual link to a
        live paper, plus any tag placed in a group (curated by the user).
        Auto-only ungrouped tags are hidden to keep the sidebar clean.
        include_auto=True returns the full tag table.
        """
        if include_auto:
            rows = self._conn.execute("SELECT * FROM tags ORDER BY name").fetchall()
            return [Tag(**dict(r)) for r in rows]
        rows = self._conn.execute(
            """SELECT t.* FROM tags t
                WHERE EXISTS (
                    SELECT 1 FROM paper_tags pt
                    JOIN papers p ON p.id = pt.paper_id AND p.is_deleted = 0
                    WHERE pt.tag_id = t.id AND pt.source = 'manual'
                ) OR EXISTS (
                    SELECT 1 FROM tag_group_members m WHERE m.tag_id = t.id
                )
                ORDER BY name"""
        ).fetchall()
        return [Tag(**dict(r)) for r in rows]

    @_locked
    def delete_tag(self, tag_id: str) -> None:
        self._conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        self._conn.commit()

    @_locked
    def set_tag_category(self, tag_id: str, category: str | None) -> None:
        """Classify a tag as 'problem' / 'model' / 'algorithm'.

        An empty value clears the classification (back to uncategorized).
        """
        if category:
            category = category.strip().lower()
            if category not in _TAG_CATEGORIES:
                valid = ", ".join(sorted(_TAG_CATEGORIES))
                raise ValueError(
                    f"category must be one of {valid}, got {category!r}"
                )
        else:
            category = None
        self._conn.execute(
            "UPDATE tags SET category = ? WHERE id = ?", (category, tag_id)
        )
        self._conn.commit()

    # ── Papers ────────────────────────────────────────────────

    @_locked
    def add_paper(self, paper: Paper) -> Paper:
        if not paper.id:
            paper = paper.model_copy(update={"id": _new_id()})

        self._conn.execute(
            """INSERT OR IGNORE INTO papers
               (id, title, year, venue, volume, issue, pages, publisher,
                language, doi, url, abstract,
                keywords, citation_count, added_date,
                pdf_path, bibtex_key, paper_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                paper.id,
                paper.title,
                paper.year,
                paper.venue,
                paper.volume,
                paper.issue,
                paper.pages,
                paper.publisher,
                paper.language,
                paper.doi,
                paper.url,
                paper.abstract,
                json.dumps(paper.keywords),
                paper.citation_count,
                paper.added_date,
                paper.pdf_path,
                paper.bibtex_key,
                paper.paper_type,
            ),
        )

        # Authors
        for pos, author in enumerate(paper.authors):
            aid = self._upsert_author(author)
            self._conn.execute(
                "INSERT OR IGNORE INTO paper_authors (paper_id, author_id, position) "
                "VALUES (?, ?, ?)",
                (paper.id, aid, pos),
            )

        # Tags (by name → resolve to id); paper.tags are manual, auto_tags are auto
        for tag_name in paper.tags:
            self._link_tag_by_name(paper.id, tag_name, source="manual")
        for tag_name in paper.auto_tags:
            self._link_tag_by_name(paper.id, tag_name, source="auto")

        self._conn.commit()
        return paper

    @_locked
    def get_paper(self, paper_id: str) -> Paper | None:
        row = self._conn.execute(
            "SELECT * FROM papers WHERE id = ? AND is_deleted = 0", (paper_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_paper(row)

    @_locked
    def get_paper_by_doi(self, doi: str) -> Paper | None:
        row = self._conn.execute(
            "SELECT * FROM papers WHERE doi = ? AND is_deleted = 0", (doi,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_paper(row)

    @_locked
    def get_paper_by_title(self, title: str) -> Paper | None:
        """Fuzzy title match: lowercase, strip punctuation."""
        import unicodedata

        def _norm(s: str) -> str:
            s = s.lower().strip()
            s = "".join(
                c
                for c in unicodedata.normalize("NFKD", s)
                if c.isalnum() or c.isspace()
            )
            return s[:120]

        norm = _norm(title)
        rows = self._conn.execute(
            "SELECT * FROM papers WHERE is_deleted = 0"
        ).fetchall()
        for row in rows:
            if _norm(row["title"]) == norm:
                return self._row_to_paper(row)
        return None

    @_locked
    def update_paper(self, paper_id: str, **fields) -> None:
        """Update one or more paper fields."""
        allowed = {"title", "year", "venue", "volume", "issue", "pages",
                   "publisher", "language", "doi", "url", "abstract",
                   "paper_type", "pdf_path"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [paper_id]
        self._conn.execute(
            f"UPDATE papers SET {set_clause} WHERE id = ?", values
        )
        self._conn.commit()

    @_locked
    def rename_tag(self, old_name: str, new_name: str) -> None:
        """Rename a tag (normalized)."""
        norm_old = _normalize_tag_name(old_name)
        norm_new = _normalize_tag_name(new_name)
        if not norm_new:
            return
        # Check if target name already exists
        existing = self.get_tag_by_name(norm_new)
        if existing:
            # Merge: re-link papers from old to existing tag, delete old
            old_tag = self.get_tag_by_name(norm_old)
            if old_tag:
                self._conn.execute(
                    "INSERT OR IGNORE INTO paper_tags (paper_id, tag_id) "
                    "SELECT paper_id, ? FROM paper_tags WHERE tag_id = ?",
                    (existing.id, old_tag.id),
                )
                # Also re-parent children of old tag
                self._conn.execute(
                    "UPDATE tags SET parent_id = ? WHERE parent_id = ?",
                    (existing.id, old_tag.id),
                )
                self._conn.execute(
                    "DELETE FROM paper_tags WHERE tag_id = ?", (old_tag.id,)
                )
                self._conn.execute("DELETE FROM tags WHERE id = ?", (old_tag.id,))
        else:
            self._conn.execute(
                "UPDATE tags SET name = ? WHERE name = ?",
                (norm_new, norm_old),
            )
        self._conn.commit()

    @_locked
    def set_tag_parent(self, tag_name: str, parent_name: str | None) -> None:
        """Set a tag's parent (for hierarchy). None = top-level theme."""
        tag = self.get_tag_by_name(_normalize_tag_name(tag_name))
        if not tag:
            return
        parent_id = None
        if parent_name:
            parent = self.get_tag_by_name(_normalize_tag_name(parent_name))
            if not parent:
                parent = self.create_tag(parent_name)
            parent_id = parent.id
        # Prevent circular reference
        if parent_id and tag.id == parent_id:
            return
        self._conn.execute(
            "UPDATE tags SET parent_id = ? WHERE id = ?",
            (parent_id, tag.id),
        )
        self._conn.commit()

    @_locked
    def get_tag_tree(self) -> list[dict]:
        """Return tags as a tree structure for the UI."""
        rows = self._conn.execute("SELECT * FROM tags").fetchall()
        all_tags = []
        for r in rows:
            all_tags.append({
                "id": r["id"],
                "name": r["name"],
                "parent_id": r["parent_id"],
                "color": r["color"],
                "category": r["category"],
            })
        return all_tags

    # ── Perspectives ───────────────────────────────────────

    @_locked
    def list_perspectives(self) -> list[dict]:
        """Return all perspectives ordered by sort_order."""
        rows = self._conn.execute(
            "SELECT * FROM perspectives ORDER BY sort_order, created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    @_locked
    def create_perspective(self, name: str, icon: str = "") -> dict:
        """Create a new perspective."""
        pid = _new_id()
        self._conn.execute(
            "INSERT INTO perspectives (id, name, icon) VALUES (?, ?, ?)",
            (pid, name.strip(), icon),
        )
        self._conn.commit()
        return {"id": pid, "name": name.strip(), "icon": icon}

    @_locked
    def delete_perspective(self, perspective_id: str) -> None:
        """Delete a perspective and all its groups (cascade)."""
        self._conn.execute(
            "DELETE FROM perspectives WHERE id = ?", (perspective_id,)
        )
        self._conn.commit()

    @_locked
    def rename_perspective(self, perspective_id: str, name: str) -> None:
        self._conn.execute(
            "UPDATE perspectives SET name = ? WHERE id = ?",
            (name.strip(), perspective_id),
        )
        self._conn.commit()

    # ── Tag Groups ─────────────────────────────────────────

    @_locked
    def list_groups(self, perspective_id: str) -> list[dict]:
        """Return all groups for a perspective with tag counts."""
        rows = self._conn.execute(
            """SELECT g.*, COUNT(m.tag_id) as tag_count
               FROM tag_groups g
               LEFT JOIN tag_group_members m ON m.group_id = g.id
               WHERE g.perspective_id = ?
               GROUP BY g.id
               ORDER BY g.sort_order, g.created_at""",
            (perspective_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @_locked
    def create_group(
        self, perspective_id: str, name: str, sort_order: int = 0
    ) -> dict:
        gid = _new_id()
        self._conn.execute(
            "INSERT INTO tag_groups (id, perspective_id, name, sort_order) "
            "VALUES (?, ?, ?, ?)",
            (gid, perspective_id, name.strip(), sort_order),
        )
        self._conn.commit()
        return {"id": gid, "name": name.strip(), "perspective_id": perspective_id}

    @_locked
    def delete_group(self, group_id: str) -> None:
        self._conn.execute(
            "DELETE FROM tag_groups WHERE id = ?", (group_id,)
        )
        self._conn.commit()

    @_locked
    def rename_group(self, group_id: str, name: str) -> None:
        self._conn.execute(
            "UPDATE tag_groups SET name = ? WHERE id = ?",
            (name.strip(), group_id),
        )
        self._conn.commit()

    # ── Group Membership ───────────────────────────────────

    @_locked
    def add_tag_to_group(self, group_id: str, tag_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO tag_group_members (group_id, tag_id) "
            "VALUES (?, ?)",
            (group_id, tag_id),
        )
        self._conn.commit()

    @_locked
    def remove_tag_from_group(self, group_id: str, tag_id: str) -> None:
        self._conn.execute(
            "DELETE FROM tag_group_members WHERE group_id = ? AND tag_id = ?",
            (group_id, tag_id),
        )
        self._conn.commit()

    @_locked
    def get_perspective_view(
        self, perspective_id: str, include_auto: bool = False
    ) -> list[dict]:
        """Return groups with their tags for a perspective.

        Returns: [{group_id, group_name, tags: [{id, name, paper_count}]}]
        """
        source_clause = "" if include_auto else "AND pt.source = 'manual'"
        groups = self.list_groups(perspective_id)
        result = []
        for g in groups:
            tag_rows = self._conn.execute(
                f"""SELECT t.id, t.name, t.category, COUNT(p.id) as paper_count
                    FROM tag_group_members m
                    JOIN tags t ON t.id = m.tag_id
                    LEFT JOIN paper_tags pt ON pt.tag_id = t.id {source_clause}
                    LEFT JOIN papers p ON p.id = pt.paper_id AND p.is_deleted = 0
                    WHERE m.group_id = ?
                    GROUP BY t.id
                    ORDER BY paper_count DESC, t.name""",
                (g["id"],),
            ).fetchall()
            paper_rows = self._conn.execute(
                """SELECT gp.paper_id FROM group_papers gp
                   JOIN papers p ON p.id = gp.paper_id AND p.is_deleted = 0
                   WHERE gp.group_id = ?
                   ORDER BY p.rowid""",
                (g["id"],),
            ).fetchall()
            result.append({
                "group_id": g["id"],
                "group_name": g["name"],
                "tags": [dict(r) for r in tag_rows],
                "paper_ids": [r["paper_id"] for r in paper_rows],
            })
        return result

    @_locked
    def add_paper_to_group(self, group_id: str, paper_id: str) -> None:
        """File a paper directly into a project (Zotero collection membership)."""
        self._conn.execute(
            "INSERT OR IGNORE INTO group_papers (group_id, paper_id) VALUES (?, ?)",
            (group_id, paper_id),
        )
        self._conn.commit()

    @_locked
    def remove_paper_from_group(self, group_id: str, paper_id: str) -> None:
        self._conn.execute(
            "DELETE FROM group_papers WHERE group_id = ? AND paper_id = ?",
            (group_id, paper_id),
        )
        self._conn.commit()

    @_locked
    def get_group_papers(self, group_id: str) -> list[dict]:
        """Directly-filed papers of a group: [{paper_id, title}]."""
        rows = self._conn.execute(
            """SELECT gp.paper_id, p.title FROM group_papers gp
               JOIN papers p ON p.id = gp.paper_id AND p.is_deleted = 0
               WHERE gp.group_id = ?
               ORDER BY p.rowid""",
            (group_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @_locked
    def get_ungrouped_tags(
        self, perspective_id: str, include_auto: bool = False
    ) -> list[dict]:
        """Return tags not in any group of the given perspective."""
        source_clause = "" if include_auto else "AND pt2.source = 'manual'"
        rows = self._conn.execute(
            f"""SELECT t.id, t.name, t.category, COUNT(p.id) as paper_count
               FROM tags t
               LEFT JOIN paper_tags pt ON pt.tag_id = t.id
               LEFT JOIN papers p ON p.id = pt.paper_id AND p.is_deleted = 0
               WHERE t.id NOT IN (
                   SELECT m.tag_id FROM tag_group_members m
                   JOIN tag_groups g ON g.id = m.group_id
                   WHERE g.perspective_id = ?
               ) AND EXISTS (
                   SELECT 1 FROM paper_tags pt2
                   JOIN papers p2 ON p2.id = pt2.paper_id AND p2.is_deleted = 0
                   WHERE pt2.tag_id = t.id {source_clause}
               )
                   GROUP BY t.id
                   ORDER BY paper_count DESC, t.name""",
            (perspective_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @_locked
    def migrate_to_perspectives(self) -> str:
        """One-time migration: convert parent_id hierarchy to perspectives.

        Creates a 'Default' perspective with groups from the old theme tree.
        """
        # Check if migration already done
        existing = self._conn.execute(
            "SELECT COUNT(*) FROM perspectives"
        ).fetchone()[0]
        if existing > 0:
            return "Perspectives already exist, skipping migration"

        # Create default perspective
        pid = _new_id()
        self._conn.execute(
            "INSERT INTO perspectives (id, name, icon, sort_order) "
            "VALUES (?, 'Default', '📂', 0)",
            (pid,),
        )

        # For each tag with children (theme tags), create a group
        theme_rows = self._conn.execute(
            """SELECT DISTINCT parent.id, parent.name
               FROM tags parent
               JOIN tags child ON child.parent_id = parent.id
               ORDER BY parent.name"""
        ).fetchall()

        for theme in theme_rows:
            gid = _new_id()
            self._conn.execute(
                "INSERT INTO tag_groups (id, perspective_id, name) "
                "VALUES (?, ?, ?)",
                (gid, pid, theme["name"]),
            )
            # Add children to the group
            children = self._conn.execute(
                "SELECT id FROM tags WHERE parent_id = ?", (theme["id"],)
            ).fetchall()
            for child in children:
                self._conn.execute(
                    "INSERT OR IGNORE INTO tag_group_members "
                    "(group_id, tag_id) VALUES (?, ?)",
                    (gid, child["id"]),
                )

        self._conn.commit()
        return f"Created 'Default' perspective with {len(theme_rows)} groups"

    # ── Knowledge Graph ────────────────────────────────────

    @_locked
    def get_knowledge_graph(
        self,
        *,
        tag_filter: list[str] | None = None,
        min_papers: int = 1,
    ) -> dict:
        """Return nodes and edges for knowledge graph visualization.

        Returns: {"nodes": [...], "edges": [...]}
        Nodes: {id, label, type, size, extra}
        Edges: {source, target, type, weight}
        """
        nodes = []
        edges = []
        node_ids = set()

        # 1. Paper nodes
        paper_rows = self._conn.execute(
            "SELECT id, title, year, venue FROM papers WHERE is_deleted = 0"
        ).fetchall()

        # Filter papers by tag if specified
        if tag_filter:
            placeholders = ",".join("?" for _ in tag_filter)
            paper_rows = self._conn.execute(
                f"""SELECT DISTINCT p.id, p.title, p.year, p.venue
                    FROM papers p
                    JOIN paper_tags pt ON pt.paper_id = p.id
                    JOIN tags t ON pt.tag_id = t.id
                    WHERE t.name IN ({placeholders}) AND p.is_deleted = 0""",
                tag_filter,
            ).fetchall()

        for r in paper_rows:
            label = (r["title"] or "Untitled")[:40]
            if len(r["title"] or "") > 40:
                label += "…"
            nodes.append({
                "id": f"paper:{r['id']}",
                "label": label,
                "type": "paper",
                "size": 10,
                "extra": {
                    "title": r["title"],
                    "year": r["year"],
                    "venue": r["venue"],
                    "paper_id": r["id"],
                },
            })
            node_ids.add(f"paper:{r['id']}")

        paper_id_list = [r["id"] for r in paper_rows]

        # 2. Tag nodes (connected to filtered papers)
        if paper_id_list:
            placeholders = ",".join("?" for _ in paper_id_list)
            tag_rows = self._conn.execute(
                f"""SELECT t.id, t.name, COUNT(pt.paper_id) as paper_count
                    FROM tags t
                    JOIN paper_tags pt ON pt.tag_id = t.id
                    WHERE pt.paper_id IN ({placeholders})
                    GROUP BY t.id
                    HAVING paper_count >= ?
                    ORDER BY paper_count DESC""",
                paper_id_list + [min_papers],
            ).fetchall()

            for t in tag_rows:
                nodes.append({
                    "id": f"tag:{t['id']}",
                    "label": t["name"],
                    "type": "tag",
                    "size": min(5 + t["paper_count"] * 2, 30),
                    "extra": {"name": t["name"], "paper_count": t["paper_count"]},
                })
                node_ids.add(f"tag:{t['id']}")

            # 3. Paper-Tag edges
            pt_rows = self._conn.execute(
                f"""SELECT pt.paper_id, pt.tag_id
                    FROM paper_tags pt
                    WHERE pt.paper_id IN ({placeholders})""",
                paper_id_list,
            ).fetchall()
            for r in pt_rows:
                pid, tid = f"paper:{r['paper_id']}", f"tag:{r['tag_id']}"
                if pid in node_ids and tid in node_ids:
                    edges.append({
                        "source": pid, "target": tid,
                        "type": "tagged", "weight": 1,
                    })

        # 4. Author nodes
        if paper_id_list:
            placeholders = ",".join("?" for _ in paper_id_list)
            author_rows = self._conn.execute(
                f"""SELECT a.id, a.name, COUNT(pa.paper_id) as paper_count
                    FROM authors a
                    JOIN paper_authors pa ON pa.author_id = a.id
                    WHERE pa.paper_id IN ({placeholders})
                    GROUP BY a.id
                    HAVING paper_count >= ?
                    ORDER BY paper_count DESC
                    LIMIT 50""",
                paper_id_list + [min_papers],
            ).fetchall()

            for a in author_rows:
                nodes.append({
                    "id": f"author:{a['id']}",
                    "label": a["name"],
                    "type": "author",
                    "size": min(5 + a["paper_count"] * 3, 25),
                    "extra": {"name": a["name"], "paper_count": a["paper_count"]},
                })
                node_ids.add(f"author:{a['id']}")

            # 5. Paper-Author edges
            pa_rows = self._conn.execute(
                f"""SELECT pa.paper_id, pa.author_id
                    FROM paper_authors pa
                    WHERE pa.paper_id IN ({placeholders})""",
                paper_id_list,
            ).fetchall()
            for r in pa_rows:
                pid = f"paper:{r['paper_id']}"
                aid = f"author:{r['author_id']}"
                if pid in node_ids and aid in node_ids:
                    edges.append({
                        "source": pid, "target": aid,
                        "type": "authored", "weight": 1,
                    })

        # 6. Tag-Tag co-occurrence edges
        if len(tag_rows) > 1:
            tag_ids = [t["id"] for t in tag_rows]
            tag_placeholders = ",".join("?" for _ in tag_ids)
            cooc_rows = self._conn.execute(
                f"""SELECT pt1.tag_id as t1, pt2.tag_id as t2,
                       COUNT(*) as weight
                    FROM paper_tags pt1
                    JOIN paper_tags pt2 ON pt1.paper_id = pt2.paper_id
                    WHERE pt1.tag_id IN ({tag_placeholders})
                      AND pt2.tag_id IN ({tag_placeholders})
                      AND pt1.tag_id < pt2.tag_id
                    GROUP BY pt1.tag_id, pt2.tag_id
                    HAVING weight >= 2
                    ORDER BY weight DESC
                    LIMIT 100""",
                tag_ids + tag_ids,
            ).fetchall()
            for r in cooc_rows:
                n1, n2 = f"tag:{r['t1']}", f"tag:{r['t2']}"
                if n1 in node_ids and n2 in node_ids:
                    edges.append({
                        "source": n1, "target": n2,
                        "type": "cooccurrence", "weight": r["weight"],
                    })

        return {"nodes": nodes, "edges": edges}

    @_locked
    def list_papers(self) -> list[Paper]:
        rows = self._conn.execute(
            "SELECT * FROM papers WHERE is_deleted = 0 ORDER BY added_date DESC"
        ).fetchall()
        return [self._row_to_paper(r) for r in rows]

    @_locked
    def list_trash(self) -> list[Paper]:
        """Papers in the trash (soft-deleted), newest deletion first."""
        rows = self._conn.execute(
            "SELECT * FROM papers WHERE is_deleted = 1 "
            "ORDER BY deleted_date DESC, added_date DESC"
        ).fetchall()
        return [self._row_to_paper(r) for r in rows]

    @_locked
    def find_papers_by_tags(
        self, tag_names: Sequence[str], *, match_all: bool = True
    ) -> list[Paper]:
        """Filter papers by tags (includes children of theme tags)."""
        if not tag_names:
            return self.list_papers()

        # Expand tag_names to include children
        expanded = set(tag_names)
        for name in tag_names:
            tag = self.get_tag_by_name(name)
            if tag:
                children = self._conn.execute(
                    "SELECT name FROM tags WHERE parent_id = ?", (tag.id,)
                ).fetchall()
                for c in children:
                    expanded.add(c["name"])

        tag_list = list(expanded)
        placeholders = ",".join("?" for _ in tag_list)
        having = (
            f"HAVING COUNT(DISTINCT t.name) = {len(tag_names)}"
            if match_all
            else ""
        )

        rows = self._conn.execute(
            f"""SELECT p.* FROM papers p
                JOIN paper_tags pt ON p.id = pt.paper_id
                JOIN tags t ON pt.tag_id = t.id
                WHERE t.name IN ({placeholders}) AND p.is_deleted = 0
                GROUP BY p.id
                {having}
                ORDER BY p.added_date DESC""",
            tag_list,
        ).fetchall()
        return [self._row_to_paper(r) for r in rows]

    @_locked
    def search_papers(self, query: str) -> list[Paper]:
        """Full-text search across title, abstract, and venue."""
        pattern = f"%{query}%"
        rows = self._conn.execute(
            """SELECT * FROM papers
               WHERE is_deleted = 0
                 AND (title LIKE ? OR abstract LIKE ? OR venue LIKE ?)
               ORDER BY added_date DESC""",
            (pattern, pattern, pattern),
        ).fetchall()
        return [self._row_to_paper(r) for r in rows]

    @_locked
    def delete_paper(self, paper_id: str) -> None:
        """Move a paper to the trash (soft delete)."""
        self._conn.execute(
            "UPDATE papers SET is_deleted = 1, deleted_date = date('now') "
            "WHERE id = ? AND is_deleted = 0",
            (paper_id,),
        )
        self._conn.commit()

    @_locked
    def restore_paper(self, paper_id: str) -> None:
        """Restore a paper from the trash."""
        self._conn.execute(
            "UPDATE papers SET is_deleted = 0, deleted_date = NULL WHERE id = ?",
            (paper_id,),
        )
        self._conn.commit()

    @_locked
    def purge_paper(self, paper_id: str) -> None:
        """Permanently delete a paper row (links cascade).

        Does not touch the stored PDF file.
        """
        self._conn.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
        self._conn.commit()

    @_locked
    def empty_trash(self) -> int:
        """Permanently delete all trashed papers. Returns count purged."""
        cur = self._conn.execute("DELETE FROM papers WHERE is_deleted = 1")
        self._conn.commit()
        return cur.rowcount

    # ── Notes ──────────────────────────────────────────────────

    @_locked
    def list_notes(self, paper_id: str) -> list[dict]:
        """All notes of a paper in insertion order (rowid beats the
        second-granularity created_at when notes land in the same second)."""
        rows = self._conn.execute(
            "SELECT id, content, created_at, updated_at FROM paper_notes "
            "WHERE paper_id = ? ORDER BY rowid",
            (paper_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @_locked
    def add_note(self, paper_id: str, content: str) -> dict:
        note_id = _new_id()
        self._conn.execute(
            "INSERT INTO paper_notes (id, paper_id, content) VALUES (?, ?, ?)",
            (note_id, paper_id, content),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id, content, created_at, updated_at FROM paper_notes "
            "WHERE id = ?",
            (note_id,),
        ).fetchone()
        return dict(row)

    @_locked
    def update_note(self, note_id: str, content: str) -> None:
        self._conn.execute(
            "UPDATE paper_notes SET content = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (content, note_id),
        )
        self._conn.commit()

    @_locked
    def delete_note(self, note_id: str) -> None:
        self._conn.execute("DELETE FROM paper_notes WHERE id = ?", (note_id,))
        self._conn.commit()

    @_locked
    def set_paper_authors(self, paper_id: str, authors: Sequence[Author]) -> None:
        """Replace a paper's author list (used by the detail editor)."""
        self._conn.execute(
            "DELETE FROM paper_authors WHERE paper_id = ?", (paper_id,)
        )
        for pos, author in enumerate(authors):
            aid = self._upsert_author(author)
            self._conn.execute(
                "INSERT OR IGNORE INTO paper_authors "
                "(paper_id, author_id, position) VALUES (?, ?, ?)",
                (paper_id, aid, pos),
            )
        self._conn.commit()

    @_locked
    def update_paper_pdf(self, paper_id: str, pdf_path: str) -> None:
        self._conn.execute(
            "UPDATE papers SET pdf_path = ? WHERE id = ?",
            (pdf_path, paper_id),
        )
        self._conn.commit()

    @_locked
    def add_tag_to_paper(
        self, paper_id: str, tag_name: str, source: str = "manual"
    ) -> None:
        self._link_tag_by_name(paper_id, tag_name, source=source)
        self._conn.commit()

    @_locked
    def mark_tag_source(
        self, paper_id: str, tag_names: Sequence[str], source: str
    ) -> int:
        """Set source on a paper's existing links. Returns rows changed."""
        if not tag_names:
            return 0
        placeholders = ",".join("?" for _ in tag_names)
        cur = self._conn.execute(
            f"""UPDATE paper_tags SET source = ?
                WHERE paper_id = ? AND tag_id IN (
                    SELECT id FROM tags WHERE name IN ({placeholders}))""",
            (source, paper_id, *tag_names),
        )
        self._conn.commit()
        return cur.rowcount

    @_locked
    def remove_tag_from_paper(self, paper_id: str, tag_name: str) -> None:
        tag = self.get_tag_by_name(tag_name)
        if tag:
            self._conn.execute(
                "DELETE FROM paper_tags WHERE paper_id = ? AND tag_id = ?",
                (paper_id, tag.id),
            )
            self._conn.commit()

    # ── Conversations & Messages ──────────────────────────────

    @_locked
    def create_conversation(self, paper_id: str) -> str:
        conv_id = _new_id()
        self._conn.execute(
            "INSERT INTO conversations (id, paper_id) VALUES (?, ?)",
            (conv_id, paper_id),
        )
        self._conn.commit()
        return conv_id

    @_locked
    def get_conversation(self, paper_id: str) -> str | None:
        """Return the latest conversation ID for a paper, or None."""
        row = self._conn.execute(
            "SELECT id FROM conversations WHERE paper_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (paper_id,),
        ).fetchone()
        return row["id"] if row else None

    @_locked
    def add_message(self, conversation_id: str, role: str, content: str) -> None:
        self._conn.execute(
            "INSERT INTO messages (id, conversation_id, role, content) "
            "VALUES (?, ?, ?, ?)",
            (_new_id(), conversation_id, role, content),
        )
        self._conn.commit()

    @_locked
    def get_messages(self, conversation_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT role, content, created_at FROM messages "
            "WHERE conversation_id = ? ORDER BY created_at",
            (conversation_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Internal helpers ──────────────────────────────────────

    def _upsert_author(self, author: Author) -> str:
        """Return the author's id, inserting only if truly new.

        Matches by ORCID first, then by full name so the same person
        resolves to one row (keeps knowledge-graph author nodes unique).
        """
        if author.orcid:
            row = self._conn.execute(
                "SELECT id FROM authors WHERE orcid = ?", (author.orcid,)
            ).fetchone()
            if row:
                return row["id"]

        row = self._conn.execute(
            "SELECT id FROM authors WHERE name = ?", (author.name,)
        ).fetchone()
        if row:
            return row["id"]

        aid = _new_id()
        self._conn.execute(
            "INSERT OR IGNORE INTO authors "
            "(id, name, first_name, last_name, affiliation, orcid, email) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                aid,
                author.name,
                author.first_name,
                author.last_name,
                author.affiliation,
                author.orcid,
                author.email,
            ),
        )
        return aid

    @_locked
    def get_papers_with_dois(self) -> list[tuple[str, str]]:
        """(paper_id, doi) for every live paper that has a DOI."""
        rows = self._conn.execute(
            "SELECT id, doi FROM papers "
            "WHERE is_deleted = 0 AND doi IS NOT NULL AND doi != ''"
        ).fetchall()
        return [(r["id"], r["doi"]) for r in rows]

    @_locked
    def set_paper_author_affiliations(
        self, paper_id: str, affiliations: dict[str, str]
    ) -> int:
        """Fill author affiliations for one paper, matched by author name.

        Only fills authors whose affiliation is still empty — user edits
        are never overwritten. Returns the number of authors updated.
        """
        updated = 0
        for name, aff in affiliations.items():
            aff = (aff or "").strip()
            if not aff:
                continue
            cur = self._conn.execute(
                """UPDATE authors SET affiliation = ?
                   WHERE (affiliation IS NULL OR affiliation = '')
                     AND id IN (
                       SELECT author_id FROM paper_authors WHERE paper_id = ?
                     )
                     AND name = ?""",
                (aff, paper_id, name),
            )
            updated += cur.rowcount
        self._conn.commit()
        return updated

    def _link_tag_by_name(
        self, paper_id: str, tag_name: str, source: str = "manual"
    ) -> None:
        """Ensure tag exists (normalized) and link it to a paper."""
        norm = _normalize_tag_name(tag_name)
        if not norm:
            return
        tag = self.get_tag_by_name(norm)
        if not tag:
            tag = self.create_tag(norm)
        self._conn.execute(
            "INSERT OR IGNORE INTO paper_tags (paper_id, tag_id, source) "
            "VALUES (?, ?, ?)",
            (paper_id, tag.id, source),
        )

    @_locked
    def merge_duplicate_tags(self) -> int:
        """Merge tags that are identical after normalization. Returns merge count."""
        rows = self._conn.execute("SELECT id, name FROM tags").fetchall()
        # Group by normalized name
        groups: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            norm = _normalize_tag_name(r["name"])
            groups.setdefault(norm, []).append(r)

        merged = 0
        for _norm, members in groups.items():
            if len(members) <= 1:
                continue
            # Keep the first (or the one that's already normalized), remove others
            keeper = members[0]
            for dup in members[1:]:
                # Re-link all papers from dup → keeper
                self._conn.execute(
                    "INSERT OR IGNORE INTO paper_tags (paper_id, tag_id) "
                    "SELECT paper_id, ? FROM paper_tags WHERE tag_id = ?",
                    (keeper["id"], dup["id"]),
                )
                # Remove dup links and the tag itself
                self._conn.execute(
                    "DELETE FROM paper_tags WHERE tag_id = ?", (dup["id"],)
                )
                self._conn.execute(
                    "DELETE FROM tags WHERE id = ?", (dup["id"],)
                )
                merged += 1
        self._conn.commit()
        return merged

    def _row_to_paper(self, row: sqlite3.Row) -> Paper:
        d = dict(row)
        d["keywords"] = json.loads(d.get("keywords", "[]"))

        # Load tags, split by source
        tag_rows = self._conn.execute(
            "SELECT t.name, pt.source FROM tags t "
            "JOIN paper_tags pt ON t.id = pt.tag_id "
            "WHERE pt.paper_id = ?",
            (d["id"],),
        ).fetchall()
        d["tags"] = [r["name"] for r in tag_rows if r["source"] == "manual"]
        d["auto_tags"] = [r["name"] for r in tag_rows if r["source"] == "auto"]

        # Load authors
        author_rows = self._conn.execute(
            "SELECT a.* FROM authors a "
            "JOIN paper_authors pa ON a.id = pa.author_id "
            "WHERE pa.paper_id = ? ORDER BY pa.position",
            (d["id"],),
        ).fetchall()
        d["authors"] = [
            Author(
                name=r["name"],
                first_name=r["first_name"] or "",
                last_name=r["last_name"] or "",
                affiliation=r["affiliation"],
                orcid=r["orcid"],
                email=r["email"],
            )
            for r in author_rows
        ]

        return Paper(**d)
